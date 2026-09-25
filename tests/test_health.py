import copy
import unittest

from urban_network.health import DEFAULT_CONFIG, evaluate, input_fingerprint, validate_config
from urban_network.models import Reading, Segment, as_dict
from urban_network.service import NetworkService
from datetime import datetime, timezone


def _seg(sid="S1", district="east", criticality=4, install_year=2000, material="steel"):
    return Segment(sid, district, "water", 100, criticality, install_year=install_year, material=material)


def _readings(sid, pressures, stamp="2026-01-0{0}T00:00:00+00:00"):
    return [Reading(f"{sid}-R{i}", sid, "sensor", p, 200.0, 60.0, stamp.format(i)) for i, p in enumerate(pressures, 1)]


class HealthScoringTests(unittest.TestCase):
    def setUp(self):
        self.as_of = datetime(2026, 1, 10, tzinfo=timezone.utc)

    def test_weights_sum_to_score_and_bands(self):
        segment = as_dict(_seg(install_year=1996))  # 30 年钢质管 -> 腐蚀因子满值
        result = evaluate(segment, _readings("S1", [350] * 6), [], DEFAULT_CONFIG, self.as_of)
        contributions = result["contributions"]
        total = sum(c["points"] for c in contributions.values())
        self.assertAlmostEqual(total, result["score"], places=4)
        self.assertAlmostEqual(contributions["corrosion"]["factor"], 1.0, places=6)
        self.assertFalse(result["uncertain"])
        self.assertIn(result["risk_band"], {"low", "medium", "high", "critical"})

    def test_missing_factor_renormalizes_weights(self):
        result = evaluate(as_dict(_seg(install_year=None)), _readings("S1", [350, 360]), [], DEFAULT_CONFIG, self.as_of)
        self.assertEqual(result["contributions"]["corrosion"]["weight"], 0.0)
        weights = [c["weight"] for k, c in result["contributions"].items() if k != "corrosion"]
        self.assertAlmostEqual(sum(weights), 1.0, places=4)
        self.assertIn("missing-install-year", result["reasons"])

    def test_insufficient_samples_marked_uncertain_not_ranked_precision(self):
        few = evaluate(as_dict(_seg()), _readings("S1", [350, 300]), [], DEFAULT_CONFIG, self.as_of)
        self.assertTrue(few["uncertain"])
        self.assertEqual(few["confidence"], "low")
        self.assertIn("insufficient-readings", few["reasons"])
        enough = evaluate(as_dict(_seg()), _readings("S1", [350] * 5), [], DEFAULT_CONFIG, self.as_of)
        self.assertFalse(enough["uncertain"])

    def test_validate_config_rejects_bad_weights_and_bands(self):
        bad = copy.deepcopy(DEFAULT_CONFIG)
        bad["weights"]["corrosion"] = -1
        with self.assertRaises(ValueError):
            validate_config(bad)
        bad = copy.deepcopy(DEFAULT_CONFIG); bad["risk_bands"] = []
        with self.assertRaises(ValueError):
            validate_config(bad)
        with self.assertRaises(ValueError):
            validate_config({"weights": {"nope": 1.0}})

    def test_fingerprint_stable_and_distinct(self):
        snapshot = {"a": 1, "b": [2, 3]}
        self.assertEqual(input_fingerprint(snapshot), input_fingerprint({"b": [2, 3], "a": 1}))
        self.assertNotEqual(input_fingerprint(snapshot), input_fingerprint({"a": 1, "b": [2, 4]}))


class HealthReportServiceTests(unittest.TestCase):
    def setUp(self):
        self.s = NetworkService(); self.s.bootstrap()
        self.t = self.s.auth.login("admin", "network-admin")
        self.s.register_segment(self.t, _seg("OLD", "north", 5, 1995, "cast_iron"))
        self.s.register_segment(self.t, _seg("NEW", "north", 2, 2024, "pe"))
        self.s.register_segment(self.t, _seg("EAST1", "east", 4, 2010, "steel"))
        for r in _readings("OLD", [350, 250, 450, 200, 480, 220]):
            self.ingested = self.s.ingest_reading(self.t, r)
        for r in _readings("EAST1", [350, 351, 349, 350, 352]):
            self.s.ingest_reading(self.t, r)
        self.s.ingest_reading(self.t, Reading("NEW-R1", "NEW", "sensor", 350, 200, 60, "2026-01-01T00:00:00+00:00"))

    def _completed_order(self, sid):
        alert = self.s.db.execute("SELECT alert_id FROM alerts WHERE segment_id=?", (sid,)).fetchone()
        order = self.s.create_work_order(self.t, sid, alert[0], "crew", 2)
        wid = order["work_order_id"]
        for target, reason in (("assigned", "ok"), ("in_progress", "ok"), ("completed", "done")):
            self.s.transition_work_order(self.t, wid, target, reason)
        return wid

    def test_report_freezes_rule_input_and_contributions(self):
        wid = self._completed_order("OLD")
        report = self.s.generate_health_report(self.t, as_of=None)
        self.assertEqual(report["rule_id"], "baseline-health")
        self.assertEqual(len(report["rule_fingerprint"]), 64)
        self.assertEqual(len(report["input_fingerprint"]), 64)
        old_entry = next(x for x in report["segments"] if x["segment_id"] == "OLD")
        self.assertEqual(set(old_entry["contributions"]), {"corrosion", "repairs", "pressure", "criticality"})
        self.assertGreaterEqual(old_entry["contributions"]["repairs"]["factor"], 0.2)
        before = {x["segment_id"]: (x["score"], x["reasons"]) for x in report["segments"]}
        fp_before = report["input_fingerprint"]

        # 补录读数 + 发布新规则：旧报告不得改变。
        self.s.ingest_reading(self.t, Reading("NEW-R2", "NEW", "sensor", 520, 200, 60, "2026-01-05T00:00:00+00:00"))
        cfg = copy.deepcopy(DEFAULT_CONFIG); cfg["weights"] = {"corrosion": 0.5, "repairs": 0.1, "pressure": 0.3, "criticality": 0.1}
        self.s.publish_health_rule(self.t, "baseline-health", cfg, "2026-10-01T00:00:00+00:00")
        frozen = self.s.health_report(self.t, report["report_id"])
        self.assertEqual(frozen["input_fingerprint"], fp_before)
        self.assertEqual(frozen["rule_version"], 1)
        self.assertEqual({x["segment_id"]: (x["score"], x["reasons"]) for x in frozen["segments"]}, before)

        # 新规则只作用于新报告。
        latest = self.s.generate_health_report(self.t, as_of="2026-10-02T00:00:00+00:00")
        self.assertEqual(latest["rule_version"], 2)
        self.assertNotEqual(latest["input_fingerprint"], fp_before)

    def test_district_and_risk_band_filters(self):
        report = self.s.generate_health_report(self.t, as_of=None)
        rid = report["report_id"]
        north = self.s.health_report(self.t, rid, district="north")
        self.assertEqual({x["segment_id"] for x in north["segments"]}, {"OLD", "NEW"})
        band = next(x["risk_band"] for x in report["segments"] if x["segment_id"] == "OLD")
        banded = self.s.health_report(self.t, rid, risk_band=band)
        self.assertTrue(all(x["risk_band"] == band for x in banded["segments"]))
        ranged = self.s.health_report(self.t, rid, min_score=0, max_score=40)
        self.assertTrue(all(x["score"] <= 40 for x in ranged["segments"]))

    def test_uncertain_segments_have_no_rank_and_trace_to_inputs(self):
        wid = self._completed_order("OLD")
        report = self.s.generate_health_report(self.t, as_of=None)
        entries = {x["segment_id"]: x for x in report["segments"]}
        self.assertIsNone(entries["NEW"]["rank_position"])
        self.assertTrue(entries["NEW"]["uncertain"])
        self.assertIsNotNone(entries["OLD"]["rank_position"])
        self.assertEqual(report["uncertain_count"], 1)

        trace = self.s.health_report_trace(self.t, report["report_id"], "OLD")
        self.assertTrue(trace["fingerprint_verified"])
        self.assertEqual(len(trace["readings"]), 6)
        self.assertEqual([w["work_order_id"] for w in trace["work_orders"]], [wid])

    def test_report_generation_scoped_to_district_snapshot(self):
        report = self.s.generate_health_report(self.t, district="east", as_of=None)
        self.assertEqual(report["district_scope"], "east")
        self.assertEqual([x["segment_id"] for x in report["segments"]], ["EAST1"])
        with self.assertRaises(KeyError):
            self.s.health_report_trace(self.t, report["report_id"], "OLD")

    def test_rule_publishing_requires_approval_and_is_versioned(self):
        token = self.s.auth.login("operator", "network-operator")
        with self.assertRaises(PermissionError):
            self.s.publish_health_rule(token, "x", DEFAULT_CONFIG, "2026-03-01T00:00:00+00:00")
        v2 = self.s.publish_health_rule(self.t, "baseline-health", DEFAULT_CONFIG, "2026-03-01T00:00:00+00:00")
        self.assertEqual(v2["version"], 2)
        v3 = self.s.publish_health_rule(self.t, "other-rule", DEFAULT_CONFIG, "2026-03-01T00:00:00+00:00")
        self.assertEqual(v3["version"], 1)


if __name__ == "__main__":
    unittest.main()

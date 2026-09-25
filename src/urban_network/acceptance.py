"""离线命令行验收入口。"""
from __future__ import annotations
import argparse,json,copy
from .health import DEFAULT_CONFIG
from .models import Reading,Segment
from .service import NetworkService

def _readings(segment_id,pressures):
    stamp=lambda i: f"2026-09-20T{i:02d}:00:00+00:00"
    return [Reading(f"{segment_id}-R{i+1}",segment_id,"sensor-01",p,200.0,60.0,stamp(i)) for i,p in enumerate(pressures)]

def run(workspace="."):
    s=NetworkService(); s.bootstrap(); t=s.auth.login("admin","network-admin")
    # 老铸铁管（腐蚀重）样本充足；新 PE 管样本不足，报告必须标记不确定性。
    s.register_segment(t,Segment("SEG-OLD","north","water",680,5,install_year=1998,material="cast_iron"))
    s.register_segment(t,Segment("SEG-NEW","north","water",420,2,install_year=2024,material="pe"))
    s.register_segment(t,Segment("SEG-EAST","east","gas",900,4,install_year=2010,material="steel"))
    alert_id=None
    for r in _readings("SEG-OLD",[350,300,250,400,200,450]):
        result=s.ingest_reading(t,r); alert_id=result.get("alert_id") or alert_id
    for r in _readings("SEG-EAST",[350,352,348,351,349,350]): s.ingest_reading(t,r)
    s.ingest_reading(t,Reading("SEG-NEW-R1","SEG-NEW","sensor-02",350,200,60,"2026-09-20T00:00:00+00:00"))
    order=s.create_work_order(t,"SEG-OLD",alert_id,"crew-north",1)
    s.transition_work_order(t,order["work_order_id"],"assigned","crew accepted")
    s.transition_work_order(t,order["work_order_id"],"in_progress","repair started")
    s.transition_work_order(t,order["work_order_id"],"completed","repair finished")
    s.add_resource(t,"PUMP-01","mobile-pump","north",2)
    allocation=s.allocate(t,"PUMP-01",order["work_order_id"],1)

    report=s.generate_health_report(t)
    before={"fp":report["input_fingerprint"],"rule":report["rule_version"],"segments":{x["segment_id"]:(x["score"],x["uncertain"],x["reasons"]) for x in report["segments"]}}

    # 事后补录读数并发布新版本规则：旧报告必须保持冻结。
    s.ingest_reading(t,Reading("SEG-NEW-R2","SEG-NEW","sensor-02",520,200,60,"2026-09-20T12:00:00+00:00"))
    new_config=copy.deepcopy(DEFAULT_CONFIG); new_config["weights"]={"corrosion":0.40,"repairs":0.10,"pressure":0.35,"criticality":0.15}
    s.publish_health_rule(t,"baseline-health",new_config,"2026-10-01T00:00:00+00:00")
    frozen=s.health_report(t,report["report_id"])
    assert frozen["input_fingerprint"]==before["fp"] and frozen["rule_version"]==before["rule"], "旧报告被补录数据或新规则改写"
    assert frozen["segments"] and all((x["score"],x["uncertain"],x["reasons"])==before["segments"][x["segment_id"]] for x in frozen["segments"])

    # 按片区与风险区间筛选；样本不足的管段没有精确名次。
    north_high=s.health_report(t,report["report_id"],district="north",min_score=0,max_score=100)
    new_entry=next(x for x in frozen["segments"] if x["segment_id"]=="SEG-NEW")
    old_entry=next(x for x in frozen["segments"] if x["segment_id"]=="SEG-OLD")
    assert new_entry["uncertain"] and "insufficient-readings" in new_entry["reasons"] and new_entry["rank_position"] is None
    assert (not old_entry["uncertain"]) and old_entry["rank_position"] is not None

    # 沿报告追溯到冻结时使用的工单与读数。
    trace=s.health_report_trace(t,report["report_id"],"SEG-OLD")
    assert trace["fingerprint_verified"] and len(trace["readings"])==6 and trace["work_orders"][0]["work_order_id"]==order["work_order_id"]

    # 新规则只影响新报告。
    latest=s.generate_health_report(t,as_of="2026-10-02T00:00:00+00:00")
    return {"status":"ok","report_id":report["report_id"],"rule_version":report["rule_version"],"input_fingerprint":report["input_fingerprint"],"segments":len(frozen["segments"]),"uncertain":frozen["uncertain_count"],"north_returned":north_high["returned_count"],"trace_readings":len(trace["readings"]),"frozen_after_backfill":frozen["input_fingerprint"]==before["fp"],"latest_rule_version":latest["rule_version"],"allocation":allocation["allocation_id"]}
def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--workspace",default="."); parser.parse_args(); print(json.dumps(run(),ensure_ascii=False))
if __name__=="__main__":main()

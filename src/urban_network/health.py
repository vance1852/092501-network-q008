"""管段健康指数：可配置、带生效时间的多因子评分。

健康评分不是只看最近一次泄漏告警，而是综合四个因子：

- corrosion   腐蚀年限（按安装年份、材质腐蚀速率归一化）
- repairs     历史维修（时间窗内已完成工单数量）
- pressure    压力波动（压力读数总体标准差）
- criticality 关键设施等级（1-5 级）

规则为带版本号和生效时间的配置对象（见 ``DEFAULT_CONFIG``）。样本不足或
关键属性缺失时，评分结果必须显式标注不确定性，且不参与精确排名。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import pstdev
from typing import Any

from .models import parse_time

FACTORS = ("corrosion", "repairs", "pressure", "criticality")

DEFAULT_CONFIG: dict[str, Any] = {
    "weights": {"corrosion": 0.30, "repairs": 0.20, "pressure": 0.30, "criticality": 0.20},
    "corrosion_full_scale_years": 30.0,
    "material_rates": {"steel": 1.0, "cast_iron": 1.4, "pe": 0.4, "pvc": 0.4},
    "pressure_full_scale_kpa": 120.0,
    "repair_lookback_days": 1095,
    "repair_full_scale_count": 5,
    "min_readings": 5,
    "risk_bands": [
        {"name": "low", "min_inclusive": 0.0, "max_exclusive": 25.0},
        {"name": "medium", "min_inclusive": 25.0, "max_exclusive": 50.0},
        {"name": "high", "min_inclusive": 50.0, "max_exclusive": 75.0},
        {"name": "critical", "min_inclusive": 75.0, "max_exclusive": 100.01},
    ],
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def rule_fingerprint(rule_id: str, version: int, effective_at: str, config: dict[str, Any]) -> str:
    payload = canonical_json(
        {"rule_id": rule_id, "version": version, "effective_at": effective_at, "config": config}
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def input_fingerprint(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()


def validate_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ValueError("scoring rule config must be an object")
    weights = config.get("weights")
    if not isinstance(weights, dict) or not weights:
        raise ValueError("scoring rule requires non-empty weights")
    unknown = set(weights) - set(FACTORS)
    if unknown:
        raise ValueError(f"unknown scoring factors: {sorted(unknown)}")
    for key, value in weights.items():
        if not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"weight for {key} must be a non-negative number")
    if sum(float(v) for v in weights.values()) <= 0:
        raise ValueError("weights must sum to a positive number")
    for key in ("corrosion_full_scale_years", "pressure_full_scale_kpa", "repair_full_scale_count"):
        if not isinstance(config.get(key), (int, float)) or config[key] <= 0:
            raise ValueError(f"{key} must be a positive number")
    if not isinstance(config.get("repair_lookback_days"), int) or config["repair_lookback_days"] <= 0:
        raise ValueError("repair_lookback_days must be a positive integer")
    if not isinstance(config.get("min_readings"), int) or config["min_readings"] <= 0:
        raise ValueError("min_readings must be a positive integer")
    bands = config.get("risk_bands")
    if not isinstance(bands, list) or not bands:
        raise ValueError("risk_bands must be a non-empty list")
    cursor = 0.0
    for band in bands:
        lo, hi = band.get("min_inclusive"), band.get("max_exclusive")
        if not isinstance(band.get("name"), str) or not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
            raise ValueError("each risk band needs a name and numeric bounds")
        if lo < cursor or hi <= lo:
            raise ValueError("risk bands must be ordered and non-overlapping")
        cursor = hi
    if cursor < 100.0:
        raise ValueError("risk bands must cover scores up to 100")
    return config


def risk_band(score: float, bands: list[dict[str, Any]]) -> str:
    for band in bands:
        if band["min_inclusive"] <= score < band["max_exclusive"]:
            return band["name"]
    return bands[-1]["name"]


@dataclass(frozen=True)
class FactorInput:
    raw: float | None
    weight: float
    points: float


def _field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _corrosion_raw(segment, config: dict[str, Any], as_of: datetime) -> float | None:
    install_year = _field(segment, "install_year")
    if install_year is None:
        return None
    install = datetime(int(install_year), 1, 1, tzinfo=as_of.tzinfo)
    age_years = max(0.0, (as_of - install).total_seconds()) / (365.2425 * 86400.0)
    rate = float(config.get("material_rates", {}).get(_field(segment, "material") or "steel", 1.0))
    return min(1.0, max(0.0, age_years * rate / float(config["corrosion_full_scale_years"])))


def evaluate(
    segment: dict[str, Any],
    readings: list[dict[str, Any]],
    work_orders: list[dict[str, Any]],
    config: dict[str, Any],
    as_of: datetime,
) -> dict[str, Any]:
    """计算单个管段的健康评分，返回因子贡献、样本数与不确定性原因。

    数据缺失的因子不参与评分，其权重在可用因子间重新归一化，并记录原因。
    """
    reasons: list[str] = []
    sample_count = len(readings)
    if sample_count < int(config["min_readings"]):
        reasons.append("insufficient-readings")

    corrosion_raw = _corrosion_raw(segment, config, as_of)
    if corrosion_raw is None:
        reasons.append("missing-install-year")

    pressure_raw: float | None = None
    if sample_count >= 2:
        pressure_raw = min(
            1.0,
            pstdev(float(_field(r, "pressure_kpa")) for r in readings)
            / float(config["pressure_full_scale_kpa"]),
        )

    cutoff = as_of - timedelta(days=int(config["repair_lookback_days"]))
    repairs = [
        w
        for w in work_orders
        if _field(w, "status") == "completed" and cutoff <= parse_time(_field(w, "created_at")) <= as_of
    ]
    repairs_raw = min(1.0, len(repairs) / float(config["repair_full_scale_count"]))
    criticality_raw = float(_field(segment, "criticality")) / 5.0

    raws = {
        "corrosion": corrosion_raw,
        "repairs": repairs_raw,
        "pressure": pressure_raw,
        "criticality": criticality_raw,
    }
    weights = config["weights"]
    available_weight = sum(float(weights[f]) for f in FACTORS if raws[f] is not None)

    contributions: dict[str, dict[str, Any]] = {}
    score = 0.0
    for factor in FACTORS:
        raw = raws[factor]
        if raw is None:
            weight = points = 0.0
        else:
            weight = float(weights[factor]) / available_weight
            points = 100.0 * weight * raw
            score += points
        contributions[factor] = {
            "factor": None if raw is None else round(raw, 6),
            "weight": round(weight, 6),
            "points": round(points, 4),
        }
    score = round(score, 4)

    return {
        "score": score,
        "risk_band": risk_band(score, config["risk_bands"]),
        "uncertain": bool(reasons),
        "confidence": "low" if reasons else "high",
        "reasons": tuple(reasons),
        "contributions": contributions,
        "samples": {"readings": sample_count, "repairs": len(repairs)},
        "repair_orders": repairs,
    }

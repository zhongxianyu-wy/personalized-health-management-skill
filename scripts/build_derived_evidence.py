#!/usr/bin/env python3
"""Generate derived log-odds evidence caches from raw JSON evidence."""

from __future__ import annotations

import sys as _sys
_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import _env_bootstrap  # noqa: F401 — 跨runtime环境自检(PYTHONHOME/UTF-8)

import argparse
import json
import math
from pathlib import Path
from typing import Any

import yaml


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _midpoint(low: float | None, high: float | None, *, ratio: bool) -> float | None:
    if low is None or high is None:
        return None
    if ratio:
        if low <= 0 or high <= 0:
            return None
        return math.sqrt(low * high)
    return (low + high) / 2


def _calculation_value(record: dict[str, Any], *, ratio: bool) -> float | None:
    value = record.get("effect_value")
    if value is not None:
        return float(value)
    return _midpoint(record.get("ci_low"), record.get("ci_high"), ratio=ratio)


def _normalize_probability(value: Any) -> float | None:
    if value is None:
        return None
    value = float(value)
    if value > 1:
        value = value / 100
    return value


def _standardize_probability_for_log_odds(value: Any) -> tuple[float | None, float | None, bool]:
    raw = _normalize_probability(value)
    if raw is None:
        return None, None, False
    if raw <= 0:
        return raw, 0.01, True
    if raw >= 1:
        return raw, 0.99, True
    return raw, raw, False


def build_derived_evidence(store: Path, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_assertions = _read_json(store / "risk_assertions.json")["assertions"]
    raw_detection = _read_json(store / "detection_performance.json")["tests"]

    derived_assertions = []
    for assertion in raw_assertions:
        effect_type = assertion.get("effect_type")
        approximation = effect_type in {"RR", "HR"}
        calculation_value = _calculation_value(assertion, ratio=True)
        usable = effect_type in {"OR", "RR", "HR"} and calculation_value is not None and calculation_value > 0
        conversion_status = "usable" if usable else "explanatory_only"
        derived_assertions.append({
            "assertion_id": assertion["assertion_id"],
            "assertion_key": "|".join([
                assertion["cancer_id"],
                assertion["factor_id"],
                str(assertion.get("factor_level") or "unknown"),
                assertion["assertion_id"],
            ]),
            "cancer_id": assertion["cancer_id"],
            "factor_id": assertion["factor_id"],
            "factor_level": assertion.get("factor_level"),
            "effect_type": effect_type,
            "calculation_value": calculation_value,
            "log_odds_delta": math.log(calculation_value) if usable else None,
            "conversion_rule": (
                f"ln({effect_type})" if effect_type == "OR" and usable
                else f"ln({effect_type})_approximation" if usable
                else "not_admitted"
            ),
            "conversion_status": conversion_status,
            "approximation": approximation,
            "source_id": assertion.get("source_id"),
        })

    derived_detection = []
    for entry in raw_detection:
        raw_sensitivity, sensitivity, sensitivity_adjusted = _standardize_probability_for_log_odds(entry.get("sensitivity"))
        raw_specificity, specificity, specificity_adjusted = _standardize_probability_for_log_odds(entry.get("specificity"))
        usable = (
            sensitivity is not None
            and specificity is not None
            and 0 < sensitivity < 1
            and 0 < specificity < 1
        )
        if usable:
            lr_positive = sensitivity / (1 - specificity)
            lr_negative = (1 - sensitivity) / specificity
            positive_delta = math.log(lr_positive)
            negative_delta = math.log(lr_negative)
            counterinformative = lr_positive < 1 or lr_negative > 1
            reason = "counterinformative_likelihood_ratio" if counterinformative else None
            usable = not counterinformative
        else:
            lr_positive = lr_negative = positive_delta = negative_delta = None
            reason = "missing_or_boundary_sensitivity_specificity"
        derived_detection.append({
            "test_id": entry["test_id"],
            "cancer_id": entry["cancer_id"],
            "raw_sensitivity": raw_sensitivity,
            "raw_specificity": raw_specificity,
            "sensitivity": sensitivity,
            "specificity": specificity,
            "probability_boundary_adjustment": sensitivity_adjusted or specificity_adjusted,
            "lr_positive": lr_positive,
            "lr_negative": lr_negative,
            "positive_log_odds_delta": positive_delta,
            "negative_log_odds_delta": negative_delta,
            "conversion_status": "usable" if usable else "unusable",
            "conversion_rule": "LR+/LR- from sensitivity/specificity" if usable else "not_admitted",
            "unusable_reason": reason,
            "source_id": entry.get("source_id"),
        })

    return (
        {"derived_assertions": derived_assertions},
        {"derived_detection_performance": derived_detection},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-store", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    store = Path(args.evidence_store)
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    risk, detection = build_derived_evidence(store, config)
    (store / "risk_assertions_derived.json").write_text(
        json.dumps(risk, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (store / "detection_performance_derived.json").write_text(
        json.dumps(detection, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "[build_derived_evidence] "
        f"risk={len(risk['derived_assertions'])} "
        f"detection={len(detection['derived_detection_performance'])}"
    )


if __name__ == "__main__":
    main()

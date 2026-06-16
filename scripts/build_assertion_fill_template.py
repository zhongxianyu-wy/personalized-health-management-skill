#!/usr/bin/env python3
"""Build slim file-level risk-factor fill templates for Task 4.

This is a knowledge-base preparation step. It deduplicates raw risk
assertions into one immutable record per ``factor_id|factor_level``. At
runtime the skill LLM may only fill file context and factual presence fields;
script-derived ``risk_evidence_values`` must be preserved unchanged.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "risk-factor-file-fill-v3.3"

LLM_FILL_TEMPLATE = {
    "exists": "unknown",
    "exam_date": None,
    "evidence_text": None,
    "source_file": None,
    "source_section": None,
    "source_page": None,
    "negated": False,
    "confidence": 0.0,
    "raw_field_description": None,
}


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _applies_to_sex(applicable: str | None, sex: str | None) -> bool:
    if not sex:
        return True
    return (applicable or "all") in {"all", sex}


def _factor_key(factor_id: str, factor_level: str | None) -> str:
    return "|".join([factor_id, str(factor_level or "unknown")])


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ratio_calculation_value(record: dict[str, Any]) -> tuple[float | None, str]:
    value = _as_float(record.get("effect_value"))
    if value is not None:
        return value, "point_estimate"
    low = _as_float(record.get("ci_low") or record.get("effect_value_low"))
    high = _as_float(record.get("ci_high") or record.get("effect_value_high"))
    if low is not None and high is not None and low > 0 and high > 0:
        return math.sqrt(low * high), "interval_geometric_midpoint"
    return None, "missing_effect_value"


def _normalize_probability(value: Any) -> float | None:
    value = _as_float(value)
    if value is None:
        return None
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


def _standardize_risk_evidence(assertion: dict[str, Any]) -> dict[str, Any]:
    """Derive Bayes-ready values while preserving the raw assertion trace."""
    effect_type = str(assertion.get("effect_type") or "").upper()
    source_id = assertion.get("source_id")
    base = {
        "assertion_id": assertion.get("assertion_id"),
        "effect_type": effect_type or None,
        "source_id": source_id,
        "evidence_grade": assertion.get("evidence_grade"),
        "ci_low": assertion.get("ci_low"),
        "ci_high": assertion.get("ci_high"),
        "calculation_value": None,
        "log_odds_delta": None,
        "conversion_rule": "not_admitted",
        "conversion_status": "unusable",
        "approximation": False,
        "unusable_reason": None,
    }

    if effect_type in {"OR", "RR", "HR"}:
        calculation_value, value_source = _ratio_calculation_value(assertion)
        base["calculation_value"] = calculation_value
        if calculation_value is None or calculation_value <= 0:
            base["unusable_reason"] = value_source
            return base
        base["log_odds_delta"] = math.log(calculation_value)
        base["conversion_status"] = "usable"
        base["approximation"] = effect_type in {"RR", "HR"}
        if effect_type == "OR":
            base["conversion_rule"] = "ln_or" if value_source == "point_estimate" else "ln_or_interval_geometric_midpoint"
        else:
            base["conversion_rule"] = f"ln_{effect_type.lower()}_approximation"
        return base

    raw_sensitivity, sensitivity, sensitivity_adjusted = _standardize_probability_for_log_odds(assertion.get("sensitivity"))
    raw_specificity, specificity, specificity_adjusted = _standardize_probability_for_log_odds(assertion.get("specificity"))
    if raw_sensitivity is not None or raw_specificity is not None:
        base["raw_sensitivity"] = raw_sensitivity
        base["raw_specificity"] = raw_specificity
        base["sensitivity"] = sensitivity
        base["specificity"] = specificity
        base["probability_boundary_adjustment"] = sensitivity_adjusted or specificity_adjusted
        usable = (
            sensitivity is not None
            and specificity is not None
            and 0 < sensitivity < 1
            and 0 < specificity < 1
        )
        if not usable:
            base["unusable_reason"] = "missing_or_boundary_sensitivity_specificity"
            return base
        lr_positive = sensitivity / (1 - specificity)
        lr_negative = (1 - sensitivity) / specificity
        base.update({
            "calculation_value": {"lr_positive": lr_positive, "lr_negative": lr_negative},
            "lr_positive": lr_positive,
            "lr_negative": lr_negative,
            "positive_log_odds_delta": math.log(lr_positive),
            "negative_log_odds_delta": math.log(lr_negative),
            "conversion_rule": "LR+/LR- from sensitivity/specificity",
            "conversion_status": "usable",
        })
        return base

    base["conversion_status"] = "explanatory_only"
    base["unusable_reason"] = "no_supported_numeric_risk_metric"
    return base


def build_assertion_fill_template(
    store: Path,
    sex: str | None = None,
    age: int | None = None,
    enabled_cancers: set[str] | None = None,
) -> dict[str, Any]:
    """Return a slim file-level factor/level template payload."""
    cancers = _read(store / "cancers.json")["cancers"]
    factors = _read(store / "risk_factors.json")["risk_factors"]
    assertions = _read(store / "risk_assertions.json")["assertions"]
    observables = _read(store / "factor_observables.json").get("factors", {})
    synonyms = _read(store / "factor_synonyms.json").get("synonyms", {})
    version = _read(store / "evidence_version.json").get("version")

    cancer_by_id = {c["cancer_id"]: c for c in cancers}
    factor_by_id = {f["factor_id"]: f for f in factors}
    if enabled_cancers is None:
        enabled_cancers = {c["cancer_id"] for c in cancers if _applies_to_sex(c.get("applicable_sex"), sex)}
    else:
        enabled_cancers = {
            cid for cid in enabled_cancers
            if cid in cancer_by_id and _applies_to_sex(cancer_by_id[cid].get("applicable_sex"), sex)
        }

    by_key: dict[str, dict[str, Any]] = {}
    for assertion in assertions:
        cancer_id = assertion["cancer_id"]
        factor_id = assertion["factor_id"]
        factor = factor_by_id.get(factor_id)
        if cancer_id not in enabled_cancers or not factor:
            continue
        if not _applies_to_sex(factor.get("applicable_sex"), sex):
            continue
        if age is not None:
            min_age = factor.get("applicable_age_min")
            max_age = factor.get("applicable_age_max")
            if min_age is not None and age < min_age:
                continue
            if max_age is not None and age > max_age:
                continue
        level = assertion.get("factor_level") or "unknown"
        key = _factor_key(factor_id, level)
        by_key.setdefault(key, {
            "factor_key": key,
            "factor_id": factor_id,
            "factor_name": factor.get("factor_name_zh") or factor.get("factor_name") or factor_id,
            "factor_type": factor.get("factor_type"),
            "factor_level": level,
            "applicable_sex": factor.get("applicable_sex", "all"),
            "interaction_needed_if_missing": bool(factor.get("interaction_needed_if_missing", False)),
            "interaction_question_group": factor.get("interaction_question_group"),
            "expected_evidence": observables.get(factor_id, {}).get("expected_evidence", []),
            "synonyms": synonyms.get(factor_id, []),
            "risk_evidence_values": [],
            "exists": "missing",
            "evidence_text": None,
            "source_section": None,
            "source_page": None,
            "negated": False,
            "confidence": 0.0,
            "raw_field_description": None,
        })
        evidence_values = by_key[key]["risk_evidence_values"]
        if assertion.get("assertion_id") not in {item.get("assertion_id") for item in evidence_values}:
            evidence_values.append(_standardize_risk_evidence(assertion))

    # v7: merge imaging findings into the same template stream so they are
    # filled, gated, merged, archived, and longitudinally tracked together
    # with OR-based risk factors.  Snapshot_risk still splits them out at
    # compute time (Bayes vs PPV midpoint) — see compute_snapshot().
    imaging_path = store / "imaging_findings.json"
    if imaging_path.is_file():
        imaging_ontology = _read(imaging_path)
        for finding in imaging_ontology.get("findings", []):
            cancer_id = finding.get("cancer_id")
            if cancer_id not in enabled_cancers:
                continue
            finding_id = finding["finding_id"]
            cancer = cancer_by_id.get(cancer_id)
            if cancer and not _applies_to_sex(cancer.get("applicable_sex"), sex):
                continue
            key = _factor_key(finding_id, "present")
            by_key[key] = {
                "factor_key": key,
                "factor_id": finding_id,
                "factor_name": finding.get("finding_name_zh") or finding.get("finding_name_en") or finding_id,
                "factor_type": "imaging_finding",
                "factor_level": "present",
                "applicable_sex": cancer.get("applicable_sex", "all") if cancer else "all",
                "interaction_needed_if_missing": False,
                "interaction_question_group": None,
                "expected_evidence": [],
                "synonyms": [],
                "risk_evidence_values": [{
                    "finding_id": finding_id,
                    "cancer_id": cancer_id,
                    "malignancy_ppv_range": finding.get("malignancy_ppv_range"),
                    "ppv_point_used": (
                        (float(finding["malignancy_ppv_range"][0]) + float(finding["malignancy_ppv_range"][1])) / 2
                        if finding.get("malignancy_ppv_range") else None
                    ),
                    "source_id": finding.get("source_id"),
                    "next_step": finding.get("next_step"),
                    "conversion_status": "ppv_based",
                }],
                "exists": "missing",
                "evidence_text": None,
                "source_section": None,
                "source_page": None,
                "negated": False,
                "confidence": 0.0,
                "raw_field_description": None,
            }

    records = sorted(by_key.values(), key=lambda r: r["factor_key"])
    return {
        "schema_version": SCHEMA_VERSION,
        "evidence_version": version,
        "filters": {"sex": sex, "age": age},
        "file_context": {"exam_date": None, "source_file": None, "source_data_id": None},
        "risk_factor_templates": records,
        "counts": {"risk_factor_templates": len(records)},
    }

_FEMALE_ONLY_TEMPLATES = {"cervical_hpv", "cervical_tct", "ovarian_mass", "breast_biopsy", "breast_density_ge75"}
_MALE_ONLY_TEMPLATES = {"prostate_mri"}


def _emit_indicator_schema(evidence_store: Path, sex: str | None, out_path: Path) -> None:
    """Copy indicator_fill_schemas.json filtered for patient sex to out_path."""
    src = evidence_store / "indicator_fill_schemas.json"
    schemas = json.loads(src.read_text(encoding="utf-8"))
    if sex == "male":
        schemas["templates"] = [t for t in schemas["templates"] if t["template_id"] not in _FEMALE_ONLY_TEMPLATES]
    elif sex == "female":
        schemas["templates"] = [t for t in schemas["templates"] if t["template_id"] not in _MALE_ONLY_TEMPLATES]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(schemas, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[assertion_fill_template] indicator_schemas sex={sex} templates={len(schemas['templates'])} -> {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-store", required=True)
    parser.add_argument("--sex", choices=["male", "female"], default=None)
    parser.add_argument("--age", type=int, default=None)
    parser.add_argument("--enabled-cancers", default=None)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--indicator-schema-output",
        default=None,
        help="If provided, write sex-filtered indicator_fill_schemas.json to this path",
    )
    args = parser.parse_args()

    enabled = None
    if args.enabled_cancers:
        enabled = {x.strip() for x in args.enabled_cancers.split(",") if x.strip()}
    payload = build_assertion_fill_template(Path(args.evidence_store), args.sex, args.age, enabled)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"[assertion_fill_template] sex={args.sex} age={args.age} "
        f"templates={payload['counts']['risk_factor_templates']} -> {out}"
    )

    if args.indicator_schema_output:
        _emit_indicator_schema(Path(args.evidence_store), args.sex, Path(args.indicator_schema_output))


if __name__ == "__main__":
    main()

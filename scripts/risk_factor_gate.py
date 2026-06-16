#!/usr/bin/env python3
"""Validate fixed assertion-key LLM fills and admit risk factors."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


FIXED_FIELDS = {
    "assertion_key",
    "assertion_id",
    "cancer_id",
    "factor_id",
    "factor_name",
    "factor_type",
    "factor_level",
    "effect_type",
    "effect_value",
    "applicable_sex",
    "interaction_needed_if_missing",
    "interaction_question_group",
    "expected_evidence",
    "synonyms",
}

LLM_FILL_FIELDS = {
    "exists",
    "exam_date",
    "evidence_text",
    "source_file",
    "source_section",
    "source_page",
    "negated",
    "confidence",
    "raw_field_description",
}


def _records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records = payload.get("records", payload.get("risk_factor_templates", payload.get("assertion_templates", payload.get("candidates"))))
    if not isinstance(records, list):
        raise ValueError("payload must contain records, risk_factor_templates, or assertion_templates list")
    return records



def _is_slim_payload(payload: dict[str, Any]) -> bool:
    return payload.get("schema_version") == "risk-factor-file-fill-v3.3" or "risk_factor_templates" in payload


def _same_slim_fields(template: dict[str, Any], candidate: dict[str, Any]) -> bool:
    fixed = {"factor_key", "factor_id", "factor_name", "factor_type", "factor_level", "risk_evidence_values"}
    return all(candidate.get(field) == template.get(field) for field in fixed)


def _structured_slim_record(record: dict[str, Any], file_context: dict[str, Any]) -> dict[str, Any]:
    return {
        "factor_key": record["factor_key"],
        "factor_id": record["factor_id"],
        "factor_name": record["factor_name"],
        "factor_type": record.get("factor_type"),
        "factor_level": record["factor_level"],
        "exists": record["exists"],
        "exam_date": file_context.get("exam_date"),
        "source_file": file_context.get("source_file"),
        "source_data_id": file_context.get("source_data_id"),
        "evidence_text": record.get("evidence_text"),
        "source_section": record.get("source_section"),
        "source_page": record.get("source_page"),
        "confidence": record.get("confidence", 0),
    }

def _same_fixed_fields(template: dict[str, Any], candidate: dict[str, Any]) -> bool:
    return all(candidate.get(field) == template.get(field) for field in FIXED_FIELDS)


def _reject(record: dict[str, Any], reason: str) -> dict[str, Any]:
    out = dict(record)
    out["reject_reason"] = reason
    return out


def _structured_record(record: dict[str, Any]) -> dict[str, Any]:
    fill = record["llm_fill"]
    return {
        "assertion_key": record["assertion_key"],
        "assertion_id": record["assertion_id"],
        "cancer_id": record["cancer_id"],
        "factor_id": record["factor_id"],
        "factor_name": record["factor_name"],
        "factor_type": record.get("factor_type"),
        "factor_level": record["factor_level"],
        "interaction_needed_if_missing": record.get("interaction_needed_if_missing", False),
        "interaction_question_group": record.get("interaction_question_group"),
        "exists": fill["exists"],
        "exam_date": fill.get("exam_date"),
        "evidence_text": fill["evidence_text"],
        "source_file": fill.get("source_file"),
        "source_section": fill.get("source_section"),
        "source_page": fill.get("source_page"),
        "confidence": fill.get("confidence", 0),
        "admitted_assertion_keys": [record["assertion_key"]],
    }


def gate_assertion_fills(
    template_payload: dict[str, Any],
    candidate_payload: dict[str, Any],
    *,
    content_text: str,
    acceptance_confidence: float = 0.85,
    needs_confirmation_min_confidence: float = 0.60,
) -> dict[str, Any]:
    """Validate LLM-filled assertion records and split by admission status."""
    if _is_slim_payload(template_payload):
        template_by_key = {r["factor_key"]: r for r in _records(template_payload)}
        file_context = candidate_payload.get("file_context") or template_payload.get("file_context", {})
        structured: list[dict[str, Any]] = []
        needs_confirmation: list[dict[str, Any]] = []
        low_confidence: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        assertion_events: list[dict[str, Any]] = []
        event_keys: set[str] = set()
        for candidate in _records(candidate_payload):
            key = candidate.get("factor_key")
            template = template_by_key.get(key)
            if not template:
                rejected.append(_reject(candidate, "unknown_factor_key"))
                continue
            if not _same_slim_fields(template, candidate):
                rejected.append(_reject(candidate, "fixed_field_tampered"))
                continue
            exists = candidate.get("exists")
            evidence = candidate.get("evidence_text") or ""
            confidence = float(candidate.get("confidence") or 0)
            if exists is not True:
                if exists is False and evidence and evidence in content_text:
                    event = _structured_slim_record(candidate, file_context)
                    event["status_reason"] = "explicit_absent"
                    assertion_events.append(event)
                    event_keys.add(key)
                elif exists in {"missing", "unknown"}:
                    pass
                else:
                    rejected.append(_reject(candidate, "not_present"))
                continue
            if candidate.get("negated") is True:
                rejected.append(_reject(candidate, "negated"))
                continue
            if not evidence:
                rejected.append(_reject(candidate, "missing_evidence_text"))
                continue
            if evidence not in content_text:
                rejected.append(_reject(candidate, "evidence_text_not_found"))
                continue
            if confidence >= acceptance_confidence:
                event = _structured_slim_record(candidate, file_context)
                structured.append(event)
                assertion_events.append(event)
                event_keys.add(key)
            elif confidence >= needs_confirmation_min_confidence:
                needs_confirmation.append(candidate)
                event = _structured_slim_record(candidate, file_context)
                event["exists"] = "unknown"
                event["status_reason"] = "needs_confirmation"
                assertion_events.append(event)
                event_keys.add(key)
            else:
                low_confidence.append(candidate)
        return {
            "structured_risk_factors": structured,
            "needs_confirmation_factors": needs_confirmation,
            "unmapped_or_low_confidence_factors": low_confidence,
            "rejected_records": rejected,
            "risk_factor_assertion_status": {
                "assertion_events": assertion_events,
                "assertion_missing_by_template": [template for key, template in template_by_key.items() if key not in event_keys],
            },
        }

    template_by_key = {r["assertion_key"]: r for r in _records(template_payload)}
    structured: list[dict[str, Any]] = []
    needs_confirmation: list[dict[str, Any]] = []
    low_confidence: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    assertion_events: list[dict[str, Any]] = []
    event_keys: set[str] = set()

    for candidate in _records(candidate_payload):
        key = candidate.get("assertion_key")
        template = template_by_key.get(key)
        if not template:
            rejected.append(_reject(candidate, "unknown_assertion_key"))
            continue
        if not _same_fixed_fields(template, candidate):
            rejected.append(_reject(candidate, "fixed_field_tampered"))
            continue
        fill = candidate.get("llm_fill")
        if not isinstance(fill, dict) or (LLM_FILL_FIELDS - set(fill)):
            rejected.append(_reject(candidate, "invalid_llm_fill"))
            continue

        exists = fill.get("exists")
        evidence = fill.get("evidence_text") or ""
        confidence = float(fill.get("confidence") or 0)
        if exists is not True:
            if exists is False and evidence and evidence in content_text:
                event = _structured_record(candidate)
                event["admitted_assertion_keys"] = []
                event["status_reason"] = "explicit_absent"
                assertion_events.append(event)
                event_keys.add(key)
            elif exists == "unknown":
                pass
            else:
                rejected.append(_reject(candidate, "not_present"))
            continue
        if fill.get("negated") is True:
            rejected.append(_reject(candidate, "negated"))
            continue
        if not evidence:
            rejected.append(_reject(candidate, "missing_evidence_text"))
            continue
        if evidence not in content_text:
            rejected.append(_reject(candidate, "evidence_text_not_found"))
            continue
        if confidence >= acceptance_confidence:
            event = _structured_record(candidate)
            structured.append(event)
            assertion_events.append(event)
            event_keys.add(key)
        elif confidence >= needs_confirmation_min_confidence:
            needs_confirmation.append(candidate)
            event = _structured_record(candidate)
            event["exists"] = "unknown"
            event["admitted_assertion_keys"] = []
            event["status_reason"] = "needs_confirmation"
            assertion_events.append(event)
            event_keys.add(key)
        else:
            low_confidence.append(candidate)

    return {
        "structured_risk_factors": structured,
        "needs_confirmation_factors": needs_confirmation,
        "unmapped_or_low_confidence_factors": low_confidence,
        "rejected_records": rejected,
        "risk_factor_assertion_status": {
            "assertion_events": assertion_events,
            "assertion_missing_by_template": [
                template
                for key, template in template_by_key.items()
                if key not in event_keys
            ],
        },
    }


def write_gate_outputs(result: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    mapping = {
        "structured_risk_factors": "structured_risk_factors.json",
        "needs_confirmation_factors": "needs_confirmation_factors.json",
    }
    for key, filename in mapping.items():
        (output_dir / filename).write_text(
            json.dumps({key: result.get(key, [])}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    (output_dir / "risk_factor_assertion_status.json").write_text(
        json.dumps(result.get("risk_factor_assertion_status", {}), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--content-md", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    template = json.loads(Path(args.template).read_text(encoding="utf-8"))
    candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
    content = Path(args.content_md).read_text(encoding="utf-8")
    result = gate_assertion_fills(template, candidate, content_text=content)
    write_gate_outputs(result, Path(args.output_dir))
    print(
        f"[risk_factor_gate] structured={len(result['structured_risk_factors'])} "
        f"needs_confirmation={len(result['needs_confirmation_factors'])}"
    )


if __name__ == "__main__":
    main()

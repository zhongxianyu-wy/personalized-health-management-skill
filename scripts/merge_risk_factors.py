#!/usr/bin/env python3
"""Merge report-derived and interactive risk-factor events for Task 5."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


VALID_SCREENING_RESULTS = {"negative", "positive"}


def _items(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _measurement_sort_value(event: dict[str, Any]) -> float:
    try:
        return float(event.get("measurement_value") or 0)
    except (TypeError, ValueError):
        return 0.0


def _event_sort_key(event: dict[str, Any]) -> tuple[str, int, float, float]:
    date = event.get("exam_date") or ""
    source_rank = 1 if event.get("source") in {"report_llm_fill", "mineru_report", None} else 0
    confidence = float(event.get("confidence") or 0)
    measurement = _measurement_sort_value(event)
    return (str(date), source_rank, measurement, confidence)


def _event_dedup_key(event: dict[str, Any]) -> str | None:
    """Either the cancer-expanded ``assertion_key`` or the slim Task4
    ``factor_key`` (``factor_id|factor_level``). Both are stable enough to
    pick a single current-snapshot entry per observed factor."""
    return event.get("assertion_key") or event.get("factor_key")


def _current_states(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for event in events:
        key = _event_dedup_key(event)
        if not key:
            continue
        if key not in by_key or _event_sort_key(event) >= _event_sort_key(by_key[key]):
            by_key[key] = event
    return [by_key[k] for k in sorted(by_key)]


def _screening_dedup_key(test: dict[str, Any]) -> str | None:
    return test.get("test_id")


def _current_screening_tests(tests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for test in tests:
        key = _screening_dedup_key(test)
        if not key:
            continue
        if key not in by_key or _event_sort_key(test) >= _event_sort_key(by_key[key]):
            by_key[key] = test
    return [by_key[k] for k in sorted(by_key)]


def merge_payloads(
    *,
    structured: dict[str, Any],
    assertion_status: dict[str, Any],
    supplemental_updates: dict[str, Any],
    supplemental_factors: dict[str, Any],
) -> dict[str, Any]:
    report_events = _items(assertion_status, "assertion_events")
    if not report_events:
        report_events = _items(structured, "structured_risk_factors")
    covered_factor_ids = {
        str(e["factor_id"])
        for e in report_events
        if e.get("exists") in {True, False} and e.get("factor_id")
    }

    factor_events = [dict(e, source=e.get("source", "report_llm_fill")) for e in report_events]
    conflicts: list[dict[str, Any]] = []
    for update in _items(supplemental_updates, "updates"):
        factor_id = update.get("factor_id")
        if factor_id in covered_factor_ids:
            conflicts.append({
                "reason": "interactive_blocked_by_report_evidence",
                "factor_id": factor_id,
                "assertion_key": update.get("assertion_key"),
                "blocked_update": update,
            })
            continue
        factor_events.append(dict(update, source=update.get("source", "interactive_completion")))

    screening_tests = [
        test
        for test in _items(supplemental_factors, "screening_tests")
        if test.get("result") in VALID_SCREENING_RESULTS
    ]

    return {
        "factor_events": factor_events,
        "current_factor_states": _current_states(factor_events),
        "screening_tests": _current_screening_tests(screening_tests),
        "conflicts": conflicts,
        "uncertainties": _items(supplemental_factors, "uncertainties"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True)
    args = parser.parse_args()
    artifacts = Path(args.artifacts)
    merged = merge_payloads(
        structured=json.loads((artifacts / "structured_risk_factors.json").read_text(encoding="utf-8")),
        assertion_status=json.loads((artifacts / "risk_factor_assertion_status.json").read_text(encoding="utf-8")),
        supplemental_updates=json.loads((artifacts / "supplemental_risk_factor_updates.json").read_text(encoding="utf-8")),
        supplemental_factors=json.loads((artifacts / "supplemental_risk_factors.json").read_text(encoding="utf-8")),
    )
    (artifacts / "merged_risk_factors.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"[merge_risk_factors] events={len(merged['factor_events'])} "
        f"current={len(merged['current_factor_states'])} screening_tests={len(merged['screening_tests'])}"
    )


if __name__ == "__main__":
    main()

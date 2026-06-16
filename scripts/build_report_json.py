#!/usr/bin/env python3
"""P1 report.json assembler.

PURE remapper: read the internal JSON artifacts written by earlier
deterministic stages and produce one combined ``<out>/artifacts/report.json``.

NO math, NO LLM calls, NO network — just read + remap + atomic write.

Source artifacts (all under ``<out>/artifacts/`` unless noted):
- ``snapshot_risk.json``  — ``cancers`` / ``section4_screening`` /
  ``uncertainties_summary`` / ``person_context`` (sex, age).
- ``voi_ranking.json``    — ``top_recommendation`` / ``rankings`` /
  ``total_methods_evaluated``.
- ``health_summary_structured_summary.json`` — ``status`` /
  ``abnormal_non_cancer_count`` / ``items`` (degrade if absent).
- ``tumor_markers.json`` (a dict with a ``tests`` list) with fallback to
  ``tumor_markers.candidate.json``; both absent → ``[]``.
- ``answers.json`` (``{"answers": {...}}`` or bare dict) — ``q_jizaoan_result``
  and (when positive) ``q_jizaoan_top1`` / ``q_jizaoan_top2``.

There is no BRCA answer key in the questionnaire, so ``brca_status`` always
defaults to ``"unknown"``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "report-v1"

# CP4 health-summary HTML blocks (assessment_result.*) carried verbatim into
# report.json for the full-fidelity template (P2). API-sourced, rendered | safe.
_CP4_BLOCK_KEYS = (
    "risk_level", "core_risk_factors", "overall_assessment",
    "abnormal_table", "disease_cards", "advice_list", "conclusion_table",
)


def _read_json(path: Path | None, default: Any) -> Any:
    """Read a JSON file, returning ``default`` on any read/parse failure."""
    if path is None or not Path(path).is_file():
        return default
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _unwrap_answers(raw: Any) -> dict[str, Any]:
    """Accept ``{"answers": {...}}`` or a bare dict; return the answers dict."""
    if not isinstance(raw, dict):
        return {}
    inner = raw.get("answers")
    if isinstance(inner, dict):
        return inner
    return raw


def _tumor_markers_list(artifacts: Path) -> list[Any]:
    """Prefer ``tumor_markers.json``, fall back to candidate, else ``[]``.

    Both files are dicts with a ``tests`` key; tolerate a bare list too.
    """
    for name in ("tumor_markers.json", "tumor_markers.candidate.json"):
        data = _read_json(artifacts / name, None)
        if data is None:
            continue
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("tests", [])
    return []


def _jizaoan(answers: dict[str, Any]) -> tuple[str, list[str]]:
    result = answers.get("q_jizaoan_result", "unknown")
    top_cancers: list[str] = []
    if result == "positive":
        for qid in ("q_jizaoan_top1", "q_jizaoan_top2"):
            value = answers.get(qid)
            if value and value != "unknown":
                top_cancers.append(value)
    return result, top_cancers


def assemble_report_json(
    *,
    artifacts: Path,
    out: Path,
    answers_path: Path | None,
    person_id: str,
    run_id: str,
    evidence_version: Any,
) -> dict[str, Any]:
    """Assemble report.json from internal artifacts and atomically write it.

    Returns the assembled dict. Missing optional files degrade to sane empty
    defaults instead of raising.
    """
    artifacts = Path(artifacts)

    snapshot = _read_json(artifacts / "snapshot_risk.json", {})
    voi = _read_json(artifacts / "voi_ranking.json", {})
    health = _read_json(artifacts / "health_summary_structured_summary.json", {})
    answers = _unwrap_answers(_read_json(answers_path, {}))

    person_ctx = snapshot.get("person_context", {}) if isinstance(snapshot, dict) else {}
    jizaoan_result, jizaoan_top_cancers = _jizaoan(answers)

    patient = health.get("patient_data", {}) if isinstance(health, dict) else {}
    assessment = health.get("assessment_result", {}) if isinstance(health, dict) else {}
    person_name = patient.get("name") or person_id
    if person_name == "未提供":
        person_name = person_id

    report = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at": datetime.now().isoformat(),
        "person": {
            "person_id": person_id,
            "name": person_name,
            "sex": person_ctx.get("sex"),
            "age": person_ctx.get("age"),
        },
        "jizaoan_result": jizaoan_result,
        "jizaoan_top_cancers": jizaoan_top_cancers,
        "brca_status": "unknown",
        "health_summary": {
            "status": health.get("status"),
            "abnormal_non_cancer_count": health.get("abnormal_non_cancer_count", 0),
            "items": health.get("items", []),
            "blocks": {k: assessment.get(k) for k in _CP4_BLOCK_KEYS},
        },
        "snapshot": {
            "cancers": snapshot.get("cancers", []),
            "section4_screening": snapshot.get("section4_screening", []),
            "uncertainties_summary": snapshot.get("uncertainties_summary", {}),
        },
        "voi": {
            "top_recommendation": voi.get("top_recommendation"),
            "rankings": voi.get("rankings", []),
            "total_methods_evaluated": voi.get("total_methods_evaluated", 0),
        },
        "tumor_markers": _tumor_markers_list(artifacts),
        "evidence_version": evidence_version,
    }

    _atomic_write_json(artifacts / "report.json", report)
    return report


def _atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    """Write JSON to a temp file in the same dir, then ``os.replace``."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)

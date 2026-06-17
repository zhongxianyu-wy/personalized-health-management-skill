"""Tests for the P1 report.json assembler (build_report_json.assemble_report_json).

The assembler is a PURE remapper: it reads internal JSON artifacts written by
earlier deterministic stages and produces a single combined report.json. No
math, no LLM, no network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import build_report_json  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures (match the REAL on-disk shapes of the source artifacts)
# ---------------------------------------------------------------------------


def _write(path: Path, obj: object) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _snapshot() -> dict:
    return {
        "schema_version": "snapshot-risk-v1",
        "person_context": {"sex": "male", "age": 68},
        "cancers": [{"cancer_id": "lung", "posterior_probability": 0.02}],
        "section4_screening": [{"cancer_id": "lung", "test_id": "ldct"}],
        "uncertainties_summary": {"cancers_missing_prior": 1},
    }


def _voi() -> dict:
    return {
        "schema_version": "voi-ranking-v1",
        "rankings": [{"method": "ldct", "voi_score": 1.2}],
        "total_methods_evaluated": 3,
        "top_recommendation": "ldct",
    }


def _health_summary() -> dict:
    return {
        "status": "ready_for_render",
        "abnormal_non_cancer_count": 2,
        "items": [{"name": "blood pressure"}],
    }


def _tumor_markers() -> dict:
    # Real shape written by master_scan.gate_tumor_markers: a DICT with "tests".
    return {
        "schema_version": "tumor-markers-gated-v1",
        "run_date": "2026-06-08",
        "tests": [{"test_id": "afp_serum", "value": 3.1}],
        "rejected_tests": [],
    }


def _answers(wrapper: dict) -> Path:
    return wrapper


@pytest.fixture
def artifacts(tmp_path: Path) -> Path:
    art = tmp_path / "artifacts"
    art.mkdir()
    _write(art / "snapshot_risk.json", _snapshot())
    _write(art / "voi_ranking.json", _voi())
    _write(art / "health_summary_structured_summary.json", _health_summary())
    _write(art / "tumor_markers.json", _tumor_markers())
    return art


@pytest.fixture
def answers_path(tmp_path: Path) -> Path:
    p = tmp_path / "answers.json"
    _write(p, {"answers": {"q_jizaoan_result": "negative"}})
    return p


def _assemble(artifacts: Path, answers_path: Path | None, **over) -> dict:
    kwargs = dict(
        artifacts=artifacts,
        out=artifacts.parent,
        answers_path=answers_path,
        person_id="zhangsan_m68",
        run_id="run-20260608-000000",
        evidence_version="v14.0.0",
    )
    kwargs.update(over)
    return build_report_json.assemble_report_json(**kwargs)


# ---------------------------------------------------------------------------
# Test 1 — full schema with all sources present
# ---------------------------------------------------------------------------


def test_full_schema_keys_present(artifacts: Path, answers_path: Path) -> None:
    result = _assemble(artifacts, answers_path)
    expected_keys = {
        "schema_version", "run_id", "generated_at", "generated_at_display", "person",
        "jizaoan_result", "jizaoan_top_cancers", "brca_status",
        "brca_detail", "checkup_window",
        "timeline_tiers", "x_addons", "package_tiers", "liquid_biopsy_perf", "long_term_intervention",
        "health_summary", "snapshot", "voi", "tumor_markers",
        "evidence_version",
    }
    assert set(result.keys()) == expected_keys
    assert result["schema_version"] == "report-v1"
    assert result["run_id"] == "run-20260608-000000"
    assert result["evidence_version"] == "v14.0.0"
    # snapshot copied verbatim
    assert result["snapshot"]["cancers"] == _snapshot()["cancers"]
    assert result["snapshot"]["section4_screening"] == _snapshot()["section4_screening"]
    assert result["snapshot"]["uncertainties_summary"] == _snapshot()["uncertainties_summary"]
    # voi copied
    assert result["voi"]["top_recommendation"] == "ldct"
    assert result["voi"]["rankings"] == _voi()["rankings"]
    assert result["voi"]["total_methods_evaluated"] == 3
    # health summary mapped
    assert result["health_summary"]["status"] == "ready_for_render"
    assert result["health_summary"]["abnormal_non_cancer_count"] == 2
    assert result["health_summary"]["items"] == [{"name": "blood pressure"}]


# ---------------------------------------------------------------------------
# Test 2 — missing optional files degrade gracefully
# ---------------------------------------------------------------------------


def test_missing_optional_files_degrade(tmp_path: Path) -> None:
    art = tmp_path / "artifacts"
    art.mkdir()
    # No tumor_markers.json AND no candidate; no health summary; no voi/snapshot.
    result = _assemble(art, None)
    assert result["tumor_markers"] == []
    assert result["snapshot"] == {
        "cancers": [], "section4_screening": [], "uncertainties_summary": {},
    }
    assert result["voi"] == {
        "top_recommendation": None, "rankings": [], "total_methods_evaluated": 0,
    }
    # P1 keys preserved; P2 adds `blocks` (CP4 HTML, None when absent).
    assert result["health_summary"]["status"] is None
    assert result["health_summary"]["abnormal_non_cancer_count"] == 0
    assert result["health_summary"]["items"] == []
    assert all(v is None for v in result["health_summary"]["blocks"].values())
    assert result["jizaoan_result"] == "unknown"
    assert result["jizaoan_top_cancers"] == []
    assert result["brca_status"] == "unknown"
    assert result["person"]["sex"] is None
    assert result["person"]["age"] is None


# ---------------------------------------------------------------------------
# Test 3 — tumor_markers.json preferred over candidate
# ---------------------------------------------------------------------------


def test_tumor_markers_json_preferred_over_candidate(artifacts: Path) -> None:
    # candidate has DIFFERENT tests; the .json must win.
    _write(
        artifacts / "tumor_markers.candidate.json",
        {"tests": [{"test_id": "CANDIDATE_ONLY"}]},
    )
    result = _assemble(artifacts, None)
    assert result["tumor_markers"] == [{"test_id": "afp_serum", "value": 3.1}]


def test_tumor_markers_candidate_fallback(tmp_path: Path) -> None:
    art = tmp_path / "artifacts"
    art.mkdir()
    _write(
        art / "tumor_markers.candidate.json",
        {"tests": [{"test_id": "afp_serum"}]},
    )
    result = _assemble(art, None)
    assert result["tumor_markers"] == [{"test_id": "afp_serum"}]


# ---------------------------------------------------------------------------
# Test 4 — jizaoan sourced from answers
# ---------------------------------------------------------------------------


def test_jizaoan_positive_yields_top_cancers(artifacts: Path, tmp_path: Path) -> None:
    ans = tmp_path / "ans.json"
    _write(ans, {"answers": {
        "q_jizaoan_result": "positive",
        "q_jizaoan_top1": "lung",
        "q_jizaoan_top2": "unknown",
    }})
    result = _assemble(artifacts, ans)
    assert result["jizaoan_result"] == "positive"
    assert result["jizaoan_top_cancers"] == ["lung"]


def test_jizaoan_default_unknown_no_answers(artifacts: Path) -> None:
    result = _assemble(artifacts, None)
    assert result["jizaoan_result"] == "unknown"
    assert result["jizaoan_top_cancers"] == []


def test_answers_bare_dict_shape(artifacts: Path, tmp_path: Path) -> None:
    ans = tmp_path / "bare.json"
    _write(ans, {"q_jizaoan_result": "negative"})  # no "answers" wrapper
    result = _assemble(artifacts, ans)
    assert result["jizaoan_result"] == "negative"


# ---------------------------------------------------------------------------
# Test 5 — report.json written atomically and equals returned dict
# ---------------------------------------------------------------------------


def test_report_written_to_disk_equals_returned(artifacts: Path, answers_path: Path) -> None:
    result = _assemble(artifacts, answers_path)
    written = artifacts / "report.json"
    assert written.is_file()
    on_disk = json.loads(written.read_text(encoding="utf-8"))
    assert on_disk == result


# ---------------------------------------------------------------------------
# Test 6 — person sex/age from snapshot person_context; person_id from param
# ---------------------------------------------------------------------------


def test_person_fields(artifacts: Path, answers_path: Path) -> None:
    result = _assemble(artifacts, answers_path, person_id="custom_id")
    assert result["person"]["person_id"] == "custom_id"
    assert result["person"]["sex"] == "male"
    assert result["person"]["age"] == 68

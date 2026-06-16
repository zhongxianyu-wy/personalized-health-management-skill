"""Tests for the P1 thin report renderer (render_report).

render_report is a THIN Jinja2 renderer: it loads
``templates/integrated_report_v14.html`` under StrictUndefined and renders a
report.json-shaped dict plus a disclaimer. No math, no LLM, no network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import build_report_json  # noqa: E402
import render_report  # noqa: E402

SKILL_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = SKILL_ROOT / "templates" / "integrated_report_v14.html"
DISCLAIMER = "本报告仅用于健康管理参考，不构成医学诊断。"


# ---------------------------------------------------------------------------
# Fixtures — build a real, schema-complete report dict via the assembler so the
# context can never drift from the report.json schema StrictUndefined enforces.
# ---------------------------------------------------------------------------


def _write(path: Path, obj: object) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_report(artifacts: Path, *, snapshot: dict, voi: dict) -> dict:
    _write(artifacts / "snapshot_risk.json", snapshot)
    _write(artifacts / "voi_ranking.json", voi)
    _write(
        artifacts / "health_summary_structured_summary.json",
        {"status": "ready_for_render", "abnormal_non_cancer_count": 2,
         "items": [{"name": "blood pressure"}]},
    )
    _write(
        artifacts / "tumor_markers.json",
        {"schema_version": "tumor-markers-gated-v1", "run_date": "2026-06-08",
         "tests": [{"test_id": "afp_serum", "value": 3.1}], "rejected_tests": []},
    )
    answers = artifacts.parent / "answers.json"
    _write(answers, {"answers": {"q_jizaoan_result": "negative"}})
    return build_report_json.assemble_report_json(
        artifacts=artifacts,
        out=artifacts.parent,
        answers_path=answers,
        person_id="zhangsan_m68",
        run_id="run-20260608-000000",
        evidence_version="v14.0.0",
    )


@pytest.fixture
def negative_report(tmp_path: Path) -> dict:
    art = tmp_path / "artifacts"
    art.mkdir()
    return _build_report(
        art,
        snapshot={
            "schema_version": "snapshot-risk-v1",
            "person_context": {"sex": "male", "age": 68},
            "cancers": [],
            "section4_screening": [],
            "uncertainties_summary": {},
        },
        voi={
            "schema_version": "voi-ranking-v1",
            "rankings": [],
            "total_methods_evaluated": 0,
            "top_recommendation": None,
        },
    )


@pytest.fixture
def positive_report(tmp_path: Path) -> dict:
    art = tmp_path / "artifacts"
    art.mkdir()
    return _build_report(
        art,
        snapshot={
            "schema_version": "snapshot-risk-v1",
            "person_context": {"sex": "female", "age": 55},
            "cancers": [{"cancer_id": "lung", "posterior_probability": 0.02}],
            "section4_screening": [{"cancer_id": "lung", "test_id": "ldct"}],
            "uncertainties_summary": {"cancers_missing_prior": 1},
        },
        voi={
            "schema_version": "voi-ranking-v1",
            "rankings": [{"method": "ldct", "voi_score": 1.2}],
            "total_methods_evaluated": 3,
            "top_recommendation": "ldct",
        },
    )


# ---------------------------------------------------------------------------
# render_report
# ---------------------------------------------------------------------------


def test_render_report_returns_html_with_disclaimer_and_person(negative_report: dict) -> None:
    html = render_report.render_report(negative_report, TEMPLATE, DISCLAIMER)

    assert isinstance(html, str)
    assert html.strip()
    assert DISCLAIMER in html
    assert negative_report["person"]["person_id"] in html


def test_render_report_positive_case_renders(positive_report: dict) -> None:
    html = render_report.render_report(positive_report, TEMPLATE, DISCLAIMER)

    assert DISCLAIMER in html
    assert "ldct" in html


# ---------------------------------------------------------------------------
# write_report_html
# ---------------------------------------------------------------------------


def test_write_report_html_writes_file_matching_render(
    negative_report: dict, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()

    written_path = render_report.write_report_html(negative_report, TEMPLATE, DISCLAIMER, out)

    assert written_path == out / "report.html"
    assert written_path.is_file()
    expected = render_report.render_report(negative_report, TEMPLATE, DISCLAIMER)
    assert written_path.read_text(encoding="utf-8") == expected

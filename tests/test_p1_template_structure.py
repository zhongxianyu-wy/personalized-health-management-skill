"""Structure tests for the P1 integrated report template (scaffold).

The template `templates/integrated_report_v14.html` is rendered with the
report.json context under Jinja2 StrictUndefined. These tests only assert
scaffold-level guarantees: it renders without UndefinedError for both a
negative and a positive context, exposes the six named section containers,
and surfaces the jizaoan positive/negative branches.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATE_NAME = "integrated_report_v14.html"

SECTION_IDS = [
    "section-header",
    "section-timeline",
    "section-clinical-design",
    "section-liquid-biopsy",
    "section-package",
    "section-lifestyle",
]


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,
    )


def _base_context() -> dict:
    """A minimal but schema-complete render context (negative case)."""
    return {
        "schema_version": "report-v1",
        "run_id": "run-test",
        "generated_at": "2026-06-08T00:00:00",
        "person": {"person_id": "zhangsan_m68", "name": "zhangsan_m68", "sex": "male", "age": 68},
        "jizaoan_result": "negative",
        "jizaoan_top_cancers": [],
        "brca_status": "negative",
        "health_summary": {
            "status": None, "abnormal_non_cancer_count": 0, "items": [],
            "blocks": {
                "risk_level": None, "core_risk_factors": None, "overall_assessment": None,
                "abnormal_table": None, "disease_cards": None, "advice_list": None,
                "conclusion_table": None,
            },
        },
        "snapshot": {"cancers": [], "section4_screening": [], "uncertainties_summary": {}},
        "voi": {"top_recommendation": None, "rankings": [], "total_methods_evaluated": 0},
        "tumor_markers": [],
        "evidence_version": None,
        "disclaimer": "本报告仅供参考，不构成诊断。",
    }


def _render(context: dict) -> str:
    return _env().get_template(TEMPLATE_NAME).render(**context)


def test_negative_context_renders_nonempty() -> None:
    html = _render(_base_context())
    assert html.strip()
    assert "<!DOCTYPE html>" in html


def test_all_six_section_ids_present() -> None:
    html = _render(_base_context())
    for sid in SECTION_IDS:
        assert f'id="{sid}"' in html, f"missing section id: {sid}"


def test_person_id_appears() -> None:
    html = _render(_base_context())
    assert "zhangsan_m68" in html


def test_negative_case_contains_yinxing() -> None:
    html = _render(_base_context())
    assert "阴性" in html


def test_positive_case_contains_yangxing() -> None:
    ctx = {
        **_base_context(),
        "jizaoan_result": "positive",
        "jizaoan_top_cancers": ["肺癌"],
        "brca_status": "positive",
    }
    html = _render(ctx)  # must not raise UndefinedError
    assert "阳性" in html
    assert "肺癌" in html

"""Structure tests for the integrated report template (temp, sole authority).

The template ``templates/integrated_report_temp.html`` (strictly aligned to
``temp/html-preview-10.html``) is rendered under Jinja2 StrictUndefined. These
tests assert scaffold guarantees: renders without UndefinedError for negative
and positive contexts, exposes the temp section containers (timeline/package/
tech-board), and surfaces the jizaoan positive/negative branches.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
TEMPLATE_NAME = "integrated_report_temp.html"

# temp 模版结构标记（class/标题；temp 用 class 非 v14 的 section-* id）
STRUCTURE_MARKERS = ["timeline-container", "package-grid", "核心建议执行时间轴", "组合套餐"]


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        undefined=StrictUndefined,
    )


def _base_context() -> dict:
    """temp 模版最小完整 context（含 5 section artifact 默认空 + brca_detail/checkup_window）。"""
    return {
        "schema_version": "report-v1",
        "run_id": "run-test",
        "generated_at": "2026-06-08T00:00:00",
        "generated_at_display": "2026年6月8日",
        "person": {"person_id": "zhangsan_m68", "name": "zhangsan_m68", "sex": "male", "age": 68},
        "jizaoan_result": "negative",
        "jizaoan_top_cancers": [],
        "brca_status": "unknown",
        "brca_detail": "",
        "checkup_window": "6-12 个月内",
        "timeline_tiers": {"priority": [], "important": [], "maintain": []},
        "x_addons": [],
        "package_tiers": [],
        "liquid_biopsy_perf": {"sensitivity": "-", "specificity": "-",
                               "market_price_range": "-", "clinical_hint": "",
                               "negative_risk_reduction": ""},
        "long_term_intervention": {"genetic_management": [], "lifestyle": []},
        "health_summary": {"blocks": {"risk_level": None, "overall_assessment": None}},
        "snapshot": {"cancers": [], "section4_screening": [], "uncertainties_summary": {}},
        "evidence_version": "v14.0.0",
        "disclaimer": "本报告仅供参考，不构成诊断。",
    }


def _render(context: dict) -> str:
    return _env().get_template(TEMPLATE_NAME).render(**context)


def test_negative_context_renders_nonempty() -> None:
    html = _render(_base_context())
    assert html.strip()
    assert "<!DOCTYPE html>" in html


def test_structure_markers_present() -> None:
    html = _render(_base_context())
    for m in STRUCTURE_MARKERS:
        assert m in html, f"missing structure marker: {m}"


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
        "brca_detail": "BRCA1 基因突变致病位点携带者",
    }
    html = _render(ctx)  # must not raise UndefinedError
    assert "阳性" in html
    assert "肺癌" in html

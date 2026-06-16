"""P2 T2-T7: full-fidelity section rendering of integrated_report_v14.html.

Renders the real template the way render_report.py does (StrictUndefined +
autoescape) and asserts each section binds real data, plus the escaping
contract: trusted CP4 HTML blocks render unescaped (| safe), scalar fields
stay autoescaped.
"""
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

TEMPLATES = Path(__file__).parent.parent / "templates"


def _render(ctx: dict) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        undefined=StrictUndefined,
        autoescape=select_autoescape(["html"]),
    )
    return env.get_template("integrated_report_v14.html").render(**ctx)


def _base_ctx(**over) -> dict:
    ctx = {
        "generated_at": "2026-06-09T10:00:00",
        "evidence_version": "evidence-v0003",
        "disclaimer": "本报告仅供健康管理参考。",
        "person": {"person_id": "p1", "name": "张三", "sex": "male", "age": 68},
        "jizaoan_result": "negative",
        "jizaoan_top_cancers": [],
        "brca_status": "negative",
        "tumor_markers": [{"test_id": "afp_serum", "value": "4.16", "result": "negative"}],
        "health_summary": {
            "status": "ready_for_render", "abnormal_non_cancer_count": 0, "items": [],
            "blocks": {
                "risk_level": "🟠 高风险（ADR 47）",
                "core_risk_factors": "慢性萎缩性胃炎",
                "overall_assessment": "<p>68岁男性总体评估。</p>",
                "abnormal_table": "<table id='ab'><tr><td>总胆固醇 6.37</td></tr></table>",
                "disease_cards": "<table id='dc'><tr><td>胃恶性肿瘤</td></tr></table>",
                "advice_list": "<ul><li>戒烟限酒</li></ul>",
                "conclusion_table": "<table id='cc'><tr><td>高风险</td></tr></table>",
            },
        },
        "snapshot": {
            "cancers": [], "uncertainties_summary": {},
            "section4_screening": [
                {"cancer_name_zh": "甲状腺癌", "risk_tier": "high_workup",
                 "standard_screening": [{"method": "甲状腺彩超", "population": "成人",
                                         "interval": "每年1次", "trigger": "结节",
                                         "source_id": "guideline_thyroid_2023"}]},
                {"cancer_name_zh": "胃癌", "risk_tier": "moderate_workup",
                 "standard_screening": [{"method": "胃镜", "population": "40岁以上",
                                         "interval": "每2年", "trigger": "萎缩性胃炎",
                                         "source_id": "guideline_gastric"}]},
            ],
        },
        "voi": {
            "top_recommendation": "吉早安", "total_methods_evaluated": 3,
            "rankings": [
                {"method": "胃肠镜", "recommendation": "强烈推荐", "cost_rmb": 2000,
                 "cost_level": "high", "invasiveness": "invasive", "guideline": "GL-A",
                 "cancer_name_zh": "胃癌"},
                {"method": "吉早安", "recommendation": "常规", "cost_rmb": 3000,
                 "cost_level": "high", "invasiveness": "non-invasive", "guideline": "GL-B",
                 "cancer_name_zh": "多癌", "is_liquid_biopsy": True},
            ],
        },
    }
    ctx.update(over)
    return ctx


# --- T2 header ---
def test_header_shows_name_and_risk_level():
    out = _render(_base_ctx())
    assert "张三" in out
    assert "高风险" in out
    assert "evidence-v0003" in out


# --- T3 timeline tiers ---
def test_timeline_cards_by_tier():
    out = _render(_base_ctx())
    assert "甲状腺彩超" in out and "胃镜" in out
    assert "每年1次" in out
    assert "guideline_thyroid_2023" in out
    # high_workup -> priority, moderate_workup -> important
    assert "timeline-card priority" in out
    assert "timeline-card important" in out


# --- T4 clinical-design + escaping contract ---
def test_clinical_blocks_unescaped():
    out = _render(_base_ctx())
    assert "<table id='ab'>" in out  # abnormal_table NOT escaped
    assert "<table id='dc'>" in out  # disease_cards NOT escaped
    assert "总胆固醇 6.37" in out


def test_scalar_fields_escaped():
    out = _render(_base_ctx(person={"person_id": "p1", "name": "<b>x</b>",
                                    "sex": "male", "age": 68}))
    assert "&lt;b&gt;x&lt;/b&gt;" in out  # scalar escaped
    assert "<b>x</b>" not in out


# --- T5 liquid-biopsy branches ---
def test_liquid_biopsy_negative():
    out = _render(_base_ctx())
    assert "阴性" in out
    assert "afp_serum" in out


def test_liquid_biopsy_positive():
    out = _render(_base_ctx(jizaoan_result="positive", jizaoan_top_cancers=["肺癌"]))
    assert "阳性" in out
    assert "肺癌" in out


# --- T6 package tier ordering ---
def test_package_sorted_by_tier():
    out = _render(_base_ctx())
    assert "胃肠镜" in out and "3000" in out
    # 强烈推荐 entry before 常规 entry
    assert out.index("强烈推荐") < out.index("常规")


# --- T7 lifestyle + brca ---
def test_lifestyle_advice_unescaped():
    out = _render(_base_ctx())
    assert "戒烟限酒" in out
    assert "<ul><li>戒烟限酒</li></ul>" in out  # advice_list safe
    assert "本报告仅供健康管理参考" in out


def test_brca_branch():
    pos = _render(_base_ctx(brca_status="positive"))
    neg = _render(_base_ctx(brca_status="negative"))
    assert "遗传" in pos
    assert 'class="genetic-alert"' in pos        # body class switches palette
    assert 'class="genetic-alert"' not in neg    # CSS rule .genetic-alert is always present; body class is not

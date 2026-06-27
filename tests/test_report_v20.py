"""T6 v20 报告模版契约测试：6 section 存在 + BRCA/吉早安 阳阴双路径 + StrictUndefined 全变量落地。"""
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_report_json as brj
import render_report as rr

SKILL_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = SKILL_ROOT / "templates" / "integrated_report_v20.html"


def _mock_artifacts(tmp, *, brca=False, jizaoan="negative"):
    art = tmp / "artifacts"
    art.mkdir(exist_ok=True)

    def w(n, o):
        json.dump(o, open(art / n, "w"), ensure_ascii=False)

    w("snapshot_risk.json", {"cancers": [{"cancer_id": "lung_cancer", "risk_tier": "high"}], "section4_screening": [], "person_context": {"sex": "male", "age": 55}, "uncertainties_summary": {}})
    w("health_summary_structured_summary.json", {"patient_data": {"name": "测试"}, "assessment_result": {}})
    w("timeline_tiers.json", {"priority": [{"item_name": "结肠镜", "rationale": "高危"}], "important": [], "maintain": []})
    w("x_addons.json", [{"risk_source": "ALT高", "risk_level_tag": "warning", "method": "肝超声", "interval": "3月", "price_range": "¥100", "clinical_value": "排查"}])
    w("package_tiers.json", [{"name": "基础", "price_range": "¥1200-3000", "includes": ["LDCT"], "note": "基础", "recommended": False}, {"name": "进阶", "price_range": "¥3000-8000", "includes": ["LDCT"], "note": "推荐", "recommended": True}, {"name": "深度", "price_range": "¥5000-12000", "includes": ["MRI"], "note": "深度", "recommended": False}])
    w("liquid_biopsy_perf.json", {"sensitivity": "74.9%", "specificity": "99.0%", "market_price_range": "¥1280", "negative_risk_reduction": "降级"})
    w("long_term_intervention.json", {"genetic_management": [], "lifestyle": ["戒烟"]})
    ans = {"q_jizaoan_result": jizaoan}
    if jizaoan == "positive":
        ans.update({"q_jizaoan_top1": "colorectal_cancer", "q_jizaoan_top2": "lung_cancer"})
    if brca:
        ans["q_genetic_mutations_brca"] = ["brca1"]
    return art, ans


def _render(tmp, brca, jizaoan):
    art, ans = _mock_artifacts(tmp, brca=brca, jizaoan=jizaoan)
    ap = tmp / "answers.json"
    json.dump({"answers": ans}, open(ap, "w"), ensure_ascii=False)
    report = brj.assemble_report_json(artifacts=art, out=tmp, answers_path=ap, person_id="t", run_id="r", evidence_version="v")
    return report, rr.render_report(report, template_path=TEMPLATE, disclaimer="d")


def test_positive_path_brca_jizaoan(tmp_path):
    """BRCA阳性+吉早安阳性 → genetic-alert 主题 + 阳性看板。"""
    report, html = _render(tmp_path, brca=True, jizaoan="positive")
    assert report["brca_status"] == "positive"
    assert report["jizaoan_result"] == "positive"
    assert "genetic-alert" in html
    assert "阳性" in html


def test_negative_path_standard(tmp_path):
    """unknown+negative → standard 主题 + 阴性板块。"""
    report, html = _render(tmp_path, brca=False, jizaoan="negative")
    assert report["brca_status"] == "unknown"
    assert report["jizaoan_result"] == "negative"
    assert "standard" in html
    assert "阴性" in html


def test_six_sections_present(tmp_path):
    """v20 模版 6 section 标记齐全。"""
    _, html = _render(tmp_path, brca=True, jizaoan="positive")
    for marker in ["核心建议执行时间轴", "循证", "组合套餐", "长期健康干预"]:
        assert marker in html
    assert "package-card" in html  # 套餐 grid
    assert "timeline-card" in html  # 三档时间轴


def test_strict_undefined_no_error(tmp_path):
    """StrictUndefined 下双路径都渲染成功 → 所有 Jinja 变量已落地。"""
    _render(tmp_path, brca=True, jizaoan="positive")
    _render(tmp_path, brca=False, jizaoan="negative")

"""报告模版（temp 唯一权威）契约测试。

验证 ``templates/integrated_report_temp.html``（严格对齐
``temp/html-preview-10.html``）的三态渲染、条件渲染适配、spec 兜底与空边界。

temp 是高危阳性案例的静态稿；本测试确认 temp 模版严格还原 temp 的 DOM/CSS
（genetic-alert 紫主题 / 双暴露标签条 / 三档时间轴 grid / X 加项宽表 / 液检
tech-board / 套餐 package-grid / 长期干预分组），且对低危·阴性·未测做最小条件
渲染（standard 主题、双暴露隐藏、液检绿/灰态、无遗传管理）——不改变 temp 视觉。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_report_json as brj
import render_report as rr

SKILL_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = SKILL_ROOT / "templates" / "integrated_report_temp.html"


def _mock(tmp, *, brca=False, jizaoan="unknown",
          timeline=None, x_addons=None, packages=None, intervention=None, liquid=None):
    art = tmp / "artifacts"
    art.mkdir(exist_ok=True)

    def w(n, o):
        (art / n).write_text(json.dumps(o, ensure_ascii=False), encoding="utf-8")

    w("snapshot_risk.json", {
        "cancers": [{"cancer_id": "lung_cancer", "risk_tier": "high"}],
        "section4_screening": [], "person_context": {"sex": "female", "age": 45},
        "uncertainties_summary": {},
    })
    w("health_summary_structured_summary.json", {"patient_data": {"name": "魏女士"},
                                                  "assessment_result": {}})
    w("timeline_tiers.json", timeline if timeline is not None else {
        "priority": [{"item_name": "无痛结肠镜检查", "rationale": "结直肠癌后验3.99%"}],
        "important": [{"item_name": "乳腺MRI+钼靶", "rationale": "BRCA1高危"}],
        "maintain": [{"item_name": "HPV+TCT", "rationale": "常规"}],
    })
    w("x_addons.json", x_addons if x_addons is not None else [{
        "risk_source": "吉早安阳性TOP1", "risk_level_tag": "danger",
        "risk_level_label": "High(肠癌高危)", "method": "无痛结肠镜",
        "interval": "每3-5年", "price_range": "¥900-1400", "clinical_value": "早癌精查",
    }])
    w("package_tiers.json", packages if packages is not None else [
        {"name": "核心风险筛查档", "price_range": "¥2,000", "includes": ["LDCT"], "note": "核心", "recommended": False},
        {"name": "全面覆盖档", "price_range": "¥4,000", "includes": ["LDCT", "无痛结肠镜"], "note": "全面", "recommended": True},
        {"name": "癌症深入筛查档", "price_range": "¥5,999", "includes": ["LDCT", "无痛结肠镜", "吉早安"], "note": "在全面覆盖档基础上增加吉早安检测（+1999元）", "recommended": False},
    ])
    w("long_term_intervention.json", intervention if intervention is not None else {
        "genetic_management": ["预防性手术决策(BSO)", "家族遗传阻断"],
        "lifestyle": ["严格无烟戒酒", "维持BMI 18.5-23.9"],
    })
    w("liquid_biopsy_perf.json", liquid if liquid is not None else {
        "sensitivity": "81.9%",
        "market_price_range": "¥1,980-2,980", "clinical_hint": "阳性信号提示早期异型细胞活动",
        "negative_risk_reduction": "阴性降低风险评级",
    })
    ans = {"q_jizaoan_result": jizaoan}
    if jizaoan == "positive":
        ans.update({"q_jizaoan_top1": "结直肠癌", "q_jizaoan_top2": "肺癌"})
    if brca:
        ans["q_genetic_mutations_brca"] = ["brca1"]
        ans["q_brca_detail"] = "BRCA1 基因突变致病位点携带者"
    ap = tmp / "answers.json"
    ap.write_text(json.dumps({"answers": ans}, ensure_ascii=False), encoding="utf-8")
    return art, ap


def _render(tmp, **kw):
    art, ap = _mock(tmp, **kw)
    report = brj.assemble_report_json(
        artifacts=art, out=tmp, answers_path=ap,
        person_id="t", run_id="r", evidence_version="evidence-v0003")
    return report, rr.render_report(report, template_path=TEMPLATE, disclaimer="免责声明")


def test_positive_replays_temp(tmp_path):
    """魏女士双阳性 → 严格复现 temp 阳态 DOM。"""
    _, html = _render(tmp_path, brca=True, jizaoan="positive")
    assert 'class="genetic-alert"' in html                      # 紫主题
    assert "先天遗传风险" in html and "后天现症风险" in html      # 双暴露双行
    assert "BRCA1 基因突变致病位点携带者" in html                # brca_detail
    assert "timeline-card priority" in html                      # 三档时间轴
    assert "timeline-card important" in html
    assert "timeline-card maintain" in html
    assert "🔴 优先执行" in html and "🟠 重要检查" in html and "🟢 持续管理" in html
    assert "data-table" in html and "tag-danger" in html         # X 加项宽表 + 级别 tag
    assert "tech-board" in html and "result-box" in html         # 液检阳态看板
    assert "阳性" in html
    assert "package-grid" in html                                # 套餐 grid
    assert "全面覆盖档 (推荐)" in html                           # 中卡 recommended 高亮
    assert "遗传特异性临床管理" in html                          # 遗传管理
    assert "生活方式干预" in html                                # 生活方式
    assert "evidence-v0003" in html                              # 页脚版本


def test_negative_low_risk(tmp_path):
    """张三低危阴性 → standard 主题 / 双暴露整 box 隐藏 / 液检绿态 / 无遗传管理。"""
    _, html = _render(tmp_path, brca=False, jizaoan="negative",
                      timeline={"priority": [{"item_name": "复查血糖", "rationale": "偏高"}],
                                "important": [], "maintain": []},
                      intervention={"genetic_management": [], "lifestyle": ["控制碳水"]})
    assert 'class="standard"' in html
    assert '<div class="user-badge-grid">' not in html          # 双暴露整 box 隐藏
    assert "tech-board negative" in html                         # 液检阴性绿态
    assert "检测结果：阴性" in html
    assert "遗传特异性临床管理" not in html                      # 无遗传管理


def test_untested_neutral(tmp_path):
    """未测吉早安 → tech-board untested 中性提示（避免阳/阴误导）。"""
    _, html = _render(tmp_path, brca=False, jizaoan="unknown",
                      intervention={"genetic_management": [], "lifestyle": ["运动"]})
    assert "tech-board untested" in html
    assert "本次未检测" in html


def test_deep_package_displays_comprehensive_plus_jizaoan(tmp_path):
    """档3展示为全面覆盖档基础上增加吉早安检测。"""
    packages = [
        {"name": "核心风险筛查档", "price_range": "¥500", "includes": ["LDCT"], "note": "基础", "recommended": False},
        {"name": "全面覆盖档", "price_range": "¥1500", "includes": ["LDCT", "肠镜"], "note": "全面", "recommended": True},
        {
            "name": "癌症深入筛查档",
            "price_range": "¥3499",
            "includes": ["LDCT", "肠镜", "吉早安"],
            "note": "在全面覆盖档基础上增加吉早安检测（+1999元）",
            "recommended": False,
        },
    ]
    _, html = _render(tmp_path, packages=packages)
    assert "癌症深入筛查档" in html
    assert "在全面覆盖档基础上增加吉早安检测（+1999元）" in html
    assert "LDCT、肠镜、吉早安" in html
    assert "吉早安® + 未替代检测" not in html
    assert "全部检测项 + 吉早安®" not in html


def test_package_template_displays_each_include_price(tmp_path):
    """套餐推荐细节展示每个具体检测项目及其价格。"""
    packages = [
        {
            "name": "核心风险筛查档",
            "price_range": "¥620",
            "includes": ["低剂量胸部CT", "甲状腺彩超"],
            "include_details": [
                {"name": "低剂量胸部CT", "price_range": "¥500"},
                {"name": "甲状腺彩超", "price_range": "¥120"},
            ],
            "note": "核心",
            "recommended": False,
        },
        {
            "name": "全面覆盖档",
            "price_range": "¥1500",
            "includes": ["低剂量胸部CT", "无痛肠镜"],
            "include_details": [
                {"name": "低剂量胸部CT", "price_range": "¥500"},
                {"name": "无痛肠镜", "price_range": "¥1000"},
            ],
            "note": "全面",
            "recommended": True,
        },
        {
            "name": "癌症深入筛查档",
            "price_range": "¥3499",
            "includes": ["低剂量胸部CT", "无痛肠镜", "吉早安"],
            "include_details": [
                {"name": "低剂量胸部CT", "price_range": "¥500"},
                {"name": "无痛肠镜", "price_range": "¥1000"},
                {"name": "吉早安", "price_range": "¥1999"},
            ],
            "note": "在全面覆盖档基础上增加吉早安检测（+1999元）",
            "recommended": False,
        },
    ]
    _, html = _render(tmp_path, packages=packages)
    assert "低剂量胸部CT：¥500" in html
    assert "甲状腺彩超：¥120" in html
    assert "无痛肠镜：¥1000" in html
    assert "吉早安：¥1999" in html


def test_template_uses_chinese_labels_and_deep_tier_simple_addon(tmp_path):
    """模板输出使用中文风险级别；档3展示为全面覆盖基础上加吉早安，不再展示替代双价。"""
    packages = [
        {"name": "核心风险筛查档", "price_range": "¥500", "includes": ["LDCT"], "note": "核心", "recommended": False},
        {"name": "全面覆盖档", "price_range": "¥1500", "includes": ["LDCT", "肠镜"], "note": "全面", "recommended": True},
        {
            "name": "癌症深入筛查档",
            "price_range": "¥3499",
            "includes": ["LDCT", "肠镜", "吉早安"],
            "note": "在全面覆盖档基础上增加吉早安检测（+1999元）",
            "recommended": False,
        },
    ]
    x_addons = [
        {"risk_source": "中风险来源", "risk_level_tag": "warning", "method": "重要检查", "interval": "1个月内", "price_range": "¥100", "clinical_value": "复查"},
        {"risk_source": "高风险来源", "risk_level_tag": "danger", "method": "优先检查", "interval": "1-2周内", "price_range": "¥200", "clinical_value": "排查"},
    ]
    _, html = _render(tmp_path, packages=packages, x_addons=x_addons)
    assert "风险级别" in html
    assert "CancerRisk 级别" not in html
    assert "高风险" in html and "中风险" in html
    assert "癌症深入筛查档" in html
    assert "在全面覆盖档基础上增加吉早安检测（+1999元）" in html
    assert "吉早安® + 未替代检测" not in html
    assert "全部检测项 + 吉早安®" not in html


def test_risk_level_tags_render_expected_colors_from_chinese_labels(tmp_path):
    """风险源等级仅有中文标签时，模板仍输出深红/橙/灰三档颜色。"""
    x_addons = [
        {"risk_source": "低风险来源", "risk_level_label": "低风险", "method": "常规复查"},
        {"risk_source": "高风险来源", "risk_level_label": "高风险", "method": "优先检查"},
        {"risk_source": "中等风险来源", "risk_level_label": "中等风险", "method": "重要检查"},
    ]
    _, html = _render(tmp_path, x_addons=x_addons)
    assert ".tag-danger { background: #c53030; }" in html
    assert ".tag-warning { background: #ed8936; }" in html
    assert ".tag-info { background: #4a5568; }" in html
    assert "tag-danger" in html and "高风险" in html
    assert "tag-warning" in html and "中等风险" in html
    assert "tag-info" in html and "低风险" in html


def test_timeline_render_hides_internal_workup_tokens(tmp_path):
    """核心建议时间轴不展示 moderate_workup 等内部风险枚举。"""
    timeline = {
        "priority": [
            {
                "item_name": "甲状腺超声 moderate_workup",
                "rationale": "甲状腺结节异常，甲状腺癌后验概率 8.2%，risk_tier=moderate_workup，建议专科复查",
            }
        ],
        "important": [],
        "maintain": [],
    }
    _, html = _render(tmp_path, timeline=timeline, x_addons=[])
    assert "moderate_workup" not in html
    assert "risk_tier" not in html
    assert "甲状腺癌后验概率 8.2%" in html


def test_empty_artifacts_no_crash(tmp_path):
    """5 artifact 全空（最小边界）→ StrictUndefined 不崩，section 结构常驻。"""
    _, html = _render(tmp_path, brca=False, jizaoan="unknown",
                      timeline={"priority": [], "important": [], "maintain": []},
                      x_addons=[], packages=[],
                      intervention={"genetic_management": [], "lifestyle": []}, liquid={})
    for marker in ["核心建议执行时间轴", "循证", "组合套餐", "长期健康干预"]:
        assert marker in html


def test_spec_fallback_from_detection_performance(tmp_path):
    """LLM 缺 specificity → 从检测性能库综合记录回填 99.0%。"""
    report, _ = _render(tmp_path, brca=False, jizaoan="negative",
                        liquid={"sensitivity": "81.9%", "market_price_range": "¥1,980-2,980",
                                "clinical_hint": "提示", "negative_risk_reduction": "降级"})
    assert report["liquid_biopsy_perf"]["specificity"] == "99.0%"


def test_sens_spec_always_from_detection_performance(tmp_path):
    """sens/spec 总从检测性能库综合记录覆盖 LLM 多口径值。"""
    report, _ = _render(tmp_path, brca=False, jizaoan="negative",
                        liquid={"sensitivity": "74.9%", "specificity": "99.5%",
                                "market_price_range": "¥1,980-2,980"})
    assert report["liquid_biopsy_perf"]["specificity"] == "99.0%"
    assert report["liquid_biopsy_perf"]["sensitivity"] == "81.9%"

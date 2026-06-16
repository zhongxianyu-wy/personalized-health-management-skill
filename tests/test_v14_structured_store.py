"""
契约测试 — evidence_store_v14 引擎兼容性验证
TDD: 先写失败测试，再实现，再验证通过。
"""
import json
import pathlib

ES = pathlib.Path("references/database/cancerrisk/json")

EXPECTED_CANCER_IDS = {
    "lung_cancer", "liver_cancer", "gastric_cancer", "esophageal_cancer",
    "colorectal_cancer", "breast_cancer", "cervical_cancer", "prostate_cancer",
    "bladder_cancer", "ovarian_cancer", "kidney_cancer", "head_neck_cancer",
    "biliary_tract_cancer", "thyroid_cancer", "pancreatic_cancer",
}


# ── 基础契约测试（原始 4 条，修正 applicable_sex 值集合）─────────────────────

def test_cancers_complete_required_fields():
    d = json.loads((ES / "cancers.json").read_text())
    ids = {c["cancer_id"] for c in d["cancers"]}
    assert EXPECTED_CANCER_IDS <= ids, f"Missing cancer_ids: {EXPECTED_CANCER_IDS - ids}"
    # 引擎代码：cancer.get("applicable_sex", "all") — 值必须是 "male"/"female"/"all"
    for c in d["cancers"]:
        assert c.get("applicable_sex") in {"male", "female", "all"}, (
            f"cancer_id={c['cancer_id']} has invalid applicable_sex={c.get('applicable_sex')!r}"
        )
        assert c.get("cancer_name_zh"), f"cancer_id={c['cancer_id']} missing cancer_name_zh"


def test_derived_assertions_engine_fields():
    d = json.loads((ES / "risk_assertions_derived.json").read_text())
    assert d["derived_assertions"], "derived_assertions is empty"
    for a in d["derived_assertions"]:
        for k in ("assertion_id", "cancer_id", "factor_id", "factor_level", "log_odds_delta"):
            assert k in a, f"Missing field {k!r} in derived assertion {a.get('assertion_id')}"


def test_detection_derived_engine_fields():
    d = json.loads((ES / "detection_performance_derived.json").read_text())
    for a in d["derived_detection_performance"]:
        for k in ("cancer_id", "test_id", "positive_log_odds_delta", "negative_log_odds_delta"):
            assert k in a, f"Missing field {k!r} in detection entry cancer_id={a.get('cancer_id')}"


def test_priors_have_missing_list():
    d = json.loads((ES / "cancer_age_sex_priors.json").read_text())
    assert "missing_priors" in d, "cancer_age_sex_priors.json missing top-level 'missing_priors'"


# ── 新增目标测试（验证实际落库的决策）─────────────────────────────────────────

def test_lynch_colorectal_assertions_usable():
    """Lynch断言已落库：MLH1/MSH2/MSH6/PMS2 → colorectal_cancer，且 conversion_status=usable"""
    d = json.loads((ES / "risk_assertions_derived.json").read_text())
    lynch_colorectal = [
        a for a in d["derived_assertions"]
        if a["cancer_id"] == "colorectal_cancer"
        and a["factor_id"].startswith("lynch_")
        and a.get("conversion_status") == "usable"
    ]
    factor_ids = {a["factor_id"] for a in lynch_colorectal}
    required = {"lynch_mlh1_carrier", "lynch_msh2_carrier", "lynch_msh6_carrier",
                "lynch_pms2_carrier", "lynch_unspecified_carrier"}
    assert required <= factor_ids, (
        f"Missing Lynch colorectal assertions: {required - factor_ids}"
    )


def test_lynch_gastric_assertions_usable():
    """Lynch断言已落库：MLH1/MSH2/unspecified → gastric_cancer，且 conversion_status=usable"""
    d = json.loads((ES / "risk_assertions_derived.json").read_text())
    lynch_gastric = [
        a for a in d["derived_assertions"]
        if a["cancer_id"] == "gastric_cancer"
        and a["factor_id"].startswith("lynch_")
        and a.get("conversion_status") == "usable"
    ]
    factor_ids = {a["factor_id"] for a in lynch_gastric}
    required = {"lynch_mlh1_carrier", "lynch_msh2_carrier", "lynch_unspecified_carrier"}
    assert required <= factor_ids, (
        f"Missing Lynch gastric assertions: {required - factor_ids}"
    )


def test_lynch_risk_factors_defined():
    """Lynch 5 个因子定义已在 risk_factors.json"""
    d = json.loads((ES / "risk_factors.json").read_text())
    fids = {f["factor_id"] for f in d["risk_factors"]}
    required = {
        "lynch_mlh1_carrier", "lynch_msh2_carrier", "lynch_msh6_carrier",
        "lynch_pms2_carrier", "lynch_unspecified_carrier",
    }
    assert required <= fids, f"Missing Lynch factor defs: {required - fids}"


def test_jizaoan_pancreatic_detection_exists():
    """吉早安胰腺癌条目已落库（产品覆盖8癌种，缺一不可）"""
    d = json.loads((ES / "detection_performance_derived.json").read_text())
    pancreatic_jizaoan = [
        e for e in d["derived_detection_performance"]
        if e["cancer_id"] == "pancreatic_cancer"
        and e["test_id"] == "jizaoan_multi_cancer_screening"
        and e.get("conversion_status") == "usable"
    ]
    assert pancreatic_jizaoan, "Missing jizaoan_multi_cancer_screening entry for pancreatic_cancer"


def test_head_neck_screening_has_npc_ebv():
    """head_neck_cancer 筛查推荐包含鼻咽癌 NPC EBV 抗体筛查"""
    d = json.loads((ES / "screening_recommendations.json").read_text())
    hn_entries = [r for r in d["recommendations"] if r["cancer_id"] == "head_neck_cancer"]
    assert hn_entries, "No head_neck_cancer entry in screening_recommendations"
    all_methods = " ".join(
        m.get("method", "") + m.get("population", "")
        for entry in hn_entries
        for m in entry.get("standard_screening", [])
    )
    assert "EBV" in all_methods or "鼻咽" in all_methods, (
        f"head_neck_cancer screening does not mention NPC/EBV. Methods: {all_methods!r}"
    )


def test_kidney_screening_states_not_recommended():
    """肾癌筛查推荐已标注一般人群不推荐"""
    d = json.loads((ES / "screening_recommendations.json").read_text())
    kidney_entries = [r for r in d["recommendations"] if r["cancer_id"] == "kidney_cancer"]
    assert kidney_entries, "No kidney_cancer entry in screening_recommendations"
    all_text = json.dumps(kidney_entries, ensure_ascii=False)
    assert "不推荐" in all_text, (
        f"kidney_cancer screening does not state 不推荐. Content: {all_text!r}"
    )


def test_biliary_tract_screening_has_psc_mri():
    """胆道癌筛查推荐：一般人群不推荐，PSC高危 → MRI/MRCP"""
    d = json.loads((ES / "screening_recommendations.json").read_text())
    biliary_entries = [r for r in d["recommendations"] if r["cancer_id"] == "biliary_tract_cancer"]
    assert biliary_entries, "No biliary_tract_cancer entry in screening_recommendations"
    all_text = json.dumps(biliary_entries, ensure_ascii=False)
    assert "不推荐" in all_text, "biliary_tract_cancer screening missing 不推荐 for general population"
    assert "MRI" in all_text or "MRCP" in all_text, (
        "biliary_tract_cancer screening missing MRI/MRCP for PSC high-risk"
    )


def test_thyroid_cancer_screening_entry_exists():
    """甲状腺癌筛查推荐条目已存在"""
    d = json.loads((ES / "screening_recommendations.json").read_text())
    ids = {r["cancer_id"] for r in d["recommendations"]}
    assert "thyroid_cancer" in ids, "Missing thyroid_cancer in screening_recommendations"


def test_pancreatic_cancer_screening_entry_exists():
    """胰腺癌筛查推荐条目已存在"""
    d = json.loads((ES / "screening_recommendations.json").read_text())
    ids = {r["cancer_id"] for r in d["recommendations"]}
    assert "pancreatic_cancer" in ids, "Missing pancreatic_cancer in screening_recommendations"


def test_pancreatic_risk_assertions_exist():
    """胰腺癌流行病学风险因子断言已落库（smoking/family_history/obesity/chronic_pancreatitis）"""
    d = json.loads((ES / "risk_assertions_derived.json").read_text())
    pancreatic_usable = [
        a for a in d["derived_assertions"]
        if a["cancer_id"] == "pancreatic_cancer"
        and a.get("conversion_status") == "usable"
        and not a["factor_id"].startswith("pancreas_mass")  # 排除现有占位因子
    ]
    assert pancreatic_usable, "No usable pancreatic epidemiologic risk assertions found"
    factor_ids = {a["factor_id"] for a in pancreatic_usable}
    # 至少要有吸烟 + 家族史两个核心因子（OR/RR 有来源）
    smoking_like = {f for f in factor_ids if "smok" in f}
    family_like = {f for f in factor_ids if "family" in f}
    assert smoking_like, f"No smoking assertion for pancreatic_cancer. factors={factor_ids}"
    assert family_like, f"No family history assertion for pancreatic_cancer. factors={factor_ids}"


def test_thyroid_risk_assertions_exist():
    """甲状腺癌风险因子断言已落库（family_history、儿童期放射）"""
    d = json.loads((ES / "risk_assertions_derived.json").read_text())
    thyroid_usable = [
        a for a in d["derived_assertions"]
        if a["cancer_id"] == "thyroid_cancer"
        and a.get("conversion_status") == "usable"
    ]
    assert thyroid_usable, "No usable thyroid_cancer risk assertions found"

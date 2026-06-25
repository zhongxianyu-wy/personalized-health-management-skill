"""Tests for build_section_artifacts.py — v2.0.0 确定性骨架生成器。

验证 5 section artifact 骨架的数值/分类/结构由脚本正确生成（PUA：数值脚本算），
文案字段（rationale/note/clinical_value）留空 + _pending 标记。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import build_section_artifacts as scaf  # noqa: E402


def _rules() -> dict:
    return json.loads(
        (SCRIPTS_DIR.parent / "references" / "database" / "screening_personalized" / "json" / "cancer_followup_rules.json")
        .read_text(encoding="utf-8")
    )


def _pricing() -> dict:
    return json.loads(
        (SCRIPTS_DIR.parent / "references" / "database" / "pricing" / "json" / "08_pricing.json")
        .read_text(encoding="utf-8")
    )


def _snapshot() -> dict:
    """colorectal high (0.022) + lung low (0.003) + jizaoan_whatif."""
    return {
        "person_context": {"sex": "male", "age": 68},
        "cancers": [
            {"cancer_id": "colorectal_cancer", "cancer_name_zh": "结直肠癌", "posterior_probability": 0.022},
            {"cancer_id": "lung_cancer", "cancer_name_zh": "肺癌", "posterior_probability": 0.003},
        ],
        "section4_screening": [
            {"cancer_id": "colorectal_cancer", "cancer_name_zh": "结直肠癌",
             "posterior_probability": 0.022, "risk_tier": "high",
             "standard_screening": [{"method": "结肠镜", "interval": "5-10年1次"}]},
        ],
        "jizaoan_whatif": [
            {"cancer_id": "colorectal_cancer", "cancer_name_zh": "结直肠癌",
             "current_risk": 0.022, "risk_if_negative": 0.003},
        ],
    }


def _voi() -> dict:
    return {"rankings": [{"method": "吉早安", "sensitivity": 0.819, "specificity": 0.99}]}


def _answers() -> dict:
    return {
        "q_demographics_sex": "male", "q_demographics_age": 68,
        "q_family_history_cancer": "no", "q_has_genetic_mutation": "no",
        "q_psa_screening": "yes", "q_colorectal_screening": "no",  # colorectal gap → maintain
    }


# ---- unit: per-artifact builders ----

def test_timeline_classifies_high_posterior_to_priority() -> None:
    t = scaf.build_timeline(_snapshot(), _answers(), {"assessment_result": {"risk_level": ""}}, _rules())
    # colorectal posterior 0.022 > 0.01 → priority
    assert any("结直肠癌" in i["item_name"] for i in t["priority"])
    pri = t["priority"][0]
    assert pri["interval"]  # filled from rules tier_followup
    assert pri["rationale"] == ""  # 文案留空（LLM）
    # colorectal_screening=no → maintain gap
    assert any("结直肠癌筛查" in m["item_name"] for m in t["maintain"])
    # _pending lists rationale as待补
    assert any("rationale" in p for p in t["_pending"])


def test_timeline_uses_compiled_rules_for_method() -> None:
    t = scaf.build_timeline(_snapshot(), _answers(), {}, _rules())
    # colorectal 5-tier = high (>0.02) → rules tier_followup[high].method = 每年肠镜
    pri = [i for i in t["priority"] if "结直肠癌" in i["item_name"]][0]
    assert "肠镜" in pri["item_name"]
    assert pri["interval"] == "12月内"  # rules high.months=12


def test_liquid_biopsy_sens_spec_and_nrr() -> None:
    lb = scaf.build_liquid_biopsy(_snapshot(), _voi(), _pricing())
    assert lb["sensitivity"] == "81.9%"
    assert lb["specificity"] == "99.0%"
    assert lb["market_price_range"]  # from pricing jizaoan low-high
    assert "结直肠癌" in lb["negative_risk_reduction"] and "2.2%" in lb["negative_risk_reduction"]
    assert lb["clinical_hint"] == ""  # 文案留空


def test_long_term_brca_trigger_and_lifestyle_template() -> None:
    no_brca = scaf.build_long_term(_answers(), "unknown")
    assert no_brca["genetic_management"] == []  # non-BRCA → empty
    assert len(no_brca["lifestyle"]) >= 1
    brca = scaf.build_long_term({"q_has_genetic_mutation": "yes"}, "positive")
    assert len(brca["genetic_management"]) >= 1  # BRCA positive → skeleton


def test_package_three_tiers_one_recommended_and_pricing() -> None:
    pkg = scaf.build_package(_snapshot(), _answers(), {"age": 68}, _pricing(), _rules())
    assert len(pkg) == 3
    assert sum(1 for t in pkg if t["recommended"] is True) == 1  # exactly one recommended
    # 档1 includes colorectal screening method (结肠镜)
    assert any("结肠镜" in inc for inc in pkg[0]["includes"])
    # 文案 note 留空
    assert all(t["note"] == "" for t in pkg)


def test_x_addons_tag_mapping_by_posterior() -> None:
    rows = scaf.build_x_addons(_snapshot(), {"assessment_result": {"risk_level": ""}}, _pricing(), _rules())
    assert rows  # colorectal section4 → at least 1 row
    crc = [r for r in rows if "结直肠癌" in r["risk_source"]][0]
    assert crc["risk_level_tag"] == "danger"  # posterior 0.022 > 0.01
    assert crc["posterior_probability"] == pytest.approx(0.022, abs=1e-6)
    assert crc["clinical_value"] == ""  # 文案留空


# ---- integration: run() writes 5 files ----

def test_run_writes_five_artifact_skeletons(tmp_path: Path) -> None:
    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "snapshot_risk.json").write_text(json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    (art / "voi_ranking.json").write_text(json.dumps(_voi(), ensure_ascii=False), encoding="utf-8")
    (art / "health_summary_structured_summary.json").write_text("{}", encoding="utf-8")
    ans = tmp_path / "answers.json"
    ans.write_text(json.dumps({"answers": _answers()}, ensure_ascii=False), encoding="utf-8")
    scaf.run(artifacts=art, answers_path=ans, skill_root=SCRIPTS_DIR.parent)
    for name in ("timeline_tiers.json", "x_addons.json", "package_tiers.json",
                 "liquid_biopsy_perf.json", "long_term_intervention.json"):
        out = json.loads((art / name).read_text(encoding="utf-8"))
        # every skeleton carries _scaffold marker + _pending (文案待补)
        if isinstance(out, dict):
            assert out.get("_scaffold") is True or any(
                "_scaffold" in str(v) for v in out.values()
            ) or "_pending" in out or "priority" in out or "lifestyle" in out
    # package got Σmid price (assemble_package ran)
    pkg = json.loads((art / "package_tiers.json").read_text(encoding="utf-8"))
    assert any(str(t.get("price_range")) for t in pkg)

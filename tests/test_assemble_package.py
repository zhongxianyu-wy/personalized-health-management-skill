"""Tests for assemble_package.py — deterministic Σmid package-price summation.

Covers the v0.1.3 CRITICAL fix: assemble_package must be wired into the
orchestrator (not an agent-only manual step), so package prices are always
script-derived and reproducible. Tests the function behaviour + a wiring
contract test so the integration cannot silently regress.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import assemble_package  # noqa: E402


PRICING = {
    "items": {
        "ldct": {"name": "低剂量胸部CT", "aliases": ["LDCT", "肺CT"], "mid": 500},
        "colonoscopy": {"name": "无痛肠镜", "aliases": ["肠镜"], "mid": 1000},
        "jizaoan": {"name": "吉早安", "aliases": ["吉早安多癌筛查"], "mid": 2480},
        "thyroid_us": {"name": "甲状腺彩超", "aliases": ["甲状腺超声"], "mid": 120},
    }
}


def _write_pkg(tmp_path: Path, tiers: list) -> Path:
    p = tmp_path / "package_tiers.json"
    p.write_text(json.dumps(tiers, ensure_ascii=False), encoding="utf-8")
    return p


def test_basic_mid_sum_overwrites_price(tmp_path: Path) -> None:
    """Σmid replaces whatever the LLM hand-filled in price_range."""
    p = _write_pkg(tmp_path, [{
        "name": "档1", "price_range": "LLM手填",
        "includes": ["LDCT", "甲状腺彩超"], "recommended": True,
    }])
    assemble_package.assemble_package(p, PRICING)
    out = json.loads(p.read_text(encoding="utf-8"))
    assert out[0]["price_range"] == "¥620"  # 500 + 120


def test_package_names_are_canonical(tmp_path: Path) -> None:
    """LLM 手写名称会被固定为报告设计三档名称。"""
    p = _write_pkg(tmp_path, [
        {"name": "档1", "includes": ["LDCT"], "recommended": False},
        {"name": "档2", "includes": ["LDCT", "甲状腺彩超"], "recommended": True},
    ])
    assemble_package.assemble_package(p, PRICING)
    out = json.loads(p.read_text(encoding="utf-8"))
    assert [t["name"] for t in out] == ["核心风险筛查档", "全面覆盖档"]
    assert out[0]["price_range"] == "¥500"
    assert out[1]["price_range"] == "¥620"


def test_pricing_keys_are_rendered_as_chinese_include_names(tmp_path: Path) -> None:
    """LLM 若直接写 pricing key，报告「包含」应展示中文项目名而不是内部字段。"""
    p = _write_pkg(tmp_path, [
        {"name": "档1", "includes": ["ldct", "thyroid_us"], "recommended": False},
        {"name": "档2", "includes": ["colonoscopy", "thyroid_us"], "recommended": True},
        {"name": "档3", "includes": [], "recommended": False},
    ])
    assemble_package.assemble_package(p, PRICING)
    out = json.loads(p.read_text(encoding="utf-8"))
    assert out[0]["includes"] == ["低剂量胸部CT", "甲状腺彩超"]
    assert out[1]["includes"] == ["无痛肠镜", "甲状腺彩超"]
    assert out[2]["includes"] == ["无痛肠镜", "甲状腺彩超", "吉早安"]
    assert "thyroid_us" not in json.dumps(out, ensure_ascii=False)


def test_deep_tier_does_not_double_count_jizaoan_when_llm_includes_it(tmp_path: Path) -> None:
    """档3固定按档2 +1999；LLM 在旧字段里写吉早安也不会重复计价。"""
    p = _write_pkg(tmp_path, [
        {"name": "档1", "includes": ["LDCT"], "recommended": False},
        {"name": "档2", "includes": ["LDCT", "甲状腺彩超", "吉早安"], "recommended": True},
        {"name": "档3", "includes": ["吉早安"], "includes_all": ["吉早安", "LDCT"], "recommended": False},
    ])
    assemble_package.assemble_package(p, PRICING)
    out = json.loads(p.read_text(encoding="utf-8"))
    assert out[2]["includes"] == ["低剂量胸部CT", "甲状腺彩超", "吉早安"]
    assert out[2]["price_range"] == "¥2619"
    assert out[2]["_pricing_detail"]["base_tier_price"] == 620
    assert out[2]["_pricing_detail"]["jizaoan_addon_price"] == 1999


def test_legacy_includes_all_removed_from_deep_tier(tmp_path: Path) -> None:
    """旧替代字段 includes_all 不再进入模板；档3直接继承档2并追加吉早安。"""
    p = _write_pkg(tmp_path, [
        {"name": "档1", "includes": ["LDCT"], "recommended": False},
        {"name": "档2", "includes": ["LDCT", "肠镜", "甲状腺彩超"], "recommended": True},
        {"name": "档3", "includes": [], "includes_all": ["LDCT", "肠镜", "甲状腺彩超"], "recommended": False},
    ])
    assemble_package.assemble_package(p, PRICING)
    out = json.loads(p.read_text(encoding="utf-8"))
    assert out[2]["includes"] == ["低剂量胸部CT", "无痛肠镜", "甲状腺彩超", "吉早安"]
    assert "includes_all" not in out[2]
    assert out[2]["price_range"] == "¥3619"


def test_canonical_package_names_and_deep_tier_adds_jizaoan_to_comprehensive(tmp_path: Path) -> None:
    """三档名称固定；档3=全面覆盖档 + 吉早安检测（+1999元），不再输出替代双价格。"""
    p = _write_pkg(tmp_path, [
        {"name": "LLM自定义1", "includes": ["LDCT"], "recommended": False},
        {"name": "LLM自定义2", "includes": ["LDCT", "甲状腺彩超"], "recommended": True},
        {
            "name": "LLM旧替换档",
            "includes": [],
            "includes_all": ["吉早安", "LDCT", "肠镜", "甲状腺彩超"],
            "recommended": False,
        },
    ])
    assemble_package.assemble_package(p, PRICING)
    out = json.loads(p.read_text(encoding="utf-8"))
    assert [t["name"] for t in out] == ["核心风险筛查档", "全面覆盖档", "癌症深入筛查档"]
    assert out[2]["includes"] == ["低剂量胸部CT", "甲状腺彩超", "吉早安"]
    assert "includes_all" not in out[2]
    assert out[1]["price_range"] == "¥620"
    assert out[2]["price_range"] == "¥2619"
    assert out[2]["_pricing_detail"]["base_tier_price"] == 620
    assert out[2]["_pricing_detail"]["jizaoan_addon_price"] == 1999


def test_unmatched_include_warned_not_crash(tmp_path: Path, capsys) -> None:
    """An include that matches no pricing item is warned on stderr, others still sum."""
    p = _write_pkg(tmp_path, [{
        "name": "档1", "includes": ["未知项目XYZ", "LDCT"], "recommended": True,
    }])
    assemble_package.assemble_package(p, PRICING)
    out = json.loads(p.read_text(encoding="utf-8"))
    assert out[0]["price_range"] == "¥500"  # only LDCT matched
    assert "未知项目XYZ" in capsys.readouterr().err


def test_load_pricing_real_file() -> None:
    """The real pricing DB is present and well-formed (F1 wiring depends on it)."""
    skill_root = Path(__file__).resolve().parent.parent
    pricing = assemble_package.load_pricing(skill_root)
    assert pricing is not None
    assert "items" in pricing and len(pricing["items"]) > 0
    for key in ("ldct", "colonoscopy", "jizaoan", "thyroid_us"):
        assert key in pricing["items"], f"missing pricing item: {key}"
    assert pricing["items"]["jizaoan"]["mid"] == 2480


def test_assemble_package_wired_into_orchestrator() -> None:
    """Contract test: the orchestrator must invoke assemble_package before report
    assembly. Guards against regression to the agent-only-manual-step PUA hole."""
    src = (
        Path(__file__).resolve().parent.parent / "scripts" / "run_formal_analysis.py"
    ).read_text(encoding="utf-8")
    assert "import assemble_package" in src
    assert "assemble_package.load_pricing(" in src
    assert "assemble_package.assemble_package(" in src

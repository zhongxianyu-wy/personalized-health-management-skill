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


def test_dual_price_includes_all(tmp_path: Path) -> None:
    """Tier-3 jizaoan replace/compensate: price1=jizaoan+未被替代, price2=jizaoan+全部."""
    p = _write_pkg(tmp_path, [{
        "name": "档3", "price_range": "占位",
        "includes": ["甲状腺彩超"],               # 未被替代项 → price1
        "includes_all": ["LDCT", "甲状腺彩超"],    # 全部推荐项 → price2
        "recommended": False,
    }])
    assemble_package.assemble_package(p, PRICING)
    out = json.loads(p.read_text(encoding="utf-8"))
    # price1 = 2480 + 120 = 2600; price2 = 2480 + 500 + 120 = 3100
    assert out[0]["price_range"] == "¥2600 / ¥3100"
    assert out[0]["_pricing_detail"]["price1"] == 2600
    assert out[0]["_pricing_detail"]["price2"] == 3100


def test_dual_price_does_not_double_count_jizaoan_when_llm_includes_it(tmp_path: Path) -> None:
    """If LLM writes 吉早安 in includes/includes_all, assemble adds it only once."""
    p = _write_pkg(tmp_path, [{
        "name": "档3",
        "includes": ["吉早安", "甲状腺彩超"],
        "includes_all": ["吉早安", "LDCT", "甲状腺彩超"],
        "recommended": False,
    }])
    assemble_package.assemble_package(p, PRICING)
    out = json.loads(p.read_text(encoding="utf-8"))
    assert out[0]["price_range"] == "¥2600 / ¥3100"
    assert out[0]["_pricing_detail"]["price1"] == 2600
    assert out[0]["_pricing_detail"]["price2"] == 3100
    assert "吉早安" not in " ".join(out[0]["_pricing_detail"]["price1_items"])


def test_dual_price_derives_unreplaced_items_from_all_items(tmp_path: Path) -> None:
    """When only includes_all is supplied, derive price1 by excluding replaceable cancer-screen items."""
    p = _write_pkg(tmp_path, [{
        "name": "档3",
        "includes": [],
        "includes_all": ["LDCT", "肠镜", "甲状腺彩超"],
        "recommended": False,
    }])
    assemble_package.assemble_package(p, PRICING)
    out = json.loads(p.read_text(encoding="utf-8"))
    assert out[0]["includes"] == ["甲状腺彩超"]
    assert out[0]["price_range"] == "¥2600 / ¥4100"
    assert out[0]["_pricing_detail"]["price1"] == 2600
    assert out[0]["_pricing_detail"]["price2"] == 4100


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

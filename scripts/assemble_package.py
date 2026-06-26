#!/usr/bin/env python3
"""确定性套餐价格求和（P0 [DATA-4] 修复）。

读 package_tiers.json 的 includes[] → join pricing/json/08_pricing.json →
Σmid 更新 price_range（具体数字，非区间）。解决 LLM 手填价格不可复现问题。

三档名称固定：
  1. 核心风险筛查档
  2. 全面覆盖档
  3. 癌症深入筛查档

档3固定为「全面覆盖档 + 吉早安检测（+1999元）」：
  档3价格 = 档2脚本价格 + 1999
  档3包含 = 档2 includes + 吉早安

用法：
    python scripts/assemble_package.py --package <out>/artifacts/package_tiers.json \\
        --skill-root <skill_root>

LLM 产 package_tiers.json 时只需写 includes 项目名（匹配 pricing aliases）+ note，
price_range 留空或任意，本脚本确定性覆写为 Σmid。
"""
from __future__ import annotations

import sys as _sys
_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import _env_bootstrap  # noqa: F401 — 跨runtime环境自检(PYTHONHOME/UTF-8)

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


JIZAOAN_KEY = "jizaoan"
JIZAOAN_DISPLAY_NAME = "吉早安"
JIZAOAN_ADDON_PRICE = 1999
CANONICAL_PACKAGE_NAMES = ("核心风险筛查档", "全面覆盖档", "癌症深入筛查档")


def load_pricing(skill_root: Path) -> dict[str, Any] | None:
    p = skill_root / "references" / "database" / "pricing" / "json" / "08_pricing.json"
    if not p.is_file():
        print(f"FAIL: {p} not found", file=sys.stderr)
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _clean(s: str) -> str:
    """清理 include 字符串：去括号内容/数字/空格，便于匹配 pricing aliases。"""
    s = re.sub(r"[（(].*?[)）]", "", str(s))
    s = re.sub(r"\d+", "", s)
    return s.strip().rstrip("：:，,、 ")


def _trigrams(s: str) -> set[str]:
    """中文 3-gram 片段集合（用于保守模糊匹配；3-gram 比 2-gram 更具区分度，降低误匹配）。"""
    s = _clean(s)
    return {s[i:i + 3] for i in range(len(s) - 2)} if len(s) >= 3 else ({s} if s else set())


def match_item(include_str: str, items: dict[str, Any]) -> tuple[str | None, dict | None]:
    """匹配 include → (pricing_key, item)。三级：①精确 name/alias ②包含 ③保守 3-gram 重叠（兜底，
    处理 LLM 命名差异如「颈部淋巴结超声」↔「甲状腺及颈部淋巴结彩超」；阈值≥3 重叠防误匹配）。"""
    cleaned = _clean(include_str)
    if not cleaned:
        return None, None
    # 0. JSON key 精确匹配（LLM 可能直接用 pricing key 如 "thyroid_us"/"ldct"）
    raw = str(include_str).strip()
    if raw in items:
        return raw, items[raw]
    # ① 精确匹配 name/aliases
    for key, item in items.items():
        if cleaned == _clean(item.get("name", "")):
            return key, item
        for alias in item.get("aliases", []):
            if cleaned == _clean(alias):
                return key, item
    # ② 包含匹配（cleaned 含 alias 或 alias 含 cleaned）
    for key, item in items.items():
        for alias in item.get("aliases", []):
            a = _clean(alias)
            if a and (a in cleaned or cleaned in a):
                return key, item
    # ③ 保守 3-gram 重叠兜底（LLM 命名差异）
    inc_grams = _trigrams(cleaned)
    if not inc_grams:
        return None, None
    best_key, best_item, best_overlap = None, None, 0
    for key, item in items.items():
        candidates = [item.get("name", "")] + item.get("aliases", [])
        for c in candidates:
            overlap = len(inc_grams & _trigrams(c))
            if overlap > best_overlap:
                best_key, best_item, best_overlap = key, item, overlap
    if best_overlap >= 3:  # ≥3 个 3-gram 重叠才认（保守，防误匹配）
        return best_key, best_item
    return None, None



def assemble_package(package_path: Path, pricing: dict[str, Any]) -> list[dict]:
    """对 package_tiers.json 每档 includes 求和 Σmid，覆写 price_range。"""
    pkg = json.loads(package_path.read_text(encoding="utf-8"))
    items = pricing.get("items", {})

    def _sum_mid(item_list: list) -> tuple[int, list[str], list[str]]:
        total, matched, unmatched = 0, [], []
        for inc in item_list:
            text = str(inc)
            key, item = match_item(text, items)
            if item:
                total += item["mid"]
                matched.append(f"{item['name']}({item['mid']})")
            else:
                unmatched.append(text)
        return total, matched, unmatched

    def _strip_jizaoan(item_list: list) -> list:
        out = []
        for inc in item_list:
            key, _ = match_item(str(inc), items)
            if key != JIZAOAN_KEY:
                out.append(inc)
        return out

    def _display_include_names(item_list: list) -> list[str]:
        """模板展示字段：匹配到 pricing 的项目统一写中文 name；未匹配保留原文。"""
        out: list[str] = []
        for inc in item_list:
            key, item = match_item(str(inc), items)
            if key == JIZAOAN_KEY:
                out.append(JIZAOAN_DISPLAY_NAME)
            elif item:
                out.append(str(item.get("name") or inc))
            else:
                out.append(str(inc))
        return out

    def _price_from_tier(tier: dict) -> int:
        detail = tier.get("_pricing_detail", {})
        for key in ("sum_mid", "total", "price"):
            value = detail.get(key)
            if isinstance(value, int):
                return value
        text = str(tier.get("price_range", ""))
        m = re.search(r"¥?\s*([0-9][0-9,]*)", text)
        return int(m.group(1).replace(",", "")) if m else 0

    for idx, tier in enumerate(pkg):
        if idx < len(CANONICAL_PACKAGE_NAMES):
            tier["name"] = CANONICAL_PACKAGE_NAMES[idx]
        if idx == 2 and len(pkg) >= 2:
            continue
        includes = tier.get("includes", [])
        if not isinstance(includes, list):
            includes = []
        if idx < 2:
            includes = _strip_jizaoan(includes)
        total_mid, matched, unmatched = _sum_mid(includes)
        tier["includes"] = _display_include_names(includes)

        if total_mid > 0:
            tier["price_range"] = f"¥{total_mid}"

        tier["_pricing_detail"] = {
            "matched": matched, "unmatched": unmatched, "sum_mid": total_mid,
        }
        if unmatched:
            print(
                f"[assemble_package] ⚠ {tier.get('name', '?')}: "
                f"未匹配 pricing 的 includes: {unmatched}",
                file=sys.stderr,
            )

    if len(pkg) >= 3:
        base_tier = pkg[1] if isinstance(pkg[1], dict) else {}
        deep_tier = pkg[2] if isinstance(pkg[2], dict) else {}
        base_includes = base_tier.get("includes", [])
        if not isinstance(base_includes, list) or not base_includes:
            base_includes = deep_tier.get("includes", [])
        if not isinstance(base_includes, list):
            base_includes = []
        deep_includes = _display_include_names(_strip_jizaoan(base_includes)) + [JIZAOAN_DISPLAY_NAME]
        base_price = _price_from_tier(base_tier)
        total_price = base_price + JIZAOAN_ADDON_PRICE
        deep_tier["name"] = CANONICAL_PACKAGE_NAMES[2]
        deep_tier["includes"] = deep_includes
        deep_tier.pop("includes_all", None)
        deep_tier["price_range"] = f"¥{total_price}"
        deep_tier["note"] = "在全面覆盖档基础上增加吉早安检测（+1999元）"
        deep_tier["_pricing_detail"] = {
            "base_tier_name": base_tier.get("name", CANONICAL_PACKAGE_NAMES[1]),
            "base_tier_price": base_price,
            "jizaoan_addon_price": JIZAOAN_ADDON_PRICE,
            "total": total_price,
        }
        print(
            f"  [assemble] {deep_tier.get('name','?')}: "
            f"{base_price}+{JIZAOAN_ADDON_PRICE}={total_price} "
            "（全面覆盖档+吉早安）"
        )

    package_path.write_text(
        json.dumps(pkg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return pkg


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, help="package_tiers.json 路径")
    parser.add_argument("--skill-root", required=True, help="skill 根目录")
    args = parser.parse_args()

    pricing = load_pricing(Path(args.skill_root))
    if pricing is None:
        sys.exit(2)

    pkg = assemble_package(Path(args.package), pricing)
    for t in pkg:
        d = t.get("_pricing_detail", {})
        print(
            f"  {t.get('name', '?')}: price_range={t.get('price_range', '?')} "
            f"(Σmid={d.get('sum_mid', 0)}, matched={len(d.get('matched', []))}, "
            f"unmatched={len(d.get('unmatched', []))})"
        )


if __name__ == "__main__":
    main()

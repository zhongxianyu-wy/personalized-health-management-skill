#!/usr/bin/env python3
"""确定性套餐价格求和（P0 [DATA-4] 修复）。

读 package_tiers.json 的 includes[] → join pricing/json/08_pricing.json →
Σmid 更新 price_range（具体数字，非区间）。解决 LLM 手填价格不可复现问题。

档3（吉早安替换/弥补）的 "价格1/价格2" 双价格：
  price1 = 吉早安 mid + 未被替代项 mid 之和
  price2 = 吉早安 mid + 所有推荐项 mid 之和
  assemble 时需 LLM 在 includes 标注分类（本脚本按 includes 顺序求和）。

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

    for tier in pkg:
        includes = tier.get("includes", [])
        # 双价格模式（档3 吉早安替换/弥补：includes=未被替代项, includes_all=全部推荐项）
        includes_all = tier.get("includes_all")
        if isinstance(includes_all, list) and includes_all:
            jz_mid = items.get("jizaoan", {}).get("mid", 2480)
            def _sum_mid(item_list: list) -> tuple[int, list[str]]:
                s, m = 0, []
                for inc in item_list:
                    _, item = match_item(str(inc), items)
                    if item:
                        s += item["mid"]
                        m.append(f"{item['name']}({item['mid']})")
                return s, m
            sum1, m1 = _sum_mid(includes)
            sum2, m2 = _sum_mid(includes_all)
            tier["price_range"] = f"¥{jz_mid + sum1} / ¥{jz_mid + sum2}"
            tier["_pricing_detail"] = {
                "price1_items": m1, "price2_items": m2,
                "jizaoan_mid": jz_mid, "price1": jz_mid + sum1, "price2": jz_mid + sum2,
            }
            print(f"  [assemble] {tier.get('name','?')}: "
                  f"双价格 {jz_mid + sum1}/{jz_mid + sum2} "
                  f"(吉早安{jz_mid}+未替代{sum1} / 吉早安{jz_mid}+全部{sum2})")
            continue
        total_mid = 0
        matched: list[str] = []
        unmatched: list[str] = []

        for inc in includes:
            # includes 可能是 str 或含"价格1/价格2"标注的复杂格式
            text = str(inc)
            key, item = match_item(text, items)
            if item:
                total_mid += item["mid"]
                matched.append(f"{item['name']}({item['mid']})")
            else:
                unmatched.append(text)

        if total_mid > 0:
            # 档3 双价格：如果 price_range 含 "/"（如"价格1/价格2"），保留双价格格式
            existing = str(tier.get("price_range", ""))
            if "/" in existing and "价格" in existing:
                # 双价格档：price_range 已含 LLM 标注的 price1/price2，只附 Σmid 到 note
                tier["_pricing_sum_mid"] = total_mid
            else:
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

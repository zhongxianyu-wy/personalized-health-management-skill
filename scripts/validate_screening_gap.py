#!/usr/bin/env python3
"""CP5 筛查缺口校验器（v2.0.4 大幅精简）。

设计原则：只校验核心逻辑正确性（丢失完整性），不做字符级/字段级过度校验。
LLM 产 1 个 screening_recommendations.json（含 A/B/C 三段 + gap 问答内嵌）。

仅 3 条核心校验：
1. A/B/C dedup_key 无跨段重复
2. done+normal 不进 periodic_management
3. cancer_risk 只含 snapshot medium+
"""
from __future__ import annotations

import sys as _sys
_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import _env_bootstrap  # noqa: F401

import argparse
import json
import sys
from pathlib import Path
from typing import Any

MEDIUM_OR_ABOVE = {"medium", "high", "very_high", "moderate_workup", "high_workup", "urgent_workup"}
ABC_SECTIONS = ("cancer_risk", "other_abnormalities", "periodic_management")


def validate(payload: Any, snapshot: Any = None) -> list[str]:
    """核心校验：返回错误列表（空=通过）。只查逻辑错误，不查字段格式。"""
    if not isinstance(payload, dict):
        return ["screening_recommendations.json 必须是 JSON object"]

    errors: list[str] = []

    # 1. A/B/C 三段必须存在且是 list（完整性检查，不要求精确字段名）
    sections = {}
    for sec in ABC_SECTIONS:
        val = payload.get(sec)
        if val is None:
            errors.append(f"缺少 {sec} 段（应为 list）")
        elif not isinstance(val, list):
            errors.append(f"{sec} 应为 list，实际 {type(val).__name__}")
        else:
            sections[sec] = val

    if errors:
        return errors  # 结构都不对，后面的检查无意义

    # 2. dedup_key 跨段去重检查（核心逻辑错误）
    seen: dict[str, str] = {}
    for sec_name, rows in sections.items():
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("dedup_key") or "").strip()
            if not key:
                continue
            if key in seen:
                errors.append(f"dedup_key「{key}」在 {seen[key]} 和 {sec_name} 重复")
            else:
                seen[key] = sec_name

    # 3. done+normal 不应进 periodic_management（核心逻辑错误）
    for row in sections.get("periodic_management", []):
        if not isinstance(row, dict):
            continue
        disposition = str(row.get("disposition") or "").strip().lower()
        gap_answer = str(row.get("gap_answer") or "").strip().lower()
        # 检测 done+normal 误入（disposition 或 gap_answer 含 done+normal）
        if "done_normal" in disposition or ("done" in disposition and "normal" in disposition):
            errors.append(
                f"periodic_management/{row.get('dedup_key', '?')}: "
                "done+normal 不应进入周期管理（应排除）"
            )
        if "done" in gap_answer and "normal" in gap_answer:
            errors.append(
                f"periodic_management/{row.get('dedup_key', '?')}: "
                "gap_answer 显示做过且正常，不应进入周期管理"
            )

    # 4. cancer_risk 只含 snapshot medium+（核心优先级错误，需 snapshot 输入）
    if snapshot and isinstance(snapshot, dict):
        valid_cancer_ids = {
            str(c.get("cancer_id"))
            for c in snapshot.get("cancers", [])
            if isinstance(c, dict)
            and c.get("risk_tier") in MEDIUM_OR_ABOVE
        }
        for row in sections.get("cancer_risk", []):
            if not isinstance(row, dict):
                continue
            cid = str(row.get("cancer_id") or "").strip()
            if cid and valid_cancer_ids and cid not in valid_cancer_ids:
                errors.append(
                    f"cancer_risk/{row.get('dedup_key', '?')}: "
                    f"cancer_id「{cid}」在 snapshot 中非 medium+（不应在 A 段）"
                )

    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description="CP5 筛查缺口校验（v2.0.4 精简版）")
    parser.add_argument("--input", required=True, help="screening_recommendations.json 路径")
    parser.add_argument("--snapshot", default=None, help="snapshot_risk.json（可选，用于 cancer medium+ 检查）")
    args = parser.parse_args()

    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    snapshot = None
    if args.snapshot:
        snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))

    errors = validate(payload, snapshot)
    if errors:
        for e in errors:
            print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
    print("OK: CP5 校验通过（3 条核心规则：dedup / done+normal / medium+）")


if __name__ == "__main__":
    main()

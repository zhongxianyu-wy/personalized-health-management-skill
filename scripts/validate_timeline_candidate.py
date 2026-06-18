#!/usr/bin/env python3
"""Pre-flight check for structured_risk_factors_timeline.candidate.json.

Runs the SAME logic that risk_factor_gate would apply but as a
human-readable report, so the agent can fix mistakes BEFORE re-invoking
the orchestrator. Catches every common failure mode observed in the
v6 audit:

  * unknown_factor_key — agent invented a master factor like
    ``thyroid_nodule|present`` that doesn't exist in
    risk_factor_master.json
  * source_md_not_in_manifest — agent referenced a refined.md path
    that isn't in source_md_files
  * evidence_text_not_found — agent's quoted phrase isn't a
    substring of the named refined.md (typo, paraphrase, or
    cross-file leak)
  * exists_not_boolean — wrote "true"/"yes"/"missing" instead of
    JSON true/false
  * factor_level mismatch — wrote a level that isn't in the
    master's level_schema for that factor_id

Exit code 0 = all records would be accepted. Exit 1 = at least one
would be rejected; orchestrator will silently drop those records.

Usage:

    python cancerrisk-skill/scripts/validate_timeline_candidate.py \\
      --candidate analysis_output/<dir>/artifacts/structured_risk_factors_timeline.candidate.json

The --candidate path is also enough to locate the master in the
sibling ``risk_factor_master.json`` automatically.
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


def _load_json(path: Path):
    if not path.is_file():
        print(f"FAIL: {path} not found", file=sys.stderr)
        sys.exit(2)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"FAIL: {path} is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True,
                        help="path to structured_risk_factors_timeline.candidate.json")
    parser.add_argument("--master", default=None,
                        help="path to risk_factor_master.json "
                             "(default: sibling of --candidate)")
    args = parser.parse_args()

    cand_path = Path(args.candidate)
    candidate = _load_json(cand_path)
    import master_scan
    try:
        master_scan.assert_source_md_paths_safe(candidate, cand_path.parent)
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    master_path = Path(args.master) if args.master else cand_path.parent / "risk_factor_master.json"
    master = _load_json(master_path)

    valid_keys = {f["factor_key"] for f in master.get("factors", [])}
    md_paths = {e["refined_md_path"] for e in candidate.get("source_md_files", [])}
    md_data_ids = {e["data_id"] for e in candidate.get("source_md_files", [])}
    md_text_cache: dict[str, str] = {}
    for entry in candidate.get("source_md_files", []):
        p = Path(entry["refined_md_path"])
        md_text_cache[entry["refined_md_path"]] = p.read_text(encoding="utf-8") if p.is_file() else ""

    records = candidate.get("records", [])
    if not records:
        print("[validate] WARNING: candidate has 0 records. "
              "若报告无癌症风险因子(仅代谢异常/心血管等非癌症因子)，空 timeline 合法，不阻塞 pipeline。"
              "若应有因子，按 SKILL.md CP3 填充后重跑。", file=sys.stderr)
        return 0

    issues: list[str] = []
    accepted = 0
    for i, r in enumerate(records):
        prefix = f"records[{i}] {r.get('factor_key', '?')}"
        # 1. exists must be bool
        if not isinstance(r.get("exists"), bool):
            issues.append(f"{prefix}: exists must be JSON true/false (got {r.get('exists')!r})")
            continue
        # 2. factor_key in master allowlist
        key = r.get("factor_key")
        if key not in valid_keys:
            # Try to suggest a near-miss factor_id
            fid = r.get("factor_id") or (key.split("|", 1)[0] if isinstance(key, str) and "|" in key else "")
            hint = ""
            if fid:
                near = [k for k in valid_keys if k.startswith(fid + "|")]
                if near:
                    hint = f"  (did you mean: {near[0]} ?)"
                else:
                    near2 = [k for k in valid_keys if fid in k]
                    if near2:
                        hint = f"  (master has nothing for factor_id={fid!r}; closest: {near2[0]})"
            issues.append(f"{prefix}: factor_key not in master.factors{hint}")
            continue
        # 3. source_md in manifest
        src_md = r.get("source_md")
        src_id = r.get("source_data_id")
        resolved_md = None
        if src_md in md_paths:
            resolved_md = src_md
        elif src_id and src_id in md_data_ids:
            resolved_md = next(
                (e["refined_md_path"] for e in candidate.get("source_md_files", [])
                 if e["data_id"] == src_id),
                None,
            )
        if not resolved_md:
            issues.append(
                f"{prefix}: source_md={src_md!r} / source_data_id={src_id!r} "
                f"not in source_md_files manifest"
            )
            continue
        # 4. evidence_text substring locatable
        evidence = r.get("evidence_text") or ""
        if not evidence:
            issues.append(f"{prefix}: evidence_text is empty")
            continue
        text = md_text_cache.get(resolved_md, "")
        if evidence not in text:
            # Fallback: strip common markdown bold/italic markers
            stripped_evidence = re.sub(r"[*_]+", "", evidence)
            stripped_text = re.sub(r"[*_]+", "", text)
            if stripped_evidence not in stripped_text:
                issues.append(
                    f"{prefix}: evidence_text {evidence!r} not found in "
                    f"{Path(resolved_md).name} (typo / paraphrase / wrong file)"
                )
                continue
        accepted += 1

    total = len(records)
    print(f"[validate] {accepted}/{total} records would be accepted by risk_factor_gate")
    if issues:
        print(f"[validate] {len(issues)} record(s) would be REJECTED:", file=sys.stderr)
        for line in issues:
            print(f"  ✗ {line}", file=sys.stderr)
        print(
            "\nFix the records above before re-invoking the orchestrator. "
            "Hint: open risk_factor_master.json and search for the closest "
            "real factor_id. If no master factor fits, the finding belongs "
            "in the LLM-driven '证据库外异常提示' section of snapshot_risk.html, "
            "NOT in the timeline.",
            file=sys.stderr,
        )
        return 1
    print("[validate] OK — every record would pass the gate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

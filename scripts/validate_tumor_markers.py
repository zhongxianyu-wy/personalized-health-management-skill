#!/usr/bin/env python3
"""Pre-flight check for tumor_markers.candidate.json (CP3.6).

Runs the SAME logic as ``gate_tumor_markers`` but reports human-readable
errors per record. Catches every gate rejection mode:

  * unknown_test_id — agent invented a test_id not in
    evidence_store/assertions/detection_performance.json
  * invalid_result — must be "positive" or "negative" (not "high",
    "elevated", "正常", or anything else)
  * source_md_not_in_manifest — referenced refined.md not declared
  * evidence_text_not_found — phrase isn't a substring of named refined.md
  * missing_evidence_text — agent omitted the quote

Usage:
    python cancerrisk-skill/scripts/validate_tumor_markers.py \\
      --candidate <out>/artifacts/tumor_markers.candidate.json
"""

from __future__ import annotations

import sys as _sys
_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import _env_bootstrap  # noqa: F401 — 跨runtime环境自检(PYTHONHOME/UTF-8)

import argparse
import json
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent


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
                        help="path to tumor_markers.candidate.json")
    parser.add_argument(
        "--detection-derived",
        default=str(SKILL_ROOT / "references" / "database" / "cancerrisk" / "json" / "detection_performance_derived.json"),
        help="path to detection_performance_derived.json",
    )
    args = parser.parse_args()

    candidate_path = Path(args.candidate)
    candidate = _load_json(candidate_path)
    import master_scan
    try:
        master_scan.assert_source_md_paths_safe(candidate, candidate_path.parent)
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    derived = _load_json(Path(args.detection_derived))
    valid_test_ids = {
        d["test_id"] for d in derived.get("derived_detection_performance", [])
        if d.get("test_id") and d["test_id"] != "jizaoan_multi_cancer_screening"
    }

    md_paths = {e["refined_md_path"] for e in candidate.get("source_md_files", [])}
    md_data_ids = {e["data_id"] for e in candidate.get("source_md_files", [])}
    md_text: dict[str, str] = {}
    for entry in candidate.get("source_md_files", []):
        p = Path(entry["refined_md_path"])
        md_text[entry["refined_md_path"]] = p.read_text(encoding="utf-8") if p.is_file() else ""

    tests = candidate.get("tests", [])
    if not tests:
        print("[validate_tumor_markers] WARNING: 0 tumor marker records. "
              "Most patient reports include AFP/CEA/T-PSA — if absent, "
              "verify the refined.md kept tumor marker rows (recipe was "
              "updated in v6 P1 to keep them even when normal).")
        return 0

    issues: list[str] = []
    accepted = 0
    for i, t in enumerate(tests):
        prefix = f"tests[{i}] {t.get('test_id','?')} cancer={t.get('cancer_id','?')}"
        tid = t.get("test_id")
        if tid not in valid_test_ids:
            near = [k for k in valid_test_ids if tid and tid.split('_')[0] in k]
            hint = f"  (did you mean: {near[0]}?)" if near else ""
            issues.append(f"{prefix}: test_id not in detection_performance allowlist{hint}")
            continue
        if t.get("result") not in ("positive", "negative"):
            issues.append(
                f"{prefix}: result={t.get('result')!r} invalid — must be exactly "
                "'positive' (≥threshold ↑) or 'negative' (in-range)"
            )
            continue
        src_md = t.get("source_md")
        src_id = t.get("source_data_id")
        resolved = None
        if src_md in md_paths:
            resolved = src_md
        elif src_id and src_id in md_data_ids:
            resolved = next(
                (e["refined_md_path"] for e in candidate.get("source_md_files", [])
                 if e["data_id"] == src_id),
                None,
            )
        if not resolved:
            issues.append(f"{prefix}: source_md={src_md!r} / source_data_id={src_id!r} not in manifest")
            continue
        evidence = t.get("evidence_text") or ""
        if not evidence:
            issues.append(f"{prefix}: evidence_text empty")
            continue
        if evidence not in md_text.get(resolved, ""):
            issues.append(
                f"{prefix}: evidence_text {evidence!r} not in {Path(resolved).name}"
            )
            continue
        accepted += 1

    total = len(tests)
    print(f"[validate_tumor_markers] {accepted}/{total} tests would be accepted")
    if issues:
        print(f"[validate_tumor_markers] {len(issues)} would be REJECTED:", file=sys.stderr)
        for line in issues:
            print(f"  ✗ {line}", file=sys.stderr)
        print(
            "\nFix the records above before re-invoking the orchestrator. "
            "Open tumor_markers.candidate.json → valid_test_ids for the "
            "allowed list.",
            file=sys.stderr,
        )
        return 1
    print("[validate_tumor_markers] OK — every record would pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

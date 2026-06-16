#!/usr/bin/env python3
"""Pre-flight check for imaging_findings.candidate.json (CP3.5).

Runs the same rejection logic as ``gate_imaging_findings`` but reports
each problem in human-readable form so the agent can fix mistakes
BEFORE re-invoking the orchestrator. Catches:

  * unknown_finding_id — agent invented a finding_id not in
    evidence_store/ontology/imaging_findings.json
  * source_md_not_in_manifest — referenced refined.md not declared
  * evidence_text_not_found — phrase isn't a substring of the
    named refined.md
  * missing_evidence_text — agent omitted the quote

Exit code 0 = all findings accepted. Exit 1 = at least one rejected.

Usage:

    python cancerrisk-skill/scripts/validate_imaging_findings.py \\
      --candidate analysis_output/<dir>/artifacts/imaging_findings.candidate.json
"""

from __future__ import annotations

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
                        help="path to imaging_findings.candidate.json")
    parser.add_argument("--ontology", default=str(SKILL_ROOT / "references" / "database" / "cancerrisk" / "json" / "imaging_findings.json"),
                        help="ontology source (default: evidence_store/ontology/imaging_findings.json)")
    args = parser.parse_args()

    candidate = _load_json(Path(args.candidate))
    ontology = _load_json(Path(args.ontology))
    valid_ids = {f["finding_id"] for f in ontology.get("findings", [])}
    md_paths = {e["refined_md_path"] for e in candidate.get("source_md_files", [])}
    md_data_ids = {e["data_id"] for e in candidate.get("source_md_files", [])}
    md_text: dict[str, str] = {}
    for entry in candidate.get("source_md_files", []):
        p = Path(entry["refined_md_path"])
        md_text[entry["refined_md_path"]] = p.read_text(encoding="utf-8") if p.is_file() else ""

    findings = candidate.get("findings", [])
    if not findings:
        print("[validate_imaging] WARNING: candidate has 0 findings. "
              "If the report has no imaging lesions, that's fine; "
              "otherwise the Imaging findings recipe was skipped.")
        return 0

    issues: list[str] = []
    accepted = 0
    for i, f in enumerate(findings):
        prefix = f"findings[{i}] {f.get('finding_id', '?')}"
        fid = f.get("finding_id")
        if fid not in valid_ids:
            near = [k for k in valid_ids if fid and fid.split('_')[0] in k]
            hint = f"  (did you mean: {near[0]}?)" if near else ""
            issues.append(f"{prefix}: finding_id not in imaging ontology{hint}")
            continue
        src_md = f.get("source_md")
        src_id = f.get("source_data_id")
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
        evidence = f.get("evidence_text") or ""
        if not evidence:
            issues.append(f"{prefix}: evidence_text is empty")
            continue
        if evidence not in md_text.get(resolved, ""):
            issues.append(
                f"{prefix}: evidence_text {evidence!r} not found in "
                f"{Path(resolved).name} (typo / paraphrase / wrong file)"
            )
            continue
        accepted += 1

    total = len(findings)
    print(f"[validate_imaging] {accepted}/{total} findings would be accepted")
    if issues:
        print(f"[validate_imaging] {len(issues)} finding(s) would be REJECTED:", file=sys.stderr)
        for line in issues:
            print(f"  ✗ {line}", file=sys.stderr)
        print(
            "\nFix the findings above before re-invoking the orchestrator. "
            "Open imaging_findings.candidate.json → valid_finding_ids for "
            "the allowed list. If a clinical lesion in the report has no "
            "matching finding_id (e.g. a rare site or grade), do NOT "
            "force-fit; the finding will surface in the LLM-driven "
            "'证据库外异常提示' section instead.",
            file=sys.stderr,
        )
        return 1
    print("[validate_imaging] OK — every finding would pass the gate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

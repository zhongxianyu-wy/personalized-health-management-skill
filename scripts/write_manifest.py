#!/usr/bin/env python3
"""Task9 manifest writer.

Collects run-level metadata (evidence version, MinerU token source,
per-stage outputs, archive root, audit notes) into a single
``manifest.json`` that downstream consumers and the index router page
can rely on.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = "manifest-v1"


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _audit_notes(audit_dir: Path) -> list[dict[str, str]]:
    if not audit_dir.is_dir():
        return []
    return [
        {"name": p.name, "path": str(p)}
        for p in sorted(audit_dir.iterdir())
        if p.is_file()
    ]


def build_manifest(
    *,
    output_dir: Path,
    artifacts: Path,
    evidence_store: Path,
    archives_root: Path,
    person_id: str,
    run_id: str,
    input_path: str | None = None,
) -> dict[str, Any]:
    evidence_version = _read_json(evidence_store / "evidence_version.json", {}) or {}
    mineru_manifest = _read_json(artifacts / "conversion_manifest.json", {}) or {}
    snapshot = _read_json(artifacts / "snapshot_risk.json", {}) or {}
    longitudinal = _read_json(artifacts / "longitudinal_risk.json", {}) or {}
    archive_proposal = _read_json(artifacts / "archive_update_proposal.json", {}) or {}
    health_summary = _read_json(artifacts / "health_summary_structured_summary.json", {}) or {}

    reports = (
        {"report.html": str(output_dir / "report.html")}
        if (output_dir / "report.html").is_file()
        else {}
    )

    failures: list[str] = []
    if not (output_dir / "report.html").is_file():
        failures.append("report.html missing")
    status = "success" if not failures else "partial"

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_path": input_path,
        "output_dir": str(output_dir),
        "status": status,
        "failures": failures,
        "person": {
            "person_id": person_id,
            "context": snapshot.get("person_context"),
        },
        "evidence_version": evidence_version.get("version"),
        "evidence_built_at": evidence_version.get("built_at"),
        "mineru": {
            "status": mineru_manifest.get("status"),
            "token_source": mineru_manifest.get("token_source"),
            "token_fingerprint": mineru_manifest.get("token_fingerprint"),
            "file_count": len(mineru_manifest.get("files", [])),
        },
        "task6_health_summary": {
            "status": health_summary.get("status"),
            "provider": health_summary.get("health_summary_provider"),
            "mode": health_summary.get("health_summary_mode"),
        },
        "task7_snapshot": {
            "cancers_total": len(snapshot.get("cancers", [])),
            "cancers_scored": sum(
                1 for c in snapshot.get("cancers", []) if c.get("posterior_probability") is not None
            ),
            "section4_count": len(snapshot.get("section4_screening", [])),
        },
        "task8_archive": {
            "person_id": person_id,
            "person_archive_dir": str(archives_root / person_id),
            "proposal_run_id": archive_proposal.get("run_id"),
            "factor_events_added": len(archive_proposal.get("factor_timeline_additions", [])),
            "screening_events_added": len(archive_proposal.get("screening_timeline_additions", [])),
        },
        "task8_longitudinal": longitudinal.get("summary", {}),
        "reports": reports,
        "audit_notes": _audit_notes(output_dir / "module_audits"),
        "safety_disclaimer_present": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--evidence-store", default=str(SKILL_ROOT / "references" / "database" / "cancerrisk" / "json"))
    parser.add_argument("--archives-root", required=True)
    parser.add_argument("--person-id", required=True)
    parser.add_argument("--run-id", default=os.environ.get("CANCERRISK_RUN_ID") or datetime.now().strftime("run-%Y%m%d-%H%M%S"))
    parser.add_argument("--input", default=None)
    parser.add_argument("--output-name", default="manifest.json")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    manifest = build_manifest(
        output_dir=output_dir,
        artifacts=output_dir / "artifacts",
        evidence_store=Path(args.evidence_store),
        archives_root=Path(args.archives_root),
        person_id=args.person_id,
        run_id=args.run_id,
        input_path=args.input,
    )
    target = output_dir / args.output_name
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[manifest] status={manifest['status']} -> {target}")


if __name__ == "__main__":
    main()

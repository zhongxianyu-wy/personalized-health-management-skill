#!/usr/bin/env python3
"""Task8 archive manager.

The archive layer is split into two steps:

1. ``build_archive_proposal`` reads ``merged_risk_factors.json`` and the
   existing archive (if any) and emits ``archive_update_proposal.json``
   listing per-event additions, screening-test additions, and unit /
   semantic conflicts. No archive file is touched.

2. ``apply_archive_updates`` consumes the proposal and merges new
   timepoints into ``factor_timeline.json``,
   ``screening_test_timeline.json``, and updates ``report_index.json``.

The orchestrator stops at the proposal (``--stop-after archive-proposal``)
unless the caller explicitly passes ``--auto-apply-archive``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

PROPOSAL_SCHEMA_VERSION = "archive-proposal-v1"


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _atomic_write(path: Path, data: Any) -> None:
    """Write JSON atomically via a temp file in the same directory."""
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    dir_ = path.parent
    dir_.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _factor_dedup_key(record: dict[str, Any]) -> str | None:
    """Either the cancer-expanded ``assertion_key`` or the slim Task4
    ``factor_key``; archived events use whichever the source carried."""
    return record.get("assertion_key") or record.get("factor_key")


def _factor_timeline_key(record: dict[str, Any]) -> tuple[str, str | None, str | None] | None:
    dk = _factor_dedup_key(record)
    if not dk:
        return None
    return (dk, record.get("exam_date"), record.get("source_data_id"))


def _screening_timeline_key(record: dict[str, Any]) -> tuple[str, str | None, str | None] | None:
    tid = record.get("test_id")
    if not tid:
        return None
    return (tid, record.get("exam_date"), record.get("source_data_id"))


def _existing_factor_keys(timeline: dict[str, Any]) -> set[tuple[str, str | None, str | None]]:
    """Return ``{(dedup_key, exam_date, source_data_id)}`` already on the timeline."""
    keys: set[tuple[str, str | None, str | None]] = set()
    for record in timeline.get("entries", []):
        key = _factor_timeline_key(record)
        if key:
            keys.add(key)
    return keys


def _existing_screening_keys(timeline: dict[str, Any]) -> set[tuple[str, str | None, str | None]]:
    keys: set[tuple[str, str | None, str | None]] = set()
    for record in timeline.get("entries", []):
        key = _screening_timeline_key(record)
        if key:
            keys.add(key)
    return keys


def build_archive_proposal(
    *,
    merged: dict[str, Any],
    factor_timeline: dict[str, Any],
    screening_timeline: dict[str, Any],
    report_index: dict[str, Any],
    run_id: str,
    snapshot_summary: dict[str, Any] | None = None,
    longitudinal_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing_factor = _existing_factor_keys(factor_timeline)
    existing_screening = _existing_screening_keys(screening_timeline)

    new_factor_entries: list[dict[str, Any]] = []
    for event in merged.get("factor_events", []):
        dk = _factor_dedup_key(event)
        if not dk:
            continue
        key = (dk, event.get("exam_date"), event.get("source_data_id"))
        if key in existing_factor:
            continue
        new_factor_entries.append({
            "assertion_key": event.get("assertion_key"),
            "factor_key": event.get("factor_key"),
            "factor_id": event.get("factor_id"),
            "factor_level": event.get("factor_level"),
            "factor_type": event.get("factor_type"),
            "exists": event.get("exists"),
            "exam_date": event.get("exam_date"),
            "evidence_text": event.get("evidence_text"),
            "source": event.get("source"),
            "source_data_id": event.get("source_data_id"),
            "source_path": event.get("source_path"),
            "status_reason": event.get("status_reason"),
            "confidence": event.get("confidence"),
            "measurement_value": event.get("measurement_value"),
            "measurement_unit": event.get("measurement_unit"),
            # imaging_finding fields (null for non-imaging entries)
            "cancer_id": event.get("cancer_id"),
            "ppv_point_used": event.get("ppv_point_used"),
            "malignancy_ppv_range": event.get("malignancy_ppv_range"),
            "finding_name_zh": event.get("finding_name_zh"),
            "ingested_at_run": run_id,
        })

    new_screening_entries: list[dict[str, Any]] = []
    for test in merged.get("screening_tests", []):
        tid = test.get("test_id")
        if not tid:
            continue
        key = (tid, test.get("exam_date"), test.get("source_data_id"))
        if key in existing_screening:
            continue
        new_screening_entries.append({
            "test_id": tid,
            "test_name": test.get("test_name"),
            "result": test.get("result"),
            "top_cancers": test.get("top_cancers", []),
            "exam_date": test.get("exam_date"),
            "source": test.get("source"),
            "source_data_id": test.get("source_data_id"),
            "evidence_text": test.get("evidence_text"),
            "ingested_at_run": run_id,
        })

    report_entry = {
        "run_id": run_id,
        "ingested_at": datetime.now().isoformat(timespec="seconds"),
        "factor_events_added": len(new_factor_entries),
        "screening_events_added": len(new_screening_entries),
        "snapshot_summary": snapshot_summary or {},
        "longitudinal_summary": longitudinal_summary or {},
    }

    return {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "run_id": run_id,
        "factor_timeline_additions": new_factor_entries,
        "screening_timeline_additions": new_screening_entries,
        "report_index_addition": report_entry,
        "conflicts": merged.get("conflicts", []),
        "uncertainties": merged.get("uncertainties", []),
    }


def apply_archive_updates(
    *,
    proposal: dict[str, Any],
    person_archive_root: Path,
) -> dict[str, Any]:
    person_archive_root.mkdir(parents=True, exist_ok=True)

    factor_path = person_archive_root / "factor_timeline.json"
    screening_path = person_archive_root / "screening_test_timeline.json"
    report_index_path = person_archive_root / "report_index.json"

    factor_timeline = _read_json(factor_path, {"entries": []})
    screening_timeline = _read_json(screening_path, {"entries": []})
    report_index = _read_json(report_index_path, {"entries": []})

    factor_entries = factor_timeline.setdefault("entries", [])
    factor_keys = {_factor_timeline_key(entry) for entry in factor_entries}
    for entry in proposal.get("factor_timeline_additions", []):
        key = _factor_timeline_key(entry)
        if key and key in factor_keys:
            continue
        factor_entries.append(entry)
        if key:
            factor_keys.add(key)

    screening_entries = screening_timeline.setdefault("entries", [])
    screening_keys = {_screening_timeline_key(entry) for entry in screening_entries}
    for entry in proposal.get("screening_timeline_additions", []):
        key = _screening_timeline_key(entry)
        if key and key in screening_keys:
            continue
        screening_entries.append(entry)
        if key:
            screening_keys.add(key)
    report_index.setdefault("entries", []).append(proposal.get("report_index_addition", {}))

    _atomic_write(factor_path, factor_timeline)
    _atomic_write(screening_path, screening_timeline)
    _atomic_write(report_index_path, report_index)

    return {
        "factor_timeline_path": str(factor_path),
        "screening_timeline_path": str(screening_path),
        "report_index_path": str(report_index_path),
        "factor_events_added": len(proposal.get("factor_timeline_additions", [])),
        "screening_events_added": len(proposal.get("screening_timeline_additions", [])),
    }


def write_baseline_snapshot(
    *,
    person_dir: Path,
    timeline_payload: dict[str, Any],
    snapshot_payload: dict[str, Any],
    longitudinal_payload: dict[str, Any] | None = None,
    run_id: str,
    run_date: str,
    snapshots_subdir: str = "snapshots",
) -> dict[str, str]:
    """Write the per-run baseline snapshot regardless of event count.

    Each run drops a single file at
    ``<person_dir>/<snapshots_subdir>/<exam_date>.json`` containing the
    un-deduped timeline plus a slim snapshot summary. This guarantees the
    archive always has an anchor point even when no historical data exists,
    so the longitudinal stage can render a meaningful "baseline_only" card.
    """
    snapshots_dir = person_dir / snapshots_subdir
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    exam_date = run_date
    snapshot_path = snapshots_dir / f"{exam_date}.json"
    payload = {
        "schema_version": "archive-baseline-snapshot-v1",
        "run_id": run_id,
        "exam_date": exam_date,
        "timeline_records_total": len(timeline_payload.get("records", [])),
        "timeline": timeline_payload,
        "snapshot_summary": {
            "cancers_total": len(snapshot_payload.get("cancers", [])),
            "cancers_scored": sum(
                1 for c in snapshot_payload.get("cancers", []) if c.get("posterior_probability") is not None
            ),
            "section4_count": len(snapshot_payload.get("section4_screening", [])),
        },
        "longitudinal_result": {
            "summary": (longitudinal_payload or {}).get("summary", {}),
            "cancers": [
                {
                    "cancer_id": c.get("cancer_id"),
                    "cancer_name_zh": c.get("cancer_name_zh"),
                    "current_posterior_probability": c.get("current_posterior_probability"),
                    "corrected_posterior_probability": c.get("corrected_posterior_probability"),
                    "trend": c.get("trend"),
                    "trend_correction": c.get("trend_correction"),
                }
                for c in (longitudinal_payload or {}).get("cancers", [])
            ],
        },
    }
    snapshot_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"baseline_snapshot_path": str(snapshot_path)}


def update_person_index(
    *,
    archives_root: Path,
    person_id: str,
    display_name: str | None,
    run_id: str,
    index_filename: str = "person_index.json",
) -> str:
    """Maintain archives_root/<index_filename> mapping display name → person_id."""
    archives_root.mkdir(parents=True, exist_ok=True)
    index_path = archives_root / index_filename
    index = _read_json(index_path, {"persons": []})
    persons = index.setdefault("persons", [])
    found = next((p for p in persons if p.get("person_id") == person_id), None)
    now_iso = datetime.now().isoformat(timespec="seconds")
    if found is None:
        persons.append({
            "person_id": person_id,
            "display_name": display_name or person_id,
            "created_at": now_iso,
            "last_run_id": run_id,
            "last_run_at": now_iso,
        })
    else:
        if display_name:
            found["display_name"] = display_name
        found["last_run_id"] = run_id
        found["last_run_at"] = now_iso
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(index_path)


def run_archive_stage(
    *,
    artifacts: Path,
    archives_root: Path,
    person_id: str,
    run_id: str | None = None,
    auto_apply: bool = False,
    display_name: str | None = None,
    run_date: str | None = None,
    snapshots_subdir: str = "snapshots",
    person_index_filename: str = "person_index.json",
) -> dict[str, Any]:
    merged = _read_json(artifacts / "merged_risk_factors.json", {"factor_events": [], "screening_tests": []})
    snapshot = _read_json(artifacts / "snapshot_risk.json", {})
    longitudinal = _read_json(artifacts / "longitudinal_risk.json", {})
    timeline_payload = _read_json(
        artifacts / "structured_risk_factors_timeline.json",
        {"records": [], "source_md_files": []},
    )

    # v6: route every read through resolve_person_archive so the
    # lookup is observable + path convention is enforced in one place.
    lookup = resolve_person_archive(archives_root, person_id, snapshots_subdir=snapshots_subdir)
    person_dir = Path(lookup["person_dir"])
    print(
        f"[task8] archive lookup: person_id={person_id} dir={person_dir} "
        f"status={lookup['status']} "
        f"factor_entries={lookup['files']['factor_timeline']['entries']} "
        f"screening_entries={lookup['files']['screening_test_timeline']['entries']} "
        f"snapshots={lookup['files']['snapshots_dir']['snapshot_count']}",
        file=sys.stderr,
    )
    factor_timeline = _read_json(person_dir / "factor_timeline.json", {"entries": []})
    screening_timeline = _read_json(person_dir / "screening_test_timeline.json", {"entries": []})
    report_index = _read_json(person_dir / "report_index.json", {"entries": []})

    snapshot_summary = {
        "scored_cancers": sum(1 for r in snapshot.get("cancers", []) if r.get("posterior_probability") is not None),
        "section4_count": len(snapshot.get("section4_screening", [])),
    }
    longitudinal_summary = longitudinal.get("summary", {}) if isinstance(longitudinal, dict) else {}

    rid = run_id or datetime.now().strftime("run-%Y%m%d-%H%M%S")
    rdate = run_date or datetime.now().strftime("%Y-%m-%d")

    proposal = build_archive_proposal(
        merged=merged,
        factor_timeline=factor_timeline,
        screening_timeline=screening_timeline,
        report_index=report_index,
        run_id=rid,
        snapshot_summary=snapshot_summary,
        longitudinal_summary=longitudinal_summary,
    )

    proposal_path = artifacts / "archive_update_proposal.json"
    proposal_path.write_text(json.dumps(proposal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result: dict[str, Any] = {
        "proposal_path": str(proposal_path),
        "factor_events_added": len(proposal["factor_timeline_additions"]),
        "screening_events_added": len(proposal["screening_timeline_additions"]),
        "applied": False,
    }

    if auto_apply:
        applied = apply_archive_updates(proposal=proposal, person_archive_root=person_dir)
        result.update(applied)
        baseline = write_baseline_snapshot(
            person_dir=person_dir,
            timeline_payload=timeline_payload,
            snapshot_payload=snapshot,
            longitudinal_payload=longitudinal,
            run_id=rid,
            run_date=rdate,
            snapshots_subdir=snapshots_subdir,
        )
        result.update(baseline)
        person_index_path = update_person_index(
            archives_root=archives_root,
            person_id=person_id,
            display_name=display_name,
            run_id=rid,
            index_filename=person_index_filename,
        )
        result["person_index_path"] = person_index_path
        result["applied"] = True
    return result


def resolve_person_archive(
    archives_root: Path,
    person_id: str,
    *,
    snapshots_subdir: str = "snapshots",
) -> dict[str, Any]:
    """Single source of truth for "where do this person's archives live".

    Every stage that needs to READ from or WRITE to a person archive
    (longitudinal_risk, archive_manager.run_archive_stage) MUST call
    this helper first so:

      1. The path convention is enforced in one place (no string
         concatenation drift across modules).
      2. The result is observable — callers can log
         "archive lookup: <dir> (status=found / fresh_person)" instead
         of silently treating "file missing" as "empty timeline".
      3. The longitudinal/archive outputs carry the lookup result for
         audit ("did this person have prior data, or is this their
         first run?").

    Return shape:
      {
        "person_id": "<id>",
        "person_dir": "<absolute path>",
        "status": "found" | "fresh_person" | "missing_root",
        "files": {
          "factor_timeline":         {"path": "...", "exists": bool, "entries": int},
          "screening_test_timeline": {"path": "...", "exists": bool, "entries": int},
          "report_index":            {"path": "...", "exists": bool, "entries": int},
          "snapshots_dir":           {"path": "...", "exists": bool, "snapshot_count": int},
        },
      }

    Status semantics:
      - "missing_root"  : archives_root itself doesn't exist (first run anywhere).
      - "fresh_person"  : root exists, but this person_id has no folder yet.
      - "found"         : root and per-person dir both exist (re-run or new exam).
    """
    person_dir = (archives_root / person_id).resolve()
    if not str(person_dir).startswith(str(archives_root.resolve())):
        raise ValueError(f"person_id '{person_id}' resolves outside archives_root")
    factor_path = person_dir / "factor_timeline.json"
    screen_path = person_dir / "screening_test_timeline.json"
    index_path = person_dir / "report_index.json"
    snapshots_dir = person_dir / snapshots_subdir

    def _count_entries(p: Path) -> int:
        if not p.is_file():
            return 0
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            return len(obj.get("entries", [])) if isinstance(obj, dict) else 0
        except Exception:
            return 0

    if not archives_root.is_dir():
        status = "missing_root"
    elif not person_dir.is_dir():
        status = "fresh_person"
    else:
        status = "found"

    return {
        "person_id": person_id,
        "person_dir": str(person_dir),
        "status": status,
        "files": {
            "factor_timeline": {
                "path": str(factor_path),
                "exists": factor_path.is_file(),
                "entries": _count_entries(factor_path),
            },
            "screening_test_timeline": {
                "path": str(screen_path),
                "exists": screen_path.is_file(),
                "entries": _count_entries(screen_path),
            },
            "report_index": {
                "path": str(index_path),
                "exists": index_path.is_file(),
                "entries": _count_entries(index_path),
            },
            "snapshots_dir": {
                "path": str(snapshots_dir),
                "exists": snapshots_dir.is_dir(),
                "snapshot_count": (
                    sum(1 for _ in snapshots_dir.iterdir()) if snapshots_dir.is_dir() else 0
                ),
            },
        },
    }


def has_existing_person_archives(
    archives_root: Path,
    *,
    person_id_default: str | None = None,
) -> bool:
    """Detect whether archives_root already contains non-default person dirs.

    Used by the orchestrator to decide whether to enforce a real --person-id.
    Empty dirs and the default sentinel are ignored.
    """
    if not archives_root.is_dir():
        return False
    for entry in archives_root.iterdir():
        if not entry.is_dir():
            continue
        if person_id_default and entry.name == person_id_default:
            continue
        if any(entry.iterdir()):
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True)
    parser.add_argument("--archives-root", required=True)
    parser.add_argument("--person-id", required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--apply", action="store_true",
                        help="apply the proposal to the archive immediately")
    args = parser.parse_args()

    result = run_archive_stage(
        artifacts=Path(args.artifacts),
        archives_root=Path(args.archives_root),
        person_id=args.person_id,
        run_id=args.run_id,
        auto_apply=args.apply,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

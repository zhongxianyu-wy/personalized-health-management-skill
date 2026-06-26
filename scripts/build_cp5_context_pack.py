#!/usr/bin/env python3
"""Build a compact CP5 context pack for LLM screening-gap analysis.

The pack is deterministic pre-processing only: age/sex schedule filtering,
coarse timeline matching, medium+ cancer extraction, and health abnormality
projection. It does not decide final recommendations.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "cp5-context-pack-v1"
MEDIUM_OR_ABOVE = {"medium", "high", "very_high", "moderate_workup", "high_workup", "urgent_workup"}


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return default


def _age_sex(snapshot: dict[str, Any]) -> dict[str, Any]:
    ctx = snapshot.get("person_context", {}) if isinstance(snapshot, dict) else {}
    return {"sex": ctx.get("sex"), "age": ctx.get("age")}


def _gender_matches(item_gender: Any, sex: Any) -> bool:
    gender = str(item_gender or "all").lower()
    sex = str(sex or "").lower()
    return gender in {"all", "both", "*"} or gender == sex


def _age_matches(item: dict[str, Any], age: Any) -> bool:
    if not isinstance(age, int):
        try:
            age = int(age)
        except (TypeError, ValueError):
            return False
    return int(item.get("start_age", 0)) <= age <= int(item.get("stop_age", 999))


def _norm(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def _candidate_terms(item: dict[str, Any]) -> list[str]:
    terms = [item.get("name"), item.get("method"), item.get("id")]
    aliases = item.get("aliases", [])
    if isinstance(aliases, list):
        terms.extend(aliases)
    return [str(t) for t in terms if t]


def _timeline_records(artifacts: Path) -> list[dict[str, Any]]:
    raw = _read_json(artifacts / "screening_test_timeline.json", [])
    if isinstance(raw, dict):
        for key in ("screening_tests", "tests", "records", "items"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict)]


def _record_text(record: dict[str, Any]) -> str:
    fields = [
        record.get("test_id"),
        record.get("test_name"),
        record.get("name"),
        record.get("item_name"),
        record.get("method"),
        record.get("result"),
        record.get("evidence_text"),
    ]
    return _norm(" ".join(str(f) for f in fields if f))


def _exam_date(record: dict[str, Any]) -> str | None:
    for key in ("exam_date", "date", "report_date", "collected_at"):
        value = record.get(key)
        if value:
            text = str(value)
            iso = re.search(r"\d{4}-\d{1,2}-\d{1,2}", text)
            if iso:
                return iso.group(0)
            cn = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日?", text)
            if cn:
                y, m, d = cn.groups()
                return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    return None


def _match_records(item: dict[str, Any], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    terms = [_norm(t) for t in _candidate_terms(item)]
    terms = [t for t in terms if t and len(t) >= 2]
    matched: list[dict[str, Any]] = []
    for record in records:
        text = _record_text(record)
        if any(term in text or text in term for term in terms):
            matched.append(
                {
                    "test_id": record.get("test_id"),
                    "test_name": record.get("test_name") or record.get("name") or record.get("item_name"),
                    "exam_date": _exam_date(record),
                    "result": record.get("result"),
                    "source": record.get("source") or record.get("source_data_id"),
                }
            )
    return matched


def _gap_prefill(matches: list[dict[str, Any]]) -> str:
    if not matches:
        return "never_seen"
    if any(m.get("exam_date") for m in matches):
        return "seen_with_date"
    return "seen_without_date"


def _periodic_candidates(
    *,
    schedule: dict[str, Any],
    person: dict[str, Any],
    records: list[dict[str, Any]],
    warnings: list[str],
) -> list[dict[str, Any]]:
    age = person.get("age")
    sex = person.get("sex")
    if age is None or sex is None:
        warnings.append("missing_demographics")
        return []
    items = schedule.get("items", []) if isinstance(schedule, dict) else []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if not _age_matches(item, age) or not _gender_matches(item.get("gender"), sex):
            continue
        matches = _match_records(item, records)
        out.append(
            {
                "id": item.get("id"),
                "dedup_key": item.get("id") or _norm(item.get("name")),
                "name": item.get("name"),
                "method": item.get("method"),
                "aliases": item.get("aliases", []),
                "category": item.get("category"),
                "interval_years": item.get("interval_years"),
                "high_risk_only": bool(item.get("high_risk_only")),
                "source": item.get("source"),
                "gap_prefill": _gap_prefill(matches),
                "matched_evidence": matches,
            }
        )
    return out


def _medium_plus_cancers(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    rows = snapshot.get("cancers", []) if isinstance(snapshot, dict) else []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("risk_tier") not in MEDIUM_OR_ABOVE:
            continue
        out.append(
            {
                "cancer_id": row.get("cancer_id"),
                "cancer_name": row.get("cancer_name_zh") or row.get("cancer_name") or row.get("cancer_id"),
                "risk_tier": row.get("risk_tier"),
                "posterior_probability": row.get("posterior_probability"),
            }
        )
    return out


def _health_abnormalities(health: dict[str, Any]) -> list[dict[str, Any]]:
    items = health.get("items", []) if isinstance(health, dict) else []
    out = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "name": item.get("name") or item.get("item_name"),
                "risk_level": item.get("risk_level") or item.get("severity"),
                "recommendation": item.get("recommendation") or item.get("advice"),
            }
        )
    return out


def _existing_evidence(records: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for record in records:
        matched_ids = []
        text = _record_text(record)
        for cand in candidates:
            terms = [_norm(t) for t in (cand.get("name"), cand.get("method"), cand.get("id")) if t]
            aliases = cand.get("aliases", [])
            if isinstance(aliases, list):
                terms.extend(_norm(t) for t in aliases if t)
            if any(term and (term in text or text in term) for term in terms):
                matched_ids.append(cand.get("id"))
        out.append(
            {
                "test_id": record.get("test_id"),
                "test_name": record.get("test_name") or record.get("name") or record.get("item_name"),
                "exam_date": _exam_date(record),
                "result": record.get("result"),
                "matched_candidate_ids": [m for m in matched_ids if m],
            }
        )
    return out


def build_context_pack(*, artifacts: Path, knowledge_root: Path) -> dict[str, Any]:
    artifacts = Path(artifacts)
    knowledge_root = Path(knowledge_root)
    warnings: list[str] = []

    snapshot = _read_json(artifacts / "snapshot_risk.json", {})
    health = _read_json(artifacts / "health_summary_structured_summary.json", {})
    schedule_path = knowledge_root / "screening_general" / "json" / "periodic_screening_schedule.json"
    schedule = _read_json(schedule_path, None)
    if schedule is None:
        warnings.append(f"missing_schedule:{schedule_path}")
        schedule = {"items": []}

    person = _age_sex(snapshot)
    records = _timeline_records(artifacts)
    candidates = _periodic_candidates(schedule=schedule, person=person, records=records, warnings=warnings)
    medium_plus = _medium_plus_cancers(snapshot)
    abnormalities = _health_abnormalities(health)

    return {
        "schema_version": SCHEMA_VERSION,
        "person_context": person,
        "cancer_risk_medium_plus": medium_plus,
        "health_abnormalities": abnormalities,
        "periodic_candidates_prefiltered": candidates,
        "existing_screening_evidence": _existing_evidence(records, candidates),
        "dedup_seed_keys": [row["dedup_key"] for row in candidates if row.get("dedup_key")],
        "warnings": warnings,
        "llm_instructions": [
            "优先读取本 context pack 生成 CP5 screening_recommendations.json。",
            "脚本只做确定性预筛和粗匹配；最终 A/B/C 推荐、医学去重和文案仍由 LLM 判断。",
            "gap_prefill=never_seen/seen_with_date/seen_without_date 仅为粗状态，证据不足时回查 refined.md、content.md 和知识库。",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True, help="analysis artifacts directory")
    parser.add_argument("--knowledge-root", required=True, help="references/database directory")
    args = parser.parse_args()

    artifacts = Path(args.artifacts)
    pack = build_context_pack(artifacts=artifacts, knowledge_root=Path(args.knowledge_root))
    out = artifacts / "cp5_context_pack.json"
    out.write_text(json.dumps(pack, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"OK: wrote {out}")


if __name__ == "__main__":
    main()

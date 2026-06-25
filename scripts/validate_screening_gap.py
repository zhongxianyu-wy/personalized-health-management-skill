#!/usr/bin/env python3
"""Validate the LLM-authored CP5 screening-gap artifacts.

This module is a PUA gate only. It never generates, deletes, deduplicates, or
rewrites medical recommendations.
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


DRAFT_SCHEMA = "screening-recommendations-draft-v1"
QUESTIONNAIRE_SCHEMA = "screening-gap-questionnaire-v1"
FINAL_SCHEMA = "screening-recommendations-final-v1"
SECTIONS = ("cancer_risk", "other_abnormalities", "periodic_candidates")
FINAL_SECTIONS = ("cancer_risk", "other_abnormalities", "periodic_management")
GAP_STATUSES = {"never_recorded", "overdue", "unverifiable_date"}
MEDIUM_OR_ABOVE = {
    "medium", "high", "high_workup", "moderate_workup", "pathology_confirmed"
}


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _answers_dict(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("answers")
    return inner if isinstance(inner, dict) else payload


def _rows(payload: dict[str, Any], names: tuple[str, ...]) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for name in names:
        value = payload.get(name, [])
        if isinstance(value, list):
            out.extend((name, row) for row in value if isinstance(row, dict))
    return out


def _duplicate_key_errors(payload: dict[str, Any], names: tuple[str, ...]) -> list[str]:
    locations: dict[str, list[str]] = {}
    errors: list[str] = []
    for section, row in _rows(payload, names):
        key = str(row.get("dedup_key") or "").strip()
        if not key:
            errors.append(f"{section}: missing dedup_key")
            continue
        locations.setdefault(key, []).append(section)
    for key, sections in locations.items():
        if len(sections) > 1:
            errors.append(f"duplicate dedup_key {key!r} across {sections}")
    return errors


def _find_knowledge_file(root: Path, source: str) -> Path | None:
    if not source:
        return None
    direct = root / source
    if direct.is_file():
        return direct
    matches = [p for p in root.rglob(Path(source).name) if p.is_file()]
    return matches[0] if len(matches) == 1 else None


def validate_draft(
    draft: Any,
    *,
    snapshot: Any,
    knowledge_root: Path,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(draft, dict):
        return ["draft must be a JSON object"]
    if draft.get("schema_version") != DRAFT_SCHEMA:
        errors.append(f"schema_version must be {DRAFT_SCHEMA}")
    for section in SECTIONS:
        if not isinstance(draft.get(section), list):
            errors.append(f"{section} must be a list")
    if not isinstance(draft.get("dedup_audit"), list):
        errors.append("dedup_audit must be a list")
    errors.extend(_duplicate_key_errors(draft, SECTIONS))
    section_keys = {
        section: {
            str(row.get("dedup_key"))
            for row in draft.get(section, [])
            if isinstance(row, dict) and row.get("dedup_key")
        }
        for section in SECTIONS
    }
    for audit in draft.get("dedup_audit", []):
        if not isinstance(audit, dict):
            errors.append("dedup_audit entries must be objects")
            continue
        key = str(audit.get("dedup_key") or "")
        removed_from = str(audit.get("removed_from") or "")
        kept_in = str(audit.get("kept_in") or "")
        if removed_from not in SECTIONS or kept_in not in SECTIONS:
            errors.append(f"dedup_audit/{key}: invalid removed_from or kept_in")
            continue
        if key not in section_keys[kept_in] or key in section_keys[removed_from]:
            errors.append(
                f"dedup_audit/{key}: key must exist only in kept_in={kept_in}"
            )
        if not str(audit.get("reason") or "").strip():
            errors.append(f"dedup_audit/{key}: missing reason")

    cancers = {}
    if isinstance(snapshot, dict):
        for row in snapshot.get("cancers", []):
            if isinstance(row, dict) and row.get("cancer_id"):
                cancers[str(row["cancer_id"])] = row

    for section, row in _rows(draft, SECTIONS):
        source = str(row.get("guideline_source") or "").strip()
        evidence = str(row.get("evidence_text") or "")
        if not source:
            errors.append(f"{section}/{row.get('dedup_key')}: missing guideline_source")
        if not evidence:
            errors.append(f"{section}/{row.get('dedup_key')}: missing evidence_text")
        source_path = _find_knowledge_file(Path(knowledge_root), source)
        if source and source_path is None:
            errors.append(
                f"{section}/{row.get('dedup_key')}: guideline_source {source!r} not uniquely found"
            )
        elif source_path is not None and evidence:
            text = source_path.read_text(encoding="utf-8")
            if evidence not in text:
                errors.append(
                    f"{section}/{row.get('dedup_key')}: evidence_text is not a literal substring "
                    f"of {source_path.name}"
                )

        if section == "cancer_risk":
            cancer_id = str(row.get("cancer_id") or "")
            cancer = cancers.get(cancer_id)
            if not cancer:
                errors.append(
                    f"cancer_risk/{row.get('dedup_key')}: cancer_id {cancer_id!r} "
                    "not found in snapshot"
                )
            elif cancer.get("risk_tier") not in MEDIUM_OR_ABOVE:
                errors.append(
                    f"cancer_risk/{row.get('dedup_key')}: cancer must be medium or above"
                )

        if section == "periodic_candidates":
            status = row.get("gap_status")
            if status not in GAP_STATUSES:
                errors.append(
                    f"periodic_candidates/{row.get('dedup_key')}: invalid gap_status {status!r}"
                )
            checked = row.get("timeline_checked_sources")
            if not isinstance(checked, list) or not checked:
                errors.append(
                    f"periodic_candidates/{row.get('dedup_key')}: "
                    "timeline_checked_sources must be a non-empty list"
                )
            timeline_evidence = str(row.get("timeline_evidence") or "")
            if status in {"overdue", "unverifiable_date"} and not timeline_evidence:
                errors.append(
                    f"periodic_candidates/{row.get('dedup_key')}: "
                    f"{status} requires timeline_evidence"
                )
    return errors


def _option_values(question: dict[str, Any]) -> set[str]:
    return {
        str(option.get("value"))
        for option in question.get("options", [])
        if isinstance(option, dict) and option.get("value") is not None
    }


def validate_questionnaire(draft: Any, questionnaire: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(draft, dict) or not isinstance(questionnaire, dict):
        return ["draft and questionnaire must be JSON objects"]
    if questionnaire.get("schema_version") != QUESTIONNAIRE_SCHEMA:
        errors.append(f"schema_version must be {QUESTIONNAIRE_SCHEMA}")
    questions = questionnaire.get("questions")
    if not isinstance(questions, list):
        return errors + ["questions must be a list"]

    candidate_keys = {
        str(row.get("dedup_key"))
        for row in draft.get("periodic_candidates", [])
        if isinstance(row, dict) and row.get("dedup_key")
    }
    by_key: dict[str, list[dict[str, Any]]] = {}
    for question in questions:
        if not isinstance(question, dict):
            errors.append("question entries must be objects")
            continue
        key = str(question.get("dedup_key") or "")
        if key not in candidate_keys:
            errors.append(f"question dedup_key {key!r} is not a periodic candidate")
        by_key.setdefault(key, []).append(question)

    for key in candidate_keys:
        group = by_key.get(key, [])
        done_questions = [
            q for q in group
            if {"done", "not_done", "unknown"}.issubset(_option_values(q))
            and not q.get("conditional_on")
        ]
        if len(done_questions) != 1:
            errors.append(f"{key}: expected exactly one done question")
            continue
        done_qid = done_questions[0].get("question_id")
        result_questions = [
            q for q in group
            if {"normal", "abnormal", "unknown"}.issubset(_option_values(q))
            and (q.get("conditional_on") or {}).get("question_id") == done_qid
            and (q.get("conditional_on") or {}).get("value") == "done"
        ]
        if len(result_questions) != 1:
            errors.append(f"{key}: expected exactly one conditional result question")
    return errors


def validate_answers(questionnaire: Any, answers_payload: Any) -> list[str]:
    if not isinstance(questionnaire, dict):
        return ["questionnaire must be a JSON object"]
    questions = questionnaire.get("questions", [])
    if not isinstance(questions, list):
        return ["questions must be a list"]
    answers = _answers_dict(answers_payload)
    if not isinstance(answers, dict):
        return ["answers must be a JSON object"]

    errors: list[str] = []
    expected_ids = {
        str(q.get("question_id"))
        for q in questions
        if isinstance(q, dict) and q.get("question_id")
    }
    extras = set(answers) - expected_ids
    if extras:
        errors.append(f"unexpected answer keys: {sorted(extras)}")
    for question in questions:
        if not isinstance(question, dict) or not question.get("question_id"):
            continue
        qid = str(question["question_id"])
        cond = question.get("conditional_on")
        if isinstance(cond, dict):
            if answers.get(str(cond.get("question_id"))) != cond.get("value"):
                continue
        if qid not in answers:
            errors.append(f"missing answer for {qid}")
            continue
        if answers[qid] not in _option_values(question):
            errors.append(f"{qid}: invalid option value {answers[qid]!r}")
    return errors


def _question_ids_by_key(questionnaire: dict[str, Any]) -> dict[str, tuple[str | None, str | None]]:
    result: dict[str, tuple[str | None, str | None]] = {}
    for question in questionnaire.get("questions", []):
        if not isinstance(question, dict):
            continue
        key = str(question.get("dedup_key") or "")
        done_qid, result_qid = result.get(key, (None, None))
        values = _option_values(question)
        if {"done", "not_done", "unknown"}.issubset(values) and not question.get("conditional_on"):
            done_qid = str(question.get("question_id"))
        elif {"normal", "abnormal", "unknown"}.issubset(values):
            result_qid = str(question.get("question_id"))
        result[key] = (done_qid, result_qid)
    return result


def validate_final(
    draft: Any,
    questionnaire: Any,
    answers_payload: Any,
    final: Any,
) -> list[str]:
    errors = validate_answers(questionnaire, answers_payload)
    if not isinstance(draft, dict) or not isinstance(questionnaire, dict) or not isinstance(final, dict):
        return errors + ["draft, questionnaire and final must be JSON objects"]
    if final.get("schema_version") != FINAL_SCHEMA:
        errors.append(f"schema_version must be {FINAL_SCHEMA}")
    for section in (*FINAL_SECTIONS, "excluded_done_normal", "dedup_audit"):
        if not isinstance(final.get(section), list):
            errors.append(f"{section} must be a list")
    errors.extend(_duplicate_key_errors(final, FINAL_SECTIONS))
    for section in ("cancer_risk", "other_abnormalities"):
        draft_keys = {
            str(row.get("dedup_key"))
            for row in draft.get(section, [])
            if isinstance(row, dict) and row.get("dedup_key")
        }
        final_keys = {
            str(row.get("dedup_key"))
            for row in final.get(section, [])
            if isinstance(row, dict) and row.get("dedup_key")
        }
        if draft_keys != final_keys:
            errors.append(
                f"{section} dedup_key set {sorted(final_keys)} does not match "
                f"draft {sorted(draft_keys)}"
            )

    periodic = {
        str(row.get("dedup_key")): row
        for row in final.get("periodic_management", [])
        if isinstance(row, dict) and row.get("dedup_key")
    }
    excluded = {
        str(row.get("dedup_key")): row
        for row in final.get("excluded_done_normal", [])
        if isinstance(row, dict) and row.get("dedup_key")
    }
    answers = _answers_dict(answers_payload)
    qids = _question_ids_by_key(questionnaire)
    for candidate in draft.get("periodic_candidates", []):
        if not isinstance(candidate, dict) or not candidate.get("dedup_key"):
            continue
        key = str(candidate["dedup_key"])
        done_qid, result_qid = qids.get(key, (None, None))
        done = answers.get(done_qid) if done_qid else None
        result = answers.get(result_qid) if result_qid else None
        if done == "done" and result == "normal":
            if key in periodic:
                errors.append(f"{key}: done + normal must not enter periodic_management")
            if key not in excluded:
                errors.append(f"{key}: done + normal missing excluded_done_normal record")
            elif excluded[key].get("disposition") != "done_normal":
                errors.append(f"{key}: excluded disposition must be 'done_normal'")
        elif done in {"not_done", "unknown"} or (
            done == "done" and result in {"abnormal", "unknown"}
        ):
            if key not in periodic:
                errors.append(f"{key}: missing final disposition in periodic_management")
            else:
                expected_disposition = (
                    done
                    if done in {"not_done", "unknown"}
                    else f"done_{result}"
                )
                if periodic[key].get("disposition") != expected_disposition:
                    errors.append(
                        f"{key}: disposition must be {expected_disposition!r}"
                    )
            if key in excluded:
                errors.append(f"{key}: actionable answer must not be excluded_done_normal")
        elif done is not None:
            errors.append(f"{key}: unsupported answer combination done={done!r} result={result!r}")
    return errors


def validate_report_artifacts(
    final: Any,
    timeline_tiers: Any,
    package_tiers: Any,
) -> list[str]:
    del package_tiers  # packages are LLM-authored; only explicit future keys are gateable.
    if not isinstance(final, dict) or not isinstance(timeline_tiers, dict):
        return ["final and timeline_tiers must be JSON objects"]
    expected = {
        str(row.get("dedup_key"))
        for row in final.get("periodic_management", [])
        if isinstance(row, dict) and row.get("dedup_key")
    }
    actual = {
        str(row.get("dedup_key"))
        for row in timeline_tiers.get("maintain", [])
        if isinstance(row, dict) and row.get("dedup_key")
    }
    if expected != actual:
        return [
            f"maintain dedup_key set {sorted(actual)} does not match "
            f"final periodic_management {sorted(expected)}"
        ]
    return []


def _print_errors(errors: list[str]) -> int:
    if not errors:
        print("[validate_screening_gap] OK")
        return 0
    print(f"[validate_screening_gap] {len(errors)} error(s):", file=sys.stderr)
    for error in errors:
        print(f"  ✗ {error}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    draft_parser = sub.add_parser("draft")
    draft_parser.add_argument("--draft", required=True)
    draft_parser.add_argument("--snapshot", required=True)
    draft_parser.add_argument("--knowledge-root", required=True)

    q_parser = sub.add_parser("questionnaire")
    q_parser.add_argument("--draft", required=True)
    q_parser.add_argument("--questionnaire", required=True)

    a_parser = sub.add_parser("answers")
    a_parser.add_argument("--questionnaire", required=True)
    a_parser.add_argument("--answers", required=True)

    f_parser = sub.add_parser("final")
    f_parser.add_argument("--draft", required=True)
    f_parser.add_argument("--questionnaire", required=True)
    f_parser.add_argument("--answers", required=True)
    f_parser.add_argument("--final", required=True)

    args = parser.parse_args(argv)
    try:
        if args.mode == "draft":
            errors = validate_draft(
                _load_json(Path(args.draft)),
                snapshot=_load_json(Path(args.snapshot)),
                knowledge_root=Path(args.knowledge_root),
            )
        elif args.mode == "questionnaire":
            errors = validate_questionnaire(
                _load_json(Path(args.draft)),
                _load_json(Path(args.questionnaire)),
            )
        elif args.mode == "answers":
            errors = validate_answers(
                _load_json(Path(args.questionnaire)),
                _load_json(Path(args.answers)),
            )
        else:
            errors = validate_final(
                _load_json(Path(args.draft)),
                _load_json(Path(args.questionnaire)),
                _load_json(Path(args.answers)),
                _load_json(Path(args.final)),
            )
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[validate_screening_gap] invalid input: {exc}", file=sys.stderr)
        return 2
    return _print_errors(errors)


if __name__ == "__main__":
    raise SystemExit(main())

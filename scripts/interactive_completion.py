#!/usr/bin/env python3
"""Task 5 — fixed yaml-driven interactive completion (runs BEFORE Task4).

The questionnaire is built from ``config/formal.yaml::interactive
.required_questions``. Two branches (common + sex-specific) are merged
and capped at ``max_total_questions``. Answers produce:

* ``artifacts/interactive_answers.md`` — markdown summary, used as the
  7th source md for the master-fill scan and concatenated into the
  Task6 refined_content_bundle.
* ``artifacts/structured_risk_factors_timeline.user_reported.json`` —
  evidence records with ``source="user_reported"``, ``confidence=1.0``,
  ``exam_date="now"`` (rewritten to run_date by the gate).
* ``artifacts/interactive_questionnaire.json`` — the active questionnaire
  for audit.

The legacy ``build_questionnaire`` / ``apply_answers`` helpers are kept
for backwards compatibility with existing tests that exercise the old
question-templates flow and Jizaoan logic.
"""

from __future__ import annotations

import sys as _sys
_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import _env_bootstrap  # noqa: F401 — 跨runtime环境自检(PYTHONHOME/UTF-8)

import argparse
import json
import re as _re
from datetime import date
from pathlib import Path
from typing import Any

import yaml

SKILL_ROOT = Path(__file__).resolve().parent.parent

INTERACTABLE_TYPES = {"lifestyle", "medical_history", "family_history", "screening_test"}
JIZAOAN_ID = "jizaoan_multi_cancer_screening"


# ---------------------------------------------------------------------------
# Questionnaire → master factor_key fan-out (Bug B fix).
#
# config/formal.yaml::interactive.required_questions declares answers using
# umbrella factor_ids ("family_history_cancer", "smoking_current",
# "alcohol_heavy"). These are intentionally coarser than the master factor
# vocabulary (which has many per-cancer flavours like
# family_history_crc_first, smoking_squamous, alcohol_30g_per_day). Without
# this table, every user_reported record is rejected by the gate as
# "unknown_factor_key" because the umbrella key does not exist in the
# master. The mapping is `(questionnaire_factor_id, questionnaire_level)`
# → list of `(master_factor_id, master_factor_level)` targets to emit.
#
# Screening-test questions (`*_screening_recent|done`) are intentionally
# absent: master has no risk-factor key for them, so we drop those rows
# from the user_reported timeline and only keep them in the markdown
# summary. Screening behaviours feed a separate archive
# (screening_test_timeline.json) at the task8 layer.
# ---------------------------------------------------------------------------

FACTOR_FANOUT: dict[tuple[str, str], list[tuple[str, str]]] = {
    # family_history_cancer "yes" only sets the generic first-degree factor.
    # Specific per-cancer factors (family_history_lung_first, etc.) are derived
    # by parsing the q_family_history_detail free-text answer.
    ("family_history_cancer", "present"): [
        ("family_history_first_degree", "present"),
    ],
    # smoking_adenocarcinoma removed: the ontology has only esophageal_cancer (not separate
    # squamous/adenocarcinoma cancer_ids), so keeping both would double-count the smoking
    # contribution to esophageal_cancer. smoking_squamous (OR=2.9) is retained as the
    # representative assertion because squamous cell carcinoma is >90% of Chinese esophageal cancer.
    ("smoking_current", "current"): [
        ("smoking_current", "present"),
        ("smoking_general", "present"),
        ("smoking_male", "present"),
        ("smoking_continuous", "present"),
        ("smoking_squamous", "present"),
    ],
    ("alcohol_heavy", "heavy"): [
        ("alcohol_30g_per_day", "present"),
        ("alcohol_3plus_drinks", "present"),
        ("alcohol_non_cardia", "present"),
        ("alcohol_squamous_3plus", "present"),
        ("alcohol_daily_heavy", "present"),  # → liver_cancer OR 3.5
    ],
}

# Screening-question factor ids — kept in markdown summary, dropped from
# the user_reported risk-factor timeline. They feed the screening archive
# through a different code path (see archive_manager.run_archive_stage).
SCREENING_FACTOR_IDS: set[str] = {
    "psa_screening_recent",
    "colorectal_screening_recent",
    "cervical_screening_recent",
    "breast_screening_recent",
}

# ---------------------------------------------------------------------------
# Family history free-text parser (for q_family_history_detail text_fill)
# ---------------------------------------------------------------------------

# Maps Chinese cancer keyword → (single-relative factor_id, multiple-relative factor_id | None)
# Longer strings must come before shorter ones sharing the same characters.
_CANCER_FH_FACTORS: dict[str, tuple[str, str | None]] = {
    "结直肠": ("family_history_crc_first", "family_history_crc_multiple"),
    "大肠":   ("family_history_crc_first", "family_history_crc_multiple"),
    "食管":   ("family_history_esophageal", "family_history_esophageal_multiple"),
    "甲状腺": ("family_history_thyroid", "family_history_thyroid_multiple"),
    "前列腺": ("family_history_prostate", None),
    "胰腺":   ("family_history_pancreatic", None),
    "胆道":   ("family_history_biliary", None),
    "胆管":   ("family_history_biliary", None),
    "胆囊":   ("family_history_biliary", None),
    "头颈":   ("family_history_head_neck", None),
    "宫颈":   ("family_history_cervical", None),
    "卵巢":   ("family_history_ovarian_first", "family_history_ovarian_multiple"),
    "乳腺":   ("family_history_breast_first", None),
    "乳房":   ("family_history_breast_first", None),
    "膀胱":   ("family_history_bladder", "family_history_bladder_multiple"),
    "胃":     ("family_history_gastric_first", "family_history_gastric_multiple"),
    "肝":     ("family_history_liver", "family_history_liver_multiple"),
    "肺":     ("family_history_first_degree", None),  # no lung-specific factor
    "肾":     ("family_history_kidney", "family_history_kidney_multiple"),
    "肠":     ("family_history_crc_first", "family_history_crc_multiple"),
}

# Multiple-relatives markers in Chinese text
_MULTIPLE_RE = _re.compile(r"多[人名位个]|≥\s*2|>=\s*2|两[人名位个]|三[人名位个]|四[人名位个]|多位|多名|以上")


def _parse_family_history_text(text: str) -> list[dict[str, Any]]:
    """Parse free-text family history answer into timeline-ready factor records.

    Input: "父亲肺癌（1人）、母亲乳腺癌（多人）"
    Output: [{factor_id: family_history_first_degree, ...}, {factor_id: family_history_breast_first, ...}]
    """
    # Split into per-person segments to prevent count markers from one entry
    # bleeding into the keyword context of an adjacent entry.
    segments = [s.strip() for s in _re.split(r"[，,、\n；;]+", text) if s.strip()]
    if not segments:
        segments = [text]

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    cancer_count: int = 0
    sorted_factors = sorted(_CANCER_FH_FACTORS.items(), key=lambda x: -len(x[0]))

    for seg in segments:
        is_multiple = bool(_MULTIPLE_RE.search(seg))
        for keyword, (single_fid, multi_fid) in sorted_factors:
            if keyword not in seg:
                continue
            factor_id = (multi_fid if (is_multiple and multi_fid) else single_fid)
            if factor_id and factor_id not in seen:
                seen.add(factor_id)
                cancer_count += 1
                records.append({
                    "factor_key": f"{factor_id}|present",
                    "factor_id": factor_id,
                    "factor_level": "present",
                    "factor_type": "family_history",
                    "exists": True,
                    "evidence_text": text,
                })
            break  # only first matched keyword per segment

    # ≥2 distinct cancer types → generic multiple-type marker
    if cancer_count >= 2 and "family_history_multiple" not in seen:
        records.append({
            "factor_key": "family_history_multiple|present",
            "factor_id": "family_history_multiple",
            "factor_level": "present",
            "factor_type": "family_history",
            "exists": True,
            "evidence_text": text,
        })

    return records


# ---------------------------------------------------------------------------
# v4 fixed questionnaire
# ---------------------------------------------------------------------------


def _resolve_sex_for_branch(answers: dict[str, Any], demographics: dict[str, Any] | None) -> str | None:
    sex = answers.get("q_demographics_sex") if isinstance(answers, dict) else None
    if sex in {"male", "female"}:
        return sex
    if demographics and isinstance(demographics, dict):
        sex = demographics.get("sex")
        if sex in {"male", "female"}:
            return sex
    return None


def build_fixed_questionnaire(
    *,
    config: dict[str, Any],
    demographics: dict[str, Any] | None = None,
    answers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the questionnaire from yaml required_questions, branched by sex."""
    interactive_cfg = (config.get("interactive") or {}) if isinstance(config, dict) else {}
    required = (interactive_cfg.get("required_questions") or {}) if isinstance(interactive_cfg, dict) else {}
    common = list(required.get("common") or [])
    answers = answers or {}
    sex = _resolve_sex_for_branch(answers, demographics)
    sex_specific: list[dict[str, Any]] = []
    if sex == "male":
        sex_specific = list(required.get("male") or [])
    elif sex == "female":
        sex_specific = list(required.get("female") or [])

    yaml_questions = [dict(q, group="common") for q in common] + [dict(q, group=sex or "unknown") for q in sex_specific]

    # v7: screening-behaviour questions (PSA, colonoscopy, mammography, cervical)
    # are NOT asked interactively — they belong to the report-extracted evidence
    # path (imaging_finding / timeline) or are omitted when the report already
    # contains the result.  Only jizaoan remains as the explicitly-required
    # screening question.
    yaml_questions = [
        q for q in yaml_questions
        if q.get("factor_id") not in SCREENING_FACTOR_IDS
    ]

    # placement=last questions are separated out so they always appear after
    # all other questions (outside the cap), regardless of yaml order.
    last_questions = [q for q in yaml_questions if q.get("placement") == "last"]
    main_questions = [q for q in yaml_questions if q.get("placement") != "last"]

    max_total = int(interactive_cfg.get("max_total_questions") or interactive_cfg.get("max_questions") or 10)
    if len(main_questions) > max_total:
        main_questions = main_questions[:max_total]

    # Jizaoan prepended outside cap; placement=last questions appended outside cap.
    questions = _jizaoan_questions() + main_questions + last_questions

    return {
        "schema_version": "interactive-questionnaire-v2",
        "agent_directive": (
            "🛑 STOP. The fields below are questions FOR THE END USER "
            "(the patient or their proxy). You MUST elicit answers by "
            "asking the user directly via AskUserQuestion / IM bot / "
            "web form / whatever channel your wrapper owns. DO NOT "
            "infer answers from the medical report ('no smoking "
            "mentioned' is NOT evidence the patient never smoked; "
            "'T-PSA 0.25 from 2020' is NOT evidence a recent PSA "
            "screening was done). Inferred answers produce a misleading "
            "report and trip the validator (scripts/validate_answers.py). "
            "See SKILL.md 'Interactive recipe (Agent Checkpoint 2)'."
        ),
        "demographics_branch_sex": sex,
        "max_total_questions": max_total,
        "questions": questions,
        "question_count": len(questions),
        "question_scope": f"最多{max_total}题，部分问题视答案条件进行补充",
    }


def _normalize_answer_value(value: Any) -> str:
    """Normalize common answer-value mismatches to canonical option values.

    Handles: Chinese yes/no/unknown variants, boolean strings, numeric
    strings, and label-to-value drift from agent hand-off.
    """
    if value is None:
        return "unknown"
    s = str(value).strip().lower()
    if s in {"yes", "是", "有", "true", "1", "y", "done", "做过", "阳性"}:
        return "yes"
    if s in {"no", "否", "无", "false", "0", "n", "none", "没做过", "阴性", "从未"}:
        return "no"
    if s in {"unknown", "不清楚", "未知", "不知道", "skip", "不愿透露", "未提供", "nan", ""}:
        return "unknown"
    # Demographic sex normalisation
    if s in {"male", "男", "m", "男士", "男性"}:
        return "male"
    if s in {"female", "女", "f", "女士", "女性"}:
        return "female"
    # Smoking / alcohol normalisation
    if s in {"never", "从不", "从未", "没吸过", "不喝酒"}:
        return "never"
    if s in {"former", "已戒烟", "以前吸", "已戒"}:
        return "former"
    if s in {"current", "目前吸烟", "正在吸", "吸烟", "经常大量饮酒", "经常", "大量"}:
        return "current"
    if s in {"occasional", "偶尔饮酒", "偶尔"}:
        return "occasional"
    if s in {"heavy", "经常大量饮酒", "大量饮酒", "酗酒"}:
        return "heavy"
    return str(value).strip()


def _resolve_option(question: dict[str, Any], answer_value: Any) -> dict[str, Any] | None:
    options = question.get("options") or []
    normalized = _normalize_answer_value(answer_value)
    # 1st pass — exact match on raw value
    for opt in options:
        if opt.get("value") == answer_value:
            return opt
    # 2nd pass — match on normalized value
    for opt in options:
        if _normalize_answer_value(opt.get("value")) == normalized:
            return opt
    # 3rd pass — fuzzy match on label text
    for opt in options:
        label = str(opt.get("label", "")).strip()
        if label and _normalize_answer_value(label) == normalized:
            return opt
    return None


def apply_fixed_answers(
    questionnaire: dict[str, Any],
    answers_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Turn the yaml-driven answers into demographics + user_reported timeline + md text."""
    answers = (answers_payload or {}).get("answers") if isinstance(answers_payload, dict) else None
    answers = answers if isinstance(answers, dict) else (answers_payload or {})

    demographics_updates: dict[str, Any] = {}
    timeline_records: list[dict[str, Any]] = []
    md_lines: list[str] = ["# 用户补充信息（Task5 交互获取）", ""]
    skipped: list[dict[str, Any]] = []
    screening_tests: list[dict[str, Any]] = []

    # Jizaoan processing (options use updates:[] not exists, so handled here explicitly)
    jizaoan_result = answers.get("q_jizaoan_result", "unknown")
    if jizaoan_result in {"negative", "positive"}:
        top_cancers: list[str] = []
        if jizaoan_result == "positive":
            for qid in ("q_jizaoan_top1", "q_jizaoan_top2"):
                val = answers.get(qid)
                if val and val != "unknown":
                    top_cancers.append(str(val))
        screening_tests.append({
            "test_id": JIZAOAN_ID,
            "factor_id": JIZAOAN_ID,
            "test_name": "吉早安多癌早筛",
            "result": jizaoan_result,
            "top_cancers": top_cancers[:2],
            "exam_date": "now",
            "source": "interactive_completion",
        })
        md_lines.append(f"- 吉早安检测结果：{'阳性' if jizaoan_result == 'positive' else '阴性'}")
    else:
        skipped.append({"question_id": "q_jizaoan_result", "value": jizaoan_result, "reason": "jizaoan_unknown_or_skipped"})

    for question in questionnaire.get("questions", []):
        qid = question.get("question_id")
        if not qid:
            continue
        # Jizaoan questions already processed above
        if str(qid).startswith("q_jizaoan"):
            continue
        target = question.get("target") or "timeline"
        value = answers.get(qid)
        if target == "demographics":
            if qid == "q_demographics_sex" and value in {"male", "female"}:
                demographics_updates["sex"] = value
                md_lines.append(f"- 性别：{'男' if value == 'male' else '女'}")
            elif qid == "q_demographics_age":
                try:
                    age = int(value)
                except (TypeError, ValueError):
                    age = None
                if age is not None:
                    demographics_updates["age"] = age
                    md_lines.append(f"- 年龄：{age} 岁")
            continue

        q_type = question.get("type") or "single_choice"

        if target == "gate":
            # Gate questions control routing only — no timeline records
            md_lines.append(f"- {question.get('prompt', qid).splitlines()[0]}：{value or '（未答）'}")
            continue

        # ---- text_fill (free-text, conditional) ----
        if q_type == "text_fill":
            cond = question.get("conditional_on")
            if cond:
                cond_qid = str(cond.get("question_id") or "")
                cond_val = str(cond.get("value") or "")
                if _normalize_answer_value(answers.get(cond_qid)) != _normalize_answer_value(cond_val):
                    continue  # trigger condition not met — skip
            raw_text = (answers.get(qid) or "").strip()
            if not raw_text or _normalize_answer_value(raw_text) in {"unknown"}:
                skipped.append({"question_id": qid, "reason": "no_text_provided"})
                md_lines.append(f"- {question.get('prompt', qid).splitlines()[0]}：（未提供）")
                continue
            md_lines.append(f"- {question.get('prompt', qid).splitlines()[0]}")
            md_lines.append(f"  答：{raw_text}")
            parsed = _parse_family_history_text(raw_text)
            for rec in parsed:
                timeline_records.append({
                    **rec,
                    "exam_date": "now",
                    "source_md": None,
                    "source_data_id": "interactive_answers",
                    "source": "user_reported",
                    "confidence": 0.9,
                })
            if not parsed:
                md_lines.append("  （未能解析出已知癌种，仅作文字记录）")
            continue

        # ---- multi_select ----
        if q_type == "multi_select":
            cond = question.get("conditional_on")
            if cond:
                cond_qid = str(cond.get("question_id") or "")
                cond_val = str(cond.get("value") or "")
                if _normalize_answer_value(answers.get(cond_qid)) != _normalize_answer_value(cond_val):
                    continue  # trigger condition not met — skip
            raw = answers.get(qid)
            if isinstance(raw, list):
                selected = [str(v).strip() for v in raw if str(v).strip()]
            elif isinstance(raw, str):
                selected = [v.strip() for v in raw.split(",") if v.strip()]
            else:
                selected = []
            selected_set = set(selected)
            selected_labels: list[str] = []
            for opt in question.get("options", []):
                opt_value = str(opt.get("value") or "")
                if opt_value not in selected_set:
                    continue
                opt_label = opt.get("label") or opt_value
                opt_factor_id = opt.get("factor_id")
                opt_factor_level = opt.get("factor_level", "present")
                opt_exists = opt.get("exists")
                if opt_factor_id and opt_exists is True:
                    selected_labels.append(opt_label)
                    factor_key = f"{opt_factor_id}|{opt_factor_level}"
                    timeline_records.append({
                        "factor_key": factor_key,
                        "factor_id": opt_factor_id,
                        "factor_level": opt_factor_level,
                        "factor_type": "genetic_predisposition",
                        "exists": True,
                        "exam_date": "now",
                        "source_md": None,
                        "source_data_id": "interactive_answers",
                        "source": "user_reported",
                        "evidence_text": opt_label,
                        "confidence": 1.0,
                    })
            if not selected or selected_set == {"none"}:
                md_lines.append(f"- {question.get('prompt', qid).splitlines()[0]}：均无/未检测")
            elif selected_labels:
                md_lines.append(f"- {question.get('prompt', qid).splitlines()[0]}：{', '.join(selected_labels)}")
            else:
                md_lines.append(f"- {question.get('prompt', qid).splitlines()[0]}：（未提供）")
            continue

        # ---- single_choice / boolean (default) ----
        option = _resolve_option(question, value)
        if option is None or "exists" not in option:
            skipped.append({"question_id": qid, "value": value, "reason": "no_exists_mapping"})
            md_lines.append(f"- {question.get('prompt', qid)}：（未提供 / 不清楚）")
            continue
        factor_id = question.get("factor_id")
        factor_level = question.get("factor_level")
        if not factor_id:
            skipped.append({"question_id": qid, "value": value, "reason": "no_factor_id"})
            continue
        evidence_text = option.get("label") or str(value)
        exists_bool = bool(option["exists"])

        # Screening behaviours don't map to a master risk-factor key — keep
        # them in the markdown summary but skip the timeline emit so the
        # gate doesn't reject them as unknown.
        if factor_id in SCREENING_FACTOR_IDS:
            md_lines.append(
                f"- {question.get('prompt', qid)} → {option.get('label', value)} "
                f"_(筛查行为，不进入风险因子时间线)_"
            )
            continue

        targets = FACTOR_FANOUT.get((factor_id, factor_level or "")) or [
            (factor_id, factor_level)
        ]
        for tgt_factor_id, tgt_factor_level in targets:
            factor_key = f"{tgt_factor_id}|{tgt_factor_level or 'unknown'}"
            timeline_records.append({
                "factor_key": factor_key,
                "factor_id": tgt_factor_id,
                "factor_level": tgt_factor_level,
                "exists": exists_bool,
                "exam_date": "now",
                "source_md": None,
                "source_data_id": "interactive_answers",
                "source": "user_reported",
                "evidence_text": evidence_text,
                "confidence": 1.0,
                "fanout_from": f"{factor_id}|{factor_level}",
            })
        md_lines.append(f"- {question.get('prompt', qid)} → {option.get('label', value)}")

    if len(md_lines) <= 2:
        md_lines.append("- 用户未提供补充信息。")

    return {
        "demographics_updates": demographics_updates,
        "user_reported_timeline": {
            "schema_version": "risk-factors-timeline-v1",
            "records": timeline_records,
        },
        "interactive_answers_md": "\n".join(md_lines) + "\n",
        "screening_tests": screening_tests,
        "skipped": skipped,
    }


def write_fixed_outputs(
    *,
    output_dir: Path,
    questionnaire: dict[str, Any],
    result: dict[str, Any],
    interactive_cfg: dict[str, Any] | None = None,
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    interactive_cfg = interactive_cfg or {}
    answers_md_name = interactive_cfg.get("interactive_answers_md", "interactive_answers.md")
    timeline_name = interactive_cfg.get("user_reported_timeline", "structured_risk_factors_timeline.user_reported.json")
    questionnaire_name = interactive_cfg.get("questionnaire_output", "interactive_questionnaire.json")

    paths = {
        "questionnaire": str(output_dir / questionnaire_name),
        "interactive_answers_md": str(output_dir / answers_md_name),
        "user_reported_timeline": str(output_dir / timeline_name),
    }
    (output_dir / questionnaire_name).write_text(
        json.dumps(questionnaire, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / answers_md_name).write_text(result["interactive_answers_md"], encoding="utf-8")
    (output_dir / timeline_name).write_text(
        json.dumps(result["user_reported_timeline"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return paths


# ---------------------------------------------------------------------------
# Task 8 — person_id confirmation
# ---------------------------------------------------------------------------


def extract_candidate_names(refined_md_paths: list[Path]) -> list[str]:
    """Pick out Chinese person names from refined.md headers.

    The refine recipe keeps the demographics line(s) verbatim; person names
    appear as `姓名：XXX` or `姓名: XXX` (full-width / half-width colon).
    We aggregate unique non-empty 2-4 character names.
    """
    import re

    names: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(r"姓名[\s]*[:：]\s*([一-鿿]{2,6})")
    for path in refined_md_paths:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            name = match.group(1).strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def normalize_person_id(name_or_id: str) -> str:
    """Slugify a Chinese name or arbitrary string into a filesystem-safe id."""
    import re

    cleaned = re.sub(r"\s+", "-", name_or_id.strip())
    cleaned = re.sub(r"[^\w一-鿿-]", "", cleaned)
    return cleaned or "anonymous"


def resolve_person_id_interactively(
    *,
    refined_md_paths: list[Path],
    cli_person_id: str | None,
    person_id_default: str,
    archives_root: Path,
    person_index_path: Path | None,
    operator_choice: str | None = None,
) -> dict[str, Any]:
    """Determine the person_id to use for archive writes.

    Resolution order:
      1. ``--person-id`` is non-default → trust the operator.
      2. Extract candidate names from refined.md; if ``operator_choice`` is
         passed (e.g. an answer payload), match it against the candidates.
      3. Otherwise return ``needs_confirmation`` with the candidate list
         and let the caller halt for agent prompting.
    """
    if cli_person_id and cli_person_id != person_id_default:
        return {
            "status": "resolved",
            "person_id": cli_person_id,
            "display_name": cli_person_id,
            "source": "cli",
        }

    candidates = extract_candidate_names(refined_md_paths)

    if operator_choice == "__skip__":
        return {"status": "skipped", "person_id": None, "display_name": None, "candidates": candidates}

    if operator_choice:
        normalized = normalize_person_id(operator_choice)
        return {
            "status": "resolved",
            "person_id": normalized,
            "display_name": operator_choice,
            "source": "operator_confirmed",
            "candidates": candidates,
        }

    if len(candidates) == 1:
        return {
            "status": "needs_confirmation",
            "person_id": None,
            "candidates": candidates,
            "prompt": f"本次结果是否记入「{candidates[0]}」的健康档案？（yes / 输入自定义 ID / __skip__）",
        }

    return {
        "status": "needs_confirmation",
        "person_id": None,
        "candidates": candidates,
        "prompt": (
            "本次结果记入哪个人的健康档案？候选："
            + (", ".join(candidates) if candidates else "（未在体检报告中检测到姓名）")
            + " 。请回复候选之一 / 输入自定义 ID / __skip__"
        ),
    }


# ---------------------------------------------------------------------------
# Legacy v3 helpers — kept so existing tests stay green during the migration
# ---------------------------------------------------------------------------


def _answer_exam_date(answers: dict[str, Any]) -> str:
    raw = answers.get("answer_date") if isinstance(answers, dict) else None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return date.today().isoformat()


def _records(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key, [])
    return value if isinstance(value, list) else []


def _covered_factor_ids(assertion_status: dict[str, Any]) -> set[str]:
    covered = set()
    for event in _records(assertion_status, "assertion_events"):
        if event.get("exists") in {True, False} and event.get("factor_id"):
            covered.add(str(event["factor_id"]))
    return covered


def _question_by_factor(question_templates: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(q["factor_id"]): q
        for q in _records(question_templates, "questions")
        if q.get("factor_id")
    }


def _jizaoan_questions() -> list[dict[str, Any]]:
    cancers = [
        "lung_cancer",
        "liver_cancer",
        "gastric_cancer",
        "esophageal_cancer",
        "colorectal_cancer",
        "breast_cancer",
        "ovarian_cancer",
    ]
    cancer_options = [{"value": cid, "label": cid, "updates": []} for cid in cancers]
    cancer_options.append({"value": "unknown", "label": "不清楚/暂不提供", "updates": []})
    return [
        {
            "question_id": "q_jizaoan_result",
            "question_group": "jizaoan_result",
            "factor_id": JIZAOAN_ID,
            "factor_type": "screening_test",
            "prompt": "请补充：吉早安多癌早筛检测结果？",
            "type": "single_choice",
            "required": True,
            "options": [
                {"value": "negative", "label": "阴性", "updates": []},
                {"value": "positive", "label": "阳性", "updates": []},
                {"value": "unknown", "label": "不清楚/暂不提供", "updates": []},
            ],
        },
        {
            "question_id": "q_jizaoan_top1",
            "question_group": "jizaoan_top1",
            "factor_id": JIZAOAN_ID,
            "factor_type": "screening_test",
            "prompt": "若吉早安为阳性，请补充溯源 top1 癌种。",
            "type": "single_choice",
            "required": True,
            "conditional_on": {"question_id": "q_jizaoan_result", "value": "positive"},
            "options": cancer_options,
        },
        {
            "question_id": "q_jizaoan_top2",
            "question_group": "jizaoan_top2",
            "factor_id": JIZAOAN_ID,
            "factor_type": "screening_test",
            "prompt": "若吉早安为阳性，请补充溯源 top2 癌种。",
            "type": "single_choice",
            "required": False,
            "conditional_on": {"question_id": "q_jizaoan_result", "value": "positive"},
            "options": cancer_options,
        },
    ]


def _clone_question(question: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(question, ensure_ascii=False))
    out.setdefault("question_id", out.get("question_group") or out.get("factor_id"))
    out.setdefault("type", "single_choice")
    out.setdefault("required", False)
    return out


def build_questionnaire(
    *,
    assertion_template: dict[str, Any],
    assertion_status: dict[str, Any],
    needs_confirmation: dict[str, Any],
    question_templates: dict[str, Any],
    max_questions: int = 10,
) -> dict[str, Any]:
    covered = _covered_factor_ids(assertion_status)
    by_factor = _question_by_factor(question_templates)
    questions: list[dict[str, Any]] = []
    seen: set[str] = set()

    questions.extend(_jizaoan_questions())
    seen.add(JIZAOAN_ID)

    def add_factor(factor_id: str) -> None:
        if factor_id in covered or factor_id in seen:
            return
        q = by_factor.get(factor_id)
        if not q:
            return
        if q.get("factor_type") not in INTERACTABLE_TYPES:
            return
        questions.append(_clone_question(q))
        seen.add(factor_id)

    for item in _records(needs_confirmation, "needs_confirmation_factors"):
        if item.get("factor_type") in INTERACTABLE_TYPES:
            add_factor(str(item.get("factor_id")))

    for record in assertion_template.get("assertion_templates", []):
        if not record.get("interaction_needed_if_missing"):
            continue
        if record.get("factor_type") not in INTERACTABLE_TYPES:
            continue
        add_factor(str(record.get("factor_id")))

    questions = questions[:max_questions]
    return {"questions": questions, "question_count": len(questions), "max_questions": max_questions}


def _answer_lookup(answers_payload: dict[str, Any]) -> dict[str, Any]:
    answers = answers_payload.get("answers", answers_payload)
    return answers if isinstance(answers, dict) else {}


def apply_answers(
    questionnaire: dict[str, Any],
    answers_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    answers = _answer_lookup(answers_payload or {})
    updates: list[dict[str, Any]] = []
    supplemental_items: list[dict[str, Any]] = []
    uncertainties: list[dict[str, Any]] = []
    screening_tests: list[dict[str, Any]] = []

    jizaoan_result = answers.get("q_jizaoan_result", answers.get("jizaoan_result", "unknown"))
    answer_date = _answer_exam_date(answers)
    if jizaoan_result in {"negative", "positive"}:
        top_cancers: list[str] = []
        if jizaoan_result == "positive":
            for qid in ("q_jizaoan_top1", "q_jizaoan_top2"):
                value = answers.get(qid)
                if value and value != "unknown":
                    top_cancers.append(str(value))
        screening_tests.append(
            {
                "test_id": JIZAOAN_ID,
                "factor_id": JIZAOAN_ID,
                "test_name": "吉早安多癌早筛",
                "result": jizaoan_result,
                "top_cancers": top_cancers[:2],
                "exam_date": answer_date,
                "source": "interactive_completion",
            }
        )
    else:
        uncertainties.append({"factor_id": JIZAOAN_ID, "reason": "unknown_or_skipped"})

    for question in questionnaire.get("questions", []):
        qid = question.get("question_id") or question.get("question_group") or question.get("factor_id")
        if str(qid).startswith("q_jizaoan"):
            continue
        value = answers.get(qid, answers.get(question.get("factor_id"), "unknown"))
        option = next((o for o in question.get("options", []) if o.get("value") == value), None)
        if option is None:
            option = next((o for o in question.get("options", []) if o.get("value") == "unknown"), None)
        if option is None:
            uncertainties.append({"factor_id": question.get("factor_id"), "reason": "no_valid_option"})
            continue
        for patch in option.get("updates", []):
            if patch.get("exists") == "unknown":
                continue
            item = {
                **patch,
                "factor_id": question.get("factor_id"),
                "factor_type": question.get("factor_type"),
                "question_id": qid,
                "question_group": question.get("question_group"),
                "source": "interactive_completion",
                "confidence": 1.0,
                "exam_date": answer_date,
            }
            updates.append(item)
            supplemental_items.append(item)

    return {
        "updates": updates,
        "supplemental_risk_factors": supplemental_items,
        "screening_tests": screening_tests,
        "uncertainties": uncertainties,
    }


def write_interactive_outputs(
    *,
    output_dir: Path,
    questionnaire: dict[str, Any],
    answer_result: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "interactive_questionnaire.json").write_text(
        json.dumps(questionnaire, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "supplemental_risk_factor_updates.json").write_text(
        json.dumps({"updates": answer_result["updates"]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "supplemental_risk_factors.json").write_text(
        json.dumps(
            {
                "supplemental_risk_factors": answer_result["supplemental_risk_factors"],
                "screening_tests": answer_result["screening_tests"],
                "uncertainties": answer_result["uncertainties"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    lines = ["# 交互补充风险因子", ""]
    for item in answer_result["supplemental_risk_factors"]:
        lines.append(f"- {item.get('factor_id')}: {item.get('exists')}")
    for test in answer_result["screening_tests"]:
        lines.append(f"- {test.get('test_name')}: {test.get('result')}")
    if len(lines) == 2:
        lines.append("- 无明确补充。")
    (output_dir / "supplemental_risk_factors.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True)
    parser.add_argument("--config", default=str(SKILL_ROOT / "config" / "formal.yaml"))
    parser.add_argument("--question-templates")
    parser.add_argument("--answers")
    parser.add_argument("--mode", choices=("fixed", "legacy"), default="fixed",
                        help="fixed: yaml required_questions; legacy: v3 question_templates")
    parser.add_argument("--max-questions", type=int, default=10)
    args = parser.parse_args()

    artifacts = Path(args.artifacts)
    answers = json.loads(Path(args.answers).read_text(encoding="utf-8")) if args.answers else {}
    if args.mode == "fixed":
        config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
        demographics_path = artifacts / "demographics.json"
        demographics = json.loads(demographics_path.read_text(encoding="utf-8")) if demographics_path.is_file() else None
        questionnaire = build_fixed_questionnaire(config=config, demographics=demographics, answers=answers.get("answers") if isinstance(answers, dict) else None)
        result = apply_fixed_answers(questionnaire, answers)
        write_fixed_outputs(
            output_dir=artifacts,
            questionnaire=questionnaire,
            result=result,
            interactive_cfg=config.get("interactive", {}),
        )
        print(
            f"[interactive_completion] mode=fixed questions={questionnaire['question_count']} "
            f"user_reported_records={len(result['user_reported_timeline']['records'])}"
        )
        return

    questionnaire = build_questionnaire(
        assertion_template=json.loads((artifacts / "risk_factor_assertion_template.json").read_text(encoding="utf-8")),
        assertion_status=json.loads((artifacts / "risk_factor_assertion_status.json").read_text(encoding="utf-8")),
        needs_confirmation=json.loads((artifacts / "needs_confirmation_factors.json").read_text(encoding="utf-8")),
        question_templates=json.loads(Path(args.question_templates).read_text(encoding="utf-8")),
        max_questions=args.max_questions,
    )
    answer_result = apply_answers(questionnaire, answers)
    write_interactive_outputs(output_dir=artifacts, questionnaire=questionnaire, answer_result=answer_result)
    print(
        f"[interactive_completion] mode=legacy questions={questionnaire['question_count']} "
        f"updates={len(answer_result['updates'])} screening_tests={len(answer_result['screening_tests'])}"
    )


if __name__ == "__main__":
    main()

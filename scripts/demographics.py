#!/usr/bin/env python3
"""Demographics resolver — runs between MinerU and Task4.

Age + sex are the foundation of every downstream probability computation:

* ``build_assertion_fill_template`` filters out cancers / factors that do
  not apply to the person.
* ``snapshot_risk`` queries ``cancer_age_sex_priors.json`` keyed on
  ``(cancer_id, sex, age)``. The lookup rule clamps any query age above
  the database max to the highest available anchor.

Resolution order (mandatory):

1. ``--person-sex`` + ``--person-age`` CLI flags (operator override; used
   by CI and ad-hoc smoke runs).
2. Extract from the MinerU per-file ``content.md`` outputs.
3. Interactive Q&A: a fixed 2-question form (sex = single-choice, age =
   numeric fill-in) the agent must answer via ``--answers`` or by editing
   the produced ``demographics_questionnaire.json`` and re-running.
4. Otherwise the orchestrator halts with a non-zero exit and writes
   ``demographics_questionnaire.json`` for the agent to complete. No
   silent fallback to "male/68" default sneaks past this stage.

The resolved record (``sex``, ``age``, ``source``) is written to
``artifacts/demographics.json`` so every downstream stage can read the
same authoritative value.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "demographics-v1"


SEX_NORMALIZE = {
    "男": "male",
    "M": "male",
    "male": "male",
    "Male": "male",
    "MALE": "male",
    "女": "female",
    "F": "female",
    "female": "female",
    "Female": "female",
    "FEMALE": "female",
}

# Compact "钟贤宇(男)" / "张三 男" / table-cell "生别:男" / "性别:男" forms
SEX_PATTERNS = (
    re.compile(r"(?:性别|生理性别|生别)\s*[:：]?\s*(男|女)"),
    re.compile(r"[（(]\s*(男|女)\s*[）)]"),
    re.compile(r">\s*(男|女)\s*<"),
)

# Age must be label-anchored or appear in an explicit demographic-paren
# form. A bare "<num>岁" match is too dangerous — medical prose contains
# phrases like "40~60岁高发", "建议 50 岁以上人群" that would otherwise
# leak into the extraction. If neither anchored form is found, fall
# through to interactive Q&A (safer than guessing).
AGE_PATTERNS = (
    re.compile(r"(?:年龄|age)\s*[:：]\s*(\d{1,3})\s*岁?", re.IGNORECASE),
    re.compile(r"[（(]\s*(\d{1,3})\s*岁\s*[）)]"),
)


@dataclass(frozen=True)
class DemographicsQuestion:
    question_id: str
    factor_id: str
    prompt: str
    type: str
    required: bool
    options: tuple[dict[str, str], ...] | None = None
    min: int | None = None
    max: int | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "question_id": self.question_id,
            "factor_id": self.factor_id,
            "prompt": self.prompt,
            "type": self.type,
            "required": self.required,
            "answer_key": self.question_id,  # what to look up in answers payload
        }
        if self.options is not None:
            out["options"] = [dict(o) for o in self.options]
        if self.min is not None:
            out["min"] = self.min
        if self.max is not None:
            out["max"] = self.max
        return out


DEMOGRAPHICS_QUESTIONS: tuple[DemographicsQuestion, ...] = (
    DemographicsQuestion(
        question_id="q_demographics_sex",
        factor_id="demographics_sex",
        prompt="请补充：您的生理性别？",
        type="single_choice",
        required=True,
        options=(
            {"value": "male", "label": "男"},
            {"value": "female", "label": "女"},
        ),
    ),
    DemographicsQuestion(
        question_id="q_demographics_age",
        factor_id="demographics_age",
        prompt="请补充：您的年龄（岁）？",
        type="numeric",
        required=True,
        min=0,
        max=120,
    ),
)


def normalize_sex(raw: str | None) -> str | None:
    if not raw:
        return None
    return SEX_NORMALIZE.get(raw.strip())


def parse_age(raw: str | int | None) -> int | None:
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if 0 <= value <= 120:
        return value
    return None


def extract_from_text(text: str) -> dict[str, Any]:
    """Run sex + age regexes over a text blob.

    Returns ``{"sex": "male"/"female"/None, "age": int/None,
    "sex_evidence": str/None, "age_evidence": str/None}``.
    """
    sex: str | None = None
    sex_evidence: str | None = None
    for pat in SEX_PATTERNS:
        m = pat.search(text)
        if m:
            sex = normalize_sex(m.group(1))
            if sex:
                sex_evidence = m.group(0).strip()
                break

    age: int | None = None
    age_evidence: str | None = None
    for pat in AGE_PATTERNS:
        m = pat.search(text)
        if m:
            candidate = parse_age(m.group(1))
            if candidate is not None and candidate > 0:
                age = candidate
                age_evidence = m.group(0).strip()
                break

    return {
        "sex": sex,
        "age": age,
        "sex_evidence": sex_evidence,
        "age_evidence": age_evidence,
    }


def _extract_exam_date(text: str) -> str | None:
    patterns = (
        r"(?:检查日期|体检日期|报告日期|送检日期|检测日期|采样日期)[:：\s]*"
        r"([0-9]{4})[.\-/年]\s*([0-9]{1,2})[.\-/月]\s*([0-9]{1,2})",
        r"(19\d{2}|20\d{2})[.\-/年]\s*(1[0-2]|0[1-9]|[1-9])[.\-/月]\s*"
        r"(3[01]|[12][0-9]|0[1-9]|[1-9])",
    )
    for pattern in patterns:
        m = re.search(pattern, text)
        if not m:
            continue
        year, month, day = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if 1990 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def extract_from_mineru(mineru_root: Path, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Aggregate per-file content.md extraction.

    Sex uses the first reliable hit. Age uses the latest dated report; if no
    report date can be extracted, it falls back to the largest observed age.
    Returns the same shape as :func:`extract_from_text` plus a ``per_file``
    audit list.
    """
    per_file: list[dict[str, Any]] = []
    resolved_sex: str | None = None
    resolved_sex_evidence: str | None = None
    resolved_sex_data_id: str | None = None
    age_candidates: list[dict[str, Any]] = []

    iter_ids: Iterable[str]
    if manifest and isinstance(manifest.get("files"), list):
        iter_ids = [f["data_id"] for f in manifest["files"] if f.get("data_id")]
    else:
        iter_ids = sorted(d.name for d in mineru_root.iterdir() if d.is_dir())

    for data_id in iter_ids:
        content_md = mineru_root / data_id / "content.md"
        if not content_md.is_file():
            continue
        text = content_md.read_text("utf-8")
        result = extract_from_text(text)
        exam_date = _extract_exam_date(text)
        per_file.append({"data_id": data_id, "exam_date": exam_date, **result})
        if resolved_sex is None and result["sex"]:
            resolved_sex = result["sex"]
            resolved_sex_evidence = result["sex_evidence"]
            resolved_sex_data_id = data_id
        if result["age"] is not None:
            age_candidates.append({
                "age": result["age"],
                "age_evidence": result["age_evidence"],
                "age_source_data_id": data_id,
                "exam_date": exam_date,
            })

    dated_age_candidates = [c for c in age_candidates if c.get("exam_date")]
    if dated_age_candidates:
        selected_age = max(dated_age_candidates, key=lambda c: str(c["exam_date"]))
    elif age_candidates:
        selected_age = max(age_candidates, key=lambda c: int(c["age"]))
    else:
        selected_age = {}

    return {
        "sex": resolved_sex,
        "age": selected_age.get("age"),
        "sex_evidence": resolved_sex_evidence,
        "age_evidence": selected_age.get("age_evidence"),
        "sex_source_data_id": resolved_sex_data_id,
        "age_source_data_id": selected_age.get("age_source_data_id"),
        "per_file": per_file,
    }


def _missing_questions(sex: str | None, age: int | None) -> list[dict[str, Any]]:
    """Return only the questions still needed to complete demographics."""
    out: list[dict[str, Any]] = []
    if sex not in {"male", "female"}:
        out.append(DEMOGRAPHICS_QUESTIONS[0].to_dict())
    if age is None:
        out.append(DEMOGRAPHICS_QUESTIONS[1].to_dict())
    return out


def build_questionnaire(sex: str | None, age: int | None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "demographics",
        "required": True,
        "questions": _missing_questions(sex, age),
        "current_partial": {"sex": sex, "age": age},
    }


def resolve(
    *,
    cli_sex: str | None,
    cli_age: int | None,
    mineru_root: Path,
    manifest: dict[str, Any] | None,
    answers: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve sex/age using the documented priority chain.

    Returns ``{"status": "resolved", "sex", "age", "source", "trace"}`` on
    success, or ``{"status": "needs_interactive", "questionnaire", "trace"}``
    when interactive input is required. The orchestrator halts in the
    latter case and surfaces the questionnaire to the agent.
    """
    answers = answers or {}
    trace: dict[str, Any] = {"cli": None, "extraction": None, "interactive": None}

    # 1. CLI override (operator knows best; used by tests + automation)
    if cli_sex in {"male", "female"} and cli_age is not None:
        trace["cli"] = {"sex": cli_sex, "age": cli_age}
        return {
            "status": "resolved",
            "schema_version": SCHEMA_VERSION,
            "sex": cli_sex,
            "age": int(cli_age),
            "source": "cli",
            "trace": trace,
        }

    # Partial CLI is still considered — start with whatever CLI gave us.
    sex = cli_sex if cli_sex in {"male", "female"} else None
    age = int(cli_age) if cli_age is not None else None

    # 2. Extraction from MinerU content.md
    extracted = extract_from_mineru(mineru_root, manifest=manifest)
    trace["extraction"] = {k: v for k, v in extracted.items() if k != "per_file"}
    if sex is None and extracted.get("sex"):
        sex = extracted["sex"]
    if age is None and extracted.get("age") is not None:
        age = int(extracted["age"])

    # 3. Interactive answers — applied AFTER extraction so user-confirmed values
    #    override OCR heuristics (优化点2.6: interactive age has priority over OCR age).
    interactive_sex_raw = answers.get("q_demographics_sex") or answers.get("person_sex")
    interactive_sex = normalize_sex(interactive_sex_raw) if isinstance(interactive_sex_raw, str) else None
    interactive_age = parse_age(answers.get("q_demographics_age", answers.get("person_age")))
    trace["interactive"] = {"sex": interactive_sex, "age": interactive_age}
    if interactive_sex:
        sex = interactive_sex          # override extraction
    if interactive_age is not None:
        age = interactive_age          # override extraction

    if sex in {"male", "female"} and age is not None:
        if interactive_sex or interactive_age is not None:
            source = "interactive"
        elif not cli_sex and not cli_age:
            source = "report_extraction"
        else:
            source = "cli_plus_extraction"
        return {
            "status": "resolved",
            "schema_version": SCHEMA_VERSION,
            "sex": sex,
            "age": age,
            "source": source,
            "trace": trace,
            "evidence": {
                "sex_evidence": extracted.get("sex_evidence"),
                "age_evidence": extracted.get("age_evidence"),
                "sex_source_data_id": extracted.get("sex_source_data_id"),
                "age_source_data_id": extracted.get("age_source_data_id"),
            },
        }

    # 4. Need interactive input — return the questionnaire for the agent.
    return {
        "status": "needs_interactive",
        "schema_version": SCHEMA_VERSION,
        "questionnaire": build_questionnaire(sex, age),
        "trace": trace,
    }


def write_artifacts(
    artifacts_dir: Path,
    resolution: dict[str, Any],
) -> dict[str, str]:
    """Persist resolved demographics + (optional) pending questionnaire."""
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    if resolution["status"] == "resolved":
        payload = {
            "schema_version": resolution["schema_version"],
            "sex": resolution["sex"],
            "age": resolution["age"],
            "source": resolution["source"],
            "trace": resolution["trace"],
            "evidence": resolution.get("evidence", {}),
        }
        out = artifacts_dir / "demographics.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        paths["demographics_json"] = str(out)
    else:
        out = artifacts_dir / "demographics_questionnaire.json"
        out.write_text(json.dumps(resolution["questionnaire"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        paths["demographics_questionnaire_json"] = str(out)
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True, help="path to analysis_output/artifacts")
    parser.add_argument("--person-sex", choices=("male", "female"), default=None)
    parser.add_argument("--person-age", type=int, default=None)
    parser.add_argument("--answers", default=None, help="JSON file with interactive answers")
    args = parser.parse_args()

    artifacts = Path(args.artifacts)
    mineru_root = artifacts / "mineru"
    if not mineru_root.is_dir():
        parser.error(f"mineru directory not found: {mineru_root}")
    manifest_path = artifacts / "conversion_manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8")) if manifest_path.is_file() else None
    answers_payload: dict[str, Any] = {}
    if args.answers:
        answers_payload = json.loads(Path(args.answers).read_text("utf-8")).get("answers", {})

    resolution = resolve(
        cli_sex=args.person_sex,
        cli_age=args.person_age,
        mineru_root=mineru_root,
        manifest=manifest,
        answers=answers_payload,
    )
    paths = write_artifacts(artifacts, resolution)

    summary = {
        "status": resolution["status"],
        "sex": resolution.get("sex"),
        "age": resolution.get("age"),
        "source": resolution.get("source"),
        "paths": paths,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

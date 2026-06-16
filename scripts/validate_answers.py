#!/usr/bin/env python3
"""Pre-flight check for the agent-authored answers.json (CP2).

Catches the two failure modes observed in the v6 audit:

  1. **Missing or mis-typed answers** — answer is None / unrecognised
     value / wrong type for the question.

  2. **Agent self-inference smell** — the agent skipped asking the
     user and instead "推断" the answer from the medical report.
     Common tells: meta fields like ``_inferred`` / ``_source`` /
     ``_note`` carrying words such as 推断 / 可能 / 未提及 /
     "from report" / "inferred". A user dialogue doesn't produce
     such commentary; an agent fabrication often does.

Exits with code 1 when at least one hard problem is found
(unrecognised answer / missing required question). Inference smells
are emitted as warnings (exit code 0 unless `--strict-no-inference`
is set, in which case they also fail).

Usage:

    python cancerrisk-skill/scripts/validate_answers.py \\
      --questionnaire <out>/artifacts/interactive_questionnaire.json \\
      --answers <path>/answers.json [--strict-no-inference]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


SMELL_TOKENS = (
    "推断", "推测", "未提及", "可能是", "应该是", "估计",
    "inferred", "guessed", "assumed", "from report", "based on report",
    "report did not mention", "no mention",
    "由于这是测试", "fixture",
    # Copied SKILL.md format-marker tokens (placeholders, not real values):
    "from user", "← from user", "<male | female>", "<integer>",
    "<yes | no | unknown>", "<never | former | current", "<free-text>",
    "<positive | negative",
)


def _load(path: Path):
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
    parser.add_argument("--questionnaire", required=True)
    parser.add_argument("--answers", required=True)
    parser.add_argument("--strict-no-inference", action="store_true",
                        help="also fail when smell tokens are detected")
    args = parser.parse_args()

    q = _load(Path(args.questionnaire))
    a = _load(Path(args.answers))
    answers = a.get("answers", a) if isinstance(a, dict) else {}
    if not isinstance(answers, dict):
        print("FAIL: answers payload must be a JSON object (with optional top-level 'answers' wrapper)", file=sys.stderr)
        return 2

    questions = q.get("questions", []) if isinstance(q, dict) else []
    hard_errors: list[str] = []
    smells: list[str] = []

    expected_qids = set()
    for question in questions:
        qid = question.get("question_id")
        if not qid:
            continue
        expected_qids.add(qid)

        # conditional_on: skip validation when the trigger condition is not met.
        # e.g. q_family_history_detail is only required when q_family_history_cancer="yes";
        # q_jizaoan_top1/top2 are only required when q_jizaoan_result="positive".
        cond = question.get("conditional_on")
        if cond:
            cond_qid = str(cond.get("question_id") or "")
            cond_val = str(cond.get("value") or "").lower()
            actual_val = str(answers.get(cond_qid) or "").lower()
            if actual_val != cond_val:
                continue  # condition not met — question is legitimately absent

        # required=False questions are optional even without a condition.
        if question.get("required") is False and qid not in answers:
            continue

        if qid not in answers:
            hard_errors.append(f"missing answer for {qid!r} (prompt: {question.get('prompt', '')!r})")
            continue
        value = answers[qid]
        qtype = question.get("type")
        opts = question.get("options") or []
        if qtype == "single_choice":
            valid_values = [o.get("value") for o in opts]
            if value not in valid_values:
                hard_errors.append(
                    f"{qid}: value {value!r} not in options {valid_values}"
                )
        elif qtype == "integer":
            try:
                int(value)
            except (TypeError, ValueError):
                hard_errors.append(f"{qid}: expected an integer, got {value!r}")
        elif qtype == "multi_select":
            if not isinstance(value, list):
                hard_errors.append(
                    f"{qid}: expected a JSON array for multi_select, got {type(value).__name__}: {value!r}"
                )

    extras = [k for k in answers.keys() if k not in expected_qids]
    if extras:
        smells.append(f"unexpected answer key(s): {extras} — questionnaire did not ask these")

    # Jizaoan-specific: positive result with unknown top1 is a soft warning.
    if answers.get("q_jizaoan_result") == "positive" and answers.get("q_jizaoan_top1") == "unknown":
        smells.append(
            "q_jizaoan_top1 is 'unknown' despite q_jizaoan_result='positive' — "
            "confirm user was asked to locate the jizaoan report before accepting 'unknown'"
        )

    # Scan ALL string values in the answers payload for inference smells.
    def _walk(node, path="$"):
        if isinstance(node, str):
            low = node.lower()
            for tok in SMELL_TOKENS:
                if tok.lower() in low:
                    smells.append(f"inference smell at {path}: {tok!r} in {node!r}")
        elif isinstance(node, dict):
            for k, v in node.items():
                _walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                _walk(v, f"{path}[{i}]")
    _walk(a)

    if hard_errors:
        print(f"[validate_answers] {len(hard_errors)} hard error(s):", file=sys.stderr)
        for e in hard_errors:
            print(f"  ✗ {e}", file=sys.stderr)
    if smells:
        print(f"[validate_answers] {len(smells)} inference smell(s):", file=sys.stderr)
        for s in smells:
            print(f"  ⚠ {s}", file=sys.stderr)
        print(
            "\nAgent-inferred answers (vs. answers the actual user gave) "
            "produce a misleading report. If you are an agent who answered "
            "by reading the medical report, STOP — go ask the actual user "
            "(AskUserQuestion / IM bot / form) and re-run. fixture "
            "is NOT a license to fabricate values.",
            file=sys.stderr,
        )

    if hard_errors:
        return 1
    if smells and args.strict_no_inference:
        return 1
    print(f"[validate_answers] OK — {len(answers)}/{len(expected_qids)} answers, hard errors=0, smells={len(smells)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

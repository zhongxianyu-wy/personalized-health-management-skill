#!/usr/bin/env python3
"""Standalone interactive questionnaire builder — < 1 second, no MinerU.

The full orchestrator (``run_formal_analysis.py``) always runs MinerU OCR
before reaching the interactive stage, because demographics may need to
be regex-extracted from the report. When MinerU takes 30-300 seconds
(typical for 3-6 files) and the caller has a 120s timeout, the agent
never sees ``interactive_questionnaire.json`` and ends up skipping
Checkpoint 2 entirely.

This script short-circuits that by reading only
``cancerrisk-skill/config/formal.yaml::interactive.required_questions``
and the operator-supplied ``--sex`` / ``--age``. It writes
``interactive_questionnaire.json`` in the same shape the orchestrator
would, so the agent can elicit answers, write
``{"answers": {...}}`` to disk, and then invoke
``run_formal_analysis.py --answers <file>`` for the heavy pipeline.

Recommended ASK-FIRST flow (see SKILL.md "Quick start"):

    1) python cancerrisk-skill/scripts/build_questionnaire.py \\
         --sex male --age 29 \\
         --output /tmp/interactive_questionnaire.json
    2) agent reads JSON, asks user, writes /tmp/answers.json
    3) python cancerrisk-skill/scripts/run_formal_analysis.py \\
         --input <reports> \\
         --analysis-output <out> \\
         --answers /tmp/answers.json \\
         --person-id <id> --archives-root <abs path>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
import interactive_completion as interactive  # noqa: E402

CONFIG_DEFAULT = SCRIPTS_DIR.parent / "config" / "formal.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sex", required=True, choices=["male", "female"],
                        help="patient biological sex; drives the sex-branched questions")
    parser.add_argument("--age", required=True, type=int,
                        help="patient age in years (used for the demographics seed)")
    parser.add_argument("--config", default=str(CONFIG_DEFAULT),
                        help=f"path to formal.yaml (default: {CONFIG_DEFAULT})")
    parser.add_argument("--output", required=True,
                        help="where to write interactive_questionnaire.json")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_file():
        print(f"[build_questionnaire] FAIL: config {config_path} not found", file=sys.stderr)
        return 1

    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    demographics = {"sex": args.sex, "age": args.age}
    questionnaire = interactive.build_fixed_questionnaire(
        config=config,
        demographics=demographics,
        answers={"q_demographics_sex": args.sex, "q_demographics_age": args.age},
    )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(questionnaire, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"[build_questionnaire] wrote {out_path} "
        f"(sex={args.sex} age={args.age} questions={questionnaire['question_count']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

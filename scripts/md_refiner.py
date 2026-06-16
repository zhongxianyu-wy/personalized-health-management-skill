#!/usr/bin/env python3
"""Deterministic MD refiner for health-checkup OCR output.

The refiner is intentionally rule-based. It strips known navigation noise
and collapses excessive whitespace while preserving every header, table
row, and finding sentence. There is no external API call and no side
``.prompt.md`` file; the agent that runs this skill can still post-process
``refined_content.md`` further when authoring the health-summary input,
but the refiner does not pretend to defer that work to another model.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable


def refine_md(raw: str) -> str:
    """Remove navigation noise and redundant whitespace, preserve structure."""
    cleaned = re.sub(r"\[导航[^\]]*\]", "", raw)
    cleaned = re.sub(r"首页\s*关于我们", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def refine_md_file(
    input_path: Path,
    output_path: Path,
    call_llm: Callable[[str, str | None], str] | None = None,
) -> None:
    """Refine ``input_path`` and write the cleaned markdown to ``output_path``.

    ``call_llm`` is accepted only so tests can inject an alternative cleaner;
    no LLM is invoked when it is omitted.
    """
    raw = input_path.read_text(encoding="utf-8")
    if call_llm is not None:
        prompt = (
            "Distill the following medical examination markdown into clean, "
            "structured markdown. Remove navigation bars, redundant whitespace, "
            "page headers, and broken markup. Preserve all tables, headers, "
            "and medical findings.\n\n"
            f"{raw}"
        )
        refined = call_llm(prompt, "You are a medical document cleaning assistant.")
    else:
        refined = refine_md(raw)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(refined + "\n", encoding="utf-8")

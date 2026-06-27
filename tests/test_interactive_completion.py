"""Interactive questionnaire answer normalization tests."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import interactive_completion  # noqa: E402


def test_alcohol_threshold_labels_normalize_to_canonical_values() -> None:
    assert interactive_completion._normalize_answer_value("有（超过标准）") == "heavy"
    assert interactive_completion._normalize_answer_value("无（低于标准）") == "never"
    assert interactive_completion._normalize_answer_value("经常大量饮酒") == "heavy"

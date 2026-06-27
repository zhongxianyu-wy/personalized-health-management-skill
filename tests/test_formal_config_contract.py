"""formal.yaml interaction-question contract tests."""

from __future__ import annotations

from pathlib import Path

import yaml


CONFIG = Path(__file__).resolve().parent.parent / "config" / "formal.yaml"


def _common_questions() -> list[dict]:
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    return data["interactive"]["required_questions"]["common"]


def _question(question_id: str) -> dict:
    for question in _common_questions():
        if question.get("question_id") == question_id:
            return question
    raise AssertionError(f"missing question: {question_id}")


def test_genetic_result_questions_offer_unknown_as_fourth_option() -> None:
    """基因检测结果题保留「未知」选项，且固定排第 4。"""
    for qid in (
        "q_genetic_mutations_brca",
        "q_genetic_mutations_lynch_mlh1_msh2",
        "q_genetic_mutations_lynch_msh6_pms2",
    ):
        question = _question(qid)
        assert len(question["options"]) == 4
        assert question["options"][3] == {"value": "unknown", "label": "未知"}


def test_genetic_gate_question_keeps_yes_no_only() -> None:
    question = _question("q_has_genetic_mutation")
    assert question["options"] == [
        {"value": "yes", "label": "有基因突变"},
        {"value": "no", "label": "无"},
    ]


def test_alcohol_question_uses_daily_gram_threshold_and_two_options() -> None:
    question = _question("q_alcohol_status")
    assert question["prompt"] == "是否有饮酒史（男性平均每日超过40g酒精，女性平均每日超过20g酒精）？"
    assert question["options"] == [
        {"value": "heavy", "label": "有（超过标准）", "exists": True},
        {"value": "never", "label": "无（低于标准）", "exists": False},
    ]

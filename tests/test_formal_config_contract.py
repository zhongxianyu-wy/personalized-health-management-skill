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


def test_genetic_result_questions_do_not_offer_unknown_options() -> None:
    """基因检测结果题不再提供「未知/不清楚」选项；不确定时应在上游是否有突变题选择无。"""
    for qid in (
        "q_has_genetic_mutation",
        "q_genetic_mutations_brca",
        "q_genetic_mutations_lynch_mlh1_msh2",
        "q_genetic_mutations_lynch_msh6_pms2",
    ):
        question = _question(qid)
        visible = question["prompt"] + " " + " ".join(str(o.get("label", "")) for o in question.get("options", []))
        values = {o.get("value") for o in question.get("options", [])}
        assert "unknown" not in values
        assert "不清楚" not in visible
        assert "未知" not in visible


def test_alcohol_question_uses_daily_gram_threshold_and_two_options() -> None:
    question = _question("q_alcohol_status")
    assert question["prompt"] == "是否有饮酒史（男性平均每日超过40g酒精，女性平均每日超过20g酒精）？"
    assert question["options"] == [
        {"value": "heavy", "label": "有（超过标准）", "exists": True},
        {"value": "never", "label": "无（低于标准）", "exists": False},
    ]

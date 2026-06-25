from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import validate_screening_gap as vsg


def _write_kb(root: Path) -> None:
    target = root / "screening_personalized" / "md"
    target.mkdir(parents=True)
    (target / "肺癌筛查指南.md").write_text(
        "高危人群：**每年1次** LDCT\n",
        encoding="utf-8",
    )
    (target / "结直肠癌筛查指南.md").write_text(
        "每5-10年1次结肠镜\n",
        encoding="utf-8",
    )


def _snapshot() -> dict:
    return {
        "cancers": [
            {
                "cancer_id": "lung_cancer",
                "risk_tier": "medium",
                "posterior_probability": 0.008,
            },
            {
                "cancer_id": "gastric_cancer",
                "risk_tier": "low",
                "posterior_probability": 0.001,
            },
        ]
    }


def _draft() -> dict:
    return {
        "schema_version": "screening-recommendations-draft-v1",
        "cancer_risk": [
            {
                "dedup_key": "lung_ldct",
                "item_name": "低剂量螺旋 CT",
                "cancer_id": "lung_cancer",
                "source_name": "肺癌风险",
                "interval": "每年1次",
                "rationale": "按肺癌风险筛查。",
                "guideline_source": "肺癌筛查指南.md",
                "evidence_text": "高危人群：**每年1次** LDCT",
            }
        ],
        "other_abnormalities": [],
        "periodic_candidates": [
            {
                "dedup_key": "colorectal_colonoscopy",
                "item_name": "结肠镜",
                "gap_status": "overdue",
                "last_known_exam_date": "2014-06-01",
                "guideline_interval": "每5-10年1次",
                "rationale": "最近记录已超过指南周期。",
                "guideline_source": "结直肠癌筛查指南.md",
                "evidence_text": "每5-10年1次结肠镜",
                "timeline_evidence": "2014-06-01 结肠镜检查",
                "timeline_checked_sources": ["content.md", "refined.md", "screening_test_timeline.json"],
            }
        ],
        "dedup_audit": [],
    }


def _questionnaire() -> dict:
    return {
        "schema_version": "screening-gap-questionnaire-v1",
        "questions": [
            {
                "question_id": "gap_colorectal_colonoscopy_done",
                "dedup_key": "colorectal_colonoscopy",
                "type": "single_choice",
                "prompt": "最近10年内做过结肠镜吗？",
                "options": [
                    {"label": "做过", "value": "done"},
                    {"label": "未做过", "value": "not_done"},
                    {"label": "不清楚", "value": "unknown"},
                ],
            },
            {
                "question_id": "gap_colorectal_colonoscopy_result",
                "dedup_key": "colorectal_colonoscopy",
                "type": "single_choice",
                "conditional_on": {
                    "question_id": "gap_colorectal_colonoscopy_done",
                    "value": "done",
                },
                "prompt": "结果如何？",
                "options": [
                    {"label": "正常", "value": "normal"},
                    {"label": "异常", "value": "abnormal"},
                    {"label": "不清楚", "value": "unknown"},
                ],
            },
        ],
    }


def _final(periodic: list[dict] | None = None, excluded: list[dict] | None = None) -> dict:
    return {
        "schema_version": "screening-recommendations-final-v1",
        "cancer_risk": deepcopy(_draft()["cancer_risk"]),
        "other_abnormalities": [],
        "periodic_management": periodic if periodic is not None else [],
        "excluded_done_normal": excluded if excluded is not None else [],
        "dedup_audit": [],
    }


def test_draft_rejects_duplicate_dedup_keys_across_sections(tmp_path: Path) -> None:
    _write_kb(tmp_path)
    draft = _draft()
    draft["other_abnormalities"] = [
        {
            **deepcopy(draft["cancer_risk"][0]),
            "source_name": "肺结节",
        }
    ]
    errors = vsg.validate_draft(draft, snapshot=_snapshot(), knowledge_root=tmp_path)
    assert any("duplicate dedup_key" in e for e in errors)


def test_draft_rejects_evidence_not_literal_source_substring(tmp_path: Path) -> None:
    _write_kb(tmp_path)
    draft = _draft()
    draft["periodic_candidates"][0]["evidence_text"] = "每10年做一次结肠镜"
    errors = vsg.validate_draft(draft, snapshot=_snapshot(), knowledge_root=tmp_path)
    assert any("literal substring" in e for e in errors)


def test_draft_rejects_periodic_candidate_without_timeline_audit(tmp_path: Path) -> None:
    _write_kb(tmp_path)
    draft = _draft()
    draft["periodic_candidates"][0].pop("timeline_checked_sources")
    errors = vsg.validate_draft(draft, snapshot=_snapshot(), knowledge_root=tmp_path)
    assert any("timeline_checked_sources" in e for e in errors)


def test_draft_accepts_never_recorded_with_empty_timeline_evidence(tmp_path: Path) -> None:
    _write_kb(tmp_path)
    draft = _draft()
    candidate = draft["periodic_candidates"][0]
    candidate["gap_status"] = "never_recorded"
    candidate["last_known_exam_date"] = None
    candidate["timeline_evidence"] = ""
    errors = vsg.validate_draft(draft, snapshot=_snapshot(), knowledge_root=tmp_path)
    assert errors == []


def test_draft_rejects_cancer_below_medium_snapshot_tier(tmp_path: Path) -> None:
    _write_kb(tmp_path)
    draft = _draft()
    draft["cancer_risk"][0]["cancer_id"] = "gastric_cancer"
    errors = vsg.validate_draft(draft, snapshot=_snapshot(), knowledge_root=tmp_path)
    assert any("medium or above" in e for e in errors)


def test_draft_rejects_inconsistent_dedup_audit(tmp_path: Path) -> None:
    _write_kb(tmp_path)
    draft = _draft()
    draft["dedup_audit"] = [
        {
            "dedup_key": "missing_key",
            "removed_from": "periodic_candidates",
            "kept_in": "cancer_risk",
            "reason": "重复",
        }
    ]
    errors = vsg.validate_draft(draft, snapshot=_snapshot(), knowledge_root=tmp_path)
    assert any("dedup_audit" in e for e in errors)


def test_questionnaire_requires_done_and_result_pair_per_candidate() -> None:
    questionnaire = _questionnaire()
    questionnaire["questions"].pop()
    errors = vsg.validate_questionnaire(_draft(), questionnaire)
    assert any("result question" in e for e in errors)


def test_questionnaire_rejects_dedup_removed_candidate() -> None:
    draft = _draft()
    draft["periodic_candidates"] = []
    draft["dedup_audit"] = [
        {
            "dedup_key": "colorectal_colonoscopy",
            "removed_from": "periodic_candidates",
            "kept_in": "cancer_risk",
            "reason": "重复",
        }
    ]
    errors = vsg.validate_questionnaire(draft, _questionnaire())
    assert any("not a periodic candidate" in e for e in errors)


def test_questionnaire_accepts_empty_candidates_with_empty_questions() -> None:
    draft = _draft()
    draft["periodic_candidates"] = []
    errors = vsg.validate_questionnaire(
        draft,
        {"schema_version": "screening-gap-questionnaire-v1", "questions": []},
    )
    assert errors == []


def test_answers_require_triggered_result_question() -> None:
    answers = {"answers": {"gap_colorectal_colonoscopy_done": "done"}}
    errors = vsg.validate_answers(_questionnaire(), answers)
    assert any("gap_colorectal_colonoscopy_result" in e for e in errors)


def test_final_rejects_done_normal_in_periodic_management() -> None:
    answers = {
        "answers": {
            "gap_colorectal_colonoscopy_done": "done",
            "gap_colorectal_colonoscopy_result": "normal",
        }
    }
    final = _final(
        periodic=[
            {
                "dedup_key": "colorectal_colonoscopy",
                "item_name": "结肠镜",
                "disposition": "done_normal",
            }
        ]
    )
    errors = vsg.validate_final(_draft(), _questionnaire(), answers, final)
    assert any("done + normal" in e for e in errors)


def test_final_rejects_missing_disposition_for_not_done() -> None:
    answers = {"answers": {"gap_colorectal_colonoscopy_done": "not_done"}}
    errors = vsg.validate_final(_draft(), _questionnaire(), answers, _final())
    assert any("missing final disposition" in e for e in errors)


def test_final_rejects_duplicate_keys_across_abc() -> None:
    final = _final(
        periodic=[
            {
                "dedup_key": "lung_ldct",
                "item_name": "低剂量螺旋 CT",
                "disposition": "not_done",
            }
        ]
    )
    errors = vsg.validate_final(
        _draft(),
        _questionnaire(),
        {"answers": {"gap_colorectal_colonoscopy_done": "not_done"}},
        final,
    )
    assert any("duplicate dedup_key" in e for e in errors)


def test_final_rejects_changed_cancer_recommendation_set() -> None:
    final = _final()
    final["cancer_risk"] = []
    errors = vsg.validate_final(
        _draft(),
        _questionnaire(),
        {"answers": {"gap_colorectal_colonoscopy_done": "not_done"}},
        final,
    )
    assert any("cancer_risk dedup_key set" in e for e in errors)


def test_final_rejects_wrong_disposition_label() -> None:
    final = _final(
        periodic=[
            {
                "dedup_key": "colorectal_colonoscopy",
                "item_name": "结肠镜",
                "disposition": "done_abnormal",
            }
        ]
    )
    errors = vsg.validate_final(
        _draft(),
        _questionnaire(),
        {"answers": {"gap_colorectal_colonoscopy_done": "not_done"}},
        final,
    )
    assert any("disposition must be 'not_done'" in e for e in errors)


def test_final_accepts_done_normal_exclusion() -> None:
    answers = {
        "answers": {
            "gap_colorectal_colonoscopy_done": "done",
            "gap_colorectal_colonoscopy_result": "normal",
        }
    }
    final = _final(
        excluded=[
            {
                "dedup_key": "colorectal_colonoscopy",
                "item_name": "结肠镜",
                "disposition": "done_normal",
            }
        ]
    )
    assert vsg.validate_final(_draft(), _questionnaire(), answers, final) == []


def test_maintain_must_match_final_periodic_management() -> None:
    final = _final(
        periodic=[
            {
                "dedup_key": "colorectal_colonoscopy",
                "item_name": "结肠镜",
                "disposition": "not_done",
            }
        ]
    )
    timeline = {
        "priority": [],
        "important": [],
        "maintain": [{"dedup_key": "cervical_screening", "item_name": "HPV"}],
    }
    errors = vsg.validate_report_artifacts(final, timeline, [])
    assert any("maintain dedup_key set" in e for e in errors)


def test_cli_returns_one_for_semantic_errors(tmp_path: Path) -> None:
    _write_kb(tmp_path / "kb")
    draft_path = tmp_path / "draft.json"
    snapshot_path = tmp_path / "snapshot.json"
    draft = _draft()
    draft["periodic_candidates"][0]["evidence_text"] = "not literal"
    draft_path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    snapshot_path.write_text(json.dumps(_snapshot(), ensure_ascii=False), encoding="utf-8")
    assert vsg.main([
        "draft",
        "--draft", str(draft_path),
        "--snapshot", str(snapshot_path),
        "--knowledge-root", str(tmp_path / "kb"),
    ]) == 1

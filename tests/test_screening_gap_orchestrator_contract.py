from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run_formal_analysis.py"


def _load():
    spec = importlib.util.spec_from_file_location("rfa_cp5", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _stage(tmp_path: Path) -> tuple[Path, Path]:
    artifacts = tmp_path / "artifacts"
    kb = tmp_path / "kb"
    (kb / "md").mkdir(parents=True)
    (kb / "md" / "结直肠癌筛查指南.md").write_text(
        "每5-10年1次结肠镜",
        encoding="utf-8",
    )
    _write(
        artifacts / "snapshot_risk.json",
        {"cancers": []},
    )
    _write(
        artifacts / "screening_recommendations_draft.json",
        {
            "schema_version": "screening-recommendations-draft-v1",
            "cancer_risk": [],
            "other_abnormalities": [],
            "periodic_candidates": [
                {
                    "dedup_key": "colorectal_colonoscopy",
                    "item_name": "结肠镜",
                    "gap_status": "never_recorded",
                    "last_known_exam_date": None,
                    "guideline_interval": "每5-10年1次",
                    "rationale": "档案无记录",
                    "guideline_source": "结直肠癌筛查指南.md",
                    "evidence_text": "每5-10年1次结肠镜",
                    "timeline_evidence": "",
                    "timeline_checked_sources": ["content.md", "refined.md"],
                }
            ],
            "dedup_audit": [],
        },
    )
    _write(
        artifacts / "screening_gap_questionnaire.json",
        {
            "schema_version": "screening-gap-questionnaire-v1",
            "questions": [
                {
                    "question_id": "gap_colorectal_colonoscopy_done",
                    "dedup_key": "colorectal_colonoscopy",
                    "type": "single_choice",
                    "prompt": "最近10年做过吗？",
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
        },
    )
    return artifacts, kb


def test_screening_gap_stop_point_exists() -> None:
    assert "screening-gap" in _load().STOP_AFTER_CHOICES


def test_cli_has_independent_screening_gap_answers_flag() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--screening-gap-answers" in result.stdout


def test_gate_missing_draft_is_exit_11(tmp_path: Path) -> None:
    mod = _load()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    try:
        mod._validate_screening_gap_stage(
            artifacts=artifacts,
            knowledge_root=tmp_path,
            screening_gap_answers_path=None,
        )
    except mod.ScreeningGapGateError as exc:
        assert exc.code == 11
    else:
        raise AssertionError("missing CP5 draft must fail")


def test_gate_questions_without_independent_answers_is_exit_12(tmp_path: Path) -> None:
    mod = _load()
    artifacts, kb = _stage(tmp_path)
    try:
        mod._validate_screening_gap_stage(
            artifacts=artifacts,
            knowledge_root=kb,
            screening_gap_answers_path=None,
        )
    except mod.ScreeningGapGateError as exc:
        assert exc.code == 12
    else:
        raise AssertionError("missing CP5 answers must fail")


def test_gate_answers_without_final_is_exit_13(tmp_path: Path) -> None:
    mod = _load()
    artifacts, kb = _stage(tmp_path)
    answers = tmp_path / "screening_gap_answers.json"
    _write(answers, {"answers": {"gap_colorectal_colonoscopy_done": "not_done"}})
    try:
        mod._validate_screening_gap_stage(
            artifacts=artifacts,
            knowledge_root=kb,
            screening_gap_answers_path=answers,
        )
    except mod.ScreeningGapGateError as exc:
        assert exc.code == 13
    else:
        raise AssertionError("missing CP5 final artifact must fail")


def test_gate_accepts_complete_independent_cp5(tmp_path: Path) -> None:
    mod = _load()
    artifacts, kb = _stage(tmp_path)
    answers = tmp_path / "screening_gap_answers.json"
    _write(answers, {"answers": {"gap_colorectal_colonoscopy_done": "not_done"}})
    _write(
        artifacts / "screening_recommendations_final.json",
        {
            "schema_version": "screening-recommendations-final-v1",
            "cancer_risk": [],
            "other_abnormalities": [],
            "periodic_management": [
                {
                    "dedup_key": "colorectal_colonoscopy",
                    "item_name": "结肠镜",
                    "disposition": "not_done",
                }
            ],
            "excluded_done_normal": [],
            "dedup_audit": [],
        },
    )
    result = mod._validate_screening_gap_stage(
        artifacts=artifacts,
        knowledge_root=kb,
        screening_gap_answers_path=answers,
    )
    assert result["candidate_count"] == 1
    assert result["recommended_count"] == 1


def test_report_artifact_gate_rejects_maintain_mismatch(tmp_path: Path) -> None:
    mod = _load()
    artifacts, _ = _stage(tmp_path)
    _write(
        artifacts / "screening_recommendations_final.json",
        {
            "schema_version": "screening-recommendations-final-v1",
            "cancer_risk": [],
            "other_abnormalities": [],
            "periodic_management": [
                {"dedup_key": "colorectal_colonoscopy", "item_name": "结肠镜"}
            ],
            "excluded_done_normal": [],
            "dedup_audit": [],
        },
    )
    _write(
        artifacts / "timeline_tiers.json",
        {
            "priority": [],
            "important": [],
            "maintain": [{"dedup_key": "cervical_screening", "item_name": "HPV"}],
        },
    )
    _write(artifacts / "package_tiers.json", [])
    try:
        mod._validate_screening_report_artifacts(artifacts=artifacts)
    except mod.ScreeningGapGateError as exc:
        assert exc.code == 13
    else:
        raise AssertionError("CP5/report mismatch must fail")

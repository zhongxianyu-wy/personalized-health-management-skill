from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import build_cp5_context_pack as cp5  # noqa: E402


def _write(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _knowledge_root(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    _write(
        root / "screening_general" / "json" / "periodic_screening_schedule.json",
        {
            "items": [
                {
                    "id": "bp_annual",
                    "name": "血压测量",
                    "method": "血压测量",
                    "start_age": 18,
                    "stop_age": 999,
                    "gender": "all",
                    "interval_years": 1,
                    "high_risk_only": False,
                    "aliases": ["血压"],
                },
                {
                    "id": "lung_ldct",
                    "name": "肺癌筛查（LDCT）",
                    "method": "低剂量胸部CT",
                    "start_age": 50,
                    "stop_age": 74,
                    "gender": "all",
                    "interval_years": 1,
                    "high_risk_only": True,
                    "aliases": ["LDCT", "低剂量CT", "胸部CT"],
                },
                {
                    "id": "cervical_tct_hpv",
                    "name": "宫颈癌筛查（TCT/HPV）",
                    "method": "TCT/HPV",
                    "start_age": 25,
                    "stop_age": 64,
                    "gender": "female",
                    "interval_years": 3,
                    "high_risk_only": False,
                    "aliases": ["宫颈", "HPV", "TCT"],
                },
            ]
        },
    )
    return root


def test_prefilters_periodic_candidates_by_age_and_sex(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    _write(
        artifacts / "snapshot_risk.json",
        {"person_context": {"sex": "male", "age": 68}, "cancers": []},
    )
    pack = cp5.build_context_pack(artifacts=artifacts, knowledge_root=_knowledge_root(tmp_path))
    names = [row["name"] for row in pack["periodic_candidates_prefiltered"]]
    assert "血压测量" in names
    assert "肺癌筛查（LDCT）" in names
    assert "宫颈癌筛查（TCT/HPV）" not in names
    assert pack["person_context"] == {"sex": "male", "age": 68}


def test_matches_existing_screening_evidence_and_marks_seen_with_date(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    _write(
        artifacts / "snapshot_risk.json",
        {"person_context": {"sex": "male", "age": 68}, "cancers": []},
    )
    _write(
        artifacts / "screening_test_timeline.json",
        {
            "screening_tests": [
                {
                    "test_id": "ldct",
                    "test_name": "胸部LDCT",
                    "exam_date": "2026-01-02",
                    "result": "未见异常",
                    "source": "体检报告",
                }
            ]
        },
    )
    pack = cp5.build_context_pack(artifacts=artifacts, knowledge_root=_knowledge_root(tmp_path))
    lung = next(row for row in pack["periodic_candidates_prefiltered"] if row["id"] == "lung_ldct")
    assert lung["gap_prefill"] == "seen_with_date"
    assert lung["matched_evidence"][0]["exam_date"] == "2026-01-02"
    assert pack["existing_screening_evidence"][0]["matched_candidate_ids"] == ["lung_ldct"]


def test_extracts_only_medium_plus_cancer_risks_and_health_abnormalities(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    _write(
        artifacts / "snapshot_risk.json",
        {
            "person_context": {"sex": "female", "age": 45},
            "cancers": [
                {"cancer_id": "lung_cancer", "cancer_name_zh": "肺癌", "risk_tier": "medium", "posterior_probability": 0.006},
                {"cancer_id": "thyroid_cancer", "cancer_name_zh": "甲状腺癌", "risk_tier": "low", "posterior_probability": 0.001},
                {"cancer_id": "breast_cancer", "cancer_name_zh": "乳腺癌", "risk_tier": "high", "posterior_probability": 0.03},
            ],
        },
    )
    _write(
        artifacts / "health_summary_structured_summary.json",
        {
            "items": [
                {"name": "血糖升高", "risk_level": "中风险", "recommendation": "复查HbA1c"},
                {"name": "轻度脂肪肝", "risk_level": "低风险", "recommendation": "生活方式干预"},
            ]
        },
    )
    pack = cp5.build_context_pack(artifacts=artifacts, knowledge_root=_knowledge_root(tmp_path))
    assert [row["cancer_id"] for row in pack["cancer_risk_medium_plus"]] == ["lung_cancer", "breast_cancer"]
    assert pack["health_abnormalities"] == [
        {"name": "血糖升高", "risk_level": "中风险", "recommendation": "复查HbA1c"},
        {"name": "轻度脂肪肝", "risk_level": "低风险", "recommendation": "生活方式干预"},
    ]
    assert "优先读取本 context pack" in " ".join(pack["llm_instructions"])

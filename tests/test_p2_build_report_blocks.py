"""P2-T1: build_report_json carries person.name + CP4 health_summary.blocks."""
import importlib.util
import json
from pathlib import Path


def _load():
    spec = importlib.util.spec_from_file_location(
        "brj", Path(__file__).parent.parent / "scripts" / "build_report_json.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _write(p, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def test_blocks_and_name_carried(tmp_path):
    art = tmp_path / "artifacts"
    art.mkdir()
    _write(art / "snapshot_risk.json", {
        "cancers": [], "section4_screening": [], "uncertainties_summary": {},
        "person_context": {"sex": "male", "age": 68},
    })
    _write(art / "health_summary_structured_summary.json", {
        "patient_data": {"name": "张三"},
        "assessment_result": {
            "risk_level": "🟠 高风险",
            "abnormal_table": "<table><tr><td>X</td></tr></table>",
            "disease_cards": "<table></table>",
            "advice_list": "<ul><li>戒烟</li></ul>",
            "conclusion_table": "<table></table>",
            "core_risk_factors": "A",
            "overall_assessment": "B",
        },
    })
    m = _load()
    rep = m.assemble_report_json(
        artifacts=art, out=tmp_path, answers_path=None,
        person_id="p1", run_id="r1", evidence_version="ev1",
    )
    assert rep["person"]["name"] == "张三"
    b = rep["health_summary"]["blocks"]
    assert b["risk_level"] == "🟠 高风险"
    assert "<table>" in b["abnormal_table"]
    assert b["advice_list"] == "<ul><li>戒烟</li></ul>"
    assert b["overall_assessment"] == "B"


def test_blocks_graceful_when_absent(tmp_path):
    art = tmp_path / "artifacts"
    art.mkdir()
    _write(art / "snapshot_risk.json", {
        "cancers": [], "section4_screening": [], "uncertainties_summary": {},
        "person_context": {},
    })
    m = _load()
    rep = m.assemble_report_json(
        artifacts=art, out=tmp_path, answers_path=None,
        person_id="p1", run_id="r1", evidence_version=None,
    )
    assert rep["person"]["name"] in (None, "p1")
    assert rep["health_summary"]["blocks"]["risk_level"] in (None, "")


def test_p1_keys_preserved(tmp_path):
    """Backward compat: P1 health_summary keys still present."""
    art = tmp_path / "artifacts"
    art.mkdir()
    _write(art / "snapshot_risk.json", {"cancers": [], "section4_screening": [],
                                        "uncertainties_summary": {}, "person_context": {}})
    m = _load()
    rep = m.assemble_report_json(artifacts=art, out=tmp_path, answers_path=None,
                                person_id="p1", run_id="r1", evidence_version=None)
    hs = rep["health_summary"]
    assert "status" in hs and "abnormal_non_cancer_count" in hs and "items" in hs
    assert "blocks" in hs

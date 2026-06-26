"""Tests for validate_screening_gap.py — v2.0.4 精简版（3 条核心规则）。"""
from __future__ import annotations
import sys
from pathlib import Path
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import validate_screening_gap as vsg  # noqa: E402

def _rec(**over):
    base = {
        "cancer_risk": [{"dedup_key": "lung_ldct", "cancer_id": "lung_cancer", "item_name": "LDCT"}],
        "other_abnormalities": [{"dedup_key": "lipid_panel", "item_name": "血脂复查"}],
        "periodic_management": [{"dedup_key": "crc", "item_name": "结肠镜", "disposition": "not_done"}],
        "excluded_done_normal": [],
    }
    base.update(over)
    return base

def _snap():
    return {"cancers": [{"cancer_id": "lung_cancer", "risk_tier": "high"},
                        {"cancer_id": "thyroid_cancer", "risk_tier": "low"}]}

def test_valid():
    assert vsg.validate(_rec(), _snap()) == []

def test_dup_key():
    rec = _rec(other_abnormalities=[{"dedup_key": "lung_ldct"}])
    assert any("lung_ldct" in e and "重复" in e for e in vsg.validate(rec, _snap()))

def test_done_normal_rejected():
    rec = _rec(periodic_management=[{"dedup_key": "bp", "disposition": "done_normal"}])
    assert any("做过且正常" in e or ("done" in e.lower() and "normal" in e.lower()) for e in vsg.validate(rec, _snap()))

def test_done_normal_via_gap_answer():
    rec = _rec(periodic_management=[{"dedup_key": "bp", "gap_answer": "done_normal"}])
    assert any("做过且正常" in e or ("done" in e.lower() and "normal" in e.lower()) for e in vsg.validate(rec, _snap()))

def test_cancer_below_medium():
    rec = _rec(cancer_risk=[{"dedup_key": "thy", "cancer_id": "thyroid_cancer"}])
    assert any("thyroid" in e and "medium" in e for e in vsg.validate(rec, _snap()))

def test_cancer_medium_ok():
    assert vsg.validate(_rec(), _snap()) == []

def test_not_dict():
    assert vsg.validate([])

def test_missing_section():
    errs = vsg.validate({"cancer_risk": [], "other_abnormalities": []})
    assert any("periodic_management" in e for e in errs)

def test_empty_sections_ok():
    rec = {"cancer_risk": [], "other_abnormalities": [], "periodic_management": []}
    assert vsg.validate(rec, _snap()) == []

def test_no_snapshot_ok():
    rec = _rec(cancer_risk=[{"dedup_key": "any", "cancer_id": "unknown"}])
    assert vsg.validate(rec, None) == []

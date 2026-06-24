import importlib.util
import os
from pathlib import Path
def _load():
    spec = importlib.util.spec_from_file_location("rfa", Path(__file__).parent.parent/"scripts"/"run_formal_analysis.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod
def test_report_present(): assert "report" in _load().STOP_AFTER_CHOICES
def test_old_removed():
    m=_load()
    for r in ("health-summary","snapshot","longitudinal"): assert r not in m.STOP_AFTER_CHOICES

def test_archives_default_is_cwd_not_skill_package(monkeypatch, tmp_path):
    """v0.1.4: default archive root must be cwd/output (sandbox-friendly), NOT
    the read-only skill package. Guards against regressions to SKILL_ROOT/output."""
    m = _load()
    # SKILL_ROOT sentinel must no longer pin archives into the skill package.
    assert m.ARCHIVES_DEFAULT is None
    # Default (no env, no flag) → cwd/output, never the skill package.
    monkeypatch.delenv("CANCERRISK_OUTPUT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    resolved = m._resolve_archives_root(None)
    assert resolved == str(tmp_path / "output")
    assert str(m.SKILL_ROOT) not in resolved
    # Explicit flag always wins.
    assert m._resolve_archives_root("/explicit/path") == "/explicit/path"
    # Env override wins over cwd default.
    monkeypatch.setenv("CANCERRISK_OUTPUT_DIR", str(tmp_path / "fromenv"))
    assert m._resolve_archives_root(None) == str(tmp_path / "fromenv")


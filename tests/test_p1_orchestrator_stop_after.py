import importlib.util
from pathlib import Path
def _load():
    spec = importlib.util.spec_from_file_location("rfa", Path(__file__).parent.parent/"scripts"/"run_formal_analysis.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod
def test_report_present(): assert "report" in _load().STOP_AFTER_CHOICES
def test_old_removed():
    m=_load()
    for r in ("health-summary","snapshot","longitudinal"): assert r not in m.STOP_AFTER_CHOICES

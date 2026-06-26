"""CP5 orchestrator contract — v2.0.4 simplified (1 artifact, no questionnaire/answers)."""
from pathlib import Path
import importlib.util

def _load():
    spec = importlib.util.spec_from_file_location("rfa", Path(__file__).parent.parent/"scripts"/"run_formal_analysis.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

def test_screening_gap_in_stop_choices():
    assert "screening-gap" in _load().STOP_AFTER_CHOICES

def test_voi_removed():
    source = (Path(__file__).parent.parent / "scripts" / "run_formal_analysis.py").read_text()
    assert "import voi_calculator" not in source

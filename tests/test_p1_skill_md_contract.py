"""P1 contract: SKILL.md describes the single-report, auto-archive workflow.

These assertions lock the operating guide to the v1.4 P1 architecture so the
prose can't silently drift back to the retired 4-HTML / manual-入档 flow.
"""
from pathlib import Path

SKILL_MD = Path(__file__).parent.parent / "SKILL.md"
OLD_HTML = (
    "health_summary.html",
    "snapshot_risk.html",
    "longitudinal_risk.html",
    "index.html",
)


def _text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _section(text: str, header: str) -> str:
    """Return the body of a '## <header>' section up to the next '## '."""
    start = text.index(f"## {header}")
    rest = text[start + len(header) + 3 :]
    nxt = rest.find("\n## ")
    return rest if nxt == -1 else rest[:nxt]


def test_output_contract_is_single_report():
    contract = _section(_text(), "Output Contract")
    assert "report.html" in contract
    assert "report.json" in contract
    for old in OLD_HTML:
        assert old not in contract, f"retired deliverable {old} still in Output Contract"


def test_no_retired_html_anywhere():
    text = _text()
    for old in OLD_HTML:
        assert old not in text, f"retired HTML {old} still referenced in SKILL.md"


def test_pipeline_stages_lists_integrated_report():
    stages = _section(_text(), "Pipeline Stages")
    assert "report.html" in stages


def test_workflow_has_no_manual_archive_confirmation():
    text = _text()
    assert "确认入档" not in text
    assert "HALT_FOR_USER_CONFIRMATION" not in text
    # The exit-4 manual archive checkpoint is retired under auto-archive.
    assert "awaiting confirmation" not in text

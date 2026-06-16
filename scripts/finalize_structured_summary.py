#!/usr/bin/env python3
"""Task6 Checkpoint 4 helper — safely write health_summary_structured_summary.json.

The orchestrator stages an *almost-complete* skeleton at
``artifacts/health_summary_structured_summary.json`` with
``raw_assessment_markdown`` already correctly JSON-escaped from the API
response. The agent's only job is to add ``patient_data`` updates and
``assessment_result`` HTML fragments. When the agent hand-writes the
final JSON via heredoc / string concat, quotes / backslashes / emoji
inside HTML fragments routinely break the JSON, and the next
orchestrator run dies on ``json.loads``.

This script eliminates that pitfall: agent supplies the patient_data +
assessment_result content in a ``--fills <json>`` file (or per-field
plain-text files via ``--fragment KEY=path``); we deep-merge onto the
skeleton, set ``status=ready_for_render``, validate, and re-serialise
via ``json.dump`` — which guarantees correct escaping regardless of
the agent's input.

Recommended Checkpoint 4 flow:

    # Tier 1 — single JSON fills file (agent-friendly):
    cat > /tmp/fills.json <<'EOF'
    {
      "patient_data": { "name": "钟贤宇", "age": "29", ... },
      "assessment_result": {
        "risk_level": "🟠 高风险", ...,
        "lab_results_table":  "@/tmp/lab.html",
        "abnormal_table":     "@/tmp/abnormal.html",
        "disease_cards":      "@/tmp/cards.html",
        "advice_list":        "@/tmp/advice.html",
        "conclusion_table":   "@/tmp/conclusion.html"
      }
    }
    EOF
    python cancerrisk-skill/scripts/finalize_structured_summary.py \\
      --analysis-output <out> --fills /tmp/fills.json

    # Tier 2 — per-field plain-text files (no JSON authoring at all):
    python cancerrisk-skill/scripts/finalize_structured_summary.py \\
      --analysis-output <out> \\
      --field patient_data.name=钟贤宇 \\

      --fragment assessment_result.lab_results_table=/tmp/lab.html \\
      --fragment assessment_result.disease_cards=/tmp/cards.html

Any value prefixed with ``@`` in --fills JSON is treated as a path and
substituted with the raw file contents (handy for HTML fragments).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
# Pull the validator + canonical field lists from the renderer so both
# stay in lock-step.
import render_health_summary as render  # noqa: E402

STATUS_READY = render.STRUCTURED_SUMMARY_READY


def _resolve_at_path(value, base: Path):
    """If value looks like ``@/path`` or ``@relative/path``, read the file."""
    if isinstance(value, str) and value.startswith("@"):
        ref = value[1:]
        p = Path(ref) if Path(ref).is_absolute() else (base / ref)
        if not p.is_file():
            raise FileNotFoundError(f"@-reference target not found: {p}")
        return p.read_text(encoding="utf-8")
    return value


def _walk_resolve(node, base: Path):
    """Recursively resolve all @path references inside a dict / list / str."""
    if isinstance(node, dict):
        return {k: _walk_resolve(v, base) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk_resolve(v, base) for v in node]
    return _resolve_at_path(node, base)


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _apply_dotted_field(target: dict, key: str, value: str) -> None:
    """Set ``patient_data.name`` style dotted-keys onto ``target``."""
    parts = key.split(".")
    cur = target
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-output", required=True,
                        help="the orchestrator's --analysis-output directory")
    parser.add_argument("--fills", default=None,
                        help="path to a fills JSON file with patient_data + assessment_result")
    parser.add_argument("--field", action="append", default=[],
                        metavar="DOTTED.KEY=VALUE",
                        help="set a single scalar field; repeatable")
    parser.add_argument("--fragment", action="append", default=[],
                        metavar="DOTTED.KEY=PATH",
                        help="set a field from the contents of a file (no JSON escaping needed); repeatable")
    parser.add_argument("--status", default=STATUS_READY,
                        help=f"status to write (default: {STATUS_READY})")
    args = parser.parse_args()

    out = Path(args.analysis_output)
    skeleton_path = out / "artifacts" / "health_summary_structured_summary.json"
    if not skeleton_path.is_file():
        print(f"[finalize] FAIL: skeleton not found at {skeleton_path}. "
              "Run the orchestrator's health-summary-api stage first.",
              file=sys.stderr)
        return 1

    try:
        skeleton = json.loads(skeleton_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(
            f"[finalize] FAIL: skeleton at {skeleton_path} is itself invalid JSON: {exc}. "
            "Re-run the orchestrator's health-summary-api stage to regenerate it.",
            file=sys.stderr,
        )
        return 1

    fills: dict = {}
    if args.fills:
        fills_path = Path(args.fills)
        if not fills_path.is_file():
            print(f"[finalize] FAIL: --fills file not found: {fills_path}", file=sys.stderr)
            return 1
        try:
            raw = json.loads(fills_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(
                f"[finalize] FAIL: --fills file {fills_path} is invalid JSON: {exc}. "
                "Likely cause: an HTML fragment contains an unescaped quote. "
                "Move that fragment to a separate file and reference it with "
                "\"@/path/to/file\" inside fills.json, or use --fragment KEY=PATH.",
                file=sys.stderr,
            )
            return 1
        fills = _walk_resolve(raw, base=fills_path.parent)

    for spec in args.field:
        if "=" not in spec:
            print(f"[finalize] FAIL: --field expects DOTTED.KEY=VALUE, got {spec!r}", file=sys.stderr)
            return 1
        key, value = spec.split("=", 1)
        _apply_dotted_field(fills, key.strip(), value)

    for spec in args.fragment:
        if "=" not in spec:
            print(f"[finalize] FAIL: --fragment expects DOTTED.KEY=PATH, got {spec!r}", file=sys.stderr)
            return 1
        key, path_str = spec.split("=", 1)
        p = Path(path_str.strip())
        if not p.is_file():
            print(f"[finalize] FAIL: --fragment target not found: {p}", file=sys.stderr)
            return 1
        _apply_dotted_field(fills, key.strip(), p.read_text(encoding="utf-8"))

    merged = _deep_merge(skeleton, fills)
    merged["status"] = args.status

    try:
        render._validate_structured_summary(merged)  # noqa: SLF001
    except SystemExit as exc:
        # _validate raises via _fail → SystemExit; surface as a clean error.
        msg = str(exc) or "structured summary failed validation"
        print(f"[finalize] FAIL: {msg}", file=sys.stderr)
        return 1

    skeleton_path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"[finalize] wrote {skeleton_path} "
        f"status={merged['status']} "
        f"raw_md_chars={len(merged.get('raw_assessment_markdown') or '')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

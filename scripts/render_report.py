#!/usr/bin/env python3
"""Thin Jinja2 renderer for the single integrated report (P1 Task 4).

Loads ``templates/integrated_report_temp.html`` (the sole authority template,
strictly aligned to ``temp/html-preview-10.html``) under StrictUndefined and
renders a report.json-shaped dict plus a disclaimer. NO math, NO LLM, NO network.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

SKILL_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DEFAULT = SKILL_ROOT / "templates" / "integrated_report_temp.html"
CONFIG_DEFAULT = SKILL_ROOT / "config" / "formal.yaml"


def render_report(report: dict[str, Any], template_path: Path, disclaimer: str) -> str:
    """Render ``report`` against ``template_path`` and return the HTML string."""
    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        undefined=StrictUndefined,
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template(template_path.name)
    return template.render(**report, disclaimer=disclaimer)


def write_report_html(
    report: dict[str, Any], template_path: Path, disclaimer: str, out: Path
) -> Path:
    """Render and write ``<out>/report.html`` (UTF-8); return the path."""
    html_text = render_report(report, template_path, disclaimer)
    target = out / "report.html"
    target.write_text(html_text, encoding="utf-8")
    return target


def main() -> None:
    import yaml

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, help="path to report.json")
    parser.add_argument("--out", required=True, help="output dir for report.html")
    parser.add_argument("--template", default=str(TEMPLATE_DEFAULT))
    parser.add_argument("--config", default=str(CONFIG_DEFAULT))
    args = parser.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    disclaimer = str((config.get("safety") or {}).get("disclaimer") or "本报告仅用于健康管理参考。")
    target = write_report_html(report, Path(args.template), disclaimer, Path(args.out))
    print(f"[report_html] rendered -> {target}")


if __name__ == "__main__":
    main()

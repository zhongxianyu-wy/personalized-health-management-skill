"""Map LLM-filled indicator_fill_schemas output → risk_factor_assertion_template format.

Reads:
  - indicator_fill_schemas.json  (ontology; template_id + tier definitions)
  - <artifacts>/filled_indicators.json  (LLM output; one entry per template_id)
  - risk_factor_assertion_template.json  (full assertion template; already built by
    build_assertion_fill_template.py — contains risk_evidence_values per factor_key)

Writes:
  - <artifacts>/risk_factor_assertion_template_merged.json  (same format as the full
    template, but with LLM fill fields populated from filled_indicators.json)

The downstream risk_factor_gate.py and snapshot_risk.py are unchanged — they read the
merged template in the same format they already consume.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load_json(path: Path) -> dict | list:
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _build_tier_to_factor_key_map(full_template: list[dict]) -> dict[tuple[str, str], str]:
    """Map (factor_id, factor_level) → factor_key for fast lookup."""
    return {
        (entry["factor_id"], entry.get("factor_level", "present")): entry["factor_key"]
        for entry in full_template
        if "factor_key" in entry
    }


def merge(
    schemas_path: Path,
    filled_path: Path,
    full_template_path: Path,
    out_path: Path,
) -> dict:
    schemas = _load_json(schemas_path)["templates"]
    schema_by_id = {s["template_id"]: s for s in schemas}

    filled_data = _load_json(filled_path)
    # Support both list and dict formats for filled_indicators
    if isinstance(filled_data, list):
        filled_by_id = {e["template_id"]: e for e in filled_data}
    else:
        filled_by_id = filled_data

    full_template = _load_json(full_template_path)
    if isinstance(full_template, dict) and "templates" in full_template:
        full_template = full_template["templates"]

    tier_map = _build_tier_to_factor_key_map(full_template)
    full_by_key = {e["factor_key"]: e for e in full_template if "factor_key" in e}

    merged = []
    stats = {"matched": 0, "no_fill": 0, "no_tier": 0, "passthrough": 0}

    for entry in full_template:
        factor_id = entry.get("factor_id")
        factor_level = entry.get("factor_level", "present")
        factor_key = entry.get("factor_key")

        fill = filled_by_id.get(factor_id)
        if fill is None:
            # No indicator fill for this factor — pass through as-is
            merged.append(dict(entry))
            stats["passthrough"] += 1
            continue

        # Indicator was filled — determine which tier the LLM selected
        filled_tier = fill.get("tier_id") or fill.get("factor_level")
        filled_exists = fill.get("exists", "unknown")
        evidence_text = fill.get("evidence_text")
        source_file = fill.get("source_file")
        source_section = fill.get("source_section")
        source_page = fill.get("source_page")
        confidence = fill.get("confidence")
        raw_desc = fill.get("raw_field_description")

        # Determine exists state for this specific level
        if filled_exists in (False, "no", "absent"):
            level_exists = False
        elif filled_tier == factor_level:
            level_exists = True if filled_exists in (True, "yes", "present") else "unknown"
        elif filled_exists in (True, "yes", "present") and filled_tier is None:
            # LLM said present but didn't specify tier
            level_exists = "unknown"
        else:
            level_exists = False  # Different tier → this level not active

        new_entry = dict(entry)
        new_entry["exists"] = level_exists
        if evidence_text is not None:
            new_entry["evidence_text"] = evidence_text
        if source_file is not None:
            new_entry["source_file"] = source_file
        if source_section is not None:
            new_entry["source_section"] = source_section
        if source_page is not None:
            new_entry["source_page"] = source_page
        if confidence is not None:
            new_entry["confidence"] = confidence
        if raw_desc is not None:
            new_entry["raw_field_description"] = raw_desc

        merged.append(new_entry)
        if level_exists is True:
            stats["matched"] += 1
        else:
            stats["no_tier"] += 1

    _save_json(out_path, merged)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schemas", required=True, help="indicator_fill_schemas.json path")
    parser.add_argument("--filled", required=True, help="filled_indicators.json path (LLM output)")
    parser.add_argument("--full-template", required=True, help="risk_factor_assertion_template.json path")
    parser.add_argument("--output", required=True, help="Output merged template JSON path")
    args = parser.parse_args()

    stats = merge(
        schemas_path=Path(args.schemas),
        filled_path=Path(args.filled),
        full_template_path=Path(args.full_template),
        out_path=Path(args.output),
    )
    print(f"[merge_filled_template] matched={stats['matched']} no_tier={stats['no_tier']} "
          f"passthrough={stats['passthrough']}")


if __name__ == "__main__":
    main()

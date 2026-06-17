#!/usr/bin/env python3
"""Master-fill scaffold + gate for the structured_risk_factors timeline.

Replaces the per-file candidate.json flow. The orchestrator builds a
single read-only ``risk_factor_master.json`` and a scaffold
``structured_risk_factors_timeline.candidate.json`` with an empty
``records: []``. The agent walks (factor x source_md) once and appends
positive/negative evidence records to that single file. The gate then
validates substring locatability of ``evidence_text`` in the named
``source_md`` and rewrites ``exam_date="now"`` to the actual run date
for user_reported entries.

This module is intentionally narrow: it does not call any LLM and it
does not invent factors, log-odds, or probabilities. All numeric
metadata is copied from the orchestrator-prebuilt master.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TIMELINE_SCHEMA_VERSION = "risk-factors-timeline-v1"
MASTER_SCHEMA_VERSION = "risk-factor-master-v1"


def build_master_from_assertion_template(
    assertion_template: dict[str, Any],
    derived_assertions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project the assertion template into a slim master (one row per factor_key).

    standardized_prob aggregates evidence_store-derived log_odds/OR/LR
    values so the agent can reference them without touching the math.
    """
    rows: list[dict[str, Any]] = []
    template_records = assertion_template.get("risk_factor_templates") or assertion_template.get("assertion_templates") or []
    derived_by_factor_level: dict[tuple[str, str], list[dict[str, Any]]] = {}
    if derived_assertions:
        for d in derived_assertions.get("derived_assertions", []):
            fid = d.get("factor_id")
            lvl = d.get("factor_level")
            if fid and lvl:
                derived_by_factor_level.setdefault((fid, lvl), []).append(d)

    for record in template_records:
        factor_id = record.get("factor_id")
        factor_level = record.get("factor_level")
        if not factor_id:
            continue
        factor_key = record.get("factor_key") or f"{factor_id}|{factor_level or 'unknown'}"
        risk_evidence = record.get("risk_evidence_values") or []
        factor_type = record.get("factor_type")

        if factor_type == "imaging_finding":
            ppv_ev = risk_evidence[0] if risk_evidence else {}
            rows.append({
                "factor_key": factor_key,
                "factor_id": factor_id,
                "factor_name": record.get("factor_name"),
                "factor_level": factor_level,
                "factor_type": factor_type,
                "applicable_sex": record.get("applicable_sex", "all"),
                "synonyms": record.get("synonyms", []),
                "expected_evidence": record.get("expected_evidence", []),
                "standardized_prob": {
                    "primary_effect_type": "ppv",
                    "ppv_point_used": ppv_ev.get("ppv_point_used"),
                    "malignancy_ppv_range": ppv_ev.get("malignancy_ppv_range"),
                    "next_step": ppv_ev.get("next_step"),
                    "cancer_id": ppv_ev.get("cancer_id"),
                    "source_id": ppv_ev.get("source_id"),
                    "risk_evidence_values": risk_evidence,
                },
            })
            continue

        primary = next((r for r in risk_evidence if r.get("conversion_status") == "usable"), None)
        applies_to = sorted({
            d.get("cancer_id")
            for d in derived_by_factor_level.get((factor_id, factor_level or "unknown"), [])
            if d.get("cancer_id")
        })
        rows.append({
            "factor_key": factor_key,
            "factor_id": factor_id,
            "factor_name": record.get("factor_name"),
            "factor_level": factor_level,
            "factor_type": factor_type,
            "applicable_sex": record.get("applicable_sex", "all"),
            "synonyms": record.get("synonyms", []),
            "expected_evidence": record.get("expected_evidence", []),
            "standardized_prob": {
                "primary_effect_type": (primary or {}).get("effect_type"),
                "primary_log_odds_delta": (primary or {}).get("log_odds_delta"),
                "primary_calculation_value": (primary or {}).get("calculation_value"),
                "approximation": bool((primary or {}).get("approximation", False)),
                "applies_to_cancer_ids": applies_to,
                "risk_evidence_values": risk_evidence,
            },
        })

    return {
        "schema_version": MASTER_SCHEMA_VERSION,
        "evidence_version": assertion_template.get("evidence_version"),
        "filters": assertion_template.get("filters", {}),
        "factors": rows,
        "counts": {"factors": len(rows)},
    }


@dataclass(frozen=True)
class SourceMdEntry:
    data_id: str
    refined_md_path: str
    exam_date: str | None
    source_path: str | None = None


def build_imaging_findings_scaffold(
    *,
    source_md_files: list[SourceMdEntry],
    imaging_ontology: dict[str, Any] | None = None,
    run_date: str | None = None,
) -> dict[str, Any]:
    """Return an empty imaging_findings candidate ready for the agent.

    Parallel to timeline scaffold but holds per-lesion PPV findings
    (BI-RADS / TI-RADS / Fleischner) — NOT additive with OR risk
    factors. The snapshot stage uses max(bayes_posterior, max_ppv_midpoint).
    """
    valid_finding_ids: list[str] = []
    if isinstance(imaging_ontology, dict):
        valid_finding_ids = sorted(
            f["finding_id"] for f in imaging_ontology.get("findings", [])
            if f.get("finding_id")
        )
    return {
        "schema_version": "imaging-findings-v1",
        "agent_directive": (
            "🛑 STOP. ONLY use finding_id values from valid_finding_ids "
            "below — closed vocabulary from "
            "evidence_store/ontology/imaging_findings.json. These are "
            "PPV-based (probability the SPECIFIC LESION is malignant), "
            "NOT OR-based risk factors. Snapshot uses "
            "max(bayes_posterior, max_ppv_midpoint) per cancer; you do NOT need "
            "to compute anything — just record each lesion with its "
            "exam_date and source evidence text. Findings without a "
            "matching imaging_finding_id stay out of this file and "
            "appear in the LLM-driven '证据库外异常提示' instead. "
            "BEFORE re-invoking the orchestrator, run "
            "`python cancerrisk-skill/scripts/validate_imaging_findings.py "
            "--candidate <this-file>` to catch mistakes locally. "
            "See SKILL.md 'Imaging findings recipe (Agent Checkpoint 3.5)'."
        ),
        "run_date": run_date,
        "valid_finding_ids": valid_finding_ids,
        "valid_finding_ids_count": len(valid_finding_ids),
        "source_md_files": [
            {
                "data_id": entry.data_id,
                "refined_md_path": entry.refined_md_path,
                "exam_date": entry.exam_date,
                "source_path": entry.source_path,
            }
            for entry in source_md_files
        ],
        "findings": [],
    }


def gate_imaging_findings(
    *,
    imaging_ontology: dict[str, Any],
    candidate: dict[str, Any],
    run_date: str,
) -> dict[str, Any]:
    """Validate every imaging finding the agent wrote and emit the gated set.

    Rules (mirror gate_timeline_candidate):
      * finding_id must exist in imaging_ontology.
      * source_md must be one of candidate.source_md_files.
      * evidence_text must substring-locate in the named source_md.
      * exam_date "now" rewritten to run_date.
    """
    findings_by_id = {f["finding_id"]: f for f in imaging_ontology.get("findings", [])}
    md_by_path = {entry["refined_md_path"]: entry for entry in candidate.get("source_md_files", []) if entry.get("refined_md_path")}
    md_by_data_id = _md_lookup_by_data_id(candidate.get("source_md_files", []))
    md_text = _md_text_cache(candidate.get("source_md_files", []))

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for raw in candidate.get("findings", []):
        fid = raw.get("finding_id")
        finding = findings_by_id.get(fid)
        if not finding:
            rejected.append(dict(raw, reject_reason="unknown_finding_id"))
            continue
        source_md = raw.get("source_md")
        md_entry = md_by_path.get(source_md)
        if md_entry is None:
            source_data_id = raw.get("source_data_id")
            md_entry = md_by_data_id.get(source_data_id) if source_data_id else None
            if md_entry is None:
                rejected.append(dict(raw, reject_reason="source_md_not_in_manifest"))
                continue
            source_md = md_entry["refined_md_path"]
        evidence = raw.get("evidence_text") or ""
        haystack = md_text.get(source_md, "")
        if not evidence:
            rejected.append(dict(raw, reject_reason="missing_evidence_text"))
            continue
        if evidence not in haystack:
            rejected.append(dict(raw, reject_reason="evidence_text_not_found"))
            continue
        exam_date = raw.get("exam_date") or md_entry.get("exam_date") or run_date
        if exam_date == "now":
            exam_date = run_date
        ppv_low, ppv_high = finding["malignancy_ppv_range"]
        accepted.append({
            "finding_id": fid,
            "finding_name_zh": finding.get("finding_name_zh"),
            "cancer_id": finding["cancer_id"],
            "malignancy_ppv_range": finding["malignancy_ppv_range"],
            "ppv_point_used": (float(ppv_low) + float(ppv_high)) / 2,
            "source_id": finding.get("source_id"),
            "next_step": finding.get("next_step"),
            "exam_date": exam_date,
            "source_md": source_md,
            "source_data_id": md_entry.get("data_id"),
            "evidence_text": evidence,
        })
    return {
        "schema_version": "imaging-findings-gated-v1",
        "run_date": run_date,
        "findings": accepted,
        "rejected_findings": rejected,
    }


def build_tumor_markers_scaffold(
    *,
    source_md_files: list[SourceMdEntry],
    detection_derived: dict[str, Any] | None = None,
    run_date: str | None = None,
) -> dict[str, Any]:
    """v6: parallel to imaging_findings scaffold but holds serum/urine
    tumor marker test results (AFP, CEA, CA19-9, CA125, t-PSA, ...).

    Each entry the agent writes is a per-(test_id × evidence) record:
    test_id matches detection_performance entries; result is
    "positive" or "negative"; snapshot_risk._screening_contribution
    converts to log-odds via LR+/LR-.

    The agent picks test_id from valid_test_ids (closed allowlist
    derived from detection_performance_derived.json, excluding the
    multi-cancer liquid biopsy 吉早安 which has its own positive-result
    semantics requiring top_cancers).
    """
    valid_test_ids: list[str] = []
    if isinstance(detection_derived, dict):
        ids = sorted({
            d["test_id"] for d in detection_derived.get("derived_detection_performance", [])
            if d.get("test_id") and d["test_id"] != "jizaoan_multi_cancer_screening"
        })
        valid_test_ids = ids
    return {
        "schema_version": "tumor-markers-v1",
        "agent_directive": (
            "🛑 STOP. ONLY use test_id values from valid_test_ids below "
            "(closed allowlist derived from "
            "evidence_store/assertions/detection_performance.json). Each "
            "record is a per-test result. The refine recipe was updated "
            "to KEEP tumor marker rows even when normal (e.g. 'AFP 3.32 "
            "(0-20)') — they translate to a NEGATIVE screening test "
            "result, contributing a downward LR- log-odds delta to the "
            "matched cancer(s). Above-threshold (e.g. 'AFP 25 ↑') = "
            "POSITIVE result, contributing an upward LR+. "
            "Do NOT invent test_id values, OR/sens/spec/LR — those come "
            "from evidence_store. "
            "BEFORE re-invoking the orchestrator, run "
            "`python cancerrisk-skill/scripts/validate_tumor_markers.py "
            "--candidate <this-file>` to catch mistakes locally. "
            "See SKILL.md 'Tumor markers recipe (Agent Checkpoint 3.6)'."
        ),
        "run_date": run_date,
        "valid_test_ids": valid_test_ids,
        "valid_test_ids_count": len(valid_test_ids),
        "source_md_files": [
            {
                "data_id": entry.data_id,
                "refined_md_path": entry.refined_md_path,
                "exam_date": entry.exam_date,
                "source_path": entry.source_path,
            }
            for entry in source_md_files
        ],
        "tests": [],
    }


def assert_source_md_paths_safe(candidate: dict[str, Any], allowed_root: Path) -> None:
    """Reject candidate source markdown paths outside the analysis artifacts."""
    root = allowed_root.resolve()
    for entry in candidate.get("source_md_files", []):
        if not isinstance(entry, dict):
            import warnings
            warnings.warn(
                f"source_md_files entry is not a dict (got {type(entry).__name__}); skipping path check",
                stacklevel=2,
            )
            continue
        raw_path = entry.get("refined_md_path")
        if not raw_path:
            continue
        path = Path(raw_path).resolve()
        if path != root and root not in path.parents:
            raise ValueError(f"source_md path outside allowed root: {raw_path}")


def gate_tumor_markers(
    *,
    detection_derived: dict[str, Any],
    candidate: dict[str, Any],
    run_date: str,
    source_md_root: Path | None = None,
) -> dict[str, Any]:
    """Validate every tumor marker record the agent wrote and emit
    the gated set ready for merge into screening_tests.
    """
    if source_md_root is not None:
        assert_source_md_paths_safe(candidate, source_md_root)
    valid_test_ids = {
        d["test_id"] for d in detection_derived.get("derived_detection_performance", [])
        if d.get("test_id") and d["test_id"] != "jizaoan_multi_cancer_screening"
    }
    md_by_path = {entry["refined_md_path"]: entry for entry in candidate.get("source_md_files", []) if entry.get("refined_md_path")}
    md_by_data_id = _md_lookup_by_data_id(candidate.get("source_md_files", []))
    md_text = _md_text_cache(candidate.get("source_md_files", []))

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for raw in candidate.get("tests", []):
        tid = raw.get("test_id")
        if tid not in valid_test_ids:
            rejected.append(dict(raw, reject_reason="unknown_test_id"))
            continue
        result = raw.get("result")
        if result not in ("positive", "negative"):
            rejected.append(dict(raw, reject_reason="invalid_result_must_be_positive_or_negative"))
            continue
        source_md = raw.get("source_md")
        md_entry = md_by_path.get(source_md)
        if md_entry is None:
            sdid = raw.get("source_data_id")
            md_entry = md_by_data_id.get(sdid) if sdid else None
            if md_entry is None:
                rejected.append(dict(raw, reject_reason="source_md_not_in_manifest"))
                continue
            source_md = md_entry["refined_md_path"]
        evidence = raw.get("evidence_text") or ""
        haystack = md_text.get(source_md, "")
        if not evidence:
            rejected.append(dict(raw, reject_reason="missing_evidence_text"))
            continue
        if evidence not in haystack:
            rejected.append(dict(raw, reject_reason="evidence_text_not_found"))
            continue
        exam_date = raw.get("exam_date") or md_entry.get("exam_date") or run_date
        if exam_date == "now":
            exam_date = run_date
        accepted.append({
            "test_id": tid,
            "test_name": raw.get("test_name") or tid,
            "result": result,
            "exam_date": exam_date,
            "source_md": source_md,
            "source_data_id": md_entry.get("data_id"),
            "evidence_text": evidence,
        })
    return {
        "schema_version": "tumor-markers-gated-v1",
        "run_date": run_date,
        "tests": accepted,
        "rejected_tests": rejected,
    }


def build_cp3_verify_prompt(
    *,
    candidate: dict[str, Any],
    master: dict[str, Any],
) -> str:
    """Generate the CP3.1 verification agent instructions.

    Reads the filled candidate and returns a structured prompt string
    the orchestrator prints so the verification agent knows exactly
    what has been recorded and what to audit.
    """
    records = candidate.get("records", [])
    source_md_files = candidate.get("source_md_files", [])

    recorded_lines: list[str] = []
    for r in records:
        exists_marker = "✓" if r.get("exists") else "✗"
        recorded_lines.append(
            f"  {exists_marker} {r.get('factor_key')}  [{r.get('exam_date', '?')}]  "
            f"data_id={r.get('source_data_id', '?')}"
        )
    recorded_block = "\n".join(recorded_lines) if recorded_lines else "  （无记录）"

    source_lines: list[str] = []
    for entry in source_md_files:
        source_lines.append(
            f"  - {entry.get('refined_md_path')}  "
            f"(data_id={entry.get('data_id', '?')}, "
            f"exam_date={entry.get('exam_date', '?')})"
        )
    source_block = "\n".join(source_lines) if source_lines else "  （无文档）"

    master_path = "cancerrisk-skill/scripts/risk_factor_master.json"
    candidate_path = "structured_risk_factors_timeline.candidate.json"

    return (
        "\n"
        "╔══════════════════════════════════════════════════════════════╗\n"
        "║          CP3.1 验证审计 — 代理强制执行步骤                    ║\n"
        "╚══════════════════════════════════════════════════════════════╝\n"
        "\n"
        "【已录入记录】\n"
        f"{recorded_block}\n"
        "\n"
        "【待审核文档】\n"
        f"{source_block}\n"
        "\n"
        "【审计任务】\n"
        "1. 使用 Read 工具逐份读取上述每个 refined.md 全文。\n"
        "2. 以独立审计者视角识别文档中所有临床异常发现（不受已录入记录影响）。\n"
        "3. 对每个异常发现，检查是否已在「已录入记录」中有对应条目：\n"
        "   - 已录入且 exists=true → 正常，继续。\n"
        "   - 未录入或仅录入 exists=false → 疑似遗漏，进入步骤 4。\n"
        f"4. 对每个疑似遗漏，查 {master_path} 的 factors 列表，\n"
        "   找匹配的 factor_key（按 factor_name / synonyms / factor_id 对照）。\n"
        "5. 判断是否补录：\n"
        "   - 找到匹配 factor_key 且文档明确支持 → 补录 exists=true 记录。\n"
        "   - 文档明确否认（如「未见异常」）→ 补录 exists=false 记录。\n"
        "   - 无匹配 factor_key → 不补录，记入「证据库外异常」（snapshot 自动展示）。\n"
        f"6. 补录操作：直接编辑 artifacts/{candidate_path} 的 records 数组，\n"
        "   格式与现有记录相同，evidence_text 必须是 refined.md 的精确子串。\n"
        "7. 补录完成后（或确认无遗漏后），重新运行编排器继续后续阶段。\n"
        "\n"
        "⚠️  本步骤为强制步骤，不可跳过。即使认为 CP3 填写完整，\n"
        "   也必须完成独立审计后方可继续。\n"
    )


def build_timeline_scaffold(
    *,
    source_md_files: list[SourceMdEntry],
    evidence_version: str | None = None,
    run_date: str | None = None,
    master: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return an empty timeline candidate ready for the agent to fill.

    Embeds the master vocabulary as ``valid_factor_keys`` (closed
    allowlist) and a hard agent_directive so the agent CANNOT invent
    factor_keys like ``thyroid_nodule|present`` or ``alt_elevated|present``
    that don't exist in the ontology — those will be rejected by the
    gate as ``unknown_factor_key`` anyway, but the inline list lets the
    agent self-check before re-invoking the orchestrator.
    """
    valid_factor_keys: list[str] = []
    if isinstance(master, dict):
        valid_factor_keys = sorted(
            f["factor_key"] for f in master.get("factors", [])
            if f.get("factor_key")
        )
    return {
        "schema_version": TIMELINE_SCHEMA_VERSION,
        "agent_directive": (
            "🛑 STOP. ONLY use factor_key values from valid_factor_keys "
            "below — this is a CLOSED vocabulary derived from "
            "evidence_store/ontology/risk_factors.json AND "
            "evidence_store/ontology/imaging_findings.json. Inventing new "
            "keys (e.g. 'thyroid_nodule|present', 'alt_elevated|present', "
            "'prostate_calcification|present', 'hyperthyroidism|present') "
            "will be rejected by risk_factor_gate as "
            "'unknown_factor_key' and silently drop your record from "
            "snapshot/longitudinal/archive. "
            "v7 NOTE: Imaging findings (BI-RADS, TI-RADS, Fleischner) are "
            "now part of the SAME timeline file. Use the imaging factor_keys "
            "(e.g. 'breast_birads_4c|present') exactly like risk factors — "
            "set exists=true and provide evidence_text. They will be "
            "tracked in the personal health archive and longitudinal "
            "analysis alongside OR-based risk factors. "
            "If a clinical finding in the report has no matching "
            "master factor_key, do NOT force-fit it — those go into the "
            "'证据库外异常提示' section of snapshot_risk.html automatically. "
            "BEFORE re-invoking the orchestrator, run "
            "`python cancerrisk-skill/scripts/validate_timeline_candidate.py "
            "--candidate <this-file>` to catch mistakes locally. "
            "See SKILL.md 'Master fill recipe (Agent Checkpoint 3)'."
        ),
        "evidence_version": evidence_version,
        "run_date": run_date,
        "valid_factor_keys": valid_factor_keys,
        "valid_factor_keys_count": len(valid_factor_keys),
        "source_md_files": [
            {
                "data_id": entry.data_id,
                "refined_md_path": entry.refined_md_path,
                "exam_date": entry.exam_date,
                "source_path": entry.source_path,
            }
            for entry in source_md_files
        ],
        "records": [],
        "_agent_instructions": {
            "this_file_purpose": "CP3 extracted evidence ONLY — factors observed in the physical report. NOT for questionnaire/interview answers.",
            "imaging_findings": "Imaging findings (BI-RADS, TI-RADS, Fleischner, etc.) are report-extracted evidence and belong HERE in the candidate. You MUST provide source_data_id pointing to the mineru data_id. They do NOT go in user_reported.json.",
            "questionnaire_answers_go_here": "structured_risk_factors_timeline.user_reported.json (smoking, alcohol, family history, jizaoan, screening history — NOT imaging findings)",
            "reject_reason_if_violated": "interactive_factor_in_candidate for non-imaging factors; imaging_finding_requires_source_data_id for imaging findings missing source_data_id.",
        },
    }


def _md_text_cache(source_md_files: list[dict[str, Any]]) -> dict[str, str]:
    cache: dict[str, str] = {}
    for entry in source_md_files:
        path = entry.get("refined_md_path")
        if not path:
            continue
        p = Path(path)
        cache[path] = p.read_text(encoding="utf-8") if p.is_file() else ""
    return cache


def _md_lookup_by_data_id(source_md_files: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {entry["data_id"]: entry for entry in source_md_files if entry.get("data_id")}


def gate_timeline_candidate(
    *,
    master: dict[str, Any],
    candidate: dict[str, Any],
    run_date: str,
    user_reported_payload: dict[str, Any] | None = None,
    source_md_root: Path | None = None,
) -> dict[str, Any]:
    """Validate every record in candidate.records and emit the final timeline.

    Rules:
      * factor_key must exist in master.factors.
      * exists must be a bool (true|false).
      * source_md must be one of candidate.source_md_files (matched by path).
      * evidence_text must substring-locate in the named source_md (skipped
        for source="user_reported").
      * exam_date "now" is rewritten to run_date.
      * Records flagged ``source="user_reported"`` carry confidence 1.0 by
        contract; they are merged in from ``user_reported_payload`` rather
        than from the agent-filled candidate (the candidate file is for
        extracted evidence only).
    """
    factors_by_key = {f["factor_key"]: f for f in master.get("factors", [])}
    if source_md_root is not None:
        assert_source_md_paths_safe(candidate, source_md_root)
    md_by_path = {entry["refined_md_path"]: entry for entry in candidate.get("source_md_files", []) if entry.get("refined_md_path")}
    md_by_data_id = _md_lookup_by_data_id(candidate.get("source_md_files", []))
    md_text = _md_text_cache(candidate.get("source_md_files", []))

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for raw in candidate.get("records", []):
        key = raw.get("factor_key")
        factor = factors_by_key.get(key)
        if not factor:
            rejected.append(dict(raw, reject_reason="unknown_factor_key"))
            continue
        if not isinstance(raw.get("exists"), bool):
            rejected.append(dict(raw, reject_reason="exists_not_boolean"))
            continue
        source_md = raw.get("source_md")
        md_entry = md_by_path.get(source_md)
        if md_entry is None:
            source_data_id = raw.get("source_data_id")
            md_entry = md_by_data_id.get(source_data_id) if source_data_id else None
            if md_entry is None:
                if not source_md and not source_data_id:
                    # Both fields absent — distinguish imaging findings from questionnaire answers
                    is_imaging = factor.get("factor_type") == "imaging_finding"
                    if is_imaging:
                        rejected.append(dict(raw,
                                             reject_reason="imaging_finding_requires_source_data_id",
                                             agent_hint="Imaging findings (BI-RADS, TI-RADS, Fleischner, etc.) are report-extracted evidence and belong in the CP3 candidate, but MUST provide source_data_id pointing to the mineru data_id. They are NOT questionnaire answers and do NOT go in user_reported.json."))
                    else:
                        rejected.append(dict(raw,
                                             reject_reason="interactive_factor_in_candidate",
                                             agent_hint="Place this factor in structured_risk_factors_timeline.user_reported.json, not the CP3 candidate. The CP3 file is for report-extracted evidence only."))
                else:
                    rejected.append(dict(raw, reject_reason="source_md_not_in_manifest"))
                continue
            source_md = md_entry["refined_md_path"]
        evidence = raw.get("evidence_text") or ""
        haystack = md_text.get(source_md, "")
        if not evidence:
            rejected.append(dict(raw, reject_reason="missing_evidence_text"))
            continue
        if evidence not in haystack:
            # Fallback: strip common markdown bold/italic markers from both sides
            stripped_evidence = re.sub(r"[*_]+", "", evidence)
            stripped_haystack = re.sub(r"[*_]+", "", haystack)
            if stripped_evidence not in stripped_haystack:
                rejected.append(dict(raw, reject_reason="evidence_text_not_found"))
                continue
        exam_date = raw.get("exam_date") or md_entry.get("exam_date") or run_date
        if exam_date == "now":
            exam_date = run_date
        base_record = {
            "factor_key": key,
            "factor_id": factor["factor_id"],
            "factor_level": factor.get("factor_level"),
            "factor_name": factor.get("factor_name"),
            "factor_type": factor.get("factor_type"),
            "exists": raw["exists"],
            "exam_date": exam_date,
            "source_md": source_md,
            "source_data_id": md_entry.get("data_id"),
            "source": "extracted",
            "evidence_text": evidence,
            "confidence": float(raw["confidence"]) if raw.get("confidence") is not None else 0.85,
            "measurement_value": raw.get("measurement_value"),
            "measurement_unit": raw.get("measurement_unit"),
        }
        if factor.get("factor_type") == "imaging_finding":
            sp = factor.get("standardized_prob", {})
            base_record.update({
                "cancer_id": sp.get("cancer_id"),
                "malignancy_ppv_range": sp.get("malignancy_ppv_range"),
                "ppv_point_used": sp.get("ppv_point_used"),
                "next_step": sp.get("next_step"),
                "source_id": sp.get("source_id"),
                "finding_name_zh": factor.get("factor_name"),
            })
        accepted.append(base_record)

    user_records: list[dict[str, Any]] = []
    if user_reported_payload:
        for raw in user_reported_payload.get("records", []):
            key = raw.get("factor_key")
            factor = factors_by_key.get(key)
            if not factor:
                rejected.append(dict(raw, reject_reason="unknown_factor_key_user_reported"))
                continue
            if not isinstance(raw.get("exists"), bool):
                rejected.append(dict(raw, reject_reason="exists_not_boolean_user_reported"))
                continue
            exam_date = raw.get("exam_date") or run_date
            if exam_date == "now":
                exam_date = run_date
            base_record = {
                "factor_key": key,
                "factor_id": factor["factor_id"],
                "factor_level": factor.get("factor_level"),
                "factor_name": factor.get("factor_name"),
                "factor_type": factor.get("factor_type"),
                "exists": raw["exists"],
                "exam_date": exam_date,
                "source_md": raw.get("source_md"),
                "source_data_id": raw.get("source_data_id") or "interactive_answers",
                "source": "user_reported",
                "evidence_text": raw.get("evidence_text"),
                "confidence": 1.0,
            }
            if factor.get("factor_type") == "imaging_finding":
                sp = factor.get("standardized_prob", {})
                base_record.update({
                    "cancer_id": sp.get("cancer_id"),
                    "malignancy_ppv_range": sp.get("malignancy_ppv_range"),
                    "ppv_point_used": sp.get("ppv_point_used"),
                    "next_step": sp.get("next_step"),
                    "source_id": sp.get("source_id"),
                    "finding_name_zh": factor.get("factor_name"),
                })
            user_records.append(base_record)

    records = accepted + user_records
    return {
        "schema_version": TIMELINE_SCHEMA_VERSION,
        "evidence_version": candidate.get("evidence_version") or master.get("evidence_version"),
        "run_date": run_date,
        "source_md_files": candidate.get("source_md_files", []),
        "records": records,
        "rejected_records": rejected,
    }


def is_candidate_filled(candidate: dict[str, Any]) -> bool:
    """A candidate is considered 'filled' if the agent wrote at least one record.

    Empty timelines are also legal when there genuinely is no abnormal
    finding, but we still log a loud warning at the orchestrator level so
    the operator can sanity-check.
    """
    records = candidate.get("records")
    return isinstance(records, list) and len(records) > 0


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def timeline_to_factor_events(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    """Adapter: project timeline records into the legacy ``factor_events`` shape
    consumed by merge_risk_factors / archive_manager / snapshot_risk.

    The legacy shape uses ``factor_key`` as dedup key and expects the
    ``source`` label "report_llm_fill" / "interactive_completion". We map:
        source=extracted     → report_llm_fill
        source=user_reported → interactive_completion
    """
    events: list[dict[str, Any]] = []
    for record in timeline.get("records", []):
        legacy_source = "interactive_completion" if record.get("source") == "user_reported" else "report_llm_fill"
        events.append({
            "factor_key": record["factor_key"],
            "factor_id": record["factor_id"],
            "factor_level": record.get("factor_level"),
            "factor_name": record.get("factor_name"),
            "factor_type": record.get("factor_type"),
            "exists": record["exists"],
            "exam_date": record["exam_date"],
            "evidence_text": record.get("evidence_text"),
            "source": legacy_source,
            "source_data_id": record.get("source_data_id"),
            "confidence": record.get("confidence", 0.0),
            "measurement_value": record.get("measurement_value"),
            "measurement_unit": record.get("measurement_unit"),
            "cancer_id": record.get("cancer_id"),
            "ppv_point_used": record.get("ppv_point_used"),
            "malignancy_ppv_range": record.get("malignancy_ppv_range"),
            "finding_name_zh": record.get("finding_name_zh"),
            "source_id": record.get("source_id"),
            "next_step": record.get("next_step"),
        })
    return events

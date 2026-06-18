#!/usr/bin/env python3
"""P1 report.json assembler.

PURE remapper: read the internal JSON artifacts written by earlier
deterministic stages and produce one combined ``<out>/artifacts/report.json``.

NO math, NO LLM calls, NO network — just read + remap + atomic write.

Source artifacts (all under ``<out>/artifacts/`` unless noted):
- ``snapshot_risk.json``  — ``cancers`` / ``section4_screening`` /
  ``uncertainties_summary`` / ``person_context`` (sex, age).
- ``voi_ranking.json``    — ``top_recommendation`` / ``rankings`` /
  ``total_methods_evaluated``.
- ``health_summary_structured_summary.json`` — ``status`` /
  ``abnormal_non_cancer_count`` / ``items`` (degrade if absent).
- ``tumor_markers.json`` (a dict with a ``tests`` list) with fallback to
  ``tumor_markers.candidate.json``; both absent → ``[]``.
- ``answers.json`` (``{"answers": {...}}`` or bare dict) — ``q_jizaoan_result``
  and (when positive) ``q_jizaoan_top1`` / ``q_jizaoan_top2``.

``brca_status`` is derived from ``q_genetic_mutations_brca`` (positive when the
multi-select contains brca1/brca2). ``liquid_biopsy_perf.specificity`` is
back-filled from the Jizaoan row in ``voi_ranking.json`` (constant 0.990 →
99.0%) when the LLM artifact omits it — a deterministic numeric fallback, the
number is never invented (PUA: values come from script/data, not the model).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "report-v1"

# CP4 health-summary HTML blocks (assessment_result.*) carried verbatim into
# report.json for the full-fidelity template (P2). API-sourced, rendered | safe.
_CP4_BLOCK_KEYS = (
    "risk_level", "core_risk_factors", "overall_assessment",
    "abnormal_table", "disease_cards", "advice_list", "conclusion_table",
)


def _read_json(path: Path | None, default: Any) -> Any:
    """Read a JSON file, returning ``default`` on any read/parse failure."""
    if path is None or not Path(path).is_file():
        return default
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _unwrap_answers(raw: Any) -> dict[str, Any]:
    """Accept ``{"answers": {...}}`` or a bare dict; return the answers dict."""
    if not isinstance(raw, dict):
        return {}
    inner = raw.get("answers")
    if isinstance(inner, dict):
        return inner
    return raw


def _tumor_markers_list(artifacts: Path) -> list[Any]:
    """Prefer ``tumor_markers.json``, fall back to candidate, else ``[]``.

    Both files are dicts with a ``tests`` key; tolerate a bare list too.
    """
    for name in ("tumor_markers.json", "tumor_markers.candidate.json"):
        data = _read_json(artifacts / name, None)
        if data is None:
            continue
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("tests", [])
    return []


def _jizaoan(answers: dict[str, Any]) -> tuple[str, list[str]]:
    result = answers.get("q_jizaoan_result", "unknown")
    top_cancers: list[str] = []
    if result == "positive":
        for qid in ("q_jizaoan_top1", "q_jizaoan_top2"):
            value = answers.get(qid)
            if value and value != "unknown":
                top_cancers.append(value)
    return result, top_cancers


def _brca_status(answers: dict[str, Any]) -> str:
    """BRCA 阳性 = q_genetic_mutations_brca 含 brca1/brca2（multi_select）。"""
    v = answers.get("q_genetic_mutations_brca")
    if isinstance(v, list) and ("brca1" in v or "brca2" in v):
        return "positive"
    if isinstance(v, str) and v in ("brca1", "brca2"):
        return "positive"
    return "unknown"


def _brca_detail(answers: dict[str, Any], brca_status: str) -> str:
    """BRCA 遗传风险详情文案。问卷无 q_brca_detail 悬空题，brca 阳性时从
    q_genetic_mutations_brca 的值(brca1/brca2)合成默认详情，避免遗传风险标签空白。"""
    if brca_status != "positive":
        return ""
    v = answers.get("q_genetic_mutations_brca")
    if isinstance(v, list):
        genes = [g.upper() for g in v if g in ("brca1", "brca2")]
    elif isinstance(v, str) and v in ("brca1", "brca2"):
        genes = [v.upper()]
    else:
        genes = []
    label = "/".join(genes) if genes else "BRCA"
    return f"{label} 基因突变致病位点携带者"


def _checkup_window(snapshot: dict[str, Any], brca_status: str, health_risk_level: Any) -> str:
    """推荐体检时间窗。联动高危信号（与 timeline priority 档同源，避免报头窗口与首档矛盾）：
    BRCA阳性 / 任一癌症后验>1% / 健康总结评估较严重 → "1-2 周内"；否则按最高 risk_tier。"""
    cancers = snapshot.get("cancers", []) if isinstance(snapshot, dict) else []
    if brca_status == "positive":
        return "1-2 周内"
    if any(isinstance(c, dict) and (c.get("posterior_probability") or 0) > 0.01 for c in cancers):
        return "1-2 周内"
    rl = str(health_risk_level or "")
    if any(k in rl for k in ("严重", "🔴", "🟠", "高风险")):
        return "1-2 周内"
    # 否则按最高 risk_tier（含 imaging_tier）
    order = {"pathology_confirmed": 5, "very_high": 4, "urgent_workup": 4,
             "high": 3, "high_workup": 3, "medium": 2, "moderate_workup": 2, "low": 1}
    top = ""
    best = -1
    for c in cancers:
        t = c.get("risk_tier", "") if isinstance(c, dict) else ""
        if order.get(t, 0) > best:
            best = order.get(t, 0)
            top = t
    return ({"pathology_confirmed": "1-2 周内", "very_high": "1-2 周内", "urgent_workup": "1-2 周内",
             "high": "1 个月内", "high_workup": "1 个月内",
             "medium": "3 个月内", "moderate_workup": "3 个月内", "low": "6-12 个月内"}
            .get(top, "参照下方时间轴"))


# 癌种关键词 → cancer_id 映射（用于 x_addons risk_source 匹配贝叶斯后验）
_CANCER_KEYWORDS: dict[str, str] = {
    "甲状腺": "thyroid_cancer", "肺": "lung_cancer", "LDCT": "lung_cancer",
    "乳腺": "breast_cancer", "肠": "colorectal_cancer", "结肠": "colorectal_cancer",
    "直肠": "colorectal_cancer", "肝": "liver_cancer", "胃": "gastric_cancer",
    "食管": "esophageal_cancer", "胰腺": "pancreatic_cancer", "前列腺": "prostate_cancer",
    "宫颈": "cervical_cancer", "卵巢": "ovarian_cancer", "膀胱": "bladder_cancer",
    "肾": "kidney_cancer", "鼻咽": "head_neck_cancer", "头颈": "head_neck_cancer",
    "胆道": "biliary_tract_cancer",
}


def _enrich_x_addons(x_addons: list[Any], snapshot: dict[str, Any]) -> list[Any]:
    """给 x_addons 每行补充 cancer_name + posterior_probability（贝叶斯后验），
    通过 risk_source 文本匹配 snapshot.cancers 的 cancer_id。
    匹配规则：risk_source 含癌种关键词 → 对应 cancer_id → 查 cancers 后验。
    未匹配的行不受影响（保持原样，不展示概率）。"""
    if not isinstance(x_addons, list):
        return x_addons
    cancers = snapshot.get("cancers", []) if isinstance(snapshot, dict) else []
    cancer_map: dict[str, Any] = {}
    for c in cancers:
        if isinstance(c, dict) and c.get("posterior_probability"):
            cancer_map[c.get("cancer_id", "")] = c

    for x in x_addons:
        if not isinstance(x, dict):
            continue
        source = str(x.get("risk_source", ""))
        for keyword, cancer_id in _CANCER_KEYWORDS.items():
            if keyword in source and cancer_id in cancer_map:
                c = cancer_map[cancer_id]
                x["cancer_name"] = c.get("cancer_name_zh", c.get("cancer_id", ""))
                x["posterior_probability"] = c.get("posterior_probability")
                break
    return x_addons


def _format_cn_date(dt: datetime) -> str:
    """ISO datetime → 「YYYY年M月D日」供报告右上角展示。"""
    return f"{dt.year}年{dt.month}月{dt.day}日"


def _honorific_name(name: Any, sex: Any) -> str:
    """报告头部称谓：中文姓首字 + 先生/女士（对齐 temp「魏女士」格式）。
    非中文姓名或性别缺失 → 回退全名（避免「t先生」之类勉强拼接）。"""
    if not isinstance(name, str) or not name:
        return name if isinstance(name, str) else ""
    first = name[0]
    if "一" <= first <= "鿿":  # 首字为中文
        title = "先生" if sex == "male" else "女士" if sex == "female" else ""
        if title:
            return f"{first}{title}"
    return name


def _liquid_biopsy_perf(artifacts: Path, voi: dict[str, Any]) -> dict[str, Any]:
    """Liquid-biopsy performance panel for the 液体活检 section.

    Sensitivity / early-stage sensitivity / market price / clinical hints come
    from the LLM artifact ``liquid_biopsy_perf.json`` (LLM-authored from
    ``05-基于液体活检的多癌种联合筛查.md`` — the composite sens numbers are NOT in
    the per-cancer ``detection_performance.json``). ``specificity`` is
    deterministically back-filled from the Jizaoan row in ``voi_ranking.json``
    (constant 0.990 → "99.0%") when the LLM omits or malforms it, so the number
    is never invented.
    """
    perf = _read_json(artifacts / "liquid_biopsy_perf.json", {})
    if not isinstance(perf, dict):
        perf = {}
    result = {
        "sensitivity": perf.get("sensitivity", "-"),
        "specificity": perf.get("specificity", "-"),
        "market_price_range": perf.get("market_price_range", "-"),
        "clinical_hint": perf.get("clinical_hint", ""),
        "negative_risk_reduction": perf.get("negative_risk_reduction", ""),
    }
    # sens 与 spec 都从 voi_ranking 吉早安行兜底（脚本确定性，同源）。pricing MD
    # 自述其 74.9% 不该被引用为性能、05-MD 无综合 sens 数值——统一从 voi 取，
    # 消除 74.9% / 82.2% / 81.9% 多口径打架。
    for r in voi.get("rankings", []) if isinstance(voi, dict) else []:
        if not isinstance(r, dict):
            continue
        haystack = str(r.get("method", "")) + str(r.get("test_id", ""))
        if "jizaoan" in haystack.lower() or "吉早安" in haystack:
            sens = r.get("sensitivity")
            spec = r.get("specificity")
            if isinstance(sens, (int, float)) and 0 <= sens <= 1:
                result["sensitivity"] = f"{sens * 100:.1f}%"
            if isinstance(spec, (int, float)) and 0 <= spec <= 1:
                result["specificity"] = f"{spec * 100:.1f}%"
            break
    return result


def assemble_report_json(
    *,
    artifacts: Path,
    out: Path,
    answers_path: Path | None,
    person_id: str,
    run_id: str,
    evidence_version: Any,
) -> dict[str, Any]:
    """Assemble report.json from internal artifacts and atomically write it.

    Returns the assembled dict. Missing optional files degrade to sane empty
    defaults instead of raising.
    """
    artifacts = Path(artifacts)

    snapshot = _read_json(artifacts / "snapshot_risk.json", {})
    voi = _read_json(artifacts / "voi_ranking.json", {})
    health = _read_json(artifacts / "health_summary_structured_summary.json", {})
    answers = _unwrap_answers(_read_json(answers_path, {}))

    person_ctx = snapshot.get("person_context", {}) if isinstance(snapshot, dict) else {}
    jizaoan_result, jizaoan_top_cancers = _jizaoan(answers)
    brca_status = _brca_status(answers)

    patient = health.get("patient_data", {}) if isinstance(health, dict) else {}
    assessment = health.get("assessment_result", {}) if isinstance(health, dict) else {}
    person_name = patient.get("name") or person_id
    if person_name == "未提供":
        person_name = person_id

    now = datetime.now()
    report = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at": now.isoformat(),
        "generated_at_display": _format_cn_date(now),
        "person": {
            "person_id": person_id,
            "name": person_name,
            "honorific_name": _honorific_name(person_name, person_ctx.get("sex")),
            "sex": person_ctx.get("sex"),
            "age": person_ctx.get("age"),
        },
        "jizaoan_result": jizaoan_result,
        "jizaoan_top_cancers": jizaoan_top_cancers,
        "brca_status": brca_status,
        "brca_detail": _brca_detail(answers, brca_status),
        "checkup_window": _checkup_window(snapshot, brca_status, assessment.get("risk_level")),
        "timeline_tiers": _read_json(artifacts / "timeline_tiers.json", {"priority": [], "important": [], "maintain": []}),
        "x_addons": _enrich_x_addons(_read_json(artifacts / "x_addons.json", []), snapshot),
        "package_tiers": _read_json(artifacts / "package_tiers.json", []),
        "liquid_biopsy_perf": _liquid_biopsy_perf(artifacts, voi),
        "long_term_intervention": _read_json(artifacts / "long_term_intervention.json", {"genetic_management": [], "lifestyle": []}),
        "health_summary": {
            "status": health.get("status"),
            "abnormal_non_cancer_count": health.get("abnormal_non_cancer_count", 0),
            "items": health.get("items", []),
            "blocks": {k: assessment.get(k) for k in _CP4_BLOCK_KEYS},
        },
        "snapshot": {
            "cancers": snapshot.get("cancers", []),
            "section4_screening": snapshot.get("section4_screening", []),
            "uncertainties_summary": snapshot.get("uncertainties_summary", {}),
        },
        "voi": {
            "top_recommendation": voi.get("top_recommendation"),
            "rankings": voi.get("rankings", []),
            "total_methods_evaluated": voi.get("total_methods_evaluated", 0),
        },
        "tumor_markers": _tumor_markers_list(artifacts),
        "evidence_version": evidence_version,
    }

    _check_section_artifacts(report)
    _atomic_write_json(artifacts / "report.json", report)
    return report


def _check_section_artifacts(report: dict[str, Any]) -> None:
    """检测 5 section artifact 是否全空（agent 跳过 report-artifacts 步骤 → 空壳报告）。
    全空 → stderr 警告 + 标记 sections_incomplete=true，避免空壳静默交付。
    低危案例 artifact 内容少但非全空（如 package_tiers 恒 3 档），不会误报。"""
    tt = report.get("timeline_tiers") or {}
    timeline_empty = all(len(tt.get(k, [])) == 0 for k in ("priority", "important", "maintain"))
    x_empty = len(report.get("x_addons") or []) == 0
    pkg_empty = len(report.get("package_tiers") or []) == 0
    lti = report.get("long_term_intervention") or {}
    lti_empty = len(lti.get("genetic_management", [])) == 0 and len(lti.get("lifestyle", [])) == 0
    all_empty = timeline_empty and x_empty and pkg_empty and lti_empty
    report["sections_incomplete"] = all_empty
    if all_empty:
        print(
            "[report] ⚠️ 5 section artifact 全空（timeline_tiers/x_addons/package_tiers/"
            "long_term_intervention 均空）→ 报告核心 section 为空壳。"
            "请完成 SKILL.md Minimal Workflow 第10步（--stop-after report-artifacts）"
            "产出 5 JSON 后重跑，否则 report.html 无实质内容。",
            file=sys.stderr,
        )


def _atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    """Write JSON to a temp file in the same dir, then ``os.replace``."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)

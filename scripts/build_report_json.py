#!/usr/bin/env python3
"""P1 report.json assembler.

PURE remapper: read the internal JSON artifacts written by earlier
deterministic stages and produce one combined ``<out>/artifacts/report.json``.

NO math, NO LLM calls, NO network — just read + remap + atomic write.

Source artifacts (all under ``<out>/artifacts/`` unless noted):
- ``snapshot_risk.json``  — ``cancers`` / ``section4_screening`` /
  ``uncertainties_summary`` / ``person_context`` (sex, age).
- ``health_summary_structured_summary.json`` — ``status`` /
  ``abnormal_non_cancer_count`` / ``items`` (degrade if absent).
- ``tumor_markers.json`` (a dict with a ``tests`` list) with fallback to
  ``tumor_markers.candidate.json``; both absent → ``[]``.
- ``answers.json`` (``{"answers": {...}}`` or bare dict) — ``q_jizaoan_result``
  and (when positive) ``q_jizaoan_top1`` / ``q_jizaoan_top2``.

``brca_status`` is derived from ``q_genetic_mutations_brca`` (positive when the
multi-select contains brca1/brca2). Liquid-biopsy sensitivity/specificity are
back-filled from the authoritative overall Jizaoan row in
``detection_performance.json``.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "report-v1"
EVIDENCE_STORE_DEFAULT = (
    Path(__file__).resolve().parent.parent
    / "references" / "database" / "cancerrisk" / "json"
)

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


def _checkup_window(timeline_tiers: dict) -> str:
    """推荐体检时间窗：**从 LLM 产的 timeline 三档派生**（反映 LLM 的紧急度判断，不独立推荐）。
    v2.0.0 重构：不再用脚本阈值独立推荐窗口（那会抢 LLM 的推荐角色）——改为读 LLM 已分的
    三档：priority 非空→1-2 周内；important→1 个月内；maintain→3-6 个月内；空→参照下方时间轴。"""
    if timeline_tiers.get("priority"):
        return "1-2 周内"
    if timeline_tiers.get("important"):
        return "1 个月内"
    if timeline_tiers.get("maintain"):
        return "3-6 个月内"
    return "参照下方时间轴"


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


_TAG_NORMALIZE = {
    "danger": "danger", "high": "danger", "critical": "danger",
    "high_workup": "danger", "very_high": "danger", "严重": "danger", "🔴": "danger",
    "warning": "warning", "moderate": "warning", "moderate_workup": "warning",
    "medium": "warning", "mild_abnormal": "warning", "中": "warning", "🟠": "warning", "🟡": "warning",
    "info": "info", "low": "info", "mild": "info", "低": "info", "🟢": "info",
}

_TAG_LABEL_CN = {
    "danger": "高风险",
    "warning": "中风险",
    "info": "低风险",
}

_TAG_SORT_RANK = {
    "danger": 0,
    "warning": 1,
    "info": 2,
}

_TAG_LABEL_HINTS = (
    ("danger", ("高风险", "极高", "很高", "严重", "重度", "🔴")),
    ("warning", ("中等风险", "中风险", "中度", "较高", "潜在", "🟠", "🟡")),
    ("info", ("低风险", "轻度", "常规", "正常", "🟢")),
)

_INTERNAL_TIMELINE_TOKENS = (
    "moderate_workup",
    "high_workup",
    "urgent_workup",
    "very_high",
    "mild_abnormal",
)


def _infer_risk_tag_from_label(label: Any) -> str | None:
    """当 LLM 只填中文 risk_level_label 时，从展示标签反推 CSS 风险等级。"""
    text = str(label or "").strip()
    if not text:
        return None
    for tag, hints in _TAG_LABEL_HINTS:
        if any(hint in text for hint in hints):
            return tag
    return None


def _normalize_risk_tag(tag: Any, label: Any = None) -> str:
    """把 LLM 产的各种 risk_level_tag/中文 label 值映射到模板 CSS 认的 danger/warning/info。"""
    tag_text = str(tag or "").strip().lower()
    if tag_text in _TAG_NORMALIZE:
        return _TAG_NORMALIZE[tag_text]
    inferred = _infer_risk_tag_from_label(label)
    return inferred or "info"


def _normalize_risk_label(label: Any, tag: str) -> str:
    """模板展示用中文风险级别；英文/内部枚举标签统一转中文。"""
    text = str(label or "").strip()
    if not text or re.search(r"[A-Za-z_]", text):
        return _TAG_LABEL_CN.get(tag, "低风险")
    return text


def _sanitize_timeline_text(value: Any) -> Any:
    """清除面向内部调度/风险分层的枚举字段，避免在用户报告时间轴泄露。"""
    if not isinstance(value, str):
        return value
    text = value
    token_pattern = "|".join(re.escape(token) for token in _INTERNAL_TIMELINE_TOKENS)
    text = re.sub(
        rf"\b(?:risk_tier|risk_level|risk_level_tag|tier)\s*[:=：]\s*(?:{token_pattern})\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(rf"\b(?:{token_pattern})\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[（(]\s*[，,、;；\s]*[)）]", "", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([，,、;；。])", r"\1", text)
    return text.strip(" ，,、;；")


def _normalize_timeline_tiers(timeline_tiers: Any) -> dict[str, list[Any]]:
    """时间轴渲染数据归一化：保留用户可读医学内容，剔除内部枚举。"""
    if not isinstance(timeline_tiers, dict):
        return {"priority": [], "important": [], "maintain": []}
    normalized: dict[str, list[Any]] = {}
    for tier in ("priority", "important", "maintain"):
        rows = timeline_tiers.get(tier, [])
        if not isinstance(rows, list):
            normalized[tier] = []
            continue
        normalized_rows: list[Any] = []
        for row in rows:
            if isinstance(row, dict):
                new_row = dict(row)
                for field in ("item_name", "rationale", "interval"):
                    if field in new_row:
                        new_row[field] = _sanitize_timeline_text(new_row[field])
                normalized_rows.append(new_row)
            else:
                normalized_rows.append(_sanitize_timeline_text(row))
        normalized[tier] = normalized_rows
    return normalized


def _enrich_x_addons(x_addons: Any, snapshot: dict[str, Any]) -> list[Any]:
    """后验数值**权威回填**（PUA：数值必须来自脚本/snapshot，不得 LLM 编造）。
    v2.0.3: 入口类型守卫——LLM 可能产 dict（如 {recommended_addons: [...]}）而非 list，
    需提取内层 list 或视为空，防 Jinja2 遍历 dict keys 导致全表空白。"""
    if isinstance(x_addons, dict):
        for wrapper_key in ("recommended_addons", "x_addons", "items", "addons"):
            inner = x_addons.get(wrapper_key)
            if isinstance(inner, list):
                print(f"[report] ⚠ x_addons.json 是 dict（含 {wrapper_key}），提取内层 list", file=sys.stderr)
                x_addons = inner
                break
        else:
            print("[report] ⚠ x_addons.json 是 dict 且无可识别的 list 键，视为空", file=sys.stderr)
            x_addons = []
    if not isinstance(x_addons, list):
        return []
    cancers = snapshot.get("cancers", []) if isinstance(snapshot, dict) else []
    cancer_map: dict[str, Any] = {}
    for c in cancers:
        if isinstance(c, dict) and c.get("posterior_probability"):
            cancer_map[c.get("cancer_id", "")] = c

    normalized_rows: list[tuple[int, dict[str, Any]]] = []
    for order, x in enumerate(x_addons):
        if not isinstance(x, dict):
            continue
        source = str(x.get("risk_source", ""))
        # v2.0.3: normalize risk_level_tag to CSS-known values (danger/warning/info)
        x["risk_level_tag"] = _normalize_risk_tag(x.get("risk_level_tag"), x.get("risk_level_label"))
        x["risk_level_label"] = _normalize_risk_label(x.get("risk_level_label"), x["risk_level_tag"])
        # 候选 cancer_id：LLM 填的 cancer_id（若有）+ risk_source 关键词匹配
        candidates = []
        if x.get("cancer_id"):
            candidates.append(str(x.get("cancer_id")))
        matched_keyword = False
        for keyword, cancer_id in _CANCER_KEYWORDS.items():
            if keyword in source:
                matched_keyword = True
                candidates.append(cancer_id)
        linked = False
        for cid in candidates:
            if cid in cancer_map:
                c = cancer_map[cid]
                x["cancer_name"] = c.get("cancer_name_zh", c.get("cancer_id", ""))
                x["posterior_probability"] = c.get("posterior_probability")  # 权威覆盖
                linked = True
                break
        if not linked:
            # 未匹配/非癌项：清除任何 LLM 填的后验/癌种名（防编造）
            x.pop("posterior_probability", None)
            x.pop("cancer_name", None)
            if source and cancer_map and matched_keyword:
                reason = "该癌种在 snapshot 无后验概率"
                print(
                    f"[report] ⚠ x_addons 行 risk_source「{source}」未展示后验概率（{reason}）。",
                    file=sys.stderr,
                )
        normalized_rows.append((order, x))
    normalized_rows.sort(
        key=lambda row: (_TAG_SORT_RANK.get(row[1].get("risk_level_tag"), 2), row[0])
    )
    return [row for _, row in normalized_rows]


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


def _normalize_advice_list(items: Any) -> list[str]:
    """把 lifestyle/genetic_management 归一成 list-of-str（模板只渲染 str）。
    LLM 可能产 list-of-dict（{category,advice} 等）→ 取 advice/text/content/项值；
    list-of-str 原样；非 list → []。防 dict 被 str() 成字面量渲染。"""
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for it in items:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            txt = it.get("advice") or it.get("text") or it.get("content") or it.get("item") or it.get("name")
            out.append(str(txt) if txt else "：".join(str(v) for v in it.values()))
        else:
            out.append(str(it))
    return out


def _normalize_long_term(lti: Any) -> dict:
    """归一 long_term_intervention：genetic_management + lifestyle 都转 list-of-str。
    丢弃 LLM 自加的 schema 外字段（如 specialist_followup/monitoring_plan——模板不渲染，
    属 schema 外溢；SKILL.md 已约束只产 genetic_management + lifestyle）。"""
    if not isinstance(lti, dict):
        return {"genetic_management": [], "lifestyle": []}
    return {
        "genetic_management": _normalize_advice_list(lti.get("genetic_management", [])),
        "lifestyle": _normalize_advice_list(lti.get("lifestyle", [])),
    }


def _overall_jizaoan_performance(evidence_store: Path) -> tuple[Any, Any]:
    payload = _read_json(evidence_store / "detection_performance.json", {})
    rows = payload.get("tests", []) if isinstance(payload, dict) else []
    for row in rows:
        if (
            isinstance(row, dict)
            and row.get("test_id") == "jizaoan_multi_cancer_screening_overall"
        ):
            return row.get("sensitivity"), row.get("specificity")
    return None, None


def _liquid_biopsy_perf(artifacts: Path, evidence_store: Path) -> dict[str, Any]:
    """Liquid-biopsy performance panel for the 液体活检 section.

    Narrative fields come from the LLM artifact. Sensitivity/specificity always
    come from the authoritative overall Jizaoan detection-performance record.
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
    sens, spec = _overall_jizaoan_performance(evidence_store)
    if isinstance(sens, (int, float)) and 0 <= sens <= 1:
        result["sensitivity"] = f"{sens * 100:.1f}%"
    if isinstance(spec, (int, float)) and 0 <= spec <= 1:
        result["specificity"] = f"{spec * 100:.1f}%"
    return result


def assemble_report_json(
    *,
    artifacts: Path,
    out: Path,
    answers_path: Path | None,
    person_id: str,
    run_id: str,
    evidence_version: Any,
    evidence_store: Path = EVIDENCE_STORE_DEFAULT,
) -> dict[str, Any]:
    """Assemble report.json from internal artifacts and atomically write it.

    Returns the assembled dict. Missing optional files degrade to sane empty
    defaults instead of raising.
    """
    artifacts = Path(artifacts)

    snapshot = _read_json(artifacts / "snapshot_risk.json", {})
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

    # CP4 ADR badge reads health_summary.blocks.risk_level/overall_assessment
    # (temp 模版 L285). If CP4 left risk_level empty the ADR badge silently
    # disappears — warn so the agent re-runs finalize_structured_summary.
    health_status = health.get("status") if isinstance(health, dict) else None
    if health_status == "ready_for_render" and not assessment.get("risk_level"):
        print(
            "[report] ⚠ health_summary.assessment_result.risk_level 为空 → "
            "X加项 ADR 风险徽章不会渲染。请用 finalize_structured_summary.py 重新"
            "结构化健康总结（CP4），补齐 risk_level/overall_assessment。",
            file=sys.stderr,
        )

    now = datetime.now()
    timeline_tiers = _normalize_timeline_tiers(
        _read_json(artifacts / "timeline_tiers.json", {"priority": [], "important": [], "maintain": []})
    )
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
        "checkup_window": _checkup_window(timeline_tiers),
        "timeline_tiers": timeline_tiers,
        "x_addons": _enrich_x_addons(_read_json(artifacts / "x_addons.json", []), snapshot),
        "package_tiers": _read_json(artifacts / "package_tiers.json", []),
        "liquid_biopsy_perf": _liquid_biopsy_perf(artifacts, Path(evidence_store)),
        "long_term_intervention": _normalize_long_term(_read_json(artifacts / "long_term_intervention.json", {"genetic_management": [], "lifestyle": []})),
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
        "tumor_markers": _tumor_markers_list(artifacts),
        "evidence_version": evidence_version,
    }

    _check_section_artifacts(report)
    _atomic_write_json(artifacts / "report.json", report)
    return report


def _check_section_artifacts(report: dict[str, Any]) -> None:
    """检测 5 section artifact 是否有空缺（任一核心 section 空 → sections_incomplete）。
    v0.1.3 由「全空才报」改为「逐 section 判空」：agent 漏产 1 个 artifact（如仅缺
    long_term_intervention）会致对应 section 静默渲染空，原逻辑仅拦全空 → 残缺报告 exit 0。
    任一核心 section 空即标记 sections_incomplete=true，编排器据此 exit 10 halt。
    注：genetic_management 仅 BRCA 阳性有值，对非 BRCA 报告可空，故 lti 判空只看 lifestyle。"""
    tt = report.get("timeline_tiers") or {}
    timeline_empty = all(len(tt.get(k, [])) == 0 for k in ("priority", "important", "maintain"))
    _xa = report.get("x_addons")
    x_empty = not isinstance(_xa, list) or len(_xa) == 0
    pkg_empty = len(report.get("package_tiers") or []) == 0
    lti = report.get("long_term_intervention") or {}
    lifestyle_empty = len(lti.get("lifestyle", [])) == 0  # genetic_management 仅 BRCA，可空

    empty_sections = []
    if timeline_empty:
        empty_sections.append("timeline_tiers(三级全空)")
    if x_empty:
        empty_sections.append("x_addons(无行)")
    if pkg_empty:
        empty_sections.append("package_tiers(无档)")
    if lifestyle_empty:
        empty_sections.append("long_term_intervention.lifestyle(无条目)")

    report["sections_incomplete"] = bool(empty_sections)
    if empty_sections:
        print(
            "[report] ⚠️ 核心 section 空缺：" + ", ".join(empty_sections)
            + " → 对应 section 渲染空。请补齐 SKILL.md Minimal Workflow 第6步"
            "（--stop-after report-artifacts 产 5 JSON）的空缺 section 后重跑"
            "（编排器见 sections_incomplete=true 会 exit 10 halt）。",
            file=sys.stderr,
        )


def _atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    """Write JSON to a temp file in the same dir, then ``os.replace``."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)

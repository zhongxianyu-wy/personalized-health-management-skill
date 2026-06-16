#!/usr/bin/env python3
"""Task6 embedded replay of `健康管理-v1.0.0`.

The upstream skill `健康管理-v1.0.0` separates the work into three steps:

1. Collect a refined health-checkup markdown (here: `refined_content_bundle.md`).
2. Send it to the `cyzh-cfc` model and stream back a plain Markdown
   assessment ("阶段一…阶段五"); the assistant must preserve the response
   verbatim.
3. The agent (Claude) reads that response and the user's data, then calls
   `generate_html_report(patient_data, assessment_result)` to fill the
   copied HTML template. The renderer never asks the API to return HTML
   fragments — those structured fields are assembled by the agent.

Task6 keeps this exact contract. The orchestrator splits the work into
three phases:

* ``run_api_phase`` — call the model, save the raw Markdown response,
  the input bundle, the request audit, and a placeholder
  ``health_summary_structured_summary.json`` that asks the agent to fill
  it.
* The skill agent then edits ``health_summary_structured_summary.json``
  in place per the SKILL.md "Task6 structuring recipe".
* ``run_render_phase`` — read the completed structured summary and
  render ``health_summary.html`` using the unchanged copied template.

There is no JSON-with-HTML demand on the upstream model and no
``_parse_api_assessment`` fallback that fabricates fake table cells.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests
import yaml

SKILL_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DEFAULT = SKILL_ROOT / "config" / "formal.yaml"
TEMPLATE_DEFAULT = SKILL_ROOT / "templates" / "health_summary_v1.html"
CONTACT_CONFIG_DEFAULT = SKILL_ROOT / "config" / "contact.json"
TOKEN_URL_DEFAULT = "https://jiyinjia.jinbaisen.com/!token?key=skill_jk"
BASE_URL_DEFAULT = "https://ydai.jinbaisen.com/api/v1"
MODEL_DEFAULT = "cyzh-cfc"

STRUCTURED_SUMMARY_AWAITING = "awaiting_agent_structuring"
STRUCTURED_SUMMARY_READY = "ready_for_render"

ApiCaller = Callable[[list[dict[str, str]], dict[str, Any]], str]

HTML_FRAGMENT_FIELDS = (
    "lab_results_table",
    "abnormal_table",
    "disease_cards",
    "advice_list",
    "conclusion_table",
)

PATIENT_FIELDS = (
    "name",
    "age",
    "gender",
    "region",
    "job",
    "medical_history",
    "family_history",
    "lifestyle",
    "medication",
    "exam_date",
    "exam_source",
)

ASSESSMENT_SCALAR_FIELDS = (
    "risk_level",
    "core_risk_factors",
    "overall_assessment",
)


def _fail(message: str) -> None:
    print(f"[health_summary] FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def _read_required_text(path: Path, label: str) -> str:
    if not path.is_file():
        _fail(f"{label} not found: {path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        _fail(f"{label} is empty: {path}")
    return text


def _load_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    if not path.is_file():
        _fail(f"config not found: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_contact(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except json.JSONDecodeError:
        return {}


def _health_api_config(config: dict[str, Any]) -> dict[str, Any]:
    health = config.get("health_summary", {}) if isinstance(config, dict) else {}
    api = health.get("api", {}) if isinstance(health, dict) else {}
    return {
        "base_url": api.get("base_url", BASE_URL_DEFAULT),
        "token_url": api.get("token_url", TOKEN_URL_DEFAULT),
        "model": api.get("model", MODEL_DEFAULT),
        "stream": bool(api.get("stream", True)),
        "temperature": float(api.get("temperature", 0.7)),
        "max_tokens": int(api.get("max_tokens", 2048)),
        "timeout_seconds": int(api.get("timeout_seconds", 60)),
    }


def _extract_person(refined_text: str) -> dict[str, str]:
    """Pull obvious demographics out of the refined markdown bundle.

    The values are best-effort defaults. The agent overrides them when
    structuring the final summary, so this is only the seed input.
    """
    compact = re.sub(r"\s+", " ", refined_text)
    person = {
        "name": "未知",
        "age": "-",
        "gender": "未知",
        "exam_date": datetime.now().strftime("%Y年%m月%d日"),
    }
    safe_char = r"一-龥A-Za-z0-9·"
    inline = re.search(rf"#?\s*([{safe_char}]{{1,12}})[（(](男|女)[）)]", compact)
    if inline:
        person["name"] = inline.group(1).strip()
        person["gender"] = inline.group(2)
    name = re.search(rf"姓名[:：\s]*([{safe_char}]{{1,12}})", compact)
    if name:
        person["name"] = name.group(1).strip()
    gender = re.search(r"(?:性别|生理性别)[:：\s]*(男|女)", compact)
    if gender:
        person["gender"] = gender.group(1)
    age = re.search(r"(?:年龄|age)[:：\s]*(\d{1,3})", compact, re.I)
    if age:
        person["age"] = age.group(1)
    date = re.search(r"(?:检查日期|体检日期|报告日期)[:：\s]*([0-9]{4}[.\-/年][0-9]{1,2}[.\-/月][0-9]{1,2})", compact)
    if date:
        value = date.group(1).replace("年", ".").replace("月", ".").replace("日", "").replace("-", ".").replace("/", ".")
        parts = [part for part in value.split(".") if part]
        if len(parts) >= 3:
            person["exam_date"] = f"{parts[0]}年{parts[1]}月{parts[2]}日"
    return person


_API_MAX_CHARS = 2000

_SECTION_KEYS = {
    "demographics": ("人口学",),
    "abnormal": ("异常指标",),
    "imaging": ("影像/检验结论", "检验结论"),
    "positive": ("阳性体征", "阳性结果和异常情况"),
}

_SECTION_BUDGET_CHARS = {
    "demographics": 300,
    "abnormal": 650,
    "imaging": 650,
    "positive": 250,
    "key_findings": 250,
}

_IMPORTANT_API_KEYWORDS = (
    "↑",
    "↓",
    "异常",
    "结节",
    "钙化",
    "脂肪肝",
    "甲状腺",
    "促甲状腺",
    "丙氨酸",
    "ALT",
    "Tg",
    "切除",
)


def _trim_section_for_api(content: str, budget: int) -> str:
    content = content.strip()
    if len(content) <= budget:
        return content
    marker = "\n...[section truncated for API input]\n"
    important = _extract_important_snippets(content, max_chars=max(budget - len(marker) - 120, 1))
    if important:
        tail_budget = max(budget - len(marker) - len(important), 0)
        tail = content[-tail_budget:].lstrip() if tail_budget else ""
        return (important.rstrip() + marker + tail).strip()
    side_budget = max((budget - len(marker)) // 2, 1)
    tail_budget = max(budget - len(marker) - side_budget, 1)
    return content[:side_budget].rstrip() + marker + content[-tail_budget:].lstrip()


def _extract_important_snippets(content: str, *, max_chars: int) -> str:
    snippets: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    candidates = re.findall(r"<tr>.*?</tr>", content, flags=re.S)
    candidates.extend(line.strip() for line in content.splitlines() if line.strip())

    for idx, candidate in enumerate(candidates):
        compact = re.sub(r"\s+", " ", candidate).strip()
        if compact in seen or not any(keyword in compact for keyword in _IMPORTANT_API_KEYWORDS):
            continue
        seen.add(compact)
        if len(compact) > 240:
            compact = compact[:220].rstrip() + "...[row truncated]"
        score = _important_snippet_score(compact)
        snippets.append((score, idx, compact))

    selected: list[str] = []
    for _score, _idx, compact in sorted(snippets, key=lambda item: (-item[0], item[1])):
        proposed = "\n".join([*selected, compact])
        if len(proposed) > max_chars:
            continue
        selected.append(compact)

    return "\n".join(selected)


def _important_snippet_score(text: str) -> int:
    score = 1
    if "↑" in text or "↓" in text:
        score += 100
    if "ALT" in text or "丙氨酸" in text:
        score += 80
    if "促甲状腺" in text:
        score += 70
    if "Tg" in text:
        score += 50
    if "结节" in text or "脂肪肝" in text:
        score += 30
    return score


def _compact_for_api(refined_text: str) -> str:
    """Extract key clinical fields from the refined bundle for the API call.

    cyzh-cfc silently returns empty content when the user message exceeds
    its token limit (~43 k chars fails; ~2 k chars works). This function
    pulls only the sections the health-summary model needs: demographics,
    abnormal indicators, imaging/lab conclusions, and positive findings —
    collecting all instances across multi-document bundles. HTML comment
    markers are stripped. The full text is preserved in
    health_summary_input_bundle.md.
    """
    # Strip inter-document HTML comment markers injected by the bundle writer
    clean_lines = [
        ln for ln in refined_text.splitlines()
        if not ln.strip().startswith("<!--")
    ]
    clean_text = "\n".join(clean_lines)

    # Collect all instances of each target section group
    buckets: dict[str, list[str]] = {k: [] for k in _SECTION_KEYS}
    current_bucket: str | None = None
    for line in clean_lines:
        if line.startswith("# "):
            heading = line[2:].strip()
            current_bucket = None
            for bucket, aliases in _SECTION_KEYS.items():
                if heading in aliases:
                    current_bucket = bucket
                    break
        elif current_bucket is not None:
            buckets[current_bucket].append(line)

    parts: list[str] = []
    matched_any = False
    for bucket, aliases in _SECTION_KEYS.items():
        content = "\n".join(buckets[bucket]).strip()
        if content:
            matched_any = True
            content = _trim_section_for_api(content, _SECTION_BUDGET_CHARS[bucket])
            parts.append(f"# {aliases[0]}\n{content}")

    global_findings = _extract_important_snippets(
        clean_text,
        max_chars=_SECTION_BUDGET_CHARS["key_findings"],
    )
    if global_findings:
        parts.append(f"# 关键指标摘录\n{global_findings}")

    compact = "\n\n".join(parts)

    # v7 fix: if no sections matched (agent used different headings), fall back
    # to the first N chars of cleaned text so the API still receives content.
    # This prevents the "empty response" symptom when multi-file bundles use
    # heading names outside the hardcoded alias list.
    if not matched_any:
        compact = clean_text[:_API_MAX_CHARS].rstrip()

    if len(compact) > _API_MAX_CHARS:
        compact = compact[:_API_MAX_CHARS]
    return compact or refined_text[:_API_MAX_CHARS]


def _prepare_api_input(refined_text: str, artifacts_dir: Path) -> tuple[str, dict[str, Any]]:
    """Return the markdown that will be sent to the health-summary API.

    The full refined bundle remains available for agent structuring. When
    the merged refined markdown exceeds the API-safe input budget, create
    a second refined markdown artifact and use that for the API request.
    """
    compact_path = artifacts_dir / "health_summary_api_compact.md"
    audit_path = artifacts_dir / "health_summary_api_compact.audit.json"
    if len(refined_text) <= _API_MAX_CHARS:
        compact_path.unlink(missing_ok=True)
        audit_path.unlink(missing_ok=True)
        return refined_text, {
            "trigger": "within_api_limit",
            "source_chars": len(refined_text),
            "api_input_chars": len(refined_text),
            "api_input_md": None,
        }

    api_input_text = _compact_for_api(refined_text)
    compact_path.write_text(api_input_text, encoding="utf-8")
    audit = {
        "trigger": "refined_content_exceeds_api_limit",
        "source": "refined_content_bundle.md",
        "source_chars": len(refined_text),
        "api_input_chars": len(api_input_text),
        "api_input_limit_chars": _API_MAX_CHARS,
        "api_input_md": str(compact_path),
        "method": "section_compaction",
        "preserved_sections": list(_SECTION_KEYS),
        "note": (
            "The upstream health-summary API receives this second refined markdown. "
            "The agent must still use the full refined_content_bundle.md when "
            "filling health_summary_structured_summary.json."
        ),
    }
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return api_input_text, audit


def _build_messages(api_input_text: str) -> list[dict[str, str]]:
    """Build the 健康管理-v1.0.0 system+user message pair.

    The system prompt and disciplines mirror the upstream skill verbatim.
    We do not ask the API to return JSON or HTML fragments; the response
    is expected to be the standard 阶段一…阶段五 Markdown assessment.

    Note: api_input_text has already been selected by _prepare_api_input.
    The full bundle is preserved in health_summary_input_bundle.md for
    the agent's Task6 structuring step.
    """
    system_prompt = (
        "你是智能健康管理助手。任务：1)收集用户健康信息 2)信息完整后调用API获取评估 "
        "3)完整无损呈现API返回结果。纪律：禁止自己生成医疗建议；禁止删减API返回内容；保留所有格式。"
    )
    user_prompt = (
        "请基于下面这份体检报告精炼文本，严格按照健康管理-v1.0.0 的健康评估流程生成报告。\n\n"
        "纪律：\n"
        "1. 输出按阶段一、阶段二、阶段三、阶段四、阶段五完整呈现。\n"
        "2. 保留 Markdown 表格、加粗、🔴🟠🟡🟢 等颜色标识。\n"
        "3. 不要生成癌症概率、posterior_probability 或任何统计量。\n"
        "4. 不要返回 JSON，不要包装在 ```json``` 代码块里——按自然 Markdown 输出。\n\n"
        "=== refined_content_bundle.md ===\n"
        f"{api_input_text}"
    )
    return [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]


def _fetch_api_key(token_url: str, timeout_seconds: int) -> str:
    """Fetch the dynamic API key via curl, surfacing stderr on failure.

    The original 健康管理-v1.0.0 helper used curl rather than requests, and we
    keep that to match its transport behaviour. When curl fails we attach
    the stderr tail (TLS/proxy/etc.) to the RuntimeError so the orchestrator
    audit captures *why* — otherwise the agent only sees a generic
    "无法获取 API Key" and can't act.
    """
    result = subprocess.run(
        ["curl", "-sS", "--max-time", "10", token_url],
        capture_output=True,
        text=True,
        timeout=min(timeout_seconds, 15),
    )
    body = (result.stdout or "").strip()
    if body:
        try:
            envelope = json.loads(body)
        except json.JSONDecodeError:
            envelope = None
        if isinstance(envelope, dict):
            token = envelope.get("token") or envelope.get("data", {}).get("token") if isinstance(envelope.get("data"), dict) else envelope.get("token")
            if isinstance(token, str) and token:
                return token
            raise RuntimeError(
                "Token 接口返回 JSON 但未携带 token 字段："
                f" body={body[:200]}"
            )
        return body
    stderr_tail = (result.stderr or "").strip().splitlines()[-3:]
    detail = " | ".join(stderr_tail) if stderr_tail else "no stderr"
    raise RuntimeError(
        "无法获取 API Key，请检查网络连接后重试。"
        f" curl exit={result.returncode}; stderr: {detail}"
    )


def call_health_management_api(messages: list[dict[str, str]], api_config: dict[str, Any]) -> str:
    """Stream the cyzh-cfc response and return the concatenated Markdown content."""
    api_key = _fetch_api_key(api_config["token_url"], api_config["timeout_seconds"])
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": api_config["model"],
        "messages": messages,
        "stream": api_config["stream"],
        "temperature": api_config["temperature"],
        "max_tokens": api_config["max_tokens"],
    }
    response = requests.post(
        api_config["base_url"].rstrip("/") + "/chat/completions",
        headers=headers,
        json=payload,
        stream=True,
        timeout=api_config["timeout_seconds"],
    )
    response.raise_for_status()
    chunks: list[str] = []
    for raw_line in response.iter_lines():
        if not raw_line:
            continue
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else str(raw_line)
        if not line.startswith("data: "):
            continue
        data = line[6:]
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        delta = chunk.get("choices", [{}])[0].get("delta", {})
        if "content" in delta:
            chunks.append(delta["content"])
    return "".join(chunks)


def _write_input_bundle(path: Path, refined_text: str) -> None:
    path.write_text(
        "<!-- CANCERRISK_HEALTH_SUMMARY_INPUT_START source=refined_content_bundle.md -->\n"
        "# Refined Health Checkup Content\n\n"
        f"{refined_text.rstrip()}\n"
        "<!-- CANCERRISK_HEALTH_SUMMARY_INPUT_END source=refined_content_bundle.md -->\n",
        encoding="utf-8",
    )


def _structured_summary_skeleton(
    refined_text: str,
    api_text: str,
    contact: dict[str, Any],
    health_cfg: dict[str, Any],
) -> dict[str, Any]:
    """Return the placeholder structured summary that the agent must complete.

    The shape mirrors `健康管理-v1.0.0` `generate_html_report(patient_data,
    assessment_result)` exactly. The agent overwrites every field after
    reading both ``refined_content_bundle.md`` and the API response.
    """
    seed_person = _extract_person(refined_text)
    return {
        "status": STRUCTURED_SUMMARY_AWAITING,
        "instructions": (
            "STEP 1 — Read artifacts/health_summary_api_response.md (Markdown from the health-management API). "
            "This contains the FULL assessment text with sections like 阶段一, 阶段二, etc. "
            "It is NOT JSON — do NOT try to parse it as JSON.\n"
            "STEP 2 — Extract structured data from that Markdown and fill EVERY field below.\n"
            "  * patient_data: copy demographics from the report header (name, age, gender, region, job, etc.).\n"
            "  * assessment_result: extract ADR score, risk level, core risk factors, overall assessment.\n"
            "  * lab_results_table / abnormal_table / disease_cards / advice_list / conclusion_table: "
            "    these MUST be valid HTML table strings (<table>...</table>). "
            "    Extract the content from the API response Markdown tables and convert to HTML.\n"
            "STEP 3 — Set status to '" + STRUCTURED_SUMMARY_READY + "'.\n"
            "STEP 4 — Keep raw_assessment_markdown as the ORIGINAL API response text (do NOT modify it).\n"
            "STEP 5 — Use scripts/finalize_structured_summary.py to write the JSON safely; "
            "do NOT hand-write JSON strings (escaping errors break the render stage)."
        ),
        "health_summary_provider": health_cfg.get("provider", "health-management-v1.0.0"),
        "health_summary_mode": health_cfg.get("mode", "external_api_replay"),
        "patient_data": {
            "name": seed_person["name"],
            "age": seed_person["age"],
            "gender": seed_person["gender"],
            "region": "未知",
            "job": "未知",
            "medical_history": "见体检报告",
            "family_history": "未提供",
            "lifestyle": "未提供",
            "medication": "未提供",
            "exam_date": seed_person["exam_date"],
            "exam_source": "用户提供的体检报告",
        },
        "assessment_result": {
            "risk_level": "",
            "core_risk_factors": "",
            "overall_assessment": "",
            "lab_results_table": "",
            "abnormal_table": "",
            "disease_cards": "",
            "advice_list": "",
            "conclusion_table": "",
        },
        "raw_assessment_markdown": api_text,
        "contact": contact,
    }


def build_api_fallback_summary(refined_bundle_path: Path) -> dict[str, Any]:
    """Fallback summary when the health-summary API is unavailable.

    Status is NOT ready_for_render — the pipeline must halt so the agent
    can either fix the API issue or manually fill the structured summary.
    """
    refined_text = (
        refined_bundle_path.read_text(encoding="utf-8") if refined_bundle_path.is_file() else ""
    )
    seed = _extract_person(refined_text)
    return {
        "status": "api_unavailable_fallback",
        "health_summary_provider": "api_unavailable_fallback",
        "health_summary_mode": "fallback",
        "patient_data": {
            "name": seed["name"],
            "age": seed["age"],
            "gender": seed["gender"],
            "region": "",
            "job": "",
            "medical_history": "",
            "family_history": "",
            "lifestyle": "",
            "medication": "",
            "exam_date": seed["exam_date"],
            "exam_source": "",
        },
        "assessment_result": {
            "risk_level": "",
            "core_risk_factors": "",
            "overall_assessment": "",
            "lab_results_table": "",
            "abnormal_table": "",
            "disease_cards": "",
            "advice_list": "",
            "conclusion_table": "",
        },
        "raw_assessment_markdown": "",
    }


def _validate_structured_summary(summary: dict[str, Any]) -> None:
    if not isinstance(summary, dict):
        _fail("structured summary must be a JSON object")
    if summary.get("status") != STRUCTURED_SUMMARY_READY:
        _fail(
            "structured summary status is "
            f"{summary.get('status')!r}; expected '{STRUCTURED_SUMMARY_READY}'. "
            "Follow SKILL.md 'Task6 structuring recipe' to fill the summary."
        )
    patient = summary.get("patient_data")
    if not isinstance(patient, dict):
        _fail("structured summary.patient_data must be an object")
    assessment = summary.get("assessment_result")
    if not isinstance(assessment, dict):
        _fail("structured summary.assessment_result must be an object")
    missing_patient = [field for field in PATIENT_FIELDS if not str(patient.get(field, "")).strip()]
    missing_assessment = [
        field for field in (*ASSESSMENT_SCALAR_FIELDS, *HTML_FRAGMENT_FIELDS)
        if not str(assessment.get(field, "")).strip()
    ]
    if missing_patient or missing_assessment:
        _fail(
            "structured summary missing fields — patient_data: "
            f"{missing_patient}; assessment_result: {missing_assessment}"
        )


_GENDER_EMOJI = {"男": "👨", "male": "👨", "M": "👨", "女": "👩", "female": "👩", "F": "👩"}


def _normalize_age(raw: Any) -> str:
    """Strip a trailing 岁 the agent may have included.

    The template appends 岁 itself; if the structured summary already
    carries it, the rendered string ends up as "29岁岁".
    """
    text = str(raw).strip()
    while text.endswith("岁"):
        text = text[:-1].rstrip()
    return text


def _gender_emoji(gender: Any) -> str:
    return _GENDER_EMOJI.get(str(gender).strip(), "🧑")


def _build_report_data(summary: dict[str, Any]) -> dict[str, str]:
    """Mirror v1.0.0 ``HealthAssistant._build_report_data`` placeholder map."""
    patient = summary["patient_data"]
    assessment = summary["assessment_result"]
    contact = summary.get("contact") or {}
    screening = contact.get("screening", {}) if isinstance(contact, dict) else {}
    hotline = contact.get("hotline", {}) if isinstance(contact, dict) else {}
    report = contact.get("report", {}) if isinstance(contact, dict) else {}
    now = datetime.now().strftime("%Y年%m月%d日")
    return {
        "SCREENING_URL": screening.get("url", "https://bmsapp.geneplus.org.cn/business/addOrder"),
        "SCREENING_NAME": screening.get("name", "健康筛查"),
        "SCREENING_DESCRIPTION": screening.get("description", "专业健康筛查，助您早发现、早预防、早干预"),
        "HOTLINE_NUMBER": hotline.get("number", "400-166-6506"),
        "REPORT_TITLE": report.get("title", "健康疾病风险评估报告"),
        "REPORT_SUBTITLE": report.get("subtitle", "Smart Health Risk Assessment Report"),
        "POWERED_BY": report.get("powered_by", "健康"),
        "PATIENT_NAME": patient["name"],
        "PATIENT_AGE": _normalize_age(patient["age"]),
        "PATIENT_GENDER": patient["gender"],
        "PATIENT_GENDER_EMOJI": _gender_emoji(patient["gender"]),
        "PATIENT_REGION": patient["region"],
        "PATIENT_JOB": patient["job"],
        "MEDICAL_HISTORY": patient["medical_history"],
        "FAMILY_HISTORY": patient["family_history"],
        "LIFESTYLE": patient["lifestyle"],
        "MEDICATION": patient["medication"],
        "EXAM_DATE": patient["exam_date"],
        "EXAM_SOURCE": patient["exam_source"],
        "RISK_LEVEL": assessment["risk_level"],
        "CORE_RISK_FACTORS": assessment["core_risk_factors"],
        "OVERALL_ASSESSMENT": assessment["overall_assessment"],
        "LAB_RESULTS_TABLE": assessment["lab_results_table"],
        "ABNORMAL_INDICATORS_TABLE": assessment["abnormal_table"],
        "DISEASE_RISK_CARDS": assessment["disease_cards"],
        "ADVICE_LIST": assessment["advice_list"],
        "CONCLUSION_TABLE": assessment["conclusion_table"],
        "GENERATE_DATE": now,
    }


def _api_feedback_section(raw_assessment_markdown: str) -> str:
    escaped = html.escape(raw_assessment_markdown).replace("\n", "<br>")
    return (
        '<div class="section">\n'
        '  <h2 class="section-title">API原始反馈</h2>\n'
        f'  <div class="api-raw-feedback" style="font-size:13px;line-height:1.7;color:#555;background:#f8f9fa;border-radius:8px;padding:15px;">{escaped}</div>\n'
        '</div>\n'
    )


def _has_cancer_keyword(summary: dict[str, Any]) -> bool:
    pool = " ".join(
        str(summary["assessment_result"].get(field, ""))
        for field in (
            "core_risk_factors",
            "overall_assessment",
            "disease_cards",
            "conclusion_table",
        )
    ) + " " + str(summary.get("raw_assessment_markdown", ""))
    return any(token in pool for token in ("癌", "肿瘤", "cancer"))


def _render_template(
    template: str,
    values: dict[str, Any],
    disclaimer: str,
    raw_assessment_markdown: str,
    has_cancer_risk: bool,
) -> str:
    if has_cancer_risk:
        rendered = re.sub(r"\{\{#if HAS_CANCER_RISK\}\}\s*", "", template)
        rendered = re.sub(r"\s*\{\{/if\}\}", "", rendered)
    else:
        rendered = re.sub(r"\{\{#if HAS_CANCER_RISK\}\}.*?\{\{/if\}\}", "", template, flags=re.S)
    raw_html_keys = {"LAB_RESULTS_TABLE", "ABNORMAL_INDICATORS_TABLE", "DISEASE_RISK_CARDS", "ADVICE_LIST", "CONCLUSION_TABLE"}
    for key, value in values.items():
        replacement = str(value) if key in raw_html_keys else html.escape(str(value))
        rendered = rendered.replace("{{" + key + "}}", replacement)
    rendered = re.sub(
        r"<div class=\"disclaimer\">.*?</div>",
        "<div class=\"disclaimer\"><strong>重要提示：</strong>" + html.escape(disclaimer).replace("\n", "<br>") + "</div>",
        rendered,
        flags=re.S,
    )
    rendered = re.sub(r"\{\{[A-Z0-9_]+\}\}", "未提供", rendered)
    return rendered


def run_api_phase(
    *,
    refined_content_bundle: Path,
    output_dir: Path,
    config_path: Path | None = CONFIG_DEFAULT,
    contact_config_path: Path | None = CONTACT_CONFIG_DEFAULT,
    api_caller: ApiCaller | None = None,
) -> dict[str, Any]:
    """Call the upstream API and stage the artifacts the agent will edit."""
    refined_text = _read_required_text(refined_content_bundle, "refined_content_bundle")
    config = _load_config(config_path)
    contact = _load_contact(contact_config_path)
    health_cfg = config.get("health_summary", {}) if isinstance(config, dict) else {}

    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir = output_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    input_bundle_path = artifacts_dir / str(health_cfg.get("input_bundle", "health_summary_input_bundle.md"))
    _write_input_bundle(input_bundle_path, refined_text)

    # v6: short-circuit when the agent has already produced a
    # ready_for_render structured summary. Without this skip, every
    # re-run hits the upstream API even though the result will be
    # discarded — wastes quota and triggers the empty-response guard
    # on transient TLS errors.
    structured_path_check = artifacts_dir / str(
        health_cfg.get("structured_summary", "health_summary_structured_summary.json")
    )
    if structured_path_check.is_file():
        try:
            existing = json.loads(structured_path_check.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            existing = {}
        if existing.get("status") == STRUCTURED_SUMMARY_READY:
            print(
                f"[task6][api] skipped — {structured_path_check.name} is "
                "already ready_for_render (agent CP4 done); upstream API not called."
            )
            return {
                "status": STRUCTURED_SUMMARY_READY,
                "skipped": True,
                "structured_summary_json": str(structured_path_check),
            }

    api_config = _health_api_config(config)
    api_input_text, api_input_audit = _prepare_api_input(refined_text, artifacts_dir)

    # Append interactive questionnaire answers so the health-summary API
    # receives jizaoan result, smoking, family history etc.
    _answers_md = artifacts_dir / "interactive_answers.md"
    if _answers_md.is_file():
        _q_block = "\n\n" + _answers_md.read_text(encoding="utf-8").strip()
        # Trim OCR compact text to keep total within the API budget
        _budget = max(0, _API_MAX_CHARS - len(_q_block))
        api_input_text = api_input_text[:_budget] + _q_block

    messages = _build_messages(api_input_text)
    (artifacts_dir / "health_summary_api_messages.json").write_text(
        json.dumps(
            {"messages": messages, "api_config": api_config, "api_input": api_input_audit},
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    caller = api_caller or call_health_management_api
    api_text = caller(messages, api_config)
    # Defensive: an empty / whitespace-only API response means upstream
    # silently failed (bad token, blocked stream, prompt rejection, etc.).
    # If we let it through, the agent gets a blank skeleton and renders a
    # report with no narrative — exactly the regression that masked the
    # _fetch_api_key JSON bug for so long. Surface it loudly instead.
    if not (api_text or "").strip():
        raise RuntimeError(
            "health-summary API returned an empty response. Inspect "
            "artifacts/health_summary_api_messages.json + the upstream "
            "token endpoint before re-running."
        )
    (artifacts_dir / "health_summary_api_response.md").write_text(api_text, encoding="utf-8")

    structured_path = artifacts_dir / str(
        health_cfg.get("structured_summary", "health_summary_structured_summary.json")
    )
    summary_skeleton = _structured_summary_skeleton(refined_text, api_text, contact, health_cfg)
    if not structured_path.exists() or json.loads(structured_path.read_text(encoding="utf-8") or "{}").get("status") != STRUCTURED_SUMMARY_READY:
        structured_path.write_text(
            json.dumps(summary_skeleton, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    return {
        "status": STRUCTURED_SUMMARY_AWAITING,
        "health_summary_provider": health_cfg.get("provider", "health-management-v1.0.0"),
        "health_summary_mode": health_cfg.get("mode", "external_api_replay"),
        "health_summary_inputs": {
            "refined_content_bundle_md": str(refined_content_bundle),
            "api_input_chars": api_input_audit["api_input_chars"],
            "api_input_md": api_input_audit["api_input_md"],
        },
        "health_summary_outputs": {
            "input_bundle_md": str(input_bundle_path),
            "api_messages_json": str(artifacts_dir / "health_summary_api_messages.json"),
            "api_response_md": str(artifacts_dir / "health_summary_api_response.md"),
            "structured_summary_json": str(structured_path),
        },
    }


def run_render_phase(
    *,
    output_dir: Path,
    config_path: Path | None = CONFIG_DEFAULT,
    template_path: Path | None = None,
) -> dict[str, Any]:
    """Render the final HTML from the agent-completed structured summary."""
    config = _load_config(config_path)
    health_cfg = config.get("health_summary", {}) if isinstance(config, dict) else {}
    safety_cfg = config.get("safety", {}) if isinstance(config, dict) else {}
    disclaimer = str(safety_cfg.get("disclaimer") or "本报告仅用于健康管理参考，不构成医学诊断或治疗建议。")
    template = Path(template_path or health_cfg.get("template_file") or TEMPLATE_DEFAULT)
    template_text = _read_required_text(template, "health_summary_template")

    artifacts_dir = output_dir / "artifacts"
    structured_path = artifacts_dir / str(
        health_cfg.get("structured_summary", "health_summary_structured_summary.json")
    )
    if not structured_path.is_file():
        _fail(f"structured summary not found: {structured_path}")
    raw = structured_path.read_text(encoding="utf-8")
    try:
        summary = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Most common cause: agent hand-wrote the JSON via heredoc and an
        # HTML fragment / Chinese punctuation broke escaping. Point them
        # at the helper that bypasses the problem entirely.
        _fail(
            f"structured summary {structured_path} is invalid JSON: {exc}. "
            "Most likely an HTML fragment (lab_results_table / disease_cards / "
            "advice_list / etc.) contains an unescaped quote or backslash. "
            "Use scripts/finalize_structured_summary.py — it serialises via "
            "json.dump and supports @path indirection for HTML fragments so "
            "you never hand-escape JSON. Example: "
            "`python cancerrisk-skill/scripts/finalize_structured_summary.py "
            "--analysis-output <dir> --fills <path/to/fills.json>`."
        )
    _validate_structured_summary(summary)

    report_data = _build_report_data(summary)
    html_text = _render_template(
        template_text,
        report_data,
        disclaimer,
        summary.get("raw_assessment_markdown", ""),
        _has_cancer_keyword(summary),
    )

    html_path = output_dir / str(health_cfg.get("output_html", "health_summary.html"))
    html_path.write_text(html_text, encoding="utf-8")

    payload = {
        "status": "rendered",
        "health_summary_provider": summary.get("health_summary_provider", health_cfg.get("provider", "health-management-v1.0.0")),
        "health_summary_mode": summary.get("health_summary_mode", health_cfg.get("mode", "external_api_replay")),
        "health_summary_outputs": {
            "html": str(html_path),
            "input_bundle_md": str(artifacts_dir / str(health_cfg.get("input_bundle", "health_summary_input_bundle.md"))),
            "api_messages_json": str(artifacts_dir / "health_summary_api_messages.json"),
            "api_response_md": str(artifacts_dir / "health_summary_api_response.md"),
            "structured_summary_json": str(structured_path),
        },
        "patient_data": summary["patient_data"],
        "assessment_result": summary["assessment_result"],
        "raw_assessment_markdown": summary.get("raw_assessment_markdown", ""),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    return payload


def render_health_summary(
    *,
    refined_content_bundle: Path,
    output_dir: Path,
    config_path: Path | None = CONFIG_DEFAULT,
    contact_config_path: Path | None = CONTACT_CONFIG_DEFAULT,
    template_path: Path | None = None,
    api_caller: ApiCaller | None = None,
    structured_summary_provider: Callable[[dict[str, Any], Path], None] | None = None,
) -> dict[str, Any]:
    """End-to-end helper: API phase → optional structuring callback → render phase.

    Production runs let the agent edit the structured summary between
    phases, so the orchestrator calls ``run_api_phase`` followed by
    ``run_render_phase`` separately. This helper exists for tests and
    in-process callers that pass a ``structured_summary_provider``
    callback to fill the summary directly.
    """
    api_outputs = run_api_phase(
        refined_content_bundle=refined_content_bundle,
        output_dir=output_dir,
        config_path=config_path,
        contact_config_path=contact_config_path,
        api_caller=api_caller,
    )
    structured_path = Path(api_outputs["health_summary_outputs"]["structured_summary_json"])

    if structured_summary_provider is not None:
        skeleton = json.loads(structured_path.read_text(encoding="utf-8"))
        structured_summary_provider(skeleton, structured_path)

    return run_render_phase(
        output_dir=output_dir,
        config_path=config_path,
        template_path=template_path,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay health-management-v1.0.0 strategy in 3 phases.")
    parser.add_argument("--refined-content-bundle", required=False)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--config", default=str(CONFIG_DEFAULT))
    parser.add_argument("--contact-config", default=str(CONTACT_CONFIG_DEFAULT))
    parser.add_argument("--template", default=None)
    parser.add_argument(
        "--phase",
        choices=("api", "render", "all"),
        default="all",
        help="api: call the API and stage artifacts; render: render HTML from the agent-edited structured summary.",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    config_path = Path(args.config) if args.config else None
    template_path = Path(args.template) if args.template else None
    contact_path = Path(args.contact_config) if args.contact_config else None

    if args.phase in ("api", "all"):
        if not args.refined_content_bundle:
            parser.error("--refined-content-bundle is required for phase=api or phase=all")
        outputs = run_api_phase(
            refined_content_bundle=Path(args.refined_content_bundle),
            output_dir=output_dir,
            config_path=config_path,
            contact_config_path=contact_path,
        )
        print(json.dumps(outputs, ensure_ascii=False, indent=2))
        if args.phase == "api":
            return

    rendered = run_render_phase(
        output_dir=output_dir,
        config_path=config_path,
        template_path=template_path,
    )
    print(json.dumps(rendered["health_summary_outputs"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

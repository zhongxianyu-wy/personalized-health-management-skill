#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# P2 [BLOCKER-1/ERROR-2] Windows 环境自检：清除 PYTHONHOME/PYTHONPATH 污染 + UTF-8 输出
os.environ.pop("PYTHONHOME", None)
os.environ.pop("PYTHONPATH", None)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml

SKILL_ROOT = Path(__file__).resolve().parent.parent
ARCHIVES_DEFAULT = None  # v0.1.4: resolved at runtime → cwd/output or CANCERRISK_OUTPUT_DIR env (sandbox-friendly; not the read-only skill package)
EVIDENCE_STORE = SKILL_ROOT / "references" / "database" / "cancerrisk" / "json"
PERSON_ID_DEFAULT = "test-person-001"
CONFIG_DEFAULT = str(SKILL_ROOT / "config" / "formal.yaml")
SUPPORTED_INPUT_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff",
    ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".html", ".htm",
}

STOP_AFTER_CHOICES = [
    "config",
    "mineru",
    "refine",
    "demographics",
    "interactive",
    "master-template",
    "assertion-template",  # legacy alias for master-template
    "cp3-verify",
    "risk-factor-gate",
    "health-summary-api",
    "screening-gap",
    "archive-proposal",
    "archive",
    "report-artifacts",
    "report",
]


def write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class ScreeningGapGateError(RuntimeError):
    def __init__(self, code: int, errors: list[str]):
        self.code = code
        self.errors = errors
        super().__init__("; ".join(errors))


def _validate_screening_gap_stage(
    *,
    artifacts: Path,
    knowledge_root: Path,
    screening_gap_answers_path: Path | None,
) -> dict:
    """v2.0.4: 简化 CP5 校验——只读 1 个 screening_recommendations.json，
    只查 3 条核心规则（dedup / done+normal / medium+），不做字段级校验。"""
    scripts_dir = str(Path(__file__).resolve().parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import validate_screening_gap as screening_gate

    # 单一产物（不再分 draft/questionnaire/answers/final）
    rec_path = artifacts / "screening_recommendations.json"
    if not rec_path.is_file():
        raise ScreeningGapGateError(11, [
            "缺少 screening_recommendations.json——agent 需产 A/B/C 三段推荐 "
            "（A 癌症风险/B 异常复查/C 周期管理 + gap 问答内嵌）"
        ])
    try:
        payload = json.loads(rec_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScreeningGapGateError(11, [f"screening_recommendations.json JSON 解析失败: {exc}"]) from exc

    snapshot = None
    snap_path = artifacts / "snapshot_risk.json"
    if snap_path.is_file():
        try:
            snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
        except Exception:
            pass  # snapshot 非必需，有则查 medium+

    errors = screening_gate.validate(payload, snapshot)
    if errors:
        raise ScreeningGapGateError(11, errors)

    return {
        "cancer_risk_count": len(payload.get("cancer_risk", [])),
        "other_abnormalities_count": len(payload.get("other_abnormalities", [])),
        "periodic_management_count": len(payload.get("periodic_management", [])),
    }


def _validate_screening_report_artifacts(*, artifacts: Path) -> None:
    """v2.0.4: 简化——只查 timeline maintain 非空时有 dedup_key（不强制匹配 final），
    不再做 maintain=periodic_management 严格等值校验（允许 LLM 在 artifact 阶段微调）。"""
    timeline_path = artifacts / "timeline_tiers.json"
    if timeline_path.is_file():
        try:
            timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
            # 仅检查 maintain 项有内容（完整性），不查字符级
            maintain = timeline.get("maintain", [])
            if isinstance(maintain, list):
                for row in maintain:
                    if isinstance(row, dict) and not row.get("item_name"):
                        print("[report] ⚠ timeline maintain 有项缺 item_name", file=sys.stderr)
        except Exception:
            pass  # 非阻断


def _supported_input_names(input_path: Path) -> set[str]:
    if input_path.is_file():
        return {input_path.name} if input_path.suffix.lower() in SUPPORTED_INPUT_EXTENSIONS else set()
    if input_path.is_dir():
        return {
            p.name
            for p in input_path.iterdir()
            if p.is_file()
            and not p.name.startswith(".")
            and p.suffix.lower() in SUPPORTED_INPUT_EXTENSIONS
        }
    return set()


def _resolve_archives_root(args_archives_root: str | None) -> str:
    """v0.1.4: sandbox-friendly archive root. Default to cwd/output (NOT the
    read-only skill package) so WorkBuddy/QwenPaw sandboxes can write; the
    CANCERRISK_OUTPUT_DIR env var overrides (platform-injected). Explicit
    --archives-root always wins."""
    if args_archives_root:
        return args_archives_root
    env_out = os.environ.get("CANCERRISK_OUTPUT_DIR")
    if env_out:
        return str(Path(env_out).expanduser().resolve())
    return str(Path.cwd() / "output")


def _manifest_matches_input(manifest: dict, input_path: Path) -> bool:
    sources = {
        Path(f["source_path"]).name
        for f in manifest.get("files", [])
        if f.get("source_path")
    }
    current = _supported_input_names(input_path)
    return bool(current) and current == sources


def _validate_refined_md(refined_path: Path, content_path: Path) -> None:
    text = refined_path.read_text(encoding="utf-8").strip()
    if not text:
        raise SystemExit(f"[refine] FAIL: refined.md structure invalid: {refined_path} is empty")
    if content_path.is_file() and text == content_path.read_text(encoding="utf-8").strip():
        raise SystemExit(
            f"[refine] FAIL: refined.md structure invalid: {refined_path} is identical to content.md"
        )
    has_demographics = any(token in text for token in ("人口学", "姓名", "性别", "年龄"))
    has_clinical_section = any(token in text for token in (
        "异常指标", "影像", "检验结论", "阳性体征",
        "检验结果", "检验项目", "小结", "体检结论", "未见异常", "检查结论", "建议",
    ))
    if not has_demographics:
        raise SystemExit(
            f"[refine] FAIL: refined.md missing demographics section in {refined_path}; "
            "expected at least one of: 人口学/姓名/性别/年龄"
        )
    if not has_clinical_section:
        raise SystemExit(
            f"[refine] FAIL: refined.md missing clinical section in {refined_path}; "
            "expected at least one of: 异常指标/影像/检验结果/小结/体检结论/未见异常/检查结论/建议"
        )


def _collect_refined_paths(mineru_root: Path, manifest: dict) -> dict[str, Path]:
    refined_paths: dict[str, Path] = {}
    missing_refine: list[str] = []
    for record in manifest.get("files", []):
        data_id = record.get("data_id")
        if not data_id:
            continue
        content_md = mineru_root / data_id / "content.md"
        if not content_md.exists():
            continue
        refined = mineru_root / data_id / "refined.md"
        if not refined.is_file():
            missing_refine.append(data_id)
            continue
        _validate_refined_md(refined, content_md)
        refined_paths[data_id] = refined
    if missing_refine:
        raise SystemExit(
            "[refine] FAIL: missing refined.md for data_ids: "
            + ", ".join(missing_refine)
            + ". Follow SKILL.md 'Refine recipe' to write each refined.md, then re-run."
        )
    return refined_paths


def _guard_master_fill_not_empty(
    *,
    artifacts: Path,
    timeline_candidate: dict,
    tumor_markers_candidate: dict,
) -> None:
    """Halt when Agent Checkpoint 3 was skipped.

    The master-fill checkpoint is intentionally agent-driven: the script
    scaffolds candidate files, and the agent fills them from refined.md.
    An empty timeline plus empty tumor-marker candidate almost always means
    the orchestrator was re-run past CP3 without the agent doing that fill.
    """
    timeline_records = timeline_candidate.get("records")
    tumor_tests = tumor_markers_candidate.get("tests")
    n_timeline = len(timeline_records) if isinstance(timeline_records, list) else 0
    n_tumor = len(tumor_tests) if isinstance(tumor_tests, list) else 0
    if n_timeline or n_tumor:
        return

    sentinel = artifacts / "master_fill_skipped.warning"
    sentinel.write_text(
        "MASTER_FILL_SKIPPED\n"
        f"run_at={datetime.now().isoformat()}\n"
        f"timeline_records={n_timeline}\n"
        f"tumor_marker_tests={n_tumor}\n"
        "remediation=follow SKILL.md Agent Checkpoint 3: fill "
        "structured_risk_factors_timeline.candidate.json and/or "
        "tumor_markers.candidate.json, validate them, then re-run.\n",
        encoding="utf-8",
    )
    raise SystemExit(
        "[task4] HALT: Master fill candidate is EMPTY. "
        "The orchestrator built structured_risk_factors_timeline.candidate.json "
        "and tumor_markers.candidate.json, but neither contains agent-filled "
        "records. Follow SKILL.md 'Master fill recipe' / 'Tumor markers recipe', "
        "run the validators, then re-run. "
        f"Sentinel file: {sentinel}"
    )


def attach_source_context(
    records: list[dict],
    *,
    data_id: str,
    source_path: str | None,
    content_md_path: Path,
) -> list[dict]:
    enriched = []
    for record in records:
        item = dict(record)
        item["source_data_id"] = data_id
        item["source_path"] = source_path
        item["content_md_path"] = str(content_md_path)
        enriched.append(item)
    return enriched


def run_health_summary_api_stage(
    *,
    out: Path,
    artifacts: Path,
    config_path: Path,
    contact_config_path: Path | None = None,
    api_caller=None,
) -> dict:
    """Task6 phase A: call cyzh-cfc, stage artifacts, scaffold structured summary."""
    import render_health_summary

    return render_health_summary.run_api_phase(
        refined_content_bundle=artifacts / "refined_content_bundle.md",
        output_dir=out,
        config_path=config_path,
        contact_config_path=contact_config_path,
        api_caller=api_caller,
    )


def run_health_summary_stage(
    *,
    out: Path,
    artifacts: Path,
    config_path: Path,
    contact_config_path: Path | None = None,
    api_caller=None,
    structured_summary_provider=None,
) -> dict:
    """Compatibility helper: run all three phases in one process (test path)."""
    import render_health_summary

    rendered = render_health_summary.render_health_summary(
        refined_content_bundle=artifacts / "refined_content_bundle.md",
        output_dir=out,
        config_path=config_path,
        contact_config_path=contact_config_path,
        api_caller=api_caller,
        structured_summary_provider=structured_summary_provider,
    )
    health_outputs = rendered["health_summary_outputs"]
    task6_audit = (
        "# Task06 Health Summary Audit\n\n"
        f"- provider: {rendered['health_summary_provider']}\n"
        f"- mode: {rendered['health_summary_mode']}\n"
        "- strategy: health-management-v1.0.0 markdown reply, agent-structured fields\n"
        f"- html: {health_outputs['html']}\n"
        f"- input bundle: {health_outputs['input_bundle_md']}\n"
        f"- api response: {health_outputs['api_response_md']}\n"
        f"- structured summary: {health_outputs['structured_summary_json']}\n"
        "- probability source: none; health summary is display-only\n"
    )
    (out / "module_audits").mkdir(parents=True, exist_ok=True)
    (out / "module_audits" / "task06_health_summary.md").write_text(
        task6_audit,
        encoding="utf-8",
    )
    return rendered


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursive dict merge — override wins, lists are replaced wholesale."""
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_config(path: Path) -> dict:
    """Load formal.yaml, then deep-merge cancerrisk-skill/config/local.yaml
    on top if it exists.

    local.yaml is gitignored — it's the place operators put their own
    MinerU token, change the health-summary API endpoint, or override
    external absolute paths without touching the canonical config that
    ships in git. See SKILL.md "Configuration overrides".
    """
    if not path.is_file():
        raise SystemExit(f"[config] FAIL: {path} not found")
    base = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    local_path = path.parent / "local.yaml"
    if local_path.is_file():
        local = yaml.safe_load(local_path.read_text(encoding="utf-8")) or {}
        if isinstance(local, dict) and isinstance(base, dict):
            merged = _deep_merge(base, local)
            print(f"[config] merged local override from {local_path}")
            return merged
    return base


def _extract_exam_date(text: str) -> str | None:
    """Best-effort exam-date extraction from MinerU OCR text.

    Recognised patterns, tried in order:
      1. ``检查日期/体检日期/报告日期：YYYY-MM-DD`` (with multiple separators)
      2. Bare ``YYYY-MM-DD`` (or YYYY/MM/DD, YYYY.MM.DD, YYYY年MM月DD日)
         appearing in the first 5KB of the document — picks the earliest
         one. Handles "2026-01-1911:56:11" by anchoring the day portion.
      3. ``项目编号 / 检验编号 ... YYYYMMDD`` — compact 8-digit date after
         a Chinese code label.
      4. ``标本编号 ... YYMMDD`` — 2-digit-year prefix in a 12+ digit
         specimen number; assumes 20YY.
    Returns ``YYYY-MM-DD`` or ``None`` when no plausible date is found.
    """
    if not text:
        return None
    import re as _re
    from datetime import date as _date

    def _canon(year: int, month: int, day: int) -> str | None:
        if year < 1990 or year > 2100:
            return None
        try:
            _date(year, month, day)
        except ValueError:
            return None
        return f"{year:04d}-{month:02d}-{day:02d}"

    # 1) Labelled date.
    m = _re.search(
        r"(?:检查日期|体检日期|报告日期|送检日期|检测日期|采样日期)[:：\s]*"
        r"([0-9]{4})[.\-/年]\s*([0-9]{1,2})[.\-/月]\s*([0-9]{1,2})",
        text,
    )
    if m:
        d = _canon(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return d

    # 2) Bare YYYY-MM-DD anywhere; day portion is 1-2 digits, validated
    # below by _canon. We deliberately allow trailing digits (e.g. a time
    # stamp mashed in like "2026-01-1911:56:11" → 2026-01-19) and rely on
    # _canon to reject implausible months/days.
    # NOTE: alternation order matters. Python regex picks the first match,
    # so list 2-digit days before 1-digit to avoid "19" → day=1 in
    # mashed timestamps like "2026-01-1911:56:11".
    m = _re.search(
        r"(19\d{2}|20\d{2})[.\-/年]\s*(1[0-2]|0[1-9]|[1-9])[.\-/月]\s*(3[01]|[12][0-9]|0[1-9]|[1-9])",
        text,
    )
    if m:
        d = _canon(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return d

    # 3) Compact YYYYMMDD inside 项目编号 / 检验编号 / 报告编号.
    m = _re.search(
        r"(?:项目编号|检验编号|报告编号|检查编号)[^0-9]*((?:19|20)\d{2})([0-1][0-9])([0-3][0-9])",
        text,
    )
    if m:
        d = _canon(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return d

    # 4) 标本编号: YYMMDD prefix in a long numeric code. Assume century=2000.
    m = _re.search(
        r"标本编号[:：\s]*([0-9]{2})([0-1][0-9])([0-3][0-9])[0-9]{4,}",
        text,
    )
    if m:
        d = _canon(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return d

    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", dest="output_dir")
    parser.add_argument("--analysis-output", dest="output_dir")
    parser.add_argument("--config", default=CONFIG_DEFAULT)
    parser.add_argument("--stop-after", choices=STOP_AFTER_CHOICES, default=None)
    parser.add_argument("--archives-root", default=ARCHIVES_DEFAULT)
    parser.add_argument("--person-id", default=PERSON_ID_DEFAULT)
    parser.add_argument("--answers", default=None)
    parser.add_argument(
        "--screening-gap-answers",
        default=None,
        help="independent CP5 screening-gap answers JSON; never merged into --answers",
    )
    parser.add_argument("--mineru-token", default=None)
    parser.add_argument("--use-demo-mineru-token", action="store_true")
    parser.add_argument("--save-mineru-token", default=None)
    parser.add_argument("--person-sex", choices=("male", "female"), default=None,
                        help="override the person sex used by every demographic-aware stage")
    parser.add_argument("--person-age", type=int, default=None,
                        help="override the person age used by every demographic-aware stage")
    parser.add_argument("--reuse-mineru-cache", action="store_true",
                        help="reuse an existing artifacts/conversion_manifest.json + "
                             "mineru/<data_id>/ tree instead of calling MinerU again. "
                             "Fails if no cached manifest is found.")
    parser.add_argument("--auto-apply-archive", action="store_true",
                        dest="auto_apply_archive",
                        help="automatically apply the archive proposal without agent confirmation. "
                             "Normally the orchestrator stops at archive-proposal for review.")
    args = parser.parse_args()

    if not args.output_dir:
        parser.error("either --output-dir or --analysis-output is required")

    # v0.1.4: sandbox-friendly archive root resolution. Default to cwd/output
    # (NOT the read-only skill package) so WorkBuddy/QwenPaw sandboxes can write.
    # CANCERRISK_OUTPUT_DIR env overrides (platform-injected). Explicit
    # --archives-root always wins.
    archives_root_explicit = bool(args.archives_root)
    args.archives_root = _resolve_archives_root(args.archives_root)

    # Production-safety warnings on default values. We don't block here so
    # tests / smoke runs stay frictionless, but the operator and audit log
    # both see the warning before downstream stages write archive entries.
    if not archives_root_explicit:
        print(
            f"[orchestrator] WARNING: --archives-root not provided; defaulting to "
            f"'{args.archives_root}' (cwd-relative for sandbox portability). "
            f"Pass --archives-root or set CANCERRISK_OUTPUT_DIR for explicit control.",
            file=sys.stderr,
        )
    if args.person_id == PERSON_ID_DEFAULT:
        print(
            f"[orchestrator] WARNING: --person-id not provided; defaulting to "
            f"'{PERSON_ID_DEFAULT}'. Production archives MUST use a stable, "
            f"unique person identifier.",
            file=sys.stderr,
        )

    out = Path(args.output_dir).resolve()

    artifacts = out / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    scripts = SKILL_ROOT / "scripts"

    config_path = Path(args.config)
    config = _load_config(config_path)
    audit_summary = {
        "stop_after": args.stop_after,
        "config_path": str(config_path),
        "input": args.input,
        "output_dir": str(out),
        "config_sections": sorted(config.keys()),
    }
    write_json(out / "module_audits" / "config_audit.json", audit_summary)
    print(f"[config] loaded {config_path} sections={audit_summary['config_sections']}")

    if args.stop_after == "config":
        print("[stop-after=config] early exit after config audit")
        return

    # --- v3 MinerU OCR stage ----------------------------------------------
    sys.path.insert(0, str(SKILL_ROOT / "scripts"))
    try:
        import mineru_client  # noqa: WPS433 — orchestrator-local import
    finally:
        # cleanly remove from sys.path to avoid leaking state in subprocess
        # callers; the import has already been cached in sys.modules.
        pass
    if args.save_mineru_token:
        local_path = mineru_client.save_local_mineru_token(config_path, args.save_mineru_token)
        write_json(out / "module_audits" / "mineru_config_audit.json", {
            "status": "configured",
            "token_source": "user",
            "local_config_path": str(local_path),
            "token_fingerprint": mineru_client.token_fingerprint(args.save_mineru_token),
        })
        print(f"[mineru] saved user token to {local_path}")
        return

    cached_manifest_path = artifacts / "conversion_manifest.json"

    # Auto-cache-reuse: when a successful manifest already exists for the
    # same analysis_output dir AND the input set hasn't changed, skip the
    # MinerU API call. Saves the 30-300s OCR round-trip on re-runs (the
    # exact failure mode that caused the v6 timeout report). Caller can
    # still force a fresh OCR by deleting the manifest.
    cache_auto_hit = False
    if not args.reuse_mineru_cache and cached_manifest_path.is_file():
        try:
            candidate = json.loads(cached_manifest_path.read_text("utf-8"))
            if candidate.get("status") == "success" and _manifest_matches_input(candidate, Path(args.input)):
                mineru_manifest = candidate
                cache_auto_hit = True
                print(
                    f"[mineru] auto cache hit at {cached_manifest_path} "
                    f"(files={len(mineru_manifest.get('files', []))}); "
                    "MinerU API not called. Delete the manifest to force a fresh OCR."
                )
        except Exception:
            cache_auto_hit = False  # fall through to live MinerU

    if args.reuse_mineru_cache:
        if not cached_manifest_path.is_file():
            print(
                f"[mineru] FAIL: --reuse-mineru-cache requested but no manifest "
                f"at {cached_manifest_path}. Run once without the flag first.",
                file=sys.stderr,
            )
            sys.exit(1)
        mineru_manifest = json.loads(cached_manifest_path.read_text("utf-8"))
        print(
            f"[mineru] REUSE cache from {cached_manifest_path} "
            f"(files={len(mineru_manifest.get('files', []))}); "
            "MinerU API not called."
        )
    elif cache_auto_hit:
        pass  # already loaded above
    else:
        try:
            mineru_manifest = mineru_client.run_mineru_stage(
                Path(args.input),
                artifacts,
                config_path,
                token_override=args.mineru_token,
                force_demo_token=args.use_demo_mineru_token,
            )
        except mineru_client.MinerUError as exc:
            write_json(out / "module_audits" / "mineru_audit.json", {
                "status": "fail",
                "reason": str(exc),
            })
            print(f"[mineru] FAIL: {exc}", file=sys.stderr)
            sys.exit(1)

    write_json(out / "module_audits" / "mineru_audit.json", {
        "status": mineru_manifest["status"],
        "files": [
            {k: v for k, v in r.items() if k != "outputs"} | {"outputs": r["outputs"]}
            for r in mineru_manifest["files"]
        ],
        "token_source": mineru_manifest["token_source"],
        "token_fingerprint": mineru_manifest["token_fingerprint"],
    })
    print(
        f"[mineru] status={mineru_manifest['status']} files={len(mineru_manifest['files'])} "
        f"token={mineru_manifest['token_source']} fingerprint={mineru_manifest['token_fingerprint']}"
    )

    if mineru_manifest["status"] == "fail":
        print("[mineru] every file failed — halting before downstream stages", file=sys.stderr)
        sys.exit(2)

    if args.stop_after == "mineru":
        print("[stop-after=mineru] early exit after MinerU OCR")
        return

    # --- v3 Demographics resolve (pre-Task4) -----------------------------
    # Age + sex drive the assertion template filter AND the prior probability
    # lookup. We resolve them now in a fixed priority order:
    #   1. CLI --person-sex/--person-age (operator override)
    #   2. Regex extraction from MinerU content.md
    #   3. Interactive answers (--answers payload)
    #   4. Halt — agent must complete demographics_questionnaire.json
    # config/formal.yaml::person_context is NOT silently consumed anymore;
    # use the CLI flags or the answers fixture instead.
    import demographics

    answers_for_demographics: dict = {}
    if args.answers:
        try:
            answers_for_demographics = json.loads(Path(args.answers).read_text(encoding="utf-8")).get("answers", {})
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[demographics] WARNING: cannot read --answers {args.answers}: {exc}", file=sys.stderr)

    demographics_resolution = demographics.resolve(
        cli_sex=args.person_sex,
        cli_age=args.person_age,
        mineru_root=artifacts / "mineru",
        manifest=mineru_manifest,
        answers=answers_for_demographics,
    )
    demographics_paths = demographics.write_artifacts(artifacts, demographics_resolution)

    if demographics_resolution["status"] != "resolved":
        questionnaire = demographics_resolution["questionnaire"]
        write_json(out / "module_audits" / "demographics_audit.json", {
            "status": "needs_interactive",
            "trace": demographics_resolution["trace"],
            "questionnaire_path": demographics_paths.get("demographics_questionnaire_json"),
            "open_questions": [q["question_id"] for q in questionnaire["questions"]],
        })
        print(
            "[demographics] FAIL: missing sex/age. Wrote "
            f"{demographics_paths.get('demographics_questionnaire_json')} with "
            f"{len(questionnaire['questions'])} mandatory question(s). "
            "Either re-run with --person-sex/--person-age, or add "
            "q_demographics_sex/q_demographics_age to your --answers JSON.",
            file=sys.stderr,
        )
        sys.exit(5)

    t4_sex = demographics_resolution["sex"]
    t4_age = demographics_resolution["age"]
    write_json(out / "module_audits" / "demographics_audit.json", {
        "status": "resolved",
        "sex": t4_sex,
        "age": t4_age,
        "source": demographics_resolution["source"],
        "trace": demographics_resolution["trace"],
        "evidence": demographics_resolution.get("evidence"),
    })
    print(f"[demographics] resolved sex={t4_sex} age={t4_age} source={demographics_resolution['source']}")

    if args.stop_after == "demographics":
        print("[stop-after=demographics] early exit after demographics resolve")
        return

    # --- v4 Refine checkpoint (NEW) --------------------------------------
    # Agent Checkpoint 1: agent must have written artifacts/mineru/<data_id>/refined.md
    # per data_id, distilled from content.md (人口学 + 异常指标 + 影像结论).
    mineru_root = artifacts / "mineru"
    mineru_sources = {
        record["data_id"]: record.get("source_path")
        for record in mineru_manifest.get("files", [])
    }
    data_id_list = [
        record["data_id"]
        for record in mineru_manifest.get("files", [])
        if (mineru_root / record["data_id"] / "content.md").exists()
    ]

    if args.stop_after == "refine":
        print(
            "[stop-after=refine] MinerU OCR ready. The skill agent must now follow "
            "SKILL.md 'Refine recipe' to write artifacts/mineru/<data_id>/refined.md "
            "(distilled: 人口学 + 异常指标 + 影像结论) for every data_id, then re-run "
            "the orchestrator with a later --stop-after."
        )
        return

    # CP1 is mandatory for every downstream stage. Only refined.md is accepted:
    # legacy refined_content.md may be a deterministic cleanup artifact and is
    # not a valid substitute for the agent-authored refine recipe.
    try:
        refined_paths = _collect_refined_paths(mineru_root, mineru_manifest)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(3)

    # --- v4 Build risk_factor_master + master template (deterministic) ---
    import master_scan
    import interactive_completion as interactive

    # v0.1.1 提前解析 evidence_version（缓存键需要，原在 L1097 才解析）
    _ev = None
    try:
        _ev = json.loads(
            (EVIDENCE_STORE / "evidence_version.json").read_text(encoding="utf-8")
        ).get("version")
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # v0.1.1 优化：subprocess → 直接 import；确定性输出 skip-if-exists（输入 sex+age+evidence_version
    # 未变则复用 artifacts 缓存，省 build_assertion_fill_template + build_master 每轮重算）
    assertion_template_path = artifacts / "risk_factor_assertion_template.json"
    master_path = artifacts / "risk_factor_master.json"
    _master_cache_key = f"{t4_sex}_{int(t4_age)}_{_ev}"
    _master_cache_sentinel = artifacts / ".master_cache_key"
    _cache_valid = (
        _master_cache_sentinel.is_file()
        and _master_cache_sentinel.read_text(encoding="utf-8").strip() == _master_cache_key
        and master_path.is_file()
    )
    if _cache_valid:
        print(f"[task4] cache hit: risk_factor_master.json (key={_master_cache_key})")
        master = json.loads(master_path.read_text(encoding="utf-8"))
    else:
        import build_assertion_fill_template as baft
        assertion_template = baft.build_assertion_fill_template(
            EVIDENCE_STORE, str(t4_sex), int(t4_age), None)
        write_json(assertion_template_path, assertion_template)
        derived_assertions = json.loads(
            (EVIDENCE_STORE / "risk_assertions_derived.json").read_text(encoding="utf-8")
        ) if (EVIDENCE_STORE / "risk_assertions_derived.json").is_file() else {"derived_assertions": []}
        master = master_scan.build_master_from_assertion_template(assertion_template, derived_assertions)
        master_scan.write_json(master_path, master)
        _master_cache_sentinel.write_text(_master_cache_key, encoding="utf-8")
        print(f"[task4] risk_factor_master.json factors={master['counts']['factors']} (cached key={_master_cache_key})")

    # --- v4 Task 5 (interactive) — yaml-driven, BEFORE Task4 fill -------
    answers_payload_raw = (
        json.loads(Path(args.answers).read_text(encoding="utf-8"))
        if args.answers else {}
    )
    answers_dict = answers_payload_raw.get("answers", answers_payload_raw) if isinstance(answers_payload_raw, dict) else {}
    demographics_snapshot = {"sex": t4_sex, "age": t4_age}
    fixed_questionnaire = interactive.build_fixed_questionnaire(
        config=config,
        demographics=demographics_snapshot,
        answers=answers_dict,
    )
    fixed_result = interactive.apply_fixed_answers(fixed_questionnaire, answers_payload_raw)
    interactive.write_fixed_outputs(
        output_dir=artifacts,
        questionnaire=fixed_questionnaire,
        result=fixed_result,
        interactive_cfg=config.get("interactive", {}),
    )
    print(
        f"[task5] mode=fixed questions={fixed_questionnaire['question_count']} "
        f"user_reported={len(fixed_result['user_reported_timeline']['records'])}"
    )

    if args.stop_after == "interactive":
        questionnaire_path = artifacts / config.get("interactive", {}).get(
            "questionnaire_output", "interactive_questionnaire.json"
        )
        n_answers = len(fixed_result["user_reported_timeline"]["records"])
        if n_answers == 0:
            print(
                "[stop-after=interactive] questionnaire written but NO answers "
                "collected yet. This is your cue to do AGENT CHECKPOINT 2:\n"
                f"  1. Read questions from: {questionnaire_path}\n"
                "  2. Ask the user every question via your channel\n"
                "     (AskUserQuestion / IM bot / web form / etc.)\n"
                "  3. Write a JSON file: {\"answers\": {\"q_demographics_sex\": ...}}\n"
                "  4. Re-run THIS command without --stop-after interactive and\n"
                "     WITH --answers <that-file> to continue the pipeline.\n"
                "DO NOT proceed to downstream stages without answers — the\n"
                "orchestrator will hard-halt at task5 with exit 8."
            )
        else:
            print(
                f"[stop-after=interactive] {n_answers} user_reported records "
                "ready. Re-run without --stop-after interactive (keep --answers) "
                "to continue to Task4 master fill."
            )
        return

    # HARD HALT when downstream stages would run with an empty interactive
    # timeline. The only valid earlier exit is --stop-after interactive
    # (already returned above). Re-run with --answers after CP2.
    if not fixed_result["user_reported_timeline"]["records"]:
        questionnaire_path = artifacts / config.get("interactive", {}).get(
            "questionnaire_output", "interactive_questionnaire.json"
        )
        sentinel = artifacts / "interactive_skipped.warning"
        sentinel.write_text(
            "INTERACTIVE_SKIPPED\n"
            f"run_at={datetime.now().isoformat()}\n"
            f"questions_offered={fixed_questionnaire['question_count']}\n"
            f"user_reported_records=0\n"
            "remediation=re-run with --answers <file> after collecting answers "
            "via interactive_questionnaire.json (Agent Checkpoint 2).\n",
            encoding="utf-8",
        )
        print(
            "[task5] HALT: interactive questionnaire was generated but the "
            "user_reported_timeline is EMPTY. The orchestrator will not "
            "proceed to downstream stages without lifestyle / family-history "
            "/ screening answers — that produces a misleading report.\n"
            "\n"
            "WHAT TO DO NEXT (Agent Checkpoint 2):\n"
            f"  1. Read the questionnaire: {questionnaire_path}\n"
            "  2. Ask the user every question via AskUserQuestion.\n"
            "  3. Write their answers to a JSON file (values from the user, NOT placeholders):\n"
            '       {"answers": {"q_demographics_sex": "<male|female — user said>",\n'
            '                    "q_demographics_age": <integer — user said>,\n'
            '                    "q_family_history_cancer": "<yes|no|unknown — user said>",\n'
            '                    "q_smoking_status": "<never|former|current|unknown — user said>",\n'
            '                    "q_alcohol_status": "<never|occasional|heavy|unknown — user said>", ...}}\n'
            "  4. Re-run with `--answers <that-file>`.\n"
            f"\nSentinel file: {sentinel}",
            file=sys.stderr,
        )
        sys.exit(8)

    # --- v4 Master-fill scaffold (Agent Checkpoint 3) -------------------
    source_md_files: list[master_scan.SourceMdEntry] = []
    refined_segments: list[str] = []
    for record in mineru_manifest.get("files", []):
        data_id = record["data_id"]
        refined = refined_paths.get(data_id)
        if not refined:
            continue
        # Try to read exam_date from the file's content.md (and fall back to
        # the refined.md) if not already known.
        exam_date = None
        content_md = mineru_root / data_id / "content.md"
        scan_sources: list[str] = []
        if content_md.is_file():
            scan_sources.append(content_md.read_text(encoding="utf-8")[:5000])
        if refined.is_file():
            scan_sources.append(refined.read_text(encoding="utf-8")[:5000])
        for text_head in scan_sources:
            exam_date = _extract_exam_date(text_head)
            if exam_date:
                break
        source_md_files.append(master_scan.SourceMdEntry(
            data_id=data_id,
            refined_md_path=str(refined.resolve()),
            exam_date=exam_date,
            source_path=record.get("source_path"),
        ))
        refined_segments.append(
            f"<!-- CANCERRISK_REFINED_SOURCE_START data_id={data_id} source_path={record.get('source_path')} exam_date={exam_date} -->\n"
            + refined.read_text(encoding="utf-8").rstrip()
            + f"\n<!-- CANCERRISK_REFINED_SOURCE_END data_id={data_id} -->\n"
        )

    # Append the interactive answers md as an additional source.
    interactive_md_path = artifacts / config.get("interactive", {}).get("interactive_answers_md", "interactive_answers.md")
    if interactive_md_path.is_file():
        source_md_files.append(master_scan.SourceMdEntry(
            data_id="interactive_answers",
            refined_md_path=str(interactive_md_path.resolve()),
            exam_date="now",
            source_path=None,
        ))
        refined_segments.append(
            "<!-- CANCERRISK_INTERACTIVE_ANSWERS_START -->\n"
            + interactive_md_path.read_text(encoding="utf-8").rstrip()
            + "\n<!-- CANCERRISK_INTERACTIVE_ANSWERS_END -->\n"
        )

    run_date = datetime.now().strftime("%Y-%m-%d")
    timeline_candidate_path = artifacts / "structured_risk_factors_timeline.candidate.json"
    if not timeline_candidate_path.is_file():
        timeline_scaffold = master_scan.build_timeline_scaffold(
            source_md_files=source_md_files,
            evidence_version=master.get("evidence_version"),
            run_date=run_date,
            master=master,
        )
        master_scan.write_json(timeline_candidate_path, timeline_scaffold)
    else:
        existing_candidate = json.loads(timeline_candidate_path.read_text(encoding="utf-8"))
        if "source_md_files" not in existing_candidate:
            existing_candidate["source_md_files"] = [
                dataclasses.asdict(e) for e in source_md_files
            ]
            master_scan.write_json(timeline_candidate_path, existing_candidate)

    # v7: imaging findings are now part of the timeline scaffold (same
    # candidate file as risk factors).  The agent fills them via
    # factor_key entries with factor_type="imaging_finding".

    # v6: scaffold tumor_markers.candidate.json (parallel to timeline).
    detection_derived_path = EVIDENCE_STORE / "detection_performance_derived.json"
    detection_derived = (
        json.loads(detection_derived_path.read_text(encoding="utf-8"))
        if detection_derived_path.is_file() else {"derived_detection_performance": []}
    )
    tumor_markers_candidate_path = artifacts / "tumor_markers.candidate.json"
    if not tumor_markers_candidate_path.is_file():
        tumor_markers_scaffold = master_scan.build_tumor_markers_scaffold(
            source_md_files=source_md_files,
            detection_derived=detection_derived,
            run_date=run_date,
        )
        master_scan.write_json(tumor_markers_candidate_path, tumor_markers_scaffold)

    if args.stop_after in ("assertion-template", "master-template"):
        print(
            f"[task4] master scaffold ready: risk_factor_master.json + "
            f"structured_risk_factors_timeline.candidate.json "
            f"(source_md_files={len(source_md_files)}). The skill agent must "
            f"now follow SKILL.md 'Master fill recipe (CP3)' to append records "
            f"(including imaging findings with factor_type='imaging_finding'), "
            f"then re-run with --stop-after cp3-verify."
        )
        return

    # --- CP3.1 Verification gate -----------------------------------------
    if args.stop_after == "cp3-verify":
        try:
            candidate = json.loads(timeline_candidate_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise SystemExit(
                f"[cp3-verify] candidate not found or invalid: {timeline_candidate_path}\n"
                f"  {exc}\n"
                f"  Complete CP3 (--stop-after master-template) before running cp3-verify."
            ) from exc
        if not master_scan.is_candidate_filled(candidate):
            raise SystemExit(
                "[cp3-verify] candidate.records is missing — CP3 fill has not been done.\n"
                "  Run with --stop-after master-template first, fill the candidate, "
                "then re-run with --stop-after cp3-verify."
            )
        prompt = master_scan.build_cp3_verify_prompt(candidate=candidate, master=master)
        print(prompt)
        return

    # --- CP3.1 audit result gate -----------------------------------------
    # The agent must write artifacts/cp3_audit_result.json after --stop-after
    # cp3-verify before the pipeline can proceed. See SKILL.md Checkpoint 3.1.
    cp3_audit_path = artifacts / "cp3_audit_result.json"
    if not cp3_audit_path.is_file():
        print(
            "[cp3.1] HALT: cp3_audit_result.json not found.\n"
            "Complete SKILL.md Checkpoint 3.1 (independent audit of every refined.md "
            "for omissions), then write the result file and re-run.\n"
            "Required format:\n"
            '  {"no_omissions": true}\n'
            "  or\n"
            '  {"no_omissions": false, "added_factor_keys": ["factor_key_1", ...]}',
            file=sys.stderr,
        )
        sys.exit(9)

    # --- v4 Gate the agent-filled timeline -------------------------------
    try:
        timeline_candidate = json.loads(timeline_candidate_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(
            f"[task4] cannot read timeline candidate: {timeline_candidate_path}\n"
            f"  {exc}\n"
            f"  Delete or repair the file, then re-run."
        ) from exc
    try:
        tumor_markers_candidate = json.loads(tumor_markers_candidate_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(
            f"[task4] cannot read tumor markers candidate: {tumor_markers_candidate_path}\n"
            f"  {exc}\n"
            f"  Delete or repair the file, then re-run."
        ) from exc
    _guard_master_fill_not_empty(
        artifacts=artifacts,
        timeline_candidate=timeline_candidate,
        tumor_markers_candidate=tumor_markers_candidate,
    )
    user_reported_path = artifacts / config.get("interactive", {}).get(
        "user_reported_timeline", "structured_risk_factors_timeline.user_reported.json"
    )
    user_reported_payload = (
        json.loads(user_reported_path.read_text(encoding="utf-8"))
        if user_reported_path.is_file() else {"records": []}
    )
    timeline = master_scan.gate_timeline_candidate(
        master=master,
        candidate=timeline_candidate,
        run_date=run_date,
        user_reported_payload=user_reported_payload,
        source_md_root=artifacts,
    )
    timeline_path = artifacts / "structured_risk_factors_timeline.json"
    master_scan.write_json(timeline_path, timeline)
    print(
        f"[task4] timeline records={len(timeline['records'])} "
        f"rejected={len(timeline['rejected_records'])}"
    )

    # H5: warn when gated timeline is empty yet refined.md carries abnormal
    # findings — a strong signal CP3 fill was skipped/incomplete for real
    # abnormalities. Pure-metabolic normal reports legitimately have an empty
    # timeline, so this is a warning, not a halt.
    if not timeline.get("records"):
        abnormal_re = re.compile(r"结节|息肉|肿块|阳性|↑|异常|占位|病变")
        hit_files = [
            str(p)
            for p in refined_paths.values()
            if p.is_file() and abnormal_re.search(p.read_text(encoding="utf-8"))
        ]
        if hit_files:
            print(
                "[task4] ⚠ timeline 空但 refined.md 含异常发现，可能漏填 CP3："
                + "; ".join(hit_files),
                file=sys.stderr,
            )

    # v7: imaging findings are now gated together with timeline records
    # inside gate_timeline_candidate (factor_type == "imaging_finding").

    # v6: gate the agent-filled tumor markers (P1).
    tumor_markers_gated = master_scan.gate_tumor_markers(
        detection_derived=detection_derived,
        candidate=tumor_markers_candidate,
        run_date=run_date,
        source_md_root=artifacts,
    )
    tumor_markers_gated_path = artifacts / "tumor_markers.json"
    master_scan.write_json(tumor_markers_gated_path, tumor_markers_gated)
    print(
        f"[task4] tumor markers tests={len(tumor_markers_gated['tests'])} "
        f"rejected={len(tumor_markers_gated['rejected_tests'])}"
    )

    # --- v4 Adapter: timeline → legacy merge_risk_factors inputs ---------
    import merge_risk_factors
    factor_events = master_scan.timeline_to_factor_events(timeline)
    # Project into structured/assertion_status shapes that downstream still expects.
    structured_payload = {
        "structured_risk_factors": [e for e in factor_events if e.get("exists") is True],
    }
    assertion_status_payload = {
        "assertion_events": factor_events,
        "assertion_missing_by_template": [],
    }
    supplemental_updates_payload = {"updates": []}  # Task5 answers already in timeline
    # v6 P1: feed gated tumor markers into the screening_tests channel.
    # snapshot_risk._screening_contribution joins by test_id and converts
    # LR+/LR- → log_odds_delta.
    tumor_marker_tests = [
        {
            "test_id": t["test_id"],
            "test_name": t["test_name"],
            "result": t["result"],
            "exam_date": t["exam_date"],
            "source_data_id": t["source_data_id"],
            "evidence_text": t["evidence_text"],
            # tumor markers are single-cancer tests, but the existing
            # snapshot path expects top_cancers for positive results
            # (designed for jizaoan). For tumor markers, every entry
            # already has a single cancer_id mapped in the derived
            # store, so we list it as the only "top" so the gate logic
            # passes through without changes.
            "top_cancers": [
                d["cancer_id"]
                for d in detection_derived.get("derived_detection_performance", [])
                if d["test_id"] == t["test_id"]
            ],
        }
        for t in tumor_markers_gated.get("tests", [])
    ]
    # Merge jizaoan from interactive answers; skip if already present in tumor markers
    existing_test_ids = {t["test_id"] for t in tumor_marker_tests}
    interactive_screening = [
        t for t in fixed_result.get("screening_tests", [])
        if t.get("test_id") not in existing_test_ids
    ]
    supplemental_factors_payload = {
        "supplemental_risk_factors": [],
        "screening_tests": tumor_marker_tests + interactive_screening,
        "uncertainties": [],
    }
    write_json(artifacts / "structured_risk_factors.json", structured_payload)
    write_json(artifacts / "needs_confirmation_factors.json", {"needs_confirmation_factors": []})
    write_json(artifacts / "risk_factor_assertion_status.json", assertion_status_payload)
    write_json(artifacts / "risk_factor_extraction_status.json", {"status": "gated", "data_ids": [e.data_id for e in source_md_files]})
    write_json(artifacts / "supplemental_risk_factor_updates.json", supplemental_updates_payload)
    write_json(artifacts / "supplemental_risk_factors.json", supplemental_factors_payload)

    merged = merge_risk_factors.merge_payloads(
        structured=structured_payload,
        assertion_status=assertion_status_payload,
        supplemental_updates=supplemental_updates_payload,
        supplemental_factors=supplemental_factors_payload,
    )
    write_json(artifacts / "merged_risk_factors.json", merged)

    (artifacts / "refined_content_bundle.md").write_text(
        "\n".join(refined_segments),
        encoding="utf-8",
    )

    task5_audit = (
        "# Task05 Interactive Completion Audit (v4 fixed)\n\n"
        f"- questions: {fixed_questionnaire['question_count']}\n"
        f"- user_reported timeline records: {len(fixed_result['user_reported_timeline']['records'])}\n"
        f"- demographics_updates: {fixed_result['demographics_updates']}\n"
    )
    (out / "module_audits" / "task05_interactive_completion.md").parent.mkdir(parents=True, exist_ok=True)
    (out / "module_audits" / "task05_interactive_completion.md").write_text(task5_audit, encoding="utf-8")

    if args.stop_after == "risk-factor-gate":
        print("[stop-after=risk-factor-gate] early exit after timeline gate")
        return

    # --- v3 Task 6 phase A: call cyzh-cfc and scaffold structured summary --
    contact_config_path = SKILL_ROOT / "config" / "contact.json"
    contact_path_arg = contact_config_path if contact_config_path.is_file() else None
    try:
        api_phase = run_health_summary_api_stage(
            out=out,
            artifacts=artifacts,
            config_path=config_path,
            contact_config_path=contact_path_arg,
        )
        if api_phase.get("skipped"):
            print(
                f"[task6][api] skipped (CP4 already done); structured_summary="
                f"{api_phase.get('structured_summary_json')} status={api_phase['status']}"
            )
        else:
            print(
                f"[task6][api] response={api_phase['health_summary_outputs']['api_response_md']} "
                f"structured_summary={api_phase['health_summary_outputs']['structured_summary_json']} "
                f"status={api_phase['status']}"
            )
    except RuntimeError as exc:
        import render_health_summary as _rhs
        print(f"[task6][api] WARN: {exc}; staging api_unavailable_fallback structured summary")
        structured_path = artifacts / "health_summary_structured_summary.json"
        if not structured_path.is_file():
            fallback = _rhs.build_api_fallback_summary(artifacts / "refined_content_bundle.md")
            structured_path.write_text(
                json.dumps(fallback, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        api_phase = {"skipped": True, "status": "api_unavailable_fallback"}

    if api_phase.get("status") == "api_unavailable_fallback":
        print(
            "\n[task6] HALT: health-summary API unavailable. "
            "The structured summary has been staged as 'api_unavailable_fallback'. "
            "To proceed, EITHER:\n"
            "  1. Fix the API issue (check token, network, upstream service) and re-run, OR\n"
            "  2. Manually fill artifacts/health_summary_structured_summary.json "
            "     following SKILL.md 'Task6 structuring recipe', set status to 'ready_for_render', "
            "     then re-run the orchestrator.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.stop_after == "health-summary-api":
        print(
            "[stop-after=health-summary-api] API phase done. The skill agent must "
            "now follow SKILL.md 'Task6 structuring recipe' to fill "
            "artifacts/health_summary_structured_summary.json, then re-run the "
            "orchestrator (without --stop-after, or with --stop-after report)."
        )
        return

    # --- v3 Task 7: snapshot cancer risk ---------------------------------
    import snapshot_risk

    snapshot = snapshot_risk.run_snapshot_stage(
        artifacts=artifacts,
        evidence_store=EVIDENCE_STORE,
        config_path=config_path,
        person_sex=t4_sex,
        person_age=int(t4_age),
    )

    safety_cfg = config.get("safety", {}) if isinstance(config, dict) else {}
    disclaimer = str(safety_cfg.get("disclaimer") or "本报告仅用于健康管理参考。")
    evidence_version = None
    try:
        evidence_version = json.loads(
            (EVIDENCE_STORE / "evidence_version.json").read_text(encoding="utf-8")
        ).get("version")
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    task7_audit = (
        "# Task07 Snapshot Risk Audit\n\n"
        f"- evidence_version: {evidence_version}\n"
        f"- person: sex={t4_sex} age={t4_age}\n"
        f"- cancers_total: {len(snapshot['cancers'])}\n"
        f"- cancers_scored: {sum(1 for r in snapshot['cancers'] if r['posterior_probability'] is not None)}\n"
        f"- missing_prior: {snapshot['uncertainties_summary']['cancers_missing_prior']}\n"
        f"- not_applicable: {snapshot['uncertainties_summary']['cancers_not_applicable']}\n"
        f"- section4_count: {len(snapshot['section4_screening'])}\n"
    )
    (out / "module_audits" / "task07_snapshot_risk.md").write_text(task7_audit, encoding="utf-8")
    print(
        f"[task7] snapshot scored={sum(1 for r in snapshot['cancers'] if r['posterior_probability'] is not None)}"
        f" section4={len(snapshot['section4_screening'])}"
    )

    if args.stop_after == "screening-gap":
        print(
            "[stop-after=screening-gap] snapshot and health summary are ready. "
            "Complete independent CP5 using LLM + knowledge base: write "
            "artifacts/screening_recommendations.json (A cancer_risk / B other_abnormalities "
            "/ C periodic_management + excluded_done_normal). "
            "Read periodic_screening_schedule.json (age/gender→应筛+间隔), ask gap "
            "questions separately from CP2, record answers inline. "
            "Scripts validate only core logic (dedup / done+normal / medium+)."
        )
        return

    try:
        screening_gap_summary = _validate_screening_gap_stage(
            artifacts=artifacts,
            knowledge_root=SKILL_ROOT / "references" / "database",
            screening_gap_answers_path=None,  # v2.0.4: answers 内嵌在 recommendations 里，不再独立文件
        )
    except ScreeningGapGateError as exc:
        print(
            f"[cp5] HALT(exit {exc.code}): " + " | ".join(exc.errors),
            file=sys.stderr,
        )
        sys.exit(exc.code)

    (out / "module_audits" / "task_cp5_screening_gap.md").write_text(
        "# CP5 Screening Gap Audit\n\n"
        f"- cancer_risk(A): {screening_gap_summary.get('cancer_risk_count', 0)}\n"
        f"- other_abnormalities(B): {screening_gap_summary.get('other_abnormalities_count', 0)}\n"
        f"- periodic_management(C): {screening_gap_summary.get('periodic_management_count', 0)}\n"
        "- decision_engine: LLM + knowledge base\n"
        "- script_role: PUA validation only (3 rules: dedup / done+normal / medium+)\n",
        encoding="utf-8",
    )

    # --- v4 Task 8a: archive proposal + baseline snapshot ---------------
    import archive_manager

    archives_root = Path(args.archives_root)

    # Enforce --person-id when archives_root is already populated with
    # non-default person directories. Prevents cross-person contamination.
    archive_cfg = config.get("archive", {}) if isinstance(config, dict) else {}
    if (
        archive_cfg.get("strict_person_id_when_populated", True)
        and args.person_id == PERSON_ID_DEFAULT
        and archive_manager.has_existing_person_archives(archives_root, person_id_default=PERSON_ID_DEFAULT)
    ):
        print(
            "[task8] FAIL: archives_root="
            f"{archives_root} already contains real person directories, but "
            "--person-id was not provided. Re-run with --person-id <id> to "
            "avoid contaminating an existing person's archive.",
            file=sys.stderr,
        )
        sys.exit(6)

    # Try to resolve a richer display_name from the refined.md (used for
    # the person_index.json mapping).
    refined_paths_list: list[Path] = []
    if mineru_root.exists():
        for record in mineru_manifest.get("files", []):
            data_id = record.get("data_id")
            if not data_id:
                continue
            candidate = mineru_root / data_id / "refined.md"
            if not candidate.is_file():
                candidate = mineru_root / data_id / "refined_content.md"
            if candidate.is_file():
                refined_paths_list.append(candidate)
    person_resolution = interactive.resolve_person_id_interactively(
        refined_md_paths=refined_paths_list,
        cli_person_id=args.person_id,
        person_id_default=PERSON_ID_DEFAULT,
        archives_root=archives_root,
        person_index_path=archives_root / archive_cfg.get("person_index_filename", "person_index.json"),
        operator_choice=(
            (answers_payload_raw.get("answers", {}) if isinstance(answers_payload_raw, dict) else {})
            .get("person_id_choice")
        ),
    )
    if person_resolution["status"] == "needs_confirmation":
        # Emit a halt artifact so the agent can prompt the user, then re-run.
        write_json(artifacts / "archive_person_id_prompt.json", person_resolution)
        print(
            "[task8] needs confirmation — refined.md contains candidate name(s); "
            "the skill agent must confirm which person this run belongs to. "
            f"Prompt: {person_resolution.get('prompt')}",
            file=sys.stderr,
        )
        sys.exit(7)
    resolved_person_id = person_resolution.get("person_id") or args.person_id
    resolved_display_name = person_resolution.get("display_name") or resolved_person_id

    # --- v4 Task 8c: archive proposal/apply ----------------------------
    archive_result = archive_manager.run_archive_stage(
        artifacts=artifacts,
        archives_root=archives_root,
        person_id=resolved_person_id,
        auto_apply=True,
        display_name=resolved_display_name,
        run_date=run_date,
        snapshots_subdir=archive_cfg.get("baseline_snapshot_dir", "snapshots"),
        person_index_filename=archive_cfg.get("person_index_filename", "person_index.json"),
    )
    print(
        f"[task8] archive proposal events={archive_result['factor_events_added']}"
        f" screenings={archive_result['screening_events_added']}"
        f" applied={archive_result['applied']}"
    )
    (out / "module_audits" / "task08_archive_proposal.md").write_text(
        "# Task08 Archive Proposal Audit\n\n"
        f"- proposal_path: {archive_result['proposal_path']}\n"
        f"- factor_events_added: {archive_result['factor_events_added']}\n"
        f"- screening_events_added: {archive_result['screening_events_added']}\n"
        f"- applied: {archive_result['applied']}\n"
        f"- mode: auto\n",
        encoding="utf-8",
    )

    if args.stop_after == "archive-proposal":
        print(
            "[stop-after=archive-proposal] archive_update_proposal.json written and "
            "auto-applied to output."
        )
        return

    if args.stop_after == "archive":
        print("[stop-after=archive] archive auto-applied")
        return

    if args.stop_after == "report-artifacts":
        print(
            "[stop-after=report-artifacts] CP5/snapshot/archive done. The skill "
            "agent must now produce the 5 section artifacts by 读取干净知识 JSON "
            "(`references/database/screening_personalized/json/cancer_followup_rules.json` "
            "查癌种复查方法/周期) + `异常指标复查推荐.md`(异常→复查，覆盖乳腺/妇科/心电等任意异常) "
            "+ snapshot 后验 + health_summary 异常 + pricing，**LLM 提取异常+推荐筛查+写文案**；"
            "数值字段（sens/spec/price/posterior）留空由下游脚本兜底。然后不带 --stop-after 重跑渲染 report.html：\n"
            "  timeline_tiers.json / x_addons.json / package_tiers.json / "
            "liquid_biopsy_perf.json / long_term_intervention.json\n"
            "  (into <out>/artifacts/)."
        )
        return

    try:
        _validate_screening_report_artifacts(artifacts=artifacts)
    except ScreeningGapGateError as exc:
        print(
            f"[cp5-report] HALT(exit {exc.code}): " + " | ".join(exc.errors),
            file=sys.stderr,
        )
        sys.exit(exc.code)

    # --- P1 Task 9: single integrated report ----------------------------
    # tumor_markers.json is already materialized above (master_scan.gate_tumor_markers
    # writes it before any early exit on the path to report), so no extra copy here.
    import build_report_json
    import render_report
    import write_manifest

    # v0.1.3: deterministically compute package prices (Σmid) BEFORE assembling
    # the report. assemble_package.py was previously an agent-only manual step
    # (never invoked by the orchestrator), so skipping it left
    # package_tiers[].price_range as unreliable LLM-hand-filled values — a PUA
    # hole. Wired in so prices are always script-derived and reproducible.
    import assemble_package
    package_pricing = assemble_package.load_pricing(SKILL_ROOT)
    package_tiers_path = artifacts / "package_tiers.json"
    if package_pricing is not None and package_tiers_path.is_file():
        try:
            pkg_tiers = assemble_package.assemble_package(package_tiers_path, package_pricing)
            print(f"[report] package prices assembled (Σmid) -> {package_tiers_path.name}")
            if isinstance(pkg_tiers, list):
                rec_count = sum(1 for t in pkg_tiers if t.get("recommended") is True)
                if len(pkg_tiers) != 3:
                    print(
                        f"[report] ⚠ package_tiers {len(pkg_tiers)} 档（temp 模版期望恒 3 档）",
                        file=sys.stderr,
                    )
                if rec_count != 1:
                    print(
                        f"[report] ⚠ package_tiers recommended=True {rec_count} 档（期望恰好 1 档推荐）",
                        file=sys.stderr,
                    )
        except Exception as exc:  # price calc must never block an otherwise-valid report
            print(f"[report] ⚠ assemble_package skipped ({exc})", file=sys.stderr)

    run_id = datetime.now().strftime("run-%Y%m%d-%H%M%S")
    report = build_report_json.assemble_report_json(
        artifacts=artifacts,
        out=out,
        answers_path=Path(args.answers) if args.answers else None,
        person_id=resolved_person_id,
        run_id=run_id,
        evidence_version=evidence_version,
    )
    print(
        f"[report] report.json assembled run_id={run_id} "
        f"cancers={len(report['snapshot']['cancers'])} "
        f"tumor_markers={len(report['tumor_markers'])}"
    )

    report_html_path = render_report.write_report_html(
        report, SKILL_ROOT / "templates" / "integrated_report_temp.html", disclaimer, out
    )
    print(f"[report] report.html rendered -> {report_html_path}")
    if report.get("sections_incomplete"):
        print(
            "[report] HALT(exit 10): 报告核心 section 空（5 section artifact 未产）。"
            "report.html 已生成但为空壳——请完成 SKILL.md 第6步 report-artifacts "
            "（--stop-after report-artifacts 产 5 JSON）后重跑。",
            file=sys.stderr,
        )
        sys.exit(10)

    (out / "module_audits" / "task_p1_report.md").write_text(
        "# P1 Report\n\n"
        f"- report.json: {out / 'artifacts' / 'report.json'}\n"
        f"- report.html: {report_html_path}\n"
        f"- run_id: {run_id}\n"
        f"- cancers: {len(report['snapshot']['cancers'])}\n"
        f"- tumor_markers: {len(report['tumor_markers'])}\n",
        encoding="utf-8",
    )

    manifest = write_manifest.build_manifest(
        output_dir=out,
        artifacts=artifacts,
        evidence_store=EVIDENCE_STORE,
        archives_root=archives_root,
        person_id=resolved_person_id,
        run_id=run_id,
        input_path=args.input,
    )
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[manifest] status={manifest['status']} -> {out / 'manifest.json'}")

    if args.stop_after == "report":
        print("[stop-after=report] report.json assembled")
        return


if __name__ == "__main__":
    main()

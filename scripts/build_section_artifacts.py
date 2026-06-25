#!/usr/bin/env python3
"""v2.0.0 确定性骨架生成器（性能优化核心）。

在 ``--stop-after report-artifacts`` 阶段**先于 agent** 产出 5 个 section artifact
**骨架**：数值/分类/结构脚本算（PUA），文案字段（rationale / note / clinical_hint）
留 ``""`` 并标 ``_pending``，agent 只补文案——把 agent 产 5 artifact 的 ~5m 砍到 <2m。

数据源（全部确定性，不查 LLM、不编造）：
- snapshot_risk.json  → cancers[].posterior / section4_screening / jizaoan_whatif / person_context
- voi_ranking.json    → 吉早安 sens/spec
- answers.json        → BRCA / 吉早安结果 / 筛查缺口
- health_summary_structured_summary.json → risk_level（严重度）
- pricing/json/08_pricing.json → 价格 mid
- screening_personalized/json/cancer_followup_rules.json → 癌×风险档→复查（编译自复查规则 MD）

PUA 边界：sens/spec/price/posterior/tier/months/method/includes/recommended 脚本算；
rationale/note/clinical_hint/lifestyle 措辞一律留空 + _pending，**绝不生成医学文案**。
"""
from __future__ import annotations

import sys as _sys
_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import _env_bootstrap  # noqa: F401 — 跨runtime环境自检(PYTHONHOME/UTF-8)

import argparse
import json
import re
from pathlib import Path
from typing import Any

SKILL_ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = SKILL_ROOT / "references" / "database" / "screening_personalized" / "json" / "cancer_followup_rules.json"
ABNORMAL_MAP_PATH = SKILL_ROOT / "references" / "database" / "screening_personalized" / "json" / "abnormal_followup_map.json"
PRICING_PATH = SKILL_ROOT / "references" / "database" / "pricing" / "json" / "08_pricing.json"

# 筛查缺口题 → 屏幕展示项名（maintain 档来源）
_GAP_QUESTIONS = {
    "q_psa_screening": "PSA（前列腺特异性抗原）筛查",
    "q_colorectal_screening": "结直肠癌筛查（肠镜 / FIT）",
    "q_cervical_screening": "宫颈癌筛查（HPV / TCT）",
    "q_mammography_screening": "乳腺癌筛查（乳腺 X 线 / 超声）",
}
# 通用生活方式模板（lifestyle 档兜底，agent 按个体微调）
_LIFESTYLE_TEMPLATE = [
    "戒烟限酒（如适用）",
    "维持 BMI 在 18.5-24 正常范围",
    "每周 ≥150 分钟中等强度有氧运动",
    "均衡饮食：限红肉(<500g/周)、增蔬果、控盐控糖",
    "保证睡眠（成人 7-9 小时）",
]
# BRCA 阳性遗传管理骨架条目（agent 按基因型细化）
_GENETIC_BRCA_SKELETON = [
    "乳腺 MRI 每年（30 岁起，与钼靶交替）",
    "乳腺超声 + 钼靶每 6-12 月",
    "CA-125 + 经阴道超声（卵巢监测，讨论预防性手术时机）",
]


def _load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default
    except (json.JSONDecodeError, OSError):
        return default


def _unwrap_answers(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw.get("answers", raw) if "answers" in raw else raw
    return {}


def _risk_tier_5(posterior: float | None, rules: dict) -> str:
    """从后验算 5 档风险（very_low..very_high），对齐 cancer_followup_rules.risk_tiers。"""
    if posterior is None:
        return "very_low"
    for tier in ("very_low", "low", "medium", "high", "very_high"):
        if posterior <= rules["risk_tiers"][tier]["max_posterior"]:
            return tier
    return "very_high"


def _health_severe(health: dict) -> str:
    """health_summary.assessment_result.risk_level → 'severe'/'medium'/'none'。"""
    rl = str((health.get("assessment_result") or {}).get("risk_level") or "").lower() if isinstance(health, dict) else ""
    if any(k in rl for k in ("严重", "🔴", "high", "very_high")):
        return "severe"
    if any(k in rl for k in ("高", "🟠")):
        return "severe"
    if any(k in rl for k in ("中", "🟡", "medium", "potential")):
        return "medium"
    return "none"


def _rating_to_tier(rating: str, abnormal_map: dict) -> str:
    """风险评级文案（高风险/中度/低/🟠...）→ timeline 三档（priority/important/maintain）。"""
    r2t = abnormal_map.get("risk_rating_to_tier", {})
    for k, v in r2t.items():
        if k in rating:
            return v
    return ""


def _match_abnormal(text: str, abnormal_map: dict) -> dict | None:
    """文本关键词匹配 abnormal_followup_map.mappings → 命中的映射（含 followup/price）。"""
    low = text.lower()
    for m in abnormal_map.get("mappings", []):
        if any(kw.lower() in low for kw in m.get("keywords", [])):
            return m
    return None


def _extract_abnormals(health: dict, abnormal_map: dict) -> list[dict]:
    """从 health_summary 抽取异常项 + 风险评级，关键词匹配 abnormal_followup_map。
    数据源：abnormal_table（HTML 表，最结构化）+ core_risk_factors（文本兜底）。
    返回 [{indicator, tier, followup, price_hint, matched}]——finding-driven 骨架来源，
    让「异常驱动」画像（无癌后验）也能拿到非空 timeline/x_addons。"""
    if not isinstance(health, dict) or not abnormal_map:
        return []
    ar = health.get("assessment_result") or {}
    out: list[dict] = []
    seen: set[str] = set()

    def _add(indicator: str, rating: str, matched: dict | None) -> None:
        # 去重键：命中 map 则用 map 的 indicator（让 table 与 core_risk_factors 的同一异常合并），
        # 否则用原始 indicator 文本。
        key = (matched["indicator"] if matched else indicator).strip()
        if not key or key in seen:
            return
        seen.add(key)
        display = (matched["indicator"] if matched else indicator).strip()
        tier = _rating_to_tier(rating, abnormal_map) or (matched.get("default_tier") if matched else "") or "important"
        out.append({
            "indicator": display, "tier": tier, "rating": rating,
            "followup": matched.get("followup", "") if matched else "",
            "price_hint": matched.get("price_hint", "") if matched else "",
            "matched": bool(matched),
        })

    # 1. abnormal_table HTML 行（指标/结果/范围/偏离/风险评级）
    html = str(ar.get("abnormal_table") or "")
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if not cells:
            continue
        indicator = cells[0]
        rating = cells[4] if len(cells) >= 5 else ""
        _add(indicator, rating, _match_abnormal(indicator + " " + " ".join(cells), abnormal_map))

    # 2. core_risk_factors 文本兜底（table 没覆盖的异常）
    for seg in re.split(r"[；;。\n]", str(ar.get("core_risk_factors") or "")):
        seg = seg.strip()
        if not seg:
            continue
        rating = "高风险" if "高风险" in seg else ("中度风险" if "中度" in seg else ("低风险" if "低风险" in seg else ""))
        matched = _match_abnormal(seg, abnormal_map)
        ind = matched["indicator"] if matched else (seg.split("（")[0][:14] or seg[:14])
        _add(ind, rating, matched)

    return out


_TIER_INTERVAL = {"priority": "1-2 周内", "important": "1 个月内", "maintain": "3-6 个月内"}
_TIER_TAG = {"priority": ("danger", "高风险"), "important": ("warning", "中等风险"), "maintain": ("info", "关注")}


# ---------- 5 artifact 骨架 ----------

def build_timeline(snapshot: dict, answers: dict, health: dict, rules: dict, abnormal_map: dict | None = None) -> dict:
    """timeline_tiers 骨架：① 癌后验→三档（priority>1%/important 0.5-1%）+ ② 异常驱动项
    （health_summary 异常→abnormal_followup_map→复查，让「异常驱动」画像也有非空骨架）+
    ③ 筛查缺口→maintain。rationale 留空（LLM）。均匀机制：标 _imbalance_flag。"""
    section4 = [s for s in snapshot.get("section4_screening", []) if isinstance(s, dict)]
    priority, important = [], []

    # ① 癌后验驱动
    for s in section4:
        cid = s.get("cancer_id")
        post = s.get("posterior_probability")
        name = s.get("cancer_name_zh", cid)
        t5 = _risk_tier_5(post, rules)
        followup = (rules.get("cancers", {}).get(cid, {}) or {}).get("tier_followup", {}).get(t5) or {}
        method = followup.get("method") or (s.get("standard_screening") or [{}])[0].get("method", "复查")
        months = followup.get("months")
        interval = f"{months}月内" if months else "按指南复查"
        item = {"item_name": f"{method}（{name}）", "interval": interval, "rationale": "", "cancer_id": cid}
        if post is not None and post > 0.01:
            priority.append(item)
        elif post is not None and 0.005 <= post <= 0.01:
            important.append(item)

    # ② 异常驱动（finding-driven）：health_summary 异常 → 复查项，按风险评级分档
    abnormals = _extract_abnormals(health, abnormal_map or {})
    for ab in abnormals:
        method = ab["followup"] or f"{ab['indicator']}复查"
        item = {"item_name": method, "interval": _TIER_INTERVAL.get(ab["tier"], "1 个月内"),
                "rationale": "", "_source": "abnormal"}
        if ab["tier"] == "priority":
            priority.append(item)
        elif ab["tier"] == "important":
            important.append(item)

    # maintain: 筛查缺口 + 异常驱动的 maintain 档
    maintain = []
    for qid, label in _GAP_QUESTIONS.items():
        if str(answers.get(qid, "")).lower() in ("no", "没做过"):
            maintain.append({"item_name": label, "interval": _TIER_INTERVAL["maintain"], "rationale": ""})
    for ab in abnormals:
        if ab["tier"] == "maintain":
            maintain.append({"item_name": ab["followup"] or f"{ab['indicator']}复查",
                             "interval": _TIER_INTERVAL["maintain"], "rationale": "", "_source": "abnormal"})

    imbalance = (len(priority) == 0) != (len(important) == 0) and (priority or important)
    return {
        "priority": priority, "important": important, "maintain": maintain,
        "_scaffold": True,
        "_imbalance_flag": bool(imbalance),
        "_pending": ["每条 rationale 文案（结合个体后验/严重度措辞）"] + (["priority/important 失衡，按双优先级重排"] if imbalance else []),
    }


def build_liquid_biopsy(snapshot: dict, voi: dict, pricing: dict) -> dict:
    """liquid_biopsy_perf 骨架：sens/spec（voi 兜底 81.9/99.0）+ market_price（pricing）+
    negative_risk_reduction（snapshot.jizaoan_whatif 数值）。clinical_hint 留空（LLM）。"""
    # sens/spec from detection_performance overall / voi 吉早安行（与 build_report_json 一致）
    sens = spec = None
    for r in voi.get("rankings", []):
        if isinstance(r, dict) and ("jizaoan" in str(r.get("method", "")).lower() or "吉早安" in str(r.get("method", ""))):
            sens = r.get("sensitivity"); spec = r.get("specificity"); break
    if sens is None:
        sens, spec = 0.819, 0.990
    sens_pct = f"{round(float(sens) * 100, 1)}%" if sens is not None else "81.9%"
    spec_pct = f"{round(float(spec) * 100, 1)}%" if spec is not None else "99.0%"

    jz = pricing.get("items", {}).get("jizaoan", {})
    mid = jz.get("mid"); low = jz.get("low"); high = jz.get("high")
    market = f"{low}-{high}元" if low and high else (f"{mid}元" if mid else "")

    # negative_risk_reduction: 取 jizaoan_whatif 中 current_risk 最大的癌，算降幅
    whatif = [w for w in snapshot.get("jizaoan_whatif", []) if isinstance(w, dict)] if isinstance(snapshot.get("jizaoan_whatif"), list) else []
    nrr = ""
    if whatif:
        top = max(whatif, key=lambda w: w.get("current_risk") or 0)
        cur, neg = top.get("current_risk"), top.get("risk_if_negative")
        if cur and neg is not None and cur > 0:
            nrr = f"{top.get('cancer_name_zh','')}后验可从 {round(cur*100,2)}% 降至 {round(neg*100,2)}%（吉早安阴性情境）"

    return {
        "sensitivity": sens_pct, "specificity": spec_pct,
        "market_price_range": market, "negative_risk_reduction": nrr,
        "clinical_hint": "", "early_stage_sensitivity": "",
        "_scaffold": True, "_pending": ["clinical_hint 文案（阳性溯源/阴性降级路径，读 05-液检 MD）"],
    }


def build_long_term(answers: dict, brca_status: str) -> dict:
    """long_term_intervention 骨架：BRCA 阳性→genetic_management 骨架；lifestyle 通用模板。"""
    genetic = list(_GENETIC_BRCA_SKELETON) if brca_status == "positive" else []
    return {
        "genetic_management": genetic,
        "lifestyle": list(_LIFESTYLE_TEMPLATE),
        "_scaffold": True,
        "_pending": (["genetic_management 按具体 BRCA1/2 基因型细化"] if brca_status == "positive" else [])
                  + ["lifestyle 按个体主要癌种/异常微调"],
    }


def build_package(snapshot: dict, answers: dict, person: dict, pricing: dict, rules: dict) -> list:
    """package_tiers 骨架：3 档 includes（section4 standard_screening 方法 + basic_panel + 缺口）
    + recommended（套餐 MD 风险驱动规则）。price 由 assemble_package 后续 Σmid 求和。note 留空。"""
    section4 = [s for s in snapshot.get("section4_screening", []) if isinstance(s, dict)]
    # 收集中风险+ 癌的标准筛查方法名（去重）
    def _methods(s):
        return [m.get("method", "") for m in (s.get("standard_screening") or []) if isinstance(m, dict) and m.get("method")]
    high_methods, med_methods = [], []
    any_high = False
    for s in section4:
        post = s.get("posterior_probability") or 0
        t5 = _risk_tier_5(post, rules)
        ms = _methods(s)
        if post > 0.02 or t5 in ("high", "very_high"):
            any_high = True
            high_methods += ms
        elif post >= 0.005:
            med_methods += ms
    gaps = [label for qid, label in _GAP_QUESTIONS.items() if str(answers.get(qid, "")).lower() in ("no", "没做过")]

    tier1_includes = (high_methods + med_methods)[:5] or ["基础体检套餐"]
    tier2_includes = list(dict.fromkeys(high_methods + med_methods + gaps + ["基础体检套餐"]))
    # 档3：吉早安替换——未被替代项（非 8 癌专项保留）+ 全量
    tier3_includes = [m for m in tier2_includes]  # 简化：未被替代项 = tier2（agent 标注哪些被吉早安替代）
    tier3_includes_all = list(tier2_includes)

    # recommended：档2（全面覆盖）是默认推荐；档3（吉早安档）仅在吉早安阳性时推荐
    # （否则推荐一个用户未做的吉早安档不合理）；全低风险+年轻+无家族史→档1。
    # agent 可按个体在 note 里改推荐（scaffold 只给确定性默认）。
    age = person.get("age")
    genetic = str(answers.get("q_has_genetic_mutation", "")).lower() == "yes"
    family = str(answers.get("q_family_history_cancer", "")).lower() == "yes"
    jizaoan_pos = str(answers.get("q_jizaoan_result", "")).lower() == "positive"
    if jizaoan_pos:
        rec = 2  # 档3（吉早安阳性→吉早安档）
    elif med_methods or any_high or (age and age >= 45) or family or gaps or genetic:
        rec = 1  # 档2（默认推荐，覆盖多数中/高风险）
    else:
        rec = 0  # 档1

    tiers = [
        {"name": "风险靶向聚合档", "price_range": "", "includes": tier1_includes, "note": "", "recommended": rec == 0, "_scaffold": True},
        {"name": "全面覆盖档", "price_range": "", "includes": tier2_includes, "note": "", "recommended": rec == 1, "_scaffold": True},
        {"name": "吉早安替换/弥补档", "price_range": "", "includes": tier3_includes, "includes_all": tier3_includes_all, "note": "", "recommended": rec == 2, "_scaffold": True},
    ]
    for t in tiers:
        t["_pending"] = ["note 定位文案", ("档3 标注哪些专项被吉早安替代" if "吉早安" in t["name"] else "")]
        t["_pending"] = [p for p in t["_pending"] if p]
    return tiers


def build_x_addons(snapshot: dict, health: dict, pricing: dict, rules: dict, abnormal_map: dict | None = None) -> list:
    """x_addons 骨架：① 中风险+ 癌生成行（risk_level_tag/method/interval/posterior）+
    ② 异常驱动行（health_summary 异常→abnormal_followup_map）。clinical_value 留空（LLM）。"""
    rows = []
    for s in snapshot.get("section4_screening", []):
        if not isinstance(s, dict):
            continue
        cid = s.get("cancer_id"); post = s.get("posterior_probability") or 0
        name = s.get("cancer_name_zh", cid)
        t5 = _risk_tier_5(post, rules)
        fu = (rules.get("cancers", {}).get(cid, {}) or {}).get("tier_followup", {}).get(t5) or {}
        tag, label = _TIER_TAG["priority"] if post > 0.01 else (_TIER_TAG["important"] if 0.005 <= post <= 0.01 else _TIER_TAG["maintain"])
        rows.append({
            "risk_source": f"{name}风险", "risk_level_tag": tag, "risk_level_label": label,
            "method": fu.get("method") or (s.get("standard_screening") or [{}])[0].get("method", ""),
            "interval": f"{fu.get('months')}月内" if fu.get("months") else "",
            "price_range": "", "clinical_value": "",
            "cancer_name": name, "posterior_probability": round(post, 6) if post else None,
            "_scaffold": True, "_pending": ["risk_source 措辞细化", "clinical_value 文案", "price_range（assemble_package 求）"],
        })
    # ② 异常驱动行
    for ab in _extract_abnormals(health, abnormal_map or {}):
        tag, label = _TIER_TAG.get(ab["tier"], _TIER_TAG["important"])
        rows.append({
            "risk_source": ab["indicator"], "risk_level_tag": tag, "risk_level_label": label,
            "method": ab["followup"], "interval": _TIER_INTERVAL.get(ab["tier"], "1 个月内"),
            "price_range": ab["price_hint"], "clinical_value": "",
            "_scaffold": True, "_pending": ["risk_source 措辞细化", "clinical_value 文案"],
        })
    return rows


def run(artifacts: Path, answers_path: Path | None, skill_root: Path) -> None:
    """可被编排器 import 调用的核心入口（无需 argparse）。"""
    art = Path(artifacts)
    skill = Path(skill_root)
    snapshot = _load(art / "snapshot_risk.json", {})
    voi = _load(art / "voi_ranking.json", {})
    health = _load(art / "health_summary_structured_summary.json", {})
    ans_src = answers_path if answers_path else (art.parent / "answers.json")
    answers = _unwrap_answers(_load(ans_src, {}))
    rules = _load(RULES_PATH, {})
    abnormal_map = _load(ABNORMAL_MAP_PATH, {})
    pricing = _load(PRICING_PATH, {})
    person = snapshot.get("person_context", {}) if isinstance(snapshot, dict) else {}

    # brca_status（复用 build_report_json 逻辑）
    try:
        import build_report_json as brj
        brca_status = brj._brca_status(answers)
    except Exception:
        brca = answers.get("q_genetic_mutations_brca", [])
        brca_status = "positive" if (isinstance(brca, list) and any(b in ("brca1", "brca2") for b in brca)) else "unknown"

    timeline = build_timeline(snapshot, answers, health, rules, abnormal_map)
    liquid = build_liquid_biopsy(snapshot, voi, pricing)
    long_term = build_long_term(answers, brca_status)
    package = build_package(snapshot, answers, person, pricing, rules)
    x_addons = build_x_addons(snapshot, health, pricing, rules, abnormal_map)

    def _write(name: str, obj: Any) -> None:
        p = art / name
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[scaffold] {name} -> {p}")

    _write("timeline_tiers.json", timeline)
    _write("liquid_biopsy_perf.json", liquid)
    _write("long_term_intervention.json", long_term)
    _write("package_tiers.json", package)
    _write("x_addons.json", x_addons)

    # package 价格由 assemble_package Σmid 求和（复用）
    try:
        import assemble_package as apkg
        pkg_pricing = apkg.load_pricing(skill)
        if pkg_pricing is not None:
            apkg.assemble_package(art / "package_tiers.json", pkg_pricing)
            print("[scaffold] package_tiers.json prices assembled (Σmid)")
    except Exception as exc:
        print(f"[scaffold] ⚠ assemble_package skipped: {exc}", file=_sys.stderr)

    print("[scaffold] 5 section artifact 骨架已生成。agent 只需补 _pending 标记的文案字段（rationale/note/clinical_value）。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True, help="<out>/artifacts 目录")
    parser.add_argument("--answers", default=None)
    parser.add_argument("--skill-root", default=str(SKILL_ROOT))
    args = parser.parse_args()
    run(Path(args.artifacts), Path(args.answers) if args.answers else None, Path(args.skill_root))


if __name__ == "__main__":
    main()


#!/usr/bin/env python3
"""Task7 snapshot cancer-risk prediction.

Inputs (read-only):
- ``merged_risk_factors.json``         — Task5 merged events; uses
  ``current_factor_states`` + ``screening_tests`` only.
- ``cancer_age_sex_priors.json``       — Task2 GLOBOCAN-derived priors.
- ``risk_assertions_derived.json``     — per-assertion ``log_odds_delta``.
- ``detection_performance_derived.json`` — Jizaoan LR+ / LR- per cancer.
- ``cancers.json``                     — sex applicability and name lookup.
- ``screening_recommendations.json``   — section 4 deep-screening recipes.
- ``config/formal.yaml``               — tier thresholds + screening rules.

Output:
- ``snapshot_risk.json`` with per-cancer prior / components / posterior /
  tier / uncertainties, plus a ``section4_screening`` block that obeys
  the spec's ``posterior > medium_min`` filter and top-N cap.

Numeric rules (mirroring spec §7.2/§7.3):
- ``posterior_log_odds = logit(prior) + Σ component.log_odds_delta``
- Jizaoan ``negative``: add ``negative_log_odds_delta`` for every covered
  cancer.
- Jizaoan ``positive``: add ``positive_log_odds_delta`` only for
  cancers listed in ``screening_tests[*].top_cancers`` (top1/top2).
- Sex mismatch (``applicable_sex != person.sex``) → ``not_applicable``,
  ``posterior_probability=null``.
- Missing prior (in ``priors.missing_priors``) → keep the cancer in
  output with ``posterior_probability=null`` and
  ``status_reason="no_prior_data"``.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import yaml

from build_age_sex_priors import resolve_prior

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = "snapshot-risk-v1"
MAX_NON_PATHOLOGY_PROBABILITY = 0.75
PATHOLOGY_CONFIRMED_PROBABILITY = 0.99

# Tumor markers and ctDNA tests whose negative result provides protective
# downward adjustment when PPV dominates (优化点3).
PROTECTIVE_TEST_IDS: frozenset[str] = frozenset({
    "jizaoan_multi_cancer_screening",
    "afp_serum", "cea_serum", "ca199_serum", "ca125_serum",
    "psa_total_serum", "psa_free_ratio",
    "cyfra211_serum", "scc_serum", "nmp22_urine",
    "ca724_serum", "ca153_serum",
})


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------


def logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def assign_tier(probability: float | None, tiers: list[dict[str, Any]]) -> str | None:
    if probability is None:
        return None
    for tier in tiers:
        if probability <= float(tier["max_probability"]):
            return tier["tier"]
    return tiers[-1]["tier"] if tiers else None


def assign_imaging_tier(ppv: float | None, imaging_tiers: list[dict[str, Any]]) -> str | None:
    """v6: tier scheme for PPV-dominated cancers.

    Distinct from `assign_tier` because the clinical action is
    different — a 50% PPV thyroid nodule needs a biopsy, not a
    "high cancer risk" label.
    """
    if ppv is None:
        return None
    for tier in imaging_tiers:
        if ppv <= float(tier["max_ppv"]):
            return tier["tier"]
    return imaging_tiers[-1]["tier"] if imaging_tiers else None


def cap_non_pathology_probability(probability: float) -> tuple[float, float | None]:
    if probability > MAX_NON_PATHOLOGY_PROBABILITY:
        return MAX_NON_PATHOLOGY_PROBABILITY, MAX_NON_PATHOLOGY_PROBABILITY
    return probability, None


def _pathology_confirmed_for_cancer(cancer_id: str, states: list[dict[str, Any]]) -> dict[str, Any] | None:
    for state in states:
        if state.get("cancer_id") != cancer_id:
            continue
        text = " ".join(
            str(state.get(key) or "")
            for key in ("evidence_text", "finding_name_zh", "factor_name")
        )
        if not any(marker in text for marker in ("病理", "活检", "穿刺")):
            continue
        if "原位癌" in text or "浸润癌" in text or ("浸润性" in text and "癌" in text):
            return state
    return None


# ---------------------------------------------------------------------------
# Index helpers
# ---------------------------------------------------------------------------


def _derived_by_assertion_id(derived_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {d["assertion_id"]: d for d in derived_payload.get("derived_assertions", [])}


def _derived_by_factor_level(
    derived_payload: dict[str, Any],
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    """Index derived assertions by ``(cancer_id, factor_id, factor_level)``."""
    index: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for d in derived_payload.get("derived_assertions", []):
        key = (d.get("cancer_id"), d.get("factor_id"), d.get("factor_level"))
        if all(key):
            index.setdefault(key, []).append(d)
    return index


def _detection_by_cancer_id(detection_payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """v6 P1: returns LIST of detections per cancer_id (not single).

    Pre-P1 there was always exactly one detection (jizaoan) per cancer.
    With tumor markers added, lung_cancer alone has jizaoan + CEA +
    CYFRA21-1 + CA125 + SCC, so we keep all of them and let
    `_screening_contribution` accumulate matched results.
    """
    by_cancer: dict[str, list[dict[str, Any]]] = {}
    for d in detection_payload.get("derived_detection_performance", []):
        by_cancer.setdefault(d["cancer_id"], []).append(d)
    return by_cancer


def _screening_by_cancer_id(screening_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["cancer_id"]: r for r in screening_payload.get("recommendations", [])}


def _split_assertion_key(assertion_key: str) -> tuple[str, str, str, str] | None:
    parts = assertion_key.split("|")
    if len(parts) != 4:
        return None
    return parts[0], parts[1], parts[2], parts[3]


def _state_cancer_id(state: dict[str, Any]) -> str | None:
    key = state.get("assertion_key")
    if not key:
        return None
    split = _split_assertion_key(key)
    return split[0] if split else None


def _state_assertion_id(state: dict[str, Any]) -> str | None:
    key = state.get("assertion_key")
    if not key:
        return None
    split = _split_assertion_key(key)
    return split[3] if split else None


# ---------------------------------------------------------------------------
# Component assembly
# ---------------------------------------------------------------------------


def _components_for_cancer(
    cancer_id: str,
    current_states: list[dict[str, Any]],
    derived_by_id: dict[str, dict[str, Any]],
    derived_by_factor_level: dict[tuple[str, str, str], list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build snapshot components for one cancer from current factor states.

    Two state shapes are accepted:

    * Full ``assertion_key`` events (cancer-expanded): join by
      ``assertion_id`` and gate on matching ``cancer_id``.
    * Slim ``factor_key`` events (one per factor/level, applies to many
      cancers via the ontology): join by ``(cancer_id, factor_id,
      factor_level)`` against ``risk_assertions_derived`` so the same
      observed factor adds a component to every cancer it actually
      affects.
    """
    components: list[dict[str, Any]] = []
    notes: list[str] = []
    seen_assertion_ids: set[str] = set()
    for state in current_states:
        if state.get("exists") is not True:
            continue
        if state.get("assertion_key"):
            split = _split_assertion_key(state["assertion_key"])
            if split and split[0] != cancer_id:
                continue
            assertion_id = split[3] if split else None
            derived = derived_by_id.get(assertion_id) if assertion_id else None
            matches = [derived] if derived else []
        else:
            factor_id = state.get("factor_id")
            factor_level = state.get("factor_level")
            if not factor_id or not factor_level:
                continue
            matches = list(derived_by_factor_level.get((cancer_id, factor_id, factor_level), []))
        if not matches:
            continue
        for derived in matches:
            if derived is None:
                continue
            if derived.get("conversion_status") != "usable":
                notes.append(f"assertion_id={derived.get('assertion_id')} conversion_status={derived.get('conversion_status')}")
                continue
            assertion_id = derived.get("assertion_id")
            if assertion_id in seen_assertion_ids:
                continue
            seen_assertion_ids.add(assertion_id)
            components.append({
                "assertion_key": derived.get("assertion_key") or state.get("assertion_key"),
                "assertion_id": assertion_id,
                "factor_id": state.get("factor_id"),
                "factor_level": state.get("factor_level"),
                "source": state.get("source", "report_llm_fill"),
                "evidence_text": state.get("evidence_text"),
                "exam_date": state.get("exam_date"),
                "log_odds_delta": float(derived["log_odds_delta"]),
                "effect_type": derived.get("effect_type"),
                "calculation_value": derived.get("calculation_value"),
                "approximation": bool(derived.get("approximation", False)),
                "source_id": derived.get("source_id"),
            })
    return components, notes


def _screening_contribution(
    cancer_id: str,
    screening_tests: list[dict[str, Any]],
    detection_by_id: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """v6 P1: returns LIST of contributions (was single).

    Iterates every detection registered for this cancer_id and every
    matching screening_test (joined by test_id). Multiple positive/
    negative results for the same cancer accumulate — assumes tests
    are conditionally independent given disease status (standard
    Bayesian aggregation of independent likelihood ratios).

    For each contribution:
      - negative result → adds `negative_log_odds_delta` (typically <0)
      - positive result → adds `positive_log_odds_delta` (typically >0),
        gated by `top_cancers` (preserves the jizaoan invariant: a
        positive multi-cancer panel only contributes to its declared
        tissue-of-origin call; for single-cancer tumor markers we
        list the cancer_id itself in top_cancers at orchestrator
        merge time, so this gate passes through).
    """
    detections = detection_by_id.get(cancer_id) or []
    if not detections:
        return [], []
    contributions: list[dict[str, Any]] = []
    notes: list[str] = []
    for detection in detections:
        if detection.get("conversion_status") != "usable":
            continue
        test_id = detection.get("test_id")
        for test in screening_tests:
            if test.get("test_id") != test_id:
                continue
            result = test.get("result")
            top_cancers = test.get("top_cancers") or []
            if result == "negative":
                contributions.append({
                    "test_id": test_id,
                    "test_name": test.get("test_name"),
                    "result": "negative",
                    "log_odds_delta": float(detection["negative_log_odds_delta"]),
                    "lr": float(detection.get("lr_negative", 0)),
                    "approximation": bool(detection.get("probability_boundary_adjustment", False)),
                    "source_id": detection.get("source_id"),
                    "evidence_text": test.get("evidence_text"),
                    "exam_date": test.get("exam_date"),
                })
            elif result == "positive":
                if cancer_id not in top_cancers:
                    notes.append(f"{test_id} positive but {cancer_id} not in top_cancers={top_cancers}")
                    continue
                contributions.append({
                    "test_id": test_id,
                    "test_name": test.get("test_name"),
                    "result": "positive",
                    "log_odds_delta": float(detection["positive_log_odds_delta"]),
                    "lr": float(detection.get("lr_positive", 0)),
                    "approximation": bool(detection.get("probability_boundary_adjustment", False)),
                    "source_id": detection.get("source_id"),
                    "evidence_text": test.get("evidence_text"),
                    "exam_date": test.get("exam_date"),
                })
    return contributions, notes


# ---------------------------------------------------------------------------
# Snapshot computation
# ---------------------------------------------------------------------------


def compute_snapshot(
    *,
    merged: dict[str, Any],
    priors_payload: dict[str, Any],
    derived_assertions: dict[str, Any],
    detection_derived: dict[str, Any],
    cancers_ontology: dict[str, Any],
    screening_recommendations: dict[str, Any],
    person_sex: str | None,
    person_age: int | None,
    config: dict[str, Any] | None = None,
    factor_name_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    config = config or {}
    risk_cfg = config.get("risk_prediction", {}) if isinstance(config, dict) else {}
    tiers = risk_cfg.get("risk_tiers") or [
        {"tier": "low", "max_probability": 0.005},
        {"tier": "medium", "max_probability": 0.02},
        {"tier": "high", "max_probability": 1.01},
    ]
    # v6: imaging-PPV-dominated tier scheme — separate from the
    # Bayes-only tiers so users can tell "elevated future-incidence
    # risk" from "specific lesion needing workup".
    imaging_tiers = [
        {"tier": "moderate_workup", "max_ppv": 0.10},
        {"tier": "high_workup", "max_ppv": 0.50},
        {"tier": "urgent_workup", "max_ppv": 1.01},
    ]
    # v7: imaging findings are now part of current_factor_states (merged).
    # We split them out here so Bayes computation only sees OR-based risk
    # factors, while PPV midpoint still uses the imaging findings.
    current_states = merged.get("current_factor_states", [])
    bayes_states = [s for s in current_states if s.get("factor_type") != "imaging_finding"]
    imaging_from_merged = [s for s in current_states if s.get("factor_type") == "imaging_finding" and s.get("exists") is True]

    # Group imaging findings by cancer_id for fast lookup (v7: merged path only).
    findings_by_cancer: dict[str, list[dict[str, Any]]] = {}
    for f in imaging_from_merged:
        cid = f.get("cancer_id")
        if cid:
            findings_by_cancer.setdefault(cid, []).append(f)
    pathology_by_cancer = {
        cancer_id: finding
        for cancer_id, findings in findings_by_cancer.items()
        if (finding := _pathology_confirmed_for_cancer(cancer_id, findings)) is not None
    }

    section4_cfg = risk_cfg.get("screening_section") or {}
    medium_min = float(section4_cfg.get("medium_min_probability", 0.005))
    section4_top_n = int(section4_cfg.get("top_n", 3))
    section4_include_above = float(section4_cfg.get("include_probability_above", 0.02))

    screening_tests = merged.get("screening_tests", [])
    derived_by_id = _derived_by_assertion_id(derived_assertions)
    derived_by_fl = _derived_by_factor_level(derived_assertions)
    detection_by_id = _detection_by_cancer_id(detection_derived)
    screen_by_cancer = _screening_by_cancer_id(screening_recommendations)

    cancer_results: list[dict[str, Any]] = []
    for cancer in cancers_ontology.get("cancers", []):
        cancer_id = cancer["cancer_id"]
        applicable_sex = cancer.get("applicable_sex", "all")
        result: dict[str, Any] = {
            "cancer_id": cancer_id,
            "cancer_name_zh": cancer.get("cancer_name_zh"),
            "applicable_sex": applicable_sex,
            "not_applicable": False,
            "status_reason": None,
            "prior_probability": None,
            "prior_log_odds": None,
            "prior_source_id": None,
            "prior_anchor_age": None,
            "components": [],
            "screening_contribution": None,
            "posterior_log_odds": None,
            "posterior_probability": None,
            "risk_tier": None,
            "uncertainties": [],
        }

        if applicable_sex != "all" and applicable_sex != person_sex:
            result["not_applicable"] = True
            result["status_reason"] = "sex_mismatch"
            cancer_results.append(result)
            continue

        prior_record = resolve_prior(priors_payload, cancer_id, person_sex, person_age)
        if prior_record is None:
            # v6: even without a prior, imaging PPV findings can still
            # drive a posterior. PPV is conditional on the lesion being
            # observed, not on a population baseline rate, so the
            # absence of a future-incidence prior does not invalidate it.
            imaging_for_cancer = findings_by_cancer.get(cancer_id, [])
            if not imaging_for_cancer:
                result["status_reason"] = "no_prior_data"
                cancer_results.append(result)
                continue
            pathology_confirmed = pathology_by_cancer.get(cancer_id)
            if pathology_confirmed:
                result.update({
                    "status_reason": "pathology_confirmed",
                    "components": [],
                    "screening_contribution": None,
                    "posterior_log_odds": None,
                    "posterior_probability_bayes": None,
                    "posterior_probability": PATHOLOGY_CONFIRMED_PROBABILITY,
                    "imaging_findings": imaging_for_cancer,
                    "imaging_ppv_max": None,
                    "posterior_source": {
                        "dominant": "pathology_confirmed",
                        "narrative": f"病理结果提示原位癌或浸润癌：{pathology_confirmed.get('evidence_text')}",
                        "bayes_posterior_for_comparison": None,
                        "imaging_ppv_max": None,
                        "imaging_findings_count": len(imaging_for_cancer),
                    },
                    "risk_tier": "pathology_confirmed",
                    "uncertainties": [],
                })
                cancer_results.append(result)
                continue
            # Pick dominant finding (highest PPV midpoint)
            dominant = max(imaging_for_cancer, key=lambda f: float(f["ppv_point_used"]))
            ppv_max = float(dominant["ppv_point_used"])
            final_ppv, capped_at = cap_non_pathology_probability(ppv_max)
            ppv_low, ppv_high = dominant["malignancy_ppv_range"]
            narrative = (
                f"本次预测由影像学发现主导：{dominant['finding_name_zh']}"
                f"（{dominant['evidence_text']}），恶性概率参考区间 "
                f"{ppv_low*100:.0f}-{ppv_high*100:.0f}%"
                f"（{dominant.get('source_id', '')}）。"
                f"注：本癌种当前缺少年龄/性别先验数据 (missing_priors)，"
                f"Bayes 累积危险因素路径不可用，本次仅以影像 PPV 评估。"
                f"建议：{dominant.get('next_step', '请专科随诊')}。"
            )
            result.update({
                "status_reason": "imaging_ppv_only_no_prior",
                "components": [],
                "screening_contribution": None,
                "posterior_log_odds": None,
                "posterior_probability_bayes": None,
                "posterior_probability": final_ppv,
                "imaging_findings": imaging_for_cancer,
                "imaging_ppv_max": ppv_max,
                "posterior_source": {
                    "dominant": "imaging_ppv_no_prior",
                    "narrative": narrative,
                    "bayes_posterior_for_comparison": None,
                    "imaging_ppv_max": ppv_max,
                    "imaging_findings_count": len(imaging_for_cancer),
                    "capped_at": capped_at,
                },
                "risk_tier": assign_imaging_tier(final_ppv, imaging_tiers),
                "uncertainties": [{
                    "reason": "no_prior_data_imaging_ppv_only",
                    "detail": "cancer has no age/sex prior; output is PPV-only and reflects a specific lesion, not future incidence",
                }],
            })
            cancer_results.append(result)
            continue

        prior = prior_record["annual_probability"]
        if not math.isfinite(prior) or prior <= 0 or prior >= 1:
            result["status_reason"] = "invalid_prior_probability"
            result["uncertainties"].append({"reason": "prior_out_of_range", "value": prior})
            cancer_results.append(result)
            continue

        prior_log_odds = logit(prior)
        components, notes = _components_for_cancer(
            cancer_id, bayes_states, derived_by_id, derived_by_fl
        )
        if factor_name_map:
            for comp in components:
                fid = comp.get("factor_id")
                if fid and fid in factor_name_map:
                    comp["factor_name_zh"] = factor_name_map[fid]
        # v6 P1: _screening_contribution now returns a LIST of
        # contributions (multiple tests can target the same cancer:
        # e.g. lung_cancer accumulates jizaoan + CEA + CYFRA21-1 + SCC +
        # CA125 hits independently).
        screening_contribs, screening_notes = _screening_contribution(
            cancer_id, screening_tests, detection_by_id
        )

        delta_sum = sum(c["log_odds_delta"] for c in components)
        screening_delta = sum(sc["log_odds_delta"] for sc in screening_contribs)
        posterior_log_odds = prior_log_odds + delta_sum + screening_delta
        posterior = sigmoid(posterior_log_odds)

        uncertainties: list[dict[str, Any]] = []
        for c in components:
            if c["approximation"]:
                uncertainties.append({
                    "reason": "rr_or_hr_approximation",
                    "assertion_id": c["assertion_id"],
                    "factor_id": c["factor_id"],
                })
        for sc in screening_contribs:
            if sc.get("approximation"):
                uncertainties.append({
                    "reason": "detection_probability_boundary_adjustment",
                    "test_id": sc["test_id"],
                })
        for note in notes:
            uncertainties.append({"reason": "component_dropped", "detail": note})
        for sn in screening_notes:
            uncertainties.append({"reason": "screening_skipped", "detail": sn})

        # v6/v7: merge imaging PPV midpoint via max(bayes, ppv_midpoint).
        imaging_for_cancer = findings_by_cancer.get(cancer_id, [])
        pathology_confirmed = pathology_by_cancer.get(cancer_id)
        ppv_max = 0.0
        dominant_imaging = None
        for f in imaging_for_cancer:
            ppv_midpoint = float(f["ppv_point_used"])
            if ppv_midpoint > ppv_max:
                ppv_max = ppv_midpoint
                dominant_imaging = f

        bayes_posterior = posterior  # keep the Bayes-only value for audit
        capped_at = None
        if pathology_confirmed:
            final_posterior = PATHOLOGY_CONFIRMED_PROBABILITY
            dominant_source = "pathology_confirmed"
            narrative = f"病理结果提示原位癌或浸润癌：{pathology_confirmed.get('evidence_text')}"
            tier_final = "pathology_confirmed"
        elif dominant_imaging and ppv_max > bayes_posterior:
            # Apply negative LR from protective tests (tumor markers / ctDNA)
            # to reduce PPV-based probability when results are negative.
            protective_neg = [
                sc for sc in screening_contribs
                if sc.get("test_id") in PROTECTIVE_TEST_IDS and sc.get("result") == "negative"
            ]
            if protective_neg:
                protective_delta = sum(sc["log_odds_delta"] for sc in protective_neg)
                final_posterior = sigmoid(logit(ppv_max) + protective_delta)
                dominant_source = "imaging_ppv_protective_adjusted"
            else:
                final_posterior = ppv_max
                dominant_source = "imaging_ppv"
            ppv_low, ppv_high = dominant_imaging["malignancy_ppv_range"]
            protective_note = ""
            if protective_neg:
                names = "、".join(sc.get("test_name") or sc.get("test_id") for sc in protective_neg)
                protective_note = (
                    f"阴性保护性检测（{names}）下调影像 PPV"
                    f"（调整后 {final_posterior*100:.4f}%）。"
                )
            narrative = (
                f"本次预测由影像学发现主导：{dominant_imaging['finding_name_zh']}"
                f"（{dominant_imaging['evidence_text']}）"
                f"，恶性概率参考区间 {ppv_low*100:.0f}-{ppv_high*100:.0f}%"
                f"（{dominant_imaging['source_id']}）。"
                f"累积危险因素 Bayes 后验为 {bayes_posterior*100:.4f}%。"
                f"{protective_note}"
                f"建议：{dominant_imaging.get('next_step', '请专科随诊')}。"
            )
            tier_final = assign_imaging_tier(final_posterior, imaging_tiers)
        elif imaging_for_cancer:
            final_posterior = bayes_posterior
            dominant_source = "bayes_factors"
            narrative = (
                f"本次预测以累积危险因素 Bayes 模型为主导（后验 "
                f"{bayes_posterior*100:.4f}%）。同时存在影像学发现 "
                f"{len(imaging_for_cancer)} 处，恶性概率中位值 {ppv_max*100:.0f}%，"
                f"低于 Bayes 后验，不主导本次预测；详见影像学发现列表。"
            )
            tier_final = assign_tier(bayes_posterior, tiers)
        else:
            final_posterior = bayes_posterior
            dominant_source = "bayes_factors"
            narrative = None
            tier_final = assign_tier(bayes_posterior, tiers)

        if dominant_source != "pathology_confirmed":
            final_posterior, capped_at = cap_non_pathology_probability(final_posterior)

        result.update({
            "prior_probability": prior,
            "prior_log_odds": prior_log_odds,
            "prior_source_id": prior_record["source_id"],
            "prior_anchor_age": prior_record["age"],
            "components": components,
            "screening_contributions": screening_contribs,   # v6 P1: list
            "screening_contribution": screening_contribs[0] if screening_contribs else None,  # legacy single
            "posterior_log_odds": posterior_log_odds,
            "posterior_probability_bayes": bayes_posterior,
            "posterior_probability": final_posterior,
            "imaging_findings": imaging_for_cancer,
            "imaging_ppv_max": ppv_max if imaging_for_cancer else None,
            "posterior_source": {
                "dominant": dominant_source,
                "narrative": narrative,
                "bayes_posterior_for_comparison": bayes_posterior,
                "imaging_ppv_max": ppv_max if imaging_for_cancer else None,
                "imaging_findings_count": len(imaging_for_cancer),
                "capped_at": capped_at,
            },
            "risk_tier": tier_final,
            "uncertainties": uncertainties,
        })
        cancer_results.append(result)

    cancer_results.sort(
        key=lambda r: (
            r["posterior_probability"] is None,
            -(r["posterior_probability"] or 0.0),
            r["cancer_id"],
        )
    )

    eligible = [
        r for r in cancer_results
        if r["posterior_probability"] is not None and r["posterior_probability"] >= medium_min
    ]
    if section4_top_n > 0:
        selected_ids: set[str] = set()
        section4 = []
        for r in eligible[:section4_top_n]:
            section4.append(r)
            selected_ids.add(r["cancer_id"])
        for r in eligible:
            if r["cancer_id"] in selected_ids:
                continue
            if float(r["posterior_probability"]) > section4_include_above:
                section4.append(r)
                selected_ids.add(r["cancer_id"])
    else:
        section4 = eligible
    section4_payload = []
    for r in section4:
        screen_entry = screen_by_cancer.get(r["cancer_id"], {})
        section4_payload.append({
            "cancer_id": r["cancer_id"],
            "cancer_name_zh": r["cancer_name_zh"],
            "posterior_probability": r["posterior_probability"],
            "risk_tier": r["risk_tier"],
            "standard_screening": screen_entry.get("standard_screening", []),
        })

    # Jizaoan what-if: for each applicable cancer with a valid posterior and
    # a usable jizaoan detection entry, compute risk_if_negative.
    jizaoan_whatif: list[dict[str, Any]] = []
    for r in cancer_results:
        if r.get("not_applicable") or r.get("posterior_log_odds") is None:
            continue
        jizaoan_det = next(
            (d for d in detection_by_id.get(r["cancer_id"], [])
             if d.get("test_id") == "jizaoan_multi_cancer_screening"
             and d.get("conversion_status") == "usable"),
            None,
        )
        if jizaoan_det is None:
            continue
        neg_delta = float(jizaoan_det.get("negative_log_odds_delta", 0))
        jizaoan_whatif.append({
            "cancer_id": r["cancer_id"],
            "cancer_name_zh": r["cancer_name_zh"],
            "current_risk": r["posterior_probability"],
            "risk_if_negative": sigmoid(r["posterior_log_odds"] + neg_delta),
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "person_context": {"sex": person_sex, "age": person_age},
        "risk_tier_thresholds": tiers,
        "section4_filter": {
            "medium_min_probability": medium_min,
            "top_n": section4_top_n,
            "include_probability_above": section4_include_above,
            "selected_count": len(section4_payload),
        },
        "cancers": cancer_results,
        "section4_screening": section4_payload,
        "convenient_screening": {
            "test_id": risk_cfg.get("screening_strategies", {}).get("convenient", "jizaoan_multi_cancer_screening"),
            "results": screening_tests,
        },
        "jizaoan_whatif": jizaoan_whatif,
        "uncertainties_summary": {
            "approximation_components": sum(
                1
                for r in cancer_results
                for c in r["components"]
                if c["approximation"]
            ),
            "cancers_missing_prior": [r["cancer_id"] for r in cancer_results if r["status_reason"] == "no_prior_data"],
            "cancers_not_applicable": [r["cancer_id"] for r in cancer_results if r["not_applicable"]],
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run_snapshot_stage(
    *,
    artifacts: Path,
    evidence_store: Path,
    config_path: Path,
    person_sex: str | None,
    person_age: int | None,
) -> dict[str, Any]:
    config = _load_yaml(config_path)
    risk_cfg = config.get("risk_prediction", {}) if isinstance(config, dict) else {}

    merged = _read_json(artifacts / "merged_risk_factors.json")
    priors_payload = _read_json(Path(risk_cfg.get(
        "priors_file",
        evidence_store / "cancer_age_sex_priors.json",
    )))
    derived_assertions = _read_json(evidence_store / "risk_assertions_derived.json")
    detection_derived = _read_json(evidence_store / "detection_performance_derived.json")
    cancers_ontology = _read_json(evidence_store / "cancers.json")
    screening_recommendations = _read_json(evidence_store / "screening_recommendations.json")
    rf_path = evidence_store / "risk_factors.json"
    _factor_zh: dict[str, str] = {}
    if rf_path.is_file():
        rf_data = _read_json(rf_path)
        for rf in rf_data.get("risk_factors", []):
            fid = rf.get("factor_id")
            if fid and rf.get("factor_name_zh"):
                _factor_zh[fid] = rf["factor_name_zh"]
    snapshot = compute_snapshot(
        merged=merged,
        priors_payload=priors_payload,
        derived_assertions=derived_assertions,
        detection_derived=detection_derived,
        cancers_ontology=cancers_ontology,
        screening_recommendations=screening_recommendations,
        person_sex=person_sex,
        person_age=person_age,
        config=config,
        factor_name_map=_factor_zh,
    )

    cp3_audit_path = artifacts / "cp3_audit_result.json"
    if cp3_audit_path.is_file():
        cp3_audit = json.loads(cp3_audit_path.read_text(encoding="utf-8"))
        unmatched = cp3_audit.get("unmatched_findings", [])
        if unmatched:
            snapshot["unmatched_findings"] = unmatched

    output_name = risk_cfg.get("snapshot_output_json", "snapshot_risk.json")
    output_path = artifacts / output_name
    output_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True)
    parser.add_argument("--evidence-store", default=str(SKILL_ROOT / "references" / "database" / "cancerrisk" / "json"))
    parser.add_argument("--config", default=str(SKILL_ROOT / "config" / "formal.yaml"))
    parser.add_argument("--person-sex", choices=("male", "female"), default=None)
    parser.add_argument("--person-age", type=int, default=None)
    args = parser.parse_args()

    config = _load_yaml(Path(args.config))
    person_ctx = config.get("person_context", {}) if isinstance(config, dict) else {}
    sex = args.person_sex or person_ctx.get("sex")
    age = args.person_age if args.person_age is not None else person_ctx.get("age")

    snapshot = run_snapshot_stage(
        artifacts=Path(args.artifacts),
        evidence_store=Path(args.evidence_store),
        config_path=Path(args.config),
        person_sex=sex,
        person_age=int(age) if age is not None else None,
    )
    summary = {
        "cancers": len(snapshot["cancers"]),
        "scorable": sum(1 for r in snapshot["cancers"] if r["posterior_probability"] is not None),
        "section4": len(snapshot["section4_screening"]),
    }
    print(f"[snapshot_risk] {summary}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build the per-cancer age/sex prior probability table for snapshot risk.

This is a Task2 knowledge-base preparation step. It reads the upstream
GLOBOCAN incidence-rate table (annual incidence per 100,000) and converts
the Chinese cancer names to our `cancer_id` vocabulary, normalises the
anchor ages, and emits

    evidence_store/ontology/cancer_age_sex_priors.json

Snapshot risk later looks up the appropriate prior with
``resolve_prior(cancer_id, sex, age)`` which clamps the query age to the
anchor range and uses the nearest lower anchor between anchors:

    age > max_anchor → use max_anchor's value
    age < min_anchor → use min_anchor's value
    otherwise        → use the largest anchor ≤ age

Cancers without any prior in the source data are listed in
``missing_priors`` so downstream stages can mark them as
``no_prior_data`` instead of fabricating a number.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "cancer-prior-v1"

# maintainer-only knowledge-base prep tool (not in pipeline 12 stages).
# /Volumes/... default kept for the original maintainer's machine; env override
# lets other maintainers point at their own oncoRAG checkout.
ONCORAG_INCIDENCE_DEFAULT = Path(
    os.environ.get(
        "ONCORAG_INCIDENCE_PATH",
        "/Volumes/exp/geneplu_work/1.skill_tijian/28.project_tijian/oncoRAG/ontology/references/incidence_rates.json",
    )
)
SKILL_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_STORE_DEFAULT = SKILL_ROOT / "references" / "database" / "cancerrisk" / "json"
SOURCE_ID_DEFAULT = "globocan_2022"

# Chinese cancer-name → cancer_id mapping. Keep in sync with cancers.json.
CN_TO_CANCER_ID = {
    "肺癌": "lung_cancer",
    "肝癌": "liver_cancer",
    "胃癌": "gastric_cancer",
    "结直肠癌": "colorectal_cancer",
    "乳腺癌": "breast_cancer",
    "前列腺癌": "prostate_cancer",
    "食管癌": "esophageal_cancer",
    "宫颈癌": "cervical_cancer",
    "卵巢癌": "ovarian_cancer",
    "膀胱癌": "bladder_cancer",
    "肾癌": "kidney_cancer",
    "头颈癌": "head_neck_cancer",
    "头颈肿瘤": "head_neck_cancer",
    "胆道癌": "biliary_tract_cancer",
    "胆道肿瘤": "biliary_tract_cancer",
}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _to_priors(per_sex: dict[str, Any], sex: str, source_id: str) -> list[dict[str, Any]]:
    """Convert ``{"30": 3.92, ...}`` to sorted prior records.

    Age keys that are not integers, and zero-rate entries with no other
    data point (e.g. ``"乳腺癌.male: {30: 0.0}"``) are filtered out so the
    derived "applicable" sex picture matches the cancers.json contract.
    """
    rows: list[dict[str, Any]] = []
    for age_key, rate in per_sex.items():
        try:
            age = int(age_key)
        except (TypeError, ValueError):
            continue
        if not isinstance(rate, (int, float)):
            continue
        rate = float(rate)
        rows.append({
            "sex": sex,
            "age": age,
            "annual_incidence_per_100000": rate,
            "annual_probability": rate / 100000.0,
            "source_id": source_id,
        })
    rows.sort(key=lambda r: r["age"])
    if not rows:
        return rows
    # Drop a series that is uniformly zero — male breast cancer, female
    # prostate cancer, etc. are encoded as "not applicable" by the cancers
    # ontology, not as a real prior. (Raw upstream md keeps these as
    # 10-element 0.0 placeholders, so the previous ``len(rows) <= 1``
    # guard let them slip through — leading resolve_prior to return a
    # bogus 0.0 record for sex-mismatched queries.)
    if all(row["annual_incidence_per_100000"] == 0.0 for row in rows):
        return []
    return rows


def build_priors(
    incidence_source: Path,
    cancers_ontology: Path,
    source_id: str = SOURCE_ID_DEFAULT,
) -> dict[str, Any]:
    """Build the cancer_age_sex_priors payload."""
    src = _read_json(incidence_source)
    cancers = _read_json(cancers_ontology)["cancers"]
    ontology_ids = {c["cancer_id"] for c in cancers}
    cancer_by_id = {c["cancer_id"]: c for c in cancers}

    src_cancers = src.get("cancers", {})
    metadata = src.get("metadata", {})

    records: list[dict[str, Any]] = []
    found_ids: set[str] = set()
    for cn_name, per_sex in src_cancers.items():
        cancer_id = CN_TO_CANCER_ID.get(cn_name)
        if cancer_id is None or cancer_id not in ontology_ids:
            continue
        priors: list[dict[str, Any]] = []
        for sex in ("male", "female"):
            sex_rows = _to_priors(per_sex.get(sex, {}) or {}, sex, source_id)
            applicable_sex = cancer_by_id[cancer_id].get("applicable_sex", "all")
            if applicable_sex != "all" and applicable_sex != sex and not sex_rows:
                continue
            priors.extend(sex_rows)
        if not priors:
            continue
        records.append({
            "cancer_id": cancer_id,
            "cancer_name_zh": cn_name,
            "applicable_sex": cancer_by_id[cancer_id].get("applicable_sex", "all"),
            "priors": priors,
        })
        found_ids.add(cancer_id)

    records.sort(key=lambda r: r["cancer_id"])
    missing_priors = sorted(ontology_ids - found_ids)

    return {
        "schema_version": SCHEMA_VERSION,
        "metadata": {
            "source": metadata.get("source") or "unknown",
            "unit": metadata.get("unit") or "1/100,000",
            "derived_from": str(incidence_source),
            "default_source_id": source_id,
        },
        "lookup_rule": {
            "between_anchors": "nearest_lower_anchor",
            "above_max_age": "use_max_age_value",
            "below_min_age": "use_min_age_value",
        },
        "cancers": records,
        "missing_priors": missing_priors,
    }


def resolve_prior(
    priors_payload: dict[str, Any],
    cancer_id: str,
    sex: str | None,
    age: int | None,
) -> dict[str, Any] | None:
    """Return the matching prior record for a (cancer_id, sex, age) query.

    Returns ``None`` when the cancer has no priors in the source, the sex
    does not match, or the age cannot be interpreted. The caller decides
    whether to skip the cancer or fall back to "no_prior_data".
    """
    if cancer_id in priors_payload.get("missing_priors", []):
        return None
    if sex not in {"male", "female"} or age is None:
        return None
    for record in priors_payload.get("cancers", []):
        if record["cancer_id"] != cancer_id:
            continue
        same_sex = [p for p in record["priors"] if p["sex"] == sex]
        if not same_sex:
            return None
        ages = sorted({p["age"] for p in same_sex})
        if age <= ages[0]:
            picked_age = ages[0]
        elif age >= ages[-1]:
            picked_age = ages[-1]
        else:
            picked_age = max(a for a in ages if a <= age)
        for p in same_sex:
            if p["age"] == picked_age:
                return p
        return None
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incidence-source", default=str(ONCORAG_INCIDENCE_DEFAULT))
    parser.add_argument("--evidence-store", default=str(EVIDENCE_STORE_DEFAULT))
    parser.add_argument("--source-id", default=SOURCE_ID_DEFAULT)
    parser.add_argument("--output", default=None,
                        help="defaults to <evidence-store>/ontology/cancer_age_sex_priors.json")
    args = parser.parse_args()

    store = Path(args.evidence_store)
    cancers_ontology = store / "cancers.json"
    output = Path(args.output) if args.output else store / "cancer_age_sex_priors.json"

    payload = build_priors(Path(args.incidence_source), cancers_ontology, source_id=args.source_id)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"[age_sex_priors] cancers={len(payload['cancers'])} "
        f"missing={len(payload['missing_priors'])} -> {output}"
    )


if __name__ == "__main__":
    main()

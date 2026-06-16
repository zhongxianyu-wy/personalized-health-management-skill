# Evidence Ontology Reference

This file describes the lightweight JSON evidence store that backs every
numeric step in v3. Open it when editing `references/database/cancerrisk/json/` or when adding
new cancers, factors, assertions, or detection-performance entries.

## 1. Why JSON

- Single source of truth, version-controllable, diff-able, reviewable.
- No graph database, no triple store, no vector index — those are
  explicitly out of scope for v3.
- Every script that touches probability reads JSON directly; LLMs are
  forbidden from generating odds, sensitivities, or recommendations.

## 2. Directory layout

```
references/database/cancerrisk/json/
  ontology/
    cancers.json              # cancer entities
    risk_factors.json         # factor entities (granularity follows source)
    factor_observables.json   # how a factor surfaces in a report
    factor_synonyms.json      # zh/en aliases, hospital-style variants
  assertions/
    risk_assertions.json      # OR / RR / HR connecting factor↔cancer
    detection_performance.json# sensitivity / specificity per test
  screening/
    screening_recommendations.json
  versions/
    evidence_version.json
    changelog.md
  unstructured_evidence_notes.json
```

## 3. cancers.json

```json
{
  "version": "evidence-v0001",
  "cancers": [
    {
      "cancer_id": "gastric_cancer",
      "cancer_name_zh": "胃癌",
      "cancer_name_en": "Gastric cancer",
      "applicable_sex": "all",
      "prior_probability": 0.0021,
      "prior_source_id": "incidence_zh_2020",
      "notes": "Population baseline from CNCR 2020 incidence."
    }
  ]
}
```

`applicable_sex` is `all|male|female`. Sex-specific cancers (e.g.
prostate, cervical) drive the `not_applicable` rule in
`scripts/snapshot_risk.py`.

## 4. risk_factors.json

```json
{
  "version": "evidence-v0001",
  "risk_factors": [
    {
      "factor_id": "atrophic_gastritis",
      "factor_name_zh": "萎缩性胃炎",
      "factor_name_en": "Atrophic gastritis",
      "factor_type": "exam_finding",
      "applicable_sex": "all",
      "applicable_age_min": null,
      "applicable_age_max": null,
      "level_schema": ["present", "absent", "C1", "C2", "O1", "O2"],
      "required_by_cancers": ["gastric_cancer"],
      "interaction_needed_if_missing": false,
      "notes": ""
    }
  ]
}
```

- `factor_type ∈ {exam_finding, lab, lifestyle, family_history, medical_history, screening_test}`.
- `level_schema` enumerates the **allowed** values for downstream
  `factor_level`. Anything outside this list is rejected by the script
  gate.
- `interaction_needed_if_missing=true` flags factors the interactive
  layer must ask about when no evidence is present.

## 5. factor_observables.json

Describes where a factor can appear in a real report. Used by the LLM
prompts and the target-list generator; never enters probability math.

```json
{
  "factors": {
    "atrophic_gastritis": {
      "exam_sections": ["endoscopy", "pathology"],
      "expected_evidence": ["胃镜", "病理"],
      "value_units": null
    }
  }
}
```

## 6. factor_synonyms.json

Maps any clinical wording to a canonical `factor_id`. Aliases stay
deliberately literal — do not invent synonyms a hospital would not write.

```json
{
  "synonyms": {
    "atrophic_gastritis": ["萎缩性胃炎", "慢性萎缩性胃炎", "atrophic gastritis", "CAG"]
  }
}
```

## 7. risk_assertions.json

Only file that contributes to probability calculation:

```json
{
  "version": "evidence-v0001",
  "assertions": [
    {
      "assertion_id": "asrt_gastric_atrophic_gastritis_001",
      "cancer_id": "gastric_cancer",
      "factor_id": "atrophic_gastritis",
      "factor_level": "present",
      "effect_type": "OR",
      "effect_value": 3.2,
      "ci_low": 2.1,
      "ci_high": 4.8,
      "evidence_grade": "A",
      "population": "East Asian adults",
      "source_id": "source_001"
    }
  ]
}
```

- `effect_type ∈ {OR, RR, HR}`.
- OR uses `ln(OR)`.
- RR/HR are standardized whenever numeric values are available using
  `ln(RR)` or `ln(HR)` and must be marked `approximation=true`.
- Missing OR/RR/HR values ⇒ the assertion can still appear in narrative but
  cannot participate in `posterior_log_odds`.

## 8. detection_performance.json

```json
{
  "tests": [
    {
      "test_id": "jizaoan_multi_cancer_screening",
      "test_name": "吉早安多癌早筛",
      "cancer_id": "lung_cancer",
      "sensitivity": 0.88,
      "specificity": 0.92,
      "population": "general adult population",
      "source_id": "vendor_white_paper_2024"
    }
  ]
}
```

Used only for log-odds updates from the Jizaoan multi-cancer screening
result; never extrapolated to other tests.

## 9. screening_recommendations.json

The deep-screening (临床标准) catalog. LLM output never enters this
file. Section 4 of the snapshot report reads it verbatim.

```json
{
  "recommendations": [
    {
      "cancer_id": "colorectal_cancer",
      "cancer_name": "结直肠癌",
      "standard_screening": [
        {
          "method": "结肠镜",
          "population": "中高风险人群",
          "interval": "根据医生建议",
          "trigger": "结肠息肉史、便潜血阳性、家族史",
          "source_id": "guideline_crc_001"
        }
      ]
    }
  ]
}
```

## 10. unstructured_evidence_notes.json

Anything from the raw oncoRAG source that could not be structured into
the schemas above must land here with a reason. Skipping it silently is
forbidden by the v3 spec.

```json
{
  "notes": [
    {
      "factor_id": "obesity_visceral",
      "cancer_id": "colorectal_cancer",
      "reason": "Source only describes qualitative association, no OR/CI.",
      "source_excerpt": "..."
    }
  ]
}
```

## 11. versions/

- `evidence_version.json` carries `{"version": "evidence-v0001", "built_at": "..."}`.
- `changelog.md` records every bump with the diff summary and source pointer.

The manifest layer copies the version string into every analysis run so
downstream consumers can detect ontology drift.

## 12. Invariants enforced by `tests/test_v3_evidence_ontology.py`

- Every `factor_id` in `risk_factors.json` appears either in
  `risk_assertions.json` or in `unstructured_evidence_notes.json`.
- Every assertion references a known `cancer_id` and `factor_id`,
  declares an `effect_type ∈ {OR, RR, HR}`, and has a `source_id`.
- Synonyms and observables only refer to existing `factor_id`s.

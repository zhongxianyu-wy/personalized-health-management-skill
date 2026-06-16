# Risk Prediction Reference

This reference covers the deterministic math behind v3 cancer-risk
prediction — snapshot, screening recommendations, and longitudinal
trend. Open it when working on any script under `scripts/snapshot_risk.py`,
`scripts/longitudinal_risk.py`, or their renderers.

## 1. Hard rules

- LLM must never generate a probability, OR, RR, HR, sensitivity,
  specificity, VoI value, or risk tier.
- All numeric updates come from `references/database/cancerrisk/json/`. The orchestrator
  refuses to run snapshot when the evidence store is missing.
- Cancers not in `references/database/cancerrisk/json/cancers.json` do not appear
  in `snapshot_risk.json`, even as zero.
- Sex-specific cancers whose `applicable_sex` does not match the
  person are emitted with `posterior_probability=null` and
  `applicable=false`. They never get a number.

## 2. Snapshot math

For each cancer `c`:

```
prior          = cancers.json::prior_probability
prior_log_odds = log(prior / (1 - prior))

posterior_log_odds = prior_log_odds + sum_i component_i.log_odds_delta
                                    + (jizaoan_delta if covered else 0)

posterior_probability = exp(L) / (1 + exp(L))     # where L = posterior_log_odds
```

The summation index runs over the **eligible factors** for that cancer.
A factor is eligible iff:

- It is in `references/database/cancerrisk/json/risk_factors.json`.
- It is in `merged_risk_factors.json.current_factor_states`.
- The combination `(cancer_id, factor_id, factor_level)` exists in
  `references/database/cancerrisk/json/risk_assertions_derived.json` or equivalent
  runtime derivation with `conversion_status=usable`.
- OR enters as `ln(OR)`. RR/HR enter as approximate multiplicative evidence
  using `ln(RR)` or `ln(HR)` and must carry `approximation=true`.

## 3. Jizaoan likelihood-ratio update

The Jizaoan multi-cancer screening result is the only screening test
admitted into the log-odds update path.

```
LR+ = sensitivity / (1 - specificity)
LR- = (1 - sensitivity) / specificity
```

If a detection performance record contains a boundary probability, preserve
the original as `raw_sensitivity/raw_specificity`, calculate with 0.99 for
100% or 0.01 for 0%, and mark `probability_boundary_adjustment=true`.

Update rules:

- Result `negative` → for every cancer with detection-performance
  numbers in the ontology, add `log(LR-)` to its posterior log-odds.
- Result `positive` → only the two declared origin cancers
  (`traceability_top1`, `traceability_top2`) receive `log(LR+)`. If a
  top is `unknown`, that slot is skipped.
- Result `unknown` → never enters the log-odds path.

Every update writes a `component_type=screening_test` entry into
`snapshot_risk.json` so reviewers can inspect the contribution.

## 4. Output schema `snapshot_risk.json`

```json
{
  "version": "v3",
  "evidence_version": "evidence-v0001",
  "person": {"sex": "male", "age": 68, "exam_date": "2025-04-18"},
  "cancers": [
    {
      "cancer_id": "gastric_cancer",
      "applicable": true,
      "prior_probability": 0.0021,
      "posterior_probability": 0.011,
      "posterior_log_odds": -4.49,
      "risk_tier": "medium",
      "components": [
        {
          "component_type": "risk_factor",
          "factor_id": "atrophic_gastritis",
          "factor_level": "C1",
          "assertion_id": "asrt_gastric_atrophic_gastritis_001",
          "log_or": 1.163,
          "effect_type": "OR",
          "source_id": "source_001"
        },
        {
          "component_type": "screening_test",
          "test_name": "吉早安多癌早筛",
          "result": "negative",
          "sensitivity": 0.88,
          "specificity": 0.92,
          "likelihood_ratio": 0.13,
          "log_odds_delta": -2.04,
          "source": "detection_performance_knowledge_base"
        }
      ]
    }
  ]
}
```

`risk_tier` is selected by the thresholds in `formal.yaml::risk_prediction.risk_tiers`.

## 5. Section 4 of the snapshot HTML

The v4.2 template controls the layout. v3 replaces only section 4 with
two strategies:

- **便捷式 (convenient)** — recommends the Jizaoan multi-cancer
  screening, framed as one-shot multi-cancer convenience. The renderer
  pulls language from the template and inserts the safety caveats from
  `config/formal.yaml::safety.disclaimer`.
- **深入式 (deep)** — pulls from
  `references/database/cancerrisk/json/screening_recommendations.json`, filtered
  by the person's risk tier per cancer, sex, and age. The LLM is not
  allowed to generate or paraphrase these recommendations.

If a cancer has no entry in the screening catalog, the renderer writes
"暂无针对性筛查建议" rather than fabricating one.

## 6. VoI ranking

VoI is computed from the current individual snapshot probability:

`VoI = posterior_probability × 100000 × (stage1_5y_os - late_stage_5y_os) × 5 × sensitivity`

Single-cancer deep screening uses the knowledge-base method for that
cancer. 吉早安 is one convenience-screening entry whose score is the sum
of covered cancer contributions. Cancers with no posterior probability
are excluded from VoI and should surface through the PPV/imaging path if
applicable.

## 7. Longitudinal logic

Longitudinal risk reads:

- The current `snapshot_risk.json` (always the baseline).
- `output/<person_id>/factor_timeline.json`
  (prior factor values).
- `output/<person_id>/screening_test_timeline.json`
  (prior screening results).
- The current run's `merged_risk_factors.json`, merged in memory only.

Rules:

1. The output schema is `longitudinal_risk.json` and inherits the
   per-cancer structure of the snapshot.
2. Every cancer in the snapshot is processed; there is no top-N filter.
3. Existing docudatabase timelines and current-run events are merged in
   memory for analysis. Longitudinal analysis does not create or modify
   archive files.
4. When no historical archive entries exist, cancers render as
   `baseline_only` with the current snapshot value as the single point.
5. `longitudinal_risk.json::analysis_input` records archived/current/
   merged entry counts and `archive_mutated=false`.

## 8. Archive apply / 入档

`archive_update_proposal.json` is generated after longitudinal analysis.
Only confirmed入档 (`--auto-apply-archive` or equivalent manual apply)
creates or updates `docudatabase/<person_id>/`.

Confirmed入档:

- dedups factor entries by `(factor_key/assertion_key, exam_date, source_data_id)`;
- dedups screening entries by `(test_id, exam_date, source_data_id)`;
- updates `factor_timeline.json`, `screening_test_timeline.json`, and
  `person_index.json`;
- writes `snapshots/<exam_date>.json` containing the run timeline,
  snapshot summary, and processed longitudinal result.

All archive paths must be resolved via
`archive_manager.resolve_person_archive()`.

## 9. Comparable-history rule

A historical record is **comparable** when:

- The same `factor_id` appears in both records.
- Either both records carry a numeric `value` with the same `unit`, or
  both carry the same `factor_level` from `level_schema`.
- The historical record is dated before the current exam date.

Two records with the same `factor_level` but different numeric values
(e.g. a 3 mm nodule and a 5 mm nodule both `present`) are comparable
only when the schema treats numeric size as the comparison axis. That
choice lives in `formal.yaml::risk_prediction` (planned in Task 10).

## 10. Auditing

`snapshot_risk.json` and `longitudinal_risk.json` must include enough
provenance to reproduce every number: each component must declare its
`source_id` or `assertion_id`, and the manifest must record the
ontology version used. Reviewers can replay the math with only these
two files plus `references/database/cancerrisk/json/`.

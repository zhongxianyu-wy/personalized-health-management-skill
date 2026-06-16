# Runtime Workflow Reference

This file holds the detailed operating notes that do not belong in
`SKILL.md`. Load it when executing or debugging the staged pipeline.

## Stop Points

Useful `--stop-after` values:

- `mineru`: OCR done, refine files next.
- `interactive`: questionnaire built or answers applied.
- `master-template`: closed-vocabulary fill templates ready.
- `risk-factor-gate`: candidate records validated and merged.
- `health-summary-api`: upstream API response and summary skeleton ready.
- `health-summary`: health summary rendered.
- `snapshot`: snapshot risk rendered.
- `longitudinal`: longitudinal analysis done, archive not necessarily applied.
- `archive-proposal`: longitudinal done and入档 proposal written.

## Checkpoint 1: Refine

Input: `artifacts/mineru/<data_id>/content.md`.
Output: `artifacts/mineru/<data_id>/refined.md`.

**Do NOT write Python scripts or temporary files to batch-process mineru
results.** Read each `content.md` directly with the Read tool and write
the corresponding `refined.md` directly with the Write tool. Inline
Python/heredoc scripts cause indentation errors and spawn unnecessary
temporary files — process files one by one using the native tools.

Keep only:

- demographics and exam/report dates;
- abnormal lab rows carrying markers such as `↑`, `↓`, `阳性`, `异常`,
  or values outside the stated reference range;
- tumor-marker rows even when normal: AFP, CEA, CA19-9, CA125, PSA,
  f-PSA, CYFRA21-1, SCC, NMP22, CA72-4, CA15-3;
- imaging/test conclusions under headings such as 小结, 结论, 印象,
  诊断, 建议;
- positive physical findings.

Do not summarize away values, units, reference ranges, dates, or source
phrases needed for substring validation.

## Checkpoint 2: Interactive Answers

The questionnaire is fixed and sex-branched in
`config/formal.yaml::interactive.required_questions`.

Real runs must ask the user. Do not infer lifestyle, family history, or
recent screening status from a medical report. Validate answers before
rerun:

```bash
python scripts/validate_answers.py \
  --questionnaire <out>/artifacts/interactive_questionnaire.json \
  --answers <answers.json>
```

`--allow-empty-interactive` is for explicit prior-only tests, not real
runs.

## Checkpoint 3: Master Fill

Inputs:

- `artifacts/risk_factor_master.json`
- `artifacts/structured_risk_factors_timeline.candidate.json`
- all `refined.md`
- `interactive_answers.md`

Rules:

- Use only `valid_factor_keys`.
- Append a record only when source text supports or explicitly denies
  the factor.
- `exam_date` is report date; use `"now"` only for
  `interactive_answers.md`. The gate rewrites it to run date.
- `evidence_text` must be copied from `source_md`. Strip markdown bold/italic markers (`**`, `*`, `_`) from the quoted text — the gate validates by substring match after normalizing those markers.
- Do not create factors for evidence outside the ontology; those belong
  in display-only abnormal findings, not probability math.

Validate:

```bash
python scripts/validate_timeline_candidate.py \
  --candidate <out>/artifacts/structured_risk_factors_timeline.candidate.json
```

Then re-run with `--stop-after cp3-verify` to trigger the mandatory
verification audit (CP3.1).

## Checkpoint 3.1: Verification Audit

Triggered by `--stop-after cp3-verify`. The orchestrator prints a
structured audit task and exits. This step is **mandatory** — do not
skip it even when confident CP3 is complete.

Inputs:

- `artifacts/structured_risk_factors_timeline.candidate.json` (must be
  filled; orchestrator errors if records list is absent)
- all `refined.md` listed in the audit task

Agent role: independent auditor, NOT the CP3 extractor.

Rules:

- Read each `refined.md` independently, without anchoring to the
  existing candidate records.
- Identify all clinical abnormal findings in the document.
- For each finding absent from the candidate, look up `risk_factor_master.json`
  for a matching `factor_key` (by `factor_name`, `synonyms`, or
  `factor_id`).
- If match found and document supports it: add record (`exists=true`).
- If match found and document explicitly denies it: add record
  (`exists=false`).
- If no match in master: do not add; the finding goes into
  "证据库外异常提示" automatically.
- `evidence_text` must be a literal substring of the named `refined.md`
  (same gate rule as CP3).

After audit (with or without additions), re-run the orchestrator
without `--stop-after cp3-verify` to proceed to the Gate.

## Imaging Findings

Imaging findings are timeline records whose master row has
`factor_type == "imaging_finding"`. The agent only identifies the
finding in source text. PPV metadata comes from the master/evidence
store, not the agent.

Typical targets:

- lung CT nodules by size/morphology;
- breast BI-RADS grades;
- thyroid TI-RADS grades or high-risk calcified nodules.

When imaging PPV dominates, snapshot uses an imaging-workup tier rather
than the low/medium/high incidence tier.

## Tumor Markers

Tumor markers are screening tests, not OR risk factors. Fill
`tumor_markers.candidate.json` using only `valid_test_ids`.

Normal in-range values are `negative`; above-threshold or flagged values
are `positive`. The gate joins `test_id` to
`detection_performance_derived.json` and applies LR+/LR-.

Validate:

```bash
python scripts/validate_tumor_markers.py \
  --candidate <out>/artifacts/tumor_markers.candidate.json
```

## Checkpoint 4: Health Summary Structuring

Input:

- `health_summary_api_response.md`
- `refined_content_bundle.md`
- summary skeleton JSON

Use `scripts/finalize_structured_summary.py` rather than hand-writing
JSON. Put large HTML fragments in files and reference them via `@path`
inside the fills JSON. Preserve `raw_assessment_markdown` verbatim.

Health summary is display-only. Do not use snapshot/longitudinal
probabilities, ontology data, OR/LR, or screening recommendation logic
inside it.

## Archive Confirmation

Longitudinal analysis runs before archive mutation. It merges existing
docudatabase timelines with current events in memory and writes
`longitudinal_risk.json`.

After that, `archive_update_proposal.json` is the入档 prompt. Confirmed
apply writes deduped current events and the processed longitudinal
result into `docudatabase/<person_id>/`.

## Common Failure Signals

- `interactive_skipped.warning`: user answers were not collected.
- `master_fill_skipped.warning`: CP3 fill was skipped.
- `unknown_factor_key`: agent invented a factor outside master.
- `evidence_text_not_found`: evidence was paraphrased or copied from
  the wrong source file.
- `json.JSONDecodeError` in health summary: use
  `finalize_structured_summary.py`, not heredoc JSON.

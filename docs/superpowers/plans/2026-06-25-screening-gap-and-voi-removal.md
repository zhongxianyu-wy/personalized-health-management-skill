# Independent Screening Gap and VoI Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an independent LLM-driven CP5 screening-gap workflow with deterministic PUA validation, while completely removing unused VoI analysis from the production pipeline and report contract.

**Architecture:** The orchestrator stops after snapshot generation and asks the agent to produce a screening draft plus an independent gap questionnaire. New validators check evidence traceability, A/B/C deduplication, questionnaire coverage, answer completeness, and final recommendation consistency without generating or modifying medical recommendations. The report continues to consume the existing five section artifacts; liquid-biopsy performance is read directly from `detection_performance.json`, eliminating the only residual VoI dependency.

**Tech Stack:** Python 3.11, pytest, JSON artifacts, Markdown knowledge base, Jinja2.

---

## File Structure

- Create `scripts/validate_screening_gap.py`: pure CP5 draft/questionnaire/answer/final validators.
- Create `tests/test_validate_screening_gap.py`: unit tests for every CP5 PUA rule.
- Create `tests/test_screening_gap_orchestrator_contract.py`: stop point, CLI and halt-code contract tests.
- Modify `scripts/run_formal_analysis.py`: remove VoI, add CP5 stop point/argument/gates.
- Modify `scripts/build_report_json.py`: remove `report.voi`; read liquid-biopsy performance from detection performance data.
- Modify report tests to remove VoI fixtures and assert the new performance source.
- Delete `scripts/voi_calculator.py`, `references/database/cancerrisk/json/voi_parameters.json`, `templates/integrated_report_v14.html`, and the template-only VoI test.
- Modify `SKILL.md`, `references/runtime_workflow.md`, `references/缺口筛查与交互确认.md`, `references/risk_prediction.md`, and database indexes.

### Task 1: Lock the VoI Removal Contract

**Files:**
- Modify: `tests/test_p1_build_report_json.py`
- Modify: `tests/test_report_temp.py`
- Modify: `tests/test_p1_orchestrator_stop_after.py`

- [ ] **Step 1: Write failing report-contract tests**

Update the expected report keys to remove `voi`, remove `voi_ranking.json` fixtures, and add:

```python
def test_report_has_no_voi_contract(artifacts: Path, answers_path: Path) -> None:
    result = _assemble(artifacts, answers_path)
    assert "voi" not in result
```

- [ ] **Step 2: Write failing liquid-biopsy source tests**

Replace VoI fallback tests with a temporary evidence store containing:

```json
{
  "tests": [{
    "test_id": "jizaoan_multi_cancer_screening_overall",
    "sensitivity": 0.819,
    "specificity": 0.99
  }]
}
```

Assert:

```python
assert report["liquid_biopsy_perf"]["sensitivity"] == "81.9%"
assert report["liquid_biopsy_perf"]["specificity"] == "99.0%"
```

- [ ] **Step 3: Write a failing orchestrator source test**

Add a source-level contract asserting:

```python
source = (SCRIPTS_DIR / "run_formal_analysis.py").read_text(encoding="utf-8")
assert "import voi_calculator" not in source
assert "run_voi_stage" not in source
```

- [ ] **Step 4: Run tests and verify RED**

Run:

```bash
python -m pytest tests/test_p1_build_report_json.py tests/test_report_temp.py tests/test_p1_orchestrator_stop_after.py -q
```

Expected: failures because report assembly and orchestrator still consume VoI.

### Task 2: Remove VoI from Runtime and Report Assembly

**Files:**
- Modify: `scripts/run_formal_analysis.py`
- Modify: `scripts/build_report_json.py`
- Delete: `scripts/voi_calculator.py`
- Delete: `references/database/cancerrisk/json/voi_parameters.json`

- [ ] **Step 1: Remove the orchestrator VoI stage**

Delete the import, `run_voi_stage` call, logging, and all stop-point text that says `snapshot/VoI`.

- [ ] **Step 2: Add the evidence-store parameter to report assembly**

Change the signature:

```python
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
```

The default points to `references/database/cancerrisk/json`.

- [ ] **Step 3: Replace VoI liquid-biopsy fallback**

Implement a pure loader:

```python
def _overall_jizaoan_performance(evidence_store: Path) -> tuple[Any, Any]:
    payload = _read_json(evidence_store / "detection_performance.json", {})
    for row in payload.get("tests", []):
        if row.get("test_id") == "jizaoan_multi_cancer_screening_overall":
            return row.get("sensitivity"), row.get("specificity")
    return None, None
```

Always override LLM-provided sensitivity/specificity with valid authoritative values.

- [ ] **Step 4: Remove `voi` from report JSON**

Delete the `voi_ranking.json` read and the `report["voi"]` block.

- [ ] **Step 5: Delete obsolete runtime files**

Delete the calculator and VoI parameter JSON only after tests no longer import them.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run:

```bash
python -m pytest tests/test_p1_build_report_json.py tests/test_report_temp.py tests/test_p1_orchestrator_stop_after.py -q
```

Expected: all pass.

### Task 3: Define CP5 PUA Validators

**Files:**
- Create: `scripts/validate_screening_gap.py`
- Create: `tests/test_validate_screening_gap.py`

- [ ] **Step 1: Write failing draft validation tests**

Cover:

```python
def test_draft_rejects_duplicate_dedup_keys_across_sections(...): ...
def test_draft_rejects_missing_guideline_evidence(...): ...
def test_draft_rejects_evidence_not_literal_source_substring(...): ...
def test_draft_rejects_periodic_candidate_without_timeline_audit(...): ...
def test_draft_accepts_never_recorded_with_empty_timeline_evidence(...): ...
def test_draft_rejects_cancer_below_medium_snapshot_tier(...): ...
```

- [ ] **Step 2: Write failing questionnaire validation tests**

Cover:

```python
def test_questionnaire_requires_done_and_result_pair_per_candidate(...): ...
def test_questionnaire_rejects_dedup_removed_candidate(...): ...
def test_questionnaire_accepts_empty_candidates_with_empty_questions(...): ...
```

- [ ] **Step 3: Write failing answer and final validation tests**

Cover:

```python
def test_answers_require_triggered_result_question(...): ...
def test_final_rejects_done_normal_in_periodic_management(...): ...
def test_final_rejects_missing_disposition_for_not_done(...): ...
def test_final_rejects_duplicate_keys_across_abc(...): ...
def test_final_accepts_empty_periodic_management_when_no_candidates(...): ...
```

- [ ] **Step 4: Run tests and verify RED**

Run:

```bash
python -m pytest tests/test_validate_screening_gap.py -q
```

Expected: import failure because the validator does not exist.

- [ ] **Step 5: Implement pure validation functions**

Expose:

```python
def validate_draft(draft, *, snapshot, knowledge_root, timeline_text) -> list[str]
def validate_questionnaire(draft, questionnaire) -> list[str]
def validate_answers(questionnaire, answers) -> list[str]
def validate_final(draft, questionnaire, answers, final) -> list[str]
```

Functions return error strings and never mutate input.

- [ ] **Step 6: Add CLI modes**

Support:

```bash
python scripts/validate_screening_gap.py draft ...
python scripts/validate_screening_gap.py questionnaire ...
python scripts/validate_screening_gap.py answers ...
python scripts/validate_screening_gap.py final ...
```

Exit `0` when valid, `1` for semantic validation failures, `2` for missing or malformed input.

- [ ] **Step 7: Run tests and verify GREEN**

Run:

```bash
python -m pytest tests/test_validate_screening_gap.py -q
```

Expected: all pass.

### Task 4: Wire the Independent CP5 Checkpoint

**Files:**
- Modify: `scripts/run_formal_analysis.py`
- Create: `tests/test_screening_gap_orchestrator_contract.py`

- [ ] **Step 1: Write failing stop-point and CLI tests**

Assert:

```python
assert "screening-gap" in STOP_AFTER_CHOICES
```

and parser help contains `--screening-gap-answers`.

- [ ] **Step 2: Write failing source-order tests**

Assert CP5 appears after snapshot and before archive/report artifacts, and no CP5 question is read from `--answers`.

- [ ] **Step 3: Implement stop point and argument**

Add:

```python
parser.add_argument("--screening-gap-answers", default=None)
```

After snapshot, if `--stop-after screening-gap`, print the exact CP5A input/output instructions and return.

- [ ] **Step 4: Implement CP5 gates**

For downstream execution:

1. Require and validate:
   - `screening_recommendations_draft.json`
   - `screening_gap_questionnaire.json`
2. If questionnaire has questions and no independent answers, exit `12`.
3. Validate independent answers.
4. Require and validate `screening_recommendations_final.json`; exit `13` if missing/inconsistent.
5. Never generate or edit recommendation artifacts.

Use exit `11` for draft/questionnaire failures.

- [ ] **Step 5: Write CP5 audit output**

Create `module_audits/task_cp5_screening_gap.md` recording counts and validation status, not medical content.

- [ ] **Step 6: Run focused tests**

Run:

```bash
python -m pytest tests/test_screening_gap_orchestrator_contract.py tests/test_p1_orchestrator_stop_after.py -q
```

Expected: all pass.

### Task 5: Enforce CP5 Against Final Report Artifacts

**Files:**
- Modify: `scripts/validate_screening_gap.py`
- Modify: `scripts/run_formal_analysis.py`
- Modify: `tests/test_validate_screening_gap.py`

- [ ] **Step 1: Write failing artifact consistency tests**

Cover:

```python
def test_maintain_must_match_final_periodic_management(...): ...
def test_package_items_cannot_reintroduce_duplicate_screening_keys(...): ...
```

The package check only validates explicit `dedup_key` annotations when present; it must not infer medical equivalence.

- [ ] **Step 2: Implement final-artifact validation**

Add:

```python
def validate_report_artifacts(final, timeline_tiers, package_tiers) -> list[str]
```

Require `timeline_tiers.maintain` entries to carry `dedup_key` and match final periodic management exactly.

- [ ] **Step 3: Invoke the gate before report assembly**

If validation fails, exit `13` with actionable errors. Do not rewrite artifacts.

- [ ] **Step 4: Run focused tests**

Run:

```bash
python -m pytest tests/test_validate_screening_gap.py -q
```

Expected: all pass.

### Task 6: Update the Skill and Runtime Documentation

**Files:**
- Modify: `SKILL.md`
- Modify: `references/runtime_workflow.md`
- Modify: `references/缺口筛查与交互确认.md`
- Modify: `references/risk_prediction.md`
- Modify: `references/database/index.json`
- Modify: `references/database/cancerrisk/index.json`

- [ ] **Step 1: Update pipeline and workflow**

Remove VoI from stages and artifacts. Add CP5 after snapshot with independent answer file and exits 11–13.

- [ ] **Step 2: Rewrite the gap reference**

Make it explicit that LLM performs gap analysis and deduplication; scripts only validate.

- [ ] **Step 3: Remove VoI reference material and indexes**

Delete the VoI section and all loader/index entries.

- [ ] **Step 4: Update contract tests**

Add SKILL.md assertions for `screening-gap`, `screening_gap_answers.json`, and absence of `VoI`.

- [ ] **Step 5: Run documentation contract tests**

Run:

```bash
python -m pytest tests/test_p1_skill_md_contract.py tests/test_v14_kb_index.py -q
```

Expected: all pass.

### Task 7: Delete Obsolete Template and Test Surface

**Files:**
- Delete: `templates/integrated_report_v14.html`
- Delete: `tests/test_p2_sections.py`
- Modify: `config/formal.yaml`
- Modify tests that still create `voi_ranking.json`

- [ ] **Step 1: Remove stale template configuration**

Delete `snapshot_template: templates/integrated_report_v14.html` if no production consumer remains.

- [ ] **Step 2: Delete obsolete template and exclusive test**

The sole authority remains `templates/integrated_report_temp.html`.

- [ ] **Step 3: Remove all remaining VoI fixtures**

Search:

```bash
grep -RIn -E '\bVoI\b|\bvoi\b|voi_' SKILL.md scripts templates tests references config
```

Keep no production/runtime references.

- [ ] **Step 4: Run report/template tests**

Run:

```bash
python -m pytest tests/test_p1_render_report.py tests/test_p1_template_structure.py tests/test_report_temp.py tests/test_report_v20.py -q
```

Expected: all pass.

### Task 8: Full Verification and Skill Evaluation

**Files:**
- Create or modify eval assets only as required by `skill-creator` and `darwin-skill`.

- [ ] **Step 1: Run syntax and focused checks**

Run:

```bash
python -m py_compile scripts/run_formal_analysis.py scripts/build_report_json.py scripts/validate_screening_gap.py
python -m pytest tests/test_validate_screening_gap.py tests/test_screening_gap_orchestrator_contract.py -q
```

- [ ] **Step 2: Run the full suite**

Run:

```bash
python -m pytest -q
```

Expected: all tests pass, with only previously documented skips.

- [ ] **Step 3: Run repository residue checks**

Run:

```bash
grep -RIn -E '\bVoI\b|\bvoi\b|voi_' SKILL.md scripts templates tests references config
git diff --check
git status --short
```

Expected: no VoI production references and no whitespace errors.

- [ ] **Step 4: Run skill-creator evaluation**

Use realistic cases covering never-recorded, overdue, date-unknown, cancer-risk duplicate, abnormality duplicate, and colonoscopy aliases.

- [ ] **Step 5: Run darwin-skill assessment**

Evaluate the final `SKILL.md` and CP5 workflow. Treat its score as advisory; retain changes only when they preserve the confirmed architecture and pass tests.

- [ ] **Step 6: Commit implementation**

Stage only files belonging to this feature; do not include pre-existing `scripts/master_scan.py` or `assemble.stderr`.


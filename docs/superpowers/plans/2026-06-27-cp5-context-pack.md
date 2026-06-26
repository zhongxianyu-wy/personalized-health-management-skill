# CP5 Context Pack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add v2.0.5 CP5 context pack generation to reduce LLM workload before recommendation screening.

**Architecture:** Add a focused deterministic builder script that creates `artifacts/cp5_context_pack.json` from existing artifacts and the periodic screening schedule. Wire it into `run_formal_analysis.py` immediately before the `screening-gap` stop point; update skill docs so LLM reads the compact pack first.

**Tech Stack:** Python 3.11, pytest, JSON artifacts, existing `run_formal_analysis.py` orchestrator.

---

### Task 1: Add CP5 context pack builder tests

**Files:**
- Create: `tests/test_build_cp5_context_pack.py`

- [ ] **Step 1: Write failing tests**

Create tests for age/sex prefiltering, timeline evidence matching, and medium+ snapshot extraction.

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m pytest tests/test_build_cp5_context_pack.py -q`

Expected: import failure because `build_cp5_context_pack.py` does not exist.

### Task 2: Implement `build_cp5_context_pack.py`

**Files:**
- Create: `scripts/build_cp5_context_pack.py`

- [ ] **Step 1: Implement `build_context_pack(...)`**

Read artifacts and schedule JSON. Return a dict with schema version, person context, medium+ cancer risk rows, health abnormalities, periodic candidates, screening evidence, dedup seed keys, warnings, and LLM instructions.

- [ ] **Step 2: Add CLI**

Support:

```bash
python3 scripts/build_cp5_context_pack.py --artifacts <out>/artifacts --knowledge-root references/database
```

Expected output: writes `cp5_context_pack.json` under artifacts.

- [ ] **Step 3: Run tests**

Run: `python3 -m pytest tests/test_build_cp5_context_pack.py -q`

Expected: pass.

### Task 3: Wire builder into orchestrator

**Files:**
- Modify: `scripts/run_formal_analysis.py`
- Test: `tests/test_screening_gap_orchestrator_contract.py`

- [ ] **Step 1: Add failing contract test**

Assert `run_formal_analysis.py` imports/calls `build_cp5_context_pack.build_context_pack`.

- [ ] **Step 2: Implement orchestration**

After snapshot generation and before `if args.stop_after == "screening-gap"`, call builder and print context pack path.

- [ ] **Step 3: Run contract tests**

Run: `python3 -m pytest tests/test_screening_gap_orchestrator_contract.py -q`

Expected: pass.

### Task 4: Update workflow documentation

**Files:**
- Modify: `SKILL.md`
- Modify: `references/缺口筛查与交互确认.md`
- Modify: `references/runtime_workflow.md`

- [ ] **Step 1: Update CP5 instructions**

State that LLM must read `artifacts/cp5_context_pack.json` first, then only回查 source artifacts when evidence is insufficient.

- [ ] **Step 2: Add contract checks**

Update `tests/test_p1_skill_md_contract.py` if necessary to require `cp5_context_pack.json` mention.

### Task 5: Verify and commit

- [ ] **Step 1: Run full tests**

Run: `python3 -m pytest -q`

Expected: all tests pass.

- [ ] **Step 2: Commit**

Commit message:

```bash
git commit -m "feat(v2.0.5): add cp5 context pack"
```

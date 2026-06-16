# Risk-Factor Event Format

Open this file when adding a new consumer of `merged_risk_factors.json`,
`factor_timeline.json`, or any `assertion_events` list. Every consumer must
support **both** event shapes; treating one as canonical and silently
dropping the other is the most common Task4-vs-Task7 integration bug in
this codebase.

## Two equivalent shapes

The v3 ontology produces risk events in two equivalent shapes depending on
where they come from:

| Shape | Producer | Identity key | Why it exists |
|---|---|---|---|
| **Slim (factor-level)** | `build_assertion_fill_template.py` (Task2) → Task4 gate output → Task5 merge | `factor_key = "factor_id|factor_level"` | One observed factor can apply to N cancers; the slim shape lets the agent fill the factor **once** instead of N times |
| **Full (cancer-expanded)** | Legacy fixed-assertion fill (Task4 v3.2), runtime-injected events, ontology fixtures | `assertion_key = "cancer_id|factor_id|factor_level|assertion_id"` | One event per (cancer, factor, level, source assertion) — useful when the source provides per-cancer odds-ratio evidence directly |

Both shapes carry the same observation semantics (`exists`,
`evidence_text`, `exam_date`, `source`, `confidence`, `negated`, …).
Only the identity key differs.

## Required bridging at consumption time

Anywhere downstream code needs to (a) **dedup** events or (b) **join** an
event to per-cancer log-odds evidence, it must handle both shapes:

### Dedup key

```python
def event_dedup_key(event: dict) -> str | None:
    # Use whichever identity is present. assertion_key is preferred when
    # both are set (it's strictly more specific).
    return event.get("assertion_key") or event.get("factor_key")
```

Reference implementations:
- `merge_risk_factors.py::_event_dedup_key`
- `archive_manager.py::_factor_dedup_key`

### Join to derived risk evidence (Task7 / Task8 longitudinal)

For each cancer being scored:

```python
if event.get("assertion_key"):
    # Full shape: split out the cancer_id from the key; reject if not
    # for this cancer; join by assertion_id.
    cancer_id_in_key, _, _, assertion_id = event["assertion_key"].split("|")
    if cancer_id_in_key != cancer_id:
        continue
    derived = derived_by_assertion_id.get(assertion_id)
else:
    # Slim shape: join by (cancer_id, factor_id, factor_level) — one
    # observation can fan out to multiple cancers.
    derived = derived_by_factor_level[(cancer_id, event["factor_id"], event["factor_level"])]
```

Reference implementations:
- `snapshot_risk.py::_components_for_cancer`
- `longitudinal_risk.py::_timeline_points_for_cancer`

## How to know which shape you'll get

| Source file | Shape produced |
|---|---|
| `risk_factor_assertion_template.json` from `build_assertion_fill_template.py` | Slim |
| `mineru/<data_id>/risk_factor_extraction.candidate.json` (current) | Slim |
| `structured_risk_factors.json` (current gate output) | Slim |
| `risk_factor_assertion_status.json::assertion_events` (current gate output) | Slim |
| `merged_risk_factors.json::factor_events` & `current_factor_states` | Slim (may carry interactive patches with assertion_key) |
| `merged_risk_factors.json::screening_tests` | Neither; keyed by `test_id` |
| `archive/<person>/factor_timeline.json` | Whatever was added (slim today, may be mixed if legacy data exists) |
| Test fixtures in `tests/test_v3_risk_prediction.py` | Full (uses cancer-expanded `assertion_key`) |

## Writing a new consumer — checklist

1. **Iterate** over events without assuming an identity key:
   ```python
   for event in events:
       did_assertion_key = event.get("assertion_key")
       did_factor_key = event.get("factor_key")
       if not (did_assertion_key or did_factor_key):
           continue  # malformed, log and skip
   ```
2. **Dedup** with the helper above. Do not key directly off
   `assertion_key`.
3. **Join** with the cancer-aware logic above. Do not key directly off
   `assertion_id`.
4. **Add a regression test** that feeds both shapes into your consumer and
   asserts identical behaviour. Copy fixtures from
   `tests/test_v3_archive_timeline.py::test_longitudinal_replays_history_across_timeline`
   for a starting point.

If you find yourself writing `event["assertion_key"]` without an
`or event["factor_key"]` fallback, stop — that line will silently drop
slim Task4 events and your stage will be empty for any non-fixture run.

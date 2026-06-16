# Health Summary Reference

Open this file only when working on Task6 health-summary integration,
`scripts/render_health_summary.py`, or `templates/health_summary_v1.html`.

## Contract

Task6 replays `健康管理-v1.0.0` inside CancerRisk v3. It does not invent a new
summary algorithm and does not keyword-extract findings from OCR text. The
upstream skill kept the work in three explicit steps — call the API, get a
natural-Markdown assessment, then have the agent assemble the structured
report data — and Task6 mirrors that split.

## Input

Task6 has exactly one business input:

| Input | Source |
|---|---|
| `analysis_output/artifacts/refined_content_bundle.md` | Task4 refined MinerU markdown bundle |

Do not read Task5 supplemental data, ontology JSON, `merged_risk_factors.json`,
snapshot output, or longitudinal output. Health summary is display-only and
must not influence probability math.

## Runtime Strategy (three phases)

### Phase A — API call (orchestrator, automatic)

1. Read `refined_content_bundle.md`.
2. Build the same system + user messages used by `健康管理-v1.0.0` (see
   `scripts/render_health_summary.py::_build_messages`). The prompt asks for
   the natural 阶段一…阶段五 Markdown assessment and explicitly forbids JSON
   or fenced code blocks.
3. Fetch the external API token from `https://jiyinjia.jinbaisen.com/!token?key=skill_jk`.
4. Call `https://ydai.jinbaisen.com/api/v1/chat/completions` with model `cyzh-cfc`,
   stream the response, and concatenate the `delta.content` chunks.
5. Save artifacts:
   - `analysis_output/artifacts/health_summary_api_messages.json` — request audit.
   - `analysis_output/artifacts/health_summary_api_response.md` — raw Markdown
     reply, preserved 1:1.
   - `analysis_output/artifacts/health_summary_input_bundle.md` — the refined
     markdown actually fed to the API.
   - `analysis_output/artifacts/health_summary_structured_summary.json` —
     **placeholder** with `status: "awaiting_agent_structuring"` and seeded
     `patient_data` extracted from the bundle. The agent rewrites this file
     in phase B.

### Phase B — Agent structures the response (skill-native)

The skill agent (Claude running this skill) follows the *Task6 structuring
recipe* in `SKILL.md`. It reads `health_summary_api_response.md` and
`refined_content_bundle.md`, then overwrites
`health_summary_structured_summary.json` with the same shape `健康管理-v1.0.0`
hands to `HealthAssistant.generate_html_report(patient_data, assessment_result)`:

```json
{
  "status": "ready_for_render",
  "patient_data": {
    "name": "…", "age": "…", "gender": "…", "region": "…", "job": "…",
    "medical_history": "…", "family_history": "…", "lifestyle": "…",
    "medication": "…", "exam_date": "…", "exam_source": "…"
  },
  "assessment_result": {
    "adr_score": "…", "risk_level": "…", "core_risk_factors": "…",
    "overall_assessment": "…",
    "lab_results_table": "<table>…</table>",
    "abnormal_table": "<table>…</table>",
    "disease_cards": "<div class=\"disease-card\">…</div>",
    "advice_list": "<ul class=\"advice-list\">…</ul>",
    "conclusion_table": "<table>…</table>"
  },
  "raw_assessment_markdown": "<verbatim API response>",
  "contact": { … }
}
```

`raw_assessment_markdown` must be kept verbatim — phase C re-renders it under
the "API原始反馈" section to preserve the upstream skill's "无损呈现" discipline.

### Phase C — HTML render (orchestrator, automatic)

The orchestrator reads the now-completed structured summary, validates that
every patient/assessment field is non-empty, then fills the copied
`templates/health_summary_v1.html`. The template is unchanged from the
upstream `健康管理-v1.0.0` skill. There is no JSON parsing of the API response
in this phase and no fallback that fabricates "见原文" placeholders.

## Outputs

```text
analysis_output/health_summary.html
analysis_output/artifacts/health_summary_input_bundle.md
analysis_output/artifacts/health_summary_api_messages.json
analysis_output/artifacts/health_summary_api_response.md
analysis_output/artifacts/health_summary_structured_summary.json
analysis_output/module_audits/task06_health_summary.md
```

`health_summary_input_bundle.md` contains only the refined markdown consumed by
Task6. `health_summary_api_messages.json` records the external API request
messages for audit. `health_summary_api_response.md` preserves the upstream
health-management response.

## Progressive Disclosure

Keep `SKILL.md` short. Load this file only for Task6 work. Load the copied HTML
template only when changing rendered fields or styling. Do not load evidence
ontology or risk-prediction references for Task6.

## Boundaries

- Do not call CancerRisk risk evidence scripts.
- Do not calculate cancer probabilities.
- Do not use Task5 interactive answers.
- Do not extract findings with local keyword rules beyond the conservative
  patient-demographic seed regex.
- Do not edit the copied HTML template; structural changes must be matched in
  `upstream 健康管理-v1.0.0/templates/report_template.html` first.

## Tests

`tests/test_v3_health_summary.py` uses an injected fake API caller to verify the
contract without transmitting health data externally. The fake returns natural
Markdown and a `structured_summary_provider` callback fills the structured
summary the same way the agent would in production. Live API tests require
explicit user-side execution because they send health-report text to the
configured external service.

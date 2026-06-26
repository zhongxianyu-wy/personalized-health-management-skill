# CP5 Context Pack Design

## 背景

v2.0.4 的 CP5 自动校验耗时已经在毫秒级；实际耗时来自 LLM 读取完整知识库、报告原文、历史时间线后再筛选周期检查、查找既往检查记录和去重。v2.0.5 的目标是减少 LLM 输入规模，而不改变“LLM 判断医学推荐、脚本只做门控/校验”的原则。

## 设计目标

1. 在 `--stop-after screening-gap` 前自动生成 `artifacts/cp5_context_pack.json`。
2. context pack 只做确定性预处理：年龄/性别周期筛查预筛、时间线粗匹配、medium+ 癌症摘要、健康总结异常摘要。
3. LLM 仍负责 A/B/C 最终推荐、医学去重和文案，不由脚本自动生成 `screening_recommendations.json`。
4. 缺少部分输入时降级为空数组或 `null`，不阻断流水线。

## 输出结构

`cp5_context_pack.json`：

```json
{
  "schema_version": "cp5-context-pack-v1",
  "person_context": {"sex": "male", "age": 68},
  "cancer_risk_medium_plus": [],
  "health_abnormalities": [],
  "periodic_candidates_prefiltered": [],
  "existing_screening_evidence": [],
  "dedup_seed_keys": [],
  "llm_instructions": []
}
```

`periodic_candidates_prefiltered` 每项包含 `dedup_key/name/method/interval_years/high_risk_only/gap_prefill/matched_evidence`。`gap_prefill` 只允许脚本给出粗状态：`never_seen`、`seen_with_date`、`seen_without_date`。

## 数据流

1. `run_formal_analysis.py` 完成健康总结和 `snapshot_risk.py` 后调用 `build_cp5_context_pack.build_context_pack(...)`。
2. 脚本读取：
   - `snapshot_risk.json`
   - `health_summary_structured_summary.json`
   - `screening_test_timeline.json`
   - `references/database/screening_general/json/periodic_screening_schedule.json`
3. 脚本写入 `artifacts/cp5_context_pack.json`。
4. `--stop-after screening-gap` 提示 agent 优先读取该 context pack。
5. 后续 CP5 gate 仍只校验 `screening_recommendations.json`。

## 错误处理

- 周期表缺失：输出空 `periodic_candidates_prefiltered`，并在 `warnings` 记录原因。
- 人口学缺失：输出空周期候选，记录 `missing_demographics`。
- 时间线缺失：所有候选 `gap_prefill=never_seen`，但 `matched_evidence=[]`。
- 日期解析只做 ISO/常见年月日格式的粗解析；无法解析时标为 `seen_without_date`。

## 测试策略

1. 年龄/性别过滤：68 岁男性应获得男性适用和通用项，不含女性专属项。
2. 时间线粗匹配：已有 LDCT 日期时，肺癌筛查候选应为 `seen_with_date`。
3. snapshot 摘要：只输出 `medium/high/very_high/moderate_workup/high_workup/urgent_workup`。
4. 编排器契约：`run_formal_analysis.py` 引入并调用 `build_cp5_context_pack`，`SKILL.md` 指向 `cp5_context_pack.json`。

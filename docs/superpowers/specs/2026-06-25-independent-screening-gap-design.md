# 独立周期性筛查缺口确认设计

**版本目标：** 修复 v2.0.1 中 `references/缺口筛查与交互确认.md` 仅被引用、未在 pipeline 中形成独立执行检查点的问题。

**确认日期：** 2026-06-25

## 1. 设计原则

本功能遵循项目既定的“脚本与 LLM 分工”：

- **脚本确定性计算**：仅用于贝叶斯后验概率、VoI、价格等已有数值计算。
- **LLM + 知识库判断**：用于异常解读、筛查推荐、缺口识别、时间周期判断、检查项目归并和去重、患者面向文案。
- **脚本 PUA 门控**：用于检查阶段是否执行、输入输出是否齐全、schema 是否正确、知识库证据能否追溯、交互回答是否完整、最终结果是否违反已确认规则。

脚本不得新增年龄/性别筛查规则计算器、日期超期判定器或推荐项目自动去重器。脚本可以发现并拒绝不合规结果，但不能替代 LLM 做医学推荐判断。

## 2. 推荐筛查三部分

推荐筛查由 LLM 在同一推荐阶段综合生成，但结果必须明确分为三类：

### A. 癌症风险筛查

- 输入：`snapshot_risk.json` 的快照式后验结果。
- 仅纳入中等风险及以上癌症。
- 超过三个癌种时沿用现有 TOP3 规则。
- 推荐方法和周期来自对应癌种筛查知识库。

### B. 其他疾病或异常指标复查

- 输入：健康总结结构化结果中的癌症外疾病和异常指标。
- 剔除已由 A 表达的癌症风险及其重复推荐。
- 推荐方法和周期来自异常指标、慢病及专科筛查知识库。

### C. 周期性筛查管理

- 输入：年龄、性别、当前体检资料、历史档案时间线和常规筛查知识库。
- LLM 识别：
  - 档案中没有任何相关检查信息；
  - 有检查记录，但距今已超过指南周期；
  - 有检查记录，但日期不完整，无法确认是否仍在指南周期内。
- C 在提出问题前必须剔除已被 A 或 B 推荐的检查项目。
- 只有独立交互确认后仍需补充或复查的项目进入最终 C。

优先级固定为：

```text
A 癌症风险筛查 > B 其他异常复查 > C 周期性筛查管理
```

## 3. 独立推荐筛查检查点

新增 **CP5 推荐筛查分析与缺口确认**。CP5 位于健康总结结构化、快照式癌症风险和 VoI 完成之后，最终报告 section artifact 生成之前，不与 CP2 合并。

```text
CP2 风险因素问诊
  → CP3 证据填充与审计
  → CP4 健康总结结构化
  → snapshot + VoI
  → CP5A LLM 生成 A/B，并分析周期性筛查缺口
  → CP5A LLM 去重，生成独立缺口问卷
  → 用户逐项回答
  → CP5B LLM 根据回答更新 C 和最终 A/B/C 推荐
  → PUA 校验
  → 生成五个报告 artifact、套餐和报告
```

新增 stop point：

```text
--stop-after screening-gap
```

运行到该 stop point 后，pipeline 必须已经产出健康总结、snapshot、VoI、人口学和可用档案上下文。agent 随后执行 CP5A，而不是由脚本直接生成缺口候选。

## 4. CP5A：LLM 分析与生成独立问题

### 4.1 必读输入

LLM 必须读取：

- `artifacts/demographics.json`
- `artifacts/snapshot_risk.json`
- `artifacts/health_summary_structured_summary.json`
- 当前运行的 `content.md` 与 `refined.md`
- 当前结构化检查产物
- 可用的历史 `screening_test_timeline.json`
- `references/缺口筛查与交互确认.md`
- `references/database/screening_general/md/` 中与年龄、性别匹配的指南
- `references/database/screening_personalized/md/` 中与 A/B 推荐相关的指南

LLM 必须先检查资料时间线，再判断某项检查是无记录、已超期还是日期不可确认。不能把“报告未突出显示”直接等同于“从未做过”。

### 4.2 反向全文核验

判定缺口前，LLM 必须全文核查 `content.md`、`refined.md` 和历史筛查时间线中的项目名称、常用别名及结果描述。

例如，判断结肠镜缺口前必须检索肠镜、胃肠镜、结肠镜、息肉、病理、切除和 Boston 评分等表达。若已存在相关检查及阳性发现，不得归为“从未做过”。

### 4.3 A/B/C 草案与 LLM 去重

LLM 写出 `artifacts/screening_recommendations_draft.json`：

```json
{
  "schema_version": "screening-recommendations-draft-v1",
  "cancer_risk": [
    {
      "dedup_key": "lung_ldct",
      "item_name": "低剂量螺旋 CT",
      "source_name": "肺癌风险",
      "interval": "每年1次",
      "rationale": "结合当前肺癌风险，建议按指南进行低剂量螺旋 CT 筛查。",
      "guideline_source": "肺癌筛查指南.md",
      "evidence_text": "高危人群：**每年1次** LDCT"
    }
  ],
  "other_abnormalities": [],
  "periodic_candidates": [
    {
      "dedup_key": "colorectal_colonoscopy",
      "item_name": "结肠镜",
      "gap_status": "overdue",
      "last_known_exam_date": "2014-06-01",
      "guideline_interval": "每5-10年1次",
      "rationale": "最近记录已超过指南周期",
      "guideline_source": "结直肠癌筛查指南.md",
      "evidence_text": "每5-10年1次结肠镜",
      "timeline_evidence": "2014-06-01 结肠镜检查"
    }
  ],
  "dedup_audit": [
    {
      "dedup_key": "lung_ldct",
      "removed_from": "periodic_candidates",
      "kept_in": "cancer_risk",
      "reason": "该检查已由中等及以上肺癌风险推荐"
    }
  ]
}
```

要求：

- `dedup_key` 由 LLM 使用稳定、可审计的英文标识填写。
- A、B、C 草案中不得存在相同 `dedup_key`。
- A 与 B 重复时保留 A。
- C 与 A/B 重复时从 C 删除，不生成该项目的缺口问题。
- 每次删除都写入 `dedup_audit`，不得静默丢弃。
- `evidence_text` 必须是指定知识库文件的字面子串。
- `timeline_evidence` 必须来自当前或历史档案原文；无任何记录时允许为空，并把 `gap_status` 设为 `never_recorded`。

`gap_status` 仅允许：

- `never_recorded`
- `overdue`
- `unverifiable_date`

### 4.4 独立缺口问卷

LLM 根据去重后的 `periodic_candidates` 写出 `artifacts/screening_gap_questionnaire.json`。

每个候选两步询问：

1. 是否在指南周期内做过该检查；
2. 仅当回答做过时，追问结果正常、异常或不清楚。

```json
{
  "schema_version": "screening-gap-questionnaire-v1",
  "questions": [
    {
      "question_id": "gap_colorectal_colonoscopy_done",
      "dedup_key": "colorectal_colonoscopy",
      "prompt": "根据您的年龄和现有档案，建议每5-10年进行一次结肠镜。您在最近10年内做过吗？",
      "options": [
        {"label": "做过", "value": "done"},
        {"label": "未做过", "value": "not_done"},
        {"label": "不清楚", "value": "unknown"}
      ]
    },
    {
      "question_id": "gap_colorectal_colonoscopy_result",
      "dedup_key": "colorectal_colonoscopy",
      "conditional_on": {
        "question_id": "gap_colorectal_colonoscopy_done",
        "value": "done"
      },
      "prompt": "该项检查的结果如何？",
      "options": [
        {"label": "正常", "value": "normal"},
        {"label": "异常", "value": "abnormal"},
        {"label": "不清楚", "value": "unknown"}
      ]
    }
  ]
}
```

缺口问题必须逐项新启动，不得混入 CP2 的问卷。用户答案独立保存为：

```text
<out>/screening_gap_answers.json
```

内容格式：

```json
{
  "answers": {
    "gap_colorectal_colonoscopy_done": "not_done"
  }
}
```

## 5. CP5B：LLM 根据回答更新最终推荐

LLM 读取草案、独立问卷和用户回答，写出 `artifacts/screening_recommendations_final.json`：

```json
{
  "schema_version": "screening-recommendations-final-v1",
  "cancer_risk": [],
  "other_abnormalities": [],
  "periodic_management": [],
  "excluded_done_normal": [],
  "dedup_audit": []
}
```

回答处理规则：

| 是否做过 | 结果 | LLM 处理 |
|---|---|---|
| `not_done` | — | 纳入 `periodic_management` |
| `unknown` | — | 纳入 `periodic_management` |
| `done` | `abnormal` | 纳入 `periodic_management`，改写为异常后复查建议 |
| `done` | `unknown` | 纳入 `periodic_management` |
| `done` | `normal` | 从推荐中排除，写入 `excluded_done_normal` |

CP5B 必须再次检查 A/B/C 的 `dedup_key`，确保最终推荐没有重复。C 最终仅进入 `timeline_tiers.json.maintain`；A/B 根据风险与严重程度进入 priority 或 important。

## 6. PUA 门控范围

新增校验脚本只验证，不生成医学判断。

### 6.1 草案校验

校验 `screening_recommendations_draft.json`：

- 三类字段和 schema 完整；
- 癌症部分只引用 snapshot 中中等及以上癌症；
- 每条建议包含知识库来源；
- `evidence_text` 是来源文件的字面子串；
- 周期候选包含 gap 状态和档案核验说明；
- A/B/C 之间没有重复 `dedup_key`；
- `dedup_audit` 中被删除项目与保留来源一致。

校验器发现重复时应拒绝结果并要求 LLM 重做去重，不能自行删除。

### 6.2 问卷与回答校验

- 每个周期候选对应一组问题；
- 已从 C 去重删除的项目不得出现在问卷中；
- CP2 `answers.json` 不得包含 CP5 问题；
- CP5 回答不得写入 CP2 文件；
- 所有当前可见问题必须由用户真实回答；
- 条件未触发的问题允许不回答。

### 6.3 最终推荐校验

- `done + normal` 项目不得出现在最终 C；
- `not_done`、`unknown`、`done + abnormal` 和 `done + unknown` 必须有最终处置记录；
- A/B/C 最终仍不得存在重复 `dedup_key`；
- C 与问卷候选、用户回答一一对应；
- `timeline_tiers.maintain` 不得绕过 `screening_recommendations_final.json` 增加项目。

校验器只报告错误并阻断 pipeline，不修改 LLM 输出。

## 7. Pipeline 恢复与退出

新增独立参数：

```text
--screening-gap-answers <file>
```

新增退出码：

| Code | 条件 | 动作 |
|---|---|---|
| 11 | CP5 草案或问卷缺失/校验失败 | 完成或修正 CP5A 后重跑 |
| 12 | 有缺口问题但独立回答缺失/不完整 | 逐项询问并提供 `--screening-gap-answers` |
| 13 | CP5 最终推荐缺失或与回答、去重规则不一致 | 完成或修正 CP5B 后重跑 |

无周期性候选时，LLM 仍需写出空问卷和空 `periodic_management`，让门控确认 CP5 已实际执行，不能因“看起来没有缺口”而跳过阶段。

## 8. 报告和现有 artifact

现有报告模板继续使用：

- `timeline_tiers.json`
- `x_addons.json`
- `package_tiers.json`
- `liquid_biopsy_perf.json`
- `long_term_intervention.json`

CP5 最终结果作为这些 artifact 的上游约束：

- A/B 进入 timeline 的 priority/important 和 X 加项。
- C 只进入 timeline 的 maintain。
- 套餐使用去重后的 A/B/C 检查集合。
- 报告不重复展示相同 `dedup_key` 的检查。

脚本不根据 CP5 结果自动生成筛查建议或套餐内容，只校验 LLM 产出的 artifact 是否与 CP5 最终结果一致。

## 9. 文档和知识库调整

实现时修改：

- `SKILL.md`：新增 CP5 独立流程、输入输出、PUA 和退出码。
- `references/runtime_workflow.md`：新增 `screening-gap` stop point 和恢复命令。
- `references/缺口筛查与交互确认.md`：删除 CP2 扩展表述，改为 CP5 LLM 分析与独立交互。
- `references/database/index.json`：明确常规筛查 MD 由 CP5 按年龄、性别和时间线按需读取。

不新增用于自动计算推荐的常规筛查 JSON 规则库。

## 10. 测试与评估

测试重点是 PUA 是否阻止 LLM 跳步或输出不一致，而不是测试脚本能否代替 LLM 推荐：

1. pipeline 暴露独立 `screening-gap` stop point。
2. CP2 与 CP5 问卷、答案完全隔离。
3. 缺少 CP5 草案时 pipeline 阻断。
4. 草案缺少知识库证据或字面证据不匹配时阻断。
5. A/B/C 存在重复 `dedup_key` 时阻断，校验器不自动去重。
6. 去重删除项仍出现在问卷时阻断。
7. 周期候选没有时间线核验说明时阻断。
8. 问卷缺少两步分支时阻断。
9. 回答缺失或条件触发题未回答时阻断。
10. `done + normal` 仍进入 C 时阻断。
11. 应纳入项目没有最终处置记录时阻断。
12. 最终 A/B/C 重复时阻断。
13. `timeline_tiers.maintain` 与 CP5 最终 C 不一致时阻断。
14. 无候选时仍需存在空问卷和空最终 C。
15. 现有全量测试无回归。

技能效果评估使用接近真实档案的测试提示，至少覆盖：

- 从未记录的周期检查；
- 已做但超过指南周期；
- 已做且仍在周期内；
- 日期不完整；
- 已由癌症风险推荐、应从 C 剔除；
- 已由其他异常指标推荐、应从 C 剔除；
- 胃肠镜、息肉、病理等非标准名称记录。

实现完成后联合执行 `skill-creator` 与 `darwin-skill` 评估。功能测试和 PUA 门控测试是发布前置条件，评估不能替代测试。

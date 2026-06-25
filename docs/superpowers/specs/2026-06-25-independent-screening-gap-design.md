# 独立周期性筛查缺口确认设计

**版本目标：** 修复 v2.0.1 中 `references/缺口筛查与交互确认.md` 仅被文档引用、未被 pipeline 实际执行的问题。

**确认日期：** 2026-06-25

## 1. 目标与验收口径

推荐筛查必须按以下三个来源独立生成：

1. **A：癌症风险筛查**
   - 输入为 `snapshot_risk.json` 的快照式后验结果。
   - 仅纳入中等风险及以上癌症；超过三个癌种时沿用现有 TOP3 规则。
2. **B：其他疾病或异常指标复查**
   - 输入为健康总结结构化结果。
   - 排除已经由 A 表达的癌症风险，避免同一风险重复展示。
3. **C：周期性筛查管理**
   - 根据年龄、性别和适用的指南条件生成应筛项目。
   - 同时识别两类候选：档案中从无记录；有记录但已超过指南周期。
   - 在推荐筛查阶段启动独立问卷，不与 CP2 风险因素问诊合并。
   - 先剔除已经由 A 或 B 推荐的检查项目，再向用户确认。
   - 只有用户确认未做、结果异常或不清楚的项目进入 C；指南周期内已做且结果正常的项目不推荐。

验收时必须证明：缺口规则由 pipeline 代码实际执行，而不是依赖 agent 阅读参考文档后临时发挥。

## 2. 方案选择

采用“结构化规则 + 确定性候选检测 + 独立交互检查点 + LLM 生成 A/B 文案”的混合方案。

未采用的方案：

- 仅增强 `SKILL.md`：无法保证缺口检测、动态提问和去重实际发生。
- 把三部分都交给报告阶段 LLM：无法稳定判断超期、规范项目别名或验证回答完整性。

确定性逻辑负责年龄/性别适用性、日期周期计算、项目 ID 去重、问卷分支和答案过滤；LLM 只负责从知识库生成 A/B 的医学建议和患者面向文案。

## 3. Pipeline 时序

新增推荐筛查检查点 **CP5**，位于健康总结结构化、快照式癌症风险和 VoI 完成之后，最终五个报告 artifact 生成之前。

```text
CP2 风险因素问诊
  → CP3 证据填充与审计
  → CP4 健康总结结构化
  → snapshot + VoI
  → CP5A 生成 A/B 基础筛查推荐
  → 脚本计算周期性筛查候选并按 A/B 去重
  → CP5B 独立缺口问卷
  → 脚本应用缺口回答并生成 C
  → 最终五个 section artifact、套餐和报告
```

CP5 分两次可恢复执行：

1. `--stop-after screening-base`
   - pipeline 已完成 snapshot、VoI 和健康总结。
   - agent 读取知识库，生成并校验 A/B 基础推荐。
2. `--stop-after screening-gap`
   - pipeline 读取 A/B，执行缺口检测和去重。
   - 若存在候选，写出独立问卷并停下等待用户回答。
   - 若无候选，写出空的确认结果并直接允许进入报告 artifact 阶段。

用户答案通过独立参数 `--screening-gap-answers <file>` 传入，不写入 CP2 的 `answers.json`。

## 4. 数据模型

### 4.1 标准检查项目目录

新增 `references/database/screening_general/json/screening_item_catalog.json`：

```json
{
  "schema_version": "screening-item-catalog-v1",
  "items": [
    {
      "screening_item_id": "colorectal_colonoscopy",
      "display_name": "结肠镜",
      "aliases": ["肠镜", "结肠镜", "胃肠镜"],
      "result_aliases": ["息肉", "病理", "切除", "Boston评分"]
    }
  ]
}
```

项目 ID 是 A/B/C 去重的唯一主键。显示名称或自然语言相同不作为最终去重依据。

### 4.2 年龄性别周期规则

新增 `references/database/screening_general/json/general_screening_rules.json`，由现有筛查指南机械整理：

```json
{
  "schema_version": "general-screening-rules-v1",
  "rules": [
    {
      "rule_id": "crc-colonoscopy-standard",
      "screening_item_id": "colorectal_colonoscopy",
      "sex": "all",
      "min_age": 45,
      "max_age": null,
      "interval_min_months": 60,
      "interval_max_months": 120,
      "eligibility": [],
      "guideline_source": "03-居民常见恶性肿瘤筛查推荐2025.md",
      "evidence_text": "45 岁开始 每年 1 次大便隐血检测（FOBT）, 每 10 年 1 次肠镜检查。"
    }
  ]
}
```

规则必须保留来源和字面证据。区间型指南同时保存最短和最长周期；“已超期”以最长周期为硬阈值，避免把仍处于允许区间内的检查误判为缺口。仅当人口学和已知风险条件满足时生成候选；不得为获取资格条件而把 CP2 问题重复问一遍。

### 4.3 A/B 基础推荐

CP5A 生成 `artifacts/screening_recommendations_base.json`：

```json
{
  "schema_version": "screening-recommendations-base-v1",
  "cancer_risk": [
    {
      "screening_item_id": "lung_ldct",
      "item_name": "低剂量螺旋 CT",
      "source_id": "lung_cancer",
      "interval": "1年内",
      "rationale": "患者面向说明"
    }
  ],
  "other_abnormalities": [
    {
      "screening_item_id": "carotid_ultrasound",
      "item_name": "颈动脉超声",
      "source_id": "dyslipidemia",
      "interval": "3个月内",
      "rationale": "患者面向说明"
    }
  ]
}
```

`screening_item_id` 必须来自项目目录。新增校验器拒绝未知 ID、重复 ID、缺少来源或空建议。A 与 B 重复时保留 A，B 中删除重复项目。

### 4.4 缺口候选

脚本生成 `artifacts/screening_gap_candidates.json`：

```json
{
  "schema_version": "screening-gap-candidates-v1",
  "candidates": [
    {
      "screening_item_id": "colorectal_colonoscopy",
      "item_name": "结肠镜",
      "status": "never_recorded",
      "last_exam_date": null,
      "interval_min_months": 60,
      "interval_max_months": 120,
      "due_date": null,
      "guideline_source": "03-居民常见恶性肿瘤筛查推荐2025.md",
      "evidence_text": "45 岁开始 每年 1 次大便隐血检测（FOBT）, 每 10 年 1 次肠镜检查。"
    }
  ],
  "excluded_by_prior_recommendation": []
}
```

`status` 仅允许：

- `never_recorded`：当前报告和历史筛查时间线均无记录。
- `overdue`：存在最近检查日期，但运行日期已晚于 `last_exam_date + interval_max_months`。
- `unverifiable_date`：存在检查记录，但没有可用于周期计算的日期。

在候选写出前，脚本按项目 ID 删除 A/B 已推荐项目，并把删除原因记录到 `excluded_by_prior_recommendation` 供审计。

### 4.5 独立问卷与回答

`artifacts/screening_gap_questionnaire.json` 对每个候选生成两步问题：

```json
{
  "schema_version": "screening-gap-questionnaire-v1",
  "questions": [
    {
      "question_id": "gap_colorectal_colonoscopy_done",
      "screening_item_id": "colorectal_colonoscopy",
      "prompt": "您是否在最近10年内做过结肠镜？",
      "options": [
        {"label": "做过", "value": "done"},
        {"label": "未做过", "value": "not_done"},
        {"label": "不清楚", "value": "unknown"}
      ]
    },
    {
      "question_id": "gap_colorectal_colonoscopy_result",
      "screening_item_id": "colorectal_colonoscopy",
      "conditional_on": {
        "question_id": "gap_colorectal_colonoscopy_done",
        "value": "done"
      },
      "prompt": "该项检查结果如何？",
      "options": [
        {"label": "正常", "value": "normal"},
        {"label": "异常", "value": "abnormal"},
        {"label": "不清楚", "value": "unknown"}
      ]
    }
  ]
}
```

用户答案保存为独立的 `screening_gap_answers.json`：

```json
{
  "answers": {
    "gap_colorectal_colonoscopy_done": "not_done"
  }
}
```

校验器要求所有当前可见问题均已回答，不接受从报告推断或预填。

### 4.6 确认结果与 C

脚本生成 `artifacts/screening_gap_confirmed.json`：

```json
{
  "schema_version": "screening-gap-confirmed-v1",
  "recommended": [],
  "excluded_done_normal": [],
  "audit": []
}
```

过滤规则：

| 是否做过 | 结果 | 输出 |
|---|---|---|
| `not_done` | — | 进入 C |
| `unknown` | — | 进入 C |
| `done` | `abnormal` | 进入 C，并标记异常复查 |
| `done` | `unknown` | 进入 C |
| `done` | `normal` | 排除，写入审计 |

最终 `timeline_tiers.json.maintain` 只能从 `screening_gap_confirmed.json.recommended` 生成，不允许 agent 绕过确认结果自行增加周期缺口项目。

## 5. 档案和时间线检测

缺口检测按以下优先级寻找最近检查记录：

1. 当前运行的所有 `content.md` 与 `refined.md`。
2. 当前结构化检查产物，包括肿瘤标志物和已识别检查事件。
3. `output/<person_id>/screening_test_timeline.json` 的历史记录。

匹配使用项目目录中的别名，不依赖单一中文名称。记录必须包含可解析日期才可用于“是否超期”计算；只有项目记录但日期缺失时，生成 `unverifiable_date` 候选，由用户确认最近指南周期内是否做过。

胃肠镜、息肉、病理、切除和 Boston 评分等证据必须映射为已做结肠镜，防止已做且有阳性发现被误判为从未检查。

## 6. 去重规则

统一优先级：

```text
A 癌症风险筛查 > B 其他异常复查 > C 周期性筛查管理
```

同一 `screening_item_id` 只允许出现在最高优先级来源中：

- A 与 B 重复：保留 A。
- A/B 与 C 重复：候选问卷生成前剔除 C，不向用户询问该项缺口。
- 同一来源内部重复：合并 `source_id` 和 rationale，不生成两条检查。

去重只影响检查项目展示，不删除原始癌症风险、异常指标或审计证据。

## 7. 错误处理和恢复

| 条件 | 行为 |
|---|---|
| A/B artifact 缺失或校验失败 | 停在 `screening-base`，不生成缺口问卷 |
| 人口学缺失 | 沿用现有 exit 5，禁止猜测年龄或性别 |
| 指南规则缺来源证据 | 数据库校验失败，禁止进入生产 pipeline |
| 无周期性候选 | 写空问卷和空确认结果，继续生成报告 |
| 有候选但未提供独立回答 | 新退出码 11，提示 `--screening-gap-answers` |
| 回答缺少条件触发题 | 校验失败，保持在 CP5B |
| 历史记录日期不可解析 | 不直接判定已按期完成，转为用户确认并记录审计原因 |
| 未知项目 ID | 拒绝 A/B artifact 或规则文件，不做名称模糊兜底 |

## 8. 报告与兼容性

现有报告模板继续使用 `timeline_tiers` 和 `x_addons`，避免无关模板重构：

- A/B 继续进入优先执行或重要检查，并作为 X 加项来源。
- C 只进入 `timeline_tiers.maintain`。
- 套餐生成读取去重后的 A/B/C 合集。
- `report.json` 增加 CP5 审计字段，但旧模板不强制渲染该字段。

CP2 的问卷、答案和风险因素时间线保持不变。旧的 `--answers` 参数只负责 CP2；新的 `--screening-gap-answers` 只负责 CP5B。

## 9. 测试策略

测试必须先失败再实现，覆盖：

1. pipeline 暴露 `screening-base` 和 `screening-gap` stop point。
2. 年龄或性别不适用时不生成候选。
3. 从无记录生成 `never_recorded`。
4. 有记录且未超期不生成候选。
5. 有记录且超期生成 `overdue`。
6. 日期缺失记录进入用户确认而非直接排除。
7. 胃肠镜及病理别名识别为结肠镜记录。
8. A/B 项目在问卷前从 C 剔除。
9. 两步问卷条件分支正确。
10. `done + normal` 排除；其余组合进入 C。
11. 缺回答时退出码为 11，不能继续生成报告。
12. CP2 `answers.json` 与 CP5B 回答完全隔离。
13. 最终 `maintain` 不包含 A/B 重复项。
14. 现有全量测试无回归。

至少增加一个接近真实档案的端到端 fixture，包含“已做但超期”“从未记录”“已被癌症风险推荐”“已被异常指标推荐”四类项目。

## 10. 文档同步

实现时同步修改：

- `SKILL.md`：pipeline 阶段数、Minimal Workflow、独立 CP5、退出码和 artifact。
- `references/runtime_workflow.md`：新增 stop point 和恢复命令。
- `references/缺口筛查与交互确认.md`：删除“CP2 扩展”表述，改为 CP5 独立检查点。
- 数据库索引：登记新 JSON 目录和加载方。

实现完成后，按仓库宪法运行完整测试，并联合执行 `skill-creator` 与 `darwin-skill` 评估；评估不得代替功能测试。

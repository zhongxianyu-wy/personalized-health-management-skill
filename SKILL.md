---
name: 个性化健康管理
description: |
  Use when user sends or references a health checkup report (体检报告, 体检, 体检结果,
  medical checkup, 癌症风险, cancer risk, 健康报告, 体检报告解读, 肿瘤标志物, 筛查推荐,
  checkup analysis) and wants it analyzed into a single integrated report. Make sure to
  use this skill whenever the user mentions 体检报告/体检结果/癌症风险/肿瘤标志物/筛查推荐/健康报告
  — even if they don't explicitly say "分析", e.g. "帮我看看这份体检报告""这个指标偏高怎么回事(附报告)""肺结节要紧吗".
  Pipeline: MinerU OCR → Socratic risk-factor interview → abnormal interpretation
  (健康总结 API) → cancer-risk scoring (Bayesian dual-path) → personalized screening →
  packaged HTML report. Does NOT handle: diagnosis, treatment, medication, urgent triage,
  single-symptom Q&A, or general health/diet/exercise/lifestyle advice WITHOUT a checkup report.
safety: |
  报告仅用于健康管理与筛查决策辅助，不构成医学诊断、治疗方案或用药建议。
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - AskUserQuestion  # CP2 苏格拉底问诊；其他 runtime（openclaw/codex 等）用等效交互机制
version: "0.1.0"
---

# 个性化健康管理 Skill

本 skill 是薄操作手册，按当前阶段按需加载 reference。单一入口为编排器：

```bash
uv run --python 3.11 --with PyYAML --with jsonschema --with jinja2 --with requests \
  python scripts/run_formal_analysis.py \
  --input <file-or-folder> --analysis-output <out> --person-id <stable_id>
```

> **命令约定**：下文 `uv run ... python scripts/X.py` 简记为 `scripts/X.py`；步骤间 `...` 表示继承首步的 `--input/--analysis-output/--person-id`，从 CP2 起加 `--answers <file>`，不得丢旗。归档默认落 `output/<person_id>/`（与 SKILL.md 同级）。

## 对话语气（功能性，非人设）

语气平和、用「您」、医学术语先白话再术语；处理用户焦虑时先安抚再给依据。**语气只影响对话层（CP1 精炼措辞、CP2 苏格拉底问诊、报告叙述），不改变 PUA 协议的严格性**：数值、ID、概率、筛查周期一律来自脚本与知识库，不得编造。

## Use / Refuse

**Use**：上传体检报告（PDF/图片/OCR markdown）→ 生成单一综合报告（`report.html`）；同人重跑刷新档案；证据/答案更新后重跑。

**Refuse/redirect**：疾病诊断、治疗方案、用药剂量、紧急分诊、单症状临床问答。

## Non-Negotiables

- LLM/agent 只能从原文或用户答案填**事实字段**，不得编造 factor ID、cancer ID、概率、OR/RR/HR、LR、灵敏度、特异度、筛查周期、建议。
- 只用编排器派发的**闭合词表**：`risk_factor_master.json`、`structured_risk_factors_timeline`、`tumor_markers.candidate.json`、`imaging_findings.candidate.json`、`cancerrisk/json/` 内 JSON。
- `evidence_text` 必须是命名源 md 的**字面子串**。
- 真实运行不得跳过必需用户答案，缺则停并问用户。
- 4 个 agent 检查点的文件读写在本 agent loop 内完成，**不得委派子智能体**做确定性填充。

## 数据库与应用模式（核心边界）

四级库在 `references/database/`，三种应用模式严格分离（= PUA 边界）：

| 模块 | 路径 | 应用模式 | 读取者 | 何时加载 |
|---|---|---|---|---|
| 风险计算（本体/断言/先验/PPV/检测） | `cancerrisk/json/`（15 JSON） | **脚本确定性读** | snapshot_risk / voi_calculator / build_assertion_fill | 该阶段执行时 |
| 风险背景知识 | `cancerrisk/md/`（5 MD） | LLM 按需读 | CP1/CP3 参考 | 需要时 |
| 常规筛查（年龄/性别驱动） | `screening_general/md/`（3 MD） | LLM 按需读 | 缺口筛查 / 常规推荐 | 筛查阶段 |
| 专家筛查（癌种/慢病/判读/复查规则/液体活检） | `screening_personalized/md/`（21 MD） | LLM 按需读 | 个性化筛查推荐 / 复查分级 | 筛查阶段，单次≤3 篇 |
| 价格 | `pricing/md/`（1 MD） | LLM 按需读 | 套餐预算 | 套餐阶段 |

**原则**：贝叶斯概率相关（OR/PPV/LR/先验/后验/VoI）→ JSON，脚本算，LLM 不碰数值；异常判读、筛查推荐、套餐、复查规则、报告文案 → MD，LLM 参考知识库做判断，**不查 JSON 表**。健康总结走远程 API（display-only，不参与概率）。

## Pipeline Stages

| # | 阶段 | 脚本 | 数据库文件 | 应用模式 | CP? |
|---|---|---|---|---|---|
| 1 | 环境检测 | `env_check.py` | 校验 `cancerrisk/json/` + templates | 脚本 | — |
| 2 | MinerU OCR | `mineru_client.py` | —（远程 MinerU） | 脚本+远程 | — |
| 3 | **精炼建档** | _(agent 写 `refined.md`)_ | `cancerrisk/md/` 参考 | LLM | **CP1** |
| 4 | 人口学 | `demographics.py` | — | 脚本 | — |
| 5 | **苏格拉底问诊+缺口确认** | `interactive_completion.py` | `cancerrisk/json/interaction_question_templates.json` + `screening_general/md/居民常见恶性肿瘤筛查和预防推荐（2025版）.md` | 脚本生成问卷→LLM 问诊（风险因子 + 缺口项目逐项确认）。⚠ 筛查行为题(PSA/肠镜/乳腺/宫颈)**走报告提取不交互**(v7 设计，被 SCREENING_FACTOR_IDS 过滤)| **CP2** |
| 6 | 健康总结 API | `render_health_summary.py` | —（金百森远程） | 脚本+远程 | — |
| 7 | **健康总结结构化** | `finalize_structured_summary.py` | — | LLM+脚本 | **CP4** |
| 8 | **证据填充+审计** | `build_assertion_fill_template.py`+`master_scan.py` | `cancerrisk/json/`(cancers/risk_factors/assertions/observables/synonyms/imaging) | LLM 填闭合词表→脚本 gate | **CP3/3.1** |
| 9 | 贝叶斯风险 | `snapshot_risk.py` | `cancerrisk/json/`(priors/derived/screening_recommendations) | 脚本确定性 | — |
| 10 | VoI 排序 | `voi_calculator.py` | `cancerrisk/json/`(voi_parameters/screening_methods/detection_derived) | 脚本确定性 | — |
| 11 | 筛查推荐 | _(LLM 参考 MD)_ | `screening_general/md/` + `screening_personalized/md/` | **LLM 读 MD** | — |
| 12 | 综合报告+归档 | LLM 产 5 section artifact → `build_report_json.py`+`render_report.py`+`archive_manager.py` | `report.html`（temp 模版，唯一权威）+`manifest.json` → `output/<id>/` | LLM+脚本渲染 | — |

> 行 3/5/7/8 需 agent 行动；其余在 `run_formal_analysis.py` 内自动。需求流程里"癌症证据内置初次提取+第二次审核"= CP3 + CP3.1；"苏格拉底式高危因素收集"= CP2。
> **执行顺序注**：demographics(行4)读 `content.md`/CLI 性别年龄，与 CP1(行3)独立；代码执行顺序 demographics→CP1 refine gate，但 CP1 产物 `refined.md` 是 CP3 证据填充的输入（逻辑上 CP1 先于 CP3，二者不冲突）。

## Minimal Workflow

1. 环境检测 + OCR，停：
   ```bash
   uv run --python 3.11 --with PyYAML --with jsonschema --with jinja2 --with requests \
     python scripts/run_formal_analysis.py --input <input> --analysis-output <out> --stop-after mineru
   ```

2. 🔴 **CP1 精炼建档**（agent）。对每个 `artifacts/mineru/<data_id>/content.md` 先做质量门：少于 20 行或不含 {检查,化验,报告,结果,项目} → 停并报错；否则写同目录 `refined.md`，保留人口学/异常行/肿瘤标志物（含正常值）/影像结论/阳性体征/**胃肠镜等内镜检查记录（含息肉/病理/切除/Boston 评分——若有必抽全段；漏抽会致"已做"误判为"未做过"，如肠镜+息肉切除被误判为缺口）**。不得概括掉值/单位/参考范围/日期（字面校验需要）。详见 `references/runtime_workflow.md`。

3. 跑到问诊：
   ```bash
   ... scripts/run_formal_analysis.py ... --stop-after interactive
   ```

4. 🔴 **CP2 苏格拉底问诊**（agent）。`--stop-after interactive` 后 exit 0 并打印 `[stop-after=interactive] questionnaire written`。
   - **4a** 读 `<out>/artifacts/interactive_questionnaire.json` 的 `questions`。
   - **4b 苏格拉底三铁律**：①每次只问一题（Claude Code 用 `AskUserQuestion`；其他 runtime 用等效交互机制）；②触发即解决（trigger 题答完→进下一无关题前走完跟进链）；③不完整→按完整度标准重问。
     - `conditional_on` 检查：题有 `conditional_on`，查已答值≠`conditional_on.value` 则跳过。
     - label↔value 映射：用户选 label（「阳性」）→ 记 option 的 `value`（`positive`），绝不记原 label。
     - 触发：`q_family_history_cancer=yes`→立即问 `q_family_history_detail`（须含具体癌种+亲属人数，缺一不可）；`q_jizaoan_result=positive`→问 top1/top2 癌种。
   - **4c** 写 `<out>/answers.json`：`{"answers": {<qid>: <value>}}`，仅含本会话已问的题，`text_fill` 逐字原话，未触发 key 省略。
   - **4d** 校验：`... scripts/validate_answers.py --questionnaire <out>/artifacts/interactive_questionnaire.json --answers <out>/answers.json`（exit 0 继续，1 修答案，2 文件缺失）。

5. 跑到证据模板：
   ```bash
   ... scripts/run_formal_analysis.py ... --stop-after master-template
   ```

6. 🔴 **CP3 证据填充**（agent，提取者角色）。用合并模板（`merge_filled_template.py` 产出）填 `structured_risk_factors_timeline.candidate.json` + `tumor_markers.candidate.json` + `imaging_findings.candidate.json`，**只用闭合词表**（factor_key 来自 `risk_factor_master.json`；test_id 来自 `detection_performance_derived.json` 排除 `jizaoan_multi_cancer_screening`；finding_id 来自 `imaging_findings.json`）。校验：
   ```bash
   ... scripts/validate_timeline_candidate.py --candidate <out>/artifacts/structured_risk_factors_timeline.candidate.json
   ... scripts/validate_tumor_markers.py --candidate <out>/artifacts/tumor_markers.candidate.json
   ... scripts/validate_imaging_findings.py --candidate <out>/artifacts/imaging_findings.candidate.json
   ```
   再 `... --stop-after cp3-verify`。

7. 🔴 **CP3.1 审计**（agent，独立审计员角色，认知重置）。独立重读每个 `refined.md`，找遗漏：未入卷的发现查 `risk_factor_master.json` 匹配 `factor_key`，匹配且文档支撑→加 `exists=true`；明确否定→`exists=false`；无匹配→不强塞（自动进「证据库外异常提示」）。写 `<out>/artifacts/cp3_audit_result.json`（缺则 exit 9）：
   ```json
   {"no_omissions": true}
   ```
   或 `{"no_omissions": false, "added_factor_keys": [...], "unmatched_findings": [{"finding_text":"<报告字面引文>","reason":"<一句话>","related_cancer":"<可选>"}]}`。

8. 跑到健康总结 API：
   ```bash
   ... scripts/run_formal_analysis.py ... --stop-after health-summary-api
   ```

9. 🔴 **CP4 健康总结结构化**（agent）。把 API markdown 转 `health_summary_structured_summary.json`，**必须用 `finalize_structured_summary.py`**（直写 JSON 会被 runtime 截断）：
   ```bash
   ... scripts/finalize_structured_summary.py --analysis-output <out> --fills <fills.json>
   ```
   **fills.json 的 @ 引用格式**：大 HTML 片段用 `@/absolute/path` 或 `@relative/path`（@ 后**直接跟路径**，**不带 `file:`**，否则 finalize 解析失败）。健康总结展示专用，不得用 snapshot 概率/ontology OR-LR/筛查逻辑。

10. 🔴 **报告前产 5 section artifact**（agent）。跑到 report-artifacts（snapshot/VoI/筛查/归档完成）：
    ```bash
    ... scripts/run_formal_analysis.py ... --person-id <id> --stop-after report-artifacts
    ```
    exit 0 → 按「报告 section artifact」配方读对应 MD/JSON 产 5 JSON 到 `<out>/artifacts/`：`timeline_tiers.json` / `x_addons.json` / `package_tiers.json` / `liquid_biopsy_perf.json` / `long_term_intervention.json`。**这些 artifact 无任何脚本产出，跳过则报告 5 个核心 section 渲染空**（specificity 留空由 build_report_json 兜底，勿 LLM 填）。

11. 跑最终报告+归档（不带 stop-after）：
    ```bash
    ... scripts/run_formal_analysis.py ... --person-id <id>
    ```
    exit 0 → `report.html` 就绪（temp 模版，用户交付物）；snapshot 已自动 dedup 合入 `output/<person_id>/`。

## 单一报告偶联规则（需求 7）

`report.html` 为唯一用户交付物，**每条结论必须偶联数据出处**（脚本产出 or LLM 参考知识库，不得编造）：

- **癌症展示**：仅纳入**中等风险及以上**后验（来自 `snapshot_risk.json`）；若超过 3 个，仅选**风险因素 top3**。展示后验概率/PPV；若有遗传突变基因（CP3 tumor_markers），提示遗传基因相关证据。
- **其他异常**：从健康总结 API 反馈的异常部分总结，**剔除已展现的癌症相关异常避免重复**。
- **复查项目三级（时间轴，LLM 按 `癌症风险分层与复查规则.md`「时间轴三档规则」+ `异常指标复查推荐.md` + snapshot 后验 + 健康总结严重度）**：
  - ①**优先执行 priority（1-2 周内）**：高风险紧急项 = 健康总结评估较严重 **或** 癌症后验 >1%
  - ②**重要检查 important（1 个月内）**：中等项 = 健康总结中等风险 **或** 癌症后验 0.5%-1%
  - ③**持续管理 maintain（3-6 个月内）**：筛查缺口（按指南应到时间限但未检测，参 `缺口筛查与交互确认.md`）
  - **均匀机制**：若 priority 与 important 极不平衡（某档过载、某档空），LLM 按双优先级重排——**优先级 1=高风险紧急程度，优先级 2=复查时间对疾病进展的影响程度**——适当均分，避免单档堆积。
- **缺口补充推荐（筛查第三部分）**：按 `居民常见恶性肿瘤筛查和预防推荐（2025版）.md` 时间表（**注意：报告检查日期至今须已满足参考年限，才判定为缺口**）+ 档案已检项目对照，LLM 分析「应做未做 / 超期」缺口；对缺口**两步苏格拉底交互**：①依次问「是否在筛查指南年限内做过 [检查]？」②若做过→追问「结果正常还是异常？」；最终**只保留未做过 或 近期检查异常**的指标（做过且正常→排除）。详见 `references/缺口筛查与交互确认.md`。
- **液体活检（吉早安）专项**：阳性按模版展示信号癌种；阴性按「可降低目前高风险癌症多少风险评级」展示（原版阴性降级逻辑，LR⁻ 路径）。
- **组合套餐（三档·风险驱动，参 `套餐三档与风险驱动.md`）**：**价格为具体数字（`pricing/md/08` 各项目「中位」值之和，非区间）**——每项目价格唯一取中位（如 LDCT 500、无痛肠镜 1000、甲状腺彩超 120、颈动脉超声 200、吉早安 2480、遗传咨询 500），套餐价 = 所含项目中位之和。
  - **档1·风险靶向聚合档**：包含**所有高风险项目**；不足 5 项用**中等风险项目**补齐至 5 项；仍不足用第三档（缺口）项目按优先级补齐。price = 所含项目中位之和（具体数字）。
  - **档2·全面覆盖档（推荐）**：**全面型**，覆盖全部三档风险（高+中+缺口）。price = 所含项目中位之和（具体数字）。
  - **档3·吉早安替换/弥补档（两个具体价格 + 项目分两类）**：
    - **价格1（吉早安+未被替代项）**：将吉早安目标癌症（肺/结直肠/胃/肝/食管/胰腺/乳腺/卵巢）涉及的专项筛查**替换为吉早安**（一管血替代多侵入项），未被替代的项目保留；price1 = 吉早安中位 + 未被替代项中位之和（如 4320 = 2480 + 1840）。
    - **价格2（吉早安+所有推荐项）**：档2 全量基础上**额外加吉早安**（标准筛查+多癌早筛双覆盖）；price2 = 吉早安中位 + 所有推荐项中位之和（如 5900 = 2480 + 3420）。
    - 检测项目按「**被吉早安替代的 8 癌专项** / **其他未被替代项**」两类展示，price 字段填「price1/price2」（如 `4320/5900`）。
  - 价格区间复合自 `pricing/md/08`；吉早安性能引用 `detection_performance.json`（不编造）；附长期健康干预+专科建议。

**报告 section artifact（temp 模版，报告前 LLM 产出 → build_report_json 透传 → render 渲染）**：5 artifact 落 `<out>/artifacts/`，文件名/字段名与模版 Jinja 变量严格一致，每 section 严格偶联数据库（PUA，不编造）。**文案类 LLM 读 MD 产（不查 JSON 表、数值不编造）；specificity 留空由 build_report_json 脚本从 voi_ranking 吉早安行兜底 0.990→99.0%（数值脚本算，真实数据源）**：
- `timeline_tiers.json`（时间轴三级）schema `{priority/important/maintain:[{item_name,rationale}]}` ↔ `癌症风险分层与复查规则.md`「时间轴三档规则」+ snapshot 后验(>1%/0.5%-1%) + 健康总结严重度 + `异常指标复查推荐.md`（priority=高风险紧急, important=中等, maintain=缺口；均匀机制=双优先级排序均分）
- `x_addons.json` schema `[{risk_source,risk_level_tag(danger/warning/info),risk_level_label,method,interval,price_range,clinical_value}]` ↔ `异常指标复查推荐.md` + `pricing/md/08`（interval/price 须 MD 字面）
- `package_tiers.json`（恒 3 档）schema `[{name,price_range,includes[],note,recommended}]`（**每档必填 recommended(bool)，仅 1 档 true，漏字段 StrictUndefined 报错**；档3 吉早安替换/弥补两策略各一卡或合并）↔ `套餐三档与风险驱动.md`（档1风险靶向聚合≥5项 / 档2全面覆盖 / 档3吉早安替换+弥补）+ `pricing/md/08`（price 须 pricing 字面）
- `liquid_biopsy_perf.json` schema `{sensitivity,specificity,market_price_range,clinical_hint,negative_risk_reduction}`：**sensitivity/specificity 由 build_report_json 脚本从 voi_ranking 吉早安行兜底（统一白皮书口径 81.9%/99.0%，voi_calculator::_JIZAOAN_SENSITIVITY=0.819；勿 LLM 填）**；market_price_range/clinical_hint/negative_risk_reduction 由 LLM 读 `05-基于液体活检的多癌种联合筛查.md` + pricing 字面搬运（**MD 无则留「-」不编造**）
- `long_term_intervention.json` schema `{genetic_management[](仅 brca positive),lifestyle[]}` ↔ `07-肿瘤预防与健康管理.md`（药物/手术获益须 MD 字面）

## PUA Protocol（防跳过强制，本节具有约束力，违者致命失败）

> **为什么强约束**：本 skill 的输出会影响用户的筛查与就医决策。若智能体跳过检查点、或编造 OR/概率/筛查周期，可能误导诊疗（漏掉高危癌、给错复查间隔、误判遗传风险）。因此数值必须来自脚本与知识库、检查点必须走完——这不是形式主义，而是医疗安全的底线。理解了这个「为什么」，下面的强约束就不再是机械服从，而是守住不伤害用户的原则。

### TL;DR
- 🔴 **CP1**：为每个 `content.md` 写 `refined.md`，带下一 `--stop-after` 重跑。
- 🔴 **CP2**：逐题问用户、收答案，带 `--answers` 重跑。
- 🔴 **CP3/3.1**：填 candidate、校验、独立审计、写 `cp3_audit_result.json`，重跑。
- 🔴 **CP4**：用 `finalize_structured_summary.py` 转 API markdown，重跑出报告。（归档自动，无确认步骤。）

### 禁止行为（agent 不得）
- 以任何理由（含「省时间」「数据看起来完整」）跳过任何检查点。
- 用自身知识产出健康风险分析/癌症概率/筛查建议，而不跑 pipeline 脚本。
- 未跑必需脚本、未验证退出码就宣称阶段完成。
- 用从报告推断的值预填问诊答案——每题须用户亲答。
- 在问用户前预填 `answers.json`，或写本次会话未向用户呈现过的 `question_id`。
- `conditional_on` 触发时跳过 `text_fill` 跟进——触发匹配即强制立即问。
- 不查 `conditional_on` 就批量问所有题——每个触发题后须紧跟其门控的 `text_fill`。
- 只跑完部分阶段就用「全分析已完成」的语言总结。
- 跳过第 10 步（报告前产 5 section artifact）直接跑最终报告 → temp 模版 5 个核心 section（复查三级/X加项/套餐/液检性能/长期干预）渲染空，输出无效。

### 进入下一检查点前的自报三项
agent 须在回复中确认：①运行的脚本命令（带全 flag）；②收到的退出码；③产出的 artifact（路径+存在性检查）。任一缺失/失败→停并报告用户，不得继续。

### 退出码
| Code | 来源 | 含义 | 必需动作 |
|---|---|---|---|
| 0 | — | 正常 | 进下一步 |
| 1 | MinerU/健康总结 API | 请求失败 | 报确切错误；用户确认后重试一次；不得继续 |
| 2 | MinerU | 全部文件 OCR 失败 | 报错 halt；让用户核验输入 |
| 3 | CP1 | `refined.md` 缺失/结构检查失败 | 按配方写/修，重跑 |
| 5 | 人口学 | 性别/年龄缺失 | 带 `--person-sex/--person-age` 或加进 `--answers` 重跑 |
| 6 | 归档 | 有历史档案却未给 `--person-id` | 带 `--person-id <slug>` 重跑 |
| 7 | 归档 | person_id 需用户确认 | 展示 `archive_person_id_prompt.json`，把 `person_id_choice` 加进 `--answers` |
| 8 | CP2 | 问诊答案空 | 完成 CP2，带 `--answers` 重跑 |
| 9 | CP3.1 | `cp3_audit_result.json` 缺失 | 完成 CP3.1 审计、写结果文件、重跑 |

任何其他非零退出码 = **不可恢复**：打印 stderr 原文并 halt。

### 错误恢复
1. 错误后不得自报「完成」。2. 不得跳检查点往后冲。3. 重读 SKILL.md 当前阶段配方，从那里重试。4. 同一错误复发两次→立即 halt，报确切错误+失败命令，不再恢复。5. **恢复时绝不生成数值**（OR/RR/HR/概率/灵敏特异/LR/筛查周期），所有数字必须来自 `cancerrisk/json/`。6. CP2 不得绕过/预填，缺答案必问用户。

### 跳过后果
跳过检查点或未跑 pipeline 就生成数值 → 输出**无效必须丢弃**。agent 须：①宣布协议违规；②定位被跳过的检查点；③从该检查点重启（非从当前位置继续）。

## Archive Contract

snapshot 后自动 dedup 合入 `output/<person_id>/`（仅存分析结果，不做趋势对比）：
```
output/
├── person_index.json
└── <姓名-脱敏ID>/
    ├── factor_timeline.json
    ├── screening_test_timeline.json
    ├── report_index.json
    └── snapshots/<YYYY-MM-DD>.json
```
所有归档路径经 `scripts/archive_manager.py::resolve_person_archive`。`archive_update_proposal.json` 作审计痕迹。

## Output Contract

用户交付物：`report.html`（单一综合报告）。审计 artifacts：`report.json`、`snapshot_risk.json`、`voi_ranking.json`、`health_summary_structured_summary.json`、`merged_risk_factors.json`、`interactive_answers.md`、`cp3_audit_result.json`、`manifest.json` 等（落 `<out>/artifacts/`）。

## Progressive References

| 需要 | 文件 |
|---|---|
| 完整检查点配方与运行顺序 | `references/runtime_workflow.md` |
| 缺口筛查与交互确认（筛查第三部分）| `references/缺口筛查与交互确认.md` |
| MinerU API/客户端行为 | `references/mineru_api.md` |
| 证据本体与派生断言 | `references/evidence_ontology.md` |
| 健康总结 API/模板结构化 | `references/health_summary_rebuild.md` |
| snapshot/VoI/归档规则 | `references/risk_prediction.md` |
| 数据库四级索引（按需加载入口） | `references/database/index.json` + 各级 `index.json` |
| 运行配置 | `config/formal.yaml`（MinerU/金百森 token 走 `config/local.yaml`） |
| 确定性实现 | `scripts/*.py` |

## Safety / Verification

- 报告是决策辅助，非诊断。ontology 外的癌种不影响概率。性别不匹配的癌种 `posterior_probability: null`。提取失败在概率计算前 halt。
- 生产 MinerU/金百森 token 用 `config/local.yaml`，仓库不硬编码。
- 验证：`uv run --python 3.11 --with PyYAML --with jsonschema --with jinja2 --with requests python -m pytest -q`（测试位于 `tests/`，阶段3 迁移）。

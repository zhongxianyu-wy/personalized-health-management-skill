---
name: personalized-health-management
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
version: "2.0.5"
metadata:
  requires:
    bins: [python3, curl]    # python3≥3.10(推荐3.11); uv 由 launcher 探测，非必需
  network:
    domains: [mineru.net, ydai.jinbaisen.com, jiyinjia.jinbaisen.com]
---

# 个性化健康管理 Skill

把体检报告分析成单一综合 HTML 报告。单一入口（编排器）：

```bash
bash scripts/run.sh scripts/run_formal_analysis.py \
  --input <file-or-folder> --analysis-output <out> --person-id <stable_id>
```

> `scripts/run.sh` 是跨 runtime launcher（uv 优先，无 uv 走 python3+pip；详见 `references/deployment.md`）。下文 `scripts/X.py` 简记此调用；`...` 表示继承首步 `--input/--analysis-output/--person-id`，CP2 起加 `--answers`。归档默认落 `<cwd>/output/<person_id>/`（`--archives-root` 或 `CANCERRISK_OUTPUT_DIR` 覆盖）。

## Use / Refuse
**Use**：上传体检报告（PDF/图片/OCR md）→ 生成 `report.html`；同人重跑刷新；证据/答案更新后重跑。
**Refuse**：诊断/治疗/用药/紧急分诊/单症状问答/无报告的生活方式咨询。

## 数据库边界（PUA 核心）
- **数值**（OR/PPV/LR/先验/后验/sens/spec/价格）→ `cancerrisk/json/` + `pricing/json/`，**脚本算或权威读取，LLM 不碰**。
- **判读/筛查/套餐/复查/文案** → `*/md/`，**LLM 按需读**做判断。
- `evidence_text` 必须是源 md 的字面子串。健康总结走远程 API（display-only，不进概率）。

## Pipeline（12 阶段；🔴 需 agent 行动，余自动）
| # | 阶段 | 脚本 | CP |
|---|---|---|---|
| 1 | 环境检测 | `env_check.py` | — |
| 2 | MinerU OCR | `mineru_client.py` | — |
| 3 | 🔴 精炼建档 | agent 写 `refined.md` | **CP1** |
| 4 | 人口学 | `demographics.py` | — |
| 5 | 🔴 问诊+缺口确认 | `interactive_completion.py`（生成问卷→LLM 问诊） | **CP2** |
| 6 | 健康总结 API | `render_health_summary.py` | — |
| 7 | 🔴 健康总结结构化 | `finalize_structured_summary.py` | **CP4** |
| 8 | 🔴 证据填充+审计 | `build_assertion_fill_template.py`+`master_scan.py` | **CP3/3.1** |
| 9 | 贝叶斯风险 | `snapshot_risk.py` | — |
| 10 | 🔴 推荐筛查+独立缺口确认 | LLM 读 MD，脚本仅 PUA 校验 | **CP5** |
| 11 | 筛查 section artifact | LLM 读 MD | — |
| 12 | 综合报告+归档 | LLM 产 5 artifact → `build_report_json.py`+`render_report.py`+`archive_manager.py` | — |

## Minimal Workflow（4 轮；每轮跑到 `--stop-after`，agent 行动，再续跑）

1. **OCR**：`... --stop-after mineru`
2. 🔴 **CP1 精炼**：为每个 `artifacts/mineru/<id>/content.md` 写同目录 `refined.md`。质量门：<20 行或缺 {检查,化验,报告,结果,项目} → 停报错。保留人口学/异常行/肿瘤标志物(含正常值)/影像结论/阳性体征/**胃肠镜全段(息肉/病理/切除/Boston 评分)**；不概括掉值/单位/参考范围/日期。→ `... --stop-after interactive`
3. 🔴 **CP2 问诊**：读 `<out>/artifacts/interactive_questionnaire.json` 的 `questions`，**依次逐题问用户并收答案**——`conditional_on` 不满足的题跳过；用户选 label 则记 option 的 `value`。写 `<out>/answers.json` = `{"answers":{<qid>:<value>}}`，校验：
   ```bash
   ... scripts/validate_answers.py --questionnaire <out>/artifacts/interactive_questionnaire.json --answers <out>/answers.json --strict-no-inference
   ```
   （缺口筛查两步交互参 `references/缺口筛查与交互确认.md`，结果并入 answers.json）
4. 🔴 **CP3 填+审计**：`... --stop-after master-template` → 填 `structured_risk_factors_timeline.candidate.json` + `tumor_markers.candidate.json`（用 valid_factor_keys/valid_test_ids，evidence_text 字面子串）→ 校验 `validate_timeline_candidate.py`/`validate_tumor_markers.py` → **独立审计**（重读 refined.md 找漏抽）写 `cp3_audit_result.json` → `... --stop-after cp3-verify`
5. 🔴 **CP4 结构化**：`... --stop-after health-summary-api` → `finalize_structured_summary.py` 结构化 → `... --person-id <id> --stop-after screening-gap`（snapshot 自动）。
6. 🔴 **CP5 独立推荐筛查**：先读 `artifacts/cp5_context_pack.json`（脚本已按 age/gender 预筛周期项目、粗匹配历史检查、摘取 medium+ 癌症和健康总结异常），证据不足时再回查 `periodic_screening_schedule.json` / `refined.md` / `content.md` / 历史时间线。写 **1 个** `screening_recommendations.json`（A=cancer_risk / B=other_abnormalities / C=periodic_management + excluded_done_normal）。缺口问答内嵌在同一 JSON（每项附 `gap_question` + `gap_answer`），**不另产问卷/答案文件**。LLM 做最终判断+去重（A>B>C 优先级），脚本只校验 3 条核心规则（dedup / done+normal / medium+）。→ 不带 `--screening-gap-answers` 跑到 `--stop-after report-artifacts`。
7. 🔴 **5 artifact**：按 CP5 推荐产 5 section artifact（见下表）；A/B 分 priority/important，C 进 maintain。数值字段留空下游兜底 → `... scripts/assemble_package.py --package <out>/artifacts/package_tiers.json --skill-root <skill_root>`
8. **最终报告**：保留 `--answers`，不带 stop-after → exit 0 → `report.html` 就绪。

## 5 section artifact（LLM 产，落 `<out>/artifacts/`）
| artifact | schema | LLM 产（读知识库） | 留空（下游脚本算） |
|---|---|---|---|
| `timeline_tiers.json` | `{priority/important/maintain:[{dedup_key,item_name,interval,rationale}]}` | 读 CP5 final + 复查知识；**priority 排序规则**：BRCA/Lynch 遗传咨询 + 确诊癌随访 + 后验>1% + 健康总结高风险 → priority；未做高危筛查 + 后验0.5-1% + 异常复查 → important；周期常规 + 慢病 → maintain。**interval 须与分层时间窗一致**（「尽快」不能在 maintain）。A/B 分 priority/important，C → maintain | — |
| `x_addons.json` | **`[{risk_source,risk_level_tag,risk_level_label,method,interval,price_range,clinical_value}]`（必须是 list-of-dict，非 `{recommended_addons:[...]}`）** | risk_source/method/interval/tag/clinical_value（任意异常含乳腺/妇科/心电）。**B/C 去重**：同一病灶已在 B 不再进 C（C 只保留周期节奏措辞） | posterior/cancer_name（`_enrich_x_addons` 权威回填 + tag 归一化） |
| `package_tiers.json` | `[{name,price_range,includes[],note,recommended}]` 恒3档，仅1档 recommended=true | name/includes(**用 `08_pricing.json` 项目名或 key**)/note(患者面向)/recommended。**档1 必须覆盖 timeline priority 层全部检查项；档2 覆盖 priority+important** | price_range（`assemble_package` Σmid，含¥） |
| `liquid_biopsy_perf.json` | `{sensitivity,specificity,market_price_range,clinical_hint,negative_risk_reduction}` | clinical_hint/阴性降风险文案 | sens/spec(81.9/99.0)/market_price |
| `long_term_intervention.json` | `{genetic_management[](仅BRCA),lifestyle[](list-of-str)}` 仅2字段 | genetic+lifestyle（读 07预防MD） | — |

> 每条结论偶联数据出处（脚本 or 知识库，不编造）。套餐三档组成/复查三级阈值/缺口规则等详规见对应知识 MD（`套餐三档与风险驱动.md`/`癌症风险分层与复查规则.md`/`缺口筛查与交互确认.md`），不在本文件重复。CP4 结构化的 `risk_level`/`overall_assessment` 须有效（喂 X加项 ADR 徽章）。

## PUA（硬约束：数值来自脚本/知识库不编造；检查点走完）
**禁止**：跳检查点；不跑脚本就用自身知识产风险/概率/筛查建议；编造 OR/概率/灵敏特异/筛查周期/价格；从报告预填问诊答案（每题用户亲答）。
**跳过后果**：跳检查点或未跑脚本就产数值 → 输出**无效必须丢弃**，从被跳过的检查点重启。

## 退出码
| Code | 含义 | 动作 |
|---|---|---|
| 0 | 正常 | 进下一步 |
| 1 | MinerU/金百森请求失败 | 报错，用户确认后重试1次 |
| 2 | MinerU 全部 OCR 失败 | halt |
| 3 | CP1 refined.md 缺失/结构失败 | 写/修 refined，重跑 |
| 5 | 人口学 性别/年龄缺失 | 加 `--person-sex/--person-age` 重跑 |
| 6 | 归档 有档案未给 person-id | 加 `--person-id` 重跑 |
| 7 | 归档 person_id 需确认 | 展示 prompt |
| 8 | CP2 问诊答案空 | 完成 CP2，带 `--answers` 重跑 |
| 9 | CP3.1 cp3_audit_result.json 缺 | 完成审计，重跑 |
| 10 | report sections_incomplete（5 artifact 空/缺） | 产 5 artifact，重跑 |
| 11 | CP5 screening_recommendations.json 缺失或核心校验失败（dedup/done+normal/medium+） | 修正 A/B/C 推荐后重跑 |

其他非零 = 不可恢复：打印 stderr halt。同一错误复发两次 → halt 不再恢复。

## 产物
用户交付物：`report.html`。审计 artifacts（`report.json`/`snapshot_risk.json`/`cp5_context_pack.json`/`screening_recommendations.json`/`cp3_audit_result.json`/`manifest.json` 等）落 `<out>/artifacts/`。归档落 `<cwd>/output/<person_id>/`（经 `archive_manager.py`，自动）。

## Progressive References（按需加载）
| 需要 | 文件 |
|---|---|
| 完整运行顺序/检查点配方 | `references/runtime_workflow.md` |
| 缺口筛查与交互确认（CP5 LLM 分析指引） | `references/缺口筛查与交互确认.md` |
| **CP5 最小上下文包（优先读）** | **`artifacts/cp5_context_pack.json`** |
| **周期筛查周期表（CP5 查 age/gender→应筛+间隔）** | **`references/database/screening_general/json/periodic_screening_schedule.json`** |
| MinerU/健康总结 API 行为 | `references/mineru_api.md` / `references/health_summary_rebuild.md` |
| snapshot/归档规则 | `references/risk_prediction.md` |
| 癌种复查方法/周期（产 timeline） | `references/database/screening_personalized/json/cancer_followup_rules.json` |
| 套餐三档/复查三级/异常复查详规 | `references/database/screening_personalized/md/`（套餐三档与风险驱动/癌症风险分层与复查规则/异常指标复查推荐） |
| 数据库四级索引 | `references/database/index.json` |
| 跨 runtime 部署（WorkBuddy/QwenPaw） | `references/deployment.md` |
| 运行配置 | `config/formal.yaml`（token 走 `config/local.yaml`） |
| 确定性实现 | `scripts/*.py` |

## 验证
`bash scripts/run.sh scripts/run_formal_analysis.py --help`（入口可用）。测试 dev-only（不经生产 launcher）：`uv run --python 3.11 --with pytest python -m pytest -q`。

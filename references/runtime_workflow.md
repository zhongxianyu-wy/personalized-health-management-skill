# Runtime Workflow Reference

> 对齐 v0.1.2。详细操作笔记（不属于 `SKILL.md` 的部分），执行或调试分阶段 pipeline 时加载。
> 唯一权威 stop points = `run_formal_analysis.py::STOP_AFTER_CHOICES`。下文每条 `--stop-after` 值都在该列表内。

## Stop Points

`--stop-after` 取值（按 pipeline 顺序，完整 13 个 + 1 legacy 别名）：

| 值 | 含义 | 之后谁行动 |
|---|---|---|
| `config` | 配置审计后早退 | 调试 |
| `mineru` | OCR 完成，待精炼 | **CP1**（agent 写 refined.md） |
| `demographics` | 人口学解析后早退 | 调试 |
| `refine` | OCR 就绪，等 agent 写 refined | **CP1**（同 mineru 之后的精炼锚点） |
| `interactive` | 问卷写出 / 答案未应用 | **CP2**（agent 苏格拉底问诊） |
| `master-template` | candidate scaffold 就绪 | **CP3**（agent 填 candidate + 审计，一轮做） |
| `assertion-template` | master-template 的 legacy 别名 | 同上 |
| `cp3-verify` | 触发 CP3.1 审计任务 | **CP3.1**（agent 独立审计） |
| `risk-factor-gate` | candidate 校验并 merge 后早退 | 调试 |
| `health-summary-api` | 上游 API 响应就绪 | **CP4**（agent 结构化健康总结） |
| `archive-proposal` | 归档 proposal 写出并**自动应用** | 调试 |
| `archive` | 归档自动应用后 | 调试 |
| `report-artifacts` | snapshot/VoI/归档完成，待 5 section artifact | **报告前**（agent 产 5 JSON） |
| `report` | 最终报告就绪 | 交付 |

> ⚠ `health-summary` / `snapshot` / `longitudinal` **不是**有效 stop point（早期版本残留，已并入 `health-summary-api` / `risk-factor-gate` / `archive`）。

## Minimal Workflow（v0.1.1：7→4 轮 agent 介入）

`SKILL.md` 已合并为 4 轮 `uv run`：①mineru ②CP1 refine（agent）→③interactive（agent CP2）→⑤master-template（agent CP3+CP3.1 一轮）→⑥health-summary-api→report-artifacts（agent CP4+5 artifact 一轮）→⑦最终报告。每轮脚本部分（config→mineru[cache]→demographics→...→stop 点）自动跑，agent 只在 stop 点行动。

## Checkpoint 1: Refine

输入：`artifacts/mineru/<data_id>/content.md`。
输出：同目录 `refined.md`。

**质量门**：`content.md` 少于 20 行或不含 {检查,化验,报告,结果,项目} 任一关键词 → 停并报错（OCR 可能失败）。

**用 Read/Write 逐文件处理**，不要写 Python/heredoc 批处理脚本（缩进错误 + 多余临时文件）。

保留（其余丢弃）：

- 人口学与体检/报告日期；
- 异常化验行（带 `↑`/`↓`/`阳性`/`异常` 或超参考范围）；
- 肿瘤标志物行（**即使正常也留**）：AFP, CEA, CA19-9, CA125, PSA, f-PSA, CYFRA21-1, SCC, NMP22, CA72-4, CA15-3；
- 影像/检查结论（小结/结论/印象/诊断/建议 等标题下）；
- 阳性体征；
- **胃肠镜等内镜检查记录（含息肉/病理/切除/Boston 评分——若有必抽全段）**。漏抽会致"已做"误判为"未做过"（如肠镜+息肉切除被误判为缺口）。

不得概括掉值/单位/参考范围/日期/源短语（字面子串校验需要）。

## Checkpoint 2: Interactive Answers

问卷在 `config/formal.yaml::interactive`（含 `required_questions` / `SCREENING_FACTOR_IDS` 等）。

**真实运行必须逐题问用户**，不从报告推断生活方式/家族史/近期筛查状态。筛查行为题（PSA/肠镜/乳腺/宫颈）走报告提取不交互（被 `SCREENING_FACTOR_IDS` 过滤）。

校验（**真实运行必加 `--strict-no-inference`**，agent 编造答案会被该 flag 拒收）：

```bash
... scripts/validate_answers.py \
  --questionnaire <out>/artifacts/interactive_questionnaire.json \
  --answers <out>/answers.json --strict-no-inference
```

> `--allow-empty-interactive` 在 v0.1.2 **不存在**（argparse 无此 flag）。早期 prior-only 测试现在走正常的 `--answers` 空值路径。

## Checkpoint 3: Master Fill + CP3.1 审计（一轮完成）

输入：

- `artifacts/risk_factor_master.json`
- `artifacts/structured_risk_factors_timeline.candidate.json`
- `artifacts/tumor_markers.candidate.json`
- 所有 `refined.md`
- `interactive_answers.md`

填 candidate 规则：

- 只用 `valid_factor_keys`（timeline）/`valid_test_ids`（tumor markers）。
- 源文支持或明确否定某 factor 时才追加 record。
- `exam_date` = 报告日期；`interactive_answers.md` 才用 `"now"`（gate 会改写为 run 日期）。
- `evidence_text` 必须是 `source_md` 的**字面子串**（strip 掉 `**`/`*`/`_` markdown 标记后做子串匹配）。
- ontology 外的证据不建 factor（走 display-only 异常提示，不进概率计算）。

校验：

```bash
... scripts/validate_timeline_candidate.py \
  --candidate <out>/artifacts/structured_risk_factors_timeline.candidate.json
... scripts/validate_tumor_markers.py \
  --candidate <out>/artifacts/tumor_markers.candidate.json
```

**v0.1.1 合并**：master-template 与 cp3-verify 之间无远程调用，agent 在一轮内完成「填 candidate → 校验 → 写 `cp3_audit_result.json`（认知重置独立做审计）」，无需分两轮跑。

## CP3.1 审计（mandatory，不可跳）

`--stop-after cp3-verify` 打印结构化审计任务后 exit。即便 CP3 已确认完整也要做。

审计 agent 是**独立审计者**，不是 CP3 提取者：

- 独立读每个 `refined.md`，不锚定现有 candidate 记录；
- 找出所有临床异常发现；
- candidate 缺的发现 → 查 `risk_factor_master.json`（按 `factor_name`/`synonyms`/`factor_id`）匹配；
- 匹配且文档支持 → 加 `exists=true`；明确否定 → `exists=false`；无匹配 → 不加（自动进"证据库外异常提示"）；
- `evidence_text` 必须是 `refined.md` 的字面子串。

审计后，不带 `--stop-after` 重跑进 Gate。

## Imaging Findings

影像发现 = master 行 `factor_type == "imaging_finding"` 的 timeline 记录。agent 只在源文识别发现，PPV 元数据来自 master/evidence store（非 agent）。

典型目标：肺 CT 结节（大小/形态）、乳腺 BI-RADS、甲状腺 TI-RADS 或高风险钙化结节。影像 PPV 主导时，snapshot 用 imaging-workup tier 而非 low/medium/high incidence tier。

## Tumor Markers

肿瘤标志物是筛查 test，非 OR 风险 factor。用 `valid_test_ids` 填 `tumor_markers.candidate.json`。范围内正常 = `negative`；超阈或标记 = `positive`。gate 把 `test_id` join 到 `detection_performance_derived.json` 并应用 LR+/LR-。

## Checkpoint 4: 健康总结结构化 + 报告前 5 section artifact（一轮完成）

健康总结结构化输入：`health_summary_api_response.md` + `refined_content_bundle.md` + summary skeleton JSON。用 `scripts/finalize_structured_summary.py`，不手写 JSON。大 HTML 片段放文件，fills JSON 内用 `@path` 引用。保留 `raw_assessment_markdown` 原文。健康总结是 **display-only**——不掺 snapshot 概率 / ontology / OR/LR / 筛查推荐逻辑。

然后跑到 `report-artifacts`，snapshot/VoI/归档**自动完成**。agent 产 5 section artifact（`timeline_tiers.json` / `x_addons.json` / `package_tiers.json` / `liquid_biopsy_perf.json` / `long_term_intervention.json`，落 `<out>/artifacts/`），跑 `assemble_package.py` 求和套餐价格。5 artifact schema 见 `SKILL.md`「报告 section artifact」集中表。

## 归档（自动，无确认步骤）

snapshot 后**自动** dedup 合入 `output/<person_id>/`（`auto_apply=True` + `archive.require_user_confirmation: false`）。`archive_update_proposal.json` 作审计痕迹，不需 agent/用户确认。归档只存分析结果，不做趋势对比。

## 退出码速查

| Code | 来源 | 含义 |
|---|---|---|
| 0 | — | 正常 |
| 1 | MinerU/金百森 | 请求失败 |
| 2 | MinerU | 全部 OCR 失败 |
| 3 | CP1 | refined.md 缺失/结构失败 |
| 5 | 人口学 | 性别/年龄缺失 |
| 6 | 归档 | 有档案未给 person-id |
| 7 | 归档 | person_id 需确认 |
| 8 | CP2 | 问诊答案空 |
| 9 | CP3.1 | cp3_audit_result.json 缺失 |
| 10 | report | sections_incomplete（空壳，exit 10 halt） |

## 常见失败信号

- `interactive_skipped.warning`：未收用户答案（CP2 跳过）。
- `master_fill_skipped.warning`：CP3 填充被跳过（timeline AND tumor markers 都空）。
- `unknown_factor_key`：agent 造了 master 外的 factor。
- `evidence_text_not_found`：证据被改写或抄自错误源文件。
- exit 10 + `sections_incomplete=true`：5 section artifact 未产，报告空壳 → 跑 `--stop-after report-artifacts` 产 5 JSON 后重跑。
- health summary `json.JSONDecodeError`：用 `finalize_structured_summary.py`，别手写 heredoc JSON。

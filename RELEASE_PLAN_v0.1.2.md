# v0.1.2 发布前修复计划

> 分支：`v0.1.2`（基于 `v0.1.1`，commit `9c58893`）
> 创建：2026-06-22
> 目标：修复宪法审计 CRITICAL 4 项 + HIGH 6 项 → 达到 v0.1.2-beta 可发布状态
> 执行原则：按编号顺序执行，每步完成后 pytest 验证 + commit。不可跳步。

---

## 第 1 步：C1 — 空壳 report halt（CRITICAL）

**问题**：`sections_incomplete=True` 时 `run_formal_analysis.py` 只 stderr warning 不 halt → agent 跳过产 5 artifact 仍 exit 0 + 交付空壳。

**修复文件**：`scripts/run_formal_analysis.py`
**修复位置**：L1265-1271（report 渲染后 sections_incomplete 检查段）
**修复内容**：
- 将 warning 改为 `sys.exit(10)`（新增退出码 10 = sections_incomplete）
- 同步更新 `SKILL.md` 退出码表（L229-241 区域）加入 `| 10 | report | sections_incomplete=true | 产 5 section artifact 后重跑 |`

**验证**：构造 5 artifact 全空 → 跑 pipeline → exit 10（非 0）

---

## 第 2 步：C2 — frontmatter version 对齐（CRITICAL）

**问题**：`SKILL.md:21` `version: "0.1.0"` vs 全文/git 自称 v0.1.1/v0.1.2

**修复文件**：`SKILL.md`
**修复位置**：L21
**修复内容**：`version: "0.1.0"` → `version: "0.1.2"`

**验证**：`grep 'version:' SKILL.md | head -1` 显示 0.1.2

---

## 第 3 步：C3 — runtime_workflow.md 重写（CRITICAL）

**问题**：`references/runtime_workflow.md` 全面过期——stop points 列了不存在的值（health-summary/snapshot/longitudinal）、flag 已删（--allow-empty-interactive）、manual 入档已自动化但文档仍描述手动确认。

**修复文件**：`references/runtime_workflow.md`
**修复内容**（全文重写，对齐 v0.1.2 实际）：
- stop points 列表对齐 `run_formal_analysis.py` 的 `STOP_AFTER_CHOICES`（L32-47）
- 删除 `--allow-empty-interactive` 描述（argparse 无此 flag）
- Archive 段改为自动归档（`require_user_confirmation: false` + `auto_apply=True`）
- CP 配方对齐 Minimal Workflow v0.1.1（7→4 步合并）
- refined.md 配方含胃肠镜必抽（darwin dim8 修复）

**验证**：grep runtime_workflow.md 中的 stop points → 全部在 STOP_AFTER_CHOICES 内

---

## 第 4 步：H5 — candidate 空 gate 加固（HIGH）

**问题**：`_guard_master_fill_not_empty`（run_formal_analysis.py:134-172）要求 timeline AND tumor_markers **都空**才 halt → agent 填一个 tumor marker 即可让 timeline 空也不 halt。

**修复文件**：`scripts/run_formal_analysis.py`
**修复位置**：L901-905（`_guard_master_fill_not_empty` 调用段）
**修复内容**：
- 增加：当 timeline records=0 且 refined.md 含异常证据关键词（结节/息肉/肿块/阳性/↑/异常）→ stderr 警告"timeline 空但 refined.md 含异常发现，可能漏填 CP3"（warning 不 halt，因空 timeline 对纯代谢异常报告合法）

**验证**：refined 含"结节" + timeline 空 → stderr 警告输出

---

## 第 5 步：H6 — validate_answers 默认 strict（HIGH）

**问题**：SKILL.md Minimal Workflow 第 4d 步校验命令缺 `--strict-no-inference`，agent 编造答案可通过默认校验。

**修复文件**：`SKILL.md`
**修复位置**：L126（Minimal Workflow 第 4d 步 validate_answers 命令）
**修复内容**：命令加 `--strict-no-inference`

**验证**：grep SKILL.md validate_answers → 含 --strict-no-inference

---

## 第 6 步：H7 — demo token 移出入库（HIGH）

**问题**：`config/formal.yaml:13` demo_token 是完整 JWT（含 phone/uuid/exp），clone 仓库即可盗用。

**修复文件**：`config/formal.yaml`
**修复内容**：
- L13 `demo_token:` 值清空 → `demo_token: ""`
- 新增注释：`# demo token 请通过 config/local.yaml::mineru.user_token 覆盖；空值时 use_demo_token_by_default 生效但需用户提供`
- 确认 `config/local.yaml` 已在 `.gitignore`

**验证**：grep formal.yaml demo_token → 值为空

---

## 第 7 步：H9 — name kebab-case + 匹配文件夹（HIGH）

**问题**：`name: 个性化健康管理` 非 kebab-case，且与文件夹 `个性化健康管理skill` 不匹配。

**修复文件**：`SKILL.md` L2
**修复内容**：`name: 个性化健康管理` → `name: personalized-health-management`
> 注意：中文 frontmatter name 在部分 skill loader 中可能不兼容。kebab-case 英文名是宪法要求。

**验证**：grep 'name:' SKILL.md frontmatter → personalized-health-management

---

## 第 8 步：H10 — 补 3 个脚本 _env_bootstrap（HIGH）

**问题**：`voi_calculator.py`、`snapshot_risk.py`、`interactive_completion.py` 有 `__main__` 但无 `_env_bootstrap` 注入。

**修复文件**：`scripts/voi_calculator.py`、`scripts/snapshot_risk.py`、`scripts/interactive_completion.py`
**修复内容**：在每个文件 `from __future__ import annotations` 后插入：
```python
import sys as _sys
_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import _env_bootstrap  # noqa: F401
```

**验证**：grep `_env_bootstrap` 3 个文件 → 全部命中

---

## 第 9 步：M11 — CP4 .html 描述矛盾修正（MEDIUM）

**问题**：SKILL.md:195 称"temp 不读 health_summary.blocks"，但 temp:285 实际读了 `blocks.overall_assessment` 和 `blocks.risk_level`（ADR 徽章）。

**修复文件**：`SKILL.md`
**修复位置**：L195（REDUNDANT-1 注）
**修复内容**：改为"temp 模版 X加项标题读取 `health_summary.blocks.overall_assessment`（ADR 徽章）和 `blocks.risk_level`（着色），CP4 fills 的 .html 需含有效 risk_level/overall_assessment 值"

**验证**：grep SKILL.md health_summary.blocks → 描述与模版实际一致

---

## 第 10 步：M12-M13 — 删冗余（MEDIUM）

**问题**：5 schema 表与逐项重复 ~15 行；BLOCKER/REDUNDANT 开发注释残留。

**修复文件**：`SKILL.md`
**修复内容**：
- 删除 L197-202 逐项 schema bullet（已被 L185-195 集中表覆盖）
- 删除 L185 BLOCKER-6 标注、L195 REDUNDANT-1 标注中的开发期标记（保留功能描述，删 `[BLOCKER-6]` `[REDUNDANT-1]` 前缀）

**验证**：wc -l SKILL.md → 应减少 ~15 行

---

## 第 11 步：全量 pytest + 端到端验证

**命令**：
```bash
cd /Volumes/exp/project/cancerrisk_beta_v2.0/个性化健康管理skill
uv run --python 3.11 --with PyYAML --with jsonschema --with jinja2 --with requests --with pytest \
  python -m pytest tests/ -q
```
- 预期：62+ passed（可能 +1 新退出码测试）
- 端到端：跑 test_1 全流程 → report.html 非空壳（sections_incomplete=False）+ exit 0

---

## 第 12 步：commit + push

```bash
git add -A
git commit -m "release(v0.1.2): 宪法审计修复 C1-C3+H5-H10+M11-M13（发布前加固）"
git push origin v0.1.2
```

---

## 退出码表（v0.1.2 更新后）

| Code | 来源 | 含义 | 必需动作 |
|---|---|---|---|
| 0 | — | 正常 | 进下一步 |
| 1 | MinerU/金百森 | 请求失败 | 报错误；重试 |
| 2 | MinerU | 全部 OCR 失败 | halt |
| 3 | CP1 | refined.md 缺失 | 写 refined，重跑 |
| 5 | 人口学 | 性别/年龄缺失 | 加 CLI flag 重跑 |
| 6 | 归档 | 有档案未给 person-id | 加 --person-id 重跑 |
| 7 | 归档 | person_id 需确认 | 展示 prompt |
| 8 | CP2 | 问诊答案空 | 完成 CP2 重跑 |
| 9 | CP3.1 | cp3_audit_result.json 缺 | 完成审计重跑 |
| **10** | **report** | **sections_incomplete（空壳）** | **产 5 section artifact 后重跑** |

---

## 不在本批范围（后续 v0.2.0）

- H8 缺口两步交互脚本化（大改，需设计 interactive_completion 扩展）
- M14 allowed-tools 收窄 Bash（需评估各 runtime 兼容）
- M15 Gotchas 段起步
- B6 agent-loop eval（≥3 positive + 3 negative）
- C4 human safety review（需用户介入）
- BLOCKER-4 术后治愈状态（risk_factor_master 扩）
- BLOCKER-2 content→refined 半自动
- BLOCKER-5 金百森 API 结构化 JSON

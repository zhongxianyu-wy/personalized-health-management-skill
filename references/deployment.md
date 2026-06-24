# 部署指南（WorkBuddy 优先 / QwenPaw 次要）

> v0.1.4 起，本 skill 适配通用 Agent Skills 范式（OpenClaw 兼容 SKILL.md），
> 可在 WorkBuddy（腾讯）与 QwenPaw（AgentScope/阿里）沙箱运行。本文档汇总两平台的安装、约束与跨 runtime 注意事项。

## 1. Skill 身份与安装路径

- **`name`（通用 loader 识别的唯一标识）**：`personalized-health-management`（见 `SKILL.md` frontmatter）
- **GitHub repo**：`personalized-health-management-skill`
- **安装路径（通用范式约定）**：`skills/personalized-health-management/`
- **dev 文件夹名**（`个性化健康管理skill`）仅本地开发用，与安装目录名不同——通用 loader 读 frontmatter `name` 而非目录名，故不影响发现/加载。安装时按 `name` 目录化即可。

## 2. WorkBuddy（腾讯，主要平台）安装与约束

- **平台范式**：OpenClaw 兼容 SKILL.md（官方"兼容 OpenClaw 技能"），底层为 CodeBuddy Agent runtime + MCP + 沙箱。
- **安装**：将本目录复制或符号链接到 `skills/personalized-health-management/`（沙箱技能目录，常见 `~/.workbuddy/skills/`）。
- **沙箱约束**（部署前须满足）：
  - **出口网络白名单**放行 §5 的 3 个域名（OCR + 健康总结 API，pipeline 必经）。
  - 沙箱须有 `python3`（≥3.10，推荐 3.11）+ `curl`（取健康总结 API token，见 §7）。
  - **uv 非必需**：`scripts/run.sh` launcher 自动探测，无 uv 走 `python3 + pip --user` 兜底（见 §4）。
  - 沙箱只读 skill 包时，输出落 `<cwd>/output/`（见 §6），不写回 skill 包。

## 3. QwenPaw（AgentScope/阿里，次要平台）安装与约束

- **安装**：CLI `qwenpaw skills install <github-url>` 可直接从 GitHub 仓库导入（支持 `https://github.com/...`）；或控制台「工作区 → 技能 → 从 Skills Hub 导入」。安装后默认启用。
- **依赖声明**：QwenPaw 读 `SKILL.md::metadata.requires.bins/env`（扁平列表）透出为 `require_bins`/`require_envs`。本 skill 声明 `bins: [python3, curl]`、`env: [CANCERRISK_OUTPUT_DIR]`。
- **安全扫描**：见 §7——`demo_token`（公共试用 JWT）会被标红，手动批准即可。
- **uv 兜底 / 网络 / 输出**：同 WorkBuddy（§4/§5/§6）。
- **CP2 交互**：QwenPaw 多频道（console/钉钉/飞书/微信等），苏格拉底问诊走原生轮次对话（见 §8）。

## 4. 跨 runtime 启动：`scripts/run.sh` launcher（核心）

所有入口命令统一走 launcher，调用方无需关心沙箱有无 uv：

```bash
bash scripts/run.sh scripts/run_formal_analysis.py --input <pdf> --analysis-output <out> --person-id <id> [其他flag]
bash scripts/run.sh scripts/env_check.py --json                       # 环境探测
```

> 测试（pytest）是 **dev-only**，不经过生产 launcher：开发者自行 `uv run --python 3.11 --with pytest python -m pytest -q`。launcher 只携生产运行时依赖（PyYAML/jsonschema/jinja2/requests）。

launcher 行为：
1. **有 uv**（dev 机 / 装了 uv 的沙箱）→ `uv run --python 3.11 --with PyYAML --with jsonschema --with jinja2 --with requests python <script>`
2. **无 uv**（WorkBuddy/QwenPaw 沙箱常见）→ 探测 `python3.11`/`python3.10`/`python3`(校验 ≥3.10) → `pip install --user PyYAML jsonschema jinja2 requests` → 直接跑
3. **既无 uv 又禁 pip 出网** → `exit 1` 明确报错（不静默降级到 import 失败）

- **Windows（CoPaw 等）**：launcher 是 POSIX bash，Windows 建议直接用 `uv run`（dev 环境）；PYTHONHOME 污染由各脚本内嵌 `_env_bootstrap` 清理。
- 依赖落 `~/.local`（沙箱只读 site-packages 时唯一可写处）。

## 5. 网络白名单（3 域名，必放行）

| 域名 | 用途 | 配置位置 |
|---|---|---|
| `mineru.net` | MinerU OCR + token 注册 | `config/formal.yaml::mineru.api_base` / `token_register_url` |
| `ydai.jinbaisen.com` | 健康总结 API（chat completions） | `config/formal.yaml::health_summary.api.base_url` |
| `jiyinjia.jinbaisen.com` | 健康总结 API 动态 token | `config/formal.yaml::health_summary.api.token_url` |

> 冷启动若沙箱无 uv 还需放行 `astral.sh`（uv 安装源）；建议直接预装 uv 或走 §4 兜底，避免依赖该域名。

## 6. 输出路径

- **默认**：`<cwd>/output/<person_id>/`（沙箱友好，cwd 是 agent 可写工作目录，非只读 skill 包）。
- **覆盖**：`--archives-root <dir>` 或环境变量 `CANCERRISK_OUTPUT_DIR`（沙箱部署由平台注入）。
- 分析产物（`--analysis-output <out>`）独立于归档根，仍需调用方显式指定可写目录。

## 7. demo_token 与 QwenPaw 安全扫描

- `config/formal.yaml::mineru.demo_token` 是 **OpenXLab 公共试用 JWT**（phone/email 字段为空、无 PII，任何人可在 mineru.net 免费注册），**内置不动**（开箱即用，供内部工作人员便捷使用）。
- **QwenPaw 安全扫描**会把它标红为"硬编码 secret"——这是**已知且有意接受的取舍**（真正风险仅为共享 token 配额耗尽，非凭据泄露）。安装时**手动批准**即可。
- **生产环境**覆盖（可选）：`config/local.yaml::mineru.user_token`（local.yaml 已 gitignore），无需改 formal.yaml。

## 8. CP2 苏格拉底问诊的跨 runtime 交互映射

交互**语义**统一由 `SKILL.md` 正文（Minimal Workflow 第4步 4a-4e）定义，frontmatter `allowed-tools` 不绑定单一 runtime 的 tool 名。各 runtime 用自身交互机制实现：

| 语义（SKILL.md 正文） | Claude Code | WorkBuddy | QwenPaw |
|---|---|---|---|
| 每次只问一题 | `AskUserQuestion` 单题 | 原生轮次对话单题 | 原生轮次对话（console/钉钉/飞书…） |
| trigger 即跟进 | `conditional_on` 门控 | 同左 | 同左 |
| label↔value 记录 | option.value | 同左 | 同左 |
| 不完整重问 | 完整度标准重问 | 同左 | 同左 |
| 缺口筛查两步交互（4e） | AskUserQuestion 两步 | 原生轮次两步 | 同左 |

> 答案统一写入 `<out>/answers.json` 并经 `validate_answers.py --strict-no-inference` 校验（拒收 agent 编造/推断的答案）。

## 9. 快速验证（部署后）

```bash
bash scripts/run.sh scripts/env_check.py --json                 # 环境探测（uv/python/curl/token/网络）
bash scripts/run.sh scripts/run_formal_analysis.py --help       # 入口可用
bash scripts/run.sh scripts/run_formal_analysis.py \
  --input <pdf> --analysis-output <out> --person-id <id> --stop-after mineru   # OCR 冒烟
# 测试是 dev-only（不经过生产 launcher）：uv run --python 3.11 --with pytest python -m pytest -q
```

## 10. 已知不在本批范围

- 真实 WorkBuddy/QwenPaw 沙箱实测（网络白名单 / uv 兜底 / 输出路径）需在对应平台环境验证；本批为代码适配 + dev 机验证。
- L2 SKILL.md 体积瘦身、routing eval 属 v0.2.0。

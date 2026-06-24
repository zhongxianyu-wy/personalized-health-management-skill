#!/usr/bin/env bash
# 通用 launcher —— 跨 runtime 沙箱适配（v0.1.4）。
#
# 解决 WorkBuddy/QwenPaw 沙箱可能无 uv 的问题：uv 优先，无 uv 则 python3 + pip 兜底。
# SKILL.md 所有入口命令统一走 `bash scripts/run.sh <script.py> [args...]`，
# 由本 launcher 决定运行时与依赖安装方式，调用方无需关心。
#
# 用法:
#   bash scripts/run.sh scripts/run_formal_analysis.py --input <pdf> --analysis-output <out> ...
#   bash scripts/run.sh scripts/env_check.py --json
#   bash scripts/run.sh -m pytest tests/ -q          # 模块模式：第一个参数以 - 开头则直接交给 python -m
#
# 行为:
#   1. 有 uv  → uv run --python 3.11 --with PyYAML --with jsonschema --with jinja2 --with requests
#   2. 无 uv  → 选 python3.11 / python3.10 / python3(≥3.10) + pip install --user 依赖 + 直接跑
#   3. 既无 uv 又禁 pip 出网 → exit 1 明确报错（不静默降级到 import 失败）
#
# 沙箱只读 skill 包时，依赖落 ~/.local，输出落 $CANCERRISK_OUTPUT_DIR 或 cwd/output。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEPS="PyYAML jsonschema jinja2 requests"

if [ "$#" -lt 1 ]; then
  echo "用法: bash scripts/run.sh <script.py|模块模式> [args...]" >&2
  exit 2
fi

# 模块模式：第一个参数以 - 开头（如 -m pytest）→ 交给 python -m
MODULE_MODE=0
if [[ "$1" == -* ]]; then
  MODULE_MODE=1
fi

# 解析目标脚本路径：相对调用方 cwd（SKILL_ROOT）传入的 scripts/X.py 优先；
# 不在 cwd 则回退到 SKILL_ROOT 下（支持从任意目录调用）。
if [ "$MODULE_MODE" = "0" ]; then
  TARGET="$1"
  [ -f "$TARGET" ] || TARGET="$SKILL_ROOT/$1"
fi

# ---------- 1. uv 优先 ----------
if command -v uv >/dev/null 2>&1; then
  if [ "$MODULE_MODE" = "1" ]; then
    cd "$SKILL_ROOT"
    exec uv run --python 3.11 \
      --with PyYAML --with jsonschema --with jinja2 --with requests \
      python "$@"
  fi
  exec uv run --python 3.11 \
    --with PyYAML --with jsonschema --with jinja2 --with requests \
    python "$TARGET" "${@:2}"
fi

# ---------- 2. 无 uv 兜底：选 python ----------
if command -v python3.11 >/dev/null 2>&1; then
  PY=python3.11
elif command -v python3.10 >/dev/null 2>&1; then
  PY=python3.10
else
  PY=python3
  if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    echo "[run.sh] FATAL: 需要 Python >=3.10，当前 $("$PY" -V 2>&1)。请装 python3.11 或 uv。" >&2
    exit 1
  fi
fi

# 依赖：检测缺失才 pip install --user（沙箱只读 site-packages 时唯一可写处）
NEED_INSTALL=0
for dep in $DEPS; do
  "$PY" -c "import importlib.metadata as m; m.version('$dep')" 2>/dev/null || { NEED_INSTALL=1; break; }
done
if [ "$NEED_INSTALL" = "1" ]; then
  echo "[run.sh] 无 uv，用 pip --user 安装依赖 ($DEPS) 到 ~/.local" >&2
  "$PY" -m pip install --user --quiet $DEPS >/dev/null 2>&1 || {
    echo "[run.sh] pip install 失败（沙箱可能禁网或无 pip）。请用 uv，或预装依赖。" >&2
    exit 1
  }
fi

# ---------- 3. 执行 ----------
if [ "$MODULE_MODE" = "1" ]; then
  cd "$SKILL_ROOT"
  exec "$PY" "$@"
fi
exec "$PY" "$TARGET" "${@:2}"

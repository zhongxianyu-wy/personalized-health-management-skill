"""跨 runtime 环境自检 bootstrap（v0.1.1）。

CoPaw/Windows 等环境系统 PYTHONHOME 指向 Python 3.12 会污染 uv 管理的 3.11，
导致 SRE module mismatch / ModuleNotFoundError。本模块在任何 import 之前清除
污染变量 + 强制 UTF-8 输出（中文 GBK 报错）。

用法：在入口脚本最顶部（from __future__ 之后）加：
    import _env_bootstrap  # noqa: F401 — 必须在所有其他 import 之前
"""
import os
import sys

os.environ.pop("PYTHONHOME", None)
os.environ.pop("PYTHONPATH", None)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

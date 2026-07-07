# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for mini-coding-agent.

用法:
    .venv/bin/pip install pyinstaller
    .venv/bin/pyinstaller pyinstaller.spec

产物在 dist/mini-agent（macOS/Linux 可执行文件）。

注意: 生成的是当前平台的可执行文件，无法跨平台。
如需分发给不同 OS 的用户，需要在对应平台上分别构建。
"""

import os
import sys
from pathlib import Path

block_cipher = None

# 项目根目录
PROJECT_ROOT = os.path.abspath(os.path.dirname(SPEC))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")

# ─── 收集需要打包的数据文件 ───────────────────────────────────
datas = []

# 包含 skills 目录中的 YAML 文件（如果存在项目级 skills/）
skills_dir = os.path.join(PROJECT_ROOT, "skills")
if os.path.isdir(skills_dir):
    datas.append((skills_dir, "skills"))

# ─── 隐式导入（PyInstaller 静态分析可能遗漏的模块）───────────
hiddenimports = [
    # OpenAI SDK 内部动态导入
    "openai",
    "openai.lib.streaming",
    "openai.resources",
    "openai.resources.chat",
    "openai.resources.chat.completions",
    # python-dotenv
    "dotenv",
    # Typer / Rich / prompt_toolkit
    "typer",
    "rich",
    "prompt_toolkit",
    # 项目内部模块（flat imports 可能被遗漏）
    "agent",
    "agent.async_loop",
    "agent.async_tools",
    "agent.async_tool_registry",
    "agent.display",
    "agent.message_utils",
    "agent.path_sandbox",
    "agent.subagent",
    "agent.tools",
    "cli",
    "cli.commands",
    "cli.commands.chat",
    "cli.commands.config_cmd",
    "cli.commands.run",
    "cli.display",
    "cli.input",
    "cli.main",
    "config",
    "context_manager",
    "context_manager.context",
    "context_manager.macro_compressor",
    "context_manager.meso_compressor",
    "context_manager.micro_compressor",
    "context_manager.pipeline",
    "context_manager.task_graph",
    "llm",
    "llm.factory",
    "llm.interface",
    "llm.message_wrapper",
    "llm.openai_provider",
    "logger",
    "session",
    "session.manager",
    "skills",
    "skills.loader",
    "skills.skill_tool",
]

# ─── 可选: 智谱 AI 支持 ───────────────────────────────────────
try:
    import zhipuai  # noqa: F401

    hiddenimports.extend(["zhipuai", "zhipuai.core", "zhipuai.api_resource"])
except ImportError:
    pass

a = Analysis(
    [os.path.join(SRC_DIR, "cli", "main.py")],
    pathex=[SRC_DIR],  # 让 PyInstaller 能找到 flat import 的模块
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 排除不需要的大型依赖（减小体积）
        "tkinter",
        "matplotlib",
        "numpy",
        "scipy",
        "pandas",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # 使用目录模式（比单文件启动更快）
    name="mini-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,  # 去除调试符号（减小体积）
    upx=True,  # 使用 UPX 压缩（需系统安装 upx）
    console=True,  # 终端应用
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=True,
    upx=True,
    upx_exclude=[],
    name="mini-agent",
)

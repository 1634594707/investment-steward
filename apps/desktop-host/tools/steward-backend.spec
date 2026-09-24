# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：把三个 Python sidecar 冻结成一个 steward-backend.exe。

关键：uvicorn/fastapi/sqlalchemy 大量动态导入，必须 collect_all 收全数据与子模块；
core 自身的插件能力实现（official.*）是本包子模块，一并 collect_submodules。
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = "../../../"  # 相对 spec 所在目录（apps/desktop-host/tools）

datas: list = []
binaries: list = []
hiddenimports: list = []

for pkg in (
    "uvicorn",
    "fastapi",
    "starlette",
    "sqlalchemy",
    "pydantic",
    "pydantic_core",
    "cryptography",
    "alembic",
    "anyio",
    "httpx",
    "httpcore",
):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# uvicorn 的 loop/protocol 是字符串配置后动态 import 的，PyInstaller 静态分析不到
hiddenimports += [
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "uvicorn.logging",
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.sqlite.pysqlite",
]

# 包内数据文件（宏观手册静态回退数据）：冻结后靠 sys._MEIPASS 定位，必须显式收集。
datas += [
    (ROOT + "apps/core-api/src/investment_steward_core/data", "investment_steward_core/data"),
]

# 三个 sidecar 包（源码在 pathex 里，非 site-packages）
hiddenimports += collect_submodules("investment_steward_core")
hiddenimports += collect_submodules("investment_steward_agent_worker")
hiddenimports += collect_submodules("investment_steward_plugin_runner")

block_cipher = None

a = Analysis(
    ["sidecar_entry.py"],
    pathex=[
        ROOT + "apps/core-api/src",
        ROOT + "apps/agent-worker/src",
        ROOT + "apps/plugin-runner/src",
    ],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="steward-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # sidecar 需要 stdout（runner 走 JSON-RPC、core 打 core_ready 行）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

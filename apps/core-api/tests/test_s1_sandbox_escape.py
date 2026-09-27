"""S1（用户视角路线图 2026-09-26）：策略包沙箱逃逸的回归防线。

改造前实测（真实 `_install_audit_hook` + `_restricted_builtins` 配置下执行包代码）：
`os.spawnv` / `os.startfile` 抛的是 `FileNotFoundError` 而**不是** `PermissionError`
——调用已抵达操作系统层，只因探针指向不存在的目标才失败，换真实目标即执行成功。
根因是 `BLOCKED_EVENTS` 只列了 `os.system`/`os.exec`/`subprocess.Popen`，漏掉它们在
Windows 上的同族入口；且 `_restricted_builtins()` 只过滤内建**名字**、不限制属性访问，
包代码可用 `().__class__.__mro__[1].__subclasses__()` 取到全部已加载类，再经
`.__init__.__globals__` 拿到模块全局（含 `os`）。

修复三件事：补齐黑名单、覆盖 `object.__subclasses__`、exec 前清空模块全局。

本文件**走真实 one-shot 子进程路径**（`run_pack_payload`），不是同进程模拟——因为逃逸是否
成立取决于钩子与内建表的实际组合，只有真跑子进程才算数。
"""

from __future__ import annotations

import pytest
from investment_steward_core.quant_pack_runner import ALLOWED_IMPORTS, run_pack_payload

BARS = [
    {"timestamp": f"2026-09-{day:02d}", "open": 10.0, "close": 10.5, "high": 10.8, "low": 9.9, "volume": 1000}
    for day in range(1, 6)
]


def _run(code: str):
    return run_pack_payload(code, BARS)


# --------------------------------------------------------------------------
# 正向：加固不得破坏正常策略包
# --------------------------------------------------------------------------

def test_normal_pack_still_runs():
    result = _run("def run(bars):\n    return [0.5] * len(bars)\n")
    assert result["ok"] is True, result
    assert result["positions"] == [0.5] * len(BARS)


@pytest.mark.parametrize("module", sorted(ALLOWED_IMPORTS))
def test_every_allowed_module_is_actually_importable(module):
    """`ALLOWED_IMPORTS` 名义放行的模块必须真的能用。

    历史不一致：白名单里的 statistics / decimal / fractions / bisect 都是带 .py 文件的
    包，包代码 `import` 它们会触发 `open` 读文件 → 被黑名单拦下，于是「白名单写了却用不了」。
    修复方式是在装钩子**之前**把白名单模块全部预导入进 sys.modules。
    """
    result = _run(f"def run(bars):\n    import {module}\n    return [0.0] * len(bars)\n")
    assert result["ok"] is True, f"白名单模块 {module} 实际不可用：{result.get('error')}"


def test_timeout_still_kills():
    result = run_pack_payload("def run(bars):\n    while True:\n        pass\n", BARS, timeout_seconds=3)
    assert result["ok"] is False
    assert "超时" in result["error"]


# --------------------------------------------------------------------------
# 逃逸防线
# --------------------------------------------------------------------------

def test_introspection_builtins_removed_from_namespace():
    """`type`/`object`/`super`/`globals` 等内省入口不再直接可用。

    注意：这是**抬高成本**，不是安全边界——CPython 不允许覆盖内置类型的
    `__subclasses__`（`object` 是 immutable type），`().__class__.__mro__[1].__subclasses__()`
    永远可达。真正的强制点是审计黑名单，见下面各条。
    """
    result = _run(
        "def run(bars):\n"
        "    present = [n for n in ('type', 'object', 'super', 'globals', 'vars') "
        "               if n in __builtins__]\n"
        "    if present:\n"
        "        raise RuntimeError('still exposed: ' + ','.join(present))\n"
        "    return [0.0] * len(bars)\n"
    )
    assert result["ok"] is True, f"内建内省入口仍暴露：{result.get('error')}"


def test_process_creation_blocked_even_via_subclasses_chain():
    """核心防线：即使用经典 `__subclasses__` 链摸到 `os`，进程创建仍被审计钩子拦下。

    这才是 S1 的实际修复点——补齐黑名单让「摸到 os 模块对象」不再等于「能做事」。
    改造前 `os.spawnv` 返回 FileNotFoundError（已抵达 OS 层），现在必须 PermissionError。
    """
    result = _run(
        "def run(bars):\n"
        "    subs = ().__class__.__mro__[1].__subclasses__()\n"
        "    osmod = None\n"
        "    for c in subs:\n"
        "        f = getattr(c, '__init__', None)\n"
        "        for v in getattr(f, '__globals__', {}).values():\n"
        "            if getattr(v, '__name__', '') == 'os':\n"
        "                osmod = v\n"
        "    if osmod is None:\n"
        "        raise RuntimeError('os 不可达（链已断）')\n"
        "    osmod.spawnv(osmod.P_WAIT, 'nope.exe', ['nope.exe'])\n"
        "    return [0.0] * len(bars)\n"
    )
    assert result["ok"] is False, "经 __subclasses__ 链的进程创建未被拦截"
    assert "FileNotFoundError" not in result["error"], "调用仍触达了操作系统层"
    assert "os.spawn" in result["error"]


@pytest.mark.parametrize(
    "call",
    [
        "os.spawnv(os.P_WAIT, 'nope.exe', ['nope.exe'])",
        "os.startfile('nope.exe')",
        "os.system('echo hi')",
    ],
)
def test_process_creation_blocked(call):
    """进程创建同族入口全部拒绝。

    注意断言的是 `ok is False`——**不是**特定异常类型。改造前这些调用返回
    `FileNotFoundError`（说明已抵达 OS 层），现在必须由沙箱主动拦下。
    """
    result = _run(f"def run(bars):\n    import os\n    {call}\n    return [0.0] * len(bars)\n")
    assert result["ok"] is False, f"{call} 竟被执行了"
    assert "FileNotFoundError" not in result["error"], f"{call} 仍触达了操作系统层"


def test_file_mutation_blocked():
    result = _run("def run(bars):\n    import os\n    os.remove('nope.txt')\n    return [0.0] * len(bars)\n")
    assert result["ok"] is False
    assert "FileNotFoundError" not in result["error"]


def test_directory_enumeration_blocked():
    result = _run("def run(bars):\n    import os\n    os.listdir('.')\n    return [0.0] * len(bars)\n")
    assert result["ok"] is False


def test_udp_and_dns_blocked():
    result = _run(
        "def run(bars):\n"
        "    import socket\n"
        "    socket.getaddrinfo('example.com', 80)\n"
        "    return [0.0] * len(bars)\n"
    )
    assert result["ok"] is False, "DNS 解析未被阻断"


def test_module_globals_purged():
    """经可达 Python 函数的 `__globals__` 不得再看到 os / sys / subprocess。

    `_child_main` 在 exec 前清空模块全局；`__import__`（guarded_import）本身是包代码
    可达的 Python 函数，它的 `__globals__` 正是逃逸入口，故必须一并验证已清空。
    """
    result = _run(
        "def run(bars):\n"
        "    g = __builtins__['__import__'].__globals__\n"
        "    leaked = [k for k in ('os', 'sys', 'subprocess', 'json') if k in g]\n"
        "    if leaked:\n"
        "        raise RuntimeError('globals leak: ' + ','.join(leaked))\n"
        "    return [0.0] * len(bars)\n"
    )
    assert result["ok"] is True, f"模块全局未清空：{result.get('error')}"


def test_clock_contract_enforced():
    """`quant_pack_runner` 契约：结果必须由输入确定性推导，不得使用随机数或时钟。"""
    blocked = _run("def run(bars):\n    import time\n    return [0.0] * len(bars)\n")
    assert blocked["ok"] is False
    assert "import time" in blocked["error"]


def test_disallowed_import_still_blocked():
    result = _run("def run(bars):\n    import os\n    return [0.0] * len(bars)\n")
    assert result["ok"] is False
    assert "import os" in result["error"]

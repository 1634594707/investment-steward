"""策略包受限执行器(quant_pack_runner):阶段 B 的执行环境。

信任模型(ADR-0008 修订):只有通过发布者 Ed25519 验签的策略包才会入库,执行器只对
这些已签名包运行。执行边界为双层:

1. 一次性子进程:崩溃/超时/资源残留随进程终结,父进程强制 timeout 终止;
2. CPython 审计钩子(PEP 578):在包代码运行前安装,阻断文件读写、网络、子进程、
   动态库加载等系统事件,import 仅放行确定性的纯计算标准库模块。

已知边界(诚实声明):这不等同于容器级隔离——CPython 解释器自身的原生层漏洞、
纯计算资源耗尽(以超时兜底)不在钩子能防的范围内;容器/远程执行作为后续硬化路径。

S1 加固后的准确边界(2026-09-26 实测校正):

- **强制点是审计黑名单,不是内省封锁。** CPython 不允许覆盖内置类型的 `__subclasses__`
  (`object` 是 immutable type),所以 `().__class__.__mro__[1].__subclasses__()` 永远
  可达,拿得到已加载类、再经 `.__init__.__globals__` 摸到模块全局。纯 Python 无法禁掉它。
- 因此黑名单按「同一能力的所有入口」列全(进程创建 os.spawn*/os.startfile、文件改写与
  目录枚举、UDP/DNS 外发),使「摸到 `os` 模块对象」不再等于「能做事」。
- exec 前会清空本模块全局,堵掉「经 `__import__.__globals__` 白拿 os/sys/subprocess」这条路;
  并从命名空间去掉 `type`/`object`/`super`/`vars`/`globals`/`locals`/`dir`——抬高成本,
  但**不构成安全边界**。
- 仍然明确不在防护内:解释器原生层漏洞、纯计算资源耗尽(仅靠 30s 超时兜底)。

包代码契约:定义 ``def run(bars: list[dict]) -> list[float]``,输入为 OHLCV K 线
(闭 K 线口径),输出逐 bar 仓位序列(数值将被截断到 [-1, 1])。结果必须由输入
确定性推导,不得使用随机数或时钟。
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

# 包代码允许 import 的标准库模块(全部为确定性纯计算,无 I/O 面)。
ALLOWED_IMPORTS = frozenset({
    "math", "statistics", "decimal", "fractions", "itertools", "functools",
    "collections", "bisect", "operator",
})
# 无论事件名,直接阻断的系统访问面。
# S1（用户视角路线图 2026-09-26）补齐：原表只挡了 `os.system` / `os.exec` /
# `subprocess.Popen`，漏掉了它们在 Windows 上的**同族**入口。实测（真实 `_install_audit_hook`
# + `_restricted_builtins` 配置下执行包代码）`os.spawnv` / `os.startfile` 抛的是
# `FileNotFoundError` 而非 `PermissionError`——说明调用已抵达操作系统层，只是探针指向了
# 不存在的目标。换真实目标即执行成功。黑名单按「同一能力的所有入口」而非「已知的那几个」
# 来列：进程创建（os.spawn* / os.startfile）、文件改写与目录枚举、UDP/DNS 外发。
BLOCKED_EVENTS = frozenset({
    # —— 进程创建 ——
    "subprocess.Popen", "os.system", "os.exec", "os.fork", "os.kill", "os.posix_spawn",
    "os.spawn", "os.startfile", "os.startfile/1", "os.startfile/2",
    # —— 文件与目录 ——
    "open", "os.remove", "os.rename", "os.replace", "os.mkdir", "os.rmdir", "os.listdir",
    "os.scandir", "os.walk", "os.truncate", "os.chmod", "os.chown", "os.link", "os.symlink",
    # —— 网络 ——
    "socket.connect", "socket.bind", "socket.listen", "socket.socketpair", "socket.sendto",
    "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr", "socket.__new__",
    # —— 动态库 / 其它系统面 ——
    "ctypes.dlopen", "ctypes.dlsym", "ctypes.seh", "mmap", "winreg.OpenKey",
})
RUN_TIMEOUT_SECONDS = 30

# S1：内省相关的内建名也从命名空间里去掉。CPython **不允许**覆盖内置类型的
# `__subclasses__`（`object` 是 immutable type，`object.__subclasses__ = f` 抛
# TypeError），所以「让 `__subclasses__` 本身不可达」在纯 Python 里做不到——
# `().__class__.__mro__[1].__subclasses__()` 永远拿得到解释器里已加载的类。
#
# 因此本执行器的**实际强制点是审计黑名单**，不是内省封锁：即使包代码经内省链摸到 `os`
# 模块对象，`os.spawn*` / `os.startfile` / `os.remove` / `socket.*` 也一律被钩子拦下。
# 这里再去掉 `type` / `object` / `super` 只是**抬高成本**、缩小顺手可用的面，
# 不是安全边界本身。剩余已知边界见模块 docstring。
_INTROSPECTION_BUILTINS = ("type", "object", "super", "vars", "globals", "locals", "dir")


def _install_audit_hook() -> None:
    """阻断系统访问面与未放行 import。仅覆盖包代码执行期(安装于全部引导 import 之后)。

    S1：黑名单与白名单经**默认参数**捕获，不读模块全局——`_child_main` 在 exec 前会清空
    本模块的 `__dict__`（防 `__globals__` 逃逸），闭包/默认值是这里唯一还能活着的引用。
    """
    blocked = BLOCKED_EVENTS
    allowed = ALLOWED_IMPORTS

    def hook(event: str, args: tuple[Any, ...], _blocked=blocked, _allowed=allowed) -> None:
        if event in _blocked:
            raise PermissionError(f"策略包执行被阻断:{event}(受限执行器不允许该操作)")
        if event == "import":
            module = str(args[0]) if args else ""
            root = module.split(".")[0]
            if root not in _allowed:
                raise PermissionError(f"策略包执行被阻断:import {module}(仅放行纯计算标准库:{', '.join(sorted(_allowed))})")

    sys.addaudithook(hook)


def _restricted_builtins() -> dict[str, Any]:
    import builtins

    blocked = ("open", "eval", "exec", "compile", "input", "breakpoint") + _INTROSPECTION_BUILTINS
    namespace = {
        name: getattr(builtins, name)
        for name in dir(builtins)
        if name not in blocked and not name.startswith("_")
    }

    # S1：`builtins` 与白名单同样用默认参数捕获——本函数本身是包代码可达的 Python 函数，
    # 它的 `__globals__` 指向本模块 dict；清空全局后，这里若再读全局就会 NameError。
    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0,
                       _builtins=builtins, _allowed=ALLOWED_IMPORTS):
        root = str(name).split(".")[0]
        if root not in _allowed:
            raise PermissionError(
                f"策略包执行被阻断:import {name}(仅放行纯计算标准库:{', '.join(sorted(_allowed))})"
            )
        return _builtins.__import__(name, globals, locals, fromlist, level)

    namespace["__import__"] = guarded_import
    return namespace


# S1：这些名字不能留在模块全局里——包代码可经任意可达 Python 函数的 `__globals__`
# 读到本模块 dict，进而拿到 `os` / `sys` / `subprocess` 模块对象，绕开审计黑名单调用
# 进程创建。`__child_main` 在 exec 前把除 dunder 之外的名字全部清空。
_PRESERVED_GLOBALS = frozenset({
    "__name__", "__doc__", "__package__", "__loader__", "__spec__", "__file__",
    "__builtins__", "__path__", "__cached__", "__dict__", "__weakref__",
})


def _purge_module_globals() -> None:
    """清空本模块全局，防止包代码经 `__globals__` 触达 `os`/`sys`/`subprocess`。"""
    module_globals = globals()
    for name in [n for n in module_globals if n not in _PRESERVED_GLOBALS]:
        del module_globals[name]


def _child_main() -> None:  # pragma: no cover - 子进程路径,由父进程调用
    import math

    # 先把本模块后续还要用到的引用收进局部变量；清空全局后它们仍然有效。
    stdin_buffer = sys.stdin.buffer
    loads = json.loads
    dumps = json.dumps
    emit = print
    restricted = _restricted_builtins()  # 注意调用：命名空间里放的是 dict，不是函数本身

    request = loads(stdin_buffer.read().decode("utf-8"))
    code = str(request.get("code", ""))
    bars = request.get("bars")
    if not isinstance(bars, list):
        emit(dumps({"ok": False, "error": "bars 数据缺失"}))
        return
    # S1 补充：把白名单里的模块在装钩子**之前**全部导入、使其进入 sys.modules。
    # 否则包代码写 `import statistics` 会触发 `open` 去读 .py 文件，被黑名单拦下——
    # 实测 statistics / decimal / fractions / bisect 四个「白名单成员」全都因此不可用，
    # 即 `ALLOWED_IMPORTS` 名义上放行、实际拿不到。先导入即与「开放」语义对齐。
    for _allowed_name in ALLOWED_IMPORTS:
        __import__(_allowed_name)
    _install_audit_hook()
    namespace: dict[str, Any] = {"__name__": "strategy_pack", "math": math, "__builtins__": restricted}
    # S1：exec 之前清空模块全局——包代码经任意可达 Python 函数的 `__globals__`
    # 就能拿到本模块 dict，进而拿到 `os` / `sys` / `subprocess` 模块对象。
    _purge_module_globals()
    try:
        exec(code, namespace)  # noqa: S102 - 唯一执行点:已验签包代码,审计钩子已生效
        run = namespace.get("run")
        if not callable(run):
            # 保持 ValueError（不是 TypeError）：`run_pack_payload` 与端点都以
            # `except ValueError` 捕获并转成 422，改异常类型会破坏这条既有契约。
            raise ValueError(  # noqa: TRY004 - 见上：异常类型是跨层契约的一部分
                "策略包必须定义 def run(bars) -> list[float]"
            )
        positions = run(bars)
        if not isinstance(positions, list) or not all(isinstance(v, (int, float)) for v in positions):
            raise ValueError("run() 必须返回数值列表(逐 bar 仓位)")
        emit(dumps({"ok": True, "positions": [float(v) for v in positions]}))
    except PermissionError as error:
        emit(dumps({"ok": False, "error": str(error)}))
    except Exception as error:  # noqa: BLE001 - 包代码任意异常都只降级为失败结果
        emit(dumps({"ok": False, "error": f"策略包运行失败:{error}"}))


def run_pack_payload(code: str, bars: list[dict[str, Any]], *, timeout_seconds: float = RUN_TIMEOUT_SECONDS) -> dict[str, Any]:
    """父进程入口:一次性子进程执行已验签包代码,返回 {ok, positions} 或 {ok:False, error}。

    I/O 全程显式 UTF-8 字节流(子进程 -I 隔离模式会忽略编码环境变量,不能用 locale 默认值)。
    """
    request = json.dumps({"code": code, "bars": bars}, ensure_ascii=False).encode("utf-8")
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-m", "investment_steward_core.quant_pack_runner"],
            input=request,
            capture_output=True,
            timeout=timeout_seconds,
            # 非零退出码由下面的 returncode 分支自行解读并组装成结构化失败，
            # 不用 check=True 抛 CalledProcessError。
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"策略包执行超时(>{timeout_seconds:.0f}s),已强制终止"}
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
    line = (stdout.strip().splitlines() or [""])[-1]
    try:
        result = json.loads(line) if line else {"ok": False, "error": "执行器无输出"}
    except ValueError:
        result = {"ok": False, "error": "执行器输出不可解析"}
    if completed.returncode != 0 and result.get("ok") is not False:
        tail = stderr[-200:] or "无 stderr"
        result = {"ok": False, "error": f"执行器异常退出({completed.returncode}):{tail}"}
    return result


def equity_stats(bars: list[dict[str, Any]], positions: list[float]) -> dict[str, Any]:
    """仓位 → 回测统计:equity *= 1 + clamp(pos) × 次日收益(闭 K 线口径)。确定性。"""
    curve: list[dict[str, Any]] = []
    returns: list[float] = []
    equity = 1.0
    for index in range(len(bars) - 1):
        close_today = float(bars[index]["close"])
        close_next = float(bars[index + 1]["close"])
        position = max(-1.0, min(1.0, float(positions[index]))) if index < len(positions) else 0.0
        day_return = (close_next - close_today) / close_today if close_today else 0.0
        equity *= 1.0 + position * day_return
        returns.append(position * day_return)
        curve.append({
            "date": str(bars[index]["timestamp"])[:10],
            "position": round(position, 4),
            "equity": round(equity, 6),
        })
    wins = sum(1 for r in returns if r > 0)
    return {
        "equity": round(equity, 6),
        "curve": curve,
        "win_rate": round(wins / len(returns), 4) if returns else None,
        "bars": len(returns),
    }


if __name__ == "__main__":  # pragma: no cover
    _child_main()

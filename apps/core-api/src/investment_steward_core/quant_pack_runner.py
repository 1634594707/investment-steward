"""策略包受限执行器(quant_pack_runner):阶段 B 的执行环境。

信任模型(ADR-0008 修订):只有通过发布者 Ed25519 验签的策略包才会入库,执行器只对
这些已签名包运行。执行边界为双层:

1. 一次性子进程:崩溃/超时/资源残留随进程终结,父进程强制 timeout 终止;
2. CPython 审计钩子(PEP 578):在包代码运行前安装,阻断文件读写、网络、子进程、
   动态库加载等系统事件,import 仅放行确定性的纯计算标准库模块。

已知边界(诚实声明):这不等同于容器级隔离——CPython 解释器自身的原生层漏洞、
纯计算资源耗尽(以超时兜底)不在钩子能防的范围内;容器/远程执行作为后续硬化路径。

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
BLOCKED_EVENTS = frozenset({
    "open", "socket.connect", "socket.bind", "socket.listen", "socket.socketpair",
    "subprocess.Popen", "os.system", "os.exec", "os.fork", "os.kill", "os.posix_spawn",
    "ctypes.dlopen", "ctypes.dlsym", "ctypes.seh", "mmap", "winreg.OpenKey",
})
RUN_TIMEOUT_SECONDS = 30


def _install_audit_hook() -> None:
    """阻断系统访问面与未放行 import。仅覆盖包代码执行期(安装于全部引导 import 之后)。"""

    def hook(event: str, args: tuple[Any, ...]) -> None:
        if event in BLOCKED_EVENTS:
            raise PermissionError(f"策略包执行被阻断:{event}(受限执行器不允许该操作)")
        if event == "import":
            module = str(args[0]) if args else ""
            root = module.split(".")[0]
            if root not in ALLOWED_IMPORTS:
                raise PermissionError(f"策略包执行被阻断:import {module}(仅放行纯计算标准库:{', '.join(sorted(ALLOWED_IMPORTS))})")

    sys.addaudithook(hook)


def _restricted_builtins() -> dict[str, Any]:
    import builtins

    blocked = ("open", "eval", "exec", "compile", "input", "breakpoint")
    namespace = {
        name: getattr(builtins, name)
        for name in dir(builtins)
        if name not in blocked and not name.startswith("_")
    }

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002 - 签名对齐内建
        root = str(name).split(".")[0]
        if root not in ALLOWED_IMPORTS:
            raise PermissionError(
                f"策略包执行被阻断:import {name}(仅放行纯计算标准库:{', '.join(sorted(ALLOWED_IMPORTS))})"
            )
        return builtins.__import__(name, globals, locals, fromlist, level)

    namespace["__import__"] = guarded_import
    return namespace


def _child_main() -> None:  # pragma: no cover - 子进程路径,由父进程调用
    import math

    request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    code = str(request.get("code", ""))
    bars = request.get("bars")
    if not isinstance(bars, list):
        print(json.dumps({"ok": False, "error": "bars 数据缺失"}))
        return
    _install_audit_hook()
    namespace: dict[str, Any] = {"__name__": "strategy_pack", "math": math, "__builtins__": _restricted_builtins()}
    try:
        exec(code, namespace)  # noqa: S102 - 唯一执行点:已验签包代码,审计钩子已生效
        run = namespace.get("run")
        if not callable(run):
            raise ValueError("策略包必须定义 def run(bars) -> list[float]")
        positions = run(bars)
        if not isinstance(positions, list) or not all(isinstance(v, (int, float)) for v in positions):
            raise ValueError("run() 必须返回数值列表(逐 bar 仓位)")
        print(json.dumps({"ok": True, "positions": [float(v) for v in positions]}))
    except PermissionError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
    except Exception as error:  # noqa: BLE001 - 包代码任意异常都只降级为失败结果
        print(json.dumps({"ok": False, "error": f"策略包运行失败:{error}"}))


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

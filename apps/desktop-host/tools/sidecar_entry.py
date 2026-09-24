"""打包版 Python 后端统一入口（PyInstaller 冻结目标）。

一个可执行文件承载三个 sidecar，用 --mode 分发（避免三份 ~100MB 冻结产物）：

    steward-backend.exe --mode core   --host 127.0.0.1 --port N --default-data-dir D --session-token T
    steward-backend.exe --mode worker --status-file F --core-url U --session-token T
    steward-backend.exe --mode runner

语义与 dev 模式的 `python -m <package> ...` 完全一致（runpy 走包 __main__），
因此 main.ts 只需把 command 换成本 exe、args 前缀加 `--mode <name>`。
"""

from __future__ import annotations

import runpy
import sys

MODES = {
    "core": "investment_steward_core",
    "worker": "investment_steward_agent_worker",
    "runner": "investment_steward_plugin_runner",
}

USAGE = (
    "用法: steward-backend --mode {core|worker|runner} [参数...]\n"
    "  core   : --host --port --data-dir --default-data-dir --session-token\n"
    "  worker : --status-file --core-url --session-token\n"
    "  runner : 无额外参数（stdin/stdout JSON-RPC）\n"
)


def main() -> int:
    args = sys.argv[1:]
    # 两种调用形态都兼容：`--mode core ...` 与 `core ...`
    if args and args[0] == "--mode":
        args.pop(0)
    if len(args) < 1 or args[0] not in MODES:
        sys.stderr.write(USAGE)
        return 2
    mode = args.pop(0)
    sys.argv = [f"steward-{mode}", *args]
    runpy.run_module(MODES[mode], run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

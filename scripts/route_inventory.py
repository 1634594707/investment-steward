"""从**注册后的真实路由表**导出 Core 端点清单（方法 / 路径模板 / 是否要求会话鉴权）。

为什么不用源码正则：源码里 `@app.get("...")` 的写法会随重构变化（挂到 APIRouter、
拆到子模块、条件注册），按文字扫描会漏扫或误扫。这里直接实例化 `create_app()`，
读 `app.routes`，得到的就是运行时真实对外暴露的集合。

用途：
- M0 基线：记录改造前的端点全集，作为后续重构「行为等价」的对照物。
- M2/A02：作为宿主桥暴露清单的生成源，替代 desktop-host 里手写的大正则。

输出 JSON 结构：
{
  "generated_at": "...",
  "core_version": "...",
  "count": 233,
  "routes": [
    {"method": "GET", "path": "/evidence", "auth": true, "path_params": ["evidence_id"]},
    ...
  ]
}

用法：
    .venv/Scripts/python.exe scripts/route_inventory.py --out docs/architecture/route-inventory.json
"""
from __future__ import annotations

import argparse
import json
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# FastAPI 为每个 GET 自动补 HEAD；OPTIONS 由 CORS 中间件处理。二者都不是业务端点，
# 记录进来会污染「路由清单一致」的比对，故排除。
NON_BUSINESS_METHODS = {"HEAD", "OPTIONS"}


def _iter_api_routes(app: object) -> list[tuple[object, str]]:
    """展开真实路由表，返回 (route, 有效路径)。

    **A06 踩坑（2026-09-25）**：本仓库使用的 FastAPI 对 `include_router` 采用惰性挂载——
    调用后 `app.routes` 里放的是 `_IncludedRouter` 代理（持有 `original_router` 与
    `include_context.prefix`），真正的 APIRoute 要等请求匹配时才展开。按域拆解后若不显式
    展开，子路由会**整批从清单里消失且不报错**（只是漏扫），表现为「拆出去的路由凭空不见」。
    """
    resolved: list[tuple[object, str]] = []

    def walk(routes: object, prefix: str = "") -> None:
        for route in routes:  # type: ignore[union-attr]
            inner = getattr(route, "original_router", None)
            if inner is not None:
                context = getattr(route, "include_context", None)
                walk(getattr(inner, "routes", []), prefix + str(getattr(context, "prefix", "") or ""))
                continue
            path = getattr(route, "path", None)
            if path is None:
                continue
            resolved.append((route, prefix + str(path)))

    walk(getattr(app, "routes", []))
    return resolved


def _business_methods(route: object) -> list[str]:
    methods = getattr(route, "methods", None)
    if not methods:
        return []
    # FastAPI 为每个 GET 自动补 HEAD；OPTIONS 由 CORS 中间件处理。二者都不是业务端点，
    # 记录进来会污染「路由清单一致」的比对，故排除。
    return sorted(set(methods) - NON_BUSINESS_METHODS)


def _requires_session(route: object) -> bool:
    """沿 FastAPI 的依赖树查找会话守卫。

    真实鉴权由 `Depends(_require_session)` 提供；它可能直接挂在路由函数签名上，
    也可能挂在子依赖里。这里递归遍历 `dependant.dependencies`，避免只看第一层。
    """

    def walk(dependant: object) -> bool:
        call = getattr(dependant, "call", None)
        name = getattr(call, "__name__", "")
        if name in {"_require_session", "require_session"}:
            return True
        for sub in getattr(dependant, "dependencies", []) or []:
            if walk(sub):
                return True
        return False

    return walk(getattr(route, "dependant", None))


def _path_params(path: str) -> list[str]:
    return re.findall(r"\{([^}]+)\}", path)


# ---------------------------------------------------------------------------
# 桌面桥暴露范围（M0 基线用；M2/A02 会被生成的暴露清单取代）
# ---------------------------------------------------------------------------

_MAIN_TS = REPO_ROOT / "apps" / "desktop-host" / "src" / "main.ts"


def _extract_host_regex() -> re.Pattern[str]:
    """从 desktop-host 源码提取桥白名单正则。

    切片规则与 `scripts/check-bridge-coverage.mjs` 完全一致：以 `$/` 作为正则字面量
    结束标记（`$` 锚点必须纳入切片，否则退化成前缀匹配，多一段的路径会被误判放行）。
    这套提取方式依赖源码写法，正是 A02 要消除的重复维护点——此处仅为基线取证。
    """
    src = _MAIN_TS.read_text(encoding="utf-8")
    line = next((l for l in src.split("\n") if "const corePath = /" in l), None)
    if line is None:
        raise RuntimeError("未在 main.ts 找到 corePath 白名单正则")
    marker = "const corePath = "
    literal_start = line.index("/", line.index(marker) + len(marker))
    literal_end = line.index("$/")
    if literal_end <= literal_start:
        raise RuntimeError("无法定位 corePath 正则结尾")
    return re.compile(line[literal_start + 1 : literal_end + 1])


def _bridge_sample(name: str) -> str:
    """把路径模板里的参数名换成样例值——宿主桥匹配的是具体请求路径。"""
    if name.startswith("plugin_id"):
        return "official.cn-market-data"
    if name.startswith("symbol"):
        return "600519"
    if name.startswith("job_id"):
        return "job-1"
    return "abc123"


def annotate_bridge_exposure(inventory: dict[str, object]) -> None:
    """就地给每条路由补 `bridge_exposed` 字段（当前宿主正则是否放行）。"""
    bridge = _extract_host_regex()
    for item in inventory["routes"]:  # type: ignore[index]
        concrete = re.sub(
            r"\{([^}]+)\}",
            lambda m: _bridge_sample(m.group(1)),
            str(item["path"]),
        )
        item["bridge_exposed"] = bool(bridge.search(concrete))


def build_inventory() -> dict[str, object]:
    from investment_steward_core import __version__
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings

    # ignore_cleanup_errors：CoreState 持有的 sqlite 连接在 app 释放前仍打开，
    # Windows 下临时目录删除会撞 WinError 32。清理失败不影响本脚本的产物。
    with tempfile.TemporaryDirectory(prefix="route-inventory-", ignore_cleanup_errors=True) as tmp:
        app = create_app(CoreSettings(session_token="inventory-token", data_dir=Path(tmp)))
        entries: list[dict[str, object]] = []
        for route, path in _iter_api_routes(app):
            methods = _business_methods(route)
            if not methods:
                continue
            for method in methods:
                entries.append(
                    {
                        "method": method,
                        "path": path,
                        "auth": _requires_session(route),
                        "path_params": _path_params(path),
                    }
                )
    entries.sort(key=lambda item: (str(item["path"]), str(item["method"])))
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "core_version": __version__,
        "count": len(entries),
        "routes": entries,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="导出 Core 运行时路由清单")
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "docs" / "architecture" / "route-inventory.json",
    )
    parser.add_argument(
        "--with-bridge",
        action="store_true",
        help="额外标注每条路由当前是否被桌面宿主桥放行（依赖 main.ts 源码写法，M2 后废弃）",
    )
    args = parser.parse_args()
    inventory = build_inventory()
    if args.with_bridge:
        annotate_bridge_exposure(inventory)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    unauth = [r for r in inventory["routes"] if not r["auth"]]
    print(f"已导出 {inventory['count']} 条路由 → {args.out}")
    print(f"其中无需会话鉴权：{len(unauth)} 条")
    for item in unauth:
        print(f"  {item['method']} {item['path']}")
    if args.with_bridge:
        blocked = [r for r in inventory["routes"] if not r.get("bridge_exposed")]
        print(f"未被宿主桥放行：{len(blocked)} 条")
        for item in blocked:
            print(f"  {item['method']} {item['path']}")


if __name__ == "__main__":
    main()

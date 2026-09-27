"""契约生成引擎（A03，架构改进路线图 2026-09-25）。

旧实现的问题：`MODELS` 字典硬编码 4 个模型；TypeScript 侧另有一份手写声明；
Python 测试里再抄一份枚举字面量。三份副本之间没有任何机制保证一致——实测已经漂移出
`DecisionEntry.inaction_reason` 缺失与 `NotificationTriageReport.schema_version` 多余两处。

现在：
- **模型清单来自真实路由表**：实例化 `create_app()` 后遍历 `route.response_model`，
  谁真的会被返回就导出谁，不再人工维护清单；
- **发布 JSON Schema** 到 `schemas/*.schema.json`（类名 kebab-case，与既有 4 份文件同名兼容）；
- **生成 TypeScript 声明**到 `packages/domain-contracts/src/generated/contracts.ts`；
- `--check`：重新生成并与已提交产物逐字节比对，有差异退出码 1。

用法：
    .venv/Scripts/python.exe scripts/export_contracts.py           # 生成/更新
    .venv/Scripts/python.exe scripts/export_contracts.py --check   # 只校验（CI）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT / "apps" / "core-api" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

SCHEMA_DIR = ROOT / "schemas"
TS_OUTPUT = ROOT / "packages" / "domain-contracts" / "src" / "generated" / "contracts.ts"
INDEX_OUTPUT = SCHEMA_DIR / "contracts.index.json"

SCHEMA_BASE = "https://investment-steward.local/schemas"


# ---------------------------------------------------------------------------
# 1. 从真实路由表发现公共模型
# ---------------------------------------------------------------------------


def _leaf_models(annotation: Any, out: set[type]) -> None:
    """拆 `list[X]` / `X | None` / `dict[str, X]`，收集其中的 Pydantic 模型。"""
    from pydantic import BaseModel

    if annotation is None:
        return
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        out.add(annotation)
        return
    for arg in getattr(annotation, "__args__", ()):
        _leaf_models(arg, out)


def _iter_routes(routes: Any, prefix: str = "") -> list[tuple[Any, str]]:
    """展开真实路由表，返回 (route, 有效路径)。

    **A06 踩坑（2026-09-25）**：本仓库使用的 FastAPI 对 `include_router` 采用惰性挂载——
    `app.routes` 里只是 `_IncludedRouter` 代理，真正的 APIRoute 要等请求匹配才展开。
    按域拆解后若照旧直接遍历 `app.routes`，**被拆出去的路由的 `response_model` 会整批
    从模型清单里消失且不报错**（模型数 36 → 31），契约生成悄悄漏掉 5 个模型。
    `scripts/route_inventory.py` 有同一个坑的同一套修法。
    """
    resolved: list[tuple[Any, str]] = []
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            context = getattr(route, "include_context", None)
            resolved.extend(
                _iter_routes(
                    getattr(inner, "routes", []),
                    prefix + str(getattr(context, "prefix", "") or ""),
                )
            )
            continue
        path = getattr(route, "path", None)
        if path is None:
            continue
        resolved.append((route, prefix + str(path)))
    return resolved


def _response_models(app: Any) -> list[tuple[Any, Any]]:
    """遍历真实路由，产出 (路由, 叶子模型集合)。

    `response_model` 不一定是类：`list[X]`、`X | None`、`dict[str, X]` 都是合法声明，
    所以这里对注解递归下钻，而不是只认 `issubclass(..., BaseModel)`。
    字符串前向引用无法在此解析，直接跳过而不是猜。
    """

    pairs: list[tuple[Any, set[type]]] = []
    for route, _path in _iter_routes(getattr(app, "routes", [])):
        model = getattr(route, "response_model", None)
        if model is None or isinstance(model, str):
            continue
        leaves: set[type] = set()
        _leaf_models(model, leaves)
        if leaves:
            pairs.append((route, leaves))
    return pairs


def public_models() -> dict[str, type]:
    """返回 {类名: 模型类}——只包含真正出现在某个路由 `response_model` 里的模型。"""
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings

    with tempfile.TemporaryDirectory(prefix="contracts-", ignore_cleanup_errors=True) as tmp:
        app = create_app(CoreSettings(session_token="contracts-token", data_dir=Path(tmp)))
        found: dict[str, type] = {}
        for _route, leaves in _response_models(app):
            for leaf in leaves:
                found[leaf.__name__] = leaf
    return found


def route_index(models: dict[str, type]) -> dict[str, list[str]]:
    """模型 → 返回它的路由清单（用于索引文件，便于人查「这个模型谁在用」）。"""
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings

    index: dict[str, list[str]] = {name: [] for name in models}
    with tempfile.TemporaryDirectory(prefix="contracts-", ignore_cleanup_errors=True) as tmp:
        app = create_app(CoreSettings(session_token="contracts-token", data_dir=Path(tmp)))
        for route, leaves in _response_models(app):
            methods = sorted(set(getattr(route, "methods", set())) - {"HEAD", "OPTIONS"})
            for leaf in leaves:
                for method in methods:
                    index.setdefault(leaf.__name__, []).append(f"{method} {route.path}")
    for key, value in index.items():
        index[key] = sorted(set(value))
    return index


def kebab(name: str) -> str:
    """`InvestmentPolicyVersion` → `investment-policy-version`（与既有 schema 文件名一致）。

    分两步是为了处理连续大写（首字母缩写）：`UICard` 直接按「大写前插连字符」会得到
    `u-i-card`，正确结果是 `ui-card`。
    """
    step1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1-\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", step1).lower()


# ---------------------------------------------------------------------------
# 2. 渲染 JSON Schema
# ---------------------------------------------------------------------------


def render_schemas(models: dict[str, type]) -> dict[str, str]:
    """{相对路径: 文件内容}。逐模型发布 schema，供跨语言消费与人工审阅。"""
    out: dict[str, str] = {}
    for name, model in models.items():
        schema = model.model_json_schema(mode="serialization")
        schema["$id"] = f"{SCHEMA_BASE}/{kebab(name)}/1.0"
        out[f"{kebab(name)}.schema.json"] = json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
    return out


def render_index(models: dict[str, type], index: dict[str, list[str]]) -> str:
    payload = {
        "generated_from": "apps/core-api 运行时路由表（route.response_model）",
        "schema_base": SCHEMA_BASE,
        "models": {
            name: {"schema": f"{kebab(name)}.schema.json", "routes": index.get(name, [])}
            for name in sorted(models)
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


# ---------------------------------------------------------------------------
# 3. 渲染 TypeScript 声明
# ---------------------------------------------------------------------------


def _ts_type(schema: Any) -> str:
    if not isinstance(schema, dict):
        return "unknown"
    if "$ref" in schema:
        return str(schema["$ref"]).rsplit("/", 1)[-1]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            members = [_ts_type(item) for item in schema[key]]
            return " | ".join(dict.fromkeys(members))
    if "allOf" in schema:
        members = [_ts_type(item) for item in schema["allOf"]]
        return " & ".join(dict.fromkeys(members))
    if "enum" in schema:
        return " | ".join(json.dumps(item, ensure_ascii=False) for item in schema["enum"])
    if "const" in schema:
        return json.dumps(schema["const"], ensure_ascii=False)
    kind = schema.get("type")
    if kind == "array":
        inner = _ts_type(schema.get("items", {}))
        return f"({inner})[]" if " | " in inner else f"{inner}[]"
    if kind == "object" or "properties" in schema:
        properties = schema.get("properties")
        if properties:
            required = set(schema.get("required", []))
            body = "; ".join(
                f"{field}{'' if field in required else '?'}: {_ts_type(sub)}"
                for field, sub in properties.items()
            )
            return f"{{ {body} }}"
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            return f"Record<string, {_ts_type(extra)}>"
        return "Record<string, unknown>"
    if kind in {"integer", "number"}:
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "null":
        return "null"
    if kind == "string":
        return "string"
    return "unknown"


def _collect_defs(models: dict[str, type]) -> dict[str, dict]:
    """把所有模型的顶层 schema 与它们的 `$defs` 合并成一张定义表。"""
    defs: dict[str, dict] = {}
    for name, model in models.items():
        schema = model.model_json_schema(mode="serialization")
        defs[name] = {key: value for key, value in schema.items() if key != "$defs"}
        for def_name, def_schema in schema.get("$defs", {}).items():
            defs.setdefault(def_name, def_schema)
    return defs


def render_typescript(models: dict[str, type]) -> str:
    defs = _collect_defs(models)
    lines = [
        "/* eslint-disable */",
        "// 本文件由 scripts/export_contracts.py 生成，请勿手工修改。",
        "//",
        "// 生成源：apps/core-api 的 Pydantic 模型 + 真实路由 response_model 声明。",
        "// 校验：scripts/export_contracts.py --check（差异即失败），以及",
        "//       apps/core-api/tests/test_a03_contract_parity.py 与手写声明的逐字段比对。",
        "//",
        "// 注意：本模块与 src/index.ts 目前并存——index.ts 是待迁移的手写副本，",
        "// 两者的一致性由上面那个测试强制，不存在「生成物只是摆设」的情况。",
        "",
    ]
    for name in sorted(defs):
        schema = defs[name]
        enum_values = schema.get("enum")
        if isinstance(enum_values, list) and schema.get("type") == "string":
            literals = " | ".join(json.dumps(item, ensure_ascii=False) for item in enum_values)
            lines.append(f"export type {name} = {literals};")
            continue
        properties = schema.get("properties")
        if not properties:
            lines.append(f"export type {name} = {_ts_type(schema)};")
            continue
        required = set(schema.get("required", []))
        lines.append(f"export interface {name} {{")
        for field, sub in properties.items():
            optional = "" if field in required else "?"
            lines.append(f"  {field}{optional}: {_ts_type(sub)};")
        lines.append("}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 4. 生成 / 校验
# ---------------------------------------------------------------------------


def build_outputs() -> dict[Path, str]:
    models = public_models()
    index = route_index(models)
    outputs: dict[Path, str] = {}
    for name, content in render_schemas(models).items():
        outputs[SCHEMA_DIR / name] = content
    outputs[INDEX_OUTPUT] = render_index(models, index)
    outputs[TS_OUTPUT] = render_typescript(models)
    return outputs


def check(outputs: dict[Path, str] | None = None) -> list[str]:
    """返回漂移说明列表；空列表表示生成物与当前源码一致。"""
    outputs = outputs if outputs is not None else build_outputs()
    drift: list[str] = []
    for path, expected in outputs.items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current is None:
            drift.append(f"缺少生成物：{path.relative_to(ROOT)}")
        elif current != expected:
            drift.append(f"生成物已过期：{path.relative_to(ROOT)}")
    return drift


def main() -> None:
    parser = argparse.ArgumentParser(description="从真实路由响应声明生成契约产物")
    parser.add_argument("--check", action="store_true", help="只校验，不写盘；有漂移则退出码 1")
    args = parser.parse_args()

    outputs = build_outputs()
    if args.check:
        drift = check(outputs)
        if drift:
            print("契约产物与当前源码不一致：")
            for item in drift:
                print(f"  - {item}")
            print("\n请运行：.venv/Scripts/python.exe scripts/export_contracts.py")
            raise SystemExit(1)
        print(f"契约产物一致：{len(outputs)} 个文件（模型 {len(public_models())} 个）")
        return

    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    print(f"已生成 {len(outputs)} 个契约产物（模型 {len(public_models())} 个）")


if __name__ == "__main__":
    main()

"""A03（架构改进路线图 2026-09-25）：跨语言契约对账。

改造前的问题：`scripts/export_contracts.py` 只硬编码导出 4 个模型；
`packages/domain-contracts/src/index.ts` 另有一份手写声明；
`test_g0_contract_parity.py` 把 TypeScript 的枚举与字段**手抄**进 Python 测试。
三份副本之间没有任何机制保证一致，所以「TS 改了、Python 测试副本没改」这种漂移
现有测试根本识别不出来。

本文件把对账基线换成**真实 TypeScript 源文件**（含生成物与手写声明），并覆盖四个维度：

1. **生成物新鲜度**——已提交的 schema / 生成 TS 必须与当前 Pydantic 源码重新生成的
   结果逐字节一致；改了公共字段而忘记重新生成 → 直接失败；
2. **字段名**——手写声明与 Pydantic 模型的字段集合必须完全相等（增删都拦）；
3. **枚举取值**——两侧枚举成员必须完全相等；
4. **必填 / 可空 / 嵌套结构**——按生成物（即 Pydantic 的序列化 schema）逐个字段核对，
   不只看字段名。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

import export_contracts

INDEX_TS = ROOT / "packages" / "domain-contracts" / "src" / "index.ts"
GENERATED_TS = ROOT / "packages" / "domain-contracts" / "src" / "generated" / "contracts.ts"


# ---------------------------------------------------------------------------
# 极简 TS 声明解析：只覆盖本仓库实际使用的写法（顶层 interface / 顶层 type 别名）
# ---------------------------------------------------------------------------


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", text)


def _body_of(text: str, start: int) -> str:
    """从 `{` 的下标开始，按花括号配平取出接口体。"""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index]
    raise AssertionError("接口体未闭合")


def parse_interfaces(path: Path) -> dict[str, dict[str, str]]:
    """{接口名: {字段名: 类型文本}}；嵌套对象/泛型里的字段不会被误收（只看顶层）。"""
    text = _strip_comments(path.read_text(encoding="utf-8"))
    found: dict[str, dict[str, str]] = {}
    for match in re.finditer(r"export interface (\w+)[^{]*\{", text):
        body = _body_of(text, match.end() - 1)
        # 去掉嵌套花括号内容，避免把内联对象类型的键当顶层字段。
        flat = re.sub(r"\{[^{}]*\}", "{}", body)
        fields: dict[str, str] = {}
        for line in flat.split(";"):
            field = re.match(r"\s*([A-Za-z_]\w*)(\?)?\s*:\s*(.+)", line, flags=re.DOTALL)
            if field:
                fields[field.group(1)] = field.group(3).strip()
        found[match.group(1)] = fields
    return found


def parse_union_types(path: Path) -> dict[str, list[str]]:
    """{类型别名: 字面量取值列表}——只收纯字符串字面量联合（枚举）。"""
    text = _strip_comments(path.read_text(encoding="utf-8"))
    found: dict[str, list[str]] = {}
    for match in re.finditer(r"export type (\w+)\s*=\s*([^;]+);", text):
        raw = match.group(2)
        literals = re.findall(r'"([^"]*)"', raw)
        # 去掉注释残留与空白项后，必须「整段就是字面量联合」才算枚举。
        residue = re.sub(r'"[^"]*"', "", raw)
        residue = re.sub(r"[\s|]", "", residue)
        if literals and not residue:
            found[match.group(1)] = literals
    return found


# ---------------------------------------------------------------------------
# 1. 生成物新鲜度
# ---------------------------------------------------------------------------


def test_generated_contracts_are_up_to_date():
    """已提交的 schema 与生成 TS 必须等于重新生成的结果（漂移即失败）。"""
    drift = export_contracts.check()
    assert drift == [], (
        "契约产物已过期，说明有公共字段改动未重新生成：\n  "
        + "\n  ".join(drift)
        + "\n运行：.venv/Scripts/python.exe scripts/export_contracts.py"
    )


def test_generated_typescript_is_reachable_and_non_trivial():
    """生成物必须真的存在且覆盖模型（防止把「生成」退化成空文件）。"""
    assert GENERATED_TS.exists(), "生成 TS 不存在"
    interfaces = parse_interfaces(GENERATED_TS)
    models = export_contracts.public_models()
    missing = sorted(set(models) - set(interfaces))
    assert not missing, f"以下模型未生成 TS 声明：{missing}"


def test_modified_public_field_fails_drift_check(monkeypatch):
    """A03 验收：改一个真实公共字段，差异检查必须失败；恢复后通过。

    做法：用 `create_model` 造一个**同名的** `Evidence` 副本并多出一个字段（等价于
    把 `summary` 改名/加字段后的真实漂移），只替换 `public_models()` 的返回值——
    不动工作树、不动已导入的类本身，因此「失败」只能来自字段改动。
    """
    from investment_steward_core.domain import Evidence
    from pydantic import create_model

    baseline = export_contracts.render_typescript(export_contracts.public_models())
    assert "  summary:" in baseline, "基线里应能定位到 Evidence.summary"

    drifted = create_model(
        "Evidence",
        __base__=Evidence,
        summary_renamed_for_drift_test=(str, ...),
    )
    models = export_contracts.public_models()
    models["Evidence"] = drifted
    monkeypatch.setattr(export_contracts, "public_models", lambda: dict(models))

    mutated = export_contracts.render_typescript(export_contracts.public_models())
    assert mutated != baseline, "公共字段变化后生成物必须变化，否则差异检查形同虚设"
    assert "summary_renamed_for_drift_test" in mutated

    drift = export_contracts.check()
    assert any("contracts.ts" in item for item in drift), f"差异检查未识别生成物过期：{drift}"

    monkeypatch.undo()
    # 恢复后必须重新通过（证明失败来自字段改动本身，而不是测试环境残留）。
    assert export_contracts.check() == []


# ---------------------------------------------------------------------------
# 2~4. 手写声明 vs Pydantic：字段名 / 枚举 / 必填·可空·嵌套
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ts_interfaces() -> dict[str, dict[str, str]]:
    return parse_interfaces(INDEX_TS)


@pytest.fixture(scope="module")
def models() -> dict[str, type]:
    return export_contracts.public_models()


def test_handwritten_field_names_match_python(ts_interfaces, models):
    """两侧同名的接口，字段集合必须完全相等。"""
    shared = sorted(set(ts_interfaces) & set(models))
    assert len(shared) >= 25, f"可对账的同名接口过少（{len(shared)}），疑似解析器失效"
    problems = []
    for name in shared:
        py_fields = set(models[name].model_fields)
        ts_fields = set(ts_interfaces[name])
        if py_fields != ts_fields:
            problems.append(
                f"{name}: 仅 Python={sorted(py_fields - ts_fields)} 仅 TS={sorted(ts_fields - py_fields)}"
            )
    assert not problems, "手写 TS 与 Pydantic 字段漂移：\n  " + "\n  ".join(problems)


def test_handwritten_enums_match_python():
    """枚举取值必须完全相等（正漂移与负漂移都拦）。

    基线取自**生成物里真实存在的枚举**，Python 侧按同名从 domain 包取，因此不存在
    「手抄一份枚举进测试」的问题——两侧都是从各自源码读出来的。
    """
    from investment_steward_core import domain

    ts_enums = parse_union_types(GENERATED_TS)
    ts_enums.update(parse_union_types(INDEX_TS))
    problems = []
    checked = 0
    for name, values in sorted(ts_enums.items()):
        py_enum = getattr(domain, name, None)
        if py_enum is None or not hasattr(py_enum, "__members__"):
            continue
        checked += 1
        py_values = [member.value for member in py_enum]
        if sorted(py_values) != sorted(values):
            problems.append(f"{name}: Python={sorted(py_values)} TS={sorted(values)}")
    assert checked >= 4, f"对账到的枚举过少（{checked}）"
    assert not problems, "枚举取值漂移：\n  " + "\n  ".join(problems)


def _schema_props(model: type) -> tuple[dict, set[str]]:
    schema = model.model_json_schema(mode="serialization")
    return schema.get("properties", {}), set(schema.get("required", []))


def _allows_null(schema: object) -> bool:
    if not isinstance(schema, dict):
        return False
    if schema.get("type") == "null":
        return True
    for key in ("anyOf", "oneOf"):
        if any(_allows_null(item) for item in schema.get(key, [])):
            return True
    return False


def _ref_names(schema: object) -> set[str]:
    """取出该字段引用的具名类型（含数组元素、联合分支）。"""
    if not isinstance(schema, dict):
        return set()
    names: set[str] = set()
    if "$ref" in schema:
        names.add(str(schema["$ref"]).rsplit("/", 1)[-1])
    for key in ("anyOf", "oneOf", "allOf"):
        for item in schema.get(key, []):
            names |= _ref_names(item)
    if "items" in schema:
        names |= _ref_names(schema["items"])
    return names


def test_generated_typescript_preserves_required_nullable_and_nesting(models):
    """生成物必须逐字段保住必填、可空与嵌套结构——不能只保字段名。"""
    generated = parse_interfaces(GENERATED_TS)
    # 具名类型既有 interface 也有 type 别名（枚举），两者都算「引用了某个具名类型」。
    ts_ref_names = (
        set(generated)
        | set(parse_interfaces(INDEX_TS))
        | set(parse_union_types(GENERATED_TS))
        | set(parse_union_types(INDEX_TS))
    )
    problems = []
    for name, model in sorted(models.items()):
        assert name in generated, f"{name} 未出现在生成物里"
        properties, required = _schema_props(model)
        ts_fields = generated[name]
        for field, sub in properties.items():
            ts_type = ts_fields.get(field)
            if ts_type is None:
                problems.append(f"{name}.{field}: 生成物缺该字段")
                continue
            # 必填：Pydantic 序列化 schema 的 required 必须与 TS 的可选标记一致。
            ts_optional = f"{field}" not in ts_fields or _is_optional(generated, name, field)
            if (field not in required) != ts_optional:
                problems.append(
                    f"{name}.{field}: 必填不一致（Python required={field in required}，TS 可选={ts_optional}）"
                )
            # 可空：Python 允许 None ⇔ TS 类型含 null。
            if _allows_null(sub) != ("null" in ts_type):
                problems.append(f"{name}.{field}: 可空不一致（Python={_allows_null(sub)}，TS={ts_type}）")
            # 嵌套：引用的具名类型集合必须相等。
            py_refs = _ref_names(sub)
            ts_refs = {ref for ref in re.findall(r"[A-Za-z_]\w*", ts_type) if ref in ts_ref_names}
            if py_refs != ts_refs:
                problems.append(
                    f"{name}.{field}: 嵌套引用不一致（Python={sorted(py_refs)}，TS={sorted(ts_refs)}）"
                )
    assert not problems, "生成物未完整保留契约信息：\n  " + "\n  ".join(problems[:40])


def _is_optional(generated: dict[str, dict[str, str]], name: str, field: str) -> bool:
    """生成物里以 `?` 标记可选的字段——从源码文本重新读一次，避免类型文本误判。"""
    text = _strip_comments(GENERATED_TS.read_text(encoding="utf-8"))
    match = re.search(rf"export interface {name}[^{{]*\{{", text)
    if match is None:
        return False
    body = _body_of(text, match.end() - 1)
    return re.search(rf"^\s*{re.escape(field)}\?\s*:", body, flags=re.MULTILINE) is not None


def test_published_schemas_match_models(models):
    """发布的 JSON Schema 必须与模型当前定义一致（逐模型比对关键片段）。"""
    for name, model in models.items():
        path = export_contracts.SCHEMA_DIR / f"{export_contracts.kebab(name)}.schema.json"
        assert path.exists(), f"缺少 schema：{path.name}"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["$id"].endswith(f"/{export_contracts.kebab(name)}/1.0")
        properties, required = _schema_props(model)
        assert set(payload.get("properties", {})) == set(properties), f"{name} schema 字段漂移"
        assert set(payload.get("required", [])) == required, f"{name} schema 必填漂移"

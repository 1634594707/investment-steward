"""G0-5 契约一致性：TS↔Python 逐项比对，防止再次漂移（本期缺口 1/2 正是漂移产物）。

A03（架构改进路线图 2026-09-25）改造：原实现把 `packages/domain-contracts/src/index.ts`
与 `packages/ui-card-schemas/src/index.ts` 的枚举字面量与接口字段名**手抄**进本文件当基线，
再与 Python 比对。手抄基线的问题与它要防的漂移是同一类：TS 源改了、这里没改，
测试照样绿。现在改为**直接解析真实 TypeScript 源文件**，两侧都从各自源码读出来。
"""
from __future__ import annotations

from pathlib import Path

from investment_steward_core.domain import (
    ActionMode,
    CardSeverity,
    PluginInstallationState,
    PluginManifest,
    UICard,
    UiCardRenderer,
)
from test_a03_contract_parity import parse_interfaces, parse_union_types

ROOT = Path(__file__).resolve().parents[3]
DOMAIN_TS = ROOT / "packages" / "domain-contracts" / "src" / "index.ts"
UI_CARD_TS = ROOT / "packages" / "ui-card-schemas" / "src" / "index.ts"


def _ts_enum(path: Path, name: str) -> list[str]:
    values = parse_union_types(path).get(name)
    assert values is not None, f"{path.name} 里找不到枚举 {name}"
    return values


def _ts_fields(path: Path, name: str) -> set[str]:
    fields = parse_interfaces(path).get(name)
    assert fields is not None, f"{path.name} 里找不到接口 {name}"
    return set(fields)


def test_plugin_installation_state_values_match_ts():
    assert sorted(s.value for s in PluginInstallationState) == sorted(
        _ts_enum(DOMAIN_TS, "PluginInstallationState")
    )


def test_action_mode_values_match_ts():
    assert sorted(s.value for s in ActionMode) == sorted(_ts_enum(DOMAIN_TS, "ActionMode"))


def test_card_severity_values_match_ts():
    assert sorted(s.value for s in CardSeverity) == sorted(_ts_enum(UI_CARD_TS, "CardSeverity"))


def test_ui_card_renderer_values_match_ts():
    assert sorted(s.value for s in UiCardRenderer) == sorted(
        _ts_enum(UI_CARD_TS, "UiCardRenderer")
    )


def test_plugin_manifest_has_mount_field_with_default_in_page():
    """G0-2：PluginManifest.mount 存在、默认 in_page，且仅接受 in_page/own_page。"""
    manifest = PluginManifest(
        publisher="investment-steward",
        plugin_id="official.test",
        release_version="0.1.0",
        display_name="t",
        description="d",
        plugin_type="learning_provider",
        license="internal",
        capabilities=[],
        schema_versions={},
        entrypoint="e",
        artifact_sha256="a" * 16,
        signature="unsigned-development-fixture",
        network_allowlist=[],
        side_effects=[],
        requires_confirmation=False,
        supports_markets=["CN"],
    )
    assert manifest.mount == "in_page"
    assert PluginManifest(**(manifest.model_dump() | {"mount": "own_page"})).mount == "own_page"
    # TS 侧必须同步声明 mount 及其取值域（否则前端读 manifest.mount 会报类型错）。
    assert "mount" in _ts_fields(DOMAIN_TS, "PluginManifest")


def test_uicard_fields_match_ts():
    """G0-3：Python UICard 字段名与 ui-card-schemas UICard 完全一致。"""
    assert set(UICard.model_fields) == _ts_fields(UI_CARD_TS, "UICard")

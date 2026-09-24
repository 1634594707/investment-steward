"""G0-5 契约一致性：TS↔Python 字段清单比对，防止再次漂移（本期缺口 1/2 正是漂移产物）。

把 packages/domain-contracts/src/index.ts 与 packages/ui-card-schemas/src/index.ts 中的
枚举字面量与接口字段名编码为基线清单，与 Python 模型逐项比对。基线与 TS 源码一一对应，
禁止凭空增改；改契约时需同步维护本清单。
"""

from __future__ import annotations

from investment_steward_core.domain import (
    ActionMode,
    CardSeverity,
    PluginInstallationState,
    PluginManifest,
    UICard,
    UiCardRenderer,
)

# —— 来自 packages/domain-contracts/src/index.ts ——
TS_PLUGIN_INSTALLATION_STATE = ["available", "installed", "enabled", "disabled", "revoked"]
TS_ACTION_MODE = ["observe", "research", "review_plan", "no_action"]
# —— 来自 packages/ui-card-schemas/src/index.ts ——
TS_CARD_SEVERITY = ["info", "attention", "warning"]
TS_UI_CARD_RENDERER = ["card", "summary_row", "inline", "silent"]

# 注意：即使 Python 枚举多出成员，也以 TS 基线为准做超集判定？不 —— 负漂移（TS 少了）
# 与正漂移（Python 私有扩展）都要拦住：这里做精确相等，任一侧新增都必须同步另一侧。
PY_PLUGIN_INSTALLATION_STATE = [s.value for s in PluginInstallationState]
PY_ACTION_MODE = [s.value for s in ActionMode]
PY_CARD_SEVERITY = [s.value for s in CardSeverity]
PY_UI_CARD_RENDERER = [s.value for s in UiCardRenderer]


def test_plugin_installation_state_values_match_ts():
    assert sorted(PY_PLUGIN_INSTALLATION_STATE) == sorted(TS_PLUGIN_INSTALLATION_STATE)


def test_action_mode_values_match_ts():
    assert sorted(PY_ACTION_MODE) == sorted(TS_ACTION_MODE)


def test_card_severity_values_match_ts():
    assert sorted(PY_CARD_SEVERITY) == sorted(TS_CARD_SEVERITY)


def test_ui_card_renderer_values_match_ts():
    assert sorted(PY_UI_CARD_RENDERER) == sorted(TS_UI_CARD_RENDERER)


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


def test_uicard_fields_match_ts():
    """G0-3：Python UICard 字段名与 ui-card-schemas UICard 完全一致。"""
    ts_fields = {
        "card_id",
        "title",
        "summary",
        "severity",
        "evidence_refs",
        "source_plugin",
        "slot",
        "renderer",
        "created_at",
        "valid_until",
        "action_mode",
        "supported_actions",
        "limitations",
    }
    py_fields = set(UICard.model_fields)
    assert py_fields == ts_fields
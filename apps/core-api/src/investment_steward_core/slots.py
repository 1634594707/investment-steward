"""插槽注册表与仲裁引擎（G1）。

- `DEFINED_SLOTS`：设计稿 SLOTS 表 9 槽的编码化（page 为前端 slotPageOf 映射：
  today / investment / research / review / settings / library / macro）。
- `compute_slot_occupancy`：按已启用插件的声明重算每槽 targeted/used/cap/queued；
  `used >= cap` 时溢出进入 queued（=「更多 N 项」折叠区数据源）。
- `resolve_renderer`：按插槽级别分发渲染原语（L3→card / L2→summary_row / L1→inline /
  L0→silent），对应 ui-card-schemas `UiCardRenderer`。
"""

from __future__ import annotations

from investment_steward_core.config import CoreSettings

# 独立应用挂载标识（对齐 PluginManifest.mount）
MOUNT_OWN_PAGE = "own_page"


# slot: 插槽 id；page: 输出页面路由（与 base-shell slotPageOf 一致）；cap: 容量；level: 最高呈现级别。
DEFINED_SLOTS: tuple[dict[str, str | int], ...] = (
    {"slot": "today.brief", "page": "today", "cap": 3, "level": "L3"},
    {"slot": "today.learning", "page": "today", "cap": 2, "level": "L2"},
    {"slot": "invest.market_view", "page": "investment", "cap": 1, "level": "L3"},
    {"slot": "invest.thesis", "page": "investment", "cap": 2, "level": "L2"},
    {"slot": "research.board", "page": "research", "cap": 3, "level": "L3"},
    {"slot": "review.plan", "page": "review", "cap": 2, "level": "L2"},
    {"slot": "notification.global", "page": "settings", "cap": 1, "level": "L0"},
    {"slot": "app.library", "page": "library", "cap": 4, "level": "L3"},
    {"slot": "app.macro", "page": "macro", "cap": 4, "level": "L3"},
    {"slot": "app.tactics", "page": "tactics", "cap": 4, "level": "L3"},
    # P3-D01：游资雷达（official.youzi-radar），与 macro/tactics 同为独立应用槽。
    # 注意容量：app_library_cap 默认 4，宏观/图书馆/战法各占 1，本槽是**第 4 个**——
    # 再加第 5 个独立应用会被 409「应用区已满」拒绝（见 api/app.py 的 market/install）。
    {"slot": "app.youzi", "page": "youzi", "cap": 4, "level": "L3"},
)


def slot_page_of(slot: str) -> str:
    """插槽 → 输出页面路由；L0 静默槽归到设置页，与 base-shell `slotPageOf()` 同构。"""
    if slot == "notification.global":
        return "settings"
    for entry in DEFINED_SLOTS:
        if entry["slot"] == slot:
            return str(entry["page"])
    prefix = slot.split(".")[0]
    return {
        "today": "today",
        "invest": "investment",
        "research": "research",
        "review": "review",
        "app": "library",
    }.get(prefix, "settings")


def resolve_renderer(level: str) -> str:
    """按插槽级别分发渲染原语（对齐 ui-card-schemas UiCardRenderer）。"""
    return {"L3": "card", "L2": "summary_row", "L1": "inline", "L0": "silent"}.get(level, "card")


def slot_cap(slot: str, settings: CoreSettings) -> int:
    """某槽容量；app.library 的配额来自配置（D-6，默认 4），其余取注册表常量。"""
    if slot == "app.library":
        return settings.app_library_cap
    for entry in DEFINED_SLOTS:
        if entry["slot"] == slot:
            return int(entry["cap"])
    return 0


def compute_slot_occupancy(
    settings: CoreSettings, targeted_by_slot: dict[str, int]
) -> list[dict[str, int | str]]:
    """按注册表与各槽已启用插件声明数重算 9 槽占用；`used = min(targeted, cap)`，溢出进 queued。"""
    rows: list[dict[str, int | str]] = []
    for entry in DEFINED_SLOTS:
        slot = str(entry["slot"])
        cap = slot_cap(slot, settings)
        targeted = targeted_by_slot.get(slot, 0)
        used = min(targeted, cap)
        rows.append(
            {
                "slot": slot,
                "page": slot_page_of(slot),
                "level": str(entry["level"]),
                "targeted": targeted,
                "used": used,
                "cap": cap,
                "queued": max(0, targeted - cap),
            }
        )
    return rows


def resolve_outputs(manifest_slots: list[object]) -> list[dict[str, str]]:
    """catalog 每条目的 resolved_outputs：由服务端插槽注册表解析，避免前端再维护 slotPageOf 兜底。"""
    outputs: list[dict[str, str]] = []
    for plugin_slot in manifest_slots:
        slot_id = getattr(plugin_slot, "slot", None)
        if slot_id is None:
            continue
        level = getattr(plugin_slot, "level", None)
        outputs.append({"slot": str(slot_id), "page": slot_page_of(str(slot_id)), "level": str(level or "")})
    return outputs


def targeted_slots_by_installations(
    installations: list[object], manifest_slots_by_id: dict[str, list[object]]
) -> dict[str, int]:
    """汇总已启用插件对每槽的声明数（targeted）。

    - 仅统计 ENABLED 安装；
    - targeted 来源 = 该插件 manifest 的 ui_slots（安装 payload 本身不含 ui_slots，须由调用方提供）。
    """
    targeted: dict[str, int] = {}
    for installation in installations:
        plugin_id = getattr(installation, "plugin_id", None)
        state = getattr(installation, "state", None)
        if getattr(state, "value", state) != "enabled":
            continue
        for declared in manifest_slots_by_id.get(plugin_id, []):
            slot_id = getattr(declared, "slot", None)
            if slot_id:
                key = str(slot_id)
                targeted[key] = targeted.get(key, 0) + 1
    return targeted
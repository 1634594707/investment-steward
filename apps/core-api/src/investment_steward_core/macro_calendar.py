"""事件日历（信息层·事件层首版，零外部依赖）。

方案 §2「事件层」与 §5「本周数据日历」：数据发布时间轴。首版只做**官方固定节奏
规则可推算**的事件（铁律 ADR-0006：不编造）——
- exact：官方固定规则（如 BLS 非农 = 每月第一个周五、统计局 PMI = 每月最后一日）；
- window：官方只有窗口节奏（如美 CPI 约每月中旬、日本 CPI 约 19 日前后），输出窗口；
- 非固定节奏事件（FOMC/ECB 议息、春斗等）**绝不生成日期**，由前端明示
  「具体日期以官方日历为准，官方日历抓取留后续」。

v37 修复（用户反馈「CPI 已公布但报告仍当作未公布的最大不确定性」）：
1. `phase` 区分窗口位置（upcoming / in_window / elapsed）——窗口期内的窗口型事件过去一律
   被当成「即将到来」，导致已发布数据被当成未来催化；现在 `elapsed` 明确「按规则推算官方
   应已发布」；
2. `linked_series` + `attach_actual_values` 把**实际值**接入事件：宏观管道（macro_feed）
   本就在拉美国 CPI 同比（FRED CPIAUCSL），此前未与日历事件关联；现在按
   `{region}:{indicator}` 关联后写入 `actual_*` 字段，取不到即保持 None 并如实声明。
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from investment_steward_core.domain import MacroCalendarEvent, MacroCalendarSnapshot

_KIND_LABELS: dict[str, str] = {
    "employment": "就业",
    "inflation": "通胀",
    "growth": "增长",
    "monetary": "货币",
}

# 规则口径：
# - first_friday：每月第 1 个周五（BLS 非农发布日，公开固定规则）；
# - last_day：每月最后一日（国家统计局官方制造业 PMI 发布日，公开固定规则）；
# - window=[a, b]：每月 a..b 日窗口（官方节奏约在此窗口，具体日期每年排定）。
# 窗口端点全部取保守宽窗口并 note 明示推算口径，避免给出伪精确日期。
#
# `linked_series`（v37）：指向 macro_feed 的缓存键 "{region}:{indicator}"，用于把实际值
# 接到事件上。只填**确有对应指标**的项；PMI 等尚无实拉序列的事件留空（保持 None）。
EVENT_DEFS: list[dict] = [
    {"region": "us", "event_key": "nfp", "label": "美国非农就业报告", "kind": "employment",
     "rule": "first_friday", "note": "BLS 每月第一个周五发布（北京时间当晚）",
     "linked_series": "us:nfp"},
    # v39：美国 CPI 有两个统计口径，数值不同（季调 vs 未季调）。主口径取**未季调**
    # （BLS 官方 headline 即未季调口径），另一口径作为并列读数展示，避免单一数字误导。
    {"region": "us", "event_key": "us_cpi", "label": "美国 CPI", "kind": "inflation",
     "rule": "window", "window": [10, 15], "note": "BLS 每月中旬发布，具体日期以 BLS 年度日历为准",
     "linked_series": "us:cpi_yoy_nsa", "primary_variant_label": "未季调",
     "variant_series": [{"series": "us:cpi_yoy_nsa", "label": "未季调（对齐 BLS headline）"},
                        {"series": "us:cpi_yoy", "label": "季调"}]},
    {"region": "cn", "event_key": "cn_pmi", "label": "中国官方制造业 PMI", "kind": "growth",
     "rule": "last_day", "note": "国家统计局每月最后一日发布（当月值）",
     "linked_series": "cn:manufacturing_pmi"},
    {"region": "cn", "event_key": "cn_cpi", "label": "中国 CPI", "kind": "inflation",
     "rule": "window", "window": [8, 12], "note": "国家统计局每月上中旬发布，具体日期以官方日历为准",
     # C01（2026-09-15）：月度事件只关联**月度**序列（IMF via FRED，实测可得但发布滞后）；
     # 世界银行年更年度值不再作为「实际值」接入，只作年度背景（attach_actual_values 频率隔离）。
     "linked_series": "cn:cpi_yoy_imf"},
    {"region": "eu", "event_key": "hicp_flash", "label": "欧元区 HICP 快报", "kind": "inflation",
     "rule": "window", "window": [27, 31], "note": "Eurostat 月末发布快报（快报口径不含完整权重）",
     # 世界银行年更序列只能作年度背景（C01 频率隔离），月度快报值未接入 → 如实声明。
     "linked_series": "eu:hicp_yoy"},
    {"region": "jp", "event_key": "jp_cpi", "label": "日本 CPI", "kind": "inflation",
     "rule": "window", "window": [17, 22], "note": "总务省每月 19 日前后发布，具体日期以官方日历为准",
     # 日本 FRED 月度序列实测不可用（仅 6 期观测）；年更序列只作年度背景。
     "linked_series": "jp:cpi_yoy"},
    {"region": "in", "event_key": "in_cpi", "label": "印度 CPI", "kind": "inflation",
     "rule": "window", "window": [10, 15], "note": "MoSPI 每月中旬发布，具体日期以官方日历为准",
     "linked_series": "in:cpi_yoy_imf"},
]

REGION_LABELS: dict[str, str] = {"us": "美国", "cn": "中国", "eu": "欧元区", "jp": "日本", "in": "印度"}


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """某月第 n 个指定星期几（weekday：0=周一 … 4=周五）；不存在则抛 ValueError。"""
    first_weekday, days_in_month = calendar.monthrange(year, month)
    offset = (weekday - first_weekday) % 7
    day = offset + 1 + (n - 1) * 7
    if day > days_in_month:
        raise ValueError(f"{year}-{month:02d} 没有第 {n} 个星期{weekday + 1}")
    return date(year, month, day)


def _event_date(definition: dict, year: int, month: int) -> tuple[date, date | None]:
    """按规则算出（事件日, 窗口止或 None）。"""
    rule = definition["rule"]
    if rule == "first_friday":
        return nth_weekday(year, month, 4, 1), None
    if rule == "last_day":
        _, days_in_month = calendar.monthrange(year, month)
        return date(year, month, days_in_month), None
    start, end = definition["window"]
    _, days_in_month = calendar.monthrange(year, month)
    return date(year, month, min(start, days_in_month)), date(year, month, min(end, days_in_month))


def event_phase(today: date, event_day: date, window_end: date | None) -> str:
    """发布阶段（v37）：upcoming / in_window / elapsed。

    只有 window 型有 in_window；exact 型事件日过后即 elapsed。
    注意：in_window **不表示未发布**——官方可能已在窗口内任意一天发布，规则推算无法得知，
    故不在 phase 里下判断，由调用方结合 actual_value 呈现。
    """
    if today < event_day:
        return "upcoming"
    if window_end is not None and event_day <= today <= window_end:
        return "in_window"
    return "elapsed"


def upcoming_events(today: date, days: int = 14) -> list[MacroCalendarEvent]:
    """生成 [today, today+days] 内的固定节奏事件，按日期升序；覆盖当月与次月。

    window 型：窗口与观察期有交集即纳入（含「窗口正在进行」与**窗口刚过但在观察期内**）；
    note 恒含推算口径。v37 起每条带 `phase`。
    """
    horizon = today + timedelta(days=days)
    events: list[MacroCalendarEvent] = []
    months = {(today.year, today.month), (horizon.year, horizon.month)}
    for year, month in sorted(months):
        for definition in EVENT_DEFS:
            event_day, window_end = _event_date(definition, year, month)
            starts_in_range = today <= event_day <= horizon  # 当日发布也算「即将到来」（今天要看）
            ongoing = (
                window_end is not None
                and event_day <= today <= window_end <= horizon
            )
            # v37：窗口刚刚结束（窗口止 < today）时，按规则官方应已发布，纳入以免「已发布
            # 数据被当作未来催化」；只在窗口止仍在观察期内时纳入，避免无限回溯。
            just_elapsed = (
                window_end is not None
                and window_end < today <= horizon
                and (today - window_end).days <= 7
            )
            if not (starts_in_range or ongoing or just_elapsed):
                continue
            events.append(
                MacroCalendarEvent(
                    region=definition["region"],
                    event_key=definition["event_key"],
                    label=definition["label"],
                    kind=definition["kind"],
                    date_type="exact" if window_end is None else "window",
                    date=event_day.isoformat(),
                    window_end=window_end.isoformat() if window_end else None,
                    phase=event_phase(today, event_day, window_end),
                    linked_series=definition.get("linked_series"),
                    note=f"{definition['note']}；规则推算（{_KIND_LABELS[definition['kind']]}维），具体以官方日历为准",
                )
            )
    events.sort(key=lambda event: (event.date, event.region, event.event_key))
    return events


def attach_actual_values(
    events: list[MacroCalendarEvent],
    readings: dict[str, dict] | None,
    variant_map: dict[str, list[dict[str, str]]] | None = None,
) -> list[MacroCalendarEvent]:
    """把宏观指标实际值接到事件上（v37，就地修改并返回同一列表）。

    `readings`：`{ "{region}:{indicator}": {"latest", "obs_date", "unit", "source"} }`，
    与 macro_feed 缓存键同构。缺失/无值/status != ok 时不写，保持 None（不编造）。
    只接 `elapsed` / `in_window` 阶段的事件——`upcoming` 事件尚未发布，接上期数值会造成
    「把上月数据当本月结论」的新误解。

    v39：`variant_map`（event_key → [{series, label}]）用于**并列多口径读数**——同一指标
    在不同统计口径下数值不同（美国 CPI 季调 vs 未季调），主口径写 `actual_*`，其余口径写
    `actual_variants`，让使用者看清差异来自口径而非数据错误。

    C01（2026-09-15 路线图）观测期隔离：读数带 `frequency` 时，
    - frequency == "annual"（年更年度值）**不写入** `actual_*`——月度发布事件不得把
      年度值冒充当月实际值；改写入 `annual_background`（仅作年度背景，证据文本明示）；
    - 其余频率（monthly 等）照旧写 `actual_*`，并把频率记入 `actual_frequency`。
    无 frequency 的旧读数保持原行为（向后兼容）。
    """
    if not readings:
        return events
    variant_map = variant_map or {}

    def _push_background(event: MacroCalendarEvent, label: str, payload: dict) -> None:
        value = payload.get("latest")
        if value is None:
            return
        event.annual_background.append({
            "label": label,
            "value": float(value),
            "unit": payload.get("unit") or "",
            "obs_date": payload.get("obs_date") or "",
            "source": payload.get("source") or "",
        })

    for event in events:
        if event.phase == "upcoming":
            continue
        series = event.linked_series
        # v39：并列口径（先收集，主口径由 linked_series 决定）
        for spec in variant_map.get(event.event_key, []) or []:
            variant_series = str(spec.get("series") or "").strip()
            payload = readings.get(variant_series)
            if not payload:
                continue
            value = payload.get("latest")
            if value is None or payload.get("status") not in (None, "ok"):
                continue
            # C01：年度频率的「并列口径」也是年度背景，不进 actual_variants。
            if payload.get("frequency") == "annual":
                _push_background(event, str(spec.get("label") or variant_series), payload)
                continue
            event.actual_variants.append({
                "label": str(spec.get("label") or variant_series),
                "series": variant_series,
                "value": float(value),
                "unit": payload.get("unit") or "",
                "obs_date": payload.get("obs_date") or "",
                "source": payload.get("source") or "",
            })
        if not series:
            continue
        payload = readings.get(series)
        if not payload:
            continue
        latest = payload.get("latest")
        if latest is None or payload.get("status") not in (None, "ok"):
            continue
        # C01：年更年度值不得作为月度事件的「实际值」——只作年度背景。
        if payload.get("frequency") == "annual":
            _push_background(event, "年度背景值", payload)
            continue
        event.actual_value = float(latest)
        event.actual_unit = payload.get("unit") or None
        event.actual_obs_date = payload.get("obs_date") or None
        event.actual_source = payload.get("source") or None
        event.actual_frequency = payload.get("frequency") or None
    return events


def event_variant_map() -> dict[str, list[dict[str, str]]]:
    """event_key → 并列口径列表（来自 EVENT_DEFS 的 `variant_series`）。"""
    return {
        str(definition["event_key"]): list(definition.get("variant_series") or [])
        for definition in EVENT_DEFS
        if definition.get("variant_series")
    }


def event_primary_variant_labels() -> dict[str, str]:
    """event_key → 主口径名称（如「未季调」）。"""
    return {
        str(definition["event_key"]): str(definition["primary_variant_label"])
        for definition in EVENT_DEFS
        if definition.get("primary_variant_label")
    }


def calendar_snapshot(
    today: date,
    days: int = 14,
    readings: dict[str, dict] | None = None,
) -> MacroCalendarSnapshot:
    events = attach_actual_values(
        upcoming_events(today, days), readings, variant_map=event_variant_map()
    )
    # v39：标注主口径名称，前端/证据文本据此说明「这个数字是哪个口径」。
    primary_labels = event_primary_variant_labels()
    for event in events:
        label = primary_labels.get(event.event_key)
        if label and event.actual_value is not None:
            event.actual_variant_label = label
    return MacroCalendarSnapshot(events=events, days=days)

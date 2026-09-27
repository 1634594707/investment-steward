"""本地横截面面板（QL10）:确定 universe → 逐标的拉日线落本地缓存 → 对齐成「日期 × 标的」矩阵 → 内容寻址快照。

设计约束（对齐《量化研究实验室质量提升任务路线图》2026-09-22 的 P2）:
- **可重建**:面板从快照可完整重建（``load_panel`` 与 ``build_panel`` 输出逐位一致）。
- **可续传**:逐标的行情落 ``layout.cache/quant-panel-bars/`` —— 同一 UTC 日期内重跑直接命中,
  失败/取消后重发只补缺口;跨日一律重取（不把昨天的面板当今天的用）。
- **可追溯**:universe 规则、榜单来源与观测日、每个标的的取数来源与行数、失败清单全部落响应与快照元数据。
- **不编造**:拉不到就进 ``failures`` 并让覆盖度下降,不补 0、不假装市场有这只票。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from investment_steward_core.storage import artifact_store
from investment_steward_core.storage.paths import StorageLayout

# —— 口径版本（改动面板结构/对齐规则必须递增）——
# v2（2026-09-22）：交集对齐前先做**历史长度资格审查**。v1 的纯交集口径下，universe 里只要
# 有一个短历史成员就会把整个面板压塌——实测成交额前 8 名里的 688825（2026-07-27 上市）
# 只有 42 根，250 根窗口的面板被压成 42 天，三段式 IC 直接判「样本不足」，横截面挖掘完全不可用。
PANEL_SPEC_VERSION = 2
# 逐标的日线缓存（可变,当日有效）与成品面板快照（内容寻址,不可变）
PANEL_CACHE_DIRNAME = "quant-panel-bars"
PANEL_SNAPSHOT_KIND = "panel_snapshots"

DEFAULT_UNIVERSE_SIZE = 30
MIN_UNIVERSE_SIZE = 5
MAX_UNIVERSE_SIZE = 200
DEFAULT_PANEL_WINDOW = 250
ALIGNMENT_MODES = ("intersection",)
DEFAULT_ALIGNMENT = "intersection"
# 覆盖度低于该比例时显式标注「面板仍可用但样本偏薄」,不静默
COVERAGE_WARN_RATIO = 0.6

# —— 历史长度资格审查（v2）——
# 交集口径要求「同一天所有标的都有数据」，因此历史最短的那个成员决定整个面板的长度。
# 装配前剔除历史明显短于同期最佳覆盖的成员，剔除清单（excluded）连同门槛一并如实报出，
# **不静默丢票**，也不为凑数把短历史票留在面板里等它把样本压塌。
#
# 判据**只做相对比较**（相对同期覆盖最好的标的），不设「面板至少多少行」的绝对下限：
# 绝对下限会与下游 `quant_cross.MIN_DATES_FOR_IC` 的可行性闸门重复，而且会在小窗口上
# 把全部成员一起剔光（v2 初版用 max(60, 0.9×best) 就把 window=30/40 的全员判出局）。
# 「整体样本本来就不够」由下游如实证伪，不在这里提前清空面板。
PANEL_MIN_HISTORY_RATIO = 0.9

# —— 分组（QL11 中性化用）——
GROUP_MODES = ("none", "industry")
DEFAULT_GROUP_MODE = "none"
# 行业分组缺口超过该比例即判定「不足以冒充做过中性化」,调用方应降级为 none
MAX_UNASSIGNED_RATIO = 0.3


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def resolve_universe(
    board_rows: Iterable[dict[str, Any]],
    *,
    size: int = DEFAULT_UNIVERSE_SIZE,
    source: str = "cn-market-board",
    observed_at: str | None = None,
) -> dict[str, Any]:
    """从全市场榜单（调用方按成交额降序取回）裁出 universe,规则与来源显式记录。

    标的字段兼容 ``code``（东财口径）与 ``symbol``（新浪榜单/板块成分股口径）两种写法,
    先取 ``code`` 再退回 ``symbol`` —— 两种榜单管道都直接可用,不做字段改名。
    不按代码排序、也不做二次筛选:榜单顺序即规则（成交额降序）,这样「为什么是这些票」可复现。
    """
    if not MIN_UNIVERSE_SIZE <= int(size) <= MAX_UNIVERSE_SIZE:
        raise ValueError(f"universe 大小必须在 {MIN_UNIVERSE_SIZE}—{MAX_UNIVERSE_SIZE},收到 {size}")
    candidates = [row for row in board_rows if str(row.get("code") or row.get("symbol") or "").strip()]
    picked = candidates[: int(size)]
    return {
        "rule": f"成交额前 {len(picked)} 名（榜单原始顺序,不做二次筛选）",
        "requested_size": int(size),
        "size": len(picked),
        "symbols": [str(row.get("code") or row.get("symbol")) for row in picked],
        "candidates_available": len(candidates),
        "source": source,
        "observed_at": observed_at or datetime.now(UTC).isoformat(),
        "names": [str(row.get("name") or "") for row in picked],
    }


def _cache_path(layout: StorageLayout, symbol: str) -> Path:
    safe = "".join(ch for ch in symbol if ch.isalnum() or ch in ("-", "_")) or "unknown"
    return layout.cache / PANEL_CACHE_DIRNAME / f"{safe}.json"


def read_cached_bars(layout: StorageLayout, symbol: str, *, window: int, today: str) -> list[dict[str, Any]] | None:
    """读当日缓存:仅当缓存日期 == 今天且窗口足够才复用,否则返回 None（重取,不用旧数据充新）。"""
    path = _cache_path(layout, symbol)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("cached_for_date") != today:
        return None
    if int(payload.get("window") or 0) < int(window):
        return None
    bars = payload.get("bars")
    if not isinstance(bars, list) or not bars:
        return None
    return bars


def write_cached_bars(
    layout: StorageLayout, symbol: str, bars: list[dict[str, Any]], *, window: int, source: str, today: str
) -> None:
    path = _cache_path(layout, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {
                "symbol": symbol,
                "window": int(window),
                "source": source,
                "cached_for_date": today,
                "fetched_at": datetime.now(UTC).isoformat(),
                "bars": bars,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tmp.replace(path)


def build_panel(
    layout: StorageLayout,
    symbols: list[str],
    *,
    window: int = DEFAULT_PANEL_WINDOW,
    fetch_bars: Callable[[str, int], tuple[list[dict[str, Any]], str]],
    alignment: str = DEFAULT_ALIGNMENT,
    use_cache: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
    on_step: Callable[[dict[str, Any]], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """逐标的拉日线并对齐成面板;失败/取消不抛错,记进 ``failures`` / ``cancelled``。

    ``on_progress(done, total)`` 用于任务进度;``on_step(step)`` 记逐标的明细;
    ``is_cancelled()`` 为真时停止发新取数（已完成的部分保留在缓存里,重跑即续传）。
    """
    if alignment not in ALIGNMENT_MODES:
        raise ValueError(f"alignment 仅支持 {' / '.join(ALIGNMENT_MODES)},收到 {alignment!r}")
    if window <= 0:
        raise ValueError(f"window 必须为正整数,收到 {window}")

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    total = len(symbols)
    steps: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    per_symbol: dict[str, list[dict[str, Any]]] = {}
    cancelled = False

    for index, symbol in enumerate(symbols):
        if is_cancelled is not None and is_cancelled():
            cancelled = True
            break
        source = ""
        bars: list[dict[str, Any]] | None = None
        from_cache = False
        if use_cache:
            bars = read_cached_bars(layout, symbol, window=window, today=today)
            if bars is not None:
                from_cache = True
                source = "cache"
        if bars is None:
            try:
                bars, source = fetch_bars(symbol, window)
            except Exception as error:  # noqa: BLE001 - 逐标的失败要如实记录并继续
                failures.append({"symbol": symbol, "reason": f"{type(error).__name__}: {error}"})
                steps.append({"symbol": symbol, "ok": False, "reason": failures[-1]["reason"]})
                if on_progress is not None:
                    on_progress(index + 1, total)
                continue
            if use_cache:
                write_cached_bars(layout, symbol, bars, window=window, source=source, today=today)
        per_symbol[symbol] = bars
        step = {
            "symbol": symbol,
            "ok": True,
            "from_cache": from_cache,
            "source": source,
            "rows": len(bars),
            "first_bar": str(bars[0]["timestamp"])[:10] if bars else None,
            "last_bar": str(bars[-1]["timestamp"])[:10] if bars else None,
        }
        steps.append(step)
        if on_step is not None:
            on_step(step)
        if on_progress is not None:
            on_progress(index + 1, total)

    return assemble_panel(
        per_symbol,
        symbols=[symbol for symbol in symbols if symbol in per_symbol],
        window=window,
        alignment=alignment,
        steps=steps,
        failures=failures,
        cancelled=cancelled,
        requested_symbols=list(symbols),
    )


def assemble_panel(
    per_symbol: dict[str, list[dict[str, Any]]],
    *,
    symbols: list[str],
    window: int,
    alignment: str = DEFAULT_ALIGNMENT,
    steps: list[dict[str, Any]] | None = None,
    failures: list[dict[str, Any]] | None = None,
    cancelled: bool = False,
    requested_symbols: list[str] | None = None,
) -> dict[str, Any]:
    """把逐标的 K 线对齐成「日期 × 标的」矩阵（交集口径:只保留**全部标的都有**的交易日）。

    交集口径的理由:横截面 rank IC 要求同一天有完整的截面;缺失格用 0 或前值填充会伪造截面。
    代价是样本变短——这个代价必须显式报出来（``dropped_dates`` / ``coverage``）。

    **v2 起先做历史资格审查**：交集口径下板块长度由最短历史成员决定，一个刚上市的新股会把
    整个面板压塌（实测 688825 只有 42 根 → 30×250 的面板变成 30×42，挖掘直接判样本不足）。
    资格审查剔除历史 < ``max(PANEL_MIN_ROWS, 最优覆盖 × PANEL_MIN_HISTORY_RATIO)`` 的成员，
    被剔除者连同门槛写进 ``excluded``（不静默丢票）；剩余成员仍按严格交集对齐，**不填充**。

    矩阵按**列式**存:``columns[symbol] = {"open": [...], "high": [...], "low": [...], "close": [...], "volume": [...]}``,
    与 ``dates`` 逐位对齐。列式既能让 ``bars_of`` 还原成逐 bar 形态喂给 ``_feature_series``,
    也比逐 bar 对象省一半以上体积。
    """
    if not per_symbol:
        return {
            "panel_spec_version": PANEL_SPEC_VERSION,
            "available": False,
            "alignment": alignment,
            "window": int(window),
            "symbols": [],
            "dates": [],
            "cells": 0,
            "coverage": 0.0,
            "dropped_dates": {},
            "own_date_counts": {},
            "excluded": [],
            "history_floor": 0,
            "alignment_loss": {"union_dates": 0, "common_dates": 0, "dropped_by_intersection": 0},
            "columns": {},
            "steps": steps or [],
            "failures": failures or [],
            "cancelled": cancelled,
            "degraded_reason": "没有任何标的数据可用——不构造空面板冒充结果。",
        }

    by_symbol: dict[str, dict[str, dict[str, Any]]] = {}
    for symbol in symbols:
        bars = per_symbol[symbol]
        by_symbol[symbol] = {str(bar["timestamp"])[:10]: bar for bar in bars}
    own_date_counts = {symbol: len(by_symbol[symbol]) for symbol in symbols}

    # —— 历史资格审查（v2）——
    best_count = max(own_date_counts.values()) if own_date_counts else 0
    history_floor = int(best_count * PANEL_MIN_HISTORY_RATIO)
    eligible = [symbol for symbol in symbols if own_date_counts[symbol] >= history_floor]
    excluded = [
        {
            "symbol": symbol,
            "rows": own_date_counts[symbol],
            "reason": (
                f"历史 {own_date_counts[symbol]} 根 < 门槛 {history_floor}"
                f"（最优覆盖 {best_count} 根 × {PANEL_MIN_HISTORY_RATIO}）"
                "——留在面板里会把交集长度压到它自己的历史长度"
            ),
        }
        for symbol in symbols
        if own_date_counts[symbol] < history_floor
    ]

    date_sets = [set(by_symbol[symbol].keys()) for symbol in eligible]
    common = set.intersection(*date_sets) if date_sets else set()
    dates = sorted(common)
    union = set().union(*date_sets) if date_sets else set()
    # 对齐代价如实报出:每个**入选**标的自身交易日中被交集裁掉的数量 + 面板整体的并集/交集差
    dropped = {symbol: len(by_symbol[symbol]) - len(common) for symbol in eligible}
    alignment_loss = {
        "union_dates": len(union),
        "common_dates": len(common),
        "dropped_by_intersection": len(union) - len(common),
        "requested_symbols": len(symbols),
        "eligible_symbols": len(eligible),
        "excluded_by_history": len(excluded),
    }

    fields = ("open", "high", "low", "close", "volume")
    columns: dict[str, dict[str, list[float]]] = {}
    for symbol in eligible:
        columns[symbol] = {}
        for field in fields:
            if field == "volume":
                columns[symbol][field] = [float(by_symbol[symbol][day].get("volume") or 0.0) for day in dates]
            else:
                columns[symbol][field] = [float(by_symbol[symbol][day][field]) for day in dates]

    cells = len(dates) * len(eligible)
    available = bool(dates) and len(eligible) >= MIN_UNIVERSE_SIZE
    coverage = round(len(eligible) / max(1, len(requested_symbols or symbols)), 6)
    panel: dict[str, Any] = {
        "panel_spec_version": PANEL_SPEC_VERSION,
        "available": available,
        "alignment": alignment,
        "window": int(window),
        "symbols": list(eligible),
        "dates": dates,
        "date_count": len(dates),
        "symbol_count": len(eligible),
        "cells": cells,
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "coverage": coverage,
        "dropped_dates": dropped,
        "own_date_counts": own_date_counts,
        "excluded": excluded,
        "history_floor": history_floor,
        "alignment_loss": alignment_loss,
        "columns": columns,
        "steps": steps or [],
        "failures": failures or [],
        "cancelled": cancelled,
        "degraded_reason": None,
    }
    reasons: list[str] = []
    if not dates:
        reasons.append("标的之间没有共同交易日（数据源覆盖不一致）,不做填充、不伪造截面。")
    if not eligible:
        reasons.append(
            f"全部 {len(symbols)} 只标的的历史都不到门槛 {history_floor} 根"
            "——没有可用的同截面观测,不构造短到无法判定的面板。"
        )
    if len(eligible) < MIN_UNIVERSE_SIZE:
        reasons.append(f"可用标的仅 {len(eligible)} 只（横截面研究至少 {MIN_UNIVERSE_SIZE} 只）")
    if coverage < COVERAGE_WARN_RATIO:
        reasons.append(f"标的覆盖率 {coverage:.0%} 偏低——缺失的票在 failures 里逐条列明")
    if excluded:
        reasons.append(
            f"{len(excluded)} 只标的因历史短于门槛 {history_floor} 根被剔除（见 excluded）："
            "交集口径下它的历史长度就是整个面板的长度"
        )
    if cancelled:
        reasons.append("任务被取消:已完成部分已落本地缓存,重发只补缺口")
    if reasons:
        panel["degraded_reason"] = "；".join(reasons)
    return panel


def bars_of(panel: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    """把列式面板还原成逐 bar 形态（供 ``quant_factors._feature_series`` 等复用）。"""
    columns = (panel.get("columns") or {}).get(symbol)
    dates = panel.get("dates") or []
    if not isinstance(columns, dict):
        return []
    return [
        {
            "timestamp": f"{day}T00:00:00+00:00",
            "open": columns["open"][index],
            "high": columns["high"][index],
            "low": columns["low"][index],
            "close": columns["close"][index],
            "volume": columns["volume"][index],
        }
        for index, day in enumerate(dates)
    ]


def panel_digest(panel: dict[str, Any]) -> str:
    """面板内容哈希:只吃矩阵本体与元数据里的口径字段（不含 steps/failures 这类运行噪声）。"""
    core = {
        "panel_spec_version": panel["panel_spec_version"],
        "alignment": panel["alignment"],
        "symbols": panel["symbols"],
        "dates": panel["dates"],
        "columns": panel["columns"],
    }
    return hashlib.sha256(_canonical(core).encode("utf-8")).hexdigest()


def save_panel(layout: StorageLayout, panel: dict[str, Any], *, built_at: str | None = None) -> dict[str, Any]:
    """把面板落成**内容寻址**快照,返回登记信息（哈希进响应,可复核）。

    ``built_at`` 显式传入才写进制品:缺省为 ``None`` 时制品字节**完全由内容决定**,
    同一面板重复保存得到同一 ``content_hash``（可复算的前提）。时间溯源由 universe 的
    ``observed_at`` 与逐标的缓存的 ``fetched_at`` 承担,不用「保存时刻」污染内容哈希。
    """
    digest = panel_digest(panel)
    file_name = f"panel-{digest}.json"
    payload = {
        "kind": "cross_section_panel",
        "panel_spec_version": panel["panel_spec_version"],
        "digest": digest,
        "alignment": panel["alignment"],
        "window": panel["window"],
        "available": panel.get("available", False),
        "symbols": panel["symbols"],
        "dates": panel["dates"],
        "first_date": panel.get("first_date"),
        "last_date": panel.get("last_date"),
        "dropped_dates": panel.get("dropped_dates") or {},
        "own_date_counts": panel.get("own_date_counts") or {},
        "excluded": panel.get("excluded") or [],
        "history_floor": panel.get("history_floor", 0),
        "alignment_loss": panel.get("alignment_loss") or {},
        "columns": panel["columns"],
        "steps": panel.get("steps") or [],
        "failures": panel.get("failures") or [],
        "coverage": panel.get("coverage"),
        "built_at": built_at,
    }
    _, content_hash = artifact_store.save_artifact(layout, PANEL_SNAPSHOT_KIND, file_name, payload)
    return {
        "snapshot_hash": digest,
        "file_name": file_name,
        "content_hash": content_hash,
        "date_count": panel.get("date_count", len(panel["dates"])),
        "symbol_count": panel.get("symbol_count", len(panel["symbols"])),
        "cells": panel.get("cells", len(panel["dates"]) * len(panel["symbols"])),
        "first_date": panel.get("first_date"),
        "last_date": panel.get("last_date"),
    }


def load_panel(layout: StorageLayout, *, content_hash: str, file_name: str) -> dict[str, Any]:
    """从快照重建面板（QL10 验收:面板必须可从快照完整重建,且与原始逐位一致）。"""
    payload = artifact_store.read_artifact(layout, PANEL_SNAPSHOT_KIND, file_name, content_hash)
    if payload.get("kind") != "cross_section_panel":
        raise ValueError("快照不是横截面面板:kind 不匹配")
    panel = {
        "panel_spec_version": payload["panel_spec_version"],
        "available": bool(payload.get("available")),
        "alignment": payload["alignment"],
        "window": payload["window"],
        "symbols": payload["symbols"],
        "dates": payload["dates"],
        "date_count": len(payload["dates"]),
        "symbol_count": len(payload["symbols"]),
        "cells": len(payload["dates"]) * len(payload["symbols"]),
        "first_date": payload.get("first_date"),
        "last_date": payload.get("last_date"),
        "coverage": payload.get("coverage"),
        "dropped_dates": payload.get("dropped_dates") or {},
        "own_date_counts": payload.get("own_date_counts") or {},
        "excluded": payload.get("excluded") or [],
        "history_floor": payload.get("history_floor", 0),
        "alignment_loss": payload.get("alignment_loss") or {},
        "columns": payload["columns"],
        "steps": payload.get("steps") or [],
        "failures": payload.get("failures") or [],
        "cancelled": False,
        "degraded_reason": None,
    }
    if panel_digest(panel) != payload["digest"]:
        raise ValueError("面板快照哈希不一致:内容被改动或损坏")
    return panel


def _unassigned_ratio(groups: dict[str, str | None], symbols: list[str]) -> float:
    missing = sum(1 for symbol in symbols if not (groups.get(symbol) or "").strip())
    return round(missing / max(1, len(symbols)), 6)


def resolve_industry_groups(
    symbols: list[str],
    *,
    sector_list: Callable[[], list[dict[str, Any]]] | None = None,
    sector_members: Callable[..., list[dict[str, Any]]] | None = None,
    top: int = 100,
    max_sectors: int | None = None,
) -> dict[str, Any]:
    """用新浪行业板块成分股反查每只标的所属行业（QL11 中性化的分组来源）。

    **诚实原则**：反查不到就不给分组（``groups[symbol] = None``），并把缺口比例报出来；
    ``unassigned_ratio > MAX_UNASSIGNED_RATIO`` 时 ``usable=False`` —— 调用方必须降级为
    ``none``，绝不能把未分组标的塞进某组假装做过中性化。

    ``sector_list`` / ``sector_members`` 可注入（测试用），缺省用 ``market_feed`` 的实现。
    """
    if sector_list is None or sector_members is None:  # 延迟导入:避免模块级循环依赖
        from investment_steward_core import market_feed as _market_feed

        sector_list = sector_list or _market_feed.fetch_cn_sector_list
        sector_members = sector_members or _market_feed.fetch_cn_sector_members

    wanted = [str(symbol).strip() for symbol in symbols if str(symbol).strip()]
    groups: dict[str, str | None] = {symbol: None for symbol in wanted}
    errors: list[dict[str, Any]] = []
    try:
        sectors = sector_list()
    except Exception as error:  # noqa: BLE001 - 拿不到板块清单就是拿不到,不猜行业
        return {
            "mode": "none",
            "source": "sina-industry-sectors",
            "groups": groups,
            "assigned": 0,
            "unassigned": list(wanted),
            "unassigned_ratio": 1.0 if wanted else 0.0,
            "usable": False,
            "sectors_total": 0,
            "sectors_scanned": 0,
            "errors": [{"scope": "sector_list", "reason": f"{type(error).__name__}: {error}"}],
            "degraded_reason": f"行业板块清单拉取失败（{error}）——本次不做行业中性化，不做任何推测分组。",
        }

    scanned = 0
    pending = set(wanted)
    for sector in sectors:
        if max_sectors is not None and scanned >= int(max_sectors):
            break
        if not pending:
            break
        code = str(sector.get("code") or "").strip()
        name = str(sector.get("name") or "").strip()
        if not code:
            continue
        scanned += 1
        try:
            members = sector_members(code, top=top)
        except Exception as error:  # noqa: BLE001 - 单板块失败不拖垮整体
            errors.append({"scope": code, "reason": f"{type(error).__name__}: {error}"})
            continue
        for row in members:
            symbol = str(row.get("symbol") or row.get("code") or "").strip()
            if symbol in pending:
                groups[symbol] = name or code
                pending.discard(symbol)

    unassigned = sorted(pending)
    ratio = _unassigned_ratio(groups, wanted)
    usable = bool(wanted) and ratio <= MAX_UNASSIGNED_RATIO
    degraded = None
    if not usable:
        if not wanted:
            degraded = "universe 为空:没有可分组标的。"
        else:
            degraded = (
                f"行业反查缺口 {ratio:.0%}（阈值 {MAX_UNASSIGNED_RATIO:.0%}）："
                f"{len(unassigned)} 只未匹配到行业板块，已如实列出；本次不做行业中性化。"
            )
    return {
        "mode": "industry" if usable else "none",
        "source": "sina-industry-sectors",
        "groups": groups,
        "assigned": len(wanted) - len(unassigned),
        "unassigned": unassigned,
        "unassigned_ratio": ratio,
        "usable": usable,
        "sectors_total": len(sectors),
        "sectors_scanned": scanned,
        "errors": errors,
        "degraded_reason": degraded,
    }


def summarise_panel(panel: dict[str, Any]) -> dict[str, Any]:
    """面板摘要:给前端/日志一句话可核验的形态,不回传整个矩阵。"""
    return {
        "available": panel.get("available", False),
        "panel_spec_version": panel.get("panel_spec_version"),
        "alignment": panel.get("alignment"),
        "window": panel.get("window"),
        "symbol_count": panel.get("symbol_count", len(panel.get("symbols") or [])),
        "date_count": panel.get("date_count", len(panel.get("dates") or [])),
        "cells": panel.get("cells"),
        "first_date": panel.get("first_date"),
        "last_date": panel.get("last_date"),
        "coverage": panel.get("coverage"),
        "alignment_loss": panel.get("alignment_loss") or {},
        "excluded": panel.get("excluded") or [],
        "history_floor": panel.get("history_floor", 0),
        "symbols": panel.get("symbols") or [],
        "failures": panel.get("failures") or [],
        "steps": panel.get("steps") or [],
        "cancelled": panel.get("cancelled", False),
        "degraded_reason": panel.get("degraded_reason"),
    }

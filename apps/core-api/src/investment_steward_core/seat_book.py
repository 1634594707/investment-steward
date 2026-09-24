"""席位观察名单内核：加载 / 校验 / 命中判定（纯 stdlib，可离线测试）。

`plugins/official/youzi-radar/seat_watchlist.json` 是**观察名单**，不是排行榜。
本模块只做三件事：

1. 把静态 JSON 读成结构化条目，**主键一律用 `operatedept_code`**（营业部更名/合并会让
   名字失效，名字只用于展示与逐字核验）。
2. 校验条目自洽（代码非空、名字非空、kind 合法）。
3. 判定某条榜单行是否命中游资观察名单——**桶一律不算命中**。

## 为什么「桶」必须排除

交易所披露里的 `机构专用`、`沪股通专用`、`深股通专用`，以及券商 `*总部`，
是匿名汇总通道，不是某家营业部。实测（见 `tests/fixtures/youzi/README.md`）：
它们出现在明细行里，但按名单独查营业部维度表返回 `code=9201`（返回数据为空）。

把它们计入「游资命中」会造成两种失真：
法人机构被讲成游资；同一桶在不同股票上被当成同一个主体追踪。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

#: 插件目录覆盖点：打包宿主（desktop-host）把它指向 `resources/plugins`，
#: 与 `api/app.py::_registry_entries` 完全同一口径。
PLUGIN_REGISTRY_DIR_ENV = "STEWARD_PLUGIN_REGISTRY_DIR"

#: 名单在插件目录内的相对位置。
WATCHLIST_RELATIVE_PATH = Path("official") / "youzi-radar" / "seat_watchlist.json"

#: 仓库内回退路径（**仅非打包形态可用**）。
#: 冻结成 PyInstaller onefile 后 `__file__` 落在解包临时目录，`parents[4]` 会指向
#: `%LOCALAPPDATA%`——真机实测报错路径为
#: `C:\Users\<user>\AppData\plugins\official\youzi-radar\seat_watchlist.json`，
#: 于是 /youzi/watchlist 直接 500。打包形态必须先读上面的覆盖点。
_REPO_WATCHLIST_PATH = (
    Path(__file__).resolve().parents[4]
    / "plugins"
    / "official"
    / "youzi-radar"
    / "seat_watchlist.json"
)


def default_watchlist_path() -> Path:
    """当前生效的默认名单路径。

    懒解析（每次调用读环境变量）而非模块级常量：既避免 import 顺序决定路径，
    也让测试可以直接改环境变量而不必重载模块。
    """
    registry = os.environ.get(PLUGIN_REGISTRY_DIR_ENV)
    if registry:
        return Path(registry) / WATCHLIST_RELATIVE_PATH
    return _REPO_WATCHLIST_PATH

SEAT_KIND = "seat"
BUCKET_KIND = "bucket"


class WatchlistError(RuntimeError):
    """观察名单文件缺失或结构不合法。缺失是**失败**，不允许静默降级成空名单——
    空名单会让「今日无命中」和「名单没加载」变得无法区分。"""


@dataclass(frozen=True)
class WatchSeat:
    """观察名单中的一条营业部席位。"""

    operatedept_code: str
    name: str
    kind: str
    aliases: tuple[str, ...]
    last_seen_trade_date: str
    record_count: int
    note: str

    @property
    def is_bucket(self) -> bool:
        return self.kind == BUCKET_KIND


@dataclass(frozen=True)
class Watchlist:
    """整份观察名单。"""

    version: str
    disclaimer: str
    seats: tuple[WatchSeat, ...]
    buckets: tuple[WatchSeat, ...]
    bucket_name_suffixes: tuple[str, ...]

    @property
    def all_entries(self) -> tuple[WatchSeat, ...]:
        return self.seats + self.buckets

    def by_code(self, operatedept_code: str) -> WatchSeat | None:
        target = (operatedept_code or "").strip()
        if not target:
            return None
        return next((item for item in self.all_entries if item.operatedept_code == target), None)

    def by_name(self, name: str) -> WatchSeat | None:
        target = (name or "").strip()
        if not target:
            return None
        for item in self.all_entries:
            if item.name == target or target in item.aliases:
                return item
        return None

    def is_bucket_name(self, name: str) -> bool:
        """按名判定是否为匿名汇总桶（机构/沪深股通专用、券商总部等）。"""
        target = (name or "").strip()
        if not target:
            return False
        if any(item.name == target for item in self.buckets):
            return True
        return any(target.endswith(suffix) for suffix in self.bucket_name_suffixes)


def parse_watchlist(payload: dict[str, object]) -> Watchlist:
    """解析并校验名单；结构不合法直接抛 `WatchlistError`（不返回半份名单）。"""
    raw_seats = payload.get("seats")
    if not isinstance(raw_seats, list) or not raw_seats:
        raise WatchlistError("观察名单缺少非空 seats")

    seats: list[WatchSeat] = []
    seen_codes: set[str] = set()
    for index, raw in enumerate(raw_seats):
        if not isinstance(raw, dict):
            raise WatchlistError(f"seats[{index}] 不是对象")
        code = str(raw.get("operatedept_code") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not code:
            raise WatchlistError(f"seats[{index}] 缺少 operatedept_code（主键不可为空）")
        if not name:
            raise WatchlistError(f"seats[{index}]（{code}）缺少 name")
        if code in seen_codes:
            raise WatchlistError(f"operatedept_code 重复：{code}")
        seen_codes.add(code)
        kind = str(raw.get("kind") or SEAT_KIND).strip()
        if kind != SEAT_KIND:
            raise WatchlistError(f"seats[{index}]（{code}）的 kind 必须是 {SEAT_KIND}，桶请放进 buckets")
        aliases = raw.get("aliases") or []
        if not isinstance(aliases, list):
            raise WatchlistError(f"seats[{index}]（{code}）的 aliases 必须是数组")
        record_count = raw.get("record_count")
        seats.append(
            WatchSeat(
                operatedept_code=code,
                name=name,
                kind=kind,
                aliases=tuple(str(item) for item in aliases),
                last_seen_trade_date=str(raw.get("last_seen_trade_date") or ""),
                record_count=int(record_count) if isinstance(record_count, (int, float)) else 0,
                note=str(raw.get("note") or ""),
            )
        )

    buckets: list[WatchSeat] = []
    for index, raw in enumerate(payload.get("buckets") or []):
        if not isinstance(raw, dict):
            raise WatchlistError(f"buckets[{index}] 不是对象")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise WatchlistError(f"buckets[{index}] 缺少 name")
        buckets.append(
            WatchSeat(
                operatedept_code=str(raw.get("operatedept_code") or "").strip(),
                name=name,
                kind=str(raw.get("kind") or BUCKET_KIND),
                aliases=(),
                last_seen_trade_date="",
                record_count=0,
                note=str(raw.get("note") or ""),
            )
        )

    suffixes = payload.get("bucket_name_suffixes") or []
    if not isinstance(suffixes, list):
        raise WatchlistError("bucket_name_suffixes 必须是数组")

    return Watchlist(
        version=str(payload.get("watchlist_version") or ""),
        disclaimer=str(payload.get("disclaimer") or ""),
        seats=tuple(seats),
        buckets=tuple(buckets),
        bucket_name_suffixes=tuple(str(item) for item in suffixes),
    )


def load_watchlist(path: Path | str | None = None) -> Watchlist:
    """从磁盘加载名单。文件缺失或解析失败抛 `WatchlistError`。"""
    target = Path(path) if path is not None else default_watchlist_path()
    if not target.is_file():
        raise WatchlistError(f"观察名单文件不存在：{target}")
    try:
        payload = json.loads(target.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WatchlistError(f"观察名单读取失败：{target}（{error}）") from error
    if not isinstance(payload, dict):
        raise WatchlistError(f"观察名单顶层必须是对象：{target}")
    return parse_watchlist(payload)


@dataclass(frozen=True)
class SeatMatch:
    """一次命中判定结果。"""

    matched: bool
    seat: WatchSeat | None
    reason: str


def match_seat(
    watchlist: Watchlist, *, operatedept_code: str = "", operatedept_name: str = ""
) -> SeatMatch:
    """判定某条榜单行是否命中观察名单。

    规则（顺序即优先级）：
    1. **桶一律不命中**——先按 `kind=bucket` 的名单与后缀判定，命中则直接返回不匹配。
    2. 代码命中优先于名字命中（更名后名字对不上、代码仍在）。
    3. 名字只做**精确匹配**或别名匹配：东财不支持 `like` 查询，模糊匹配没有数据源支撑，
       自己造一套模糊规则会让「命中」变成猜测。
    """
    if watchlist.is_bucket_name(operatedept_name):
        return SeatMatch(matched=False, seat=None, reason="匿名汇总桶（机构/沪深股通/券商总部），不计入游资命中")

    by_code = watchlist.by_code(operatedept_code)
    if by_code is not None:
        if by_code.is_bucket:
            return SeatMatch(matched=False, seat=None, reason="该代码对应匿名汇总桶")
        return SeatMatch(matched=True, seat=by_code, reason="代码命中观察名单")

    by_name = watchlist.by_name(operatedept_name)
    if by_name is not None:
        if by_name.is_bucket:
            return SeatMatch(matched=False, seat=None, reason="该名称对应匿名汇总桶")
        return SeatMatch(matched=True, seat=by_name, reason="名称命中观察名单（精确匹配或别名）")

    return SeatMatch(matched=False, seat=None, reason="不在观察名单")


def hits_for_rows(
    watchlist: Watchlist, rows: list[dict[str, object]]
) -> list[tuple[dict[str, object], SeatMatch]]:
    """对一批榜单行做命中判定，只返回命中的行（保持输入顺序）。"""
    results: list[tuple[dict[str, object], SeatMatch]] = []
    for row in rows:
        match = match_seat(
            watchlist,
            operatedept_code=str(row.get("OPERATEDEPT_CODE") or row.get("operatedept_code") or ""),
            operatedept_name=str(row.get("OPERATEDEPT_NAME") or row.get("operatedept_name") or ""),
        )
        if match.matched:
            results.append((row, match))
    return results


def describe_watchlist(watchlist: Watchlist) -> str:
    """渲染名单说明文本（供插件 UI 与学习教材共用）。

    逐条展示「最近一次上榜」，因为名单里的席位活跃度差异极大——
    只写名气会让人误以为它们都在交易。
    """
    lines = [
        (
            f"席位观察名单 v{watchlist.version or '未标注'}"
            f"（{len(watchlist.seats)} 个席位 + {len(watchlist.buckets)} 个桶）"
        ),
        "所有条目均为通道名，非实名账户；名单用于识别席位事实，不构成投资建议。",
    ]
    for seat in watchlist.seats:
        last = seat.last_seen_trade_date or "未知"
        lines.append(
            f"- {seat.name}（{seat.operatedept_code}）：最近一次上榜 {last}，"
            f"历史记录 {seat.record_count:,} 条"
        )
    if watchlist.buckets:
        lines.append("不计入游资命中的匿名汇总桶：")
        lines.extend(f"- {seat.name}：{seat.note}" for seat in watchlist.buckets)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# P2 C04：个股研报 S6 —— 上榜摘要（仅上榜才生成）
# ---------------------------------------------------------------------------

#: S6 证据的来源等级与局限（方案 §九：东财是转载，不是一级源）。
S6_SOURCE_NAME = "东方财富数据中心龙虎榜（转载自沪深交易所公开披露）"
S6_LIMITATION = "转载自东财数据中心，原始披露为沪深交易所；席位为营业部通道，非实名账户。"
S6_LICENSE_STATUS = "local-personal-use"

#: 逐侧展示的席位数（买入前 3 / 卖出前 3）。
S6_TOP_SEATS = 3


def _format_amount(value: float | None) -> str:
    if value is None:
        return "无数据"
    # 亿元/万元 分档，避免长串数字不可读；口径写在文本里。
    if abs(value) >= 1e8:
        return f"{value / 1e8:.2f} 亿元"
    if abs(value) >= 1e4:
        return f"{value / 1e4:.2f} 万元"
    return f"{value:.2f} 元"


def _describe_seat_line(seat: object, match: SeatMatch, direction: str) -> str:
    """渲染一条席位行。

    **实测口径（2026-09-16，务必保留）**：`NET` 是**同一侧的买卖轧差净额**，
    在 BUY 与 SELL 两个 report 上含义相同（都是 buy − sell），**不是**「该侧净额」。
    例：`深股通专用` 同时出现在 000592 的买入与卖出席位里，两行 BUY/SELL/NET 完全相同
    （净买入 1.02 亿）。因此这里按侧展示**该侧金额**，净额单独标为「净额」——
    写成「卖出净额 1.02 亿」会把净买入讲成净卖出。
    """
    name = str(getattr(seat, "operatedept_name", "") or "未知席位")
    net = getattr(seat, "net", None)
    side_amount = getattr(seat, "buy" if direction == "buy" else "sell", None)
    hit = "【观察名单命中】" if match.matched else ""
    if not match.matched and match.reason.startswith("匿名汇总桶"):
        hit = "【匿名汇总桶】"
    side_label = "买入" if direction == "buy" else "卖出"
    net_text = _format_amount(net) if net is not None else "无数据"
    signed = f"{net_text}" if net is None or net >= 0 else f"-{_format_amount(abs(net))}"
    return (
        f"{name}（{side_label} {_format_amount(side_amount) if side_amount is not None else '无数据'}，"
        f"买卖轧差净额 {signed}）{hit}"
    )


def build_billboard_summary(
    security_code: str,
    billboard_rows: list[object],
    seat_rows: list[object],
    watchlist: Watchlist,
    *,
    lookback_days: int = 1,
) -> str | None:
    """组装个股研报 S6 摘要文本。

    **未上榜返回 None**——调用方据此不追加 S6，不生成空表，也不写
    「游资没进」这种负向编造（方案 §4.3、铁律 5）。

    摘要内容：上榜原因、买入前 3 / 卖出前 3 席位、净额、是否命中观察名单。
    """
    code = (security_code or "").strip()
    if not code:
        return None

    mine = [row for row in billboard_rows if str(getattr(row, "security_code", "")) == code]
    if not mine:
        return None

    lines: list[str] = []
    days = sorted({str(getattr(row, "trading_day", "")) for row in mine})
    latest = days[-1] if days else ""
    window = f"近 {lookback_days} 个交易日" if lookback_days > 1 else "当日"
    lines.append(f"{code} 在{window}登上龙虎榜（共 {len(days)} 个交易日、{len(mine)} 条上榜记录）：")

    for row in mine:
        day = str(getattr(row, "trading_day", ""))
        name = str(getattr(row, "security_name", "") or "")
        lines.append(
            f"- {day} {name}：上榜原因「{getattr(row, 'explanation', '') or '未披露'}」，"
            f"买入 {_format_amount(getattr(row, 'billboard_buy_amt', None))}，"
            f"卖出 {_format_amount(getattr(row, 'billboard_sell_amt', None))}，"
            f"净额 {_format_amount(getattr(row, 'billboard_net_amt', None))}"
        )
        # G03：榜单扩展字段（原口径），缺失显式写「无数据」——不拿换手率瞎凑。
        turnover = getattr(row, "turnover_rate", None)
        free_cap = getattr(row, "free_market_cap", None)
        turnover_text = "无数据" if turnover is None else f"{turnover:.2f}%"
        cap_text = "无数据" if free_cap is None else _format_amount(free_cap)
        lines.append(
            f"  - 换手率 {turnover_text}，自由流通市值 {cap_text}"
            "（东财原口径字段；换手率为百分比、市值为元）"
        )
        source_url = str(getattr(row, "source_url", "") or "")
        if source_url:
            lines.append(f"  - 原始披露页：{source_url}")
        note = str(getattr(row, "explain_note", "") or "")
        if note:
            # 数据商口径原文保留说明，但**不**当成我方数值（铁律 6）。
            lines.append(f"  - 数据商原文标注（非本产品口径，不作依据）：{note}")

    hits = 0
    for direction, label in (("buy", "买入"), ("sell", "卖出")):
        side_rows = [r for r in seat_rows if str(getattr(r, "direction", "")) == direction]
        # 按**该侧金额**排序（买入侧看 BUY、卖出侧看 SELL），而不是按 NET：
        # NET 是买卖轧差，用它排卖出席位会把「净买入但也在卖」的席位排到前面。
        amount_attr = "buy" if direction == "buy" else "sell"
        picked = sorted(
            [r for r in side_rows if str(getattr(r, "security_code", "")) == code],
            key=lambda r: (
                getattr(r, amount_attr, None) is None,
                -(getattr(r, amount_attr, 0) or 0),
            ),
        )[:S6_TOP_SEATS]
        if not picked:
            lines.append(f"- {label}前 {S6_TOP_SEATS} 席位：未披露")
            continue
        lines.append(f"- {label}前 {S6_TOP_SEATS} 席位：")
        for seat in picked:
            match = match_seat(
                watchlist,
                operatedept_code=str(getattr(seat, "operatedept_code", "")),
                operatedept_name=str(getattr(seat, "operatedept_name", "")),
            )
            hits += 1 if match.matched else 0
            lines.append(f"  - {_describe_seat_line(seat, match, direction)}")

    if hits:
        lines.append(f"- 观察名单命中 {hits} 条（命中只说明该营业部在名单上，不代表任何买卖建议）。")
    else:
        lines.append("- 观察名单未命中（不表示「游资没进」，只表示本次上榜席位都不在观察名单上）。")

    lines.append("- 口径说明：净额 = 该席位当日买入 − 卖出（两榜同义）；同一席位可能同时出现在买卖两侧。")
    lines.append(f"- 局限：{S6_LIMITATION}")
    if latest:
        lines.append(f"- 数据口径：上榜日为 {latest}，收盘后披露；仅覆盖进入龙虎榜的股票，非全市场。")
    return "\n".join(lines)

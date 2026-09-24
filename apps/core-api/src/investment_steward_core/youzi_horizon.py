"""披露后离散期限（D1/D2/D5/D10/D20/D30）状态与刷新（youzi-radar 二期 Y3-05/06）。

## 口径（Y0-08 已联网核验，2026-09-18）

`RPT_DAILYBILLBOARD_DETAILS` 的 `D{N}_CLOSE_ADJCHRATE` 字段 = **披露日收盘起算**的
**累计**涨跌幅（百分比），不足 N 个交易日时**恒为 null**。核验要点：

- 成熟度 5/5 逐字段精确一致（例：2026-09-16→`{D1:71,D2:0,D5:0,…}`；
  2026-08-14→`{…D1..D20:72,D30:0}`；2026-07-31→`{…D30:77}`）。
- 独立复算：用腾讯前复权日线按「披露日收盘 → 第 N 个后续交易日收盘」复算
  25 行 × D1/D2/D5/D10/D20 → exact 115 / differed 10（复权口径，<0.3pp）/ 无法验证 0。
- **D3 不纳入本版**（Y0-08 决定）。

## 四条不可让步的规则

1. **null 不是 0。** 未成熟/缺失一律 `value_pct=None` + 显式 `status`，绝不填 0
   ——「还没到」和「涨跌 0%」是两件事。
2. **`not_due` 与 `missing` 必须分开。** 前者是「按可信日历还没走够 N 个交易日」，
   后者是「已到期但数据源没给」。两者混同会让使用者以为数据源有问题（或反之）。
3. **缺失不永久负缓存。** `missing`/`unknown` 不进缓存（或只进极短冷却），
   否则一次抖动会把某个期限**永久**钉成缺失。
4. **抓取时间可见。** 每个期限携带 `fetched_at`，旧快照与本次抓取能分辨。

本模块不读系统时钟判断交易日——成熟度只依据**显式传入的可信日历**。
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass, field

#: 本版承诺的期限（D3 不承诺：Y0-08 决定，字段存在但语义未核验）。
HORIZON_DAYS: tuple[int, ...] = (1, 2, 5, 10, 20, 30)

#: 期限状态（互斥，穷尽）：
#: - `disclosed`：数据源已给出该期限的数值。
#: - `not_due`  ：按可信日历，披露日之后**尚未走够** N 个交易日。
#: - `missing`  ：按日历**已到期**，但本次响应里该字段为 null/缺失。
#: - `unknown`  ：连「是否到期」都判断不了（无可信日历或披露日本身不在日历中）。
STATUS_DISCLOSED = "disclosed"
STATUS_NOT_DUE = "not_due"
STATUS_MISSING = "missing"
STATUS_UNKNOWN = "unknown"

#: 数据源字段名模板。
FIELD_TEMPLATE = "D{n}_CLOSE_ADJCHRATE"

#: 缺失/未知结果的短冷却（秒）。**刻意不设长 TTL**：缺失不永久负缓存（规则 3），
#: 但同一请求内反复重试没有意义，故给一个远小于 60s 正缓存的冷却窗口。
MISS_COOLDOWN_SECONDS = 5.0
#: 已披露值的缓存 TTL（秒）——与分页路径口径一致（60s）。
VALUE_TTL_SECONDS = 60.0


class HorizonInputError(ValueError):
    """输入不合法（日期格式、字段类型等）。"""


@dataclass(frozen=True)
class HorizonValue:
    """单个期限的取值与状态（`value_pct` 为百分比数值，非小数）。"""

    label: str  # "D1" / "D2" / ...
    sessions: int  # 1 / 2 / 5 / ...
    value_pct: float | None
    status: str
    #: 到期目标交易日（YYYYMMDD）。`not_due` 时为预计到期日；无法判定时为空串。
    target_date: str
    fetched_at: float
    source: str
    #: 不可用原因（`disclosed` 时为空串）。
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "sessions": self.sessions,
            "value_pct": self.value_pct,
            "status": self.status,
            "target_date": self.target_date,
            "fetched_at": self.fetched_at,
            "source": self.source,
            "reason": self.reason,
        }


def _valid_day(day: object) -> bool:
    return isinstance(day, str) and len(day) == 8 and day.isdigit()


def _finite_or_none(value: object) -> float | None:
    """只接受有限数值；bool / 字符串 / NaN / inf 一律视为缺失（不猜）。"""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def horizon_labels() -> tuple[str, ...]:
    return tuple(f"D{n}" for n in HORIZON_DAYS)


def target_day(
    disclosure_day: str, sessions: int, trading_days: list[str] | tuple[str, ...]
) -> str:
    """披露日后第 `sessions` 个交易日的日期；不足则返回空串（不推算自然日）。

    `trading_days` 是**显式**的可信交易日序列（升序）。披露日本身不在序列里时
    返回空串——那属于 `unknown`，不能默认它是序列首日。
    """
    if not _valid_day(disclosure_day):
        raise HorizonInputError(f"披露日格式不合法（应为 YYYYMMDD）：{disclosure_day!r}")
    if not isinstance(sessions, int) or isinstance(sessions, bool) or sessions < 1:
        raise HorizonInputError(f"sessions 必须为正整数：{sessions!r}")
    if disclosure_day not in trading_days:
        return ""
    index = list(trading_days).index(disclosure_day)
    target = index + sessions
    if target >= len(trading_days):
        return ""
    return str(trading_days[target])


def resolve_horizons(
    disclosure_day: str,
    row: dict[str, object],
    trading_days: list[str] | tuple[str, ...] | None,
    *,
    calendar_available: bool,
    as_of_trading_day: str = "",
    fetched_at: float | None = None,
    source: str = "RPT_DAILYBILLBOARD_DETAILS",
) -> tuple[HorizonValue, ...]:
    """把一行榜单数据的六个期限字段解析成带状态的结构。

    状态判定（严格按上表，不猜测）：

    - 字段有有限数值 → `disclosed`（附带到期目标日，能算就算，算不出留空串）。
    - 字段为 null/非法 + **无可信日历**（`calendar_available=False` 或序列为空）
      → `unknown`（我判断不了是否到期）。
    - 字段为 null/非法 + 披露日**不在**日历里 → `unknown`。
    - 字段为 null/非法 + 披露日之后**不足 N 个交易日**（含 as_of 截断后仍不足）
      → `not_due`，并给出**预计**到期日（序列内能算到就给，算不到留空串）。
    - 其余（已到期但字段仍为 null）→ `missing`。

    `as_of_trading_day` 非空时，只考虑 `<= as_of` 的交易日——用于「按历史时点
    复盘」：即使数据源今天已经补齐了后续期限，也不能把未来交易日当作已发生。
    """
    if not _valid_day(disclosure_day):
        raise HorizonInputError(f"披露日格式不合法（应为 YYYYMMDD）：{disclosure_day!r}")
    stamp = time.time() if fetched_at is None else float(fetched_at)
    #: 截至 `as_of` 的可信交易日序列。★ 目标日与到期判定都只用它：
    #: `as_of` 之后的交易日**不是已知事实**，若拿它算目标日就等于用未来信息
    #: 反推当下（Y0-08 的到期分界会整体前移）。因此目标日严格 ≤ `as_of`，
    #: 超出即留空串——空串表示「按此刻还不知道那一天是哪天」，不是「没有期限」。
    days: tuple[str, ...] = tuple(trading_days or ())
    if as_of_trading_day:
        if not _valid_day(as_of_trading_day):
            raise HorizonInputError(f"as_of 格式不合法（应为 YYYYMMDD）：{as_of_trading_day!r}")
        days = tuple(day for day in days if day <= as_of_trading_day)

    known_calendar = bool(calendar_available) and bool(days) and disclosure_day in days

    out: list[HorizonValue] = []
    for sessions in HORIZON_DAYS:
        label = f"D{sessions}"
        target = target_day(disclosure_day, sessions, days) if known_calendar else ""
        raw = row.get(FIELD_TEMPLATE.format(n=sessions))
        value = _finite_or_none(raw)

        if value is not None:
            # 已披露：有值就是有值。目标日算不出来也不影响「已披露」这个事实。
            out.append(
                HorizonValue(
                    label=label,
                    sessions=sessions,
                    value_pct=value,
                    status=STATUS_DISCLOSED,
                    target_date=target,
                    fetched_at=stamp,
                    source=source,
                )
            )
            continue

        if not known_calendar:
            # 无可信日历，或披露日不在日历里 → 连「是否到期」都不知道。
            out.append(
                HorizonValue(
                    label=label,
                    sessions=sessions,
                    value_pct=None,
                    status=STATUS_UNKNOWN,
                    target_date="",
                    fetched_at=stamp,
                    source=source,
                    reason=(
                        "calendar_missing"
                        if not calendar_available or not days
                        else "disclosure_day_not_in_calendar"
                    ),
                )
            )
            continue

        if not target:
            # 已到期判定：披露日之后序列里还有没有第 N 天，区分 not_due / missing。
            index = list(days).index(disclosure_day)
            remaining = len(days) - index - 1
            if remaining < sessions:
                out.append(
                    HorizonValue(
                        label=label,
                        sessions=sessions,
                        value_pct=None,
                        status=STATUS_NOT_DUE,
                        target_date="",
                        fetched_at=stamp,
                        source=source,
                        reason=f"only_{remaining}_trading_days_elapsed",
                    )
                )
            else:
                # 序列里明明够 N 天却算不出目标日——属实现矛盾，不冒充 missing。
                out.append(
                    HorizonValue(
                        label=label,
                        sessions=sessions,
                        value_pct=None,
                        status=STATUS_UNKNOWN,
                        target_date="",
                        fetched_at=stamp,
                        source=source,
                        reason="target_day_unresolvable",
                    )
                )
            continue

        # 目标日在日历内 → 已到期；字段仍为 null → 数据源缺失。
        out.append(
            HorizonValue(
                label=label,
                sessions=sessions,
                value_pct=None,
                status=STATUS_MISSING,
                target_date=target,
                fetched_at=stamp,
                source=source,
                reason="field_null_after_due",
            )
        )
    return tuple(out)


@dataclass
class HorizonCache:
    """期限取值的**有界**缓存，区分正缓存与缺失冷却（Y3-06 规则 3）。

    - `disclosed` 值按 `value_ttl` 缓存（默认与分页口径一致的 60s）。
    - 非 `disclosed` 结果只按 `miss_cooldown`（默认 5s）短冷却，**不长期负缓存**：
      一次抖动不能把某个期限永久钉成缺失。
    - 容量有硬上限；超限按插入序淘汰最旧条目（`entries` 为插入有序 dict）。
    """

    value_ttl: float = VALUE_TTL_SECONDS
    miss_cooldown: float = MISS_COOLDOWN_SECONDS
    max_entries: int = 256
    _store: dict[str, tuple[float, tuple[HorizonValue, ...]]] = field(default_factory=dict)
    _inserted: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for name in ("value_ttl", "miss_cooldown"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise HorizonInputError(f"{name} 必须为非负数：{value!r}")
        if not isinstance(self.max_entries, int) or isinstance(self.max_entries, bool):
            raise HorizonInputError(f"max_entries 必须为整数：{self.max_entries!r}")
        if self.max_entries < 1:
            raise HorizonInputError(f"max_entries 至少为 1：{self.max_entries!r}")

    @staticmethod
    def key(disclosure_day: str, security_code: str, as_of: str, inputs_fingerprint: str = "") -> str:
        """缓存键 = 披露日 + 证券 + `as_of` + **输入指纹**。

        ★ 为什么必须带 `inputs_fingerprint`：期限值不只取决于这三个坐标，还取决于
        （a）该行六个期限字段的当前取值、（b）可信交易日序列。上游补数会让同一
        (披露日, 证券, as_of) 的结论**变化**；若只按三坐标做键，缓存会把旧结论
        当成新结论返回（实测：同一 key 先以「已披露」写入，随后同 key 应当是
        「未到期」的请求被缓存短路，直接给出错误状态）。
        """
        base = f"{disclosure_day}|{security_code}|{as_of}"
        return f"{base}|{inputs_fingerprint}" if inputs_fingerprint else base

    @staticmethod
    def fingerprint(row: dict[str, object], trading_days: object) -> str:
        """把「决定期限结论的输入」压成短指纹。

        只取真正参与判定的量：六个 `D{N}_CLOSE_ADJCHRATE` 字段 + 截断后的交易日
        序列。这样上游补数或日历变化会自动换键，而无关字段的变动不会白白失效。
        """
        fields = [repr(row.get(FIELD_TEMPLATE.format(n=n))) for n in HORIZON_DAYS]
        days = "".join(str(day) for day in (trading_days or ()))
        digest = hashlib.sha256(("\u0001".join([*fields, days])).encode("utf-8")).hexdigest()
        return digest[:16]
    def get(
        self,
        disclosure_day: str,
        security_code: str,
        as_of: str = "",
        inputs_fingerprint: str = "",
        *,
        now: float | None = None,
    ) -> tuple[HorizonValue, ...] | None:
        key = self.key(disclosure_day, security_code, as_of, inputs_fingerprint)
        entry = self._store.get(key)
        if entry is None:
            return None
        stamp, values = entry
        current = time.time() if now is None else float(now)
        # 全部已披露 → 走正缓存 TTL；只要有任一期限缺失/未到期 → 走短冷却。
        # 这里必须用**同一时钟域**比较：`put` 记录的时刻也由调用方传入（或取
        # `time.time()`），否则注入 `now` 的测试与真实运行会得到不同结论。
        ttl = (
            self.value_ttl
            if all(v.status == STATUS_DISCLOSED for v in values)
            else self.miss_cooldown
        )
        if current - stamp >= ttl:
            self._store.pop(key, None)
            return None
        return values

    def put(
        self,
        disclosure_day: str,
        security_code: str,
        values: tuple[HorizonValue, ...],
        as_of: str = "",
        inputs_fingerprint: str = "",
        *,
        now: float | None = None,
    ) -> None:
        key = self.key(disclosure_day, security_code, as_of, inputs_fingerprint)
        stamp = time.time() if now is None else float(now)
        if key not in self._store:
            self._inserted.append(key)
        self._store[key] = (stamp, values)
        while len(self._inserted) > self.max_entries:
            oldest = self._inserted.pop(0)
            self._store.pop(oldest, None)

    def clear(self) -> None:
        self._store.clear()
        self._inserted.clear()

    def __len__(self) -> int:
        return len(self._store)

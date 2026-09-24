"""E2 · 统一标的身份：Instrument 规范化层。

全仓统一标识由单个规范的 `InstrumentIdentity` 决定（`key` + `kind` + `market`），
丢给研究/行情/持仓的任意形态都先归一化，避免同一标的多写形态（如 "510300"、"sh510300"、
"SH.510300"、"510300.sh"）被当成不同实体。

规则（确定性、纯 stdlib、无外部数据）：
- 去分隔符并转大写 → 取尾部连续 6 位数字为主码；
- 6 位 A股/场内基金码：按首段约定导出 kind（股票/ETF/分级/其他）与市场（sh/sz/bj）；
- 无法解析 → kind=unknown、market=none（仍给出规范 key，不丢弃）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 常见写法：DIGIT6、{market}{digit6}、{digit6}.{market}、{market}.{digit6}
_DIGITS = re.compile(r"(\d{6})")

# 首两位 → (kind, market)：覆盖常见的股票 / 场内 ETF / 场外基金参考码。
_CN_STOCKS = {"60", "68", "90"}
_CN_SZ_STOCKS = {"00", "30", "10"}
_CN_BJ_STOCKS = {"83", "87", "43", "92"}
# 场内基金按真实上市地：51/56/58/50 → 沪市（510300 等沪市 ETF），15/16/18 → 深市（159xxx 等深市 ETF）。
_ETF_SH = {"51", "56", "58", "50"}
_ETF_SZ = {"15", "16", "18"}
_ETF_PREFIX = _ETF_SH | _ETF_SZ


@dataclass(frozen=True)
class InstrumentIdentity:
    key: str  # 规范主码（6 位大写）
    kind: str  # stock / etf / other / unknown
    market: str  # sh / sz / bj / none
    display: str  # 人类可读：6 位码

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "kind": self.kind, "market": self.market, "display": self.display}


def _market_for(kind: str, prefix: str) -> str:
    if prefix.startswith(("6", "9")):
        return "sh"
    if prefix.startswith(("0", "3", "1", "2")):
        return "sz"
    if prefix.startswith(("8", "4")):
        return "bj"
    return "none"


def normalize_instrument(symbol: str | None) -> InstrumentIdentity:
    """把任意常见形态归一为统一标的身份；空/无法解析 → `key=""` 的 unknown。"""
    raw = (symbol or "").strip().upper().replace(" ", "")
    # 规范化优先取最后一组 6 位数字，兼容 CN:ETF:510300 和
    # 含前置年份/账户数字的历史 subject_ref。
    matches = list(_DIGITS.finditer(raw))
    match = matches[-1] if matches else None
    if match is None:
        return InstrumentIdentity(key="", kind="unknown", market="none", display="")
    code = match.group(1)
    prefix = code[:2]
    if prefix in _CN_STOCKS:
        kind, market = "stock", "sh"
    elif prefix in _CN_SZ_STOCKS:
        kind, market = "stock", "sz"
    elif prefix in _CN_BJ_STOCKS:
        kind, market = "stock", "bj"
    elif prefix in _ETF_PREFIX:
        kind = "etf"
        market = "sh" if prefix in _ETF_SH else "sz"
    else:
        kind = "other"
        market = _market_for("other", prefix)
    return InstrumentIdentity(key=code, kind=kind, market=market, display=code)


def canonical_key(symbol: str | None) -> str:
    """快捷取规范 key（写入边界统一标识用）。"""
    return normalize_instrument(symbol).key


def subject_matches_instrument(subject_ref: str, instrument: str | None) -> bool:
    """判断证据 subject 是否指向同一规范标的。

    subject_refs 历史上同时出现过 `instrument:` 前缀、市场前缀和
    `CN:ETF:<code>` 形式，因此匹配必须经过同一 canonical_key 层。
    """
    if not instrument:
        return False
    raw = subject_ref.strip()
    if raw.lower().startswith("instrument:"):
        raw = raw.split(":", 1)[1]
    target = canonical_key(instrument)
    return bool(target) and canonical_key(raw) == target

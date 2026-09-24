"""中金所股指期货日频席位内核（纯 stdlib，零新增依赖）。

对齐 `news_evidence.py` / `market_feed.py` 的既有约定：三次重试 + 退避 + UA + 模块级
TTL 缓存 + 失败冷却；取数失败抛 `CffexError` 由调用方**显式降级**，不返回空壳、
不生成任何证据、更不编造席位数字。

边界（ADR-0004 同源）：本模块只取中金所**公开日频统计与会员排名**，对外分发口径
统一为「公开披露的二次整理」，不宣称官方授权。

## 三条由真实数据决定的口径（2026-09-16 实测，见 tests/fixtures/youzi/README.md）

1. **`index.xml` 不是只有股指。** 其中同时包含期权（`productid=MO`）与国债期货
   （`T`/`TF`/`TL`/`TS`）。因此**必须**按 `CFFEX_INDEX_PRODUCTS` 白名单过滤，
   否则会把期权行当股指写进方向研判证据。
2. **`datatypeid` 有三个口径，不可混用。** `0` 成交量 / `1` 持买单量 / `2` 持卖单量。
   实测同一天同一席位在三者下数字不同（中信期货(代客)：成交量口径 24,673，
   持买单量口径 10,979），证据文本必须写明取自哪个 `datatypeid`。
3. **`quote_IF.txt` 不可用。** 实测该路径返回 HTML 错误页（「网页错误」），
   本模块不依赖它，只走 `index.xml` + `ccpm/*.xml`。
"""

from __future__ import annotations

import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date

from investment_steward_core import feed_health

logger = logging.getLogger(__name__)

# —— 冻结常量（P0 A03：单一来源，模块内不得有第二处硬编码） ——

#: 只保留股指期货四品种；期权（MO/HO/IO）与国债期货（T/TF/TL/TS）一律过滤掉。
CFFEX_INDEX_PRODUCTS: tuple[str, ...] = ("IF", "IH", "IC", "IM")

#: datatypeid → 中文口径标签。顺序即证据文本中的展示顺序。
CFFEX_DATATYPE_LABELS: dict[str, str] = {
    "0": "成交量",
    "1": "持买单量",
    "2": "持卖单量",
}
CFFEX_DATATYPE_VOLUME = "0"
CFFEX_DATATYPE_LONG = "1"
CFFEX_DATATYPE_SHORT = "2"

#: 股指期货品种 → 对应现货指数代码（基差前置条件）。
#: 只有本机已有该指数数据时才计算基差；没有就如实写「基差未计算」，不拿别的指数冒充。
CFFEX_INDEX_SPOT_MAP: dict[str, str] = {
    "IF": "000300",  # 沪深 300
    "IH": "000016",  # 上证 50
    "IC": "000905",  # 中证 500
    "IM": "000852",  # 中证 1000
}

#: 会员排名每品种取前 N 名（中金所公开披露口径）。
CFFEX_RANK_TOP_N = 20

#: 每个品种取**近月 + 次近月**两个合约（方案 §4.1 原话）。
#: 「近月」= 合约月份最近的一个，按 `PRODUCT + YYMM` 的 YYMM 升序判定，**不是**按持仓量判定。
#: 另单独给出「持仓量最高合约」，两者用途不同、标签也必须不同（见 CffexProductDay）。
CFFEX_LEAD_CONTRACT_COUNT = 2

_INDEX_URL_TEMPLATE = "http://www.cffex.com.cn/sj/hqsj/rtj/{yyyymm}/{dd}/index.xml"
_CCPM_URL_TEMPLATE = "http://www.cffex.com.cn/sj/ccpm/{yyyymm}/{dd}/{product}.xml"

_USER_AGENT = "InvestmentSteward/0.1"
_REQUEST_TIMEOUT = 20
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = 1.0

_CACHE_TTL_SECONDS = 300.0
_FAIL_COOLDOWN_SECONDS = 300.0
_cache: dict[str, tuple[float, str]] = {}
_fail_cache: dict[str, float] = {}

_DATA_BLOCK = re.compile(r"<data\b[^>]*>(.*?)</data>", re.DOTALL)
_DAILY_BLOCK = re.compile(r"<dailydata\b[^>]*>(.*?)</dailydata>", re.DOTALL)


class CffexError(RuntimeError):
    """中金所取数或结构异常，应显式降级而非中断调用方，更不应用占位数据填充。"""


# —— 数据类型 ——


@dataclass(frozen=True)
class CffexContract:
    """`index.xml` 中的单个合约日统计行。空字段一律为 None，不填 0 冒充。"""

    instrument_id: str
    product_id: str
    trading_day: str
    close: float | None
    settlement: float | None
    pre_settlement: float | None
    volume: int | None
    open_interest: int | None
    pre_open_interest: int | None

    @property
    def open_interest_change(self) -> int | None:
        """持仓量变化；任一缺失则为 None（不假设昨仓为 0）。"""
        if self.open_interest is None or self.pre_open_interest is None:
            return None
        return self.open_interest - self.pre_open_interest


@dataclass(frozen=True)
class CffexRankRow:
    """`ccpm/{PRODUCT}.xml` 中的单个会员排名行。"""

    product_id: str
    instrument_id: str
    trading_day: str
    datatype_id: str
    rank: int
    short_name: str
    volume: int
    volume_change: int
    party_id: str

    @property
    def datatype_label(self) -> str:
        return CFFEX_DATATYPE_LABELS.get(self.datatype_id, f"未知口径({self.datatype_id})")


@dataclass(frozen=True)
class CffexProductDay:
    """单品种当日的汇总视图。

    `near_contracts` 与 `open_interest_leader` 是**两个不同口径的字段**，刻意不合并：

    - `near_contracts`：近月 + 次近月，按合约月份的 YYMM 升序。
    - `open_interest_leader`：持仓量最高的合约。

    实测两者经常不是同一个（2026-09-16 IF：近月 IF2609 持仓 57,081，
    而 IF2612 持仓 145,927）。把它们混成一个「主力合约」字段，就是在替用户下
    一个产品不该下的判断。
    """

    product_id: str
    near_contracts: tuple[CffexContract, ...]
    open_interest_leader: CffexContract | None
    total_volume: int | None
    total_open_interest: int | None
    contract_count: int
    spot_index_code: str


@dataclass(frozen=True)
class CffexSnapshot:
    """一次中金所席位快照（供方向研判证据包与插件 UI 共用）。"""

    trading_day: str
    products: tuple[CffexProductDay, ...]
    limitations: tuple[str, ...] = ()
    basis: dict[str, float] = field(default_factory=dict)
    basis_note: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.products


# —— 解析（纯函数，夹具可离线测试） ——


def _tag_text(block: str, tag: str) -> str:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.DOTALL)
    return match.group(1).strip() if match else ""


def _float_or_none(raw: str) -> float | None:
    text = raw.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _int_or_none(raw: str) -> int | None:
    value = _float_or_none(raw)
    if value is None:
        return None
    try:
        return int(value)
    except (OverflowError, ValueError):
        return None


def _int_or_zero(raw: str) -> int:
    value = _int_or_none(raw)
    return value if value is not None else 0


def normalise_trading_day(trading_day: str | date) -> str:
    """归一化为 `YYYYMMDD`；入参也接受 `YYYY-MM-DD` 与 `date`。"""
    if isinstance(trading_day, date):
        return trading_day.strftime("%Y%m%d")
    text = trading_day.strip().replace("-", "").replace("/", "")
    if len(text) != 8 or not text.isdigit():
        raise ValueError(f"交易日格式不合法：{trading_day!r}（应为 YYYYMMDD）")
    return text


def parse_index_contracts(xml_text: str) -> list[CffexContract]:
    """解析 `index.xml`，**按品种白名单过滤**，只返回股指期货合约。

    期权与国债期货行在此处被丢弃——这是本函数存在的首要理由，
    调用方无需（也不应）再各自过滤一遍。
    """
    contracts: list[CffexContract] = []
    for raw in _DAILY_BLOCK.findall(xml_text):
        product_id = _tag_text(raw, "productid")
        if product_id not in CFFEX_INDEX_PRODUCTS:
            continue
        instrument_id = _tag_text(raw, "instrumentid")
        if not instrument_id:
            continue
        contracts.append(
            CffexContract(
                instrument_id=instrument_id,
                product_id=product_id,
                trading_day=_tag_text(raw, "tradingday"),
                close=_float_or_none(_tag_text(raw, "closeprice")),
                settlement=_float_or_none(_tag_text(raw, "settlementprice")),
                pre_settlement=_float_or_none(_tag_text(raw, "presettlementprice")),
                volume=_int_or_none(_tag_text(raw, "volume")),
                open_interest=_int_or_none(_tag_text(raw, "openinterest")),
                pre_open_interest=_int_or_none(_tag_text(raw, "preopeninterest")),
            )
        )
    return contracts


def parse_ranking(xml_text: str, product_id: str) -> list[CffexRankRow]:
    """解析 `ccpm/{PRODUCT}.xml`，返回该品种的全部会员排名行。

    行内已带 `productid`；若与请求品种不一致则丢弃（防御串档）。
    """
    rows: list[CffexRankRow] = []
    for raw in _DATA_BLOCK.findall(xml_text):
        row_product = _tag_text(raw, "productid") or product_id
        if row_product != product_id:
            continue
        short_name = _tag_text(raw, "shortname")
        if not short_name:
            continue
        rows.append(
            CffexRankRow(
                product_id=row_product,
                instrument_id=_tag_text(raw, "instrumentid"),
                trading_day=_tag_text(raw, "tradingday"),
                datatype_id=_tag_text(raw, "datatypeid"),
                rank=_int_or_zero(_tag_text(raw, "rank")),
                short_name=short_name,
                volume=_int_or_zero(_tag_text(raw, "volume")),
                volume_change=_int_or_zero(_tag_text(raw, "varvolume")),
                party_id=_tag_text(raw, "partyid"),
            )
        )
    return rows


_CONTRACT_SUFFIX = re.compile(r"^([A-Z]+)(\d{3,4})$")


def contract_month_key(instrument_id: str) -> tuple[int, str]:
    """从合约代码解析月份排序键。

    中金所格式为 `品种代码 + YYMM`（如 `IF2609` = 2026-09）。
    解析不出来时排到最后并用原字符串兜底，**不猜月份**。

    `2609` 这类 YYMM 直接按整数比较即可得到正确先后（YY 递增则 MM 也单调），
    无需还原世纪——这里刻意不做「20xx 还是 19xx」的推断。
    """
    match = _CONTRACT_SUFFIX.match(instrument_id.strip().upper())
    if match is None:
        return (10**9, instrument_id)
    return (int(match.group(2)), instrument_id)


def select_near_contracts(
    contracts: list[CffexContract], count: int = CFFEX_LEAD_CONTRACT_COUNT
) -> tuple[CffexContract, ...]:
    """按**合约月份**升序取近月 + 次近月（方案 §4.1 原话）。"""
    ordered = sorted(contracts, key=lambda item: contract_month_key(item.instrument_id))
    return tuple(ordered[:count])


def select_open_interest_leader(contracts: list[CffexContract]) -> CffexContract | None:
    """持仓量最高的合约。持仓量全缺失时返回 None（不猜）。"""
    with_oi = [item for item in contracts if item.open_interest is not None]
    if not with_oi:
        return None
    return max(with_oi, key=lambda item: item.open_interest or 0)


def build_product_day(contracts: list[CffexContract], product_id: str) -> CffexProductDay | None:
    """把某品种的全部合约汇总为当日视图；该品种无合约时返回 None（如实缺项）。"""
    mine = [item for item in contracts if item.product_id == product_id]
    if not mine:
        return None
    volumes = [item.volume for item in mine if item.volume is not None]
    interests = [item.open_interest for item in mine if item.open_interest is not None]
    return CffexProductDay(
        product_id=product_id,
        near_contracts=select_near_contracts(mine),
        open_interest_leader=select_open_interest_leader(mine),
        total_volume=sum(volumes) if volumes else None,
        total_open_interest=sum(interests) if interests else None,
        contract_count=len(mine),
        spot_index_code=CFFEX_INDEX_SPOT_MAP.get(product_id, ""),
    )


def build_snapshot(
    contracts: list[CffexContract],
    *,
    spot_closes: dict[str, float] | None = None,
    trading_day: str = "",
) -> CffexSnapshot:
    """汇总为快照。

    基差处理（B04）：**仅当**调用方提供了对应现货指数收盘价时才计算；
    否则 `basis` 为空并给出「基差未计算」说明。本函数不会自己去抓指数行情——
    指数历史接口不稳定（实测 `push2his` 主机连续请求后拒连），
    把「拿不到」如实说出来，好过为凑齐字段去引一个不可靠依赖。
    """
    products = tuple(
        item
        for product in CFFEX_INDEX_PRODUCTS
        if (item := build_product_day(contracts, product)) is not None
    )
    missing = [p for p in CFFEX_INDEX_PRODUCTS if p not in {i.product_id for i in products}]

    limitations: list[str] = [
        "会员排名为交易所公开披露的会员/客户汇总口径，前 20 名，非全市场。",
        "「代客」是客户持仓的汇总通道，不等于该会员自营观点，也不指向某个实名账户。",
        "日数据收盘后才完整；盘中与午盘不做当日统计。",
        "合约展示分两个口径：近月+次近月按合约月份，持仓量最高合约按持仓量；两者经常不是同一个，本产品不认定「主力合约」。",
    ]
    if missing:
        limitations.append(f"当日未取得以下品种数据：{'、'.join(missing)}（如实缺项，未用其他品种代替）。")

    basis: dict[str, float] = {}
    if spot_closes:
        for product in products:
            spot = spot_closes.get(product.spot_index_code)
            lead = product.open_interest_leader
            if spot is None or lead is None or lead.close is None:
                continue
            basis[product.product_id] = lead.close - spot

    if not spot_closes:
        basis_note = "基差未计算：本机未取得对应现货指数收盘价（不调用外部指数行情，不拿其他指数冒充）。"
    elif len(basis) < len(products):
        basis_note = "部分品种基差未计算：对应现货指数或合约收盘价缺失，缺失项如实留空。"
    else:
        basis_note = "基差 = 持仓量最高合约收盘价 - 对应现货指数收盘价（合约口径指明为持仓量最高，非近月）。"

    return CffexSnapshot(
        trading_day=trading_day or (contracts[0].trading_day if contracts else ""),
        products=products,
        limitations=tuple(limitations),
        basis=basis,
        basis_note=basis_note,
    )


# —— 取数（薄封装：TTL 缓存 + 失败冷却 + 重试） ——


def _decode_body(payload: bytes) -> str:
    """中金所响应体解码：先 UTF-8，解不动再退 gb18030。

    正常日数据是 UTF-8 的 XML；但**文件缺失时返回的是 gb2312 的 HTML 错误页**
    （实测 `http://www.cffex.com.cn/sj/hqsj/rtj/202609/17/index.xml` → HTTP 200、
    `Content-Type: text/html`、`<meta ... charset=gb2312>`、`<title>网页错误</title>`、
    无 `<instrument>` 节点）。此处必须先退回 gb18030（gb2312 的超集）再交给
    下面的 HTML 判定——否则 `UnicodeDecodeError` 会抢在前面抛出，把本该清晰的
    「HTML 错误页（该交易日文件不可用）」淹没成
    `'utf-8' codec can't decode byte 0xcd in position 252` 这种对使用者无意义的栈。
    """
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("gb18030", errors="replace")


def _http_get_text(url: str) -> str:
    last_error: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            # F02：逐次尝试埋点（含重试的第几次）；HTML 错误页也计入失败（它是采集失败，
            # 不是"成功拿到一个页面"）。
            with feed_health.attempt(
                feed_health.SOURCE_CFFEX, url, attempt_index=attempt + 1
            ):
                with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT) as response:
                    payload = response.read()
                text = _decode_body(payload)
                # 中金所在缺失/异常时会返回 HTML 错误页而不是 404；识别出来当失败处理。
                # 实测（2026-09-17）真实错误页里 `<html` 落在第 123 个字符，本来就在 200 窗口内
                # ——所以这段判定**不是**失效点，失效点只在上面那句被写死的 decode。
                # 这里顺手放宽到 600 只是防御其他形状的错误页，不改变本次修复的性质。
                if "<html" in text[:600].lower():
                    raise CffexError("中金所返回 HTML 错误页（该交易日文件不可用）")
                return text
        except Exception as error:  # noqa: BLE001 - 网络抖动统一重试
            last_error = error
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_BACKOFF_SECONDS * (attempt + 1))
    raise CffexError(f"中金所请求失败：{url}（{last_error}）")


def _fetch_cached(url: str) -> str:
    cache_key = url
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    fail_at = _fail_cache.get(cache_key)
    if fail_at and now - fail_at < _FAIL_COOLDOWN_SECONDS:
        raise CffexError(f"中金所数据源近期失败，处于冷却期：{url}")

    try:
        text = _http_get_text(url)
    except CffexError:
        _fail_cache[cache_key] = time.monotonic()
        raise
    _cache[cache_key] = (time.monotonic(), text)
    return text


def _day_path(trading_day: str | date) -> tuple[str, str]:
    normalised = normalise_trading_day(trading_day)
    return normalised[:6], normalised[6:]


def fetch_index_contracts(trading_day: str | date) -> list[CffexContract]:
    """拉取并解析某交易日的股指期货合约日统计。失败抛 `CffexError`。"""
    yyyymm, dd = _day_path(trading_day)
    url = _INDEX_URL_TEMPLATE.format(yyyymm=yyyymm, dd=dd)
    contracts = parse_index_contracts(_fetch_cached(url))
    if not contracts:
        raise CffexError(f"中金所 {trading_day} 未返回任何股指期货合约数据")
    return contracts


def fetch_ranking(trading_day: str | date, product: str) -> list[CffexRankRow]:
    """拉取并解析某交易日、某品种的会员排名。失败抛 `CffexError`。"""
    if product not in CFFEX_INDEX_PRODUCTS:
        raise ValueError(f"不支持的品种：{product}（仅支持 {'/'.join(CFFEX_INDEX_PRODUCTS)}）")
    yyyymm, dd = _day_path(trading_day)
    url = _CCPM_URL_TEMPLATE.format(yyyymm=yyyymm, dd=dd, product=product)
    rows = parse_ranking(_fetch_cached(url), product)
    if not rows:
        raise CffexError(f"中金所 {trading_day} {product} 未返回会员排名数据")
    return rows


def ranking_top(
    rows: list[CffexRankRow], datatype_id: str, top_n: int = CFFEX_RANK_TOP_N
) -> list[CffexRankRow]:
    """取某口径下排名前 N 行（按 rank 升序，rank 缺失的排在最后）。"""
    filtered = [row for row in rows if row.datatype_id == datatype_id]
    ordered = sorted(filtered, key=lambda row: (row.rank <= 0, row.rank))
    return ordered[:top_n]


def fetch_snapshot(
    trading_day: str | date, *, spot_closes: dict[str, float] | None = None
) -> CffexSnapshot:
    """一步取到快照。缺少当日文件时抛 `CffexError`，由调用方记为 `source_errors`。"""
    contracts = fetch_index_contracts(trading_day)
    return build_snapshot(
        contracts,
        spot_closes=spot_closes,
        trading_day=normalise_trading_day(trading_day),
    )


def try_fetch_snapshot(
    trading_day: str | date, *, spot_closes: dict[str, float] | None = None
) -> tuple[CffexSnapshot | None, str]:
    """降级友好的取数：(快照, 错误说明)。

    成功返回 `(snapshot, "")`；失败返回 `(None, 原因)` 而不抛异常。
    调用方（方向研判证据包）拿到 `None` 时应记入 `source_errors` 并**照常产出其余来源**，
    绝不为补齐字段而编造席位数字。
    """
    try:
        return fetch_snapshot(trading_day, spot_closes=spot_closes), ""
    except (CffexError, ValueError) as error:
        logger.info("中金所席位取数降级：%s", error)
        return None, str(error)


def reset_caches() -> None:
    """清空模块级缓存（测试用；与 `valuation_evidence.reset_cooldowns` 同义）。"""
    _cache.clear()
    _fail_cache.clear()


# —— 证据文本（供方向研判证据包与插件 UI 共用） ——


def _format_int(value: int | None) -> str:
    return "无数据" if value is None else f"{value:,}"


def _format_price(value: float | None) -> str:
    return "无数据" if value is None else f"{value:g}"


def _format_change(value: int | None) -> str:
    if value is None:
        return ""
    return f"({value:+,})"


def describe_snapshot(snapshot: CffexSnapshot) -> str:
    """渲染为中金所席位证据文本。

    文本里每个数字都带口径；缺失写「无数据」而不是 0；局限逐条列出。
    """
    lines: list[str] = []
    day = snapshot.trading_day or "（交易日未知）"
    lines.append(f"中金所股指期货席位统计（交易日 {day}，公开日频披露，收盘后完整）")

    if snapshot.is_empty:
        lines.append("- 当日未取得任何股指期货品种数据（如实缺项，未用其他品种代替）。")
    for product in snapshot.products:
        lines.append(
            f"- {product.product_id}（对应现货 {product.spot_index_code or '未映射'}）："
            f"合约 {product.contract_count} 个，"
            f"成交合计 {_format_int(product.total_volume)} 手，"
            f"持仓合计 {_format_int(product.total_open_interest)} 手"
        )
        for label, contract in _iter_contract_lines(product):
            lines.append(
                f"  - {label}：收盘 {_format_price(contract.close)}，"
                f"结算 {_format_price(contract.settlement)}，"
                f"成交 {_format_int(contract.volume)} 手，"
                f"持仓 {_format_int(contract.open_interest)} 手"
                f"{_format_change(contract.open_interest_change)}"
            )
        if not product.near_contracts and product.open_interest_leader is None:
            lines.append("  - 无合约明细（持仓量口径缺失，未按其他口径替代）。")

    if snapshot.basis:
        for product_id, value in snapshot.basis.items():
            lines.append(f"- {product_id} 基差（持仓量最高合约 - 现货）：{value:g}")
    lines.append(f"- 基差口径：{snapshot.basis_note}")

    lines.append("- 局限：")
    lines.extend(f"  - {item}" for item in snapshot.limitations)
    return "\n".join(lines)


def _iter_contract_lines(product: CffexProductDay) -> list[tuple[str, CffexContract]]:
    """列出该品种要展示的合约及其**口径标签**，同一合约只出现一次。"""
    rows: list[tuple[str, CffexContract]] = []
    seen: set[str] = set()
    for index, contract in enumerate(product.near_contracts):
        label = "近月" if index == 0 else f"次近月（第 {index + 1} 近）"
        rows.append((label, contract))
        seen.add(contract.instrument_id)
    leader = product.open_interest_leader
    if leader is not None and leader.instrument_id not in seen:
        rows.append(("持仓量最高", leader))
    return rows

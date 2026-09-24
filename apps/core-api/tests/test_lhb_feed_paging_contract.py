"""Synthetic offline tests for the Y2-07 paging path in `lhb_feed`.

钉住一条**上游硬契约**，它曾经把整个跨日事实链路静默打空：

`sortColumns` 与 `sortTypes` 是**两个等长逗号串**。旧实现写死 `sortTypes: 1`
（"单个方向值"），于是带两列排序的两个席位报告一律被上游拒绝（code=9501
"排序字段和顺序数量不一致"）→ `PagingKernel` 判 `upstream_error` →
`coverage=fetch_failed` → 事件一条都生成不出来。

这些测试**不打网络**：`PagingKernel` 的 transport 被替换成受控函数，因此断言的是
「我方发出的 query 长什么样、以及上游按真实规则拒绝时会怎样降级」，而不是
「今天东财是否在线」。
"""

from __future__ import annotations

from urllib.parse import parse_qs

import pytest
from investment_steward_core import lhb_feed, youzi_paging

#: ★ 2026-09-18 实测（`RPT_BILLBOARD_DAILYDETAILSBUY`，`TRADE_DATE='2026-08-14'`）：
#: `sortTypes` 必须是「数字串、与列数等长」的形式；下面两种都是上游真实拒绝过的形状。
_REJECTED_SORT_TYPES = ("1", "[1,1]", "[1]")


def _ok_envelope(rows: list[dict[str, object]]) -> dict[str, object]:
    return {"success": True, "code": 0, "message": "ok",
            "result": {"count": len(rows), "pages": 1, "data": rows}}


def _upstream_sort_error() -> dict[str, object]:
    """上游对非法排序参数的真实回应（code=9501）。"""
    return {"success": False, "code": 9501, "message": "排序字段和顺序数量不一致"}


class _QuerySpyKernel:
    """记录每次 paging 请求的 query，并按真实上游规则判定成功/失败。"""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.queries: list[dict[str, list[str]]] = []

    def fetch(self, params, *, sort_params, filter_params, budget):  # noqa: ANN001
        merged = {**params, **sort_params, **filter_params}
        self.queries.append(parse_qs(_urlencode(merged)))
        columns = str(merged.get("sortColumns") or "")
        types = str(merged.get("sortTypes") or "")
        # 复刻上游判据：两个逗号串必须等长且都是数字。
        ok = (
            len(columns.split(",")) == len(types.split(","))
            and all(part.isdigit() for part in types.split(","))
        )
        if not ok:
            return youzi_paging.PagingResult(
                "fetch_failed", "upstream_error", [], 1, 1
            )
        return youzi_paging.PagingResult("complete", "all_pages_validated", self.rows, 1, 1)


def _urlencode(mapping: dict[str, object]) -> str:
    from urllib.parse import urlencode

    return urlencode({key: str(value) for key, value in mapping.items()})


@pytest.fixture()
def spy(monkeypatch: pytest.MonkeyPatch):
    rows = [{"SECURITY_CODE": "600519", "OPERATEDEPT_CODE": "10000001"}]
    kernel = _QuerySpyKernel(rows)
    monkeypatch.setattr(lhb_feed, "_kernel", lambda: kernel)
    # 单例缓存会跨用例串味：显式复位，保证每个用例都拿到自己的 spy。
    monkeypatch.setattr(lhb_feed, "_paging_kernel", None)
    return kernel


def test_sort_types_is_same_length_as_sort_columns(spy: _QuerySpyKernel) -> None:
    """★ 回归核心：每个已登记报告的 `sortTypes` 必须与 `sortColumns` 等长。

    旧实现的 `sortTypes: 1` 让 BUY/SELL 两个报告恒被判失败，本断言就是它的墓碑。
    """
    assert lhb_feed._SORT_COLUMNS, "至少应登记一个分页报告"
    for report_name, columns in lhb_feed._SORT_COLUMNS.items():
        types = lhb_feed._SORT_TYPES[report_name]
        assert len(types.split(",")) == len(columns.split(",")), report_name
        assert all(part.isdigit() for part in types.split(",")), report_name


def test_every_registered_report_fetches_complete(spy: _QuerySpyKernel) -> None:
    """三个报告都要走通；数量不等长的报告**不允许**存在于登记表里。"""
    for report_name in lhb_feed._SORT_COLUMNS:
        rows, coverage, reason = lhb_feed.fetch_report_page_complete(report_name, "20260814")
        assert coverage == "complete", (report_name, reason)
        assert rows == spy.rows


def test_two_column_reports_send_matching_sort_types(spy: _QuerySpyKernel) -> None:
    """带 tie-breaker 的报告确实按两列两方向发出，而不是被压成单值。"""
    lhb_feed.fetch_report_page_complete(lhb_feed.REPORT_SEAT_BUY, "20260814")
    query = spy.queries[-1]
    assert query["sortColumns"] == ["SECURITY_CODE,OPERATEDEPT_CODE"]
    assert query["sortTypes"] == ["1,1"]


def test_legacy_single_value_sort_types_fails_loudly(spy: _QuerySpyKernel) -> None:
    """把旧写法 `sortTypes: 1` 放回去，必须**显式**失败而不是悄悄 0 行。

    路线图 Y2-07 的门槛是「完整分页」，而旧写法让两个席位报告恒被上游以 9501 拒绝、
    最终表现成 `coverage=fetch_failed` + `events=[]`（用户看到的只是「这段时间没数据」）。
    本地自检与上游判定**任一**拦住都算合格，但绝不允许静默通过。
    """
    original = lhb_feed._SORT_TYPES[lhb_feed.REPORT_SEAT_BUY]
    lhb_feed._SORT_TYPES[lhb_feed.REPORT_SEAT_BUY] = "1"
    try:
        with pytest.raises(lhb_feed.LhbError, match="排序键与方向数量不一致"):
            lhb_feed.fetch_report_page_complete(lhb_feed.REPORT_SEAT_BUY, "20260814")
    finally:
        lhb_feed._SORT_TYPES[lhb_feed.REPORT_SEAT_BUY] = original
    # 本地拦住即可：请求数不增，且**没有**任何行被当成完整输入返回。
    assert spy.queries == []


def test_upstream_rejection_degrades_instead_of_returning_empty_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """若上游真按 9501 拒绝，必须降级为 fetch_failed，不得把 0 行当 complete。

    这条不依赖我方自检：直接喂一个「列数配平但上游仍拒绝」的 transport，
    验证降级语义本身是对的（否则调用方会把空集当完整输入生成跨日事实）。
    """

    def rejecting(params: dict[str, object], timeout: float) -> dict[str, object]:
        return _upstream_sort_error()

    monkeypatch.setattr(lhb_feed, "_paging_kernel", None)
    monkeypatch.setattr(
        lhb_feed,
        "_kernel",
        lambda: youzi_paging.PagingKernel(
            rejecting, policy=youzi_paging.PagingPolicy(retries_per_page=0)
        ),
    )
    rows, coverage, reason = lhb_feed.fetch_report_page_complete(
        lhb_feed.REPORT_SEAT_BUY, "20260814"
    )
    assert (rows, coverage) == ([], "fetch_failed")
    assert reason == "upstream_error"


def test_short_sort_types_is_rejected_before_any_request(spy: _QuerySpyKernel) -> None:
    """登记表自相矛盾时（列数 ≠ 方向数）本地就拦住，不发请求、不产假数据。"""
    spy_queries_before = len(spy.queries)
    lhb_feed._SORT_COLUMNS["RPT_SYNTHETIC_MISMATCH"] = "A,B,C"
    lhb_feed._SORT_TYPES["RPT_SYNTHETIC_MISMATCH"] = "1,1"
    try:
        with pytest.raises(lhb_feed.LhbError, match="排序键与方向数量不一致"):
            lhb_feed.fetch_report_page_complete("RPT_SYNTHETIC_MISMATCH", "20260814")
    finally:
        del lhb_feed._SORT_COLUMNS["RPT_SYNTHETIC_MISMATCH"]
        del lhb_feed._SORT_TYPES["RPT_SYNTHETIC_MISMATCH"]
    assert len(spy.queries) == spy_queries_before


def test_unregistered_report_refuses_to_page(spy: _QuerySpyKernel) -> None:
    """未登记排序键的报告拒绝分页取数——宁可降级也不猜排序。"""
    with pytest.raises(lhb_feed.LhbError, match="未登记稳定排序键"):
        lhb_feed.fetch_report_page_complete("RPT_NOT_REGISTERED", "20260814")

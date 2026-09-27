"""本地横截面面板测试（QL10）:universe 可复现、交集对齐、失败清单、断点续传、快照重建、行业分组诚实缺口。"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta

import pytest

from investment_steward_core import quant_panel
from investment_steward_core.storage import artifact_store
from investment_steward_core.storage.paths import StorageLayout
from test_quant_pool import _db, _lay

# --------------------------------------------------------------------------- #
# 合成夹具（确定性:同输入同输出,不依赖行情源）
# --------------------------------------------------------------------------- #
def synth_dates(count: int, start: date = date(2025, 1, 2)) -> list[str]:
    return [(start + timedelta(days=index)).isoformat() for index in range(count)]


def synth_bars(symbol: str, dates: list[str], *, slope: float = 0.0, base: float = 10.0) -> list[dict[str, object]]:
    """确定性伪随机日线:``slope`` 决定日收益的截面排序（横截面 IC 方向测试的基础）。"""
    seed = sum(ord(char) for char in symbol)
    price = base + (seed % 13) * 0.2
    bars: list[dict[str, object]] = []
    for index, day in enumerate(dates):
        ret = slope + 0.004 * math.sin((index + seed) * 0.41) + 0.002 * math.cos(index * 0.17 + seed)
        nxt = round(max(0.5, price * (1.0 + ret)), 4)
        bars.append({
            "timestamp": f"{day}T00:00:00+00:00",
            "open": round(price, 4),
            "high": round(max(price, nxt) * 1.001, 4),
            "low": round(min(price, nxt) * 0.999, 4),
            "close": nxt,
            "volume": 1000.0 + seed * 10 + index,
        })
        price = nxt
    return bars


SYMBOLS = [f"6000{index:02d}" for index in range(12)]


def synth_universe_boards(size: int = 12) -> list[dict[str, object]]:
    """模拟「成交额降序」榜单行（字段与 fetch_cn_market_board 一致）。"""
    return [
        {"symbol": symbol, "name": f"票{symbol}", "price": 10.0, "change_pct": 0.0,
         "volume": 1e6, "turnover": 1e9 - index * 1e6, "turnover_rate": 1.0}
        for index, symbol in enumerate(SYMBOLS[:size])
    ]


def _fetcher(dates: list[str], *, failing: set[str] | None = None, missing: dict[str, list[str]] | None = None,
             calls: list[str] | None = None):
    failing = failing or set()
    missing = missing or {}

    def fetch(symbol: str, window: int):
        if calls is not None:
            calls.append(symbol)
        if symbol in failing:
            raise RuntimeError("模拟行情源不可达")
        days = [day for day in dates if day not in set(missing.get(symbol, []))]
        return synth_bars(symbol, days[:window]), "stub-feed"

    return fetch


# --------------------------------------------------------------------------- #
# universe 规则
# --------------------------------------------------------------------------- #
def test_universe_rule_is_reproducible_and_records_provenance():
    boards = synth_universe_boards(12)
    first = quant_panel.resolve_universe(boards, size=8, source="turnover-board", observed_at="2026-09-22T00:00:00+00:00")
    second = quant_panel.resolve_universe(boards, size=8, source="turnover-board", observed_at="2026-09-22T00:00:00+00:00")
    assert first == second  # 同榜单同 size → 逐位一致
    assert first["symbols"] == SYMBOLS[:8]  # 榜单顺序即规则(不做二次排序)
    assert first["size"] == 8 and first["requested_size"] == 8
    assert first["candidates_available"] == 12
    assert first["source"] == "turnover-board"
    assert first["observed_at"] == "2026-09-22T00:00:00+00:00"
    assert "成交额前 8 名" in first["rule"]


@pytest.mark.parametrize("size", [0, 4, 201])
def test_universe_size_bounds_are_enforced(size):
    with pytest.raises(ValueError, match="universe 大小必须在"):
        quant_panel.resolve_universe(synth_universe_boards(12), size=size)


# --------------------------------------------------------------------------- #
# 交集对齐 / 失败清单 / 覆盖度
# --------------------------------------------------------------------------- #
def test_alignment_uses_intersection_and_reports_dropped_dates(tmp_path):
    layout = _lay(tmp_path)
    dates = synth_dates(40)
    thin = SYMBOLS[0]
    panel = quant_panel.build_panel(
        layout, SYMBOLS, window=40,
        fetch_bars=_fetcher(dates, missing={thin: [dates[30], dates[31]]}),
        use_cache=False,
    )
    assert panel["available"] is True
    assert panel["alignment"] == "intersection"
    # 交集:全员都有该交易日才保留 → 被裁掉的 2 天不在面板里
    assert dates[30] not in panel["dates"] and dates[31] not in panel["dates"]
    assert len(panel["dates"]) == 38
    # 对齐代价如实报出:该标的自身只有 38 天(缺 2 天),交集把全体的并集裁掉 2 天
    assert panel["own_date_counts"][thin] == 38
    assert all(panel["own_date_counts"][symbol] == 40 for symbol in SYMBOLS[1:])
    assert panel["dropped_dates"][thin] == 0  # 交集对它无损(它本来就少那两天)
    assert panel["alignment_loss"] == {
        "union_dates": 40,
        "common_dates": 38,
        "dropped_by_intersection": 2,
        # v2 起一并报出资格审查结果:小缺口属正常停牌级别,全员入选
        "requested_symbols": 12,
        "eligible_symbols": 12,
        "excluded_by_history": 0,
    }
    assert panel["cells"] == 38 * 12


def test_failures_are_listed_and_coverage_is_reported(tmp_path):
    layout = _lay(tmp_path)
    dates = synth_dates(40)
    panel = quant_panel.build_panel(
        layout, SYMBOLS, window=40,
        fetch_bars=_fetcher(dates, failing={SYMBOLS[3]}),
        use_cache=False,
    )
    assert len(panel["failures"]) == 1
    assert panel["failures"][0]["symbol"] == SYMBOLS[3]
    assert "模拟行情源不可达" in panel["failures"][0]["reason"]
    assert SYMBOLS[3] not in panel["symbols"]
    assert panel["symbol_count"] == 11
    assert panel["coverage"] == round(11 / 12, 6)
    # 失败标的在 steps 里也留痕（ok=False）,不是静默跳过
    failed_steps = [step for step in panel["steps"] if not step["ok"]]
    assert [step["symbol"] for step in failed_steps] == [SYMBOLS[3]]


def test_panel_is_unavailable_when_below_min_universe(tmp_path):
    layout = _lay(tmp_path)
    dates = synth_dates(40)
    panel = quant_panel.build_panel(
        layout, SYMBOLS[:3], window=40, fetch_bars=_fetcher(dates), use_cache=False,
    )
    assert panel["available"] is False  # 3 只 < 5 只下限
    assert "横截面研究至少" in panel["degraded_reason"]


def test_empty_fetch_result_does_not_build_a_fake_panel(tmp_path):
    layout = _lay(tmp_path)
    panel = quant_panel.build_panel(
        layout, [], window=40, fetch_bars=_fetcher(synth_dates(10)), use_cache=False,
    )
    assert panel["available"] is False and panel["cells"] == 0
    assert panel["degraded_reason"] == "没有任何标的数据可用——不构造空面板冒充结果。"


def test_no_common_trading_dates_is_reported_not_padded(tmp_path):
    layout = _lay(tmp_path)
    dates = synth_dates(30)
    early, late = SYMBOLS[0:2], SYMBOLS[2:5]

    def fetch(symbol: str, window: int):
        days = dates[:15] if symbol in early else dates[15:]
        return synth_bars(symbol, days), "stub-feed"

    panel = quant_panel.build_panel(layout, SYMBOLS[:5], window=30, fetch_bars=fetch, use_cache=False)
    assert panel["dates"] == [] and panel["available"] is False
    assert panel["alignment_loss"]["union_dates"] == 30
    assert panel["alignment_loss"]["common_dates"] == 0
    assert "没有共同交易日" in panel["degraded_reason"]


# --------------------------------------------------------------------------- #
# 进度 / 断点续传 / 取消
# --------------------------------------------------------------------------- #
def test_progress_callback_advances_over_every_symbol(tmp_path):
    layout = _lay(tmp_path)
    dates = synth_dates(30)
    seen: list[tuple[int, int]] = []
    quant_panel.build_panel(
        layout, SYMBOLS, window=30, fetch_bars=_fetcher(dates, failing={SYMBOLS[2]}),
        use_cache=False, on_progress=lambda done, total: seen.append((done, total)),
    )
    assert [done for done, _ in seen] == list(range(1, len(SYMBOLS) + 1))
    assert all(total == len(SYMBOLS) for _, total in seen)


def test_same_day_cache_resumes_and_retries_only_the_gap(tmp_path):
    layout = _lay(tmp_path)
    dates = synth_dates(30)
    calls: list[str] = []
    quant_panel.build_panel(
        layout, SYMBOLS, window=30, fetch_bars=_fetcher(dates, failing={SYMBOLS[5]}, calls=calls),
    )
    assert sorted(calls) == sorted(SYMBOLS)  # 首轮全量取数

    second_calls: list[str] = []
    panel = quant_panel.build_panel(
        layout, SYMBOLS, window=30, fetch_bars=_fetcher(dates, calls=second_calls),
    )
    assert second_calls == [SYMBOLS[5]]  # 只补上一轮的缺口
    from_cache = [step for step in panel["steps"] if step["from_cache"]]
    assert len(from_cache) == len(SYMBOLS) - 1
    assert panel["symbol_count"] == len(SYMBOLS)  # 缺口补上后是满员面板


def test_cache_is_invalid_across_days(tmp_path):
    layout = _lay(tmp_path)
    bars = synth_bars("600000", synth_dates(10))
    quant_panel.write_cached_bars(layout, "600000", bars, window=10, source="stub", today="2026-09-22")
    assert quant_panel.read_cached_bars(layout, "600000", window=10, today="2026-09-22") == bars
    assert quant_panel.read_cached_bars(layout, "600000", window=10, today="2026-09-23") is None
    # 窗口不足也不复用（不用短历史冒充长历史）
    assert quant_panel.read_cached_bars(layout, "600000", window=250, today="2026-09-22") is None


def test_cancellation_keeps_partial_progress_in_cache(tmp_path):
    layout = _lay(tmp_path)
    dates = synth_dates(30)
    done = {"count": 0}
    calls: list[str] = []

    def is_cancelled() -> bool:
        return done["count"] >= 4

    def fetch(symbol: str, window: int):
        calls.append(symbol)
        done["count"] += 1
        return synth_bars(symbol, dates[:window]), "stub-feed"

    panel = quant_panel.build_panel(layout, SYMBOLS, window=30, fetch_bars=fetch, is_cancelled=is_cancelled)
    assert panel["cancelled"] is True
    assert panel["symbol_count"] == 4
    assert len(calls) == 4  # 取消后不再发新取数
    assert "任务被取消" in panel["degraded_reason"]
    # 已完成部分落缓存 → 重发只补缺口
    resume_calls: list[str] = []
    quant_panel.build_panel(layout, SYMBOLS, window=30, fetch_bars=_fetcher(dates, calls=resume_calls))
    assert resume_calls == SYMBOLS[4:]


# --------------------------------------------------------------------------- #
# 内容寻址快照
# --------------------------------------------------------------------------- #
def test_panel_rebuilds_from_snapshot_bitwise(tmp_path):
    layout = _lay(tmp_path)
    dates = synth_dates(40)
    panel = quant_panel.build_panel(layout, SYMBOLS, window=40, fetch_bars=_fetcher(dates), use_cache=False)
    saved = quant_panel.save_panel(layout, panel)
    assert saved["snapshot_hash"] == quant_panel.panel_digest(panel)
    assert saved["file_name"] == f"panel-{saved['snapshot_hash']}.json"

    rebuilt = quant_panel.load_panel(layout, content_hash=saved["content_hash"], file_name=saved["file_name"])
    assert quant_panel.panel_digest(rebuilt) == saved["snapshot_hash"]
    assert rebuilt["symbols"] == panel["symbols"]
    assert rebuilt["dates"] == panel["dates"]
    assert rebuilt["columns"] == panel["columns"]
    assert rebuilt["cells"] == panel["cells"] == saved["cells"]
    # 重建后的面板能直接喂下游（bars_of 还原出逐 bar 形态）
    assert quant_panel.bars_of(rebuilt, SYMBOLS[0])[0]["close"] == panel["columns"][SYMBOLS[0]]["close"][0]


def test_snapshot_is_idempotent_for_identical_content(tmp_path):
    layout = _lay(tmp_path)
    dates = synth_dates(30)
    panel = quant_panel.build_panel(layout, SYMBOLS, window=30, fetch_bars=_fetcher(dates), use_cache=False)
    first = quant_panel.save_panel(layout, panel)
    second = quant_panel.save_panel(layout, panel)
    # 内容寻址:同内容 → 同 digest / 同文件名 / 同文件字节哈希（不写「保存时刻」污染哈希）
    assert first == second
    # 显式给 built_at 时才记录保存时刻,且不改变内容寻址键
    stamped = quant_panel.save_panel(layout, panel, built_at="2026-09-22T00:00:00+00:00")
    assert stamped["snapshot_hash"] == first["snapshot_hash"]
    assert stamped["file_name"] == first["file_name"]
    assert stamped["content_hash"] != first["content_hash"]


def test_tampered_snapshot_file_is_detected(tmp_path):
    layout = _lay(tmp_path)
    dates = synth_dates(30)
    panel = quant_panel.build_panel(layout, SYMBOLS, window=30, fetch_bars=_fetcher(dates), use_cache=False)
    saved = quant_panel.save_panel(layout, panel)
    path = artifact_store.artifact_path(layout, quant_panel.PANEL_SNAPSHOT_KIND, saved["file_name"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["columns"][SYMBOLS[0]]["close"][0] = 999.0
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(artifact_store.ArtifactIntegrityError, match="哈希不一致"):
        quant_panel.load_panel(layout, content_hash=saved["content_hash"], file_name=saved["file_name"])
    # 即使把新文件哈希如实传进来,语义层 digest 也不能通过（双保险）
    _, new_hash = artifact_store.save_artifact(
        layout, quant_panel.PANEL_SNAPSHOT_KIND, saved["file_name"], payload
    )
    with pytest.raises(ValueError, match="快照哈希不一致"):
        quant_panel.load_panel(layout, content_hash=new_hash, file_name=saved["file_name"])


def test_summarise_panel_does_not_ship_the_matrix(tmp_path):
    layout = _lay(tmp_path)
    panel = quant_panel.build_panel(
        layout, SYMBOLS, window=30, fetch_bars=_fetcher(synth_dates(30)), use_cache=False,
    )
    summary = quant_panel.summarise_panel(panel)
    assert "columns" not in summary
    assert summary["symbol_count"] == 12 and summary["date_count"] == 30
    assert summary["panel_spec_version"] == quant_panel.PANEL_SPEC_VERSION == 2
    assert summary["steps"][0]["source"] == "stub-feed"


def test_spec_version_is_frozen():
    # v2：对齐规则变更（交集前加入历史资格审查）必须递增版本号，防止旧快照被当成新口径复用。
    assert quant_panel.PANEL_SPEC_VERSION == 2
    assert quant_panel.DEFAULT_ALIGNMENT in quant_panel.ALIGNMENT_MODES


# --------------------------------------------------------------------------- #
# 历史资格审查（v2）:短历史成员不得把整个面板压塌
# --------------------------------------------------------------------------- #
def test_short_history_member_is_excluded_and_reported_not_collapsing_the_panel(tmp_path):
    """实测缺陷（2026-09-22）：成交额前 8 名里的新股 688825 只有 42 根,把 250 根窗口的面板压成 42 天。

    交集口径下面板长度由最短成员决定 —— 一个刚上市的票就能让整个横截面研究样本不足。
    v2 起剔除它并把门槛与原因如实报出,其余成员仍按严格交集对齐（不填充）。
    """
    layout = _lay(tmp_path)
    dates = synth_dates(120)
    fresh = SYMBOLS[0]
    # 其余 11 只各 120 根,新票只有 40 根（< 0.9 × 120 = 108）
    per_symbol = {symbol: synth_bars(symbol, dates) for symbol in SYMBOLS[1:]}
    per_symbol[fresh] = synth_bars(fresh, dates[-40:])

    panel = quant_panel.assemble_panel(
        per_symbol, symbols=SYMBOLS, window=120, requested_symbols=SYMBOLS,
    )
    assert panel["available"] is True
    assert fresh not in panel["symbols"], "短历史成员必须被剔除,否则它决定整个面板的长度"
    assert panel["symbol_count"] == 11
    assert panel["date_count"] == 120, "其余成员历史齐平 → 交集应当保住全部 120 天"
    # 剔除不是静默的:清单、门槛、原因都在
    assert panel["history_floor"] == 108
    assert [item["symbol"] for item in panel["excluded"]] == [fresh]
    assert panel["excluded"][0]["rows"] == 40
    assert "压到它自己的历史长度" in panel["excluded"][0]["reason"]
    assert panel["alignment_loss"]["excluded_by_history"] == 1
    assert panel["alignment_loss"]["eligible_symbols"] == 11
    assert "被剔除" in panel["degraded_reason"]


def test_history_filter_keeps_everyone_when_coverage_is_even(tmp_path):
    """门槛是相对当天最优覆盖的:全员历史齐平时（含整体都很短）不得剔掉任何人。

    整体样本太短该由下游可行性闸门如实证伪,不能在装配期把面板清空。
    """
    layout = _lay(tmp_path)
    dates = synth_dates(30)
    per_symbol = {symbol: synth_bars(symbol, dates) for symbol in SYMBOLS}
    panel = quant_panel.assemble_panel(
        per_symbol, symbols=SYMBOLS, window=30, requested_symbols=SYMBOLS,
    )
    assert panel["excluded"] == []
    assert panel["symbol_count"] == len(SYMBOLS)
    assert panel["date_count"] == 30


def test_small_shortfall_stays_in_the_panel_and_is_handled_by_intersection(tmp_path):
    """只差一点点（38/40 = 95% > 90%）属于正常停牌级别,不该剔除 —— 由交集如实裁掉那两天。"""
    layout = _lay(tmp_path)
    dates = synth_dates(40)
    per_symbol = {symbol: synth_bars(symbol, dates) for symbol in SYMBOLS}
    per_symbol[SYMBOLS[0]] = synth_bars(SYMBOLS[0], [day for day in dates if day not in {dates[30], dates[31]}])

    panel = quant_panel.assemble_panel(
        per_symbol, symbols=SYMBOLS, window=40, requested_symbols=SYMBOLS,
    )
    assert panel["excluded"] == []
    assert panel["symbol_count"] == len(SYMBOLS)
    assert panel["date_count"] == 38
    assert panel["alignment_loss"]["dropped_by_intersection"] == 2


# --------------------------------------------------------------------------- #
# 行业分组（QL11 中性化来源）:缺口如实报,不猜测
# --------------------------------------------------------------------------- #
def _sectors() -> list[dict[str, object]]:
    return [
        {"code": "new_blhy", "name": "玻璃行业"},
        {"code": "new_dzqj", "name": "电子器件"},
        {"code": "new_yhhy", "name": "银行行业"},
    ]


def _members(mapping: dict[str, list[str]], failing: set[str] | None = None):
    failing = failing or set()

    def fetch(code: str, top: int = 100):
        if code in failing:
            raise RuntimeError("板块成分股接口不可达")
        return [{"symbol": symbol, "name": symbol} for symbol in mapping.get(code, [])]

    return fetch


def test_industry_groups_are_resolved_from_sector_members():
    mapping = {
        "new_blhy": SYMBOLS[0:4],
        "new_dzqj": SYMBOLS[4:8],
        "new_yhhy": SYMBOLS[8:12],
    }
    result = quant_panel.resolve_industry_groups(
        SYMBOLS, sector_list=_sectors, sector_members=_members(mapping),
    )
    assert result["mode"] == "industry" and result["usable"] is True
    assert result["unassigned"] == [] and result["unassigned_ratio"] == 0.0
    assert result["groups"][SYMBOLS[0]] == "玻璃行业"
    assert result["groups"][SYMBOLS[11]] == "银行行业"
    assert result["sectors_scanned"] == 3
    assert result["degraded_reason"] is None


def test_industry_groups_report_gap_without_inventing_a_bucket():
    mapping = {"new_blhy": SYMBOLS[0:3]}  # 只有 3/12 能匹配
    result = quant_panel.resolve_industry_groups(
        SYMBOLS, sector_list=_sectors, sector_members=_members(mapping),
    )
    assert result["assigned"] == 3
    assert result["unassigned"] == SYMBOLS[3:]
    assert result["unassigned_ratio"] == pytest.approx(9 / 12)
    assert result["usable"] is False and result["mode"] == "none"  # 缺口过大 → 拒绝冒充中性化
    assert "行业反查缺口" in result["degraded_reason"]
    assert all(result["groups"][symbol] is None for symbol in SYMBOLS[3:])


def test_industry_groups_survive_a_failing_sector_and_early_exit():
    mapping = {"new_blhy": SYMBOLS[0:6], "new_dzqj": SYMBOLS[6:12]}
    calls: list[str] = []

    def members(code: str, top: int = 100):
        calls.append(code)
        return _members(mapping, failing={"new_blhy"})(code, top)

    whole = quant_panel.resolve_industry_groups(SYMBOLS, sector_list=_sectors, sector_members=members)
    assert len(whole["errors"]) == 1 and whole["errors"][0]["scope"] == "new_blhy"
    assert whole["assigned"] == 6 and whole["mode"] == "none"  # 一半缺口 → 不达标

    # 全覆盖时提前退出:扫到第 1 个板块就已分配全部 → 不再请求后续板块
    partial = quant_panel.resolve_industry_groups(
        SYMBOLS[0:4], sector_list=_sectors, sector_members=_members({"new_blhy": SYMBOLS[0:4]}),
    )
    assert partial["sectors_scanned"] == 1
    assert partial["mode"] == "industry"


def test_industry_groups_degrade_when_sector_list_is_unavailable():
    def boom():
        raise RuntimeError("板块清单接口不可达")

    result = quant_panel.resolve_industry_groups(SYMBOLS, sector_list=boom, sector_members=_members({}))
    assert result["mode"] == "none" and result["usable"] is False
    assert result["unassigned_ratio"] == 1.0
    assert result["errors"][0]["scope"] == "sector_list"
    assert "不做任何推测分组" in result["degraded_reason"]


def test_industry_groups_with_empty_universe():
    result = quant_panel.resolve_industry_groups([], sector_list=_sectors, sector_members=_members({}))
    assert result["usable"] is False and result["assigned"] == 0
    assert result["degraded_reason"] == "universe 为空:没有可分组标的。"

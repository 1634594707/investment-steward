"""QL10 / QL11 / QL12 端点级测试：面板构建任务、快照重建、横截面挖掘 → 报告卡闭环。"""

from __future__ import annotations

import math
import time as time_module
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings

TOKEN = "ql-panel-endpoint-token"
SYMBOLS = [f"6000{index:02d}" for index in range(12)]
BAR_COUNT = 100


def _client(tmp_path):
    app = create_app(CoreSettings(session_token=TOKEN, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": TOKEN}


def _install_plugin(http, headers) -> None:
    assert http.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200


def _poll(http, headers, path: str, timeout_s: float = 60.0) -> dict:
    deadline = time_module.monotonic() + timeout_s
    while time_module.monotonic() < deadline:
        body = http.get(path, headers=headers).json()
        if body["state"] in ("done", "cancelled", "error"):
            return body
        time_module.sleep(0.02)
    raise AssertionError(f"任务未在时限内到达终态：{path}")


def _fake_board(board: str, top: int = 30):
    return [
        {"symbol": symbol, "name": f"票{symbol}", "price": 10.0 + index * 0.1,
         "change_pct": 0.0, "volume": 1e6, "turnover": 1e9 - index * 1e6, "turnover_rate": 1.0}
        for index, symbol in enumerate(SYMBOLS[:top])
    ]


def _engineered_bars(symbol: str, limit: int) -> list[dict[str, object]]:
    """确定性日线:标的间有持久基准收益差（可挖的横截面信号）+ 逐日旋转的小扰动（IC 有方差）。"""
    index = SYMBOLS.index(symbol)
    drift = 0.002 * (index - (len(SYMBOLS) - 1) / 2)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    price = 10.0 + index * 0.05
    bars: list[dict[str, object]] = []
    for step in range(limit):
        wobble = 0.0009 * math.sin(step * 0.7 + index * 2.3) + 0.0006 * math.cos(step * 0.13 + index * 5.1)
        previous = price
        price = max(0.5, price * (1.0 + drift + wobble))
        bars.append({
            "timestamp": (start + timedelta(days=step)).isoformat(),
            "open": previous,
            "high": max(previous, price),
            "low": min(previous, price),
            "close": price,
            "volume": 1e6 + index * 1000 + step,
        })
    return bars


def _fake_feeds(monkeypatch, *, failing: set[str] | None = None) -> None:
    from investment_steward_core.api import app as app_module

    failing = failing or set()

    def kline(symbol: str, limit: int = 250, period: str = "day"):
        if symbol in failing:
            raise RuntimeError("模拟行情源不可达")
        return _engineered_bars(symbol, min(limit, BAR_COUNT)), "unit-test"

    monkeypatch.setattr(app_module, "fetch_cn_market_board", _fake_board)
    monkeypatch.setattr(app_module, "fetch_cn_kline", kline)


def _build_panel(http, headers, **overrides) -> dict:
    body = {"universe_size": 12, "board": "turnover", "window": BAR_COUNT, "refresh": False}
    body.update(overrides)
    start = http.post("/quant/panel/build", headers=headers, json=body).json()
    assert start["ok"] is True and start["reused"] is False
    job = _poll(http, headers, f"/quant/panel/build/{start['job_id']}")
    assert job["state"] == "done", job
    return job["summary"]


# --------------------------------------------------------------------------- #
# QL10：面板构建
# --------------------------------------------------------------------------- #
def test_panel_endpoints_are_usable_without_manual_plugin_install(tmp_path):
    """cn-market-data 是引导安装且默认启用的,所以面板链路无需手动装插件即可用。"""
    from investment_steward_core.domain.models import PluginInstallationState

    http, headers = _client(tmp_path)
    installation = http.app.state.core.database.get_plugin_installation("official.cn-market-data")
    assert installation is not None
    assert installation.state == PluginInstallationState.ENABLED
    assert http.get("/quant/panel/snapshots", headers=headers).json() == []


def test_panel_build_rejects_bad_universe_size_and_board(tmp_path):
    http, headers = _client(tmp_path)
    assert http.post("/quant/panel/build", headers=headers, json={"universe_size": 1}).status_code == 422
    assert http.post("/quant/panel/build", headers=headers,
                     json={"universe_size": 12, "board": "nope"}).status_code == 422


def test_panel_build_reports_progress_failures_and_snapshot(tmp_path, monkeypatch):
    http, headers = _client(tmp_path)
    _install_plugin(http, headers)
    _fake_feeds(monkeypatch, failing={SYMBOLS[4]})
    summary = _build_panel(http, headers)

    # 可追溯:universe 规则/来源/观测日 + 口径
    assert summary["universe"]["rule"].startswith("成交额前 12 名")
    assert summary["universe"]["source"] == "cn-market-board:turnover"
    assert summary["universe"]["observed_at"]
    assert summary["caliber"]["panel_spec_version"] == 2
    assert summary["caliber"]["alignment"] == "intersection"
    # 失败清单如实列出,覆盖度与标的数同步下降
    assert [f["symbol"] for f in summary["failures"]] == [SYMBOLS[4]]
    assert summary["symbol_count"] == 11
    assert summary["coverage"] < 1.0
    # 内容寻址快照
    snapshot = summary["snapshot"]
    assert len(snapshot["snapshot_hash"]) == 64
    assert snapshot["file_name"] == f"panel-{snapshot['snapshot_hash']}.json"
    assert snapshot["date_count"] == BAR_COUNT and snapshot["cells"] == BAR_COUNT * 11
    # 矩阵本体不回传
    assert "columns" not in summary


def test_panel_snapshot_listing_and_rebuild_from_snapshot(tmp_path, monkeypatch):
    http, headers = _client(tmp_path)
    _install_plugin(http, headers)
    _fake_feeds(monkeypatch)
    summary = _build_panel(http, headers)

    listed = http.get("/quant/panel/snapshots", headers=headers).json()
    assert len(listed) == 1
    assert listed[0]["kind"] == "panel_snapshots"
    assert listed[0]["content_hash"] == summary["snapshot"]["content_hash"]
    assert listed[0]["payload_meta"]["universe_rule"].startswith("成交额前 12 名")

    loaded = http.post("/quant/panel/load", headers=headers, json={
        "content_hash": summary["snapshot"]["content_hash"],
        "file_name": summary["snapshot"]["file_name"],
    }).json()
    # 验收:面板可从快照完整重建（digest 与构建时逐位一致）
    assert loaded["digest"] == summary["snapshot"]["snapshot_hash"]
    assert loaded["symbol_count"] == summary["symbol_count"]
    assert loaded["date_count"] == summary["date_count"]
    assert loaded["cells"] == summary["snapshot"]["cells"]
    assert loaded["available"] is True
    assert "哈希已复核" in loaded["note"]
    assert "columns" not in loaded


def test_panel_load_rejects_tampered_or_unknown_snapshot(tmp_path, monkeypatch):
    http, headers = _client(tmp_path)
    _install_plugin(http, headers)
    _fake_feeds(monkeypatch)
    summary = _build_panel(http, headers)
    bad = http.post("/quant/panel/load", headers=headers, json={
        "content_hash": "0" * 64, "file_name": summary["snapshot"]["file_name"],
    })
    assert bad.status_code == 409
    missing = http.post("/quant/panel/load", headers=headers, json={
        "content_hash": summary["snapshot"]["content_hash"], "file_name": "panel-doesnotexist.json",
    })
    assert missing.status_code == 409


def test_panel_build_reuses_running_job_and_cancel_on_finished_job(tmp_path, monkeypatch):
    http, headers = _client(tmp_path)
    _install_plugin(http, headers)
    _fake_feeds(monkeypatch)
    body = {"universe_size": 12, "board": "turnover", "window": BAR_COUNT}
    first = http.post("/quant/panel/build", headers=headers, json=body).json()
    _poll(http, headers, f"/quant/panel/build/{first['job_id']}")
    # 已结束 → 不再命中,重新建一个（指纹只匹配 running）
    again = http.post("/quant/panel/build", headers=headers, json=body).json()
    assert again["reused"] is False and again["job_id"] != first["job_id"]
    _poll(http, headers, f"/quant/panel/build/{again['job_id']}")
    # 已结束任务的取消返回 ok=False（不假装取消成功）
    assert http.post(f"/quant/panel/build/{first['job_id']}/cancel", headers=headers).json()["ok"] is False
    assert http.get("/quant/panel/build/not-a-job", headers=headers).status_code == 404


def test_panel_build_second_run_hits_same_day_cache(tmp_path, monkeypatch):
    """断点续传:同一 UTC 日期内二次构建直接命中缓存,不再打行情源。"""
    http, headers = _client(tmp_path)
    _install_plugin(http, headers)
    _fake_feeds(monkeypatch)
    _build_panel(http, headers)

    from investment_steward_core.api import app as app_module

    calls: list[str] = []

    def counting_kline(symbol: str, limit: int = 250, period: str = "day"):
        calls.append(symbol)
        return _engineered_bars(symbol, limit), "unit-test"

    monkeypatch.setattr(app_module, "fetch_cn_kline", counting_kline)
    summary = _build_panel(http, headers)
    assert calls == []  # 一个取数都没有,全部来自当日缓存
    assert summary["symbol_count"] == 12
    assert all(step["from_cache"] for step in summary["steps"])


# --------------------------------------------------------------------------- #
# QL11 + QL12：挖掘闭环
# --------------------------------------------------------------------------- #
def _mine(http, headers, summary, **overrides) -> dict:
    body = {
        "content_hash": summary["snapshot"]["content_hash"],
        "file_name": summary["snapshot"]["file_name"],
        "top_n": 2, "beam_width": 6, "beam_levels": 2, "quantiles": 4, "folds": 3,
    }
    body.update(overrides)
    start = http.post("/quant/panel/mine", headers=headers, json=body).json()
    assert start["ok"] is True
    job = _poll(http, headers, f"/quant/panel/mine/{start['job_id']}")
    assert job["state"] == "done", job
    return job["summary"]


def test_cross_section_mine_closed_loop_produces_report_cards(tmp_path, monkeypatch):
    http, headers = _client(tmp_path)
    _install_plugin(http, headers)
    _fake_feeds(monkeypatch)
    summary = _build_panel(http, headers)
    mined = _mine(http, headers, summary)

    # QL11:横截面榜单 + 口径 + 定义
    board = mined["board"]
    assert board["cs_spec_version"] == 1
    assert board["ic_definition"]["kind"] == "daily_cross_sectional_spearman"
    assert board["search"]["selection_segment"] == "train"
    assert board["split"]["feasible"] is True
    assert board["multiple_testing"]["method"] == "fdr"
    assert board["top"], "确定性截面信号应当被挖出"
    first = board["top"][0]
    assert first["test_significant"] is True
    assert "significance_note" in first

    # QL12:每条入选公式都带一张报告卡
    assert len(mined["reports"]) == len(board["top"]) <= 2
    for report in mined["reports"]:
        assert report["available"] is True
        assert len(report["report_hash"]) == 64
        assert report["caliber"]["backtest_spec_version"] == 2
        assert report["caliber"]["annual_trading_days"] == 252
        assert report["ic"]["mean"] is not None
        long_short = report["long_short"]
        assert long_short["samples"] >= 50
        assert long_short["turnover_mean"] > 0
        assert long_short["net_total"] < long_short["gross_total"]  # 成本必须被扣掉
        assert long_short["quantile_mean_forward_returns"][-1] > long_short["quantile_mean_forward_returns"][0]
        assert len(report["folds"]) == 3
        assert report["capacity"]["available"] is True

    # 口径与快照绑定:可从快照复算
    assert mined["caliber"]["cs_spec_version"] == 1
    assert mined["caliber"]["panel_spec_version"] == 2
    assert mined["caliber"]["backtest_spec_version"] == 2
    assert mined["caliber"]["forward_days"] == 1
    assert mined["panel_snapshot"]["content_hash"] == summary["snapshot"]["content_hash"]
    assert mined["panel_snapshot"]["digest"] == summary["snapshot"]["snapshot_hash"]
    assert "columns" not in mined["panel"]


def test_cross_section_mine_is_reproducible_from_the_same_snapshot(tmp_path, monkeypatch):
    """验收:同一快照 + 同一参数 → 榜单与报告哈希逐位一致（全部数字可复算）。"""
    http, headers = _client(tmp_path)
    _install_plugin(http, headers)
    _fake_feeds(monkeypatch)
    summary = _build_panel(http, headers)
    first = _mine(http, headers, summary)
    second = _mine(http, headers, summary)
    assert first["board"]["top"] == second["board"]["top"]
    assert [r["report_hash"] for r in first["reports"]] == [r["report_hash"] for r in second["reports"]]
    assert [r["long_short"]["net_total"] for r in first["reports"]] == [
        r["long_short"]["net_total"] for r in second["reports"]
    ]


def test_cross_section_mine_shuffled_labels_yields_empty_board(tmp_path, monkeypatch):
    """验收:标签置换后榜单为空（防过拟合的直接证据）。"""
    http, headers = _client(tmp_path)
    _install_plugin(http, headers)
    _fake_feeds(monkeypatch)
    summary = _build_panel(http, headers)
    mined = _mine(http, headers, summary, label_shuffle_seed=11)
    assert mined["board"]["top"] == []
    assert mined["board"]["label_shuffle"]["seed"] == 11
    assert mined["reports"] == []
    assert "未挖到显著公式" in mined["board"]["degraded_reason"]
    assert mined["caliber"]["label_shuffle_seed"] == 11


def test_cross_section_mine_reports_industry_gap_without_faking_it(tmp_path, monkeypatch):
    """QL11 验收:行业映射取不到时如实标注缺口,并降级为不做中性化。"""
    http, headers = _client(tmp_path)
    _install_plugin(http, headers)
    _fake_feeds(monkeypatch)
    summary = _build_panel(http, headers)

    from investment_steward_core import market_feed

    def boom(*_args, **_kwargs):
        raise RuntimeError("板块清单不可达")

    monkeypatch.setattr(market_feed, "fetch_cn_sector_list", boom)
    monkeypatch.setattr(market_feed, "fetch_cn_sector_members", boom)
    mined = _mine(http, headers, summary, industry_groups=True, neutralise="rank")
    info = mined["industry_groups"]
    assert info["mode"] == "none" and info["usable"] is False
    assert info["unassigned_ratio"] == 1.0
    assert "不做任何推测分组" in info["degraded_reason"]
    # 口径仍如实标为 rank,但 unassigned_ratio=1.0 表明并没有真做中性化
    assert mined["board"]["neutralise"]["mode"] == "rank"
    assert mined["board"]["neutralise"]["unassigned_ratio"] == 1.0
    assert "groups" not in info


def test_cross_section_mine_rejects_bad_requests(tmp_path, monkeypatch):
    http, headers = _client(tmp_path)
    _install_plugin(http, headers)
    _fake_feeds(monkeypatch)
    # 6 只标的的面板:quantiles 上限 = 6
    summary = _build_panel(http, headers, universe_size=6)
    keys = {"content_hash": summary["snapshot"]["content_hash"],
            "file_name": summary["snapshot"]["file_name"]}
    # 未知中性化口径
    response = http.post("/quant/panel/mine", headers=headers, json={**keys, "neutralise": "magic"})
    assert response.status_code == 422
    # quantiles 超过面板标的数
    response = http.post("/quant/panel/mine", headers=headers, json={**keys, "quantiles": 10})
    assert response.status_code == 422
    assert "不能超过面板标的数" in response.json()["detail"]
    # 快照哈希不存在
    response = http.post("/quant/panel/mine", headers=headers, json={
        "content_hash": "0" * 64, "file_name": summary["snapshot"]["file_name"],
    })
    assert response.status_code == 409


def test_cross_section_mine_rejects_unavailable_panel(tmp_path, monkeypatch):
    """面板不可用时 422,不允许「拿不足的样本硬出结论」。"""
    http, headers = _client(tmp_path)
    _install_plugin(http, headers)
    # 请求 6 只但 2 只取数失败 → 只剩 4 只,低于 MIN_UNIVERSE_SIZE=5 → 面板不可用
    _fake_feeds(monkeypatch, failing={SYMBOLS[1], SYMBOLS[3]})
    summary = _build_panel(http, headers, universe_size=6)
    assert summary["available"] is False
    assert summary["symbol_count"] == 4
    response = http.post("/quant/panel/mine", headers=headers, json={
        "content_hash": summary["snapshot"]["content_hash"],
        "file_name": summary["snapshot"]["file_name"],
    })
    assert response.status_code == 422
    assert "面板不可用" in response.json()["detail"]


def test_panel_mine_cancel_on_finished_job_and_unknown_job(tmp_path, monkeypatch):
    http, headers = _client(tmp_path)
    _install_plugin(http, headers)
    _fake_feeds(monkeypatch)
    summary = _build_panel(http, headers)
    body = {"content_hash": summary["snapshot"]["content_hash"],
            "file_name": summary["snapshot"]["file_name"], "top_n": 1, "beam_width": 4, "beam_levels": 1}
    start = http.post("/quant/panel/mine", headers=headers, json=body).json()
    _poll(http, headers, f"/quant/panel/mine/{start['job_id']}")
    assert http.post(f"/quant/panel/mine/{start['job_id']}/cancel", headers=headers).json()["ok"] is False
    assert http.get("/quant/panel/mine/not-a-job", headers=headers).status_code == 404

"""v26 板块扫描与评分 v2 回归守卫（2026-09-11）。

背景（用户真机反馈）：「提高扫描质量评分」——旧 v1 公式在强势股上直接贴顶，
榜单扫描前 5 名出现多个 100 分（排名退化）。v2 四处升级：
1. 同族合并（同 category 只计最高贡献，避免「均线系三条一起刷分」）；
2. 饱和曲线（100 分是渐近上限，高分段保留区分度）；
3. 新增量能确认 + 位置维度（**随信号新鲜度缩放**：没有新鲜信号的票不该靠这两项拿分）；
4. 板块共振（横截面因子，仅板块扫描时注入；单票扫描如实置 0）。

红线：
- 所有分项必须可复算、可在结果里展开（score_parts / hit_detail）；
- 反向信号只对冲、绝不作为数量加分；
- 板块共振不得在无板块上下文时凭空产生（单票扫描必须为 0）；
- 纯函数、确定性、不联网（行情一律 monkeypatch 或合成）。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from investment_steward_core import tactics, tactics_score

_NAME_BY_ID = {item["id"]: item["name"] for item in tactics.catalog()}


def _bars(
    count: int = 120,
    *,
    close_start: float = 10.0,
    close_step: float = 0.0,
    volume: float = 1_000_000.0,
    volume_step: float = 0.0,
    start: str = "2026-01-01",
) -> list[dict[str, object]]:
    """合成日线：可控制收盘趋势与量能趋势（用于验证量价/位置维度）。"""
    base = date.fromisoformat(start)
    bars: list[dict[str, object]] = []
    for index in range(count):
        close = close_start + close_step * index
        bars.append({
            "timestamp": (base + timedelta(days=index)).isoformat(),
            "open": close,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "volume": volume + volume_step * index,
        })
    return bars


def _signal(tactic_id: str, day: str, direction: str) -> dict[str, object]:
    return {
        "tactic_id": tactic_id,
        "tactic_name": _NAME_BY_ID[tactic_id],
        "direction": direction,
        "detail": "test",
        "date": day,
    }


def _last_day(bars: list[dict[str, object]]) -> str:
    return str(bars[-1]["timestamp"])[:10]


# ---- A. 同族合并 ----


def test_family_merge_counts_only_best_signal_per_family():
    """ma 族三条同向信号只计最高一条（其余标注 family_merged，不计 contribution）。"""
    bars = _bars()
    day = _last_day(bars)
    signals = [
        _signal("ma_golden_cross", day, "bullish"),        # 权重 7.0（族内最高）
        _signal("ma_bullish_alignment", day, "bullish"),   # 6.0
        _signal("ma20_support", day, "bullish"),           # 5.5
    ]
    result = tactics_score.score(signals, bars)
    assert result["family_merge"] is True
    assert result["score_parts"]["bullish"] == pytest.approx(7.0, abs=0.01)
    merged = [item for item in result["hit_detail"] if item["family_merged"]]
    assert len(merged) == 2
    assert all(item["contribution"] == 0.0 for item in merged)
    assert all(item["raw_contribution"] > 0 for item in merged)  # 原值仍可见（可复算）
    assert "ma" in result["merged_families"]
    # 同族合并只影响计分，不影响「命中可见性」：三条都还在 hit_tactics 里
    assert sorted(result["hit_tactics"]) == ["ma20_support", "ma_bullish_alignment", "ma_golden_cross"]
    assert result["counted_tactics"] == ["ma_golden_cross"]


def test_family_merge_does_not_touch_cross_family_signals():
    """跨族信号不受合并影响：两条不同族的同向信号都应计入 base 并形成共振。"""
    bars = _bars()
    day = _last_day(bars)
    result = tactics_score.score(
        [_signal("ma_golden_cross", day, "bullish"), _signal("volume_breakout", day, "bullish")], bars
    )
    assert result["score_parts"]["bullish"] == pytest.approx(15.0, abs=0.01)
    assert result["score_parts"]["resonance"] == pytest.approx(1.0)
    assert result["merged_families"] == []


# ---- B. 量能确认 ----


def test_volume_surge_beats_volume_dry_up():
    """同一形态，放量确认应高于缩量（量比 ≤0.7 无加成）。"""
    day_index = 118
    surge_bars = _bars(volume=500_000.0)
    surge_bars[day_index]["volume"] = 1_600_000.0     # 量比 ≈ 3.2 → 加满
    dry_bars = _bars(volume=1_500_000.0)
    dry_bars[day_index]["volume"] = 600_000.0         # 量比 ≈ 0.4 → 无加成
    day = str(surge_bars[day_index]["timestamp"])[:10]
    surge = tactics_score.score([_signal("platform_breakout", day, "bullish")], surge_bars)
    dry = tactics_score.score([_signal("platform_breakout", day, "bullish")], dry_bars)
    assert surge["score_parts"]["volume_bonus"] > dry["score_parts"]["volume_bonus"]
    assert dry["score_parts"]["volume_bonus"] == 0.0
    assert surge["tactic_score"] > dry["tactic_score"]
    assert surge["volume_ratio"] is not None and surge["volume_ratio"] > 1.5


def test_volume_bonus_missing_data_is_zero_and_reported():
    """无 volume 字段时量能项为 0 且 volume_ratio=None（如实标注，不猜）。"""
    bars = _bars()
    for bar in bars:
        bar.pop("volume", None)
    result = tactics_score.score([_signal("ma_golden_cross", _last_day(bars), "bullish")], bars)
    assert result["score_parts"]["volume_bonus"] == 0.0
    assert result["volume_ratio"] is None


# ---- C. 位置维度 ----


def test_low_position_beats_high_position():
    """同一形态：低位（区间下沿）应高于高位（区间上沿 90% 以上）。"""
    # 高位票：8 → 18.1 一路上涨，最新收盘贴区间顶（pos ≈ 0.97）
    high_bars = _bars(close_start=8.0, close_step=0.085)
    # 低位票：20 → 12.3 一路下跌，最新收盘贴区间底（pos ≈ 0.03）
    low_bars = _bars(close_start=20.0, close_step=-0.065)
    day = _last_day(high_bars)
    high = tactics_score.score([_signal("ma_golden_cross", day, "bullish")], high_bars)
    low = tactics_score.score([_signal("ma_golden_cross", day, "bullish")], low_bars)
    assert high["position"] is not None and high["position"] >= 0.9
    assert low["position"] is not None and low["position"] <= 0.5
    assert high["score_parts"]["position_bonus"] < 0
    assert low["score_parts"]["position_bonus"] > 0
    assert low["tactic_score"] > high["tactic_score"]


def test_bonus_scales_with_signal_freshness():
    """量价/位置/板块等 bonus 必须随信号新鲜度缩放：老信号不得靠 bonus 拿分。"""
    bars = _bars()
    fresh = tactics_score.score([_signal("ma_golden_cross", _last_day(bars), "bullish")], bars)
    stale_day = str(bars[5]["timestamp"])[:10]
    stale = tactics_score.score([_signal("ma_golden_cross", stale_day, "bullish")], bars)
    assert fresh["score_parts"]["bonus_scale"] == pytest.approx(1.0, abs=0.001)
    assert stale["score_parts"]["bonus_scale"] < 0.05
    # 老信号的 bonus 实际值趋近 0（位置同为低位、量能相同的前提下）
    assert stale["score_parts"]["volume_bonus"] == pytest.approx(0.0, abs=0.1)
    assert stale["tactic_score"] < 1.0


# ---- D. 板块共振（横截面因子）----


def test_sector_context_requires_matching_direction_and_adds_bonus():
    bars = _bars()
    day = _last_day(bars)
    signals = [_signal("ma_golden_cross", day, "bullish")]
    without = tactics_score.score(signals, bars)
    with_bull = tactics_score.score(
        signals, bars, sector_context={"direction": "bullish", "ratio": 0.75}
    )
    with_bear = tactics_score.score(
        signals, bars, sector_context={"direction": "bearish", "ratio": 0.75}
    )
    assert without["score_parts"]["sector_bonus"] == 0.0
    assert without["sector_ratio"] is None
    assert with_bull["score_parts"]["sector_bonus"] > 0
    assert with_bull["tactic_score"] > without["tactic_score"]
    # 方向不一致 → 不加分（不给「板块普跌但个股看多」的票加分）
    assert with_bear["score_parts"]["sector_bonus"] == 0.0


def test_sector_contexts_thresholds():
    """横截面因子：同向占比 ≥0.6 且样本 ≥4 才产出上下文。"""
    def row(direction: str) -> dict[str, object]:
        return {"direction_bias": direction}

    strong = [row("bullish")] * 5 + [row("bearish")] * 1     # 5/6 ≈ 0.83
    contexts = tactics_score.sector_contexts(strong)
    assert "bullish" in contexts
    assert contexts["bullish"]["ratio"] == pytest.approx(0.833, abs=0.01)
    assert contexts["bullish"]["sample"] == 6

    split = [row("bullish")] * 3 + [row("bearish")] * 3      # 0.5 < 阈值
    assert tactics_score.sector_contexts(split) == {}

    tiny = [row("bullish")] * 2                              # 样本不足
    assert tactics_score.sector_contexts(tiny) == {}


# ---- E. 饱和曲线（不贴顶）----


def test_score_never_reaches_100_even_with_many_signals():
    """极端输入（全族同向 + 巨量 + 低位）也达不到 100：饱和曲线是渐近上限。"""
    bars = _bars(volume=500_000.0)
    bars[-1]["volume"] = 5_000_000.0
    day = _last_day(bars)
    all_bullish = [
        _signal(tactic_id, day, "bullish")
        for tactic_id, direction in (
            (item["id"], item["direction"]) for item in tactics.TACTICS
        )
        if direction == "bullish"
    ]
    result = tactics_score.score(all_bullish, bars)
    assert result["tactic_score"] < 100.0
    assert result["tactic_score"] > 60.0      # 极强共振仍应显著高于普通票
    assert result["score_parts"]["saturation_scale"] == tactics_score.SATURATION_SCALE


def test_score_formula_version_is_reported():
    bars = _bars()
    result = tactics_score.score([_signal("ma_golden_cross", _last_day(bars), "bullish")], bars)
    assert result["score_formula_version"] == tactics_score.SCORE_FORMULA_VERSION
    assert tactics_score.SCORE_FORMULA_VERSION == "v2"
    # v1 字段保留（语义改为饱和尺度），避免旧前端读数缺失
    assert result["score_parts"]["norm_scale"] == tactics_score.SATURATION_SCALE


# ---- F. 板块扫描端点（mode=sectors）----
#
# 约定：行情/板块取数一律 monkeypatch，绝不真实联网；
# 需要写模型凭据的用例走 _file_client（文件凭据后端），不污染系统凭据管理器。

# 5 只：per_sector 下限为 5，同时满足板块共振的 min_sample=4。
_SECTOR_MEMBERS = [
    {"symbol": "600176", "name": "中国巨石"},
    {"symbol": "300395", "name": "菲利华"},
    {"symbol": "603601", "name": "再升科技"},
    {"symbol": "600529", "name": "山东药玻"},
    {"symbol": "002080", "name": "中材科技"},
]


def test_sectors_endpoint_lists_industry_sectors(client, monkeypatch):
    """GET /tactics/sectors 是「按板块划分」的选项来源（code/name/领涨股）。"""
    from investment_steward_core.api import app as app_module

def _run_scan_to_done(test_client, headers, payload):
    """D01：POST 只建任务，这里轮询到终态并返回 summary（done）/任务体（cancelled/error）。"""
    import time as _time

    response = test_client.post("/tactics/scan-market", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    started = response.json()
    assert started["ok"] is True and started["state"] == "running"
    job_id = started["job_id"]
    for _ in range(150):
        _time.sleep(0.1)
        status_response = test_client.get(f"/tactics/scan-market/{job_id}", headers=headers).json()
        if status_response["state"] == "done":
            return status_response["summary"]
        if status_response["state"] in ("cancelled", "error"):
            return status_response
    raise AssertionError("扫描任务 15s 内未完成")


    test_client, headers = client
    monkeypatch.setattr(app_module, "fetch_cn_sector_list", lambda: [
        {"code": "new_blhy", "name": "玻璃行业", "member_count": 30, "change_pct": 2.1,
         "leader_symbol": "600176", "leader_name": "中国巨石"},
    ])
    response = test_client.get("/tactics/sectors", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "新浪财经 行业板块"
    assert body["sectors"][0]["code"] == "new_blhy"
    assert body["sectors"][0]["leader_name"] == "中国巨石"


def test_sectors_endpoint_reports_upstream_failure(client, monkeypatch):
    """板块清单取数失败如实转 502，不让前端拿到空列表还以为「今天没有板块」。"""
    from investment_steward_core.api import app as app_module
    from investment_steward_core.market_feed import FeedError

    test_client, headers = client

    def _boom() -> list[dict[str, object]]:
        raise FeedError("上游不可达")

    monkeypatch.setattr(app_module, "fetch_cn_sector_list", _boom)
    response = test_client.get("/tactics/sectors", headers=headers)
    assert response.status_code == 502
    assert "行业板块清单取数失败" in response.json()["detail"]


def test_scan_market_sectors_mode_groups_results_by_sector(client, tmp_path, monkeypatch):
    """mode=sectors：结果按板块分组返回，个股带板块归属，板块内横截面共振生效。"""
    from investment_steward_core.api import app as app_module
    from test_stock_research_tools import _file_client

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    rising = _bars(120, close_start=10.0, close_step=0.05)
    monkeypatch.setattr(
        app_module, "fetch_cn_kline", lambda symbol, limit=250, period="day": (rising, "test-provider")
    )
    monkeypatch.setattr(app_module, "fetch_cn_sector_list", lambda: [
        {"code": "new_blhy", "name": "玻璃行业", "member_count": 30, "change_pct": 2.1},
    ])
    monkeypatch.setattr(app_module, "fetch_cn_sector_members", lambda code, top=60: _SECTOR_MEMBERS[:top])

    body = _run_scan_to_done(test_client, headers, {"mode": "sectors", "sectors": ["new_blhy"], "per_sector": 5, "recent_bars": 10})
    assert body["mode"] == "sectors"
    assert body["score_formula_version"] == "v2"
    assert body["sector_resonance"] is True

    rows = [row for row in body["results"] if row["ok"]]
    assert len(rows) == 5
    assert all(row["sector_code"] == "new_blhy" for row in rows)
    assert all(row["sector_name"] == "玻璃行业" for row in rows)

    block = body["sectors"][0]
    assert block["code"] == "new_blhy" and block["ok"] is True
    assert block["scanned"] == 5 and block["failed"] == 0
    # 同一合成行情 → 方向全同 → 同向占比 1.0，共振应产出
    assert block["resonance"] is not None
    assert block["resonance"]["ratio"] == pytest.approx(1.0, abs=0.01)
    assert block["resonance"]["sample"] == 5
    assert any(row["score_parts"]["sector_bonus"] > 0 for row in rows)


def test_scan_market_sectors_mode_can_disable_resonance(client, tmp_path, monkeypatch):
    """sector_resonance=False：不得凭空产生板块加成（单票口径一致，可对照）。"""
    from investment_steward_core.api import app as app_module
    from test_stock_research_tools import _file_client

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    rising = _bars(120, close_start=10.0, close_step=0.05)
    monkeypatch.setattr(
        app_module, "fetch_cn_kline", lambda symbol, limit=250, period="day": (rising, "test-provider")
    )
    monkeypatch.setattr(app_module, "fetch_cn_sector_list", lambda: [])
    monkeypatch.setattr(app_module, "fetch_cn_sector_members", lambda code, top=60: _SECTOR_MEMBERS[:top])

    body = _run_scan_to_done(test_client, headers, {"mode": "sectors", "sectors": ["new_blhy"], "per_sector": 5,
                                                    "recent_bars": 10, "sector_resonance": False})
    assert body["sector_resonance"] is False
    assert body["sectors"][0]["resonance"] is None
    rows = [row for row in body["results"] if row["ok"]]
    assert rows
    assert all(row["score_parts"]["sector_bonus"] == 0.0 for row in rows)


def test_scan_market_sectors_mode_requires_sector_codes(client, tmp_path, monkeypatch):
    """没选板块就扫描 → 422 并指路 GET /tactics/sectors（不做「默认全市场」的隐式行为）。"""
    from test_stock_research_tools import _file_client

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    response = test_client.post(
        "/tactics/scan-market", headers=headers, json={"mode": "sectors", "sectors": []},
    )
    assert response.status_code == 422
    assert "sectors 为空" in response.json()["detail"]


def test_scan_market_sectors_mode_isolates_failing_sector(client, tmp_path, monkeypatch):
    """某个板块成分股取数失败：只废该板块的块，不影响其他板块（不整轮 502）。"""
    from investment_steward_core.api import app as app_module
    from investment_steward_core.market_feed import FeedError
    from test_stock_research_tools import _file_client

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    rising = _bars(120, close_start=10.0, close_step=0.05)
    monkeypatch.setattr(
        app_module, "fetch_cn_kline", lambda symbol, limit=250, period="day": (rising, "test-provider")
    )
    monkeypatch.setattr(app_module, "fetch_cn_sector_list", lambda: [
        {"code": "new_blhy", "name": "玻璃行业"},
        {"code": "new_zzbl", "name": "造纸行业"},
    ])

    def _members(code: str, top: int = 60) -> list[dict[str, object]]:
        if code == "new_zzbl":
            raise FeedError("成分股接口 503")
        return _SECTOR_MEMBERS[:top]

    monkeypatch.setattr(app_module, "fetch_cn_sector_members", _members)

    body = _run_scan_to_done(test_client, headers, {"mode": "sectors", "sectors": ["new_blhy", "new_zzbl"], "per_sector": 5, "recent_bars": 10})
    blocks = {block["code"]: block for block in body["sectors"]}
    assert blocks["new_blhy"]["ok"] is True and blocks["new_blhy"]["scanned"] == 5
    assert blocks["new_zzbl"]["ok"] is False
    assert "成分股取数失败" in blocks["new_zzbl"]["error"]
    assert {row["sector_code"] for row in body["results"] if row["ok"]} == {"new_blhy"}


# ---- G. 战法 AI 复核（手动触发，落库留痕）----


def test_ai_review_persists_then_lists_and_deletes(client, tmp_path, monkeypatch):
    """AI 复核：规则分与 AI 分并列落库（不互相覆盖），历史可查可删，供长期研究回看。"""
    import json

    from investment_steward_core import model_client as mc
    from investment_steward_core import tactics_ai
    from investment_steward_core.api import app as app_module
    from test_stock_research_tools import _file_client, _setup_active_profile

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    rising = _bars(120, close_start=10.0, close_step=0.05)
    monkeypatch.setattr(
        app_module, "fetch_cn_kline", lambda symbol, limit=250, period="day": (rising, "test-provider")
    )
    monkeypatch.setattr(app_module, "fetch_cn_sector_list", lambda: [
        {"code": "new_blhy", "name": "玻璃行业", "change_pct": 2.1, "member_count": 30},
    ])
    review_json = json.dumps({
        "ai_score": 72,
        "verdict": "bullish",
        "agreement": "partial",
        "summary": "突破形态成立但量能未同步放大",
        "strengths": ["均线多头排列", "回踩不破"],
        "concerns": ["量能背离", "逼近前高"],
        "fake_breakout_risk": "medium",
        "key_levels": {"support": 10.5, "resistance": 12.8},
        "watch_points": ["回踩 10.5 是否守住"],
        "rule_score_comment": "规则分略高估，量能确认未同步",
    }, ensure_ascii=False)
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": review_json}}]})

    created = test_client.post("/tactics/ai-review", headers=headers, json={
        "symbol": "600176", "sector_code": "new_blhy", "question": "这个突破能不能追",
    })
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["ok"] is True
    review = body["review"]
    assert review["symbol"] == "600176"
    assert review["ai_score"] == 72
    assert review["verdict"] == "bullish"
    # 规则分与 AI 分并存：规则分是排序依据，AI 分是可靠度复核
    assert review["rule_score"] is not None and review["rule_score"] != review["ai_score"]
    assert review["payload"]["prompt_version"] == tactics_ai.REVIEW_PROMPT_VERSION
    assert review["payload"]["score_formula_version"] == "v2"
    assert review["payload"]["question"] == "这个突破能不能追"
    assert "突破" in review["summary"]

    listed = test_client.get("/tactics/ai-reviews", headers=headers, params={"symbol": "600176"}).json()
    assert [item["review_id"] for item in listed] == [review["review_id"]]
    assert test_client.delete(f"/tactics/ai-reviews/{review['review_id']}", headers=headers).status_code == 204
    assert test_client.get("/tactics/ai-reviews", headers=headers, params={"symbol": "600176"}).json() == []
    assert test_client.delete(f"/tactics/ai-reviews/{review['review_id']}", headers=headers).status_code == 404


def test_ai_review_reports_unavailable_instead_of_502_when_nothing_usable(client, tmp_path, monkeypatch):
    """JV03（2026-09-21）行为变更：模型没按 schema 输出**不再 502**。

    以前 `normalize_review` 返回 None 就整次复核作废（用户白等一次模型调用、什么也没留下）。
    现在两层（Jev 判定 + chat 叙述）都拿不到结论时才报「本轮未复核」，且把原因说清楚——
    仍是 `ok: False`（前端照旧弹错误），但不再是一个 502 网关错误。
    """
    from investment_steward_core import model_client as mc
    from investment_steward_core.api import app as app_module
    from test_stock_research_tools import _file_client, _setup_active_profile

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    rising = _bars(120, close_start=10.0, close_step=0.05)
    monkeypatch.setattr(
        app_module, "fetch_cn_kline", lambda symbol, limit=250, period="day": (rising, "test-provider")
    )
    monkeypatch.setattr(app_module, "fetch_cn_sector_list", lambda: [])
    monkeypatch.setattr(
        mc, "_post_json",
        lambda *a, **k: {"choices": [{"message": {"content": "抱歉，我无法回答这个问题。"}}]},
    )
    response = test_client.post("/tactics/ai-review", headers=headers, json={"symbol": "600176"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["stage"] == "review_unavailable"
    assert body["engine"] == "none"
    assert body["narrative_status"] == "unparsable"
    # 原因仍要能看出「是提示词问题还是模型问题」——原文片段照旧回贴。
    assert "无法解析" in body["detail"]
    # 什么都没留下：不落库（不能把一次失败伪装成一条复核记录）。
    assert test_client.get("/tactics/ai-reviews", headers=headers).json() == []


def test_normalize_review_clamps_score_and_falls_back_to_neutral_enums():
    """归一化：非法枚举回退中性、分数 clamp、列表截断；无 summary 视为不可用。"""
    from investment_steward_core import tactics_ai

    review = tactics_ai.normalize_review({
        "ai_score": 180, "verdict": "乱写", "agreement": "乱写",
        "fake_breakout_risk": "乱写", "summary": "摘要",
        "strengths": ["a", "b", "c", "d", "e"],
        "key_levels": {"support": -1, "resistance": "abc"},
    })
    assert review["ai_score"] == 100.0
    assert review["verdict"] == "neutral"
    assert review["agreement"] == "partial"
    assert review["fake_breakout_risk"] == "medium"
    assert len(review["strengths"]) == 3                      # 列表截断
    assert review["key_levels"] == {"support": None, "resistance": None}   # 非法价位如实置空
    assert tactics_ai.normalize_review({"ai_score": 50}) is None           # 缺 summary → 不可用
    assert tactics_ai.normalize_review("不是对象") is None

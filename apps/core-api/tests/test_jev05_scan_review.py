"""JV05 契约测试：全市场扫描批量去误报（路线图 P1 第一站）。

覆盖三层：

1. **纯函数**（`tactics_ai`）：降级阶梯（削内容而不截断）、双重上限分片、全局编号、
   逐票标注的四种结果（通过 / 存疑 / 低置信 / 未取得）、统计行。
2. **胶水**（`jev_client.build_shard_reviewer`，JV05/JV08 共用）：总闸与场景开关的两层关闭、分片级指数退避
   只在可重试状态码上发生、退避序列正确、**单分片失败不影响其他分片**。
3. **端点**（`POST /tactics/scan-market` → 轮询）：逐票标注 + 汇总块落库、`purpose` 可查、
   **软校验铁律**（判为误报的票仍在结果里）、以及最要紧的
   **Jev 关闭时扫描链路零变化**（无标注字段、无内部暂存字段泄漏、零出网）。

最后一条是本文件的核心：JV00 铁律 6「关掉就真的关掉，不留半截行为」——
若关闭时仍多出字段或仍算 K 线摘要，这条会失败。
"""

from __future__ import annotations

import sqlite3
import time as time_module
from uuid import UUID

import pytest
from investment_steward_core import credential_store, jev_client, tactics_ai

# ——————————————————————————————————————————————————————————————
# 合成素材
# ——————————————————————————————————————————————————————————————

_ROW = {
    "symbol": "600176",
    "name": "中国巨石",
    "ok": True,
    "eligible": True,
    "direction_bias": "bullish",
    "tactic_score": 58.0,
    "score_formula_version": "v2",
    "volume_ratio": 1.4,
    "position": 0.72,
    "indicators": {"ma20": 10.5, "rsi14": 62.0, "macd": 0.13},
    "score_parts": {"form": 30.0, "resonance": 12.0},
    "hit_detail": [
        {
            "tactic_name": "放量突破",
            "direction": "bullish",
            "family": "形态族",
            "date": "2026-06-10",
            "age_bars": 3,
            "freshness": 0.9,
            "weight": 12,
            "contribution": 10.8,
            "trigger_price": 11.2,
            "invalidation_rule": "跌破 10.8",
            "counted": True,
        }
    ],
}


def _row(symbol: str = "600176", **overrides) -> dict[str, object]:
    return {**_ROW, "symbol": symbol, **overrides}


def _answer(question_id: str, choice: str, confidence: float | None = 0.71):
    return jev_client.JevAnswer(
        question_id=question_id, type="choice", choice=choice,
        probabilities={choice: confidence or 0.5}, confidence=confidence,
    )


# ——————————————————————————————————————————————————————————————
# 1. 纯函数：单票文本与降级阶梯
# ——————————————————————————————————————————————————————————————


def test_jev_scan_item_text_full_block_carries_whitelisted_evidence():
    text, degraded = tactics_ai.jev_scan_item_text(
        _row(), bars_tail="2026-06-10 O11.0 H11.3 L10.9 C11.2 V1200000"
    )
    assert degraded is None
    for marker in ("【标的】600176", "【命中明细】", "【指标现值】", "【近期 K 线（日）】", "触发价 11.2"):
        assert marker in text, marker


def test_jev_scan_item_text_degrades_instead_of_truncating():
    """超预算时按阶梯削内容，**绝不截断**——块尾的触发价/失效条件必须仍在。"""
    bars_tail = "\n".join(f"2026-06-{day:02d} O1 H2 L0.5 C1.5 V1000000" for day in range(1, 29))
    full, _ = tactics_ai.jev_scan_item_text(_row(), bars_tail=bars_tail)
    budget = len(full) - 200  # 刚好容不下完整块

    text, degraded = tactics_ai.jev_scan_item_text(_row(), bars_tail=bars_tail, max_chars=budget)
    assert degraded == "为塞进预算已去掉尾部 K 线摘要"
    assert "【近期 K 线（日）】" not in text
    assert "触发价 11.2" in text, "降级削掉的是 K 线，不是最关键判据"
    assert len(text) <= budget

    # 再压到连指标现值都放不下
    tighter, degraded2 = tactics_ai.jev_scan_item_text(
        _row(), bars_tail=bars_tail, max_chars=len(text) - 20
    )
    assert degraded2 == "为塞进预算已去掉尾部 K 线摘要与指标现值"
    assert "【指标现值】" not in tighter
    assert "触发价 11.2" in tighter


def test_jev_scan_item_text_returns_empty_when_even_trimmed_block_does_not_fit():
    text, degraded = tactics_ai.jev_scan_item_text(_row(), max_chars=10)
    assert text == ""
    assert degraded == "单票命中明细超预算，整票未送评"


def test_jev_scan_bars_tail_uses_pinned_tail_count():
    bars = [
        {"timestamp": f"2026-06-{index + 1:02d}", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
        for index in range(30)
    ]
    tail = tactics_ai.jev_scan_bars_tail(bars)
    assert len(tail.splitlines()) == tactics_ai.JEV_SCAN_BARS_TAIL
    assert tactics_ai.jev_scan_bars_tail([]) == "（无 K 线数据）"


# ——————————————————————————————————————————————————————————————
# 2. 纯函数：分片
# ——————————————————————————————————————————————————————————————


def test_jev_scan_shards_respects_per_shard_cap_and_keeps_global_index():
    items = [
        {"symbol": f"{600000 + i}", "name": f"票{i}", "row": _row(f"{600000 + i}"), "bars_tail": ""}
        for i in range(120)
    ]
    bundle = tactics_ai.jev_scan_shards(items, max_per_shard=50)
    assert [len(shard["evaluated"]) for shard in bundle["shards"]] == [50, 50, 20]
    # 全局编号连续 1..120，且题号与之对应
    indexes = [item["index"] for shard in bundle["shards"] for item in shard["evaluated"]]
    assert indexes == list(range(1, 121))
    first = bundle["shards"][0]
    assert "【候选 1】" in first["state"] and "【候选 50】" in first["state"]
    assert set(first["questions"]) == {
        f"c{index}_{tactics_ai.JEV_SCAN_QUESTION_SUFFIX}" for index in range(1, 51)
    }
    assert bundle["skipped"] == []


def test_jev_scan_shards_also_caps_state_chars():
    """字数上限独立生效：票数没到 50 也要按字数切。"""
    fat = _row()
    fat["hit_detail"] = fat["hit_detail"] * 40  # 单票约 2k+ 字
    items = [{"symbol": f"6000{i:02d}", "name": "票", "row": fat, "bars_tail": ""} for i in range(30)]
    bundle = tactics_ai.jev_scan_shards(
        items, max_per_shard=50, max_chars=9_000, item_max_chars=8_000
    )
    assert len(bundle["shards"]) > 1, "字数上限未生效"
    assert all(len(shard["state"]) <= 9_000 for shard in bundle["shards"])
    # 一票放不下当前片就开新片，而不是把它挤掉
    assert sum(len(shard["evaluated"]) for shard in bundle["shards"]) + len(bundle["skipped"]) == 30


def test_jev_scan_shards_reports_every_skip_reason_without_silent_drops():
    huge = _row()
    huge["hit_detail"] = huge["hit_detail"] * 200  # 单票独占一片仍超预算
    items = [{"symbol": "600001", "name": "超预算", "row": huge, "bars_tail": ""}]
    items += [
        {"symbol": f"6001{i:03d}", "name": "正常", "row": _row(), "bars_tail": ""}
        for i in range(tactics_ai.JEV_SCAN_MAX_CANDIDATES + 5)
    ]
    bundle = tactics_ai.jev_scan_shards(items)
    reasons = {item["reason"] for item in bundle["skipped"]}
    assert reasons == {"over_item_budget", "over_candidate_limit"}
    budget_skip = next(item for item in bundle["skipped"] if item["reason"] == "over_item_budget")
    assert budget_skip["symbol"] == "600001"
    limit_skips = [item for item in bundle["skipped"] if item["reason"] == "over_candidate_limit"]
    assert len(limit_skips) == 6, "超上限的每一票都要如实计入，不能只报一票"
    assert all("未送评" in item["note"] for item in limit_skips)


# ——————————————————————————————————————————————————————————————
# 3. 纯函数：标注合并
# ——————————————————————————————————————————————————————————————


def _shard_result(evaluated, answers, error=None):
    return {"evaluated": evaluated, "answers": answers, "error": error, "attempts": 1}


def test_jev_scan_annotations_marks_valid_with_confidence_as_reviewed():
    evaluated = [{"index": 1, "symbol": "600176", "name": "中国巨石"}]
    answers = {"c1_tactic_valid": _answer("c1_tactic_valid", "valid", 0.82)}
    annotations = tactics_ai.jev_scan_annotations([_shard_result(evaluated, answers)])
    got = annotations["600176"]
    assert got["verdict"] == "valid"
    assert got["review_state"] == tactics_ai.JEV_SCAN_STATE_REVIEWED
    assert got["label"] == "形态成立"


def test_jev_scan_annotations_folds_doubtful_invalid_and_low_confidence_into_pending():
    evaluated = [
        {"index": 1, "symbol": "600001", "name": "A"},
        {"index": 2, "symbol": "600002", "name": "B"},
        {"index": 3, "symbol": "600003", "name": "C"},
    ]
    answers = {
        "c1_tactic_valid": _answer("c1_tactic_valid", "invalid", 0.9),
        "c2_tactic_valid": _answer("c2_tactic_valid", "doubtful", 0.6),
        # 判了 valid 但置信度低于地板 → 仍进待核验，且 note 点明是「低置信」而不是「存疑」
        "c3_tactic_valid": _answer("c3_tactic_valid", "valid", 0.31),
    }
    annotations = tactics_ai.jev_scan_annotations([_shard_result(evaluated, answers)])
    assert annotations["600001"]["label"] == "疑似误报"
    assert annotations["600002"]["label"] == "形态存疑"
    for symbol in ("600001", "600002", "600003"):
        assert annotations[symbol]["review_state"] == tactics_ai.JEV_SCAN_STATE_PENDING
    assert "低于地板" in annotations["600003"]["note"]


def test_jev_scan_annotations_never_infers_a_verdict_from_failure():
    """分片失败或缺答 → `review_state=None`（没送出去评），**不等于形态成立**。"""
    evaluated = [{"index": 1, "symbol": "600176", "name": "中国巨石"}]
    failed = tactics_ai.jev_scan_annotations(
        [_shard_result(evaluated, {}, error="服务端返回 429（触发限流）")]
    )
    assert failed["600176"]["verdict"] is None
    assert failed["600176"]["review_state"] is None
    assert "不等于形态成立" in failed["600176"]["note"]

    missing = tactics_ai.jev_scan_annotations([_shard_result(evaluated, {})])
    assert missing["600176"]["verdict"] is None
    assert missing["600176"]["review_state"] is None


def test_jev_scan_stats_splits_reviewed_pending_and_unannotated():
    rows = [
        {"jev_review_state": tactics_ai.JEV_SCAN_STATE_REVIEWED, "jev_verdict": "valid"},
        {"jev_review_state": tactics_ai.JEV_SCAN_STATE_PENDING, "jev_verdict": "invalid"},
        {"jev_review_state": tactics_ai.JEV_SCAN_STATE_PENDING, "jev_verdict": "doubtful"},
        {"jev_review_state": None, "jev_verdict": None},
    ]
    stats = tactics_ai.jev_scan_stats(rows)
    assert stats == {
        "total": 4, "reviewed": 1, "pending": 2, "unannotated": 1,
        "verdicts": {"valid": 1, "doubtful": 1, "invalid": 1},
    }


# ——————————————————————————————————————————————————————————————
# 4. 胶水：build_shard_reviewer（JV05/JV08 共用）
# ——————————————————————————————————————————————————————————————


_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


class _FakeCore:
    def __init__(self, *, enabled: bool, access: bool):
        from types import SimpleNamespace

        self.database = _FakeDb()
        self.local_user_id = _USER_ID
        self.settings = SimpleNamespace(model_access_enabled=access)
        self._enabled = enabled

    def jev_settings(self):
        from investment_steward_core.domain.models import JevSettings

        return JevSettings(
            user_id=_USER_ID, enabled=self._enabled, base_url="https://openrouter.ai/api/alpha/decisions",
            model="typesafe/jev-1.13", credential_ref="jev_api_key",
        )


class _FakeDb:
    def __init__(self):
        self.saved = None

    def get_jev_settings(self, user_id):
        return self.saved


def _bundle(state: str, entries: list[tuple[str, int]]) -> dict[str, object]:
    """构造一份真实形状的分片包（题面走 `choice_question`，否则 `call_jev` 会在校验处抛错）。"""
    evaluated = [{"index": index, "symbol": symbol, "name": symbol} for symbol, index in entries]
    return {
        "shards": [{
            "state": state,
            "questions": tactics_ai.jev_scan_questions(evaluated),
            "evaluated": evaluated,
        }],
        "skipped": [],
    }


def _core(*, enabled: bool = True, access: bool = True) -> _FakeCore:
    core = _FakeCore(enabled=enabled, access=access)
    core.database.saved = core.jev_settings()
    return core


class _Store:
    def __init__(self, secret: str | None = "sk-jev"):
        self.secret = secret

    def get(self, key_id):
        return self.secret if key_id == credential_store.JEV_API_KEY else None

    def list_records(self):
        return []


def test_build_shard_reviewer_returns_none_when_disabled_or_gate_closed():
    assert jev_client.build_shard_reviewer(_core(enabled=False), _Store(), purpose="jev:scan-review") is None
    assert jev_client.build_shard_reviewer(_core(access=False), _Store(), purpose="jev:scan-review") is None
    assert jev_client.build_shard_reviewer(_core(), _Store(), purpose="jev:scan-review") is not None


def test_build_shard_reviewer_does_not_touch_network_when_no_shards(monkeypatch):
    monkeypatch.setattr(jev_client, "_post_endpoint", lambda *a, **k: pytest.fail("不应出网"))
    reviewer = jev_client.build_shard_reviewer(_core(), _Store(), purpose="jev:scan-review", sleep=lambda _s: None)
    assert reviewer({"shards": [], "skipped": []}) == []


def test_build_shard_reviewer_backs_off_only_on_retryable_status(monkeypatch):
    import urllib.error

    sleeps: list[float] = []
    attempts: list[int] = []

    def _rate_limited(*_args, **_kwargs):
        attempts.append(1)
        raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(jev_client, "_post_endpoint", _rate_limited)
    reviewer = jev_client.build_shard_reviewer(_core(), _Store(), purpose="jev:scan-review", sleep=sleeps.append)
    bundle = _bundle("x", [("600176", 1)])
    results = reviewer(bundle)
    assert len(attempts) == jev_client.JEV_SHARD_MAX_ATTEMPTS, "429 应退避重试到上限"
    assert sleeps == [1.5, 3.0], "退避应是指数序列 1.5 → 3.0"
    assert results[0]["attempts"] == jev_client.JEV_SHARD_MAX_ATTEMPTS
    assert results[0]["error"] and "429" in results[0]["error"]
    assert results[0]["answers"] == {}

    # 401 是确定性失败：重试没有意义，只试一次且不睡
    sleeps.clear()
    attempts.clear()

    def _unauthorized(*_args, **_kwargs):
        attempts.append(1)
        raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(jev_client, "_post_endpoint", _unauthorized)
    reviewer = jev_client.build_shard_reviewer(_core(), _Store(), purpose="jev:scan-review", sleep=sleeps.append)
    results = reviewer(bundle)
    assert len(attempts) == 1
    assert sleeps == []
    assert results[0]["attempts"] == 1


def test_build_shard_reviewer_isolates_shard_failures(monkeypatch):
    """一个分片挂了，另一个分片的结果照样交付——不是「整轮作废」。"""
    import urllib.error

    calls: list[str] = []

    def _flaky(_base_url, payload, _api_key, _timeout):
        first = payload["state"]
        calls.append(first)
        if first == "bad":
            raise urllib.error.HTTPError("u", 422, "Unprocessable", {}, None)
        return {
            "model": "typesafe/jev-1.13-20260917",
            "answers": {"c2_tactic_valid": {"type": "choice", "choice": "valid", "probabilities": {"valid": 0.9}, "confidence": 0.9}},
            "usage": {"input_tokens": 900, "output_tokens": 8},
        }

    monkeypatch.setattr(jev_client, "_post_endpoint", _flaky)
    reviewer = jev_client.build_shard_reviewer(_core(), _Store(), purpose="jev:scan-review", sleep=lambda _s: None)
    bundle = _bundle("bad", [("600001", 1)])
    bundle["shards"].extend(_bundle("good", [("600002", 2)])["shards"])
    results = reviewer(bundle)
    assert results[0]["error"] and results[0]["answers"] == {}
    assert results[1]["error"] is None
    assert results[1]["answers"]["c2_tactic_valid"].choice == "valid"
    assert results[1]["model"] == "typesafe/jev-1.13-20260917"

    annotations = tactics_ai.jev_scan_annotations(results)
    assert annotations["600001"]["review_state"] is None
    assert annotations["600002"]["review_state"] == tactics_ai.JEV_SCAN_STATE_REVIEWED


# ——————————————————————————————————————————————————————————————
# 5. 端点：三模式统一接线
# ——————————————————————————————————————————————————————————————

_SCAN_BODY = {"mode": "boards", "boards": ["turnover"], "per_board": 30, "max_symbols": 60, "recent_bars": 60}


def _bars(count: int = 120) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    price = 10.0
    for index in range(count):
        price = round(price * 1.01, 2)
        rows.append({
            "timestamp": f"2026-06-{(index % 28) + 1:02d}",
            "open": price * 0.99, "close": price, "high": price * 1.02,
            "low": price * 0.98, "volume": 1_000_000 + index * 1_000,
        })
    return rows


def _client(tmp_path, monkeypatch, *, jev_enabled: bool):
    from investment_steward_core.api import app as api_app
    from test_stock_research_tools import _file_client, _setup_active_profile

    http, headers = _file_client(tmp_path, monkeypatch)
    assert http.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(http, headers)
    monkeypatch.setattr(
        api_app, "fetch_cn_market_board",
        lambda board, top=30: [
            {"symbol": "600176", "name": "中国巨石", "price": 11.2, "change_pct": 1.2,
             "volume": 1e6, "turnover": 1e8, "turnover_rate": 1.0, "volume_ratio": 1.1},
            {"symbol": "600519", "name": "贵州茅台", "price": 1680.0, "change_pct": 0.3,
             "volume": 1e6, "turnover": 1e8, "turnover_rate": 0.4, "volume_ratio": 1.0},
        ],
    )
    monkeypatch.setattr(api_app, "fetch_cn_kline", lambda symbol, limit=250, period="day": (_bars(120), "test-provider"))
    if jev_enabled:
        assert http.put(
            f"/credentials/{credential_store.JEV_API_KEY}", json={"secret": "sk-jev-secret"}, headers=headers
        ).status_code == 200
        saved = http.put("/jev/config", headers=headers, json={
            "enabled": True,
            "base_url": "https://openrouter.ai/api/alpha/decisions",
            "model": "typesafe/jev-1.13",
            "credential_ref": credential_store.JEV_API_KEY,
            "timeout_secs": 60,
        })
        assert saved.status_code == 200, saved.text
    return http, headers


def _run_scan(http, headers) -> dict:
    started = http.post("/tactics/scan-market", headers=headers, json=_SCAN_BODY).json()
    assert started["ok"] is True, started
    deadline = time_module.monotonic() + 20.0
    while time_module.monotonic() < deadline:
        body = http.get(f"/tactics/scan-market/{started['job_id']}", headers=headers).json()
        if body["state"] in ("done", "cancelled", "error"):
            return body
        time_module.sleep(0.05)
    raise AssertionError("扫描任务未在时限内到达终态")


def _fake_jev(*, invalid_symbols: frozenset[str] = frozenset({"600519"})):
    """按 state 里的标的决定选项——**不依赖排序**，测试因此不会因为榜单次序变化而假通过。

    `invalid_symbols` 里的票回 `invalid`（构造「误报」场景），其余回 `valid` 高置信。
    同时把「候选 N 属于哪只票」解析出来回传，便于断言题号与标的的对应关系。
    """
    calls: list[dict[str, object]] = []
    seen: dict[str, str] = {}

    def _fake(_base_url, payload, _api_key, _timeout):
        calls.append(payload)
        answers = {}
        for block in str(payload["state"]).split("【候选 "):
            if not block.strip():
                continue
            index = block.split("】", 1)[0].strip()
            symbol = block.split("【标的】", 1)[1].split(" ", 1)[0].strip() if "【标的】" in block else ""
            if not symbol:
                continue
            seen[symbol] = index
            choice = "invalid" if symbol in invalid_symbols else "valid"
            confidence = 0.88 if choice == "invalid" else 0.91
            question_id = f"c{index}_{tactics_ai.JEV_SCAN_QUESTION_SUFFIX}"
            answers[question_id] = {
                "type": "choice", "choice": choice,
                "probabilities": {choice: confidence}, "confidence": confidence,
            }
        return {
            "model": "typesafe/jev-1.13-20260917",
            "answers": answers,
            "usage": {"input_tokens": 1200, "output_tokens": 16},
        }

    return _fake, calls, seen


def test_scan_market_annotates_rows_and_reports_jev_review(tmp_path, monkeypatch):
    http, headers = _client(tmp_path, monkeypatch, jev_enabled=True)
    fake, calls, seen = _fake_jev()
    monkeypatch.setattr(jev_client, "_post_endpoint", fake)

    body = _run_scan(http, headers)
    assert body["state"] == "done"
    summary = body["summary"]
    rows = {row["symbol"]: row for row in summary["results"]}
    assert len(calls) == 1, "两票应合成一个分片、只出网一次"
    assert seen == {"600176": "1", "600519": "2"}, "题号与标的的对应关系必须正确"

    assert rows["600176"]["jev_verdict"] == "valid"
    assert rows["600176"]["jev_review_state"] == tactics_ai.JEV_SCAN_STATE_REVIEWED
    assert rows["600176"]["jev_label"] == "形态成立"
    assert rows["600519"]["jev_verdict"] == "invalid"
    assert rows["600519"]["jev_label"] == "疑似误报"
    assert rows["600519"]["jev_review_state"] == tactics_ai.JEV_SCAN_STATE_PENDING

    review = summary["jev_review"]
    assert review["enabled"] is True
    assert review["model"] == "typesafe/jev-1.13-20260917"
    assert review["total"] == 2 and review["reviewed"] == 1 and review["pending"] == 1
    assert review["unannotated"] == 0
    assert review["verdicts"] == {"valid": 1, "doubtful": 0, "invalid": 1}
    assert review["shards"] == 1

    # 归集口径：`purpose` 必须可查（与 chat 链路同表同钩子）
    db = http.app.state.core.database
    with sqlite3.connect(db.path) as raw:
        records = list(raw.execute("SELECT purpose, outcome FROM model_calls"))
    assert ("jev:scan-review", "ok") in records


def test_scan_market_soft_check_never_deletes_or_reorders_candidates(tmp_path, monkeypatch):
    """软校验铁律：判为「疑似误报」的票**仍然在结果里**，只是被标注、折叠进待核验。"""
    http, headers = _client(tmp_path, monkeypatch, jev_enabled=True)
    fake, _calls, _seen = _fake_jev(invalid_symbols=frozenset({"600176", "600519"}))
    monkeypatch.setattr(jev_client, "_post_endpoint", fake)

    summary = _run_scan(http, headers)["summary"]
    symbols = [row["symbol"] for row in summary["results"]]
    assert sorted(symbols) == ["600176", "600519"], "候选被删除了"
    assert summary["scanned"] == 2
    assert all(row["jev_verdict"] == "invalid" for row in summary["results"])
    assert all(row["jev_review_state"] == tactics_ai.JEV_SCAN_STATE_PENDING for row in summary["results"])


def test_scan_market_shard_failure_is_annotated_honestly(tmp_path, monkeypatch):
    """出网失败 → 逐票如实标「未取得判定」，**不假装形态成立**，也不影响扫描结果本身。"""
    import urllib.error

    http, headers = _client(tmp_path, monkeypatch, jev_enabled=True)

    def _boom(*_args, **_kwargs):
        raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(jev_client, "_post_endpoint", _boom)
    summary = _run_scan(http, headers)["summary"]
    assert summary["scanned"] == 2, "扫描本身不该受 Jev 失败影响"
    review = summary["jev_review"]
    assert review["enabled"] is True and review["reviewed"] == 0 and review["unannotated"] == 2
    for row in summary["results"]:
        assert row["jev_review_state"] is None
        assert row["jev_label"] == "未取得判定"
        assert "不等于形态成立" in row["jev_note"]


def test_scan_market_zero_change_when_jev_disabled(tmp_path, monkeypatch):
    """**核心验收**：Jev 关闭时扫描链路零变化——无标注字段、无内部暂存字段、零出网。"""
    http, headers = _client(tmp_path, monkeypatch, jev_enabled=False)
    monkeypatch.setattr(
        jev_client, "_post_endpoint", lambda *a, **k: pytest.fail("Jev 关闭时不得出网")
    )
    summary = _run_scan(http, headers)["summary"]
    assert summary["scanned"] == 2
    assert summary["jev_review"]["enabled"] is False
    assert summary["jev_review"]["note"] and "未启用" in summary["jev_review"]["note"]
    for row in summary["results"]:
        # JV05 往行里加的全部是 `jev_*`，暂存字段是 `_` 前缀——关闭时这两类一个都不能有。
        leaked = sorted(key for key in row if key.startswith(("jev", "_")))
        assert leaked == [], f"Jev 关闭时不该出现这些字段：{leaked}"
    # summary 只多一个显式的「未启用」标记（与 JV04 的 `jev.available=False` 同惯例），
    # 不是「半截行为」——它让前端能区分「复核没跑」与「复核跑了但没标注」。
    assert summary["jev_review"]["note"].startswith("Jev 未启用")


def test_scan_market_marks_skipped_rows_with_reason(tmp_path, monkeypatch):
    """超出单轮上限的票如实标「未送评」+ 原因，不留一个说不清的空白。"""
    http, headers = _client(tmp_path, monkeypatch, jev_enabled=True)
    monkeypatch.setattr(tactics_ai, "JEV_SCAN_MAX_CANDIDATES", 1)
    fake, calls, _seen = _fake_jev()
    monkeypatch.setattr(jev_client, "_post_endpoint", fake)

    summary = _run_scan(http, headers)["summary"]
    assert len(calls) == 1
    review = summary["jev_review"]
    assert len(review["skipped"]) == 1
    skipped = review["skipped"][0]
    assert skipped["reason"] == "over_candidate_limit"
    row = next(item for item in summary["results"] if item["symbol"] == skipped["symbol"])
    assert row["jev_review_state"] is None
    assert row["jev_label"] == "未送评"
    assert "上限" in row["jev_note"]


# ——————————————————————————————————————————————————————————————
# JV05 补接：`POST /tactics/scan`（观察清单 + 持仓 + 手动列表）
#
# 真机验证时才发现漏了这条：`/tactics/scan-market`（boards/all/sectors 三模式）走
# `_scan_run_engine`，而 `/tactics/scan` 是**自己的**扫描循环。不单独接一次，「扫描去误报」
# 就没覆盖 2026-09-07 拍板的**日常主用范围**——用户最常点的是这个，不是全市场扫描。
# ——————————————————————————————————————————————————————————————


def _run_watchlist_scan(http, headers) -> dict:
    """`/tactics/scan` 是同步端点，用 manual 源指定两只票，不依赖观察清单/持仓的库状态。"""
    return http.post(
        "/tactics/scan",
        headers=headers,
        json={"sources": ["manual"], "symbols": ["600176", "600519"], "recent_bars": 60},
    ).json()


def test_tactics_scan_annotates_rows_and_reports_jev_review(tmp_path, monkeypatch):
    """日常扫描路径同样要带语义复核——否则「去误报」只覆盖全市场扫描那一半。"""
    http, headers = _client(tmp_path, monkeypatch, jev_enabled=True)
    fake, calls, seen = _fake_jev()
    monkeypatch.setattr(jev_client, "_post_endpoint", fake)

    body = _run_watchlist_scan(http, headers)
    rows = {row["symbol"]: row for row in body["results"]}

    assert len(calls) == 1, "两票应合成一个分片、只出网一次"
    assert seen == {"600176": "1", "600519": "2"}
    assert rows["600176"]["jev_verdict"] == "valid"
    assert rows["600176"]["jev_review_state"] == tactics_ai.JEV_SCAN_STATE_REVIEWED
    assert rows["600519"]["jev_verdict"] == "invalid"
    assert rows["600519"]["jev_review_state"] == tactics_ai.JEV_SCAN_STATE_PENDING

    review = body["jev_review"]
    assert review["enabled"] is True
    assert review["total"] == 2
    # 软校验铁律：只加标注，**不重排、不删除**——两只票都还在，顺序仍按 rank_score。
    assert [row["symbol"] for row in body["results"]] == ["600176", "600519"]


def test_tactics_scan_purpose_is_recorded_for_cost_reconciliation(tmp_path, monkeypatch):
    """`purpose` 落 `model_calls`，JV10 按它分场景对账——两条扫描路径必须同一个 purpose。"""
    import sqlite3

    http, headers = _client(tmp_path, monkeypatch, jev_enabled=True)
    fake, _calls, _seen = _fake_jev()
    monkeypatch.setattr(jev_client, "_post_endpoint", fake)

    _run_watchlist_scan(http, headers)

    db = http.app.state.core.database
    with sqlite3.connect(db.path) as conn:
        rows = conn.execute("SELECT purpose FROM model_calls").fetchall()
    assert ("jev:scan-review",) in rows


def test_tactics_scan_zero_change_when_jev_disabled(tmp_path, monkeypatch):
    """零变化开关：日常扫描关闭时行内一个 `jev*`/`_*` 字段都不能有，且零出网。"""
    http, headers = _client(tmp_path, monkeypatch, jev_enabled=False)
    monkeypatch.setattr(jev_client, "_post_endpoint", lambda *a, **k: pytest.fail("Jev 关闭时不得出网"))

    body = _run_watchlist_scan(http, headers)
    assert body["scanned"] == 2
    assert body["jev_review"]["enabled"] is False
    for row in body["results"]:
        leaked = sorted(key for key in row if key.startswith(("jev", "_")))
        assert leaked == [], f"Jev 关闭时不该出现这些字段：{leaked}"


def test_tactics_scan_empty_candidates_keeps_response_shape(tmp_path, monkeypatch):
    """空候选也带 `jev_review`——否则前端分不清「没跑」和「没票可评」。"""
    http, headers = _client(tmp_path, monkeypatch, jev_enabled=True)
    monkeypatch.setattr(jev_client, "_post_endpoint", lambda *a, **k: pytest.fail("没票可评时不得出网"))

    body = http.post(
        "/tactics/scan", headers=headers, json={"sources": ["manual"], "symbols": [], "recent_bars": 60}
    ).json()
    assert body["scanned"] == 0
    assert "jev_review" in body, "形状要与主路径一致"

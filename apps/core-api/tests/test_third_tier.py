"""第三梯队回归：Q1 / T8 / T9 / Q2 / Q3。

本文件集中锁住五处**口径类**修复。它们与前两批的差别在于：改错了不会报错，
只会安静地给出**别的答案**——所以每条断言都比对「可复算的值」，而不只是「没抛异常」。
"""

from __future__ import annotations

import itertools
import threading
import time
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from investment_steward_core import instrument_lookup, jev_client, quant_models
from investment_steward_core.api.app import _RequestPacer

# ---------------------------------------------------------------- Q1：λ 选择口径


def _synthetic_bars(count: int) -> list[dict[str, Any]]:
    """构造一段量纲差异极大的 K 线：成交量放大到百万级，收盘价在 10 元附近。

    正是这种量纲差异让「用原始特征打分」与「用标准化特征打分」给出不同的排序。
    """
    bars: list[dict[str, Any]] = []
    for i in range(count):
        close = 10.0 + 0.05 * i + 0.3 * ((i % 7) - 3)
        bars.append({
            "timestamp": f"2026-01-{(i % 28) + 1:02d}T00:00:00+00:00",
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": 1_000_000 + i * 37_000,
        })
    return bars


def test_lambda_selection_scores_on_standardized_features():
    """Q1 核心：选 λ 时的打分口径必须与出厂模型（`standardization=stats`）一致。

    做法是直接检验 `_select_lambda` 的入参契约——不依赖「选出来的 λ 恰好变了」，
    因为 λ 相同并不代表口径正确（那只是这个样本上恰好没差）。
    """
    import inspect

    signature = inspect.signature(quant_models._select_lambda)
    assert "standardization" in signature.parameters, "λ 选择必须能拿到标准化参数"
    assert signature.parameters["standardization"].kind is inspect.Parameter.KEYWORD_ONLY


def test_lambda_selection_uses_same_scores_as_factory(monkeypatch):
    """λ 选择与出厂打分喂给 `score_series` 的 standardization 必须是同一份。"""
    seen: list[Any] = []
    real_score = quant_models.score_series

    def _spy(weights, bars, **kwargs):
        seen.append(kwargs.get("standardization"))
        return real_score(weights, bars, **kwargs)

    monkeypatch.setattr(quant_models, "score_series", _spy)
    bars = _synthetic_bars(200)
    rows = quant_models.design_matrix(bars)
    stats = quant_models.standardize_fit(rows[:60])
    train_z = quant_models.standardize_apply(rows[:60], stats)

    quant_models._select_lambda(
        bars, [float(b["close"]) for b in bars], train_z, [0.01] * 60,
        prefix_end=100, lambda_grid=(0.1, 1.0), standardization=stats,
    )
    assert seen, "没有观察到 score_series 调用"
    assert all(item is stats for item in seen), f"选 λ 时未用训练集统计量打分：{seen}"


def test_train_passes_stats_into_lambda_selection(monkeypatch):
    """Q1 真正的回归点：**调用方** `train()` 必须把训练集统计量传进选 λ。

    前两条测试直接调 `_select_lambda`，只锁住了「函数能接收 standardization」这个契约；
    把 `train()` 里的实参删掉，它们**照样全绿**——这正是 S2 那次「测试没失败」同款陷阱。
    本条走真实调用链：只观察 `train` 期间 `score_series` 收到的 standardization，
    在 λ 选择阶段必须非空。
    """
    seen: list[Any] = []
    real_score = quant_models.score_series

    def _spy(weights, bars, **kwargs):
        seen.append(kwargs.get("standardization"))
        return real_score(weights, bars, **kwargs)

    monkeypatch.setattr(quant_models, "score_series", _spy)
    quant_models.train(_synthetic_bars(260))

    assert seen, "train 期间没有观察到 score_series 调用"
    # 全程都不得出现 standardization=None：出厂打分与选 λ 必须同口径
    assert all(item is not None for item in seen), (
        f"选 λ 或出厂打分仍有一处用原始特征：None 出现 {seen.count(None)} 次"
    )


def test_raw_versus_standardized_scores_differ():
    """对照组：证明「标准化」确实会改变分数——否则 Q1 的修复没有实际效果。"""
    bars = _synthetic_bars(120)
    rows = quant_models.design_matrix(bars)
    stats = quant_models.standardize_fit(rows[:60])
    train_z = quant_models.standardize_apply(rows[:60], stats)
    weights = quant_models.ridge_fit(train_z, [0.01] * 60, 0.1)
    raw = quant_models.score_series(weights, bars)
    z = quant_models.score_series(weights, bars, standardization=stats)
    assert raw != z, "两种口径给出相同分数，Q1 的前提不成立（请复核 fixture）"


# ---------------------------------------------------------------- T8：间隔语义


def test_pacer_enforces_minimum_gap_under_concurrency():
    """T8 核心：并发下任意两次「放行」的间隔仍不小于设定值。

    改造前每个 worker 在自己开头各睡一次，concurrency=4、interval=0.15 的真实行为是
    「4 个请求齐发」——本测试用并发线程模拟那个场景。
    """
    pace = _RequestPacer(0.08)
    stamps: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        pace.wait()
        with lock:
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.monotonic() - started

    assert len(stamps) == 8
    stamps.sort()
    gaps = [b - a for a, b in itertools.pairwise(stamps)]
    # 允许极小的调度误差，但不能出现「齐发」。
    assert min(gaps) >= 0.08 * 0.8, f"出现齐发，最小间隔 {min(gaps):.4f}s"
    assert elapsed >= 0.08 * 7 * 0.8, "总耗时说明限速没有生效"


def test_pacer_zero_interval_does_not_sleep():
    pace = _RequestPacer(0.0)
    started = time.monotonic()
    for _ in range(50):
        pace.wait()
    assert time.monotonic() - started < 0.5, "interval=0 时不该有任何等待"


def test_pacer_first_request_does_not_wait():
    """第一个请求不等待（与原串行路径「第一票不睡」一致）。"""
    pace = _RequestPacer(1.5)
    started = time.monotonic()
    pace.wait()
    assert time.monotonic() - started < 0.3, "首个请求不应被 interval 拖慢"


def _scan_market_rows() -> list[dict[str, Any]]:
    return [
        {"symbol": f"60000{i}", "name": f"股{i}", "price": 10.0 + i, "change_pct": 3.0,
         "volume": 1000, "turnover": 5.0e8, "turnover_rate": 5.0}
        for i in range(1, 7)
    ]


def _scan_bars() -> list[dict[str, Any]]:
    rows = []
    price = 10.0
    for index in range(60):
        price = round(price * 1.01, 2)
        rows.append({
            "timestamp": f"2026-06-{(index % 28) + 1:02d}",
            "open": price * 0.99, "close": price, "high": price * 1.02,
            "low": price * 0.98, "volume": 1_000_000 + index * 1000,
        })
    return rows


def test_concurrent_scan_still_rate_limits_by_interval(tmp_path, monkeypatch):
    """T8 真正的回归点（走真实端点）：concurrency=4 时取数**发起时刻**仍受 interval 约束。

    改造前并发路径给每个 worker 各睡一次，于是 4 个请求**齐发**——本测试把
    `fetch_cn_kline` 的调用时刻记下来，断言相邻两次至少隔开 interval。
    这是「间隔」这个设置第一次真的生效的证明。
    """
    from fastapi.testclient import TestClient
    from investment_steward_core.api import app as api_app
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    test_client = TestClient(create_app(CoreSettings(session_token="t", data_dir=tmp_path)))
    headers = {"X-Core-Session-Token": "t"}
    test_client.post("/plugins/official.cn-market-data/install", headers=headers)

    monkeypatch.setattr(
        api_app, "fetch_cn_market_full_a", lambda max_pages=40: (_scan_market_rows(), True)
    )
    starts: list[float] = []
    lock = threading.Lock()

    def _timed_fetch(symbol, limit=250, period="day"):
        with lock:
            starts.append(time.monotonic())
        return (_scan_bars(), "test-provider")

    monkeypatch.setattr(api_app, "fetch_cn_kline", _timed_fetch)

    interval = 0.05
    response = test_client.post("/tactics/scan-market", headers=headers, json={
        "mode": "all", "top_symbols": 6, "recent_bars": 5,
        "concurrency": 4, "interval_secs": interval,
    })
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    for _ in range(400):
        time.sleep(0.05)
        status = test_client.get(f"/tactics/scan-market/{job_id}", headers=headers).json()
        if status["state"] in ("done", "error"):
            break
    assert status["state"] == "done", status

    assert len(starts) >= 4, f"取数次数过少，测不出限速：{len(starts)}"
    starts.sort()
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    # 允许调度误差；改造前并发齐发的间隔会接近 0
    assert min(gaps) >= interval * 0.8, f"并发下仍出现齐发，最小间隔 {min(gaps):.4f}s：{gaps}"


# ---------------------------------------------------------------- T9：查找缓存


def test_lookup_failure_is_not_cached(monkeypatch):
    """T9 核心：取数失败**不得**写缓存，否则网络抖动后 60 秒内重试永远拿不到结果。"""
    instrument_lookup.reset_lookup_cache()
    calls = {"n": 0}

    def _boom(query: str) -> list[dict[str, str]]:
        calls["n"] += 1
        raise TimeoutError("供应方不可达")

    monkeypatch.setattr(instrument_lookup, "_fetch_em", _boom)
    monkeypatch.setattr(instrument_lookup, "_fetch_tx", _boom)

    assert instrument_lookup.lookup_instruments("平安银行") == []
    assert instrument_lookup.lookup_instruments("平安银行") == []
    assert calls["n"] == 4, "失败被缓存了：第二次查询没有真正重试"
    assert instrument_lookup._cache == {}, "失败结果不应留在缓存里"


def test_lookup_success_with_no_hit_is_cached(monkeypatch):
    """对照：**成功但无命中**要缓存——否则每敲一个字都问一遍供应方。"""
    instrument_lookup.reset_lookup_cache()
    calls = {"n": 0}

    def _empty(query: str) -> list[dict[str, str]]:
        calls["n"] += 1
        return []

    monkeypatch.setattr(instrument_lookup, "_fetch_em", _empty)
    monkeypatch.setattr(instrument_lookup, "_fetch_tx", _empty)

    assert instrument_lookup.lookup_instruments("不存在XYZ") == []
    before = calls["n"]
    assert instrument_lookup.lookup_instruments("不存在XYZ") == []
    assert calls["n"] == before, "「查无此票」是有效答案，应命中缓存"
    instrument_lookup.reset_lookup_cache()


def test_lookup_cache_is_bounded(monkeypatch):
    """缓存必须有上限：搜索框逐字触发，无界 dict 会一直涨。"""
    instrument_lookup.reset_lookup_cache()

    def _one(query: str) -> list[dict[str, str]]:
        return [{"key": "600000", "name": "浦发银行", "kind": "stock", "market": "sh", "display": "浦发银行（600000）"}]

    monkeypatch.setattr(instrument_lookup, "_fetch_em", _one)
    monkeypatch.setattr(instrument_lookup, "_fetch_tx", _one)

    for i in range(instrument_lookup._CACHE_MAX_ENTRIES + 80):
        instrument_lookup.lookup_instruments(f"查询词{i}")
    assert len(instrument_lookup._cache) <= instrument_lookup._CACHE_MAX_ENTRIES, (
        f"缓存无上限：{len(instrument_lookup._cache)} 条"
    )
    instrument_lookup.reset_lookup_cache()


def test_lookup_endpoint_requires_session(client):
    """T9：查找端点会外呼第三方，必须与其它业务端点同级鉴权。"""
    test_client, _ = client
    assert test_client.get("/instruments/lookup", params={"q": "平安银行"}).status_code == 401


def test_lookup_endpoint_authorized_still_works(client):
    """加守卫不能把功能弄坏：带会话令牌仍应正常返回。"""
    test_client, headers = client
    response = test_client.get("/instruments/lookup", params={"q": ""}, headers=headers)
    assert response.status_code == 200
    assert response.json() == []


# ---------------------------------------------------------------- Q3：Jev 急停


class _Core:
    """最小 core 替身：只提供 `effective_settings` 实际读到的三个成员。"""

    def __init__(self, saved: Any, env_enabled: bool) -> None:
        # local_user_id 必须是 UUID（JevSettings.user_id 的类型约束）
        self.local_user_id = UUID("11111111-1111-1111-1111-111111111111")
        self.database = self
        self.settings = self
        self._saved = saved
        self.jev_enabled = env_enabled
        self.jev_base_url = "https://jev.example"
        self.jev_model = "systemone"
        self.jev_credential_ref = "jev_api_key"
        self.jev_timeout_secs = 20.0

    def get_jev_settings(self, _user_id: Any) -> Any:
        return self._saved


def _settings(enabled: bool) -> Any:
    from investment_steward_core.domain.models import JevSettings

    return JevSettings(
        user_id=UUID("11111111-1111-1111-1111-111111111111"), enabled=enabled, base_url="https://saved.example",
        model="saved-model", credential_ref="k", timeout_secs=9.0,
    )


def test_env_kill_switch_beats_saved_enabled(monkeypatch):
    """Q3 核心：设置页已启用 + 显式 `STEWARD_JEV_ENABLED=0` ⇒ 必须以「关」为准。

    改造前保存行直接盖过环境变量，于是「我关了它」与「数据仍在出境」可以并存。
    """
    monkeypatch.setenv("STEWARD_JEV_ENABLED", "0")
    core = _Core(_settings(True), env_enabled=False)
    assert jev_client.effective_settings(core).enabled is False


def test_env_absent_does_not_trigger_kill_switch(monkeypatch):
    """未设置环境变量 ≠ 显式关闭：这时仍走 JV02 原排序（保存行优先）。"""
    monkeypatch.delenv("STEWARD_JEV_ENABLED", raising=False)
    core = _Core(_settings(True), env_enabled=False)
    assert jev_client.effective_settings(core).enabled is True
    assert jev_client.effective_settings(core).model == "saved-model"


def test_env_enable_does_not_override_saved_disable(monkeypatch):
    """反向也不许翻车：`=1` 不能覆盖设置页里的显式关闭。"""
    monkeypatch.setenv("STEWARD_JEV_ENABLED", "1")
    core = _Core(_settings(False), env_enabled=True)
    assert jev_client.effective_settings(core).enabled is False


def test_kill_switch_uses_config_parsing_rule(monkeypatch):
    """急停判定必须与 `config.py` 的解析口径一致：只有恰为 "1" 才算开启。"""
    for value in ("0", "false", "False", "", "yes", " 1"):
        monkeypatch.setenv("STEWARD_JEV_ENABLED", value)
        core = _Core(_settings(True), env_enabled=True)
        assert jev_client.effective_settings(core).enabled is False, f"{value!r} 应判为关闭"
    monkeypatch.setenv("STEWARD_JEV_ENABLED", "1")
    core = _Core(_settings(True), env_enabled=True)
    assert jev_client.effective_settings(core).enabled is True


def test_kill_switch_keeps_other_fields_from_config(monkeypatch):
    """急停只改 `enabled`；base_url / model / 凭据等仍按配置取，不被清空。"""
    monkeypatch.setenv("STEWARD_JEV_ENABLED", "0")
    core = _Core(_settings(True), env_enabled=False)
    resolved = jev_client.effective_settings(core)
    assert resolved.base_url == "https://jev.example"
    assert resolved.model == "systemone"
    assert resolved.credential_ref == "jev_api_key"


# ---------------------------------------------------------------- Q2：用量归因


def _emit(client, purpose: str | None, *, prompt: int, completion: int, total: int | None,
          profile_id: str | None, model: str, retried: bool = False, outcome: str = "ok") -> None:
    core = client.app.state.core
    core.database.record_model_call(profile_id=profile_id, model=model, purpose=purpose, prompt_tokens=prompt, completion_tokens=completion, total_tokens=total, latency_ms=100, retried=retried, outcome=outcome)


def test_usage_is_grouped_by_purpose(client):
    """Q2 核心：同一模型、同一 profile、**不同场景**必须分成两行，否则无法归因。"""
    test_client, _ = client
    _emit(test_client, "jev:scan-review", prompt=10, completion=5, total=None, profile_id=None, model="systemone")
    _emit(test_client, "jev:claim-support", prompt=20, completion=7, total=None, profile_id=None, model="systemone")
    rows = test_client.app.state.core.database.summarize_model_usage(7)
    jev_rows = [r for r in rows if r["model"] == "systemone"]
    assert len(jev_rows) == 2, f"Jev 两种场景被并成了一行：{jev_rows}"
    assert {r["purpose"] for r in jev_rows} == {"jev:scan-review", "jev:claim-support"}


def test_jev_not_sunk_to_bottom_by_null_total_tokens(client):
    """Jev 的 total_tokens 恒 null（F01 口径，不本地相加），不能因此永远排最后。"""
    test_client, _ = client
    # 先写入一条用量远大于 Jev 的普通方案
    _emit(test_client, "研报", prompt=10, completion=5, total=15, profile_id="p1", model="gpt")
    # Jev 只花了 100 tokens——比上面少，但它必须**按自己的实际用量**参与排序
    _emit(test_client, "jev:scan-review", prompt=80, completion=20, total=None, profile_id=None, model="systemone")
    rows = test_client.app.state.core.database.summarize_model_usage(7)
    jev = next(r for r in rows if r["model"] == "systemone")
    assert jev["prompt_tokens"] == 80 and jev["completion_tokens"] == 20
    assert jev["total_tokens"] == 0, "total_tokens 仍应为 0（NULL 求和不推算）"
    # 排序键用 prompt+completion：Jev 的 100 > 普通方案的 15，应排在前面
    assert rows[0]["model"] == "systemone", f"Jev 仍被沉底：{[r['model'] for r in rows]}"


def test_regular_model_ordering_unchanged(client):
    """对照：走 OpenAI 兼容协议、如实回报 total 的方案，排序不应被改动。"""
    test_client, _ = client
    _emit(test_client, "研报", prompt=100, completion=50, total=150, profile_id="p1", model="big")
    _emit(test_client, "方向", prompt=10, completion=5, total=15, profile_id="p2", model="small")
    rows = test_client.app.state.core.database.summarize_model_usage(7)
    assert [r["model"] for r in rows] == ["big", "small"]


def test_jev_retry_is_recorded_honestly(monkeypatch):
    """Q2：分片重试是**已计费**的二次调用，`retried` 必须为 True 而不是硬编码 False。"""
    from investment_steward_core.domain.models import JevSettings

    config = JevSettings(user_id=UUID("11111111-1111-1111-1111-111111111111"), enabled=True, base_url="https://jev.example",
                         model="systemone", credential_ref="k", timeout_secs=5.0)

    class _Store:
        def get(self, _key): return "sk"
        def list_records(self): return []

    attempts = {"n": 0}

    def _post(base_url, payload, api_key, timeout):
        attempts["n"] += 1
        if attempts["n"] == 1:
            # retryable 是**只读属性**，由 status_code 推导（429/529/5xx 才可重试），
            # 不是构造参数——照实传状态码。
            raise jev_client.JevUnavailable("限流", status_code=429)
        return {"model": "systemone", "usage": {"input_tokens": 5, "output_tokens": 3},
                "answers": [{"question_id": q, "type": "text", "text": "ok"} for q in payload["questions"]]}

    monkeypatch.setattr(jev_client, "_post_endpoint", _post)
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(jev_client, "emit_model_call_record", recorded.append)

    reviewer = jev_client.build_shard_reviewer(
        _FakeCore(config), _Store(), purpose="jev:scan-review", max_attempts=2, sleep=lambda _s: None
    )
    # 题面用生产里的构造器（与 test_jev05_scan_review 同口径），不自造 shape
    from investment_steward_core import tactics_ai

    evaluated = [{"index": 0, "symbol": "600000", "name": "浦发银行"}]
    reviewer({"shards": [{
        "evaluated": evaluated,
        "questions": tactics_ai.jev_scan_questions(evaluated),
        "state": "s",
    }]})

    assert attempts["n"] == 2, "第一次可重试失败后应重试一次"
    assert len(recorded) == 2, f"应有两条计费记录，实际 {len(recorded)}"
    assert recorded[0]["retried"] is False, "首次尝试不该标重试"
    assert recorded[1]["retried"] is True, "重试的那次必须如实标 retried=True"


class _FakeDb:
    """`effective_settings` 只需要 `get_jev_settings`（duck typing）。"""

    def __init__(self, saved: Any) -> None:
        self.saved = saved

    def get_jev_settings(self, _user_id: Any) -> Any:
        return self.saved


class _FakeCore:
    def __init__(self, config: Any) -> None:
        # local_user_id 必须是 UUID（JevSettings.user_id 的类型约束）
        self.local_user_id = UUID("11111111-1111-1111-1111-111111111111")
        # 存回库 ⇒ effective_settings 直接返回它（与 test_jev05_scan_review 的构造同口径），
        # 不会落到「环境变量/默认」分支
        self.settings = SimpleNamespace(model_access_enabled=True)
        self.database = _FakeDb(config)
        self._config = config

"""JV07 契约测试：行动模式预判路由（路线图 P1 第四站）。

要解决的问题：研究链路的引擎选择是**硬编码**的——`_answer` 只要 `model_client` 存在就先跑模型
路径（`_try_model`，一次完整 prompt + 全部证据上下文），失败才回退本地确定性引擎。于是「这个
提问其实不需要研究」的情况也要付一次完整模型调用的钱。

JV07 在跑模型路径**之前**先问 Jev 一道 `choice`：这次提问该进哪条链路。`confidence`
**从 choice 应答读取**（官方 intent-routing 原型），低置信 → 维持完整链路。

覆盖四层：

1. **纯函数 · state 与题面**：state 只带「用户问题 + 数据完备度标志」，**不带证据正文**；
   题面是一道 `choice`、四个行动模式作选项（含描述性说明）。
2. **纯函数 · 判定**：只有 `no_action` 且置信度达标才换路径；其余（含失败 / 缺答 / 低置信 /
   判为 research·observe·review_plan）一律**维持完整链路**。
3. **纯函数 · 顶层的健壮性**：`reviewer=None` 返回 None；回调抛错或分片带 error 时
   如实标 `unannotated` 且**不换路径**（路由是附加的省钱层，它挂掉不能让提问答不上来）。
4. **端点**：换路径时模型路径**确实没被调用**；`action_mode` **永不被预判改写**；
   路由决策落审计（含 confidence、所选模式、完整题目回执）；**Jev 关闭时零变化**。

第 4 层里「`action_mode` 不被改写」是验收硬约束：预判说的是「这次提问该不该花一次完整调用」，
`action_mode` 是作答方按证据账本给出的结论——两者不一致是**正常且可能**的，如实并陈，不互相覆盖。
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from investment_steward_core import credential_store, jev_client, research
from investment_steward_core.api import app as api_app
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.jev_client import JevAnswer

_QUESTION = "沪深300 当前估值分位如何？"
_CHITCHAT = "今天天气不错，你觉得呢？"


def _answer(*, choice: str | None, confidence: float | None, probabilities=None) -> JevAnswer:
    """构造一道 `choice` 应答（**不伪造**缺的字段：confidence 为 None 就是 None）。"""
    return JevAnswer(
        question_id=research.JEV_ROUTE_QUESTION_ID,
        type="choice",
        choice=choice,
        probabilities=probabilities or {},
        confidence=confidence,
    )


# ——————————————————————————————————————————————————————————————
# 1. 纯函数：state 与题面
# ——————————————————————————————————————————————————————————————


def test_jev_route_state_carries_question_and_completeness_flags_only():
    """state 只送「提问 + 完备度标志」——证据正文会被完整链路原样用上，再送一遍是同一份内容付两次钱。"""
    state = research.jev_route_state(
        _QUESTION, supporting_count=3, contradicting_count=1, unknown_count=2,
        has_profile=True, stale_count=1,
    )
    assert _QUESTION in state
    assert "支持证据：3 条" in state
    assert "相反证据：1 条" in state
    assert "关系未知：2 条" in state
    assert "已建立投资者画像：是" in state
    assert "已过期证据：1 条" in state
    # **明确不带**证据正文：这里连一个证据摘要字段都没有入口
    assert "沪深300估值分位" not in state


def test_jev_route_state_handles_empty_question_honestly():
    """空问题如实写「（空问题）」，而不是留空让模型自己猜。"""
    assert "（空问题）" in research.jev_route_state("")
    assert "（空问题）" in research.jev_route_state(None)  # type: ignore[arg-type]


def test_jev_route_state_clips_overlong_question():
    long_question = "啊" * (research.JEV_ROUTE_QUESTION_CHARS + 500)
    state = research.jev_route_state(long_question)
    assert state.count("啊") == research.JEV_ROUTE_QUESTION_CHARS


def test_jev_route_question_is_one_choice_with_four_described_modes():
    questions = research.jev_route_question()
    assert list(questions) == [research.JEV_ROUTE_QUESTION_ID]
    question = questions[research.JEV_ROUTE_QUESTION_ID]
    assert question["type"] == "choice"
    assert list(question["criteria"]) == ["research", "observe", "review_plan", "no_action"]
    # 四个选项都带描述性说明（光秃秃的内部词面在模型看来没有判别力）
    assert all(question["criteria"][key] for key in question["criteria"])
    # **confidence 不塞进 options**——它由应答侧给出（官方 intent-routing 原型）
    assert "confidence" not in question


def test_jev_route_shards_is_single_shard():
    bundle = research.jev_route_shards(_QUESTION, supporting_count=2)
    assert len(bundle["shards"]) == 1
    assert _QUESTION in bundle["shards"][0]["state"]
    assert research.JEV_ROUTE_QUESTION_ID in bundle["shards"][0]["questions"]


# ——————————————————————————————————————————————————————————————
# 2. 纯函数：判定（只有 no_action + 高置信才换路径）
# ——————————————————————————————————————————————————————————————


def test_no_action_with_high_confidence_bypasses_model_path():
    decision = research.jev_route_decision(_answer(choice="no_action", confidence=0.9))
    assert decision["bypassed_model"] is True
    assert decision["route_state"] == research.JEV_ROUTE_STATE_ROUTED
    assert decision["mode"] == "no_action"
    assert "跳过完整研究链路" in decision["note"]


def test_no_action_with_low_confidence_keeps_full_path():
    """保守方向：置信度不达标就**不省这次调用**——走错链路是少给答案，比多花钱严重。"""
    decision = research.jev_route_decision(_answer(choice="no_action", confidence=0.2))
    assert decision["bypassed_model"] is False
    assert decision["route_state"] == research.JEV_ROUTE_STATE_FULL
    assert "维持完整研究链路" in decision["note"]


def test_no_action_without_confidence_keeps_full_path():
    """缺 `confidence` 一样不换路径（**不伪造**一个值去凑判定）。"""
    decision = research.jev_route_decision(_answer(choice="no_action", confidence=None))
    assert decision["bypassed_model"] is False
    assert decision["confidence"] is None
    assert "置信度缺失" in decision["note"]


@pytest.mark.parametrize("mode", ["research", "observe", "review_plan"])
def test_other_modes_never_bypass_even_at_high_confidence(mode):
    """判为要紧的模式恰恰说明这题值得认真回答——不为省额度牺牲回答质量。"""
    decision = research.jev_route_decision(_answer(choice=mode, confidence=0.99))
    assert decision["bypassed_model"] is False
    assert decision["route_state"] == research.JEV_ROUTE_STATE_FULL
    assert "维持完整研究链路" in decision["note"]


def test_missing_and_unknown_answers_are_unannotated_not_bypass():
    for answer in (None, _answer(choice=None, confidence=0.9), _answer(choice="teleport", confidence=0.9)):
        decision = research.jev_route_decision(answer)
        assert decision["bypassed_model"] is False
        assert decision["route_state"] == research.JEV_ROUTE_STATE_UNANNOTATED
        assert decision["mode"] is None


def test_decision_keeps_probabilities_for_later_recomputation():
    decision = research.jev_route_decision(
        _answer(choice="observe", confidence=0.6, probabilities={"observe": 0.61, "no_action": 0.39})
    )
    assert decision["probabilities"] == {"observe": 0.61, "no_action": 0.39}


# ——————————————————————————————————————————————————————————————
# 3. 纯函数：顶层健壮性
# ——————————————————————————————————————————————————————————————


def test_jev_action_route_returns_none_without_reviewer():
    assert research.jev_action_route(reviewer=None, question=_QUESTION) is None


def _fake_reviewer(*, choice: str = "no_action", confidence: float = 0.9, error: str | None = None):
    calls: list[dict] = []

    def _review(bundle):
        calls.append(bundle)
        return [{
            "evaluated": [{"index": 1}],
            "answers": {}
            if error
            else {
                research.JEV_ROUTE_QUESTION_ID: _answer(choice=choice, confidence=confidence)
            },
            "error": error,
            "attempts": 1,
            "model": "jev-latest",
        }]

    return _review, calls


def test_jev_action_route_never_raises_when_reviewer_boom():
    def _boom(_bundle):
        raise RuntimeError("网络炸了")

    route = research.jev_action_route(reviewer=_boom, question=_QUESTION)
    assert route is not None, "路由失败必须如实回执，而不是静默消失"
    assert route["bypassed_model"] is False
    assert route["route_state"] == research.JEV_ROUTE_STATE_UNANNOTATED
    assert "网络炸了" in route["note"]


def test_jev_action_route_marks_unannotated_on_shard_error():
    reviewer, _calls = _fake_reviewer(error="429 限速")
    route = research.jev_action_route(reviewer=reviewer, question=_QUESTION)
    assert route["bypassed_model"] is False
    assert route["route_state"] == research.JEV_ROUTE_STATE_UNANNOTATED
    assert "429" in route["note"]


def test_jev_action_route_reports_model_and_availability():
    reviewer, calls = _fake_reviewer(choice="research", confidence=0.8)
    route = research.jev_action_route(reviewer=reviewer, question=_QUESTION, supporting_count=1)
    assert route["available"] is True
    assert route["model"] == "jev-latest"
    assert len(calls) == 1, "一次路由只出网一次（恒单片）"


# ——————————————————————————————————————————————————————————————
# 4. 端点
# ——————————————————————————————————————————————————————————————


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(api_app, "resolve_store", lambda db: credential_store.DbCredentialStore(db))
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


def _enable_jev(http, headers) -> None:
    assert http.put(
        f"/credentials/{credential_store.JEV_API_KEY}",
        json={"secret": "sk-jev-secret"},
        headers=headers,
    ).status_code == 200
    saved = http.put("/jev/config", headers=headers, json={
        "enabled": True,
        "base_url": "https://openrouter.ai/api/v1/decisions",
        "model": "typesafe/jev-1.13",
        "credential_ref": credential_store.JEV_API_KEY,
        "timeout_secs": 60,
    })
    assert saved.status_code == 200


def _post_endpoint_stub(*, choice: str, confidence: float | None):
    calls: list[dict] = []

    def _fake(base_url, json_payload, api_key, timeout):
        calls.append(json_payload)
        return {
            "model": "jev-latest",
            "answers": {
                research.JEV_ROUTE_QUESTION_ID: {
                    "type": "choice",
                    "choice": choice,
                    "confidence": confidence,
                    "probabilities": {choice: 0.9},
                }
            },
        }

    return _fake, calls


def _spy_model_path(monkeypatch) -> list[str]:
    """把模型路径换成计数桩：它被调用了几次、有没有被调用，是路由生效与否的**直接**证据。"""
    calls: list[str] = []
    original_synthesize = research._synthesize

    def _spy(db, run, question, supporting, contradicting, unknown, profile=None):
        calls.append(question)
        local = original_synthesize(db, run, question, supporting, contradicting, unknown, profile=profile)
        return local.model_copy(update={"model_provider": "fake-model", "model_name": "fake"})

    monkeypatch.setattr(research, "_try_model", _spy)
    return calls


def _run(http, headers, question: str = _QUESTION) -> dict:
    run = http.post("/research/runs", headers=headers, json={"user_question": question}).json()
    return http.post(f"/research/runs/{run['run_id']}/run", headers=headers).json()


def test_bypass_skips_model_path_and_never_rewrites_action_mode(client, monkeypatch):
    """换路径时模型路径**确实没被调用**；且 `action_mode` 由作答方给出，不是预判灌进去的。"""
    http, headers = client
    _enable_jev(http, headers)
    model_calls = _spy_model_path(monkeypatch)
    fake, jev_calls = _post_endpoint_stub(choice="no_action", confidence=0.9)
    monkeypatch.setattr(jev_client, "_post_endpoint", fake)

    answer = _run(http, headers, _CHITCHAT)

    assert model_calls == [], "预判为 no_action 且高置信 → 不该再跑一次完整模型调用"
    assert len(jev_calls) == 1
    assert answer["jev_route"]["bypassed_model"] is True
    assert answer["jev_route"]["mode"] == "no_action"
    assert answer["jev_route"]["confidence"] == 0.9
    # 硬约束①：action_mode 是本地引擎按证据账本给的，**不是** "no_action" 被灌进去
    assert answer["action_mode"] in ("observe", "research", "review_plan", "no_action")
    assert answer["model_provider"] != "fake-model"
    # 并陈说明：用户能看到「预判说不必研究、下面是作答方的结论」
    assert any("JV07 路由" in item for item in answer["limitations"])


def test_full_path_preserved_when_prediction_says_research(client, monkeypatch):
    http, headers = client
    _enable_jev(http, headers)
    model_calls = _spy_model_path(monkeypatch)
    fake, _ = _post_endpoint_stub(choice="research", confidence=0.95)
    monkeypatch.setattr(jev_client, "_post_endpoint", fake)

    answer = _run(http, headers)

    assert len(model_calls) == 1, "判为要紧的模式 → 维持完整链路（不为省额度牺牲质量）"
    assert answer["jev_route"]["bypassed_model"] is False
    assert answer["jev_route"]["route_state"] == research.JEV_ROUTE_STATE_FULL
    assert answer["model_provider"] == "fake-model"


def test_low_confidence_keeps_full_path(client, monkeypatch):
    """`confidence < 0.5` → 维持现行完整研究链路（官方 intent-routing 原型）。"""
    http, headers = client
    _enable_jev(http, headers)
    model_calls = _spy_model_path(monkeypatch)
    fake, _ = _post_endpoint_stub(choice="no_action", confidence=0.1)
    monkeypatch.setattr(jev_client, "_post_endpoint", fake)

    answer = _run(http, headers, _CHITCHAT)

    assert len(model_calls) == 1
    assert answer["jev_route"]["bypassed_model"] is False


def test_route_decision_is_audited_with_full_question_receipt(client, monkeypatch):
    """硬约束②：路由决策落审计，含 confidence、所选模式与**完整题目回执**。"""
    http, headers = client
    _enable_jev(http, headers)
    _spy_model_path(monkeypatch)
    fake, _ = _post_endpoint_stub(choice="observe", confidence=0.77)
    monkeypatch.setattr(jev_client, "_post_endpoint", fake)

    answer = _run(http, headers)
    tenant_id = UUID(http.get("/session", headers=headers).json()["user_id"])
    events = http.app.state.core.database.list_audit(tenant_id)
    route_events = [item for item in events if item.action == "research.run.jev_route"]

    assert len(route_events) == 1
    payload = route_events[0].payload
    assert payload["mode"] == "observe"
    assert payload["confidence"] == 0.77
    assert payload["bypassed_model"] is False
    assert payload["action_mode"] == answer["action_mode"]
    # 完整题目回执：拿到它就能复现当时问了什么（审计走 JSON 落库，键序不保证，故比集合）
    assert payload["question"]["type"] == "choice"
    assert set(payload["question"]["criteria"]) == {"research", "observe", "review_plan", "no_action"}
    assert all(payload["question"]["criteria"][key] for key in payload["question"]["criteria"])
    # 密钥绝不进审计
    assert "sk-jev-secret" not in str(payload)


def test_purpose_is_recorded_for_scoped_cost_reconciliation(client, monkeypatch):
    """`purpose` 落 `model_calls`，是 JV10 分场景对账的唯一依据。"""
    import sqlite3

    http, headers = client
    _enable_jev(http, headers)
    _spy_model_path(monkeypatch)
    fake, _ = _post_endpoint_stub(choice="research", confidence=0.8)
    monkeypatch.setattr(jev_client, "_post_endpoint", fake)

    _run(http, headers)

    db = http.app.state.core.database
    with sqlite3.connect(db.path) as conn:
        rows = conn.execute("SELECT purpose FROM model_calls").fetchall()
    assert ("jev:action-routing",) in rows


def test_jev_disabled_means_zero_change(client, monkeypatch):
    """零变化开关：Jev 未启用时 `jev_route` 为 None，且一次都不出网。"""
    http, headers = client
    _spy_model_path(monkeypatch)
    monkeypatch.setattr(jev_client, "_post_endpoint", lambda *a, **k: pytest.fail("Jev 关闭时不得出网"))

    answer = _run(http, headers)

    assert answer["jev_route"] is None
    assert not any("JV07" in item for item in answer["limitations"])


def test_model_access_disabled_skips_routing_entirely(tmp_path: Path, monkeypatch):
    """模型出网关闭时本来就走本地引擎——预判既省不下钱又要多花一次 Jev 调用，故整层不跑。"""
    monkeypatch.setattr(api_app, "resolve_store", lambda db: credential_store.DbCredentialStore(db))
    token = "test-session-token"
    # `CoreSettings` 是 frozen dataclass，只能在建 app 时给，不能事后改字段。
    app = create_app(
        CoreSettings(session_token=token, data_dir=tmp_path, model_access_enabled=False)
    )
    http, headers = TestClient(app), {"X-Core-Session-Token": token}
    _enable_jev(http, headers)
    _spy_model_path(monkeypatch)
    monkeypatch.setattr(jev_client, "_post_endpoint", lambda *a, **k: pytest.fail("模型出网关闭时不得出网"))

    answer = _run(http, headers)

    assert answer["jev_route"] is None

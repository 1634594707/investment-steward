"""JV01 契约测试：Jev 客户端（`jev_client`）。

全部注入式：不给真实网络发请求。覆盖路线图 JV01 要求的六类路径
（正常 / 超时 / 限流 / 非 JSON / 密钥缺失 / 总闸关闭），外加题型上限校验、
usage 字段映射（Jev 无 total_tokens）、`noul` 无 confidence、逐题类型化解析的失败面。
"""

from __future__ import annotations

import json
import urllib.error

import pytest
from investment_steward_core import jev_client, model_client
from investment_steward_core.jev_client import JevConfig, JevUnavailable

BASE = "https://api.typesafe.ai/v1"


class _Store:
    """最小凭据库替身：只需 `.get(key_id)`（`list_records` 缺失时 resolve_credential 会自行兜底）。"""

    def __init__(self, secrets: dict[str, str] | None = None):
        self._secrets = secrets or {}

    def get(self, key_id: str) -> str | None:
        return self._secrets.get(key_id)


def _config(**overrides) -> JevConfig:
    data = {"base_url": BASE, "model": "jev-latest", "credential_ref": "jev_api_key"}
    data.update(overrides)
    return JevConfig(**data)


# ---------------------------------------------------------------------------
# 题型构造器
# ---------------------------------------------------------------------------


def test_noul_question_has_no_criteria_by_default():
    question = jev_client.noul_question("这段文本是否提到了「连通性测试」？")
    assert question == {"type": "noul", "instructions": "这段文本是否提到了「连通性测试」？"}


def test_noul_question_criteria_needs_both_sides():
    question = jev_client.noul_question("是否为公告？", true_means="来源标注为公告")
    assert question["criteria"]["true"] == "来源标注为公告"
    # 只给一侧时，另一侧必须有兜底描述——官方 noul criteria 需要 true/false 成对出现
    assert question["criteria"]["false"]


def test_choice_question_accepts_mapping_and_sequence():
    from_map = jev_client.choice_question("哪个部门处理？", {"billing": "账单", "technical": None})
    assert from_map["criteria"] == {"billing": "账单", "technical": None}
    from_seq = jev_client.choice_question("哪个部门处理？", ["billing", "technical"])
    assert from_seq["criteria"] == {"billing": None, "technical": None}


def test_choice_question_rejects_over_255_options():
    options = [f"opt-{index}" for index in range(256)]
    with pytest.raises(ValueError, match="255"):
        jev_client.choice_question("选一个", options)


def test_score_question_accepts_two_levels():
    question = jev_client.score_question("可靠程度？", ["证据不足", "证据充分"])
    assert question["type"] == "score"
    assert question["criteria"] == ["证据不足", "证据充分"]


def test_score_question_rejects_single_level():
    with pytest.raises(ValueError, match="至少"):
        jev_client.score_question("可靠程度？", ["只有一级"])


def test_score_question_rejects_eleven_levels():
    with pytest.raises(ValueError, match="最多"):
        jev_client.score_question("可靠程度？", [f"第{i}级" for i in range(11)])


def test_score_question_rejects_empty_level_text():
    with pytest.raises(ValueError, match="不能为空"):
        jev_client.score_question("可靠程度？", ["低", "  "])


# ---------------------------------------------------------------------------
# 路径 1：正常（三题型混合 + 概率/confidence 透传 + score 归一化 + usage 映射）
# ---------------------------------------------------------------------------

_OK_PAYLOAD = {
    "model": "jev-1.13.0",
    "answers": {
        "reliability": {
            "type": "score",
            "score": 1.05,
            "confidence": 0.92,
            "legend": {"0": "证据不足", "1": "证据一般", "2": "证据充分"},
            "probabilities": {"0": 0.0, "1": 0.95, "2": 0.05},
        },
        "assessment": {
            "type": "choice",
            "choice": "低估",
            "confidence": 0.81,
            "probabilities": {"低估": 0.88, "相符": 0.12, "高估": 0.0},
        },
        "data_gaps": {"type": "noul", "noul": 0.93},
    },
    "usage": {"input_tokens": 392, "output_tokens": 65},
}


def _ok_questions() -> dict:
    return {
        "reliability": jev_client.score_question("规则分是否可靠？", ["证据不足", "证据一般", "证据充分"]),
        "assessment": jev_client.choice_question("规则分相对合理区间？", {"低估": "偏低", "相符": None, "高估": "偏高"}),
        "data_gaps": jev_client.noul_question("是否存在数据缺口？"),
    }


def _patch_post(monkeypatch, payload):
    captured: dict = {}

    def fake_post(base_url, json_payload, api_key, timeout):
        captured.update(base_url=base_url, json_payload=json_payload, api_key=api_key, timeout=timeout)
        return payload

    monkeypatch.setattr(jev_client, "_post_endpoint", fake_post)
    return captured


def test_call_parses_three_question_types_and_usage(monkeypatch):
    captured = _patch_post(monkeypatch, _OK_PAYLOAD)
    reply = jev_client.call_jev(
        _config(),
        _Store({"jev_api_key": "sk-jev-secret"}),
        {"rule_score": 62, "bars_tail": [1, 2, 3]},
        _ok_questions(),
        purpose="jev:tactics-review",
    )

    # 端点与密钥：Bearer 只出现在请求头路径上（此处断言形状，密钥不回传）
    assert captured["base_url"] == BASE
    assert captured["api_key"] == "sk-jev-secret"
    assert captured["json_payload"]["model"] == "jev-latest"
    assert captured["json_payload"]["state"]["rule_score"] == 62

    # 留痕用响应回的真实版本号，不用别名
    assert reply.model == "jev-1.13.0"
    assert reply.requested_model == "jev-latest"
    assert reply.purpose == "jev:tactics-review"

    # usage：Jev 只有 input/output，没有 total
    assert reply.input_tokens == 392
    assert reply.output_tokens == 65

    # score：原始值是等级轴上的加权均值，不是 0–100
    reliability = reply.answer("reliability")
    assert reliability.score == pytest.approx(1.05)
    assert reliability.levels == 3
    assert reliability.confidence == pytest.approx(0.92)
    assert reliability.normalized_score == pytest.approx(1.05 / 2)
    assert reliability.score_percent == pytest.approx(52.5)
    assert reliability.legend["2"] == "证据充分"

    # choice：所选项 + 概率分布 + confidence
    assessment = reply.answer("assessment")
    assert assessment.choice == "低估"
    assert assessment.confidence == pytest.approx(0.81)
    assert assessment.probabilities["低估"] == pytest.approx(0.88)
    assert assessment.top_probability() == pytest.approx(0.88)

    # noul：0–1 值 + 三路划分，且没有 confidence
    data_gaps = reply.answer("data_gaps")
    assert data_gaps.noul == pytest.approx(0.93)
    assert data_gaps.confidence is None
    assert data_gaps.noul_verdict() == "yes"


def test_noul_ignores_confidence_even_if_server_sends_one(monkeypatch):
    payload = {
        "model": "jev-1.13.0",
        "answers": {"data_gaps": {"type": "noul", "noul": 0.5, "confidence": 0.99}},
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }
    _patch_post(monkeypatch, payload)
    reply = jev_client.call_jev(_config(), _Store({"jev_api_key": "sk"}), "文本", {"data_gaps": jev_client.noul_question("有缺口？")})
    answer = reply.answer("data_gaps")
    # 官方：noul 没有第二决策轴。多给也置 None，避免上层在不存在的东西上做路由。
    assert answer.confidence is None
    assert answer.noul_verdict() == "uncertain"


def test_noul_verdict_three_way_split():
    """三路划分边界 = R6 中文标定值（YES 0.85 / NO 0.3），**不是官方英文范例的 0.8 / 0.2**。

    标定报告：`docs/evidence/jev-cjk-calibration-2026-09-21.md`。边界必须逐点钉住——
    0.8 在英文口径下是「编造」，在中文标定口径下是「存疑」，差一档就是差一次降级。
    """
    def _answer(value: float) -> str | None:
        return jev_client.JevAnswer(question_id="q", type="noul", noul=value).noul_verdict()

    assert _answer(0.95) == "yes"
    assert _answer(0.85) == "yes"      # 阈值本身算「是」（含边界）
    assert _answer(0.80) == "uncertain"  # ⚠️ 英文口径下这里曾是 yes
    assert _answer(0.50) == "uncertain"
    assert _answer(0.30) == "no"       # 阈值本身算「否」（含边界）
    assert _answer(0.20) == "no"
    assert _answer(0.03) == "no"


def test_recorder_receives_jev_record_with_null_total_tokens(monkeypatch):
    """用量必须经 model_client 的公共钩子落同一张表；total_tokens 记 null（Jev 不提供）。"""
    _patch_post(monkeypatch, _OK_PAYLOAD)
    records: list[dict] = []
    monkeypatch.setattr(model_client, "_model_call_recorder", records.append)

    jev_client.call_jev(_config(), _Store({"jev_api_key": "sk"}), "文本", _ok_questions(), purpose="jev:claim-support")

    assert len(records) == 1
    record = records[0]
    assert record["purpose"] == "jev:claim-support"
    assert record["model"] == "jev-1.13.0"
    assert record["profile_id"] is None  # Jev 不走方案表
    assert record["prompt_tokens"] == 392
    assert record["completion_tokens"] == 65
    assert record["total_tokens"] is None  # 不本地加总，保 F01「缺失记 null 不猜」口径
    assert record["retried"] is False
    assert record["outcome"] == "ok"


def test_recorder_defaults_purpose_to_jev_unspecified(monkeypatch):
    _patch_post(monkeypatch, _OK_PAYLOAD)
    records: list[dict] = []
    monkeypatch.setattr(model_client, "_model_call_recorder", records.append)
    jev_client.call_jev(_config(), _Store({"jev_api_key": "sk"}), "文本", _ok_questions())
    # 默认值保证所有记录都能被 purpose LIKE 'jev:%' 归集
    assert records[0]["purpose"] == "jev:unspecified"


def test_emit_model_call_record_is_public_and_shared():
    """chat 链路与 Jev 链路共用同一钩子——公开出口存在且转发到同一个 recorder。"""
    records: list[dict] = []
    original = model_client._model_call_recorder
    try:
        model_client.set_model_call_recorder(records.append)
        model_client.emit_model_call_record({"model": "x"})
        assert records == [{"model": "x"}]
    finally:
        model_client.set_model_call_recorder(original)


# ---------------------------------------------------------------------------
# 路径 2：超时
# ---------------------------------------------------------------------------


def test_call_reports_timeout_with_actionable_detail(monkeypatch):
    def fake_post(*_a, **_k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(jev_client, "_post_endpoint", fake_post)
    records: list[dict] = []
    monkeypatch.setattr(model_client, "_model_call_recorder", records.append)

    with pytest.raises(JevUnavailable, match="超时"):
        jev_client.call_jev(_config(), _Store({"jev_api_key": "sk"}), "文本", _ok_questions())

    assert records[0]["outcome"] == "timeout"


def test_call_reports_urlerror_as_unreachable(monkeypatch):
    def fake_post(*_a, **_k):
        raise urllib.error.URLError("getaddrinfo failed")

    monkeypatch.setattr(jev_client, "_post_endpoint", fake_post)
    with pytest.raises(JevUnavailable, match="无法连接"):
        jev_client.call_jev(_config(), _Store({"jev_api_key": "sk"}), "文本", _ok_questions())


# ---------------------------------------------------------------------------
# 路径 3：限流（429）与官方错误码语义
# ---------------------------------------------------------------------------


def _http_error(status: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.typesafe.ai/v1/systemone", status, "err", {}, None)


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, "", "401"),
        (404, "", "/v1"),
        (422, "", "422"),
        (429, "", "429"),
        (529, "", "529"),
        (503, "", "503"),
    ],
)
def test_http_error_diagnostics_are_actionable(status, body, expected):
    detail = jev_client._http_error_detail(status, body)
    assert expected in detail


def test_403_waf_block_is_distinguished_from_auth_rejection():
    assert "WAF" in jev_client._http_error_detail(403, '{"error":{"code":1010}}')
    assert "403" in jev_client._http_error_detail(403, '{"error":"forbidden"}')


def test_post_endpoint_maps_429_to_jev_unavailable(monkeypatch):
    def fake_urlopen(*_a, **_k):
        raise _http_error(429, '{"error":"rate limited"}')

    monkeypatch.setattr(jev_client.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(JevUnavailable, match="429"):
        jev_client._post_endpoint(BASE, {"state": "x"}, "sk", 5.0)


def test_post_endpoint_uses_systemone_path_and_bearer(monkeypatch):
    captured: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return json.dumps(_OK_PAYLOAD).encode("utf-8")

    def fake_urlopen(request, **_kwargs):
        captured["auth"] = request.get_header("Authorization")
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["ua"] = request.get_header("User-agent")
        return _Resp()

    monkeypatch.setattr(jev_client.urllib.request, "urlopen", fake_urlopen)
    jev_client._post_endpoint(BASE, {"state": "x"}, "sk-token", 5.0)

    assert captured["url"] == "https://api.typesafe.ai/v1/systemone"
    assert captured["method"] == "POST"
    assert captured["auth"] == "Bearer sk-token"
    # 显式 UA：防网关按 Python-urllib 特征封禁（与 chat 链路同源）
    assert captured["ua"] == model_client.MODEL_USER_AGENT


# ---------------------------------------------------------------------------
# 端点解析：官方 API 根要补 /systemone，网关的完整端点**不得**再补
#
# 背景（2026-09-21 用户实测）：base_url 填 OpenRouter 的
# `https://openrouter.ai/api/alpha/decisions` 时，旧实现拼成 `…/decisions/systemone` → 404。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        # 官方：API 根（带版本段或不带）→ 补 systemone
        ("https://api.typesafe.ai/v1", "https://api.typesafe.ai/v1/systemone"),
        ("https://api.typesafe.ai", "https://api.typesafe.ai/systemone"),
        ("https://api.typesafe.ai/v1/", "https://api.typesafe.ai/v1/systemone"),
        ("  https://api.typesafe.ai/v2  ", "https://api.typesafe.ai/v2/systemone"),
        # 官方完整端点：已经带了，不能再补一次
        ("https://api.typesafe.ai/v1/systemone", "https://api.typesafe.ai/v1/systemone"),
        # 网关：URL 本身就是完整端点，原样使用
        ("https://openrouter.ai/api/alpha/decisions", "https://openrouter.ai/api/alpha/decisions"),
        ("https://jevtypesafeai.com/api/v1/decide", "https://jevtypesafeai.com/api/v1/decide"),
        # 自建中转的深层路径同样按完整端点对待（用户比我们更清楚自己的路径）
        ("https://gw.example.com/typesafe", "https://gw.example.com/typesafe"),
        # 空值回退官方默认后再补
        ("", "https://api.typesafe.ai/v1/systemone"),
    ],
)
def test_resolve_endpoint(base_url, expected):
    assert jev_client.resolve_endpoint(base_url) == expected


def test_config_resolved_endpoint_handles_gateway():
    config = _config(base_url="https://openrouter.ai/api/alpha/decisions", model="typesafe/jev-1.13")
    assert config.resolved_endpoint == "https://openrouter.ai/api/alpha/decisions"
    assert config.resolved_model == "typesafe/jev-1.13"


def test_gateway_endpoint_is_not_suffixed(monkeypatch):
    """端到端：走网关时实际请求 URL 就是用户填的那个（不补 /systemone）。"""
    captured: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return json.dumps(_OK_PAYLOAD).encode("utf-8")

    def fake_urlopen(request, **_kwargs):
        captured["url"] = request.full_url
        return _Resp()

    monkeypatch.setattr(jev_client.urllib.request, "urlopen", fake_urlopen)
    jev_client._post_endpoint("https://openrouter.ai/api/alpha/decisions", {"state": "x"}, "sk", 5.0)
    assert captured["url"] == "https://openrouter.ai/api/alpha/decisions"


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.typesafe.ai/v1", "https://api.typesafe.ai/v1/models"),
        ("https://openrouter.ai/api/alpha/decisions", "https://openrouter.ai/api/v1/models"),
    ],
)
def test_resolve_models_url(base_url, expected):
    assert jev_client.resolve_models_url(base_url) == expected


def test_list_models_accepts_openai_shape_and_filters_gateway(monkeypatch):
    """OpenRouter 是 `{"data":[{"id"}]}` 且挂着 400+ 模型 → 认形状，并只留 typesafe/ 家的。"""
    payload = {
        "data": [
            {"id": "typesafe/jev-1.13"},
            {"id": "typesafe/jev"},
            {"id": "openai/gpt-5"},
        ]
    }

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(jev_client.urllib.request, "urlopen", lambda *_a, **_k: _Resp())
    models = jev_client.list_jev_models("https://openrouter.ai/api/alpha/decisions", "sk", 5.0)
    assert models == ["typesafe/jev-1.13", "typesafe/jev"]


# （官方形状 `{"models":[{"name"}]}` 与「形状不认识就报错」的用例在下方
#   「模型列表」小节已有覆盖，这里不重复。）


# ---------------------------------------------------------------------------
# 路径 4：非 JSON / 结构畸形
# ---------------------------------------------------------------------------


def test_post_endpoint_rejects_non_json(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return b"<html>not json</html>"

    monkeypatch.setattr(jev_client.urllib.request, "urlopen", lambda *_a, **_k: _Resp())
    with pytest.raises(JevUnavailable, match="不是合法 JSON"):
        jev_client._post_endpoint(BASE, {"state": "x"}, "sk", 5.0)


def test_parse_rejects_missing_answers_object():
    with pytest.raises(JevUnavailable, match="缺少 answers"):
        jev_client._parse_answers({"model": "jev-1.13.0"}, _ok_questions(), model="jev-1.13.0")


def test_parse_rejects_incomplete_answers(monkeypatch):
    payload = dict(_OK_PAYLOAD)
    payload["answers"] = {"reliability": _OK_PAYLOAD["answers"]["reliability"]}
    _patch_post(monkeypatch, payload)
    # 应答不完整 → 报错，绝不当作「已判定」
    with pytest.raises(JevUnavailable, match="缺少题目"):
        jev_client.call_jev(_config(), _Store({"jev_api_key": "sk"}), "文本", _ok_questions())


def test_parse_rejects_undefined_choice_option(monkeypatch):
    payload = dict(_OK_PAYLOAD)
    payload["answers"] = dict(_OK_PAYLOAD["answers"])
    payload["answers"]["assessment"] = {"type": "choice", "choice": "不存在", "probabilities": {}, "confidence": 0.5}
    _patch_post(monkeypatch, payload)
    with pytest.raises(JevUnavailable, match="未定义的选项"):
        jev_client.call_jev(_config(), _Store({"jev_api_key": "sk"}), "文本", _ok_questions())


def test_parse_rejects_out_of_range_score(monkeypatch):
    payload = dict(_OK_PAYLOAD)
    payload["answers"] = dict(_OK_PAYLOAD["answers"])
    payload["answers"]["reliability"] = {"type": "score", "score": 7.5, "probabilities": {}, "confidence": 0.5}
    _patch_post(monkeypatch, payload)
    # 越界说明请求侧等级数与响应不一致——宁可报错也不裁剪出一个假的展示分
    with pytest.raises(JevUnavailable, match="越界"):
        jev_client.call_jev(_config(), _Store({"jev_api_key": "sk"}), "文本", _ok_questions())


def test_parse_rejects_noul_out_of_range(monkeypatch):
    payload = {"model": "jev-1.13.0", "answers": {"data_gaps": {"type": "noul", "noul": 1.4}}, "usage": {}}
    _patch_post(monkeypatch, payload)
    with pytest.raises(JevUnavailable, match="越界"):
        jev_client.call_jev(_config(), _Store({"jev_api_key": "sk"}), "文本", {"data_gaps": jev_client.noul_question("有缺口？")})


def test_parse_rejects_unknown_question_type(monkeypatch):
    payload = {"model": "jev-1.13.0", "answers": {"q": {"type": "essay", "text": "…"}}, "usage": {}}
    _patch_post(monkeypatch, payload)
    with pytest.raises(JevUnavailable, match="未知题型"):
        jev_client.call_jev(_config(), _Store({"jev_api_key": "sk"}), "文本", {"q": jev_client.noul_question("有缺口？")})


def test_usage_missing_records_none_not_zero(monkeypatch):
    payload = {"model": "jev-1.13.0", "answers": {"data_gaps": {"type": "noul", "noul": 0.9}}}
    _patch_post(monkeypatch, payload)
    records: list[dict] = []
    monkeypatch.setattr(model_client, "_model_call_recorder", records.append)
    reply = jev_client.call_jev(_config(), _Store({"jev_api_key": "sk"}), "文本", {"data_gaps": jev_client.noul_question("有缺口？")})
    assert reply.input_tokens is None
    assert reply.output_tokens is None
    assert records[0]["prompt_tokens"] is None  # 缺失记 null，不按字数反推
    assert records[0]["completion_tokens"] is None


# ---------------------------------------------------------------------------
# 路径 5：密钥缺失
# ---------------------------------------------------------------------------


def test_call_raises_when_credential_missing_without_request(monkeypatch):
    def _never(*_a, **_k):
        raise AssertionError("凭据缺失时不应发请求")

    monkeypatch.setattr(jev_client, "_post_endpoint", _never)
    records: list[dict] = []
    monkeypatch.setattr(model_client, "_model_call_recorder", records.append)

    with pytest.raises(JevUnavailable, match="凭据"):
        jev_client.call_jev(_config(credential_ref="missing"), _Store({}), "文本", _ok_questions())

    assert records[0]["outcome"] == "error"


def test_credential_tail_number_fallback_is_inherited(monkeypatch):
    """复用 model_client.resolve_credential：允许用户凭直觉填凭据尾号。"""
    _patch_post(monkeypatch, _OK_PAYLOAD)
    store = _Store({"jev_api_key": "sk-real"})

    class _Record:
        key_id = "jev_api_key"
        last4 = "REAL"

    store.list_records = lambda: [_Record()]  # type: ignore[attr-defined]
    reply = jev_client.call_jev(_config(credential_ref="real"), store, "文本", _ok_questions())
    assert reply.model == "jev-1.13.0"


# ---------------------------------------------------------------------------
# 路径 6：总闸关闭 / 场景开关关闭
# ---------------------------------------------------------------------------


def test_call_refuses_when_model_access_disabled(monkeypatch):
    def _never(*_a, **_k):
        raise AssertionError("总闸关闭时不应发请求")

    monkeypatch.setattr(jev_client, "_post_endpoint", _never)
    records: list[dict] = []
    monkeypatch.setattr(model_client, "_model_call_recorder", records.append)

    with pytest.raises(JevUnavailable, match="总闸"):
        jev_client.call_jev(
            _config(), _Store({"jev_api_key": "sk"}), "文本", _ok_questions(), access_enabled=False
        )
    # 总闸关闭连记录都不落——本轮 Jev 根本没参与，不该在用量表里留下痕迹
    assert records == []


def test_call_refuses_when_scene_switch_disabled(monkeypatch):
    def _never(*_a, **_k):
        raise AssertionError("场景开关关闭时不应发请求")

    monkeypatch.setattr(jev_client, "_post_endpoint", _never)
    with pytest.raises(JevUnavailable, match="未启用"):
        jev_client.call_jev(_config(enabled=False), _Store({"jev_api_key": "sk"}), "文本", _ok_questions())


# ---------------------------------------------------------------------------
# 前置校验：state 形状与题目定义
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["", "   ", {}, []])
def test_empty_state_degrades_instead_of_requesting(monkeypatch, state):
    def _never(*_a, **_k):
        raise AssertionError("空 state 不应发请求")

    monkeypatch.setattr(jev_client, "_post_endpoint", _never)
    # 空 state 是「本次没东西可判」的运行时条件 → 降级，不是代码缺陷
    with pytest.raises(JevUnavailable, match="无内容可判"):
        jev_client.call_jev(_config(), _Store({"jev_api_key": "sk"}), state, _ok_questions())


def test_non_text_state_is_a_caller_bug():
    with pytest.raises(ValueError, match="只能是字符串、对象或数组"):
        jev_client.call_jev(_config(), _Store({"jev_api_key": "sk"}), b"bytes", _ok_questions())


def test_empty_questions_degrades():
    with pytest.raises(JevUnavailable, match="没有题目"):
        jev_client.call_jev(_config(), _Store({"jev_api_key": "sk"}), "文本", {})


def test_invalid_question_definition_is_a_caller_bug():
    with pytest.raises(ValueError, match="type"):
        jev_client.call_jev(_config(), _Store({"jev_api_key": "sk"}), "文本", {"q": {"type": "essay"}})


def test_state_accepts_structured_object_and_array(monkeypatch):
    captured = _patch_post(monkeypatch, _OK_PAYLOAD)
    jev_client.call_jev(
        _config(),
        _Store({"jev_api_key": "sk"}),
        {"claims": [{"text": "判断", "evidence": "证据"}], "note": "中文"},
        _ok_questions(),
    )
    # 结构化 state 原样透传（官方推荐 object 形状，字段名自带语义）
    assert captured["json_payload"]["state"]["note"] == "中文"


def test_state_char_count_never_estimates_tokens():
    assert jev_client._state_char_count("中文") == 2
    assert jev_client._state_char_count({"a": "中文"}) > 2


# ---------------------------------------------------------------------------
# 草稿态探测（只读）与模型列表
# ---------------------------------------------------------------------------


def test_probe_uses_chinese_sample_and_returns_model_version(monkeypatch):
    payload = {
        "model": "jev-1.13.0",
        "answers": {"probe_ok": {"type": "noul", "noul": 0.97}},
        "usage": {"input_tokens": 40, "output_tokens": 3},
    }
    captured: dict = {}

    def fake_post(base_url, json_payload, api_key, timeout):
        captured.update(base_url=base_url, json_payload=json_payload, api_key=api_key, timeout=timeout)
        return payload

    monkeypatch.setattr(jev_client, "_post_endpoint", fake_post)
    reply = jev_client.probe_systemone(BASE, "jev-latest", "sk-plaintext", timeout=15.0)

    assert reply.model == "jev-1.13.0"
    assert reply.sample.noul == pytest.approx(0.97)
    assert reply.input_tokens == 40
    assert captured["api_key"] == "sk-plaintext"  # 草稿态允许尚未保存的明文密钥
    assert captured["timeout"] == 15.0
    # 探测题必须是中文——顺带暴露 R6（CJK 精度）的第一手信号
    assert "连通性测试" in captured["json_payload"]["state"]


def test_probe_surfaces_http_error(monkeypatch):
    def fake_post(*_a, **_k):
        raise JevUnavailable(jev_client._http_error_detail(401, ""))

    monkeypatch.setattr(jev_client, "_post_endpoint", fake_post)
    with pytest.raises(JevUnavailable, match="401"):
        jev_client.probe_systemone(BASE, "jev-latest", "sk", timeout=15.0)


def test_list_jev_models_parses_official_models_shape(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return json.dumps(
                {
                    "models": [
                        {"name": "jev-latest", "description": "旗舰", "release_date": "2026-09-15"},
                        {"name": "jev-preview", "description": "预览", "release_date": "2026-09-15"},
                    ]
                }
            ).encode("utf-8")

    monkeypatch.setattr(jev_client.urllib.request, "urlopen", lambda *_a, **_k: _Resp())
    assert jev_client.list_jev_models(BASE, "sk", 10.0) == ["jev-latest", "jev-preview"]


def test_list_jev_models_accepts_openai_shape_for_gateway(monkeypatch):
    """网关（OpenRouter 等）回的是 OpenAI 形状 `{"data":[{"id"}]}`——必须认，否则「拉取模型」永远失败。

    ⚠️ 2026-09-21 行为变更：旧实现只认官方的 `models` 数组，把 `data` 当形状错误报错。
    实测用户走 OpenRouter 时该按钮直接 404/报错，故改为**两种形状都认**。
    """

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return b'{"data":[{"id":"typesafe/jev-1.13"}]}'

    monkeypatch.setattr(jev_client.urllib.request, "urlopen", lambda *_a, **_k: _Resp())
    assert jev_client.list_jev_models("https://openrouter.ai/api/alpha/decisions", "sk", 10.0) == [
        "typesafe/jev-1.13"
    ]


def test_list_jev_models_rejects_unknown_shape(monkeypatch):
    """既没有 `models` 也没有 `data` → 如实报错，不静默返回空列表。"""

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return b'{"ok":true}'

    monkeypatch.setattr(jev_client.urllib.request, "urlopen", lambda *_a, **_k: _Resp())
    with pytest.raises(JevUnavailable, match="models"):
        jev_client.list_jev_models(BASE, "sk", 10.0)


# ---------------------------------------------------------------------------
# 留痕摘要（非密钥回执）
# ---------------------------------------------------------------------------


def test_describe_reply_has_no_secret_and_reports_normalized_score(monkeypatch):
    _patch_post(monkeypatch, _OK_PAYLOAD)
    reply = jev_client.call_jev(_config(), _Store({"jev_api_key": "sk-secret"}), "文本", _ok_questions())
    summary = jev_client.describe_reply(reply)
    assert "sk-secret" not in summary
    assert "jev-1.13.0" in summary
    assert "in=392" in summary
    # 1.05 / (3-1) × 100 = 52.5 → `:.0f` 走 Python 的银行家舍入（半值取偶）→ 52%
    assert "52%" in summary
    assert "低估" in summary


def test_config_normalizes_base_url_and_model():
    assert JevConfig(base_url="https://api.typesafe.ai/v1/", model="").resolved_base_url == "https://api.typesafe.ai/v1"
    assert JevConfig(model="").resolved_model == jev_client.JEV_MODEL_ALIAS
    assert JevConfig(base_url="").resolved_base_url == jev_client.JEV_DEFAULT_BASE_URL


def test_schema_version_is_declared():
    # 题型与 state 结构版本：改动必须升版本（对齐 TACTIC_WEIGHT_VERSION 惯例）
    assert jev_client.JEV_QUESTION_SCHEMA_VERSION == "v1"

"""JV02 契约测试：Jev 配置端点与草稿态探测。

覆盖：
1. 首次访问：从未保存时 GET /jev/config 回内置默认（saved=false、enabled=false）；
2. PUT 持久化：可原样回读，且 created_at 幂等保留、updated_at 刷新；
3. 入参边界：base_url 非 http(s) / 模型名空 / 超时越界一律 422；未带会话令牌 401；
4. 密钥红线：任何响应体都不出现明文密钥，配置里也没有密钥字段（只有 credential_ref）；
   并且**保存时明文会被搬进凭据库**，`jev_config.payload` 只留 key_id；
5. 探测降级：总闸关闭时如实返回「无法真实测试」且**不发请求**；凭据不存在时给可操作提示；
6. 探测成功/拉取模型：按官方响应形状解析（`models` 而非 `data`）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from investment_steward_core import jev_client, model_client
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.credential_store import JEV_API_KEY, DbCredentialStore
from investment_steward_core.domain.models import JevSettings
from sqlalchemy import text

SECRET = "sk-jev-very-secret-value"


@pytest.fixture()
def client(tmp_path: Path):
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


def test_first_visit_returns_builtin_defaults(client):
    test_client, headers = client
    response = test_client.get("/jev/config", headers=headers)
    assert response.status_code == 200
    body = response.json()

    assert body["ok"] is True
    assert body["saved"] is False
    # 默认关闭：state 会出网到第三方，必须由用户显式开启
    assert body["config"]["enabled"] is False
    assert body["config"]["base_url"] == jev_client.JEV_DEFAULT_BASE_URL
    assert body["config"]["model"] == jev_client.JEV_MODEL_ALIAS
    assert body["config"]["credential_ref"] == ""
    assert body["access_enabled"] is True
    assert body["schema_version"] == jev_client.JEV_QUESTION_SCHEMA_VERSION


def test_config_exposes_official_limits_and_data_handling(client):
    test_client, headers = client
    body = test_client.get("/jev/config", headers=headers).json()

    assert body["limits"]["choice_max_options"] == 255
    assert body["limits"]["score_min_levels"] == 2
    assert body["limits"]["score_max_levels"] == 10
    # noul 没有 confidence，阈值在代码里——界面不该出现「置信度」这种字段。
    # 值是 R6 中文标定结果（0.85 / 0.3），不是官方英文范例（0.8 / 0.2）：设置页要如实显示
    # 生效口径，否则用户按官方文档理解判定结果会错档。
    assert body["noul_thresholds"] == {"yes": 0.85, "no": 0.3}
    # 数据出网必须明示（R2 的隐私冲突面）
    assert body["data_handling"]["trains_on_input"] is False
    assert body["data_handling"]["zero_data_retention"] == "enterprise_only"
    assert body["data_handling"]["hosted_in"] == "美国"
    # §E2 拍板（2026-09-21）：**不申请**企业版 ZDR。必须如实暴露给界面——
    # 否则读者会误以为有「零留存」兜底，进而认为 state 可以放宽（白名单按最严口径的前提就在这）。
    assert body["data_handling"]["zdr_applied"] is False
    assert "不申请 ZDR" in "\n".join(body["notes"])


def test_config_response_never_contains_secret_or_secret_field(client):
    test_client, headers = client
    body = test_client.get("/jev/config", headers=headers).json()
    serialized = str(body)
    assert SECRET not in serialized
    # 配置里只允许有引用，不允许有密钥字段本身
    assert "api_key" not in body["config"]
    assert set(body["config"]) >= {"enabled", "base_url", "model", "credential_ref", "timeout_secs"}


def test_put_persists_and_round_trips(client):
    test_client, headers = client
    response = test_client.put(
        "/jev/config",
        headers=headers,
        json={
            "enabled": True,
            "base_url": "https://api.typesafe.ai/v1",
            "model": "jev-1.13.0",
            "credential_ref": "jev_api_key",
            "timeout_secs": 45,
        },
    )
    assert response.status_code == 200
    assert response.json()["saved"] is True

    reloaded = test_client.get("/jev/config", headers=headers).json()
    assert reloaded["saved"] is True
    assert reloaded["config"]["enabled"] is True
    assert reloaded["config"]["model"] == "jev-1.13.0"
    assert reloaded["config"]["credential_ref"] == "jev_api_key"
    assert reloaded["config"]["timeout_secs"] == 45


def test_put_is_idempotent_and_preserves_created_at(client):
    test_client, headers = client
    payload = {"enabled": True, "base_url": "https://api.typesafe.ai/v1", "model": "jev-latest"}
    first = test_client.put("/jev/config", headers=headers, json=payload).json()["config"]
    second = test_client.put("/jev/config", headers=headers, json=payload).json()["config"]
    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] >= first["updated_at"]


# ---------------------------------------------------------------------------
# 保存时把「粘贴的明文密钥」搬进凭据库（2026-09-21 实测缺陷的回归）
# ---------------------------------------------------------------------------


@pytest.fixture()
def file_client(tmp_path: Path, monkeypatch):
    """把凭据后端钉死为文件兜底，避免测试往真实 Windows 凭据管理器里写东西。

    `api.app` 以 `from ...credential_store import resolve_store` 直接绑定名字，
    所以要 patch `api.app` 命名空间上的那个（与 G3/G5 测试同惯例）。
    """
    from investment_steward_core import credential_store as cs
    from investment_steward_core.api import app as api_app

    monkeypatch.setattr(api_app, "resolve_store", lambda db: cs.DbCredentialStore(db))
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


def test_put_moves_plaintext_key_into_credential_store(file_client):
    """明文密钥保存时必须搬进凭据库，配置表只留 key_id。

    锁住两个真实缺陷（2026-09-21 用 OpenRouter 密钥实测发现）：
    ① 明文原样写进 `jev_config.payload` —— 直接违反 P0 验收项「密钥零泄漏」，
       而前端文案早已承诺「明文不会写进本配置」（承诺不能只靠调用方自觉）；
    ② `call_jev` 走 `resolve_credential`（只认 key_id 与尾号，**不认明文**），
       明文 ref 会造出「测连通性通过、协同流水线却解析不到密钥」的错位——
       用户以为 Jev 装好了，其实一次都没跑成。
    """
    test_client, headers = file_client
    response = test_client.put(
        "/jev/config",
        headers=headers,
        json={
            "enabled": True,
            "base_url": "https://openrouter.ai/api/alpha/decisions",
            "model": "typesafe/jev-1.13",
            "credential_ref": SECRET,
            "timeout_secs": 60,
        },
    )
    body = response.json()

    # 配置里只留 key_id，回执如实说明搬到了哪里，但不回显密钥
    assert body["config"]["credential_ref"] == JEV_API_KEY
    assert "已存入本机凭据库" in (body["credential_note"] or "")
    assert SECRET not in str(body)

    # 明文确实进了凭据库，且流水线那条解析路径能取到它
    store = DbCredentialStore(test_client.app.state.core.database)
    assert store.get(JEV_API_KEY) == SECRET
    assert model_client.resolve_credential(store, JEV_API_KEY) == SECRET

    # 库里落的是引用，不是明文——直接读原始 payload，别只看模型层
    database = test_client.app.state.core.database
    with database._connection() as connection:
        raw = connection.execute(text("SELECT payload FROM jev_config")).scalar_one()
    assert SECRET not in raw
    assert JEV_API_KEY in raw


def test_put_maps_credential_tail_to_key_id(file_client):
    """填凭据尾号也能存：与 `resolve_credential` 的尾号兜底同口径（单人本机产品允许凭直觉填）。"""
    test_client, headers = file_client
    test_client.put(
        "/jev/config",
        headers=headers,
        json={"base_url": "https://api.typesafe.ai/v1", "model": "jev-latest", "credential_ref": SECRET},
    )
    tail = SECRET[-4:]
    body = test_client.put(
        "/jev/config",
        headers=headers,
        json={"base_url": "https://api.typesafe.ai/v1", "model": "jev-latest", "credential_ref": tail},
    ).json()
    assert body["config"]["credential_ref"] == JEV_API_KEY
    assert "尾号" in (body["credential_note"] or "")


def test_put_keeps_unknown_ref_as_is(client):
    """既非明文、又匹配不到凭据的引用原样保存——不猜、不报错，让用户在探测时拿到可操作提示。"""
    test_client, headers = client
    body = test_client.put(
        "/jev/config",
        headers=headers,
        json={"base_url": "https://api.typesafe.ai/v1", "model": "jev-latest", "credential_ref": "jev_api_key"},
    ).json()
    assert body["config"]["credential_ref"] == "jev_api_key"
    assert body["credential_note"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"base_url": "ftp://nope"},
        {"base_url": ""},
        {"model": ""},
        {"model": "   "},
        {"timeout_secs": 1},
        {"timeout_secs": 9999},
    ],
)
def test_put_rejects_invalid_payload(client, payload):
    test_client, headers = client
    response = test_client.put("/jev/config", headers=headers, json=payload)
    assert response.status_code == 422


def test_put_requires_session(client):
    test_client, _headers = client
    response = test_client.put("/jev/config", json={"enabled": True})
    assert response.status_code == 401


@pytest.fixture()
def gated_client(tmp_path: Path):
    """总闸关闭（STEWARD_MODEL_ACCESS=0）的实例：用于验证「零出网」。"""
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path, model_access_enabled=False))
    return TestClient(app), {"X-Core-Session-Token": token}


def test_probe_reports_gate_closed_without_request(gated_client, monkeypatch):
    test_client, headers = gated_client

    def _never(*_a, **_k):
        raise AssertionError("总闸关闭时不应发请求")

    monkeypatch.setattr(jev_client, "probe_systemone", _never)
    response = test_client.post(
        "/jev/probe",
        headers=headers,
        json={"base_url": jev_client.JEV_DEFAULT_BASE_URL, "model": "jev-latest", "credential_ref": "jev_api_key"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "总闸" in body["detail"]


def test_config_reports_gate_closed_to_ui(gated_client):
    """总闸关闭时配置接口必须如实告知，设置页据此把按钮降级。"""
    test_client, headers = gated_client
    assert test_client.get("/jev/config", headers=headers).json()["access_enabled"] is False


def test_probe_reports_unknown_credential(client, monkeypatch):
    test_client, headers = client

    def _never(*_a, **_k):
        raise AssertionError("凭据解析失败时不应发请求")

    monkeypatch.setattr(jev_client, "probe_systemone", _never)
    response = test_client.post(
        "/jev/probe",
        headers=headers,
        json={
            "base_url": jev_client.JEV_DEFAULT_BASE_URL,
            "model": "jev-latest",
            "credential_ref": "不存在的凭据引用-abcdefghijklmnop",
        },
    )
    body = response.json()
    assert body["ok"] is False
    assert "凭据" in body["detail"]


def test_probe_accepts_unsaved_plaintext_key(client, monkeypatch):
    """草稿态探测允许「先试后存」：明文密钥直接用，不落库、不写凭据。"""
    test_client, headers = client
    captured: dict = {}

    def fake_probe(base_url, model, api_key, *, timeout):
        captured.update(base_url=base_url, model=model, api_key=api_key, timeout=timeout)
        return jev_client.JevProbeReply(
            model="jev-1.13.0",
            requested_model=model,
            latency_ms=123,
            sample=jev_client.JevAnswer(question_id="probe_ok", type="noul", noul=0.97),
            input_tokens=40,
            output_tokens=3,
        )

    monkeypatch.setattr(jev_client, "probe_systemone", fake_probe)
    response = test_client.post(
        "/jev/probe",
        headers=headers,
        json={"base_url": jev_client.JEV_DEFAULT_BASE_URL, "model": "jev-latest", "credential_ref": SECRET},
    )
    body = response.json()

    assert body["ok"] is True
    assert body["latency_ms"] == 123
    assert body["model"] == "jev-1.13.0"
    assert body["probe_noul"] == pytest.approx(0.97)
    assert body["probe_verdict"] == "yes"
    assert captured["api_key"] == SECRET
    # 回执里不得出现密钥原文
    assert SECRET not in str(body)


def test_probe_surfaces_unavailable_as_readable_detail(client, monkeypatch):
    test_client, headers = client

    def fake_probe(*_a, **_k):
        raise jev_client.JevUnavailable("服务端返回 401（密钥缺失或无效）：请检查凭据引用")

    monkeypatch.setattr(jev_client, "probe_systemone", fake_probe)
    response = test_client.post(
        "/jev/probe",
        headers=headers,
        json={"base_url": jev_client.JEV_DEFAULT_BASE_URL, "model": "jev-latest", "credential_ref": SECRET},
    )
    body = response.json()
    assert body["ok"] is False
    assert "401" in body["detail"]
    assert SECRET not in str(body)


def test_discover_models_parses_official_shape(client, monkeypatch):
    test_client, headers = client
    monkeypatch.setattr(jev_client, "list_jev_models", lambda *_a, **_k: ["jev-latest", "jev-preview"])
    response = test_client.post(
        "/jev/models",
        headers=headers,
        json={"base_url": jev_client.JEV_DEFAULT_BASE_URL, "credential_ref": SECRET},
    )
    body = response.json()
    assert body["ok"] is True
    assert body["models"] == ["jev-latest", "jev-preview"]


def test_discover_models_reports_gate_closed(gated_client, monkeypatch):
    test_client, headers = gated_client

    def _never(*_a, **_k):
        raise AssertionError("总闸关闭时不应发请求")

    monkeypatch.setattr(jev_client, "list_jev_models", _never)
    response = test_client.post(
        "/jev/models", headers=headers, json={"base_url": jev_client.JEV_DEFAULT_BASE_URL}
    )
    body = response.json()
    assert body["ok"] is False
    assert body["models"] == []
    assert "总闸" in body["detail"]


# ---------------------------------------------------------------------------
# `build_judge`：JV03/JV04 共用的整层开关（P0 验收项「总闸关闭时零出网」的守门人）
# ---------------------------------------------------------------------------


def _core_with_jev(tmp_path: Path, *, enabled: bool, access: bool):
    """造一个真实 CoreState（走真库），把 Jev 配置写成 enabled。"""
    app = create_app(
        CoreSettings(session_token="t", data_dir=tmp_path, model_access_enabled=access)
    )
    core = app.state.core
    core.database.upsert_jev_settings(
        JevSettings(
            user_id=core.local_user_id,
            enabled=enabled,
            base_url="https://openrouter.ai/api/alpha/decisions",
            model="typesafe/jev-1.13",
            credential_ref="jev_api_key",
        )
    )
    return core


def test_build_judge_returns_none_when_jev_disabled(tmp_path):
    """场景开关关着 → 返回 None（整层不运行），而不是「跑一个空判定」。

    `validate_report(jev_judge=None)` 的行为与接入前**逐字节一致**（JV00 铁律 6）：
    关掉就要真的关掉，不留半截行为。
    """
    core = _core_with_jev(tmp_path, enabled=False, access=True)
    assert jev_client.build_judge(core, DbCredentialStore(core.database)) is None


def test_build_judge_returns_none_when_global_gate_closed(tmp_path, monkeypatch):
    """总闸关着（STEWARD_MODEL_ACCESS=0）→ 返回 None，**即使 Jev 已启用**。

    这是 P0 验收项「总闸关闭下所有 Jev 场景零出网」的守门人：判定必须在**构造回调时**
    就返回 None，不能等到回调被调用才拦——否则调用方会拿到一个看似可用的 judge。
    """
    core = _core_with_jev(tmp_path, enabled=True, access=False)

    def _never(*_a, **_k):
        raise AssertionError("总闸关闭时不应有任何出网调用")

    monkeypatch.setattr(jev_client, "call_jev", _never)
    assert jev_client.build_judge(core, DbCredentialStore(core.database)) is None


def test_build_judge_is_inert_until_invoked(tmp_path, monkeypatch):
    """两项都开着 → 返回可调用对象，且**构造本身不出网**；调用时才出一次网。

    构造与调用分离是「纯函数 + 注入回调」分工的关键（《契约》§J1）：`report_quality` 保持
    纯函数，出网只发生在闸门真的需要判定的时候。构造即出网会让「只是渲染一份报告」也计费。
    """
    core = _core_with_jev(tmp_path, enabled=True, access=True)
    calls: list[dict] = []

    def _fake_call_jev(config, store, state, questions, **kwargs):
        calls.append({"state": state, "questions": questions, **kwargs})
        return jev_client.JevReply(
            model="typesafe/jev-1.13-20260917",
            requested_model=config.model,
            latency_ms=1,
            answers={},
            input_tokens=1,
            output_tokens=1,
        )

    monkeypatch.setattr(jev_client, "call_jev", _fake_call_jev)
    judge = jev_client.build_judge(core, DbCredentialStore(core.database), purpose="jev:claim-support")
    assert judge is not None
    assert calls == [], "构造回调本身不得出网"

    assert judge("判断原句 + 证据", {"c1_supports": {"type": "choice", "criteria": {"support": "x"}}}) == {
        "answers": {},
        "model": "typesafe/jev-1.13-20260917",
    }
    assert len(calls) == 1
    # purpose 必须带 jev: 前缀，否则 model_calls 归集会把两套协议混在一起
    assert calls[0]["purpose"] == "jev:claim-support"
    assert calls[0]["access_enabled"] is True



def test_saving_plaintext_key_leaves_no_trace_in_audit_or_storage(file_client):
    """P0 验收项「密钥出现在日志/审计/前端响应中的自动化检查为零命中」的落库侧守门人。

    只断言「响应里没有密钥」是不够的——真正的泄漏面是**审计事件与数据库**。这里保存一次
    明文密钥后，把审计事件与**整库每一张表的原始行**都扫一遍。表驱动而不是只扫 `jev_config`：
    将来多一张表、多一个 payload 字段，这条测试照样拦得住。
    """
    test_client, headers = file_client
    test_client.put(
        "/jev/config",
        headers=headers,
        json={
            "base_url": "https://openrouter.ai/api/alpha/decisions",
            "model": "typesafe/jev-1.13",
            "credential_ref": SECRET,
        },
    )
    core = test_client.app.state.core

    # ① 审计事件（明文密钥绝不能进审计 payload）
    audit_text = "\n".join(
        str(event.model_dump(mode="json")) for event in core.database.list_audit(core.local_user_id)
    )
    assert SECRET not in audit_text

    # ② 整库逐表原始行。**`credentials` 是唯一的合法存放处**——凭据库本来就是放密钥的地方
    #（Windows 上明文在 OS 凭据管理器，表里只剩 last4；本测试用文件后端，故明文落这张表）。
    # 要断言的是「除凭据库外一处都没有」。
    leaked: list[str] = []
    with core.database._connection() as connection:
        tables = [
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            )
        ]
        for table in tables:
            if table.startswith("sqlite_") or table == "credentials":
                continue
            try:
                rows = connection.execute(text(f'SELECT * FROM "{table}"')).fetchall()
            except Exception:  # noqa: BLE001, S112 - 虚拟表/异常表跳过
                continue
            for row in rows:
                if SECRET in " ".join(str(value) for value in row if value is not None):
                    leaked.append(table)
    assert leaked == [], f"密钥明文出现在凭据库以外的这些表里：{leaked}"

    # ③ 反向确认：它确实在凭据库里（否则上一条会因为「哪都没有」而假通过）
    assert DbCredentialStore(core.database).get(JEV_API_KEY) == SECRET

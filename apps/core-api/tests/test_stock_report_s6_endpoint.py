"""P2-C04 端到端：个股研报端点在有/无上榜时的 S6 行为——全程不联网。

这是本路线图里最值得端到端验证的一条：S6 必须**只**在该票上榜时出现。
下面用真实龙虎榜夹具打桩 `lhb_feed` 的取数函数，走完整端点，
断言「上榜 → 有 S6」「未上榜 → 无 S6 且 S1–S5 不变」。
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

from conftest import load_youzi_json
from investment_steward_core import lhb_feed as lf


def _file_client(tmp_path, monkeypatch, **settings_overrides):
    from fastapi.testclient import TestClient
    from investment_steward_core.api import app as api_app
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path, **settings_overrides))
    return TestClient(app), {"X-Core-Session-Token": token}


def _credential_setup(test_client, headers, key_id: str, value: str):
    """写入一条凭据。

    路径与字段名都从应用对象里取，**不按字面手打**：仓库对凭据路由/字段做了
    同形字混淆（防猜测），照抄文档里的字形会拿到 404/422。
    """
    from investment_steward_core.api.app import CredentialUpsertRequest

    template = next(
        route.path
        for route in test_client.app.routes
        if getattr(route, "path", "").endswith("/{key_id}")
    )
    field = next(iter(CredentialUpsertRequest.model_fields))
    response = test_client.put(
        template.replace("{key_id}", key_id), json={field: value}, headers=headers
    )
    assert response.status_code in (200, 201), response.text
    return response


def _setup(tmp_path, monkeypatch, **settings_overrides):
    test_client, headers = _file_client(tmp_path, monkeypatch, **settings_overrides)
    assert (
        test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code
        == 200
    )
    _credential_setup(test_client, headers, "model_api_key", "sk-test-1234567890")
    created = test_client.post(
        "/model-profiles",
        json={
            "name": "Deepseek",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
            "credential_ref": "model_api_key",
        },
        headers=headers,
    ).json()
    assert (
        test_client.post(
            f"/model-profiles/{created['profile_id']}/activate", headers=headers
        ).status_code
        == 200
    )
    return test_client, headers


def _synth_bars(count: int = 60) -> list[dict[str, object]]:
    rows = []
    price = 10.0
    for index in range(count):
        price = round(price * 1.01, 2)
        rows.append(
            {
                "timestamp": f"2026-06-{(index % 28) + 1:02d}",
                "open": price * 0.99,
                "close": price,
                "high": price * 1.02,
                "low": price * 0.98,
                "volume": 1_000_000 + index * 1000,
            }
        )
    return rows


def _base_sources(monkeypatch):
    """打桩 S1–S5 五类来源（不含 S6），确保端点能走到证据组装之后。

    这里的桩**形状照抄** `test_v31_repair_and_comparison._mock_sources`——
    财务行用 `statement_type/period_end/items` 结构、估值用带 `percentiles` 的
    `ValuationSnapshot`。形状不对会被端点内部按「取数失败」吞掉，
    结果是 S4/S5 静默缺席（本文件第一版就踩了这个坑）。
    """
    import investment_steward_core.api.app as app_module
    from investment_steward_core.valuation_evidence import ValuationSnapshot

    monkeypatch.setattr(
        app_module,
        "fetch_cn_kline",
        lambda symbol, limit=250, period="day": (_synth_bars(), "test-provider"),
    )
    monkeypatch.setattr(
        app_module,
        "fetch_cn_news",
        lambda symbol, limit=20: [
            SimpleNamespace(
                published_raw="2026-09-01", title="甲股中标大单", content="内容正文"
            )
        ],
    )
    monkeypatch.setattr(
        app_module,
        "fetch_cn_announcements",
        lambda symbol, limit=20: [
            SimpleNamespace(
                notice_date_raw="2026-09-02",
                title="关于回购的公告",
                ann_type="回购",
                url="http://x",
                art_code="A1",
            )
        ],
    )
    monkeypatch.setattr(
        app_module,
        "fetch_financials",
        lambda symbol, limit_per_type=8: [
            SimpleNamespace(
                statement_type="income",
                period_end="2026-06-30",
                published_raw="2026-08-30",
                items={"revenue": "1000000000", "net_profit": "120000000"},
                identity="h-income-2606",
            ),
            SimpleNamespace(
                statement_type="income",
                period_end="2025-06-30",
                published_raw="2025-08-30",
                items={"revenue": "900000000", "net_profit": "100000000"},
                identity="h-income-2506",
            ),
        ],
    )
    monkeypatch.setattr(
        app_module,
        "fetch_valuation",
        lambda symbol, **kwargs: ValuationSnapshot(
            symbol=symbol,
            name="甲股",
            board_code="016165",
            board_name="测试行业",
            trade_date="2026-09-10",
            close_price=10.0,
            pe_ttm=19.73,
            pe_static=19.52,
            pb_mrq=6.39,
            ps_ttm=9.27,
            peg=1.0,
            pcf_ocf_ttm=13.49,
            total_market_cap=1e11,
            float_market_cap=1e11,
            total_shares=1e10,
            percentiles={"pe_ttm": 6.7, "pb_mrq": 4.2, "ps_ttm": 3.6},
            percentile_window_bars=1250,
            percentile_window_from="2021-07-19",
        ),
    )


def _capture_seat_sources(monkeypatch) -> dict[str, object]:
    """拦截 `_build_stock_evidence` 的返回值源头：记录到底有没有 S6。"""
    import investment_steward_core.api.app as app_module

    seen: dict[str, object] = {}

    original = app_module.create_app

    def _wrap(settings):
        app = original(settings)
        return app

    monkeypatch.setattr(app_module, "create_app", _wrap)
    return seen


def _stub_lhb_from_fixture(monkeypatch, *, listed: bool) -> None:
    """用真实夹具打桩龙虎榜取数。

    `listed=True` 时榜单含 000592（真实上榜），`listed=False` 时榜单为空。
    """
    billboard = lf.parse_billboard_rows(load_youzi_json("em_lhb_details_20260916.json"))
    seats = lf.parse_seat_rows(
        load_youzi_json("em_lhb_detailbuy_20260916.json"), lf.DIRECTION_BUY
    ) + lf.parse_seat_rows(load_youzi_json("em_lhb_detailsell_20260916.json"), lf.DIRECTION_SELL)

    if listed:
        monkeypatch.setattr(lf, "fetch_billboard", lambda day: billboard)
        monkeypatch.setattr(lf, "fetch_seats", lambda day: seats)
    else:
        monkeypatch.setattr(lf, "fetch_billboard", lambda day: [])
        monkeypatch.setattr(lf, "fetch_seats", lambda day: [])


_EVIDENCE_LINE = re.compile(r"^\[(S\d)\]\s", re.MULTILINE)


def _evidence_keys(payload: dict) -> set[str]:
    """从送模型的 payload 里取出**证据行**的来源键。

    只认真实证据行（行首 `[S#] `），因为提示词铁律里也会提到 `[S1]..[S6]`——
    直接搜子串会把「说明文字」误判成「证据存在」（本文件第一版就踩了这个坑）。
    """
    keys: set[str] = set()
    for message in payload.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            keys.update(_EVIDENCE_LINE.findall(content))
    return keys


def _model_reply(monkeypatch, calls: list[set[str]]) -> None:
    """打桩模型调用，逐次记录本次送进模型的**证据来源键**。

    每次调用追加一个新集合——共用同一个集合会让上一次调用的结果泄漏到下一次断言。
    """
    import investment_steward_core.model_client as mc

    def _fake(_base_url, payload, _key, _timeout):
        calls.append(_evidence_keys(payload))
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "title": "测试报告",
                                "executive_summary": "摘要",
                                "report": "".join(
                                    f"## {name}\n本节按事实展开，给出可核对的数据与边界条件。\n"
                                    for name in ("技术面", "消息面", "基本面", "综合判断")
                                ),
                                "core_judgments": [],
                                "catalysts": [],
                                "risks": [],
                                "data_gaps": [],
                                "next_verification": "跟踪",
                                "citations": ["S1"],
                                "limitations": ["局限一", "局限二", "局限三"],
                                "confidence": "medium",
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(mc, "_post_json", _fake)


def _report_body(test_client, headers, symbol: str) -> dict:
    return test_client.post(
        "/evidence/stock-research-report",
        json={"symbol": symbol, "with_counter_check": False, "with_auto_repair": False},
        headers=headers,
    ).json()


def test_listed_stock_gets_s6_and_unlisted_does_not(tmp_path, monkeypatch):
    """上榜 → 证据里出现 S6；未上榜 → 证据里不出现 S6。"""
    _base_sources(monkeypatch)
    _stub_lhb_from_fixture(monkeypatch, listed=True)
    calls: list[set[str]] = []
    _model_reply(monkeypatch, calls)

    test_client, headers = _setup(tmp_path, monkeypatch)
    _report_body(test_client, headers, "000592")
    listed_keys = calls[-1]

    _report_body(test_client, headers, "600519")
    unlisted_keys = calls[-1]

    assert "S6" in listed_keys, "上榜股票的证据包应包含席位来源 S6"
    assert "S6" not in unlisted_keys, "未上榜股票不得出现 S6（也不得报缺口）"


def test_unlisted_stock_keeps_s1_to_s5_unchanged(tmp_path, monkeypatch):
    """未上榜时，S1–S5 五类来源一个不少、一个不多。"""
    _base_sources(monkeypatch)
    _stub_lhb_from_fixture(monkeypatch, listed=False)
    calls: list[set[str]] = []
    _model_reply(monkeypatch, calls)

    test_client, headers = _setup(tmp_path, monkeypatch)
    _report_body(test_client, headers, "600519")
    keys = calls[-1]

    assert {"S1", "S2", "S3", "S4", "S5"} <= keys
    assert "S6" not in keys


def test_s6_absence_is_not_reported_as_a_source_error(tmp_path, monkeypatch):
    """未上榜不能进 source_errors——那会把「正常」报成「失败」。"""
    _base_sources(monkeypatch)
    _stub_lhb_from_fixture(monkeypatch, listed=False)
    calls: list[set[str]] = []
    _model_reply(monkeypatch, calls)

    test_client, headers = _setup(tmp_path, monkeypatch)
    body = _report_body(test_client, headers, "600519")
    errors = json.dumps(body.get("source_errors") or {}, ensure_ascii=False)
    assert "seat" not in errors.lower()
    assert "龙虎榜" not in errors


def test_lookback_zero_disables_the_source(tmp_path, monkeypatch):
    """`youzi_lookback_days=0` 显式关闭该来源，此时不得出现 S6。"""
    _base_sources(monkeypatch)
    _stub_lhb_from_fixture(monkeypatch, listed=True)
    calls: list[set[str]] = []
    _model_reply(monkeypatch, calls)

    test_client, headers = _setup(tmp_path, monkeypatch, youzi_lookback_days=0)
    _report_body(test_client, headers, "000592")
    assert calls, "打桩链路应至少送出一份证据"
    assert "S6" not in calls[-1]


def _evidence_blocks(payload: dict) -> dict[str, str]:
    """把送模型的提示词按 `[S#]` 切成 {来源键: 证据正文}，用于逐字比对。"""
    text = ""
    for message in payload.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            text += content + "\n"
    blocks: dict[str, str] = {}
    matches = list(_EVIDENCE_LINE.finditer(text))
    for index, match in enumerate(matches):
        key = match.group(1)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        # 证据段以空行为界；空行之后通常是提示词指令，不属于证据。
        body = text[start:end].split("\n\n", 1)[0]
        if key not in blocks:
            blocks[key] = body.strip()
    return blocks


def _capture_prompt(monkeypatch, captured: list[dict]) -> None:
    """打桩模型调用并保存完整 payload，用于逐字比对证据正文。"""
    import investment_steward_core.model_client as mc

    def _fake(_base_url, payload, _key, _timeout):
        captured.append(payload)
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "title": "测试报告",
                                "executive_summary": "摘要",
                                "report": "".join(
                                    f"## {name}\n本节按事实展开，给出可核对的数据与边界条件。\n"
                                    for name in ("技术面", "消息面", "基本面", "综合判断")
                                ),
                                "core_judgments": [],
                                "catalysts": [],
                                "risks": [],
                                "data_gaps": [],
                                "next_verification": "跟踪",
                                "citations": ["S1"],
                                "limitations": ["局限一", "局限二", "局限三"],
                                "confidence": "medium",
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(mc, "_post_json", _fake)


def test_s1_to_s5_text_is_byte_identical_with_and_without_s6(tmp_path, monkeypatch):
    """C04 验收原文：对**同一只票**，S1–S5 必须逐字不变。

    同一标的跑两次——一次开启席位来源（该票真实上榜），一次关闭——再逐字比对
    S1–S5 五段证据正文。这比「键集合相同」更强：能发现「加了 S6 却顺手改了 S5 文案」
    这类回归。
    """
    _base_sources(monkeypatch)
    _stub_lhb_from_fixture(monkeypatch, listed=True)

    captured: list[dict] = []
    _capture_prompt(monkeypatch, captured)

    on_client, on_headers = _setup(tmp_path / "on", monkeypatch, youzi_lookback_days=1)
    _report_body(on_client, on_headers, "000592")
    on_blocks = _evidence_blocks(captured[-1])

    off_client, off_headers = _setup(tmp_path / "off", monkeypatch, youzi_lookback_days=0)
    _report_body(off_client, off_headers, "000592")
    off_blocks = _evidence_blocks(captured[-1])

    assert "S6" in on_blocks, "开启时该票应带 S6"
    assert "S6" not in off_blocks, "关闭时不应有 S6"

    for key in ("S1", "S2", "S3", "S4", "S5"):
        assert key in on_blocks and key in off_blocks, f"{key} 两侧都应存在"
        assert on_blocks[key] == off_blocks[key], f"{key} 在加入 S6 后发生了变化（应逐字不变）"

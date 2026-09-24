"""v34 方向研判板块行情证据层测试（2026-09-14）。

- 纯函数：主题 → 行业板块解析（直接包含 / 实盘核验别名表 / 去重封顶 / 风格主题不硬凑）；
- 纯函数：板块快照文本（数值 + 当日声明，标注「非景气度结论」防混层）；
- 端点：板块证据注入证据包与提示词；插件未启用 / 取数失败 / 主题未命中三条降级路径如实声明。

约定：行情/板块取数一律 monkeypatch，绝不真实联网；
需要写模型凭据的用例走 _file_client（文件凭据后端），不污染系统凭据管理器。
"""

from __future__ import annotations

import json

from investment_steward_core import direction_research as direction_engine

_SECTOR_ROWS = [
    {
        "code": "new_jtys",
        "name": "交通运输",
        "member_count": 87,
        "change_pct": -0.04,
        "amount": 2.05887e10,
        "leader_symbol": "601872",
        "leader_name": "招商轮船",
        "leader_change_pct": 0.0,
    },
    {
        "code": "new_dzqj",
        "name": "电子器件",
        "member_count": 152,
        "change_pct": 0.23,
        "amount": 1.37789e11,
        "leader_symbol": "000636",
        "leader_name": "风华高科",
        "leader_change_pct": 5.20,
    },
    {
        "code": "new_dzxx",
        "name": "电子信息",
        "member_count": 247,
        "change_pct": -0.77,
        "amount": 1.23451e11,
        "leader_symbol": "600330",
        "leader_name": "天通股份",
        "leader_change_pct": 10.01,
    },
    {
        "code": "new_mthy",
        "name": "煤炭行业",
        "member_count": 41,
        "change_pct": -0.94,
        "amount": 1.45034e10,
        "leader_symbol": "601225",
        "leader_name": "陕西煤业",
        "leader_change_pct": 1.2,
    },
    {
        "code": "new_blhy",
        "name": "玻璃行业",
        "member_count": 19,
        "change_pct": 0.50,
        "amount": 1.98963e10,
        "leader_symbol": "600176",
        "leader_name": "中国巨石",
        "leader_change_pct": 3.3,
    },
]

_MEMBERS = [
    {"symbol": "601872", "name": "招商轮船", "price": 20.0, "change_pct": 0.0, "turnover": 4.35e9},
    {"symbol": "600026", "name": "中远海能", "price": 20.9, "change_pct": 0.19, "turnover": 1.87e9},
    {
        "symbol": "601919",
        "name": "中远海控",
        "price": 16.1,
        "change_pct": -0.56,
        "turnover": 1.13e9,
    },
    {"symbol": "601006", "name": "大秦铁路", "price": 4.81, "change_pct": 0.21, "turnover": 5.7e8},
    {"symbol": "600115", "name": "中国东航", "price": 3.37, "change_pct": -1.17, "turnover": 4.3e8},
]


# ---------------------------------------------------------------------------
# 单元：主题 → 板块解析（纯函数）
# ---------------------------------------------------------------------------


def test_match_sector_direct_contains_both_ways():
    """直接匹配：核心词「煤炭」⊂ 板块名「煤炭行业」（去后缀）；宽主题 ⊃ 板块名；清单外主题不硬凑。"""
    assert [row["code"] for row in direction_engine.match_sector_rows("煤炭", _SECTOR_ROWS)] == [
        "new_mthy"
    ]
    assert [
        row["code"] for row in direction_engine.match_sector_rows("玻璃行业涨价", _SECTOR_ROWS)
    ] == ["new_blhy"]
    assert direction_engine.match_sector_rows("船舶制造板块", _SECTOR_ROWS) == []


def test_match_sector_alias_from_real_data():
    """别名映射（实盘核验）：海运→交通运输、半导体/光伏→电子器件。"""
    assert [
        row["code"] for row in direction_engine.match_sector_rows("海运板块", _SECTOR_ROWS)
    ] == ["new_jtys"]
    assert [
        row["code"] for row in direction_engine.match_sector_rows("半导体设备", _SECTOR_ROWS)
    ] == ["new_dzqj"]
    assert [
        row["code"] for row in direction_engine.match_sector_rows("光伏产业链", _SECTOR_ROWS)
    ] == ["new_dzqj"]


def test_match_sector_multiple_aliases_dedup_and_cap():
    """复合主题（AI 芯片）：两条别名命中两板块；同板块多别名去重；封顶 2。"""
    matched = direction_engine.match_sector_rows("AI芯片", _SECTOR_ROWS)
    assert [row["code"] for row in matched] == ["new_dzqj", "new_dzxx"]
    # 同一板块的多个别名只进一次
    assert [
        row["code"] for row in direction_engine.match_sector_rows("海运航运", _SECTOR_ROWS)
    ] == ["new_jtys"]
    # 封顶：三条别名也只取前两个板块
    matched_many = direction_engine.match_sector_rows("AI芯片半导体电子", _SECTOR_ROWS)
    assert len(matched_many) <= 2


def test_match_sector_style_topic_no_hard_match():
    """风格/概念主题（低估板块）不硬凑板块：空列表，由调用方声明缺口。"""
    assert direction_engine.match_sector_rows("低估板块", _SECTOR_ROWS) == []
    assert direction_engine.match_sector_rows("", _SECTOR_ROWS) == []
    assert direction_engine.match_sector_rows("白酒", []) == []
    assert direction_engine.match_sector_rows("白酒", "不是列表") == []  # type: ignore[arg-type]


def test_describe_sector_snapshot_fields_and_disclaimer():
    """快照文本：数值 + 排名 + 领涨股 + 成分前五；固定标注当日口径与非景气度结论。"""
    text = direction_engine.describe_sector_snapshot(
        _SECTOR_ROWS[0], _MEMBERS, rank=1, total=len(_SECTOR_ROWS)
    )
    assert "板块「交通运输」" in text
    assert "成分股 87 只" in text
    assert "当日涨跌 -0.04%" in text
    assert "当日涨跌幅排名 1/5" in text
    assert "成交额 205.9 亿元" in text
    assert "领涨股 招商轮船 +0.00%" in text
    assert "招商轮船(601872) +0.00%" in text and "中远海控(601919) -0.56%" in text
    assert "非景气度结论" in text and "当日行情快照" in text
    # 成分缺数 / 字段缺失不崩，仅少对应片段
    sparse = direction_engine.describe_sector_snapshot({"name": "空板块"}, [], rank=0, total=0)
    assert sparse.startswith("板块「空板块」") and "非景气度结论" in sparse


# ---------------------------------------------------------------------------
# 端点：板块证据层注入 / 三条降级路径
# ---------------------------------------------------------------------------


def _good_direction_payload() -> dict:
    sections = "".join(
        f"## {name}\n本节按事实→推理→不确定性展开，给出可核对的数据与边界条件，避免空泛表述。\n"
        for name in direction_engine.REQUIRED_DIRECTION_SECTIONS
    )
    return {
        "title": "海运：运价周期与板块结构",
        "executive_summary": "方向判断：油运供需偏紧。最强证据：板块当日行情快照 [E5]。最大不确定性：地缘溢价回落速度。",
        "report": sections,
        "core_judgments": [
            {
                "id": "J1",
                "text": "2026 年油运吨海里需求增长",
                "kind": "industry",
                "confidence": "medium",
            },
            {
                "id": "J2",
                "text": "头部船东格局集中",
                "kind": "competitiveness",
                "confidence": "low",
            },
            {"id": "J3", "text": "板块当日资金表现分化", "kind": "sentiment", "confidence": "low"},
        ],
        "catalysts": ["红海航线恢复节奏"],
        "risks": ["新船交付集中"],
        "stock_pool": [
            {
                "symbol_raw": "601919",
                "name": "中远海控",
                "sector": "海运",
                "business_link": "集运主干航线",
                "profit_path": "运价与舱位利用率",
                "counter_evidence": "板块当日成交额萎缩",
            },
        ],
        "data_gaps": ["行业运价指数未接入"],
    }


def _direction_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from investment_steward_core.api import app as api_app
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


def _setup_profile(test_client, headers) -> str:
    test_client.put(
        "/credentials/model_api_key", json={"secret": "sk-test-1234567890"}, headers=headers
    )
    created = test_client.post(
        "/model-profiles",
        json={
            "name": "方案A",
            "base_url": "https://api.a.com",
            "model": "model-a",
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
    return created["profile_id"]


def _stub_model(monkeypatch):
    from investment_steward_core import model_client as mc

    calls: list[str] = []

    def _fake(_base_url, payload, _key, _timeout):
        blob = json.dumps(payload, ensure_ascii=False)
        calls.append(blob)
        return {
            "choices": [
                {"message": {"content": json.dumps(_good_direction_payload(), ensure_ascii=False)}}
            ]
        }

    monkeypatch.setattr(mc, "_post_json", _fake)
    return calls


def test_direction_evidence_includes_sector_snapshot(tmp_path, monkeypatch):
    """主题「海运板块」命中交通运输 → 板块快照进证据包与提示词，E# 编号衔接。"""
    from investment_steward_core.api import app as app_module

    test_client, headers = _direction_client(tmp_path, monkeypatch)
    _setup_profile(test_client, headers)
    calls = _stub_model(monkeypatch)
    assert (
        test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code
        == 200
    )
    monkeypatch.setattr(app_module, "fetch_cn_sector_list", lambda: _SECTOR_ROWS)
    monkeypatch.setattr(
        app_module,
        "fetch_cn_sector_members",
        lambda code, top=60: _MEMBERS[:top] if code == "new_jtys" else [],
    )

    body = test_client.post(
        "/evidence/direction-research",
        json={"topic": "海运板块", "question": "运价周期怎么看", "with_counter_check": False},
        headers=headers,
    ).json()
    assert body["ok"] is True
    assert body["research_mode"] == "evidence"
    sector_items = [
        item for item in body["evidence_snapshot"] if item["source_type"] == "sector_market"
    ]
    assert len(sector_items) == 1
    item = sector_items[0]
    assert item["sector_code"] == "new_jtys"
    assert "板块「交通运输」" in item["content"]
    assert "招商轮船(601872)" in item["content"]
    assert item["retrieved_at"]
    assert "板块行情" not in (body["evidence_unavailable"] or "")
    # 提示词携带板块快照（证据块逐条 [E#]），模型可回链
    assert any("板块「交通运输」" in blob and "[E" in blob for blob in calls)


def test_direction_evidence_sector_layer_plugin_disabled(tmp_path, monkeypatch):
    """cn-market-data 被禁用（启动时自动安装，先显式禁用走真实路径）→ 板块层如实进 unavailable，日历证据仍在。"""
    test_client, headers = _direction_client(tmp_path, monkeypatch)
    _setup_profile(test_client, headers)
    _stub_model(monkeypatch)
    assert (
        test_client.post("/plugins/official.cn-market-data/disable", headers=headers).status_code
        == 200
    )

    body = test_client.post(
        "/evidence/direction-research",
        json={"topic": "海运板块", "with_counter_check": False},
        headers=headers,
    ).json()
    assert body["ok"] is True
    assert body["research_mode"] == "evidence"  # 日历层兜底
    assert "板块行情层未启用" in (body["evidence_unavailable"] or "")
    assert not any(item["source_type"] == "sector_market" for item in body["evidence_snapshot"])


def test_direction_evidence_sector_layer_fetch_failure(tmp_path, monkeypatch):
    """板块清单取数失败 → 缺口如实声明（含失败原因），其余证据层不受拖累。"""
    from investment_steward_core.api import app as app_module
    from investment_steward_core.market_feed import FeedError

    test_client, headers = _direction_client(tmp_path, monkeypatch)
    _setup_profile(test_client, headers)
    _stub_model(monkeypatch)
    assert (
        test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code
        == 200
    )

    def _boom() -> list[dict[str, object]]:
        raise FeedError("上游断连")

    monkeypatch.setattr(app_module, "fetch_cn_sector_list", _boom)
    body = test_client.post(
        "/evidence/direction-research",
        json={"topic": "海运板块", "with_counter_check": False},
        headers=headers,
    ).json()
    assert body["ok"] is True
    assert "板块行情取数失败" in (body["evidence_unavailable"] or "")
    assert "上游断连" in body["evidence_unavailable"]
    assert not any(item["source_type"] == "sector_market" for item in body["evidence_snapshot"])


def test_direction_evidence_style_topic_declares_gap(tmp_path, monkeypatch):
    """风格主题「低估板块」未命中板块清单 → 无板块证据，缺口声明进入 unavailable_note。"""
    from investment_steward_core.api import app as app_module

    test_client, headers = _direction_client(tmp_path, monkeypatch)
    _setup_profile(test_client, headers)
    _stub_model(monkeypatch)
    assert (
        test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code
        == 200
    )
    monkeypatch.setattr(app_module, "fetch_cn_sector_list", lambda: _SECTOR_ROWS)

    body = test_client.post(
        "/evidence/direction-research",
        json={"topic": "低估板块", "with_counter_check": False},
        headers=headers,
    ).json()
    assert body["ok"] is True
    assert "未命中行业板块清单" in (body["evidence_unavailable"] or "")
    assert not any(item["source_type"] == "sector_market" for item in body["evidence_snapshot"])

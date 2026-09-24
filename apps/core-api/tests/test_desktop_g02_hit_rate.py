"""G02（桌面端升级路线图 2026-09-18）：后验结果进复盘与学习（命中率）。

冻结口径（改动前先读 `longterm.JUDGMENT_HIT_RATE_POLICY`，那是随返回体一起下发的同一份文案）：
1. **含分母**：命中率 = 成立 /（成立 + 失效）；「无数据」与未回填不进分母但如实计数；
2. **样本不足不冒充 0%**：分母为 0 时 `hit_rate` 必须是 `None` 并给出说明；
3. **可展开**：桶内直接带成员判定原文与最近一次回填记录 —— 数字必须能追溯到判定；
4. **分桶口径不猜**：模型方案取来源研报 `model`，无来源研报归入 `HIT_RATE_MODEL_UNKNOWN`；
   主题优先取研报标题、否则取判断对象，并以 `theme_source` 标注取值来源；
5. **只读**：命中率与投资原则之间没有自动通道（原则变更仍走显式确认链），本端点无写方法；
6. **路由顺序**：`/judgments/hit-rate` 必须注册在参数化路由 `/judgments/{judgment_id}` **之前**，
   否则会被当成 judgment_id 解析并返回 422（项目铁律：精确路由先于参数化路由）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用

from test_stock_research_tools import (  # noqa: F401
    _file_client,
    _mock_sources,
    _setup_active_profile,
)

from investment_steward_core import longterm as longterm_service


def _report_json(**extra) -> str:
    base = {
        "title": "丙股：命中率样本",
        "executive_summary": "结论：技术面缓涨，基本面稳健。",
        "report": (
            "### 技术面\n股价沿 MA20 缓涨上行，量能温和放大，结构完好 [S1]\n"
            "### 消息面\n公司公告中标大单，市场关注度明显上升 [S2]\n"
            "### 基本面\n营收净利稳健增长，经营现金流为正 [S4]\n"
            "### 综合判断\n趋势向好但估值已不便宜，总体中性看待，跟踪量能确认。"
        ),
        "citations": ["S1", "S2", "S4"],
        "confidence": "medium",
        "valuation": {"verdict": "合理"},
    }
    base.update(extra)
    return json.dumps(base, ensure_ascii=False)


def _two_call_model(report: str):
    calls: list[dict] = []

    def _fake(base_url, json_payload, api_key, timeout):
        calls.append(json_payload)
        return {"choices": [{"message": {"content": report}}]}

    return _fake


def _iso(days_from_now: int) -> str:
    return (datetime.now(UTC) + timedelta(days=days_from_now)).strftime("%Y-%m-%dT%H:%M:%S")


def _create_judgment(test_client, headers, *, subject: str, statement: str, due_days: int) -> str:
    created = test_client.post(
        "/judgments",
        json={
            "subject": subject,
            "direction": "看多",
            "statement": statement,
            "due_at": _iso(due_days),
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    return str(created.json()["judgment_id"])


def _refill(test_client, headers, judgment_id: str, result: str, outcome: str) -> None:
    response = test_client.post(
        f"/judgments/{judgment_id}/verify",
        json={"result": result, "outcome": outcome},
        headers=headers,
    )
    assert response.status_code == 200, response.text


def test_hit_rate_route_is_registered_before_parameterized_route(tmp_path, monkeypatch):
    """★ 路由顺序守护：精确路由在前，否则 `/judgments/hit-rate` 会被当作 judgment_id 解析成 422。"""
    test_client, headers = _file_client(tmp_path, monkeypatch)
    paths = [getattr(route, "path", "") for route in test_client.app.routes]
    assert paths.index("/judgments/hit-rate") < paths.index("/judgments/{judgment_id}")
    assert test_client.get("/judgments/hit-rate", headers=headers).status_code == 200
    assert test_client.get("/judgments/hit-rate/nope", headers=headers).status_code in {404, 422}


def test_hit_rate_rejects_unknown_window(tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    response = test_client.get("/judgments/hit-rate?window=7d", headers=headers)
    assert response.status_code == 422
    assert "非法时间窗" in response.json()["detail"]


def test_hit_rate_is_read_only_no_write_methods_on_that_path(tmp_path, monkeypatch):
    """命中率只读：不存在 POST/PUT/PATCH/DELETE 的 /judgments/hit-rate，不新增静默改原则路径。"""
    test_client, headers = _file_client(tmp_path, monkeypatch)
    for method in ("post", "put", "patch", "delete"):
        response = test_client.request(method.upper(), "/judgments/hit-rate", json={}, headers=headers)
        assert response.status_code == 405, f"{method.upper()} 不该存在"


def test_hit_rate_counts_only_conclusive_refills_and_excludes_insufficient_data(tmp_path, monkeypatch):
    """★ 主例：同一主题下成立 1 / 失效 1 / 无数据 1 → 命中率 0.5 且分母 2（无数据不进分母）。"""
    test_client, headers = _file_client(tmp_path, monkeypatch)
    verified_id = _create_judgment(test_client, headers, subject="600010", statement="站上 MA20", due_days=-5)
    refuted_id = _create_judgment(test_client, headers, subject="600010", statement="跌破 MA20", due_days=-4)
    no_data_id = _create_judgment(test_client, headers, subject="600010", statement="量能放大", due_days=-3)
    _refill(test_client, headers, verified_id, "verified", "回踩不破")
    _refill(test_client, headers, refuted_id, "refuted", "跌破且缩量")
    _refill(test_client, headers, no_data_id, "insufficient_data", "数据缺失")

    body = test_client.get("/judgments/hit-rate?window=30d", headers=headers).json()
    totals = body["totals"]
    assert totals["judgments"] == 3
    assert (totals["verified"], totals["refuted"], totals["insufficient_data"], totals["pending"]) == (1, 1, 1, 0)
    assert totals["denominator"] == 2
    assert totals["hit_rate"] == 0.5
    # 口径随返回体下发，逐条可核。
    assert body["policy"] == longterm_service.JUDGMENT_HIT_RATE_POLICY

    # 三条手工判断都没有来源研报 → 模型方案如实标「未标注」，主题取判断对象并标注来源。
    assert len(body["buckets"]) == 1
    bucket = body["buckets"][0]
    assert bucket["model"] == longterm_service.HIT_RATE_MODEL_UNKNOWN
    assert bucket["theme"] == "600010" and bucket["theme_source"] == "judgment_subject"
    assert bucket["judgment_count"] == 3
    assert bucket["hit_rate"] == 0.5 and bucket["hit_rate_denominator"] == 2
    # 可展开：成员判定带原文与最近一次回填记录。
    statements = {item["statement"] for item in bucket["judgments"]}
    assert statements == {"站上 MA20", "跌破 MA20", "量能放大"}
    refilled = next(item for item in bucket["judgments"] if item["judgment_id"] == refuted_id)
    assert refilled["status"] == "refuted"
    assert refilled["verification"]["outcome"] == "跌破且缩量"


def test_hit_rate_separates_buckets_by_subject_for_manual_judgments(tmp_path, monkeypatch):
    """手工判断没有来源研报，主题就是判断对象 → 不同对象各自成桶（不跨对象混算命中率）。"""
    test_client, headers = _file_client(tmp_path, monkeypatch)
    a = _create_judgment(test_client, headers, subject="600020", statement="A 成立", due_days=-2)
    b = _create_judgment(test_client, headers, subject="600021", statement="B 无数据", due_days=-1)
    _refill(test_client, headers, a, "verified", "成立")
    _refill(test_client, headers, b, "insufficient_data", "数据缺失")

    body = test_client.get("/judgments/hit-rate", headers=headers).json()
    by_theme = {bucket["theme"]: bucket for bucket in body["buckets"]}
    assert set(by_theme) == {"600020", "600021"}
    assert by_theme["600020"]["hit_rate"] == 1.0
    assert by_theme["600021"]["hit_rate"] is None  # 无数据不进分母 → 该桶分母 0
    assert body["totals"]["denominator"] == 1 and body["totals"]["hit_rate"] == 1.0


def test_hit_rate_uses_latest_refill_result(tmp_path, monkeypatch):
    """同一判定回填两次时取**最近一次**（与 verify 的追加语义一致），分母不重复计数。"""
    test_client, headers = _file_client(tmp_path, monkeypatch)
    judgment_id = _create_judgment(test_client, headers, subject="600013", statement="回填两次", due_days=-2)
    _refill(test_client, headers, judgment_id, "verified", "第一次成立")
    _refill(test_client, headers, judgment_id, "refuted", "第二次被否定")

    body = test_client.get("/judgments/hit-rate", headers=headers).json()
    assert body["totals"]["judgments"] == 1
    assert (body["totals"]["verified"], body["totals"]["refuted"]) == (0, 1)
    assert body["totals"]["hit_rate"] == 0.0
    member = body["buckets"][0]["judgments"][0]
    assert member["status"] == "refuted"
    assert member["verification"]["outcome"] == "第二次被否定"


def test_hit_rate_is_none_when_denominator_is_zero(tmp_path, monkeypatch):
    """★ 样本不足不冒充 0%：分母 0 时 hit_rate 必须为 None 并带说明。"""
    test_client, headers = _file_client(tmp_path, monkeypatch)
    _create_judgment(test_client, headers, subject="600014", statement="还没到回填", due_days=-1)

    body = test_client.get("/judgments/hit-rate", headers=headers).json()
    assert body["totals"]["denominator"] == 0
    assert body["totals"]["hit_rate"] is None
    assert body["buckets"][0]["hit_rate"] is None
    assert "分母 0" in body["buckets"][0]["hit_rate_note"]


def test_hit_rate_window_filters_by_due_date_and_excludes_not_due(tmp_path, monkeypatch):
    """时间窗按**到期时间**筛选：90 天外的到期判定不进 90d 窗，未到期的单列不参与后验。"""
    test_client, headers = _file_client(tmp_path, monkeypatch)
    old_id = _create_judgment(test_client, headers, subject="600015", statement="很久以前到期", due_days=-120)
    recent_id = _create_judgment(test_client, headers, subject="600016", statement="刚到期", due_days=-2)
    _create_judgment(test_client, headers, subject="600017", statement="还没到期", due_days=10)
    _refill(test_client, headers, old_id, "verified", "成立")
    _refill(test_client, headers, recent_id, "refuted", "失效")

    windowed = test_client.get("/judgments/hit-rate?window=90d", headers=headers).json()
    assert windowed["window_days"] == 90
    assert windowed["totals"]["judgments"] == 1
    assert windowed["totals"]["verified"] == 0 and windowed["totals"]["refuted"] == 1
    # 未到期的不算「该回填没回填」，单列告知。
    assert windowed["totals"]["not_due_excluded"] == 1

    everything = test_client.get("/judgments/hit-rate?window=all", headers=headers).json()
    assert everything["window_days"] is None
    assert everything["totals"]["judgments"] == 2
    assert everything["totals"]["hit_rate"] == 0.5
    assert everything["totals"]["not_due_excluded"] == 1


def test_hit_rate_buckets_by_report_model_and_title_for_materialized_watchpoints(tmp_path, monkeypatch):
    """来源研报派生的判定：模型方案取研报 `model`、主题取研报标题（theme_source=report_title）。"""
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    report = _report_json(watchpoints=[
        {"signal": "站稳 MA20 后放量", "verify_by": _iso(-6)[:10], "expected_if_true": "倾向乐观"},
    ])
    monkeypatch.setattr(mc, "_post_json", _two_call_model(report))
    assert test_client.post(
        "/evidence/stock-research-report", json={"symbol": "600018"}, headers=headers
    ).json()["ok"] is True

    # G01 读路径把带日期的验证点落成判断。
    queue = test_client.get("/ai-research/review-queue", headers=headers).json()
    judgment_id = str(queue["items"][0]["judgment_id"])
    _refill(test_client, headers, judgment_id, "verified", "确实站上并放量")

    stored = test_client.app.state.core.database.get_ai_research_report(str(queue["items"][0]["report_id"]))
    assert stored is not None
    body = test_client.get("/judgments/hit-rate?window=90d", headers=headers).json()
    bucket = body["buckets"][0]
    assert bucket["model"] == stored["model"]
    assert bucket["theme"] == stored["title"]
    assert bucket["theme_source"] == "report_title"
    assert bucket["hit_rate"] == 1.0 and bucket["hit_rate_denominator"] == 1
    member = bucket["judgments"][0]
    assert member["source_report_id"] == str(queue["items"][0]["report_id"])
    assert member["statement"] == "站稳 MA20 后放量"


def test_hit_rate_survives_deleted_source_report(tmp_path, monkeypatch):
    """来源研报被删除后：模型方案如实标「未标注」、主题退化为判断对象，不抛异常、不猜模型。"""
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    report = _report_json(watchpoints=[
        {"signal": "删除来源研报后的判定", "verify_by": _iso(-6)[:10], "expected_if_true": "倾向乐观"},
    ])
    monkeypatch.setattr(mc, "_post_json", _two_call_model(report))
    assert test_client.post(
        "/evidence/stock-research-report", json={"symbol": "600019"}, headers=headers
    ).json()["ok"] is True
    queue = test_client.get("/ai-research/review-queue", headers=headers).json()
    report_id = str(queue["items"][0]["report_id"])
    judgment_id = str(queue["items"][0]["judgment_id"])
    _refill(test_client, headers, judgment_id, "refuted", "不成立")
    assert test_client.delete(f"/ai-research/reports/{report_id}", headers=headers).status_code == 200

    body = test_client.get("/judgments/hit-rate", headers=headers).json()
    bucket = body["buckets"][0]
    assert bucket["model"] == longterm_service.HIT_RATE_MODEL_UNKNOWN
    assert bucket["theme_source"] == "judgment_subject"
    assert body["totals"]["hit_rate"] == 0.0

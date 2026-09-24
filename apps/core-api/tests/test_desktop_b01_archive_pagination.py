"""B01（桌面端升级路线图 2026-09-18）：档案筛选/分页/计数下沉 SQL 的端点级测试。

覆盖：model/q/is_draft/kind+collab/generated_from 服务端过滤、count_only、轻量投影、
全局 facets 真值计数（direction/stock/collab/draft_stock）与模型清单。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings

TOKEN = "b01-archive-token"


def _client(tmp_path):
    app = create_app(CoreSettings(session_token=TOKEN, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": TOKEN}


def _seed(database) -> None:
    """6 份档案：2 方向 + 2 个股正式 + 1 个股草稿 + 1 个股协同流水线，模型与标题各不相同。"""
    start = datetime(2026, 8, 1, tzinfo=UTC)
    records = [
        {"report_id": "dir-1", "kind": "direction", "subject": "半导体设备", "title": "半导体方向研判", "model": "model-a"},
        {"report_id": "dir-2", "kind": "direction", "subject": "有色金属", "title": "有色方向研判", "model": "model-b"},
        {"report_id": "stock-1", "kind": "stock", "subject": "002468", "title": "002468 深研", "model": "model-a", "is_draft": False},
        {"report_id": "stock-2", "kind": "stock", "subject": "000060", "title": "000060 深研", "model": "model-b", "is_draft": True},
        {"report_id": "collab-1", "kind": "stock", "subject": "600127", "title": "协同报告", "model": "model-a·协同流水线"},
        {"report_id": "stock-3", "kind": "stock", "subject": "601857", "title": "旧档案无草稿键", "model": "model-c"},
    ]
    for index, record in enumerate(records):
        record = {**record, "generated_at": (start + timedelta(days=index)).isoformat()}
        database.insert_ai_research_report(record)


def test_filters_model_q_draft(tmp_path):
    http, headers = _client(tmp_path)
    _seed(http.app.state.core.database)

    by_model = http.get("/ai-research/reports", params={"model": "model-a"}, headers=headers).json()
    # 精确匹配：与旧前端 `item.model !== historyModel` 同口径，`model-a·协同流水线` 是另一个模型串。
    assert {item["report_id"] for item in by_model["items"]} == {"dir-1", "stock-1"}
    assert by_model["total"] == 2

    # q 命中 title（旧前端 haystack 是 subject+title+topic+model）。
    by_title = http.get("/ai-research/reports", params={"q": "深研"}, headers=headers).json()
    assert {item["report_id"] for item in by_title["items"]} == {"stock-1", "stock-2"}

    by_subject = http.get("/ai-research/reports", params={"q": "000060"}, headers=headers).json()
    assert {item["report_id"] for item in by_subject["items"]} == {"stock-2"}

    drafts = http.get("/ai-research/reports", params={"is_draft": True}, headers=headers).json()
    assert {item["report_id"] for item in drafts["items"]} == {"stock-2"}, "缺 is_draft 键的旧档案不算草稿"

    formal = http.get("/ai-research/reports", params={"is_draft": False}, headers=headers).json()
    assert {item["report_id"] for item in formal["items"]} == {"dir-1", "dir-2", "stock-1", "collab-1", "stock-3"}


def test_kind_collab_filters_and_generated_range(tmp_path):
    http, headers = _client(tmp_path)
    _seed(http.app.state.core.database)

    collab = http.get("/ai-research/reports", params={"kind": "stock", "collab": True}, headers=headers).json()
    assert {item["report_id"] for item in collab["items"]} == {"collab-1"}

    plain_stock = http.get("/ai-research/reports", params={"kind": "stock", "collab": False}, headers=headers).json()
    assert {item["report_id"] for item in plain_stock["items"]} == {"stock-1", "stock-2", "stock-3"}

    ranged = http.get(
        "/ai-research/reports",
        params={"generated_from": "2026-08-03T00:00:00+00:00", "generated_to": "2026-08-05T23:59:59+00:00"},
        headers=headers,
    ).json()
    assert {item["report_id"] for item in ranged["items"]} == {"stock-1", "stock-2", "collab-1"}


def test_depth_below_min_filter(tmp_path):
    """J06「未达档位深度」：正文低于档位下限的档案可经 depth=below_min 服务端筛出。"""
    http, headers = _client(tmp_path)
    start = datetime(2026, 8, 1, tzinfo=UTC)
    database = http.app.state.core.database
    records = [
        # 深研版正文 1839 字 < 下限 3500 → 命中。
        {"report_id": "deep-thin", "kind": "stock", "subject": "002468", "model": "m1",
         "report_mode": "deep", "report_chars": 1839, "report_mode_budget": {"min": 3500, "max": 6000}},
        # 标准版正文 3000 字 ≥ 下限 2000 → 不命中。
        {"report_id": "std-ok", "kind": "stock", "subject": "000060", "model": "m1",
         "report_mode": "standard", "report_chars": 3000, "report_mode_budget": {"min": 2000, "max": 3500}},
        # 无 report_mode_budget 的旧档案 → 不命中（不误伤）。
        {"report_id": "legacy", "kind": "stock", "subject": "601857", "model": "m1"},
    ]
    for index, record in enumerate(records):
        database.insert_ai_research_report({**record, "generated_at": (start + timedelta(days=index)).isoformat()})

    shallow = http.get("/ai-research/reports", params={"depth": "below_min"}, headers=headers).json()
    assert {item["report_id"] for item in shallow["items"]} == {"deep-thin"}
    assert shallow["total"] == 1
    bad = http.get("/ai-research/reports", params={"depth": "nonsense"}, headers=headers)
    assert bad.status_code == 422


def test_count_only_and_light_projection(tmp_path):
    http, headers = _client(tmp_path)
    _seed(http.app.state.core.database)

    counts = http.get("/ai-research/reports", params={"count_only": True}, headers=headers).json()
    assert counts["items"] == []
    assert counts["total"] == 6
    assert counts["counts"] == {"total": 6, "direction": 2, "stock": 3, "collab": 1, "draft_stock": 1}
    assert counts["models"] == ["model-a", "model-a·协同流水线", "model-b", "model-c"]

    listed = http.get("/ai-research/reports", headers=headers).json()
    assert all(
        set(item) == {"report_id", "kind", "subject", "generated_at", "model", "is_draft", "title", "topic", "symbol"}
        for item in listed["items"]
    ), "轻量投影字段集合固定，不带 payload"
    draft_flag = {item["report_id"]: item["is_draft"] for item in listed["items"]}
    assert draft_flag["stock-2"] is True, "is_draft 序列化回 true/false，不是 SQLite 的 1/0"
    assert draft_flag["stock-3"] is None

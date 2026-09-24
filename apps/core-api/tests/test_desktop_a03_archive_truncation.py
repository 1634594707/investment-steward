"""A03（桌面端升级路线图 2026-09-18）：研报档案「看不全」缺陷的验收对照物。

2026-09-19 B01 落地后按本文件头部原指令改写：原三例锁定的是缺陷现状（列表静默截断到 50、
响应不带总数、第 51 份任何一次列表请求都取不到）；现在断言翻转为「可翻页 + 计数为真值」，
并保留详情端点直读与 limit 上限两条边界，防止回退。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings

TOKEN = "a03-archive-token"
SEEDED = 55


def _client(tmp_path):
    app = create_app(CoreSettings(session_token=TOKEN, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": TOKEN}


def _seed(database, count: int, offset: int = 0) -> list[str]:
    """写入 count 份档案（id 从 offset 起，避免与既有种子冲突），generated_at 逐日递增。"""
    start = datetime(2026, 7, 1, tzinfo=UTC)
    report_ids = []
    for index in range(offset, offset + count):
        stamp = start + timedelta(days=index)
        record = {
            "report_id": f"report-{index:03d}",
            "kind": "stock",
            "subject": f"002468 第 {index + 1} 份",
            "generated_at": stamp.isoformat(),
            "confidence": "medium",
            "model": "test-model",
        }
        database.insert_ai_research_report(record)
        report_ids.append(record["report_id"])
    return report_ids


def test_list_supports_paging_with_true_counts(tmp_path):
    """B01 落地后：默认 50 条/页，但 total 为真值，offset 可翻到最旧一份。"""
    http, headers = _client(tmp_path)
    _seed(http.app.state.core.database, SEEDED)

    listed = http.get("/ai-research/reports", headers=headers).json()
    assert listed["ok"] is True
    assert len(listed["items"]) == 50, "默认 limit=50 一页"
    assert listed["total"] == SEEDED, "页签计数必须是 COUNT(*) 真值，不是 items 长度"
    assert listed["counts"]["total"] == SEEDED
    assert listed["items"][0]["report_id"] == f"report-{SEEDED - 1:03d}", "列表按生成时间倒序"
    # 轻量投影：列表项不带 payload（首屏传输量随此下降），全文走详情端点。
    assert all("payload" not in item for item in listed["items"])

    last_page = http.get(
        "/ai-research/reports", params={"offset": 50}, headers=headers
    ).json()
    assert {item["report_id"] for item in last_page["items"]} == {
        "report-000",
        "report-001",
        "report-002",
        "report-003",
        "report-004",
    }, "第 51 份起经 offset 翻页可达"
    assert last_page["total"] == SEEDED


def test_truncated_report_is_still_readable_by_id(tmp_path):
    """详情端点无 limit，直读 id 仍能取到（翻页之外的兜底路径，保持不变）。"""
    http, headers = _client(tmp_path)
    report_ids = _seed(http.app.state.core.database, SEEDED)

    detail = http.get(f"/ai-research/reports/{report_ids[0]}", headers=headers).json()
    assert detail["ok"] is True and detail["item"]["report_id"] == report_ids[0]
    assert detail["item"]["subject"] == "002468 第 1 份"


def test_max_limit_200_is_a_hard_ceiling_but_paging_reaches_everything(tmp_path):
    """单页上限仍是 200（A02 基线口径不回退），但任何一份都可经翻页取到。"""
    http, headers = _client(tmp_path)
    oldest = _seed(http.app.state.core.database, 55)
    _seed(http.app.state.core.database, 200, offset=55)

    capped = http.get(
        "/ai-research/reports", params={"limit": 10_000}, headers=headers
    ).json()
    assert len(capped["items"]) == 200, "limit 仍被 clamp 到 200"
    assert capped["total"] == 255

    tail = http.get(
        "/ai-research/reports", params={"limit": 200, "offset": 200}, headers=headers
    ).json()
    assert len(tail["items"]) == 55
    assert {item["report_id"] for item in tail["items"]} == set(oldest), "最旧 55 份翻页可达"

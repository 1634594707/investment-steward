"""M5（2026-09-15 第二轮路线图）F01–F03：删除自选不必写原因。

F01：自选移除不要求原因（前端不再渲染说明框）。
F02：实现口径**写死一种**——自选与持仓两条路径都**不强制**原因；持仓保留说明框但改为选填。
     前端由 `ConfirmDialog` 的 `noteOptional` 控制（见 `ConfirmDialog.m5.test.tsx`）。
F03：删除仍写 `holding.deleted`；用户没写说明时审计摘要用固定句兜底，不写空原因。
"""

from __future__ import annotations


def _create(http, headers, *, status: str, label: str, instrument: str):
    response = http.post(
        "/holdings",
        json={"instrument": instrument, "label": label, "status": status, "strategy_note": ""},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _deleted_event(http, headers) -> dict:
    events = http.get("/audit", headers=headers).json()
    matched = [event for event in events if event["action"] == "holding.deleted"]
    assert matched, "未写入 holding.deleted 审计事件"
    return matched[-1]


def test_f01_watchlist_delete_without_note_succeeds(client):
    http, headers = client
    holding = _create(http, headers, status="watchlist", label="东瑞股份", instrument="001201")

    response = http.delete(f"/holdings/{holding['holding_id']}", headers=headers)

    assert response.status_code == 204
    assert http.get("/holdings", headers=headers).json() == []


def test_f03_watchlist_without_note_uses_fixed_audit_sentence(client):
    http, headers = client
    holding = _create(http, headers, status="watchlist", label="东瑞股份", instrument="001201")

    http.delete(f"/holdings/{holding['holding_id']}", headers=headers)

    payload = _deleted_event(http, headers)["payload"]
    assert payload["summary"] == "用户确认移除自选"
    assert payload["note_provided"] is False
    assert payload["kind"] == "watchlist"
    assert payload["instrument"] == "001201"
    assert payload["label"] == "东瑞股份"


def test_f03_watchlist_with_note_keeps_user_note(client):
    http, headers = client
    holding = _create(http, headers, status="watchlist", label="东瑞股份", instrument="001201")

    response = http.delete(
        f"/holdings/{holding['holding_id']}",
        params={"note": "不再跟踪猪价周期"},
        headers=headers,
    )

    assert response.status_code == 204
    payload = _deleted_event(http, headers)["payload"]
    assert payload["summary"] == "不再跟踪猪价周期"
    assert payload["note_provided"] is True


def test_f02_holding_delete_without_note_is_optional(client):
    """F02：持仓删除保留说明框但**不强制**——空说明照常删除成功。"""
    http, headers = client
    holding = _create(http, headers, status="holding", label="贵州茅台", instrument="600519")

    response = http.delete(f"/holdings/{holding['holding_id']}", headers=headers)

    assert response.status_code == 204
    payload = _deleted_event(http, headers)["payload"]
    assert payload["summary"] == "用户确认移除持仓"
    assert payload["kind"] == "holding"


def test_f02_holding_delete_with_note_keeps_user_note(client):
    http, headers = client
    holding = _create(http, headers, status="holding", label="贵州茅台", instrument="600519")

    http.delete(f"/holdings/{holding['holding_id']}", params={"note": "估值到位，落袋"}, headers=headers)

    payload = _deleted_event(http, headers)["payload"]
    assert payload["summary"] == "估值到位，落袋"
    assert payload["note_provided"] is True


def test_f03_audit_summary_never_empty(client):
    """F03 铁律：无论有没有用户说明，审计摘要都不允许是空串。"""
    http, headers = client
    for status in ("watchlist", "holding"):
        holding = _create(http, headers, status=status, label=f"标的{status}", instrument="001201")
        http.delete(f"/holdings/{holding['holding_id']}", headers=headers)
        payload = _deleted_event(http, headers)["payload"]
        assert payload["summary"].strip(), f"{status} 的审计摘要为空"

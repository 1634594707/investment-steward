"""U03/U05（桌面端升级路线图 2026-09-18）：游资档案截断声明与课程单一数据源的端点级测试。

- U03：席位档案取满一页（50 条）时端点必须返回 `possibly_truncated=true`（数据源不回 total，
  只能如实声明「可能还有更早记录」）；未满一页时为 false 且 returned 为真值。
- U05：`/youzi/curriculum` 读插件目录的 curriculum.json（与 /youzi/watchlist 同一套路径解析），
  返回带 version/updated_at/principle/acceptance 的完整课程。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from investment_steward_core.config import CoreSettings

TOKEN = "u03-u05-youzi-token"
PLUGIN_ID = "official.youzi-radar"
REPO_ROOT = Path(__file__).resolve().parents[3]


def _client(tmp_path):
    from investment_steward_core.api import create_app

    app = create_app(CoreSettings(session_token=TOKEN, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": TOKEN}


def _install_youzi(http, headers) -> None:
    assert http.post(f"/plugins/{PLUGIN_ID}/install", headers=headers).status_code == 200


def test_profile_truncation_declaration_full_page(tmp_path, monkeypatch):
    """取满 50 条 → possibly_truncated=true + page_size=50（U03 验收：取满一页必出说明）。"""
    http, headers = _client(tmp_path)
    _install_youzi(http, headers)
    from investment_steward_core import lhb_feed

    monkeypatch.setattr(
        lhb_feed,
        "fetch_operatedept_history",
        lambda code, *, page_size=50: [
            {"TRADE_DATE": "2026-09-18", "SECURITY_CODE": "600127", "SECURITY_NAME_ABBR": "中金岭南",
             "ACT_BUY": 1.0, "ACT_SELL": None, "NET_AMT": 1.0, "EXPLANATION": "日涨幅偏离值达7%"}
            for _ in range(page_size)
        ],
    )
    body = http.get("/youzi/profile/10025390", headers=headers).json()
    assert body["available"] is True
    assert body["returned"] == 50
    assert body["page_size"] == 50
    assert body["possibly_truncated"] is True, "取满一页必须声明可能截断（静默截断是 B01/A03 同类缺陷）"


def test_profile_truncation_declaration_partial_page(tmp_path, monkeypatch):
    """未满一页 → possibly_truncated=false（U03 验收：不得常驻一行噪声）。"""
    http, headers = _client(tmp_path)
    _install_youzi(http, headers)
    from investment_steward_core import lhb_feed

    monkeypatch.setattr(
        lhb_feed,
        "fetch_operatedept_history",
        lambda code, *, page_size=50: [
            {"TRADE_DATE": "2026-09-18", "SECURITY_CODE": "600127", "SECURITY_NAME_ABBR": "中金岭南",
             "ACT_BUY": 1.0, "ACT_SELL": None, "NET_AMT": 1.0, "EXPLANATION": "日涨幅偏离值达7%"}
        ],
    )
    body = http.get("/youzi/profile/10025390", headers=headers).json()
    assert body["returned"] == 1
    assert body["page_size"] == 50
    assert body["possibly_truncated"] is False


def test_curriculum_endpoint_serves_plugin_json(tmp_path):
    """U05：端点从插件目录读 curriculum.json（仓库回退路径），前端不再手抄副本。"""
    http, headers = _client(tmp_path)
    _install_youzi(http, headers)
    body = http.get("/youzi/curriculum", headers=headers).json()
    assert body["ok"] is True
    curriculum = body["curriculum"]
    assert curriculum["version"] == "2026-09-18-v2"
    assert len(curriculum["lessons"]) == 11
    # U05 的核心缺口字段：principle（这课的道理）与 acceptance（怎么算学会）必须随端点下发。
    for lesson in curriculum["lessons"]:
        assert lesson.get("principle"), f"{lesson['level']} 缺 principle"
        assert lesson.get("acceptance"), f"{lesson['level']} 缺 acceptance"
    # 与磁盘上的真实文件同源（不是测试夹具）。
    on_disk = json.loads(
        (REPO_ROOT / "plugins" / "official" / "youzi-radar" / "curriculum.json").read_text(encoding="utf-8")
    )
    assert curriculum["version"] == on_disk["version"]

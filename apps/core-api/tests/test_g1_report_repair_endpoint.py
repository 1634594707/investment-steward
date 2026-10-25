"""G1（研报产出与交付链路路线图 2026-09-27）：定向修复端点的端点级回归。

**要解决的问题**：闸门「发现得准、修不了」——摘要过短（<150 字）、一句话结论缺失只写进
「质量校验痕迹」，报告照发；界面上没有任何「补写摘要 / 补写结论 / 重新生成本节」的入口，
用户只能重新发起一次完整生成（又一次付费调用 + 一次等待），而系统已经精确知道缺什么。

本文件锁定 `POST /ai-research/reports/{report_id}/repair` 的对外契约：

1. 三条低成本策略真的能修好对应的缺陷，且**正文一个字都不动**（除 `section_only`）；
2. 修好后 `report_revision` +1、`revision_history` 追加一条带 `origin="manual"` 的留痕、
   报告**原地更新**（回读 GET 能拿到新结论 / 新摘要）；
3. 修不好（输出为空 / 引用校验未过）→ `repaired=false`、**报告逐字节保持原状**，
   不重试、不伪造成功、不静默截断；
4. 不可执行的请求（`full_rerun` / 非个股 / 无使用中方案 / 出网关闭 / 报告不存在）
   如实拒绝并写清 `stage`，**不发起任何模型调用**。

模型全部打桩（`model_client._post_json`），测试绝不联网、不花钱。
"""

from __future__ import annotations

import json

import pytest
from conftest import (  # noqa: F401
    SAMPLE_CONCLUSION,
    SAMPLE_SUMMARY_IN_BAND,
    client as client_fixture,
    run_stock_report,
)
from test_v31_repair_and_comparison import (  # noqa: F401  复用固定证据快照与样本
    _CannedModel,
    _good_payload,
    _good_report_text,
    _setup,
)

SHORT_SUMMARY = "结论偏多：缓涨趋势完好，量价配合健康。"  # 远低于目标区间下界 150 字


def _generate(
    client, tmp_path, monkeypatch, payload: dict, *, symbol: str = "600001", extra: list[str] | None = None
) -> tuple:
    """生成一份**未自动修复**的报告（`with_auto_repair=False`），返回 (canned, client, headers, body)。

    关掉自动修复是为了让「初稿确实缺这一项」稳定成立——否则生成路径自己就修好了，
    端点级用例就测不到手动修复这条路。`extra` 预排后续修复调用的桩响应（同一 FIFO 桩）。
    """
    canned = _CannedModel(monkeypatch, [json.dumps(payload, ensure_ascii=False), *(extra or [])])
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    body = run_stock_report(
        test_client,
        headers,
        **{"symbol": symbol, "with_counter_check": False, "with_auto_repair": False},
    )
    assert body["ok"] is True, body
    assert body["report_id"], "报告必须落库，定向修复需要 report_id"
    return canned, test_client, headers, body


def _repair(test_client, headers, report_id: str, strategy: str, section: str = "") -> dict:
    response = test_client.post(
        f"/ai-research/reports/{report_id}/repair",
        json={"strategy": strategy, "section": section},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 三条低成本策略：真的能修好
# ---------------------------------------------------------------------------

def test_repair_conclusion_only_fills_first_screen_card(client, tmp_path, monkeypatch):
    """只缺一句话结论 → 单次定向补写；正文不动、修订升版、首屏结论卡不再为空。

    这是 G1 的验收本体：此前 `conclusion.statement` 缺失只落一条 warning 放过，
    首屏结论卡（整份报告信息密度最高的一行）永远是空的。
    """
    payload = _good_payload()
    payload.pop("conclusion")  # 模拟模型没输出 conclusion
    canned, test_client, headers, body = _generate(
        client, tmp_path, monkeypatch, payload,
        extra=[json.dumps(SAMPLE_CONCLUSION, ensure_ascii=False)],
    )
    assert not body["conclusion"]["statement"], "初稿确实没有结论（自动修复已关闭）"
    assert any("conclusion.statement" in warning for warning in body["quality_warnings"])
    assert canned.call_count == 1, "关闭自动修复后，初稿只应有一次模型调用"

    result = _repair(test_client, headers, body["report_id"], "conclusion_only")

    assert result["ok"] is True
    assert result["repair"]["strategy"] == "conclusion_only"
    assert result["repair"]["label"] == "只补写一句话结论"
    assert result["repair"]["repaired"] is True
    item = result["item"]
    assert item["conclusion"]["statement"] == SAMPLE_CONCLUSION["statement"]
    assert item["conclusion"]["direction"] == "偏多"
    assert item["report"] == _good_report_text(), "补写结论绝不改写正文"
    assert item["report_revision"] == 2
    entry = item["revision_history"][-1]
    assert entry["strategy"] == "conclusion_only"
    assert entry["origin"] == "manual", "手动修复必须在留痕里与自动修复区分开"
    assert "一句话结论" in entry["reason"]
    # 首屏结论卡随新结论重建（否则修好了但首屏仍空）。
    assert item["decision_card"]["conclusion"]["statement"] == SAMPLE_CONCLUSION["statement"]
    # 重跑同一闸门 → 「未输出一句话结论」告警消失。
    assert not any("conclusion.statement" in warning for warning in item["quality_warnings"])
    assert canned.call_count == 2, "定向补写只花一次调用（初稿 1 次 + 补写 1 次）"

    # 原地更新：回读 GET 拿到的是修好后的报告，不是内存里的临时副本。
    persisted = test_client.get(f"/ai-research/reports/{body['report_id']}", headers=headers).json()
    assert persisted["item"]["conclusion"]["statement"] == SAMPLE_CONCLUSION["statement"]
    assert persisted["item"]["report_revision"] == 2


def test_repair_summary_only_rewrites_below_target_summary(client, tmp_path, monkeypatch):
    """摘要低于目标区间 → 只重写摘要（G1 新增的下界分支），正文一个字都不动。

    生成路径的**前置**摘要重写（v30 D04 + G1 下界）无条件运行，所以这里让它返回空串
    （`apply` 不接受空输出、保留原文），才能造出「落库报告带着一份过短摘要」的状态——
    这正是用户点「重写执行摘要」时要修的东西。
    """
    payload = _good_payload()
    payload["executive_summary"] = SHORT_SUMMARY
    canned, test_client, headers, body = _generate(
        client, tmp_path, monkeypatch, payload,
        extra=["", SAMPLE_SUMMARY_IN_BAND],  # 第 2 个给前置重写（空 → 不接受），第 3 个给手动修复
    )
    assert body["executive_summary"] == SHORT_SUMMARY, "前置重写未被接受 → 落库的仍是过短摘要"
    assert any("低于目标区间" in warning for warning in body["quality_warnings"])

    result = _repair(test_client, headers, body["report_id"], "summary_only")

    assert result["ok"] is True
    assert result["repair"]["repaired"] is True
    assert canned.call_count == 3, "手动重写摘要 = 一次调用，绝不重跑正文"
    item = result["item"]
    assert item["executive_summary"] == SAMPLE_SUMMARY_IN_BAND
    assert item["report"] == _good_report_text(), "重写摘要绝不改写正文"
    assert item["report_revision"] == 2
    assert "低于目标区间" in item["revision_history"][-1]["reason"]
    assert not any("低于目标区间" in warning for warning in item["quality_warnings"])


def test_repair_section_only_regenerates_missing_section(client, tmp_path, monkeypatch):
    """缺一节 → 只补这一节；合并回正文、引用清单同步扩充、缺失项清零。"""
    payload = _good_payload()
    payload["report"] = payload["report"].replace(
        "### 基本面\n营收 10.00 亿同比增长，净利 1.20 亿同比增长，经营现金流 1.50 亿为正，利润质量健康；"
        "PE(TTM) 19.73，近5年 6.7% 分位，低于同业中位，估值偏便宜。[S4][S5]\n\n",
        "### 基本面\n见上文。\n\n",
    )
    payload["citations"] = ["S1", "S2", "S3"]
    canned, test_client, headers, body = _generate(
        client, tmp_path, monkeypatch, payload,
        extra=[json.dumps({
            "section": "基本面",
            "content": "### 基本面\n营收 10.00 亿同比增长，净利 1.20 亿同比增长，经营现金流 1.50 亿为正；"
                       "PE(TTM) 19.73，近5年 6.7% 分位，估值偏便宜。[S4][S5]",
        }, ensure_ascii=False)],
    )
    assert body["missing_sections"] == ["基本面"]

    result = _repair(test_client, headers, body["report_id"], "section_only", section="基本面")

    assert result["ok"] is True
    assert result["repair"]["repaired"] is True
    assert canned.call_count == 2, "只补一节 = 一次调用"
    item = result["item"]
    assert "营收 10.00 亿同比增长" in item["report"]
    assert item["missing_sections"] == []
    assert {"S4", "S5"} <= set(item["citations"]), "合并后引用清单必须纳入新小节里真实可用的引用"
    assert item["report_chars"] > body["report_chars"]


# ---------------------------------------------------------------------------
# 修不好 / 不可执行：如实拒绝，报告保持原状
# ---------------------------------------------------------------------------

def test_repair_unparsable_output_keeps_report_unchanged(client, tmp_path, monkeypatch):
    """补写输出无法解析 → `repaired=false` + 写清原因；报告逐字节不变、不升版、不留痕。"""
    payload = _good_payload()
    payload.pop("conclusion")
    canned, test_client, headers, body = _generate(
        client, tmp_path, monkeypatch, payload, extra=["{}"]
    )

    result = _repair(test_client, headers, body["report_id"], "conclusion_only")

    assert result["ok"] is True, "「没修成」不是接口错误，是一个如实的结果"
    assert result["repair"]["repaired"] is False
    assert "保留原状" in result["repair"]["detail"]
    assert result["repair"]["revision"] == 1
    assert canned.call_count == 2
    item = result["item"]
    assert not item["conclusion"]["statement"]
    assert item["report"] == _good_report_text()
    assert item["report_revision"] == 1
    assert item["revision_history"] == []
    persisted = test_client.get(f"/ai-research/reports/{body['report_id']}", headers=headers).json()
    assert persisted["item"]["report_revision"] == 1


def test_repair_rolls_back_when_citations_no_longer_valid(client, tmp_path, monkeypatch):
    """修复后引用校验未过 → 整条修复作废（无引用不发布），报告保持原状。

    正常生成的报告里 `citations ⊆ available_citations` 恒成立，所以这条是**防御性**用例：
    用一份引用了不存在来源的 payload 直接喂给端点，锁定「绝不引入虚构引用」这条硬边界。
    """
    from investment_steward_core.storage import database as db_module

    payload = _good_payload()
    payload.pop("conclusion")
    payload["available_citations"] = ["S1", "S2", "S3", "S4", "S5"]
    payload["citations"] = ["S9"]  # 不存在于本报告真实来源
    payload.update({"report_id": "r-rollback", "kind": "stock", "subject": "600001"})
    canned = _CannedModel(monkeypatch, [json.dumps(SAMPLE_CONCLUSION, ensure_ascii=False)])
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    monkeypatch.setattr(
        db_module.Database, "get_ai_research_report", lambda self, report_id: dict(payload)
    )

    result = _repair(test_client, headers, "r-rollback", "conclusion_only")

    assert result["ok"] is True
    assert result["repair"]["repaired"] is False
    assert "引用校验未通过" in result["repair"]["detail"]
    # 回滚 = 原 payload 逐字节不变：没有结论、引用清单也没被改写、版本不升。
    assert not (result["item"].get("conclusion") or {}).get("statement")
    assert result["item"]["citations"] == ["S9"]
    assert result["item"].get("report_revision") in (None, 1)
    assert not result["item"].get("revision_history")
    assert result["repair"]["revision"] == 1


def test_repair_rejects_full_rerun_strategy(client, tmp_path, monkeypatch):
    """`full_rerun` 刻意不开放：整稿重跑需要当时的完整证据包，落库报告不保存证据。

    请求体在模型层就被 `Literal` 拒掉（422），连端点都不会进——**不发一次付费调用**。
    """
    payload = _good_payload()
    payload.pop("conclusion")
    _canned, test_client, headers, body = _generate(client, tmp_path, monkeypatch, payload)

    response = test_client.post(
        f"/ai-research/reports/{body['report_id']}/repair",
        json={"strategy": "full_rerun"},
        headers=headers,
    )
    assert response.status_code == 422


def test_repair_unknown_report_is_404(client, tmp_path, monkeypatch):
    test_client, headers = _setup(client, tmp_path, monkeypatch)
    response = test_client.post(
        "/ai-research/reports/does-not-exist/repair",
        json={"strategy": "conclusion_only"},
        headers=headers,
    )
    assert response.status_code == 404


def test_repair_rejects_non_stock_report(client, tmp_path, monkeypatch):
    """方向研判没有个股研报的字段结构，如实拒绝而不是硬套一套提示词。"""
    from investment_steward_core.storage import database as db_module

    test_client, headers = _setup(client, tmp_path, monkeypatch)
    monkeypatch.setattr(
        db_module.Database,
        "get_ai_research_report",
        lambda self, report_id: {"report_id": report_id, "kind": "direction", "subject": "大盘"},
    )

    result = _repair(test_client, headers, "r-direction", "conclusion_only")
    assert result["ok"] is False
    assert result["stage"] == "unsupported_kind"


def test_repair_refuses_without_available_citations(client, tmp_path, monkeypatch):
    """拿不准「哪些来源真实存在」时不修复——否则补写会产出无法校验的引用。"""
    from investment_steward_core.storage import database as db_module

    test_client, headers = _setup(client, tmp_path, monkeypatch)
    monkeypatch.setattr(
        db_module.Database,
        "get_ai_research_report",
        lambda self, report_id: {"report_id": report_id, "kind": "stock", "subject": "600001"},
    )

    result = _repair(test_client, headers, "r-no-citations", "conclusion_only")
    assert result["ok"] is False
    assert result["stage"] == "no_available_citations"


def test_repair_refuses_when_no_active_profile(tmp_path, monkeypatch):
    """没有「使用中」的模型方案 → 如实回报，不建任何调用。"""
    from fastapi.testclient import TestClient
    from investment_steward_core.api import app as api_app
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings
    from investment_steward_core.credential_store import DbCredentialStore
    from investment_steward_core.storage import database as db_module

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    token = "test-session-token"
    test_client = TestClient(create_app(CoreSettings(session_token=token, data_dir=tmp_path)))
    headers = {"X-Core-Session-Token": token}
    monkeypatch.setattr(
        db_module.Database,
        "get_ai_research_report",
        lambda self, report_id: {
            "report_id": report_id, "kind": "stock", "subject": "600001",
            "available_citations": ["S1"],
        },
    )

    result = _repair(test_client, headers, "r-no-profile", "conclusion_only")
    assert result["ok"] is False
    assert result["stage"] == "no_active_profile"


def test_repair_refuses_when_model_access_disabled(tmp_path, monkeypatch):
    """出网总闸关闭 → 不发起任何模型请求（与生成端点同一口径）。"""
    from fastapi.testclient import TestClient
    from investment_steward_core.api import app as api_app
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings
    from investment_steward_core.credential_store import DbCredentialStore
    from investment_steward_core.storage import database as db_module

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    token = "test-session-token"
    test_client = TestClient(
        create_app(CoreSettings(session_token=token, data_dir=tmp_path, model_access_enabled=False))
    )
    headers = {"X-Core-Session-Token": token}
    monkeypatch.setattr(
        db_module.Database,
        "get_ai_research_report",
        lambda self, report_id: {
            "report_id": report_id, "kind": "stock", "subject": "600001",
            "available_citations": ["S1"],
        },
    )

    result = _repair(test_client, headers, "r-no-net", "conclusion_only")
    assert result["ok"] is False
    assert result["stage"] == "model_access_disabled"
    assert "未发起任何模型请求" in result["detail"]

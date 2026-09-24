"""v23 AI 协同流水线测试：串行角色链（check→draft→risk→revise）+ 跨报告综合。

红线：
- 角色严格串行：一次 /evidence/collab-next 只推进一个角色，失败不跳过、重试同阶段；
- 证据同源：运行创建时定稿证据包，全角色共享；
- 综合不投票：分歧如实陈列；无 citations 的报告类输出不发布（ADR-0006）。
模型调用一律 mock _post_json，行情/新闻/财报取数一律 monkeypatch，绝不真实联网。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用

from test_stock_research_tools import _file_client, _mock_sources, _setup_active_profile


def _report_payload(title: str) -> str:
    return json.dumps(
        {
            "title": title,
            "executive_summary": "结论先行：震荡上行，量能是关键。",
            "report": "### 技术面\n**缓涨趋势完好** [S1]\n### 消息面\n中标大单为已落地事实 [S2]\n### 基本面\n无数据\n### 综合判断\n多空情景与关键价位见正文 [S1]",
            "citations": ["S1", "S2"],
            "limitations": ["来源仅覆盖近 20 日新闻"],
            "confidence": "medium",
        },
        ensure_ascii=False,
    )


def _stage_reply_factory(fail_on_call: int | None = None):
    """按调用序返回各角色产出；fail_on_call 指定第 N 次调用返回垃圾（模拟解析失败）。"""
    calls = {"count": 0}

    def _fake(*_args, **_kwargs):
        calls["count"] += 1
        if fail_on_call is not None and calls["count"] == fail_on_call:
            return {"choices": [{"message": {"content": "这不是 JSON 输出"}}]}
        index = calls["count"]
        if fail_on_call is not None and calls["count"] > fail_on_call:
            index = calls["count"] - 1  # 失败重试后，后续角色按原序取产出
        payloads = [
            json.dumps({"observations": ["S1 为日线 K 线，截至 2026-06-29"], "gaps": [], "readiness": "high"}, ensure_ascii=False),
            _report_payload("初稿：甲股震荡上行"),
            json.dumps({"risks": ["初稿对量能的判断证据不足：S1 近 5 日均量下降"], "verdict": "partial"}, ensure_ascii=False),
            json.dumps(
                {
                    **json.loads(_report_payload("修订稿：甲股震荡上行（已吸收风险意见）")),
                    "revision_notes": "采纳：下调量能判断。",
                },
                ensure_ascii=False,
            ),
        ]
        return {"choices": [{"message": {"content": payloads[index - 1]}}]}

    return _fake


def _start_run(test_client, headers, symbol: str = "600001") -> dict:
    body = test_client.post(
        "/evidence/collab-run",
        json={"symbol": symbol, "question": "趋势是否延续"},
        headers=headers,
    ).json()
    assert body["ok"] is True, body
    return body["run"]


def test_collab_pipeline_runs_all_stages_serially(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    monkeypatch.setattr(mc, "_post_json", _stage_reply_factory())

    run = _start_run(test_client, headers)
    assert run["done"] is False and run["next_index"] == 0
    assert [stage["stage"] for stage in run["stages"]] == ["check", "draft", "risk", "revise"]
    assert all(stage["status"] == "pending" for stage in run["stages"])

    expected_order = ["check", "draft", "risk", "revise"]
    for step, stage_name in enumerate(expected_order):
        body = test_client.post(
            "/evidence/collab-next", json={"run_id": run["run_id"]}, headers=headers
        ).json()
        assert body["ok"] is True, body
        assert body["executed_stage"] == stage_name
        assert body["executed_status"] == "done"
        assert body["run"]["next_index"] == step + 1
        # 串行约束的显式体现：每一步只推进一个角色，其余仍 pending
        assert body["run"]["stages"][step]["status"] == "done"
        assert all(
            stage["status"] == "pending" for stage in body["run"]["stages"][step + 1:]
        )

    final = test_client.post(
        "/evidence/collab-next", json={"run_id": run["run_id"]}, headers=headers
    ).json()
    assert final["done"] is True
    # 修订稿落库：模型名带「·协同流水线」标记，可从历史产出回看
    history = test_client.get("/ai-research/reports", params={"kind": "stock"}, headers=headers).json()
    assert any(item["model"].endswith("·协同流水线") for item in history["items"])


def test_collab_failed_stage_does_not_skip_and_retry_works(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)
    monkeypatch.setattr(mc, "_post_json", _stage_reply_factory(fail_on_call=2))  # 第 2 次调用 = draft 失败

    run = _start_run(test_client, headers)
    body = test_client.post("/evidence/collab-next", json={"run_id": run["run_id"]}, headers=headers).json()
    assert body["executed_stage"] == "check" and body["executed_status"] == "done"

    body = test_client.post("/evidence/collab-next", json={"run_id": run["run_id"]}, headers=headers).json()
    assert body["executed_stage"] == "draft" and body["executed_status"] == "failed"
    assert body["done"] is False  # 失败不跳过，流水线停在原阶段
    failed_stage = body["run"]["stages"][1]
    assert failed_stage["status"] == "failed" and "无法解析" in failed_stage["error"]

    # 重试同阶段（而非下一阶段）→ 成功后继续走 risk / revise
    body = test_client.post("/evidence/collab-next", json={"run_id": run["run_id"]}, headers=headers).json()
    assert body["executed_stage"] == "draft" and body["executed_status"] == "done"
    assert body["run"]["stages"][2]["status"] == "pending"


def test_compare_synthesis_reports_divergences_honestly(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    synthesis_json = json.dumps(
        {
            "summary": "两模型对趋势方向一致，对量能解读分歧。",
            "consensus": ["两模型均认为技术面缓涨趋势完好（报告 1/2）"],
            "divergences": ["量能：模型 A 认为放量有效；模型 B 认为缩量上涨不可持续"],
            "to_verify": ["下一期财报验证利润质量"],
        },
        ensure_ascii=False,
    )
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": synthesis_json}}]})
    body = test_client.post(
        "/evidence/compare-synthesis",
        json={
            "question": "趋势是否延续",
            "reports": [
                {"symbol": "600001", "model": "模型A", "confidence": "medium", "executive_summary": "看多", "report": "### 技术面\n看多"},
                {"symbol": "600001", "model": "模型B", "confidence": "low", "executive_summary": "谨慎", "report": "### 技术面\n缩量上行动能不足"},
            ],
        },
        headers=headers,
    )
    assert body.status_code == 200, body.text
    payload = body.json()
    assert payload["ok"] is True
    assert len(payload["divergences"]) == 1 and "模型 A" in payload["divergences"][0]
    assert payload["consensus"] and payload["to_verify"]


def test_compare_synthesis_unparseable_output_is_honest(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": "无法解析"}}]})
    body = test_client.post(
        "/evidence/compare-synthesis",
        json={
            "reports": [
                {"symbol": "600001", "model": "模型A", "report": "正文A"},
                {"symbol": "600002", "model": "模型B", "report": "正文B"},
            ]
        },
        headers=headers,
    ).json()
    assert body["ok"] is False and body["stage"] == "parse"


def test_collab_run_all_sources_fail_and_unknown_run_404(client, tmp_path, monkeypatch):
    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    import investment_steward_core.api.app as app_module

    def _raise(*_a, **_k):
        raise RuntimeError("网络不可用")

    monkeypatch.setattr(app_module, "fetch_cn_kline", _raise)
    monkeypatch.setattr(app_module, "fetch_cn_news", _raise)
    monkeypatch.setattr(app_module, "fetch_cn_announcements", _raise)
    monkeypatch.setattr(app_module, "fetch_financials", _raise)
    monkeypatch.setattr(app_module, "fetch_valuation", _raise)
    body = test_client.post("/evidence/collab-run", json={"symbol": "600001"}, headers=headers).json()
    assert body["ok"] is False and body["stage"] == "evidence_unavailable"
    assert set(body["source_errors"].keys()) == {
        "kline", "kline_week", "kline_month", "news", "announcements", "financials", "valuation",
    }

    # 不存在的运行：404（运行态在内存，重启清空属预期行为）
    response = test_client.post("/evidence/collab-next", json={"run_id": "nonexistent-run-id"}, headers=headers)
    assert response.status_code == 404
    response = test_client.get("/evidence/collab-state", params={"run_id": "nonexistent-run-id"}, headers=headers)
    assert response.status_code == 404


def test_collab_stage_profiles_assign_different_models_per_role(client, tmp_path, monkeypatch):
    """用户拍板：不同模型执行不同阶段，整体串行——每个角色用各自指定的方案出话。"""
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    created_b = test_client.post(
        "/model-profiles",
        json={"name": "免费Doubao", "base_url": "https://ark.example.com",
              "model": "doubao-seed-2-1-pro", "credential_ref": "model_api_key"},
        headers=headers,
    ).json()
    _mock_sources(monkeypatch)

    calls: list[str] = []

    # 复用按调用序返回产出的工厂，但额外记录每次调用的 model 字段（_post_json(base_url, payload, key, timeout)）
    inner = _stage_reply_factory()

    def _spy(*args, **_kwargs):
        calls.append(args[1].get("model", ""))
        return inner(*args, **_kwargs)

    monkeypatch.setattr(mc, "_post_json", _spy)

    body = test_client.post(
        "/evidence/collab-run",
        json={
            "symbol": "600001",
            "question": "趋势是否延续",
            "stage_profiles": {"check": created_b["profile_id"], "risk": created_b["profile_id"]},
        },
        headers=headers,
    ).json()
    assert body["ok"] is True, body
    run_id = body["run"]["run_id"]
    # 核对/初稿/风险/修订 = 4 次调用；check 与 risk 用 Doubao，draft 与 revise 回落使用中方案
    for _ in range(4):
        step = test_client.post("/evidence/collab-next", json={"run_id": run_id}, headers=headers).json()
        assert step["ok"] is True and step["executed_status"] == "done"
    assert calls == ["doubao-seed-2-1-pro", "deepseek-v4-flash", "doubao-seed-2-1-pro", "deepseek-v4-flash"]
    assert body["run"]["stage_models"]["check"] == "免费Doubao"

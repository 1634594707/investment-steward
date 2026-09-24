"""v33 方向研判升级（2026-09-14 路线图 B/C 组）测试。

- B01 两种诚实输出状态：无证据 → knowledge（必须声明数据缺口），有证据 → evidence；
- B09/B10/B11/B12 候选股：证券身份归一（instruments）、三种资格分列、模型估价剔除、入选解释；
- B15 方向专属质量闸门：十小节、核心判断分层、引用真实性、三态状态；
- B06 反方审查：覆盖全部判断与候选理由，id/symbol 越界剔除；
- C02/C04/E04（后端部分）：本次模型可选——显式 profile_id 贯通到调用与落库，
  未知 profile 404、未知 mode 422；旧客户端不传沿用默认。
"""

from __future__ import annotations

import json

from investment_steward_core import direction_research as direction_engine

# ---------------------------------------------------------------------------
# 单元：候选池归一化（B09/B11）
# ---------------------------------------------------------------------------

def test_normalize_pool_identity_and_dedupe():
    pool, warnings = direction_engine.normalize_pool([
        {"symbol_raw": "600519", "name": "贵州茅台", "sector": "高端白酒",
         "business_link": "白酒主业", "profit_path": "提价+放量", "counter_evidence": "消费税改革"},
        {"symbol_raw": "SH600519", "name": "重复", "sector": "白酒"},
        {"symbol_raw": "999999", "name": "不存在"},
        {"symbol_raw": "510300", "name": "沪深300ETF"},
        {"symbol_raw": "000001", "name": "平安银行", "reference_price": "12.5"},
    ])
    by_symbol = {item["symbol"]: item for item in pool}
    assert by_symbol["600519"]["identity_status"] == "verified"
    assert by_symbol["600519"]["exchange"] == "sh"
    assert by_symbol["999999"]["identity_status"] == "invalid"
    assert by_symbol["510300"]["identity_status"] == "invalid"  # ETF 不是股票候选
    assert by_symbol["000001"]["identity_status"] == "verified"
    # B11：模型价格字段剔除并告警
    assert any("价格字段" in warning for warning in warnings)
    assert all("reference_price" not in item or item.get("reference_price") is None for item in pool)
    # 去重标记
    dup_rows = [item for item in pool if item["duplicate_of_pool"]]
    assert len(dup_rows) == 1 and dup_rows[0]["symbol"] == "600519"
    # B10：三种资格分列
    assert by_symbol["600519"]["qualification"]["market_data"] == "unverified"
    assert by_symbol["999999"]["qualification"]["identity"] == "invalid"


def test_normalize_pool_empty_and_garbage():
    pool, warnings = direction_engine.normalize_pool(None)
    assert pool == [] and warnings == []
    pool, _ = direction_engine.normalize_pool(["不是字典", {"symbol_raw": ""}])
    assert pool == []


# ---------------------------------------------------------------------------
# 单元：核心判断分层（B05）与质量闸门（B15）
# ---------------------------------------------------------------------------

def _good_direction_payload() -> dict:
    sections = "".join(
        f"## {name}\n本节按事实→推理→不确定性展开，给出可核对的数据与边界条件，避免空泛表述。\n"
        for name in direction_engine.REQUIRED_DIRECTION_SECTIONS
    )
    return {
        "title": "固态电池：量产前夜的产业链利润再分配",
        "executive_summary": "方向判断：2027 年前半固态先装车。最强证据：头部电池厂中试线投产。最大不确定性：良率与成本下降斜率，数据缺口需行业排产数据验证。",
        "report": sections,
        "core_judgments": [
            {"id": "J1", "text": "2026 年半固态装车量翻倍", "kind": "industry", "confidence": "medium"},
            {"id": "J2", "text": "电解质厂商格局优于电芯厂", "kind": "competitiveness", "confidence": "low"},
            {"id": "J3", "text": "主题热度高位，资金分歧加大", "kind": "sentiment", "confidence": "low"},
        ],
        "catalysts": ["头部电池厂量产公告"],
        "risks": ["良率爬坡不及预期"],
        "stock_pool": [
            {"symbol_raw": "300450", "name": "先导智能", "sector": "设备",
             "business_link": "固态电池产线设备订单", "profit_path": "设备验收确认收入",
             "counter_evidence": "资本开支周期下行"},
        ],
        "data_gaps": ["行业排产数据未接入"],
    }


def test_validate_direction_good_payload_complete():
    quality = direction_engine.validate_direction(_good_direction_payload(), mode="standard", research_mode="knowledge", evidence_ids=set())
    assert quality["quality_status"] in ("complete", "needs_review")
    assert quality["quality_blockers"] == []
    assert quality["mode"] == "standard"
    assert quality["valid_pool_count"] == 1
    assert quality["pool"][0]["symbol"] == "300450"
    assert any("资金情绪" not in warning for warning in quality["quality_warnings"])
    kinds = {item["kind"] for item in quality["judgments"]}
    assert {"industry", "competitiveness", "sentiment"} <= kinds


def test_validate_direction_missing_section_is_incomplete():
    payload = _good_direction_payload()
    payload["report"] = payload["report"].replace("## 景气阶段\n本节按事实→推理→不确定性展开，给出可核对的数据与边界条件，避免空泛表述。\n", "## 景气阶段\n见上文。\n")
    quality = direction_engine.validate_direction(payload, mode="standard", research_mode="knowledge", evidence_ids=set())
    assert quality["quality_status"] == "incomplete"
    assert any(blocker["code"] == "direction_missing_section" for blocker in quality["quality_blockers"])
    assert "景气阶段" in quality["missing_sections"]


def test_validate_direction_knowledge_mode_with_fake_refs_warns():
    payload = _good_direction_payload()
    payload["report"] = payload["report"].replace("## 需求供给与价格\n", "## 需求供给与价格\n需求前置 [E1]。\n")
    quality = direction_engine.validate_direction(payload, mode="standard", research_mode="knowledge", evidence_ids=set())
    assert any("知识概览模式" in warning for warning in quality["quality_warnings"])


def test_validate_direction_empty_pool_requires_explanation():
    payload = _good_direction_payload()
    payload["stock_pool"] = []
    payload["data_gaps"] = []  # 无解释 → 必须告警
    quality = direction_engine.validate_direction(payload, mode="standard", research_mode="knowledge", evidence_ids=set())
    assert any("候选池为空" in warning for warning in quality["quality_warnings"])
    payload["data_gaps"] = ["主题过窄，无合规候选"]
    quality2 = direction_engine.validate_direction(payload, mode="standard", research_mode="knowledge", evidence_ids=set())
    assert not any("候选池为空" in warning for warning in quality2["quality_warnings"])


def test_direction_mode_budget_unknown_raises():
    try:
        direction_engine.direction_mode_budget("ultra")
        assert False, "should raise"
    except ValueError as error:
        assert "未知方向研判模式" in str(error)


# ---------------------------------------------------------------------------
# 单元：反方审查归一化（B06）
# ---------------------------------------------------------------------------

def test_direction_counter_check_normalization_gates_ids():
    normalized = direction_engine.normalize_direction_counter_check(
        {
            "strongest_counter": "需求前置：2026 年装车量含库存虚增",
            "affected_conclusions": [
                {"id": "J1", "effect": "weakens", "reason": "量能数据含渠道库存"},
                {"id": "JX", "effect": "supports", "reason": "自造编号"},
            ],
            "pool_challenges": [
                {"symbol": "300450", "challenge": "设备订单增速已放缓"},
                {"symbol": "999999", "challenge": "不在候选清单"},
            ],
            "verdict_robustness": "fragile",
        },
        judgment_ids={"J1", "J2"},
        pool_symbols={"300450"},
    )
    assert normalized is not None
    assert [item["id"] for item in normalized["affected_conclusions"]] == ["J1"]
    assert normalized["dropped_refs"] == ["JX", "999999"]
    assert normalized["verdict_robustness"] == "fragile"
    assert direction_engine.normalize_direction_counter_check({"affected_conclusions": []}, judgment_ids={"J1"}, pool_symbols=set()) is None


# ---------------------------------------------------------------------------
# 端点：本次模型可选（C02/C04/E04 后端）+ 质量字段 + 落库
# ---------------------------------------------------------------------------

def _file_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from investment_steward_core.api import app as api_app
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


def _setup_two_profiles(test_client, headers) -> tuple[str, str]:
    """创建两个模型方案并激活第一个；返回 (profile_a_id, profile_b_id)。"""
    test_client.put("/credentials/model_api_key", json={"secret": "sk-test-1234567890"}, headers=headers)
    created_a = test_client.post(
        "/model-profiles",
        json={"name": "方案A", "base_url": "https://api.a.com", "model": "model-a", "credential_ref": "model_api_key"},
        headers=headers,
    ).json()
    created_b = test_client.post(
        "/model-profiles",
        json={"name": "方案B", "base_url": "https://api.b.com", "model": "model-b", "credential_ref": "model_api_key"},
        headers=headers,
    ).json()
    assert test_client.post(f"/model-profiles/{created_a['profile_id']}/activate", headers=headers).status_code == 200
    return created_a["profile_id"], created_b["profile_id"]


def test_direction_endpoint_profile_selection_and_quality(client, tmp_path, monkeypatch):
    """C02/C04：显式选方案 B → 调用与落库都是 B，全局仍是 A；质量字段与身份归一随响应下发。"""
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    profile_a, profile_b = _setup_two_profiles(test_client, headers)

    canned_bodies: list[str] = []

    def _fake(_base_url, payload, _key, _timeout):
        blob = json.dumps(payload, ensure_ascii=False)
        canned_bodies.append(blob)
        if "反方审查员" in blob:
            return {"choices": [{"message": {"content": json.dumps({
                "strongest_counter": "需求前置透支",
                "affected_conclusions": [{"id": "J1", "effect": "weakens", "reason": "含渠道库存"}],
                "pool_challenges": [{"symbol": "300450", "challenge": "订单放缓"}],
                "verdict_robustness": "mixed",
            }, ensure_ascii=False)}}]}
        return {"choices": [{"message": {"content": json.dumps(_good_direction_payload(), ensure_ascii=False)}}]}

    monkeypatch.setattr(mc, "_post_json", _fake)

    body = test_client.post(
        "/evidence/direction-research",
        json={"topic": "固态电池产业化", "question": "未来 3 年哪些环节利润最厚", "profile_id": profile_b, "with_counter_check": True},
        headers=headers,
    ).json()
    assert body["ok"] is True
    # C04：调用身份冻结——本次用了 B（请求方案=实际方案），全局 A 未被改动
    trace = body["generation_trace"]
    assert trace["profile_id"] == profile_b
    assert trace["requested_profile_id"] == profile_b
    assert trace["model"] == "model-b"
    assert body["model"] == "model-b"
    assert test_client.get("/model-profiles", headers=headers).json()  # 全局仍 A（下面断言）
    profiles = {item["profile_id"]: item for item in test_client.get("/model-profiles", headers=headers).json()}
    assert profiles[profile_a]["status"] == "使用中"
    # B01：宏观数据日历（纯规则推算，恒可用）→ evidence 模式；事件层未装插件如实进 unavailable
    assert body["research_mode"] == "evidence"
    assert body["evidence_snapshot"], "日历快照应至少给出数据发布日历条目"
    assert all(item["evidence_id"] and item["retrieved_at"] for item in body["evidence_snapshot"])
    assert any("macro-radar" in (body["evidence_unavailable"] or "") for _ in [0])
    # B09/B11：身份归一 + 无价格字段
    assert body["stock_pool"][0]["identity_status"] == "verified"
    assert body["stock_pool"][0]["symbol"] == "300450"
    assert not any(item.get("reference_price") for item in body["stock_pool"])
    # B15：质量三态随响应；B06：反方审查成功
    assert body["quality_status"] in ("complete", "needs_review")
    assert body["counter_check"]["ok"] is True
    assert body["counter_check"]["affected_conclusions"][0]["id"] == "J1"
    # 落库：kind=direction + is_draft + generation_trace
    record = test_client.get(f"/ai-research/reports/{body['report_id']}", headers=headers).json()["item"]
    assert record["kind"] == "direction"
    assert record["is_draft"] is False
    assert record["generation_trace"]["profile_id"] == profile_b
    assert len(canned_bodies) == 2  # 研判 + 反方审查


def test_direction_endpoint_c02_industry_layer_survives_counter_check(client, tmp_path, monkeypatch):
    """M2-C02 次序回归（2026-09-15 第二轮路线图；G02 真实重跑发现）。

    真实重跑里反方审查撤下了 J1/J2/J4/J5，其中一条正是承载「产业景气」层的判断 →
    `apply_quality_repairs` 早先那次「已补齐产业景气层」在定稿后失效，质量告警又出现
    「核心判断缺少『产业景气』层」。保证必须在**定稿之后**成立：撤下后须再补一次 low 置信
    兜底条（只写「证据未覆盖，不能从低 PB 推出景气修复」，不编景气数据、不冒充反转）。
    """
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    _setup_two_profiles(test_client, headers)

    def _fake(_base_url, payload, _key, _timeout):
        blob = json.dumps(payload, ensure_ascii=False)
        if "反方审查员" in blob:
            # 撤下 J1（产业景气层）——正是 G02 里发生的次序问题
            return {"choices": [{"message": {"content": json.dumps({
                "strongest_counter": "装车量翻倍缺排产证据",
                "affected_conclusions": [{"id": "J1", "effect": "weakens", "reason": "无排产数据"}],
                "pool_challenges": [],
                "verdict_robustness": "fragile",
            }, ensure_ascii=False)}}]}
        return {"choices": [{"message": {"content": json.dumps(_good_direction_payload(), ensure_ascii=False)}}]}

    monkeypatch.setattr(mc, "_post_json", _fake)

    body = test_client.post(
        "/evidence/direction-research",
        json={"topic": "固态电池产业化", "with_counter_check": True},
        headers=headers,
    ).json()
    assert body["counter_check_application"]["removed_judgment_ids"] == ["J1"]
    kinds = [item["kind"] for item in body["core_judgments"]]
    assert "industry" in kinds, "定稿后仍必须有产业景气层"
    added = [item for item in body["core_judgments"] if item["kind"] == "industry"][0]
    assert added["confidence"] == "low"
    assert added["text"] == direction_engine.INDUSTRY_JUDGMENT_FALLBACK_TEXT
    assert not any("缺少「产业景气」层" in warning for warning in body["quality_warnings"])
    assert body["quality_repairs"]["industry_layer_after_counter_check"]["added"] is True


def test_direction_endpoint_unknown_profile_404_and_mode_422(client, tmp_path, monkeypatch):
    from uuid import uuid4

    test_client, headers = _file_client(tmp_path, monkeypatch)
    _setup_two_profiles(test_client, headers)
    assert test_client.post(
        "/evidence/direction-research", json={"topic": "测试主题", "profile_id": str(uuid4())}, headers=headers
    ).status_code == 404
    assert test_client.post(
        "/evidence/direction-research", json={"topic": "测试主题", "mode": "ultra"}, headers=headers
    ).status_code == 422


def test_direction_endpoint_parse_failure_no_persistence(client, tmp_path, monkeypatch):
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    _setup_two_profiles(test_client, headers)
    monkeypatch.setattr(mc, "_post_json", lambda *a, **k: {"choices": [{"message": {"content": "不是 JSON"}}]})
    body = test_client.post(
        "/evidence/direction-research", json={"topic": "测试主题", "with_counter_check": False}, headers=headers
    ).json()
    assert body["ok"] is False
    assert body["stage"] == "parse"
    listing = test_client.get("/ai-research/reports?kind=direction", headers=headers).json()
    assert listing["items"] == []


def test_industry_template_hint_matches_industries():
    """B07：按主题关键词匹配行业指标模板；未命中给通用框架并声明缺口口径。"""
    assert "制造类" in direction_engine.industry_template_hint("固态电池设备")
    assert "消费类" in direction_engine.industry_template_hint("新消费渠道")
    assert "资源类" in direction_engine.industry_template_hint("铜矿资本开支")
    assert "科技类" in direction_engine.industry_template_hint("AI 商业化")
    generic = direction_engine.industry_template_hint("完全未知的主题")
    assert "通用框架" in generic and "如实说明缺口" in generic

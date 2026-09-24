"""P3（游资雷达插件）D01/D02/D06 端点与产物测试——不联网。

守的是三件事：
1. **D01 插槽与导航**：`app.youzi` 注册且**解析到 youzi 页**（不是回落 library）。
2. **D02 插件产物**：manifest 可验签、可安装、占用恰好一个独立应用额度。
3. **D06 边界**：插件未启用时 /youzi/* 全 409；取数失败如实 `available=False`，
   不用空表冒充「今天没上榜」。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from investment_steward_core import slots as slots_module
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.signing import verify_manifest_integrity

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = REPO_ROOT / "plugins" / "official" / "youzi-radar"
PUBLIC_KEY = REPO_ROOT / "apps" / "core-api" / "keys" / "steward-plugin-publishing.pub.pem"

PLUGIN_ID = "official.youzi-radar"
SLOT_ID = "app.youzi"

#: Y4-01：二期新增的五课（扫描/校验以这些为对象，旧课另有覆盖）。
NEW_LEVELS = {"L6", "L7", "L8", "L9", "L10"}

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "youzi"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _billboard():
    """2026-09-16 真实上榜列表（含 000592，不含 600519）。"""
    from investment_steward_core import lhb_feed as lf

    return lf.parse_billboard_rows(_fixture("em_lhb_details_20260916.json"))


def _seats():
    """同一天的买卖两侧席位（含观察名单席位，也含匿名汇总桶）。"""
    from investment_steward_core import lhb_feed as lf

    return lf.parse_seat_rows(
        _fixture("em_lhb_detailbuy_20260916.json"), lf.DIRECTION_BUY
    ) + lf.parse_seat_rows(_fixture("em_lhb_detailsell_20260916.json"), lf.DIRECTION_SELL)


@pytest.fixture()
def client(tmp_path: Path):
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


# —— D01：插槽与导航 ——


def test_app_youzi_slot_is_registered():
    slots = {entry["slot"]: entry for entry in slots_module.DEFINED_SLOTS}
    assert SLOT_ID in slots
    assert slots[SLOT_ID]["page"] == "youzi"
    assert slots[SLOT_ID]["level"] == "L3"


def test_app_youzi_resolves_to_youzi_page_not_library():
    """`app.*` 前缀默认回落 library；app.youzi 必须显式映射，否则页面会错落到图书馆。"""
    assert slots_module.slot_page_of(SLOT_ID) == "youzi"
    # 未注册的 app.* 仍回落 library（既有兜底行为未被破坏）
    assert slots_module.slot_page_of("app.something-else") == "library"


def test_slots_endpoint_returns_youzi_row(client):
    test_client, headers = client
    rows = {row["slot"]: row for row in test_client.get("/slots", headers=headers).json()}
    assert SLOT_ID in rows
    assert rows[SLOT_ID]["page"] == "youzi"
    assert rows[SLOT_ID]["used"] == 0


# —— D02：插件产物 ——


def test_manifest_exists_and_is_valid_json():
    manifest = json.loads((PLUGIN_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["plugin_id"] == PLUGIN_ID
    assert manifest["plugin_type"] == "evidence_app"
    assert manifest["mount"] == "own_page"
    assert manifest["ui_slots"] == [{"slot": SLOT_ID, "level": "L3"}]
    assert manifest["requires_confirmation"] is True


def test_manifest_signature_verifies():
    """产物必须能被发布者公钥验签；签名坏了就装不上。"""
    manifest = json.loads((PLUGIN_DIR / "manifest.json").read_text(encoding="utf-8"))
    verify_manifest_integrity(manifest, PUBLIC_KEY.read_text(encoding="utf-8"))


def test_manifest_declares_expected_network_allowlist():
    """白名单要与内核实际访问的域名一致（多写少写都是问题）。"""
    manifest = json.loads((PLUGIN_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["network_allowlist"]) == {
        "www.cffex.com.cn",
        "datacenter-web.eastmoney.com",
        "data.eastmoney.com",
    }


def test_manifest_declares_known_schemas_only():
    """schema 必须是 Core 已知的，否则健康检查会拒绝切换。"""
    from investment_steward_core.api.app import _KNOWN_SCHEMAS

    manifest = json.loads((PLUGIN_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["schema_versions"]) <= _KNOWN_SCHEMAS


def test_plugin_appears_in_catalog_with_youzi_slot(client):
    test_client, headers = client
    catalog = test_client.get("/plugins/catalog", headers=headers).json()
    entry = next(e for e in catalog if e["manifest"]["plugin_id"] == PLUGIN_ID)
    assert entry["installation"] is None  # 未安装
    assert entry["resolved_outputs"] == [{"slot": SLOT_ID, "page": "youzi", "level": "L3"}]


def test_plugin_install_succeeds_and_enables(client):
    test_client, headers = client
    response = test_client.post(f"/plugins/{PLUGIN_ID}/install", headers=headers)
    assert response.status_code == 200
    assert response.json()["state"] == "enabled"


def test_all_four_own_page_apps_fit_in_capacity(client):
    """应用区容量 4：图书馆 + 宏观 + 战法 + 游资 = 恰好用满（第 5 个才会 409）。"""
    test_client, headers = client
    for plugin_id in (
        "official.reading-library",
        "official.macro-radar",
        "official.stock-tactics",
        PLUGIN_ID,
    ):
        assert test_client.post(f"/plugins/{plugin_id}/install", headers=headers).status_code == 200


# —— 课程表：与页面同源 ——


def test_curriculum_eleven_lessons_and_required_fields():
    """Y4-01/Y4-06：保留 L0–L5 并新增 L6–L10，共十一课，逐课字段齐全。"""
    curriculum = json.loads((PLUGIN_DIR / "curriculum.json").read_text(encoding="utf-8"))
    lessons = curriculum["lessons"]
    assert [item["level"] for item in lessons] == [
        "L0", "L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8", "L9", "L10",
    ]
    for lesson in lessons:
        assert lesson["title"] and lesson["mechanism"] and lesson["exercise"]
        assert lesson["acceptance"]
        # 新课逐条要求 visible 边界说明。
        if lesson["level"] in {"L6", "L7", "L8", "L9", "L10"}:
            assert lesson.get("principle"), f"{lesson['level']} 缺 principle"
            assert lesson.get("boundary"), f"{lesson['level']} 缺 boundary（可见性边界）"


def test_curriculum_version_is_bumped_to_v2():
    curriculum = json.loads((PLUGIN_DIR / "curriculum.json").read_text(encoding="utf-8"))
    assert curriculum["version"] == "2026-09-18-v2"


def test_curriculum_mechanisms_resolve_to_delivered_capabilities():
    """Y4-02：机制引用必须能解析到**已登记**的能力，而不是只检查非空。

    两种可接受形态：①直接引用 `module.symbol` 形式的真实符号；②引用
    `mechanism_registry` 里登记的 id。指向未实现能力时必须显式标 `not_implemented`。
    """
    import importlib

    curriculum = json.loads((PLUGIN_DIR / "curriculum.json").read_text(encoding="utf-8"))
    registry = {entry["id"]: entry for entry in curriculum["mechanism_registry"]["entries"]}

    def resolve_symbol(dotted: str) -> bool:
        """`module.attr` → 真实可导入且属性存在。"""
        if "." not in dotted:
            return False
        module_name, _, attr = dotted.partition(".")
        try:
            module = importlib.import_module(f"investment_steward_core.{module_name}")
        except ImportError:
            return False
        return hasattr(module, attr)

    checked = 0
    for lesson in curriculum["lessons"]:
        if lesson["level"] not in {"L6", "L7", "L8", "L9", "L10"}:
            continue
        mechanism = lesson["mechanism"]
        # 机制串里必须点名一个已登记的 capability id。
        hit = next((cid for cid in registry if cid in mechanism), None)
        assert hit is not None, f"{lesson['level']} 的机制未引用任何已登记能力：{mechanism!r}"
        entry = registry[hit]
        assert entry["status"] in {"delivered", "not_implemented"}
        if entry["status"] == "delivered":
            assert entry["endpoint"], f"{hit} 标为已交付却没有端点"
            assert resolve_symbol(entry["ref"].split("（")[0]), (
                f"{hit} 声称已交付但符号解析不到：{entry['ref']!r}"
            )
            checked += 1
    assert checked >= 5, "至少五课应挂到已交付且可解析的机制上"


def test_curriculum_marks_unimplemented_capability_as_not_observable():
    """Y4-05：不在本版范围内的能力（打板识别）必须标 `not_implemented` 且无端点。"""
    curriculum = json.loads((PLUGIN_DIR / "curriculum.json").read_text(encoding="utf-8"))
    entries = {entry["id"]: entry for entry in curriculum["mechanism_registry"]["entries"]}
    assert entries["y5.intraday_pattern"]["status"] == "not_implemented"
    assert entries["y5.intraday_pattern"]["endpoint"] == ""


def test_curriculum_glossary_every_term_states_its_evidence_boundary():
    """Y4-05：逐词给出证据边界或「当前不可观测」，不得把未实现能力写成已可检测。"""
    curriculum = json.loads((PLUGIN_DIR / "curriculum.json").read_text(encoding="utf-8"))
    terms = curriculum["glossary"]["terms"]
    expected = {"首板", "接力", "反核", "一日游", "返场", "锁仓", "翘板", "地天板", "核按钮"}
    assert {item["term"] for item in terms} == expected
    for item in terms:
        assert item.get("evidence_boundary"), f"{item['term']} 缺证据边界"
        assert "当前不可观测" in item["evidence_boundary"], (
            f"{item['term']} 未声明当前不可观测——不得把未实现能力写成已可检测"
        )


def _curriculum_without_negation_fields(curriculum: dict) -> str:
    """★ 取出**不含否定式免责字段**的课程文本用于禁词扫描。

    课程里有大量「不出现『已了结』」「不出现『目标价』」这类**声明不做**的说明，
    它们的正向文本里就含禁词本身。整块 JSON 扫会把正当的否定说明误伤成违规
    （与后端 `boundary_notice` / `limitations` 是同一个坑）。做法 = 剥掉承载免责
    说明的字段再扫，并**另行逐字断言这些说明确实存在**——不允许靠删掉它们来通过。
    """
    stripped = {
        **curriculum,
        "lessons": [
            {key: value for key, value in lesson.items() if key != "acceptance"}
            for lesson in curriculum["lessons"]
        ],
        "glossary": {"note": "", "terms": [
            {key: value for key, value in item.items() if key != "evidence_boundary"}
            for item in curriculum["glossary"]["terms"]
        ]},
        "bucket_note": "",
    }
    return json.dumps(stripped, ensure_ascii=False)


def _new_lessons_blob(curriculum: dict) -> str:
    """★ 只取 L6–L10 的正文用于禁词扫描。

    为什么只扫新课：L0–L5 是**一期已冻结**文案，其中 L5 的 `principle`/`exercise`
    成段讨论「东财的三日上涨概率为什么不能当排序键」——这里「上涨概率」是**被否定
    的对象**，整块扫会把它自己误伤成违规（与后端 `boundary_notice` 同一个坑）。
    Y4-06 的验收对象是新增内容，因此扫描范围收敛到新课；旧课的越界风险另由
    `test_curriculum_does_not_assert_clearing_or_same_trade` 从「越界结论」角度守。
    """
    new_lessons = [
        # ★ `acceptance` 是**验收判据**，天然是否定式（「不出现 X」「不含 Y」），
        # 扫它等于让验收标准自己违规。正文（title/mechanism/principle/exercise/
        # boundary）才是课程真正教了什么。
        {key: value for key, value in lesson.items() if key != "acceptance"}
        for lesson in curriculum["lessons"]
        if lesson["level"] in NEW_LEVELS
    ]
    return json.dumps(new_lessons, ensure_ascii=False)


def test_curriculum_does_not_teach_trading_actions():
    """Y4-06：新增课程不含交易指导（买卖动作句式、目标价、仓位建议）。"""
    curriculum = json.loads((PLUGIN_DIR / "curriculum.json").read_text(encoding="utf-8"))
    scanned = _new_lessons_blob(curriculum)
    # 「买入/卖出」作为**名词**描述披露方向是允许的（如「买入榜」），
    # 因此这里只禁「动作句式」与预测类措辞。
    for banned in ("建议买入", "建议卖出", "可以买入", "可以卖出", "应当买入", "应当卖出"):
        assert banned not in scanned, f"课程出现交易指导：{banned}"
    for banned in ("目标价", "止盈", "止损位", "上涨概率", "必涨", "稳赚"):
        assert banned not in scanned, f"课程出现预测性措辞：{banned}"

    # ★ 反面确认：「目标价」「上涨概率」只是出现在**否定式说明**里，且说明本人在场。
    blob = json.dumps(curriculum, ensure_ascii=False)
    assert "不出现「目标价」「上涨概率」" in blob


def test_curriculum_new_lessons_all_carry_boundary_statements():
    """Y4-01：L6–L10 逐课都带 `boundary`（可见性边界），不能只有标题。"""
    curriculum = json.loads((PLUGIN_DIR / "curriculum.json").read_text(encoding="utf-8"))
    for lesson in curriculum["lessons"]:
        if lesson["level"] not in NEW_LEVELS:
            continue
        boundary = lesson["boundary"]
        assert len(boundary) >= 30, f"{lesson['level']} 的边界说明过短，形同占位"
        assert any(
            word in boundary for word in ("不", "不可观测", "不等于", "只")
        ), f"{lesson['level']} 的边界说明未写明「不做什么」"


def test_curriculum_does_not_assert_clearing_or_same_trade():
    """Y4-07：课程不得暗示清仓/了结——代码标签中性但文案越界同样是缺陷。"""
    curriculum = json.loads((PLUGIN_DIR / "curriculum.json").read_text(encoding="utf-8"))
    assert "已了结" not in _curriculum_without_negation_fields(curriculum), (
        "课程在非免责字段里断言了「已了结」"
    )
    # 反面确认：关于「不能说」的说明必须真的在（不能被删掉来通过检查）。
    blob = json.dumps(curriculum, ensure_ascii=False)
    assert "不出现「已了结」" in blob
    assert "同一营业部不等于同一账户" in blob


def test_bucket_note_and_glossary_note_are_negation_style_disclaimers():
    curriculum = json.loads((PLUGIN_DIR / "curriculum.json").read_text(encoding="utf-8"))
    assert "不计入游资观察命中" in curriculum["bucket_note"]
    assert "不是检测器" in curriculum["glossary"]["note"]
    assert "不做打板识别引擎" in curriculum["glossary"]["note"]


def test_curriculum_states_it_is_not_investment_advice():
    curriculum = json.loads((PLUGIN_DIR / "curriculum.json").read_text(encoding="utf-8"))
    blob = json.dumps(curriculum, ensure_ascii=False)
    assert "不构成投资建议" in blob
    assert "非实名账户" in blob


def test_curriculum_glossary_is_labelled_as_annotation_not_detector():
    """「首板/接力」这类词只能是市场语言注释，不能当成检测器。"""
    curriculum = json.loads((PLUGIN_DIR / "curriculum.json").read_text(encoding="utf-8"))
    glossary = curriculum["glossary"]
    assert "不是检测器" in glossary["note"]
    assert "不做打板识别引擎" in glossary["note"]
    assert {item["term"] for item in glossary["terms"]} == {
        "首板", "接力", "反核", "一日游", "返场", "锁仓", "翘板", "地天板", "核按钮",
    }


def test_curriculum_l2_records_the_verified_seat_name_correction():
    """L2 的教材素材锚定真实核验结论，避免有人把方案里的错名再抄回来。"""
    curriculum = json.loads((PLUGIN_DIR / "curriculum.json").read_text(encoding="utf-8"))
    l2 = next(item for item in curriculum["lessons"] if item["level"] == "L2")
    assert "查无此名" in l2["note"]
    assert "10026937" in l2["note"]
    assert "10456710" in l2["note"]


# —— D06：边界 ——


@pytest.mark.parametrize(
    "path",
    [
        "/youzi/today",
        "/youzi/watchlist",
        "/youzi/cffex",
        "/youzi/profile/10026937",
        "/youzi/seats/000592",
        "/youzi/cross-check",
    ],
)
def test_youzi_endpoints_409_when_plugin_not_enabled(client, path):
    test_client, headers = client
    assert test_client.get(path, headers=headers).status_code == 409


def test_youzi_watchlist_endpoint_returns_registry(client):
    test_client, headers = client
    test_client.post(f"/plugins/{PLUGIN_ID}/install", headers=headers)
    body = test_client.get("/youzi/watchlist", headers=headers).json()
    assert body["version"]
    assert len(body["seats"]) >= 8
    assert body["buckets"]
    assert "通道名" in body["description"]
    # 桶的说明必须写清「不计入游资命中」
    assert any("不计入游资命中" in item["note"] for item in body["buckets"])


def test_youzi_today_reports_unavailable_instead_of_empty_when_fetch_fails(client, monkeypatch):
    """取数失败必须 `available=False` + 原因，不能返回空 rows 冒充「今天没上榜」。"""
    import investment_steward_core.lhb_feed as lf

    test_client, headers = client
    test_client.post(f"/plugins/{PLUGIN_ID}/install", headers=headers)

    def _boom(day):
        raise lf.LhbError("模拟数据源不可用")

    monkeypatch.setattr(lf, "fetch_billboard", _boom)
    body = test_client.get("/youzi/today", headers=headers).json()
    assert body["available"] is False
    assert "模拟数据源不可用" in body["reason"]
    assert body["rows"] == []


def test_youzi_seats_rejects_bad_security_code(client):
    test_client, headers = client
    test_client.post(f"/plugins/{PLUGIN_ID}/install", headers=headers)
    response = test_client.get("/youzi/seats/not-a-code", headers=headers)
    assert response.status_code == 422


def test_youzi_cffex_degrades_with_reason(client, monkeypatch):
    """非交易日/收盘前拿不到中金所文件属正常，必须如实降级而非编造。"""
    from investment_steward_core import cffex_feed

    test_client, headers = client
    test_client.post(f"/plugins/{PLUGIN_ID}/install", headers=headers)

    monkeypatch.setattr(
        cffex_feed, "try_fetch_snapshot", lambda *a, **k: (None, "模拟：非交易日无当日文件")
    )
    body = test_client.get("/youzi/cffex", headers=headers).json()
    assert body["available"] is False
    assert "非交易日" in body["reason"]
    assert body["products"] == []


def test_youzi_cffex_success_path_returns_products_and_limitations(client, monkeypatch):
    """成功路径必须真正被覆盖：四品种 + 基差口径 + 局限清单。

    这条是补的——此前只有降级路径有测试，成功路径没被端到端跑过，
    等于「取到数时会不会 500」没人守。
    """
    from investment_steward_core import cffex_feed

    test_client, headers = client
    test_client.post(f"/plugins/{PLUGIN_ID}/install", headers=headers)

    contracts = cffex_feed.parse_index_contracts(
        (FIXTURES / "cffex_index_20260916.xml").read_text(encoding="utf-8")
    )
    monkeypatch.setattr(
        cffex_feed,
        "try_fetch_snapshot",
        lambda *a, **k: (cffex_feed.build_snapshot(contracts, trading_day="20260916"), ""),
    )
    body = test_client.get("/youzi/cffex", headers=headers).json()

    assert body["available"] is True
    assert body["trading_day"] == "20260916"
    assert [item["product_id"] for item in body["products"]] == ["IF", "IH", "IC", "IM"]
    # 未提供现货 → 基差未计算，不得凭空给数
    assert body["basis"] == {}
    assert "基差未计算" in body["basis_note"]
    assert body["limitations"]
    # 现货映射如实给出
    assert body["products"][0]["spot_index_code"] == "000300"
    # 期权/国债不得混进股指摘要
    blob = json.dumps(body, ensure_ascii=False)
    assert "期权" not in blob
    assert '"MO"' not in blob


# —— P4-E01：持仓/自选与今日上榜的交叉提示 ——


def test_cross_check_without_holdings_says_so_explicitly(client):
    """本机没有持仓/自选时，必须说「无从比对」，不能表现成「今日无人上榜」。"""
    test_client, headers = client
    test_client.post(f"/plugins/{PLUGIN_ID}/install", headers=headers)
    body = test_client.get("/youzi/cross-check", headers=headers).json()
    assert body["available"] is True
    assert body["checked"] == 0
    assert body["hits"] == []
    assert "无从比对" in body["note"]
    assert "今日无人上榜" in body["note"]  # 明确否认这种误读


def test_cross_check_matches_only_listed_holdings(client, monkeypatch):
    """命中的持仓必须是**确实上榜**的那只；未上榜的不出现在 hits 里。"""
    import investment_steward_core.lhb_feed as lf

    test_client, headers = client
    test_client.post(f"/plugins/{PLUGIN_ID}/install", headers=headers)
    for code, label in (("000592", "平潭发展"), ("600519", "贵州茅台")):
        test_client.post(
            "/holdings",
            json={"instrument": code, "label": label, "status": "watchlist", "strategy_note": ""},
            headers=headers,
        )

    monkeypatch.setattr(lf, "fetch_billboard", lambda day: _billboard())
    monkeypatch.setattr(lf, "fetch_seats", lambda day: _seats())
    body = test_client.get("/youzi/cross-check", headers=headers).json()

    assert body["available"] is True
    assert body["checked"] == 2
    codes = {item["security_code"] for item in body["hits"]}
    assert codes == {"000592"}


def test_cross_check_reports_watchlist_participation(client, monkeypatch):
    import investment_steward_core.lhb_feed as lf

    test_client, headers = client
    test_client.post(f"/plugins/{PLUGIN_ID}/install", headers=headers)
    test_client.post(
        "/holdings",
        json={
            "instrument": "000592",
            "label": "平潭发展",
            "status": "holding",
            "strategy_note": "",
        },
        headers=headers,
    )
    monkeypatch.setattr(lf, "fetch_billboard", lambda day: _billboard())
    monkeypatch.setattr(lf, "fetch_seats", lambda day: _seats())
    body = test_client.get("/youzi/cross-check", headers=headers).json()

    hit = body["hits"][0]
    assert hit["security_code"] == "000592"
    assert hit["watchlist_hit"] is True
    assert hit["statuses"] == ["holding"]
    assert hit["explanation"]


def test_cross_check_contains_no_action_words(client, monkeypatch):
    """E01 红线：只提示「今日上榜」，**不提示买**。"""
    import investment_steward_core.lhb_feed as lf

    test_client, headers = client
    test_client.post(f"/plugins/{PLUGIN_ID}/install", headers=headers)
    test_client.post(
        "/holdings",
        json={
            "instrument": "000592",
            "label": "平潭发展",
            "status": "holding",
            "strategy_note": "",
        },
        headers=headers,
    )
    monkeypatch.setattr(lf, "fetch_billboard", lambda day: _billboard())
    monkeypatch.setattr(lf, "fetch_seats", lambda day: _seats())
    body = test_client.get("/youzi/cross-check", headers=headers).json()

    blob = json.dumps(body, ensure_ascii=False)
    for banned in ("建议买", "建议卖", "买入建议", "卖出建议", "加仓", "减仓", "止盈", "止损"):
        assert banned not in blob, f"交叉提示不得出现动作词：{banned}"
    assert "不含任何买卖建议" in body["note"]


def test_cross_check_degrades_when_billboard_unavailable(client, monkeypatch):
    import investment_steward_core.lhb_feed as lf

    test_client, headers = client
    test_client.post(f"/plugins/{PLUGIN_ID}/install", headers=headers)
    test_client.post(
        "/holdings",
        json={
            "instrument": "000592",
            "label": "平潭发展",
            "status": "holding",
            "strategy_note": "",
        },
        headers=headers,
    )

    def _boom(day):
        raise lf.LhbError("模拟数据源不可用")

    monkeypatch.setattr(lf, "fetch_billboard", _boom)
    body = test_client.get("/youzi/cross-check", headers=headers).json()
    assert body["available"] is False
    assert "模拟数据源不可用" in body["reason"]
    assert body["hits"] == []


def test_youzi_seats_orders_each_side_by_its_amount(client, monkeypatch):
    """D03/C06: the endpoint ranks by side amount without changing reported NET."""
    from investment_steward_core import lhb_feed as lf

    test_client, headers = client
    assert test_client.post(f"/plugins/{PLUGIN_ID}/install", headers=headers).status_code == 200
    rows = _seats()
    monkeypatch.setattr(lf, "fetch_seats", lambda day: rows)
    response = test_client.get("/youzi/seats/000592?trading_day=20260916", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    for direction in (lf.DIRECTION_BUY, lf.DIRECTION_SELL):
        actual = body[direction]
        expected = sorted(
            [row for row in rows if row.security_code == "000592" and row.direction == direction],
            key=lambda row: (getattr(row, direction) is None, -(getattr(row, direction) or 0)),
        )
        assert actual
        assert [(row["operatedept_code"], row["net"]) for row in actual] == [
            (row.operatedept_code, row.net) for row in expected
        ]

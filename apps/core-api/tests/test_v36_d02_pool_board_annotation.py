"""D02（v36，2026-09-14 路线图 §6）：前端适配的服务端契约测试。

前端候选表要展示「所属板块分类」列与非低估象限告警标记。为避免两端口径漂移，
分类归属与违规判定**一律由服务端完成**，结果直接写在候选行上（`pool_board_name` /
`pool_board_judgment` / `pool_quadrant_violation` / `pool_override_argument`），
前端只做渲染。本文件锁定该契约：

1. `_annotate_pool_board_judgments` 的归属匹配与违规标注（含别名双向子串、无法归属不猜）；
2. 非风格主题（无 board_judgments）时不改动候选行 → 前端整列按 `—` 渲染；
3. 端到端：`/evidence/direction-research` 风格主题返回的 stock_pool 行带上述字段，
   且与顶层 `pool_quadrant_violations` 口径一致。

测试环境约定与 v36 主测试文件相同：pytest 从 `apps/core-api` 目录运行，
`--basetemp` 落在 OS 临时目录（沙箱 safe-delete 守卫）。
"""

from __future__ import annotations

import json
import textwrap
from typing import Any

from investment_steward_core import direction_research as direction_engine
from investment_steward_core import valuation_evidence as ve

# —— 与主测试文件同源的固定样例（本版报告 E7-E11 真实数值） ——

FIVE_BOARDS: dict[str, dict[str, float]] = {
    "银行Ⅱ": {"pb": 96.6, "pe": 97.9, "loss": 0.0},
    "房屋建设Ⅱ": {"pb": 3.4, "pe": 84.5, "loss": 0.375},
    "普钢": {"pb": 28.9, "pe": 52.2, "loss": 0.522},
    "房地产开发": {"pb": 10.3, "pe": 72.5, "loss": 0.725},
    "保险Ⅱ": {"pb": 37.4, "pe": 0.1, "loss": 0.0},
}

# 本版报告的 6 只地产链候选（sector 为模型写的别名，非板块全名）
DEVELOPER_POOL: list[dict[str, Any]] = [
    {"symbol": "600048", "name": "保利发展", "sector": "房地产开发", "business_link": "x",
     "profit_path": "y", "identity_status": "verified"},
    {"symbol": "001979", "name": "招商蛇口", "sector": "房地产开发", "business_link": "x",
     "profit_path": "y", "identity_status": "verified"},
    {"symbol": "600325", "name": "华发股份", "sector": "房地产开发", "business_link": "x",
     "profit_path": "y", "identity_status": "verified"},
    {"symbol": "000002", "name": "万科A", "sector": "房地产", "business_link": "x",
     "profit_path": "y", "identity_status": "verified"},
    {"symbol": "600383", "name": "金地集团", "sector": "房屋建设", "business_link": "x",
     "profit_path": "y", "identity_status": "verified"},
    {"symbol": "002285", "name": "世联行", "sector": "房屋建设", "business_link": "x",
     "profit_path": "y", "identity_status": "verified"},
]


def _judgment_map() -> dict[str, str]:
    """五个板块 → 系统参考分类（与主测试文件 `_judgment_map` 同源）。"""
    return {
        "银行Ⅱ": direction_engine.VALUATION_JUDGMENT_HIGH,
        "房屋建设Ⅱ": direction_engine.VALUATION_JUDGMENT_DEEP_FALL,
        "普钢": direction_engine.VALUATION_JUDGMENT_DEEP_FALL,
        "房地产开发": direction_engine.VALUATION_JUDGMENT_DEEP_FALL,
        "保险Ⅱ": direction_engine.VALUATION_JUDGMENT_CYCLE_TOP,
    }


def _violations_of(symbols: list[str], judgments: dict[str, str]) -> list[dict[str, Any]]:
    """借用真实闸门产出违规清单，保证测试与实现同源（不手写期望结构）。"""
    quality = direction_engine.validate_direction(
        {
            "title": "t", "executive_summary": "摘" * 160,
            "report": "".join(
                f"## {name}\n本节按事实→推理→不确定性展开，给出可核对的数据与边界条件。\n"
                for name in direction_engine.REQUIRED_DIRECTION_SECTIONS
            ),
            "core_judgments": [
                {"id": "J1", "text": "a", "kind": "industry", "confidence": "high"},
                {"id": "J2", "text": "b", "kind": "competitiveness", "confidence": "medium"},
                {"id": "J3", "text": "c", "kind": "sentiment", "confidence": "low"},
            ],
            "catalysts": ["x"], "risks": ["y"],
            "stock_pool": [c for c in DEVELOPER_POOL if c["symbol"] in set(symbols)],
            "data_gaps": ["缺口"], "next_verification": "z",
        },
        mode="standard", research_mode="evidence", evidence_ids={"E7"},
        board_judgments=judgments,
    )
    return quality["pool_quadrant_violations"]


# ---------------------------------------------------------------------------
# 单元：服务端归属与标注
# ---------------------------------------------------------------------------


def test_d02_alias_matching_assigns_board_and_label():
    """模型写的别名（房地产/房屋建设）应双向子串匹配到板块全名，并写入分类。"""
    helper = _extract_helper()

    pool = [dict(c) for c in DEVELOPER_POOL]
    helper(pool, _judgment_map(), [], [])

    by_symbol = {c["symbol"]: c for c in pool}
    assert by_symbol["000002"]["pool_board_name"] == "房地产开发"
    assert by_symbol["000002"]["pool_board_judgment"] == direction_engine.VALUATION_JUDGMENT_DEEP_FALL
    assert by_symbol["600383"]["pool_board_name"] == "房屋建设Ⅱ"
    # 精确命中优先：sector 就是板块全名
    assert by_symbol["600048"]["pool_board_name"] == "房地产开发"


def test_d02_unmatched_sector_stays_unannotated():
    """无法归属到任何已分类板块的候选不猜分类（不写 pool_board_* 字段）。"""
    helper = _extract_helper()
    pool = [{"symbol": "600519", "name": "贵州茅台", "sector": "白酒"}]
    helper(pool, _judgment_map(), [], [])
    assert "pool_board_name" not in pool[0]
    assert "pool_board_judgment" not in pool[0]


def test_d02_violation_and_override_flags_written_on_row():
    """违规候选行写 True，带推翻论证的候选写论证片段（二者互斥）。"""
    helper = _extract_helper()
    violations = _violations_of(["600048", "000002"], _judgment_map())
    assert len(violations) == 2
    pool = [dict(c) for c in DEVELOPER_POOL if c["symbol"] in {"600048", "000002", "600383"}]
    helper(
        pool,
        _judgment_map(),
        violations,
        [{"symbol": "600383", "override_argument": "房屋建设Ⅱ 的「深跌未反转」应推翻：..."}],
    )
    by_symbol = {c["symbol"]: c for c in pool}
    assert by_symbol["600048"]["pool_quadrant_violation"] is True
    assert by_symbol["000002"]["pool_quadrant_violation"] is True
    assert "pool_quadrant_violation" not in by_symbol["600383"]
    assert by_symbol["600383"]["pool_override_argument"].startswith("房屋建设Ⅱ")


def test_d02_style_gate_disabled_leaves_rows_untouched():
    """非风格主题（无 board_judgments / violations）→ 候选行零改动，前端整列按 `—` 渲染。"""
    helper = _extract_helper()
    pool = [dict(c) for c in DEVELOPER_POOL]
    snapshot = json.dumps(pool, ensure_ascii=False, sort_keys=True)
    helper(pool, {}, [], [])
    assert json.dumps(pool, ensure_ascii=False, sort_keys=True) == snapshot


def _extract_helper():
    """按 AST 提取生产源码中的 `_annotate_pool_board_judgments` 并编译执行。

    该 helper 是 app 工厂内的闭包（与 `_append_sector_valuation_evidence` 同级），
    无法直接 import。用 `inspect.getsource(create_app)` 取到工厂源码后，用 `ast`
    精确定位目标函数节点的 `lineno`/`end_lineno` 切片，再 `exec` 执行——
    执行的是**即将上线的同一份代码文本**，而非副本；这与仓库既有做法（对无法
    端到端触达的逻辑做真实源码契约断言）一致。用 AST 而非按行扫描，是因为
    函数签名跨多行，按行扫描的缩进启发式会截断签名。
    """
    import ast
    import inspect

    from investment_steward_core.api import app as api_app

    source = inspect.getsource(api_app.create_app)
    tree = ast.parse(textwrap.dedent(source))
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_annotate_pool_board_judgments"
    )
    segment = ast.get_source_segment(textwrap.dedent(source), target)
    assert segment, "未能从 create_app 源码中切出 _annotate_pool_board_judgments"
    namespace: dict[str, Any] = {"annotations": None}
    exec(compile(textwrap.dedent(segment), "<annotate>", "exec"), namespace)  # noqa: S102
    return namespace["_annotate_pool_board_judgments"]


# ---------------------------------------------------------------------------
# 端到端：风格主题响应契约
# ---------------------------------------------------------------------------


def _full_report() -> str:
    return "".join(
        f"## {name}\n本节按事实→推理→不确定性展开，给出可核对的数据与边界条件。\n"
        for name in direction_engine.REQUIRED_DIRECTION_SECTIONS
    )


def _payload_json() -> dict:
    return {
        "title": "被低估的板块：便宜≠低估",
        "executive_summary": (
            "三问作答：满足低估判定的板块——本期无；只是跌得久未反转的——房地产开发、"
            "房屋建设Ⅱ、普钢；已处高位的——银行Ⅱ，盈利周期顶——保险Ⅱ。"
            "最强证据：板块四维数值 [E7]。最大不确定性：分位样本覆盖。"
        ),
        "report": _full_report(),
        "core_judgments": [
            {"id": "J1", "text": "地产链是深跌未反转而非低估", "kind": "industry",
             "support_refs": ["E7"], "confidence": "high"},
            {"id": "J2", "text": "银行处自身历史高位", "kind": "competitiveness",
             "support_refs": ["E7"], "confidence": "high"},
            {"id": "J3", "text": "普钢盈利广度差", "kind": "sentiment",
             "support_refs": ["E7"], "confidence": "medium"},
        ],
        "catalysts": ["盈利端修复"], "risks": ["分位样本不足"],
        "stock_pool": [dict(c) for c in DEVELOPER_POOL],
        "data_gaps": ["候选池按象限约束应说明"], "next_verification": "跟踪地产销售与银行净息差",
    }


def test_d02_e2e_style_topic_pool_rows_carry_board_judgment(tmp_path, monkeypatch):
    """端到端：风格主题响应里候选行带所属板块分类 + 违规标记，与顶层口径一致。"""
    from fastapi.testclient import TestClient

    from investment_steward_core import model_client as mc
    from investment_steward_core.api import app as api_app
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings
    from investment_steward_core.credential_store import DbCredentialStore

    monkeypatch.setattr(api_app, "resolve_store", lambda db: DbCredentialStore(db))
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    client = TestClient(app)
    headers = {"X-Core-Session-Token": token}

    client.put("/credentials/model_api_key", json={"secret": "sk-test-1234567890"}, headers=headers)
    profile = client.post(
        "/model-profiles",
        json={"name": "方案A", "base_url": "https://api.a.com", "model": "model-a",
              "credential_ref": "model_api_key"},
        headers=headers,
    ).json()
    client.post(f"/model-profiles/{profile['profile_id']}/activate", headers=headers)

    def _fake(_base_url, payload, _key, _timeout):
        return {"choices": [{"message": {"content": json.dumps(_payload_json())}}]}

    monkeypatch.setattr(mc, "_post_json", _fake)

    client.post(
        "/plugins/official.cn-market-data/install", json={"source": "bundled"}, headers=headers
    )
    rows = []
    board_of_symbol: dict[str, str] = {}
    for board_idx, (name, spec) in enumerate(FIVE_BOARDS.items()):
        code = f"BK_{name}"
        loss_n = round(spec["loss"] * 20)
        for i in range(20):
            # 每板块用独立代码段，使打桩快照能按板块返回对应分位（真实链路口径）。
            symbol = f"{600000 + board_idx * 100 + i:06d}"
            board_of_symbol[symbol] = name
            rows.append(ve.CrossSectionRow(
                symbol=symbol, name=f"{name}股{i}",
                board_code=code, board_name=name,
                total_market_cap=1e11 - i * 1e9,
                pe_ttm=-3.0 if i < loss_n else 5.0,
                pb_mrq=0.5 + i * 0.05,
                ps_ttm=1.0,
            ))
    monkeypatch.setattr(ve, "latest_valuation_trade_date", lambda: "2026-09-11")
    monkeypatch.setattr(ve, "fetch_market_cross_section", lambda trade_date: list(rows))

    pb_pct = {name: spec["pb"] for name, spec in FIVE_BOARDS.items()}
    pe_pct = {name: spec["pe"] for name, spec in FIVE_BOARDS.items()}

    def _snap(symbol, **_kw):
        name = board_of_symbol.get(symbol, "银行Ⅱ")
        return ve.ValuationSnapshot(
            symbol=symbol, name=f"股{symbol}", board_code=f"BK_{name}", board_name=name,
            trade_date="2026-09-11", close_price=6.1, pe_ttm=5.0, pe_static=5.1, pb_mrq=0.5,
            ps_ttm=1.6, peg=None, pcf_ocf_ttm=None, total_market_cap=1e11,
            float_market_cap=5e10, total_shares=1e10,
            percentiles={"pe_ttm": pe_pct[name], "pb_mrq": pb_pct[name]},
            percentile_window_bars=1215,
            percentile_window_from="2021-09-13", peers=(), peer_count=18,
            peer_median={"pe_ttm": 6.0, "pb_mrq": 0.55}, peer_median_basis={"pe_ttm": 18, "pb_mrq": 18},
            peer_rank={"pe_ttm": 3, "pb_mrq": 2}, peer_rank_basis={"pe_ttm": 18, "pb_mrq": 18},
        )

    monkeypatch.setattr(api_app, "fetch_valuation", _snap)
    monkeypatch.setattr(ve, "fetch_valuation", _snap)
    monkeypatch.setattr(api_app, "fetch_cn_sector_list", lambda: [
        {"code": "new_jtys", "name": "交通运输", "member_count": 87, "change_pct": -0.04,
         "amount": 2.05e10, "leader_symbol": "601872", "leader_name": "招商轮船"},
    ])

    body = client.post(
        "/evidence/direction-research",
        json={"topic": "被低估的板块", "with_counter_check": False},
        headers=headers,
    ).json()

    assert body["ok"] is True, body
    pool = body["stock_pool"]
    assert pool, "打桩候选池不应为空"
    judgments = body["board_judgments"]
    assert judgments

    # 每只候选都被服务端归属到板块（6 只地产链 → 房地产开发 / 房屋建设Ⅱ）
    for row in pool:
        assert row.get("pool_board_name") in {"房地产开发", "房屋建设Ⅱ"}, row
        assert row["pool_board_judgment"] == direction_engine.VALUATION_JUDGMENT_DEEP_FALL
        assert row.get("pool_quadrant_violation") is True

    # 与顶层违规清单同口径（同一集合）
    top = {v["symbol"] for v in body["pool_quadrant_violations"]}
    rows_flag = {r["symbol"] for r in pool if r.get("pool_quadrant_violation")}
    assert top == rows_flag, (top, rows_flag)

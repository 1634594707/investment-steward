"""D01（v36，2026-09-14 路线图 §6）：低估判定框架端到端测试。

固定样例取自 2026-09-14 20:19 版「被低估的板块」报告的真实数值（E7-E11），
其中两条是用户对本版报告的判定，也是本框架必须复现的验收锚点：

| 板块 | 四维数值 | 系统参考分类 | 用户判定 |
|---|---|---|---|
| 银行Ⅱ | PB 分位 96.6% / PE 分位 97.9% | 高位 | 「银行其实在现在已经是处于高位了」 |
| 房屋建设Ⅱ | PB 分位 3.4% / 背离 81.1pp / 亏损面 37.5% | 深跌未反转 | 地产链「跌的久了」 |
| 普钢 | PB 分位 28.9% / 亏损面 52.2% | 深跌未反转 | — |
| 房地产开发 | PB 分位 10.3% / 亏损面 72.5% | 深跌未反转 | 「整个行业到现在还没有迎来反转」 |
| 保险Ⅱ | PB 分位 37.4% / PE 分位 0.1% | 盈利周期顶 | — |

断言要点（路线图 D01 验收）：分类总表输出五个标签与用户判定一致；候选池不含地产链个股；
执行摘要含三问回答；质量初筛对照清单包含地产与普钢。复合主题「低估的银行」回归：
横截面限定 + 分类标签 + v34 板块行情快照并存；既有 v33/v34/v35 与全量套件零回归。
"""
from __future__ import annotations

import json

import pytest

from investment_steward_core import direction_research as direction_engine
from investment_steward_core import valuation_evidence as ve

# ---------------------------------------------------------------------------
# 固定样例：本版报告五板块真实数值
# ---------------------------------------------------------------------------

# 板块 → (PB 分位中位数, PE 分位中位数, 亏损面)  —— E7-E11 原文数值
FIVE_BOARDS: dict[str, dict[str, float]] = {
    "银行Ⅱ": {"pb": 96.6, "pe": 97.9, "loss": 0.0},
    "房屋建设Ⅱ": {"pb": 3.4, "pe": 84.5, "loss": 0.375},
    "普钢": {"pb": 28.9, "pe": 52.2, "loss": 0.522},
    "房地产开发": {"pb": 10.3, "pe": 72.5, "loss": 0.725},
    "保险Ⅱ": {"pb": 37.4, "pe": 0.1, "loss": 0.0},
}
# 目标分类（用户判定 + 框架口径）
EXPECTED_LABELS = {
    "银行Ⅱ": direction_engine.VALUATION_JUDGMENT_HIGH,
    "房屋建设Ⅱ": direction_engine.VALUATION_JUDGMENT_DEEP_FALL,
    "普钢": direction_engine.VALUATION_JUDGMENT_DEEP_FALL,
    "房地产开发": direction_engine.VALUATION_JUDGMENT_DEEP_FALL,
    "保险Ⅱ": direction_engine.VALUATION_JUDGMENT_CYCLE_TOP,
}
# 地产链候选（本版报告候选池里来自地产链的 6 只）
DEVELOPER_POOL = [
    {"symbol_raw": "600048", "name": "保利发展", "sector": "房地产开发",
     "business_link": "住宅开发与销售", "profit_path": "结算毛利率与销售回款"},
    {"symbol_raw": "001979", "name": "招商蛇口", "sector": "房地产开发",
     "business_link": "园区与住宅开发", "profit_path": "结算规模与拿地成本"},
    {"symbol_raw": "600325", "name": "华发股份", "sector": "房地产开发",
     "business_link": "区域住宅开发", "profit_path": "结转收入与融资成本"},
    {"symbol_raw": "000002", "name": "万科A", "sector": "房地产",
     "business_link": "综合地产开发", "profit_path": "开发结算与经营服务收入"},
    {"symbol_raw": "600383", "name": "金地集团", "sector": "房屋建设Ⅱ",
     "business_link": "住宅开发与建设", "profit_path": "结算毛利率"},
    {"symbol_raw": "002285", "name": "世联行", "sector": "房屋建设Ⅱ",
     "business_link": "不动产代理服务", "profit_path": "代理费收入"},
]


def _input(name: str) -> direction_engine.BoardValuationInput:
    spec = FIVE_BOARDS[name]
    return direction_engine.BoardValuationInput(
        board_code=f"BK_{name}",
        board_name=name,
        pb_percentile_median=spec["pb"],
        pe_percentile_median=spec["pe"],
        loss_ratio=spec["loss"],
        pb_percentile_sample_count=5,
        loss_count=int(spec["loss"] * 20),
        loss_total=20,
        trade_date="2026-09-11",
    )


# ---------------------------------------------------------------------------
# A02 分类引擎：五板块固定样例
# ---------------------------------------------------------------------------


def test_a02_five_boards_match_user_judgment():
    """D01 核心：五板块分类与用户判定逐一致（含两条验收锚点）。"""
    actual = {}
    for name in FIVE_BOARDS:
        label, rationale = direction_engine.classify_board_valuation(_input(name))
        actual[name] = label
        assert label in direction_engine.VALUATION_JUDGMENT_LABELS, f"{name} 标签非法：{label}"
        assert "PB 分位" in rationale, f"{name} 依据未引用 PB 分位数值"
    assert actual == EXPECTED_LABELS, f"分类不一致：{json.dumps(actual, ensure_ascii=False)}"


def test_a02_bank_is_high_not_undervalued():
    """验收锚点 1：银行绝对 PB 最低（0.57）但 PB 分位 96.6% → 高位，不是被低估。"""
    label, rationale = direction_engine.classify_board_valuation(_input("银行Ⅱ"))
    assert label == direction_engine.VALUATION_JUDGMENT_HIGH
    assert "96.6%" in rationale
    assert "70" in rationale  # 阈值引用
    # 关键：不能因绝对 PB 低而误判为低估候选
    assert label != direction_engine.VALUATION_JUDGMENT_LOW


def test_a02_property_is_deep_fall_not_undervalued():
    """验收锚点 2：房地产开发 PB 分位 10.3% 但亏损面 72.5% → 深跌未反转，不是被低估。"""
    label, rationale = direction_engine.classify_board_valuation(_input("房地产开发"))
    assert label == direction_engine.VALUATION_JUDGMENT_DEEP_FALL
    assert "10.3%" in rationale
    assert "72.5%" in rationale
    assert label != direction_engine.VALUATION_JUDGMENT_LOW


def test_a02_housing_construction_goes_deep_fall_via_roe_gap():
    """房屋建设Ⅱ 亏损面 37.5% < 40% 不触发亏损面条件，靠背离 81.1pp 在第三级被捕获。"""
    board = _input("房屋建设Ⅱ")
    assert board.loss_ratio < direction_engine.VALUATION_LOSS_RATIO_DEEP_FALL_MIN
    assert board.roe_gap_pp is not None
    assert board.roe_gap_pp >= direction_engine.VALUATION_ROE_COLLAPSE_GAP_PP
    label, rationale = direction_engine.classify_board_valuation(board)
    assert label == direction_engine.VALUATION_JUDGMENT_DEEP_FALL
    assert "81.1pp" in rationale


def test_a02_insurance_is_cycle_top():
    """保险Ⅱ PE 分位 0.1% ≤ 10% 且 PB 分位 37.4% ≥ 30% → 盈利周期顶。"""
    label, rationale = direction_engine.classify_board_valuation(_input("保险Ⅱ"))
    assert label == direction_engine.VALUATION_JUDGMENT_CYCLE_TOP
    assert "0.1%" in rationale
    assert "37.4%" in rationale


def test_a02_judgment_order_high_beats_others():
    """判定顺序（M0 冻结）：多重命中时高位优先，深跌/低估不覆盖高位。"""
    board = direction_engine.BoardValuationInput(
        board_code="BK_X", board_name="X",
        pb_percentile_median=95.0, pe_percentile_median=5.0, loss_ratio=0.9,
    )
    label, _ = direction_engine.classify_board_valuation(board)
    assert label == direction_engine.VALUATION_JUDGMENT_HIGH


def test_a02_missing_dimension_yields_pending_with_names():
    """缺失维度如实为 None → 待定 + 注明缺哪维，不填默认值。"""
    board = direction_engine.BoardValuationInput(board_code="BK_Y", board_name="Y")
    label, rationale = direction_engine.classify_board_valuation(board)
    assert label == direction_engine.VALUATION_JUDGMENT_PENDING
    assert "自身便宜度" in rationale
    assert set(board.missing_dimensions) == {"自身便宜度", "盈利能力状态", "盈利广度", "价格位置"}


# ---------------------------------------------------------------------------
# A04 初筛闸门：对照清单
# ---------------------------------------------------------------------------


def _aggregate(board_name: str, board_code: str, pb_median: float, loss_ratio: float):
    """构造 SectorValuationAggregate。

    成员数取 40，使 37.5% / 52.2% / 72.5% 这类真实亏损面能**精确表示**
    （20 只时 37.5% 会被四舍五入到 40%，恰好卡在闸门阈值上，无法复现「过闸门」路径）。
    """
    members = 40
    loss_count = round(loss_ratio * members)
    return ve.SectorValuationAggregate(
        board_code=board_code,
        board_name=board_name,
        member_count=members,
        total_market_cap=1e11,
        pe_ttm_median=10.0,
        pe_ttm_positive_count=members - loss_count,
        pb_mrq_median=pb_median,
        pb_mrq_positive_count=members,
        ps_ttm_median=1.0,
        ps_ttm_positive_count=members,
        loss_count=loss_count,
        trade_date="2026-09-11",
    )


def test_a04_prescreen_excludes_high_loss_boards():
    """A04：亏损面 ≥ 40% 的板块不入深取名单，按 PB 中位数取前 3 进对照清单。"""
    aggregates = [
        _aggregate("银行Ⅱ", "BK0475", 0.57, 0.0),
        _aggregate("房地产开发", "BK0451", 0.72, 0.725),
        _aggregate("普钢", "BK0479", 0.85, 0.522),
        _aggregate("房屋建设Ⅱ", "BK0453", 1.10, 0.375),
        _aggregate("保险Ⅱ", "BK0474", 1.35, 0.0),
    ]
    result = ve.prescreen_sector_valuations(
        aggregates,
        loss_ratio_max=ve.VAL_PRESCREEN_LOSS_RATIO_MAX,
        contrast_top_n=ve.VAL_PRESCREEN_CONTRAST_TOP_N,
    )
    deep = {item.board_name for item in result.deep_fetch}
    contrast = {item.board_name for item in result.contrast}
    # 地产与普钢亏损面过闸未过 → 对照清单
    assert "房地产开发" in contrast, f"房地产开发应在对照清单，实际 deep={deep}"
    assert "普钢" in contrast
    assert "房地产开发" not in deep
    # 房屋建设 37.5% < 40% 过闸门（靠 ROE 背离在第三级捕获）
    assert "房屋建设Ⅱ" in deep, f"房屋建设应过闸门，实际 deep={deep}"
    assert result.excluded_note
    assert "对照清单" in result.excluded_note or "深取" in result.excluded_note


def test_a04_loss_ratio_none_kept_in_deep_fetch():
    """亏损面不可得时保守留深取（宁可多取，不静默排除）。"""
    agg = ve.SectorValuationAggregate(
        board_code="BK_Z", board_name="Z", member_count=0,
        total_market_cap=None,
        pe_ttm_median=None, pe_ttm_positive_count=0,
        pb_mrq_median=1.0, pb_mrq_positive_count=20,
        ps_ttm_median=None, ps_ttm_positive_count=0,
        loss_count=0, trade_date="2026-09-11",
    )
    assert ve.board_loss_ratio(agg) is None
    result = ve.prescreen_sector_valuations([agg])
    assert {item.board_code for item in result.deep_fetch} == {"BK_Z"}


# ---------------------------------------------------------------------------
# A03 榜单：分位排序
# ---------------------------------------------------------------------------


def test_a03_ranking_uses_percentile_not_absolute_pb():
    """A03：主键 = 代表股 PB 分位中位数；银行（绝对 PB 最低但分位 96.6%）排到最后。"""
    aggregates = [
        _aggregate("银行Ⅱ", "BK0475", 0.57, 0.0),
        _aggregate("房屋建设Ⅱ", "BK0453", 1.10, 0.375),
        _aggregate("普钢", "BK0479", 0.85, 0.522),
        _aggregate("房地产开发", "BK0451", 0.72, 0.725),
        _aggregate("保险Ⅱ", "BK0474", 1.35, 0.0),
    ]
    percentiles = {
        "BK0475": 96.6, "BK0453": 3.4, "BK0479": 28.9, "BK0451": 10.3, "BK0474": 37.4,
    }
    ranking = ve.rank_sector_valuations(aggregates, top_n=10, percentiles=percentiles)
    order = [entry.aggregate.board_name for entry in ranking]
    assert order == ["房屋建设Ⅱ", "房地产开发", "普钢", "保险Ⅱ", "银行Ⅱ"], order
    assert ranking[0].sort_percentile == 3.4
    # 银行不再出现在便宜度头部（v35 会因绝对 PB 0.57 排第 1）
    assert ranking[0].aggregate.board_name != "银行Ⅱ"


def test_a03_percentile_unavailable_excluded_from_ranking():
    """A03：分位不可得的板块不上榜（不排末尾冒充满足条件）。"""
    aggregates = [_aggregate("银行Ⅱ", "BK0475", 0.57, 0.0), _aggregate("普钢", "BK0479", 0.85, 0.1)]
    ranking = ve.rank_sector_valuations(
        aggregates, top_n=10, percentiles={"BK0475": 96.6, "BK0479": None}
    )
    assert [e.aggregate.board_name for e in ranking] == ["银行Ⅱ"]


def test_a03_v35_compat_path_unchanged():
    """A03：不传 percentiles 时沿用 v35 绝对 PB 路径（向后兼容）。"""
    aggregates = [_aggregate("银行Ⅱ", "BK0475", 0.57, 0.0), _aggregate("普钢", "BK0479", 0.85, 0.1)]
    ranking = ve.rank_sector_valuations(aggregates, top_n=10)
    assert [e.aggregate.board_name for e in ranking] == ["银行Ⅱ", "普钢"]


# ---------------------------------------------------------------------------
# C02 候选池象限约束（本版报告 6 只地产链候选）
# ---------------------------------------------------------------------------


def _judgment_map() -> dict[str, str]:
    return {name: label for name, label in EXPECTED_LABELS.items()}


def test_c02_six_developer_candidates_all_flagged_without_argument():
    """C02 验收：地产链 6 只候选在正文无论证时全部触发告警。"""
    report = "## 景气阶段\n板块估值分化，未对所有板块逐一给出推翻论证。\n"
    result = direction_engine.check_pool_quadrants(
        DEVELOPER_POOL, _judgment_map(), report=report
    )
    assert len(result["violations"]) == 6, json.dumps(result["violations"], ensure_ascii=False)
    assert all(v["label"] == direction_engine.VALUATION_JUDGMENT_DEEP_FALL for v in result["violations"])
    assert not result["override_hits"]


def test_c02_argument_covers_only_named_board():
    """C02 验收：给出「房地产开发」推翻论证后，该板块候选不误伤；房屋建设仍违规。"""
    report = (
        "## 景气阶段\n"
        "房地产开发 的深跌未反转分类应推翻：PB 分位 10.3% 但 PE 分位 72.5%，"
        "背离 62.2pp 超 40pp 阈值，属盈利端未跌透而非基本面塌陷 [E9]。\n"
    )
    result = direction_engine.check_pool_quadrants(
        DEVELOPER_POOL, _judgment_map(), report=report
    )
    flagged_boards = {v["board"] for v in result["violations"]}
    assert flagged_boards == {"房屋建设Ⅱ"}, flagged_boards
    assert len(result["violations"]) == 2
    assert len(result["override_hits"]) == 4


def test_c02_undervalued_quadrant_not_flagged():
    """C02：低估候选象限与待定象限的候选不违规（不误伤）。"""
    pool = [
        {"symbol_raw": "601318", "name": "中国平安", "sector": "保险Ⅱ"},
        {"symbol_raw": "600000", "name": "浦发银行", "sector": "未匹配板块"},
    ]
    judgments = dict(_judgment_map())
    judgments["未匹配板块"] = direction_engine.VALUATION_JUDGMENT_PENDING
    # 保险Ⅱ 在五板块样例里是盈利周期顶 → 会违规；此处改成低估候选验证不误伤
    judgments["保险Ⅱ"] = direction_engine.VALUATION_JUDGMENT_LOW
    result = direction_engine.check_pool_quadrants(pool, judgments, report="")
    assert not result["violations"], result["violations"]
    assert len(result["allowed"]) == 2


def test_c02_pool_quadrant_guide_in_prompt_and_gate_shared():
    """C02：提示词象限规则与闸门口径同源；产业主题不注入。"""
    assert direction_engine.POOL_QUADRANT_GUIDE in direction_engine.pool_quadrant_rule_text()
    style_user = direction_engine.build_messages(
        topic="被低估的板块", question="", mode="standard", research_mode="evidence",
        evidence_block="E1 ……", style_terms=["低估"],
    )[1]
    assert direction_engine.POOL_QUADRANT_GUIDE in style_user
    industry_user = direction_engine.build_messages(
        topic="海运板块", question="", mode="standard", research_mode="evidence",
        evidence_block="E1 ……", style_terms=[], theme_kind=direction_engine.THEME_KIND_INDUSTRY,
    )[1]
    assert direction_engine.POOL_QUADRANT_GUIDE not in industry_user


def test_c02_validate_direction_downgrades_on_multiple_violations():
    """C02：多项违规 → 质量告警 + missing_information + 降 needs_review。"""
    report = _full_report()
    payload = {
        "title": "被低估的板块", "executive_summary": "摘" * 160, "report": report,
        "core_judgments": [
            {"id": "J1", "text": "a", "kind": "industry", "confidence": "high"},
            {"id": "J2", "text": "b", "kind": "competitiveness", "confidence": "medium"},
            {"id": "J3", "text": "c", "kind": "sentiment", "confidence": "low"},
        ],
        "catalysts": ["x"], "risks": ["y"], "stock_pool": DEVELOPER_POOL,
        "data_gaps": ["缺口"], "next_verification": "z",
    }
    quality = direction_engine.validate_direction(
        payload, mode="standard", research_mode="evidence", evidence_ids={"E7"},
        board_judgments=_judgment_map(),
    )
    assert len(quality["pool_quadrant_violations"]) == 6
    assert any("象限" in w or "脱钩" in w for w in quality["quality_warnings"])
    assert any("推翻" in m for m in quality["missing_information"])
    assert quality["quality_status"] == "needs_review"


# ---------------------------------------------------------------------------
# C03 分类判定引用闸门
# ---------------------------------------------------------------------------


def test_c03_classification_claims_require_board_evidence():
    """C03：分类标签表述须引用含该板块四维数值的证据条目。"""
    board_evidence = {"银行Ⅱ": {"E7"}, "房地产开发": {"E9"}}
    # 无引用 → 违规
    report_bad = _full_report(overrides={"景气阶段": "银行Ⅱ 处于高位，房地产开发 属深跌未反转。"})
    claims = direction_engine.find_judgment_claims(report_bad)
    probs = direction_engine.judgment_refs_cover_boards(claims, board_evidence)
    assert probs and probs[0]["reason"] == "no_refs"
    # 正确引用 → 不误伤
    report_good = _full_report(
        overrides={"景气阶段": "银行Ⅱ 处于高位 [E7]，房地产开发 属深跌未反转 [E9]。"}
    )
    claims_good = direction_engine.find_judgment_claims(report_good)
    assert not direction_engine.judgment_refs_cover_boards(claims_good, board_evidence)


def test_c03_pending_is_not_a_claim_word():
    """C03：「待定」是缺维兜底标签，出现即已声明无结论，不构成断言。"""
    assert direction_engine.VALUATION_JUDGMENT_PENDING not in direction_engine.VALUATION_JUDGMENT_CLAIM_WORDS
    claims = direction_engine.find_judgment_claims(
        _full_report(overrides={"景气阶段": "普钢 分类为待定，证据未覆盖。"})
    )
    assert not claims


# ---------------------------------------------------------------------------
# C01 提示词立场（三问 + 分类总表 + 推翻条款 + 空候选）
# ---------------------------------------------------------------------------


def test_c01_caliber_declares_four_dimensions_and_rule_source():
    """C01：口径声明声明四维框架与分类规则来源，删除「不预设立场」。"""
    cal = direction_engine.style_caliber_declaration(["低估"])
    assert "不预设立场" not in cal
    assert "不直接下" not in cal
    for token in ("四维框架", "自身便宜度", "盈利能力状态", "盈利广度", "价格位置", "分类规则来源"):
        assert token in cal, token
    for label in direction_engine.VALUATION_JUDGMENT_ORDER:
        assert label in cal


def test_c01_prompt_demands_three_questions_table_and_override():
    """C01：提示词含三问作答、分类总表、[E#] 引用、推翻须数值论证、空候选合法。"""
    user = direction_engine.build_messages(
        topic="被低估的板块", question="哪些板块被低估", mode="standard",
        research_mode="evidence", evidence_block="E1 ……", style_terms=["低估"],
    )[1]
    for q in direction_engine.STYLE_SUMMARY_QUESTIONS:
        assert q.split("（")[0] in user, q
    assert "分类总表" in user
    for col in direction_engine.STYLE_TABLE_COLUMNS:
        assert col in user, col
    assert "[E#]" in user
    assert "可以推翻" in user and "逐维数值论证" in user
    assert direction_engine.STYLE_EMPTY_CANDIDATE_CLAUSE in user
    assert "最接近低估候选" in user
    # 口径声明前置
    assert "【低估判定口径（v36）】" in user


def test_c01_empty_candidate_is_legal_conclusion_path():
    """C01 验收：空候选路径有打桩测试——无可用风格词时口径为空、提示词不报错。"""
    assert direction_engine.style_caliber_declaration([]) == ""
    assert direction_engine.style_caliber_declaration(["不存在的风格词"]) == ""
    user = direction_engine.build_messages(
        topic="某某新概念", question="", mode="standard", research_mode="knowledge",
        evidence_block="", style_terms=[],
    )[1]
    assert "低估判定口径（v36）" not in user
    assert direction_engine.STYLE_EMPTY_CANDIDATE_CLAUSE


# ---------------------------------------------------------------------------
# B 组：趋势与板块聚合（非 IO 纯函数）
# ---------------------------------------------------------------------------


def test_b01_trend_three_metrics():
    """B01：250 日涨幅 / 52 周回撤 / 200 日均线位置。"""
    rows = [{"TRADE_DATE": f"2026-01-{(i % 28) + 1:02d}", "CLOSE_PRICE": float(300 - i)} for i in range(300)]
    trend = ve.compute_price_trend(rows)
    assert trend.bar_count == 300
    assert trend.ret_250d is not None
    assert trend.drawdown_52w is not None
    assert trend.above_ma200 is False  # 单边下跌序列在均线下方
    assert trend.ma200_gap_pct is not None and trend.ma200_gap_pct < 0


def test_b01_insufficient_samples_disclosed_as_none():
    """B01：样本不足字段如实 None 并披露根数；停牌不插值。"""
    short = [{"TRADE_DATE": "2026-01-01", "CLOSE_PRICE": 10.0} for _ in range(100)]
    trend = ve.compute_price_trend(short)
    assert trend.ret_250d is None
    assert trend.above_ma200 is None
    assert trend.bar_count == 100
    assert trend.note


def test_b03_board_trend_aggregate_excludes_missing():
    """B03：趋势缺失的代表股不进中位数；全缺失 → None（不假设 0）。"""
    from investment_steward_core.valuation_evidence import BoardTrendAggregate

    agg = BoardTrendAggregate(
        ret_250d_median=12.5, drawdown_52w_median=-18.0,
        above_ma200_count=3, above_ma200_total=5,
        trend_unavailable_symbols=("600006", "600007"),
    )
    assert agg.above_ma200_ratio == 0.6
    empty = BoardTrendAggregate(
        ret_250d_median=None, drawdown_52w_median=None,
        above_ma200_count=0, above_ma200_total=0,
    )
    assert empty.above_ma200_ratio is None


# ---------------------------------------------------------------------------
# 端到端：证据条目带分类 + 候选池约束（打桩，不联网）
# ---------------------------------------------------------------------------


def _full_report(overrides: dict[str, str] | None = None) -> str:
    lines = overrides or {}
    return "".join(
        f"## {name}\n{lines.get(name, '本节按事实→推理→不确定性展开，给出可核对的数据与边界条件。')}\n"
        for name in direction_engine.REQUIRED_DIRECTION_SECTIONS
    )


def test_e2e_evidence_entries_carry_judgment_and_prompt_asks_three_questions(tmp_path, monkeypatch):
    """端到端：sector_valuation 证据条目带系统参考分类；提示词要求三问；质量块留痕。"""
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

    # 模型 profile
    client.put("/credentials/model_api_key", json={"secret": "sk-test-1234567890"}, headers=headers)
    profile = client.post(
        "/model-profiles",
        json={"name": "方案A", "base_url": "https://api.a.com", "model": "model-a",
              "credential_ref": "model_api_key"},
        headers=headers,
    ).json()
    client.post(f"/model-profiles/{profile['profile_id']}/activate", headers=headers)

    captured: list[str] = []

    def _fake(_base_url, payload, _key, _timeout):
        captured.append(json.dumps(payload, ensure_ascii=False))
        return {"choices": [{"message": {"content": json.dumps(_payload_json())}}]}

    monkeypatch.setattr(mc, "_post_json", _fake)

    # 插件与打桩数据
    client.post(
        "/plugins/official.cn-market-data/install", json={"source": "bundled"}, headers=headers
    )
    rows = []
    for name, spec in FIVE_BOARDS.items():
        code = f"BK_{name}"
        loss_n = round(spec["loss"] * 20)
        for i in range(20):
            rows.append(ve.CrossSectionRow(
                symbol=f"60{i:04d}", name=f"{name}股{i}",
                board_code=code, board_name=name,
                total_market_cap=1e11 - i * 1e9,
                pe_ttm=-3.0 if i < loss_n else 5.0,
                pb_mrq=0.5 + i * 0.05,
                ps_ttm=1.0,
            ))
    monkeypatch.setattr(ve, "latest_valuation_trade_date", lambda: "2026-09-11")
    monkeypatch.setattr(ve, "fetch_market_cross_section", lambda trade_date: list(rows))

    pb_pct = {"银行Ⅱ": 96.6, "房屋建设Ⅱ": 3.4, "普钢": 28.9, "房地产开发": 10.3, "保险Ⅱ": 37.4}
    pe_pct = {"银行Ⅱ": 97.9, "房屋建设Ⅱ": 84.5, "普钢": 52.2, "房地产开发": 72.5, "保险Ⅱ": 0.1}

    def _snap(symbol, **_kw):
        return ve.ValuationSnapshot(
            symbol=symbol, name=f"股{symbol}", board_code="BK_银行Ⅱ", board_name="银行Ⅱ",
            trade_date="2026-09-11", close_price=6.1, pe_ttm=5.0, pe_static=5.1, pb_mrq=0.5,
            ps_ttm=1.6, peg=None, pcf_ocf_ttm=None, total_market_cap=1e11,
            float_market_cap=5e10, total_shares=1e10,
            percentiles={"pe_ttm": 97.9, "pb_mrq": 96.6}, percentile_window_bars=1215,
            percentile_window_from="2021-09-13", peers=(), peer_count=18,
            peer_median={"pe_ttm": 6.0, "pb_mrq": 0.55}, peer_median_basis={"pe_ttm": 18, "pb_mrq": 18},
            peer_rank={"pe_ttm": 3, "pb_mrq": 2}, peer_rank_basis={"pe_ttm": 18, "pb_mrq": 18},
        )

    monkeypatch.setattr(api_app, "fetch_valuation", _snap)
    monkeypatch.setattr(ve, "fetch_valuation", _snap)
    monkeypatch.setattr(api_app, "fetch_cn_sector_list", lambda: [
        {"code": "new_jtys", "name": "交通运输", "member_count": 87, "change_pct": -0.04,
         "amount": 2.05e10, "leader_symbol": "601872", "leader_name": "招商轮船"}
    ])

    body = client.post(
        "/evidence/direction-research",
        json={"topic": "被低估的板块", "with_counter_check": False},
        headers=headers,
    ).json()

    assert body["ok"] is True, body
    # 提示词含三问 + 分类总表 + 象限约束
    prompt = "\n".join(captured)
    assert "哪些板块满足低估判定" in prompt
    assert "分类总表" in prompt
    assert direction_engine.POOL_QUADRANT_GUIDE in prompt
    # 证据条目带分类
    valuation_items = [
        item for item in body["evidence_snapshot"] if item["source_type"] == "sector_valuation"
    ]
    assert valuation_items
    for item in valuation_items:
        if item.get("prescreen_excluded"):
            continue
        assert item["valuation_judgment"] in direction_engine.VALUATION_JUDGMENT_ORDER
        assert "PB 分位" in item["valuation_judgment_rationale"]
    # board_judgments 进响应与 trace（A02 留痕）
    assert isinstance(body["board_judgments"], dict) and body["board_judgments"]
    assert body["generation_trace"]["board_judgments"] == body["board_judgments"]
    # 分类引用闸门与候选池象限字段在响应中留痕
    assert "unsupported_judgment_claims" in body
    assert "pool_quadrant_violations" in body


def _payload_json() -> dict:
    return {
        "title": "被低估的板块：便宜≠低估",
        "executive_summary": (
            "三问作答：满足低估判定的板块——本期无；只是跌得久未反转的——房地产开发、"
            "房屋建设Ⅱ、普钢；已处高位的——银行Ⅱ，盈利周期顶——保险Ⅱ。"
            "最强证据：板块四维数值 [E7]。最大不确定性：分位样本覆盖。"
        ),
        "report": _full_report({
            "景气阶段": (
                "银行Ⅱ 处于高位 [E7]；房地产开发 属深跌未反转 [E9]；"
                "房屋建设Ⅱ 属深跌未反转 [E8]；普钢 属深跌未反转 [E10]；"
                "保险Ⅱ 属盈利周期顶 [E11]。本期无低估候选。"
            ),
        }),
        "core_judgments": [
            {"id": "J1", "text": "地产链是深跌未反转而非低估", "kind": "industry",
             "support_refs": ["E9"], "confidence": "high"},
            {"id": "J2", "text": "银行处自身历史高位", "kind": "competitiveness",
             "support_refs": ["E7"], "confidence": "high"},
            {"id": "J3", "text": "保险盈利端处极低位", "kind": "sentiment",
             "support_refs": ["E11"], "confidence": "medium"},
        ],
        "catalysts": ["盈利端修复"], "risks": ["分位样本不足"],
        "stock_pool": [], "data_gaps": ["候选池按象限约束为空"],
        "next_verification": "跟踪地产销售与银行净息差",
    }


def test_e2e_no_developer_stocks_in_pool_when_style_topic(tmp_path, monkeypatch):
    """端到端：候选池不含地产链个股（本版报告 6 只地产链候选在无推翻论证时应被标违规）。"""
    # 直接验证闸门：本版报告的 6 只地产链候选 + 无正文论证 → 全部违规
    quality = direction_engine.validate_direction(
        {
            "title": "t", "executive_summary": "摘" * 160, "report": _full_report(),
            "core_judgments": [
                {"id": "J1", "text": "a", "kind": "industry", "confidence": "high"},
                {"id": "J2", "text": "b", "kind": "competitiveness", "confidence": "medium"},
                {"id": "J3", "text": "c", "kind": "sentiment", "confidence": "low"},
            ],
            "catalysts": ["x"], "risks": ["y"], "stock_pool": DEVELOPER_POOL,
            "data_gaps": ["缺口"], "next_verification": "z",
        },
        mode="standard", research_mode="evidence", evidence_ids={"E7"},
        board_judgments=_judgment_map(),
    )
    flagged = {v["symbol"] for v in quality["pool_quadrant_violations"]}
    assert flagged == {"600048", "001979", "600325", "000002", "600383", "002285"}, flagged
    assert any("象限" in w or "脱钩" in w for w in quality["quality_warnings"])


# ---------------------------------------------------------------------------
# 复合主题「低估的银行」回归
# ---------------------------------------------------------------------------


def test_regression_composite_topic_limits_and_classifies(tmp_path, monkeypatch):
    """回归：复合主题「低估的银行」——横截面限定银行板块 + 分类标签 + v34 板块行情并存。"""
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

    captured: list[str] = []

    def _fake(_base_url, payload, _key, _timeout):
        captured.append(json.dumps(payload, ensure_ascii=False))
        return {"choices": [{"message": {"content": json.dumps(_payload_json())}}]}

    monkeypatch.setattr(mc, "_post_json", _fake)
    client.post("/plugins/official.cn-market-data/install", json={"source": "bundled"}, headers=headers)

    rows = []
    for i in range(20):
        loss_n = round(FIVE_BOARDS["银行Ⅱ"]["loss"] * 20)
        rows.append(ve.CrossSectionRow(
            symbol=f"60{i:04d}", name=f"银行股{i}",
            board_code="BK0475", board_name="银行",
            total_market_cap=1e11 - i * 1e9,
            pe_ttm=-3.0 if i < loss_n else 5.0,
            pb_mrq=0.5 + i * 0.05,
            ps_ttm=1.0,
        ))
    monkeypatch.setattr(ve, "latest_valuation_trade_date", lambda: "2026-09-11")
    monkeypatch.setattr(ve, "fetch_market_cross_section", lambda trade_date: list(rows))

    def _snap(symbol, **_kw):
        return ve.ValuationSnapshot(
            symbol=symbol, name="某银行", board_code="BK0475", board_name="银行",
            trade_date="2026-09-11", close_price=6.1, pe_ttm=5.0, pe_static=5.1, pb_mrq=0.5,
            ps_ttm=1.6, peg=None, pcf_ocf_ttm=None, total_market_cap=1e11,
            float_market_cap=5e10, total_shares=1e10,
            percentiles={"pe_ttm": 97.9, "pb_mrq": 96.6}, percentile_window_bars=1215,
            percentile_window_from="2021-09-13", peers=(), peer_count=18,
            peer_median={"pe_ttm": 6.0, "pb_mrq": 0.55}, peer_median_basis={"pe_ttm": 18, "pb_mrq": 18},
            peer_rank={"pe_ttm": 3, "pb_mrq": 2}, peer_rank_basis={"pe_ttm": 18, "pb_mrq": 18},
        )

    monkeypatch.setattr(api_app, "fetch_valuation", _snap)
    # B03 代表股分位附录内部走 valuation_evidence 模块级 fetch_valuation，须一并打桩。
    monkeypatch.setattr(ve, "fetch_valuation", _snap)
    monkeypatch.setattr(api_app, "fetch_cn_sector_list", lambda: [
        {"code": "new_jrhy", "name": "金融行业", "member_count": 100, "change_pct": 0.3,
         "amount": 5e10, "leader_symbol": "601398", "leader_name": "工商银行"}
    ])
    monkeypatch.setattr(api_app, "fetch_cn_sector_members",
                        lambda code, top=60: [{"symbol": "601398", "name": "工商银行",
                                               "change_pct": 0.2, "amount": 1e9}][:top])

    body = client.post(
        "/evidence/direction-research",
        json={"topic": "低估的银行", "with_counter_check": False},
        headers=headers,
    ).json()

    assert body["ok"] is True, body
    # A01：复合主题判定
    assert body["theme_kind"] == direction_engine.THEME_KIND_COMPOSITE
    assert body["style_terms"] == ["低估"]
    # A03：横截面限定在银行板块（证据条目只覆盖限定板块）
    valuation_items = [
        item for item in body["evidence_snapshot"] if item["source_type"] == "sector_valuation"
    ]
    assert valuation_items, "复合主题应有估值横截面证据"
    assert all(item["sector_code"] == "BK0475" for item in valuation_items if not item.get("prescreen_excluded"))
    # A02：分类标签在证据中（银行 PB 分位 96.6% → 高位）
    labels = {item["valuation_judgment"] for item in valuation_items if item.get("valuation_judgment")}
    assert direction_engine.VALUATION_JUDGMENT_HIGH in labels, labels
    # v34：板块行情快照与估值横截面两类证据并存
    kinds = {item["source_type"] for item in body["evidence_snapshot"]}
    assert {"sector_valuation", "sector_market"} <= kinds, kinds
    # 提示词含复合主题规则（产业限定）与象限约束
    prompt = "\n".join(captured)
    assert "复合主题" in prompt
    assert direction_engine.POOL_QUADRANT_GUIDE in prompt


@pytest.mark.parametrize("mode", ["quick", "standard", "deep"])
def test_regression_modes_still_validate(mode):
    """回归：三档模式的质量校验与分类引擎共存（不因 v36 改动报错）。"""
    quality = direction_engine.validate_direction(
        {
            "title": "t", "executive_summary": "摘" * 160, "report": _full_report(),
            "core_judgments": [
                {"id": "J1", "text": "a", "kind": "industry", "confidence": "high"},
                {"id": "J2", "text": "b", "kind": "competitiveness", "confidence": "medium"},
                {"id": "J3", "text": "c", "kind": "sentiment", "confidence": "low"},
            ],
            "catalysts": ["x"], "risks": ["y"], "stock_pool": [],
            "data_gaps": ["缺口"], "next_verification": "z",
        },
        mode=mode, research_mode="evidence", evidence_ids=set(),
        board_judgments=_judgment_map(),
        board_evidence={"银行Ⅱ": {"E7"}},
    )
    assert quality["mode"] == mode
    assert quality["quality_status"] in {"complete", "needs_review", "incomplete"}

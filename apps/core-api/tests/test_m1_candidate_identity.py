"""M1（2026-09-15 第二轮路线图）：候选必须带真实 6 位代码。

- B01：`normalize_pool` 用本次证据的「公司名 → 6 位代码」表回填（只补码，不改名，歧义不猜）；
        `validate_direction` 未显式给表时自动从 `evidence_index` 派生。
- B02/B03：提示词写死 symbol 契约 + 候选优先从证据代表股抽取（见 `M3_QUALITY_RULES` 22/23）。
- B04/B05：前端代码列与可扫描判定（见 `format.ts` / `direction.tsx` 的 vitest）。

回归基准 = `资料/方向研判-低估板块-2026-09-15.md` 的**真实证据文本**：
6 只候选的 symbol 全被模型写成公司名，但证据块里一直写着「中国通号(688009)」这类「名称(代码)」。
"""

from __future__ import annotations

from investment_steward_core import direction_research as direction_engine

# ---------------------------------------------------------------------------
# 真实证据文本（逐字取自 2026-09-15 22:55 样报 E8/E9/E10 的「代表股分位附录」）
# ---------------------------------------------------------------------------

_E8_RAIL = (
    "【系统参考分类】低估候选——；板块「轨交设备Ⅱ」代表股分位附录：取市值前列 5 只，其中 5 只分位可得；"
    "代表股 PB 分位中位数 4.6%；PE 分位中位数 8.9%；PB 分位最低 3 只："
    "中国通号(688009) PB 分位 1.2%（截至 2026-09-15）、"
    "时代电气(688187) PB 分位 4.5%（截至 2026-09-15）、"
    "中国铁物(000927) PB 分位 4.6%（截至 2026-09-15）；"
    "板块趋势：代表股 250 日涨幅中位数 -6.77%；站上 200 日均线 1/5 只。"
)
_E9_APPLIANCE = (
    "板块「白色家电」代表股分位附录：PB 分位最低 3 只："
    "格力电器(000651) PB 分位 4.9%（截至 2026-09-15）、"
    "TCL智家(002668) PB 分位 4.9%（截至 2026-09-15）、"
    "海尔智家(600690) PB 分位 6.1%（截至 2026-09-15）；站上 200 日均线 2/5 只。"
)
_E10_CONGLO = (
    "板块「综合Ⅱ」代表股分位附录：PB 分位最低 3 只："
    "中国宝安(000009) PB 分位 3.1%（截至 2026-09-15）、"
    "南京新百(600682) PB 分位 8.9%（截至 2026-09-15）、"
    "时代新材(600458) PB 分位 13.9%（截至 2026-09-15）；站上 200 日均线 0/5 只。"
)

# 样报里 6 只候选的原始写法：symbol 被模型写成了公司名（根因）
_SAMPLE_POOL_WITH_NAMES_AS_SYMBOL = [
    {"symbol": "中国通号", "name": "中国通号", "sector": "轨交设备Ⅱ"},
    {"symbol": "时代电气", "name": "时代电气", "sector": "轨交设备Ⅱ"},
    {"symbol": "格力电器", "name": "格力电器", "sector": "白色家电"},
    {"symbol": "海尔智家", "name": "海尔智家", "sector": "白色家电"},
    {"symbol": "中国宝安", "name": "中国宝安", "sector": "综合Ⅱ"},
    {"symbol": "时代新材", "name": "时代新材", "sector": "综合Ⅱ"},
]
_EXPECTED_CODES = {
    "中国通号": "688009",
    "时代电气": "688187",
    "格力电器": "000651",
    "海尔智家": "600690",
    "中国宝安": "000009",
    "时代新材": "600458",
}


# ---------------------------------------------------------------------------
# B01：名称索引构造
# ---------------------------------------------------------------------------

def test_build_name_symbol_index_from_evidence_texts():
    index = direction_engine.build_name_symbol_index(
        texts=[_E8_RAIL, _E9_APPLIANCE, _E10_CONGLO]
    )
    for name, code in _EXPECTED_CODES.items():
        assert index.get(name) == code, f"{name} 未命中：{index.get(name)}"
    # 同板块的其他代表股也一并进表（同样是真实存在的证据条目）
    assert index.get("中国铁物") == "000927"
    assert index.get("TCL智家") == "002668"


def test_build_name_symbol_index_merges_structured_and_text():
    index = direction_engine.build_name_symbol_index(
        structured_pairs=[("时代电气", "688187")],
        texts=["中国通号(688009) PB 分位 1.2%"],
    )
    assert index == {"时代电气": "688187", "中国通号": "688009"}


def test_build_name_symbol_index_drops_ambiguous():
    """同名对应多个不同代码 → 整体剔除（宁缺勿错）。"""
    index = direction_engine.build_name_symbol_index(
        texts=["同名公司(600000) 与 同名公司(000001)"]
    )
    assert index == {}


def test_build_name_symbol_index_only_stocks():
    """ETF/其他品种即使名称命中也不进表——候选身份只认 A 股股票。"""
    index = direction_engine.build_name_symbol_index(
        texts=["沪深300ETF华泰柏瑞(510300) 净值", "某公司(430047) 北交所"]
    )
    assert "沪深300ETF华泰柏瑞" not in index
    assert index.get("某公司") == "430047"  # 北交所是股票，进表


# ---------------------------------------------------------------------------
# B01：候选回填（路线图逐字验收）
# ---------------------------------------------------------------------------

def test_b01_backfills_code_and_marks_verified():
    """验收：输入 symbol=中国通号, name=中国通号 → symbol=688009, identity_status=verified。"""
    index = direction_engine.build_name_symbol_index(texts=[_E8_RAIL])
    pool, warnings = direction_engine.normalize_pool(
        [{"symbol": "中国通号", "name": "中国通号"}], name_index=index
    )
    assert len(pool) == 1
    candidate = pool[0]
    assert candidate["symbol"] == "688009"
    assert candidate["identity_status"] == "verified"
    assert candidate["symbol_backfilled"] is True
    assert candidate["symbol_backfill_source"] == direction_engine.BACKFILL_SOURCE_EVIDENCE
    # 只补码，不改公司名（原始写法原样保留）
    assert candidate["symbol_raw"] == "中国通号"
    assert candidate["name"] == "中国通号"
    assert any("回填" in warning for warning in warnings)


def test_b01_matches_when_name_missing_uses_symbol_raw():
    """模型只把公司名写进 symbol、name 为空时，用原始写法匹配并补上 name（同来自证据）。"""
    index = direction_engine.build_name_symbol_index(texts=[_E8_RAIL])
    pool, _ = direction_engine.normalize_pool([{"symbol": "时代电气"}], name_index=index)
    assert pool[0]["symbol"] == "688187"
    assert pool[0]["identity_status"] == "verified"
    assert pool[0]["name"] == "时代电气"


def test_b01_no_index_keeps_invalid():
    """没有可用证据表时不猜：保持 invalid。"""
    pool, warnings = direction_engine.normalize_pool(
        [{"symbol": "中国通号", "name": "中国通号"}]
    )
    assert pool[0]["symbol"] == ""
    assert pool[0]["identity_status"] == "invalid"
    assert pool[0]["symbol_backfilled"] is False
    assert not any("回填" in warning for warning in warnings)


def test_b01_unmatched_name_keeps_invalid():
    index = direction_engine.build_name_symbol_index(texts=[_E8_RAIL])
    pool, _ = direction_engine.normalize_pool(
        [{"symbol": "查无此股", "name": "查无此股"}], name_index=index
    )
    assert pool[0]["symbol"] == ""
    assert pool[0]["identity_status"] == "invalid"


def test_b01_does_not_override_valid_code():
    """模型已经给对 6 位码时不覆盖、不标回填。"""
    index = direction_engine.build_name_symbol_index(texts=[_E8_RAIL])
    pool, warnings = direction_engine.normalize_pool(
        [{"symbol": "688009", "name": "中国通号"}], name_index=index
    )
    assert pool[0]["symbol"] == "688009"
    assert pool[0]["symbol_backfilled"] is False
    assert not any("回填" in warning for warning in warnings)


def test_b03_backfilled_code_is_not_ambiguous_with_etf():
    """B03 契约：只有落在股票前缀段、且来自证据的代码才可能 verified。"""
    index = direction_engine.build_name_symbol_index(texts=_all_evidence())
    pool, _ = direction_engine.normalize_pool(_SAMPLE_POOL_WITH_NAMES_AS_SYMBOL, name_index=index)
    codes = {item["name"]: item["symbol"] for item in pool}
    assert codes == _EXPECTED_CODES
    assert all(item["identity_status"] == "verified" for item in pool)
    assert all(item["symbol_backfilled"] is True for item in pool)


def test_b01_sample_report_six_candidates_all_resolved():
    """回归基准：今晚样报 6 只候选全部（且只有它们）拿到真实代码。"""
    index = direction_engine.build_name_symbol_index(texts=_all_evidence())
    pool, _ = direction_engine.normalize_pool(_SAMPLE_POOL_WITH_NAMES_AS_SYMBOL, name_index=index)
    assert len(pool) == 6
    assert not [item for item in pool if item["identity_status"] != "verified"]


# ---------------------------------------------------------------------------
# B01：validate_direction 自动派生（端到端接线）
# ---------------------------------------------------------------------------

def _all_evidence() -> list[str]:
    return [_E8_RAIL, _E9_APPLIANCE, _E10_CONGLO]


def _payload_with(pool: list[dict]) -> dict:
    sections = "".join(
        f"## {name}\n本节按事实→推理→不确定性展开，给出可核对的数据与边界条件。\n"
        for name in direction_engine.REQUIRED_DIRECTION_SECTIONS
    )
    return {
        "title": "低估板块：便宜与低估的分野",
        "executive_summary": "方向判断：低分位不等于低估。最强证据：代表股 PB 分位与盈利广度。"
        "最大不确定性：盈利反转无中间数据，需排产/订单证据补齐后复核。",
        "report": sections,
        "core_judgments": [
            {"id": "J1", "text": "证据未覆盖景气修复", "kind": "industry", "confidence": "low"},
            {"id": "J2", "text": "轨交设备Ⅱ盈利广度好于白色家电", "kind": "competitiveness", "confidence": "low"},
            {"id": "J3", "text": "低位板块资金分歧仍大", "kind": "sentiment", "confidence": "low"},
        ],
        "catalysts": ["三季报窗口"],
        "risks": ["盈利继续下修"],
        "stock_pool": pool,
        "data_gaps": ["行业排产数据未接入"],
    }


def test_validate_direction_derives_name_index_from_evidence_index():
    """未显式传 name_index 时，从 evidence_index 条目正文抽表回填（app.py 的实际调用形态）。"""
    evidence_index = {f"E{i}": text for i, text in enumerate(_all_evidence(), start=8)}
    quality = direction_engine.validate_direction(
        _payload_with(_SAMPLE_POOL_WITH_NAMES_AS_SYMBOL),
        mode="standard",
        research_mode="evidence",
        evidence_ids=set(evidence_index),
        evidence_index=evidence_index,
    )
    codes = {item["name"]: item["symbol"] for item in quality["pool"]}
    assert codes == _EXPECTED_CODES
    assert quality["valid_pool_count"] == 6


def test_validate_direction_explicit_name_index_wins():
    """显式传入的表优先，不被 evidence_index 派生结果覆盖。"""
    quality = direction_engine.validate_direction(
        _payload_with([{"symbol": "中国通号", "name": "中国通号"}]),
        mode="standard",
        research_mode="knowledge",
        evidence_ids=set(),
        evidence_index={"E1": "证据里没有这家公司"},
        name_index={"中国通号": "688009"},
    )
    assert quality["pool"][0]["symbol"] == "688009"
    assert quality["pool"][0]["identity_status"] == "verified"


def test_validate_direction_without_index_keeps_candidate_invalid():
    """两侧都没有表时候选仍为无效码，不被「恰好通过」——闸门口径不放松。"""
    quality = direction_engine.validate_direction(
        _payload_with([{"symbol": "中国通号", "name": "中国通号"}]),
        mode="standard",
        research_mode="knowledge",
        evidence_ids=set(),
    )
    assert quality["pool"][0]["identity_status"] == "invalid"
    assert quality["valid_pool_count"] == 0

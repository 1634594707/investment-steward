"""v2.2 研报可读性修复测试（2026-09-10 用户反馈）。

用户实测（000060 中金岭南）暴露三类问题，本文件逐条守卫：

1. **数字精度**：报告里出现 `MA20 6.648999999999999`、`DIF -0.006371615255064356`、
   `KDJ_K 31.1658824632855` 这类机器精度裸浮点，无法阅读与判断。根因是证据文本把
   `snapshot()['indicators']` 直接 `json.dumps`、S4 行项目直接 `f"{k}={v}"`。
   → 修复：证据层统一展示精度（价 2 位 / MACD 4 位 / KDJ·RSI 2 位 / 量整数 / 金额按亿·万换单位）。
   ★ 红线：**绝不改动 `tactics.py` 的 `snapshot()`/评分口径**——战法雷达排序依赖全精度。

2. **验证时点粒度**：5 条验证点全部为「2026-10 前」（提示词示例被原样照抄，只有月份）。
   → 修复：提示词要求具体到日或锚定事件；质量闸门对「全部相同」与「只写到月份」如实告警。

3. **结构重复**：正文「综合判断」已含情景/价位/验证点，页面下方又各重复一张表。
   → 修复：提示词改为「综合判断只给一段结论综述，不重复罗列」。

行情/新闻/财报/估值取数一律 monkeypatch，**绝不真实联网**。
"""

from __future__ import annotations

import json
import re

import pytest

from conftest import client as client_fixture  # noqa: F401  确保 fixture 可用

from test_stock_research_tools import (  # noqa: F401
    _file_client,
    _mock_sources,
    _setup_active_profile,
)

from investment_steward_core import prompting, report_quality
from investment_steward_core.api.app import display_amount, display_indicators

# 机器精度裸浮点：7 位及以上小数（展示精度最高为 MACD 的 4 位）。
_MACHINE_FLOAT = re.compile(r"\d\.\d{7,}")


# ---------------------------------------------------------------------------
# 1. 数字精度
# ---------------------------------------------------------------------------

def test_display_indicators_rounds_per_field():
    """指标按字段各自精度收口——正是用户报告里那批数字。"""
    raw = {
        "close": 6.61,
        "ma20": 6.648999999999999,
        "macd_dif": -0.006371615255064356,
        "macd_dea": 0.004363009317194789,
        "kdj_k": 31.1658824632855,
        "rsi14": 49.01646649238917,
        "volume": 858917.0,
        "volume_ma20": 1109837.7,
    }
    out = display_indicators(raw)
    assert out["ma20"] == 6.65
    assert out["macd_dif"] == -0.0064
    assert out["macd_dea"] == 0.0044
    assert out["kdj_k"] == 31.17
    assert out["rsi14"] == 49.02
    assert out["volume"] == 858917          # 成交量取整，不带小数
    assert out["volume_ma20"] == 1109838
    # 原始快照不被改动（红线：评分口径用全精度）
    assert raw["ma20"] == 6.648999999999999


def test_display_indicators_keeps_unparsable_values():
    """缺失/非数值如实保留，不编造 0。"""
    out = display_indicators({"ma20": None, "volume": "未知", "rsi14": 12.3456789})
    assert out["ma20"] is None
    assert out["volume"] == "未知"
    assert out["rsi14"] == 12.35


def test_display_amount_scales_units_for_numeric_strings():
    """★ 真实契约是数值字符串（`financial_evidence._extract_items` 存 `str(Decimal)`）。"""
    assert display_amount("42194101356.98") == "421.94亿元"
    assert display_amount("1142015208.13") == "11.42亿元"
    assert display_amount("-298404800.5") == "-2.98亿元"
    assert display_amount("14533518527.26") == "145.34亿元"
    assert display_amount("570895.04") == "57.09万元"
    assert display_amount("32344588.37") == "3234.46万元"
    assert display_amount("0.26") == "0.26"          # 每股收益等小额保留原值
    assert display_amount(1234.5) == "1234.50"


def test_display_amount_passes_through_unparsable():
    assert display_amount("") == ""
    assert display_amount("abc") == "abc"
    assert display_amount(None) == "None"


def test_evidence_text_carries_no_machine_precision_floats(client, tmp_path, monkeypatch):
    """端到端：进模型的证据文本里不得出现机器精度浮点，S4 金额须已换单位。"""
    from investment_steward_core import model_client as mc

    test_client, headers = _file_client(tmp_path, monkeypatch)
    assert test_client.post("/plugins/official.cn-market-data/install", headers=headers).status_code == 200
    _setup_active_profile(test_client, headers)
    _mock_sources(monkeypatch)

    captured: dict[str, object] = {}
    payloads: list[object] = []

    def fake_post(base_url, json_payload, api_key, timeout):
        # v27：普通研报默认追加一次轻量反方检查 → 本次会话有两次模型调用。
        # 本测试只关心**首次**（证据 → 报告）的 payload，因此按序留档而不是覆盖。
        payloads.append(json_payload)
        captured["payload"] = payloads[0]
        return {"choices": [{"message": {"content": json.dumps({
            "title": "甲股：缓涨",
            "executive_summary": "趋势偏多：技术面缓涨量价配合；基本面营收净利稳健增长；最大反证是估值已不便宜。",
            "report": "### 技术面\n股价沿 MA20 缓涨上行，量能温和放大，结构完好 [S1]\n### 消息面\n公司公告中标大单，市场关注度明显上升 [S2]\n### 基本面\n营收净利稳健增长，经营现金流为正 [S4]\n### 综合判断\n趋势向好但估值已不便宜，总体中性看待，跟踪量能确认。",
            "citations": ["S1", "S2", "S4"],
            "confidence": "medium",
        }, ensure_ascii=False)}}]}

    monkeypatch.setattr(mc, "_post_json", fake_post)

    body = test_client.post(
        "/evidence/stock-research-report", json={"symbol": "600001"}, headers=headers
    ).json()
    assert body["ok"] is True, body

    prompt = json.dumps(captured["payload"], ensure_ascii=False)
    # 指标仍在证据里（没被顺手删掉）
    assert "ma20" in prompt and "macd_dif" in prompt
    # 但不再有 7 位以上小数的裸浮点
    hit = _MACHINE_FLOAT.search(prompt)
    assert hit is None, f"证据文本仍有机器精度浮点：{hit.group() if hit else ''}"
    # S4 金额已按亿换算（mock revenue=1000000000 元 → 10.00 亿元），原始长整数串消失
    assert "10.00亿元" in prompt
    assert "1000000000" not in prompt

    # v27：第二次调用是反方检查（只审结论，不该再重复灌一遍完整证据）
    assert len(payloads) == 2
    counter_prompt = json.dumps(payloads[1], ensure_ascii=False)
    assert "反方审查员" in counter_prompt
    assert "strongest_counter" in counter_prompt


# ---------------------------------------------------------------------------
# 2. 验证时点粒度
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-10前", True),
        ("2026-10 前", True),
        ("2026年10月", True),
        ("2026-10-31前", False),          # 具体到日
        ("2026年10月31日", False),         # 具体到日
        ("三季报披露后", False),            # 锚定事件
        ("2026年三季报披露后", False),       # 年份+事件锚定，不是「年+月」
        ("下次董事会决议公告后", False),
        ("", False),
    ],
)
def test_month_only_verify_by_classification(value, expected):
    assert report_quality.month_only_verify_by(value) is expected


def _report_payload(**extra):
    base = {
        "report": "### 技术面\nx\n### 消息面\ny\n### 基本面\nz\n### 综合判断\nw",
        "citations": ["S1"],
        "confidence": "medium",
        "valuation": {"verdict": "合理"},
    }
    base.update(extra)
    return base


def test_quality_warns_on_duplicate_and_coarse_verify_by():
    """★ 用户实测的确切情形：5 条验证点全是「2026-10前」。"""
    watchpoints = [
        {"signal": f"信号{i}", "verify_by": "2026-10前", "expected_if_true": "x"} for i in range(5)
    ]
    result = report_quality.validate_report(
        _report_payload(watchpoints=watchpoints), allowed={"S1"}
    )
    joined = "\n".join(result["warnings"])
    assert "验证时点完全相同" in joined
    assert "只写到月份" in joined


def test_quality_does_not_warn_when_verify_by_is_specific():
    watchpoints = [
        {"signal": "量能", "verify_by": "2026-10-31前", "expected_if_true": "x"},
        {"signal": "现金流转正", "verify_by": "2026年三季报披露后", "expected_if_true": "y"},
        {"signal": "事故影响", "verify_by": "下次董事会决议公告后", "expected_if_true": "z"},
    ]
    result = report_quality.validate_report(
        _report_payload(watchpoints=watchpoints), allowed={"S1"}
    )
    joined = "\n".join(result["warnings"])
    assert "验证时点完全相同" not in joined
    assert "只写到月份" not in joined


def test_quality_warns_on_empty_verify_by_and_missing_watchpoints():
    empty = report_quality.validate_report(
        _report_payload(watchpoints=[{"signal": "a", "verify_by": "", "expected_if_true": "x"}]),
        allowed={"S1"},
    )
    assert "验证时点为空" in "\n".join(empty["warnings"])

    missing = report_quality.validate_report(_report_payload(), allowed={"S1"})
    assert "未输出结构化验证点清单" in "\n".join(missing["warnings"])


# ---------------------------------------------------------------------------
# 3. 提示词模板（版本与措辞守卫，防回退）
# ---------------------------------------------------------------------------

def test_prompt_version_bumped():
    """提示词版本随结构升级递增：v30→2.7（摘要四句/aspects 三层），v32→2.8（驾驶舱/正常化估值），
    v39→2.9（Q16/Q17/Q18 盈利拆解 / 现金流质量口径 / 估值分化与可比样本）。"""
    assert prompting.STOCK_REPORT_PROMPT_VERSION == "2.11"


def test_prompt_no_longer_leaks_concrete_example_date():
    """★ 根因守卫：示例里的具体日期会被模型原样照抄，schema 与铁律都不得再出现。"""
    assert "2026-10 前" not in prompting.STOCK_REPORT_SCHEMA_TEXT
    assert "2026-10 前" not in prompting.STOCK_REPORT_RULES_TEXT
    assert "2026-10" not in prompting.STOCK_REPORT_SCHEMA_TEXT
    assert "2026-10" not in prompting.STOCK_REPORT_RULES_TEXT
    assert "YYYY-MM-DD" in prompting.STOCK_REPORT_SCHEMA_TEXT   # 只给格式，不给可直接抄的值


def test_prompt_requires_specific_verify_by():
    rules = prompting.STOCK_REPORT_RULES_TEXT
    assert "7. 验证点清单" in rules
    assert "禁止只写到月份" in rules
    assert "禁止所有验证点共用同一个时点" in rules


def test_prompt_aligns_signal_timescale_with_confirmation():
    """★ v27 验收守卫：单日／少数几根 K 线的触发不得被写成「中长期反转」。

    S1 证据文本里确实带了 `age_bars` 与 `confirmation_rule`（见 tactics.snapshot），
    所以铁律可以直接引用这两个字段；此处一并锁住措辞，防止后续被删回退。
    """
    rules = prompting.STOCK_REPORT_RULES_TEXT
    assert "10. 技术面结论的时间尺度" in rules
    assert "age_bars" in rules
    assert "confirmation_rule" in rules
    assert "中长期反转" in rules
    assert "禁止把单日或少数几根 K 线的触发" in rules


def test_prompt_deduplicates_comprehensive_judgement():
    """综合判断只给一段综述，不再重复罗列情景/价位/验证点（结构性去重）。"""
    schema = prompting.STOCK_REPORT_SCHEMA_TEXT
    assert "不要重复罗列" in schema
    assert "多空情景**：" not in schema   # 旧的「必须包含三部分」清单已移除


def test_prompt_clarifies_display_precision():
    assert "沿用来源精度" in prompting.STOCK_REPORT_RULES_TEXT

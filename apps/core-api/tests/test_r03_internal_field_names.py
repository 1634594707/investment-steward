"""R03（2026-09-18 排版与研报呈现一致性路线图）：S4 正文不露内部字段名。

样报 B 暴露：正文出现「basic_eps 0.26元/股」「total_debt 323.09亿元」——内核把规范英文键
直接写进模型可见的 S4 文本（app.py:5878），模型照抄。修法：S4 呈现层改中文行项目名。
"""

from __future__ import annotations

from types import SimpleNamespace

from investment_steward_core.api import app as api_app
from investment_steward_core.financial_evidence import ITEM_LABEL_CN, STATEMENT_LABEL_CN


def test_item_label_cn_covers_leaked_keys_without_underscores():
    for key in ("basic_eps", "total_debt", "revenue", "net_profit"):
        assert key in ITEM_LABEL_CN
        label = ITEM_LABEL_CN[key]
        assert "_" not in label and label.isascii() is False


def test_s4_lines_use_chinese_labels_and_hide_snake_keys():
    rows = [
        SimpleNamespace(statement_type="income", period_end="2026-06-30",
                        items={"revenue": "42194000000", "basic_eps": "0.26"}),
        SimpleNamespace(statement_type="balance", period_end="2026-06-30",
                        items={"total_debt": "32309000000", "total_assets": "53530000000"}),
        SimpleNamespace(statement_type="cash_flow", period_end="2026-06-30",
                        items={"operating_cash_flow": "-298000000"}),
    ]
    text = "\n".join(api_app._s4_financial_lines(rows))
    # 内部下划线键不再外露。
    for leaked in ("basic_eps", "total_debt", "cash_flow", "operating_cash_flow", "total_assets"):
        assert leaked not in text, f"内部字段名 {leaked} 仍出现在 S4 文本"
    # 人话中文标签出现。
    assert STATEMENT_LABEL_CN["cash_flow"] in text      # 现金流量表
    assert ITEM_LABEL_CN["basic_eps"] in text           # 基本每股收益
    assert ITEM_LABEL_CN["total_debt"] in text           # 负债合计
    assert "421.94亿元" in text                          # 金额换算不变（逐位可复算）


def test_s4_lines_fall_back_to_raw_key_for_unknown_items():
    rows = [SimpleNamespace(statement_type="income", period_end="2026-06-30",
                            items={"weird_item": "1"})]
    text = api_app._s4_financial_lines(rows)[0]
    # 未收录键回退原键（宁可露陌生键也不臆造中文），但仍带金额换算。
    assert "weird_item=" in text

"""v38：方向研判追问链路的单元测试。

覆盖：
1. `direction_followup_messages` 骨架装配（J# 判断清单 / 板块分类 / 验证动作 / 缺口）；
2. 与个股 `followup_messages` 共用同一 system 提示词与归一化契约；
3. `normalize_followup` 对方向编号（J#）的闸门行为——自造编号剔除、effect 越界归 cannot_judge；
4. 端点层：kind=direction 放行、kind=unknown 拒绝（读源码断言，不启服务）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from investment_steward_core import analysis_followup
from investment_steward_core.analysis_followup import (
    FOLLOWUP_PROMPT_VERSION,
    direction_followup_messages,
    followup_messages,
    normalize_followup,
)

ROOT = Path(__file__).resolve().parents[3]


def _direction_base() -> dict:
    """一份最小方向研判落库记录（字段名取自 app.py 的 response 契约）。"""
    return {
        "kind": "direction",
        "subject": "被低估的板块，美国加息后有希望的板块",
        "topic": "被低估的板块，美国加息后有希望的板块",
        "title": "美国加息见顶预期下的低估板块筛选——轨交设备与综合Ⅱ的左侧窗口",
        "research_mode": "evidence",
        "mode_label": "标准",
        "executive_summary": "本期低估候选为轨交设备Ⅱ与综合Ⅱ；银行Ⅱ已处高位。",
        "core_judgments": [
            {
                "id": "J1",
                "kind": "industry",
                "kind_label": "产业景气",
                "text": "轨交设备Ⅱ与综合Ⅱ满足四维低估判定。",
                "support_refs": ["E9", "E10"],
                "confidence": "high",
                "missing": ["盈利周期未验证"],
            },
            {
                "id": "J6",
                "kind": "industry",
                "kind_label": "产业景气",
                "text": "「美国加息后」宏观前提未被证实。",
                "support_refs": ["E2", "E5"],
                "confidence": "low",
                "missing": ["联邦基金利率路径"],
            },
        ],
        "board_judgments": {"轨交设备Ⅱ": "低估候选", "银行Ⅱ": "高位", "饰品": "深跌未反转"},
        "next_verification": "第一优先级：看美国 CPI 实际值；第二优先级：三季报窗口。",
        "data_gaps": ["未接入联邦基金利率与点阵图", "候选公司资本开支未接入"],
        "evidence_snapshot": [{"evidence_id": f"E{i}"} for i in range(1, 15)],
    }


# --------------------------------------------------------------------------
# 1. 骨架装配
# --------------------------------------------------------------------------


def test_direction_followup_includes_judgment_ids() -> None:
    system_text, user_text = direction_followup_messages(
        base_report=_direction_base(), question="美国 CPI 已公布 3.4%，J6 是否成立？",
        supplements=[], facts=[],
    )
    assert "J1" in user_text
    assert "J6" in user_text
    assert "轨交设备Ⅱ与综合Ⅱ满足四维低估判定" in user_text
    assert "「美国加息后」宏观前提未被证实" in user_text


def test_direction_followup_includes_board_judgments_and_next_verification() -> None:
    _, user_text = direction_followup_messages(
        base_report=_direction_base(), question="q", supplements=[], facts=[],
    )
    assert "轨交设备Ⅱ：低估候选" in user_text
    assert "银行Ⅱ：高位" in user_text
    assert "下一验证动作：第一优先级" in user_text
    assert "未接入联邦基金利率与点阵图" in user_text


def test_direction_followup_declares_no_board_when_style_theme() -> None:
    base = _direction_base()
    base["board_judgments"] = {}
    _, user_text = direction_followup_messages(
        base_report=base, question="q", supplements=[], facts=[],
    )
    assert "本期无板块四维分类" in user_text


def test_direction_followup_reuses_same_system_prompt() -> None:
    """方向与个股共用同一 system 提示词——契约一致，不新增一套纪律。"""
    stock_base = {
        "kind": "stock", "symbol": "688009", "title": "t",
        "executive_summary": "s", "claims": [{"claim_id": "C1", "text": "x"}],
    }
    dir_system, _ = direction_followup_messages(
        base_report=_direction_base(), question="q", supplements=[], facts=[],
    )
    stock_system, _ = followup_messages(
        base_report=stock_base, question="q", supplements=[], facts=[],
    )
    assert dir_system == stock_system


def test_direction_followup_requires_j_prefix_in_prompt() -> None:
    _, user_text = direction_followup_messages(
        base_report=_direction_base(), question="q", supplements=[], facts=[],
    )
    assert "claim_id 只能取上面核心判断清单里已有的编号（J 开头）" in user_text


# --------------------------------------------------------------------------
# 2. 补充材料与信任标签
# --------------------------------------------------------------------------


def test_direction_followup_labels_supplements_unverified() -> None:
    supp = {
        "supplement_id": "S1",
        "trust_label": analysis_followup.USER_SUPPLEMENT_TRUST,
        "source_name": "BLS 官网",
        "url": "https://www.bls.gov/cpi/",
        "event_date": "2026-09-11",
        "text": "8 月 CPI 同比 3.4%",
    }
    fact = {
        "subject": "us", "date": "2026-09-11", "date_is_input_time": False,
        "complete": True, "source_credibility": "高", "relation": "宏观前提",
    }
    _, user_text = direction_followup_messages(
        base_report=_direction_base(), question="q", supplements=[supp], facts=[fact],
    )
    assert analysis_followup.USER_SUPPLEMENT_TRUST in user_text
    assert "BLS 官网" in user_text
    assert "与主题的关系=宏观前提" in user_text


def test_direction_followup_empty_supplements_states_so() -> None:
    _, user_text = direction_followup_messages(
        base_report=_direction_base(), question="q", supplements=[], facts=[],
    )
    assert "本次无补充材料" in user_text


# --------------------------------------------------------------------------
# 3. 归一化闸门（对 J# 编号）
# --------------------------------------------------------------------------


def test_normalize_followup_keeps_only_existing_judgment_ids() -> None:
    allowed = {"J1", "J6"}
    parsed = {
        "affected_claims": [
            {"claim_id": "J1", "effect": "supports", "reason": "CPI 回落确认前提"},
            {"claim_id": "J99", "effect": "weakens", "reason": "自造编号"},
        ],
        "conclusion_change": "strengthened",
        "answer": "回答正文",
    }
    out = normalize_followup(parsed, allowed_claim_ids=allowed)
    assert out is not None
    ids = [item["claim_id"] for item in out["affected_claims"]]
    assert ids == ["J1"]
    assert out["dropped_claim_ids"] == ["J99"]


def test_normalize_followup_out_of_vocab_effect_falls_back() -> None:
    parsed = {
        "affected_claims": [{"claim_id": "J1", "effect": "destroyed", "reason": "r"}],
        "answer": "a",
    }
    out = normalize_followup(parsed, allowed_claim_ids={"J1"})
    assert out is not None
    assert out["affected_claims"][0]["effect"] == "cannot_judge"
    assert out["affected_claims"][0]["effect_raw"] == "destroyed"


def test_normalize_followup_requires_answer() -> None:
    assert normalize_followup({"affected_claims": []}, allowed_claim_ids={"J1"}) is None


def test_followup_prompt_version_bumped_for_direction() -> None:
    """契约变更必须递增版本（仓库约定）。v39：P0 契约（直接回答/三条状态轴/日期闸门）→ 1.2。"""
    assert FOLLOWUP_PROMPT_VERSION == "1.2"


# --------------------------------------------------------------------------
# 4. 端点层：kind 路由（读源码断言，不启服务）
# --------------------------------------------------------------------------


def test_endpoint_allows_direction_and_rejects_unknown() -> None:
    src = (ROOT / "apps" / "core-api" / "src" / "investment_steward_core" / "api" / "app.py").read_text(
        encoding="utf-8"
    )
    assert 'if kind not in ("stock", "direction"):' in src
    assert "仅个股研报支持追问（方向研判无核心判断清单）" not in src
    assert "analysis_followup.direction_followup_messages(" in src


def test_endpoint_extracts_direction_ids_from_core_judgments() -> None:
    src = (ROOT / "apps" / "core-api" / "src" / "investment_steward_core" / "api" / "app.py").read_text(
        encoding="utf-8"
    )
    assert 'base.get("core_judgments")' in src
    # 方向分支必须取 item["id"]，不能沿用个股的 claim_id 字段。
    # v39 Q04：编号集合与「编号→判断名称」映射合并成一份 claims_by_id（闸门与展示同源）。
    start = src.index("claims_by_id = {")
    block = src[start:start + 420]
    assert 'item.get("id")' in block and 'base.get("core_judgments")' in block
    assert "allowed_claim_ids = set(claims_by_id)" in src


def test_direction_frontend_tab_wired() -> None:
    tsx = (
        ROOT / "apps" / "web-shell" / "src" / "routes" / "workbench" / "direction.tsx"
    ).read_text(encoding="utf-8")
    assert '"overview" | "analysis" | "candidates" | "evidence" | "followup"' in tsx
    assert "followup: { label:" in tsx
    assert "followup?: ReactNode;" in tsx
    assert 'tab === "followup" && followup' in tsx
    assert '.filter((key) => key !== "followup" || Boolean(followup))' in tsx

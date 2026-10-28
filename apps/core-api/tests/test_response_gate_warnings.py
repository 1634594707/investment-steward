"""警告不能因模型把数组写成单段文本而丢失，兼容也不能扩大行动权限。"""
import json
from uuid import uuid4

import pytest
from investment_steward_core.response_gate import (
    ResponseGateError,
    assemble_agent_response,
    build_gate_payload,
)


def _payload(**fields):
    return build_gate_payload(json.dumps({
        "user_question": "能否比较现金流？", "summary": "需要核对口径",
        "confidence_description": "有限", "action_mode": "research", **fields,
    }), [], run_id=uuid4(), user_id=uuid4())


def test_live_sample_narrative_warnings_survive_gate_and_response():
    # 2026-10-27 合成财务实测的原始两字段；不依赖本地评测文件。
    freshness = "三项证据的published_at均为null，无法判断发布时效；报告期末为2026-06-30。"
    limitation = "未计算现金流覆盖倍数，也未计算资本开支后余额；未进行任何汇率换算、年化或口径调整。"
    response = assemble_agent_response(_payload(
        freshness_warning=freshness, limitations=limitation,
        supporting_refs=[str(uuid4())],
    ), response_id=uuid4())
    assert response.freshness_warning == [freshness]
    assert response.limitations == [limitation]
    assert response.evidence_refs == []
    assert any("剔除" in line for line in response.missing_information)


@pytest.mark.parametrize("field", ["freshness_warning", "limitations"])
@pytest.mark.parametrize("value, expected", [
    ("  警告\n下一行  ", ["警告\n下一行"]),
    (["  警告  ", "", "  "], ["警告"]), ("  ", []), (None, []),
])
def test_warning_normalization_preserves_text_without_inventing_items(field, value, expected):
    assert _payload(**{field: value})[field] == expected


@pytest.mark.parametrize("field", ["freshness_warning", "limitations"])
@pytest.mark.parametrize("value", [{"warning": "必须保留"}, 42, ["警告", 42]])
def test_malformed_warning_is_rejected_instead_of_silently_losing_information(field, value):
    with pytest.raises(ResponseGateError, match=field):
        _payload(**{field: value})


def test_narrative_compatibility_does_not_accept_string_action_declarations():
    with pytest.raises(ResponseGateError, match="supported_actions"):
        _payload(freshness_warning="日期未知", supported_actions="create_plan")
    payload = _payload(tool_calls="claimed_tool", plugin_runs="claimed_plugin", supporting_refs=str(uuid4()))
    assert payload["tool_calls"] == payload["plugin_runs"] == payload["evidence_refs"] == []

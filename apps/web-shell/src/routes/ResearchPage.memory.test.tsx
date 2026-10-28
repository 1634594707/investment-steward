import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { AgentResponse, Evidence, ResearchMemoryMatch } from "@investment-steward/domain-contracts";
import { AnswerDetail } from "./ResearchPage";

afterEach(() => { cleanup(); vi.useRealTimers(); });

const memory: ResearchMemoryMatch = {
  thesis_id: "thesis-1", instrument: "TEST", original_statement: "现金流可以支撑扩张",
  core_assumptions: ["回款保持稳定"], invalidation_conditions: ["回款持续恶化"],
  thesis_updated_at: "2026-10-25T00:00:00Z", linked_evidence_refs: ["retracted", "expired", "missing"],
  candidate_supporting_refs: ["current"], candidate_contradicting_refs: ["opposite"],
};

function response(research_memory?: ResearchMemoryMatch[]): AgentResponse {
  return {
    response_id: "response-1", run_id: "run-1", user_id: "user-1", created_at: "2026-10-26T00:00:00Z",
    user_question: "现金流如何？", context_snapshot_refs: [], summary: "需要继续观察",
    reasoning_outline: [], confidence_description: "证据有限", evidence_refs: [], supporting_refs: [],
    contradicting_refs: [], missing_information: [], freshness_warning: [], limitations: [],
    action_mode: "observe", supported_actions: [], user_confirmation_required: false,
    prompt_policy_version: "1.0", plugin_runs: [], tool_calls: [], audit_refs: [], research_memory,
  };
}

function evidence(id: string, overrides: Partial<Evidence> = {}): Evidence {
  return {
    evidence_id: id, tenant_id: "user-1", subject_refs: ["TEST"], evidence_type: "financial",
    source_name: id, license_status: "unknown", collected_at: "2026-10-25T00:00:00Z",
    schema_version: "1.0", summary: "合成证据", content_hash: id, relation: "supporting",
    freshness: "current", quality_flags: [], limitations: [], status: "active", ...overrides,
  };
}

function show(memories?: ResearchMemoryMatch[], items: Evidence[] = []) {
  const onOpen = vi.fn();
  render(<AnswerDetail response={response(memories)} evidenceOf={(id) => items.find((item) => item.evidence_id === id)}
    onOpenEvidence={onOpen} onCardAction={() => {}} />);
  return onOpen;
}

describe("研究记忆展示", () => {
  it.each([undefined, []])("旧回答和无匹配回答不生成记忆内容：%j", (memories) => {
    show(memories);
    expect(screen.queryByText(/研究记忆/)).toBeNull();
  });

  it("用户判断保持原文，候选关系不能被表述为已验证的假设结论", () => {
    show([memory]);
    expect(screen.getByText("原始判断：现金流可以支撑扩张")).toBeTruthy();
    expect(screen.getByText("回款保持稳定")).toBeTruthy();
    expect(screen.getByText("回款持续恶化")).toBeTruthy();
    expect(screen.getByText(/回答生成时的用户逻辑快照/)).toBeTruthy();
    expect(screen.getByText(/尚未核验与该假设的关系/)).toBeTruthy();
    expect(screen.getByText("历史关联证据")).toBeTruthy();
    expect(screen.getByText("本轮候选相反证据")).toBeTruthy();
  });

  it("历史失效、缺详情及后来撤回的候选均明示当前状态，所有引用可追溯", () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-10-26T00:00:00Z"));
    const onOpen = show([memory], [
      evidence("retracted", { status: "retracted" }),
      evidence("expired", { valid_until: "2026-10-25T23:59:59Z" }),
      evidence("current", { valid_until: "2026-10-26T00:00:00Z" }),
      evidence("opposite", { status: "retracted", relation: "contradicting" }),
    ]);
    for (const [id, status] of [["retracted", "已撤回"], ["expired", "已过期"],
      ["missing", "详情未取得，状态未知"], ["current", "当前未标记失效"], ["opposite", "已撤回"]] as const) {
      const button = screen.getByRole("button", { name: new RegExp(id.slice(0, 8)) });
      expect(within(button).getByText(status)).toBeTruthy();
      fireEvent.click(button);
      expect(onOpen).toHaveBeenLastCalledWith(id);
    }
  });

  it("缺少假设或引用时使用空态，不推测补全", () => {
    show([{ ...memory, core_assumptions: [], invalidation_conditions: [], linked_evidence_refs: [],
      candidate_supporting_refs: [], candidate_contradicting_refs: [] }]);
    expect(screen.getAllByText("未填写")).toHaveLength(2);
    expect(screen.getByText("原始判断：现金流可以支撑扩张")).toBeTruthy();
  });
});

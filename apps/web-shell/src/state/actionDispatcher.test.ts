import { describe, expect, it, vi } from "vitest";
import { CARD_ACTIONS, dispatchCardAction, needsConfirmation, type CardActionContext } from "./actionDispatcher";
import type { Evidence } from "@investment-steward/domain-contracts";

const evidence: Evidence[] = [
  { evidence_id: "ev-1", title: "公告", kind: "announcement" } as unknown as Evidence,
  { evidence_id: "ev-2", title: "新闻", kind: "news" } as unknown as Evidence,
];

function context(overrides: Partial<CardActionContext> = {}): CardActionContext {
  return {
    card_id: "card-1",
    title: "今日关注",
    evidence_refs: ["ev-1"],
    action_mode: "observe",
    supported_actions: ["open_evidence", "create_plan", "start_learning", "review_policy"],
    ...overrides,
  };
}

const kernel = {
  openEvidence: vi.fn(),
  createPlan: vi.fn(),
  startLearning: vi.fn(),
  reviewPolicy: vi.fn(),
};

describe("dispatchCardAction", () => {
  it("未声明的动作被内核拒绝（白名单仲裁）", () => {
    const result = dispatchCardAction(context({ supported_actions: [] }), "open_evidence", kernel, evidence);
    expect(result.handled).toBe(false);
    expect(result.reason).toContain("未声明");
    expect(result.audit.resource).toBe("blocked:card-1");
    expect(kernel.openEvidence).not.toHaveBeenCalled();
  });

  it("open_evidence 展开引用的第一条证据", () => {
    const result = dispatchCardAction(context(), "open_evidence", kernel, evidence);
    expect(result.handled).toBe(true);
    expect(kernel.openEvidence).toHaveBeenCalledWith("ev-1");
    expect(result.audit.action).toBe("card.open_evidence");
  });

  it("open_evidence 无证据时不执行", () => {
    const result = dispatchCardAction(context({ evidence_refs: [] }), "open_evidence", kernel, evidence);
    expect(result.handled).toBe(false);
    expect(result.reason).toBe("无证据可展开");
  });

  it("create_plan 把卡片标题与证据引用交给内核", () => {
    const result = dispatchCardAction(context(), "create_plan", kernel, evidence);
    expect(result.handled).toBe(true);
    expect(kernel.createPlan).toHaveBeenCalledWith({ title: "今日关注", evidenceRefs: ["ev-1"] });
  });

  it("start_learning / review_policy 透传 action_mode", () => {
    dispatchCardAction(context(), "start_learning", kernel, evidence);
    dispatchCardAction(context(), "review_policy", kernel, evidence);
    expect(kernel.startLearning).toHaveBeenCalledWith("observe");
    expect(kernel.reviewPolicy).toHaveBeenCalledWith("observe");
  });

  it("动作枚举与白名单一致", () => {
    expect(CARD_ACTIONS).toHaveLength(4);
  });
});

describe("needsConfirmation", () => {
  it("create_plan 与 review_policy 需显式确认", () => {
    expect(needsConfirmation("create_plan")).toBe(true);
    expect(needsConfirmation("review_policy")).toBe(true);
    expect(needsConfirmation("open_evidence")).toBe(false);
    expect(needsConfirmation("start_learning")).toBe(false);
  });
});

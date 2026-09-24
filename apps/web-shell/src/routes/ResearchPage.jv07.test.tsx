/**
 * JV07（Jev 决策模型接入路线图 2026-09-21）：行动模式预判路由的**呈现契约**。
 *
 * 锁住三件用户能直接看见、且最容易做错的事：
 *
 * 1. **没跑预判时零渲染**——`jev_route` 为 `null`/`undefined` 时不出现「本次链路」块，
 *    与接入前逐字节一致（JV00 铁律 6）。这一条比「打开时好看」更重要。
 * 2. **「跳过了完整链路」必须明说**——`bypassed_model` 为真时写清「未调用完整研究链路」。
 *    用户有权知道这次回答**没有走完整链路**，否则他会把本地引擎的产出当成完整研究的结论。
 * 3. **预判与 `action_mode` 并陈、互不覆盖**——预判说「不行动」而 `action_mode` 显示「观察」
 *    是**正常且可能**的：两者回答的是不同问题（该不该花这次调用 vs 结论是什么）。
 *    所以渲染层必须两个都显示，不能拿一个盖掉另一个。
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import { AnswerDetail } from "./ResearchPage";
import type { AgentResponse, JevRouteDecision } from "@investment-steward/domain-contracts";

afterEach(cleanup);

function response(route: JevRouteDecision | null | undefined): AgentResponse {
  return {
    response_id: "r-1",
    run_id: "run-1",
    user_id: "u-1",
    created_at: "2026-09-22T10:00:00Z",
    user_question: "沪深300 当前估值分位如何？",
    context_snapshot_refs: [],
    summary: "本地证据账本中有 2 条支持证据、无相反证据，可给出倾向性判断。",
    reasoning_outline: [],
    confidence_description: "支持证据 2 条、相反证据 0 条，判断置信度为中等。",
    evidence_refs: [],
    supporting_refs: [],
    contradicting_refs: [],
    missing_information: [],
    freshness_warning: [],
    limitations: [],
    action_mode: "observe",
    supported_actions: ["open_evidence"],
    user_confirmation_required: false,
    prompt_policy_version: "1.0",
    plugin_runs: [],
    tool_calls: [],
    audit_refs: [],
    jev_route: route ?? null,
  };
}

function route(overrides: Partial<JevRouteDecision> = {}): JevRouteDecision {
  return {
    mode: "observe",
    mode_label: "观察",
    confidence: 0.82,
    probabilities: { observe: 0.82 },
    bypassed_model: false,
    route_state: "full",
    question_id: "q_action_mode",
    options: ["research", "observe", "review_plan", "no_action"],
    note: "预判为「观察」，说明这题值得认真回答，维持完整研究链路。",
    model: "jev-latest",
    schema_version: "1.0",
    ...overrides,
  };
}

function renderDetail(routeValue: JevRouteDecision | null | undefined) {
  render(
    <AnswerDetail
      response={response(routeValue)}
      evidenceOf={() => undefined}
      onOpenEvidence={() => {}}
      onCardAction={() => {}}
    />,
  );
}

describe("JV07 路由呈现", () => {
  it("没跑预判时零渲染（与接入前一致）", () => {
    renderDetail(null);
    expect(screen.queryByText("本次链路")).toBeNull();
    expect(screen.queryByText(/JV07/)).toBeNull();
  });

  it("undefined 与 null 同样零渲染（「没接 Jev」与「接了没跑」都不该冒出这一块）", () => {
    render(
      <AnswerDetail
        response={{ ...response(null), jev_route: undefined }}
        evidenceOf={() => undefined}
        onOpenEvidence={() => {}}
        onCardAction={() => {}}
      />,
    );
    expect(screen.queryByText("本次链路")).toBeNull();
  });

  it("维持完整链路时明说是完整链路，并显示预判与置信度", () => {
    renderDetail(route());
    expect(screen.getByText("本次链路")).toBeTruthy();
    expect(screen.getByText(/完整研究链路（预判：观察）/)).toBeTruthy();
    expect(screen.getByText(/预判置信度 0.82/)).toBeTruthy();
  });

  it("跳过完整链路时明说「未调用」（用户有权知道这次没走完整链路）", () => {
    renderDetail(
      route({
        mode: "no_action",
        mode_label: "不行动",
        bypassed_model: true,
        route_state: "routed",
        note: "预判为「不行动」且置信度 0.90，跳过完整研究链路。",
      }),
    );
    expect(screen.getByText(/未调用完整研究链路（预判：不行动）/)).toBeTruthy();
    const block = screen.getByText("本次链路").closest("section");
    expect(block?.className).toContain("warn");
  });

  it("预判与 action_mode 并陈、互不覆盖（两者回答的是不同问题）", () => {
    renderDetail(
      route({
        mode: "no_action",
        mode_label: "不行动",
        bypassed_model: true,
        route_state: "routed",
        note: "预判为「不行动」且置信度 0.90，跳过完整研究链路。",
      }),
    );
    // 预判说「不行动」，而作答方给出的 action_mode 仍是「观察」——两个都必须在页面上
    expect(screen.getByText(/未调用完整研究链路（预判：不行动）/)).toBeTruthy();
    expect(screen.getByText(/观察/)).toBeTruthy();
  });

  it("置信度缺失时不编造数值（如实显示，不写 0.00）", () => {
    renderDetail(route({ confidence: null, note: "未取得预判，维持完整研究链路。" }));
    expect(screen.queryByText(/预判置信度/)).toBeNull();
  });
});

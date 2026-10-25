import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { SCHEMA_VERSION, type Holding, type InvestmentPolicyVersion, type TodayBrief } from "@investment-steward/domain-contracts";
import { AppShell } from "./AppShell";

const time = "2026-10-25T00:00:00Z";
const policy: InvestmentPolicyVersion = {
  policy_id: "policy1", user_id: "user1", version: 1, status: "active", investment_goal: "核对投资依据",
  liquidity_needs: "测试", allowed_markets: [], allowed_asset_classes: [], risk_boundaries: {},
  preferred_methods: [], excluded_methods: [], observation_conditions: [], invalidation_conditions: [],
  change_reason: "测试", source_learning_activity_ids: [], schema_version: SCHEMA_VERSION,
  created_at: time, updated_at: time, revision: 1, conflict_policy: "测试",
};
const brief: TodayBrief = {
  brief_id: "brief1", user_id: "user1", generated_at: time, has_personalization: true,
  headline: "本次待处理事项", policy_version: "1", schema_version: SCHEMA_VERSION,
  items: [
    { display_order: 0, title: "第一事项", summary: "核对第一标的", related_instrument: "CN:STOCK:600618", signal: "review_plan", evidence_refs: ["e1"] },
    { display_order: 1, title: "第二事项", summary: "核对第二标的", related_instrument: "300750", signal: "observe", evidence_refs: ["e2"] },
    { display_order: 2, title: "缺关联事项", summary: "没有提供处理对象", signal: "review_plan", evidence_refs: [] },
  ],
};

function installChannel(holdings: Holding[]) {
  const responses = new Map<string, unknown>([
    ["/health", null], ["/overview", null], ["/personal/settings", null], ["/investor/profile", null],
    ["/investment-policies", [policy]], ["/holdings", holdings], ["/evidence", []], ["/plugins/catalog", []],
    ["/notifications/pending", { items: [] }], ["/slots", []], ["/brief/today", brief],
    ["/research/questions/latest", null], ["/research/runs", []], ["/thesis", []], ["/plans", []], ["/decisions", []],
    ["/review/weekly", null], ["/learning/unit/today", null], ["/learning/goals/current", null], ["/learning/activities", []],
    ["/channels", []], ["/capabilities", []], ["/quant/artifacts", null], ["/model-profiles", []], ["/credentials", []],
    ["/notifications/channels", []], ["/cards/slots/today.brief", []], ["/cards/slots/today.learning", []],
  ]);
  vi.stubEnv("VITE_CORE_BASE", "http://core.test");
  vi.stubEnv("VITE_CORE_TOKEN", "");
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const path = new URL(url).pathname;
    const status = responses.has(path) ? 200 : 503;
    const data = responses.has(path) ? responses.get(path) : { detail: "此测试未提供该接口数据" };
    return { ok: status < 400, status, text: async () => data === null ? "" : JSON.stringify(data) } as Response;
  }));
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("steward.onboarding-deferred-at", String(Date.now()));
});
afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

describe("U01/U04 实际壳层与页面连接", () => {
  it.each([2, 0])("%i 个持仓、3 个自选时首页与投资页分类一致", async (holdingCount) => {
    const instruments = ["600618", "300750", "600176", "600519", "000001"];
    const holdings: Holding[] = instruments.slice(0, holdingCount + 3).map((instrument, index) => ({
      holding_id: `holding${index}`, user_id: "user1", instrument, label: `标的${index}`,
      status: index < holdingCount ? "holding" : "watchlist", strategy_note: "",
      created_at: time, updated_at: time, schema_version: SCHEMA_VERSION,
    }));
    installChannel(holdings);
    render(<AppShell />);
    expect(await screen.findByText(`${holdingCount} 个持仓标的`)).toBeVisible();
    expect(screen.getByText("3 个自选标的")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "我的投资" }));
    expect(await screen.findByText(`已确认 · ${holdingCount} 个持仓 · 3 个自选`)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "今天" }));
    expect(screen.getByText(`${holdingCount} 个持仓标的`)).toBeVisible();
  });

  it("连续处理不同事项替换上下文，缺关联时不沿用旧标的，直接导航清除上下文", async () => {
    installChannel([]);
    render(<AppShell />);
    const first = await screen.findByText("第一事项");
    fireEvent.click(within(first.closest(".action-item") as HTMLElement).getByRole("button", { name: "去复核" }));
    let context = await screen.findByRole("region", { name: "简报处理上下文" });
    expect(within(context).getByText("本次处理标的：CN:STOCK:600618")).toBeVisible();
    expect(within(context).getByRole("button", { name: "查看依据 e1" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "今天" }));
    expect(screen.queryByRole("region", { name: "简报处理上下文" })).toBeNull();
    fireEvent.click(within(screen.getByText("第二事项").closest(".action-item") as HTMLElement).getByRole("button", { name: "看持仓与逻辑" }));
    context = await screen.findByRole("region", { name: "简报处理上下文" });
    expect(within(context).getByText("本次处理标的：300750")).toBeVisible();
    expect(within(context).getByRole("button", { name: "查看依据 e2" })).toBeVisible();
    expect(within(context).queryByRole("button", { name: "查看依据 e1" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "今天" }));
    fireEvent.click(within(screen.getByText("缺关联事项").closest(".action-item") as HTMLElement).getByRole("button", { name: "去复核" }));
    context = await screen.findByRole("region", { name: "简报处理上下文" });
    expect(within(context).getByText("本次处理标的：未关联标的，请自行选择处理对象")).toBeVisible();
    expect(within(context).getByText("本条未附证据引用")).toBeVisible();
    expect(within(context).queryByRole("button", { name: "查看依据 e2" })).toBeNull();
    fireEvent.click(within(context).getByRole("button", { name: "结束本次简报处理" }));
    expect(screen.queryByRole("region", { name: "简报处理上下文" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "我的投资" }));
    expect(await screen.findByRole("heading", { name: "核对投资依据" })).toBeVisible();
    expect(screen.queryByRole("region", { name: "简报处理上下文" })).toBeNull();
  });
});

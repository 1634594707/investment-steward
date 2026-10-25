import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { MacroSnapshot } from "@investment-steward/domain-contracts";
import { MacroPage } from "./MacroPage";

const state = vi.hoisted(() => ({ snapshots: {} as Record<string, MacroSnapshot>, saves: vi.fn() }));
vi.mock("../state/coreClient", () => ({ createCoreClient: () => ({}) }));
vi.mock("../hooks/useMacro", () => ({ useMacro: () => ({
  macroSnapshots: state.snapshots, macroLoading: false, macroWeights: null,
  fetchMacroUserView: async () => null, saveMacroUserView: state.saves,
  fetchMacroPricing: async () => null, fetchMacroAnalysisCache: async () => null,
  fetchMacroReleases: async () => null, fetchMacroEvents: async () => [],
  fetchMacroCalendar: async () => null, fetchSpeakerSignals: async () => [],
  fetchMacroResearchList: async () => [],
}) }));

const countries = [["us", "美国"], ["cn", "中国"], ["eu", "欧元区"], ["jp", "日本"], ["in", "印度"]] as const;
beforeEach(() => {
  state.saves.mockClear();
  state.snapshots = Object.fromEntries(countries.map(([region, label]) => [region, {
    region, label, generated_at: "2026-10-25T00:00:00Z", background: [],
    indicators: [{ indicator: "cpi_yoy", label: "居民消费价格同比完整名称", dim: "inflation", status: "ok",
      latest: 2.5, obs_date: "2025", as_of: "2026-09-01T00:00:00Z", frequency: "annual", unit: "%", source: "test", note: "固定测试数据" }],
    positioning: { region, weight_version: "v1", composite: 50, band: "放缓", limitations: [], generated_at: "2026-10-25",
      dims: [{ dim: "inflation", score: 50, indicators_used: ["cpi_yoy"], rules_fired: [] }] },
  } satisfies MacroSnapshot]));
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("Map network disabled in navigation test")));
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
});
afterEach(() => vi.unstubAllGlobals());

describe("M01—M03 宏观详情导航", () => {
  it("五地区选择均可达，观测期与快照日期分别展示", async () => {
    const user = userEvent.setup();
    render(<MacroPage isDemo={false} active onNavigate={() => {}} />);
    const navigation = screen.getByRole("navigation", { name: "五国概览与详情选择" });
    for (const [, name] of countries) {
      const button = within(navigation).getByRole("button", { name: new RegExp(name) });
      await user.click(button);
      expect(button).toHaveAttribute("aria-pressed", "true");
      const visible = screen.getAllByRole("region").filter((element) => element.getAttribute("aria-label")?.endsWith("宏观详情"));
      expect(visible).toHaveLength(1);
      const details = screen.getByRole("region", { name: `${name}宏观详情` });
      expect(within(details).getByRole("heading", { name: "年度结构数据" })).toBeTruthy();
      expect(within(details).getByText("居民消费价格同比完整名称")).toBeTruthy();
      expect(details.textContent).toContain("观测期：2025；抓取时间：2026-09-01T00:00:00Z");
      expect(details.textContent).toContain("快照生成时间");
      expect(details.textContent).toContain("规则区间：放缓");
    }
  });

  it("切换地区保留未保存的个人分析，不调用保存接口", async () => {
    const user = userEvent.setup();
    render(<MacroPage isDemo={false} active onNavigate={() => {}} />);
    const us = screen.getByRole("region", { name: "美国宏观详情" });
    await waitFor(() => expect(within(us).getByRole("button", { name: /写下我的判断/ })).toBeEnabled());
    await user.click(within(us).getByRole("button", { name: /写下我的判断/ }));
    const draft = within(us).getByPlaceholderText("我的判断依据（可选，仅存本机）");
    await user.type(draft, "保留此处尚未保存的分析");
    const nav = screen.getByRole("navigation", { name: "五国概览与详情选择" });
    await user.click(within(nav).getByRole("button", { name: /日本/ }));
    expect(us).not.toBeVisible();
    await user.click(within(nav).getByRole("button", { name: /美国/ }));
    expect(draft).toBeVisible();
    expect(draft).toHaveValue("保留此处尚未保存的分析");
    expect(state.saves).not.toHaveBeenCalled();
  });
});

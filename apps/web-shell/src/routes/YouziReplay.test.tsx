/**
 * Y3-07（游资二期）：案例复盘页签的渲染契约。
 *
 * 用**真实组件**（只桩掉 CoreClient 请求）断言即将上线的那份 JSX 行为，重点覆盖
 * 路线图 §6 明令禁止与明令要求的几条：
 *   1. 未生成结果时给显式原因，**不显示空表**（「我还不能下结论」≠「没有披露」）。
 *   2. 预算截短 / 部分覆盖 / 前取上下文都给显式提示。
 *   3. 六个期限里 `未到期` 与 `已到期但数据源未给值` 文案不同、不写 0。
 *   4. 页面**不出现**「路径全部走完」「已了结」「清仓」这类越界结论。
 *
 * 之所以走真实组件而不是只测纯函数：这几条约束的载体是 JSX 文案与分支，
 * 纯函数测不到「降级分支到底渲染了什么」。
 */
import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";

const request = vi.fn();

vi.mock("../state/coreClient", () => ({
  createCoreClient: () => ({ request }),
}));

import { YouziPage } from "./YouziPage";

const BOUNDARY =
  "榜外席位不可见，未检出未发生；同一营业部不等于同一账户，无法据此确认持仓、清仓或同一笔交易。";

function event(overrides: Record<string, unknown> = {}) {
  return {
    event_id: "e1",
    trading_day: "20260911",
    security_code: "600519",
    security_name: "测试股份",
    operatedept_code: "10026937",
    operatedept_name: "某某证券股份有限公司某营业部",
    direction: "buy",
    amount: 2.887e7,
    explanation: "日涨幅偏离值达7%的证券",
    source_report: "RPT_BILLBOARD_DAILYDETAILSBUY",
    source_record_id: "c".repeat(64),
    fact_types: [],
    fact_ids: [],
    horizons: [],
    fact_omitted_reason: "no_fact_for_window",
    ...overrides,
  };
}

function page(overrides: Record<string, unknown> = {}) {
  return {
    security_code: "600519",
    range_start: "20260907",
    range_end: "20260917",
    events: [],
    cursor: "",
    has_more: false,
    total_events: 0,
    coverage: {},
    truncated: false,
    truncated_reason: "",
    fetched_at: 1,
    limitations: ["区间内只包含**已取到**的交易日；未取到的日子在 coverage 中为 unknown，不代表当日没有披露。"],
    available: true,
    reason: "",
    boundary_notice: BOUNDARY,
    ...overrides,
  };
}

/** 打开页面并切到复盘页签，填入代码与区间后点查询。 */
async function openReplay(payload: Record<string, unknown>) {
  request.mockImplementation((req: { path: string }) => {
    // 今日席位（有效日）与观察名单的请求照常给最小可用值。
    if (req.path.startsWith("/youzi/today")) {
      return Promise.resolve({
        status: 200,
        data: { available: true, trading_day: "20260917", watchlist_version: "v1", rows: [] },
      });
    }
    if (req.path.startsWith("/youzi/watchlist")) {
      return Promise.resolve({ status: 200, data: { version: "v1", seats: [] } });
    }
    return Promise.resolve({ status: 200, data: payload });
  });

  render(<YouziPage isDemo={false} pluginEnabled onEnablePlugin={() => {}} active />);
  // 页面默认停在「今日席位」，复盘表单尚未挂载 → 先切页签。
  fireEvent.click(screen.getByTitle("按证券代码与日期区间查看披露事件时间线"));
  // ★ 页面激活即发 `/youzi/today`，那期间 `loading=true`，查询按钮会变成
  // 「取数中…」且 `disabled`——必须等首屏取数落地再操作表单，否则填了值也点不动
  // （症状是查询从未发出，或点到上一轮的加载态按钮）。
  await waitFor(() => expect(screen.getByText("查询")).toBeTruthy());
  fireEvent.change(screen.getByPlaceholderText("如 600519"), { target: { value: "600519" } });
  // 起始日 / 结束日两个输入用同一个 placeholder，必须按索引取，不能用 getBy。
  const dayInputs = screen.getAllByPlaceholderText("YYYYMMDD");
  fireEvent.change(dayInputs[0]!, { target: { value: "20260907" } });
  fireEvent.change(dayInputs[1]!, { target: { value: "20260917" } });
  fireEvent.click(screen.getByText("查询"));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(3));
}

beforeEach(() => {
  request.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("YouziPage · 案例复盘页签（Y3-07）", () => {
  it("复盘页签已登记在导航里（新增页签必须可见）", () => {
    render(<YouziPage isDemo={false} pluginEnabled onEnablePlugin={() => {}} active />);
    expect(screen.getByTitle("按证券代码与日期区间查看披露事件时间线")).toBeTruthy();
  });

  it("未启用插件时不渲染查询表单（页面级闸门优先）", () => {
    render(<YouziPage isDemo={false} pluginEnabled={false} onEnablePlugin={() => {}} active />);
    expect(screen.queryByText("查询")).toBeNull();
  });

  it("日历不可用 → 显式原因，且说明「不是没有披露」，不显示空表", async () => {
    await openReplay(page({ available: false, reason: "calendar_missing" }));
    await waitFor(() =>
      expect(screen.getByText(/可信交易日历不可用/)).toBeTruthy(),
    );
    expect(screen.getByText(/不是「区间内没有披露」/)).toBeTruthy();
    expect(screen.queryByText("区间内没有披露事件")).toBeNull();
  });

  it("区间内无交易日 → 与取数失败区分（不同文案）", async () => {
    await openReplay(page({ available: false, reason: "no_trading_day_in_range" }));
    await waitFor(() => expect(screen.getByText(/没有交易日/)).toBeTruthy());
  });

  it("预算截短 → 提示被截短并说明保留的是最近部分", async () => {
    await openReplay(
      page({
        truncated: true,
        truncated_reason: "区间内有 200 个交易日，超过单次复盘上限 60 个；已保留**最近**的 60 个。",
      }),
    );
    await waitFor(() => expect(screen.getByText("区间被上限截短")).toBeTruthy());
    expect(screen.getByText(/已保留\*\*最近\*\*的 60 个/)).toBeTruthy();
  });

  it("前取的上下文日单列说明，且声明不计入事件范围", async () => {
    await openReplay(page({ context_days: ["20260901", "20260902"] }));
    await waitFor(() => expect(screen.getByText(/额外前取了/)).toBeTruthy());
    expect(screen.getByText(/不计入/)).toBeTruthy();
  });

  it("已取到但无事件 → 说「已取到且没有命中」，并提醒覆盖不全的日子结论不成立", async () => {
    await openReplay(page({ events: [], total_events: 0, coverage: { "20260907": "unknown" } }));
    await waitFor(() => expect(screen.getByText("区间内没有披露事件")).toBeTruthy());
    expect(screen.getByText(/那些天的结论不成立/)).toBeTruthy();
  });

  it("事件行展示席位、方向、金额与「检查过没有」的事实口径", async () => {
    await openReplay(page({ events: [event()], total_events: 1 }));
    await waitFor(() => expect(screen.getByText("某某证券股份有限公司某营业部")).toBeTruthy());
    expect(screen.getByText("买入")).toBeTruthy();
    expect(screen.getByText("2887.00 万")).toBeTruthy();
    // 窗口完整但未命中 → 「未命中」文案，而不是空着或写「无事实」。
    expect(screen.getByText("窗口完整，未命中三类路径事实")).toBeTruthy();
  });

  it("★ 期限展开后区分「未到期」与「已到期但数据源未给值」，且不写 0", async () => {
    await openReplay(
      page({
        events: [
          event({
            horizons: [
              {
                label: "D1",
                sessions: 1,
                value_pct: 4.2,
                status: "disclosed",
                target_date: "20260914",
                fetched_at: 1,
                source: "RPT_DAILYBILLBOARD_DETAILS",
                reason: "",
              },
              {
                label: "D2",
                sessions: 2,
                value_pct: null,
                status: "not_due",
                target_date: "",
                fetched_at: 1,
                source: "RPT_DAILYBILLBOARD_DETAILS",
                reason: "only_0_trading_days_elapsed",
              },
              {
                label: "D5",
                sessions: 5,
                value_pct: null,
                status: "missing",
                target_date: "20260918",
                fetched_at: 1,
                source: "RPT_DAILYBILLBOARD_DETAILS",
                reason: "field_null_after_due",
              },
            ],
          }),
        ],
        total_events: 1,
      }),
    );
    await waitFor(() => expect(screen.getByText("某某证券股份有限公司某营业部")).toBeTruthy());
    // 展开该事件行。
    fireEvent.click(screen.getByText("某某证券股份有限公司某营业部"));

    await waitFor(() => expect(screen.getByText("期限状态")).toBeTruthy());
    expect(screen.getByText("+4.20%")).toBeTruthy();
    expect(screen.getByText("尚未到期")).toBeTruthy();
    expect(screen.getByText("已到期但数据源未给值")).toBeTruthy();
    // 未披露一律渲染破折号，绝不显示 0%。
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
    expect(screen.queryByText("+0.00%")).toBeNull();
  });

  it("★ 页面不出现「路径全部走完」「已了结」这类越界结论", async () => {
    await openReplay(page({ events: [event()], total_events: 1 }));
    await waitFor(() => expect(screen.getByText("某某证券股份有限公司某营业部")).toBeTruthy());
    const text = document.body.textContent ?? "";
    expect(text).not.toContain("路径全部走完");
    expect(text).not.toContain("已了结");
    // boundary_notice 是否定式免责说明，必须原样出现（不能被删掉来「通过」检查）。
    expect(text).toContain("无法据此确认持仓、清仓或同一笔交易");
  });

  it("★ 页面不提供排行/推荐入口（只有代码输入）", async () => {
    await openReplay(page({ events: [event()], total_events: 1 }));
    await waitFor(() => expect(screen.getByText("查询")).toBeTruthy());

    // ★ 不能用「正文不含『排行』二字」来判——页面上那句
    // 「只按代码查，不提供推荐或排行。」正是**声明不做**排行，属否定式免责文案，
    // 整块 textContent 扫会把它自己误伤成违规（后端 boundary_notice 也踩过同一个坑）。
    // 判据应落在「有没有排行的**入口/载体**」上：
    //   1. 没有任何排序/榜单控件（select / 榜、排序、Top-N 之类的按钮）；
    //   2. 没有绩效统计载体（表格里不出现收益/胜率列）。
    expect(document.querySelectorAll("select")).toHaveLength(0);
    for (const label of [/^排序/, /排行/, /^Top\s*\d+/i, /最活跃席位/]) {
      expect(screen.queryByRole("button", { name: label })).toBeNull();
    }
    const headers = Array.from(document.querySelectorAll("th")).map((th) => th.textContent ?? "");
    for (const header of headers) {
      expect(header).not.toMatch(/收益|胜率|上涨比例/);
    }
    // 反面确认：页面确实**只**给代码输入（不存在任何选股/选席位列表）。
    expect(screen.getByPlaceholderText("如 600519")).toBeTruthy();
  });

  it("还有下一页时给出「加载更多」并显示剩余条数", async () => {
    await openReplay(
      page({
        events: [event({ event_id: "e1" }), event({ event_id: "e2" })],
        total_events: 5,
        has_more: true,
        cursor: "cur-1",
      }),
    );
    await waitFor(() => expect(screen.getByText(/加载更多/)).toBeTruthy());
    expect(screen.getByText(/还有 3 条/)).toBeTruthy();
  });
});

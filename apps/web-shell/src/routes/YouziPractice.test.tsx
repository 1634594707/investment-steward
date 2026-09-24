/**
 * Y4-03 / Y4-04（游资二期）：练习面板的内容与交互契约。
 *
 * 这一组守的是路线图 §7 里对练习的逐字要求，逐条对应：
 *   Y4-03「用披露事实与证据回链完成练习；当日无匹配可查历史或使用标明来源的
 *          教学样例，不要求每个交易日必有指纹」
 *        → 断言证据回链三个标识可见；无样例时给显式提示而非空表。
 *   Y4-04「只选择实际已披露 D1/D2/D5 的样例；先隐藏结果、用户记录事实/假设/
 *          失效条件后揭示；说明当前快照与样本选择偏差，不声称是严格历史时点回测」
 *        → 断言揭示按钮在作答前禁用；揭示后才出现期限表；必须出现「不是历史时点
 *          回测」的说明与样本筛选计数。
 *
 * ★ 关键前置：页面默认停在「今日席位」，练习面板尚未挂载；且激活即发
 *   `/youzi/today`，期间按钮是「取数中…」且 disabled。必须先切页签、等首屏落地。
 */
import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";

const request = vi.fn();

vi.mock("../state/coreClient", () => ({
  createCoreClient: () => ({ request }),
}));

import { YouziPage } from "./YouziPage";

/** 造一条真实形状的披露事件；`horizons` 可按需覆盖状态。 */
function makeEvent(overrides: Record<string, unknown> = {}) {
  const horizon = (label: string, status: string, value: number | null) => ({
    label,
    sessions: Number(label.slice(1)),
    value_pct: value,
    status,
    target_date: "20260918",
    fetched_at: 1758000000,
    source: "D1_CLOSE_ADJCHRATE",
    reason: "",
  });
  return {
    event_id: "evt-0001",
    trading_day: "20260915",
    security_code: "600519",
    security_name: "贵州茅台",
    operatedept_code: "10026937",
    operatedept_name: "某证券营业部",
    direction: "buy",
    amount: 12345678,
    explanation: "日涨幅偏离值达7%的证券",
    source_report: "RPT_DAILYBILLBOARD_DETAILSNEW",
    source_record_id: "a".repeat(64),
    fact_types: ["adjacent_buy_sell"],
    fact_ids: ["fact-1"],
    horizons: [
      horizon("D1", "disclosed", 3.2),
      horizon("D2", "disclosed", 5.5),
      horizon("D5", "disclosed", 8.1),
      horizon("D10", "not_due", null),
      horizon("D20", "not_due", null),
      horizon("D30", "not_due", null),
    ],
    fact_omitted_reason: "",
    ...overrides,
  };
}

function makePage(events: Array<Record<string, unknown>>) {
  return {
    security_code: "600519",
    range_start: "20260907",
    range_end: "20260917",
    events,
    cursor: "",
    has_more: false,
    total_events: events.length,
    coverage: { "20260915": "complete" },
    truncated: false,
    truncated_reason: "",
    fetched_at: 1758000000,
    limitations: ["榜单只覆盖进入榜单的股票与席位。"],
    available: true,
    boundary_notice:
      "榜外席位不可见，未检出未发生；同一营业部不等于同一账户，无法据此确认持仓、清仓或同一笔交易。",
    event_day_count: 1,
    context_days: [],
  };
}

/**
 * 打开练习页签并完成一次取样。
 *
 * `replayEvents` 用来指定本次「取样例」返回的事件；其余路径返回今日席位骨架。
 */
async function openPractice(replayEvents: Array<Record<string, unknown>>) {
  request.mockImplementation((req: { path: string }) => {
    if (req.path.includes("/youzi/replay")) {
      return Promise.resolve({ status: 200, data: makePage(replayEvents) });
    }
    return Promise.resolve({
      status: 200,
      data: { available: true, trading_day: "20260917", watchlist_version: "v1", rows: [] },
    });
  });

  render(<YouziPage isDemo={false} pluginEnabled onEnablePlugin={() => {}} active />);

  // 页面默认停在「今日席位」→ 先切到练习页签。
  fireEvent.click(screen.getByTitle("用真实披露事实做识别与复盘练习"));
  // ★ 首屏 `/youzi/today` 在途时按钮渲染成「取数中…」且 disabled，必须等它落地。
  await waitFor(() => expect(screen.getByText("取样例")).toBeTruthy());

  fireEvent.change(screen.getByPlaceholderText("如 600519"), { target: { value: "600519" } });
  const dayInputs = screen.getAllByPlaceholderText("YYYYMMDD");
  fireEvent.change(dayInputs[0]!, { target: { value: "20260907" } });
  fireEvent.change(dayInputs[1]!, { target: { value: "20260917" } });
  fireEvent.click(screen.getByText("取样例"));

  // ★ 不能等「识别练习：写出已知与未知」——样例为空时它压根不渲染。
  // 也不能等「可练样例」四个字：它后面紧跟 <strong>数字</strong>，「可练样例 1 条」
  // 不构成任何单一节点的完整 textContent，getByText 永远匹配不到。
  // 这里等一个**取样后必然成立**的信号：摘要条的 testid 或任一空样例提示。
  await waitFor(() => {
    const settled =
      screen.queryByTestId("practice-sample-count") !== null ||
      screen.queryByText("这段区间里没有可用的练习样例") !== null ||
      screen.queryByText("本次练习没有可用样例") !== null;
    expect(settled).toBe(true);
  });
}

beforeEach(() => {
  request.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("YouziPage · 练习（Y4-03/Y4-04）", () => {
  it("★ Y4-03 识别练习给出证据回链三标识，且用真实披露字段", async () => {
    await openPractice([makeEvent()]);

    // 证据回链：来源报告 / 来源记录 ID / 事件 ID。
    expect(screen.getByText("RPT_DAILYBILLBOARD_DETAILSNEW")).toBeTruthy();
    expect(screen.getByText("a".repeat(64))).toBeTruthy();
    expect(screen.getByText("evt-0001")).toBeTruthy();

    // 真实披露字段落到表格里，不造假数据。
    expect(screen.getByText("600519")).toBeTruthy();
    expect(screen.getByText("日涨幅偏离值达7%的证券")).toBeTruthy();
    expect(screen.getByText("买入")).toBeTruthy();
    // 事实标签用中文标签渲染，不是把英文枚举直接抛给使用者。
    expect(screen.getByText("相邻买卖：D 日买榜、下一交易日卖榜")).toBeTruthy();
  });

  it("★ Y4-03 当日无匹配时说明可换历史区间，不显示空表凑题", async () => {
    await openPractice([]);

    expect(screen.getByText("这段区间里没有可用的练习样例")).toBeTruthy();
    // 提示要指向「换区间」，且明确不编造样例。
    expect(screen.getByText(/不为了凑题而编造样例/)).toBeTruthy();
    // 不得出现识别练习的表格。
    expect(screen.queryByText("识别练习：写出已知与未知")).toBeNull();
    expect(screen.queryByText("对照要点")).toBeNull();
  });

  it("★ Y4-03 降级（未生成结果）与「区间内没有」区分开", async () => {
    request.mockImplementation((req: { path: string }) => {
      if (req.path.includes("/youzi/replay")) {
        return Promise.resolve({
          status: 200,
          data: { available: false, reason: "calendar_missing" },
        });
      }
      return Promise.resolve({
        status: 200,
        data: { available: true, trading_day: "20260917", watchlist_version: "v1", rows: [] },
      });
    });
    render(<YouziPage isDemo={false} pluginEnabled onEnablePlugin={() => {}} active />);
    fireEvent.click(screen.getByTitle("用真实披露事实做识别与复盘练习"));
    await waitFor(() => expect(screen.getByText("取样例")).toBeTruthy());
    fireEvent.change(screen.getByPlaceholderText("如 600519"), { target: { value: "600519" } });
    const dayInputs = screen.getAllByPlaceholderText("YYYYMMDD");
    fireEvent.change(dayInputs[0]!, { target: { value: "20260907" } });
    fireEvent.change(dayInputs[1]!, { target: { value: "20260917" } });
    fireEvent.click(screen.getByText("取样例"));

    await waitFor(() => expect(screen.getByText("本次练习没有可用样例")).toBeTruthy());
    expect(screen.getByText(/不是「区间内没有披露」/)).toBeTruthy();
  });

  it("★ Y4-04 复盘练习只挑已披露 D1/D2/D5 的样例，并报出筛掉了多少条", async () => {
    // 两条：一条三个期限齐全（可练），一条 D5 未到期（必须被筛掉）。
    const good = makeEvent();
    const bad = makeEvent({
      event_id: "evt-0002",
      horizons: [
        { label: "D1", sessions: 1, value_pct: 1.0, status: "disclosed", target_date: "20260918", fetched_at: 1, source: "s", reason: "" },
        { label: "D2", sessions: 2, value_pct: 2.0, status: "disclosed", target_date: "20260918", fetched_at: 1, source: "s", reason: "" },
        { label: "D5", sessions: 5, value_pct: null, status: "not_due", target_date: "20260918", fetched_at: 1, source: "s", reason: "" },
      ],
    });

    await openPractice([good, bad]);
    fireEvent.click(screen.getByText("复盘练习（Y4-04）"));

    // ★ 用 testid 取摘要条：「可练样例」与数字分处两个节点，整串匹配不到。
    await waitFor(() => expect(screen.getByTestId("practice-sample-count")).toBeTruthy());
    expect(screen.getByTestId("practice-sample-count").textContent).toContain("1");
    // 不可练的那条必须被明确筛掉并计数（1 条可用 / 共 2 条 / 筛掉 1 条）。
    expect(screen.getByText(/按「已披露 D1\/D2\/D5」筛掉/)).toBeTruthy();
  });

  it("★★ Y4-04 作答前结果隐藏：三栏写全才能点「揭示结果」", async () => {
    await openPractice([makeEvent()]);
    fireEvent.click(screen.getByText("复盘练习（Y4-04）"));

    // 未作答 → 按钮是禁用的，且提示三栏都要写。
    const before = screen.getByText("三栏都写完后可揭示") as HTMLButtonElement;
    expect(before.disabled).toBe(true);
    // 结果表此刻**不得存在**——这是本题的核心契约。
    expect(screen.queryByText("揭示：已披露的期限")).toBeNull();

    const boxes = screen.getAllByRole("textbox").filter(
      (node) => node.tagName === "TEXTAREA",
    ) as HTMLTextAreaElement[];
    expect(boxes).toHaveLength(3);
    fireEvent.change(boxes[0]!, { target: { value: "09-15 该席位买入榜披露 1234.57 万" } });
    fireEvent.change(boxes[1]!, { target: { value: "可能是短线资金集中买入" } });
    // 只写两栏仍不可点。
    expect((screen.getByText("三栏都写完后可揭示") as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(boxes[2]!, { target: { value: "若次日同席位反向披露则不成立" } });
    const ready = screen.getByText("揭示结果") as HTMLButtonElement;
    expect(ready.disabled).toBe(false);

    fireEvent.click(ready);
    // 揭示后才出现期限表，且只展示承诺的三档。
    await waitFor(() => expect(screen.getByText("揭示：已披露的期限")).toBeTruthy());
    const labels = Array.from(document.querySelectorAll("td.mono")).map((td) => td.textContent ?? "");
    expect(labels).toContain("D1");
    expect(labels).toContain("D2");
    expect(labels).toContain("D5");
  });

  it("★★ Y4-04 揭示结果必须声明「不是历史时点回测」并说明样本选择偏差", async () => {
    await openPractice([makeEvent()]);
    fireEvent.click(screen.getByText("复盘练习（Y4-04）"));

    const boxes = screen.getAllByRole("textbox").filter(
      (node) => node.tagName === "TEXTAREA",
    ) as HTMLTextAreaElement[];
    for (const box of boxes) {
      fireEvent.change(box, { target: { value: "写下内容" } });
    }
    fireEvent.click(screen.getByText("揭示结果"));

    await waitFor(() => expect(screen.getByText("揭示：已披露的期限")).toBeTruthy());
    // 这段免责必须逐字存在——去掉它，练习就变成变相回测。
    expect(screen.getByText(/这不是历史时点回测/)).toBeTruthy();
    expect(screen.getByText(/不构成任何绩效统计/)).toBeTruthy();
  });

  it("★ 练习不产生绩效统计：无排序/排行控件、无收益列、无胜率", async () => {
    await openPractice([makeEvent()]);
    fireEvent.click(screen.getByText("复盘练习（Y4-04）"));

    // ★ 不能用「正文不含『排行』」来判——页面上的免责句里含这些字。
    // 判据是「有没有承载排行的入口/列」。
    expect(document.querySelectorAll("select")).toHaveLength(0);
    const headers = Array.from(document.querySelectorAll("th")).map((th) => th.textContent ?? "");
    for (const header of headers) {
      expect(header).not.toMatch(/收益|胜率|上涨比例|排序/);
    }
    for (const label of [/^排序/, /排行/, /^Top\s*\d+/i]) {
      expect(screen.queryByRole("button", { name: label })).toBeNull();
    }
  });

  it("★ 练习页不触碰持仓/自选数据", async () => {
    await openPractice([makeEvent()]);
    fireEvent.click(screen.getByText("复盘练习（Y4-04）"));

    const paths = request.mock.calls.map((call) => (call[0] as { path: string }).path);
    expect(paths.some((path) => path.includes("cross-check") || path.includes("portfolio"))).toBe(
      false,
    );
    expect(paths.some((path) => path.includes("/youzi/replay"))).toBe(true);
  });

  it("★ 换一次样例会清空上一题的作答，避免旧结论被当成新答案", async () => {
    await openPractice([makeEvent()]);
    fireEvent.click(screen.getByText("复盘练习（Y4-04）"));

    const boxes = screen.getAllByRole("textbox").filter(
      (node) => node.tagName === "TEXTAREA",
    ) as HTMLTextAreaElement[];
    for (const box of boxes) {
      fireEvent.change(box, { target: { value: "上一题的答案" } });
    }
    fireEvent.click(screen.getByText("揭示结果"));
    await waitFor(() => expect(screen.getByText("揭示：已披露的期限")).toBeTruthy());

    // 再取一次样：作答与揭示状态都必须回到初始。
    fireEvent.click(screen.getByText("取样例"));
    await waitFor(() =>
      expect((screen.getByText("三栏都写完后可揭示") as HTMLButtonElement).disabled).toBe(true),
    );
    for (const box of screen.getAllByRole("textbox").filter((n) => n.tagName === "TEXTAREA")) {
      expect((box as HTMLTextAreaElement).value).toBe("");
    }
    expect(screen.queryByText("揭示：已披露的期限")).toBeNull();
  });
});

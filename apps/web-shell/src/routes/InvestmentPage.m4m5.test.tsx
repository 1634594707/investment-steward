/**
 * M4/M5（2026-09-15 第二轮路线图）投资页交互：
 * - M4-E02：新建持仓改成**单框搜索**——输入即查；1 条自动回填、多条必须点选、0 条禁止保存；
 * - M5-F01：自选移除不渲染说明框，确认即可移除，并把空说明交给上层（后端固定句兜底）。
 *
 * 用真实组件 + 桩回调（不 mock 组件内部），断言的是**即将上线的那份 JSX 行为**。
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { Holding } from "@investment-steward/domain-contracts";
import { InvestmentPage } from "./InvestmentPage";
import type { InstrumentLookupHit, InstrumentLookupOutcome } from "../state/instrumentLookup";

function holding(over: Partial<Holding> = {}): Holding {
  return {
    holding_id: "h-1",
    user_id: "u-1",
    instrument: "688009",
    label: "中国通号",
    status: "watchlist",
    strategy_note: "",
    created_at: "2026-09-15T00:00:00Z",
    updated_at: "2026-09-15T00:00:00Z",
    schema_version: "1.0",
    ...over,
  } as Holding;
}

const RAIL_HIT: InstrumentLookupHit = {
  key: "688009",
  name: "中国通号",
  kind: "stock",
  market: "sh",
  display: "中国通号（688009）",
};
const APPLIANCE_HITS: InstrumentLookupHit[] = [
  { key: "000651", name: "格力电器", kind: "stock", market: "sz", display: "格力电器（000651）" },
  { key: "600690", name: "海尔智家", kind: "stock", market: "sh", display: "海尔智家（600690）" },
];

function renderPage(options: {
  holdings?: Holding[];
  lookup: (q: string) => Promise<InstrumentLookupOutcome>;
}) {
  // 参数类型与 `InvestmentPage` props 同源：显式标注后才能断言 `mock.calls[i][0]`（否则参数元组为 []）。
  const onCreateHolding = vi.fn(
    async (_input: {
      instrument: string;
      label: string;
      status: "holding" | "watchlist";
      strategy_note: string;
    }) => true,
  );
  const onDeleteHolding = vi.fn(async (_holdingId: string, _note?: string) => true);
  render(
    <InvestmentPage
      policy={null}
      evidence={[]}
      holdings={options.holdings ?? []}
      theses={[]}
      candles={null}
      candlesLoading={false}
      candlePeriod="day"
      isDemo={false}
      marketPluginEnabled={false}
      onNavigate={vi.fn()}
      onSaveThesis={vi.fn(async () => true)}
      onCreateHolding={onCreateHolding}
      onDeleteHolding={onDeleteHolding}
      onLookupInstrument={options.lookup}
      onCreateThesis={vi.fn(async () => true)}
      onCreatePolicyDraft={vi.fn(async () => null)}
      onConfirmPolicy={vi.fn(async () => true)}
      onSelectInstrument={vi.fn()}
      onSelectCandlePeriod={vi.fn()}
      onPullEvidence={vi.fn(async () => null)}
      onDedupEvidence={vi.fn(async () => null)}
      onOpenEvidence={vi.fn()}
    />,
  );
  return { onCreateHolding, onDeleteHolding };
}

const ok = (hits: InstrumentLookupHit[]): Promise<InstrumentLookupOutcome> =>
  Promise.resolve({ ok: true, hits });

/* 注意：搜索框自身的 value 就等于用户输入，不能用 getByDisplayValue("688009") 等回填
 * （会立刻命中搜索框本身）。必须按可访问名定位「标的编码 / 名称」两个字段。 */
const codeField = (): HTMLInputElement =>
  screen.getByRole("textbox", { name: "标的编码" }) as HTMLInputElement;
const nameField = (): HTMLInputElement =>
  screen.getByRole("textbox", { name: "名称（可改）" }) as HTMLInputElement;

describe("新建持仓：单框搜索（M4-E02/E03）", () => {
  it("唯一命中自动回填代码与名称，保存按钮可用", async () => {
    renderPage({ lookup: async (q) => ok(q.includes("688009") || q.includes("通号") ? [RAIL_HIT] : []) });
    fireEvent.click(screen.getByRole("button", { name: /新建第一个标的/ }));
    fireEvent.change(screen.getByPlaceholderText("001201 / 510300 / 东瑞股份"), { target: { value: "688009" } });
    await waitFor(() => {
      expect(codeField().value).toBe("688009");
      expect(nameField().value).toBe("中国通号");
    }, { timeout: 2000 });
    expect(screen.getByRole("button", { name: /保存/ })).toHaveProperty("disabled", false);
  });

  it("多条命中必须点选：未点选时保存不可用，且不会静默取第一条", async () => {
    renderPage({ lookup: async () => ok(APPLIANCE_HITS) });
    fireEvent.click(screen.getByRole("button", { name: /新建第一个标的/ }));
    fireEvent.change(screen.getByPlaceholderText("001201 / 510300 / 东瑞股份"), { target: { value: "家电" } });
    await waitFor(() => expect(screen.getByRole("listbox", { name: "匹配到的标的" })).toBeTruthy(), { timeout: 2000 });
    expect(screen.getByRole("button", { name: /保存/ })).toHaveProperty("disabled", true);
    // 点选第二条 → 回填海尔智家
    fireEvent.click(screen.getByRole("option", { name: /海尔智家/ }));
    expect(codeField().value).toBe("600690");
    expect(nameField().value).toBe("海尔智家");
    expect(screen.getByRole("button", { name: /保存/ })).toHaveProperty("disabled", false);
  });

  it("0 条命中提示「未找到标的」且禁止保存", async () => {
    renderPage({ lookup: async () => ok([]) });
    fireEvent.click(screen.getByRole("button", { name: /新建第一个标的/ }));
    fireEvent.change(screen.getByPlaceholderText("001201 / 510300 / 东瑞股份"), { target: { value: "查无此股" } });
    await waitFor(() => expect(screen.getByText("未找到标的")).toBeTruthy(), { timeout: 2000 });
    expect(screen.getByRole("button", { name: /保存/ })).toHaveProperty("disabled", true);
  });

  it("查找服务不可用与「没找到」分开提示，且同样禁止保存", async () => {
    renderPage({
      lookup: async () => ({ ok: false, message: "查找服务暂不可用，请稍后重试（未使用本地猜测的名称）。" }),
    });
    fireEvent.click(screen.getByRole("button", { name: /新建第一个标的/ }));
    fireEvent.change(screen.getByPlaceholderText("001201 / 510300 / 东瑞股份"), { target: { value: "东瑞" } });
    await waitFor(() => expect(screen.getByText(/查找服务暂不可用/)).toBeTruthy(), { timeout: 2000 });
    expect(screen.getByRole("button", { name: /保存/ })).toHaveProperty("disabled", true);
  });

  it("E03：保存时仍走原 POST /holdings 契约（instrument + label + status + strategy_note）", async () => {
    const { onCreateHolding } = renderPage({ lookup: async () => ok([RAIL_HIT]) });
    fireEvent.click(screen.getByRole("button", { name: /新建第一个标的/ }));
    fireEvent.change(screen.getByPlaceholderText("001201 / 510300 / 东瑞股份"), { target: { value: "688009" } });
    await waitFor(() => expect(codeField().value).toBe("688009"), { timeout: 2000 });
    fireEvent.click(screen.getByRole("button", { name: /保存/ }));
    await waitFor(() => expect(onCreateHolding).toHaveBeenCalledTimes(1));
    expect(onCreateHolding.mock.calls[0]![0]).toEqual({
      instrument: "688009",
      label: "中国通号",
      status: "watchlist",
      strategy_note: "",
    });
  });

  it("查询改动后旧选中立即失效：改字后不再能用上一只标的保存", async () => {
    renderPage({ lookup: async (q) => (q.includes("通号") || q === "688009" ? ok([RAIL_HIT]) : ok([])) });
    fireEvent.click(screen.getByRole("button", { name: /新建第一个标的/ }));
    const input = screen.getByPlaceholderText("001201 / 510300 / 东瑞股份");
    fireEvent.change(input, { target: { value: "688009" } });
    await waitFor(() => expect(codeField().value).toBe("688009"), { timeout: 2000 });
    fireEvent.change(input, { target: { value: "688009x" } });
    expect(screen.getByRole("button", { name: /保存/ })).toHaveProperty("disabled", true);
  });
});

describe("移除自选：不写原因（M5-F01）", () => {
  it("自选移除弹窗不出现必填说明框，点确认即移除并传空说明", async () => {
    const { onDeleteHolding } = renderPage({ holdings: [holding()], lookup: async () => ok([]) });
    fireEvent.click(screen.getByRole("button", { name: "移除中国通号" }));
    expect(screen.getByRole("dialog", { name: /移除自选/ })).toBeTruthy();
    expect(screen.queryByText(/确认说明（必填/)).toBeNull();
    const confirm = screen.getByRole("button", { name: "确认移除" });
    expect(confirm).toHaveProperty("disabled", false);
    fireEvent.click(confirm);
    await waitFor(() => expect(onDeleteHolding).toHaveBeenCalledWith("h-1", ""));
  });

  it("持仓移除弹出的是「选填」说明框（F02 实现口径）", () => {
    renderPage({ holdings: [holding({ status: "holding" })], lookup: async () => ok([]) });
    fireEvent.click(screen.getByRole("button", { name: "移除中国通号" }));
    expect(screen.getByRole("dialog", { name: /移除持仓/ })).toBeTruthy();
    expect(screen.getByText("确认说明（选填，留空则审计记为固定说明）")).toBeTruthy();
  });
});

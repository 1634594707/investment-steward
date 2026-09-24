/**
 * G01（桌面端升级路线图 2026-09-18）前端契约：
 * 「待复盘」面板不再只读——带确切日期的验证点由服务端幂等落成判断，
 * 面板据此提供「回填结论（成立/失效/无数据）」表单与「到期未回填」计数。
 *
 * 这些用例锁定的是**即将上线的那份代码文本**的行为：
 * ① 计数只来自服务端 `closure`（前端不自己数、不猜状态）；
 * ② 回填参数逐字对齐后端 `JudgmentVerifyRequest.result` 的三态；
 * ③ 锚定事件（`judgment_id` 为空）不给回填入口，只如实显示不落库原因；
 * ④ 回填失败如实现错误文案且表单不收起（不把失败当成功）。
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReviewQueue } from "./researchTypes";
import { ReviewQueuePanel } from "./panels";

function queue(overrides: Partial<ReviewQueue> = {}): ReviewQueue {
  return {
    ok: true,
    today: "2026-09-19",
    counts: { overdue: 1, scheduled: 0, event_anchored: 1 },
    item_count: 2,
    items: [
      {
        report_id: "rep-1",
        symbol: "600000",
        title: "测试研报",
        signal: "放量站上 MA20 后回踩不破",
        verify_by: "两周内",
        due_on: "2026-09-15",
        expected_if_true: "均线多头排列保持",
        bucket: "overdue",
        days_until: -4,
        status: "pending",
        judgment_id: "11111111-1111-5111-8111-111111111111",
        verification: null,
        scenarios: [],
      },
      {
        report_id: "rep-1",
        symbol: "600000",
        signal: "三季报毛利率环比回升",
        verify_by: "三季报披露后",
        due_on: "",
        expected_if_true: "",
        bucket: "event_anchored",
        days_until: null,
        status: "pending_review",
        judgment_id: null,
        materialization_note: "验证时点锚定事件、无确切日期，而判断必须带到期时间——不猜日期，故不自动落库。",
        scenarios: [],
      },
    ],
    closure: { materialized: 1, filled: 0, overdue_unfilled: 1, not_materialized: 1 },
    ...overrides,
  };
}

describe("ReviewQueuePanel · G01 回填闭环", () => {
  it("计数只来自服务端 closure（到期未回填 / 已回填）", () => {
    render(<ReviewQueuePanel queue={queue()} />);
    expect(screen.getByText("到期未回填 1")).toBeTruthy();
    // filled=0 时不渲染「已回填」徽标（不显示 0 条噪声）。
    expect(screen.queryByText("已回填 0")).toBeNull();
    expect(screen.getByText(/已落库判定 1（已回填 0 \/ 到期未回填 1 \/ 锚定事件未落库 1）/)).toBeTruthy();
  });

  it("已回填计数与状态标签随服务端返回展示", () => {
    render(
      <ReviewQueuePanel
        queue={queue({
          closure: { materialized: 1, filled: 1, overdue_unfilled: 0, not_materialized: 1 },
          items: [
            {
              report_id: "rep-1",
              symbol: "600000",
              signal: "放量站上 MA20 后回踩不破",
              due_on: "2026-09-15",
              bucket: "overdue",
              days_until: -4,
              status: "refuted",
              judgment_id: "22222222-2222-5222-8222-222222222222",
              verification: { result: "refuted", outcome: "跌破 MA20 且量能萎缩", checked_at: "2026-09-18T02:00:00Z" },
              scenarios: [],
            },
          ],
        })}
        onRefill={vi.fn(async () => null)}
      />,
    );
    expect(screen.getByText("已回填 1")).toBeTruthy();
    // 到期未回填的 coral 徽标只在 >0 时渲染；工具栏文案里的 0 是如实口径，不是徽标。
    expect(screen.queryByText("到期未回填 0")).toBeNull();
    expect(screen.getByText(/已落库判定 1（已回填 1 \/ 到期未回填 0 \/ 锚定事件未落库 1）/)).toBeTruthy();
    expect(screen.getByText("失效")).toBeTruthy();
    expect(screen.getByText(/最近回填：失效 · 跌破 MA20 且量能萎缩（2026-09-18）/)).toBeTruthy();
    // 已回填过的判定仍可重新回填（不锁死历史）。
    expect(screen.getByRole("button", { name: "重新回填" })).toBeTruthy();
  });

  it("回填三态逐字对齐后端：选「失效」+ 说明后提交，带上判断 id", async () => {
    const onRefill = vi.fn(async () => null);
    const user = userEvent.setup();
    render(<ReviewQueuePanel queue={queue()} onRefill={onRefill} />);

    await user.click(screen.getByRole("button", { name: "回填结论" }));
    await user.click(screen.getByRole("button", { name: "失效" }));
    await user.type(screen.getByLabelText("回填说明"), "跌破 MA20 且量能萎缩");
    await user.click(screen.getByRole("button", { name: "提交回填" }));

    await waitFor(() =>
      expect(onRefill).toHaveBeenCalledWith("11111111-1111-5111-8111-111111111111", "refuted", "跌破 MA20 且量能萎缩"),
    );
    // 成功后表单收起（状态由服务端刷新带回，不在前端猜）。
    await waitFor(() => expect(screen.queryByRole("button", { name: "提交回填" })).toBeNull());
  });

  it("默认回填态是「成立」，未选时不臆造为无数据", async () => {
    const onRefill = vi.fn(async () => null);
    const user = userEvent.setup();
    render(<ReviewQueuePanel queue={queue()} onRefill={onRefill} />);

    await user.click(screen.getByRole("button", { name: "回填结论" }));
    await user.click(screen.getByRole("button", { name: "提交回填" }));

    await waitFor(() => expect(onRefill).toHaveBeenCalledWith("11111111-1111-5111-8111-111111111111", "verified", ""));
  });

  it("回填失败如实显示错误文案，且表单不收起", async () => {
    const onRefill = vi.fn(async () => "判断不存在:11111111-1111-5111-8111-111111111111");
    const user = userEvent.setup();
    render(<ReviewQueuePanel queue={queue()} onRefill={onRefill} />);

    await user.click(screen.getByRole("button", { name: "回填结论" }));
    await user.click(screen.getByRole("button", { name: "提交回填" }));

    expect(await screen.findByText(/回填失败：判断不存在:/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "提交回填" })).toBeTruthy();
  });

  it("锚定事件不给回填入口，只显示不落库原因", () => {
    render(<ReviewQueuePanel queue={queue()} onRefill={vi.fn(async () => null)} />);
    // 两条验证点里只有一条可回填 → 按钮只出现一次。
    expect(screen.getAllByRole("button", { name: "回填结论" })).toHaveLength(1);
    expect(screen.getByText(/不猜日期，故不自动落库/)).toBeTruthy();
  });

  it("没有注入 onRefill 时不渲染回填入口（只读降级）", () => {
    render(<ReviewQueuePanel queue={queue()} />);
    expect(screen.queryByRole("button", { name: "回填结论" })).toBeNull();
  });
});

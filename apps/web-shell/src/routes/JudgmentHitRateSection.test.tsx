/**
 * G02（桌面端升级路线图 2026-09-18）前端契约：后验命中率面板。
 *
 * 锁定四件事：
 * ① 命中率**必带分母**，分母为 0 时显示「—（分母 0）」而不是 0%（样本不足 ≠ 全错）；
 * ② 任一命中率可展开到判定原文与回填记录（数字可追溯）；
 * ③ 口径由服务端下发、面板只做 key→人话的映射（前端不自己定义口径）；
 * ④ 面板只读：不提供任何改投资原则的入口。
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { JudgmentHitRateSection, hitRateText } from "./JudgmentHitRateSection";
import type { HitRateReport } from "./JudgmentHitRateSection";

const POLICY: Record<string, string> = {
  numerator: "命中「成立」的判定数（最近的回填结果 result = verified）",
  denominator: "已回填且结论明确的判定数（成立 + 失效）",
  excluded: "「无数据」(insufficient_data) 与尚未回填的判定不进分母，数量仍如实展示",
  hit_rate: "命中率 = 成立 /（成立 + 失效）；分母为 0 时返回 null 并说明样本不足，不显示 0%",
  model: "模型方案取自判定来源研报的 model 字段；无来源研报时归入「（未标注模型方案）」，不猜",
  theme: "主题优先取来源研报标题；无来源研报（或报告已删除）时取判断对象，并以 theme_source 标注取值来源",
  window: "时间窗按判定**到期时间**筛选（含今天、不含未到期），不是回填时间",
  verification_value: "每个判定用**最近一次**回填结果（与 POST /judgments/{id}/verify 的追加语义一致）",
  no_auto_policy: "命中率只用于复盘展示，不自动调整投资原则——原则变更仍须走显式确认链",
};

function report(overrides: Partial<HitRateReport> = {}): HitRateReport {
  return {
    ok: true,
    today: "2026-09-19",
    window_days: 90,
    policy: POLICY,
    totals: {
      judgments: 3,
      verified: 1,
      refuted: 1,
      insufficient_data: 1,
      pending: 0,
      denominator: 2,
      hit_rate: 0.5,
      not_due_excluded: 4,
      buckets: 1,
    },
    buckets: [
      {
        model: "deepseek-v4-flash",
        theme: "甲股：缓涨",
        theme_source: "report_title",
        counts: { verified: 1, refuted: 1, insufficient_data: 1, pending: 0 },
        judgment_count: 3,
        hit_rate: 0.5,
        hit_rate_denominator: 2,
        hit_rate_note: null,
        judgments: [
          {
            judgment_id: "11111111-1111-5111-8111-111111111111",
            subject: "600000",
            statement: "放量站上 MA20 后回踩不破",
            direction: "观察（来自研报验证点）",
            due_at: "2026-09-15T00:00:00Z",
            status: "verified",
            source_report_id: "rep-1",
            source_refs: ["两周内"],
            verification: { result: "verified", outcome: "回踩 MA20 不破后放量", checked_at: "2026-09-16T02:00:00Z" },
          },
        ],
        truncated: false,
      },
    ],
    note: "命中率只统计已回填且结论明确的判定；展开任一桶可看到参与的判定原文与回填记录。命中率不自动改投资原则。",
    ...overrides,
  };
}

function stubFetch(data: HitRateReport = report()) {
  // 显式声明入参类型：否则 mock.calls 被推断为 [] 空元组，断言 path 时无法取到元素。
  return vi.fn(async (_req: { method: string; path: string }) => ({ status: 200, data }));
}

describe("JudgmentHitRateSection · G02 命中率", () => {
  it("hitRateText 分母为 0 时不给百分比", () => {
    expect(hitRateText(null, 0)).toBe("—（分母 0）");
    expect(hitRateText(0.5, 2)).toBe("50.0%（2 条已回填且结论明确）");
  });

  it("总量与分桶都带分母，且展示未到期未纳入计数", async () => {
    const coreRequest = stubFetch();
    render(<JudgmentHitRateSection coreRequest={coreRequest as never} />);
    // 总量与唯一的桶都给出同一命中率（都带分母）。
    expect(await screen.findAllByText("50.0%（2 条已回填且结论明确）")).toHaveLength(2);
    expect(screen.getByText(/未到期未纳入 4/)).toBeTruthy();
    expect(screen.getByText("deepseek-v4-flash")).toBeTruthy();
    expect(screen.getByText("甲股：缓涨")).toBeTruthy();
  });

  it("展开明细能看到判定原文与回填记录（数字可追溯）", async () => {
    const user = userEvent.setup();
    render(<JudgmentHitRateSection coreRequest={stubFetch() as never} />);
    await user.click(await screen.findByRole("button", { name: "展开明细" }));
    expect(screen.getByText("放量站上 MA20 后回踩不破")).toBeTruthy();
    expect(screen.getByText(/回填说明：回踩 MA20 不破后放量/)).toBeTruthy();
    expect(screen.getByText(/来源研报 rep-1/)).toBeTruthy();
  });

  it("分母 0 的桶显示样本不足说明，不显示 0%", async () => {
    const empty = report({
      totals: { judgments: 1, verified: 0, refuted: 0, insufficient_data: 0, pending: 1, denominator: 0, hit_rate: null, not_due_excluded: 0, buckets: 1 },
      buckets: [{
        model: "（未标注模型方案）",
        theme: "600000",
        theme_source: "judgment_subject",
        counts: { verified: 0, refuted: 0, insufficient_data: 0, pending: 1 },
        judgment_count: 1,
        hit_rate: null,
        hit_rate_denominator: 0,
        hit_rate_note: "样本不足：分母 0（成立 0 + 失效 0），不显示命中率。",
        judgments: [],
        truncated: false,
      }],
    });
    render(<JudgmentHitRateSection coreRequest={stubFetch(empty) as never} />);
    expect((await screen.findAllByText("—（分母 0）")).length).toBe(2); // 总量 + 分桶
    expect(screen.getByText(/样本不足：分母 0/)).toBeTruthy();
    expect(screen.queryByText(/0\.0%/)).toBeNull();
  });

  it("口径随服务端下发展示（含「不自动改原则」），面板无写入口", async () => {
    const user = userEvent.setup();
    render(<JudgmentHitRateSection coreRequest={stubFetch() as never} />);
    await screen.findByText("deepseek-v4-flash");
    await user.click(screen.getByText(/^口径（/));
    expect(screen.getByText(/命中率 = 成立 \/（成立 \+ 失效）/)).toBeTruthy();
    expect(screen.getByText(/不自动调整投资原则/)).toBeTruthy();
    // 只读：没有任何保存/提交/确认类按钮。
    for (const name of ["保存", "提交", "确认", "应用"]) {
      expect(screen.queryByRole("button", { name })).toBeNull();
    }
  });

  it("切换时间窗按新窗口重新请求", async () => {
    const coreRequest = stubFetch();
    const user = userEvent.setup();
    render(<JudgmentHitRateSection coreRequest={coreRequest as never} />);
    await screen.findByText("deepseek-v4-flash");
    await user.selectOptions(screen.getByLabelText("命中率时间窗"), "30d");
    await waitFor(() =>
      expect(coreRequest.mock.calls.some(([req]) => req.path === "/judgments/hit-rate?window=30d")).toBe(true),
    );
  });

  it("无 Core 通道时整体降级为提示", () => {
    render(<JudgmentHitRateSection />);
    expect(screen.getByText(/没有 Core 请求通道/)).toBeTruthy();
  });
});

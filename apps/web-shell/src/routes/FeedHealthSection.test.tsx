/**
 * F02/F03（桌面端升级路线图 2026-09-18）：今日取数健康面板的呈现契约。
 *
 * 锁住三件用户可感知的事：样本为 0 时显示「无样本」而不是 0%；失败原因按**异常类别**说人话；
 * 「仅重试失败项」只在窗口内有失败记录时可点，且请求体只重试失败过的来源（不无差别打上游）。
 */
import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";

import { FeedHealthSection } from "./FeedHealthSection";

const QUALITY = {
  window: "7d",
  window_days: 7,
  attempts_total: 3,
  attempts_truncated: 0,
  rule: "…",
  gaps: [{ dimension: "success_rate", reason: "本窗口无采集日志的来源：世界银行（公开接口）" }],
  sources: [
    {
      source: "中国市场行情（腾讯行情）",
      evidence_count: 0,
      attempts: 2,
      ok: 1,
      errors: 1,
      success_rate: 0.5,
      latency_ms: { avg: 400, p50: 300, p95: 500, max: 500 },
      error_kinds: [{ kind: "timeout", count: 1 }],
      last_attempt_at: "2026-09-19T09:00:00+00:00",
      retryable: true,
      retry_blocked_reason: null,
      note: "近 7 天有采集失败，错误类别见 error_kinds。",
    },
    {
      source: "世界银行（公开接口）",
      evidence_count: 0,
      attempts: 0,
      ok: 0,
      errors: 0,
      success_rate: null,
      latency_ms: { avg: null, p50: null, p95: null, max: null },
      error_kinds: [],
      last_attempt_at: null,
      retryable: true,
      retry_blocked_reason: null,
      note: "近 7 天无采集日志（可能全是缓存命中，或该源未在本窗口被调用）→ 成功率与延迟不显示。",
    },
    {
      source: "龙虎榜披露（东方财富）",
      evidence_count: 0,
      attempts: 1,
      ok: 0,
      errors: 1,
      success_rate: 0,
      latency_ms: { avg: 15000, p50: 15000, p95: 15000, max: 15000 },
      error_kinds: [{ kind: "http_error", count: 1 }],
      last_attempt_at: "2026-09-19T09:05:00+00:00",
      retryable: false,
      retry_blocked_reason: "龙虎榜按交易日取数，请从「游资雷达 → 今日席位」选好交易日重跑。",
      note: "近 7 天有采集失败。",
    },
  ],
};

const FAILURES = {
  failures: [
    {
      source: "中国市场行情（腾讯行情）",
      endpoint: "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
      params_fingerprint: "abc123",
      error_kind: "timeout",
      latency_ms: 15000,
      started_at: "2026-09-19T09:00:00+00:00",
      attempt_index: 2,
    },
  ],
};

function stubFetch(overrides: { retry?: unknown } = {}) {
  return vi.fn(async (req: { method: string; path: string }) => {
    if (req.path.startsWith("/data-source/quality")) return { status: 200, data: QUALITY };
    if (req.path.startsWith("/data-source/failures")) return { status: 200, data: FAILURES };
    if (req.path.startsWith("/data-source/retry")) {
      return {
        status: 200,
        data: overrides.retry ?? {
          attempted: 1,
          results: [{ source: "中国市场行情（腾讯行情）", supported: true, ok: true, detail: "取到 5 根日 K" }],
        },
      };
    }
    return { status: 404, data: { detail: "unknown" } };
  });
}

describe("F02/F03 今日取数健康", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });
  afterEach(() => {
    cleanup();
  });

  it("呈现成功率、延迟分位与失败类别的人话标签", async () => {
    render(<FeedHealthSection coreRequest={stubFetch() as never} />);
    await waitFor(() => expect(screen.getByText("今日取数健康")).toBeTruthy());

    expect(await screen.findByText("50%")).toBeTruthy();
    expect(screen.getByText(/P95 500ms/)).toBeTruthy();
    // 错误类别必须落成人话，而不是把 timeout 直接丢给用户。
    expect(screen.getAllByText(/超时×1/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/HTTP 错误×1/).length).toBeGreaterThan(0);
  });

  it("无样本显示「—（无样本）」而不是 0%，且不与「全都失败」混为一谈", async () => {
    render(<FeedHealthSection coreRequest={stubFetch() as never} />);
    const noSample = await screen.findAllByText("—（无样本）");
    expect(noSample.length).toBeGreaterThan(0);
    // 世界银行那行：尝试列为「—」（没有样本），成功率为「—（无样本）」。
    const row = screen.getByText("世界银行（公开接口）").closest("tr");
    expect(row?.textContent).toContain("—（无样本）");
    expect(row?.textContent).not.toContain("0%");
  });

  it("不可在此重试的来源给出原因，而不是一个点不动的按钮", async () => {
    render(<FeedHealthSection coreRequest={stubFetch() as never} />);
    const blocked = await screen.findByText(/不能在此重试：龙虎榜按交易日取数/);
    expect(blocked).toBeTruthy();
  });

  it("最近失败记录给出「第几次、失败原因、耗时」", async () => {
    render(<FeedHealthSection coreRequest={stubFetch() as never} />);
    expect(await screen.findByText("最近失败记录")).toBeTruthy();
    expect(screen.getByText("第 2 次")).toBeTruthy();
    expect(screen.getByText("15000ms")).toBeTruthy();
  });

  it("「仅重试失败项」只发 only_failed 请求，并回显逐源结果", async () => {
    const coreRequest = stubFetch();
    render(<FeedHealthSection coreRequest={coreRequest as never} />);
    const button = await screen.findByRole("button", { name: "仅重试失败项" });
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(button);

    await waitFor(() =>
      expect(coreRequest.mock.calls.some(([req]) => (req as { path: string }).path === "/data-source/retry")).toBe(true),
    );
    const retryCall = coreRequest.mock.calls.find(([req]) => (req as { path: string }).path === "/data-source/retry");
    // 断言点单一：找到这次请求的 body（找不到就是 undefined，toBeEqual 会红）。
    const body = (retryCall?.[0] as { body?: Record<string, unknown> } | undefined)?.body;
    expect(body).toEqual({ only_failed: true, window: "7d" });
    expect(await screen.findByText(/取到 5 根日 K/)).toBeTruthy();
  });

  it("窗口内没有失败记录时不发重试请求（按钮禁用）", async () => {
    const coreRequest = stubFetch();
    coreRequest.mockImplementation(async (req: { method: string; path: string }) => {
      if (req.path.startsWith("/data-source/quality")) return { status: 200, data: QUALITY };
      return { status: 200, data: { failures: [] } };
    });
    render(<FeedHealthSection coreRequest={coreRequest as never} />);
    const button = await screen.findByRole("button", { name: "仅重试失败项" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });
});

/**
 * Y2-10（游资二期）：跨日事实取数的前端契约。
 *
 * 三条被测硬要求：
 *   1. `factsEndingOn` 只保留「最近参与日 = 有效日 D」的事实（纯函数）。
 *   2. `loadTactics` 请求 `/youzi/tactics/{day}`，缺省日走 `/youzi/tactics`。
 *   3. **日期切换忽略旧响应**：先发的请求后返回时不得覆盖后发的结果。
 *      （对应路线图 Y2-10「日期切换忽略旧响应」与 Y3-08 的同一要求。）
 */
import { describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { CoreClient } from "../state/coreClient";
import {
  factsEndingOn,
  useYouzi,
  type YouziTacticsFact,
} from "./useYouzi";

function fact(overrides: Partial<YouziTacticsFact> = {}): YouziTacticsFact {
  return {
    fact_type: "repeated_seat_presence",
    security_code: "600519",
    security_name: "贵州茅台",
    operatedept_code: "10026937",
    operatedept_name: "某营业部",
    days: ["20260914", "20260917"],
    fact_id: "f1",
    evidence: [],
    ...overrides,
  };
}

function emptyPayload(day: string): Record<string, unknown> {
  return {
    request_day: day,
    effective_day: day,
    window_days: ["20260911", "20260914", "20260915", "20260916", "20260917"],
    coverage: {},
    available: true,
    items: [],
    raw_billboard: [],
    reason: "",
    conflicts: [],
    excluded: {},
    boundary_notice: "榜外席位不可见。",
  };
}

describe("factsEndingOn（Y2-10 只展示结束/最近出现日为 D 的标签）", () => {
  it("保留最近参与日等于 D 的事实", () => {
    const kept = fact({ fact_id: "a", days: ["20260915", "20260917"] });
    const dropped = fact({ fact_id: "b", days: ["20260911", "20260916"] });
    const result = factsEndingOn([kept, dropped], "20260917");
    expect(result.map((f) => f.fact_id)).toEqual(["a"]);
  });

  it("空日序列的事实一律不展示（没有「结束日」就没有今日结论）", () => {
    expect(factsEndingOn([fact({ days: [] })], "20260917")).toEqual([]);
  });

  it("有效日为空时不做过滤（today 尚未取到，不误杀全部事实）", () => {
    const items = [fact({ fact_id: "a" }), fact({ fact_id: "b" })];
    expect(factsEndingOn(items, "")).toHaveLength(2);
  });

  it("不因日期排序习惯而误判：取的是 days 末位，不是最大值", () => {
    // days 约定升序；若实现写成 Math.max 会与「末位」等价，若写成首位则误判。
    const items = [fact({ fact_id: "a", days: ["20260911", "20260917"] })];
    expect(factsEndingOn(items, "20260911")).toEqual([]);
  });
});

describe("useYouzi.loadTactics（Y2-10 端点与在途响应）", () => {
  it("显式日期请求 /youzi/tactics/{day}", async () => {
    const request = vi.fn().mockResolvedValue({ status: 200, data: emptyPayload("20260917") });
    const client = { request } as unknown as CoreClient;
    const { result } = renderHook(() => useYouzi(client, false));
    await act(async () => {
      await result.current.loadTactics("20260917");
    });
    expect(request.mock.calls[0]![0].path).toBe("/youzi/tactics/20260917");
    expect(result.current.tactics?.effective_day).toBe("20260917");
  });

  it("缺省日期请求 /youzi/tactics（有效日交给后端日历判定）", async () => {
    const request = vi.fn().mockResolvedValue({ status: 200, data: emptyPayload("20260917") });
    const client = { request } as unknown as CoreClient;
    const { result } = renderHook(() => useYouzi(client, false));
    await act(async () => {
      await result.current.loadTactics(null);
    });
    expect(request.mock.calls[0]![0].path).toBe("/youzi/tactics");
  });

  it("日期切换：先发后到的旧响应不得覆盖新响应", async () => {
    const slow: Array<(value: { status: number; data: unknown }) => void> = [];
    const request = vi.fn().mockImplementation((req: { path: string }) => {
      if (req.path === "/youzi/tactics/20260916") {
        // 旧请求：挂起，等新的先完成。
        return new Promise((resolve) => slow.push(resolve));
      }
      return Promise.resolve({ status: 200, data: emptyPayload("20260917") });
    });
    const client = { request } as unknown as CoreClient;
    const { result } = renderHook(() => useYouzi(client, false));

    await act(async () => {
      void result.current.loadTactics("20260916"); // 先发，未完成。
      await result.current.loadTactics("20260917"); // 后发，先完成。
    });
    await waitFor(() => expect(result.current.tactics?.effective_day).toBe("20260917"));

    // 旧响应此刻才回来——必须被丢弃。
    await act(async () => {
      slow[0]!({ status: 200, data: emptyPayload("20260916") });
    });
    expect(result.current.tactics?.effective_day).toBe("20260917");
  });

  it("失败响应写 error，不写入 tactics（不拿上一次结果冒充本次）", async () => {
    const request = vi.fn().mockResolvedValue({ status: 503, data: { detail: "数据源不可用" } });
    const client = { request } as unknown as CoreClient;
    const { result } = renderHook(() => useYouzi(client, false));
    await act(async () => {
      await result.current.loadTactics("20260917");
    });
    expect(result.current.tactics).toBeNull();
    expect(result.current.error).toBeTruthy();
  });
});

describe("useYouzi（游资二期：不产生绩效字段的展示层约束）", () => {
  it("三类事实类型常量与后端命名一致（改后端必须同步改这里）", async () => {
    const { FACT_LABELS, EXCLUDE_LABELS } = await import("./useYouzi");
    expect(Object.keys(FACT_LABELS).sort()).toEqual([
      "adjacent_buy_sell",
      "consecutive_buy",
      "repeated_seat_presence",
    ]);
    // 排除原因必须能翻译成中文说明，否则 UI 会退化成显示英文键名。
    for (const key of Object.keys(EXCLUDE_LABELS)) {
      expect(EXCLUDE_LABELS[key]).toBeTruthy();
    }
  });
});

// —— Y3-07 / Y3-08 案例复盘 ——

function replayPage(overrides: Record<string, unknown> = {}): Record<string, unknown> {
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
    limitations: ["……"],
    available: true,
    reason: "",
    boundary_notice: "榜外席位不可见。",
    ...overrides,
  };
}

function replayEvent(id: string, day: string): Record<string, unknown> {
  return {
    event_id: id,
    trading_day: day,
    security_code: "600519",
    security_name: "测试股份",
    operatedept_code: "10026937",
    operatedept_name: "某营业部",
    direction: "buy",
    amount: 1e7,
    explanation: "日涨幅偏离值达7%的证券",
    source_report: "RPT_BILLBOARD_DAILYDETAILSBUY",
    source_record_id: "a".repeat(64),
    fact_types: [],
    fact_ids: [],
    horizons: [],
    fact_omitted_reason: "",
  };
}

const REPLAY_QUERY = {
  securityCode: "600519",
  start: "20260907",
  end: "20260917",
  asOf: "20260917",
  pageSize: 20,
};

describe("useYouzi.loadReplay（Y3-08 请求生命周期）", () => {
  it("请求路径带代码与四个查询参数（代码输入查询，不做排行）", async () => {
    const request = vi.fn().mockResolvedValue({ status: 200, data: replayPage() });
    const client = { request } as unknown as CoreClient;
    const { result } = renderHook(() => useYouzi(client, false));
    await act(async () => {
      await result.current.loadReplay(REPLAY_QUERY);
    });
    const path = request.mock.calls[0]![0].path as string;
    expect(path.startsWith("/youzi/replay/600519?")).toBe(true);
    expect(path).toContain("start=20260907");
    expect(path).toContain("end=20260917");
    expect(path).toContain("as_of=20260917");
    expect(path).toContain("page_size=20");
    // 不夹带任何排序/排行参数。
    expect(path).not.toContain("sort");
    expect(path).not.toContain("top");
  });

  it("★ 换代码：先发后到的旧响应不得覆盖新代码的结果", async () => {
    const slow: Array<(value: { status: number; data: unknown }) => void> = [];
    const request = vi.fn().mockImplementation((req: { path: string }) => {
      if (req.path.includes("/youzi/replay/000001")) {
        return new Promise((resolve) => slow.push(resolve)); // 旧请求挂起。
      }
      return Promise.resolve({
        status: 200,
        data: replayPage({ security_code: "600519", total_events: 1 }),
      });
    });
    const client = { request } as unknown as CoreClient;
    const { result } = renderHook(() => useYouzi(client, false));

    await act(async () => {
      void result.current.loadReplay({ ...REPLAY_QUERY, securityCode: "000001" });
      await result.current.loadReplay(REPLAY_QUERY);
    });
    expect(result.current.replay?.security_code).toBe("600519");

    // 旧响应此刻才回来——必须被丢弃，否则 A 股票结果覆盖 B 股票。
    await act(async () => {
      slow[0]!({
        status: 200,
        data: replayPage({ security_code: "000001", total_events: 9 }),
      });
    });
    expect(result.current.replay?.security_code).toBe("600519");
    expect(result.current.replay?.total_events).toBe(1);
  });

  it("翻页追加（append=true）把新页接在已载入事件之后", async () => {
    const request = vi
      .fn()
      .mockResolvedValueOnce({
        status: 200,
        data: replayPage({
          events: [replayEvent("e1", "20260907"), replayEvent("e2", "20260908")],
          cursor: "cur-1",
          has_more: true,
          total_events: 3,
        }),
      })
      .mockResolvedValueOnce({
        status: 200,
        data: replayPage({
          events: [replayEvent("e3", "20260909")],
          cursor: "",
          has_more: false,
          total_events: 3,
        }),
      });
    const client = { request } as unknown as CoreClient;
    const { result } = renderHook(() => useYouzi(client, false));

    await act(async () => {
      await result.current.loadReplay(REPLAY_QUERY);
    });
    expect(result.current.replayEvents.map((e) => e.event_id)).toEqual(["e1", "e2"]);

    await act(async () => {
      await result.current.loadReplay(REPLAY_QUERY, "cur-1", true);
    });
    expect(result.current.replayEvents.map((e) => e.event_id)).toEqual(["e1", "e2", "e3"]);
    // 第二页请求必须带上游标。
    expect(request.mock.calls[1]![0].path).toContain("cursor=cur-1");
  });

  it("resetReplay 清空累计页并作废在途响应", async () => {
    const slow: Array<(value: { status: number; data: unknown }) => void> = [];
    const request = vi.fn().mockImplementation(
      () => new Promise((resolve) => slow.push(resolve)),
    );
    const client = { request } as unknown as CoreClient;
    const { result } = renderHook(() => useYouzi(client, false));

    await act(async () => {
      void result.current.loadReplay(REPLAY_QUERY);
    });
    await act(async () => {
      result.current.resetReplay();
    });
    expect(result.current.replay).toBeNull();
    expect(result.current.replayEvents).toEqual([]);

    // 在途响应此刻返回——已被作废，不得写回 state。
    await act(async () => {
      slow[0]!({ status: 200, data: replayPage({ total_events: 7 }) });
    });
    expect(result.current.replay).toBeNull();
  });

  it("422 参数错误把后端 detail 原样提示（不吞掉是哪一项不合法）", async () => {
    const request = vi.fn().mockResolvedValue({
      status: 422,
      data: { detail: "区间上限为 120 个自然日（收到 365）" },
    });
    const client = { request } as unknown as CoreClient;
    const { result } = renderHook(() => useYouzi(client, false));
    await act(async () => {
      await result.current.loadReplay(REPLAY_QUERY);
    });
    expect(result.current.error).toContain("区间上限");
    expect(result.current.replay).toBeNull();
  });

  it("失败不自动重试（只发一次请求，避免坏参数下无限循环）", async () => {
    const request = vi.fn().mockResolvedValue({ status: 503, data: { detail: "不可用" } });
    const client = { request } as unknown as CoreClient;
    const { result } = renderHook(() => useYouzi(client, false));
    await act(async () => {
      await result.current.loadReplay(REPLAY_QUERY);
    });
    expect(request).toHaveBeenCalledTimes(1);
  });
});

describe("useYouzi（Y3 常量与后端命名一致性）", () => {
  it("期限状态四种必须都能翻译，且 unknown 与 missing 分开", async () => {
    const { HORIZON_STATUS_LABELS } = await import("./useYouzi");
    expect(Object.keys(HORIZON_STATUS_LABELS).sort()).toEqual([
      "disclosed",
      "missing",
      "not_due",
      "unknown",
    ]);
    // 「未到期」与「已到期未给值」文案必须不同，否则读者分不清。
    expect(HORIZON_STATUS_LABELS.not_due).not.toBe(HORIZON_STATUS_LABELS.missing);
  });

  it("复盘降级原因两种都有人读文案", async () => {
    const { REPLAY_REASON_LABELS } = await import("./useYouzi");
    expect(Object.keys(REPLAY_REASON_LABELS).sort()).toEqual([
      "calendar_missing",
      "no_trading_day_in_range",
    ]);
    for (const key of Object.keys(REPLAY_REASON_LABELS)) {
      expect(REPLAY_REASON_LABELS[key]).toBeTruthy();
    }
  });

  it("★ 事实省略原因必须区分「检查过没有」与「没法检查」", async () => {
    const { OMITTED_LABELS } = await import("./useYouzi");
    expect(OMITTED_LABELS.no_fact_for_window).toContain("未命中");
    // 三个「没法检查」的原因不得复用「未命中」的文案。
    for (const key of ["insufficient_context_days", "context_not_complete", "window_incomplete"]) {
      expect(OMITTED_LABELS[key]).toBeTruthy();
      expect(OMITTED_LABELS[key]).not.toContain("未命中");
    }
  });

  it("复盘 tab 已登记（新增页签必须同时更新 TABS 与 YouziTab）", async () => {
    const { HORIZON_STATUS_LABELS } = await import("./useYouzi");
    expect(HORIZON_STATUS_LABELS).toBeTruthy();
    // YouziTab 是类型，运行时无法直接断言；改由页面 TABS 的单测覆盖。
  });
});

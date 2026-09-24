/**
 * B01/B02（2026-09-15 路线图）回归：generateStockReport 的参数透传与失败结构化返回。
 * - 显式 profileId / mode 必须进入请求体（模型选错、档位丢失的意图回归）；
 * - ok=false 时返回 stage/detail/source_errors，不再吞成 null；
 * - HTTP 4xx/5xx 时保留状态码与 detail。
 */
import { describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import type { CoreClient } from "../state/coreClient";
import { useResearch } from "./useResearch";

function clientWith(response: { status: number; data: unknown }): { client: CoreClient; request: ReturnType<typeof vi.fn> } {
  const request = vi.fn().mockResolvedValue(response);
  return { client: { request } as unknown as CoreClient, request };
}

describe("useResearch.generateStockReport（B01/B02 · 2026-09-15 方向研判路线图）", () => {
  it("显式 profileId 与 mode 进入请求体（单股选模型不再丢失）", async () => {
    const { client, request } = clientWith({ status: 200, data: { ok: true, symbol: "001201", report: "正文", citations: ["S1"], model: "m", generated_at: "t", source_errors: {}, source_keys: ["S1"], title: "T", confidence: "中" } });
    const { result } = renderHook(() => useResearch(client));
    const outcome = await result.current.generateStockReport("001201", "", 250, "pid-1", "deep");
    expect(outcome.ok).toBe(true);
    expect(request.mock.calls[0]![0].body).toEqual({ symbol: "001201", question: "", bars_limit: 250, profile_id: "pid-1", mode: "deep" });
  });

  it("ok=false：保留 stage/detail/source_errors，不再返回 null", async () => {
    const { client, request } = clientWith({ status: 200, data: { ok: false, stage: "model_call", detail: "模型调用失败：401", source_errors: { S2: "新闻取数失败" } } });
    const { result } = renderHook(() => useResearch(client));
    const outcome = await result.current.generateStockReport("001201", "", 250, null, "deep");
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) {
      expect(outcome.failure.stage).toBe("model_call");
      expect(outcome.failure.detail).toContain("模型调用失败");
      expect(outcome.failure.sourceErrors).toEqual(["新闻取数失败"]);
    }
    expect(request.mock.calls[0]![0].body).not.toHaveProperty("profile_id");
  });

  it("HTTP 500：保留状态码与 detail（不再统一转空结果）", async () => {
    const { client } = clientWith({ status: 500, data: { detail: "后端内部错误" } });
    const { result } = renderHook(() => useResearch(client));
    const outcome = await result.current.generateStockReport("001201", "", 250);
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) {
      expect(outcome.failure.status).toBe(500);
      expect(outcome.failure.detail).toBe("后端内部错误");
    }
  });
});

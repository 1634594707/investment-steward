/**
 * A01（前端设计与架构优化任务路线图 2026-09-19）：书架状态唯一所有者的回归。
 * 插件安装后的重载信号必须命中当前持有书架的那份 useLibrary（LibraryPage），
 * 卸载后不再响应——用于防止重新引入壳层那份无人读取的第二个实例（方案 E10）。
 */
import { describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { CoreClient } from "../state/coreClient";
import { requestLibraryReload } from "../state/libraryRefresh";
import { useLibrary } from "./useLibrary";

function shelfClient() {
  const request = vi.fn().mockResolvedValue({ status: 200, data: [] });
  return { client: { isDemo: false, request } as unknown as CoreClient, request };
}

function shelfCalls(request: ReturnType<typeof vi.fn>) {
  return request.mock.calls.filter((call) => (call[0] as { path: string }).path === "/library/books");
}

describe("useLibrary 书架重载信号（A01 · 前端设计与架构优化任务路线图 2026-09-19）", () => {
  it("挂载即自取一次书架", async () => {
    const { client, request } = shelfClient();
    renderHook(() => useLibrary(client, "ready", true, true));
    await waitFor(() => expect(shelfCalls(request)).toHaveLength(1));
  });

  it("requestLibraryReload 让持有者重拉书架（插件安装后刷新用户看到的书架）", async () => {
    const { client, request } = shelfClient();
    renderHook(() => useLibrary(client, "ready", true, true));
    await waitFor(() => expect(shelfCalls(request)).toHaveLength(1));
    act(() => requestLibraryReload());
    await waitFor(() => expect(shelfCalls(request)).toHaveLength(2));
  });

  it("页面隐藏时停表：不首拉也不因信号发请求，回到页面补拉一次（A03）", async () => {
    const { client, request } = shelfClient();
    const { rerender } = renderHook(({ active }) => useLibrary(client, "ready", true, active), { initialProps: { active: false } });
    await new Promise((resolve) => setTimeout(resolve, 20));
    act(() => requestLibraryReload());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(shelfCalls(request)).toHaveLength(0);
    rerender({ active: true });
    await waitFor(() => expect(shelfCalls(request)).toHaveLength(1));
  });

  it("持有者卸载后不再响应信号（不为不可见的书架发请求）", async () => {
    const { client, request } = shelfClient();
    const { unmount } = renderHook(() => useLibrary(client, "ready", true, true));
    await waitFor(() => expect(shelfCalls(request)).toHaveLength(1));
    unmount();
    act(() => requestLibraryReload());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(shelfCalls(request)).toHaveLength(1);
  });
});

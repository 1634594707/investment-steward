import { useEffect, useState } from "react";
import type { Book, LibraryPlan } from "@investment-steward/domain-contracts";
import type { CoreClient, CoreConnection } from "../state/coreClient";
import { subscribeLibraryReload } from "../state/libraryRefresh";

/**
 * B2（frontend-optimization-roadmap-2026-09-12）：研读图书馆域数据，自 AppShell 原样下沉。
 * A01（前端设计与架构优化任务路线图 2026-09-19）：本钩子由 LibraryPage 单点持有，是书架状态的唯一所有者；
 * 书架在 client/connection/revision 变化时重载，并订阅 libraryRefresh 的跨域重载信号（插件安装）。
 * 认知档案走当前「使用中」模型方案。批注转研究（annotationToResearch）仍由 AppShell 承担。
 */

/** 图书馆 M5.5 认知档案响应。 */
export interface LibraryInsightsResult {
  ok: boolean;
  stage?: string;
  detail?: string;
  insights?: string | null;
  citations?: string[];
  book_count?: number;
  model?: string;
  latency_ms?: number;
  generated_at?: string;
}

const AI_GATE_DETAIL = "AI 未接通：请先在 设置 → 模型配置 添加方案、粘贴密钥并设为「使用中」。";

export function useLibrary(client: CoreClient, connection: CoreConnection, aiReady: boolean, active = true) {
  const [books, setBooks] = useState<Book[]>([]);
  const [booksLoading, setBooksLoading] = useState(false);
  const [booksError, setBooksError] = useState<string | null>(null);
  const [libraryRevision, setLibraryRevision] = useState(0);
  const [readingPlan, setReadingPlan] = useState<LibraryPlan | null>(null);

  useEffect(() => {
    // A03（前端设计与架构优化任务路线图 2026-09-19）：页面级 `active` 门控——KeepAlive 下切走视图
    // 不改 visibilityState，窗口的 `isDocumentVisible()` 挡不住隐藏页轮询；重载信号在隐藏期照样累加
    // revision，回到页面时由本 effect 补拉一次。
    if (client.isDemo || !active) {
      return;
    }
    let cancelled = false;
    setBooksLoading(true);
    setBooksError(null);
    void (async () => {
      try {
        const response = await client.request<Book[]>({ method: "GET", path: "/library/books" });
        if (cancelled) return;
        if (response.status >= 400) setBooksError("书架读取失败，请重试。");
        else setBooks(response.data);
      } catch {
        if (!cancelled) setBooksError("书架读取失败，请重试。");
      } finally {
        if (!cancelled) setBooksLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [client, connection, libraryRevision, active]);

  /** 插件安装等外部事件触发书架重载（libraryRefresh 信号与页面「重试」都走这里）。 */
  function bumpRevision(): void {
    setLibraryRevision((value) => value + 1);
  }

  // A01（前端设计与架构优化任务路线图 2026-09-19）：跨域重载信号在此订阅，
  // 使插件安装后的刷新命中用户当前看到的书架，而不是壳层那份无消费者的实例（方案 E10）。
  useEffect(() => subscribeLibraryReload(bumpRevision), []);

  async function createBook(input: { title: string; author: string; isbn?: string | null; status: Book["status"]; source?: Book["source"] }): Promise<Book | null> {
    const response = await client.request<Book>({ method: "POST", path: "/library/books", body: input });
    if (response.status >= 400) return null;
    setBooks((current) => [...current, response.data]);
    return response.data;
  }

  async function updateBook(bookId: string, input: { title: string; author: string; isbn?: string | null; progress: number; notes: string[]; status: Book["status"]; source?: Book["source"] }): Promise<Book | null> {
    const response = await client.request<Book>({ method: "PUT", path: `/library/books/${bookId}`, body: input });
    if (response.status >= 400) return null;
    setBooks((current) => current.map((item) => item.book_id === bookId ? response.data : item));
    return response.data;
  }

  async function deleteBook(bookId: string): Promise<boolean> {
    const response = await client.request<unknown>({ method: "DELETE", path: `/library/books/${bookId}` });
    if (response.status >= 400) return false;
    setBooks((current) => current.filter((item) => item.book_id !== bookId));
    return true;
  }

  async function lookupIsbn(isbn: string): Promise<{ available: boolean; title?: string; publishers?: string[]; pages?: number; source?: string; degraded_reason?: string } | null> {
    const response = await client.request<{ available: boolean; title?: string; publishers?: string[]; pages?: number; source?: string; degraded_reason?: string }>({
      method: "GET",
      path: `/library/books/lookup/${encodeURIComponent(isbn)}`,
    });
    return response.status < 400 ? response.data : null;
  }

  async function generatePlan(sourceBookId?: string | null): Promise<LibraryPlan | null> {
    const response = await client.request<LibraryPlan>({
      method: "POST",
      path: "/library/plan/generate",
      body: { source_book_id: sourceBookId ?? null },
    });
    if (response.status >= 400) return null;
    setReadingPlan(response.data);
    return response.data;
  }

  async function generateLibraryInsights(): Promise<LibraryInsightsResult | null> {
    if (!aiReady) return { ok: false, stage: "model_not_configured", detail: AI_GATE_DETAIL };
    const response = await client.request<LibraryInsightsResult>({ method: "POST", path: "/library/insights" });
    if (response.status >= 400) return null;
    return response.data;
  }

  return {
    books, booksLoading, booksError, readingPlan,
    bumpRevision, createBook, updateBook, deleteBook, lookupIsbn, generatePlan, generateLibraryInsights,
  };
}

import { useEffect, useState, useRef } from "react";
import type { CoreClient } from "../state/coreClient";
import { classifyCoreError, detailOf } from "../state/coreClient";
import { toast } from "../state/toastStore";
import type { TacticMeta, ScanResult, MarketScanResult, WatchEntry, SignalsResult, NoteEntry, SectorMeta, AiReviewRecord } from "../routes/TacticsPage";

/**
 * B2（frontend-optimization-roadmap-2026-09-12）：战法雷达领域数据，自 AppShell 原样下沉。
 * TacticsPage 直接消费本钩子；跨页联动（externalPool / onOpenStockReport / trackedInstruments）
 * 仍由 AppShell 承担，自选入库复用 AppShell 的 createHolding（与投资页同一份清单）。
 */

export function useTactics(
  client: CoreClient,
  active: boolean,
  aiReady: boolean,
  createHolding: (input: { instrument: string; label: string; status: "watchlist"; strategy_note: string }) => Promise<boolean>,
) {
  // 战法雷达（official.stock-tactics）：目录 / 扫描 / 观察清单 / 单票信号 / 笔记。
  const [tacticsCatalog, setTacticsCatalog] = useState<TacticMeta[]>([]);
  const [tacticsScan, setTacticsScan] = useState<ScanResult | null>(null);
  const [tacticsScanBusy, setTacticsScanBusy] = useState(false);
  const [tacticsMarketScan, setTacticsMarketScan] = useState<MarketScanResult | null>(null);
  const [tacticsMarketBusy, setTacticsMarketBusy] = useState(false);
  // D01（桌面端升级路线图 2026-09-18）：扫描任务化——进度（第 N/M 只）与在途任务 id（供停止）。
  const [tacticsMarketProgress, setTacticsMarketProgress] = useState<{ done: number; total: number } | null>(null);
  const marketScanJobRef = useRef<string | null>(null);
  const [tacticsWatchlist, setTacticsWatchlist] = useState<WatchEntry[]>([]);
  const [tacticsSignals, setTacticsSignals] = useState<SignalsResult | null>(null);
  const [tacticsSignalsLoading, setTacticsSignalsLoading] = useState<string | null>(null);
  const [tacticsNotes, setTacticsNotes] = useState<NoteEntry[]>([]);
  // v26 行业板块扫描 + AI 复核（手动触发，落库留痕）。
  const [tacticsSectors, setTacticsSectors] = useState<SectorMeta[]>([]);
  const [tacticsSectorsLoading, setTacticsSectorsLoading] = useState(false);
  const [tacticsAiReviews, setTacticsAiReviews] = useState<AiReviewRecord[]>([]);
  /** 正在复核的标的代码（null = 空闲）；逐票串行，避免并发打模型。 */
  const [tacticsAiReviewBusy, setTacticsAiReviewBusy] = useState<string | null>(null);

  async function loadTacticsWatchlist(): Promise<void> {
    const response = await client.request<WatchEntry[]>({ method: "GET", path: "/tactics/watchlist" });
    if (response.status < 400 && Array.isArray(response.data)) setTacticsWatchlist(response.data);
  }

  async function saveTacticsWatch(symbol: string, name: string, note: string): Promise<boolean> {
    const response = await client.request<unknown>({ method: "PUT", path: `/tactics/watchlist/${encodeURIComponent(symbol)}`, body: { name, note } });
    if (response.status >= 400) { toast.err("加入观察失败，请重试。"); return false; }
    await loadTacticsWatchlist();
    return true;
  }

  async function deleteTacticsWatch(symbol: string): Promise<boolean> {
    const response = await client.request<unknown>({ method: "DELETE", path: `/tactics/watchlist/${encodeURIComponent(symbol)}` });
    if (response.status >= 400) { toast.err("移除失败，请重试。"); return false; }
    await loadTacticsWatchlist();
    return true;
  }

  async function runTacticsScan(sources: string[], symbols: string[]): Promise<void> {
    setTacticsScanBusy(true);
    try {
      const response = await client.request<ScanResult>({
        method: "POST", path: "/tactics/scan", body: { sources, symbols },
      });
      if (response.status < 400 && response.data) setTacticsScan(response.data);
      else toast.err(`扫描失败（${classifyCoreError(response.status, detailOf(response.data)).message}）`);
    } catch {
      toast.err("扫描请求失败，请重试。");
    } finally {
      setTacticsScanBusy(false);
    }
  }

  /** D01：扫描任务统一执行器——POST 建任务 → 轮询进度（1.2s）→ 终态写结果/停止。
   * 停止（stopMarketScan）清掉 jobRef 后轮询循环自行退出。 */
  async function runMarketScanJob(body: Record<string, unknown>, failureLabel: string): Promise<void> {
    setTacticsMarketBusy(true);
    setTacticsMarketProgress(null);
    try {
      const start = await client.request<{ ok: boolean; job_id: string; state: string; reused?: boolean; done?: number; total?: number }>({
        method: "POST", path: "/tactics/scan-market", body,
      });
      if (start.status >= 400 || !start.data?.ok) {
        toast.err(`${failureLabel}（${classifyCoreError(start.status, detailOf(start.data)).message}）`);
        return;
      }
      const jobId = start.data.job_id;
      marketScanJobRef.current = jobId;
      if (start.data.reused) setTacticsMarketProgress({ done: start.data.done ?? 0, total: start.data.total ?? 0 });
      for (;;) {
        await new Promise((resolve) => setTimeout(resolve, 1200));
        if (marketScanJobRef.current !== jobId) return; // 已被停止/清理
        const status = await client.request<{ ok: boolean; state: string; done: number; total: number; summary: MarketScanResult | null; error: { detail?: string } | null }>({
          method: "GET", path: `/tactics/scan-market/${jobId}`,
        });
        if (status.status >= 400 || !status.data?.ok) {
          toast.err(`扫描进度获取失败（HTTP ${status.status}）`);
          return;
        }
        const job = status.data;
        if (job.state === "running") {
          setTacticsMarketProgress({ done: job.done, total: job.total });
          continue;
        }
        if (job.state === "done" && job.summary) {
          setTacticsMarketScan(job.summary);
          return;
        }
        if (job.state === "cancelled") {
          toast.err("扫描已停止（已完成的票不计入结果）。");
          return;
        }
        toast.err(`${failureLabel}：${job.error?.detail ?? "任务失败，请重试"}`);
        return;
      }
    } catch {
      toast.err(`${failureLabel}：请求失败，请重试。`);
    } finally {
      setTacticsMarketBusy(false);
      setTacticsMarketProgress(null);
      marketScanJobRef.current = null;
    }
  }

  /** D01：停止在途扫描——标记取消（后端逐票检测，停止后不再发新取数）并退出轮询。 */
  function stopMarketScan(): void {
    const jobId = marketScanJobRef.current;
    if (!jobId) return;
    marketScanJobRef.current = null;
    setTacticsMarketBusy(false);
    setTacticsMarketProgress(null);
    void client.request({ method: "POST", path: `/tactics/scan-market/${jobId}/cancel` }).catch(() => undefined);
  }

  async function runTacticsMarketScan(boards: string[], perBoard: number, maxSymbols: number, mode: "boards" | "all" = "boards", topSymbols = 40, segments: string[] = ["main", "chinext", "star"], priceMin: number | null = null, priceMax: number | null = null, tacticIds: string[] | null = null): Promise<void> {
    const body = mode === "all"
      ? { mode: "all", top_symbols: topSymbols, segments, price_min: priceMin, price_max: priceMax, tactic_ids: tacticIds }
      : { mode: "boards", boards, per_board: perBoard, max_symbols: maxSymbols, tactic_ids: tacticIds };
    await runMarketScanJob(body, "市场扫描失败");
  }

  /** v26 行业板块清单（GET /tactics/sectors）：板块扫描的选项来源，按当日涨跌幅降序。 */
  async function loadTacticsSectors(): Promise<void> {
    setTacticsSectorsLoading(true);
    try {
      const response = await client.request<{ sectors: SectorMeta[] }>({ method: "GET", path: "/tactics/sectors" });
      if (response.status < 400 && response.data?.sectors) setTacticsSectors(response.data.sectors);
      else toast.err(`行业板块清单获取失败（${classifyCoreError(response.status, detailOf(response.data)).message}）`);
    } catch {
      toast.err("行业板块请求失败，请重试。");
    } finally {
      setTacticsSectorsLoading(false);
    }
  }

  /** v26 板块扫描（mode=sectors）：选行业 → 拉成分股 → 跑引擎 → 板块内横截面共振 → 按板块分组返回。 */
  async function runTacticsSectorScan(codes: string[], perSector: number, resonance: boolean): Promise<void> {
    await runMarketScanJob(
      { mode: "sectors", sectors: codes, per_sector: perSector, sector_resonance: resonance },
      "板块扫描失败",
    );
  }

  /** v26 AI 复核（手动触发）：一次点击 = 一次模型调用；结果落库，规则分与 AI 分并列不互相覆盖。 */
  async function runTacticsAiReview(symbol: string, sectorCode: string | null, question: string): Promise<void> {
    if (!aiReady) {
      toast.err("AI 未接通：请先在 设置 → 模型配置 添加方案并设为「使用中」。");
      return;
    }
    setTacticsAiReviewBusy(symbol);
    try {
      const response = await client.request<{ ok: boolean; review: AiReviewRecord }>({
        method: "POST",
        path: "/tactics/ai-review",
        body: { symbol, ...(sectorCode ? { sector_code: sectorCode } : {}), ...(question ? { question } : {}) },
      });
      if (response.status >= 400 || !response.data?.ok) {
        // 502 的 detail 是「模型输出无法解析…原文片段」，直接回传，便于判断是提示词问题还是模型问题。
        toast.err(`AI 复核失败（${classifyCoreError(response.status, detailOf(response.data)).message}）`);
        return;
      }
      const review = response.data.review;
      setTacticsAiReviews((current) => [review, ...current.filter((item) => item.review_id !== review.review_id)]);
      toast.ok(`${symbol} 的 AI 复核已完成并留痕。`);
    } catch {
      toast.err("AI 复核请求失败，请重试。");
    } finally {
      setTacticsAiReviewBusy(null);
    }
  }

  /** v26 复核历史：进入战法雷达时拉最近 30 条，供长期研究回看（当时规则分 vs AI 判断）。 */
  async function loadTacticsAiReviews(): Promise<void> {
    // coreClient 的 CoreRequest 没有 query 字段（只认 path/body），查询串直接拼在 path 上。
    const response = await client.request<AiReviewRecord[]>({ method: "GET", path: "/tactics/ai-reviews?limit=30" });
    if (response.status < 400 && Array.isArray(response.data)) setTacticsAiReviews(response.data);
  }

  async function deleteTacticsAiReview(reviewId: string): Promise<void> {
    const response = await client.request({ method: "DELETE", path: `/tactics/ai-reviews/${reviewId}` });
    if (response.status >= 400) { toast.err("复核记录删除失败，请稍后重试。"); return; }
    setTacticsAiReviews((current) => current.filter((item) => item.review_id !== reviewId));
  }

  /** v26 加入投资模块自选：复用 POST /holdings（status=watchlist），与投资页共用同一份清单。 */
  async function addToWatchlist(symbol: string, name: string): Promise<boolean> {
    const ok = await createHolding({
      instrument: symbol,
      label: name || symbol,
      status: "watchlist",
      strategy_note: "来自战法雷达扫描结果，待长期研究。",
    });
    if (ok) toast.ok(`已加入自选：${name || symbol}（投资页「持仓与自选」可查看）。`);
    return ok;
  }

  async function loadTacticsSignals(symbol: string): Promise<void> {
    setTacticsSignalsLoading(symbol);
    try {
      const response = await client.request<SignalsResult>({ method: "GET", path: `/tactics/signals/${encodeURIComponent(symbol)}` });
      if (response.status < 400 && response.data) {
        setTacticsSignals(response.data);
        const notesRes = await client.request<NoteEntry[]>({ method: "GET", path: `/tactics/watchlist/${encodeURIComponent(symbol)}/notes` });
        setTacticsNotes(notesRes.status < 400 && Array.isArray(notesRes.data) ? notesRes.data : []);
      } else {
        toast.err(`信号识别失败（${classifyCoreError(response.status, detailOf(response.data)).message}）`);
      }
    } catch {
      toast.err("信号请求失败，请重试。");
    } finally {
      setTacticsSignalsLoading(null);
    }
  }

  async function addTacticsNote(symbol: string, content: string): Promise<boolean> {
    const response = await client.request<NoteEntry>({ method: "POST", path: `/tactics/watchlist/${encodeURIComponent(symbol)}/notes`, body: { content } });
    if (response.status >= 400) { toast.err("笔记保存失败，请重试。"); return false; }
    const notesRes = await client.request<NoteEntry[]>({ method: "GET", path: `/tactics/watchlist/${encodeURIComponent(symbol)}/notes` });
    setTacticsNotes(notesRes.status < 400 && Array.isArray(notesRes.data) ? notesRes.data : []);
    return true;
  }

  // 进入战法雷达时拉目录与观察清单。目录每次进入都重新拉（Core 重启可能新增战法，
  // 旧 state 永不刷新会让新战法在选择面板里缺席）——目录是小请求，无负担。
  useEffect(() => {
    if (!active) return;
    void client.request<TacticMeta[]>({ method: "GET", path: "/tactics/catalog" }).then((response) => {
      if (response.status < 400 && Array.isArray(response.data)) setTacticsCatalog(response.data);
    }).catch(() => undefined);
    void loadTacticsWatchlist();
    void loadTacticsAiReviews();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  return {
    tacticsCatalog, tacticsScan, tacticsScanBusy, tacticsMarketScan, tacticsMarketBusy, tacticsMarketProgress, stopMarketScan,
    tacticsWatchlist, tacticsSignals, tacticsSignalsLoading, tacticsNotes,
    tacticsSectors, tacticsSectorsLoading, tacticsAiReviews, tacticsAiReviewBusy,
    loadTacticsWatchlist, saveTacticsWatch, deleteTacticsWatch,
    runTacticsScan, runTacticsMarketScan, loadTacticsSectors, runTacticsSectorScan,
    runTacticsAiReview, loadTacticsAiReviews, deleteTacticsAiReview, addToWatchlist,
    loadTacticsSignals, addTacticsNote,
  };
}

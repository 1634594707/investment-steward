import { useRef, useState } from "react";
import type { CandleSeries } from "@investment-steward/domain-contracts";
import type { CoreClient, CoreRequest } from "../state/coreClient";
import { readCoreCache, useCorePoll, writeCoreCache } from "../state/useCoreQuery";

/** K 线周期（GET /market/candles/{symbol}?period=…）；timeframe 与 Core 返回口径一致。 */
export type CandlePeriod = "day" | "week" | "month";
export const CANDLE_TIMEFRAME: Record<CandlePeriod, string> = { day: "1d", week: "1w", month: "1mo" };

/** 持有/证据统一的标的编码（如 CN:ETF:510300 → 510300），对齐行情/证据端点的 symbol。 */
function marketCode(instrument: string): string {
  return instrument.split(":").pop() ?? "";
}

/**
 * B1（frontend-optimization-roadmap-2026-09-12）：行情 K 线领域数据。
 * 从 AppShell 下沉：序列状态 + 选择/周期切换（缓存秒开）+ 30s 自动刷新（useCorePoll，仅投资页且窗口可见时）。
 * 手写 candleCacheRef 改用 useCoreQuery 的模块级请求缓存。
 */
export function useCandles(client: CoreClient, active: boolean) {
  const [candles, setCandles] = useState<CandleSeries | null>(null);
  // K 线请求在途标记：区分「加载中」与「获取失败/插件未启用」，避免误报空态。
  const [candlesLoading, setCandlesLoading] = useState(false);
  // 当前行情标的代码与周期：周期切换时基于同一标的重拉。
  const [candleSymbol, setCandleSymbol] = useState<string | null>(null);
  const [candlePeriod, setCandlePeriod] = useState<CandlePeriod>("day");
  const requestSeq = useRef(0);

  async function selectInstrument(instrument: string, period?: CandlePeriod) {
    const code = marketCode(instrument);
    if (!code) return;
    const effectivePeriod = period ?? candlePeriod;
    const timeframe = CANDLE_TIMEFRAME[effectivePeriod];
    // 同标的同周期已有数据时不重复请求（点击持仓列表/周期按钮都会走到这里）。
    if (candles && marketCode(candles.instrument) === code && candles.timeframe === timeframe) return;
    const request: CoreRequest = { method: "GET", path: `/market/candles/${code}?period=${effectivePeriod}` };
    const requestSeqNow = ++requestSeq.current;
    setCandleSymbol(code);
    setCandlePeriod(effectivePeriod);
    // 缓存命中：立即呈现已取过的序列，零白屏（F2-3 语义，缓存移入 useCoreQuery 模块缓存）。
    const cached = readCoreCache<CandleSeries>(request);
    if (cached && marketCode(cached.instrument) === code && cached.timeframe === timeframe) {
      setCandles(cached);
      setCandlesLoading(false);
      return;
    }
    setCandles(null);
    setCandlesLoading(true);
    const response = await client.request<CandleSeries>(request);
    if (requestSeqNow !== requestSeq.current) return;
    setCandlesLoading(false);
    if (response.status < 400 && response.data && marketCode(response.data.instrument) === code) {
      writeCoreCache(request, response.data);
      setCandles(response.data);
    } else {
      setCandles(null);
    }
  }

  function changeCandlePeriod(period: CandlePeriod) {
    if (!candleSymbol || period === candlePeriod) return;
    void selectInstrument(candleSymbol, period);
  }

  // 30s 行情自动刷新（H3-1 语义收敛到 useCorePoll）：仅投资页可见且有标的时；窗口隐藏暂停。
  useCorePoll(async () => {
    const code = candleSymbol;
    const period = candlePeriod;
    if (!code) return;
    const request: CoreRequest = { method: "GET", path: `/market/candles/${code}?period=${period}` };
    const response = await client.request<CandleSeries>(request);
    if (response.status < 400 && response.data && marketCode(response.data.instrument) === code) {
      writeCoreCache(request, response.data);
      setCandles(response.data);
    }
  }, { intervalMs: 30000, enabled: active && !client.isDemo && !!candleSymbol });

  return {
    candles, setCandles,
    candlesLoading,
    candleSymbol, setCandleSymbol,
    candlePeriod, setCandlePeriod,
    selectInstrument, changeCandlePeriod,
  };
}

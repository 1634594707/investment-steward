import { useEffect, useMemo, useRef, useState } from "react";
import type { CandleSeries } from "@investment-steward/domain-contracts";
import { CandlestickSeries, ColorType, CrosshairMode, HistogramSeries, LineSeries, createChart } from "lightweight-charts";
import { formatDate } from "../state/format";
import type { CandlePeriod } from "../shell/AppShell";
import { useUiPrefs } from "../shell/uiprefs";

const PERIOD_LABEL: Record<CandlePeriod, string> = { day: "日K", week: "周K", month: "月K" };
const PERIOD_KICKER: Record<CandlePeriod, string> = { day: "MARKET VIEW / 1D", week: "MARKET VIEW / 1W", month: "MARKET VIEW / 1MO" };
const MA_HIDDEN_KEY = "steward.kline.ma-hidden";

/** invest.market_view 插槽的内核渲染器（K 线卡）。数据来自契约类型 CandleSeries，不来自具体插件代码。 */
export function KLineCard({ series, title, loading = false, period = "day", marketPluginEnabled = true, onManage, onSelectPeriod }: { series: CandleSeries | null; title?: string; loading?: boolean; period?: CandlePeriod; marketPluginEnabled?: boolean; onManage: () => void; onSelectPeriod?: (period: CandlePeriod) => void }) {
  return (
    <section className="kline-section">
      <div className="section-heading">
        <div>
          <span className="section-kicker">{PERIOD_KICKER[period]}</span>
          <h3>{title ?? series?.instrument ?? "标的行情"}</h3>
        </div>
        <div className="period-switch" role="group" aria-label="K 线周期">
          {(Object.keys(PERIOD_LABEL) as CandlePeriod[]).map((item) => (
            <button
              key={item}
              className={`period-chip ${period === item ? "active" : ""}`}
              aria-pressed={period === item}
              disabled={loading || !onSelectPeriod}
              onClick={() => onSelectPeriod?.(item)}
            >
              {PERIOD_LABEL[item]}
            </button>
          ))}
          <button className="text-button" onClick={onManage}>
            管理行情插件 <span>→</span>
          </button>
        </div>
      </div>
      {series ? (
        <>
          <KLineChart series={series} />
          <div className="data-line">
            <span className="source-dot" />
            {series.source_plugin_id} v{series.source_plugin_release}· {series.source_name}
            <span>·</span>
            {series.is_demo ? "演示数据" : "正式数据"}
            <time>截至 {formatDate(series.as_of)}</time>
          </div>
          <p className="chart-limit">{series.limitations[0]}</p>
        </>
      ) : loading ? (
        <div className="chart-pending" role="status" aria-live="polite">
          <span className="booting-spinner" aria-hidden="true" />
          <strong>行情加载中…</strong>
          <span>正在从本地 Core 拉取日 K 数据</span>
        </div>
      ) : marketPluginEnabled ? (
        <div className="chart-pending">
          <strong>行情暂不可用</strong>
          <span>行情插件已启用，但本次未取到数据（数据源冷却或标的代码无效）。可切换持仓或稍后重试。</span>
        </div>
      ) : (
        <div className="chart-empty">
          <strong>行情插件未启用</strong>
          <span>启用「中国市场行情」后，投资页会显示实时日 K 线（含当日蜡烛、多周期均线）。</span>
          <button className="primary-button" onClick={onManage}>
            打开扩展 <span>→</span>
          </button>
        </div>
      )}
    </section>
  );
}

const MA_WINDOWS: { window: number; color: string; label: string }[] = [
  { window: 5, color: "#7dc4ff", label: "MA5" },
  { window: 10, color: "#ffd27d", label: "MA10" },
  { window: 20, color: "#b9e6ca", label: "MA20" },
  { window: 40, color: "#d7a8ff", label: "MA40" },
];

interface BarPoint {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

/** F4-2：涨跌语义色从 CSS 令牌（--mkt-up / --mkt-down）读取，随设置页「红涨绿跌 / mint-amber」即时切换。 */
function readMarketColors(): { up: string; down: string } {
  const style = getComputedStyle(document.body);
  return {
    up: style.getPropertyValue("--mkt-up").trim() || "#e2726a",
    down: style.getPropertyValue("--mkt-down").trim() || "#5fb389",
  };
}

/** T01（2026-09-20）：K 线坐标轴文字/网格从令牌读取，随 body[data-theme] 切换（浅色主题下避免暗色文字不可读）。 */
function readChartThemeColors(): { text: string; grid: string; scaleBorder: string } {
  const style = getComputedStyle(document.body);
  const muted = style.getPropertyValue("--muted").trim() || "#9baaa3";
  return {
    text: muted,
    grid: withAlpha(muted.startsWith("#") ? muted : "#9baaa3", 0.18),
    scaleBorder: withAlpha(muted.startsWith("#") ? muted : "#9baaa3", 0.4),
  };
}

function withAlpha(hex: string, alpha: number): string {
  const value = hex.replace("#", "");
  const r = parseInt(value.slice(0, 2), 16);
  const g = parseInt(value.slice(2, 4), 16);
  const b = parseInt(value.slice(4, 6), 16);
  if ([r, g, b].some((n) => Number.isNaN(n))) return hex;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function movingAverage(bars: BarPoint[], window: number) {
  const out: { time: string; value: number }[] = [];
  let sum = 0;
  for (let i = 0; i < bars.length; i++) {
    sum += bars[i]!.close;
    if (i >= window - 1) {
      if (i >= window) sum -= bars[i - window]!.close;
      out.push({ time: bars[i]!.time, value: sum / window });
    }
  }
  return out;
}

function keyOf(time: unknown): string | null {
  if (typeof time === "string") return time;
  if (time && typeof time === "object") {
    const d = time as { year: number; month: number; day: number };
    return `${d.year}-${String(d.month).padStart(2, "0")}-${String(d.day).padStart(2, "0")}`;
  }
  if (typeof time === "number") return new Date(time * 1000).toISOString().slice(0, 10);
  return null;
}

/** 同花顺式日 K：蜡烛 + 成交量副图 + 多条均线；滚轮缩放、拖拽平移、悬停十字线看 OHLCV 详情。 */
function KLineChart({ series }: { series: CandleSeries }) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const legendRef = useRef<HTMLDivElement | null>(null);
  const prefs = useUiPrefs();
  // F4-4 MA 均线开关：按均线逐条开关，记忆在本机（点图例切换）。
  const [hiddenMa, setHiddenMa] = useState<number[]>(() => {
    try {
      const raw: unknown = JSON.parse(localStorage.getItem(MA_HIDDEN_KEY) ?? "[]");
      return Array.isArray(raw) ? raw.filter((item): item is number => typeof item === "number") : [];
    } catch {
      return [];
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(MA_HIDDEN_KEY, JSON.stringify(hiddenMa));
    } catch {
      /* 存储不可用时仅本会话生效 */
    }
  }, [hiddenMa]);

  const activeMa = useMemo(() => MA_WINDOWS.filter((ma) => !hiddenMa.includes(ma.window)), [hiddenMa]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const bars: BarPoint[] = series.bars.map((bar) => ({
      time: bar.timestamp.slice(0, 10),
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
      volume: bar.volume,
    }));
    if (bars.length === 0) return;

    const market = readMarketColors();
    const theme = readChartThemeColors();
    const chart = createChart(host, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: theme.text,
        fontSize: 10,
        fontFamily: "'JetBrains Mono', 'Noto Sans SC', monospace",
      },
      grid: {
        vertLines: { color: theme.grid },
        horzLines: { color: theme.grid },
      },
      rightPriceScale: { borderColor: theme.scaleBorder },
      timeScale: { borderColor: theme.scaleBorder, timeVisible: false, rightOffset: 3 },
      crosshair: { mode: CrosshairMode.Normal },
    });

    const candle = chart.addSeries(CandlestickSeries, {
      upColor: market.up,
      downColor: market.down,
      borderUpColor: market.up,
      borderDownColor: market.down,
      wickUpColor: market.up,
      wickDownColor: market.down,
    });
    candle.setData(bars.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));

    const volume = chart.addSeries(HistogramSeries, { priceScaleId: "", priceFormat: { type: "volume" } });
    chart.priceScale("").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    volume.setData(
      bars.map((bar) => ({
        time: bar.time,
        value: bar.volume,
        color: bar.close >= bar.open ? withAlpha(market.up, 0.45) : withAlpha(market.down, 0.45),
      })),
    );

    for (const { window, color } of activeMa) {
      const line = chart.addSeries(LineSeries, {
        color,
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
      line.setData(movingAverage(bars, window));
    }

    const barByTime = new Map(bars.map((bar) => [bar.time, bar] as const));
    function renderLegend(key: string | null) {
      const el = legendRef.current;
      if (!el) return;
      const bar = key ? barByTime.get(key) : undefined;
      const b = bar ?? bars[bars.length - 1];
      if (!b) {
        el.textContent = "";
        return;
      }
      const up = b.close >= b.open;
      el.innerHTML = [
        `<b>${b.time}</b>`,
        `<span class="leg-o">开 ${b.open.toFixed(3)}</span>`,
        `<span>高 ${b.high.toFixed(3)}</span>`,
        `<span>低 ${b.low.toFixed(3)}</span>`,
        `<span class="leg-c-${up ? "up" : "down"}">收 ${b.close.toFixed(3)}</span>`,
        `<span class="leg-v">量 ${b.volume.toLocaleString()}</span>`,
      ].join("");
    }

    chart.subscribeCrosshairMove((param) => {
      const key = keyOf(param.time);
      renderLegend(key ? (barByTime.has(key) ? key : null) : null);
    });
    renderLegend(null);

    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [series, activeMa, prefs.marketColors, prefs.theme]);

  return (
    <div className="kline-chart">
      <div className="kline-legend" ref={legendRef} />
      <div className="ma-legend">
        {MA_WINDOWS.map(({ window, color, label }) => {
          const off = hiddenMa.includes(window);
          return (
            <button
              key={window}
              type="button"
              className={`ma-toggle ${off ? "off" : ""}`}
              aria-pressed={!off}
              title={off ? `显示 ${label}` : `隐藏 ${label}`}
              onClick={() => setHiddenMa((current) => (current.includes(window) ? current.filter((item) => item !== window) : [...current, window]))}
            >
              <i style={{ background: color }} />
              <span>{label}</span>
            </button>
          );
        })}
      </div>
      <div className="lwc-host" ref={hostRef} />
    </div>
  );
}

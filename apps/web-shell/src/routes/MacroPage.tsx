import { useEffect, useMemo, useRef, useState } from "react";
import type { MacroSnapshot } from "@investment-steward/domain-contracts";
import { createCoreClient } from "../state/coreClient";
import { useMacro, type MacroAnalysisResult, type MacroCalendarSnapshot, type MacroEventImpact, type MacroPricingSnapshot, type MacroReleaseBoard, type MacroTradePoint, type MacroUserView, type SpeakerSignal, type SpeakerSignalInput, type WeightProposalResponse } from "../hooks/useMacro";

import { X, RotateCcw } from "lucide-react";
import "./macro.css";

interface Props {
  onNavigate: (view: "research") => void;
  isDemo: boolean;
  /** official.macro-radar 是否已启用：未启用时 /evidence/macro/* 全部 409（快照/定价/日历）。 */
  macroPluginEnabled?: boolean;
  /** 一键启用宏观雷达插件；成功后 useMacro 会立即重拉各国快照（数据落库 macro_cache，不反复外拉）。 */
  onEnablePlugin?: () => void;
  /** B2：当前视图是否为宏观页（KeepAlive 常驻挂载，12h TTL 重拉以它为依据）。 */
  active: boolean;
  /** M2-03：AI 前置就绪 = 存在「使用中」模型方案；未就绪时模型入口禁用。 */
  aiReady?: boolean;
}

interface ComtradePreview {
  available: boolean;
  source?: string;
  source_url?: string;
  query?: Record<string, string>;
  count?: number;
  data: Array<Record<string, unknown>>;
  as_of?: string;
  degraded_reason?: string;
}

/**
 * 宏观雷达影响图 v3 —— 世界地图版（D-17 定稿：ECharts geo + lines + effectScatter）：
 * - 底图：DataV 世界陆域边界（高德数据 · GS 审图号），存于 public/geo/ 本地离线加载，不依赖 CDN；
 *   采用陆域轮廓而非国界划分，无边界划分语义即无错画风险（地图合规红线）。
 * - 节点大小 ∝ 贸易体量，弧线宽 ∝ 流量，双口径并列（CIF/FOB 不抹平）；
 * - 事件锚点（俄乌 / 美伊）只呈现可核验指标影响链，无任何战况数字（D-22）；
 * - 全部数值实测于 2026-09-05（世界银行 / UN Comtrade / FRED），数据管道（trade.cache，G0）接入后由真实端点替换。
 */
import * as echarts from "echarts/core";
import { EffectScatterChart, LinesChart } from "echarts/charts";
import { GeoComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { DataMetaBadge } from "../components/DataMetaBadge";

echarts.use([EffectScatterChart, LinesChart, GeoComponent, TooltipComponent, CanvasRenderer]);

interface MapEntity { id: string; name: string; lon: number; lat: number; size: number; color: string; kind: "node" | "event"; role: string; headline: string; detail: string; metrics: Array<[string, string]>; }
const NODES: MapEntity[] = [
  { id: "us", name: "美国", lon: -98, lat: 39, size: 30, color: "#9cbad5", kind: "node", role: "金融中心", headline: "金融中心 · 深度逆差", detail: "外部余额深度为负、上市公司市值全球最深、信贷依赖低——靠资本市场而非银行给经济供血。", metrics: [["外部余额 / GDP", "−3.07%（2024 · 世行）"], ["上市公司市值 / GDP", "224.0%（2025 · 世行）"], ["私营部门信贷 / GDP", "44.5%（2025 · 世行）"]] },
  { id: "eu", name: "欧洲", lon: 10, lat: 51, size: 26, color: "#b9e6ca", kind: "node", role: "欧元区 EMU", headline: "欧元区 · 顺差但增长停滞", detail: "外部余额为正但 GDP 增速低于 1.5%。贸易线将走德国代理口径——EU-27 聚合（reporter=97）在 Comtrade 库内本身残缺，与是否订阅无关（实测 0.6 / 13.9 亿）。", metrics: [["GDP 实际增速", "+1.41%（2025 · 世行）"], ["外部余额 / GDP", "+3.88%（2025 · 世行）"], ["制造业增加值 / GDP", "14.11%（2025 · 世行）"]] },
  { id: "cn", name: "中国", lon: 104, lat: 35, size: 34, color: "#7dc4ff", kind: "node", role: "制造业中心", headline: "制造业中心 · 全球最大双边顺差", detail: "制造业占 GDP 近四分之一、外部余额顺差走阔、信贷驱动；对美顺差为全球最大双边失衡（双口径差 419 亿）。", metrics: [["外部余额 / GDP", "+4.19%（2025 · 世行）"], ["制造业增加值 / GDP", "24.73%（2025 · 世行）"], ["私营部门信贷 / GDP", "194.3%（2025 · 世行）"]] },
  { id: "jp", name: "日本", lon: 138, lat: 36, size: 24, color: "#ffd27d", kind: "node", role: "制造 + 金融", headline: "对美顺差 · 对华逆差", detail: "典型「中间品加工 + 终端市场在美」结构：对美顺差、对华逆差。2024 年 GDP 负增长。", metrics: [["GDP 实际增速", "+1.19%（2025）· −0.24%（2024）"], ["上市公司市值 / GDP", "171.6%（2025 · 世行）"], ["私营部门信贷 / GDP", "115.9%（2025 · 世行）"]] },
];
const EVENTS: MapEntity[] = [
  { id: "ukraine", name: "俄乌战争", lon: 32, lat: 49, size: 12, color: "#e26d5c", kind: "event", role: "事件锚点", headline: "影响链 · 欧洲能源脱钩", detail: "只呈现对中国投资者相关的影响链：德国自俄能源进口归零、中俄能源绑定加深、黑海粮食通道扰动化肥与粮价（实时值见事件影响卡）。", metrics: [["德国自俄矿物燃料 HS27", "2021 1427.7 亿 → 2024 7.6 亿美元（−99.5%）"], ["中国自俄 HS27（2024）", "952.0 亿美元"], ["小麦 / 化肥（FRED 实拉）", "见「事件影响卡」实时值"]] },
  { id: "iran", name: "美伊战争", lon: 54, lat: 32, size: 12, color: "#e26d5c", kind: "event", role: "事件锚点", headline: "影响链 · 海湾能源与通胀", detail: "海湾能源通道敞口集中在中东进口；只呈现油价与运费影响链（布伦特 / WTI 已接 FRED 实时数据，BDI 待接）。", metrics: [["中国自沙特 HS27（2024）", "491.9 亿美元（霍尔木兹敞口）"], ["布伦特 / WTI（FRED 实拉）", "见「事件影响卡」实时值"]] },
];
const ALL: MapEntity[] = [...NODES, ...EVENTS];

interface ArcDef { id: string; coords: Array<[number, number]>; width: number; color: string; dashed?: boolean; curved: number; label: string; target: string; }
/** 贸易弧（带流动光效）：宽 ∝ 流量，双口径并列（CIF/FOB 不抹平）。eu-cn 走德国代理（D-21 定稿：EU-27 聚合库内残缺）。 */
const TRADE_ARCS: ArcDef[] = [
  { id: "cn-us-trade", coords: [[104, 35], [-98, 39]], width: 4.5, color: "#378add", curved: 0.28, label: "中美贸易 · 2024：中报顺差 +3610.6 亿 ／ 美报逆差 −3191 亿", target: "cn" },
  { id: "jp-us", coords: [[138, 36], [-98, 39]], width: 2.5, color: "#378add", curved: 0.34, label: "日美 · 顺差 +565.7 亿（2024）", target: "jp" },
  { id: "jp-cn", coords: [[138, 36], [104, 35]], width: 2, color: "#378add", curved: 0.25, label: "中日 · 双口径 −424.9 / −42.3 亿", target: "jp" },
  { id: "eu-cn", coords: [[10, 51], [104, 35]], width: 2, color: "#1d9e75", curved: 0.18, label: "欧洲（德国代理）· 2025：德逆差 −1082.3 亿（实测）", target: "eu" },
];
/** 静态弧（虚线，无光效）：金融关联。 */
const STATIC_ARCS: ArcDef[] = [
  { id: "cn-us-fin", coords: [[104, 35], [-98, 39]], width: 1.6, color: "#7f77dd", dashed: true, curved: 0.12, label: "金融关联 · 市值/GDP 79.5% vs 224.0%（2025）", target: "us" },
];

const GEO_MAP_NAME = "world-land";
const GEO_URL = `${import.meta.env.BASE_URL}geo/world_land_boundary.json`;
type Chart = ReturnType<typeof echarts.init>;

function arcSeries(defs: ArcDef[], withEffect: boolean) {
  return {
    type: "lines",
    coordinateSystem: "geo",
    zlevel: 2,
    ...(withEffect ? { effect: { show: true, period: 6, symbol: "arrow", symbolSize: 4.5, trailLength: 0.2, loop: true } } : {}),
    data: defs.map((a) => ({
      coords: a.coords,
      id: a.id,
      name: a.label,
      lineStyle: { width: a.width, color: a.color, opacity: 0.85, curveness: a.curved, type: a.dashed ? ("dashed" as const) : ("solid" as const) },
    })),
  };
}

const COUNTRY_ICONS: Record<string, string> = {
  cn: "diamond", us: "roundRect", eu: "circle", jp: "rect",
  event: "path://M12 2L22 7V17L12 22L2 17V7Z",
};
function nodeColor(entity: MapEntity) {
  const token = entity.kind === "event" ? "--coral" : entity.id === "eu" ? "--mint" : entity.id === "jp" ? "--amber" : "--blue";
  return getComputedStyle(document.documentElement).getPropertyValue(token).trim() || entity.color;
}

function entitySeries(entities: MapEntity[], zlevel: number, selected: string | null, compact: boolean) {
  return {
    type: "effectScatter",
    coordinateSystem: "geo",
    zlevel,
    showEffectOn: "emphasis",
    rippleEffect: { scale: 1, brushType: "stroke" },
    emphasis: { scale: false },
    data: entities.map((n) => {
      const customSymbol = n.kind === "node" ? (COUNTRY_ICONS[n.id] ?? "diamond") : COUNTRY_ICONS.event;
      return {
        id: n.id,
        name: n.kind === "node" ? `${n.name} · ${n.role}` : n.name,
        value: [n.lon, n.lat],
        symbol: customSymbol,
        symbolSize: n.kind === "node" ? Math.round(n.size * 0.95) : Math.round(n.size * 1.3),
        itemStyle: {
          color: "#17211f",
          borderColor: nodeColor(n),
          borderWidth: selected === n.id ? 3 : 1.5,
        },
        label: {
          show: !compact || selected === n.id,
          position: "bottom",
          distance: 8,
          formatter: n.name,
          color: nodeColor(n),
          fontSize: 11.5,
          fontWeight: 600,
          textBorderColor: "#0b0e11",
          textBorderWidth: 3,
        },
      };
    }),
  };
}

function buildSeries(tradeArcs: ArcDef[], selected: string | null = null, showTrade = true, showFinance = true, showEvents = true, compact = false) {
  return [
    arcSeries(showFinance ? STATIC_ARCS : [], false),
    arcSeries(showTrade ? tradeArcs : [], false),
    entitySeries(showEvents ? EVENTS : [], 3, selected, compact),
    entitySeries(NODES, 4, selected, compact),
    { type: "effectScatter", coordinateSystem: "geo", zlevel: 5, silent: true, showEffectOn: "emphasis", symbolSize: 4, label: { show: false }, data: [...NODES, ...(showEvents ? EVENTS : [])].map(entity => ({ value: [entity.lon, entity.lat], itemStyle: { color: nodeColor(entity) } })) },
  ];
}

function buildOption(tradeArcs: ArcDef[]) {
  return {
    backgroundColor: "transparent",
    tooltip: {
      show: true,
      confine: true,
      backgroundColor: "#152225",
      borderColor: "#315054",
      textStyle: { color: "#dce8e4", fontSize: 11 },
      formatter: (p: unknown) => {
        const d = (p as { data?: { name?: string } }).data;
        return d?.name ?? "";
      },
    },
    geo: {
      map: GEO_MAP_NAME,
      roam: true,
      zoom: 1.15,
      center: [8, 34],
      scaleLimit: { min: 0.6, max: 8 },
      itemStyle: { areaColor: "#10201f", borderColor: "#2b4246", borderWidth: 0.6 },
      emphasis: { disabled: true },
      select: { disabled: true },
    },
    series: buildSeries(tradeArcs),
  };
}

/** 贸易弧实拉查询表：双口径并列（CIF/FOB 不抹平）；balance_usd 直接采用（顺差为正、逆差为负）。 */
const TRADE_QUERIES: Array<{ arcId: string; runs: Array<{ reporter: number; partner: number; tag: string }> }> = [
  { arcId: "cn-us-trade", runs: [{ reporter: 156, partner: 842, tag: "中报顺差" }, { reporter: 842, partner: 156, tag: "美报逆差" }] },
  { arcId: "jp-us", runs: [{ reporter: 392, partner: 842, tag: "顺差" }] },
  { arcId: "jp-cn", runs: [{ reporter: 392, partner: 156, tag: "日报" }, { reporter: 156, partner: 392, tag: "中报" }] },
  { arcId: "eu-cn", runs: [{ reporter: 276, partner: 156, tag: "德逆差" }] }, // D-21：EU-27 聚合库内残缺，走德国代理
];
/** 会话内缓存：避免每次进页重复消耗 Comtrade preview 限额（100 次/天 · ≥5s 间隔）。 */
const TRADE_LIVE_CACHE = new Map<string, string>();

function yi(usd: number): string {
  const v = usd / 1e8;
  return `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(1)} 亿`;
}

function MacroImpactMap({ isDemo, macroPluginEnabled = true, onFetchEvents, onFetchTrade, onRegisterResearch, onFetchResearchList }: { isDemo: boolean; macroPluginEnabled?: boolean; onFetchEvents: MacroContext["fetchMacroEvents"]; onFetchTrade: MacroContext["fetchMacroTrade"]; onRegisterResearch: MacroContext["registerMacroResearch"]; onFetchResearchList: MacroContext["fetchMacroResearchList"] }) {
  const [selected, setSelected] = useState<string | null>(null);
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const fetchResearchRef = useRef(onFetchResearchList);
  fetchResearchRef.current = onFetchResearchList;
  const [showTrade, setShowTrade] = useState(true);
  const [showFinance, setShowFinance] = useState(true);
  const [showEvents, setShowEvents] = useState(true);
  const [mapRevision, setMapRevision] = useState(0);
  const [mapWidth, setMapWidth] = useState(800);
  const [note, setNote] = useState("");
  const [timeWindow, setTimeWindow] = useState("");
  const [researchBusy, setResearchBusy] = useState(false);
  const [researchMsg, setResearchMsg] = useState<string | null>(null);
  const [researchList, setResearchList] = useState<Array<Record<string, unknown>>>([]);
  const [mapReady, setMapReady] = useState(false);
  const [mapError, setMapError] = useState(false);
  const [tradeArcs, setTradeArcs] = useState<ArcDef[]>(TRADE_ARCS);
  const [tradeState, setTradeState] = useState<"idle" | "loading" | "done">("idle");
  // 事件影响层（用户定稿：俄乌→粮食/化肥/能源，美伊→原油/航运）：/evidence/macro/events 实拉，
  // 后端实拉一次落 macro_cache。null = 未启用/未取到 → 保留静态实测卡回退（明示标注）。
  const [eventImpacts, setEventImpacts] = useState<MacroEventImpact[] | null>(null);
  const fetchEventsRef = useRef(onFetchEvents);
  fetchEventsRef.current = onFetchEvents;
  useEffect(() => {
    if (isDemo || !macroPluginEnabled) {
      setEventImpacts(null);
      return;
    }
    let cancelled = false;
    fetchEventsRef.current()
      .then((items) => {
        if (!cancelled) setEventImpacts(items);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [isDemo, macroPluginEnabled]);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<Chart | null>(null);

  // G0 实拉：底图就绪且非演示模式时，串行拉 3 条贸易弧的双口径（Comtrade preview ≥5s 间隔），失败弧保留静态实测回退。
  useEffect(() => {
    if (!mapReady || isDemo || tradeState !== "idle") return;
    let disposed = false;
    setTradeState("loading");
    (async () => {
      const liveLabels = new Map<string, string>();
      for (const query of TRADE_QUERIES) {
        const cached = TRADE_LIVE_CACHE.get(query.arcId);
        if (cached !== undefined) {
          liveLabels.set(query.arcId, cached);
          continue;
        }
        const readings: Array<{ tag: string; balance: number; period: string }> = [];
        for (const run of query.runs) {
          let reading: { tag: string; balance: number; period: string } | null = null;
          // ① 月度优先（尽可能新的数据）：最近 3 个月单请求，取最新已发布月（Comtrade 月度滞后 1-2 个月）
          try {
            const monthly = await onFetchTrade({ reporter: run.reporter, partner: run.partner, freq: "M", months: 3 });
            const point = [...(monthly?.series ?? [])].reverse().find((p) => !p.pending && typeof p.balance_usd === "number" && (p.count ?? 0) > 0 && p.period);
            if (point?.period && typeof point.balance_usd === "number") {
              reading = { tag: run.tag, balance: point.balance_usd, period: `${point.period.slice(0, 4)}-${point.period.slice(4)} 月` };
            }
          } catch {
            /* 月度失败 → 年度回退 */
          }
          // ② 年度回退（最新完整年）
          if (!reading) {
            try {
              const result = await onFetchTrade({ reporter: run.reporter, partner: run.partner, years: 1 });
              const row = result?.series?.[0];
              if (row && !row.pending && !row.truncated && typeof row.balance_usd === "number" && (row.count ?? 0) > 0) {
                reading = { tag: run.tag, balance: row.balance_usd, period: row.period ?? "" };
              }
            } catch {
              /* 单口径失败 → 该弧回退静态实测值 */
            }
          }
          if (reading) readings.push(reading);
          await new Promise((resolve) => setTimeout(resolve, 5100)); // Comtrade 限速（实测 ≥5s）
          if (disposed) return;
        }
        if (readings.length === query.runs.length) {
          const period = readings[0]!.period;
          const label =
            query.arcId === "cn-us-trade"
              ? `中美贸易 · ${period}：${readings[0]!.tag} ${yi(readings[0]!.balance)} ／ ${readings[1]!.tag} ${yi(readings[1]!.balance)}`
              : query.arcId === "jp-us"
                ? `日美 · ${readings[0]!.tag} ${yi(readings[0]!.balance)}（${period}）`
                : query.arcId === "eu-cn"
                  ? `欧洲（德国代理）· ${period}：${readings[0]!.tag} ${yi(readings[0]!.balance)}`
                  : `中日 · 双口径 ${readings[0]!.tag} ${yi(readings[0]!.balance)} / ${readings[1]!.tag} ${yi(readings[1]!.balance)}（${period}）`;
          liveLabels.set(query.arcId, label);
          TRADE_LIVE_CACHE.set(query.arcId, label);
        }
      }
      if (disposed) return;
      if (liveLabels.size > 0) {
        setTradeArcs((prev) => prev.map((arc) => (liveLabels.has(arc.id) ? { ...arc, label: liveLabels.get(arc.id)! } : arc)));
      }
      setTradeState("done");
    })();
    return () => {
      disposed = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mapReady, isDemo]);

  // 弧线动态更新：实拉完成后重设 series（不动 geo，保留用户当前缩放/平移）。
  useEffect(() => {
    if (mapReady) chartRef.current?.setOption({ series: buildSeries(tradeArcs, selected, showTrade, showFinance, showEvents, mapWidth < 500) });
  }, [tradeArcs, mapReady, selected, showTrade, showFinance, showEvents, mapWidth]);

  useEffect(() => {
    let disposed = false;
    setMapError(false);
    setMapReady(false);
    fetch(GEO_URL)
      .then((res) => {
        if (!res.ok) throw new Error(`geojson ${res.status}`);
        return res.json();
      })
      .then((geoJson: unknown) => {
        if (disposed) return;
        echarts.registerMap(GEO_MAP_NAME, geoJson as Parameters<typeof echarts.registerMap>[1]);
        if (!containerRef.current) return;
        const chart = echarts.init(containerRef.current);
        chartRef.current = chart;
        chart.setOption(buildOption(tradeArcs));
        chart.on("click", (params: unknown) => {
          const id = (params as { data?: { id?: string } }).data?.id;
          if (id) setSelected(ALL.some(entity => entity.id === id) ? id : [...tradeArcs, ...STATIC_ARCS].find(arc => arc.id === id)?.target ?? null);
        });
        setMapReady(true);
      })
      .catch(() => {
        if (!disposed) setMapError(true);
      });
    const observer = new ResizeObserver(() => { chartRef.current?.resize(); setMapWidth(containerRef.current?.clientWidth ?? 800); });
    if (containerRef.current) observer.observe(containerRef.current);
    return () => {
      disposed = true;
      observer.disconnect();
      chartRef.current?.dispose();
      chartRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mapRevision]);

  const detail = ALL.find((n) => n.id === selected);

  // 选中事件锚点时，拉取该事件已登记的研究判断（GET /evidence/macro/research-evidence?event=…）。
  useEffect(() => {
    if (!detail) return;
    let cancelled = false;
    setResearchMsg(null);
    setResearchList([]);
    void (async () => {
      try {
        const rows = await fetchResearchRef.current(detail.id);
        if (!cancelled) {
          setResearchList(rows ?? []);
          if (rows === null) setResearchMsg("研究判断读取失败。");
        }
      } catch { if (!cancelled) setResearchMsg("研究判断读取失败。"); }
    })();
    return () => { cancelled = true; };
  }, [detail]);

  async function registerResearch() {
    if (!detail || researchBusy) return;
    const claim = note.trim();
    const window_ = timeWindow.trim();
    if (claim.length < 4) {
      setResearchMsg("请先写下你的判断（至少 4 个字）");
      return;
    }
    if (!window_) {
      setResearchMsg("请填写时间窗（如：未来 6-12 个月）");
      return;
    }
    setResearchBusy(true);
    setResearchMsg(null);
    try {
    const ok = await onRegisterResearch({ event_id: detail.id, claim, time_window: window_ });
    if (selectedRef.current !== detail.id) return;
    if (ok) {
      setNote("");
      setTimeWindow("");
      setResearchMsg("已登记（进入研究判断账本，供后续复盘对照）");
      const rows = await fetchResearchRef.current(detail.id);
      if (selectedRef.current === detail.id) setResearchList(rows ?? []);
    } else {
      setResearchMsg("登记失败，请重试。");
    }
    } catch { if (selectedRef.current === detail.id) setResearchMsg("登记失败，请重试。"); }
    finally { setResearchBusy(false); }
  }
  return (
    <div className="radar-card">
      <div className="sec-head">
        <div>
          <span className="kicker mint">MACRO RADAR / GLOBAL TRANSMISSION</span>
          <h3 className="h-band">全球商品与金融传导</h3>
          
        </div>
        <span className="soft-tag amber">实测数值 · 世界地图</span>
      </div>
      <div className="macro-map-workspace">
        <aside className="map-controls" aria-label="地图筛选">
          <h4>关系图层</h4>
          <label><input type="checkbox" checked={showTrade} onChange={event => setShowTrade(event.target.checked)} />贸易关系</label>
          <label><input type="checkbox" checked={showFinance} onChange={event => setShowFinance(event.target.checked)} />金融关联</label>
          <label><input type="checkbox" checked={showEvents} onChange={event => setShowEvents(event.target.checked)} />事件影响</label>
          <h4>关注实体</h4>
          {ALL.map(entity => <button key={entity.id} className="map-entity-button" aria-pressed={selected === entity.id} onClick={() => setSelected(entity.id)} style={{ borderLeftColor: nodeColor(entity) }}>{entity.name}</button>)}
          <button className="text-button" onClick={() => chartRef.current?.setOption({ geo: { center: [8, 34], zoom: 1.15 } })}><RotateCcw size={14} />重置视图</button>
        </aside>
      <div className="transmission-grid">
        <div className="map-stage">
          <div ref={containerRef} className="map-canvas" role="img" aria-label="全球宏观关系地图，实体详情可通过关注实体列表选择" />
          {!mapReady && !mapError && (
            <div className="map-overlay">正在装载世界底图（本地 geoJSON · 离线）…</div>
          )}
          {mapError && (
            <div className="map-overlay warn" role="alert">底图加载失败。<button className="text-button" onClick={() => setMapRevision(value => value + 1)}>重新加载</button></div>
          )}
          <div className="map-legend">
            <span className="lg-item"><span className="lg-line trade" />贸易弧 · 宽 ∝ 流量（2024 · 年更）</span>
            <span className="lg-item"><span className="lg-line fin" />金融关联</span>
            <span className="lg-item"><span className="lg-line eu" />欧洲 · 德国代理（D-21：EU-27 聚合库内残缺）</span>
            <span className="lg-item"><span className="lg-dot" />事件锚点</span>
            <span className="lg-dim">底图：高德数据 · 世界陆域边界（GS 审图 · 本地离线）</span>
            {tradeState === "loading" && <span className="lg-busy">贸易弧 Comtrade 实拉中（限速串行 · 约 36 秒）…</span>}
            {tradeState === "done" && <span className="lg-ok">贸易弧已接 Comtrade 实拉 · 失败口径保留实测回退（2026-09-05）</span>}
          </div>
          <div className="map-caption">数值均实测于 2026-09-05 · 来源：世界银行 ／ UN Comtrade ／ FRED · 双口径并列不抹平 · 事件锚点不含战况数据</div>
        </div>
        <div className="map-rail">
          {(eventImpacts ?? []).map((ev) => (
            <button key={ev.event_id} onClick={() => setSelected(ev.event_id)} className={`map-card event-live${selected === ev.event_id ? " selected" : ""}`}>
              <div className="mc-label coral">{ev.title}</div>
              {ev.rows.map((row) => (
                <div className={`ei-row ${row.status}`} key={row.key} title={row.note}>
                  <span>{row.label}</span>
                  <b>
                    {row.status === "ok" && row.latest !== undefined
                      ? `${row.latest.toLocaleString("zh-CN")}${row.unit === "%" ? "%" : row.unit ? ` ${row.unit}` : ""}`
                      : "待接数据源"}
                  </b>
                </div>
              ))}
              <span className="soft-tag coral">影响链</span>
            </button>
          ))}
          {(!eventImpacts || eventImpacts.length === 0) &&
            EVENTS.map((e) => (
              <button key={e.id} onClick={() => setSelected(e.id)} className={`map-card${selected === e.id ? " selected" : ""}`}>
                <div className="mc-label">{e.name}</div>
                <strong>{e.metrics[0]![0]}</strong>
                <div className="mc-sub">{e.metrics[0]![1]}</div>
                <span className="soft-tag coral">影响链</span>
              </button>
            ))}
          {(!eventImpacts || eventImpacts.length === 0) && (
            <div className="map-card static">
              <div className="mc-label mint">油价 · FRED</div>
              <strong>布伦特 96.02 ／ WTI 91.48</strong>
              <div className="mc-sub">美元/桶 · 最新值见「市场定价」区（FRED 实拉）</div>
              <DataMetaBadge source="FRED" asOf="2026-09-01" freshness="static" />
            </div>
          )}
        </div>
      </div>
      <aside className="map-detail" aria-label="实体详情" style={{ borderTopColor: detail ? nodeColor(detail) : undefined }}>
      {detail ? (<>

          <div className="md-head">
            <b>{detail.name}</b>
            <button className="text-button" title="关闭实体详情" aria-label="关闭实体详情" disabled={researchBusy} onClick={() => setSelected(null)}><X size={16} /></button>
            <span>{detail.headline}</span>
          </div>
          <p>{detail.detail}</p>
          <div className="md-rows">
            {detail.metrics.map(([k, v]) => (
              <div key={k} className="md-row">
                <span className="k">{k}</span>
                <span className="v">{v}</span>
              </div>
            ))}
          </div>
          <div className="research-register">
            <div className="form-row">
              <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="登记你的判断（如：能源成本传导将压低欧洲制造业利润）" aria-label="研究判断" className="mini-input w-grow" />
              <input value={timeWindow} onChange={(e) => setTimeWindow(e.target.value)} placeholder="时间窗（如：未来 6-12 个月）" aria-label="时间窗" className="mini-input w-200" />
              <button className="text-btn" onClick={() => void registerResearch()} disabled={researchBusy}>{researchBusy ? "登记中…" : "登记研究判断 →"}</button>
            </div>
            {researchMsg && <span className="probe-note">{researchMsg}</span>}
            {researchList.length > 0 && (
              <div className="research-list">
                {researchList.map((row) => (
                  <div key={String(row.id)} className="research-row">
                    <span className="rw">{String(row.time_window ?? "")}</span>
                    <span className="rc">{String(row.claim ?? "")}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </>
      ) : <p>尚未选择关注实体</p>}
      </aside>
      </div>
    </div>
  );
}
function ComtradePanel({ onFetch }: { onFetch: MacroContext["fetchComtradePreview"] }) {
  const [reporter, setReporter] = useState(156);
  const [partner, setPartner] = useState(0);
  const [period, setPeriod] = useState("");
  const [cmdCode, setCmdCode] = useState("TOTAL");
  const [flowCode, setFlowCode] = useState("M");
  const [result, setResult] = useState<ComtradePreview | null>(null);
  const [busy, setBusy] = useState(false);

  async function query() {
    setBusy(true);
    const next = await onFetch({ reporter_code: reporter, partner_code: partner, period, cmd_code: cmdCode, flow_code: flowCode, max_records: 50 });
    setResult(next);
    setBusy(false);
  }

  return (
    <div className="panel mt-16">
      <div className="sec-head">
        <div>
          <span className="kicker">UN COMTRADE · 贸易数据源</span>
          <h3>商品贸易预览</h3>
        </div>
        <span className="soft-tag blue">真实 API · 不生成占位数据</span>
      </div>
      <p className="sub-lead lead-gap">需先在设置 → 数据源与通知密钥保存 `comtrade_api_key`。中国报告方代码 156；伙伴方 0 表示世界。</p>
      <div className="form-row">
        <label className="form-field"><span>报告方代码</span><input type="number" min={0} max={999} value={reporter} onChange={(event) => setReporter(Number(event.target.value))} /></label>
        <label className="form-field"><span>伙伴方代码</span><input type="number" min={0} max={999} value={partner} onChange={(event) => setPartner(Number(event.target.value))} /></label>
        <label className="form-field"><span>期间（YYYY 或 YYYYMM）</span><input value={period} placeholder="例：2025" onChange={(event) => setPeriod(event.target.value)} /></label>
        <label className="form-field"><span>商品代码</span><input value={cmdCode} onChange={(event) => setCmdCode(event.target.value)} /></label>
        <label className="form-field"><span>流向</span><input value={flowCode} onChange={(event) => setFlowCode(event.target.value)} /></label>
        <label className="form-field"><span>&nbsp;</span><button className="text-btn" disabled={busy} onClick={() => void query()}>{busy ? "查询中…" : "查询 Comtrade"} <span>→</span></button></label>
      </div>
      {result && (
        <div className="result-block">
          {result.available ? (
            <>
              <div className="result-ok">已接通 · {result.source} · 返回 {result.count ?? result.data.length} 条 · {result.as_of ?? ""}</div>
              <pre className="result-pre">{JSON.stringify(result.data.slice(0, 5), null, 2)}</pre>
            </>
          ) : <div className="result-warn">未取到数据：{result.degraded_reason ?? "未知原因"}</div>}
        </div>
      )}
    </div>
  );
}

/** 宏观雷达（独立应用插件页）：M4 起接 GET /evidence/macro/{region} 真实快照。
 *  数据清晰真实原则：pending 指标明示「待接数据源」，绝不填演示数字冒充真实值；
 *  快照未覆盖的 region（demo 模式 / 拉取失败）才降级为演示数据，且卡片上明示「演示数据」。 */
const COUNTRIES = [
  { flag: "US", region: "us", name: "美国", tag: "演示数据", tagColor: "amber" },
  { flag: "CN", region: "cn", name: "中国", tag: "演示数据", tagColor: "blue" },
  { flag: "EU", region: "eu", name: "欧元区", tag: "演示数据", tagColor: "coral" },
  { flag: "JP", region: "jp", name: "日本", tag: "演示数据", tagColor: "amber" },
  { flag: "IN", region: "in", name: "印度", tag: "演示数据", tagColor: "mint" },
] as const;

const DEMO_INDS: Record<string, { inds: string[][]; foot: string }> = {
  us: {
    inds: [
      ["非农就业（7 月）", "+17.5 万", "低于预期 18.5 万"],
      ["失业率", "4.2%", "连续 3 个月回升"],
      ["CPI 同比", "2.9%", "黏性略超预期"],
      ["ISM 制造业 PMI", "48.7", "连续 5 个月收缩区"],
    ],
    foot: "演示数据 · 非真实行情 · 启动桌面端后自动切换为 FRED 真实快照",
  },
  cn: {
    inds: [
      ["制造业 PMI（8 月）", "49.8", "徘徊荣枯线下"],
      ["CPI 同比", "0.4%", "低位企稳"],
      ["社融增量", "符合预期", "政府债支撑"],
      ["出口同比", "+6.5%", "抢出口效应减弱"],
      ["城镇调查失业率", "5.3%", "持平 · 低位区间"],
    ],
    foot: "演示数据 · 非真实行情 · 后续接入国家统计局/央行公开接口",
  },
  eu: {
    inds: [
      ["HICP 同比", "2.4%", "接近目标"],
      ["综合 PMI", "46.9", "连续收缩"],
      ["失业率", "6.5%", "历史低位附近"],
      ["存款利率", "2.15%", "已进入降息通道"],
    ],
    foot: "演示数据 · 非真实行情 · 后续接入 Eurostat / ECB 公开接口",
  },
  jp: {
    inds: [
      ["CPI 同比", "2.8%", "高于目标 14 个月"],
      ["春斗工资涨幅", "+5.1%", "33 年高位"],
      ["政策利率", "0.5%", "缓步正常化"],
      ["失业率", "2.5%", "维持低位"],
    ],
    foot: "演示数据 · 非真实行情 · 后续接入总务省 / 日银公开接口",
  },
  in: {
    inds: [
      ["GDP 同比", "+6.7%", "全球主要经济体首位"],
      ["CPI 同比", "3.5%", "回到目标区间"],
      ["制造业 PMI", "57.9", "持续扩张"],
      ["失业率（NSO）", "7.6%", "月度调查口径"],
    ],
    foot: "演示数据 · 非真实行情 · 后续接入 MoSPI 公开接口",
  },
};

/** 单条读数：ok 显示真实值 + 观测日；pending 明示原因，绝不输出编造数字。 */
function readingRow(indicator: string, label: string, status: string, latest: number | null | undefined, obsDate: string | null | undefined, unit: string, note: string) {
  // R2 视觉重构：标签定宽横排、数值等宽右对齐、说明单行省略（title 悬停看全文）——
  // 旧布局 meta 列 auto 被长备注撑爆，把标签挤成逐字竖排。
  // 单位去黑话（用户反馈）：%YoY → %（「同比」已在标签里），pp → 个百分点。
  const unitText = unit === "%YoY" ? "%" : unit === "pp" ? "个百分点" : unit;
  const value = status === "ok" && latest !== null
    ? `${typeof latest === "number" ? latest.toLocaleString("zh-CN") : latest}${unitText.startsWith("%") || !unitText ? unitText : ` ${unitText}`}`
    : "待接数据源";
  const sub = status === "ok" ? `${obsDate ?? ""}${note ? ` · ${note}` : ""}` : note || "未接入数据源，不输出数值（不编造）";
  return (
    <div className="mc-ind" key={indicator} title={sub}>
      <span>{label}</span>
      <b style={status !== "ok" ? { color: "var(--faint)" } : undefined}>{value}</b>
      <em>{sub}</em>
    </div>
  );
}

/** §3.7 第三层「我的分析」编辑器（用户主权，仅本机）：三层并排，与规则基线分歧时亮分歧标记。 */
const VIEW_DIRECTIONS = ["扩张", "放缓", "承压", "衰退风险"] as const;
const VIEW_CONFIDENCE = ["高", "中", "低"] as const;

function UserViewEditor({ region, ruleBand, aiDirection, onFetch, onSave }: {
  region: string;
  ruleBand: string | null;
  /** 模型层独立方向（三层对照）：null/undefined = 模型未生成或未输出方向。 */
  aiDirection?: string | null;
  onFetch: (region: string) => Promise<MacroUserView | null>;
  onSave: (region: string, input: { direction: string; horizon: string; confidence: string; text: string }) => Promise<MacroUserView | null>;
}) {
  const [view, setView] = useState<MacroUserView | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [editing, setEditing] = useState(false);
  const [dir, setDir] = useState<string>("放缓");
  const [horizon, setHorizon] = useState("3 个月");
  const [conf, setConf] = useState<string>("中");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  if (!loaded) {
    setLoaded(true);
    onFetch(region).then((v) => {
      if (v) {
        setView(v);
        setDir(v.direction);
        setHorizon(v.horizon);
        setConf(v.confidence);
        setText(v.text);
      }
    }).catch(() => undefined);
  }

  async function save() {
    setBusy(true);
    setErr(null);
    const saved = await onSave(region, { direction: dir, horizon: horizon.trim() || "未填", confidence: conf, text });
    setBusy(false);
    if (saved === null) {
      setErr("保存失败（Core 未就绪或字段校验未过）");
      return;
    }
    setView(saved);
    setEditing(false);
  }

  const diverged = view !== null && ruleBand !== null && view.direction !== ruleBand;
  const divergedFromAi = view !== null && !!aiDirection && view.direction !== aiDirection;
  return (
    <div className="uv-block">
      <div className="mini-controls">
        <span className="mini-note">我的分析（仅本机 · 永不参与规则计算）</span>
        {diverged && <span className="soft-tag amber">与规则基线（{ruleBand}）分歧 —— 差异即信息，不做仲裁</span>}
        {divergedFromAi && <span className="soft-tag amber">与模型方向（{aiDirection}）分歧</span>}
        {!editing && (
          <button className="text-btn" onClick={() => setEditing(true)}>
            {view ? "修改" : "写下我的判断"} <span>→</span>
          </button>
        )}
      </div>
      {view && !editing && (
        <div className="uv-summary">
          <span className={`soft-tag ${view.direction === "扩张" ? "mint" : view.direction === "放缓" ? "amber" : "coral"}`}>{view.direction}</span>
          {" "}时间窗 {view.horizon} · 置信度 {view.confidence}
          {view.text ? ` · ${view.text}` : ""}
        </div>
      )}
      {editing && (
        <div className="uv-editor">
          <div className="mini-controls">
            <select value={dir} onChange={(e) => setDir(e.target.value)} className="mini-select">
              {VIEW_DIRECTIONS.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
            <input value={horizon} onChange={(e) => setHorizon(e.target.value)} placeholder="时间窗，例：3 个月" className="mini-input w-130" />
            <select value={conf} onChange={(e) => setConf(e.target.value)} className="mini-select">
              {VIEW_CONFIDENCE.map((c) => <option key={c} value={c}>置信度 {c}</option>)}
            </select>
          </div>
          <textarea value={text} onChange={(e) => setText(e.target.value)} placeholder="我的判断依据（可选，仅存本机）" rows={2} className="mini-textarea" />
          <div className="uv-actions">
            <button className="text-btn" disabled={busy} onClick={save}>{busy ? "保存中…" : "保存我的分析"}</button>
            <button className="text-btn" onClick={() => { setEditing(false); setErr(null); }}>取消</button>
            {err && <span className="form-error flat">{err}</span>}
          </div>
        </div>
      )}
    </div>
  );
}

/** D-14 市场定价对照层（背景参考，不进四维打分）：FRED 代理序列 + 5 日趋势 + pending 明示。 */
const PRICING_SORTS: { id: "default" | "label" | "status"; label: string }[] = [
  { id: "default", label: "默认顺序" },
  { id: "label", label: "按名称" },
  { id: "status", label: "可用优先" },
];

function PricingPanel({ region, onFetch }: {
  region: string;
  onFetch: (region: string) => Promise<MacroPricingSnapshot | null>;
}) {
  const [pricing, setPricing] = useState<MacroPricingSnapshot | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [open, setOpen] = useState(false);
  // F4-5 数据表排序：默认服务端顺序，可按名称 / 可用优先。
  const [pricingSort, setPricingSort] = useState<"default" | "label" | "status">("default");

  if (!loaded) {
    setLoaded(true);
    onFetch(region).then((snapshot) => setPricing(snapshot)).catch(() => undefined);
  }

  const sortedPricingRows = useMemo(() => {
    if (!pricing) return [];
    const rows = [...pricing.rows];
    if (pricingSort === "label") rows.sort((a, b) => a.label.localeCompare(b.label, "zh-Hans-CN"));
    else if (pricingSort === "status") rows.sort((a, b) => (a.status === "ok" ? 0 : 1) - (b.status === "ok" ? 0 : 1));
    return rows;
  }, [pricing, pricingSort]);

  return (
    <details
      className="fold-sub"
      open={open}
      onToggle={(event) => setOpen((event.target as HTMLDetailsElement).open)}
    >
      <summary>
        市场定价对照（FRED 代理序列，不进打分）{pricing ? ` · ok ${pricing.rows.filter((row) => row.status === "ok").length}/${pricing.rows.length}` : ""}
      </summary>
      {!pricing && <div className="fs-body">加载中…（或 Core 未就绪）</div>}
      {pricing && (
        <div className="row-body">
          <div className="audit-sort" role="group" aria-label="定价表排序">
            {PRICING_SORTS.map((option) => (
              <button
                key={option.id}
                className={`sort-head extension-tab ${pricingSort === option.id ? "active" : ""}`}
                aria-pressed={pricingSort === option.id}
                onClick={() => setPricingSort(option.id)}
              >
                {option.label}
                {pricingSort === option.id && <span className="sort-arrow">▼</span>}
              </button>
            ))}
          </div>
          {sortedPricingRows.map((row) => (
            <div key={row.key} className="row-item">
              <span className="ri-label">{row.label}</span>
              {row.status === "ok" ? (
                <>
                  <b>{row.latest}{row.unit ? ` ${row.unit}` : ""}</b>
                  {row.trend_5d && (
                    <span className={`soft-tag ${row.trend_5d === "上升" ? "coral" : row.trend_5d === "下降" ? "mint" : "blue"}`}>
                      5 日{row.trend_5d}
                    </span>
                  )}
                  {row.ref_value !== null && (
                    <em className="row-note">约 1 个月前 {row.ref_value}（{row.ref_date}）</em>
                  )}
                </>
              ) : (
                <em className="row-note">待接数据源 · {row.note}</em>
              )}
            </div>
          ))}
          <div className="foot-note">
            市场定价是「市场怎么看」的对照层，与规则基线、模型分析、我的分析三层并排参考；日度序列缓存 24h，as_of 以服务端返回为准。
          </div>
        </div>
      )}
    </details>
  );
}

/** 事件日历（信息层·事件层首版）：未来 N 天固定节奏数据发布；FOMC 等非固定节奏明示以官方日历为准。 */
const REGION_FLAGS: Record<string, string> = { us: "US", cn: "CN", eu: "EU", jp: "JP", in: "IN" };
const KIND_LABELS: Record<string, string> = { employment: "就业", inflation: "通胀", growth: "增长", monetary: "货币" };

function CalendarPanel({ onFetch }: { onFetch: (days: number) => Promise<MacroCalendarSnapshot | null> }) {
  const [calendar, setCalendar] = useState<MacroCalendarSnapshot | null>(null);
  const [loaded, setLoaded] = useState(false);
  if (!loaded) {
    setLoaded(true);
    onFetch(14).then((snapshot) => setCalendar(snapshot)).catch(() => undefined);
  }
  return (
    <div className="panel mt-16">
      <div className="panel-title">未来 14 天数据日历</div>
      {!calendar && <div className="loading-note">加载中…（或 Core 未就绪）</div>}
      {calendar && (
        <>
          {calendar.events.length === 0 && (
            <div className="loading-note">观察期内无固定节奏发布事件。</div>
          )}
          <div className="row-list">
            {calendar.events.map((event) => (
              <div key={`${event.region}-${event.event_key}-${event.date}`} className="row-item">
                <b>{event.date}</b>
                {event.date_type === "window" && <span className="foot-note">窗口至 {event.window_end}</span>}
                <span>{REGION_FLAGS[event.region] ?? event.region}</span>
                <span>{event.label}</span>
                <span className={`soft-tag ${event.kind === "employment" ? "mint" : event.kind === "inflation" ? "amber" : "blue"}`}>
                  {KIND_LABELS[event.kind] ?? event.kind}
                </span>
                <em className="row-note">{event.note}</em>
              </div>
            ))}
          </div>
          <div className="foot-note mt-6">
            日历只含官方固定节奏规则可推算的事件；FOMC / ECB / 日银议息等非固定节奏事件不生成日期，具体以官方日历为准（官方日历抓取留后续）。
          </div>
        </>
      )}
    </div>
  );
}

/** 发言人信号流：官方原文粘贴入库 + 模型方向解读（引用原文句子，无引用不回填）。 */
function SpeakerSignalRow({ signal, busy, aiReady = true, onInterpret }: {
  signal: SpeakerSignal;
  busy: boolean;
  aiReady?: boolean;
  onInterpret: () => void;
}) {
  const dirColor = signal.direction === "鹰派" ? "coral" : signal.direction === "鸽派" ? "mint" : "blue";
  return (
    <div className="signal-row">
      <div className="signal-head">
        <b>{signal.speaker}</b>
        <span className="sh-meta">{signal.event_date}{signal.event_type ? ` · ${signal.event_type}` : ""}</span>
        <span className={`soft-tag ${signal.source_name === "官方" ? "mint" : "amber"}`}>{signal.source_name}</span>
        {signal.direction ? (
          <span className={`soft-tag ${dirColor}`}>{signal.direction}（{signal.model} 解读）</span>
        ) : (
          <span className="soft-tag blue">未解读</span>
        )}
        {signal.source_url && (
          <a href={signal.source_url} target="_blank" rel="noreferrer">原文出处 ↗</a>
        )}
        <button
          className="text-btn"
          disabled={busy || !aiReady}
          title={aiReady ? undefined : "AI 未接通：先在 设置 → 模型配置 添加方案并设为「使用中」"}
          onClick={onInterpret}
        >
          {busy ? "解读中…" : signal.direction ? "重新解读" : "模型解读（引用原文）"} <span>→</span>
        </button>
      </div>
      <div className="signal-excerpt">「{signal.excerpt.length > 180 ? `${signal.excerpt.slice(0, 180)}…` : signal.excerpt}」</div>
      {signal.ai_rationale && (
        <div className="signal-rationale">
          解读依据：{signal.ai_rationale}
          {signal.focus_shift ? `；关注点迁移：${signal.focus_shift}` : ""}
        </div>
      )}
    </div>
  );
}

function SignalsPanel({ onFetchSignals, onCreateSignal, onInterpretSignal, aiReady = true }: {
  onFetchSignals: (region: string) => Promise<SpeakerSignal[] | null>;
  onCreateSignal: (region: string, input: SpeakerSignalInput) => Promise<SpeakerSignal | null>;
  onInterpretSignal: (region: string, signalId: string) => Promise<{ ok: boolean; stage?: string; detail?: string; citations?: string[]; signal?: SpeakerSignal } | null>;
  aiReady?: boolean;
}) {
  const [region, setRegion] = useState<string>("us");
  const [signals, setSignals] = useState<SpeakerSignal[]>([]);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [form, setForm] = useState({ speaker: "", event_type: "", event_date: "", source_name: "官方" as "官方" | "转载", source_url: "", excerpt: "" });
  const [creating, setCreating] = useState(false);

  if (loadedFor !== region) {
    setLoadedFor(region);
    onFetchSignals(region).then((rows) => setSignals(rows ?? [])).catch(() => undefined);
  }

  async function submit() {
    if (!form.speaker.trim() || !form.event_date.trim() || form.excerpt.trim().length < 10) {
      setMsg("请填写发言人、日期，并粘贴至少 10 字的官方原文");
      return;
    }
    setCreating(true);
    setMsg(null);
    const created = await onCreateSignal(region, { ...form, event_type: form.event_type.trim() });
    setCreating(false);
    if (created === null) {
      setMsg("录入失败（Core 未就绪或字段校验未过）");
      return;
    }
    setSignals((current) => [created, ...current]);
    setForm({ speaker: "", event_type: "", event_date: "", source_name: "官方", source_url: "", excerpt: "" });
    setMsg("沟通证据已入库（方向留空，点「模型解读」生成方向标注）");
  }

  async function interpret(signalId: string) {
    setBusyId(signalId);
    setMsg(null);
    const result = await onInterpretSignal(region, signalId);
    setBusyId(null);
    if (result === null) {
      setMsg("模型解读请求失败（Core 未就绪）");
      return;
    }
    if (!result.ok) {
      setMsg(`解读被拒绝：${result.detail ?? "未知原因"}`);
      return;
    }
    if (result.signal) {
      setSignals((current) => current.map((row) => (row.signal_id === result.signal!.signal_id ? result.signal! : row)));
    }
    setMsg("方向已回填（依据 = 原文引用句）；权重调整请用上方提议卡或手动滑杆");
  }

  return (
    <div className="slot-block">
      <span className="slot-tag">SPEAKER SIGNALS · 发言人信号流 · 官方原文粘贴，模型只当副驾</span>
      <div className="panel stack">
        <div className="sec-head">
          <div>
            <span className="kicker">沟通证据（官方优先、转载明示）</span>
            <h3>发言人表态 → 方向标注 → 你决定要不要调权重</h3>
          </div>
          <select value={region} onChange={(event) => { setRegion(event.target.value); setMsg(null); }} className="mini-select">
            {COUNTRIES.map(({ region: r, name }) => <option key={r} value={r}>{name}</option>)}
          </select>
        </div>
        <div className="mini-form">
          <div className="mini-controls">
            <input value={form.speaker} onChange={(event) => setForm({ ...form, speaker: event.target.value })} placeholder="发言人，例：鲍威尔" className="mini-input w-140" />
            <input value={form.event_type} onChange={(event) => setForm({ ...form, event_type: event.target.value })} placeholder="场合，例：FOMC 记者会" className="mini-input w-170" />
            <input value={form.event_date} onChange={(event) => setForm({ ...form, event_date: event.target.value })} placeholder="日期 2026-08-12" className="mini-input w-130" />
            <select value={form.source_name} onChange={(event) => setForm({ ...form, source_name: event.target.value as "官方" | "转载" })} className="mini-select">
              <option value="官方">官方</option>
              <option value="转载">转载</option>
            </select>
            <input value={form.source_url} onChange={(event) => setForm({ ...form, source_url: event.target.value })} placeholder="原文出处 URL（可选）" className="mini-input w-grow" />
          </div>
          <textarea value={form.excerpt} onChange={(event) => setForm({ ...form, excerpt: event.target.value })} placeholder="粘贴官方原文（≥10 字）。模型解读必须逐字引用这里的原句，无引用不回填方向。" rows={3} className="mini-textarea" />
          <div className="card-actions">
            <button className="text-btn" disabled={creating} onClick={submit}>{creating ? "录入中…" : "录入沟通证据"}</button>
            {msg && <span className="mini-note">{msg}</span>}
          </div>
        </div>
        {signals.length === 0 && loadedFor === region && (
          <div className="loading-note">该经济体暂无沟通证据：从央行官网（Fed / ECB / 日银 / 中国央行）复制一段原文粘贴录入即可。</div>
        )}
        {signals.map((signal) => (
          <SpeakerSignalRow key={signal.signal_id} signal={signal} busy={busyId === signal.signal_id} aiReady={aiReady} onInterpret={() => interpret(signal.signal_id)} />
        ))}
        <div className="mini-hint">
          方向章（鹰派/鸽派/中性）由模型依据原文回填且必须逐字引用原句；解读只说明「四维中哪类信号更重要」，不构成买卖建议；信号与权重完全解耦——调不调、怎么调，永远你说了算。
        </div>
      </div>
    </div>
  );
}

/** 分析正文剥离首行方向头（【模型方向】/ 历史缓存【AI 方向】）：方向已在标签中展示，避免正文重复。 */
function stripDirectionLine(text: string | null | undefined): string {
  if (!text) return "";
  const stripped = text.replace(/^【(?:AI|模型)\s*方向】[^\n]*\n?/, "").trim();
  return stripped || text;
}

function realCountryCard(
  region: string,
  name: string,
  flag: string,
  snapshot: MacroSnapshot,
  onSubmitAnomalies: (region: string) => Promise<number | null>,
  setAnomalyMsg: (msg: string) => void,
  analysis: MacroAnalysisResult | null,
  analysisBusy: boolean,
  onGenerateAnalysis: (region: string) => Promise<unknown>,
  setAnalysis: (region: string, result: MacroAnalysisResult | null) => void,
  onFetchUserView: (region: string) => Promise<MacroUserView | null>,
  onSaveUserView: (region: string, input: { direction: string; horizon: string; confidence: string; text: string }) => Promise<MacroUserView | null>,
  onFetchPricing: (region: string) => Promise<MacroPricingSnapshot | null>,
  aiReady?: boolean,
) {
  const foot = snapshot.generated_at
    ? `来源 ${snapshot.indicators[0]?.source || "FRED（公开接口）"} · 快照 ${new Date(snapshot.generated_at).toLocaleString("zh-CN", { hour12: false })}`
    : "来源 FRED（公开接口）";
  const p = snapshot.positioning;
  const bandColor = p ? (p.band === "扩张" ? "mint" : p.band === "放缓" ? "amber" : "coral") : "blue";
  const severe = p
    ? p.dims.flatMap((d) => d.rules_fired).filter((rule) => {
        const match = rule.match(/→\s*(-?\d+)\s*$/);
        return match !== null && Math.abs(Number(match[1])) >= 25;
      })
    : [];
  async function runAnalysis() {
    setAnalysis(region, null);
    const result = await onGenerateAnalysis(region);
    setAnalysis(region, (result as MacroAnalysisResult | null) ?? { ok: false, detail: "模型解读请求失败（Core 未就绪）" });
  }
  return (
    <div className="macro-card" key={region}>
      <div className="mc-head">
        <span className="mc-flag">{flag}</span>
        <b>{name}</b>
        {p && <span className={`soft-tag ${bandColor}`}>{p.band} {p.composite} 分（规则基线 · {p.weight_version}）</span>}
      </div>
      {snapshot.indicators.map((row) => readingRow(row.indicator, row.label, row.status, row.latest, row.obs_date, row.unit, row.note))}
      {p && (
        <div className="mc-foot muted">
          四维：{p.dims.map((d) => `${d.dim === "growth" ? "增长" : d.dim === "employment" ? "就业" : d.dim === "inflation" ? "通胀" : "货币"} ${d.score ?? "—"}`).join(" · ")}
          {p.near_boundary_note ? `；${p.near_boundary_note}` : ""}
        </div>
      )}
      {p && <details className="fold-sub"><summary>打分依据与 limitations（可复算）</summary><div className="fs-body">{p.dims.flatMap((d) => d.rules_fired).join("；") || "该维无命中规则"}。{p.limitations.join(" ")}</div></details>}
      {p && (
        <div className="card-actions">
          <button
            className="text-btn accent"
            disabled={analysisBusy || !aiReady}
            title={aiReady ? undefined : "AI 未接通：先在 设置 → 模型配置 添加方案并设为「使用中」"}
            onClick={runAnalysis}
          >
            {analysisBusy ? "模型解读中…" : "模型解读（挂引用，不打分）"} <span>→</span>
          </button>
        </div>
      )}
      {analysis && (
        <details open className="fold-sub rule">
          <summary>
            模型分析 · {analysis.ok ? `${analysis.model}（${analysis.latency_ms}ms）· 引用 ${analysis.citations?.join("、")}` : `未发布：${analysis.detail ?? "未知原因"}`}
            {analysis.ok && analysis.direction && (
              <span className={`soft-tag push ${analysis.direction === "扩张" ? "mint" : analysis.direction === "放缓" ? "amber" : "coral"}`}>模型方向 {analysis.direction}</span>
            )}
            {analysis.ok && analysis.direction && p && analysis.direction !== p.band && (
              <span className="soft-tag push amber">与规则基线（{p.band}）分歧 —— 差异即信息，不做仲裁</span>
            )}
          </summary>
          <div className="fold-sub analysis-body">
            {analysis.ok ? stripDirectionLine(analysis.analysis) : "本次输出未通过引用校验（无引用不发布，ADR-0006），可重试生成。"}
          </div>
        </details>
      )}
      {p && (
        <UserViewEditor region={region} ruleBand={p.band} aiDirection={analysis?.ok ? analysis.direction ?? null : null} onFetch={onFetchUserView} onSave={onSaveUserView} />
      )}
      <PricingPanel region={region} onFetch={onFetchPricing} />
      {severe.length > 0 && (
        <div className="card-actions">
          <button
            className="text-btn warn"
            onClick={async () => {
              const count = await onSubmitAnomalies(region);
              setAnomalyMsg(count === null ? `${name} 异动提交失败（Core 未就绪）` : `${name} 已提交 ${count} 条异动证据到研究账本`);
              if (aiReady && count !== null && count > 0) await runAnalysis(); // 异动提交后自动附模型解读（AI 未接通时跳过）
            }}
          >
            提交 {severe.length} 条重度异动到研究页 <span>→</span>
          </button>
        </div>
      )}
      {snapshot.degraded_reason && <div className="mc-foot warn">{snapshot.degraded_reason}</div>}
      <div className="mc-foot">{foot}</div>
    </div>
  );
}

/** 背景层三卡分组：FX（美元/CFETS/有效汇率）、OIL（WTI/布伦特）、AU（金价）。 */
const BG_GROUPS = [
  { flag: "FX", name: "货币指数", tagColor: "blue", keys: ["dxy_twi", "cfets_rmb", "effective_fx"] },
  { flag: "OIL", name: "油价", tagColor: "amber", keys: ["wti", "brent"] },
  { flag: "AU", name: "金价", tagColor: "amber", keys: ["gold"] },
] as const;

const DEMO_BACKGROUND = [
  {
    flag: "FX", name: "货币指数", tagColor: "blue",
    inds: [
      ["美元指数 DXY", "103.2", "5 日 +0.6%"],
      ["人民币 CFETS 指数", "98.6", "5 日 -0.2%"],
      ["欧元 / 日元有效汇率", "94.1 / 86.3", "BIS 月度口径"],
    ],
    foot: "演示数据 · 非真实行情",
  },
  {
    flag: "OIL", name: "油价", tagColor: "amber",
    inds: [
      ["WTI 现货", "$67.8", "5 日 -1.8%"],
      ["布伦特现货", "$71.4", "5 日 -1.5%"],
    ],
    foot: "演示数据 · 非真实行情",
  },
  {
    flag: "AU", name: "金价", tagColor: "amber",
    inds: [["伦敦金定盘", "$2,531", "5 日 +1.2%"]],
    foot: "演示数据 · 非真实行情",
  },
] as const;

type MacroContext = ReturnType<typeof useMacro>;
const DIM_LABELS = { growth: "增长", employment: "就业", inflation: "通胀", monetary: "货币" } as const;
const DEFAULT_WEIGHT_PCT: Record<keyof typeof DIM_LABELS, number> = { growth: 35, employment: 25, inflation: 20, monetary: 20 };

export function MacroPage({ onNavigate, isDemo, macroPluginEnabled = true, onEnablePlugin, active, aiReady = true }: Props) {
  // B2：宏观领域数据自取（回调 props 已清零；别名对齐既有局部命名，页面主体零改动）。
  const client = useMemo(() => createCoreClient(), []);
  const { macroSnapshots: snapshots, macroLoading: loading, macroWeights: currentWeights,
    saveMacroWeights: onSaveWeights, submitMacroAnomalies: onSubmitAnomalies, generateMacroAnalysis: onGenerateAnalysis,
    requestWeightProposal: onRequestProposal, fetchMacroUserView: onFetchUserView, saveMacroUserView: onSaveUserView,
    fetchMacroAnalysisCache: onFetchAnalysisCache, fetchMacroPricing: onFetchPricing, fetchMacroCalendar: onFetchCalendar,
    fetchMacroEvents: onFetchEvents, fetchMacroReleases: onFetchReleases, fetchComtradePreview: onFetchComtrade,
    fetchMacroTrade: onFetchTrade, fetchSpeakerSignals: onFetchSignals, createSpeakerSignal: onCreateSignal,
    interpretSpeakerSignal: onInterpretSignal, registerMacroResearch: onRegisterResearch, fetchMacroResearchList: onFetchResearchList,
  } = useMacro(client, active, aiReady);
  const globalSnapshot = snapshots["global"] ?? null;
  // 经济数据发布看板（CPI/非农/利率：前值/预期/实际），启用后拉取并随启用态变化重取。
  const [releases, setReleases] = useState<MacroReleaseBoard | null>(null);
  const fetchReleasesRef = useRef(onFetchReleases);
  fetchReleasesRef.current = onFetchReleases;
  useEffect(() => {
    if (isDemo || !macroPluginEnabled) {
      setReleases(null);
      return;
    }
    let cancelled = false;
    fetchReleasesRef.current()
      .then((board) => {
        if (!cancelled) setReleases(board);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [isDemo, macroPluginEnabled]);
  const okCount = COUNTRIES.filter(({ region }) => snapshots[region]?.indicators.some((row) => row.status === "ok")).length;
  const [anomalyMsg, setAnomalyMsg] = useState<string | null>(null);
  // 权重面板草稿（百分数）；保存时换算为小数（/100），后端校验合计=1.0。
  const [weightDraft, setWeightDraft] = useState<Record<string, number>>({ ...DEFAULT_WEIGHT_PCT });
  const [weightNote, setWeightNote] = useState("");
  const [weightBusy, setWeightBusy] = useState(false);
  const [weightMsg, setWeightMsg] = useState<string | null>(null);
  const weightSum = Object.values(weightDraft).reduce((sum, value) => sum + value, 0);
  // 滑杆与已保存版本链同步：装载/保存成功后预填当前生效权重（小数→百分数），
  // 用户拖动只改 draft，不回写 currentWeights，下一次保存成功才推进版本。
  useEffect(() => {
    if (!currentWeights) return;
    setWeightDraft((draft) => {
      const next: Record<string, number> = { ...draft };
      Object.entries(currentWeights.weights).forEach(([dim, value]) => {
        next[dim] = Math.round(value * 100);
      });
      return next;
    });
  }, [currentWeights]);
  // —— 模型分析层 / 模型提议卡 ——
  const [analysisMap, setAnalysisMap] = useState<Record<string, MacroAnalysisResult | null>>({});
  const [analysisBusyRegion, setAnalysisBusyRegion] = useState<string | null>(null);
  const [proposalIntent, setProposalIntent] = useState("");
  const [proposal, setProposal] = useState<WeightProposalResponse | null>(null);
  const [proposalBusy, setProposalBusy] = useState(false);
  const cacheFetched = useRef<Set<string>>(new Set());

  // 快照就绪后静默回填历史模型分析（缓存命中不重复消耗模型调用）。
  useEffect(() => {
    if (isDemo || loading) return;
    COUNTRIES.forEach(({ region }) => {
      if (cacheFetched.current.has(region)) return;
      cacheFetched.current.add(region);
      onFetchAnalysisCache(region)
        .then((cached) => {
          if (cached?.ok) setAnalysisMap((current) => (current[region] ? current : { ...current, [region]: cached }));
        })
        .catch(() => undefined);
    });
  }, [isDemo, loading, snapshots, onFetchAnalysisCache]);

  function setAnalysis(region: string, result: MacroAnalysisResult | null) {
    setAnalysisMap((current) => ({ ...current, [region]: result }));
  }
  async function runAnalysis(region: string) {
    setAnalysis(region, null);
    setAnalysisBusyRegion(region);
    const result = await onGenerateAnalysis(region);
    setAnalysisBusyRegion(null);
    setAnalysis(region, result ?? { ok: false, detail: "模型解读请求失败（Core 未就绪）" });
  }
  async function confirmProposal() {
    if (!proposal?.ok || !proposal.proposal) return;
    setProposalBusy(true);
    // proposal.weights 已是小数口径（后端约束合计=1.0），直接保存；source=ai 计入 30 天护栏计数。
    const saved = await onSaveWeights(
      proposal.proposal.weights,
      `模型提议确认：${proposal.proposal.rationale}`,
      "ai",
    );
    setProposalBusy(false);
    setWeightMsg(saved === null
      ? "模型提议保存失败（Core 未就绪或权重校验未过）"
      : `已确认模型提议并保存为 ${saved.version}，各国定位已按新权重复算`);
    if (saved !== null) setProposal(null);
  }

  return (
    <section className="page-view macro-page">
      <header className="macro-heading">
        <div><span className="kicker">全球宏观</span><h2>宏观雷达</h2></div>
        <div><span>{loading ? "数据更新中" : isDemo ? "演示数据" : "公开数据与本机缓存"}</span><p>关注主题：贸易 · 金融 · 能源传导</p></div>
      </header>
      <dl className="macro-cycle-band" aria-label="宏观周期与数据时间">
        {COUNTRIES.map(({ region, name }) => <div key={region}><dt>{name}</dt><dd>{snapshots[region]?.positioning?.band ?? "周期待确认"}</dd><small>{snapshots[region]?.generated_at ? `更新 ${new Date(snapshots[region]!.generated_at).toLocaleDateString("zh-CN")}` : "暂无快照"}</small></div>)}
      </dl>

      {!isDemo && !macroPluginEnabled && (
        <div className="connection-bar" role="status">
          <span>宏观雷达插件未启用：快照 / 定价 / 日历端点都会被拒绝（HTTP 409），四国板显示不出真实数据。</span>
          <span className="conn-detail">启用后数据实拉一次即落本机 SQLite 缓存（macro_cache，带 as_of），不会反复外拉；断网时回退最近一次落库值。</span>
          <span className="conn-actions">
            {onEnablePlugin && (
              <button className="text-button" onClick={onEnablePlugin}>
                一键启用宏观雷达
              </button>
            )}
          </span>
        </div>
      )}

      <div className="metric-row mt-14">
        <div className="metric mint"><span>已实拉国家</span><strong>{okCount}</strong><small>有 FRED 可取数据的地区</small></div>
        <div className="metric amber"><span>待接数据源</span><strong>{5 - okCount}</strong><small>逐国接入官方公开接口</small></div>
        <div className="metric blue"><span>数据源</span><strong>按地区</strong><small>美国 FRED，其余地区显示实际 provider</small></div>
      </div>

      {/* 核心可视化 · 世界地图 */}
      <MacroImpactMap isDemo={isDemo} macroPluginEnabled={macroPluginEnabled} onFetchEvents={onFetchEvents} onFetchTrade={onFetchTrade} onRegisterResearch={onRegisterResearch} onFetchResearchList={onFetchResearchList} />

      {/* 四国定位板 */}
      <div className="head-row">
        <h3>四国定位板</h3>
        <span className="kicker sm">FRED 实拉 · PENDING 明示 · 规则基线层不打分</span>
      </div>
      <div className="macro-grid mt-10">
        {COUNTRIES.map(({ flag, region, name }) => {
          const snapshot = snapshots[region];
          if (snapshot) return realCountryCard(region, name, flag, snapshot, onSubmitAnomalies, setAnomalyMsg, analysisMap[region] ?? null, analysisBusyRegion === region, runAnalysis, setAnalysis, onFetchUserView, onSaveUserView, onFetchPricing, aiReady);
          const demo = DEMO_INDS[region]!;
          return (
            <div className="macro-card" key={region}>
              <div className="mc-head">
                <span className="mc-flag">{flag}</span>
                <b>{name}</b>
                <span className="soft-tag coral">{isDemo ? "演示数据" : "快照不可用"}</span>
              </div>
              {demo.inds.map(([label, value, note]) => (
                <div className="mc-ind" key={label}>
                  <span>{label}</span>
                  <b className="dim">{isDemo ? value : "无数据"}</b>
                  <em>{isDemo ? note : "真实快照不可用，请重试或检查数据源配置"}</em>
                </div>
              ))}
              <div className="mc-foot warn">{isDemo ? demo.foot : "无数据 · 未显示演示读数"}</div>
            </div>
          );
        })}
      </div>

      {anomalyMsg && (
        <div className="note-rule mt-14">
          <b>异动提交结果</b>
          {anomalyMsg}（同日重复提交幂等去重，不会重复入账；证据只描述规则计算结果，不构成买卖建议。）
        </div>
      )}

      {/* 权重调节 · 默认折叠，展开后完整保留原功能 */}
      {!isDemo && (
        <details className="radar-fold mt-22">
          <summary>
            <b>四维权重调节</b>
            <span className="fold-note">当前 {currentWeights ? currentWeights.version : "默认 v1"} · 你的模型你做主 · 模型只能提议，确认才生效</span>
            <span className="fold-arrow">▸</span>
          </summary>
          <div className="fold-body">
            <div className="panel mt-10">
              <div className="sec-head">
                <div>
                  <span className="kicker">四维权重调节</span>
                  <h3>
                    {currentWeights ? `当前生效 ${currentWeights.version}（已按保存值预填）` : "默认 v1 · 增长 35 · 就业 25 · 通胀 20 · 货币 20"}
                  </h3>
                </div>
                <button
                  className="text-btn"
                  disabled={weightBusy}
                  onClick={() => { setWeightDraft({ ...DEFAULT_WEIGHT_PCT }); setWeightMsg("已恢复默认权重（未保存，拖动后点保存生效）"); }}
                >
                  恢复默认 <span>→</span>
                </button>
              </div>
              <div className="form-row">
                {(Object.keys(DIM_LABELS) as Array<keyof typeof DIM_LABELS>).map((dim) => (
                  <label className="form-field" key={dim}>
                    <span>{`${DIM_LABELS[dim]}：${weightDraft[dim] ?? 0}%`}{(weightDraft[dim] ?? 0) - DEFAULT_WEIGHT_PCT[dim] > 20 ? "（高度个性化 >20pp）" : ""}</span>
                    <input
                      type="range"
                      min={0}
                      max={100}
                      value={weightDraft[dim] ?? 0}
                      onChange={(event) => setWeightDraft((draft) => ({ ...draft, [dim]: Number(event.target.value) }))}
                    />
                  </label>
                ))}
              </div>
              <div className="form-row mt-10">
                <label className="form-field">
                  <span>调整依据（可选 · 会随版本留痕，之后可回溯「当时为什么调」）</span>
                  <input value={weightNote} onChange={(event) => setWeightNote(event.target.value)} placeholder="例：更看重就业下行风险" />
                </label>
                <label className="form-field">
                  <span>&nbsp;</span>
                  <button
                    className="text-btn"
                    disabled={weightBusy || Math.abs(weightSum - 100) > 0}
                    onClick={async () => {
                      setWeightBusy(true);
                      setWeightMsg(null);
                      const weights: Record<string, number> = {};
                      Object.entries(weightDraft).forEach(([dim, pct]) => { weights[dim] = pct / 100; });
                      const saved = await onSaveWeights(weights, weightNote);
                      setWeightBusy(false);
                      setWeightMsg(saved === null
                        ? "保存失败（Core 未就绪或合计不等于 100）"
                        : `已保存为 ${saved.version}${saved.personalized.length ? `；${saved.personalized.map((dim) => DIM_LABELS[dim as keyof typeof DIM_LABELS]).join("、")}维偏离默认 >20pp，与其他口径结果不可直接对比` : ""}，各国定位已按新权重复算`);
                    }}
                  >
                    {weightBusy ? "保存中…" : `保存为新版本（合计 ${weightSum}${Math.abs(weightSum - 100) > 0 ? " ≠ 100，不可保存" : ""}）`}
                  </button>
                </label>
              </div>
              {weightMsg && <p className="form-error muted">{weightMsg}</p>}
              <div className="proposal-block">
                <span className="kicker lg">模型提议卡（待确认，确认才生效）</span>
                <div className="form-row mt-8">
                  <label className="form-field">
                    <span>调整意图（可粘贴发言人表态原句，模型必须引用它作为依据；护栏：单维 ±10pp · 每维 ≥10% · 30 天 2 次）</span>
                    <input value={proposalIntent} onChange={(event) => setProposalIntent(event.target.value)} placeholder="例：鲍威尔说开始关注就业下行风险，把就业权重调高些" />
                  </label>
                  <label className="form-field">
                    <span>&nbsp;</span>
                    <button
                      className="text-btn"
                      disabled={proposalBusy || proposalIntent.trim().length < 4 || !aiReady}
                      title={aiReady ? undefined : "AI 未接通：先在 设置 → 模型配置 添加方案并设为「使用中」"}
                      onClick={async () => {
                        setProposalBusy(true);
                        setProposal(null);
                        const result = await onRequestProposal(proposalIntent.trim());
                        setProposalBusy(false);
                        setProposal(result ?? { ok: false, detail: "提议请求失败（Core 未就绪）" });
                      }}
                    >
                      {proposalBusy ? "模型生成提议中…" : "生成模型提议"} <span>→</span>
                    </button>
                  </label>
                </div>
                {proposal && !proposal.ok && (
                  <div className="note-rule warn">
                    <b>未生成提议（{proposal.stage ?? "unknown"}）</b>{proposal.detail}
                  </div>
                )}
                {proposal?.ok && proposal.proposal && (
                  <div className="note-rule accent">
                    <b>模型提议（基于版本 {proposal.proposal.base_version} · {proposal.proposal.model}）</b>
                    <div className="my-6">
                      新权重：{Object.entries(proposal.proposal.weights).map(([dim, value]) => `${DIM_LABELS[dim as keyof typeof DIM_LABELS] ?? dim} ${Math.round(value * 100)}%`).join(" · ")}
                      （主要变动：{proposal.proposal.dim_changed} {proposal.proposal.direction}）
                    </div>
                    <div className="sub-text">依据（引用原句）：{proposal.proposal.rationale || "（模型未给出引用——建议忽略此提议）"}</div>
                    <div className="card-actions mt-8">
                      <button className="text-btn" disabled={proposalBusy} onClick={confirmProposal}>
                        {proposalBusy ? "确认中…" : "确认生效（存为新版本）"} <span>→</span>
                      </button>
                      <button className="text-btn" onClick={() => setProposal(null)}>忽略</button>
                    </div>
                  </div>
                )}
              </div>
              <div className="note-rule mt-10">
                <b>权重链与模型边界</b>
                每次保存都生成新版本（v2、v3…）留痕于本机；同一份数据 + 同一版权重永远算出同一状态带（历史可精确复算）。模型只能「提议」权重调整且走确认流，永远不能直接改你的权重；你手动调整不受模型护栏限制（护栏只约束模型提议：单维 ±10pp、每维 ≥10%、30 天 2 次）。
              </div>
            </div>
          </div>
        </details>
      )}

      {/* 全球背景层 · 统一雷达卡 */}
      <div className="radar-card mt-22">
        <div className="head-row first">
          <h3 className="h-sub">全球背景层</h3>
          <span className="kicker sm">不参与国别打分 · 仅作对照</span>
        </div>
        <div className="macro-grid mt-12">
          {globalSnapshot
            ? BG_GROUPS.map((group) => {
                const rows = group.keys.map((key) => globalSnapshot.background.find((row) => row.key === key)).filter((row): row is NonNullable<typeof row> => Boolean(row));
                return (
                  <div className="macro-card" key={group.flag}>
                    <div className="mc-head">
                      <span className="mc-flag">{group.flag}</span>
                      <b>{group.name}</b>
                      <span className={`soft-tag ${rows.every((row) => row.status === "ok") ? "mint" : rows.some((row) => row.status === "ok") ? "amber" : "coral"}`}>
                        {rows.filter((row) => row.status === "ok").length}/{rows.length} 实拉
                      </span>
                    </div>
                    {rows.map((row) => readingRow(row.key, row.label, row.status, row.latest, row.obs_date, "", row.note))}
                    <div className="mc-foot">
                      <DataMetaBadge
                        source="EIA · FRED · CFETS · BIS"
                        asOf={rows.map((row) => row.obs_date).filter(Boolean).sort().at(-1) ?? null}
                        freshness={rows.every((row) => row.status === "ok") ? "realtime" : "partial"}
                      />
                      白名单公开接口 · 不参与国别打分
                    </div>
                  </div>
                );
              })
            : DEMO_BACKGROUND.map((item) => (
                <div className="macro-card" key={item.flag}>
                  <div className="mc-head">
                    <span className="mc-flag">{item.flag}</span>
                    <b>{item.name}</b>
                    <span className="soft-tag coral">演示数据</span>
                  </div>
                  {item.inds.map(([label, value, note]) => (
                    <div className="mc-ind" key={label}>
                      <span>{label}</span>
                      <b className="dim">{value}</b>
                      <em>{note}</em>
                    </div>
                  ))}
                  <div className="mc-foot warn">{item.foot}</div>
                </div>
              ))}
        </div>
      </div>

      {/* 工具与证据 · 默认折叠，压缩页面长度 */}
      {!isDemo && (
        <div className="fold-stack">
          <details className="radar-fold">
            <summary>
              <b>事件日历</b>
              <span className="fold-note">固定节奏规则 · 议息不编造日期</span>
              <span className="fold-arrow">▸</span>
            </summary>
            <div className="fold-body"><CalendarPanel onFetch={onFetchCalendar} /></div>
          </details>
          <details className="radar-fold">
            <summary>
              <b>商品贸易预览</b>
              <span className="fold-note">UN Comtrade 订阅链路已接入 · 查询即拉取</span>
              <span className="fold-arrow">▸</span>
            </summary>
            <div className="fold-body"><ComtradePanel onFetch={onFetchComtrade} /></div>
          </details>
          <details className="radar-fold">
            <summary>
              <b>发言人信号流</b>
              <span className="fold-note">官方原文入库 · 模型解读须逐字引用原句，无引用不回填方向</span>
              <span className="fold-arrow">▸</span>
            </summary>
            <div className="fold-body"><SignalsPanel onFetchSignals={onFetchSignals} onCreateSignal={onCreateSignal} onInterpretSignal={onInterpretSignal} aiReady={aiReady} /></div>
          </details>
        </div>
      )}

      {/* 研究页通道 · 统一雷达卡 */}
      <div className="radar-card">
        <div className="sec-head first">
          <div>
            <span className="kicker">宏观异动通道</span>
            <h3>异动将作为背景证据进入研究页</h3>
          </div>
          <button className="text-btn" onClick={() => onNavigate("research")}>打开研究页 <span>→</span></button>
        </div>
        <div className="note-rule mt-10">
          <b>三层判断（已落地）</b>
          规则基线层（可复现锚）· 模型分析层（挂引用 · 独立方向）· 我的分析层（仅本机）已三层并排互不覆盖；两两方向分歧时显式标注「分歧」——分歧即信息，不做仲裁谁对。异动提交到本页后自动附模型解读。
        </div>
      </div>

      <div className="note-rule">
        <b>边界：宏观判断是背景，不是结论</b>
        本页所有数值来自白名单公开接口实拉（dataset_version 可追溯到 series 与观测日），pending 项不输出数值；它只能作为研究页的背景证据，不构成买卖建议。数据有发布延迟（非农 T+1、PMI 快报 vs 终值），口径变化会在证据 limitations 中标注。
      </div>
    </section>
  );
}

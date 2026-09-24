import { useEffect, useRef, useState } from "react";
import type { MacroSnapshot } from "@investment-steward/domain-contracts";
import type { CoreClient } from "../state/coreClient";

/**
 * B2（frontend-optimization-roadmap-2026-09-12）：宏观雷达领域数据，自 AppShell 原样下沉。
 * 页面直接消费本钩子；AppShell 只保留壳层职责。快照按 region 缓存；未取到的 region
 * 由页面用明示「演示数据」兜底；pending 状态属正常（无 FRED key / 数据源未接入），
 * 页面按「不编造」原则明示。
 *
 * 以下领域类型原样自 AppShell 搬出（MacroPage 改从本模块导入）。
 */

/** G0 贸易流（GET /evidence/macro/trade，UN Comtrade，单位美元）。年度点字段齐全；月度序列/pending 点字段可缺省。 */
export interface MacroTradePoint {
  period?: string;
  export_usd?: number;
  import_usd?: number;
  balance_usd?: number;
  count?: number;
  truncated?: boolean;
  pending?: boolean;
  degraded_reason?: string;
}

/** M5.2 模型分析结果（POST /evidence/macro/{region}/analysis 响应，ok=false 时 analysis=null）。 */
export interface MacroAnalysisResult {
  ok: boolean;
  stage?: string;
  detail?: string;
  region?: string;
  analysis?: string | null;
  citations?: string[];
  /** 模型独立判断的四档方向（M5.2 v2 三层对照）；旧缓存/模型未输出时为 null。 */
  direction?: string | null;
  band?: string;
  composite?: number;
  weight_version?: string;
  model?: string;
  latency_ms?: number;
  generated_at?: string;
  cached?: boolean;
}

/** D-14 市场定价层（GET /evidence/macro/{region}/pricing 响应）。 */
export interface MacroPricingRow {
  key: string;
  label: string;
  kind: string;
  status: "ok" | "pending" | "degraded";
  latest: number | null;
  obs_date: string | null;
  unit: string;
  trend_5d: string | null;
  ref_value: number | null;
  ref_date: string | null;
  as_of: string | null;
  source: string;
  dataset_version: string | null;
  note: string;
}

export interface MacroPricingSnapshot {
  region: string;
  label: string;
  rows: MacroPricingRow[];
  degraded_reason: string | null;
  generated_at: string;
}

/** M5.8 发言人信号（§3.5 段一/段二，D-13）：官方原文粘贴入库，模型解读须引用原文句子。 */
export interface SpeakerSignal {
  signal_id: string;
  region: string;
  speaker: string;
  event_type: string;
  event_date: string;
  source_name: "官方" | "转载";
  source_url: string;
  excerpt: string;
  direction: "鹰派" | "鸽派" | "中性" | null;
  ai_rationale: string | null;
  focus_shift: string | null;
  model: string | null;
  interpreted_at: string | null;
  created_at: string;
}

export interface SpeakerSignalInput {
  speaker: string;
  event_type: string;
  event_date: string;
  source_name: "官方" | "转载";
  source_url: string;
  excerpt: string;
}

/** 事件影响层（GET /evidence/macro/events）：俄乌→粮食/化肥/能源，美伊→原油/航运；pending 行不输出数值。 */
export interface MacroEventImpactRow {
  key: string;
  label: string;
  status: "ok" | "pending";
  latest?: number;
  obs_date?: string;
  unit?: string;
  as_of?: string;
  dataset_version?: string;
  note: string;
}

export interface MacroEventImpact {
  event_id: string;
  title: string;
  rows: MacroEventImpactRow[];
}

/** 经济数据发布看板（GET /evidence/macro/releases）：前值/实际由 FRED 计算；预期列待第三方源。 */
export interface MacroReleaseRow {
  key: string; label: string; status: "ok" | "pending";
  actual?: number | null; previous?: number | null; obs_date?: string | null;
  unit?: string | null; expectation?: null; as_of?: string | null;
  dataset_version?: string | null; release_note?: string;
}
export interface MacroReleaseBoard { rows: MacroReleaseRow[]; expectation_note: string; }

/** 事件日历（GET /evidence/macro/calendar 响应）：只含固定节奏规则可推算的事件。 */
export interface MacroCalendarEvent {
  region: string;
  event_key: string;
  label: string;
  kind: string;
  date_type: "exact" | "window";
  date: string;
  window_end: string | null;
  note: string;
}

export interface MacroCalendarSnapshot {
  events: MacroCalendarEvent[];
  days: number;
  generated_at: string;
}

/** §3.7 第三层「我的分析」（D-15 最小集，用户主权，仅本机）。 */
export interface MacroUserView {
  region: string;
  direction: "扩张" | "放缓" | "承压" | "衰退风险";
  horizon: string;
  confidence: "高" | "中" | "低";
  text: string;
  updated_at: string;
}

const REGIONS = ["us", "cn", "eu", "jp", "in", "global"] as const;

/** M5.5 模型权重提议响应（确认流由前端拿 proposal 走 onSaveWeights source=ai）。 */
export interface WeightProposalResponse {
  ok: boolean;
  stage?: string;
  detail?: string;
  proposal?: {
    weights: Record<string, number>;
    rationale: string;
    dim_changed: string;
    direction: string;
    base_version: string;
    model: string;
    latency_ms: number;
  } | null;
}

/** 与页面共享的 AI 闸口提示（设置 → 模型配置）。 */
export const AI_GATE_DETAIL = "AI 未接通：请先在 设置 → 模型配置 添加方案、粘贴密钥并设为「使用中」。";

export function useMacro(client: CoreClient, active: boolean, aiReady: boolean) {
  const [macroSnapshots, setMacroSnapshots] = useState<Record<string, MacroSnapshot | null>>({});
  const [macroLoading, setMacroLoading] = useState(false);
  // 当前生效权重版本（M5.5 版本链）：用于宏观页滑杆预填已保存值，null = 尚无用户版本（默认 v1）。
  const [macroWeights, setMacroWeights] = useState<{ version: string; weights: Record<string, number> } | null>(null);

  // loadMacroData 可重入：宏观雷达插件一键启用成功后立即重拉各国快照（AppShell changePlugin 经 ref 调用）。
  const loadMacroDataRef = useRef<() => void>(() => {});
  const macroLoadedAtRef = useRef<number | null>(null);
  useEffect(() => {
    if (client.isDemo) {
      return;
    }
    let cancelled = false;
    const loadMacroData = () => {
      macroLoadedAtRef.current = Date.now();
      setMacroLoading(true);
      void (async () => {
        const results = await Promise.all([
          ...REGIONS.map((region) => client.request<MacroSnapshot>({ method: "GET", path: `/evidence/macro/${region}` })),
        ]);
        if (cancelled) return;
        const next: Record<string, MacroSnapshot | null> = {};
        results.forEach((res, index) => {
          next[REGIONS[index]!] = res.status < 400 ? res.data : null;
        });
        setMacroSnapshots(next);
        setMacroLoading(false);
      })();
    };
    loadMacroDataRef.current = loadMacroData;
    loadMacroData();
    return () => {
      cancelled = true;
    };
  }, [client]);

  // H3-1(宏观部分):切到宏观页时检查缓存年龄,超过 12h TTL 才重拉(配合后端缓存,不反复外拉)。
  useEffect(() => {
    if (!active || client.isDemo) return;
    const loadedAt = macroLoadedAtRef.current;
    if (loadedAt !== null && Date.now() - loadedAt < 12 * 3600 * 1000) return;
    loadMacroDataRef.current();
  }, [active, client]);

  async function saveMacroWeights(weights: Record<string, number>, note: string, source: "user" | "ai" = "user"): Promise<{ version: string; personalized: string[] } | null> {
    const response = await client.request<{ version: string; weights: Record<string, number>; personalized: string[] }>({
      method: "POST",
      path: "/evidence/macro/weights",
      body: { weights, note, source },
    });
    if (response.status >= 400) return null;
    // 保存成功即成为当前生效版本，同步给宏观页滑杆。
    setMacroWeights({ version: response.data.version, weights: response.data.weights });
    setMacroLoading(true);
    // 权重变更后重拉快照：所有国家的定位立即用新版本复算。
    const results = await Promise.all(
      REGIONS.map((region) => client.request<MacroSnapshot>({ method: "GET", path: `/evidence/macro/${region}` })),
    );
    const next: Record<string, MacroSnapshot | null> = {};
    results.forEach((res, index) => {
      next[REGIONS[index]!] = res.status < 400 ? res.data : null;
    });
    setMacroSnapshots(next);
    setMacroLoading(false);
    return response.data;
  }

  async function submitMacroAnomalies(region: string): Promise<number | null> {
    const response = await client.request<unknown[]>({ method: "POST", path: `/evidence/macro/${region}/anomalies` });
    if (response.status >= 400) return null;
    return response.data.length;
  }

  async function generateMacroAnalysis(region: string): Promise<MacroAnalysisResult | null> {
    if (!aiReady) return { ok: false, stage: "model_not_configured", detail: AI_GATE_DETAIL };
    const response = await client.request<MacroAnalysisResult>({ method: "POST", path: `/evidence/macro/${region}/analysis` });
    if (response.status >= 400) return null;
    return response.data;
  }

  async function fetchMacroAnalysisCache(region: string): Promise<MacroAnalysisResult | null> {
    const response = await client.request<MacroAnalysisResult>({ method: "GET", path: `/evidence/macro/${region}/analysis` });
    if (response.status >= 400) return null;
    return response.data.cached ? response.data : null;
  }

  async function requestWeightProposal(intent: string): Promise<WeightProposalResponse | null> {
    if (!aiReady) return { ok: false, stage: "model_not_configured", detail: AI_GATE_DETAIL };
    const response = await client.request<WeightProposalResponse>({
      method: "POST",
      path: "/evidence/macro/weights/ai-proposal",
      body: { intent },
    });
    if (response.status >= 400) return null;
    return response.data;
  }

  async function fetchMacroPricing(region: string): Promise<MacroPricingSnapshot | null> {
    const response = await client.request<MacroPricingSnapshot>({ method: "GET", path: `/evidence/macro/${region}/pricing` });
    if (response.status >= 400) return null;
    return response.data;
  }

  async function fetchMacroCalendar(days: number): Promise<MacroCalendarSnapshot | null> {
    const response = await client.request<MacroCalendarSnapshot>({ method: "GET", path: `/evidence/macro/calendar?days=${days}` });
    if (response.status >= 400) return null;
    return response.data;
  }

  async function fetchMacroReleases(): Promise<MacroReleaseBoard | null> {
    const response = await client.request<MacroReleaseBoard>({ method: "GET", path: "/evidence/macro/releases" });
    if (response.status >= 400) return null;
    return response.data;
  }

  async function fetchMacroEvents(): Promise<MacroEventImpact[] | null> {
    const response = await client.request<{ items: MacroEventImpact[] }>({ method: "GET", path: "/evidence/macro/events" });
    if (response.status >= 400) return null;
    return response.data.items;
  }

  async function fetchSpeakerSignals(region: string): Promise<SpeakerSignal[] | null> {
    const response = await client.request<SpeakerSignal[]>({ method: "GET", path: `/evidence/macro/${region}/signals` });
    if (response.status >= 400) return null;
    return response.data;
  }

  async function createSpeakerSignal(region: string, input: SpeakerSignalInput): Promise<SpeakerSignal | null> {
    const response = await client.request<SpeakerSignal>({ method: "POST", path: `/evidence/macro/${region}/signals`, body: input });
    if (response.status >= 400) return null;
    return response.data;
  }

  async function interpretSpeakerSignal(region: string, signalId: string): Promise<{ ok: boolean; stage?: string; detail?: string; citations?: string[]; signal?: SpeakerSignal } | null> {
    if (!aiReady) return { ok: false, stage: "model_not_configured", detail: AI_GATE_DETAIL };
    const response = await client.request<{ ok: boolean; stage?: string; detail?: string; citations?: string[]; signal?: SpeakerSignal }>({
      method: "POST",
      path: `/evidence/macro/${region}/signals/${signalId}/interpret`,
    });
    if (response.status >= 400 && response.status !== 409) return null;
    return response.status === 409 ? { ok: false, detail: "没有「使用中」的模型方案：先在设置页把一个方案置为使用中" } : response.data;
  }

  async function fetchMacroUserView(region: string): Promise<MacroUserView | null> {
    const response = await client.request<{ region: string; view: MacroUserView | null }>({
      method: "GET",
      path: `/evidence/macro/${region}/my-view`,
    });
    return response.status < 400 ? response.data.view : null;
  }

  async function saveMacroUserView(region: string, input: { direction: string; horizon: string; confidence: string; text: string }): Promise<MacroUserView | null> {
    const response = await client.request<MacroUserView>({
      method: "PUT",
      path: `/evidence/macro/${region}/my-view`,
      body: input,
    });
    return response.status < 400 ? response.data : null;
  }

  async function fetchComtradePreview(input: { reporter_code?: number; partner_code: number; period?: string; cmd_code: string; flow_code: string; max_records: number }): Promise<{ available: boolean; source?: string; source_url?: string; query?: Record<string, string>; count?: number; data: Array<Record<string, unknown>>; as_of?: string; degraded_reason?: string } | null> {
    const params = new URLSearchParams({
      partner_code: String(input.partner_code),
      cmd_code: input.cmd_code,
      flow_code: input.flow_code,
      max_records: String(input.max_records),
    });
    if (input.reporter_code !== undefined) params.set("reporter_code", String(input.reporter_code));
    if (input.period?.trim()) params.set("period", input.period.trim());
    const response = await client.request<{ available: boolean; source?: string; source_url?: string; query?: Record<string, string>; count?: number; data: Array<Record<string, unknown>>; as_of?: string; degraded_reason?: string }>({ method: "GET", path: `/evidence/comtrade?${params.toString()}` });
    return response.status < 400 ? response.data : null;
  }

  /** G0 贸易弧实拉：GET /evidence/macro/trade（freq=M 月度多值单请求；失败返回 null 由前端回退）。 */
  async function fetchMacroTrade(input: { reporter: number; partner: number; years?: number; cmd?: string; freq?: "A" | "M"; months?: number }): Promise<{ series: MacroTradePoint[]; source: string } | null> {
    const params = new URLSearchParams({
      reporter: String(input.reporter),
      partner: String(input.partner),
      years: String(input.years ?? 1),
      cmd: input.cmd ?? "TOTAL",
      freq: input.freq ?? "A",
      months: String(input.months ?? 3),
    });
    const response = await client.request<{ reporter: number; partner: number; cmd: string; series: MacroTradePoint[]; source: string }>({ method: "GET", path: `/evidence/macro/trade?${params.toString()}` });
    return response.status < 400 ? response.data : null;
  }

  /** 登记宏观研究判断：POST /evidence/macro/research-evidence（claim 与 time_window 为必填）。 */
  async function registerMacroResearch(input: { event_id: string; claim: string; time_window: string; direction?: string; instrument_or_market?: string; source_refs?: string[] }): Promise<{ id: string } | null> {
    const response = await client.request<{ id: string }>({ method: "POST", path: "/evidence/macro/research-evidence", body: input });
    return response.status < 400 ? response.data : null;
  }

  /** 拉取已登记的宏观研究判断：GET /evidence/macro/research-evidence。 */
  async function fetchMacroResearchList(eventId?: string): Promise<Array<Record<string, unknown>> | null> {
    const response = await client.request<Array<Record<string, unknown>>>({ method: "GET", path: eventId ? `/evidence/macro/research-evidence?event=${encodeURIComponent(eventId)}` : "/evidence/macro/research-evidence" });
    return response.status < 400 ? response.data : null;
  }

  /** 宏观权重版本链：预填宏观页滑杆（loadAll 启动/刷新时调用）。macro-radar 未启用时 409 属正常业务态，静默保持 null。 */
  async function seedWeights(): Promise<void> {
    const weightsRes = await client.request<{ active_version: string; default_weights: Record<string, number>; versions: Array<{ version: string; weights: Record<string, number> }> }>({ method: "GET", path: "/evidence/macro/weights" });
    if (weightsRes.status < 400 && weightsRes.data.versions?.length) {
      const current = weightsRes.data.versions[0]!;
      setMacroWeights({ version: current.version, weights: current.weights });
    } else {
      setMacroWeights(null);
    }
  }

  return {
    macroSnapshots, macroLoading, macroWeights,
    reload: () => loadMacroDataRef.current(),
    seedWeights,
    saveMacroWeights, submitMacroAnomalies,
    generateMacroAnalysis, fetchMacroAnalysisCache, requestWeightProposal,
    fetchMacroPricing, fetchMacroCalendar, fetchMacroReleases, fetchMacroEvents,
    fetchSpeakerSignals, createSpeakerSignal, interpretSpeakerSignal,
    fetchMacroUserView, saveMacroUserView,
    fetchComtradePreview, fetchMacroTrade,
    registerMacroResearch, fetchMacroResearchList,
  };
}

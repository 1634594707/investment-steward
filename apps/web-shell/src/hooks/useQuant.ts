import { useState } from "react";
import type { ArtifactPoolView } from "@investment-steward/domain-contracts";
import { detailOf, type CoreClient } from "../state/coreClient";

/**
 * B2（frontend-optimization-roadmap-2026-09-12）：量化研究域数据，自 AppShell 原样下沉。
 * QuantPage 直接消费本钩子；pool 由 AppShell 的 loadAll 启动装载经 setPool 回填。
 * 以下领域类型原样自 AppShell 搬出（QuantPage 改从本模块导入）。
 */

/** 因子挖掘（POST /quant/factors/mine/{symbol}）：确定性枚举 + 验证集 IC 排序的参数集。 */
export interface QuantFactorItem {
  formula: string;
  formula_tokens: string[];
  train_ic: number;
  valid_ic: number;
  samples: number;
}
export interface QuantFactorMineResult {
  available: boolean;
  symbol: string;
  bars?: number;
  top: QuantFactorItem[];
  note?: string;
  source?: string;
  /** 分享池阶段 A 形态字段:收口 bar 时间与数据版本(导出参数集用)。 */
  as_of?: string | null;
  dataset_version?: string | null;
  degraded_reason?: string | null;
}

/** 分享池阶段 A/D 制品(GET /quant/parameter-sets):内容寻址、不可变;模型条目带权重与训练快照。 */
export interface QuantParameterSet {
  artifact_id: string;
  name: string;
  symbol: string;
  formula_tokens?: string[];
  formula?: string;
  weights?: number[];
  feature_order?: string[];
  metrics: { train_ic: number; valid_ic: number; samples: number; forked_from?: string };
  training?: { lambda?: number; forward_days?: number; split_index?: number; as_of?: string | null };
  type: string;
  stage: string;
  author: string;
  parent_id: string | null;
  note: string;
  as_of: string | null;
  dataset_version: string | null;
  created_at: string;
  track_records?: QuantTrackRecord[];
}

/** 实盘记录两级制(GET /quant/parameter-sets 联表):自报 vs 对账单核验,只作筛选不参与排序。 */
export interface QuantTrackRecord {
  record_id: string;
  artifact_id: string;
  state: "self_reported" | "broker_verified";
  period_start: string;
  period_end: string;
  return_pct: number;
  max_drawdown_pct: number | null;
  source: string | null;
  statement_sha256: string | null;
  note: string;
  created_at: string;
}

/** 策略包目录(GET /quant/strategy-packs):发布者签名制,受限执行器运行。 */
export interface QuantStrategyPack {
  pack_id: string;
  name: string;
  version: string;
  author: string;
  entrypoint: string;
  symbol?: string;
  capabilities: string[];
  artifact_sha256: string;
  payload_sha256?: string;
  signature_valid: boolean;
  state: string;
  imported_at: string;
}

/** 策略包回测(POST /quant/strategy-packs/{id}/run):受限执行器 + 确定性统计。 */
export interface QuantPackRunResult {
  available?: boolean;
  pack_id: string;
  name?: string;
  equity?: number;
  curve?: Array<{ date: string; position: number; equity: number }>;
  win_rate?: number | null;
  bars?: number;
  source?: string;
  note?: string;
  degraded_reason?: string | null;
}

/** 分享池阶段条(GET /quant/stages):各阶段开放状态与原因,UI 动态渲染。 */
export interface QuantStageInfo {
  key: string;
  open: boolean;
  reason: string;
}

/** 确定性回放（GET /quant/parameter-sets/{id}/replay）：tanh 仓位 × 次日收益。 */
export interface QuantReplayResult {
  available?: boolean;
  artifact_id: string;
  equity: number;
  curve: Array<{ date: string; position: number; equity: number }>;
  win_rate: number | null;
  bars: number;
  note: string;
  source?: string;
  degraded_reason?: string | null;
}

export function useQuant(client: CoreClient) {
  const [quantPool, setQuantPool] = useState<ArtifactPoolView | null>(null);

  // —— AlphaMaster 补充:确定性因子挖掘(公式 token 序列 + 验证集 IC;落库缓存) ——
  async function fetchFactorMine(symbol: string): Promise<QuantFactorMineResult | null> {
    const response = await client.request<QuantFactorMineResult>({ method: "POST", path: `/quant/factors/mine/${symbol}` });
    if (response.status >= 400) return null;
    return response.data;
  }

  // —— A4-1 分享池阶段 A 通道:本机参数集列表 / 发布 / Fork / 确定性回放 / 谱系 ——
  async function fetchQuantParameterSets(): Promise<QuantParameterSet[] | null> {
    const response = await client.request<QuantParameterSet[]>({ method: "GET", path: "/quant/parameter-sets" });
    if (response.status >= 400 || !Array.isArray(response.data)) return null;
    return response.data;
  }

  async function publishQuantParameterSet(input: { symbol: string; formulaTokens: string[]; name?: string; note?: string }): Promise<{ ok: boolean; entry?: QuantParameterSet; detail?: string }> {
    const response = await client.request<QuantParameterSet>({ method: "POST", path: "/quant/parameter-sets", body: { symbol: input.symbol, formula_tokens: input.formulaTokens, name: input.name ?? "", note: input.note ?? "" } });
    if (response.status >= 400) return { ok: false, detail: detailOf(response.data) ?? "发布失败(Core 未就绪或公式非法)。" };
    return { ok: true, entry: response.data };
  }

  async function forkQuantParameterSet(artifactId: string): Promise<QuantParameterSet | null> {
    const response = await client.request<QuantParameterSet>({ method: "POST", path: `/quant/parameter-sets/${artifactId}/fork`, body: {} });
    if (response.status >= 400) return null;
    return response.data;
  }

  async function fetchQuantReplay(artifactId: string): Promise<QuantReplayResult | null> {
    const response = await client.request<QuantReplayResult>({ method: "GET", path: `/quant/parameter-sets/${artifactId}/replay` });
    if (response.status >= 400) return null;
    return response.data;
  }

  async function fetchQuantLineage(artifactId: string): Promise<QuantParameterSet[] | null> {
    const response = await client.request<QuantParameterSet[]>({ method: "GET", path: `/quant/parameter-sets/${artifactId}/lineage` });
    if (response.status >= 400 || !Array.isArray(response.data)) return null;
    return response.data;
  }

  // —— 阶段条/模型/策略包/实盘记录(B/C/D 本机忠实形态) ——
  async function fetchQuantStages(): Promise<QuantStageInfo[] | null> {
    const response = await client.request<{ stages: QuantStageInfo[] }>({ method: "GET", path: "/quant/stages" });
    if (response.status >= 400 || !Array.isArray(response.data?.stages)) return null;
    return response.data.stages;
  }

  async function trainQuantModel(input: { symbol: string; lambda: number }): Promise<{ ok: boolean; entry?: QuantParameterSet; detail?: string }> {
    const response = await client.request<QuantParameterSet & { available?: boolean; degraded_reason?: string }>({ method: "POST", path: `/quant/models/${input.symbol}`, body: { lambda: input.lambda } });
    if (response.status >= 400) return { ok: false, detail: detailOf(response.data) ?? "训练失败(Core 未就绪或行情不可用)。" };
    if (response.data?.available === false) return { ok: false, detail: response.data.degraded_reason ?? "行情不可用,未训练。" };
    return { ok: true, entry: response.data };
  }

  async function importQuantStrategyPack(manifestText: string): Promise<{ ok: boolean; entry?: QuantStrategyPack; detail?: string }> {
    let manifest: unknown;
    try {
      manifest = JSON.parse(manifestText);
    } catch {
      return { ok: false, detail: "manifest 不是合法 JSON。" };
    }
    const response = await client.request<QuantStrategyPack>({ method: "POST", path: "/quant/strategy-packs", body: { manifest } });
    if (response.status >= 400) return { ok: false, detail: detailOf(response.data) ?? "策略包校验失败。" };
    return { ok: true, entry: response.data };
  }

  async function fetchQuantStrategyPacks(): Promise<QuantStrategyPack[] | null> {
    const response = await client.request<QuantStrategyPack[]>({ method: "GET", path: "/quant/strategy-packs" });
    if (response.status >= 400 || !Array.isArray(response.data)) return null;
    return response.data;
  }

  async function runQuantStrategyPack(packId: string): Promise<QuantPackRunResult | null> {
    const response = await client.request<QuantPackRunResult>({ method: "POST", path: `/quant/strategy-packs/${packId}/run` });
    if (response.status >= 400) return null;
    return response.data;
  }

  async function addQuantTrackRecord(artifactId: string, input: Omit<QuantTrackRecord, "record_id" | "artifact_id" | "state" | "source" | "statement_sha256" | "created_at">): Promise<{ ok: boolean; detail?: string }> {
    const response = await client.request<QuantTrackRecord>({ method: "POST", path: `/quant/parameter-sets/${artifactId}/track-records`, body: input });
    if (response.status >= 400) return { ok: false, detail: detailOf(response.data) ?? "自报记录提交失败。" };
    return { ok: true };
  }

  async function verifyQuantTrackRecord(artifactId: string, input: { statement: string; source: string; period_start: string; period_end: string; return_pct: number; max_drawdown_pct: number | null; note: string }): Promise<{ ok: boolean; detail?: string }> {
    const response = await client.request<QuantTrackRecord>({ method: "POST", path: `/quant/parameter-sets/${artifactId}/track-records/verify`, body: input });
    if (response.status >= 400) return { ok: false, detail: detailOf(response.data) ?? "对账单核验失败。" };
    return { ok: true };
  }

  return {
    quantPool, setQuantPool,
    fetchFactorMine, fetchQuantParameterSets, publishQuantParameterSet, forkQuantParameterSet,
    fetchQuantReplay, fetchQuantLineage, fetchQuantStages, trainQuantModel,
    importQuantStrategyPack, fetchQuantStrategyPacks, runQuantStrategyPack,
    addQuantTrackRecord, verifyQuantTrackRecord,
  };
}

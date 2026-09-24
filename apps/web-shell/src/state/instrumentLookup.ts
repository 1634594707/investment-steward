/**
 * M4（2026-09-15 第二轮路线图）E01/E02：标的查找（代码 ⇄ 名称）前端通道。
 *
 * 只做一件事：把搜索框输入交给 Core 的 `GET /instruments/lookup?q=`，
 * 并把「没找到」与「查找服务不可用」分成两种结果——两者都禁止保存，但提示文案不同，
 * 避免用户把服务故障误当成「这只票不存在」。
 */
import type { CoreClient } from "./coreClient";

export interface InstrumentLookupHit {
  /** 6 位 A 股 / 场内 ETF 代码（与 `POST /holdings` 的 instrument 同口径）。 */
  key: string;
  name: string;
  /** `stock` | `etf`。 */
  kind: string;
  /** `sh` | `sz` | `bj`。 */
  market: string;
  /** 下拉展示文案：`名称（代码）`。 */
  display: string;
}

export type InstrumentLookupOutcome =
  | { ok: true; hits: InstrumentLookupHit[] }
  | { ok: false; message: string };

/** 标的类型的中文展示（下拉右侧标签）。 */
export const INSTRUMENT_KIND_LABEL: Record<string, string> = {
  stock: "A股",
  etf: "场内ETF",
};

/** 交易所中文名（下拉副文案）。 */
export const INSTRUMENT_MARKET_LABEL: Record<string, string> = {
  sh: "上交所",
  sz: "深交所",
  bj: "北交所",
};

/** 查询串拼进 path（`CoreRequest` 无 query 字段，沿用既有约定）。 */
export async function lookupInstruments(
  client: CoreClient,
  query: string,
): Promise<InstrumentLookupOutcome> {
  const clean = query.trim();
  if (!clean) return { ok: true, hits: [] };
  const response = await client.request<InstrumentLookupHit[]>({
    method: "GET",
    path: `/instruments/lookup?q=${encodeURIComponent(clean)}` as `/${string}`,
  });
  if (response.status !== 200 || !Array.isArray(response.data)) {
    return { ok: false, message: "查找服务暂不可用，请稍后重试（未使用本地猜测的名称）。" };
  }
  return { ok: true, hits: response.data };
}

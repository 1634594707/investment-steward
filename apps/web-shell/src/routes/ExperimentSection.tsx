/**
 * 本地量化实验面板（路线图阶段 3 / §7.2 前端接入）。
 * 选择已发布参数集 → 跑成本口径回测实验（数据快照 + 结果哈希登记）→ 历史列表 → 一键复现（逐位比对哈希）。
 * 全部走 coreRequest 通道;无通道时整体降级为提示。
 */
import { useEffect, useState } from "react";
import { Check, X } from "lucide-react";
import type { CoreRequest } from "../state/coreClient";

interface CoreResponse<T> { status: number; data: T }

async function call<T>(coreRequest: (req: CoreRequest) => Promise<CoreResponse<unknown>>, req: CoreRequest): Promise<T> {
  const { status, data } = await coreRequest(req);
  if (status >= 400) throw new Error(`请求失败(${status})`);
  return data as T;
}

interface ParamSetLite { artifact_id: string; symbol: string; name: string; type: string }

interface ExperimentResult {
  experiment_id: string;
  result_hash: string;
  data_snapshot: { snapshot_hash: string; file_name: string; content_hash: string; bar_count: number; as_of: string };
  spec_version: string;
  costs: { commission_bps: number; slippage_bps: number; max_position: number; max_turnover_per_bar: number };
  equity: number;
  baseline_buy_hold: number;
  excess_vs_baseline: number;
  win_rate: number | null;
  max_drawdown: number;
  total_turnover: number;
  bars: number;
  split: { train_bars: number; out_of_sample_bars: number };
}

interface ExperimentRow {
  experiment_id: string;
  symbol: string;
  label: string;
  created_at: string;
  result_hash: string;
  result: { equity: number; baseline_buy_hold: number; excess_vs_baseline: number; bars: number };
}

interface ReproduceReport {
  experiment_id: string;
  match: boolean;
  expected_result_hash: string;
  actual_result_hash: string;
  detail: string;
}

function pct(value: number): string {
  return `${((value - 1) * 100).toFixed(2)}%`;
}

export function ExperimentSection({ coreRequest }: { coreRequest?: (req: CoreRequest) => Promise<CoreResponse<unknown>> }) {
  const [paramSets, setParamSets] = useState<ParamSetLite[]>([]);
  const [selected, setSelected] = useState("");
  const [experiments, setExperiments] = useState<ExperimentRow[]>([]);
  const [latest, setLatest] = useState<ExperimentResult | null>(null);
  const [repro, setRepro] = useState<ReproduceReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    if (!coreRequest) return;
    try {
      const [sets, list] = await Promise.all([
        call<ParamSetLite[]>(coreRequest, { method: "GET", path: "/quant/parameter-sets" }),
        call<ExperimentRow[]>(coreRequest, { method: "GET", path: "/quant/experiments" }),
      ]);
      setParamSets(sets);
      setExperiments(list);
    } catch (err) {
      setError(err instanceof Error ? err.message : "实验数据加载失败");
    }
  }
  useEffect(() => { void load(); /* eslint-disable-line react-hooks/exhaustive-deps */ }, [coreRequest]);

  async function runExperiment() {
    if (!selected || busy || !coreRequest) return;
    setBusy(true); setError(null); setRepro(null);
    try {
      const result = await call<ExperimentResult>(coreRequest, {
        method: "POST", path: "/quant/experiments", body: { artifact_id: selected },
      });
      setLatest(result);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "实验运行失败");
    } finally { setBusy(false); }
  }

  async function reproduce(experimentId: string) {
    if (busy || !coreRequest) return;
    setBusy(true); setError(null);
    try {
      setRepro(await call<ReproduceReport>(coreRequest, { method: "POST", path: `/quant/experiments/${experimentId}/reproduce` }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "复现失败(制品可能缺失或已损坏)");
    } finally { setBusy(false); }
  }

  if (!coreRequest) {
    return <p className="evidence-group-empty">实验面板需要本机 Core 通道（桌面端可用）。</p>;
  }

  return (
    <section className="qe-section" aria-label="本地量化实验">
      <div className="qe-block">
        <h4>本地实验 <small>成本口径回测(手续费+滑点+仓位/换手上限) · 数据快照 · 结果哈希 · 基线=买入持有</small></h4>
        <div className="qe-run">
          <select value={selected} onChange={(event) => setSelected(event.target.value)} aria-label="选择参数集">
            <option value="">选择已发布参数集…</option>
            {paramSets.map((item) => (
              <option key={item.artifact_id} value={item.artifact_id}>
                {item.name || item.artifact_id}（{item.symbol}）
              </option>
            ))}
          </select>
          <button className="primary-btn" disabled={!selected || busy} onClick={runExperiment}>
            {busy ? "运行中…" : "跑实验"}
          </button>
        </div>
        {error && <p className="form-error" role="alert">{error}</p>}
        {latest && (
          <div className="qe-result">
            <div className="qe-metrics">
              <span>策略 <b className={latest.excess_vs_baseline >= 0 ? "qe-up" : "qe-down"}>{pct(latest.equity)}</b></span>
              <span>基线 <b>{pct(latest.baseline_buy_hold)}</b></span>
              <span>超额 <b className={latest.excess_vs_baseline >= 0 ? "qe-up" : "qe-down"}>{(latest.excess_vs_baseline * 100).toFixed(2)}%</b></span>
              <span>胜率 <b>{latest.win_rate == null ? "—" : `${(latest.win_rate * 100).toFixed(1)}%`}</b></span>
              <span>最大回撤 <b>{(latest.max_drawdown * 100).toFixed(2)}%</b></span>
              <span>总换手 <b>{latest.total_turnover.toFixed(2)}</b></span>
            </div>
            <div className="qe-meta">
              <span>样本 {latest.bars} 根(训练 {latest.split.train_bars} / 样本外 {latest.split.out_of_sample_bars})</span>
              <span>口径 {latest.spec_version}(手续费 {latest.costs.commission_bps}bps + 滑点 {latest.costs.slippage_bps}bps)</span>
              <span>快照 {latest.data_snapshot.snapshot_hash.slice(0, 12)}…({latest.data_snapshot.bar_count} 根)</span>
              <span>结果哈希 {latest.result_hash.slice(0, 12)}…</span>
            </div>
            <p className="qe-note">同快照同配置幂等;复现以登记快照重跑逐位比对哈希。</p>
          </div>
        )}
        {repro && (
          <div className={`qe-repro ${repro.match ? "qe-match" : "qe-mismatch"}`} role="status">
            {repro.match ? (<><Check size={12} aria-hidden="true" /> 复现一致</>) : (<><X size={12} aria-hidden="true" /> 复现不一致</>)} — {repro.detail}
            <span className="qe-hash">{repro.actual_result_hash.slice(0, 12)}…</span>
          </div>
        )}
      </div>
      <div className="qe-block">
        <h4>实验历史 <small>{experiments.length ? `${experiments.length} 条(最多 200)` : ""}</small></h4>
        {experiments.length === 0 && <p className="evidence-group-empty">暂无实验;选择参数集后运行。</p>}
        {experiments.map((item) => (
          <div className="qe-row" key={item.experiment_id}>
            <div className="qe-row-main">
              <b>{item.label || item.experiment_id}</b>
              <span className="qe-symbol">{item.symbol}</span>
              <span className="qe-hash">{item.experiment_id}</span>
              <span className="qe-time">{item.created_at.slice(0, 16).replace("T", " ")}</span>
            </div>
            <div className="qe-row-side">
              <span>超额 <b className={item.result.excess_vs_baseline >= 0 ? "qe-up" : "qe-down"}>{(item.result.excess_vs_baseline * 100).toFixed(2)}%</b></span>
              <button className="ghost-btn" disabled={busy} onClick={() => reproduce(item.experiment_id)}>复现</button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

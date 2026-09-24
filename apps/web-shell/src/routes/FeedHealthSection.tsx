import { useEffect, useState } from "react";
import type { CoreRequest } from "../state/coreClient";
import { detailOf } from "../state/coreClient";
import "./settings.css";

/**
 * F02/F03（桌面端升级路线图 2026-09-18）：今日取数健康。
 * 数据来自 F02 的取数日志（每个三方源的尝试次数/成功率/延迟分位/错误类别），
 * 「仅重试失败项」只对窗口内失败过的来源各发一次最小真实请求（走 POST /data-source/retry），
 * 找不到前置条件（凭据）或需要交易日的来源会**如实说明原因**而不是给一个点不动的按钮。
 */
interface FeedLatencyStats {
  avg: number | null;
  p50: number | null;
  p95: number | null;
  max: number | null;
}

interface FeedSourceHealth {
  source: string;
  evidence_count: number;
  attempts: number;
  ok: number;
  errors: number;
  success_rate: number | null;
  latency_ms: FeedLatencyStats;
  error_kinds: Array<{ kind: string; count: number }>;
  last_attempt_at: string | null;
  retryable: boolean;
  retry_blocked_reason: string | null;
  note: string;
}

interface FeedQualityReport {
  sources: FeedSourceHealth[];
  gaps: Array<{ dimension: string; reason: string }>;
  window: string;
  attempts_total: number;
  attempts_truncated: number;
  rule: string;
}

interface FeedFailureRow {
  source: string;
  endpoint: string | null;
  params_fingerprint: string | null;
  error_kind: string | null;
  latency_ms: number;
  started_at: string;
  attempt_index: number;
}

interface FeedRetryResult {
  source: string;
  supported: boolean;
  ok: boolean | null;
  detail?: string;
  reason?: string;
  skipped?: boolean;
}

/** 错误类别→人话。类别由后端按**异常类型**归类，不解析供应商文案（文案会随版本变）。 */
const FEED_ERROR_KIND_LABELS: Record<string, string> = {
  timeout: "超时",
  network: "连接失败（断连/DNS/TLS）",
  http_error: "HTTP 错误",
  invalid_payload: "响应不可解析",
  feed_error: "上游响应不可用（结构异常/空数据/错误页）",
  oversized_response: "响应超过本地上限",
};

function feedErrorKindLabel(kind: string | null): string {
  if (!kind) return "—";
  return FEED_ERROR_KIND_LABELS[kind] ?? kind;
}

function feedSuccessRateText(rate: number | null): string {
  if (rate === null) return "—（无样本）";
  return `${Math.round(rate * 1000) / 10}%`;
}

function feedLatencyText(stats: FeedLatencyStats): string {
  if (stats.p95 === null) return "—（无样本）";
  return `P95 ${stats.p95}ms（P50 ${stats.p50 ?? "—"}ms · 峰值 ${stats.max ?? "—"}ms）`;
}

export function FeedHealthSection({ coreRequest }: { coreRequest?: (req: CoreRequest) => Promise<{ status: number; data: unknown }> }) {
  const [window, setWindow] = useState<"7d" | "30d">("7d");
  const [report, setReport] = useState<FeedQualityReport | null>(null);
  const [failures, setFailures] = useState<FeedFailureRow[]>([]);
  const [retryResults, setRetryResults] = useState<FeedRetryResult[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"load" | "retry" | null>(null);

  async function call<T>(req: CoreRequest): Promise<T> {
    if (!coreRequest) throw new Error("当前环境没有 Core 请求通道。");
    const { status, data } = await coreRequest(req);
    if (status >= 400) throw new Error(detailOf(data) ?? `Core 返回错误（HTTP ${status}）`);
    return data as T;
  }

  async function load(nextWindow: "7d" | "30d" = window) {
    if (!coreRequest) return;
    setBusy("load");
    setError(null);
    try {
      const [quality, failureList] = await Promise.all([
        call<FeedQualityReport>({ method: "GET", path: `/data-source/quality?window=${nextWindow}` }),
        call<{ failures: FeedFailureRow[] }>({ method: "GET", path: `/data-source/failures?window=${nextWindow}&limit=20` }),
      ]);
      setReport(quality);
      setFailures(failureList.failures ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "取数健康读取失败");
    } finally {
      setBusy(null);
    }
  }

  async function retryFailed() {
    if (!coreRequest) return;
    setBusy("retry");
    setError(null);
    try {
      const result = await call<{ attempted: number; results: FeedRetryResult[]; note?: string }>({
        method: "POST",
        path: "/data-source/retry",
        body: { only_failed: true, window },
      });
      setRetryResults(result.results ?? []);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "重试失败");
    } finally {
      setBusy(null);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const needAttention = (report?.sources ?? []).filter(
    (row) => row.errors > 0 || (row.attempts > 0 && row.evidence_count === 0),
  );

  return (
    <div className="dl-block">
      <h4>今日取数健康</h4>
      <p>
        每次向三方数据源取数都会留一条日志（成功与失败都记，含重试的第几次），所以成功率与延迟是实测值而不是估算。
        样本为 0 时显示「—（无样本）」——这与「全都失败」是两件事。缓存命中的调用不发请求，因而不产生样本。
      </p>
      <div className="filter-row">
        <button className={`tag-button ${window === "7d" ? "active" : ""}`} onClick={() => { setWindow("7d"); void load("7d"); }}>最近 7 天</button>
        <button className={`tag-button ${window === "30d" ? "active" : ""}`} onClick={() => { setWindow("30d"); void load("30d"); }}>最近 30 天</button>
        <button className="ghost-btn" disabled={busy !== null} onClick={() => void load()}>刷新</button>
        <button
          className="ghost-btn"
          disabled={busy !== null || failures.length === 0}
          onClick={() => void retryFailed()}
          title={failures.length === 0 ? "窗口内没有失败记录" : "只对失败过的来源各发一次真实请求，不对健康的源重复打上游"}
        >
          仅重试失败项
        </button>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      {report !== null && (
        report.sources.length === 0 ? (
          <p className="scheme-empty">窗口内既没有取数日志也没有证据记录。</p>
        ) : (
          <table className="kv-table">
            <thead>
              <tr><th>来源</th><th>尝试</th><th>成功率</th><th>延迟</th><th>失败类别</th><th>入账证据</th></tr>
            </thead>
            <tbody>
              {report.sources.map((row) => (
                <tr key={row.source}>
                  <td title={row.note}>{row.source}</td>
                  <td>{row.attempts === 0 ? "—" : `${row.attempts}${row.errors > 0 ? `（失败 ${row.errors}）` : ""}`}</td>
                  <td>{feedSuccessRateText(row.success_rate)}</td>
                  <td>{feedLatencyText(row.latency_ms)}</td>
                  <td>{row.error_kinds.length === 0 ? "—" : row.error_kinds.map((k) => `${feedErrorKindLabel(k.kind)}×${k.count}`).join("、")}</td>
                  <td>{row.evidence_count === 0 ? "0" : row.evidence_count.toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )
      )}

      {needAttention.length > 0 && (
        <>
          <h5>需要注意的来源</h5>
          <ul className="feed-health-notes">
            {needAttention.map((row) => (
              <li key={`attn-${row.source}`}>
                <b>{row.source}</b>
                <span>：{row.note}</span>
                {!row.retryable && row.retry_blocked_reason && (
                  <span className="feed-health-blocked">不能在此重试：{row.retry_blocked_reason}</span>
                )}
              </li>
            ))}
          </ul>
        </>
      )}

      {retryResults !== null && (
        <div className="storage-report">
          <b>重试结果</b>
          <ul>
            {retryResults.length === 0 && <li>窗口内没有失败过的来源，未发起任何请求。</li>}
            {retryResults.map((item) => (
              <li key={`retry-${item.source}`}>
                {item.source}：
                {item.ok === true ? `成功（${item.detail ?? "已取到数据"}）` : item.ok === false ? `仍失败（${item.reason ?? "原因未提供"}）` : `未发起请求（${item.reason ?? "前置条件不满足"}）`}
              </li>
            ))}
          </ul>
        </div>
      )}

      {failures.length > 0 && (
        <>
          <h5>最近失败记录</h5>
          <table className="kv-table">
            <thead>
              <tr><th>时间</th><th>来源</th><th>第几次</th><th>失败原因</th><th>耗时</th></tr>
            </thead>
            <tbody>
              {failures.map((row, index) => (
                <tr key={`${row.started_at}-${row.source}-${index}`}>
                  <td>{new Date(row.started_at).toLocaleString()}</td>
                  <td title={row.endpoint ?? ""}>{row.source}</td>
                  <td>第 {row.attempt_index} 次</td>
                  <td>{feedErrorKindLabel(row.error_kind)}</td>
                  <td>{row.latency_ms}ms</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {report !== null && report.attempts_truncated > 0 && (
        <p className="form-note">窗口内还有 {report.attempts_truncated} 条日志未计入本次统计（单次读取上限），已如实标注而非静默截断。</p>
      )}
    </div>
  );
}

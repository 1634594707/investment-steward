/**
 * G02（桌面端升级路线图 2026-09-18）：判断后验命中率面板。
 *
 * - 命中率按「模型方案 × 主题 × 时间窗」分桶，**每个数字都带分母**（成立 + 失效）；
 * - 口径由服务端随返回体下发（`policy`），前端只做文案映射，不自己写一套定义；
 * - 任一桶可展开到参与的判定原文与最近一次回填记录 —— 「命中率 50%」必须能点开看到是哪几条；
 * - 分母为 0 时显示「—（分母 0）」，不显示 0%（样本不足不等于全错）；
 * - **只读**：本面板不提供任何改投资原则的入口。原则变更仍走 `POST /investment-policies`
 *   的提议 + 确认链（下方「决策与学习」既有流程），这里只做复盘展示。
 */
import { useEffect, useState } from "react";
import type { CoreRequest } from "../state/coreClient";
import { detailOf } from "../state/coreClient";

interface CoreResponse<T> { status: number; data: T }

async function call<T>(coreRequest: (req: CoreRequest) => Promise<CoreResponse<unknown>>, req: CoreRequest): Promise<T> {
  const { status, data } = await coreRequest(req);
  if (status >= 400) throw new Error(detailOf(data) ?? `Core 返回错误（HTTP ${status}）`);
  return data as T;
}

interface HitRateVerification {
  verification_id?: string;
  result?: string;
  outcome?: string;
  checked_at?: string;
}
interface HitRateJudgment {
  judgment_id: string;
  subject: string;
  statement: string;
  direction: string;
  due_at: string;
  status: string;
  source_report_id?: string | null;
  source_refs?: string[];
  verification?: HitRateVerification | null;
}
export interface HitRateBucket {
  model: string;
  theme: string;
  /** 主题取值来源：report_title=来源研报标题 / judgment_subject=判断对象（无来源研报）。 */
  theme_source: string;
  counts: Record<string, number>;
  judgment_count: number;
  hit_rate: number | null;
  hit_rate_denominator: number;
  hit_rate_note: string | null;
  judgments: HitRateJudgment[];
  truncated: boolean;
}
export interface HitRateReport {
  ok: boolean;
  today: string;
  window_days: number | null;
  policy: Record<string, string>;
  totals: {
    judgments: number;
    verified: number;
    refuted: number;
    insufficient_data: number;
    pending: number;
    denominator: number;
    hit_rate: number | null;
    not_due_excluded: number;
    buckets: number;
  };
  buckets: HitRateBucket[];
  note?: string;
}

const WINDOWS: Array<{ value: string; text: string }> = [
  { value: "30d", text: "最近 30 天" },
  { value: "90d", text: "最近 90 天" },
  { value: "180d", text: "最近 180 天" },
  { value: "all", text: "全部" },
];

const STATUS_TEXT: Record<string, string> = {
  pending: "待回填",
  verified: "成立",
  refuted: "失效",
  insufficient_data: "无数据",
};

/** 口径条目的展示名（值与文案都来自服务端，这里只负责把 key 翻成人话）。 */
const POLICY_LABEL: Record<string, string> = {
  numerator: "分子",
  denominator: "分母",
  excluded: "不计入分母",
  hit_rate: "命中率算法",
  model: "模型方案来源",
  theme: "主题来源",
  window: "时间窗口径",
  verification_value: "回填取值",
  no_auto_policy: "与投资原则的关系",
};

/** 命中率文案：分母为 0 时如实显示「—（分母 0）」，绝不显示 0%。 */
export function hitRateText(hitRate: number | null, denominator: number): string {
  if (hitRate === null || denominator <= 0) return "—（分母 0）";
  return `${(hitRate * 100).toFixed(1)}%（${denominator} 条已回填且结论明确）`;
}

function statusText(status: string): string {
  return STATUS_TEXT[status] ?? status;
}

function bucketKey(bucket: HitRateBucket): string {
  return `${bucket.model}||${bucket.theme}`;
}

export function JudgmentHitRateSection({ coreRequest }: { coreRequest?: (req: CoreRequest) => Promise<CoreResponse<unknown>> }) {
  const [window, setWindow] = useState("90d");
  const [report, setReport] = useState<HitRateReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openBucket, setOpenBucket] = useState<string | null>(null);

  useEffect(() => {
    if (!coreRequest) return;
    let cancelled = false;
    setError(null);
    void (async () => {
      try {
        const data = await call<HitRateReport>(coreRequest, {
          method: "GET",
          path: `/judgments/hit-rate?window=${window}`,
        });
        if (!cancelled) setReport(data);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "命中率读取失败");
      }
    })();
    return () => { cancelled = true; };
  }, [coreRequest, window]);

  if (!coreRequest) {
    return (
      <div className="lt-block">
        <h4>判断后验命中率</h4>
        <p className="evidence-group-empty">当前环境没有 Core 请求通道，此面板不可用。</p>
      </div>
    );
  }

  const totals = report?.totals;

  return (
    <div className="lt-block">
      <div className="lt-head">
        <h4>
          判断后验命中率
          <small>按「模型方案 × 主题 × 时间窗」分桶 · 分母只含已回填且结论明确的判定 · 只读</small>
        </h4>
        <label className="report-meta">
          时间窗
          <select value={window} onChange={(event) => { setWindow(event.target.value); setOpenBucket(null); }} aria-label="命中率时间窗">
            {WINDOWS.map((item) => <option key={item.value} value={item.value}>{item.text}</option>)}
          </select>
        </label>
      </div>

      {error && <p className="form-error" role="alert">{error}</p>}
      {!report && !error && <p className="evidence-group-empty">加载中…</p>}

      {report && totals && (
        <>
          <div className="hr-totals" role="group" aria-label="命中率总量">
            <span className="hr-metric"><b>{totals.judgments}</b> 条判定（窗口内到期）</span>
            <span className="hr-metric hr-mint"><b>{totals.verified}</b> 成立</span>
            <span className="hr-metric hr-coral"><b>{totals.refuted}</b> 失效</span>
            <span className="hr-metric"><b>{totals.pending}</b> 待回填</span>
            <span className="hr-metric"><b>{totals.insufficient_data}</b> 无数据</span>
            <span className="hr-metric hr-strong">
              命中率 <b>{hitRateText(totals.hit_rate, totals.denominator)}</b>
            </span>
            {totals.not_due_excluded > 0 && (
              <span className="hr-metric" title="到期时间还没到的判定不参与后验——「未到期」既不等于已验证，也不算该回填没回填">
                未到期未纳入 {totals.not_due_excluded}
              </span>
            )}
          </div>

          {report.buckets.length === 0 ? (
            <p className="evidence-group-empty">
              窗口内没有已到期的判定。先在工作台「待复盘」队列回填结论，或换更长的时间窗。
            </p>
          ) : (
            <ul className="hr-buckets">
              {report.buckets.map((bucket) => {
                const key = bucketKey(bucket);
                const open = openBucket === key;
                return (
                  <li key={key} className="hr-bucket">
                    <div className="hr-bucket-head">
                      <b className="hr-model" title="模型方案取自判定来源研报的 model 字段">{bucket.model}</b>
                      <span className="hr-theme" title={bucket.theme_source === "report_title" ? "主题取自来源研报标题" : "无来源研报，主题取判断对象"}>{bucket.theme}</span>
                      <span className="hr-metric">{bucket.judgment_count} 条判定</span>
                      <span className="hr-metric hr-strong" title="命中率 = 成立 /（成立 + 失效）">
                        {hitRateText(bucket.hit_rate, bucket.hit_rate_denominator)}
                      </span>
                      <span className="hr-metric">
                        成立 {bucket.counts.verified ?? 0} · 失效 {bucket.counts.refuted ?? 0} · 待回填 {bucket.counts.pending ?? 0} · 无数据 {bucket.counts.insufficient_data ?? 0}
                      </span>
                      <button className="wb-review-open" aria-expanded={open} onClick={() => setOpenBucket(open ? null : key)}>
                        {open ? "收起明细" : "展开明细"}
                      </button>
                    </div>
                    {bucket.hit_rate_note && <small className="hr-note">{bucket.hit_rate_note}</small>}
                    {open && (
                      <div className="hr-detail">
                        <ul className="lt-verifications">
                          {bucket.judgments.map((item) => (
                            <li key={item.judgment_id}>
                              <span className="lt-statement">{item.statement}</span>
                              <small className="hr-detail-meta">
                                {item.subject} · 到期 {item.due_at.slice(0, 10)} · {statusText(item.status)}
                                {item.verification?.outcome ? ` · 回填说明：${item.verification.outcome}` : ""}
                                {item.verification?.checked_at ? `（${item.verification.checked_at.slice(0, 10)}）` : ""}
                                {item.source_report_id ? ` · 来源研报 ${item.source_report_id}` : " · 手工录入判断"}
                              </small>
                            </li>
                          ))}
                        </ul>
                        {bucket.truncated && (
                          <small className="hr-note">明细超过单桶上限（200 条）已截断：命中率与计数仍按全量计算，列表只展示前 200 条。</small>
                        )}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}

          <details className="hr-policy">
            <summary>口径（{Object.keys(report.policy).length} 条，逐条可核）</summary>
            <ul className="lt-verifications">
              {Object.entries(report.policy).map(([key, value]) => (
                <li key={key}><b>{POLICY_LABEL[key] ?? key}</b>：{value}</li>
              ))}
            </ul>
          </details>
          {report.note && <p className="report-meta">{report.note}</p>}
        </>
      )}
    </div>
  );
}

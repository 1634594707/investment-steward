import type { StockReport } from "./researchTypes";
import { SOURCE_HEALTH_LABELS as LABELS, SOURCE_HEALTH_UNAVAILABLE, sourceHealthDetails } from "./sourceHealth";

export function SourceHealthPanel({ report, onSupplement, busy = false }: {
  report: StockReport;
  onSupplement?: (question: string) => void;
  busy?: boolean;
}) {
  const health = report.source_health;
  return <div className="wb-quality">
    <b>来源数据健康</b>
    {!health || health.sources.length === 0 ? <p className="report-meta">{SOURCE_HEALTH_UNAVAILABLE}</p> : <>
      <p className="report-meta">检查日期：{health.checked_on}。{health.note}</p>
      <ul className="answer-list">
        {health.sources.map((row) => <li key={row.source_id}>
          <b>{row.source_id}</b> · {LABELS[row.state]}{row.provider && ` · ${row.provider}`}
          <p className="report-meta">{sourceHealthDetails(row)}</p>
          {row.error && <p className="report-meta amber">失败原因：{row.error}</p>}
          {row.state !== "within_threshold" && <button type="button" className="ghost-btn"
            disabled={busy || !report.report_id || !onSupplement}
            onClick={() => onSupplement?.(`请补充核验来源 ${row.source_id}：${LABELS[row.state]}（观测日期 ${row.data_date || "未知"}${row.error ? `，失败原因 ${row.error}` : ""}）。请重新获取可用证据，明确日期与来源，说明对原报告判断的影响；无法获取时保留缺口。`)}
          >准备补充研究 · {row.source_id}</button>}
        </li>)}
      </ul>
      <p className="report-meta">补研入口会准备追问草稿；提交后才调用模型。报告须保存后才能补研。</p>
    </>}
  </div>;
}

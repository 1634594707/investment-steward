/**
 * B3：协同流水线结果块（v23，四角色进度 + 各角色产出），自 ResearchWorkbenchPage 原样搬出。
 */
import { Fragment, useState, type ReactNode } from "react";
import type { CollabRunView, CollabStage, StockReport, ReportScenario, ReportLevel, ReportWatchpoint, ReportValuation, ClaimFinding, ReportConclusion, CategorizedWarning } from "./researchTypes";
import { MarkdownishText, renderInline, DecisionCardPanel, ReportResultTabs, ReportFirstScreen, keyFactHighlights } from "./panels";
import { ExportBar, buildStockReportMarkdown, buildHtmlDocument, markdownToHtml } from "./export";

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="wb-field">
      <span>{label}</span>
      {children}
    </label>
  );
}

/** v23 协同流水线各阶段结果的结构（宽松读取，缺字段如实降级显示）。 */
export interface CollabCheckResult { observations?: string[]; gaps?: string[]; readiness?: string }
export interface CollabReportResult {
  title?: string;
  executive_summary?: string;
  report?: string;
  citations?: string[];
  limitations?: string[];
  confidence?: string;
  revision_notes?: string;
  /* v24 结构化判断（由报告 schema 要求，模型未输出时为空） */
  scenarios?: ReportScenario[];
  levels?: { support?: ReportLevel[]; resistance?: ReportLevel[] };
  watchpoints?: ReportWatchpoint[];
  valuation?: ReportValuation;
  /* v27/v28：claims 覆盖 + 证据支撑链 + 一句话结论（后端 validate_report 归一后的结果） */
  claims?: ClaimFinding[];
  conclusion?: ReportConclusion;
  /* v30：告警四级分组 + 摘要字数（collab 固定 standard，无摘要重写） */
  categorized_warnings?: CategorizedWarning[];
  summary_chars?: number;
}
export interface CollabRiskResult { risks?: string[]; verdict?: string }

/** v23 协同流水线结果块：四角色进度 chips + 各角色产出（严格串行，逐步呈现）。 */
export function CollabResultBlock({
  run,
  busy,
  onResume,
  titlePrefix,
}: {
  run: CollabRunView;
  busy: boolean;
  onResume: () => void;
  titlePrefix?: string;
}) {
  const statusText = (status: string, latencyMs: number | null): string => {
    if (status === "done") return `完成${latencyMs ? `（${(latencyMs / 1000).toFixed(1)}s）` : ""}`;
    if (status === "failed") return "失败";
    if (status === "running") return "执行中…";
    return "等待";
  };
  const stageResult = <T,>(stage: CollabStage): T | null => (stage.result as T | null);
  const check = run.stages[0]?.status === "done" ? stageResult<CollabCheckResult>(run.stages[0]!) : null;
  const draft = run.stages[1]?.status === "done" ? stageResult<CollabReportResult>(run.stages[1]!) : null;
  const risk = run.stages[2]?.status === "done" ? stageResult<CollabRiskResult>(run.stages[2]!) : null;
  const revise = run.stages[3]?.status === "done" ? stageResult<CollabReportResult>(run.stages[3]!) : null;
  const reviseStage = run.stages[3];
  const finalReport: StockReport | null = revise && (revise.report || revise.title) ? {
    ok: true,
    symbol: run.symbol,
    title: revise.title ?? `${run.symbol} 协同研究报告`,
    executive_summary: revise.executive_summary ?? "",
    report: revise.report ?? "",
    citations: revise.citations ?? [],
    limitations: revise.limitations ?? [],
    confidence: revise.confidence ?? "low",
    model: `${Object.values(run.stage_models ?? {}).join("/") || run.model}·协同流水线`,
    source_keys: run.source_keys,
    source_errors: run.source_errors,
    generated_at: run.created_at,
    // v24 质量痕迹挂在「修订」阶段对象上（Core collab 引擎写入），不伪造来源可用性
    citation_dropped: reviseStage?.citation_dropped ?? [],
    quality_warnings: reviseStage?.quality_warnings ?? [],
    report_sections: reviseStage?.report_sections,
    report_chars: reviseStage?.report_chars,
    // v31 质量恢复：三态状态 + 阻断项 + 小节实质状态随修订阶段透传（与单股研报同一口径）
    quality_status: reviseStage?.quality_status,
    quality_status_label: reviseStage?.quality_status_label,
    quality_blockers: reviseStage?.quality_blockers,
    missing_sections: reviseStage?.missing_sections,
    section_states: reviseStage?.section_states,
    report_mode: run.report_mode ?? "standard",
    generation_trace: {
      model: run.model,
      stage_models: run.stage_models,
      stages_completed: run.stages.filter((stage) => stage.status === "done").map((stage) => stage.stage),
      report_mode: run.report_mode ?? "standard",
      source_keys: run.source_keys,
    },
    scenarios: revise.scenarios ?? [],
    levels: revise.levels ?? {},
    watchpoints: revise.watchpoints ?? [],
    valuation: revise.valuation,
    revision_notes: revise.revision_notes,
  } : null;

  return (
    <div className="wb-result">
      <h4>协同流水线{titlePrefix} · {run.symbol} · {run.model} · 角色依次执行</h4>
      <div className="wb-result-meta">
        <span className="soft-tag">{run.symbol}</span>
        <span className="soft-tag blue">{run.model}</span>
        {run.stage_models && Object.keys(run.stage_models).length > 0 && (
          <span className="soft-tag blue" title="角色级模型分配（不同模型执行不同角色，串行）">
            角色分配 {Object.entries(run.stage_models).map(([stageKey, modelName]) => `${stageKey}=${modelName}`).join(" / ")}
          </span>
        )}
        <span className="soft-tag">{run.done ? "已完成" : "进行中"}</span>
        <span className="report-meta">{new Date(run.created_at).toLocaleString()}</span>
      </div>
      {run.source_errors && Object.keys(run.source_errors).length > 0 && (
        <p className="report-meta amber">证据包缺口（四个角色共享同一份，未编造补齐）：{Object.values(run.source_errors).join("；")}</p>
      )}
      <div className="pref-row" role="group" aria-label="协同角色进度">
        {run.stages.map((stage, index) => (
          <button
            key={stage.stage}
            className={`tag-button ${stage.status === "done" ? "active" : ""}`}
            aria-pressed={stage.status === "done"}
            disabled
          >
            {["①", "②", "③", "④"][index]} {stage.label}
            <i style={{ display: "block", marginTop: 2 }}>
              {statusText(stage.status, stage.latency_ms)}
              {run.stage_models?.[stage.stage] ? ` · ${run.stage_models[stage.stage]}` : ""}
            </i>
          </button>
        ))}
      </div>
      {!run.done && !busy && (
        <div className="wb-actions">
          <button className="ghost-btn" onClick={onResume}>继续执行（含失败阶段重试）</button>
        </div>
      )}
      {busy && <p className="report-meta">当前角色执行中…（单次模型调用受方案超时约束，请勿关闭）</p>}
      {run.stages.some((stage) => stage.status === "failed") && (
        <p className="form-error" role="alert">
          {run.stages.filter((stage) => stage.status === "failed").map((stage) => `${stage.label}：${stage.error ?? "原因未知"}`).join("；")}
        </p>
      )}
      {/* v33 A04：核对/初稿/风控原文收纳到「生成过程」，默认折叠——最终稿是唯一阅读入口；失败阶段仍可见可重试。 */}
      {(check || draft || risk) && (
        <details className="wb-collab-process">
          <summary>生成过程（① 数据核对 · ② 首席初稿 · ③ 风险审查 原文，按需展开）</summary>
          {check && (
            <div>
              <span className="answer-block-title">① 数据核对</span>
              {check.observations && check.observations.length > 0 && (
                <ul className="answer-list">{check.observations.map((item, i) => <li key={i}>{item}</li>)}</ul>
              )}
              {check.gaps && check.gaps.length > 0 && (
                <p className="report-meta amber">证据缺口：{check.gaps.join("；")}</p>
              )}
            </div>
          )}
          {draft && (
            <div>
              <span className="answer-block-title">② 首席初稿{draft.confidence ? `（置信 ${draft.confidence}）` : ""}</span>
              {draft.executive_summary && <div className="wb-exec-summary">{draft.executive_summary}</div>}
              <MarkdownishText content={draft.report ?? ""} />
            </div>
          )}
          {risk && (
            <div>
              <span className="answer-block-title">③ 风险审查{risk.verdict ? `（立场：${risk.verdict === "against" ? "反对" : risk.verdict === "partial" ? "部分反对" : "支持"}）` : ""}</span>
              <ul className="answer-list">{(risk.risks ?? []).map((item, i) => <li key={i}>{item}</li>)}</ul>
            </div>
          )}
        </details>
      )}
      {reviseStage?.status === "failed" && (
        <p className="form-error" role="alert">
          {run.stages.find((stage) => stage.status === "failed")?.label}失败：
          {run.stages.find((stage) => stage.status === "failed")?.error}
        </p>
      )}
      {finalReport && (
        <div>
          <span className="answer-block-title">④ 首席修订稿（最终产出，已留档）</span>
          <div className="wb-result-meta">
            <span className="soft-tag">置信 {finalReport.confidence}</span>
          </div>
          {/* v31 §4.1 首屏固定行：模式 · 字数 · 三态质量状态（协同稿与单股研报同一口径） */}
          <ReportFirstScreen report={finalReport} />
          {/* v33 A04/A05：结论卡与执行摘要在「概览」页签内，不再重复堆叠在页签之外 */}
          <ExportBar
            filenameBase={`协同研报-${finalReport.symbol}-${finalReport.generated_at.slice(0, 10)}`}
            markdown={buildStockReportMarkdown(finalReport)}
            htmlDocument={buildHtmlDocument(
              finalReport.title,
              [finalReport.symbol, `模型 ${finalReport.model}`, `置信 ${finalReport.confidence}`, new Date(finalReport.generated_at).toLocaleString()],
              markdownToHtml(buildStockReportMarkdown(finalReport).replace(/^# .*$/m, "").trim(), keyFactHighlights(finalReport)),
            )}
          />
          {/* v33 结果区六页签：概览 / 正文 / 图表 / 情景与验证 / 证据 / 追问 */}
          <ReportResultTabs
            report={finalReport}
            bodyAppend={
              finalReport.limitations.length > 0 ? (
                <div>
                  <span className="answer-block-title">局限与口径声明</span>
                  <ul className="answer-list">{finalReport.limitations.map((item, i) => <li key={i}>{item}</li>)}</ul>
                </div>
              ) : null
            }
          />
          {finalReport.revision_notes && <p className="report-meta">修订说明：{finalReport.revision_notes}</p>}
        </div>
      )}
      <p className="report-meta">研究参考，不构成投资建议；关键判断请回链原始来源复核。</p>
    </div>
  );
}

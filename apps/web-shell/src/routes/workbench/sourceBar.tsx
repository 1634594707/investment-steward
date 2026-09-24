/**
 * D02（前端设计与架构优化任务路线图 2026-09-19）：单股研究的来源条。
 * 交接带入的标的、来源与预填问题此前只在可折叠表单的摘要里出现一次，阅读态下不可见；
 * 本条常驻个股区，并给出「返回来源」——落点是发起交接的视图本身，KeepAlive 驻留使该页的
 * 筛选、选中项与滚动位置原样恢复（AppShell 的 scrollMemoryRef）。
 */
import type { HandoffSourceView, ReportHandoff } from "../../state/handoff";

const SOURCE_LABEL_FALLBACK = "来源页";

export function ReportSourceBar({ handoff, onNavigate }: { handoff: ReportHandoff; onNavigate: (view: HandoffSourceView) => void }) {
  const sourceView = handoff.sourceView;
  if (!sourceView) return null;
  const sourceLabel = handoff.sourceLabel || SOURCE_LABEL_FALLBACK;
  return (
    <div className="wb-sourcebar" data-testid="report-source-bar">
      <span className="wb-sourcebar-kv">
        当前研究对象 <b>{handoff.symbol}</b> · 来自 <b>{sourceLabel}</b>
      </span>
      {handoff.question && (
        <span className="wb-sourcebar-q" title={handoff.question}>已带入研究问题：{handoff.question}</span>
      )}
      <button className="ghost-btn" onClick={() => onNavigate(sourceView)}>返回 {sourceLabel}</button>
    </div>
  );
}

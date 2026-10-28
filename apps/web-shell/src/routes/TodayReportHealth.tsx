import { useEffect, useRef, useState } from "react";
import type { CoreRequest } from "../state/coreClient";
import type { ReportSourceHealth } from "./workbench/researchTypes";
import { SOURCE_HEALTH_LABELS } from "./workbench/sourceHealth";
import { formatTimestamp } from "./workbench/format";

interface Snapshot {
  total: number;
  items: Array<{ report_id: string; title?: string; subject: string; source_health?: ReportSourceHealth }>;
}

export function TodayReportHealth({ coreRequest, active, onOpenReport }: {
  coreRequest: (req: CoreRequest) => Promise<{ status: number; data: unknown }>;
  active: boolean;
  onOpenReport: (id: string) => void;
}) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [readAt, setReadAt] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sequence = useRef(0);
  async function load() {
    const current = ++sequence.current;
    setBusy(true);
    setError(null);
    try {
      const response = await coreRequest({ method: "GET", path: "/ai-research/reports?kind=stock&limit=20&include_source_health=true" });
      const data = response.data as Snapshot & { ok?: boolean };
      if (response.status >= 400 || !data?.ok || !Array.isArray(data.items)) throw new Error("研报健康读取失败，请重试。");
      if (current !== sequence.current) return;
      setSnapshot(data);
      setReadAt(new Date().toISOString());
    } catch {
      if (current === sequence.current) setError("研报健康读取失败，请重试。");
    } finally {
      if (current === sequence.current) setBusy(false);
    }
  }
  useEffect(() => {
    if (active) void load();
    return () => { sequence.current += 1; };
    // coreRequest is the shell's stable client; reload when this kept page becomes active.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);
  return <section className="section-block" aria-label="研报数据健康">
    <div className="section-heading"><h3>研报数据健康</h3><button className="text-button" disabled={busy} onClick={() => void load()}>刷新研报健康</button></div>
    <p className="report-meta">仅检查最近 20 份个股研报的冻结来源，包含草稿；阈值内不代表实时或内容已核实。</p>
    {busy && <p role="status">正在读取研报健康…</p>}
    {error && <p role="alert">{error}</p>}
    {snapshot && <>
      <p className="report-meta">当前展示 {snapshot.items.length}/{snapshot.total} 份 · 读取时间：{formatTimestamp(readAt!)}{(busy || error) && " · 旧快照，当前状态尚未确认"}</p>
      {snapshot.items.length === 0 && <p>尚无个股研报记录。</p>}
      <ul className="answer-list">{snapshot.items.map((item) => {
        const health = item.source_health;
        const gaps = health?.sources.filter((row) => row.state !== "within_threshold") ?? [];
        return <li key={item.report_id}>
          <b>{item.title || item.subject}</b> · {item.subject}
          <p>{!health?.sources.length ? "缺少来源记录，无法核验" : gaps.length
            ? gaps.map((row) => `${row.source_id}：${SOURCE_HEALTH_LABELS[row.state]}`).join("；")
            : "已记录来源的观测日期在阈值内"}</p>
          {health && <p className="report-meta">检查日期：{health.checked_on}</p>}
          <button className="text-btn" onClick={() => onOpenReport(item.report_id)}>查看报告并补研 · {item.subject}</button>
        </li>;
      })}</ul>
    </>}
  </section>;
}

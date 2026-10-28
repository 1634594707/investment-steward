import type { Evidence } from "@investment-steward/domain-contracts";
import { EvidenceRef } from "./EvidenceRef";

export function evidenceReferenceStatus(item: Evidence | undefined, now = Date.now()): string {
  const expires = item?.valid_until ? Date.parse(item.valid_until) : null;
  return !item ? "详情未取得，状态未知"
    : item.status === "retracted" ? "已撤回"
    : item.status === "stale" || (expires !== null && expires < now) ? "已过期"
    : expires !== null && !Number.isFinite(expires) ? "有效期无法识别"
    : "当前未标记失效";
}

export function HistoricalEvidenceRefs({ refs, evidenceOf, onOpenEvidence }: {
  refs: string[];
  evidenceOf: (id: string) => Evidence | undefined;
  onOpenEvidence: (id: string) => void;
}) {
  if (!refs.length) return <span className="answer-muted">无</span>;
  const now = Date.now();
  return <div className="ref-chip-row">{refs.map((id) => {
    const item = evidenceOf(id);
    return <EvidenceRef key={id}
      title={`${id.slice(0, 8)} · ${item?.source_name ?? "未挂载"}`}
      badge={<span className="soft-tag gray">{evidenceReferenceStatus(item, now)}</span>}
      onOpen={() => onOpenEvidence(id)} />;
  })}</div>;
}

import type { BriefItem } from "@investment-steward/domain-contracts";

/** U04: preserve the exact brief item, without deriving instrument identifiers. */
export interface BriefNavigation {
  target: "investment" | "research" | "review";
  item: BriefItem;
}

export function BriefContext({ item, onOpenEvidence, onClose }: {
  item: BriefItem;
  onOpenEvidence: (id: string) => void;
  onClose: () => void;
}) {
  return <section className="section-block" aria-label="简报处理上下文">
    <strong>来自今日简报：{item.title}</strong>
    <p>{item.summary}</p>
    <p>本次处理标的：{item.related_instrument || "未关联标的，请自行选择处理对象"}</p>
    {item.evidence_refs.length ? <div className="notification-actions">
      {Array.from(new Set(item.evidence_refs)).map((id) => <button className="text-btn" key={id}
        onClick={() => onOpenEvidence(id)}>查看依据 {id}</button>)}
    </div> : <p>本条未附证据引用</p>}
    <button className="text-btn" onClick={onClose}>结束本次简报处理</button>
  </section>;
}

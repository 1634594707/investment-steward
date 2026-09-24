interface Props {
  title: string;
  badge?: React.ReactNode;
  onOpen?: () => void;
  muted?: boolean;
}

/** 证据引用：可聚焦按钮，任意页面点击打开证据抽屉（open_evidence 卡片的 UI 载体）。 */
export function EvidenceRef({ title, badge, onOpen, muted }: Props) {
  if (!onOpen) {
    return (
      <h4 className={muted ? "evidence-heading muted" : "evidence-heading"}>
        {title}
        {badge}
      </h4>
    );
  }
  return (
    <button type="button" className="evidence-ref" onClick={onOpen}>
      <span className="evidence-heading">{title}</span>
      {badge}
    </button>
  );
}
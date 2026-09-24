interface Props {
  /** 一句话结论（如「这里还没有内容」）。 */
  title: string;
  /** 一句原因 / 缺什么能力（插槽空态沿用「需要哪个能力才能填充」文案体系）。 */
  reason?: string;
  /** 唯一行动按钮文案。 */
  action?: string;
  onAction?: () => void;
}

/** 统一空态模式（F5-3）：一句原因 + 一个行动；插槽空态不隐藏区块（边界承诺不变）。 */
export function EmptyState({ title, reason, action, onAction }: Props) {
  return (
    <div className="empty-state">
      <strong>{title}</strong>
      {reason && <span>{reason}</span>}
      {action && onAction && (
        <button className="text-button" onClick={onAction}>
          {action} <span>→</span>
        </button>
      )}
    </div>
  );
}

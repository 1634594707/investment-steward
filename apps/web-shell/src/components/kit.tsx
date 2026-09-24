import type { ReactNode } from "react";

interface MetricProps {
  label: string;
  value: string | number;
  note: string;
  tone: string;
}
export function Metric({ label, value, note, tone }: MetricProps) {
  // v6 UI 优化：0 / -- 是「无事发生」的正常态，用中性色，不做彩色假警示
  const neutral = value === 0 || value === "--";
  return (
    <div className={`metric ${neutral ? "neutral" : tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{note}</small>
    </div>
  );
}

interface SectionHeadingProps {
  eyebrow: string;
  title: string;
  action?: string;
  onClick?: () => void;
}
export function SectionHeading({ eyebrow, title, action, onClick }: SectionHeadingProps) {
  return (
    <div className="section-heading">
      <div>
        <span className="section-kicker">{eyebrow}</span>
        <h3>{title}</h3>
      </div>
      {action && (
        <button className="text-button" onClick={onClick}>
          {action} <span>→</span>
        </button>
      )}
    </div>
  );
}

interface ActionItemProps {
  number: string;
  title: string;
  desc: string;
  tag: string;
  color: string;
  /** D01（前端设计与架构优化任务路线图 2026-09-19）：给了 actions 就不要再给 onClick——
   * 条目不再是单一跳转按钮，「查看依据」与「处理事项」各自成钮，名称写明去向。 */
  onClick?: () => void;
  /** D01 第二层：依据条数、数据时间等来源信息。 */
  meta?: ReactNode;
  /** D01：该条目的具名操作按钮。 */
  actions?: ReactNode;
}
export function ActionItem({ number, title, desc, tag, color, onClick, meta, actions }: ActionItemProps) {
  if (actions) {
    return (
      <div className="action-item action-item-split">
        <span className="item-number">{number}</span>
        <span className="item-copy">
          <b>{title}</b>
          <small>{desc}</small>
        </span>
        <span className={`status-tag ${color}`}>{tag}</span>
        <span className="action-item-meta">{meta}</span>
        <span className="action-item-actions">{actions}</span>
      </div>
    );
  }
  return (
    <button className="action-item" onClick={onClick}>
      <span className="item-number">{number}</span>
      <span className="item-copy">
        <b>{title}</b>
        <small>{desc}</small>
      </span>
      <span className={`status-tag ${color}`}>{tag}</span>
      <span className="arrow">↗</span>
    </button>
  );
}
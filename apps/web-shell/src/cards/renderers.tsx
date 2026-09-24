import type { ActionMode } from "@investment-steward/domain-contracts";
import { resolveUiCardRenderer, type UiCardRenderer } from "@investment-steward/ui-card-schemas";
import type { ReactNode } from "react";
import { SourceLine } from "../components/SourceLine";
import { formatDate } from "../state/format";

/**
 * 渲染器库（内核代码，非插件代码）。
 * 同一份 UICard 数据可按 renderer 分级渲染，主题 / i18n / 无障碍 / 证据溯源由内核统一控制。
 * 插件永远不产出 JSX，只产出 UICard JSON；这里是标准的四种密度渲染入口。
 */

/** 渲染器消费的最小数据视图（subset of UICard）。 */
export interface ViewerCard {
  card_id: string;
  title: string;
  summary: string;
  severity?: "info" | "attention" | "warning";
  evidence_refs?: string[];
  source_plugin?: string | null;
  created_at: string;
  valid_until?: string | null;
  action_mode: ActionMode;
  renderer?: UiCardRenderer | null;
  limitations?: string[];
}

const SEVERITY_TAG: Record<string, string> = {
  info: "信息",
  attention: "关注",
  warning: "警示",
};

export function SeverityDot({ severity }: { severity?: string }) {
  return <span className={`q-status ${severity === "warning" ? "red" : severity === "attention" ? "amber" : "mint"}`} />;
}

/** L3 card：完整卡片，带来源行、时效与限制。 */
function CardBody({ card }: { card: ViewerCard }) {
  return (
    <article className="metric-card" data-card-id={card.card_id}>
      <header className="metric-card-head">
        <div className="metric-card-title">
          {card.severity ? <SeverityDot severity={card.severity} /> : null}
          <span className="metric-card-label">{SEVERITY_TAG[card.severity ?? ""] ?? "卡"}</span>
          <h4>{card.title}</h4>
        </div>
        <time className="metric-card-time">{formatDate(card.created_at)}</time>
      </header>
      <p className="metric-card-summary">{card.summary}</p>
      <SourceLine
        sourceName={card.source_plugin ? "插件输出" : "Steward 内核"}
        pluginId={card.source_plugin ?? undefined}
        release={undefined}
        asOf={card.created_at}
      />
      {card.limitations && card.limitations.length > 0 && <p className="chart-limit">{card.limitations[0]}</p>}
    </article>
  );
}

/** L2 summary_row：与 topic 并列的紧凑行，保留来源与时效，省去标题详情折行。 */
function SummaryRowBody({ card }: { card: ViewerCard }) {
  return (
    <div className="summary-row" data-card-id={card.card_id}>
      {card.severity ? <SeverityDot severity={card.severity} /> : null}
      <span className="summary-row-title">{card.title}</span>
      <span className="summary-row-desc">{card.summary}</span>
      <span className="summary-row-meta">{card.source_plugin ?? "steward"}</span>
      <time className="summary-row-meta">{formatDate(card.created_at)}</time>
    </div>
  );
}

/** L1 inline：供卡片/段落内嵌的最短行，只保留标题与一句摘要。 */
function InlineBody({ card }: { card: ViewerCard }) {
  return (
    <span className="inline-note" data-card-id={card.card_id}>
      <SeverityDot severity={card.severity} />
      <b>{card.title}</b>
      <span>{card.summary}</span>
    </span>
  );
}

/** L0 silent：该卡片不占主视图，仅入证据账本；返回 null。 */
function SilentBody(): ReactNode {
  return null;
}

/** 按卡片声明的 renderer 分发到标准渲染器。未知/缺省由契约回退到 card。 */
export function renderCard(card: ViewerCard): ReactNode {
  switch (resolveUiCardRenderer(card.renderer)) {
    case "summary_row":
      return <SummaryRowBody card={card} />;
    case "inline":
      return <InlineBody card={card} />;
    case "silent":
      return <SilentBody />;
    case "card":
    default:
      return <CardBody card={card} />;
  }
}

/** 渲染器契约测试：同一说明在各级渲染器下都能产出（供单元测试与走查复用）。 */
export const RENDERER_LIBRARY: { renderer: UiCardRenderer; label: string }[] = [
  { renderer: "card", label: "完整卡" },
  { renderer: "summary_row", label: "摘要行" },
  { renderer: "inline", label: "内联" },
  { renderer: "silent", label: "静默" },
];

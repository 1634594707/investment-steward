import type { ActionMode } from "@investment-steward/domain-contracts";

export type CardSeverity = "info" | "attention" | "warning";

/** 渲染原语：基座按插槽级别仲裁后的呈现方式（L3 card / L2 summary_row / L1 inline / L0 silent） */
export type UiCardRenderer = "card" | "summary_row" | "inline" | "silent";

/** 渲染器契约来源：内核按此清单实现对应密度渲染器，插件只声明 renderer 值。 */
export const UI_CARD_RENDERERS = ["card", "summary_row", "inline", "silent"] as const;

/** 渲染器契约测试：未知/缺省渲染器回退到 card，保证任何 payload 都能被渲染。 */
export function resolveUiCardRenderer(renderer: UiCardRenderer | null | undefined): UiCardRenderer {
  return renderer && (UI_CARD_RENDERERS as readonly string[]).includes(renderer) ? renderer : "card";
}

export interface UICard {
  card_id: string;
  title: string;
  summary: string;
  severity: CardSeverity;
  evidence_refs: string[];
  source_plugin?: string | null;
  /** 输出目标插槽 id（如 "today.brief"）；null = 不占主视图，仅入证据账本 */
  slot?: string | null;
  renderer?: UiCardRenderer | null;
  created_at: string;
  valid_until?: string | null;
  action_mode: ActionMode;
  supported_actions: Array<"open_evidence" | "create_plan" | "start_learning" | "review_policy">;
  limitations: string[];
}

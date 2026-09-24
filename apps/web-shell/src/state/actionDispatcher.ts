import type { ActionMode, Evidence } from "@investment-steward/domain-contracts";

/** 动作白名单四值（与 UICard.supported_actions / 内核枚举一致）。插件不得自行执行，只声明，由内核呼应。 */
export type CardAction = "open_evidence" | "create_plan" | "start_learning" | "review_policy";

/** 内核仲裁所需的卡片上下文（subset of UICard，避免前端直接依赖 ui-card-schemas 包）。 */
export interface CardActionContext {
  card_id: string;
  title: string;
  evidence_refs: string[];
  action_mode: ActionMode;
  supported_actions: CardAction[];
}

export const CARD_ACTIONS: CardAction[] = [
  "open_evidence",
  "create_plan",
  "start_learning",
  "review_policy",
];

export interface KernelHandlers {
  /** open_evidence：打开证据抽屉，只能读内核提供的 Evidence */
  openEvidence: (evidenceId: string) => void;
  /** create_plan：需显式确认后创建计划（写入内核对象） */
  createPlan: (options: { title: string; evidenceRefs: string[] }) => void;
  /** start_learning：拉取学习单元 */
  startLearning: (actionMode: ActionMode) => void;
  /** review_policy：进入原则复核 */
  reviewPolicy: (actionMode: ActionMode) => void;
}

export interface DispatchResult {
  action: CardAction;
  handled: boolean;
  reason?: string;
  /** 写入审计的事件描述（真实审计落 Core；前端先记账用于演示与走查） */
  audit: { action: string; resource: string };
}

const ACTION_AUDIT: Record<CardAction, string> = {
  open_evidence: "card.open_evidence",
  create_plan: "card.create_plan",
  start_learning: "card.start_learning",
  review_policy: "card.review_policy",
};

/**
 * 内核动作处理器：动作只能操作内核对象，插件仅声明 supported_actions。
 * 白名单来自卡片自身声明，避免插件要求未声明动作。
 */
export function dispatchCardAction(
  card: CardActionContext,
  action: CardAction,
  kernel: KernelHandlers,
  evidence: Evidence[],
): DispatchResult {
  if (!card.supported_actions.includes(action)) {
    return {
      action,
      handled: false,
      reason: "插件未声明该动作，内核拒绝执行",
      audit: { action: ACTION_AUDIT[action], resource: `blocked:${card.card_id}` },
    };
  }

  switch (action) {
    case "open_evidence": {
      const firstRef = card.evidence_refs[0];
      const target = firstRef ? evidence.find((item) => item.evidence_id === firstRef) : undefined;
      if (!target) {
        return { action, handled: false, reason: "无证据可展开", audit: { action: ACTION_AUDIT[action], resource: card.card_id } };
      }
      kernel.openEvidence(target.evidence_id);
      break;
    }
    case "create_plan": {
      kernel.createPlan({ title: card.title, evidenceRefs: card.evidence_refs ?? [] });
      break;
    }
    case "start_learning":
      kernel.startLearning(card.action_mode);
      break;
    case "review_policy":
      kernel.reviewPolicy(card.action_mode);
      break;
  }
  return { action, handled: true, audit: { action: ACTION_AUDIT[action], resource: card.card_id } };
}

/** 生成一行待确认动作描述（create_plan / review_policy 需显式确认）。 */
export function needsConfirmation(action: CardAction): boolean {
  return action === "create_plan" || action === "review_policy";
}

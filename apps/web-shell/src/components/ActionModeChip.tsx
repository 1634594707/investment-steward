import type { ActionMode } from "@investment-steward/domain-contracts";

const MAP: Record<ActionMode, { label: string; tone: string }> = {
  observe: { label: "观察", tone: "mint" },
  research: { label: "研究", tone: "amber" },
  review_plan: { label: "复核计划", tone: "blue" },
  no_action: { label: "暂不行动", tone: "gray" },
};

interface Props {
  mode: ActionMode;
}

/** 四种行动模式徽章，全站唯一实现，文字 + 颜色双编码。 */
export function ActionModeChip({ mode }: Props) {
  const item = MAP[mode];
  return <span className={`status-tag ${item.tone}`}>{item.label}</span>;
}
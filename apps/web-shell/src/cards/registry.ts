import type { PluginUiSlotLevel } from "@investment-steward/domain-contracts";

export type SlotId = "invest.market_view" | "today.brief" | "today.learning";

export interface SlotMeta {
  id: SlotId;
  level: PluginUiSlotLevel;
  label: string;
  required_capability: string;
  empty_hint: string;
}

/**
 * 插槽注册表（内核代码，非插件代码）。
 * 页面只声明插槽 id；某个插槽最终由哪个插件产出、渲染成什么卡片，都由内核仲裁与分发。
 * 元素级排序 / 空态提示属于内核，插件只声明「我要注入这个插槽」。
 */
export const SLOTS: Record<SlotId, SlotMeta> = {
  "invest.market_view": {
    id: "invest.market_view",
    level: "L3",
    label: "行情视图",
    required_capability: "read_market_data",
    empty_hint: "启用「中国市场行情」插件后，这里会渲染结构化 OHLCV K 线（带来源行与限制说明）。",
  },
  "today.brief": {
    id: "today.brief",
    level: "L3",
    label: "行动简报",
    required_capability: "run_analysis",
    empty_hint: "启用「组合健康检查」插件后，这里会出现与你的持仓相关的行动卡。",
  },
  "today.learning": {
    id: "today.learning",
    level: "L2",
    label: "今日学习",
    required_capability: "submit_learning_unit",
    empty_hint: "启用学习插件后，这里会推荐一个与你的投资原则相关的练习。",
  },
};

export function getSlotMeta(id: string): SlotMeta | undefined {
  return SLOTS[id as SlotId];
}
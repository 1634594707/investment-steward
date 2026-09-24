export type AppView = "today" | "investment" | "workbench" | "research" | "review" | "quant" | "library" | "macro" | "tactics" | "youzi" | "extensions" | "settings";

export interface NavEntry {
  id: AppView;
  label: string;
  hint: string;
}

/**
 * 桌面壳层级（v3）：
 *   第一层 TitleBar     —— 窗口层（拖拽 / 品牌 / Core 状态 / 窗口三键）
 *   第二层 ActivityBar  —— 工作区层（唯一一级导航，切换工作区）
 *   第三层 Sidebar      —— 工作区内导航（随工作区变化）+ 该工作区通用块
 * 页面路由（AppView）不变，只重构壳层；业务页面零改动。
 */

/** 业务一级导航：投资管家工作区内的页面。 */
export const PRIMARY_NAV: NavEntry[] = [
  { id: "today", label: "今天", hint: "组合与计划" },
  { id: "investment", label: "我的投资", hint: "原则与逻辑" },
  { id: "workbench", label: "研究工作台", hint: "方向研判 · 标的池 · 个股研报" },
  { id: "research", label: "研究", hint: "证据与问题" },
  { id: "review", label: "复盘", hint: "决定与学习" },
];

/** 专业模式页面：随 proMode 出现在投资管家工作区侧栏。 */
export const PRO_NAV: NavEntry[] = [
  { id: "quant", label: "量化研究", hint: "分享池 · 因子 · 回测" },
];

/** 独立应用区：mount=own_page 插件的功能页入口（保留供插件计数等复用）。 */
export const APPS_NAV: NavEntry[] = [
  { id: "library", label: "研读图书馆", hint: "独立应用 · 研读插件" },
  { id: "macro", label: "宏观雷达", hint: "独立应用 · 宏观插件" },
  { id: "tactics", label: "战法雷达", hint: "独立应用 · 战法插件" },
  { id: "youzi", label: "游资雷达", hint: "独立应用 · 席位证据插件" },
];

/** 设置工作区内的页面。 */
export const SETTINGS_NAV: NavEntry[] = [
  { id: "settings", label: "设置", hint: "扩展 · 密钥 · 隐私 · 关于" },
  { id: "extensions", label: "扩展管理", hint: "插件目录 · 插槽 · 审计" },
];

/** 全量视图（TopBar 标题等按 id 查找用）。 */
export const NAV_ALL: NavEntry[] = [
  ...PRIMARY_NAV,
  ...PRO_NAV,
  ...APPS_NAV,
  ...SETTINGS_NAV,
];

export const DEFAULT_VIEW: AppView = "today";

/* ---------- 工作区（壳层第一级） ---------- */

export type WorkspaceId = "steward" | "macro" | "library" | "tactics" | "youzi" | "settings";

export interface WorkspaceDef {
  id: WorkspaceId;
  label: string;
  hint: string;
  /** 插件工作区：对应插件未启用时入口隐藏。 */
  pluginId?: string;
}

/** ActivityBar 主列表（设置固定在 ActivityBar 底部，不入主列表）。 */
export const WORKSPACES: WorkspaceDef[] = [
  { id: "steward", label: "投资管家", hint: "今天 · 组合 · 研究 · 复盘" },
  { id: "macro", label: "宏观雷达", hint: "全球四国 · 结构对照 · 影响链", pluginId: "official.macro-radar" },
  { id: "library", label: "研读图书馆", hint: "书目 · 研读计划 · 认知档案", pluginId: "official.reading-library" },
  { id: "tactics", label: "战法雷达", hint: "战法扫描 · 观察 · 信号标注", pluginId: "official.stock-tactics" },
  { id: "youzi", label: "游资雷达", hint: "今日席位 · 席位档案 · 学习", pluginId: "official.youzi-radar" },
];

export const SETTINGS_WORKSPACE: WorkspaceDef = {
  id: "settings",
  label: "系统设置",
  hint: "设置 · 扩展管理",
};

export function workspaceOfView(view: AppView): WorkspaceId {
  if (view === "macro") return "macro";
  if (view === "library") return "library";
  if (view === "tactics") return "tactics";
  if (view === "youzi") return "youzi";
  if (view === "settings" || view === "extensions") return "settings";
  return "steward";
}

export function workspaceDefaultView(ws: WorkspaceId): AppView {
  if (ws === "macro") return "macro";
  if (ws === "library") return "library";
  if (ws === "tactics") return "tactics";
  if (ws === "youzi") return "youzi";
  if (ws === "settings") return "settings";
  return "today";
}

/** 工作区内的页面导航（侧栏第二层）。 */
export function workspaceViews(ws: WorkspaceId, proMode: boolean): NavEntry[] {
  if (ws === "steward") return proMode ? [...PRIMARY_NAV, ...PRO_NAV] : [...PRIMARY_NAV];
  if (ws === "macro") return [{ id: "macro", label: "宏观雷达", hint: "全球四国 · 结构对照 · 影响链" }];
  if (ws === "library") return [{ id: "library", label: "研读图书馆", hint: "书架 · 计划 · 洞察" }];
  if (ws === "tactics") return [{ id: "tactics", label: "战法雷达", hint: "扫描 · 观察 · 信号" }];
  if (ws === "youzi") return [{ id: "youzi", label: "游资雷达", hint: "今日席位 · 档案 · 指数席位 · 学习" }];
  return SETTINGS_NAV;
}

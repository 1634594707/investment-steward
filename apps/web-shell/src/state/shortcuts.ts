import { APPS_NAV, NAV_ALL, PRIMARY_NAV, WORKSPACES, workspaceOfView, type AppView } from "../shell/nav";

/** 快捷键唯一登记表（F1-2）：速查面板（?）与帮助菜单共用这一份，禁止别处再散写。 */
export interface ShortcutDef {
  keys: string;
  /** 拆分给 <kbd> 的键位（空格分隔组合、"/" 分隔可选项）。 */
  action: string;
  scope: string;
}

/**
 * B03（桌面端升级路线图 2026-09-18）：Ctrl+数字 的落点表**由导航派生**，不再手写数组。
 * 1..PRIMARY_NAV.length 是一级入口，其后接独立应用区（APPS_NAV）；
 * 手写映射在「研究工作台」插入 PRIMARY_NAV 之后就与侧栏错位过。
 */
export const DIGIT_NAV_VIEWS: AppView[] = [...PRIMARY_NAV, ...APPS_NAV].map((entry) => entry.id);

/** 该视图是否绑定插件（未启用插件时按数字跳过去应当无效）。 */
export function viewPluginId(view: AppView): string | null {
  const workspaceId = workspaceOfView(view);
  return WORKSPACES.find((workspace) => workspace.id === workspaceId)?.pluginId ?? null;
}

export interface NavReachability {
  enabledPluginIds: ReadonlySet<string>;
  proMode: boolean;
}

/** 数字键 → 视图：越界、插件未启用、专业模式未开的 quant 一律返回 null（不跳错页）。 */
export function viewForDigit(digit: number, reachability: NavReachability): AppView | null {
  const view = DIGIT_NAV_VIEWS[digit - 1] ?? null;
  if (!view) return null;
  if (view === "quant") return reachability.proMode ? view : null;
  const pluginId = viewPluginId(view);
  if (pluginId && !reachability.enabledPluginIds.has(pluginId)) return null;
  return view;
}

const navLabelOf = (view: AppView): string => NAV_ALL.find((entry) => entry.id === view)?.label ?? view;

/** 速查面板里的导航行：与 DIGIT_NAV_VIEWS 同源生成，文案不会跑在映射前面。 */
export function navShortcutRows(): ShortcutDef[] {
  return DIGIT_NAV_VIEWS.map((view, index) => ({
    keys: `Ctrl ${index + 1}`,
    action: `前往：${navLabelOf(view)}${viewPluginId(view) ? "（需对应插件启用）" : ""}`,
    scope: "全局",
  }));
}

export const STATIC_SHORTCUTS: ShortcutDef[] = [
  { keys: "Ctrl K", action: "打开命令面板：跳页、切持仓、开插件页、执行动作", scope: "全局" },
  { keys: "?", action: "快捷键速查（本面板）", scope: "全局（输入框外）" },
  { keys: "Esc", action: "逐层关闭：命令面板 → 对话框 → 抽屉", scope: "全局" },
  { keys: "Alt 1 / 2 / 3", action: "K 线周期：日 K / 周 K / 月 K", scope: "我的投资" },
  { keys: "Ctrl = / Ctrl - / Ctrl 0", action: "放大 / 缩小 / 重置缩放", scope: "桌面端（Host 菜单）" },
];

export const SHORTCUTS: ShortcutDef[] = [...navShortcutRows(), ...STATIC_SHORTCUTS];

export const SHORTCUTS_EVENT = "steward:show-shortcuts";

export function requestShortcutsHelp(): void {
  window.dispatchEvent(new CustomEvent(SHORTCUTS_EVENT));
}

/** 输入类焦点下不响应单键快捷键（?、Alt+数字），Ctrl 组合与 Esc 例外。 */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}

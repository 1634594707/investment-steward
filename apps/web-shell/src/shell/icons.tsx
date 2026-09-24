/** R5 图标系统：统一走 lucide-react（MIT，stroke 1.6，17px 导航 / 13px 标题栏），与 Linear/VS Code 扩展生态同源。 */
import {
  BookOpen,
  Building2,
  CalendarCheck2,
  FileSearch,
  FlaskConical,
  Globe2,
  History,
  Layers,
  LayoutDashboard,
  Menu,
  Minus,
  Puzzle,
  Radar,
  Search,
  RefreshCw,
  Settings,
  SlidersHorizontal,
  Square,
  X,
  type LucideIcon,
} from "lucide-react";
import type { ReactElement } from "react";

export interface IconProps {
  className?: string;
}

function wrap(Icon: LucideIcon, baseClass: string, size: number) {
  return function LucideWrapped({ className }: IconProps): ReactElement {
    return <Icon className={className ? `${baseClass} ${className}` : baseClass} size={size} strokeWidth={1.6} aria-hidden />;
  };
}

export const IconToday = wrap(CalendarCheck2, "nav-ico", 17);
export const IconInvestment = wrap(Layers, "nav-ico", 17);
export const IconWorkbench = wrap(FlaskConical, "nav-ico", 17);
export const IconResearch = wrap(FileSearch, "nav-ico", 17);
export const IconReview = wrap(History, "nav-ico", 17);
export const IconQuant = wrap(LayoutDashboard, "nav-ico", 17);
export const IconLibrary = wrap(BookOpen, "nav-ico", 17);
export const IconMacro = wrap(Globe2, "nav-ico", 17);
export const IconTactics = wrap(Radar, "nav-ico", 17);
// P3-D01：游资雷达。席位=营业部，故用 Building2（与战法雷达的 Radar 区分开，
// 避免两个「雷达」用同一个图标让人分不清入口）。
export const IconYouzi = wrap(Building2, "nav-ico", 17);
export const IconExtensions = wrap(Puzzle, "nav-ico", 17);
export const IconSettings = wrap(Settings, "nav-ico", 17);
export const IconPro = wrap(SlidersHorizontal, "pro-ico", 17);

/** 标题栏控件图标（13px / stroke 1.6，类名 tb-ico）：侧栏折叠 + 窗口三键。 */
export const IconMenu = wrap(Menu, "tb-ico", 13);
export const IconWinMin = wrap(Minus, "tb-ico", 13);
export const IconWinMax = wrap(Square, "tb-ico", 13);
export const IconWinClose = wrap(X, "tb-ico", 13);
export const IconSearch = wrap(Search, "action-ico", 16);
export const IconRefresh = wrap(RefreshCw, "action-ico", 16);

export function BrandMark({ className = "" }: IconProps) {
  return <span className={`signal-mark ${className}`} aria-hidden="true"><i /><i /><i /><b /></span>;
}

/** 视图 → 图标映射（侧栏 / 收窄态图标列共用）。 */
export const NAV_ICONS: Record<string, (props: IconProps) => ReactElement> = {
  today: IconToday,
  investment: IconInvestment,
  workbench: IconWorkbench,
  research: IconResearch,
  review: IconReview,
  quant: IconQuant,
  library: IconLibrary,
  macro: IconMacro,
  tactics: IconTactics,
  extensions: IconExtensions,
  settings: IconSettings,
};

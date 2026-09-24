import type { PluginCatalogEntry } from "@investment-steward/domain-contracts";
import { SETTINGS_WORKSPACE, WORKSPACES, type WorkspaceDef, type WorkspaceId } from "./nav";
import { BrandMark, IconInvestment, IconLibrary, IconMacro, IconSettings, IconTactics, IconYouzi } from "./icons";
import type { ReactElement } from "react";

interface Props {
  active: WorkspaceId;
  onSelect: (ws: WorkspaceDef) => void;
  /** 插件工作区（mount=own_page）只在插件启用时显示入口。 */
  plugins: PluginCatalogEntry[];
}

/** 工作区图标表与可见性规则：窄窗口时 Sidebar 收窄态复用（单列合并导航）。 */
export const WS_ICONS: Record<WorkspaceId, (props: { className?: string }) => ReactElement> = {
  steward: IconInvestment,
  macro: IconMacro,
  library: IconLibrary,
  tactics: IconTactics,
  youzi: IconYouzi,
  settings: IconSettings,
};

export function workspaceVisible(ws: WorkspaceDef, plugins: PluginCatalogEntry[]): boolean {
  if (!ws.pluginId) return true;
  return plugins.some((entry) => entry.manifest.plugin_id === ws.pluginId && entry.installation?.state === "enabled");
}

/** 活动栏（壳层第一级导航）：只做工作区切换，不承载页面级导航。 */
export function ActivityBar({ active, onSelect, plugins }: Props) {
  const items = WORKSPACES.filter((ws) => workspaceVisible(ws, plugins));
  return (
    <aside className="activity-bar" aria-label="工作区切换">
      <BrandMark className="ab-brand" />
      <nav className="ab-nav" aria-label="工作区">
        {items.map((ws) => {
          const Icon = WS_ICONS[ws.id];
          return (
            <button
              key={ws.id}
              className={`ab-btn ${active === ws.id ? "active" : ""}`}
              title={ws.label}
              aria-label={`切换到工作区：${ws.label}`}
              aria-current={active === ws.id ? "true" : undefined}
              onClick={() => onSelect(ws)}
            >
              <Icon className="ab-ico" />
              <span className="ab-tag" aria-hidden="true">{ws.label}</span>
            </button>
          );
        })}
      </nav>
      <div className="ab-bottom">
        <button
          className={`ab-btn ${active === "settings" ? "active" : ""}`}
          title={SETTINGS_WORKSPACE.label}
          aria-current={active === "settings" ? "true" : undefined}
          aria-label={`切换到工作区：${SETTINGS_WORKSPACE.label}`}
          onClick={() => onSelect(SETTINGS_WORKSPACE)}
        >
          <IconSettings className="ab-ico" />
        </button>
      </div>
    </aside>
  );
}

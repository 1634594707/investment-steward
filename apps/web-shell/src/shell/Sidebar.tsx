import type { ReactElement } from "react";
import type { PluginCatalogEntry } from "@investment-steward/domain-contracts";
import type { InvestmentPolicyVersion } from "@investment-steward/domain-contracts";
import { SETTINGS_WORKSPACE, WORKSPACES, workspaceViews, type AppView, type NavEntry, type WorkspaceDef, type WorkspaceId } from "./nav";
import { NAV_ICONS, IconPro, IconSettings } from "./icons";
import type { CoreConnection } from "../state/coreClient";
import { useProfile } from "./profile";
import { WS_ICONS as AB_WS_ICONS, workspaceVisible } from "./ActivityBar";

interface Props {
  workspace: WorkspaceDef;
  view: AppView;
  onNavigate: (view: AppView) => void;
  connection: CoreConnection;
  policy: InvestmentPolicyVersion | null;
  proMode: boolean;
  onToggleProMode: () => void;
  /** 窄窗口单列合并：收窄态下在侧栏顶部渲染工作区切换（替代独立的 ActivityBar）。 */
  activeWorkspaceId: WorkspaceId;
  onWorkspaceSelect: (ws: WorkspaceDef) => void;
  plugins: PluginCatalogEntry[];
}

function NavItem({ entry, selected, onClick }: { entry: NavEntry; selected: boolean; onClick: () => void }) {
  const Icon = NAV_ICONS[entry.id];
  return (
    <button className={`nav-item ${selected ? "selected" : ""}`} aria-current={selected ? "page" : undefined} aria-label={entry.label} onClick={onClick} title={`${entry.label} — ${entry.hint}`}>
      {Icon && <Icon />}
      <span className="nav-copy">
        <b>{entry.label}</b>
        <small>{entry.hint}</small>
      </span>
    </button>
  );
}

/**
 * 工作区面板（壳层第二级导航）：显示当前工作区头 + 该工作区内的页面导航。
 * 底部通用块中，「专业模式」「投资档案」仅属于投资管家工作区。
 */
export function Sidebar({ workspace, view, onNavigate, connection, policy, proMode, onToggleProMode, activeWorkspaceId, onWorkspaceSelect, plugins }: Props) {
  const HeadIcon = AB_WS_ICONS[workspace.id];
  const entries = workspaceViews(workspace.id, proMode);
  // v6.5：「设置」页不再进导航（底部/活动栏齿轮就是它的入口），导航只留「扩展管理」——消除与列底齿轮的重复。
  const navEntries = entries.filter((entry) => entry.id !== "settings");
  const isSteward = workspace.id === "steward";
  // 单页工作区（宏观雷达 / 研读图书馆）：工作区头已完整描述唯一页面，不再重复渲染同名单页导航。
  const showNav = navEntries.length > 0;
  // v6.4 规划：顶部工作区组只放内容工作区；「设置」固定到侧栏底部（列底齿轮，通用惯例），
  // 避免与设置工作区自己的「设置」页导航图标相邻重复。
  const wsItems = WORKSPACES.filter((ws) => workspaceVisible(ws, plugins));
  return (
    <aside className="sidebar">
      {/* 窄窗口单列合并（body.rail-collapsed 时显示）：工作区切换并入侧栏顶部，替代独立 ActivityBar */}
      <div className="ws-mini" aria-label="工作区切换">
        {wsItems.map((ws) => {
          const MiniIcon = AB_WS_ICONS[ws.id];
          return (
            <button
              key={ws.id}
              className={`ab-btn ${activeWorkspaceId === ws.id ? "active" : ""}`}
              title={ws.label}
              aria-label={`切换到工作区：${ws.label}`}
              aria-current={activeWorkspaceId === ws.id ? "true" : undefined}
              onClick={() => onWorkspaceSelect(ws)}
            >
              <MiniIcon className="ab-ico" />
            </button>
          );
        })}
      </div>
      <div className="ws-head" aria-label={`当前工作区：${workspace.label}`}>
        {HeadIcon && <HeadIcon className="ws-head-ico" />}
        <div>
          <strong>{workspace.label}</strong>
          <span>{workspace.hint}</span>
        </div>
      </div>

      {showNav && (
        <nav className="nav" aria-label={`${workspace.label}页面`}>
          <span className="side-label">{isSteward ? "核心工作" : "工作区"}</span>
          {navEntries.map((entry) => (
            <NavItem key={entry.id} entry={entry} selected={view === entry.id} onClick={() => onNavigate(entry.id)} />
          ))}
        </nav>
      )}

      {/* v6.6：独立应用入口已删除——宏观雷达 / 研读图书馆在 ActivityBar / ws-mini 顶部有同名工作区图标，
          侧栏再渲染一组同图标入口属双重重复（用户反馈：上面有了，下面删掉）。 */}

      <div className="side-spacer" />

      {isSteward && (
        <div className="side-block">
          <button className="pro-toggle" aria-label="专业模式" title={proMode ? "关闭专业模式" : "开启专业模式"} aria-pressed={proMode} onClick={onToggleProMode}>
            <span><b>专业模式</b><small>{proMode ? "开启 · 量化研究可见" : "关闭 · 量化研究已隐藏"}</small></span>
            <IconPro className="pro-ico" />
            <span className={`switch ${proMode ? "on" : ""}`} aria-hidden="true" />
          </button>
        </div>
      )}

      {/* v6 UI 优化：连接状态移除（TitleBar 与 StatusBar 已各有一处，三重冗余）；侧栏底部只留专业模式 + 档案 chip */}

      {isSteward && <ProfileChip policyVersion={policy?.version ?? null} policyActive={policy?.status === "active"} onNavigate={onNavigate} />}

      {/* v6.4：设置工作区固定列底（仅收起态显示；展开态由 ActivityBar 底部齿轮承担同职责） */}
      <div className="side-bottom">
        <button
          className={`ab-btn ${activeWorkspaceId === SETTINGS_WORKSPACE.id ? "current" : ""}`}
          title={SETTINGS_WORKSPACE.label}
          aria-label={`切换到工作区：${SETTINGS_WORKSPACE.label}`}
          aria-current={activeWorkspaceId === SETTINGS_WORKSPACE.id ? "true" : undefined}
          onClick={() => onWorkspaceSelect(SETTINGS_WORKSPACE)}
        >
          <IconSettings className="ab-ico" />
        </button>
      </div>
    </aside>
  );
}

/** 「我的投资档案」chip：头像/名字与 TopBar 个人中心实时同步（useProfile 共享本机资料）。 */
function ProfileChip({ policyVersion, policyActive, onNavigate }: { policyVersion: number | null; policyActive: boolean; onNavigate: (view: AppView) => void }) {
  const profile = useProfile();
  return (
    <button className="profile-chip" onClick={() => onNavigate("investment")}>
      <span className="avatar" style={profile.avatar ? { padding: 0, overflow: "hidden" } : { background: profile.color, color: "#0b1018" }}>
        {profile.avatar
          ? <img src={profile.avatar} alt="" style={{ width: "100%", height: "100%", borderRadius: "50%", objectFit: "cover", display: "block" }} />
          : profile.name.slice(0, 1)}
      </span>
      <span>
        <b>我的投资档案</b>
        <small>原则 v{policyVersion ?? "—"}{policyActive ? " · 已确认" : ""}</small>
      </span>
      <span className="chev">›</span>
    </button>
  );
}

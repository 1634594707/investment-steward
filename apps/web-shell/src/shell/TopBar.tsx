import { NotificationBell } from "../components/NotificationBell";
import { IconSearch, IconRefresh } from "./icons";
import type { Notification, NotificationTriageReport, PersonalSettings } from "@investment-steward/domain-contracts";
import type { NavEntry } from "./nav";
import { useProfile } from "./profile";

interface Props {
  entry: NavEntry;
  /** R1-4：数据时效由状态栏承担，顶栏只保留动作。 */
  isDemo: boolean;
  /** 打开命令面板（搜索按钮，Ctrl+K）。 */
  onOpenPalette: () => void;
  /** 手动刷新：重拉全部数据（不重置用户当前查看的标的）。 */
  onRefresh: () => void;
  refreshing: boolean;
  /** 待处理通知（GET /notifications/pending，未读）：通知铃面板数据源。 */
  notifications: Notification[];
  onMarkNotificationRead: (notificationId: string) => Promise<boolean>;
  /** JV08：分诊报告（「已分诊未通知」）；`null` = 还没拉过。 */
  notificationTriage?: NotificationTriageReport | null;
  /** 展开通知面板时拉一次分诊报告。 */
  onLoadNotificationTriage?: () => Promise<void>;
  /** 查看全部通知：跳转今日页。 */
  onOpenToday: () => void;
  /** 个人中心设置（Core）：优先于本机镜像；演示模式为 null。 */
  personalSettings: PersonalSettings | null;
  /** 头像点击 → 跳转设置页「个人中心」分区（编辑能力已从顶栏弹窗迁移至此）。 */
  onOpenPersonalCenter: () => void;
}

/** R1-4 单行顶栏（48px）：左=页头（16px 标题+提示），右=搜索(Ctrl+K)/刷新/通知/头像。 */
export function TopBar({ entry, isDemo, onOpenPalette, onRefresh, refreshing, notifications, onMarkNotificationRead, onOpenToday, notificationTriage, onLoadNotificationTriage, personalSettings, onOpenPersonalCenter }: Props) {
  // 本机镜像兜底：Core 未返回设置（首次使用/演示模式）时头像仍有名字与底色。
  const localProfile = useProfile();
  const name = personalSettings?.display_name ?? localProfile.name;
  const color = personalSettings?.avatar_color ?? localProfile.color;
  const avatar = personalSettings?.avatar_data ?? localProfile.avatar;

  return (
    <header className="topbar">
      <div className="page-head-row">
        <h1>{entry.label}</h1>
        <span className="head-hint">{entry.hint}</span>
        {isDemo && <span className="soft-tag gray">演示模式</span>}
      </div>
      <div className="top-actions">
        <button className="topbar-search" onClick={onOpenPalette} title="命令面板（Ctrl+K）" aria-label="搜索命令（Ctrl+K）">
          <IconSearch />
          <span>搜索</span>
        </button>
        <button
          className="icon-button"
          aria-label="刷新数据"
          title="刷新全部数据"
          onClick={onRefresh}
          disabled={refreshing}
        >
          <IconRefresh className={refreshing ? "spin" : ""} />
        </button>
        <NotificationBell notifications={notifications} onMarkNotificationRead={onMarkNotificationRead} onOpenToday={onOpenToday} triage={notificationTriage} onLoadTriage={onLoadNotificationTriage} />
        <button
          className="avatar avatar-top"
          style={avatar ? undefined : { background: color, color: "#0b1018" }}
          aria-label="个人中心"
          title={`个人中心 · ${name}`}
          onClick={onOpenPersonalCenter}
        >
          {avatar ? <img src={avatar} alt="" style={{ width: "100%", height: "100%", borderRadius: "50%", objectFit: "cover" }} /> : name.slice(0, 1)}
        </button>
      </div>
    </header>
  );
}

import { useEffect, useRef, useState } from "react";
import type {
  Notification,
  NotificationTriageReport,
  PersonalNotifyPrefs,
} from "@investment-steward/domain-contracts";
import type { CoreClient } from "../state/coreClient";
import { isDocumentVisible, useCorePoll } from "../state/useCoreQuery";

/** HH:MM → 当日分钟数。非法输入返回 null（宁可当作「不在免打扰时段」，不静默吞掉提醒）。 */
export function toMinutes(hhmm: string): number | null {
  const match = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(String(hhmm ?? ""));
  if (!match) return null;
  return Number(match[1]) * 60 + Number(match[2]);
}

/** 免打扰时段是否命中当前本地时间。跨零点（如 22:00→08:00）按两段处理。 */
/**
 * 第四轮审计（api-5）：`GET /notifications/pending` 的响应形状。
 * 包装成对象是为了把「被静音」与「确实没有」分开——返回裸 `[]` 时前端无从分辨。
 */
export interface NotificationPendingPage {
  items: Notification[];
  /** 是否因个人偏好（免打扰 / 关闭站内提醒）而静音。 */
  muted: boolean;
  /** 静音原因（可展示的中文说明）。 */
  mute_reason: string;
  /** 被静音挡下的未读条数——让用户知道「有 N 条」而不是「没有」。 */
  hidden_count: number;
}

/** 静音态的对外呈现，供通知铃显示「免打扰中」而非「暂无待处理通知」。 */
export interface NotificationMuteState {
  muted: boolean;
  reason: string;
  hiddenCount: number;
}

const UNMUTED: NotificationMuteState = { muted: false, reason: "", hiddenCount: 0 };

export function isQuietHoursNow(prefs: Pick<PersonalNotifyPrefs, "quiet_hours_enabled" | "quiet_start" | "quiet_end">, now: Date = new Date()): boolean {
  if (!prefs?.quiet_hours_enabled) return false;
  const start = toMinutes(prefs.quiet_start);
  const end = toMinutes(prefs.quiet_end);
  if (start === null || end === null) return false;
  const current = now.getHours() * 60 + now.getMinutes();
  return start <= end ? current >= start && current < end : current >= start || current < end;
}

/**
 * T3（用户视角路线图 2026-09-26）：是否应为这条新通知弹系统通知。
 *
 * 改造前托盘模式下**完全收不到提醒**——`useCorePoll` 在 `document.visibilityState`
 * 隐藏时整轮跳过，而宿主早已具备原生通知能力（`preload.ts` 的 `showNotification`
 * → `host:notify` → `Notification.show()`）却从无调用方。设置页写的「保留到托盘 ·
 * 后台守护 · 保持宏观预警」因此是落空的。
 *
 * 判定口径（四条都必要，缺一条就会变成打扰）：
 * 1. 窗口**隐藏**时——用户正看着应用时再弹系统通知纯属重复；
 * 2. 只对**本会话新增**的通知弹——首轮拉取只做基线登记，否则每次启动都会把积压的
 *    历史通知一次性弹一遍；
 * 3. 同一条不重复弹（`notifiedIds` 去重）；
 * 4. 免打扰时段内不弹——与 Core 侧钉钉投递的免打扰口径一致
 *    （`api/routers/notifications.py` 的 `_quiet_hours_active`）。
 */
export function shouldShowSystemNotification(input: {
  item: Notification;
  firstLoad: boolean;
  alreadyNotified: boolean;
  documentHidden: boolean;
  quiet: boolean;
}): boolean {
  if (input.documentHidden === false) return false;
  if (input.firstLoad) return false;
  if (input.alreadyNotified) return false;
  if (input.quiet) return false;
  return true;
}

/**
 * B1（frontend-optimization-roadmap-2026-09-12）：通知领域数据。
 * 从 AppShell 下沉：pending 列表状态 + 60s 自动刷新（useCorePoll，窗口隐藏时暂停）
 * + 标记已读（乐观更新，失败回滚）+ 手动评估。
 *
 * JV08 追加：分诊报告（「已分诊未通知」）。**不跟着 60s 轮询走**——它只在用户展开通知铃
 * 时才拉一次，避免为了一个默认折叠的区块多付一次常驻请求。
 *
 * T3：轮询在窗口隐藏时虽然被跳过，但**新增通知仍要能弹系统通知**——这由
 * `bridge.showNotification` 承担（宿主已有能力，此前无调用方）。
 */
export function useNotifications(client: CoreClient, notifyPrefs?: PersonalNotifyPrefs | null) {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  // 第四轮审计（api-5）：静音态要能被通知铃读到，否则「被静音」会被渲染成「没有通知」。
  const [mutedState, setMutedState] = useState<NotificationMuteState>(UNMUTED);
  const [triage, setTriage] = useState<NotificationTriageReport | null>(null);
  // 会话内「已弹过系统通知」的 id 集合。放 ref 而非 state：它不参与渲染。
  const notifiedIds = useRef<Set<string>>(new Set());
  // 首轮只登记基线，不弹。
  const firstLoadDone = useRef(false);
  const prefsRef = useRef(notifyPrefs);
  prefsRef.current = notifyPrefs;

  /** 把新出现的未读通知转成系统通知；返回本次弹了几条（测试用）。 */
  function raiseSystemNotifications(incoming: Notification[]): number {
    const bridge = typeof window !== "undefined" ? window.steward : null;
    if (!bridge?.showNotification) return 0;
    const firstLoad = !firstLoadDone.current;
    const hidden = !isDocumentVisible();
    const quiet = isQuietHoursNow(
      prefsRef.current ?? { quiet_hours_enabled: false, quiet_start: "22:00", quiet_end: "08:00" },
    );
    let shown = 0;
    for (const item of incoming) {
      if (shouldShowSystemNotification({
        item,
        firstLoad,
        alreadyNotified: notifiedIds.current.has(item.notification_id),
        documentHidden: hidden,
        quiet,
      })) {
        notifiedIds.current.add(item.notification_id);
        shown += 1;
        void bridge.showNotification({
          title: item.title || "投资管家提醒",
          body: item.summary || "",
        });
      } else if (!notifiedIds.current.has(item.notification_id)) {
        // 首轮与免打扰时段的也登记，避免恢复可见后补弹一堆。
        notifiedIds.current.add(item.notification_id);
      }
    }
    firstLoadDone.current = true;
    return shown;
  }

  useCorePoll(async () => {
    // 第四轮审计（api-5）：Core 侧改为返回包装对象，把「静音」与「没有」分开。
    // 此前静音时返回裸 `[]`，界面会显示「暂无待处理通知」——把自己设的免打扰
    // 呈现成「市场没事」，且没有任何指引告诉用户去哪儿改设置。
    const response = await client.request<NotificationPendingPage>({
      method: "GET", path: "/notifications/pending",
    });
    if (response.status < 400 && response.data) {
      const unread = response.data.items.filter((item) => !item.read);
      setNotifications(unread);
      setMutedState({
        muted: response.data.muted,
        reason: response.data.mute_reason,
        hiddenCount: response.data.hidden_count,
      });
      // 静音时**不弹系统通知**（免打扰的本意），但要记下静音状态供铃铛呈现
      if (!response.data.muted) raiseSystemNotifications(unread);
    }
  }, { intervalMs: 60000, enabled: !client.isDemo });

  async function loadNotificationTriage(): Promise<void> {
    if (client.isDemo) return;
    const response = await client.request<NotificationTriageReport>({ method: "GET", path: "/notifications/triage" });
    if (response.status < 400) setTriage(response.data);
  }

  async function markNotificationRead(notificationId: string): Promise<boolean> {
    // F2-2 乐观更新：本地先移除，失败回滚。
    const previous = notifications;
    setNotifications((current) => current.filter((item) => item.notification_id !== notificationId));
    const response = await client.request<Notification>({ method: "POST", path: `/notifications/${notificationId}/read` });
    if (response.status >= 400) {
      setNotifications(previous);
      return false;
    }
    return true;
  }

  async function evaluateNotifications(): Promise<boolean> {
    const response = await client.request<Notification[]>({ method: "POST", path: "/notifications/evaluate" });
    if (response.status >= 400) return false;
    setNotifications(response.data.filter((item) => !item.read));
    // 分诊结论刚更新过 → 顺手刷新「已分诊未通知」，否则展开区会显示上一轮的旧结论。
    void loadNotificationTriage();
    return true;
  }

  return {
    notifications, setNotifications, mutedState, triage, loadNotificationTriage,
    markNotificationRead, evaluateNotifications,
    raiseSystemNotifications,
  };
}

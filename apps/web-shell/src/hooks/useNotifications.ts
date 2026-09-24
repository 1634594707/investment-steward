import { useState } from "react";
import type { Notification, NotificationTriageReport } from "@investment-steward/domain-contracts";
import type { CoreClient } from "../state/coreClient";
import { useCorePoll } from "../state/useCoreQuery";

/**
 * B1（frontend-optimization-roadmap-2026-09-12）：通知领域数据。
 * 从 AppShell 下沉：pending 列表状态 + 60s 自动刷新（useCorePoll，窗口隐藏时暂停）
 * + 标记已读（乐观更新，失败回滚）+ 手动评估。
 *
 * JV08 追加：分诊报告（「已分诊未通知」）。**不跟着 60s 轮询走**——它只在用户展开通知铃
 * 时才拉一次，避免为了一个默认折叠的区块多付一次常驻请求。
 */
export function useNotifications(client: CoreClient) {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [triage, setTriage] = useState<NotificationTriageReport | null>(null);

  useCorePoll(async () => {
    const response = await client.request<Notification[]>({ method: "GET", path: "/notifications/pending" });
    if (response.status < 400) setNotifications(response.data.filter((item) => !item.read));
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
    notifications, setNotifications, triage, loadNotificationTriage,
    markNotificationRead, evaluateNotifications,
  };
}

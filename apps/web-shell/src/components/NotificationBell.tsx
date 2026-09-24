import { useEffect, useRef, useState } from "react";
import type { Notification, NotificationTriageReport } from "@investment-steward/domain-contracts";
import { formatDate } from "../state/format";

interface Props {
  notifications: Notification[];
  onMarkNotificationRead: (notificationId: string) => Promise<boolean>;
  /** 查看全部：跳转今日页通知区。 */
  onOpenToday: () => void;
  /**
   * JV08：分诊报告（「已分诊未通知」）。`null` = 还没拉过；`enabled=false` = 整层没跑
   * （Jev 未启用 / 总闸关闭），此时不渲染任何分诊信息。
   */
  triage?: NotificationTriageReport | null;
  /** 展开面板时拉一次分诊报告（不跟着 60s 轮询走）。 */
  onLoadTriage?: () => Promise<void>;
}

/**
 * 顶栏通知流面板：点击通知铃展开，展示待处理通知（GET /notifications/pending，仅未读），
 * 支持就地标记已读（POST /notifications/{id}/read）与跳转今日页；点击面板外任意处收起。
 *
 * JV08：面板底部追加「已分诊未通知」折叠区。低相关提醒不再进主列表，但**必须可翻到**——
 * 否则「不再弹通知」就变成了「被静默吞掉」。默认折叠：它不该抢待处理通知的注意力，
 * 但一眼能看到有多少条被压下了。
 */
export function NotificationBell({ notifications, onMarkNotificationRead, onOpenToday, triage, onLoadTriage }: Props) {
  const [open, setOpen] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [markFailed, setMarkFailed] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDocumentClick(event: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDocumentClick);
    return () => document.removeEventListener("mousedown", onDocumentClick);
  }, [open]);

  useEffect(() => {
    // 展开时才拉：这是一个默认折叠的区块，不值得占一个常驻轮询。
    if (open && onLoadTriage) void onLoadTriage();
  }, [open, onLoadTriage]);

  async function markRead(notificationId: string) {
    if (busyId) return;
    setBusyId(notificationId);
    const ok = await onMarkNotificationRead(notificationId);
    setBusyId(null);
    setMarkFailed(!ok);
  }

  const triageReady = triage?.enabled === true;
  const suppressedItems = triageReady ? triage.suppressed_items : [];

  return (
    <div className="bell-wrap" ref={rootRef}>
      <button
        className="icon-button"
        aria-label={`通知（${notifications.length} 条待处理）`}
        aria-expanded={open}
        title={notifications.length ? `${notifications.length} 条待处理通知` : "暂无待处理通知"}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="bell" />
        {notifications.length > 0 && <span className="bell-badge">{notifications.length > 99 ? "99+" : notifications.length}</span>}
      </button>
      {open && (
        <div className="notice-panel" role="dialog" aria-label="待处理通知">
          <header className="notice-head">
            <b>待处理通知 · {notifications.length}</b>
            <button
              className="text-button"
              onClick={() => {
                setOpen(false);
                onOpenToday();
              }}
            >
              查看全部 <span>→</span>
            </button>
          </header>
          {markFailed && <p className="notice-error">标记已读失败，请稍后重试。</p>}
          {notifications.length === 0 ? (
            <p className="notice-empty">暂无待处理通知。条件引擎产生的提醒会在这里出现。</p>
          ) : (
            <ul className="notice-list">
              {notifications.slice(0, 8).map((notice) => (
                <li key={notice.notification_id} className="notice-item">
                  <div className="notice-item-main">
                    <b>{notice.title}</b>
                    <p>{notice.summary}</p>
                    <small>
                      {formatDate(notice.created_at)} · 触发条件：{notice.triggered_by}
                      {notice.delivery_status === "failed" ? ` · 投递失败（第 ${notice.delivery_attempts ?? 0} 次）` : ""}
                    </small>
                  </div>
                  <button
                    className="text-button"
                    disabled={busyId === notice.notification_id}
                    onClick={() => void markRead(notice.notification_id)}
                  >
                    {busyId === notice.notification_id ? "处理中…" : "已读"}
                  </button>
                </li>
              ))}
            </ul>
          )}
          {triageReady && (
            <details className="notice-triage" data-triage-state={triage.suppressed > 0 ? "has-suppressed" : "clean"}>
              <summary>
                已分诊未通知 · {triage.suppressed}
                <span className="notice-triage-hint">语义分诊判为低相关，未弹站内也未外发</span>
              </summary>
              <p className="notice-triage-stats">
                共 {triage.total} 条：放行 {triage.notified} · 压制 {triage.suppressed} · 未送评 {triage.unannotated}
                {triage.model ? ` · 模型 ${triage.model}` : ""}
              </p>
              {suppressedItems.length === 0 ? (
                <p className="notice-triage-empty">本轮没有被压制的提醒。</p>
              ) : (
                <ul className="notice-list notice-triage-list">
                  {suppressedItems.slice(0, 8).map((notice) => (
                    <li key={notice.notification_id} className="notice-item">
                      <div className="notice-item-main">
                        <b>{notice.title}</b>
                        <p>{notice.summary}</p>
                        <small>
                          {formatDate(notice.created_at)} · 触发条件：{notice.triggered_by}
                          {notice.triage ? ` · ${notice.triage.impact_label} / ${notice.triage.priority_label}` : ""}
                        </small>
                        {notice.triage?.note ? <small className="notice-triage-note">{notice.triage.note}</small> : null}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
              {/* 「未送评」必须与「已通过」分开说——否则用户会以为没标的就是没问题的。 */}
              {triage.unannotated > 0 && (
                <p className="notice-triage-note">
                  有 {triage.unannotated} 条本轮未送评（未启用 / 超出单轮上限 / 超预算），它们**没有**经过语义判断，按放行处理。
                </p>
              )}
            </details>
          )}
        </div>
      )}
    </div>
  );
}

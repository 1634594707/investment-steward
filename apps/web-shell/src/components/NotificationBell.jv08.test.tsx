/**
 * JV08：通知铃面板的「已分诊未通知」折叠区。
 *
 * 前端这一层的验收点是**「压制不等于消失」在界面上真的成立**：
 * 被语义分诊判为低相关的提醒不再进主列表，但用户必须能翻到它、看到压制原因与两题结论；
 * 并且「未送评」不能被渲染成「已通过」——那是两件完全不同的事。
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { Notification, NotificationTriageReport } from "@investment-steward/domain-contracts";
import { NotificationBell } from "./NotificationBell";

function notice(overrides: Partial<Notification> = {}): Notification {
  return {
    notification_id: "n1",
    user_id: "u1",
    instrument: "510300",
    triggered_by: "跌破 MA60",
    condition_kind: "invalidation_condition",
    title: "510300 命中已确认失效条件",
    summary: "本条为纯行情播报：股价当日上涨 1.2%。",
    action_mode: "review_plan",
    evidence_refs: [],
    created_at: "2026-09-21T10:00:00Z",
    read: false,
    ...overrides,
  } as Notification;
}

function report(overrides: Partial<NotificationTriageReport> = {}): NotificationTriageReport {
  return {
    enabled: true,
    model: "typesafe/jev-1.13-20260917",
    total: 3,
    notified: 1,
    suppressed: 1,
    unannotated: 1,
    impacts: { material: 1, uncertain: 0, immaterial: 1 },
    priorities: { "0": 1, "2": 1 },
    suppressed_items: [
      notice({
        triage: {
          impact: "immaterial",
          impact_label: "不实质影响",
          priority: 0,
          priority_label: "不必通知",
          priority_percent: 0,
          confidence: 0.88,
          suppressed: true,
          triage_state: "suppressed",
          note: "判为低相关且不必通知（置信度 0.88），不弹站内、不外发，留痕可查",
          model: "typesafe/jev-1.13-20260917",
          schema_version: "1.0",
        },
      }),
    ],
    note: "分诊只做标注与投递取舍：不改写通知内容、不删除记录。",
    schema_version: "1.0",
    ...overrides,
  };
}

function openPanel() {
  fireEvent.click(screen.getByLabelText(/通知（/));
}

describe("NotificationBell 已分诊未通知（JV08）", () => {
  it("没拉过分诊报告时不渲染任何分诊信息", () => {
    render(
      <NotificationBell notifications={[notice()]} onMarkNotificationRead={vi.fn()} onOpenToday={vi.fn()} />,
    );
    openPanel();
    expect(screen.queryByText(/已分诊未通知/)).toBeNull();
  });

  it("enabled=false（Jev 未启用）时零渲染——与接入前逐字节一致", () => {
    render(
      <NotificationBell
        notifications={[notice()]}
        onMarkNotificationRead={vi.fn()}
        onOpenToday={vi.fn()}
        triage={report({ enabled: false, suppressed: 0, suppressed_items: [], note: "Jev 未启用" })}
      />,
    );
    openPanel();
    expect(screen.queryByText(/已分诊未通知/)).toBeNull();
    expect(screen.queryByText(/未送评/)).toBeNull();
  });

  it("默认折叠，但被压制的条目在 DOM 里可翻到（不删除）", () => {
    render(
      <NotificationBell
        notifications={[notice({ notification_id: "n2", title: "600519 命中已确认失效条件" })]}
        onMarkNotificationRead={vi.fn()}
        onOpenToday={vi.fn()}
        triage={report()}
      />,
    );
    openPanel();

    const fold = screen.getByText(/已分诊未通知/).closest("details");
    expect(fold).toBeTruthy();
    expect(fold?.open).toBe(false);

    // 折叠 ≠ 删除：内容仍在，且带两题结论与压制原因
    expect(screen.getByText("510300 命中已确认失效条件")).toBeTruthy();
    expect(screen.getByText(/不实质影响 \/ 不必通知/)).toBeTruthy();
    expect(screen.getByText(/不弹站内、不外发，留痕可查/)).toBeTruthy();
    // 主列表只渲染待处理通知，被压制的不会混进来
    expect(screen.queryByText("600519 命中已确认失效条件")).toBeTruthy();
  });

  it("统计行把「放行 / 压制 / 未送评」三个数字分开列", () => {
    render(
      <NotificationBell
        notifications={[]}
        onMarkNotificationRead={vi.fn()}
        onOpenToday={vi.fn()}
        triage={report()}
      />,
    );
    openPanel();
    const stats = screen.getByText(/共 3 条：/);
    expect(stats.textContent).toContain("放行 1");
    expect(stats.textContent).toContain("压制 1");
    expect(stats.textContent).toContain("未送评 1");
    expect(stats.textContent).toContain("typesafe/jev-1.13-20260917");
  });

  it("有未送评条目时明确说明「没有经过语义判断」，不说成已通过", () => {
    render(
      <NotificationBell
        notifications={[]}
        onMarkNotificationRead={vi.fn()}
        onOpenToday={vi.fn()}
        triage={report()}
      />,
    );
    openPanel();
    expect(screen.getByText(/没有\*\*经过语义判断，按放行处理/)).toBeTruthy();
  });

  it("没有压制条目时给出明确的空态，而不是留白", () => {
    render(
      <NotificationBell
        notifications={[]}
        onMarkNotificationRead={vi.fn()}
        onOpenToday={vi.fn()}
        triage={report({ suppressed: 0, unannotated: 0, suppressed_items: [] })}
      />,
    );
    openPanel();
    expect(screen.getByText("本轮没有被压制的提醒。")).toBeTruthy();
    expect(screen.queryByText(/没有\*\*经过语义判断/)).toBeNull();
  });

  it("展开面板时才拉分诊报告；未展开不拉", () => {
    const onLoadTriage = vi.fn().mockResolvedValue(undefined);
    render(
      <NotificationBell
        notifications={[]}
        onMarkNotificationRead={vi.fn()}
        onOpenToday={vi.fn()}
        triage={report()}
        onLoadTriage={onLoadTriage}
      />,
    );
    expect(onLoadTriage).not.toHaveBeenCalled();
    openPanel();
    expect(onLoadTriage).toHaveBeenCalledTimes(1);
  });
});

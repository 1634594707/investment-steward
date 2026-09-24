import { useState } from "react";
import type { PluginCatalogEntry } from "@investment-steward/domain-contracts";
import type { CoreConnection } from "../state/coreClient";
import type { TaskCenterEntry } from "../state/taskCenter";
import { FreshnessIndicator } from "../components/FreshnessIndicator";
import { useZoomPercent } from "./zoom";
import { useUiPrefs } from "./uiprefs";

interface Props {
  connection: CoreConnection;
  plugins: PluginCatalogEntry[];
  /** 各数据域最新观测时间（`/health` data_as_of）：状态栏「数据截至」段的数据源。 */
  dataAsOf?: Record<string, string | null | undefined>;
  /** R1-4：顶栏移交的数据时效指示（由行情/证据推导的最新 as_of）。 */
  asOf?: string | null | undefined;
  isDemo?: boolean;
  /** Core 权威插槽占用（GET /slots 汇总）；未提供时回退本地估算。 */
  slotUsage?: { used: number; cap: number };
  coreVersion?: string;
  schemaVersion?: string;
  /** C03（桌面端升级路线图 2026-09-18）：Core 请求连续超时——状态栏给可点逃生提示。 */
  coreHung?: boolean;
  onRestartCore?: () => void;
  /** D02（桌面端升级路线图 2026-09-18）：进行中任务条目（可展开 + 停止）。 */
  tasks?: TaskCenterEntry[];
}

/**
 * 插槽本地估算（仅回退）：Core 的 GET /slots 未就绪/失败时，用与后端 slots.py
 * DEFINED_SLOTS 同源的容量常量做合计展示；权威仲裁始终在 Core。
 */
const SLOT_CAPS: Record<string, number> = {
  "today.brief": 3,
  "today.learning": 2,
  "invest.market_view": 1,
  "invest.thesis": 2,
  "research.board": 3,
  "review.plan": 2,
  "notification.global": 1,
  "app.library": 4,
  "app.macro": 4,
  // P3-D01：与后端 slots.DEFINED_SLOTS 同步；缺此键会让状态栏算的 used/cap 偏小。
  "app.tactics": 4,
  "app.youzi": 4,
};

function pluginCounts(plugins: PluginCatalogEntry[]) {
  let enabled = 0;
  let disabled = 0;
  let available = 0;
  for (const entry of plugins) {
    const state = entry.installation?.state;
    if (state === "enabled") enabled += 1;
    else if (state === "available" || state == null) available += 1;
    else disabled += 1;
  }
  return { enabled, disabled, available };
}

function localSlotUsage(plugins: PluginCatalogEntry[]) {
  const targeted = new Map<string, number>();
  for (const entry of plugins) {
    if (entry.installation?.state !== "enabled") continue;
    // 优先服务端解析结果（G2-3），mock/旧数据兜底用 manifest.ui_slots。
    const outputs = entry.resolved_outputs ?? entry.manifest.ui_slots ?? [];
    for (const output of outputs) {
      targeted.set(output.slot, (targeted.get(output.slot) ?? 0) + 1);
    }
  }
  let used = 0;
  let cap = 0;
  for (const [slot, slotCap] of Object.entries(SLOT_CAPS)) {
    cap += slotCap;
    used += Math.min(targeted.get(slot) ?? 0, slotCap);
  }
  return { used, cap };
}

/** 取各数据域最新观测时间中最晚的一个，作为状态栏「数据截至」主值；无任何数据返回 null。 */
function latestDataAsOf(dataAsOf?: Record<string, string | null | undefined>): Date | null {
  if (!dataAsOf) return null;
  let latest: Date | null = null;
  for (const value of Object.values(dataAsOf)) {
    if (!value) continue;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) continue;
    if (latest === null || date.getTime() > latest.getTime()) latest = date;
  }
  return latest;
}

function pad(value: number): string {
  return value < 10 ? `0${value}` : String(value);
}

/** `2026-09-04T15:30:00Z` → `09-04 15:30`（本地时区，对齐设计稿「数据截至 09-03 15:30」）。 */
function formatAsOf(date: Date): string {
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 底部状态栏（R1-5）：连接 / 数据时效+截至 / 插件 / 插槽占用 / 缩放档，等宽 11px，常驻不滚动。 */
export function StatusBar({ connection, plugins, dataAsOf, asOf, isDemo, slotUsage: serverUsage, coreVersion = "—", schemaVersion = "—", coreHung = false, onRestartCore, tasks = [] }: Props) {
  // D02：任务中心条目默认收起，点击展开看每个任务的进度与停止按钮。
  const [tasksOpen, setTasksOpen] = useState(false);
  const counts = pluginCounts(plugins);
  const usage = serverUsage ?? localSlotUsage(plugins);
  const zoomPercent = useZoomPercent();
  const prefs = useUiPrefs();
  const coreText =
    connection === "ready" ? "Core 已连接 · loopback" : connection === "demo" ? "演示模式 · 未连接 Core" : connection === "connecting" ? "正在连接 Core…" : "Core 未连接";
  const latest = latestDataAsOf(dataAsOf);
  return (
    <footer className="statusbar" aria-label="应用状态栏">
      <span className={`sb-dot ${connection === "ready" ? "" : "off"}`} />
      <span>{coreText}</span>
      {coreHung && (
        onRestartCore ? (
          <button
            className="text-button"
            title="Core 连续请求超时（可能挂起）。点击重启本地 Core 恢复（C03）。"
            onClick={onRestartCore}
          >
            ⚠ Core 响应异常 · 点击重启
          </button>
        ) : (
          <span>⚠ Core 响应异常</span>
        )
      )}
      <FreshnessIndicator asOf={asOf} isDemo={Boolean(isDemo)} />
      <span>
        数据截至{" "}
        <span className="sb-strong">{latest ? formatAsOf(latest) : "—"}</span>
      </span>
      <span>
        插件 <span className="sb-strong">{counts.enabled} 启用 · {counts.disabled} 停用 · {counts.available} 可安装</span>
      </span>
      <span>
        插槽占用 <span className="sb-strong">{usage.used}/{usage.cap}</span>
      </span>
      {tasks.length > 0 && (
        <span className="sb-tasks">
          <button
            className="text-button"
            aria-expanded={tasksOpen}
            title="进行中的长任务（批量研报/方向研判等）。展开可查看进度并停止。"
            onClick={() => setTasksOpen((current) => !current)}
          >
            任务 {tasks.length} 进行中 {tasksOpen ? "▲" : "▼"}
          </button>
          {tasksOpen && (
            <span className="sb-task-list">
              {tasks.map((task) => (
                <span key={task.id} className="sb-task">
                  <b>{task.label}</b>
                  <span className="sb-strong">
                    {task.progress ? ` ${task.progress.done}/${task.progress.total}` : ""}
                    {task.detail ? ` · ${task.detail}` : ""}
                  </span>
                  {task.stop && (
                    <button className="text-button" title="停止该任务；已完成的份数保留" onClick={task.stop}>
                      停止
                    </button>
                  )}
                </span>
              ))}
            </span>
          )}
        </span>
      )}
      <div className="sb-right">
        {zoomPercent !== null && <span className="sb-zoom">缩放 {zoomPercent}%</span>}
        <span className="sb-hide">{prefs.density === "compact" ? "紧凑" : "舒适"} · {prefs.marketColors === "cn" ? "红涨绿跌" : "青涨琥珀跌"}</span>
        <span>core {coreVersion} · schema {schemaVersion}</span>
      </div>
    </footer>
  );
}

import { useEffect, useMemo, useState } from "react";
import type { AuditEvent, PluginCatalogEntry, PluginManifest, UpdateChannelView } from "@investment-steward/domain-contracts";
import type { CoreClient } from "../state/coreClient";
import { formatDate } from "../state/format";
import { Puzzle, Search } from "lucide-react";
import "./extensions.css";
import { ConfirmDialog } from "../components/ConfirmDialog";

interface Props {
  catalog: PluginCatalogEntry[];
  channels: UpdateChannelView[];
  /** B02（桌面端升级路线图 2026-09-18）：审计流改由本页经 client 服务端翻页拉取，
   * 不再从 boot 全表下发（boot 不随审计增长变慢，扩展页切页不再拉全表）。 */
  client: CoreClient;
  capabilityCount: number;
  busy: string | null;
  message: string | null;
  isDemo: boolean;
  onChange: (pluginId: string, action: "install" | "disable") => Promise<boolean>;
  onUpdate: (pluginId: string, targetVersion: string) => void;
  onRevoke: (pluginId: string) => void;
}

type TabId = "installed" | "discoverable" | "updates" | "audit";

/** F4-5 审计流排序：默认时间倒序，可切时间正序 / 按动作 / 按资源。 */
type AuditSort = "time-desc" | "time-asc" | "action" | "resource";

const AUDIT_SORTS: { id: AuditSort; label: string }[] = [
  { id: "time-desc", label: "最新优先" },
  { id: "time-asc", label: "最早优先" },
  { id: "action", label: "按动作" },
  { id: "resource", label: "按资源" },
];

const TABS: { id: TabId; label: string }[] = [
  { id: "installed", label: "已安装" },
  { id: "discoverable", label: "目录" },
  { id: "updates", label: "更新渠道" },
  { id: "audit", label: "审计记录" },
];

/** 数据是否离开本机：以 manifest.network_allowlist 为依据。空名单 = 数据不出本机。 */
function leavesDevice(manifest: PluginManifest): boolean {
  return (manifest.network_allowlist ?? []).length > 0;
}

const NINE_QUESTIONS: { key: keyof PluginManifest | "data_direction" | "signature_status"; label: string }[] = [
  { key: "description", label: "它让管家多看到什么" },
  { key: "supports_markets", label: "支持什么市场" },
  { label: "用什么数据", key: "data_direction" },
  { key: "capabilities", label: "申请哪些权限" },
  { key: "sdk_max", label: "兼容性（SDK）" },
  { key: "release_version", label: "验证版本" },
  { key: "plugin_type", label: "运行状态" },
  { key: "license", label: "约束（许可）" },
  { key: "network_allowlist", label: "限制（网络）" },
  { key: "signature_status", label: "签名身份（发布者）" },
  { key: "artifact_sha256", label: "制品哈希（SHA-256）" },
];

/** 签名身份：发布者是 Core 内嵌公钥的持有方；签名已提交（非占位标记）才可被安装。 */
function signatureIdentity(manifest: PluginManifest): string {
  const committed = manifest.signature && manifest.signature !== "unsigned-development-fixture";
  return `${manifest.publisher} · ${committed ? "Ed25519 已签名" : "未签名（开发占位）"}`;
}

function questionValue(manifest: PluginManifest, key: keyof PluginManifest | "data_direction" | "signature_status"): string {
  if (key === "data_direction") return leavesDevice(manifest) ? "可能离开本机（有网络白名单）" : "不出本机（network_allowlist 为空）";
  if (key === "signature_status") return signatureIdentity(manifest);
  const value = manifest[key];
  if (Array.isArray(value)) return value.length ? value.join(" / ") : "无";
  if (typeof value === "string" && value) return key === "artifact_sha256" ? `${value.slice(0, 12)}…（${value.length} 位）` : value;
  return "—";
}

/** 审计事件是否需要撤销/阻断：撤销 = 该资源所属插件的 REVOKED（事件本身不可逆，这里仅做展示归类）。 */
function auditKind(action: string): "plugin" | "policy" | "learning" | "research" | "plan" | "other" {
  if (action.startsWith("plugin.")) return "plugin";
  if (action.startsWith("policy.")) return "policy";
  if (action.startsWith("learning.")) return "learning";
  if (action.startsWith("research.")) return "research";
  if (action.startsWith("plan.")) return "plan";
  return "other";
}

/** 设置 ▸ 扩展（二级页）。一级导航不直接暴露「扩展」。 */
export function ExtensionsPage({ catalog, channels, client, capabilityCount, busy, message, isDemo, onChange, onUpdate, onRevoke }: Props) {
  const [tab, setTab] = useState<TabId>("installed");
  const [auditFilter, setAuditFilter] = useState("");
  const [auditSort, setAuditSort] = useState<AuditSort>("time-desc");
  const [pending, setPending] = useState<{ id: string; action: "install" | "disable" | "update" | "revoke"; version?: string } | null>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [auditPage, setAuditPage] = useState(0);
  // B02：审计流服务端分页状态（当前页数据 + 真值总数），替代旧的「全表 prop + 内存过滤」。
  const [auditRows, setAuditRows] = useState<AuditEvent[]>([]);
  const [auditTotal, setAuditTotal] = useState(0);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditError, setAuditError] = useState<string | null>(null);
  const [auditRefreshKey, setAuditRefreshKey] = useState(0);

  function matches(entry: PluginCatalogEntry) {
    return [entry.manifest.display_name, entry.manifest.plugin_id, entry.manifest.description].join(" ").toLocaleLowerCase().includes(query.trim().toLocaleLowerCase());
  }
  function requestAction(action: NonNullable<typeof pending>) { setConfirmError(null); setPending(action); }
  async function confirmAction() {
    if (!pending || confirmBusy) return;
    setConfirmBusy(true); setConfirmError(null);
    try {
      const ok = pending.action === "revoke" ? await onRevoke(pending.id) : pending.action === "update" ? await onUpdate(pending.id, pending.version!) : await onChange(pending.id, pending.action);
      if (ok) { setPending(null); setAuditRefreshKey((key) => key + 1); } else setConfirmError("操作未完成，请检查提示后重试。");
    } catch { setConfirmError("操作失败，请重试。"); }
    finally { setConfirmBusy(false); }
  }

  const installed = catalog.filter((entry) => entry.installation);
  const discoverable = catalog.filter((entry) => !entry.installation);
  // 更新可用 = 目录里已有的最新 manifest 版本比当前安装版本新。
  const updatable = installed.filter((entry) => entry.installation!.state !== "revoked" && entry.manifest.release_version !== entry.installation!.release_version);

  // B02：关键字输入防抖后作为服务端 q（后端对 action/resource_type/resource_id 做 LIKE，同旧口径）。
  const [auditFilterDebounced, setAuditFilterDebounced] = useState("");
  useEffect(() => {
    const timer = window.setTimeout(() => setAuditFilterDebounced(auditFilter), 300);
    return () => window.clearTimeout(timer);
  }, [auditFilter]);

  // B02：审计页签可见 / 过滤 / 排序 / 页码 / 刷新变化 → 只拉当前页（每页 30 条，与既有分页粒度一致）。
  useEffect(() => {
    if (tab !== "audit") return;
    let cancelled = false;
    setAuditLoading(true);
    setAuditError(null);
    const params = new URLSearchParams({
      limit: "30",
      offset: String(auditPage * 30),
      order: auditSort,
      with_total: "true",
    });
    const q = auditFilterDebounced.trim();
    if (q) params.set("q", q);
    void (async () => {
      try {
        const response = await client.request<{ ok: boolean; items: AuditEvent[]; total: number }>({
          method: "GET",
          path: `/audit?${params.toString()}`,
        });
        if (cancelled) return;
        if (response.status < 400 && response.data?.ok) {
          setAuditRows(response.data.items);
          setAuditTotal(response.data.total);
          // 数据收缩后页码越界时自愈（如删除/过滤后停留在空页）。
          const lastPage = Math.max(0, Math.ceil(response.data.total / 30) - 1);
          if (response.data.items.length === 0 && auditPage > lastPage) setAuditPage(lastPage);
        } else {
          setAuditError(`审计记录加载失败（HTTP ${response.status}）。`);
        }
      } catch {
        if (!cancelled) setAuditError("审计记录加载失败，请稍后重试。");
      } finally {
        if (!cancelled) setAuditLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [client, tab, auditFilterDebounced, auditSort, auditPage, auditRefreshKey]);

  // B02：服务端真值分页（旧 auditPages 由全表长度算出，这里由 total 推出）。
  const auditPages = Math.max(1, Math.ceil(auditTotal / 30));
  const currentAuditPage = Math.min(auditPage, auditPages - 1);

  const channelHost = channels.find((item) => item["channel"] === "host");
  const channelPlugin = channels.find((item) => item["channel"] === "plugin");
  const channelContent = channels.find((item) => item["channel"] === "content");
  const pendingEntry = catalog.find(entry => entry.manifest.plugin_id === pending?.id);
  const currentPermissions = pendingEntry?.installation?.granted_capabilities.join("、") || "无";
  const networkScope = pendingEntry?.manifest.network_allowlist?.join("、") || "无外部网络授权";

  return (
    <div className="page-stack extensions-page">
      <header className="extensions-heading">
        <div><span className="section-kicker">系统治理</span><h2>扩展控制中心</h2></div>
        <span>{isDemo ? "演示模式" : "本地 Core 管理"}</span>
      </header>
      <dl className="extensions-summary">
        <div><dt>已启用</dt><dd>{installed.filter(entry => entry.installation?.state === "enabled").length}</dd></div>
        <div><dt>已安装</dt><dd>{installed.length}</dd></div>
        <div><dt>可用更新</dt><dd>{updatable.length}</dd></div>
        <div><dt>能力清单</dt><dd>{capabilityCount}</dd></div>
      </dl>
      <nav className="extension-tabs" role="tablist" aria-label="扩展分类">
        {TABS.map((item) => (
          <button role="tab" aria-selected={tab === item.id} tabIndex={tab === item.id ? 0 : -1} id={`extension-tab-${item.id}`} aria-controls="extension-panel" className={`extension-tab ${tab === item.id ? "active" : ""}`} key={item.id} onClick={() => setTab(item.id)} onKeyDown={event => { const index = TABS.findIndex(entry => entry.id === tab); const next = event.key === "ArrowRight" ? (index + 1) % TABS.length : event.key === "ArrowLeft" ? (index + TABS.length - 1) % TABS.length : event.key === "Home" ? 0 : event.key === "End" ? TABS.length - 1 : -1; if (next >= 0) { event.preventDefault(); setTab(TABS[next]!.id); document.getElementById(`extension-tab-${TABS[next]!.id}`)?.focus(); } }}>
            {item.label}
          </button>
        ))}
      </nav>
      {message && <div className={`plugin-message ${message.includes("失败") ? "error" : ""}`} role="status">{message}</div>}

      <div id="extension-panel" role="tabpanel" aria-labelledby={`extension-tab-${tab}`}>
      {(tab === "installed" || tab === "discoverable") && <label className="extension-search"><Search size={16} /><input type="search" aria-label="搜索插件" placeholder="名称、标识或能力" value={query} onChange={event => setQuery(event.target.value)} /></label>}
      {tab === "installed" && (
        <section className="plugin-list">
          {installed.length === 0 && <button className="text-button" onClick={() => setTab("discoverable")}>查看插件目录</button>}
          {installed.length > 0 && !installed.some(matches) && <p role="status">没有匹配的插件。</p>}
          {installed.filter(matches).map((entry) => {
            const installation = entry.installation!;
            const enabled = installation.state === "enabled";
            const revoked = installation.state === "revoked";
            const updateAvailable = entry.manifest.release_version !== installation.release_version;
            const direction = leavesDevice(entry.manifest) ? "可能离开本机" : "数据不出本机";
            return (
              <article className="plugin-row" key={entry.manifest.plugin_id}>
                <div className="plugin-number" aria-hidden="true"><Puzzle size={20} /></div>
                <div className="plugin-main">
                  <div className="plugin-title">
                    <h3>{entry.manifest.display_name}</h3>
                    <span className={`plugin-state ${revoked ? "revoked" : enabled ? "enabled" : "disabled"}`}>{revoked ? "已撤销" : enabled ? "已启用" : "已停用"}</span>
                    {updateAvailable && !revoked && <span className="plugin-update-badge">可升级 → v{entry.manifest.release_version}</span>}
                  </div>
                  <p>{entry.manifest.description}</p>
                  <div className="plugin-meta">
                    <span>锁定 v{installation.release_version}</span>
                    <span>{installation.source}</span>
                    <span>安装于 {formatDate(installation.installed_at)}</span>
                  </div>
                  <div className="capability-list">
                    {installation.granted_capabilities.map((capability) => (
                      <span key={capability}>{capability}</span>
                    ))}
                  </div>
                  <div className={`data-direction ${direction === "数据不出本机" ? "local" : "remote"}`}>
                    {direction} · {(entry.manifest.network_allowlist ?? []).join("、") || "无外部网络授权"}
                  </div>
                </div>
                <div className="plugin-actions">
                  {revoked ? (
                    <small className="revoked-note">已撤销：停止产生新输出，历史 Evidence 保留可读取。</small>
                  ) : (
                    <small>健康：暂无运行记录 · 以 Core 状态为准</small>
                  )}
                  {updateAvailable && !revoked && (
                    <button className="primary-button" disabled={!!busy || confirmBusy} onClick={() => requestAction({ id: entry.manifest.plugin_id, action: "update", version: entry.manifest.release_version })}>
                      {busy === entry.manifest.plugin_id ? "升级中" : `升级到 v${entry.manifest.release_version}`}
                      <span>→</span>
                    </button>
                  )}
                  {enabled ? (
                    <button className="secondary-button" disabled={!!busy || confirmBusy} onClick={() => requestAction({ id: entry.manifest.plugin_id, action: "disable" })}>
                      {busy === entry.manifest.plugin_id ? "处理中" : "停用插件"}
                    </button>
                  ) : revoked ? (
                    <button className="secondary-button" disabled>已撤销</button>
                  ) : (
                    <button className="primary-button" disabled={!!busy || confirmBusy} onClick={() => requestAction({ id: entry.manifest.plugin_id, action: "install" })}>
                      {busy === entry.manifest.plugin_id ? "安装中" : "重新启用"}
                      <span>→</span>
                    </button>
                  )}
                  {!revoked && (
                      <button className="text-button danger" disabled={!!busy || confirmBusy} onClick={() => requestAction({ id: entry.manifest.plugin_id, action: "revoke" })}>
                      撤销
                    </button>
                  )}
                </div>
              </article>
            );
          })}
        </section>
      )}

      {tab === "discoverable" && (
        <section className="discover-grid">
          {discoverable.length === 0 && <p className="evidence-group-empty">当前目录中的插件均已安装。</p>}
          {discoverable.length > 0 && !discoverable.some(matches) && <p role="status">没有匹配的插件。</p>}
          {discoverable.filter(matches).map((entry) => (
            <article className="discover-card" key={entry.manifest.plugin_id}>
              <header className="discover-head">
                <h3>{entry.manifest.display_name}</h3>
                <span className={`plugin-state ${leavesDevice(entry.manifest) ? "disabled" : "enabled"}`}>
                  {leavesDevice(entry.manifest) ? "涉及外部数据" : "仅本机"}
                </span>
              </header>
              <details className="discover-qa"><summary>权限与来源详情</summary>
                {NINE_QUESTIONS.map((item) => (
                  <div className="discover-qa-row" key={String(item.key)}>
                    <dt>{item.label}</dt>
                    <dd>{questionValue(entry.manifest, item.key)}</dd>
                  </div>
                ))}
              </details>
              <footer className="discover-foot">
                <span className="mono">{entry.manifest.plugin_id} v{entry.manifest.release_version}</span>
                <button className="primary-button" onClick={() => requestAction({ id: entry.manifest.plugin_id, action: "install" })}>
                  安装
                  <span>→</span>
                </button>
              </footer>
              <p className="plugin-footnote">
                <span className="status-dot online" />
                安装前会验证发布者签名；升级会先完成健康检查再切换，失败自动回滚旧版本。
              </p>
            </article>
          ))}
        </section>
      )}

      {tab === "updates" && (
        <section className="plugin-list">
          <div className="channel-view">
            <h3 className="channel-title">更新通道（互不覆盖）</h3>
            <div className="channel-grid">
              <div className="channel-cell">
                <span className="channel-name">HOST · 内核</span>
                <span className="channel-ver">{channelHost?.current ?? "—"}</span>
                <small>插件 API 只读</small>
              </div>
              <div className="channel-cell">
                <span className="channel-name">PLUGIN · 插件</span>
                <span className="channel-ver">{channelPlugin?.current ?? "—"}</span>
                <small>install / update / revoke 专属</small>
              </div>
              <div className="channel-cell">
                <span className="channel-name">CONTENT · 内容</span>
                <span className="channel-ver">{channelContent?.current ?? "—"}</span>
                <small>证据内容指纹（指纹变化即更新）</small>
              </div>
            </div>
          </div>

          {updatable.length === 0 ? (
            <p className="evidence-group-empty">当前安装的插件均为目录中最新版本，无需更新。</p>
          ) : (
            updatable.map((entry) => (
              <article className="plugin-row" key={entry.manifest.plugin_id}>
                <div className="plugin-number" aria-hidden="true"><Puzzle size={20} /></div>
                <div className="plugin-main">
                  <div className="plugin-title">
                    <h3>{entry.manifest.display_name}</h3>
                    <span className="plugin-update-badge">有可用更新</span>
                  </div>
                  <p>{entry.manifest.description}</p>
                  <div className="plugin-meta">
                    <span>当前 v{entry.installation!.release_version}</span>
                    <span>可升级到 v{entry.manifest.release_version}</span>
                  </div>
                </div>
                <div className="plugin-actions">
                  <button className="primary-button" disabled={!!busy || confirmBusy} onClick={() => requestAction({ id: entry.manifest.plugin_id, action: "update", version: entry.manifest.release_version })}>
                    {busy === entry.manifest.plugin_id ? "升级中" : `升级到 v${entry.manifest.release_version}`}
                    <span>→</span>
                  </button>
                </div>
              </article>
            ))
          )}
          <p className="plugin-footnote">
            <span className="status-dot online" />
            更新先通过健康检查再切换，失败自动回滚旧版本，不会留下升级到一半的状态。
          </p>
        </section>
      )}

      {tab === "audit" && (
        <section className="plugin-list">
          <div className="audit-toolbar">
            <input
              type="search"
              aria-label="筛选审计记录"
              className="audit-filter"
              placeholder="按事件 / 资源 / id 过滤，如 policy. / learning."
              value={auditFilter}
              onChange={(event) => { setAuditFilter(event.target.value); setAuditPage(0); }}
            />
            <div className="audit-sort" role="group" aria-label="审计排序">
              {AUDIT_SORTS.map((option) => (
                <button
                  key={option.id}
                  className={`sort-head extension-tab ${auditSort === option.id ? "active" : ""}`}
                  aria-pressed={auditSort === option.id}
                  onClick={() => { setAuditSort(option.id); setAuditPage(0); }}
                >
                  {option.label}
                  {auditSort === option.id && <span className="sort-arrow">▼</span>}
                </button>
              ))}
            </div>
            <span className="audit-count">{auditTotal} 条事件</span>
          </div>
          {auditError ? (
            <p className="form-error" role="alert">{auditError}</p>
          ) : auditRows.length === 0 ? (
            <p className="evidence-group-empty">{auditLoading ? "审计记录加载中…" : auditFilter ? "没有匹配的审计记录。" : "尚无审计记录。"}</p>
          ) : (
            <div className="audit-timeline">
              <ul className="audit-list">
                {auditRows.map((event) => (
                  <li className="audit-row" key={event.event_id}>
                    <span className={`audit-kind audit-kind-${auditKind(event.action)}`}>{auditKind(event.action)}</span>
                    <span className="audit-action mono">{event.action}</span>
                    <span className="audit-resource mono">{event.resource_type}{event.resource_id ? ` · ${event.resource_id.slice(0, 8)}` : ""}</span>
                    <time className="audit-time mono" dateTime={event.created_at}>{formatDate(event.created_at)}</time>
                  </li>
                ))}
              </ul>
              <nav className="audit-pagination" aria-label="审计分页"><button className="text-button" disabled={currentAuditPage === 0} onClick={() => setAuditPage(currentAuditPage - 1)}>上一页</button><span>{currentAuditPage + 1} / {auditPages}</span><button className="text-button" disabled={currentAuditPage + 1 === auditPages} onClick={() => setAuditPage(currentAuditPage + 1)}>下一页</button></nav>
            </div>
          )}
          <p className="plugin-footnote">
            <span className="status-dot online" />
            审计事件来自 Core `GET /audit`（服务端 `_audit` 写入，前端只读；B02 起按页拉取，每页 30 条）。可按动作、资源与 id 过滤。
          </p>
        </section>
      )}
      </div>
      {pending && <ConfirmDialog title={{ install: "安装并启用插件", disable: "停用插件", update: "更新插件", revoke: "撤销插件" }[pending.action]}
        summary={pending.action === "revoke" ? "撤销后停止产生新输出，历史证据保留。此状态变更不可逆。" : pending.action === "disable" ? "停止插件输出，历史证据保留。" : "确认插件版本、权限和网络访问范围。"}
        requireNote={false} busy={confirmBusy} error={confirmError} confirmLabel="确认操作"
        diffs={[
          { label: "插件", before: pendingEntry?.manifest.display_name ?? pending.id, after: pendingEntry?.manifest.display_name ?? pending.id },
          { label: "版本", before: pendingEntry?.installation?.release_version ?? "未安装", after: pending.action === "disable" || pending.action === "revoke" ? pendingEntry?.installation?.release_version ?? "未安装" : pending.version ?? pendingEntry?.manifest.release_version ?? "" },
          { label: "状态", before: pendingEntry?.installation?.state ?? "未安装", after: pending.action === "disable" ? "已停用" : pending.action === "revoke" ? "已撤销" : "已启用" },
          { label: "权限", before: currentPermissions, after: pending.action === "disable" || pending.action === "revoke" ? currentPermissions : pendingEntry?.manifest.capabilities.join("、") || "无" },
          { label: "网络范围", before: pendingEntry?.installation ? networkScope : "未授权", after: networkScope },
        ]} onConfirm={() => void confirmAction()} onCancel={() => setPending(null)} />}

    </div>
  );
}

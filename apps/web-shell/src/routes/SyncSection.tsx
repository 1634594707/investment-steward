/**
 * 多设备同步区块（阶段 4 / §4 前端接入）。
 * 显示中转通道状态,提供「立即同步」与「应用收件箱」;配置端点(/sync/config)由首配脚本/CLI 完成,
 * 界面只读不回显任何凭据。
 */
import { useEffect, useState } from "react";
import type { CoreRequest } from "../state/coreClient";
import { detailOf } from "../state/coreClient";

interface SyncStatusInfo {
  configured: boolean;
  relay_url: string | null;
  device_id: string | null;
  has_token: boolean;
  has_sync_key: boolean;
  pull_cursor: number;
  outbox: { pending: number; total: number };
  inbox_count: number;
}

interface RunReport { ok: boolean; uploaded?: number; pulled?: number; error?: string }
interface ApplyReport {
  ok: boolean;
  applied_watch_items: number;
  updated_watch_items: number;
  applied_notifications: number;
  applied_mobile_notes?: number;
  skipped_plans: string[];
  skipped_research: number;
}

export function SyncSection({ coreRequest }: { coreRequest?: (req: CoreRequest) => Promise<{ status: number; data: unknown }> }) {
  const [status, setStatus] = useState<SyncStatusInfo | null>(null);
  const [busy, setBusy] = useState<"run" | "apply" | null>(null);
  const [report, setReport] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showPairing, setShowPairing] = useState(false);
  const [pairing, setPairing] = useState<{ relay_url: string; user_token: string; sync_key: string } | null>(null);
  const [pairError, setPairError] = useState<string | null>(null);
  const [pairCode, setPairCode] = useState<{ code: string; expires_at: string } | null>(null);

  async function generatePairCode() {
    setPairError(null);
    try {
      const r = await call<{ ok: boolean; code: string; expires_at: string }>({ method: "POST", path: "/sync/pairing-code" });
      setPairCode({ code: r.code, expires_at: r.expires_at });
    } catch (err) { setPairError(err instanceof Error ? err.message : "生成配对码失败"); }
  }

  async function loadPairing() {
    setPairError(null);
    try { setPairing(await call<{ relay_url: string; user_token: string; sync_key: string }>({ method: "GET", path: "/sync/config/reveal" })); }
    catch (err) { setPairError(err instanceof Error ? err.message : "读取失败"); }
  }

  async function call<T>(req: CoreRequest): Promise<T> {
    if (!coreRequest) throw new Error("当前环境没有 Core 请求通道。");
    const { status, data } = await coreRequest(req);
    if (status >= 400) throw new Error(detailOf(data) ?? `Core 返回错误（HTTP ${status}）`);
    return data as T;
  }

  async function load() {
    if (!coreRequest) return;
    try { setStatus(await call<SyncStatusInfo>({ method: "GET", path: "/sync/status" })); }
    catch (err) { setError(err instanceof Error ? err.message : "读取同步状态失败"); }
  }
  useEffect(() => { void load(); /* eslint-disable-line react-hooks/exhaustive-deps */ }, [coreRequest]);

  async function runSync() {
    setBusy("run"); setError(null); setReport(null);
    try {
      const r = await call<RunReport>({ method: "POST", path: "/sync/run" });
      setReport(r.ok
        ? `同步完成：上传 ${r.uploaded ?? 0} 条，拉取 ${r.pulled ?? 0} 条`
        : `同步失败：${r.error ?? "未知"}`);
      await load();
    } catch (err) { setError(err instanceof Error ? err.message : "同步失败"); }
    finally { setBusy(null); }
  }

  async function applyInbox() {
    setBusy("apply"); setError(null); setReport(null);
    try {
      const r = await call<ApplyReport>({ method: "POST", path: "/sync/apply" });
      setReport(`应用完成：新增观察 ${r.applied_watch_items} · 更新观察 ${r.updated_watch_items} · 通知 ${r.applied_notifications} · 手机消息 ${r.applied_mobile_notes ?? 0} · 计划跳过 ${r.skipped_plans.length}（不自动创建）· 研究摘要只读 ${r.skipped_research}`);
    } catch (err) { setError(err instanceof Error ? err.message : "应用失败"); }
    finally { setBusy(null); }
  }

  if (!coreRequest) {
    return (
      <div className="dl-block storage-location">
        <h4>多设备同步</h4>
        <p className="storage-note">当前环境没有 Core 请求通道，同步不可用。</p>
      </div>
    );
  }
  return (
    <div className="dl-block storage-location">
      <h4>多设备同步 <small style={{ marginLeft: 8, fontWeight: 400 }}>st.18257.xyz 中转 · 端到端加密 · 服务器只存密文</small></h4>
      {status ? (
        <dl className="settings-connection storage-paths">
          <div><dt>通道</dt><dd>{status.configured ? `已配置（${status.relay_url}）` : "未配置（需先执行 /sync/config）"}</dd></div>
          <div><dt>设备</dt><dd>{status.device_id ?? "—"}</dd></div>
          <div><dt>拉取游标</dt><dd>{status.pull_cursor}</dd></div>
          <div><dt>待传 / 收件</dt><dd>{status.outbox.pending} / {status.inbox_count}</dd></div>
        </dl>
      ) : <p className="storage-note">{busy ? "读取中…" : "同步状态未加载。"}</p>}
      <div className="storage-actions">
        <button className="ghost-btn" disabled={busy !== null} onClick={() => void runSync()}>{busy === "run" ? "同步中…" : "立即同步"}</button>
        <button className="ghost-btn" disabled={busy !== null} onClick={() => void applyInbox()}>{busy === "apply" ? "应用中…" : "应用收件箱"}</button>
        <button className="ghost-btn" disabled={busy !== null} onClick={() => void generatePairCode()}>生成手机配对码</button>
        <button className="ghost-btn" disabled={busy !== null} onClick={() => { setShowPairing((v) => !v); void loadPairing(); }}>{showPairing ? "收起手动接入" : "手动接入…"}</button>
      </div>
      {pairCode && (
        <div className="storage-note" style={{ border: "1px solid #2a3546", borderRadius: 8, padding: "10px 12px", marginTop: 8 }}>
          <p style={{ marginTop: 0 }}><strong>手机配对码</strong>（10 分钟内有效、只能用一次）：在手机 App「设置 → 输入配对码」填入即可，无需抄写密钥。</p>
          <p style={{ fontSize: 30, letterSpacing: 10, margin: "6px 0", fontFamily: "monospace" }}><strong>{pairCode.code}</strong></p>
          <p style={{ marginBottom: 0 }}>过期时间：{pairCode.expires_at}（服务器暂存凭据，领取即删；泄露可等过期或撤销设备）。</p>
        </div>
      )}
      {showPairing && (
        <div className="storage-note" style={{ border: "1px solid #2a3546", borderRadius: 8, padding: "10px 12px", marginTop: 8 }}>
          <p style={{ marginTop: 0 }}><strong>手机端接入（§5 设备绑定）</strong>：在手机 App「设置」中依次粘贴以下两项。凭据仅本机展示，勿经不可信渠道传输。</p>
          {pairError && <p role="alert">读取失败：{pairError}</p>}
          {pairing ? (
            <>
              <p style={{ wordBreak: "break-all" }}>① 中转地址：<code>{pairing.relay_url}</code></p>
              <p style={{ wordBreak: "break-all" }}>② 用户令牌：<code>{pairing.user_token}</code> <button className="ghost-btn" style={{ marginLeft: 6 }} onClick={() => void navigator.clipboard?.writeText(pairing.user_token)}>复制</button></p>
              <p style={{ wordBreak: "break-all" }}>③ 同步密钥：<code>{pairing.sync_key}</code> <button className="ghost-btn" style={{ marginLeft: 6 }} onClick={() => void navigator.clipboard?.writeText(pairing.sync_key)}>复制</button></p>
              <p style={{ marginBottom: 0 }}>手机将用①②注册专属设备令牌，用③解密端到端信封。每次展示均写入审计日志。</p>
          </>
          ) : <p>读取中…</p>}
        </div>
      )}
      {report && <p className="storage-note" role="status">{report}</p>}
      {error && <p className="storage-note" role="alert">{error}</p>}
      <p className="storage-note">同步白名单仅含观察事项 / 计划状态 / 通知 / 研究摘要；完整证据与原始资料仅保存在本机。应用口径：观察事项按来源创建/更新，计划不自动创建。</p>
    </div>
  );
}

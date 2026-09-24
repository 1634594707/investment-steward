import { useCallback, useEffect, useState } from "react";
import type { JevConfigView } from "@investment-steward/domain-contracts";
import type { CoreRequest } from "../state/coreClient";
import { detailOf } from "../state/coreClient";
import "./settings.css";

/**
 * JV02（Jev 决策模型接入路线图 2026-09-21）：Jev 决策模型分区。
 *
 * 独立成模块的理由与 FeedHealthSection 相同——设置页本已近 1900 行，长文件里再塞一个区块不划算。
 *
 * 这一节要讲清三件容易搞错的事（均为官方文档 2026-09-21 的实测口径）：
 * 1. **Jev 不是 chat 模型**：官方走 `POST {base_url}/systemone`，网关（OpenRouter 等）走各自的
 *    完整端点（见 `jev_client.resolve_endpoint`），两者都只回「是/否、选哪个、打几分」的
 *    类型化答案，不生成正文。因此它与「模型配置」是两套并列协议，不共用方案表。
 * 2. **`noul` 题不返回 confidence**，**`score` 不是 0–100**（是等级轴上的概率加权均值）。
 *    界面上不出现「置信度」这种 Jev 根本不返回的字段。
 * 3. **state 会出网到第三方**（美国托管、默认非零留存、ZDR 仅企业版），所以默认关闭，
 *    开启前必须把去向与政策摆在明面上。
 *
 * 总闸关闭（STEWARD_MODEL_ACCESS=0）时所有按钮**如实降级**，不假装能测。
 */

interface Props {
  /** Core 请求通道；未传时区块显示降级说明（与存储位置管理同惯例）。 */
  coreRequest?: (req: CoreRequest) => Promise<{ status: number; data: unknown }>;
}

interface ProbeResult {
  ok: boolean;
  latency_ms: number;
  detail: string;
  models?: string[];
  model?: string;
  requested_model?: string;
  probe_noul?: number;
  probe_verdict?: string;
  input_tokens?: number | null;
  output_tokens?: number | null;
}

type Feedback = { kind: "ok" | "err"; text: string } | null;

/** 超时输入解析：空串 → null（用内置默认）；5–600 整数；其余 "invalid"。 */
function parseTimeout(raw: string): number | null | "invalid" {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  if (!/^\d+$/.test(trimmed)) return "invalid";
  const value = Number(trimmed);
  if (!Number.isInteger(value) || value < 5 || value > 600) return "invalid";
  return value;
}

export function JevSection({ coreRequest }: Props) {
  const [view, setView] = useState<JevConfigView | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  // 表单草稿（保存前不落库）
  const [enabled, setEnabled] = useState(false);
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [credentialRef, setCredentialRef] = useState("");
  const [timeoutRaw, setTimeoutRaw] = useState("");
  const [dirty, setDirty] = useState(false);

  const [feedback, setFeedback] = useState<Feedback>(null);
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [busy, setBusy] = useState<"" | "save" | "probe" | "models">("");

  const applyView = useCallback((data: JevConfigView) => {
    setView(data);
    setEnabled(data.config.enabled);
    setBaseUrl(data.config.base_url);
    setModel(data.config.model);
    setCredentialRef(data.config.credential_ref);
    setTimeoutRaw(String(data.config.timeout_secs));
    setDirty(false);
  }, []);

  const load = useCallback(async () => {
    if (!coreRequest) return;
    setLoading(true);
    setLoadError(null);
    const response = await coreRequest({ method: "GET", path: "/jev/config" });
    setLoading(false);
    if (response.status >= 400 || !response.data) {
      setLoadError(detailOf(response.data) ?? `读取失败（HTTP ${response.status}）`);
      return;
    }
    applyView(response.data as JevConfigView);
  }, [coreRequest, applyView]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!coreRequest) {
    return (
      <div className="dl-block">
        <h4>Jev 决策模型（System One）</h4>
        <p className="form-hint">当前通道不支持读取 Jev 配置（未提供 Core 请求通道）。</p>
      </div>
    );
  }

  const gateClosed = view !== null && !view.access_enabled;
  const disabled = gateClosed || busy !== "";

  const timeoutParsed = parseTimeout(timeoutRaw);
  const timeoutInvalid = timeoutParsed === "invalid";
  const baseUrlInvalid = baseUrl.trim() !== "" && !/^https?:\/\//.test(baseUrl.trim());
  const modelInvalid = model.trim() === "";
  const canSave = !disabled && !timeoutInvalid && !baseUrlInvalid && !modelInvalid;

  async function handleSave() {
    if (!coreRequest) return;
    setBusy("save");
    setFeedback(null);
    const response = await coreRequest({
      method: "PUT",
      path: "/jev/config",
      body: {
        enabled,
        base_url: baseUrl.trim(),
        model: model.trim(),
        credential_ref: credentialRef.trim(),
        timeout_secs: timeoutParsed === null || timeoutParsed === "invalid" ? 60 : timeoutParsed,
      },
    });
    setBusy("");
    if (response.status >= 400 || !response.data) {
      setFeedback({ kind: "err", text: detailOf(response.data) ?? `保存失败（HTTP ${response.status}）` });
      return;
    }
    const saved = response.data as { config: JevConfigView["config"]; credential_note?: string | null };
    setView((current) => (current ? { ...current, saved: true, config: saved.config } : current));
    // 保存后回填规范化结果：粘贴明文密钥时服务端会转成 key_id 并搬进凭据库，
    // 输入框必须跟着显示 key_id，否则用户会以为明文密钥还留在配置里（或被下次保存覆盖）。
    setCredentialRef(saved.config.credential_ref);
    setDirty(false);
    setFeedback({
      kind: "ok",
      text: `${saved.credential_note ? `${saved.credential_note} ` : ""}已保存到本机。密钥不在本配置里，仍只存凭据引用。`,
    });
  }

  async function handleProbe() {
    if (!coreRequest) return;
    setBusy("probe");
    setProbe(null);
    setFeedback(null);
    const response = await coreRequest({
      method: "POST",
      path: "/jev/probe",
      body: {
        base_url: baseUrl.trim() || undefined,
        model: model.trim() || undefined,
        credential_ref: credentialRef.trim(),
        timeout_secs: timeoutParsed === "invalid" ? undefined : timeoutParsed,
      },
    });
    setBusy("");
    if (response.status >= 400 || !response.data) {
      setFeedback({ kind: "err", text: detailOf(response.data) ?? `探测失败（HTTP ${response.status}）` });
      return;
    }
    setProbe(response.data as ProbeResult);
  }

  async function handleDiscoverModels() {
    if (!coreRequest) return;
    setBusy("models");
    setFeedback(null);
    const response = await coreRequest({
      method: "POST",
      path: "/jev/models",
      body: {
        base_url: baseUrl.trim() || undefined,
        credential_ref: credentialRef.trim(),
        timeout_secs: timeoutParsed === "invalid" ? undefined : timeoutParsed,
      },
    });
    setBusy("");
    if (response.status >= 400 || !response.data) {
      setFeedback({ kind: "err", text: detailOf(response.data) ?? `拉取失败（HTTP ${response.status}）` });
      return;
    }
    const result = response.data as ProbeResult;
    setModels(result.models ?? []);
    setFeedback({ kind: result.ok ? "ok" : "err", text: result.detail });
  }

  return (
    <div className="dl-block">
      <h4>Jev 决策模型（System One）</h4>
      <p className="form-hint">
        Jev 是<strong>另一套协议</strong>的决策模型：官方走 <code>POST {"{base_url}"}/systemone</code>，
        网关（OpenRouter 等）走各自的完整端点；两者都只回「是/否、选哪个、打几分」的类型化答案，
        <strong>不生成正文</strong>。它当质检员与分诊台，不替代研报生成；判定结果一律只做标注、降级、排序，
        <strong>不改写模型原话、不阻断交付</strong>。
      </p>

      {loading && <p className="form-hint" role="status">读取中…</p>}
      {loadError && <p className="form-error" role="alert">{loadError}</p>}

      {view && (
        <>
          {gateClosed && (
            <p className="form-error" role="alert">
              模型出网总闸已关闭（<code>STEWARD_MODEL_ACCESS=0</code>）：Jev 不会发出任何请求，
              保存与探测按钮均已停用。
            </p>
          )}
          {!view.saved && (
            <p className="form-hint">
              当前显示的是内置默认值（也可能来自 <code>STEWARD_JEV_*</code> 环境变量），尚未在本页保存过。
            </p>
          )}

          <h5>数据去向（开启前请先确认）</h5>
          <table className="kv-table">
            <tbody>
              <tr><td>用客户输入训练模型</td><td>{view.data_handling.trains_on_input ? "是" : "否（官方承诺）"}</td></tr>
              <tr><td>数据托管地</td><td>{view.data_handling.hosted_in}</td></tr>
              <tr><td>保留期</td><td>{view.data_handling.retention}</td></tr>
              <tr>
                <td>零数据留存（ZDR）</td>
                <td>
                  {view.data_handling.zero_data_retention === "enterprise_only" ? "仅企业版（需联系厂商）" : view.data_handling.zero_data_retention}
                  {" · "}
                  {view.data_handling.zdr_applied ? "本项目已申请" : "本项目不申请（既有决定）"}
                </td>
              </tr>
            </tbody>
          </table>
          <p className="form-hint">
            计费只算输入 token（输出免费），因此 <strong>state 越精简越省</strong>——
            与「state 数据最小化」是同一条约束：各场景只允许提交白名单字段。
            {!view.data_handling.zdr_applied && (
              <>
                {" "}本机已决定<strong>不申请企业版 ZDR</strong>，因此没有「反正有零留存」这层兜底，
                白名单按最严口径执行：只送判定必需的最小片段。
              </>
            )}
          </p>

          <div className="invest-form">
            <label className="form-field">
              <span>启用 Jev</span>
              <label>
                <input
                  type="checkbox"
                  checked={enabled}
                  disabled={disabled}
                  onChange={(event) => { setEnabled(event.target.checked); setDirty(true); }}
                />{" "}
                开启后各场景的 state 才会出网判定（默认关闭）
              </label>
            </label>

            <label className="form-field">
              <span>端点 base_url</span>
              <input
                type="text"
                value={baseUrl}
                disabled={disabled}
                placeholder={view.defaults.base_url}
                onChange={(event) => { setBaseUrl(event.target.value); setDirty(true); }}
              />
              <span className={baseUrlInvalid ? "probe-note err" : "form-hint"}>
                {baseUrlInvalid
                  ? "必须以 http:// 或 https:// 开头"
                  : `官方填 API 根 ${view.defaults.base_url}（本机自动补 /systemone）；走网关请填完整端点，如 https://openrouter.ai/api/alpha/decisions`}
              </span>
            </label>

            <div className="form-row">
              <label className="form-field">
                <span>模型名</span>
                <input
                  type="text"
                  value={model}
                  disabled={disabled}
                  placeholder={view.defaults.model}
                  onChange={(event) => { setModel(event.target.value); setDirty(true); }}
                />
                <span className={modelInvalid ? "probe-note err" : "form-hint"}>
                  {modelInvalid
                    ? "模型名不能为空"
                    : "官方别名 jev-latest；走网关要带命名空间（OpenRouter：typesafe/jev，或钉版本 typesafe/jev-1.13）。已按某版本标定过阈值就改钉版本号"}
                </span>
              </label>

              <label className="form-field">
                <span>超时（秒）</span>
                <input
                  type="text"
                  value={timeoutRaw}
                  disabled={disabled}
                  placeholder={String(view.defaults.timeout_secs)}
                  onChange={(event) => { setTimeoutRaw(event.target.value); setDirty(true); }}
                />
                <span className={timeoutInvalid ? "probe-note err" : "form-hint"}>
                  {timeoutInvalid ? "需为 5–600 的整数" : "System One 通常秒级返回，60 秒已很宽松"}
                </span>
              </label>
            </div>

            <label className="form-field">
              <span>凭据引用</span>
              <input
                type="text"
                value={credentialRef}
                disabled={disabled}
                placeholder="jev_api_key"
                onChange={(event) => { setCredentialRef(event.target.value); setDirty(true); }}
              />
              <span className="form-hint">
                填 key_id（推荐 <code>jev_api_key</code>）、凭据尾号，或直接粘贴明文密钥。
                明文可用于本次探测；<strong>点保存时会自动存入本机凭据库</strong>（key_id 用
                <code>jev_api_key</code>），配置表只留 key_id，明文不落 <code>jev_config</code>。
              </span>
            </label>
          </div>

          <div className="storage-actions">
            <button className="primary-btn" disabled={!canSave} onClick={() => void handleSave()}>
              {busy === "save" ? "保存中…" : "保存"}
            </button>
            <button className="ghost-btn" disabled={disabled} onClick={() => void handleProbe()}>
              {busy === "probe" ? "探测中…" : "测连通性"}
            </button>
            <button className="ghost-btn" disabled={disabled} onClick={() => void handleDiscoverModels()}>
              {busy === "models" ? "拉取中…" : "拉取模型"}
            </button>
            {dirty && <span className="form-hint">有未保存的改动</span>}
          </div>

          {feedback && (
            <p className={feedback.kind === "ok" ? "probe-note ok" : "probe-note err"} role="status">
              {feedback.text}
            </p>
          )}

          {probe && (
            <>
              <h5>探测回执</h5>
              <p className={probe.ok ? "probe-note ok" : "probe-note err"} role="status">{probe.detail}</p>
              {probe.ok && (
                <table className="kv-table">
                  <tbody>
                    <tr><td>请求模型名</td><td>{probe.requested_model ?? "—"}</td></tr>
                    <tr><td>实际应答版本</td><td>{probe.model ?? "—"}</td></tr>
                    <tr><td>延迟</td><td>{probe.latency_ms} ms</td></tr>
                    <tr><td>输入 / 输出 token</td><td>{probe.input_tokens ?? "—"} / {probe.output_tokens ?? "—"}</td></tr>
                    <tr>
                      <td>中文探测题</td>
                      <td>
                        noul={probe.probe_noul === undefined ? "—" : probe.probe_noul.toFixed(2)}
                        （{probe.probe_verdict ?? "—"}）
                      </td>
                    </tr>
                  </tbody>
                </table>
              )}
              <p className="form-hint">
                官方声明英文精度最佳、<strong>中文（CJK）可处理但精度较低</strong>：
                中文探测题的 noul 明显偏离 1 即提示中文判定不稳——各场景阈值必须用自有样本重新标定后再依赖。
              </p>
            </>
          )}

          {models.length > 0 && (
            <>
              <h5>可用模型 / 别名（{models.length}）</h5>
              <p className="form-hint">{models.join("、")}</p>
            </>
          )}

          <h5>官方协议上限（本地先拦，避免越界请求白付一次输入 token）</h5>
          <table className="kv-table">
            <tbody>
              <tr><td>choice 选项上限</td><td>{view.limits.choice_max_options}</td></tr>
              <tr><td>score 等级数</td><td>{view.limits.score_min_levels}–{view.limits.score_max_levels} 级（有序等级数组）</td></tr>
              <tr>
                <td>noul 判定阈值</td>
                <td>≥ {view.noul_thresholds.yes} 视为「是」，≤ {view.noul_thresholds.no} 视为「否」，中间进待核验</td>
              </tr>
              <tr><td>题型 schema 版本</td><td>{view.schema_version}</td></tr>
            </tbody>
          </table>
          <p className="form-hint">
            两处与直觉不同：<strong>noul 题不返回 confidence</strong>（只有 0–1 的判定值，阈值在代码里）；
            <strong>score 不是 0–100 分</strong>，而是等级轴上的概率加权均值，展示分由代码归一化换算。
          </p>

          {view.notes.length > 0 && (
            <>
              <h5>使用说明</h5>
              {view.notes.map((note) => (
                <p className="form-hint" key={note}>· {note}</p>
              ))}
            </>
          )}
        </>
      )}
    </div>
  );
}

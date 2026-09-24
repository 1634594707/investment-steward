import { useEffect, useMemo, useRef, useState } from "react";
import type { CredentialRecord, InvestorProfile, ModelProfile, PersonalSettings, PersonalSettingsInput, PluginCapability, PluginCatalogEntry } from "@investment-steward/domain-contracts";
import type { CoreConnection, CoreRequest } from "../state/coreClient";
import { detailOf } from "../state/coreClient";
import { SyncSection } from "./SyncSection";
import { ConfirmDialog } from "../components/ConfirmDialog";
// F02/F03：今日取数健康独立成模块（设置页本已近 1900 行，长文件里再塞一个 248 行的区块不划算）。
import { FeedHealthSection } from "./FeedHealthSection";
// JV02（Jev 决策模型接入路线图 2026-09-21）：Jev 分区独立成模块，理由同上。
import { JevSection } from "./JevSection";
import { DEFAULT_UI_PREFS, saveUiPrefs, useUiPrefs, type UiPrefs, type Density, type MarketColors, type Theme } from "../shell/uiprefs";
import { useZoomPercent } from "../shell/zoom";
import { NAV_ALL } from "../shell/nav";
import { AVATAR_COLORS, useProfile } from "../shell/profile";

import { useFocusTrap } from "../components/useFocusTrap";
import "./settings.css";

interface Props {
  plugins: PluginCatalogEntry[];
  capabilities: PluginCapability[];
  capabilityCount: number;
  modelProfiles: ModelProfile[];
  credentials: CredentialRecord[];
  /** 通知投递通道状态（GET /notifications/channels）：展示各通道是否已配置。 */
  notifyChannels: Array<{ channel: string; kind: string; label: string; configured: boolean }>;
  /** 投资者画像（本机保存）：数据与隐私页签展示与编辑入口。 */
  investorProfile: InvestorProfile | null;
  onNavigate: (view: "extensions") => void;
  /** 打开「编辑画像」弹窗（复用首次引导组件，预填已保存值）。 */
  onEditProfile: () => void;
  /** 个人中心设置（Core）：null = 尚未保存过（表单回退本机镜像默认值）。 */
  personalSettings: PersonalSettings | null;
  /** 保存个人中心设置；返回保存后的完整记录，失败返回 null。 */
  onSavePersonalSettings: (input: PersonalSettingsInput) => Promise<PersonalSettings | null>;
  /** TopBar 头像点击的一次性定位请求：跳到「个人中心」分区。 */
  stabRequest: "personal" | null;
  onStabConsumed: () => void;
  /** H1-2 全量数据导出（JSON 下载）。 */
  onExportAll: () => Promise<{ exported_at: string } | null>;
  onCreateModelProfile: (input: { name: string; base_url: string; model: string; credential_ref: string; timeout_secs?: number | null }) => Promise<ModelProfile | null>;
  onUpdateModelProfile: (profileId: string, input: { name: string; base_url: string; model: string; credential_ref: string; timeout_secs?: number | null }) => Promise<ModelProfile | null>;
  onActivateModelProfile: (profileId: string) => Promise<boolean>;
  onTestModelProfile: (profileId: string) => Promise<ProbeResult | null>;
  /** v2.2 弹窗内草稿态探测：测连通性看延迟（只读，不落库、不写凭据）。 */
  onProbeModelConnection: (input: { base_url: string; model: string; credential_ref: string; timeout_secs?: number | null }) => Promise<ProbeResult | null>;
  /** v2.2 弹窗内草稿态探测：拉取服务商模型列表（只读，不落库、不写凭据）。 */
  onDiscoverModels: (input: { base_url: string; credential_ref: string; timeout_secs?: number | null }) => Promise<ProbeResult | null>;
  onDeleteModelProfile: (profileId: string) => Promise<boolean>;
  onUpsertCredential: (keyId: string, secret: string) => Promise<CredentialRecord | null>;
  onTestCredential: (keyId: string) => Promise<ProbeResult | null>;
  onDeleteCredential: (keyId: string) => Promise<boolean>;
  coreVersion?: string;
  schemaVersion?: string;
  connection?: CoreConnection;
  /** 存储位置管理（路线图 3.1 第 3 步）：Core 请求通道。未传时区块显示降级说明。 */
  coreRequest?: (req: CoreRequest) => Promise<{ status: number; data: unknown }>;
}

interface ProbeResult {
  ok: boolean;
  latency_ms: number;
  detail: string;
  /** 仅「拉取模型」返回：服务商 /models 的模型 id 列表。 */
  models?: string[];
}

/** 单实例密钥清单，key_id 与 `credential_store.SINGLE_INSTANCE_KEYS` 严格同源（G3-1）。不可凭空增减。 */
const CRED_KEYS: { keyId: string; name: string; note: string; placeholder: string }[] = [
  // 模型 API 密钥（model_api_key）不在本列表：模型配置弹窗内可直接粘贴明文自动入库，避免两处填写入口。
  { keyId: "macro_data_key", name: "宏观数据 FRED Key", note: "圣路易斯联储 FRED API · 宏观雷达美区指标（免费申请：fred.stlouisfed.org）", placeholder: "粘贴 32 位小写字母数字 FRED API key" },
  { keyId: "comtrade_key", name: "UN Comtrade API Key", note: "联合国商品贸易统计数据库 · 贸易流向与商品预览查询（兼容既有 key）", placeholder: "粘贴 Comtrade subscription key" },
  { keyId: "market_data_token", name: "行情数据源 Token", note: "Tushare Pro · read_market_data", placeholder: "粘贴 Token（32 位十六进制）" },
  { keyId: "notify_webhook", name: "通知渠道 Webhook", note: "钉钉机器人 · propose_notification", placeholder: "https://oapi.dingtalk.com/robot/send?access_token=…" },
  // JV02：Jev（TypeSafe System One）的密钥。与模型密钥是**两套独立协议**，不可互相顶替
  // （端点、计费主体都不同），因此单独一个槽位；Jev 分区只引用它，不在此处再开填写入口。
  { keyId: "jev_api_key", name: "Jev 决策模型密钥（TypeSafe）", note: "TypeSafe AI · Jev System One 决策判定（申请：console.typesafe.ai/keys）", placeholder: "粘贴 TypeSafe API key" },
];

/** 凭据引用脱敏展示：中间打码，避免明文密钥整串出现在界面上。 */
function maskRef(ref: string): string {
  if (ref.length <= 12) return ref;
  return `${ref.slice(0, 6)}…${ref.slice(-4)}`;
}

/** 服务商预设（cc-switch 式快填）：仅预填公开连接参数，密钥一律由用户自行粘贴；预设值可再编辑。
 *  DeepSeek 组合为本机已验证值；本地 One API 为本机 8002 自建网关（模型名按实际通道修改）。 */
const MODEL_PRESETS: { name: string; baseUrl: string; model: string; hint: string }[] = [
  { name: "官方免费AI", baseUrl: "https://svc-k7p4.18257.xyz", model: "deepseek-v4-flash", hint: "自建中转免费通道 · 限速并发 1 · 模型名按通道改" },
  { name: "DeepSeek 官方", baseUrl: "https://api.deepseek.com", model: "deepseek-v4-flash", hint: "官方直连 · 本机已验证" },
  { name: "OpenAI 官方", baseUrl: "https://api.openai.com/v1", model: "gpt-4o-mini", hint: "OpenAI 兼容端点" },
  { name: "本地 One API", baseUrl: "http://127.0.0.1:8002/v1", model: "deepseek-v4-flash", hint: "本机 8002 自建网关 · 模型名按通道改" },
];

/** 判断是否为直接粘贴的明文密钥（sk- 前缀，或 ≥24 位无空白的连续串；key_id 如 model_api_key 不会误判）。 */
function looksLikePlaintextKey(ref: string): boolean {
  if (/^sk-/.test(ref)) return true;
  return ref.length >= 24 && !/\s/.test(ref);
}

/** 解析方案级超时输入：空串 → null（用内置默认）；合法 → 30–1800 整数；非法 → "invalid"。 */
function parseTimeoutSecs(raw: string): number | null | "invalid" {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  if (!/^\d+$/.test(trimmed)) return "invalid";
  const value = Number(trimmed);
  if (!Number.isInteger(value) || value < 30 || value > 1800) return "invalid";
  return value;
}

/** 疑似示例/占位密钥（sk-test-1234567890 之类）：保存会覆盖真实密钥，红色提示但不阻止（真机反馈：示例 key 覆盖真实 key 导致 401）。 */
function looksLikePlaceholderKey(secret: string): boolean {
  return /sk-?test|sk-?xxx|example|your[_-]?api[_-]?key|占位|测试密钥/i.test(secret);
}

/** 插槽注册表（11 槽）：与后端 slots.py DEFINED_SLOTS / 设计稿 SLOTS 表同源。 */
const SLOTS: { slot: string; page: string; cap: number; level: string }[] = [
  { slot: "today.brief", page: "today", cap: 3, level: "L3" },
  { slot: "today.learning", page: "today", cap: 2, level: "L2" },
  { slot: "invest.market_view", page: "investment", cap: 1, level: "L3" },
  { slot: "invest.thesis", page: "investment", cap: 2, level: "L2" },
  { slot: "research.board", page: "research", cap: 3, level: "L3" },
  { slot: "review.plan", page: "review", cap: 2, level: "L2" },
  { slot: "notification.global", page: "settings", cap: 1, level: "L0" },
  { slot: "app.library", page: "library", cap: 4, level: "L3" },
  { slot: "app.macro", page: "macro", cap: 4, level: "L3" },
  { slot: "app.tactics", page: "tactics", cap: 4, level: "L3" },
  // P3-D01：游资雷达。与后端 slots.py DEFINED_SLOTS 同源，改动需两侧同步。
  { slot: "app.youzi", page: "youzi", cap: 4, level: "L3" },
];

type Stab = "personal" | "core" | "ext" | "ui" | "keys" | "models" | "jev" | "close" | "privacy" | "about";
const SETTING_GROUPS: Array<[Stab, string]> = [["personal", "个人中心"], ["core", "Core 与连接"], ["keys", "凭据与安全"], ["models", "模型配置"], ["jev", "Jev 决策模型"], ["privacy", "投资者档案"], ["ui", "UI 偏好"], ["close", "关闭行为"], ["ext", "扩展与插槽"], ["about", "关于与版本"]];

type Feedback = { kind: "ok" | "err"; text: string } | null;

/** 画像枚举展示标签：与 InvestorOnboarding 的 KNOWLEDGE_OPTIONS / MARKET_OPTIONS 同源，未知值原样显示。 */
const PROFILE_KNOWLEDGE_LABEL: Record<string, string> = {
  beginner: "入门：理解基本概念",
  intermediate: "中级：能读财报与逻辑",
  advanced: "进阶：熟悉组合与风控",
};
const PROFILE_MARKET_LABEL: Record<string, string> = {
  cn_stock: "A 股个股",
  cn_etf: "A 股 ETF / 指数",
  us_stock: "美港股",
  bond: "债券 / 固收",
  commodity: "商品 / 贵金属",
  fund: "公募基金",
};

/** 个人中心 · 风险偏好展示标签：仅作界面呈现，不参与任何计算。 */
const RISK_LABEL: Record<string, string> = {
  conservative: "保守型 · 本金优先",
  balanced: "平衡型 · 稳健增值",
  aggressive: "进取型 · 波动承受",
};

/** 个人中心 · 提醒节奏展示标签（值与后端 frequency 枚举同源）。 */
const FREQUENCY_LABEL: Record<string, string> = {
  realtime: "实时",
  daily: "每日汇总",
  weekly: "每周汇总",
};

/** 个人中心 · 头像上传统一压到 96px 居中方图（dataURL 不撑爆存储）。 */
function compressAvatar(file: File, onLoad: (dataUrl: string) => void): void {
  const reader = new FileReader();
  reader.onload = () => {
    const img = new Image();
    img.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = 96;
      canvas.height = 96;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      const size = Math.min(img.width, img.height);
      ctx.drawImage(img, (img.width - size) / 2, (img.height - size) / 2, size, size, 0, 0, 96, 96);
      onLoad(canvas.toDataURL("image/png"));
    };
    img.src = String(reader.result);
  };
  reader.readAsDataURL(file);
}

export function SettingsPage({
  plugins,
  capabilities,
  capabilityCount,
  modelProfiles,
  credentials,
  notifyChannels,
  investorProfile,
  onNavigate,
  onExportAll,
  onEditProfile,
  personalSettings,
  onSavePersonalSettings,
  stabRequest,
  onStabConsumed,
  onCreateModelProfile,
  onUpdateModelProfile,
  onActivateModelProfile,
  onTestModelProfile,
  onProbeModelConnection,
  onDiscoverModels,
  onDeleteModelProfile,
  onUpsertCredential,
  onTestCredential,
  onDeleteCredential,
  coreVersion = "—",
  schemaVersion = "—",
  connection = "stopped",
  coreRequest,
}: Props) {
  const [stab, setStab] = useState<Stab>("core");
  // —— 个人中心（Core 持久化；未保存过时表单回退本机镜像） ——
  const localProfile = useProfile();
  const [pcName, setPcName] = useState(localProfile.name);
  const [pcColor, setPcColor] = useState<string>(localProfile.color);
  const [pcAvatar, setPcAvatar] = useState<string | null>(localProfile.avatar);
  const [pcDefaultView, setPcDefaultView] = useState("today");
  const [pcNotify, setPcNotify] = useState<PersonalSettings["notify"]>({
    in_app_enabled: true,
    external_enabled: true,
    quiet_hours_enabled: false,
    quiet_start: "22:00",
    quiet_end: "08:00",
    frequency: "realtime",
  });
  const [pcRisk, setPcRisk] = useState("");
  const [pcTags, setPcTags] = useState("");
  const [pcBusy, setPcBusy] = useState(false);
  const [pcFeedback, setPcFeedback] = useState<Feedback>(null);
  const pcFileRef = useRef<HTMLInputElement | null>(null);

  // Core 返回已保存设置时覆盖表单（含保存回显与首次加载）。
  useEffect(() => {
    const s = personalSettings;
    if (!s) return;
    setPcName(s.display_name);
    setPcColor(s.avatar_color);
    setPcAvatar(s.avatar_data);
    setPcDefaultView(s.default_view);
    setPcNotify({ ...s.notify });
    setPcRisk(s.risk_profile);
    setPcTags(s.style_tags.join("、"));
  }, [personalSettings]);

  // TopBar 头像点击的一次性定位：切到「个人中心」分区后立即消费，避免重复触发。
  useEffect(() => {
    if (!stabRequest) return;
    setStab(stabRequest);
    onStabConsumed();
  }, [stabRequest, onStabConsumed]);

  async function savePersonalCenter() {
    const tags = pcTags.split(/[、,，;；\s]+/).map((tag) => tag.trim()).filter(Boolean).slice(0, 12);
    setPcBusy(true);
    setPcFeedback(null);
    try {
      const saved = await onSavePersonalSettings({
        display_name: pcName.trim() || "投资人",
        avatar_data: pcAvatar,
        avatar_color: pcColor,
        default_view: pcDefaultView,
        notify: pcNotify,
        risk_profile: pcRisk,
        style_tags: tags,
      });
      setPcFeedback(saved
        ? { kind: "ok", text: "个人中心设置已保存。" }
        : { kind: "err", text: "保存失败（Host 桥或 Core 未就绪），请重试。" });
    } catch {
      setPcFeedback({ kind: "err", text: "保存失败，请重试。" });
    } finally {
      setPcBusy(false);
    }
  }

  const uiPrefs = useUiPrefs();
  const [uiFeedback, setUiFeedback] = useState<Feedback>(null);
  function updateUiPrefs(next: UiPrefs) {
    const ok = saveUiPrefs(next);
    setUiFeedback(ok ? { kind: "ok", text: "偏好已保存到本机。" } : { kind: "err", text: "本机存储不可用，偏好未保存，请重试。" });
  }
  const zoomPercent = useZoomPercent();
  const [profileFeedback, setProfileFeedback] = useState<Record<string, Feedback>>({});
  const [profileBusy, setProfileBusy] = useState(false);
  const [credFeedback, setCredFeedback] = useState<Record<string, Feedback>>({});
  const [credInputs, setCredInputs] = useState<Record<string, string>>({});
  const [credBusy, setCredBusy] = useState<Record<string, boolean>>({});

  // 新增/编辑方案弹窗（editId 非空 = 编辑态，预填原值）
  const [schemeBusy, setSchemeBusy] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [closeBusy, setCloseBusy] = useState(false);
  const [closeFeedback, setCloseFeedback] = useState<string | null>(null);
  const [schemeOpen, setSchemeOpen] = useState(false);
  const [schemeReview, setSchemeReview] = useState(false);
  const [schemeFeedback, setSchemeFeedback] = useState<string | null>(null);
  const schemeDialog = useFocusTrap<HTMLElement>(schemeOpen && !schemeReview);
  const [editId, setEditId] = useState<string | null>(null);
  const [schemeName, setSchemeName] = useState("");
  const [schemeBaseUrl, setSchemeBaseUrl] = useState("");
  const [schemeModel, setSchemeModel] = useState("");
  const [schemeCredentialRef, setSchemeCredentialRef] = useState("");
  // v22 方案级模型调用超时（秒）：空串 = 用内置默认；推理型模型可放宽（30–1800）。
  const [schemeTimeout, setSchemeTimeout] = useState("");
  const [schemeError, setSchemeError] = useState<string | null>(null);
  // v2.2 弹窗内草稿态探测：拉取模型 + 测连通性（只读探测，不落库、不写凭据）。
  const [schemeModels, setSchemeModels] = useState<string[]>([]);
  const [schemeProbeBusy, setSchemeProbeBusy] = useState<"models" | "probe" | null>(null);
  const [schemeProbe, setSchemeProbe] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [pendingDelete, setPendingDelete] = useState<{ kind: "profile" | "credential"; id: string; label: string } | null>(null);
  const [closeAction, setCloseAction] = useState<"ask" | "tray" | "quit">("ask");

  useEffect(() => {
    window.stewardWin?.getRememberedCloseAction?.().then((action) => {
      if (action) setCloseAction(action);
    }).catch(() => {});
  }, []);

  // F1-2：方案弹窗（invest-modal）Esc 关闭。
  useEffect(() => {
    if (!schemeOpen || schemeReview) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && !schemeBusy) {
        setSchemeOpen(false);
        setEditId(null);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [schemeOpen, schemeBusy, schemeReview]);

  // v2.2：弹窗每次打开都清空上一次的模型列表与探测结论，避免展示过期结果。
  useEffect(() => {
    if (!schemeOpen) return;
    setSchemeModels([]);
    setSchemeProbe(null);
    setSchemeProbeBusy(null);
  }, [schemeOpen]);

  const activeProfileId = modelProfiles.find((p) => p.status === "使用中")?.profile_id ?? null;

  const stats = useMemo(() => {
    const enabled = plugins.filter((e) => e.installation?.state === "enabled");
    const disabled = plugins.filter((e) => e.installation && e.installation.state !== "enabled");
    const available = plugins.filter((e) => !e.installation);
    const networked = plugins.filter((e) => (e.manifest.network_allowlist?.length ?? 0) > 0);
    return { enabled: enabled.length, disabled: disabled.length, available: available.length, networked: networked.length };
  }, [plugins]);

  const slotRows = useMemo(() => {
    const targeted = new Map<string, number>();
    for (const entry of plugins) {
      if (entry.installation?.state !== "enabled") continue;
      const outputs = entry.resolved_outputs ?? entry.manifest.ui_slots ?? [];
      for (const output of outputs) targeted.set(output.slot, (targeted.get(output.slot) ?? 0) + 1);
    }
    return SLOTS.map((s) => {
      const t = targeted.get(s.slot) ?? 0;
      return { ...s, targeted: t, used: Math.min(t, s.cap), queued: Math.max(0, t - s.cap) };
    });
  }, [plugins]);

  const dataFlowRows = useMemo(() => {
    const capById = new Map(capabilities.map((c) => [c.capability_id, c]));
    return plugins.map((entry) => {
      const caps = entry.manifest.capabilities.map((id) => capById.get(id)).filter(Boolean) as PluginCapability[];
      const leaves = caps.some((c) => c.data_leaves_device);
      return { id: entry.manifest.plugin_id, name: entry.manifest.display_name, leaves };
    });
  }, [plugins, capabilities]);

  async function testProfile(profileId: string) {
    setProfileBusy(true);
    try {
      const result = await onTestModelProfile(profileId);
      if (result === null) {
        setProfileFeedback((current) => ({ ...current, [profileId]: { kind: "err", text: "测试请求失败（Host 桥或 Core 未就绪）" } }));
      } else {
        setProfileFeedback((current) => ({
          ...current,
          [profileId]: { kind: result.ok ? "ok" : "err", text: result.ok ? `已连通 · ${result.latency_ms}ms · ${result.detail}` : result.detail },
        }));
      }
  
    } catch { setProfileFeedback(current => ({ ...current, [profileId]: { kind: "err", text: "连接测试失败，请重试。" } })); }
    finally { setProfileBusy(false); }
  }

  async function activateProfile(profileId: string) {
    if (profileBusy) return;
    setProfileBusy(true);
    try {
      const ok = await onActivateModelProfile(profileId);
      setProfileFeedback(current => ({ ...current, [profileId]: { kind: ok ? "ok" : "err", text: ok ? "已切换为当前生效方案" : "切换失败，请重试。" } }));
    } catch { setProfileFeedback(current => ({ ...current, [profileId]: { kind: "err", text: "切换失败，请重试。" } })); }
    finally { setProfileBusy(false); }
  }

  /** 弹窗内公共：当前表单的超时；留空视为未设置（用内置默认），非法则返回 "invalid" 由调用方拦下。 */
  function schemeTimeoutSecs(): number | null | "invalid" {
    return parseTimeoutSecs(schemeTimeout);
  }

  /** v2.2「拉取模型」：读服务商 /models 列表（只读，不落库、不写凭据）。 */
  async function discoverSchemeModels() {
    if (schemeProbeBusy) return;
    const baseUrl = schemeBaseUrl.trim();
    if (!baseUrl) { setSchemeProbe({ kind: "err", text: "先填写端点 base_url。" }); return; }
    const timeoutSecs = schemeTimeoutSecs();
    if (timeoutSecs === "invalid") { setSchemeProbe({ kind: "err", text: "超时需为 30–1800 的整数秒，或留空使用默认。" }); return; }
    setSchemeProbeBusy("models");
    setSchemeProbe(null);
    try {
      const result = await onDiscoverModels({
        base_url: baseUrl,
        credential_ref: schemeCredentialRef.trim(),
        timeout_secs: timeoutSecs,
      });
      if (result === null) { setSchemeProbe({ kind: "err", text: "拉取请求失败（Host 桥或 Core 未就绪）。" }); return; }
      if (!result.ok) { setSchemeProbe({ kind: "err", text: result.detail }); return; }
      const models = result.models ?? [];
      setSchemeModels(models);
      setSchemeProbe(models.length
        ? { kind: "ok", text: `拉取到 ${models.length} 个模型，点下方标签选择。` }
        : { kind: "err", text: "端点返回了空模型列表。" });
    } catch { setSchemeProbe({ kind: "err", text: "拉取模型失败，请重试。" }); }
    finally { setSchemeProbeBusy(null); }
  }

  /** v2.2「测连通性」：用当前表单试调一次，显示真实延迟（只读，不落库、不写凭据）。 */
  async function probeSchemeConnection() {
    if (schemeProbeBusy) return;
    const baseUrl = schemeBaseUrl.trim();
    const model = schemeModel.trim();
    if (!baseUrl || !model) { setSchemeProbe({ kind: "err", text: "先填写端点 base_url 与模型名。" }); return; }
    const timeoutSecs = schemeTimeoutSecs();
    if (timeoutSecs === "invalid") { setSchemeProbe({ kind: "err", text: "超时需为 30–1800 的整数秒，或留空使用默认。" }); return; }
    setSchemeProbeBusy("probe");
    setSchemeProbe(null);
    try {
      const result = await onProbeModelConnection({
        base_url: baseUrl,
        model,
        credential_ref: schemeCredentialRef.trim(),
        timeout_secs: timeoutSecs,
      });
      if (result === null) { setSchemeProbe({ kind: "err", text: "测试请求失败（Host 桥或 Core 未就绪）。" }); return; }
      setSchemeProbe(result.ok
        ? { kind: "ok", text: `已连通 · ${result.latency_ms}ms · ${result.detail}` }
        : { kind: "err", text: result.detail });
    } catch { setSchemeProbe({ kind: "err", text: "连通性测试失败，请重试。" }); }
    finally { setSchemeProbeBusy(null); }
  }

  async function saveCredential(keyId: string) {
    const secret = credInputs[keyId]?.trim() ?? "";
    if (!secret) {
      setCredFeedback((current) => ({ ...current, [keyId]: { kind: "err", text: "先粘贴密钥内容" } }));
      return;
    }
    setCredBusy((current) => ({ ...current, [keyId]: true }));
    try {
      const record = await onUpsertCredential(keyId, secret);
      if (record === null) {
        setCredFeedback((current) => ({ ...current, [keyId]: { kind: "err", text: "保存失败（Host 桥或 Core 未就绪）" } }));
      } else {
        setCredInputs((current) => ({ ...current, [keyId]: "" }));
        setCredFeedback((current) => ({ ...current, [keyId]: { kind: "ok", text: `已保存（后端 ${record.backend}）` } }));
      }
  
    } catch { setCredFeedback(current => ({ ...current, [keyId]: { kind: "err", text: "保存失败，请重试。" } })); }
    finally { setCredBusy((current) => ({ ...current, [keyId]: false })); }
  }

  async function testCredentialKey(keyId: string) {
    setCredBusy((current) => ({ ...current, [keyId]: true }));
    try {
      const result = await onTestCredential(keyId);
      if (result === null) {
        setCredFeedback((current) => ({ ...current, [keyId]: { kind: "err", text: "测试请求失败（Host 桥或 Core 未就绪）" } }));
      } else {
        setCredFeedback((current) => ({ ...current, [keyId]: { kind: result.ok ? "ok" : "err", text: result.detail } }));
      }
  
    } catch { setCredFeedback(current => ({ ...current, [keyId]: { kind: "err", text: "连接测试失败，请重试。" } })); }
    finally { setCredBusy((current) => ({ ...current, [keyId]: false })); }
  }

  function reviewScheme() {
    if (!schemeName.trim() || !schemeBaseUrl.trim() || !schemeModel.trim() || !schemeCredentialRef.trim()) {
      setSchemeError("四项均为必填（名称 / 端点 / 模型 / 凭据引用）。");
      return;
    }
    if (parseTimeoutSecs(schemeTimeout) === "invalid") {
      setSchemeError("模型调用超时需为 30–1800 之间的整数秒，留空使用默认值。");
      return;
    }
    setSchemeError(null);
    setSchemeReview(true);
  }

  async function submitScheme() {
    if (schemeBusy) return;
    if (!schemeName.trim() || !schemeBaseUrl.trim() || !schemeModel.trim() || !schemeCredentialRef.trim()) {
      setSchemeError("四项均为必填（名称 / 端点 / 模型 / 凭据引用）。");
      return;
    }
    setSchemeBusy(true);
    try {
    // 直接粘贴明文密钥也可用：自动存入本机凭据库，方案只引用 key_id——
    // 密钥永远不落 model_profiles 表、不进审计明文。
    // v22 多方案独立凭据：不再固定覆盖 model_api_key（多方案共享同一槽会互相踢 key，
    // 表现为切换方案后另一方案 401）。改存入「不被其他方案引用」的空闲槽。
    let credentialRef = schemeCredentialRef.trim();
    let autoStoredNote: string | null = null;
    const isKnownKeyId = credentials.some((record) => record.key_id === credentialRef);
    if (!isKnownKeyId && looksLikePlaintextKey(credentialRef)) {
      const slot = pickModelKeySlot();
      const stored = await onUpsertCredential(slot, credentialRef);
      if (stored === null) {
        setSchemeError("密钥自动保存失败（Host 桥或 Core 未就绪），请稍后重新打开本弹窗粘贴密钥重试。");
        return;
      }
      autoStoredNote = `检测到明文密钥，已自动存入本机凭据库（${slot}，尾号 ${stored.last4}），方案将引用该 key_id。`;
      credentialRef = slot;
      setSchemeCredentialRef(credentialRef);
    } else if (!isKnownKeyId) {
      // 填的是凭据尾号（如 HGDI）：自动映射为对应 key_id，与服务端尾号兜底口径一致。
      const matched = credentials.find(
        (record) => record.last4 && record.last4.toUpperCase() === credentialRef.toUpperCase(),
      );
      if (matched) credentialRef = matched.key_id;
    }
    const parsedTimeout = parseTimeoutSecs(schemeTimeout);
    if (parsedTimeout === "invalid") {
      setSchemeError("模型调用超时需为 30–1800 之间的整数秒，留空使用默认值。");
      return;
    }
    const input = {
      name: schemeName.trim(),
      base_url: schemeBaseUrl.trim(),
      model: schemeModel.trim(),
      credential_ref: credentialRef,
      timeout_secs: parsedTimeout,
    };
    try {
      const saved = editId !== null
        ? await onUpdateModelProfile(editId, input)
        : await onCreateModelProfile(input);
      if (saved === null) {
        setSchemeError(`${autoStoredNote ? `${autoStoredNote} ` : ""}模型方案保存失败，请重试。`);
        return;
      }
    } catch (error) {
      setSchemeError(`${autoStoredNote ? `${autoStoredNote} ` : ""}${error instanceof Error ? error.message : "保存失败（未知错误）。"}`);
      return;
    }
    setSchemeFeedback(editId !== null ? "模型方案修改已保存。" : "模型方案已创建。");
    setSchemeReview(false);
    setSchemeOpen(false);
    setEditId(null);
    setSchemeName("");
    setSchemeBaseUrl("");
    setSchemeModel("");
    setSchemeCredentialRef("");
    setSchemeError(null);
    } catch { setSchemeError("保存失败，请重试。"); }
    finally { setSchemeBusy(false); }
  }

  /** v22 多方案独立凭据：为新粘贴的明文密钥挑一个「不被其他方案引用」的槽，避免覆盖别人的 key。 */
  function pickModelKeySlot(): string {
    const candidates = ["model_api_key", "model_api_key_2", "model_api_key_3", "model_api_key_4", "model_api_key_5"];
    const otherRefs = new Set(
      modelProfiles
        .filter((profile) => profile.profile_id !== editId)
        .map((profile) => profile.credential_ref),
    );
    return candidates.find((candidate) => !otherRefs.has(candidate)) ?? `model_api_key_${Date.now().toString(36)}`;
  }

  function openEditScheme(scheme: ModelProfile) {
    setSchemeReview(false);
    setSchemeFeedback(null);
    setEditId(scheme.profile_id);
    setSchemeName(scheme.name);
    setSchemeBaseUrl(scheme.base_url);
    setSchemeModel(scheme.model);
    setSchemeCredentialRef(scheme.credential_ref);
    setSchemeTimeout(scheme.timeout_secs != null ? String(scheme.timeout_secs) : "");
    setSchemeError(null);
    setSchemeOpen(true);
  }

  const originalScheme = modelProfiles.find(profile => profile.profile_id === editId);
  const schemeUnchanged = !!originalScheme && schemeName.trim() === originalScheme.name && schemeBaseUrl.trim() === originalScheme.base_url && schemeModel.trim() === originalScheme.model && schemeCredentialRef.trim() === originalScheme.credential_ref && parseTimeoutSecs(schemeTimeout) === (originalScheme.timeout_secs ?? null);

  const credRecordOf = (keyId: string) => credentials.find((c) => c.key_id === keyId);
  const credState = (keyId: string) => {
    const record = credRecordOf(keyId);
    if (!record) return "未配置";
    return `已配置 ·${record.last4 ? ` 尾号 ${record.last4} ·` : ""} ${record.backend === "os" ? "系统凭据" : "本机文件"}`;
  };

  const feedbackOf = (map: Record<string, Feedback>, id: string) => map[id] ?? null;

  return (
    <section className="page-view settings-page">
      <header className="settings-heading"><div><span className="kicker">系统治理</span><h2>设置与偏好</h2></div><span className={`set-core-status ${connection === "ready" ? "ok" : connection === "connecting" ? "wait" : ""}`}><i aria-hidden="true" />Core {coreVersion} · {connection === "ready" ? "已连接" : connection === "connecting" ? "连接中" : "未连接"}</span></header>

      <div className="set-layout">
        <nav className="set-nav" aria-label="设置分区">
          {SETTING_GROUPS.map(([id, label]) => <button key={id} className={`set-item ${stab === id ? "active" : ""}`} aria-current={stab === id ? "page" : undefined} onClick={() => setStab(id)}>{label}</button>)}
        </nav>
        <div className="settings-workspace">
          <h3 className="settings-group-title">{SETTING_GROUPS.find(([id]) => id === stab)?.[1]}</h3>
          {stab === "models" && schemeFeedback && <p role="status">{schemeFeedback}</p>}
          {stab === "personal" && (
            <div className="set-pane active">
              <div className="dl-block">
                <h4>身份与头像</h4>
                <p>显示名与头像用于顶栏、侧栏与档案。保存到本机 Core（SQLite），不上传任何远端；无图片时以底色 + 首字呈现。</p>
                <div className="pc-identity">
                  <span className="avatar avatar-top" style={pcAvatar ? undefined : { background: pcColor, color: "#0b1018" }} aria-hidden="true">
                    {pcAvatar ? <img src={pcAvatar} alt="" style={{ width: "100%", height: "100%", borderRadius: "50%", objectFit: "cover" }} /> : (pcName.trim() || "投资人").slice(0, 1)}
                  </span>
                  <div>
                    <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                      <button className="ghost-btn" disabled={pcBusy} onClick={() => pcFileRef.current?.click()}>上传图片</button>
                      {pcAvatar && <button className="ghost-btn" disabled={pcBusy} onClick={() => setPcAvatar(null)}>移除图片</button>}
                      <input ref={pcFileRef} type="file" accept="image/*" style={{ display: "none" }} onChange={(event) => { const file = event.target.files?.[0]; if (file) compressAvatar(file, setPcAvatar); event.target.value = ""; }} />
                    </div>
                    <div className="swatches">
                      {AVATAR_COLORS.map((colorOption) => (
                        <button key={colorOption} className={`swatch ${pcColor === colorOption ? "active" : ""}`} style={{ background: colorOption }} onClick={() => setPcColor(colorOption)} aria-label={`底色 ${colorOption}`} />
                      ))}
                    </div>
                  </div>
                </div>
                <label className="form-field">
                  <span>显示名</span>
                  <input value={pcName} maxLength={20} disabled={pcBusy} onChange={(event) => setPcName(event.target.value)} placeholder="怎么称呼你" />
                </label>
              </div>

              <div className="dl-block">
                <h4>默认落地页</h4>
                <p>启动应用时直接进入该页面（优先于「重启回到上次所在页」）。</p>
                <div className="pref-row" role="group" aria-label="默认落地页">
                  {NAV_ALL.map((navEntry) => (
                    <button key={navEntry.id} className={`tag-button ${pcDefaultView === navEntry.id ? "active" : ""}`} aria-pressed={pcDefaultView === navEntry.id} disabled={pcBusy} onClick={() => setPcDefaultView(navEntry.id)}>
                      {navEntry.label}
                    </button>
                  ))}
                </div>
              </div>

              <div className="dl-block">
                <h4>通知偏好</h4>
                <p>通道是否可用以「凭据与安全 → 通知投递通道」为准；站内开关与免打扰时段即时生效（通知在免打扰窗口内不外露、外部不投递）。「每日 / 每周汇总」仅作用于外部投递：每日 20:00 / 每周一 09:00 后的下次检查，把待投递提醒聚合成一条汇总发送（站内提醒不受该节奏影响）。</p>
                <div className="pref-row" role="group" aria-label="提醒通道与节奏">
                  {([
                    { key: "in_app_enabled", label: "站内提醒" },
                    { key: "external_enabled", label: "外部通道" },
                  ] as const).map(({ key, label }) => (
                    <button key={key} className={`tag-button ${pcNotify[key] ? "active" : ""}`} aria-pressed={pcNotify[key]} disabled={pcBusy} onClick={() => setPcNotify({ ...pcNotify, [key]: !pcNotify[key] })}>
                      {label}<small style={{ display: "block", marginTop: 2 }}>{pcNotify[key] ? "开启" : "关闭"}</small>
                    </button>
                  ))}
                  {([
                    { value: "realtime", label: "实时" },
                    { value: "daily", label: "每日汇总" },
                    { value: "weekly", label: "每周汇总" },
                  ] as { value: PersonalSettings["notify"]["frequency"]; label: string }[]).map((option) => (
                    <button key={option.value} className={`tag-button ${pcNotify.frequency === option.value ? "active" : ""}`} aria-pressed={pcNotify.frequency === option.value} disabled={pcBusy} onClick={() => setPcNotify({ ...pcNotify, frequency: option.value })}>
                      {option.label}
                    </button>
                  ))}
                </div>
                <div className="pref-row" role="group" aria-label="免打扰时段">
                  <button className={`tag-button ${pcNotify.quiet_hours_enabled ? "active" : ""}`} aria-pressed={pcNotify.quiet_hours_enabled} disabled={pcBusy} onClick={() => setPcNotify({ ...pcNotify, quiet_hours_enabled: !pcNotify.quiet_hours_enabled })}>
                    免打扰<small style={{ display: "block", marginTop: 2 }}>{pcNotify.quiet_hours_enabled ? "时段内不打扰" : "关闭"}</small>
                  </button>
                  {pcNotify.quiet_hours_enabled && (
                    <span className="pc-quiet">
                      <input type="time" value={pcNotify.quiet_start} disabled={pcBusy} aria-label="免打扰开始" onChange={(event) => setPcNotify({ ...pcNotify, quiet_start: event.target.value })} />
                      <span>至</span>
                      <input type="time" value={pcNotify.quiet_end} disabled={pcBusy} aria-label="免打扰结束" onChange={(event) => setPcNotify({ ...pcNotify, quiet_end: event.target.value })} />
                    </span>
                  )}
                </div>
              </div>

              <div className="dl-block">
                <h4>风险偏好与风格</h4>
                <p>仅作为研究答卷的背景参考，不参与任何计算、不生成买卖建议；完整的投资者画像在「投资者档案」分区维护。</p>
                <div className="pref-row" role="group" aria-label="风险偏好">
                  {(["", "conservative", "balanced", "aggressive"] as const).map((value) => (
                    <button key={value || "unset"} className={`tag-button ${pcRisk === value ? "active" : ""}`} aria-pressed={pcRisk === value} disabled={pcBusy} onClick={() => setPcRisk(value)}>
                      {value ? RISK_LABEL[value] : "未设置"}
                    </button>
                  ))}
                </div>
                <label className="form-field">
                  <span>风格标签（顿号 / 逗号分隔，最多 12 个）</span>
                  <input value={pcTags} disabled={pcBusy} onChange={(event) => setPcTags(event.target.value)} placeholder="指数定投、长期持有、低波动" />
                </label>
              </div>

              <div className="form-foot">
                <button className="primary-button" disabled={pcBusy} onClick={() => void savePersonalCenter()}>保存个人中心设置</button>
                {pcFeedback && <span className={`probe-note ${pcFeedback.kind}`} role={pcFeedback.kind === "err" ? "alert" : "status"}>{pcFeedback.text}</span>}
              </div>
            </div>
          )}

          {stab === "core" && (
            <div className="set-pane active">
              <div className="dl-block">
                <h4>运行状态</h4>
                <dl className="settings-connection"><div><dt>连接状态</dt><dd>{connection === "ready" ? "已连接" : connection === "connecting" ? "连接中" : "未连接"}</dd></div><div><dt>Core 版本</dt><dd>{coreVersion}</dd></div><div><dt>数据结构版本</dt><dd>{schemaVersion}</dd></div></dl>
              </div>
              <StorageLocationSection coreRequest={coreRequest} />
              <SyncSection coreRequest={coreRequest} />
            </div>
          )}
          {stab === "ext" && (
            <div className="set-pane active">
              <div className="dl-block">
                <h4>扩展</h4>
                <p>已安装 {capabilityCount} 个能力。</p>
                <table className="kv-table">
                  <tbody>
                    <tr><td>已启用能力</td><td>{stats.enabled}</td></tr>
                    <tr><td>已停用能力</td><td>{stats.disabled}</td></tr>
                    <tr><td>可安装（未安装）</td><td>{stats.available}</td></tr>
                    <tr><td>需要网络出口</td><td>{stats.networked}</td></tr>
                  </tbody>
                </table>
                <button className="primary-btn mt-22" onClick={() => onNavigate("extensions")}>
                  管理插件与插槽 <span>→</span>
                </button>
              </div>
              <div className="dl-block">
                <h4>插槽仪表盘</h4>
                <table className="kv-table">
                  <tbody>
                    {slotRows.map((row) => (
                      <tr key={row.slot}>
                        <td>{row.slot}</td>
                        <td>
                          {row.page} · {row.level} · {row.used}/{row.cap}
                          {row.queued > 0 && <span className="patch-tag ml-8">排队 {row.queued}</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {stab === "ui" && (
            <div className="set-pane active">
              <button className="text-button" disabled={uiPrefs.density === DEFAULT_UI_PREFS.density && uiPrefs.marketColors === DEFAULT_UI_PREFS.marketColors && uiPrefs.theme === DEFAULT_UI_PREFS.theme} onClick={() => updateUiPrefs({ ...DEFAULT_UI_PREFS })}>恢复默认偏好</button>
              {uiFeedback && <p role={uiFeedback.kind === "err" ? "alert" : "status"}>{uiFeedback.text}</p>}
              <div className="dl-block">
                <h4>信息密度</h4>
                <p>偏好仅保存在本机。</p>
                <div className="pref-row" role="group" aria-label="信息密度">
                  {([
                    { value: "comfortable", label: "舒适", hint: "默认 · 呼吸感优先" },
                    { value: "compact", label: "紧凑", hint: "高密度 · 长时间盯盘" },
                  ] as { value: Density; label: string; hint: string }[]).map((option) => (
                    <button
                      key={option.value}
                      className={`tag-button ${uiPrefs.density === option.value ? "active" : ""}`}
                      aria-pressed={uiPrefs.density === option.value}
                      onClick={() => updateUiPrefs({ ...uiPrefs, density: option.value })}
                    >
                      {option.label}<small style={{ display: "block", marginTop: 2 }}>{option.hint}</small>
                    </button>
                  ))}
                </div>
              </div>
              <div className="dl-block">
                <h4>主题（T01）</h4>
                <p>偏好仅保存在本机。浅色主题的数据图表配色沿用深色映射，不做主题漂移。</p>
                <div className="pref-row" role="group" aria-label="主题">
                  {([
                    { value: "dark", label: "暗色 · 墨绿", hint: "默认 · 金融终端质感" },
                    { value: "graphite", label: "暗色 · 石墨", hint: "中性灰底 · 冷蓝主色" },
                    { value: "paper", label: "浅色 · 日间", hint: "白底 · 压深语义色" },
                  ] as { value: Theme; label: string; hint: string }[]).map((option) => (
                    <button
                      key={option.value}
                      className={`tag-button ${uiPrefs.theme === option.value ? "active" : ""}`}
                      aria-pressed={uiPrefs.theme === option.value}
                      onClick={() => updateUiPrefs({ ...uiPrefs, theme: option.value })}
                    >
                      {option.label}<small style={{ display: "block", marginTop: 2 }}>{option.hint}</small>
                    </button>
                  ))}
                </div>
              </div>
              <div className="dl-block">
                <h4>涨跌语义色（D-F1）</h4>
                <div className="pref-row" role="group" aria-label="涨跌语义色">
                  {([
                    { value: "cn", label: "红涨绿跌", hint: "A 股市场惯例（默认）" },
                    { value: "legacy", label: "Mint 涨 / Amber 跌", hint: "应用视觉体系" },
                  ] as { value: MarketColors; label: string; hint: string }[]).map((option) => (
                    <button
                      key={option.value}
                      className={`tag-button ${uiPrefs.marketColors === option.value ? "active" : ""}`}
                      aria-pressed={uiPrefs.marketColors === option.value}
                      onClick={() => updateUiPrefs({ ...uiPrefs, marketColors: option.value })}
                    >
                      {option.label}<small style={{ display: "block", marginTop: 2 }}>{option.hint}</small>
                    </button>
                  ))}
                </div>
                <p className="pref-swatch" aria-hidden="true">
                  <span className="mkt-up">▲ 上涨示例 +2.31%</span>
                  <span className="mkt-down">▼ 下跌示例 -1.18%</span>
                </p>
              </div>
              <div className="dl-block">
                <h4>显示</h4>
                <table className="kv-table">
                  <tbody>
                    <tr><td>界面缩放</td><td>{zoomPercent !== null ? `${zoomPercent}%（Ctrl+= / Ctrl+- / Ctrl+0，桌面端）` : "浏览器预览下由系统控制"}</td></tr>
                    <tr><td>主题</td><td>{({ dark: "暗色 · 墨绿", graphite: "暗色 · 石墨", paper: "浅色 · 日间" } as Record<Theme, string>)[uiPrefs.theme]}（在上方「主题」区切换，即时生效）</td></tr>
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {stab === "close" && <div className="set-pane active">              <div className="dl-block">
                <h4>窗口关闭行为</h4>
                <p>配置点击窗口右上角关闭按钮时的响应动作：保留在后台托盘监控，或直接彻底退出。</p>
                <div className="pref-row" role="group" aria-label="窗口关闭行为">
                  {([
                    { value: "ask", label: "每次询问", hint: "关闭前确认" },
                    { value: "tray", label: "保留到托盘", hint: "后台守护 · 保持宏观预警" },
                    { value: "quit", label: "直接退出", hint: "结束全部进程释放内存" },
                  ] as { value: "ask" | "tray" | "quit"; label: string; hint: string }[]).map((option) => (
                    <button
                      key={option.value}
                      className={`tag-button ${closeAction === option.value ? "active" : ""}`}
                      aria-pressed={closeAction === option.value}
                      disabled={closeBusy || !window.stewardWin?.setRememberedCloseAction}
                      onClick={async () => {
                        if (!window.stewardWin?.setRememberedCloseAction || closeBusy) return;
                        setCloseBusy(true); setCloseFeedback(null);
                        try { await window.stewardWin.setRememberedCloseAction(option.value); setCloseAction(option.value); setCloseFeedback("关闭行为已保存。"); }
                        catch { setCloseFeedback("保存失败，请重试。"); }
                        finally { setCloseBusy(false); }
                      }}
                    >
                      {option.label}<small style={{ display: "block", marginTop: 2 }}>{option.hint}</small>
                    </button>
                  ))}
                </div>
              </div>
{closeFeedback && <p role="status">{closeFeedback}</p>}{!window.stewardWin && <p>关闭行为仅在桌面端可设置。</p>}</div>}

          {stab === "models" && <div className="set-pane active">              <div className="dl-block">
                <h4>模型服务 · 供应商方案</h4>
                <p>模型连接配置保存在本机，密钥单独存入凭据库。</p>
                {modelProfiles.length === 0 && <p className="scheme-empty">尚无模型方案。新增方案或使用服务商预设快填后，在「凭据与安全」粘贴密钥即可接通 AI 功能。</p>}
                {modelProfiles.length > 0 && activeProfileId === null && (
                  <p className="form-error" role="status">尚无「使用中」的模型方案：宏观分析、权重提议、信号解读、认知档案等 AI 功能将无法调用。点击方案左侧圆点设为使用中。</p>
                )}
                <div className="scheme-list">
                  {modelProfiles.map((scheme) => {
                    const feedback = feedbackOf(profileFeedback, scheme.profile_id);
                    return (
                      <div key={scheme.profile_id} className={`scheme-row ${scheme.status === "使用中" ? "active" : ""}`}>
                        <button
                          className="scheme-activate"
                          title="点击切换为当前使用"
                          aria-label={`启用模型方案${scheme.name}`} disabled={profileBusy || scheme.status === "使用中"}
                          onClick={() => void activateProfile(scheme.profile_id)}
                        >
                          <span className="sc-radio" aria-hidden="true" />
                        </button>
                        <div className="sc-main">
                          <b>{scheme.name}</b>
                          <small title={`${scheme.base_url} · ${scheme.model}`}>{scheme.base_url} · {scheme.model}</small>
                        </div>
                        <span className="sc-key" title={`凭据引用：${scheme.credential_ref}`}>凭据 {maskRef(scheme.credential_ref)}</span>
                        <span className={`key-status ${scheme.status === "使用中" ? "ok" : ""}`}>{scheme.status}</span>
                        {scheme.status !== "使用中" && (
                          <button
                            className="ghost-btn scheme-activate-btn"
                            disabled={profileBusy}
                            title="把该方案设为「使用中」（原使用中方案自动让位）"
                            onClick={() => void activateProfile(scheme.profile_id)}
                          >启用</button>
                        )}
                        <button className="ghost-btn" disabled={profileBusy} onClick={() => void testProfile(scheme.profile_id)}>测试连接</button>
                        <button className="ghost-btn" onClick={() => openEditScheme(scheme)}>编辑</button>
                        <button
                          className="ghost-btn"
                          title="以此方案为模板新建（凭据引用一并复制，不会复制密钥明文）"
                          onClick={() => {
                            setEditId(null);
                            setSchemeFeedback(null);
                            setSchemeName(`${scheme.name} 副本`);
                            setSchemeBaseUrl(scheme.base_url);
                            setSchemeModel(scheme.model);
                            setSchemeCredentialRef(scheme.credential_ref);
                            setSchemeTimeout(scheme.timeout_secs != null ? String(scheme.timeout_secs) : "");
                            setSchemeError(null);
                            setSchemeOpen(true);
                          }}
                        >复制</button>
                        <button className="ghost-btn danger" onClick={() => setPendingDelete({ kind: "profile", id: scheme.profile_id, label: scheme.name })}>删除</button>
                        {feedback && <span className={`probe-note ${feedback.kind}`}>{feedback.text}</span>}
                      </div>
                    );
                  })}
                </div>
                <button className="ghost-btn mt-12" onClick={() => { setEditId(null); setSchemeName(""); setSchemeBaseUrl(""); setSchemeModel(""); setSchemeCredentialRef(""); setSchemeTimeout(""); setSchemeOpen(true); setSchemeError(null); }}>＋ 新增方案</button>
              </div>

              {/* 模型用量原先挂在「凭据与安全」页签末尾（F01 落点选错），按「模型」找的人看不到；搬进模型配置。 */}
              <ModelUsageSection coreRequest={coreRequest} />

</div>}

          {/* JV02：Jev 是另一套协议（POST /systemone），与「模型配置」并列而不混入方案表。 */}
          {stab === "jev" && (
            <div className="set-pane active">
              <JevSection coreRequest={coreRequest} />
            </div>
          )}

          {stab === "keys" && (
            <div className="set-pane active">
              <div className="dl-block">
                <h4>数据源与通知密钥</h4>
                <p>单实例密钥直接填即可；保存后写入本机凭据库，响应只回尾号与存储后端，明文从不回传。行情 token / 钉钉 webhook 只做本机形态校验，不发外网请求，避免误触发与指纹泄漏。</p>
                <div className="key-list">
                  {CRED_KEYS.map(({ keyId, name, note, placeholder }) => {
                    const record = credRecordOf(keyId);
                    const feedback = feedbackOf(credFeedback, keyId);
                    // 已配置行：占位文案与按钮改为「替换」语义，明示直接粘贴新值即可覆盖，无需先清除。
                    const replacePlaceholder = record
                      ? record.last4
                        ? `输入新密钥替换当前密钥（尾号 ${record.last4}）`
                        : "输入新密钥替换当前已保存的密钥"
                      : placeholder;
                    return (
                      <div key={keyId} className="key-row">
                        <div className="key-name"><b>{name}</b><small>{note}</small></div>
                        <input
                          className="key-input"
                          type="password"
                          placeholder={replacePlaceholder}
                          aria-label={name}
                          value={credInputs[keyId] ?? ""}
                          onChange={(event) => setCredInputs((current) => ({ ...current, [keyId]: event.target.value }))}
                          style={looksLikePlaceholderKey(credInputs[keyId] ?? "") ? { borderColor: "var(--danger, #df897a)" } : undefined}
                        />
                        {looksLikePlaceholderKey(credInputs[keyId] ?? "") && (
                          <span className="probe-note err">这看起来是示例/占位密钥，保存会覆盖你已配置的真实密钥（真实 key 请从服务商控制台重新复制）</span>
                        )}
                        <span className={`key-status ${record ? "ok" : ""}`}>{record ? credState(keyId) : "未配置"}</span>
                        <button className="ghost-btn" disabled={credBusy[keyId] || !(credInputs[keyId] ?? "").trim()} onClick={() => void saveCredential(keyId)}>{record ? "替换" : "保存"}</button>
                        <button className="ghost-btn" disabled={credBusy[keyId]} onClick={() => void testCredentialKey(keyId)}>测试连接</button>
                        {record && <button className="ghost-btn danger" disabled={credBusy[keyId]} onClick={() => setPendingDelete({ kind: "credential", id: keyId, label: name })}>清除</button>}
                        {feedback && <span className={`probe-note ${feedback.kind}`}>{feedback.text}</span>}
                      </div>
                    );
                  })}
                </div>
              </div>

              <div className="key-note">
                <b>密钥边界（和「数据与隐私」同一套规则）</b>
                所有方案与单实例密钥只在界面录入，保存在本机凭据库（Windows 凭据管理器 / keychain），不写入配置文件、不进代码仓库。密钥对插件不可见 —— 插件声明 network_allowlist 才能出网，请求由 Core 代理并注入凭据，插件拿不到原文；密钥不进日志、不进证据账本、不进任何分享池制品。
              </div>

              <FeedHealthSection coreRequest={coreRequest} />

              <div className="dl-block">
                <h4>通知投递通道</h4>
                <p>提醒默认保持站内（顶部通知铃）。配置外部通道后，评估产生的提醒会尽力投递；任何失败都不影响站内提醒。</p>
                <table className="kv-table">
                  <tbody>
                    {notifyChannels.map((channel) => (
                      <tr key={channel.channel}>
                        <td>{channel.label}</td>
                        <td>
                          <span className={`key-status ${channel.configured ? "ok" : ""}`}>{channel.configured ? "已配置" : "未配置"}</span>
                        </td>
                      </tr>
                    ))}
                    {notifyChannels.length === 0 && (
                      <tr><td>通知通道</td><td>状态未知（Core 未就绪）</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {stab === "privacy" && (
            <div className="set-pane active">
              <div className="dl-block">
                <h4>投资者画像</h4>
                <p>画像仅保存在本机，作为研究答卷的背景参考，不用于生成任何买卖建议。可随时查看与更新。</p>
                {investorProfile ? (
                  <table className="kv-table">
                    <tbody>
                      <tr><td>投资目标</td><td>{investorProfile.investment_goal || "—"}</td></tr>
                      <tr><td>投资年限</td><td>{investorProfile.horizon_years != null ? `${investorProfile.horizon_years} 年` : "—"}</td></tr>
                      <tr><td>流动性需求</td><td>{investorProfile.liquidity_needs || "—"}</td></tr>
                      <tr><td>知识自评</td><td>{PROFILE_KNOWLEDGE_LABEL[investorProfile.knowledge_self_assessment] ?? (investorProfile.knowledge_self_assessment || "—")}</td></tr>
                      <tr><td>关注市场</td><td>{investorProfile.markets_and_assets.length ? investorProfile.markets_and_assets.map((m) => PROFILE_MARKET_LABEL[m] ?? m).join("、") : "—"}</td></tr>
                      <tr><td>最近更新</td><td>{investorProfile.updated_at}</td></tr>
                    </tbody>
                  </table>
                ) : (
                  <p>尚未建立画像。</p>
                )}
                <div className="mt-10">
                  <button className="primary-button" onClick={onEditProfile}>
                    {investorProfile ? "更新画像" : "建立画像"} <span>→</span>
                  </button>
                </div>
              </div>
              <div className="dl-block">
                <h4>本地优先</h4>
                <p>Core 只监听 loopback，每次启动生成随机访问令牌。你的持仓、投资原则与复盘记录默认不离开本机。</p>
                <button
                  className="ghost-btn mt-10"
                  onClick={async () => {
                    const data = await onExportAll();
                    if (data === null) {
                      setProfileFeedback((current) => ({ ...current, export: { kind: "err", text: "导出失败（Core 未就绪）" } }));
                      return;
                    }
                    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
                    const url = URL.createObjectURL(blob);
                    const link = document.createElement("a");
                    link.href = url;
                    link.download = `steward-export-${new Date().toISOString().slice(0, 10)}.json`;
                    link.click();
                    URL.revokeObjectURL(url);
                  }}
                >
                  导出全部数据（JSON）
                </button>
                {profileFeedback.export && <span className={`probe-note ${profileFeedback.export.kind}`}>{profileFeedback.export.text}</span>}
              </div>
              <div className="dl-block">
                <h4>数据去向</h4>
                <p>以下为插件声明的数据传输范围。</p>
                <table className="kv-table">
                  <tbody>
                    {dataFlowRows.length === 0 && <tr><td>—</td><td>暂无插件数据</td></tr>}
                    {dataFlowRows.map((row) => (
                      <tr key={row.id}>
                        <td>{row.name}</td>
                        <td className={row.leaves ? "flow-remote" : "flow-local"}>{row.leaves ? "有网络出口（按能力声明代理出网）" : "全部留在本机"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {stab === "about" && (
            <div className="set-pane active">
              <div className="dl-block">
                <h4>关于 STEWARD</h4>
                <p>投资管家 · 本地优先的投资研究工作台。插件签名校验（Ed25519）、插槽仲裁、证据账本与显式确认机制共同构成信任基座。</p>
                <table className="kv-table">
                  <tbody>
                    <tr><td>版本</td><td>{coreVersion}</td></tr>
                    <tr><td>架构</td><td>{connection === "ready" ? "Electron Host · Core API · 插件注册表" : "Core 未连接：仅显示本地壳"}</td></tr>
                    <tr><td>数据位置</td><td>本机（SQLite + 凭据库）</td></tr>
                  </tbody>
                </table>
              </div>
              <div className="dl-block">
                <h4>四层版本</h4>
                <p>插件与内核之间靠这四层对齐。任何一层不匹配，插件进入「可安装但不可用」，并说明缺什么，而不是静默失败。</p>
                <table className="kv-table">
                  <tbody>
                    <tr><td>Core Version</td><td>{coreVersion}</td></tr>
                    <tr><td>Core 状态</td><td>{connection === "ready" ? "已连接" : connection === "demo" ? "演示模式" : connection === "connecting" ? "正在连接" : "未连接"}</td></tr>
                    <tr><td>Plugin SDK Version</td><td>{schemaVersion}</td></tr>
                    <tr><td>Capability Schema</td><td>{schemaVersion}</td></tr>
                    <tr><td>Plugin Artifact</td><td>按插件独立（见扩展页）</td></tr>
                  </tbody>
                </table>
              </div>
              <div className="dl-block">
                <h4>制品版本</h4>
                <p>分享池里的每个制品带精确版本 tag 与完整内容哈希。Fork 后生成的是新制品，原制品不可变 —— 别人的结果不会因为你改参数而被改写。</p>
              </div>
            </div>
          )}
        </div>
      </div>

      {schemeOpen && !schemeReview && (
        <div className="confirm-overlay" onClick={() => { if (!schemeBusy) setSchemeOpen(false); }}>
          <aside ref={schemeDialog} className="invest-modal" role="dialog" aria-modal="true" aria-label={editId ? "编辑模型方案" : "新增模型方案"} onClick={(event) => event.stopPropagation()}>
            <header className="confirm-head">
              <span className="section-kicker">{editId !== null ? "EDIT MODEL SCHEME" : "NEW MODEL SCHEME"}</span>
              <h3>{editId !== null ? "编辑供应商方案" : "新增供应商方案"}</h3>
              <p>模型连接配置保存在本机，密钥单独存入凭据库。</p>
            </header>
            <div className="invest-form">
              {editId === null && (
                <div className="form-field">
                  <span>服务商预设（点击快填，均可再修改）</span>
                  <div className="pref-row" role="group" aria-label="服务商预设">
                    {MODEL_PRESETS.map((preset) => (
                      <button
                        key={preset.name}
                        type="button"
                        className="tag-button"
                        disabled={schemeBusy}
                        title={`${preset.baseUrl} · ${preset.model || "模型名待填"}`}
                        onClick={() => { setSchemeName(preset.name); setSchemeBaseUrl(preset.baseUrl); setSchemeModel(preset.model); }}
                      >
                        {preset.name}<small style={{ display: "block", marginTop: 2 }}>{preset.hint}</small>
                      </button>
                    ))}
                  </div>
                </div>
              )}
              <label className="form-field">
                <span>名称</span>
                <input type="text" disabled={schemeBusy} value={schemeName} onChange={(event) => setSchemeName(event.target.value)} placeholder="DeepSeek 官方" />
              </label>
              <label className="form-field">
                <span>端点 base_url</span>
                <input type="text" disabled={schemeBusy} value={schemeBaseUrl} onChange={(event) => setSchemeBaseUrl(event.target.value)} placeholder="https://api.deepseek.com" />
              </label>
              <div className="form-row">
                <label className="form-field">
                  <span>模型（可点「拉取模型」从端点获取，也可直接填写）</span>
                  <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <input
                      style={{ flex: 1, minWidth: 0 }}
                      type="text"
                      disabled={schemeBusy}
                      value={schemeModel}
                      onChange={(event) => { setSchemeModel(event.target.value); setSchemeProbe(null); }}
                      placeholder="deepseek-chat"
                    />
                    <button
                      type="button"
                      className="ghost-btn"
                      disabled={schemeBusy || schemeProbeBusy !== null}
                      title="读取该端点的 /models 模型列表（只读，不保存任何配置）"
                      onClick={() => void discoverSchemeModels()}
                    >{schemeProbeBusy === "models" ? "拉取中…" : "拉取模型"}</button>
                  </div>
                </label>
                <label className="form-field">
                  <span>凭据引用（直接粘贴 sk-… 密钥即可，按当前后端安全存储；填尾号也可自动匹配）</span>
                  <input type="password" autoComplete="off" disabled={schemeBusy} value={schemeCredentialRef} onChange={(event) => { setSchemeCredentialRef(event.target.value); setSchemeProbe(null); }} placeholder="粘贴 sk-… 密钥 / key_id（model_api_key）/ 凭据尾号" />
                </label>
              </div>
              {schemeModels.length > 0 && (
                <div className="form-field">
                  <span>端点返回的模型（点击填入上方「模型」）</span>
                  <div className="pref-row" role="group" aria-label="拉取到的模型">
                    {schemeModels.map((item) => (
                      <button
                        key={item}
                        type="button"
                        className="tag-button"
                        disabled={schemeBusy}
                        onClick={() => { setSchemeModel(item); setSchemeProbe(null); }}
                      >{item}</button>
                    ))}
                  </div>
                </div>
              )}
              <label className="form-field">
                <span>模型调用超时（秒，可选）：推理型模型生成慢时可放宽，30–1800；留空使用默认 150 秒</span>
                <input
                  type="text"
                  inputMode="numeric"
                  disabled={schemeBusy}
                  value={schemeTimeout}
                  onChange={(event) => setSchemeTimeout(event.target.value)}
                  placeholder="如 300（推理模型建议）；留空 = 默认"
                />
              </label>
              {schemeProbe && (
                <span className={`probe-note ${schemeProbe.kind}`} role={schemeProbe.kind === "err" ? "alert" : "status"}>{schemeProbe.text}</span>
              )}
              {schemeError && <p className="form-error" role="alert">{schemeError}</p>}
              <footer className="form-foot">
                <button
                  className="ghost-btn"
                  disabled={schemeBusy || schemeProbeBusy !== null}
                  title="用当前表单参数试调一次查看真实延迟；只读探测，不会保存方案或凭据"
                  onClick={() => void probeSchemeConnection()}
                >{schemeProbeBusy === "probe" ? "测试中…" : "测连通性"}</button>
                {editId !== null && (
                  <button
                    className="ghost-btn"
                    disabled={schemeBusy}
                    title="不修改任何配置，直接把该方案设为「使用中」（凭据沿用已保存的引用）"
                    onClick={() => { const target = editId; setSchemeOpen(false); setEditId(null); void activateProfile(target); }}
                  >启用此方案</button>
                )}
                <button className="secondary-button" disabled={schemeBusy} onClick={() => { setSchemeOpen(false); setEditId(null); }}>取消</button>
                <button className="primary-button" disabled={schemeBusy || schemeUnchanged} onClick={reviewScheme}>{schemeUnchanged ? "无修改" : "核对修改"}</button>
              </footer>
            </div>
          </aside>
        </div>
      )}
      {schemeOpen && schemeReview && (() => {
        const pendingPlaintext = looksLikePlaintextKey(schemeCredentialRef.trim()) && !credentials.some(record => record.key_id === schemeCredentialRef.trim());
        const pendingSlot = pendingPlaintext ? pickModelKeySlot() : null;
        return (
        <ConfirmDialog
          title={editId ? "确认修改模型方案" : "确认创建模型方案"}
          summary={pendingSlot ? `新密钥将保存到本机凭据库 ${pendingSlot}（独立槽位，不影响其他方案的密钥）；方案保存失败时，已保存的凭据仍保留。` : "确认以下模型连接配置。"}
          diffs={[
            { label: "名称", before: originalScheme?.name ?? "未创建", after: schemeName.trim() },
            { label: "端点", before: originalScheme?.base_url ?? "未设置", after: schemeBaseUrl.trim() },
            { label: "模型", before: originalScheme?.model ?? "未设置", after: schemeModel.trim() },
            { label: "凭据引用", before: originalScheme ? maskRef(originalScheme.credential_ref) : "未设置", after: pendingSlot ? `新密钥（内容隐藏） → ${pendingSlot}` : maskRef(schemeCredentialRef.trim()) },
          ]}
          requireNote={false}
          busy={schemeBusy}
          error={schemeError}
          confirmLabel="确认保存"
          cancelLabel="返回编辑"
          onConfirm={() => void submitScheme()}
          onCancel={() => setSchemeReview(false)}
        />
        );
      })()}
      {pendingDelete && (
        <ConfirmDialog
          title={pendingDelete.kind === "profile" ? "删除模型方案" : "清除本机凭据"}
          summary={pendingDelete.kind === "profile" ? "方案配置会从本机移除；已保存的凭据不会自动删除。" : "凭据原文将从本机凭据库删除，之后需要重新录入才能调用相关服务。"}
          diffs={[{ label: pendingDelete.kind === "profile" ? "方案" : "凭据", before: pendingDelete.label, after: "删除" }]}
          confirmLabel={pendingDelete.kind === "profile" ? "确认删除" : "确认清除"}
          busy={deleteBusy} error={deleteError}
          onConfirm={async () => { if (deleteBusy) return; setDeleteBusy(true); setDeleteError(null); try { const ok = pendingDelete.kind === "profile" ? await onDeleteModelProfile(pendingDelete.id) : await onDeleteCredential(pendingDelete.id); if (ok) setPendingDelete(null); else setDeleteError("删除失败，请重试。"); } catch { setDeleteError("删除失败，请重试。"); } finally { setDeleteBusy(false); } }}
          onCancel={() => { setPendingDelete(null); setDeleteError(null); }}
        />
      )}
    </section>
  );
}

// —— 存储位置（路线图 3.1 第 3 步）：查看 / 打开 / 迁移预览 / 迁移 / 校验 / 恢复默认 ——

interface StorageUsage {
  database_bytes: number;
  user_data: { path: string; bytes: number; files: number };
  artifacts: { path: string; bytes: number; files: number; inside_user_data: boolean };
  cache: { path: string; bytes: number; files: number; migrated: boolean; note: string };
  free_bytes: number;
}

interface StorageLayoutInfo {
  version: number;
  user_data: string;
  cache: string;
  artifacts: string;
  install: string | null;
  database_file: string;
  pointer_file: string;
  pointer_active: boolean;
  usage: StorageUsage;
}

/** 本地数据生命周期（8.6）：备份检查 / 完整性 / 保留规则,由 /storage/lifecycle 返回。 */
interface LifecycleInfo {
  backups: {
    directory: string;
    count: number;
    retention_keep: number;
    latest: { file: string; bytes: number; modified_at: string } | null;
    note: string;
  };
  integrity: { result: string; method: string };
  artifacts_breakdown: { data_snapshots: { count: number; path: string } };
  retention_rules: string[];
}

/** E03（桌面端升级路线图 2026-09-18）：备份清单条目与恢复/导入的响应形状。 */
interface BackupListItem {
  name: string;
  bytes: number;
  created_at: string;
  table_counts: Record<string, number | null>;
}
interface BackupsInfo {
  ok: boolean;
  directory: string;
  items: BackupListItem[];
  rollback_scope: string;
  note: string;
}
interface ImportTableReport {
  export_key: string;
  table: string;
  status: string;
  incoming: number;
  existing: number;
  conflicts: number;
  inserted?: number;
  skipped?: number;
}

interface StoragePreview {
  source: StorageUsage;
  needed_bytes: number;
  target_free_bytes: number | null;
  target_user_data: string;
  target_artifacts: string | null;
  problems: string[];
  feasible: boolean;
}

interface MigrateResult {
  ok: boolean;
  copied_files: number;
  total_bytes: number;
  source_user_data: string;
  target_user_data: string;
  pointer_file: string;
  old_directory_removed: boolean;
  restart_required: boolean;
}

interface VerifyResult {
  ok: boolean;
  checked_files: number;
  mismatches: string[];
  database: { ok: boolean; detail: string; path: string };
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

/** F01（桌面端升级路线图 2026-09-18）：模型用量——按方案聚合窗口内的调用与 token（GET /model-usage）。 */
interface ModelUsageSummary {
  profile_id: string | null;
  model: string;
  calls: number;
  ok_calls: number;
  timeouts: number;
  errors: number;
  retried_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  usage_missing: number;
  avg_latency_ms: number;
  max_latency_ms: number;
}

function ModelUsageSection({ coreRequest }: { coreRequest?: (req: CoreRequest) => Promise<{ status: number; data: unknown }> }) {
  const [window, setWindow] = useState<"7d" | "30d">("7d");
  const [summaries, setSummaries] = useState<ModelUsageSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function load(nextWindow: "7d" | "30d" = window) {
    if (!coreRequest) return;
    setBusy(true);
    setError(null);
    try {
      const result = await callModelUsage(nextWindow);
      setSummaries(result.summaries);
    } catch (err) {
      setError(err instanceof Error ? err.message : "模型用量读取失败");
    } finally {
      setBusy(false);
    }
  }

  async function callModelUsage(nextWindow: "7d" | "30d"): Promise<{ summaries: ModelUsageSummary[] }> {
    if (!coreRequest) throw new Error("当前环境没有 Core 请求通道。");
    const { status, data } = await coreRequest({ method: "GET", path: `/model-usage?window=${nextWindow}` });
    if (status >= 400) throw new Error(`Core 返回错误（HTTP ${status}）`);
    return data as { summaries: ModelUsageSummary[] };
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="dl-block">
      <h4>模型用量</h4>
      <p>按方案统计本机发起的模型调用（研报/方向/协同/追问/宏观等）；token 三项来自模型返回的 usage，缺失记「—」不按字数反推。</p>
      <div className="filter-row">
        <button className={`tag-button ${window === "7d" ? "active" : ""}`} onClick={() => { setWindow("7d"); void load("7d"); }}>最近 7 天</button>
        <button className={`tag-button ${window === "30d" ? "active" : ""}`} onClick={() => { setWindow("30d"); void load("30d"); }}>最近 30 天</button>
        <button className="ghost-btn" disabled={busy} onClick={() => void load()}>刷新</button>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      {summaries !== null && (
        summaries.length === 0 ? (
          <p className="scheme-empty">窗口内没有模型调用记录。</p>
        ) : (
          <table className="kv-table">
            <thead>
              <tr><th>模型</th><th>调用</th><th>成功/失败/超时</th><th>tokens（入/出/总）</th><th>平均耗时</th></tr>
            </thead>
            <tbody>
              {summaries.map((row) => (
                <tr key={`${row.profile_id ?? "none"}-${row.model}`}>
                  <td>{row.model}</td>
                  <td>{row.calls}{row.retried_calls > 0 ? `（含重试 ${row.retried_calls}）` : ""}</td>
                  <td>{row.ok_calls}/{row.errors}/{row.timeouts}</td>
                  <td>
                    {row.usage_missing > 0 && row.total_tokens === 0
                      ? "—（未返回用量）"
                      : `${row.prompt_tokens.toLocaleString()} / ${row.completion_tokens.toLocaleString()} / ${row.total_tokens.toLocaleString()}`}
                    {row.usage_missing > 0 && row.total_tokens > 0 ? `（${row.usage_missing} 次未返回）` : ""}
                  </td>
                  <td>{(row.avg_latency_ms / 1000).toFixed(1)}s（峰值 {(row.max_latency_ms / 1000).toFixed(1)}s）</td>
                </tr>
              ))}
            </tbody>
          </table>
        )
      )}
    </div>
  );
}

function StorageLocationSection({ coreRequest }: { coreRequest?: (req: CoreRequest) => Promise<{ status: number; data: unknown }> }) {
  const [layout, setLayout] = useState<StorageLayoutInfo | null>(null);
  const [lifecycle, setLifecycle] = useState<LifecycleInfo | null>(null);
  const [preview, setPreview] = useState<StoragePreview | null>(null);
  const [migrateResult, setMigrateResult] = useState<MigrateResult | null>(null);
  const [verifyResult, setVerifyResult] = useState<VerifyResult | null>(null);
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState<"load" | "preview" | "migrate" | "verify" | "reset" | "restore" | "import-preview" | "import-apply" | null>(null);
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  // E03：备份清单与「从某份恢复」；E02 的 /import/* 恢复入口接入设置页。
  const [backups, setBackups] = useState<BackupsInfo | null>(null);
  const [restoreTarget, setRestoreTarget] = useState<string | null>(null);
  const [importPreview, setImportPreview] = useState<ImportTableReport[] | null>(null);
  const [importUnsupported, setImportUnsupported] = useState<string[]>([]);
  const [importFileName, setImportFileName] = useState<string | null>(null);
  const [importSourceBody, setImportSourceBody] = useState<Record<string, unknown> | null>(null);
  const [importMode, setImportMode] = useState<"merge" | "replace">("merge");
  const bridge = typeof window !== "undefined" ? window.steward ?? null : null;

  async function call<T>(req: CoreRequest): Promise<T> {
    if (!coreRequest) throw new Error("当前环境没有 Core 请求通道。");
    const { status, data } = await coreRequest(req);
    if (status >= 400) {
      throw new Error(detailOf(data) ?? `Core 返回错误（HTTP ${status}）`);
    }
    return data as T;
  }

  async function load() {
    if (!coreRequest) return;
    setBusy("load");
    try {
      const [nextLayout, nextLifecycle, nextBackups] = await Promise.all([
        call<StorageLayoutInfo>({ method: "GET", path: "/storage/layout" }),
        call<LifecycleInfo>({ method: "GET", path: "/storage/lifecycle" }).catch(() => null),
        call<BackupsInfo>({ method: "GET", path: "/storage/backups" }).catch(() => null),
      ]);
      setLayout(nextLayout);
      setLifecycle(nextLifecycle);
      setBackups(nextBackups);
    } catch (error) {
      setFeedback({ kind: "err", text: error instanceof Error ? error.message : "读取存储布局失败" });
    } finally {
      setBusy(null);
    }
  }

  useEffect(() => { void load(); /* eslint-disable-line react-hooks/exhaustive-deps */ }, [coreRequest]);

  async function runPreview() {
    if (!coreRequest || !target.trim()) return;
    setBusy("preview");
    setFeedback(null);
    setMigrateResult(null);
    setVerifyResult(null);
    try {
      const result = await call<StoragePreview>({ method: "POST", path: "/storage/layout/migrate/preview", body: { target_user_data: target.trim() } });
      setPreview(result);
      setConfirmOpen(false);
    } catch (error) {
      setFeedback({ kind: "err", text: error instanceof Error ? error.message : "预览失败" });
    } finally {
      setBusy(null);
    }
  }

  async function runMigrate() {
    if (!coreRequest || !target.trim()) return;
    setBusy("migrate");
    setFeedback(null);
    try {
      const result = await call<MigrateResult>({ method: "POST", path: "/storage/layout/migrate", body: { target_user_data: target.trim() } });
      setMigrateResult(result);
      setPreview(null);
      setConfirmOpen(false);
      setFeedback({ kind: "ok", text: `迁移完成（${result.copied_files} 个文件）。旧目录未删除；需重启 Core 后新位置生效。` });
      await load();
    } catch (error) {
      setFeedback({ kind: "err", text: error instanceof Error ? error.message : "迁移失败（原目录未改动）" });
      setConfirmOpen(false);
    } finally {
      setBusy(null);
    }
  }

  async function runVerify() {
    if (!coreRequest) return;
    setBusy("verify");
    setFeedback(null);
    try {
      const result = await call<VerifyResult>({ method: "POST", path: "/storage/layout/verify" });
      setVerifyResult(result);
      setFeedback(result.ok
        ? { kind: "ok", text: `校验通过：${result.checked_files} 个文件哈希一致，数据库 ${result.database.detail}。` }
        : { kind: "err", text: `校验未通过：${result.mismatches.join("；") || result.database.detail}` });
    } catch (error) {
      setFeedback({ kind: "err", text: error instanceof Error ? error.message : "校验失败" });
    } finally {
      setBusy(null);
    }
  }

  async function runReset() {
    if (!coreRequest) return;
    setBusy("reset");
    setFeedback(null);
    try {
      const result = await call<MigrateResult & { migrated?: boolean }>({ method: "POST", path: "/storage/layout/reset-default" });
      setMigrateResult(result);
      setPreview(null);
      setConfirmOpen(false);
      setFeedback({ kind: "ok", text: result.migrated ? "已迁回默认位置；需重启 Core 生效。" : "已在默认位置，无需迁移。" });
      await load();
    } catch (error) {
      setFeedback({ kind: "err", text: error instanceof Error ? error.message : "恢复默认失败（原目录未改动）" });
    } finally {
      setBusy(null);
    }
  }

  /** E03：从备份文件恢复当前库（confirm 流程在 restoreTarget 非空时呈现）。 */
  async function runBackupRestore() {
    if (!coreRequest || !restoreTarget) return;
    setBusy("restore");
    setFeedback(null);
    try {
      const result = await call<{ ok: boolean; restored_from: string; rollback_point: string; note: string }>({
        method: "POST",
        path: `/storage/backups/${restoreTarget}/restore`,
      });
      setFeedback({ kind: "ok", text: `已从 ${result.restored_from} 恢复（整库回到该时点）。刷新应用后生效；回退点备份保留在备份目录。` });
      setRestoreTarget(null);
      await load();
    } catch (error) {
      setFeedback({ kind: "err", text: error instanceof Error ? error.message : "恢复失败（原数据未改动）" });
    } finally {
      setBusy(null);
    }
  }

  /** E02 接线：读取导出 JSON → /import/preview 逐表报告冲突与缺失。 */
  async function runImportPreview(file: File) {
    if (!coreRequest) return;
    setBusy("import-preview");
    setFeedback(null);
    try {
      const parsed = JSON.parse(await file.text()) as Record<string, unknown>;
      const result = await call<{ ok: boolean; tables: ImportTableReport[]; unsupported_keys: string[] }>({
        method: "POST",
        path: "/import/preview",
        body: parsed,
      });
      setImportPreview(result.tables);
      setImportUnsupported(result.unsupported_keys);
      setImportFileName(file.name);
      setImportSourceBody(parsed);
      setFeedback(null);
    } catch (error) {
      setImportPreview(null);
      setFeedback({ kind: "err", text: error instanceof Error ? error.message : "导出文件解析失败" });
    } finally {
      setBusy(null);
    }
  }

  /** E02 接线：确认后 /import/apply（后端强制先 rotate_backup，写审计）。 */
  async function runImportApply() {
    if (!coreRequest || !importPreview || importSourceBody === null) return;
    setBusy("import-apply");
    setFeedback(null);
    try {
      const result = await call<{ ok: boolean; backup_path: string; results: Array<{ table: string; inserted: number; skipped: number }> }>({
        method: "POST",
        path: "/import/apply",
        body: { data: importSourceBody, tables: importPreview.map((row) => row.table).filter((table) => importSourceBody[table] !== undefined), mode: importMode, confirm: true },
      });
      const total = result.results.reduce((sum, row) => sum + row.inserted, 0);
      setFeedback({ kind: "ok", text: `恢复完成：${total} 行写入（${importMode === "merge" ? "合并" : "整表覆盖"}）。回退点备份：${result.backup_path}。` });
      setImportPreview(null);
      setImportFileName(null);
      await load();
    } catch (error) {
      setFeedback({ kind: "err", text: error instanceof Error ? error.message : "恢复失败（恢复前备份已生成，可回退）" });
    } finally {
      setBusy(null);
    }
  }

  async function browseTarget() {
    if (!bridge?.requestFileSelection) return;
    const picked = await bridge.requestFileSelection({ directory: true });
    if (picked && picked.length > 0) setTarget(picked[0]!);
  }

  if (!coreRequest) {
    return (
      <div className="dl-block storage-location">
        <h4>存储位置</h4>
        <p className="storage-note">当前环境没有 Core 请求通道（未通过桌面端运行），存储位置管理不可用。</p>
      </div>
    );
  }

  return (
    <div className="dl-block storage-location">
      <h4>存储位置</h4>
      {layout ? (
        <dl className="settings-connection storage-paths">
          <div><dt>用户数据目录{layout.pointer_active ? "（自选）" : "（默认）"}</dt><dd title={layout.user_data}>{layout.user_data}</dd>
            <button className="ghost-btn" onClick={() => void bridge?.openPath(layout.user_data)}>打开目录</button></div>
          <div><dt>数据库</dt><dd title={layout.database_file}>{layout.database_file}</dd></div>
          <div><dt>占用</dt><dd>
            数据 {formatBytes(layout.usage.user_data.bytes)}（{layout.usage.user_data.files} 个文件）
            {layout.usage.artifacts.inside_user_data ? "" : ` · 制品 ${formatBytes(layout.usage.artifacts.bytes)}`}
            {" "}· 缓存 {formatBytes(layout.usage.cache.bytes)}（不迁移，自动重建）
          </dd></div>
        </dl>
      ) : (
        <p className="storage-note">{busy === "load" ? "正在读取存储布局…" : "存储布局未加载。"}</p>
      )}

      {lifecycle && (
        <dl className="settings-connection storage-paths storage-lifecycle">
          <div><dt>备份检查</dt><dd>
            {lifecycle.backups.count} 份（保留最近 {lifecycle.backups.retention_keep} 份）
            {lifecycle.backups.latest ? ` · 最新 ${lifecycle.backups.latest.file}（${formatBytes(lifecycle.backups.latest.bytes)}）` : " · 暂无备份文件"}
          </dd></div>
          <div><dt>数据库完整性</dt><dd>{lifecycle.integrity.result === "ok" ? `ok（${lifecycle.integrity.method}）` : lifecycle.integrity.result}</dd></div>
          <div><dt>数据快照</dt><dd>{lifecycle.artifacts_breakdown.data_snapshots.count} 个（内容寻址，不自动删除）</dd></div>
          <div><dt>保留规则</dt><dd><ul>{lifecycle.retention_rules.map((rule) => <li key={rule}>{rule}</li>)}</ul></dd></div>
        </dl>
      )}

      {backups && backups.items.length > 0 && (
        <div className="storage-report">
          <strong>备份（最近在前 · 整库快照：恢复将回滚全部表到该时点）</strong>
          <table className="kv-table">
            <tbody>
              {backups.items.map((item) => (
                <tr key={item.name}>
                  <td className="mono">{item.name}</td>
                  <td>{formatBytes(item.bytes)} · {new Date(item.created_at).toLocaleString()}</td>
                  <td>
                    {item.table_counts.ai_research_reports != null ? `研报 ${item.table_counts.ai_research_reports} 份` : ""}
                  </td>
                  <td>
                    <button
                      className="ghost-btn danger"
                      disabled={busy !== null}
                      onClick={() => setRestoreTarget(item.name)}
                    >
                      从此份恢复
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {bridge && <button className="ghost-btn" onClick={() => void bridge.openPath(backups.directory)}>打开备份目录</button>}
          {restoreTarget && (
            <div className="storage-confirm">
              <p><strong>确认从 {restoreTarget} 恢复？</strong></p>
              <p>{backups.rollback_scope}。恢复前会先自动备份当前数据（回退点），并写入审计。</p>
              <button className="primary-button" disabled={busy === "restore"} onClick={() => void runBackupRestore()}>{busy === "restore" ? "恢复中…" : "确认恢复"}</button>
              <button className="ghost-btn" onClick={() => setRestoreTarget(null)}>取消</button>
            </div>
          )}
        </div>
      )}

      <div className="storage-report">
        <strong>从导出文件恢复（JSON）</strong>
        <p className="storage-note">
          选择此前「全量数据导出」生成的 JSON 文件。先预览（逐表报告现有行数与主键冲突），确认后按表恢复；
          合并 = 保留现有主键行只补新行，整表覆盖 = 该表回到导出时点。恢复前会自动轮换备份。
        </p>
        <label className="file-pick">
          <span className="ghost-btn">{importFileName ? `已选文件：${importFileName}` : "选择导出 JSON 文件"}</span>
          <input
            type="file"
            accept="application/json,.json"
            aria-label="选择导出 JSON 文件"
            disabled={busy !== null}
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void runImportPreview(file);
              event.target.value = "";
            }}
          />
        </label>
        {importFileName && importPreview && (
          <>
            <table className="kv-table">
              <tbody>
                {importPreview.filter((row) => row.status !== "absent").map((row) => (
                  <tr key={row.table}>
                    <td className="mono">{row.table}</td>
                    <td>现有 {row.existing} 行 · 导入 {row.incoming} 行{row.conflicts > 0 ? ` · 主键冲突 ${row.conflicts}` : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {importUnsupported.length > 0 && (
              <p className="storage-problem">未登记键（不导入）：{importUnsupported.join("、")}</p>
            )}
            <div className="filter-row">
              <label className="filter-label">
                <input type="radio" checked={importMode === "merge"} onChange={() => setImportMode("merge")} /> 合并（只补缺失行）
              </label>
              <label className="filter-label">
                <input type="radio" checked={importMode === "replace"} onChange={() => setImportMode("replace")} /> 整表覆盖（回到导出时点）
              </label>
              <button className="primary-button" disabled={busy !== null} onClick={() => void runImportApply()}>
                {busy === "import-apply" ? "恢复中…" : `确认恢复（${importMode === "merge" ? "合并" : "整表覆盖"}）`}
              </button>
            </div>
          </>
        )}
      </div>

      <div className="storage-actions">
        <input value={target} onChange={(event) => setTarget(event.target.value)} placeholder="新用户数据目录的完整路径" />
        <button className="ghost-btn" onClick={() => void browseTarget()} disabled={!bridge?.requestFileSelection}>浏览…</button>
        <button className="ghost-btn" onClick={() => void runPreview()} disabled={busy !== null || !target.trim()}>{busy === "preview" ? "计算中…" : "预览迁移"}</button>
        <button className="ghost-btn" onClick={() => void runVerify()} disabled={busy !== null}>{busy === "verify" ? "校验中…" : "校验数据"}</button>
        <button className="ghost-btn danger" onClick={() => void runReset()} disabled={busy !== null}>{busy === "reset" ? "处理中…" : "恢复默认位置"}</button>
      </div>

      {preview && (
        <div className="storage-report">
          <strong>迁移预览{preview.feasible ? "（可执行）" : "（不可执行）"}</strong>
          <ul>
            <li>需复制约 {formatBytes(preview.needed_bytes)}</li>
            <li>目标剩余空间：{preview.target_free_bytes === null ? "未知" : formatBytes(preview.target_free_bytes)}</li>
            <li>缓存目录不迁移（按 TTL 与容量清理，迁移后自动重建）</li>
            {preview.problems.map((problem) => <li key={problem} className="storage-problem">{problem}</li>)}
          </ul>
          {preview.feasible && !confirmOpen && <button className="primary-button" onClick={() => setConfirmOpen(true)}>执行迁移</button>}
          {confirmOpen && (
            <div className="storage-confirm">
              <p><strong>确认迁移到 {preview.target_user_data}？</strong></p>
              <p>流程为临时复制 → 哈希校验 → 配置切换，之后需重启 Core。失败时原目录不会改动；旧目录在您确认前不会被删除。</p>
              <button className="primary-button" disabled={busy === "migrate"} onClick={() => void runMigrate()}>{busy === "migrate" ? "迁移中…" : "确认迁移"}</button>
              <button className="ghost-btn" onClick={() => setConfirmOpen(false)}>取消</button>
            </div>
          )}
        </div>
      )}

      {migrateResult && (
        <div className="storage-report">
          <strong>迁移结果</strong>
          <ul>
            <li>{migrateResult.copied_files} 个文件（{formatBytes(migrateResult.total_bytes)}）已复制到 {migrateResult.target_user_data}</li>
            <li>旧目录 {migrateResult.source_user_data} 保留（old_directory_removed: {String(migrateResult.old_directory_removed)}）</li>
            <li>位置指针：{migrateResult.pointer_file}</li>
          </ul>
          <button className="primary-button" onClick={() => void bridge?.restartCore()}>立即重启 Core</button>
        </div>
      )}

      {feedback && <span className={`probe-note ${feedback.kind}`} role={feedback.kind === "err" ? "alert" : "status"}>{feedback.text}</span>}
    </div>
  );
}

import { useState } from "react";
import type {
  CredentialRecord,
  InvestorProfile,
  ModelProfile,
  PersonalSettings,
  PersonalSettingsInput,
  PluginCapability,
  UpdateChannelView,
} from "@investment-steward/domain-contracts";
import type { CoreClient } from "../state/coreClient";
import { saveDefaultView, saveProfile } from "../shell/profile";

/**
 * B2（frontend-optimization-roadmap-2026-09-12）：系统域数据，自 AppShell 原样下沉。
 * 模型方案 / 凭据 / 个人中心设置 / 遥测（通道、审计、能力、插槽占用、数据截至、版本）。
 * loadAll 启动装载经同名 setter 回填；SettingsPage 的 props 仍由 AppShell 传递。
 * 投资者画像的「保存」动作（saveInvestorProfile）与引导弹窗状态耦合，保留在 AppShell。
 */
export function useSystem(client: CoreClient) {
  const [modelProfiles, setModelProfiles] = useState<ModelProfile[]>([]);
  const [credentials, setCredentials] = useState<CredentialRecord[]>([]);
  // 前后端对齐接线：插槽占用（GET /slots 权威仲裁，状态栏不再本地估算）；通知通道状态（GET /notifications/channels）。
  const [slotUsage, setSlotUsage] = useState<{ used: number; cap: number } | null>(null);
  const [notifyChannels, setNotifyChannels] = useState<Array<{ channel: string; kind: string; label: string; configured: boolean }>>([]);
  const [channels, setChannels] = useState<UpdateChannelView[]>([]);
  // B02（桌面端升级路线图 2026-09-18）：审计流不再进 boot/系统域——ExtensionsPage 经 client 按页自取。
  const [capabilities, setCapabilities] = useState<PluginCapability[]>([]);
  const [capabilityCount, setCapabilityCount] = useState(0);
  // 状态栏「数据截至」：`/health` data_as_of（各数据域最新观测时间）。
  const [dataAsOf, setDataAsOf] = useState<Record<string, string | null | undefined> | null>(null);
  const [coreVersion, setCoreVersion] = useState("—");
  const [schemaVersion, setSchemaVersion] = useState("—");
  // E3 投资者画像：GET 返回 null（首次）时弹出引导；PUT 成功后消除。
  const [investorProfile, setInvestorProfile] = useState<InvestorProfile | null>(null);
  // 个人中心设置（Core 持久化）：显示名/头像/默认落地页/通知偏好/风险偏好。
  const [personalSettings, setPersonalSettings] = useState<PersonalSettings | null>(null);

  // —— G3 凭据库与模型多方案：直连本机凭据库 /model-profiles /credentials ——
  async function createModelProfile(input: { name: string; base_url: string; model: string; credential_ref: string; timeout_secs?: number | null }): Promise<ModelProfile | null> {
    const response = await client.request<ModelProfile>({ method: "POST", path: "/model-profiles", body: input });
    if (response.status >= 400) {
      // 透出服务端原因（如 405=Core 进程是旧版本未含该端点），不再吞成笼统失败。
      throw new Error((response.data as { detail?: string } | null)?.detail ?? `创建失败（HTTP ${response.status}）`);
    }
    setModelProfiles((current) => [...current, response.data]);
    return response.data;
  }

  async function updateModelProfile(profileId: string, input: { name: string; base_url: string; model: string; credential_ref: string; timeout_secs?: number | null }): Promise<ModelProfile | null> {
    const response = await client.request<ModelProfile>({ method: "PUT", path: `/model-profiles/${profileId}`, body: input });
    if (response.status >= 400) {
      throw new Error((response.data as { detail?: string } | null)?.detail ?? `保存失败（HTTP ${response.status}；405/404 通常意味着 Core 子进程还是旧版本，请完全退出并重新启动桌面端）`);
    }
    setModelProfiles((current) => current.map((item) => (item.profile_id === profileId ? response.data : item)));
    return response.data;
  }

  async function activateModelProfile(profileId: string): Promise<boolean> {
    const response = await client.request<ModelProfile>({ method: "POST", path: `/model-profiles/${profileId}/activate` });
    if (response.status >= 400) return false;
    setModelProfiles((current) => current.map((item) => (item.profile_id === profileId ? response.data : { ...item, status: "未启用" })));
    return true;
  }

  async function testModelProfile(profileId: string): Promise<{ ok: boolean; latency_ms: number; detail: string } | null> {
    const response = await client.request<{ ok: boolean; latency_ms: number; detail: string }>({ method: "POST", path: `/model-profiles/${profileId}/test` });
    if (response.status >= 400) return null;
    return response.data;
  }

  /** v2.2 弹窗内草稿态探测：测连通性看延迟（只读，不落库、不写凭据）。 */
  async function probeModelConnection(input: { base_url: string; model: string; credential_ref: string; timeout_secs?: number | null }): Promise<{ ok: boolean; latency_ms: number; detail: string; models?: string[] } | null> {
    const response = await client.request<{ ok: boolean; latency_ms: number; detail: string; models?: string[] } | null>({ method: "POST", path: "/model-profiles/probe", body: input });
    if (response.status >= 400) return null;
    return response.data;
  }

  /** v2.2 弹窗内草稿态探测：拉取服务商模型列表（只读，不落库、不写凭据）。 */
  async function discoverModels(input: { base_url: string; credential_ref: string; timeout_secs?: number | null }): Promise<{ ok: boolean; latency_ms: number; detail: string; models?: string[] } | null> {
    const response = await client.request<{ ok: boolean; latency_ms: number; detail: string; models?: string[] }>({ method: "POST", path: "/model-profiles/discover-models", body: input });
    if (response.status >= 400) return null;
    return response.data;
  }

  async function deleteModelProfile(profileId: string): Promise<boolean> {
    const response = await client.request<unknown>({ method: "DELETE", path: `/model-profiles/${profileId}` });
    if (response.status >= 400) return false;
    setModelProfiles((current) => current.filter((item) => item.profile_id !== profileId));
    return true;
  }

  async function upsertCredential(keyId: string, secret: string): Promise<CredentialRecord | null> {
    const response = await client.request<CredentialRecord>({ method: "PUT", path: `/credentials/${keyId}`, body: { secret } });
    if (response.status >= 400) return null;
    setCredentials((current) => {
      const exists = current.some((item) => item.key_id === keyId);
      return exists ? current.map((item) => (item.key_id === keyId ? response.data : item)) : [...current, response.data];
    });
    return response.data;
  }

  async function testCredential(keyId: string): Promise<{ ok: boolean; latency_ms: number; detail: string } | null> {
    const response = await client.request<{ ok: boolean; latency_ms: number; detail: string }>({ method: "POST", path: `/credentials/${keyId}/test` });
    if (response.status >= 400) return null;
    return response.data;
  }

  async function deleteCredential(keyId: string): Promise<boolean> {
    const response = await client.request<unknown>({ method: "DELETE", path: `/credentials/${keyId}` });
    if (response.status >= 400) return false;
    setCredentials((current) => current.filter((item) => item.key_id !== keyId));
    return true;
  }

  // —— 个人中心设置 ——
  /** Core 为准，本机镜像同步（头像/名字离线可见 + 启动落地页首帧生效）。 */
  function applyPersonalSettings(settings: PersonalSettings): void {
    setPersonalSettings(settings);
    saveProfile({ name: settings.display_name, color: settings.avatar_color, avatar: settings.avatar_data });
    saveDefaultView(settings.default_view);
  }

  async function savePersonalSettings(input: PersonalSettingsInput): Promise<PersonalSettings | null> {
    const response = await client.request<PersonalSettings>({ method: "PUT", path: "/personal/settings", body: input });
    if (response.status >= 400 || !response.data) return null;
    applyPersonalSettings(response.data);
    return response.data;
  }

  return {
    modelProfiles, setModelProfiles,
    credentials, setCredentials,
    slotUsage, setSlotUsage,
    notifyChannels, setNotifyChannels,
    channels, setChannels,
    capabilities, setCapabilities,
    capabilityCount, setCapabilityCount,
    dataAsOf, setDataAsOf,
    coreVersion, setCoreVersion,
    schemaVersion, setSchemaVersion,
    investorProfile, setInvestorProfile,
    personalSettings, setPersonalSettings,
    createModelProfile, updateModelProfile, activateModelProfile, testModelProfile,
    probeModelConnection, discoverModels, deleteModelProfile,
    upsertCredential, testCredential, deleteCredential,
    applyPersonalSettings, savePersonalSettings,
  };
}

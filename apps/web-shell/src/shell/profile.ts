import { useEffect, useState } from "react";

/** 个人中心（本地优先）：名字/头像仅存本机 localStorage，不上传任何远端。
 *  TopBar（编辑入口）与 Sidebar（档案 chip）通过 useProfile 共享并实时同步。 */
export const PROFILE_KEY = "steward.profile";
/** 个人中心「默认落地页」的本机镜像：启动首帧同步读取，避免等 Core 返回才跳转。 */
export const DEFAULT_VIEW_KEY = "steward.default-view";
export const AVATAR_COLORS = ["#378add", "#7f77dd", "#1d9e75", "#d85a30", "#e24b4a", "#8fd3c1"] as const;

export interface Profile { name: string; color: string; avatar: string | null; }

const PROFILE_EVENT = "steward-profile-changed";

export function loadProfile(): Profile {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(PROFILE_KEY) ?? "");
    if (raw && typeof raw === "object") {
      const obj = raw as Record<string, unknown>;
      return {
        name: typeof obj.name === "string" && obj.name.trim() ? obj.name : "投资人",
        color: typeof obj.color === "string" ? obj.color : AVATAR_COLORS[0],
        avatar: typeof obj.avatar === "string" ? obj.avatar : null,
      };
    }
  } catch { /* 首次使用无存档 */ }
  return { name: "投资人", color: AVATAR_COLORS[0], avatar: null };
}

export function saveProfile(next: Profile): void {
  try { localStorage.setItem(PROFILE_KEY, JSON.stringify(next)); } catch { /* 存储不可用时仅本会话生效 */ }
  window.dispatchEvent(new CustomEvent(PROFILE_EVENT));
}

/** 读取个人中心设置的默认落地页；未设置返回 null（回退「回到上次所在页」）。 */
export function loadDefaultView(): string | null {
  try {
    const saved = localStorage.getItem(DEFAULT_VIEW_KEY);
    return saved && saved.trim() ? saved : null;
  } catch { return null; }
}

export function saveDefaultView(view: string): void {
  try { localStorage.setItem(DEFAULT_VIEW_KEY, view); } catch { /* 存储不可用时仅本会话生效 */ }
}

/** 订阅个人资料：同窗口自定义事件 + 跨窗口 storage 事件双通道同步。 */
export function useProfile(): Profile {
  const [profile, setProfile] = useState<Profile>(loadProfile);
  useEffect(() => {
    const sync = () => setProfile(loadProfile());
    window.addEventListener(PROFILE_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(PROFILE_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);
  return profile;
}

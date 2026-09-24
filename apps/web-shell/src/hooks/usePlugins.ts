import { useState } from "react";
import type { CandleSeries, PluginCatalogEntry } from "@investment-steward/domain-contracts";
import { detailOf, classifyCoreError, type CoreClient } from "../state/coreClient";
import { toast } from "../state/toastStore";

/**
 * B2（frontend-optimization-roadmap-2026-09-12）：插件生命周期域，自 AppShell 原样下沉。
 * 安装/停用（乐观更新带回滚）/升级（健康检查失败自动回滚旧版本）/撤销。
 * 跨域联动经 deps 参数化：行情插件启停走 useCandles（缓存/竞态守卫与页面同源），
 * 宏观雷达启用后重拉快照（useMacro.reload），研读图书馆启停后经 state/libraryRefresh 请求重载书架（A01）。
 */
export interface UsePluginsDeps {
  /** 行情插件启用后：跟随首支持仓重拉日 K（useCandles.selectInstrument）。 */
  selectInstrument: (instrument: string) => Promise<void>;
  /** 行情插件停用：清空当前 K 线序列（useCandles.setCandles）。 */
  setCandles: (series: CandleSeries | null) => void;
  /** 首支持仓的标的编码（无持仓时 undefined，跳过重拉）。 */
  getFirstHoldingInstrument: () => string | undefined;
  /** 宏观雷达插件一键启用成功后立即重拉各国快照（useMacro.reload）。 */
  onMacroPluginInstalled: () => void;
  /** 研读图书馆插件启停后请求重载书架（A01：由 LibraryPage 的 useLibrary 实例响应）。 */
  onLibraryPluginInstalled: () => void;
}

export function usePlugins(client: CoreClient, deps: UsePluginsDeps) {
  const [plugins, setPlugins] = useState<PluginCatalogEntry[]>([]);
  const [pluginBusy, setPluginBusy] = useState<string | null>(null);
  const [pluginMessage, setPluginMessage] = useState<string | null>(null);

  async function changePlugin(pluginId: string, action: "install" | "disable") {
    if (pluginBusy) return false;
    setPluginBusy(pluginId);
    setPluginMessage(null);
    // F2-2 乐观启停（停用路径）：先本地翻状态，失败回滚；安装路径有后续行情拉取，保持 busy 流程。
    const previousPlugins = action === "disable" ? plugins : null;
    if (action === "disable") {
      setPlugins((current) => current.map((entry) => (entry.manifest.plugin_id === pluginId && entry.installation ? { ...entry, installation: { ...entry.installation, state: "disabled" as const } } : entry)));
    }
    try {
      const response = await client.request<PluginCatalogEntry["installation"]>({
        method: "POST",
        path: `/plugins/${pluginId}/${action}`,
      });
      if (response.status >= 400) {
        throw new Error((response.data as { detail?: string } | null)?.detail ?? "插件操作失败");
      }
      const installation = response.data;
      setPlugins((current) => current.map((entry) => (entry.manifest.plugin_id === pluginId ? { ...entry, installation } : entry)));
      if (pluginId === "official.cn-market-data" && action === "disable") deps.setCandles(null);
      if (pluginId === "official.cn-market-data" && action === "install") {
        const firstInstrument = deps.getFirstHoldingInstrument();
        // B2：行情插件启用后的重拉走 useCandles.selectInstrument（缓存/竞态守卫与页面同源）。
        if (firstInstrument) void deps.selectInstrument(firstInstrument);
      }
      setPluginMessage(action === "install" ? `${pluginId} 已安装并启用` : `${pluginId} 已停用`);
      toast.ok(action === "install" ? `${pluginId} 已安装并启用` : `${pluginId} 已停用。`);
      // 宏观雷达启用后立即重拉各国快照（此前全部 409），数据实拉一次即落库缓存。
      if (pluginId === "official.macro-radar" && action === "install") deps.onMacroPluginInstalled();
      if (pluginId === "official.reading-library") deps.onLibraryPluginInstalled();
      return true;
    } catch (error) {
      // F2-2 乐观启停失败：回滚到操作前目录状态。
      if (action === "disable" && previousPlugins) setPlugins(previousPlugins);
      const message = error instanceof Error ? error.message : "插件操作失败";
      setPluginMessage(message);
      toast.err(`插件操作失败：${message}`);
      return false;
    } finally {
      setPluginBusy(null);
    }
  }

  async function updatePlugin(pluginId: string, targetVersion: string) {
    if (pluginBusy) return false;
    setPluginBusy(pluginId);
    setPluginMessage(null);
    try {
      const response = await client.request<PluginCatalogEntry["installation"]>({
        method: "POST",
        path: `/plugins/${pluginId}/update`,
        body: { target_version: targetVersion },
      });
      if (response.status >= 400) {
        // 409 = 健康检查未通过已回滚，保留旧版本（阶段 9 事务语义），展示给用户而非静默。
        throw new Error((response.data as { detail?: string } | null)?.detail ?? `插件更新失败（HTTP ${response.status}）`);
      }
      const installation = response.data;
      setPlugins((current) => current.map((entry) => (entry.manifest.plugin_id === pluginId ? { ...entry, installation } : entry)));
      setPluginMessage(`${pluginId} 已更新到 v${installation?.release_version ?? targetVersion}`);
      toast.ok(`${pluginId} 已更新到 v${installation?.release_version ?? targetVersion}，旧版本已保留。`);
      return true;
    } catch (error) {
      setPluginMessage(error instanceof Error ? error.message : "插件更新失败");
      toast.err(`插件更新失败：${error instanceof Error ? error.message : "未知错误"}（旧版本保持可用）`);
      return false;
    } finally {
      setPluginBusy(null);
    }
  }

  async function revokePlugin(pluginId: string) {
    if (pluginBusy) return false;
    setPluginBusy(pluginId);
    setPluginMessage(null);
    try {
      const response = await client.request<PluginCatalogEntry["installation"]>({
        method: "POST",
        path: `/plugins/${pluginId}/revoke`,
      });
      if (response.status >= 400) {
        throw new Error((response.data as { detail?: string } | null)?.detail ?? `插件撤销失败（HTTP ${response.status}）`);
      }
      const installation = response.data;
      setPlugins((current) => current.map((entry) => (entry.manifest.plugin_id === pluginId ? { ...entry, installation } : entry)));
      setPluginMessage(`${pluginId} 已撤销：停止产生新输出；历史 Evidence 保留可继续读取。`);
      toast.ok(`${pluginId} 已撤销：停止产生新输出，历史证据保留可读。`);
      return true;
    } catch (error) {
      setPluginMessage(error instanceof Error ? error.message : "插件撤销失败");
      toast.err(`插件撤销失败：${error instanceof Error ? error.message : "未知错误"}`);
      return false;
    } finally {
      setPluginBusy(null);
    }
  }

  return { plugins, setPlugins, pluginBusy, pluginMessage, changePlugin, updatePlugin, revokePlugin };
}

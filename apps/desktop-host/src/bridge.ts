/**
 * A02（架构改进路线图 2026-09-25）：宿主桥暴露判定。
 *
 * 判定依据是**生成的**桥清单（`src/generated/bridgeManifest.ts`），不再在 main.ts 里
 * 手写正则。默认拒绝：清单里没有「方法 + 路径」组合一律拒绝，`bridge-exposure.json`
 * 里的豁免项同理不在此清单内。
 *
 * 清单生成链路：
 *   Core 运行时路由表（scripts/route_inventory.py）
 *     + apps/desktop-host/bridge-exposure.json（显式声明）
 *     → scripts/generate_bridge_manifest.mjs → src/generated/bridgeManifest.ts
 */
import { BRIDGE_MANIFEST, type BridgeRoute } from "./generated/bridgeManifest.js";

interface CompiledRoute extends BridgeRoute {
  regex: RegExp;
}

const COMPILED: CompiledRoute[] = BRIDGE_MANIFEST.routes.map((route) => ({
  ...route,
  regex: new RegExp(route.pattern),
}));

/** 方法 + 路径是否允许经 Host Bridge 转发到 Core。 */
export function isBridgeAllowed(method: string, pathname: string): boolean {
  const upper = method.toUpperCase();
  for (const route of COMPILED) {
    if (route.method === upper && route.regex.test(pathname)) return true;
  }
  return false;
}

/** 该路径上实际存在的方法（拒绝时用于给出可操作的提示，而不是只说「没权限」）。 */
export function allowedMethodsFor(pathname: string): string[] {
  const methods = new Set<string>();
  for (const route of COMPILED) {
    if (route.regex.test(pathname)) methods.add(route.method);
  }
  return [...methods].sort();
}

export function bridgeManifestSize(): number {
  return COMPILED.length;
}

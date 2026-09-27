#!/usr/bin/env node
/* A02（架构改进路线图 2026-09-25）：桌面宿主桥暴露清单的唯一生成源。
 *
 * 旧做法：宿主 `main.ts` 里手写一条 600+ 字符的大正则，检查脚本再用「按源码文字定位
 * 正则字面量」的方式把它抠出来，并按参数名猜样例路径。问题有三：
 *   1. 两处各自维护——历史上已两次「脚本全绿、打包版 502」；
 *   2. 只比路径、不比方法，`GET /x` 与 `POST /x` 无法区分；
 *   3. 依赖源码写法，Core 侧一重构（挂到 APIRouter、拆子模块）检查就失准。
 *
 * 新做法（默认拒绝 + 显式声明）：
 *   1. 路由真相 = Core **运行时路由表**（`scripts/route_inventory.py` 实例化 create_app 后枚举）；
 *   2. 暴露范围 = 宿主显式声明 `apps/desktop-host/bridge-exposure.json`；
 *   3. 本脚本合并二者，生成 `apps/desktop-host/src/generated/bridgeManifest.ts` 供 main.ts 消费；
 *   4. 任何 Core 路由既不在 allow 也不在 exempt → 直接失败，不再等到打包版才发现。
 *
 * 用法：
 *   node scripts/generate_bridge_manifest.mjs            # 生成/更新清单
 *   node scripts/generate_bridge_manifest.mjs --check    # 只校验是否与当前源码一致（CI）
 */
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const exposurePath = resolve(repoRoot, "apps/desktop-host/bridge-exposure.json");
const manifestPath = resolve(repoRoot, "apps/desktop-host/src/generated/bridgeManifest.ts");

/** 不经宿主桥的端点：既非业务路由，也非渲染层可调用。 */
const NON_BUSINESS = new Set(["/openapi.json"]);

function resolvePython() {
  const candidates = [
    process.env.STEWARD_CORE_PYTHON,
    join(repoRoot, ".venv", "Scripts", "python.exe"),
    join(repoRoot, ".venv", "bin", "python"),
  ].filter(Boolean);
  const found = candidates.find((item) => existsSync(item));
  return found ?? "python";
}

/** 从 Core 运行时路由表取真相（方法 / 路径模板）。 */
function loadCoreRoutes() {
  const tmp = mkdtempSync(join(tmpdir(), "bridge-routes-"));
  const out = join(tmp, "routes.json");
  execFileSync(
    resolvePython(),
    [resolve(repoRoot, "scripts", "route_inventory.py"), "--out", out],
    { cwd: repoRoot, stdio: ["ignore", "ignore", "inherit"] },
  );
  const inventory = JSON.parse(readFileSync(out, "utf8"));
  return inventory.routes.filter((route) => !NON_BUSINESS.has(route.path));
}

/** 把 FastAPI 路径模板编译成锚定正则：`{x}` 单段，`{x:path}` 跨段。 */
function templateToPattern(template) {
  // 注意：转义字符类里**不含** `{`/`}`——花括号要先保留下来做参数替换，替换完才安全。
  // （曾把 `{}` 一起转义，导致 `\{report_id\}` 被当成字面量、所有带参路由都匹配不上。）
  const escaped = template.replace(/[.*+?^$()|[\]\\]/g, "\\$&");
  const withParams = escaped.replace(/\{([^}]+)\}/g, (_match, raw) => {
    const name = String(raw);
    return name.endsWith(":path") ? ".+" : "[^/]+";
  });
  return `^${withParams}$`;
}

const key = (route) => `${route.method} ${route.path}`;

function validate(coreRoutes, exposure) {
  const problems = [];
  const coreKeys = new Set(coreRoutes.map(key));
  const declared = new Map();

  for (const entry of exposure.allow ?? []) {
    declared.set(key(entry), "allow");
    if (!coreKeys.has(key(entry))) {
      problems.push(`allow 里的 ${key(entry)} 在 Core 路由表里不存在（Core 已改名/删除？）`);
    }
  }
  for (const entry of exposure.exempt ?? []) {
    if (!String(entry.reason ?? "").trim()) {
      problems.push(`exempt 里的 ${key(entry)} 未写明豁免理由`);
    }
    declared.set(key(entry), "exempt");
    if (!coreKeys.has(key(entry))) {
      problems.push(`exempt 里的 ${key(entry)} 在 Core 路由表里不存在`);
    }
  }
  for (const route of coreRoutes) {
    if (!declared.has(key(route))) {
      problems.push(
        `Core 路由 ${key(route)} 未在 bridge-exposure.json 登记（默认拒绝 → 打包版会 502）`,
      );
    }
  }
  return problems;
}

function renderManifest(coreRoutes, exposure) {
  const allowed = new Set((exposure.allow ?? []).map(key));
  const routes = coreRoutes
    .filter((route) => allowed.has(key(route)))
    .map((route) => ({
      method: route.method,
      path: route.path,
      pattern: templateToPattern(route.path),
    }))
    .sort((a, b) => (a.path === b.path ? a.method.localeCompare(b.method) : a.path.localeCompare(b.path)));

  const body = routes
    .map(
      (route) =>
        `  { method: ${JSON.stringify(route.method)}, path: ${JSON.stringify(route.path)}, pattern: ${JSON.stringify(route.pattern)} },`,
    )
    .join("\n");

  return `/* eslint-disable */
// 本文件由 scripts/generate_bridge_manifest.mjs 生成，请勿手工修改。
//
// 生成源：Core 运行时路由表（scripts/route_inventory.py）+ apps/desktop-host/bridge-exposure.json
// 语义：默认拒绝——只有这里列出的「方法 + 路径模板」才能经 Host Bridge 转发到 Core。
// 更新方式：改 bridge-exposure.json 后运行 node scripts/generate_bridge_manifest.mjs。

export interface BridgeRoute {
  method: string;
  path: string;
  /** 锚定正则（由路径模板编译）：{x} → 单段，{x:path} → 跨段。 */
  pattern: string;
}

export const BRIDGE_MANIFEST: { version: number; routes: BridgeRoute[] } = {
  version: ${exposure.version ?? 1},
  routes: [
${body}
  ],
};
`;
}

const checkOnly = process.argv.includes("--check");
const coreRoutes = loadCoreRoutes();
const exposure = JSON.parse(readFileSync(exposurePath, "utf8"));
const problems = validate(coreRoutes, exposure);
if (problems.length) {
  console.error(`桥暴露声明与 Core 路由表不一致（${problems.length} 项）：`);
  for (const item of problems) console.error(`  - ${item}`);
  console.error(
    "\n修法：在 apps/desktop-host/bridge-exposure.json 的 allow 里登记新端点；" +
      "确属桌面端不用的，登记到 exempt 并写明理由；Core 已删除的条目要从 allow/exempt 移除。",
  );
  process.exit(1);
}

const rendered = renderManifest(coreRoutes, exposure);
if (checkOnly) {
  const current = existsSync(manifestPath) ? readFileSync(manifestPath, "utf8") : "";
  if (current !== rendered) {
    console.error(
      "生成清单与当前源码不一致（bridgeManifest.ts 已过期）。请运行：\n" +
        "  node scripts/generate_bridge_manifest.mjs",
    );
    process.exit(1);
  }
  console.log(
    `桥清单一致：Core 路由 ${coreRoutes.length} 条，暴露 ${(exposure.allow ?? []).length} 条，豁免 ${(exposure.exempt ?? []).length} 条。`,
  );
  process.exit(0);
}

mkdirSync(dirname(manifestPath), { recursive: true });
writeFileSync(manifestPath, rendered, "utf8");
console.log(
  `已生成桥清单 → ${manifestPath}\n  Core 路由 ${coreRoutes.length} 条；暴露 ${(exposure.allow ?? []).length} 条；豁免 ${(exposure.exempt ?? []).length} 条。`,
);

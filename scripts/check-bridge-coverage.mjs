/* A02（架构改进路线图 2026-09-25）：桌面宿主桥暴露清单的校验器。
 *
 * 旧脚本的做法有两个结构性缺陷，本文件整体替换：
 *   1. 从 main.ts 源码里**按文字**抠出大正则字面量再编译——宿主一改写法（变量改名、
 *      换成正则常量、拆成函数）检查就静默失准；历史上已两次「脚本全绿、打包版 502」。
 *   2. 只比路径、不比方法，`GET /x` 与 `POST /x` 无法区分；且按参数名猜样例路径。
 *
 * 新做法：直接消费 `apps/desktop-host/src/generated/bridgeManifest.ts` 的同一份数据
 * （由 scripts/generate_bridge_manifest.mjs 生成，源头是 Core 运行时路由表 + 显式暴露声明），
 * 并补齐三类断言：
 *   A. 清单本身与 Core 路由表一致（调用生成器的 --check）；
 *   B. 正面：清单内的「方法 + 路径」放行；负面：错方法、多一段、未公开路由、豁免端点全部拒绝；
 *   C. 前端源码里真实出现的请求路径必须全部被清单覆盖（不再依赖手抄清单）。
 */
import { execFileSync } from "node:child_process";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const manifestPath = resolve(repoRoot, "apps/desktop-host/src/generated/bridgeManifest.ts");
const exposurePath = resolve(repoRoot, "apps/desktop-host/bridge-exposure.json");

const failures = [];
const fail = (message) => failures.push(message);

// ---------------------------------------------------------------------------
// A. 清单与 Core 路由表一致（生成器负责；这里只透传结果）
// ---------------------------------------------------------------------------
try {
  execFileSync(
    process.execPath,
    [resolve(repoRoot, "scripts/generate_bridge_manifest.mjs"), "--check"],
    { cwd: repoRoot, stdio: ["ignore", "ignore", "inherit"] },
  );
} catch {
  fail("桥清单与 Core 路由表/暴露声明不一致（见上方生成器输出）");
}

// ---------------------------------------------------------------------------
// 读清单（与 main.ts 消费的是同一份数据）
// ---------------------------------------------------------------------------
const manifestSource = readFileSync(manifestPath, "utf8");
const routeRe =
  /\{ method: "([A-Z]+)", path: "((?:[^"\\]|\\.)*)", pattern: "((?:[^"\\]|\\.)*)" \}/g;
const routes = [];
let match;
while ((match = routeRe.exec(manifestSource))) {
  routes.push({
    method: match[1],
    path: JSON.parse(`"${match[2]}"`),
    regex: new RegExp(JSON.parse(`"${match[3]}"`)),
  });
}
if (!routes.length) throw new Error("未从生成的桥清单里解析出任何路由");

const exposure = JSON.parse(readFileSync(exposurePath, "utf8"));
const isAllowed = (method, pathname) =>
  routes.some((route) => route.method === method && route.regex.test(pathname));

/** 路径模板 → 具体请求路径（清单里的 pattern 已能直接匹配样例，这里只为造真实形状）。 */
const sampleOf = (name) =>
  name.startsWith("plugin_id")
    ? "official.cn-market-data"
    : name.startsWith("symbol")
      ? "600519"
      : name.startsWith("job_id")
        ? "job-1"
        : "abc123";

const concrete = (template) =>
  template.replace(/\{([^}]+)\}/g, (_all, raw) => sampleOf(String(raw).split(":")[0]));

// ---------------------------------------------------------------------------
// B. 正面与负面断言
// ---------------------------------------------------------------------------
const METHODS = ["GET", "POST", "PUT", "DELETE"];

// 同一具体路径可能合法地声明多个方法（如 GET /decisions 与 POST /decisions 都是 Core 端点）。
// 负例必须只针对「该路径未声明的方法」，否则会把合法组合误报成方法串味。
const declaredMethods = new Map();
for (const route of routes) {
  const path = concrete(route.path);
  if (!declaredMethods.has(path)) declaredMethods.set(path, new Set());
  declaredMethods.get(path).add(route.method);
}

for (const route of routes) {
  const path = concrete(route.path);
  if (!isAllowed(route.method, path)) {
    fail(`清单自相矛盾：${route.method} ${route.path}（实际请求 ${path}）未被自身放行`);
  }
  // 负面 1：同路径的其它方法必须被拒（旧实现只比路径，这里是最实质的增强）。
  const declared = declaredMethods.get(path) ?? new Set();
  for (const method of METHODS) {
    if (declared.has(method)) continue;
    const via = routes.filter((item) => item.method === method && item.regex.test(path));
    if (!via.length) continue;
    // 参数化模板天然覆盖字面量路径（如 GET /evidence/{evidence_id} 覆盖 /evidence/dedup）：
    // 桥按「方法 + 路径形状」放行，这不是方法串味。只有字面量模板方法不符才算缺陷。
    const overlapOnly = via.every(
      (item) => item.path.includes("{") && concrete(item.path) !== path,
    );
    if (overlapOnly) continue;
    fail(`方法未配对：${method} ${path} 被放行，但清单只声明了 ${[...declared].join("/")}`);
  }
  // 负面 2：多一段路径必须被**该路由自己的**模式拒绝（旧脚本曾因丢 `$` 锚点变成前缀匹配而漏判）。
  // 例外：`{x:path}` 这类跨段模板本就允许含斜杠（如 /evidence/news/{symbol:path}）。
  if (!route.path.includes(":path")) {
    const deeper = `${path}/__extra__`;
    if (route.regex.test(deeper)) {
      fail(`路径未锚定：${route.method} ${route.path} 的模式匹配了 ${deeper}（应只匹配自身形状）`);
    }
  }
}

for (const entry of exposure.exempt ?? []) {
  const path = concrete(entry.path);
  if (isAllowed(entry.method, path)) {
    fail(`豁免失效：${entry.method} ${path} 声明为不通桥，却被清单放行`);
  }
}
if (isAllowed("GET", "/definitely-not-a-route")) {
  fail("未公开路由被放行：默认拒绝失效");
}

// ---------------------------------------------------------------------------
// C. 前端源码里真实出现的请求必须被覆盖（替代手抄清单）
// ---------------------------------------------------------------------------
const webSrc = resolve(repoRoot, "apps/web-shell/src");

function collectFiles(dir) {
  const found = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "node_modules" || entry.name.startsWith(".")) continue;
      found.push(...collectFiles(full));
    } else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
      found.push(full);
    }
  }
  return found;
}

/** 找到包住 `path:` 的那个对象字面量，从中读 method。 */
function enclosingObject(text, index) {
  let depth = 0;
  let start = -1;
  for (let i = index; i >= 0; i -= 1) {
    const ch = text[i];
    if (ch === "}") depth += 1;
    else if (ch === "{") {
      if (depth === 0) {
        start = i;
        break;
      }
      depth -= 1;
    }
  }
  if (start < 0) return null;
  depth = 0;
  for (let i = start; i < text.length; i += 1) {
    const ch = text[i];
    if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) return text.slice(start, i + 1);
    }
  }
  return null;
}

/** 解析 `path:` 之后的字符串字面量（支持模板串）。返回 null 表示无法定形。 */
function readPathLiteral(text, index) {
  const rest = text.slice(index);
  const quote = rest.match(/^\s*([`"'])/);
  if (!quote) return null;
  const mark = quote[1];
  let cursor = index + quote[0].length;
  let value = "";
  while (cursor < text.length) {
    const ch = text[cursor];
    if (ch === "\\") {
      value += text[cursor + 1] ?? "";
      cursor += 2;
      continue;
    }
    if (mark === "`" && ch === "$" && text[cursor + 1] === "{") {
      let depth = 0;
      let end = cursor + 1;
      for (; end < text.length; end += 1) {
        if (text[end] === "{") depth += 1;
        else if (text[end] === "}") {
          depth -= 1;
          if (depth === 0) break;
        }
      }
      const expr = text.slice(cursor + 2, end);
      // 路径段一定紧跟在 `/` 后面。前面的字符不是 `/` 的插值只能是查询串/后缀拼接
      // （如 `/ai-research/reports${suffix}`、`/holdings/${id}${query}`），整段丢弃。
      const previous = value.slice(-1);
      if (previous !== "/") {
        cursor = end + 1;
        continue;
      }
      if (/join|concat|\.map\(|\+/.test(expr)) return null;
      value += "abc123";
      cursor = end + 1;
      continue;
    }
    if (ch === mark) break;
    value += ch;
    cursor += 1;
  }
  // 查询串不参与桥匹配（main.ts 用 split("?",1)[0] 截断）。
  value = value.split("?")[0].split("#")[0];
  if (!value.startsWith("/")) return null;
  return value;
}

const scanned = [];
const skipped = [];
for (const file of collectFiles(webSrc)) {
  const text = readFileSync(file, "utf8");
  const re = /path:\s*(?=[`"'])/g;
  let hit;
  while ((hit = re.exec(text))) {
    const holder = enclosingObject(text, hit.index);
    // 类型声明里的 `path:` 也会被扫到（`{ method: "GET"|...; path: \`/${string}\` }`）：
    // 对象字面量用逗号分隔，类型成员用分号——用这个差别把它们排除。
    if (holder?.includes(";")) continue;
    const literal = readPathLiteral(text, hit.index + "path:".length);
    const where = file.replace(repoRoot, ".");
    if (!literal) {
      skipped.push(`${where} @${hit.index}`);
      continue;
    }
    const methodMatch = holder?.match(/method:\s*"([A-Z]+)"/);
    if (!methodMatch) {
      skipped.push(`${where} ${literal}（未在同对象内找到静态 method）`);
      continue;
    }
    scanned.push({ method: methodMatch[1], path: literal, file: where });
  }
}

/**
 * 动态段取值由调用点决定的模板：无法从字面量定形，但不能「扫不到就算通过」。
 * 每条必须写明理由，并给出**必须被放行**的具体探针路径（正面断言，不是豁免）。
 */
const UNRESOLVED_TEMPLATES = [
  {
    shape: "POST /plugins/{plugin_id}/{action}",
    reason: "action 由调用点决定（install/disable/update/revoke/app），不是路径段常量",
    probes: [
      "POST /plugins/official.cn-market-data/install",
      "POST /plugins/official.cn-market-data/disable",
      "POST /plugins/official.cn-market-data/update",
      "POST /plugins/official.cn-market-data/revoke",
      "GET /plugins/official.reading-library/app",
    ],
  },
  {
    shape: "GET /evidence/{kind}/{symbol}",
    reason: "kind 由调用点决定（announcements/news/financials），symbol 走 :path 可跨段",
    probes: [
      "GET /evidence/announcements/600519",
      "GET /evidence/news/600519",
      "GET /evidence/financials/600519",
    ],
  },
];

// 登记了但已不再出现的模板要清理，避免清单腐烂；未登记的动态模板必须报错。
const registeredSamples = new Set(
  UNRESOLVED_TEMPLATES.map((entry) => {
    const [method, template] = entry.shape.split(" ");
    return `${method} ${template.replace(/\{[^}]+\}/g, "abc123")}`;
  }),
);
const unmatched = scanned.filter((item) => !isAllowed(item.method, item.path));
const unresolvedShapes = new Set(unmatched.map((item) => `${item.method} ${item.path}`));
for (const item of unmatched) {
  // 落到这里的是「含动态段且采样未命中」的模板：必须在上面的清单里显式登记。
  if (!registeredSamples.has(`${item.method} ${item.path}`)) {
    fail(`前端真实请求未进桥清单：${item.method} ${item.path}（${item.file}）`);
  }
}
for (const entry of UNRESOLVED_TEMPLATES) {
  for (const probe of entry.probes) {
    const [method, path] = probe.split(" ");
    if (!isAllowed(method, path)) {
      fail(`动态模板探针未进桥清单：${probe}（${entry.shape} · ${entry.reason}）`);
    }
  }
}

// 前端源码扫描出的请求里，去重后至少要有一定规模，否则说明扫描器本身失效了。
const uniqueScanned = new Set(scanned.map((item) => `${item.method} ${item.path}`));
if (uniqueScanned.size < 40) {
  fail(`前端源码扫描只得到 ${uniqueScanned.size} 条请求，疑似扫描器失效（预期 ≥40）`);
}

if (failures.length) {
  console.error(`桥清单校验失败（${failures.length} 项）：`);
  for (const item of failures) console.error(`  - ${item}`);
  console.error(
    "\n修法：新增 Core 端点 → 在 apps/desktop-host/bridge-exposure.json 登记并运行\n" +
      "      node scripts/generate_bridge_manifest.mjs；\n" +
      "      确属桌面端不用的 → 登记到 exempt 并写明理由。",
  );
  process.exit(1);
}

console.log(
  `桥清单校验通过：清单 ${routes.length} 条（方法+路径均配对），` +
    `豁免 ${(exposure.exempt ?? []).length} 条；` +
    `前端源码扫描 ${uniqueScanned.size} 条请求，` +
    `其中动态模板 ${unresolvedShapes.size} 条按登记探针验证；` +
    `跳过 ${skipped.length} 条无法定形的路径。`,
);
if (process.env.BRIDGE_CHECK_VERBOSE) {
  for (const item of skipped) console.log(`  skip: ${item}`);
}

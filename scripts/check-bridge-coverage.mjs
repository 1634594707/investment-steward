/* 真实模式冒烟（A）：宿主桥白名单正则 vs 前端实际请求全量覆盖。
 * 直接读取 desktop-host/src/main.ts 生产正则，不手工复制；逐条断言五页
 * 启动 Promise.all + 各类变更请求都能通过桥（不被 502 拒之门外）。
 */
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// 以脚本自身位置定位仓库根，避免依赖调用方 cwd（dist:win 的 cwd 是 apps/desktop-host）。
const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const mainPath = resolve(repoRoot, "apps/desktop-host/src/main.ts");
const src = readFileSync(mainPath, "utf8");

const line = src.split("\n").find((l) => l.includes("const corePath = /"));
if (!line) throw new Error("未在 main.ts 找到 corePath 白名单正则");
const regexStart = line.indexOf("const corePath = /") + "const corePath = ".length;
const literalStart = line.indexOf("/", regexStart);
// 终止标记必须是正则字面量的结束斜杠（"...$/"）：早期版本用 "/.test(request.path)" 定位，
// 代码里变量名改成 requestPath 后该标记消失 → 正则被截断成空 → 全部路径假阳性。
const literalEnd = line.indexOf("$/");
if (literalEnd <= literalStart) throw new Error("无法定位 corePath 正则结尾（应形如 ...)$/.test(...)）");
// 注意：indexOf("$/") 返回的是 `$` 自身的下标，切片必须 +1 才能把 `$` 锚点纳入。
// 早期版本写成 slice(literalStart + 1, literalEnd)，等于丢掉 `$` → 正则变成前缀匹配
// → 形如 /ai-research/reports/{id}/follow-ups 这类"多一段"的真实缺口会被判为放行（假阴性）。
const regexSource = line.slice(literalStart + 1, literalEnd + 1);
const bridge = new RegExp(regexSource);

const paths = [
  // 启动 Promise.all（AppShell）
  "GET /investment-policies",
  "GET /evidence",
  "GET /plugins/catalog",
  "GET /research/questions/latest",
  "GET /research/runs",
  "GET /holdings",
  "GET /thesis",
  "GET /plans",
  "GET /decisions",
  "GET /brief/today",
  "GET /review/weekly",
  "GET /notifications/pending",
  "GET /notifications/triage",
  "GET /learning/unit/today",
  "GET /learning/goals/current",
  "GET /learning/activities",
  "GET /channels",
  "GET /audit",
  "GET /capabilities",
  // 状态栏 / 插槽 / 卡片（G1/G4）
  "GET /health",
  "GET /slots",
  "GET /cards/slots/today.brief",
  "GET /cards/slots/notification.global",
  // 双挂载 / 应用区（G2）
  "GET /plugins/official.reading-library/app",
  // 凭据库（G3-1/G3-4）
  "GET /credentials",
  "GET /credentials/market_data_token",
  "PUT /credentials/market_data_token",
  "DELETE /credentials/market_data_token",
  "POST /credentials/market_data_token/test",
  // 模型多方案（G3-3）
  "GET /model-profiles",
  "POST /model-profiles",
  // 投资者画像（E3）
  "GET /investor/profile",
  "PUT /investor/profile",
  "POST /model-profiles/prof-1/activate",
  "POST /model-profiles/prof-1/test",
  "DELETE /model-profiles/prof-1",
  // 研读图书馆（G5）
  "GET /library/books",
  "POST /library/books",
  "DELETE /library/books/book-1",
  "POST /library/plan/generate",
  "POST /library/annotations/book-1:0/to-research",
  // 量化分享池（G6-2）
  "GET /quant/artifacts",
  "POST /quant/runs",
  "GET /quant/lineage/steward/cn-equity-mr-baseline",
  // 投资页 / 证据 / 行情
  "GET /market/candles/510300",
  "GET /market/candles/511230",
  "GET /evidence/announcements/600519",
  "GET /evidence/news/600519",
  "GET /evidence/financials/600519",
  "GET /evidence/ev-abc123",
  "POST /holdings",
  "DELETE /holdings/00000000-0000-0000-0000-000000000000",
  "POST /thesis",
  "PUT /thesis/11111111-1111-1111-1111-111111111111",
  "POST /investment-policies",
  "POST /investment-policies/pol-1/confirm",
  // 研究页
  "POST /research/runs",
  "POST /research/runs/run-1/run",
  "POST /research/runs/run-1/transition",
  "GET /research/runs/run-1/response",
  // 复盘页
  "POST /decisions",
  "POST /plans",
  "POST /plans/plan-1/transition",
  "POST /brief/today/generate",
  "POST /evidence/freshness/patrol",
  "POST /notifications/evaluate",
  "POST /notifications/11111111-1111-1111-1111-111111111111/read",
  "POST /learning/activities",
  "DELETE /learning/activities/act-1",
  "GET /learning/reflections/export",
  // 扩展页
  "POST /plugins/official.cn-market-data/install",
  "POST /plugins/official.cn-market-data/disable",
  "POST /plugins/official.cn-market-data/update",
  "POST /plugins/official.cn-market-data/revoke",
  // 学习闭环（P0 缺口补齐：活动→政策建议）
  "POST /learning/activities/act-1/propose-policy-change",
  // 游资雷达（official.youzi-radar，P3-D03；未启用时 409，页面显示启用引导）
  "GET /youzi/today",
  "GET /youzi/watchlist",
  "GET /youzi/seats/600519",
  "GET /youzi/profile/1234",
  "GET /youzi/cross-check",
  "GET /youzi/cffex",
  // 游资二期：跨日披露事实（Y2-08）与有限窗口复盘（Y3-01）
  "GET /youzi/tactics",
  "GET /youzi/tactics/20260917",
  "GET /youzi/replay/600519",
  // 标的速查与 AI 研报库
  "GET /instruments/lookup",
  "GET /instruments/resolve",
  "GET /ai-research/reports",
  "GET /ai-research/reports/r1",
  "DELETE /ai-research/reports/r1",
  "POST /ai-research/reports/r1/follow-ups",
  "GET /ai-research/reports/r1/follow-ups",
  "GET /ai-research/review-queue",
  // 战法雷达市场批量扫描（D01：建任务 → 轮询进度 → 停止；2026-09-21 补，此前整段漏检 → 打包版 502）
  "POST /tactics/catalog",
  "POST /tactics/scan",
  "POST /tactics/scan-market",
  "GET /tactics/scan-market/job-1",
  "POST /tactics/scan-market/job-1/cancel",
  "GET /tactics/sectors",
  "GET /tactics/signals/600519",
  "GET /tactics/watchlist",
  "GET /tactics/watchlist/600519",
  "GET /tactics/watchlist/600519/notes",
  "GET /tactics/ai-reviews",
  "GET /tactics/notes/600519",
  // 设置页：备份列表 / 备份恢复 / 导入 / 模型用量
  "GET /storage/backups",
  "POST /storage/backups/db-2026-09-20.sqlite/restore",
  "POST /import/preview",
  "POST /import/apply",
  "GET /model-usage",
  // Jev 决策模型（JV02）：另一套协议（POST /systemone），配置与探测独立成节
  "GET /jev/config",
  "PUT /jev/config",
  "POST /jev/probe",
  "POST /jev/models",
  // 游资雷达课程只读端点（U05/U08）
  "GET /youzi/curriculum",
];

const failures = [];
for (const item of paths) {
  const [method, path] = item.split(" ");
  const ok = bridge.test(path);
  if (!ok) failures.push(item);
}
if (failures.length) {
  console.error("桥白名单未放行（将在真实模式变 502）：");
  for (const f of failures) console.error("  " + f);
  process.exit(1);
}
console.log(`桥白名单覆盖校验通过：${paths.length} 条请求全部命中生产正则。`);

// —— 全量自动审计（2026-09-21 追加）——
// 上面的手工清单已两次漏项（F01 的 /model-usage、D01 的 /tactics/scan-market/{job_id}），
// 表现为「脚本全绿、打包版 502」：dev 模式经 vite 代理直连 Core，绕过宿主桥，因此测不出白名单缺口。
// 故此处不再依赖手抄，直接扫描 Core 源码声明的全部路由，与生产正则逐条对照。
const pyFiles = [];
(function collect(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, entry.name);
    if (entry.isDirectory()) { if (entry.name !== "__pycache__") collect(p); }
    else if (entry.name.endsWith(".py")) pyFiles.push(p);
  }
})(resolve(repoRoot, "apps/core-api/src"));

/** 有意不经宿主桥的端点（桌面端渲染层无调用方）。新增豁免必须写清理由。 */
const EXEMPT = new Map([
  ["GET /evidence/support-resistance/{symbol}", "插件能力端点，桌面端渲染层无调用方"],
]);

const sampleOf = (name) =>
  name.startsWith("plugin_id") ? "official.cn-market-data"
    : name.startsWith("symbol") ? "600519"
      : name.startsWith("job_id") ? "job-1"
        : "abc123";

const missingRoutes = [];
let declaredCount = 0;
for (const file of pyFiles) {
  const text = readFileSync(file, "utf8");
  const routeRe = /@app\.(get|post|put|delete|patch)\(\s*["']([^"']+)["']/g;
  let m;
  while ((m = routeRe.exec(text))) {
    declaredCount += 1;
    const key = `${m[1].toUpperCase()} ${m[2]}`;
    if (EXEMPT.has(key)) continue;
    const concrete = m[2].replace(/\{([^}]+)\}/g, (_s, n) => sampleOf(n));
    if (!bridge.test(concrete)) missingRoutes.push(`${key}  ->  实际请求形如 ${concrete}`);
  }
}
if (missingRoutes.length) {
  console.error(`\n以下 ${missingRoutes.length} 条 Core 端点未进宿主桥白名单（打包版会返回 502）：`);
  for (const line of missingRoutes) console.error("  " + line);
  console.error("修法：在 apps/desktop-host/src/main.ts 的 corePath 正则里同步补分支；确属桌面端不用的，加入本脚本 EXEMPT 并注明理由。");
  process.exit(1);
}
console.log(`Core 路由全量审计通过：声明 ${declaredCount} 条，除 ${EXEMPT.size} 条豁免外全部经宿主桥放行。`);

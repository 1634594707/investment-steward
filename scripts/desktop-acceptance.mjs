/**
 * 阶段 F 桌面自动验收（Playwright Electron 驱动打包 EXE，隔离数据目录）。
 * 用法：NODE_PATH=<workspace>/node_modules node scripts/desktop-acceptance.mjs
 * 证据输出：.runtime-acceptance/ 下的截图与 result.json。
 */
import { createRequire } from "node:module";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

// ESM 不走 NODE_PATH，改用 CJS require 从托管工作区解析 playwright-core
const workspaceRequire = createRequire(
  "C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/__noop.js",
);
const { _electron: electron } = workspaceRequire("playwright-core");

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const EXE = join(ROOT, "dist-desktop", "win-unpacked", "Investment Steward.exe");
const ISO_DATA = join(ROOT, ".runtime-acceptance-f");
const OUT = join(ROOT, ".runtime-acceptance");
mkdirSync(OUT, { recursive: true });

const results = [];
function record(step, ok, detail = "") {
  results.push({ step, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"} | ${step}${detail ? ` | ${detail}` : ""}`);
}

// EXE 指纹
const exeBuf = readFileSync(EXE);
const hash = createHash("sha256").update(exeBuf).digest("hex");
record("EXE 指纹", true, `${exeBuf.length} bytes sha256=${hash.slice(0, 16)}…`);

const app = await electron.launch({
  executablePath: EXE,
  args: ["--user-data-dir=" + ISO_DATA],
  cwd: dirname(EXE),
  timeout: 60000,
});
const win = await app.firstWindow();
win.on("pageerror", (err) => record("页面 JS 错误", false, String(err).slice(0, 200)));

try {
  // 1) Core 连接（TitleBar 芯片）
  await win.waitForSelector("text=已连接", { timeout: 60000 });
  record("Core 连接", true, await win.locator("text=/Core .* · 已连接/").first().textContent());
  await win.waitForTimeout(2500); // 等初始 loadAll 稳定
  await win.screenshot({ path: join(OUT, "f01-boot.png") });

  // 2) 状态栏
  const sb = await win.locator("text=Core 已连接 · loopback").count();
  record("StatusBar 连接文案", sb > 0);

  // 3) 进入复盘页
  await win.getByRole("button", { name: /^复盘/ }).first().click();
  await win.waitForSelector("text=决定时间线", { timeout: 15000 });
  record("复盘页可达（决定时间线标题）", true);
  await win.screenshot({ path: join(OUT, "f02-review.png") });

  // 4) B3：记录决定弹窗含证据选择器与计划下拉
  await win.getByRole("button", { name: /记录一次决定/ }).click();
  await win.waitForSelector("text=记录一次决定", { timeout: 10000 });
  const hasPicker = await win.locator("text=当时依据（可选").count();
  const hasPlanSelect = await win.locator("text=关联执行计划（可选）").count();
  record("B3 决定表单含证据选择器/计划下拉", hasPicker > 0 && hasPlanSelect > 0);
  await win.screenshot({ path: join(OUT, "f03-decision-dialog.png") });
  await win.getByRole("button", { name: "取消" }).first().click();
  await win.waitForTimeout(300);

  // 5) keep-alive：修改页面级筛选（决定范围 30→7 天），切到今天再切回，筛选值应保留
  const periodSelect = win.locator("select.review-period-select, label.review-period select").first();
  const beforeVal = await periodSelect.inputValue();
  await periodSelect.selectOption("7");
  await win.screenshot({ path: join(OUT, "f04a-period-changed.png") });
  await win.getByRole("button", { name: /^今天/ }).first().click();
  await win.waitForTimeout(800);
  await win.getByRole("button", { name: /^复盘/ }).first().click();
  await win.waitForSelector("text=决定时间线", { timeout: 15000 });
  const afterVal = await periodSelect.inputValue();
  record("B4 keep-alive 页面筛选状态保留", beforeVal === "30" && afterVal === "7", `切页前=${beforeVal} 切回后=${afterVal}`);
  const hiddenAlive = await win.locator("div[inert]").count();
  record("B4 keep-alive 隐藏页驻留数", hiddenAlive >= 1, `${hiddenAlive} 个隐藏页面`);
  await win.screenshot({ path: join(OUT, "f04-keepalive.png") });

  // 6) C1：研究工作台模板 chips
  await win.getByRole("button", { name: /^研究工作台/ }).first().click();
  await win.waitForSelector("text=AI 研究工作台", { timeout: 15000 });
  const tpl = await win.locator("text=研究问题模板").count();
  record("C1 研究问题模板存在", tpl > 0);
  await win.screenshot({ path: join(OUT, "f05-workbench.png") });

  // 7) E2：复盘页决定卡片（空库无决定 → 验证区块标题存在即可）
  await win.getByRole("button", { name: /^复盘/ }).first().click();
  await win.waitForSelector("text=执行计划", { timeout: 15000 });
  record("复盘页区块完整（决定时间线/执行计划/学习活动）", (await win.locator("text=学习活动与反思").count()) > 0);
  await win.screenshot({ path: join(OUT, "f06-review-full.png") });
} catch (error) {
  record("未捕获异常", false, String(error).slice(0, 300));
  try { await win.screenshot({ path: join(OUT, "f99-error.png") }); } catch {}
} finally {
  await app.close();
}

writeFileSync(join(OUT, "f-result.json"), JSON.stringify({ hash: hash.slice(0, 16), results, finished_at: new Date().toISOString() }, null, 2));
const failed = results.filter((r) => !r.ok);
console.log(`SUMMARY: ${results.length - failed.length}/${results.length} PASS`);
process.exit(failed.length ? 1 : 0);

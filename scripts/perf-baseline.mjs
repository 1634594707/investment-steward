/**
 * 阶段 10 性能基线计时（§9.3）v2：驱动打包 EXE 三轮，逐轮落盘、失败强杀。
 * 用法：node .runtime/perf-baseline.mjs
 * 输出：.runtime-acceptance/perf-baseline.json
 */
import { createRequire } from "node:module";
import { writeFileSync, mkdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const workspaceRequire = createRequire(
  "C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/__noop.js",
);
const { _electron: electron } = workspaceRequire("playwright-core");

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const EXE = join(ROOT, "dist-desktop2", "win-unpacked", "Investment Steward.exe");
const ISO_DATA = join(ROOT, ".runtime-acceptance-f");
const OUT = join(ROOT, ".runtime-acceptance");
mkdirSync(OUT, { recursive: true });

const ROUNDS = 3;
const rounds = [];
const withTimeout = (p, ms, label) =>
  Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error(label + " timeout " + ms + "ms")), ms))]);

for (let i = 1; i <= ROUNDS; i++) {
  let app = null;
  try {
    const t0 = performance.now();
    app = await withTimeout(
      electron.launch({ executablePath: EXE, args: ["--user-data-dir=" + ISO_DATA], cwd: dirname(EXE), timeout: 60000 }),
      70000,
      "launch",
    );
    const tLaunch = performance.now();
    console.log(`round ${i}: launched in ${Math.round(tLaunch - t0)}ms`);

    const win = await withTimeout(app.firstWindow(), 60000, "firstWindow");
    const tWindow = performance.now();

    await withTimeout(win.waitForSelector("text=已连接", { timeout: 90000 }), 95000, "coreChip");
    const tCore = performance.now();

    await withTimeout(win.waitForLoadState("load"), 30000, "loadState");
    const tReady = performance.now();

    const round = {
      round: i,
      kind: i === 1 ? "cold(first-launch)" : "hot",
      spawnToLaunchMs: Math.round(tLaunch - t0),
      spawnToFirstWindowMs: Math.round(tWindow - t0),
      spawnToCoreReadyMs: Math.round(tCore - t0),
      spawnToFirstScreenReadyMs: Math.round(tReady - t0),
    };
    rounds.push(round);
    console.log(`round ${i} (${round.kind}): window=${round.spawnToFirstWindowMs}ms core=${round.spawnToCoreReadyMs}ms screen=${round.spawnToFirstScreenReadyMs}ms`);
  } catch (err) {
    console.log(`round ${i} FAILED: ${String(err).slice(0, 200)}`);
  } finally {
    if (app) {
      try {
        await withTimeout(app.close(), 15000, "close");
      } catch {
        try {
          app.process().kill();
        } catch {}
      }
    }
    await new Promise((r) => setTimeout(r, 2000));
    // 每轮后落盘（含失败中断的部分结果）
    writeFileSync(join(OUT, "perf-baseline.json"), JSON.stringify({ rounds, partial: rounds.length < ROUNDS }, null, 2));
  }
}

const median = (arr) => (arr.length ? [...arr].sort((a, b) => a - b)[Math.floor(arr.length / 2)] : null);
writeFileSync(
  join(OUT, "perf-baseline.json"),
  JSON.stringify(
    {
      exe: EXE,
      rounds,
      median: {
        firstWindowMs: median(rounds.map((r) => r.spawnToFirstWindowMs)),
        coreReadyMs: median(rounds.map((r) => r.spawnToCoreReadyMs)),
        firstScreenReadyMs: median(rounds.map((r) => r.spawnToFirstScreenReadyMs)),
      },
      environment: {
        os: "Windows 10.0.26200",
        dataDir: ISO_DATA,
        note: "round1 冷启动含 OS 文件缓存预热；round2-3 热启动；隔离数据目录复用",
      },
      measuredAt: new Date().toISOString(),
    },
    null,
    2,
  ),
);
console.log(`DONE ${rounds.length}/${ROUNDS} rounds`);

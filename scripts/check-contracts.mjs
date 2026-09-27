#!/usr/bin/env node
/* A03（架构改进路线图 2026-09-25）：契约产物漂移检查。
 *
 * 重新生成 schema 与 TypeScript 声明，与已提交产物逐字节比对；有差异即失败。
 * 这样「改了公共字段但忘记重新生成」会在 CI 就暴露，而不是等到前端读到错误类型。
 *
 * 用法：node scripts/check-contracts.mjs
 *       （更新产物：.venv/Scripts/python.exe scripts/export_contracts.py）
 */
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

function resolvePython() {
  const candidates = [
    process.env.STEWARD_CORE_PYTHON,
    join(repoRoot, ".venv", "Scripts", "python.exe"),
    join(repoRoot, ".venv", "bin", "python"),
  ].filter(Boolean);
  return candidates.find((item) => existsSync(item)) ?? "python";
}

try {
  execFileSync(resolvePython(), [join(repoRoot, "scripts", "export_contracts.py"), "--check"], {
    cwd: repoRoot,
    stdio: "inherit",
  });
} catch {
  process.exit(1);
}

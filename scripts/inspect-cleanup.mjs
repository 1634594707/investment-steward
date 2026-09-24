#!/usr/bin/env node
/**
 * E04（桌面端升级路线图 2026-09-18）：清理前检查脚本（**只读**，不做任何删除）。
 *
 * 背景：2026-09-18 曾发生「备份与构建产物混放的 8.2 GB 目录被整体清空、其中 14 份
 * 历史 SQLite 快照无独立留存」的事故（路线图 A04）。自本脚本起：
 *   任何清理动作必须先运行本脚本留存产物（--json 落档），再经人工逐项确认；
 *   本脚本自身不删除、不移动、不重命名任何文件。
 *
 * 用法：
 *   node scripts/inspect-cleanup.mjs <目录…>        # 显式列出候选目录
 *   node scripts/inspect-cleanup.mjs --json <目录…> # 以 JSON 输出（便于留档）
 *
 * 分级口径：
 *   CRITICAL  含 .sqlite3 数据文件（含 steward-* 快照/活库）或含 backups/ 目录
 *             → 禁止删除；须先逐份核实恢复价值（可先复制转存）。
 *   REVIEW    无 SQLite/备份，但含非构建类内容（文档/JSON 导出/研究产出等）
 *             → 人工逐项确认后才可清理。
 *   CLEANABLE 仅构建产物（node_modules/dist/win-unpacked/cache 等），可再生
 *             → 可清理，但仍需人工确认后执行（本脚本不执行）。
 *   EMPTY     目录不存在或为空 → 无需处置。
 */

import { readdir, lstat, stat } from "node:fs/promises";
import { join, resolve, sep } from "node:path";

const SQLITE_RE = /\.sqlite3$/i;
const STEWARD_RE = /^steward-.*\.sqlite3$/i;
const BACKUP_DIR_RE = /^(backups?)$/i;
const BUILD_HINTS = ["node_modules", "dist", "build", "cache", ".cache", "win-unpacked", "__pycache__", ".vite", "coverage"];
const DOCUMENT_HINTS = [".md", ".json", ".csv", ".xlsx", ".pdf"];

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "未知";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

/** 递归收集目录统计；不跟随符号链接（防止逃逸出目标目录）。 */
async function inspect(dir, state) {
  let entries;
  try {
    entries = await readdir(dir, { withFileTypes: true });
  } catch {
    state.unreadable = true;
    return;
  }
  for (const entry of entries) {
    const full = join(dir, entry.name);
    let info;
    try {
      info = await lstat(full);
    } catch {
      continue;
    }
    if (info.isSymbolicLink()) {
      state.symlinks += 1;
      continue;
    }
    if (info.isDirectory()) {
      state.dirs += 1;
      if (BACKUP_DIR_RE.test(entry.name)) state.hasBackupsDir = true;
      state.buildOnly = state.buildOnly && BUILD_HINTS.includes(entry.name.toLowerCase());
      await inspect(full, state);
      continue;
    }
    state.files += 1;
    state.bytes += info.size;
    if (info.mtimeMs > state.newestMtime) state.newestMtime = info.mtimeMs;
    if (SQLITE_RE.test(entry.name)) {
      state.sqliteFiles.push({ path: full, bytes: info.size, steward: STEWARD_RE.test(entry.name) });
    }
    const lower = entry.name.toLowerCase();
    if (DOCUMENT_HINTS.some((hint) => lower.endsWith(hint))) state.documentFiles += 1;
  }
}

function grade(state) {
  if (state.unreadable) return { level: "REVIEW", reason: "目录不可读，人工核实后再处置" };
  if (state.sqliteFiles.length > 0) {
    const steward = state.sqliteFiles.filter((file) => file.steward);
    return {
      level: "CRITICAL",
      reason: steward.length > 0
        ? `含 ${steward.length} 个 steward-* 数据库快照/活库（另有 ${state.sqliteFiles.length - steward.length} 个其他 SQLite）——禁止删除，先逐份核实恢复价值`
        : `含 ${state.sqliteFiles.length} 个 SQLite 数据文件——禁止删除，先核实恢复价值`,
    };
  }
  if (state.hasBackupsDir) return { level: "CRITICAL", reason: "含 backups/ 备份目录——禁止删除" };
  if (state.files === 0 && state.dirs === 0) return { level: "EMPTY", reason: "目录不存在或为空，无需处置" };
  if (state.buildOnly && state.documentFiles === 0) {
    return { level: "CLEANABLE", reason: "仅构建产物/缓存（可再生），可清理——仍需人工确认后执行" };
  }
  const reasons = [];
  if (state.documentFiles > 0) reasons.push(`含 ${state.documentFiles} 个文档/导出类文件`);
  if (state.symlinks > 0) reasons.push(`含 ${state.symlinks} 个符号链接`);
  if (reasons.length === 0) reasons.push("含非构建类内容");
  return { level: "REVIEW", reason: `${reasons.join("；")}——人工逐项确认后才可清理` };
}

async function inspectRoot(root) {
  const state = {
    files: 0,
    dirs: 0,
    bytes: 0,
    newestMtime: 0,
    sqliteFiles: [],
    hasBackupsDir: false,
    buildOnly: true,
    documentFiles: 0,
    symlinks: 0,
    unreadable: false,
  };
  try {
    const info = await stat(root);
    if (!info.isDirectory()) {
      return { root, exists: false, note: "路径不是目录" };
    }
  } catch {
    return { root, exists: false, note: "目录不存在" };
  }
  await inspect(root, state);
  const verdict = grade(state);
  return {
    root,
    exists: true,
    bytes: state.bytes,
    files: state.files,
    dirs: state.dirs,
    newest_mtime: state.newestMtime > 0 ? new Date(state.newestMtime).toISOString() : null,
    has_backups_dir: state.hasBackupsDir,
    sqlite_files: state.sqliteFiles,
    verdict,
  };
}

function printReport(results) {
  console.log("＝ E04 清理前检查（只读）＝ 本脚本不做任何删除；任何清理必须经本报告 + 人工确认 ＝");
  for (const result of results) {
    console.log(`\n■ ${result.root}`);
    if (!result.exists) {
      console.log(`  ${result.verdict ? "" : ""}${result.note}`);
      continue;
    }
    console.log(`  体积 ${formatBytes(result.bytes)} · ${result.files} 个文件 / ${result.dirs} 个子目录`);
    console.log(`  最近修改 ${result.newest_mtime ?? "未知"}`);
    console.log(`  含 backups/ 目录：${result.has_backups_dir ? "是" : "否"}`);
    if (result.sqlite_files.length > 0) {
      console.log(`  SQLite 文件 ${result.sqlite_files.length} 个：`);
      for (const file of result.sqlite_files.slice(0, 10)) {
        console.log(`    ${file.steward ? "[steward]" : "[sqlite]"} ${file.path}（${formatBytes(file.bytes)}）`);
      }
      if (result.sqlite_files.length > 10) console.log(`    … 及另外 ${result.sqlite_files.length - 10} 个`);
    }
    console.log(`  ▶ 建议处置：[${result.verdict.level}] ${result.verdict.reason}`);
  }
  console.log("\n＝ 报告结束。清理执行前：①留存本报告（--json）；②人工逐项确认；③再次运行核对无 CRITICAL 残留。＝");
}

const args = process.argv.slice(2);
const jsonMode = args.includes("--json");
const targets = args.filter((arg) => !arg.startsWith("--")).map((arg) => resolve(arg));

if (targets.length === 0) {
  console.error("用法：node scripts/inspect-cleanup.mjs [--json] <候选目录…>");
  console.error("示例：node scripts/inspect-cleanup.mjs .cleanup-trash dist-desktop2 .runtime .local-data");
  process.exit(2);
}

const results = [];
for (const target of targets) {
  const result = await inspectRoot(target);
  result.root = target.includes(sep) ? target : resolve(target);
  results.push(result);
}

if (jsonMode) {
  console.log(JSON.stringify({ generated_at: new Date().toISOString(), policy: "任何清理动作必须经本脚本产物 + 人工确认；本脚本不做任何删除", results }, null, 2));
} else {
  printReport(results);
}

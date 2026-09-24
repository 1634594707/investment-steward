#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A02（桌面端升级路线图 2026-09-18）：桌面端性能与体积基线，一条命令出全部数值。

只读：不写业务库、不改任何源码。数值同时打屏并（可选）落 JSON，供 I04 前后对比。

用法（仓库根目录）：
    uv run --directory apps/core-api python ../../scripts/desktop_baseline.py
    uv run --directory apps/core-api python ../../scripts/desktop_baseline.py --json .runtime/desktop-baseline.json
    python scripts/desktop_baseline.py --db .local-data/steward.sqlite3
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PY = ROOT / "apps/core-api/src/investment_steward_core/api/app.py"
DATABASE_PY = ROOT / "apps/core-api/src/investment_steward_core/storage/database.py"
APPSHELL_TSX = ROOT / "apps/web-shell/src/shell/AppShell.tsx"
DIST_ASSETS = ROOT / "apps/web-shell/dist/assets"

# 随使用线性增长、且当前没有 LIMIT 的启动期查询（B02 的对照对象）。
UNBOUNDED_QUERIES = {
    "list_evidence": "def list_evidence",
    "list_audit": "def list_audit",
    "list_decisions": "def list_decisions",
    "list_notifications": "def list_notifications",
}
# 活库里最能代表「用得越久越大」的表。
GROWTH_TABLES = (
    "audit_events", "evidence", "ai_research_reports", "ai_analysis_turns",
    "research_runs", "decisions", "notifications", "watch_items",
)
# 档案端点的服务端上限（B01 的对照对象）。
ARCHIVE_ROUTE_RE = re.compile(r"@app\.get\(\"/ai-research/reports\"\)[\s\S]{0,600}?def list_ai_research_reports\((?P<sig>[\s\S]*?)\)")
# `/ai-research/reports` 的服务端默认 limit（B01 要在验收时复测这一格）。
ARCHIVE_DEFAULT_LIMIT = 50


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def boot_requests() -> dict[str, object]:
    """首屏 Promise.all 的并发调用数与解构变量数（AppShell）。

    C02（桌面端升级路线图 2026-09-18）拆层后分两块：loadAll 的**首屏必需集**（阻塞可交互）
    与 loadDeferred 的**延后层**（骨架屏解除后在后台装）。`calls` 字段保持兼容 = 阻塞层
    （可操作时间的决定量），deferred_calls/total_calls 为拆层明细。"""
    text = _read(APPSHELL_TSX)
    if "async function loadAll(" not in text:
        return {"found": False}
    blocks = re.findall(r"await Promise\.all\(\[([\s\S]*?)\n\s*\]\);", text)
    calls_per_block = [len(re.findall(r"\bclient\.(?:status|request)\b", block)) for block in blocks]
    names_blocks = re.findall(r"const \[(?P<names>[^\]]*)\] = await Promise\.all", text)
    blocking = calls_per_block[0] if calls_per_block else 0
    deferred = sum(calls_per_block[1:], 0) if len(calls_per_block) > 1 else 0
    return {
        "found": True,
        "calls": blocking,
        "blocking_calls": blocking,
        "deferred_calls": deferred,
        "total_calls": blocking + deferred,
        "resolved_values": len(names_blocks[0].split(",")) if names_blocks else None,
    }


def query_caps() -> dict[str, bool]:
    """每个登记的启动期查询是否已带 LIMIT / OFFSET（True=已有界）。"""
    text = _read(DATABASE_PY)
    result: dict[str, bool] = {}
    for name, marker in UNBOUNDED_QUERIES.items():
        at = text.find(marker)
        body = text[at:at + 1200] if at != -1 else ""
        result[name] = bool(re.search(r"\bLIMIT\b|\bOFFSET\b", body))
    return result


def archive_endpoint() -> dict[str, object]:
    text = _read(APP_PY)
    match = ARCHIVE_ROUTE_RE.search(text)
    if not match:
        return {"found": False}
    signature = " ".join(match.group("sig").split())
    default = re.search(r"limit: int = (\d+)", signature)
    clamp = re.search(r"min\(limit, (\d+)\)", text[match.start():match.start() + 2500])
    return {
        "found": True,
        "signature": signature,
        "default_limit": int(default.group(1)) if default else None,
        "max_limit": int(clamp.group(1)) if clamp else None,
        "supports_server_side_filters": all(
            key in signature for key in ("offset", "q", "generated_from")
        ),
    }


def database_profile(db_path: Path) -> dict[str, object]:
    if not db_path.exists():
        return {"found": False, "path": str(db_path)}
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            row[0]: connection.execute(f'SELECT COUNT(*) FROM "{row[0]}"').fetchone()[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            if row[0] in GROWTH_TABLES
        }
        reports = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(payload)), 0), COALESCE(MAX(LENGTH(payload)), 0) "
            "FROM ai_research_reports"
        ).fetchone()
        # 打开工作台时档案列表要经 IPC 搬运的字节：按端点默认 limit 取最新 N 份的完整 payload。
        first_page = connection.execute(
            "SELECT COALESCE(SUM(LENGTH(payload)), 0) FROM ("
            "SELECT payload FROM ai_research_reports ORDER BY created_at DESC LIMIT ?)",
            (ARCHIVE_DEFAULT_LIMIT,),
        ).fetchone()[0]
        audit_span = connection.execute(
            "SELECT MIN(created_at), MAX(created_at) FROM audit_events"
        ).fetchone()
    finally:
        connection.close()
    return {
        "found": True,
        "path": str(db_path),
        "bytes": db_path.stat().st_size,
        "tables": tables,
        "archive": {
            "reports": reports[0],
            "payload_bytes_total": reports[1],
            "payload_bytes_max_single": reports[2],
            "first_page_rows": min(reports[0], ARCHIVE_DEFAULT_LIMIT),
            "first_page_bytes": first_page,
            "hidden_beyond_first_page": max(0, reports[0] - ARCHIVE_DEFAULT_LIMIT),
        },
        "audit_first_record": audit_span[0],
        "audit_last_record": audit_span[1],
    }


def dist_chunks(top: int = 10) -> dict[str, object]:
    if not DIST_ASSETS.exists():
        return {"found": False, "hint": "先跑 pnpm build:web"}
    files = sorted(DIST_ASSETS.iterdir(), key=lambda item: item.stat().st_size, reverse=True)
    assets = [{"name": item.name, "bytes": item.stat().st_size} for item in files[:top]]
    return {
        "found": True,
        "total_bytes": sum(item.stat().st_size for item in files if item.is_file()),
        "file_count": len(files),
        "largest": assets,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="桌面端性能与体积基线（只读）")
    parser.add_argument("--db", default=".local-data/steward.sqlite3", help="要统计的本机库路径")
    parser.add_argument("--json", dest="json_out", default=None, help="把结果写成 JSON 供前后对比")
    args = parser.parse_args(argv)

    report = {
        "boot": boot_requests(),
        "unbounded_startup_queries": query_caps(),
        "archive_endpoint": archive_endpoint(),
        "database": database_profile(ROOT / args.db if not Path(args.db).is_absolute() else Path(args.db)),
        "web_dist": dist_chunks(),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json_out:
        target = Path(args.json_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[written] {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path

import uvicorn

from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.storage.paths import StorageLayout


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Investment Steward local Core API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    # --data-dir：显式数据目录（最高优先，dev / 测试隔离用）；给了它就完全以它为准。
    # --default-data-dir：宿主兜底目录（桌面壳的 Electron userData/data）。
    #   优先级低于位置指针——用户在设置页迁移后的选择必须能在重启后赢回兜底值
    #   （2026-09-10 真机缺陷：桌面壳硬编码 --data-dir 导致存储位置改不动）。
    #   两者都不传时按「env > 指针 > %LOCALAPPDATA% 默认」解析。
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--default-data-dir", type=Path, default=None)
    parser.add_argument("--session-token", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    token = args.session_token or secrets.token_urlsafe(32)
    if args.data_dir is not None:
        settings = CoreSettings(
            host=args.host, port=args.port, data_dir=args.data_dir, session_token=token
        )
    else:
        layout = StorageLayout.from_environment(fallback_data_dir=args.default_data_dir)
        settings = CoreSettings(
            host=args.host,
            port=args.port,
            data_dir=layout.user_data,
            session_token=token,
            default_data_dir=args.default_data_dir,
            layout=layout,
        )
    from investment_steward_core.storage.database import rotate_backup

    backup_path = rotate_backup(settings.data_dir / "steward.sqlite3")
    if backup_path:
        print(f"[steward] backup: {backup_path.name}")

    # The Host owns the token. This readiness line intentionally never includes it.
    print(
        json.dumps({"type": "core_ready", "host": settings.host, "port": settings.port}), flush=True
    )
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()

"""中转服务配置：全部来自环境变量,不硬编码密钥。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RelaySettings:
    data_dir: Path
    bootstrap_token: str
    max_package_bytes: int = 8 * 1024 * 1024     # 中转包上限(默认 8MB)
    max_event_payload_bytes: int = 64 * 1024     # 单事件密文上限(默认 64KB)
    max_pull_limit: int = 500                    # 单次拉取上限
    max_token_ttl_seconds: int = 3600            # 一次性下载令牌最长有效期


def load_settings() -> RelaySettings:
    data_dir = Path(os.environ.get("RELAY_DATA_DIR", "relay-data")).resolve()
    bootstrap = os.environ.get("RELAY_BOOTSTRAP_TOKEN", "")
    if not bootstrap:
        raise RuntimeError("RELAY_BOOTSTRAP_TOKEN 未设置:拒绝以无引导令牌状态启动(设备登记必须鉴权)。")
    try:
        max_package = int(os.environ.get("RELAY_MAX_PACKAGE_BYTES", str(8 * 1024 * 1024)))
    except ValueError:
        max_package = 8 * 1024 * 1024
    return RelaySettings(
        data_dir=data_dir,
        bootstrap_token=bootstrap,
        max_package_bytes=max_package,
    )

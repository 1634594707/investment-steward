"""阶段 9 · 三套更新通道分离（Host / 插件 / 内容）。

互不覆盖不变式：
- 每类可更新制品只属一个更新通道，其写集与版本源互不相交；
- HOST 通道：运行中的 Core 二进制/内核版本（`__version__`）。只读于插件 API——
  任何 install/update/revoke/disable 都不得改写内核版本；
- PLUGIN 通道：`PluginInstallation` + 更新候选（`plugin_*` 表）。仅插件生命周期端点触及；
- CONTENT 通道：插件入账的数据（Evidence / CandleSeries），按 `content_hash` /
  `source_dataset_version` 版本化。插件版本更新不得改写已入账内容。

通道守卫：执行插件更新事务前后各取一次只读快照，提交后断言 HOST 版本与 CONTENT
内容指纹未被改动——若被误写（跨通道覆盖）由调用方 409 回滚。插件通道自身的版本
变化是预期结果，不参与不变式。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from investment_steward_core.domain import UpdateChannel


@dataclass(frozen=True)
class ChannelSnapshot:
    """某时刻三通道的只读版本快照，用于对照「是否发生了跨通道覆盖」。"""

    host_version: str
    plugin_versions: tuple[tuple[str, str], ...]
    content_fingerprint: str


def channel_registry() -> list[dict[str, str]]:
    """三通道的归属声明：版本源 + 写集 + 对插件 API 是否只读。

    用途：作为 `/channels` 的可观测表面，供「扩展 ▸ 更新」Tab 直观展示互不覆盖约定。
    """
    return [
        {
            "channel": UpdateChannel.HOST.value,
            "version_source": "investment_steward_core.__version__",
            "write_scope": "core_release_installer",
            "plugin_api_readonly": "true",
        },
        {
            "channel": UpdateChannel.PLUGIN.value,
            "version_source": "PluginInstallation.release_version",
            "write_scope": "plugin_installations / plugin_update_candidates",
            "plugin_api_readonly": "false",
        },
        {
            "channel": UpdateChannel.CONTENT.value,
            "version_source": "Evidence.content_hash / source_dataset_version",
            "write_scope": "evidence_ledger (evidence producer)",
            "plugin_api_readonly": "true",
        },
    ]


def _content_fingerprint(evidence_rows: list[Any]) -> str:
    """CONTENT 通道指纹：由全部证据的 id + content_hash 聚合，条目或内容任一变化即变。"""
    digest = hashlib.sha256()
    for row in sorted(evidence_rows, key=lambda e: str(getattr(e, "evidence_id", ""))):
        digest.update(str(getattr(row, "evidence_id", "")).encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(getattr(row, "content_hash", "")).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def snapshot_channels(
    db: Any,
    evidence_rows: list[Any],
    host_version: str,
) -> ChannelSnapshot:
    """读取（不写任何状态）三通道当前版本，产出只读快照。

    `db` 需具备 `list_plugin_installations()`；证据行由调用方传入已完成持久化的列表，
    避免守卫本身引入写入路径。
    """
    plugin_versions = tuple(
        sorted(
            (item.plugin_id, item.release_version) for item in db.list_plugin_installations()
        )
    )
    return ChannelSnapshot(
        host_version=host_version,
        plugin_versions=plugin_versions,
        content_fingerprint=_content_fingerprint(evidence_rows),
    )


def assert_no_cross_channel_overwrite(
    before: ChannelSnapshot, after: ChannelSnapshot
) -> None:
    """断言一次插件更新没有跨通道覆盖 HOST / CONTENT。

    插件通道（plugin_versions）允许也应当变化；HOST 版本与 CONTENT 指纹必须不变，
    否则说明某步误触了其它通道（应触发回滚而非继续）。
    """
    if before.host_version != after.host_version:
        raise ValueError(
            "跨通道覆盖：插件更新改动了 HOST 通道版本，应回滚而非继续"
            f"（{before.host_version} → {after.host_version}）"
        )
    if before.content_fingerprint != after.content_fingerprint:
        raise ValueError("跨通道覆盖：插件更新改动了 CONTENT 通道内容，应回滚而非继续")


def channel_of(kind: str) -> UpdateChannel:
    """把制品类型映射到更新通道；未知类型一律拒绝（不猜测归属）。"""
    mapping: dict[str, UpdateChannel] = {
        "core_binary": UpdateChannel.HOST,
        "plugin_manifest": UpdateChannel.PLUGIN,
        "evidence": UpdateChannel.CONTENT,
        "market_candles": UpdateChannel.CONTENT,
    }
    try:
        return mapping[kind]
    except KeyError as exc:
        raise ValueError(f"未知制品类型 {kind!r}，无法归属更新通道") from exc
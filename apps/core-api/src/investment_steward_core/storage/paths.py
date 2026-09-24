"""统一存储目录配置对象（路线图 3.1「目录模型」）。

阶段 1 目标：冻结目录模型与版本，让模块不再自行拼接路径。

四类目录：
- user_data：SQLite、证据、研究、设置、审计。用户可选择位置，可迁移。
- cache：外部数据缓存、临时结果、日志。按 TTL 与容量清理，不存用户资产。
- artifacts：参数集、模型、策略包、回测结果。按哈希保存，可选择位置。
- install：程序安装目录（EXE / Host / 前端 / 冻结后端），不存用户数据。

兼容性约束：阶段 1 只冻结结构、不改变任何现有文件位置。
`artifacts` 默认与 `user_data` 同目录，因此现有 `quant_parameter_sets.json`
仍落在原处；阶段 2 再拆分为独立制品目录并保留旧位置导入。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# 目录配置 schema 版本。目录结构发生不兼容变更时递增，
# 迁移与兼容导入逻辑据此分支（阶段 1 迁移流程尚未实现，先记录版本）。
LAYOUT_VERSION = 1

# 主数据库文件名。仅此处定义，其余模块一律通过 StorageLayout 取路径。
DATABASE_FILE = "steward.sqlite3"

# 制品文件名。量化模块的 JSON 存储位置由 StorageLayout 统一给出，
# 模块不得再自行持有文件名常量或拼接路径。
QUANT_PARAMETER_SETS_FILE = "quant_parameter_sets.json"
QUANT_STRATEGY_PACKS_FILE = "quant_strategy_packs.json"
QUANT_TRACK_RECORDS_FILE = "quant_track_records.json"

# 位置指针文件名：记录用户自选的目录布局。锚定在安装无关的固定位置，
# 数据目录迁移后仍能被下一次启动找到；删除它即回到默认位置。
POINTER_FILENAME = "storage-layout.json"


def pointer_file() -> Path:
    """位置指针文件路径。`STEWARD_LAYOUT_POINTER` 可覆盖（测试用）。"""
    env = os.environ.get("STEWARD_LAYOUT_POINTER")
    if env:
        return Path(env)
    return Path(os.environ.get("LOCALAPPDATA", ".")) / "InvestmentSteward" / POINTER_FILENAME


def load_pointer() -> dict[str, object] | None:
    """读取位置指针；不存在或损坏时返回 None（按默认布局处理，绝不猜测）。"""
    try:
        data = json.loads(pointer_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("user_data"), str):
        return None
    return data


def write_pointer(
    user_data: Path | str,
    *,
    cache: Path | str | None = None,
    artifacts: Path | str | None = None,
) -> Path:
    """原子写入位置指针（临时文件 + os.replace）。返回指针文件路径。"""
    import tempfile

    pointer = pointer_file()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": LAYOUT_VERSION,
        "user_data": str(Path(user_data)),
        "cache": str(Path(cache)) if cache is not None else None,
        "artifacts": str(Path(artifacts)) if artifacts is not None else None,
    }
    fd, tmp_name = tempfile.mkstemp(dir=pointer.parent, prefix=".pointer-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_name, pointer)
    return pointer


def clear_pointer() -> bool:
    """删除位置指针（恢复默认位置时使用）。返回是否确实删除了文件。"""
    try:
        pointer_file().unlink()
        return True
    except FileNotFoundError:
        return False


@dataclass(frozen=True)
class StorageLayout:
    """四类目录的唯一来源。任何模块不得再自行拼接这些路径。"""

    version: int
    user_data: Path
    cache: Path
    artifacts: Path
    install: Path | None = None

    @property
    def database_file(self) -> Path:
        """主 SQLite 文件路径。"""
        return self.user_data / DATABASE_FILE

    @property
    def backups(self) -> Path:
        """E03（桌面端升级路线图 2026-09-18）：备份目录纳入 StorageLayout 显式管理。

        rotate_backup 与 /storage/lifecycle、/storage/backups 一律经此属性取路径，
        不再各自拼接 user_data / "backups"。"""
        return self.user_data / "backups"

    @property
    def quant_parameter_sets_file(self) -> Path:
        """量化参数集 JSON 路径（阶段 2 将改为 SQLite 元数据索引 + 内容哈希文件）。"""
        return self.artifacts / QUANT_PARAMETER_SETS_FILE

    @property
    def quant_strategy_packs_file(self) -> Path:
        """量化策略包 JSON 路径。"""
        return self.artifacts / QUANT_STRATEGY_PACKS_FILE

    @property
    def quant_track_records_file(self) -> Path:
        """量化实盘记录 JSON 路径。"""
        return self.artifacts / QUANT_TRACK_RECORDS_FILE

    def ensure(self) -> "StorageLayout":
        """创建三类可写目录（install 由安装程序管理，不在此创建）。"""
        for directory in (self.user_data, self.cache, self.artifacts):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    @classmethod
    def from_user_data(
        cls,
        user_data: Path | str,
        *,
        install: Path | str | None = None,
        cache: Path | str | None = None,
        artifacts: Path | str | None = None,
    ) -> "StorageLayout":
        """以用户数据目录为基准构造布局。

        `cache` 与 `artifacts` 可显式指定（用户自选位置时从配置读取）；
        未指定时按阶段 1 的兼容默认值落在 user_data 之下。
        """
        root = Path(user_data)
        return cls(
            version=LAYOUT_VERSION,
            user_data=root,
            cache=Path(cache) if cache is not None else root / "cache",
            artifacts=Path(artifacts) if artifacts is not None else root,
            install=Path(install) if install is not None else None,
        )

    @classmethod
    def from_environment(cls, fallback_data_dir: Path | str | None = None) -> "StorageLayout":
        """按「环境变量 > 位置指针 > fallback_data_dir > 默认」解析布局，供 Core 启动时使用。

        - `STEWARD_DATA_DIR` 等环境变量：最高优先（显式启动参数、测试）。
        - 位置指针文件：用户在设置页选择过的目录（迁移后生效的来源）。
        - `fallback_data_dir`：宿主给的本机兜底目录（桌面壳传 Electron userData/data）。
          **必须低于指针**——否则用户迁移后的选择在每次重启都会被宿主默认值覆盖
          （2026-09-10 真机缺陷：桌面壳硬编码 --data-dir 导致存储位置永远改不动）。
        - 默认：`%LOCALAPPDATA%\\InvestmentSteward\\data`。
        """
        data_env = os.environ.get("STEWARD_DATA_DIR")
        cache_env = os.environ.get("STEWARD_CACHE_DIR")
        artifacts_env = os.environ.get("STEWARD_ARTIFACTS_DIR")
        install_env = os.environ.get("STEWARD_INSTALL_DIR")
        if data_env is None:
            pointer = load_pointer()
            if pointer is not None:
                data_env = str(pointer["user_data"])
                cache_env = cache_env or (str(pointer["cache"]) if pointer.get("cache") else None)
                artifacts_env = artifacts_env or (str(pointer["artifacts"]) if pointer.get("artifacts") else None)
        if data_env is None and fallback_data_dir is not None:
            data_env = str(fallback_data_dir)
        local_app_data = Path(os.environ.get("LOCALAPPDATA", "."))
        default_dir = local_app_data / "InvestmentSteward" / "data"
        return cls.from_user_data(
            Path(data_env) if data_env is not None else default_dir,
            install=install_env,
            cache=cache_env,
            artifacts=artifacts_env,
        )

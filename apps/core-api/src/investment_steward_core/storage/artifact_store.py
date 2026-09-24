"""量化制品文件存储（路线图 3.2：参数 JSON / 模型权重放制品目录，不进 SQLite）。

写入协议（路线图约束「临时文件、哈希校验和原子替换」）：
1. 先写同目录临时文件（`.tmp-<pid>-` 前缀）；
2. 读回并计算 SHA-256；
3. `os.replace` 原子替换最终路径（同卷 rename）；
4. 返回 (相对文件名, sha256)，由调用方写入 SQLite 元数据索引。

读取协议：给定期望哈希，读文件并复核；不一致抛 `ArtifactIntegrityError`，
绝不静默使用被篡改/损坏的制品内容。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from investment_steward_core.storage.paths import StorageLayout

KIND_PARAMETER_SETS = "parameter_sets"
KIND_MODEL_WEIGHTS = "model_weights"
CHUNK_SIZE = 1024 * 1024


class ArtifactIntegrityError(RuntimeError):
    """制品文件内容与登记哈希不一致。"""


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_path(layout: StorageLayout, kind: str, file_name: str) -> Path:
    return layout.artifacts / kind / file_name


def save_artifact(layout: StorageLayout, kind: str, file_name: str, payload: dict[str, Any]) -> tuple[str, str]:
    """原子保存制品 JSON,返回 (file_name, sha256)。file_name 由调用方按 artifact_id 命名。"""
    target = artifact_path(layout, kind, file_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".tmp-{os.getpid()}-{file_name}"
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    tmp.write_text(text, encoding="utf-8")
    written_hash = _hash_file(tmp)
    # 读回校验:确认临时文件可解析且哈希稳定后才原子替换。
    json.loads(tmp.read_text(encoding="utf-8"))
    if _hash_file(tmp) != written_hash:
        tmp.unlink(missing_ok=True)
        raise ArtifactIntegrityError(f"制品写入校验失败:{file_name}")
    os.replace(tmp, target)
    if _hash_file(target) != written_hash:  # 替换后复核(检测落盘损坏)
        raise ArtifactIntegrityError(f"制品落盘后哈希不一致:{file_name}")
    return file_name, written_hash


def read_artifact(layout: StorageLayout, kind: str, file_name: str, expected_hash: str) -> dict[str, Any]:
    """读取制品并复核哈希;损坏/被篡改时抛 ArtifactIntegrityError。"""
    path = artifact_path(layout, kind, file_name)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ArtifactIntegrityError(f"制品文件缺失:{file_name}") from error
    if _hash_file(path) != expected_hash:
        raise ArtifactIntegrityError(f"制品内容哈希不一致:{file_name}")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ArtifactIntegrityError(f"制品文件结构异常:{file_name}")
    return data

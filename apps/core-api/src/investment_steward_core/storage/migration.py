"""存储位置迁移引擎（路线图 3.1 第 3 步）。

流程（对齐路线图约束，缺一步不可）：

1. 临时复制：源文件先复制到目标目录下的 staging 子目录，绝不直接落最终路径；
2. 哈希校验：逐文件 SHA-256 比对，任何不一致立即中止；
3. 配置切换：校验通过后 staging 内容落位 + 原子写位置指针；
4. Core 重启：由调用方（桌面端 host:restart-core / 手动重启）完成，本模块只返回 restart_required；
5. 再次校验：重启后调 `verify()`，对照迁移清单复核哈希 + SQLite quick_check。

失败语义：任何一步失败，原目录零改动，staging 清理后抛 `MigrationError`；
旧目录在用户确认前绝不删除（本模块根本不提供删除旧目录的操作）。
缓存目录不迁移（按 TTL 与容量清理、可随时重建），只在预览中如实标注。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from investment_steward_core.storage.paths import StorageLayout

MANIFEST_FILENAME = "migration-manifest.json"
CHUNK_SIZE = 1024 * 1024
STAGING_PREFIX = ".steward-migration-"


class MigrationError(Exception):
    """迁移失败。code 供 API 映射为结构化错误，detail 面向用户。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _iter_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file())


def _dir_size(root: Path) -> tuple[int, int]:
    files = _iter_files(root)
    return sum(f.stat().st_size for f in files), len(files)


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def measure(layout: StorageLayout) -> dict[str, Any]:
    """迁移前预览所需的空间占用（数据库 / 用户数据 / 制品 / 缓存 / 剩余空间）。"""
    db_size = layout.database_file.stat().st_size if layout.database_file.exists() else 0
    user_bytes, user_count = _dir_size(layout.user_data)
    artifacts_same_volume_root = layout.artifacts == layout.user_data
    art_bytes, art_count = _dir_size(layout.artifacts)
    cache_bytes, cache_count = _dir_size(layout.cache)
    free_target = shutil.disk_usage(layout.user_data).free
    return {
        "database_bytes": db_size,
        "user_data": {"path": str(layout.user_data), "bytes": user_bytes, "files": user_count},
        "artifacts": {
            "path": str(layout.artifacts),
            "bytes": art_bytes,
            "files": art_count,
            "inside_user_data": artifacts_same_volume_root,
        },
        "cache": {
            "path": str(layout.cache),
            "bytes": cache_bytes,
            "files": cache_count,
            "migrated": False,
            "note": "缓存不迁移：按 TTL 与容量清理，迁移后自动重建。",
        },
        "free_bytes": free_target,
    }


def preview(layout: StorageLayout, target_user_data: str, target_artifacts: str | None = None) -> dict[str, Any]:
    """迁移预览：源占用 + 目标剩余空间 + 可行性结论，不写任何东西。"""
    source = measure(layout)
    target_root = Path(target_user_data)
    same = target_root.resolve() == layout.user_data.resolve()
    problems: list[str] = []
    if same:
        problems.append("目标目录与当前用户数据目录相同，无需迁移。")
    needed = source["user_data"]["bytes"]
    if target_artifacts is not None and Path(target_artifacts).resolve() != layout.artifacts.resolve():
        needed += source["artifacts"]["bytes"]
    try:
        target_root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(target_root).free
    except OSError as error:
        free = None
        problems.append(f"目标目录不可用:{error}")
    else:
        if free is not None and free <= needed:
            problems.append(f"目标剩余空间不足:需要约 {needed} 字节,剩余 {free} 字节。")
    return {
        "source": source,
        "needed_bytes": needed,
        "target_free_bytes": free,
        "target_user_data": str(target_root),
        "target_artifacts": str(Path(target_artifacts)) if target_artifacts else None,
        "problems": problems,
        "feasible": not problems,
    }


def _fail(layout: StorageLayout, staging: Path, code: str, detail: str) -> "MigrationError":
    shutil.rmtree(staging, ignore_errors=True)
    return MigrationError(code, detail)


def migrate(
    layout: StorageLayout,
    target_user_data: str,
    target_artifacts: str | None = None,
    target_cache: str | None = None,
) -> dict[str, Any]:
    """执行迁移：临时复制 → 哈希校验 → 落位 → 写指针。失败保留原目录。"""
    if not layout.database_file.exists():
        raise MigrationError("no_database", "当前数据库文件不存在,无内容可迁移。")
    target_root = Path(target_user_data)
    if target_root.resolve() == layout.user_data.resolve():
        raise MigrationError("same_directory", "目标目录与当前用户数据目录相同。")
    check = preview(layout, target_user_data, target_artifacts)
    if not check["feasible"]:
        raise MigrationError(check["problems"][0].split(":")[0], ";".join(check["problems"]))
    target_layout = StorageLayout.from_user_data(
        target_root, cache=target_cache, artifacts=target_artifacts
    )
    staging = target_root / f"{STAGING_PREFIX}{os.getpid()}"

    plan: list[tuple[Path, Path, str, str]] = []  # (源, staging 目标, bucket, 清单相对键)
    for src in _iter_files(layout.user_data):
        rel = src.relative_to(layout.user_data)
        if rel.parts and rel.parts[0].startswith(".steward-migration-"):
            continue
        plan.append((src, staging / "user_data" / rel, "user_data", str(rel)))
    if layout.artifacts.resolve() != layout.user_data.resolve():
        for src in _iter_files(layout.artifacts):
            rel = src.relative_to(layout.artifacts)
            if rel.parts and rel.parts[0].startswith(".steward-migration-"):
                continue
            plan.append((src, staging / "artifacts" / rel, "artifacts", str(rel)))

    total_bytes = 0
    manifest: dict[str, Any] = {"version": 1, "user_data": {}, "artifacts": {}}
    try:
        for src, dst, bucket, rel_key in plan:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            source_hash = hash_file(src)
            if hash_file(dst) != source_hash:
                return _fail(layout, staging, "hash_mismatch", f"复制后哈希不一致:{src.name}")
            manifest[bucket][rel_key] = source_hash
            total_bytes += src.stat().st_size
    except OSError as error:
        raise _fail(layout, staging, "copy_failed", f"复制失败:{error}") from error

    # 落位：staging 内容移入最终目录（同一卷内 rename,不跨盘）。
    try:
        for bucket in ("user_data", "artifacts"):
            src_dir = staging / bucket
            if not src_dir.exists():
                continue
            dst_dir = (
                target_layout.artifacts
                if bucket == "artifacts" and target_layout.artifacts.resolve() != target_root.resolve()
                else target_layout.user_data
            )
            dst_dir.mkdir(parents=True, exist_ok=True)
            for child in list(src_dir.iterdir()):
                shutil.move(str(child), str(dst_dir / child.name))
    except OSError as error:
        raise _fail(layout, staging, "finalize_failed", f"落位失败:{error}") from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    # 迁移清单写到「新」user_data（重启后 verify 的依据）。
    (target_layout.user_data / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    from investment_steward_core.storage.paths import write_pointer

    pointer = write_pointer(
        target_layout.user_data,
        cache=target_layout.cache,
        artifacts=(
            target_layout.artifacts
            if target_layout.artifacts.resolve() != target_layout.user_data.resolve()
            else None
        ),
    )
    return {
        "ok": True,
        "copied_files": len(plan),
        "total_bytes": total_bytes,
        "source_user_data": str(layout.user_data),
        "target_user_data": str(target_layout.user_data),
        "pointer_file": str(pointer),
        "old_directory_removed": False,
        "restart_required": True,
    }


def verify(layout: StorageLayout) -> dict[str, Any]:
    """再次校验：有迁移清单则逐文件复核哈希；否则退化为 SQLite quick_check。"""
    manifest_path = layout.user_data / MANIFEST_FILENAME
    checked = 0
    mismatches: list[str] = []
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            manifest = None
        if isinstance(manifest, dict):
            for bucket, root in (
                ("user_data", layout.user_data),
                ("artifacts", layout.artifacts),
            ):
                for rel, expected in (manifest.get(bucket) or {}).items():
                    path = root / rel
                    if not path.exists():
                        mismatches.append(f"缺失:{rel}")
                        continue
                    if hash_file(path) != expected:
                        mismatches.append(f"哈希不一致:{rel}")
                    checked += 1
    db_ok = False
    db_detail = "数据库文件不存在"
    if layout.database_file.exists():
        conn = None
        try:
            conn = sqlite3.connect(layout.database_file)
            row = conn.execute("PRAGMA quick_check").fetchone()
            db_ok = bool(row) and row[0] == "ok"
            db_detail = "quick_check ok" if db_ok else f"quick_check 返回 {row}"
        except sqlite3.Error as error:
            db_detail = f"quick_check 失败:{error}"
        finally:
            if conn is not None:
                conn.close()
    return {
        "ok": db_ok and not mismatches,
        "checked_files": checked,
        "mismatches": mismatches,
        "database": {"ok": db_ok, "detail": db_detail, "path": str(layout.database_file)},
    }

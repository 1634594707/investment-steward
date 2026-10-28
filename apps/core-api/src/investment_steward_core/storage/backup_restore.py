"""整库恢复：先验证和迁移临时副本，再替换当前库。调用方须持维护锁。"""
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from investment_steward_core.storage.database import SCHEMA_VERSION, Database, rotate_backup


def _copy(source: Path, target: Path) -> None:
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src,
        closing(sqlite3.connect(target)) as dst,
    ):
        src.backup(dst)


def _validate_schema(staged: Path, reference: Path) -> None:
    """版本与quick_check不能证明业务表完整；以当前迁移生成的空库核对必需列。"""
    canonical = Database(reference)
    canonical.close()
    with closing(sqlite3.connect(reference)) as expected, closing(sqlite3.connect(staged)) as actual:
        tables = expected.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        for (table,) in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            required = {row[1] for row in expected.execute(f"PRAGMA table_info({quoted})")}
            present = {row[1] for row in actual.execute(f"PRAGMA table_info({quoted})")}
            if not required <= present:
                raise ValueError("备份业务表或字段不完整，当前数据未修改")


def restore_backup(source: Path, current: Path) -> Path:
    with TemporaryDirectory(prefix="steward-restore-", dir=current.parent) as directory:
        staged = Path(directory) / "staged.sqlite3"
        try:
            _copy(source, staged)
            with closing(sqlite3.connect(staged)) as raw:
                if raw.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError("备份完整性校验失败，当前数据未修改")
                version = raw.execute("PRAGMA user_version").fetchone()[0]
                if not 1 <= version <= SCHEMA_VERSION:
                    raise ValueError("备份版本无效或高于当前程序版本，当前数据未修改")
        except sqlite3.Error as exc:
            raise ValueError("备份无法读取，当前数据未修改") from exc
        try:
            migrated = Database(staged)
            migrated.close()
        except Exception as exc:
            raise RuntimeError("备份迁移失败，当前数据未修改") from exc
        _validate_schema(staged, Path(directory) / "reference.sqlite3")
        rollback = rotate_backup(current)
        if rollback is None:
            raise RuntimeError("无法生成回退点，已中止恢复，当前数据未修改")
        try:
            _copy(staged, current)
        except Exception as exc:
            try:
                _copy(rollback, current)
            except Exception as rollback_error:
                raise RuntimeError(f"恢复及自动回退失败，请保留回退点：{rollback}") from rollback_error
            raise RuntimeError("恢复失败，已还原恢复前的数据") from exc
        return rollback

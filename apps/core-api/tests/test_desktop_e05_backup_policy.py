"""E05（桌面端升级路线图 2026-09-18）：备份触发点与按时间跨度保留策略的测试。

覆盖：每自然日首次写入触发一次备份（同日第二次写入不再备份）、保留策略
（最近 7 天日份全保留 + 最近 4 周每周最新一份 + 至少最近 7 份，其余删除）、
源库损坏时拒绝备份（保留最后一次好备份）、备份后可回报字节。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from investment_steward_core.storage.database import Database, rotate_backup

STAMP_RE_SUFFIX = "-2"


def _make_db(tmp_path: Path) -> Database:
    return Database(tmp_path / "steward.sqlite3")


def _backup_files(backup_dir: Path) -> list[str]:
    return sorted(path.name for path in backup_dir.glob("steward-*.sqlite3"))


def test_daily_backup_throttles_per_day(tmp_path):
    db = _make_db(tmp_path)
    backup_dir = db.path.parent / "backups"
    assert not backup_dir.exists() or len(_backup_files(backup_dir)) == 0

    # 首次写入检查 → 产生今日备份；同日第二次检查 → 不重复。
    db.maybe_daily_backup()
    first = _backup_files(backup_dir)
    assert len(first) == 1, "每日首次写入应产生当日独立快照"
    assert first[0].startswith(datetime.now().strftime("steward-%Y%m%d"))  # noqa: DTZ005

    db.maybe_daily_backup()
    db.maybe_daily_backup()
    assert _backup_files(backup_dir) == first, "同日再次写入不得重复备份"


def test_tiered_retention_keeps_daily_weekly_and_minimum(tmp_path):
    db = _make_db(tmp_path)
    backup_dir = db.path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()  # noqa: DTZ005

    def fake_backup(name: str) -> None:
        # 直接伪造历史备份文件（rotate_backup 只按文件名时间戳做保留决策）。
        (backup_dir / name).write_bytes(b"SQLite format 3\x00" + b"\x00" * 32)

    # 桶：昨日、3 天前、8 天前、10 天前（8/10 天在 7 天窗外，但同属上周 → 每周留最新 10 天前那份）、
    # 40 天前、70 天前（远超 4 周 → 应被清理）。
    # 当日内再补 4 份（不同时刻）：让「至少最近 7 份」全部由近期文件占据，
    # 70 天前那份不再受 min-keep 保护，检验按时间跨度清理的语义。
    for extra in range(4):
        fake_backup(now.strftime(f"steward-%Y%m%d-1{extra:02d}00") + ".sqlite3")
    fake_backup((now - timedelta(days=1)).strftime("steward-%Y%m%d-%H%M%S") + ".sqlite3")
    fake_backup((now - timedelta(days=3)).strftime("steward-%Y%m%d-%H%M%S") + ".sqlite3")
    fake_backup((now - timedelta(days=8)).strftime("steward-%Y%m%d-%H%M%S") + ".sqlite3")
    fake_backup((now - timedelta(days=10)).strftime("steward-%Y%m%d-%H%M%S") + ".sqlite3")
    fake_backup((now - timedelta(days=40)).strftime("steward-%Y%m%d-%H%M%S") + ".sqlite3")
    fake_backup((now - timedelta(days=70)).strftime("steward-%Y%m%d-%H%M%S") + ".sqlite3")

    rotate_backup(db.path)  # 今日新备份 + 保留决策

    remaining = _backup_files(backup_dir)
    assert len(remaining) <= 8, "保留总量收敛（7 天日份 + 4 周份）"
    assert any(name.startswith(now.strftime("steward-%Y%m%d")) for name in remaining), "今日备份保留"
    assert any(name.startswith((now - timedelta(days=1)).strftime("steward-%Y%m%d")) for name in remaining)
    # 每周保留该周最新一份：8 天前与 10 天前同属上周时只留 8 天前那份；
    # 若跨周界则两份各为其周最新。两种情况下「上一周有备份可回」都成立。
    kept_recent_week = any(
        name.startswith((now - timedelta(days=days_ago)).strftime("steward-%Y%m%d"))
        for name in remaining
        for days_ago in (8, 10)
    )
    assert kept_recent_week, "上周保留最新一份"
    assert not any(name.startswith((now - timedelta(days=70)).strftime("steward-%Y%m%d")) for name in remaining), "70 天前清理"


def test_corrupt_source_refuses_backup(tmp_path):
    """源库损坏（quick_check 非 ok）→ 拒绝备份，保留最后一次好备份。"""
    source_dir = tmp_path / "good"
    source_dir.mkdir(parents=True, exist_ok=True)
    db_path = source_dir / "steward.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.close()

    good = rotate_backup(db_path)
    assert good is not None
    good_name = good.name
    good_bytes = good.read_bytes()

    # 把源库写坏（plain sqlite3 文件，无 WAL 侧文件占用）。
    db_path.write_bytes(b"corrupted garbage")
    result = rotate_backup(db_path)
    assert result is None, "源库不可读时拒绝备份"
    backup_dir = source_dir / "backups"
    surviving = [path for path in backup_dir.glob("*.sqlite3") if path.name == good_name]
    assert surviving[0].read_bytes() == good_bytes, "最后一次好备份不被坏备份覆盖"
    # 附带验证：备份文件可回报字节（E05 验收「备份后回报字节」的数据源）。
    assert surviving[0].stat().st_size > 0

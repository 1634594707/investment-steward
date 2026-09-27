"""S2（用户视角路线图 2026-09-26）：崩溃残留的扫描任务不得永久锁死该输入。

改造前存在三个叠加缺陷：

1. `get_scan_job` 检出心跳超时后**只在返回的 dict 上**改写 state='error'，从不落库；
2. `create_scan_job` 的裁剪语句**显式排除** state='running'，僵尸行永不被清理；
3. `find_running_scan_job` 只按 `state='running'` 匹配，必然命中僵尸行。

后果：扫描中途合盖/崩溃后，用**相同参数**重扫永远复用那个死 job_id，界面反复显示
「任务中断」，该输入的扫描就此报废（唯一绕过是改动任一参数以改变指纹）。

本文件按真实时序验证：制造僵尸 → 确认不再被复用 → 确认重扫能建出新任务 →
确认僵尸行最终被裁剪掉。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from investment_steward_core.storage import database as db_module
from investment_steward_core.storage.database import (
    SCAN_JOB_STALE_MINUTES,
    SCAN_JOB_STALE_REASON,
    Database,
)


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(tmp_path / "steward.sqlite3")


def _make_zombie(db: Database, fingerprint: str) -> str:
    """登记一个任务后把 updated_at 拨到阈值之前，模拟崩溃留下的僵尸 running 行。"""
    job_id = "zombie-job"
    db.create_scan_job(job_id, fingerprint, {"mode": "all"})
    stale = (datetime.now(UTC) - timedelta(minutes=SCAN_JOB_STALE_MINUTES + 5)).isoformat()
    with db._connection() as connection:
        connection.execute(
            db_module.text("UPDATE scan_jobs SET updated_at = :stale WHERE job_id = :job_id"),
            {"stale": stale, "job_id": job_id},
        )
    return job_id


def test_stale_running_job_is_not_reused(db):
    """核心回归：心跳超时的 running 行不得被 find_running_scan_job 复用。

    顺序必须与真实调用链一致——`api/app.py` 的扫描入口是**先** `find_running_scan_job`
    决定是否复用、**后**才可能有人去 `get_scan_job` 轮询状态。若本用例先读状态，
    落库修复就会顺手把僵尸改成 error，从而掩盖掉「查找侧没排除僵尸」这一半缺陷。
    """
    fingerprint = "fp-stale"
    _make_zombie(db, fingerprint)

    # 用户点「市场扫描」：入口第一件事就是查有没有可复用的 running 任务
    assert db.find_running_scan_job(fingerprint) is None, (
        "僵尸任务仍被复用，重扫会继续返回死 job_id"
    )

    # 随后轮询状态：如实呈现为中断，而不是假装还在跑
    job = db.get_scan_job("zombie-job")
    assert job is not None
    assert job["state"] == "error"
    assert job["error"] == SCAN_JOB_STALE_REASON


def test_stale_state_is_persisted_not_just_masked(db):
    """超时要**落库**——只改返回值的话僵尸行会永远留在表里。"""
    fingerprint = "fp-persist"
    _make_zombie(db, fingerprint)
    db.get_scan_job("zombie-job")  # 触发落库

    with db._connection() as connection:
        state = connection.execute(
            db_module.text("SELECT state FROM scan_jobs WHERE job_id = 'zombie-job'")
        ).scalar()
    assert state == "error", f"超时应落库为 error，实际仍是 {state!r}"


def test_rescan_after_crash_creates_a_fresh_job(db):
    """模拟真实恢复路径：同参数重扫必须能建出新任务，而不是复用死 job_id。"""
    fingerprint = "fp-rescan"
    zombie_id = _make_zombie(db, fingerprint)

    # 扫描入口的判定逻辑（与 api/app.py 同序）：先查可复用任务，查不到才新建
    reused = db.find_running_scan_job(fingerprint)
    assert reused is None, "僵尸任务被复用了，重扫依旧会返回死 job_id"

    new_id = "fresh-job"
    db.create_scan_job(new_id, fingerprint, {"mode": "all"})

    running = db.find_running_scan_job(fingerprint)
    assert running is not None
    assert running["job_id"] == new_id
    assert running["job_id"] != zombie_id

    # 新任务正常推进到完成，不受僵尸影响
    db.update_scan_job_progress(new_id, 5, 5)
    db.finish_scan_job(new_id, "done", {"ok": True})
    assert db.get_scan_job(new_id)["state"] == "done"


def test_live_running_job_is_still_deduplicated(db):
    """反向保护：心跳正常的 running 任务必须仍被复用（去重语义不回退）。"""
    fingerprint = "fp-live"
    db.create_scan_job("live-job", fingerprint, {"mode": "all"})
    running = db.find_running_scan_job(fingerprint)
    assert running is not None
    assert running["job_id"] == "live-job"

    job = db.get_scan_job("live-job")
    assert job["state"] == "running", "心跳正常的任务不得被误判为中断"


def test_creating_a_job_reaps_stale_running_rows(db):
    """登记新任务时顺带清理僵尸行，避免表里无限堆积死 running。"""
    _make_zombie(db, "fp-old")
    db.create_scan_job("another", "fp-new", {"mode": "sectors"})

    with db._connection() as connection:
        states = [
            row
            for row in connection.execute(
                db_module.text("SELECT job_id, state FROM scan_jobs ORDER BY created_at")
            ).all()
        ]
    remaining = {row[0]: row[1] for row in states}
    # 僵尸行要么已被裁剪，要么已被落成 error——两种都不再是 running
    assert remaining.get("zombie-job") in (None, "error")
    assert all(state != "running" for job_id, state in remaining.items() if job_id == "zombie-job")

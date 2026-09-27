"""S8（用户视角路线图 2026-09-26）：协同流水线不能永久卡在「执行中」。

症状：某角色进入 `running` 后再没变过，此后**每一次** `POST /evidence/collab-next`
都返回 409 `CollabBusyError`；而 `_runs` 只在内存，用户唯一出路是**重启应用**。
按 `_RUN_TTL_SECONDS = 6 * 3600`（6 小时）计，这是「一次偶发异常 = 锁死半天」。

改造前 `execute_next` 只把**模型调用**包进两个 except，JSON 解析（:300）与质量闸门
（含 `report_quality.validate_report` / `jev_client.build_judge`，:300-354）都在函数体
缩进层——这些地方任何异常都无人接管，`stage["status"]` 已置 `running` 且永不复位。

本文件锁定两条修复：
1. 后处理整段进 try，并在 `finally` 兜底——**任何**退出路径都不留 `running`；
2. `_prune_locked` 不淘汰在途运行（否则运行到一半被踢，下次推进拿 404，流水线丢失）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from investment_steward_core import collab
from investment_steward_core import model_client as mc


def _make_run(stage_name: str = "draft") -> dict:
    # 签名照抄 collab.create_run 实参（symbol, question, tech_summary, sources,
    # source_errors, profile_id, model, ...），不猜参数名。
    run = collab.create_run(
        "600001", "趋势是否延续", "技术面摘要", {"S1": "原文"}, {}, "p1", "m", report_mode="standard"
    )
    # 直接把指针挪到目标阶段，跳过前序角色
    for index, spec in enumerate(STAGES_FOR_TEST):
        if spec["stage"] == stage_name:
            run["next_index"] = index
            break
    return run


STAGES_FOR_TEST = [spec for spec in collab.STAGES] if isinstance(collab.STAGES[0], dict) else [
    {"stage": name} for name in collab.STAGES
]


def _ok_payload() -> dict:
    return {"report": "## 技术面\n- 判断 [S1]", "citations": ["S1"]}


def _stub_model(monkeypatch) -> None:
    def _fake(profile, credential_store, messages, purpose=None):
        # ModelReply 的必填四字段（content/model/provider/latency_ms）照实构造，不猜。
        return mc.ModelReply(
            content=json.dumps(_ok_payload(), ensure_ascii=False), model="m", provider="p", latency_ms=1
        )

    monkeypatch.setattr(mc, "call_active_model", _fake)


class _Boom(RuntimeError):
    """注入用异常：模拟质量闸门内部炸掉。"""


def test_post_process_exception_does_not_wedge_run(monkeypatch):
    """核心回归：质量闸门抛异常时，阶段必须落到 failed 而非永远 running。"""
    _stub_model(monkeypatch)
    run = _make_run("draft")
    core, store = object(), object()

    def _explode(*_a, **_k):
        raise _Boom("质量闸门内部炸了")

    monkeypatch.setattr(collab.report_quality, "validate_report", _explode)

    stage = collab.execute_next(core, run, object(), store)

    assert stage["status"] == "failed", f"阶段被留在 {stage['status']}，运行已被永久挡住"
    assert "后处理异常" in stage.get("error", "")

    # 关键二次验证：再推一次**不**应再撞 409。
    stage2 = collab.execute_next(core, run, object(), store)
    assert stage2["status"] in ("failed", "done"), "修复后同一运行应可重试，而不是永久 busy"


def test_json_parse_exception_does_not_wedge_run(monkeypatch):
    """JSON 解析层抛异常同样不得留下 running。"""
    _stub_model(monkeypatch)
    run = _make_run("draft")
    core, store = object(), object()

    def _explode(_reply):
        raise _Boom("解析层炸了")

    monkeypatch.setattr(collab.macro_ai, "extract_json_object", _explode)

    stage = collab.execute_next(core, run, object(), store)
    assert stage["status"] == "failed"
    assert "后处理异常" in stage.get("error", "")


def test_base_exception_in_post_process_still_unwedges(monkeypatch):
    """连 BaseException（KeyboardInterrupt 类）也走 finally 兜底，不留 running。

    这里断言的是**效果**而非「异常是否向上传播」——传播与否与本修复无关，
    真正的契约是：无论怎么退出，stage 都不能停在 running。
    """
    _stub_model(monkeypatch)
    run = _make_run("draft")
    core, store = object(), object()

    def _explode(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(collab.report_quality, "validate_report", _explode)

    try:
        collab.execute_next(core, run, object(), store)
    except BaseException:  # noqa: BLE001, S110 - 传播与否不关心，只断言 finally 已复位
        pass
    assert run["stages"][1]["status"] == "failed", "异常后阶段被留在 running，会永久 busy"


def test_jev_build_judge_exception_does_not_wedge_run(monkeypatch):
    """jev_client.build_judge 抛异常（网络/凭据问题）同样不得卡死。"""
    _stub_model(monkeypatch)
    run = _make_run("draft")
    core, store = object(), object()

    def _explode(*_a, **_k):
        raise _Boom("jev 不可用")

    monkeypatch.setattr(collab.jev_client, "build_judge", _explode)

    stage = collab.execute_next(core, run, object(), store)
    assert stage["status"] == "failed"
    assert "后处理异常" in stage.get("error", "")


def _seed_runs(count: int) -> None:
    """造 count 个**确实超量**的运行。

    两个坑都要绕开：
    - `create_run` 内部就会调 `_prune_locked()`，逐个创建永远超不出 `_MAX_RUNS`，
      所以超出部分直接以已有运行为模板塞进 `_runs`；
    - `created_at` 必须取**相对当下**的时间：写死日期会被 6 小时 TTL 判成过期全量清空，
      测到的就不是淘汰逻辑而是过期逻辑了。
    """
    import copy

    collab._runs.clear()
    base = datetime.now(UTC)
    for i in range(collab._MAX_RUNS):
        run = collab.create_run(f"60000{i}", "问题", "技术面", {"S1": "原文"}, {}, "p1", "m",
                                report_mode="standard")
        run["created_at"] = (base - timedelta(seconds=count - i)).isoformat()
    while len(collab._runs) < count:
        template = copy.deepcopy(next(iter(collab._runs.values())))
        template["run_id"] = f"extra-{len(collab._runs)}"
        template["created_at"] = (base - timedelta(seconds=count)).isoformat()
        collab._runs[template["run_id"]] = template
    assert len(collab._runs) == count


def test_in_flight_run_not_evicted_by_prune(monkeypatch):
    """超量清理不得淘汰正在执行的运行（否则流水线半途消失，下次 404）。"""
    collab._runs.clear()
    try:
        _seed_runs(collab._MAX_RUNS + 1)
        assert len(collab._runs) == collab._MAX_RUNS + 1, "前置：确实超量了"
        oldest_id = min(collab._runs, key=lambda rid: collab._runs[rid]["created_at"])
        collab._runs[oldest_id]["stages"][0]["status"] = "running"

        with collab._lock:
            collab._prune_locked()

        assert oldest_id in collab._runs, "在途运行被淘汰了：流水线会半途丢失"
        # 淘汰仍在进行，只是不再踢在途的那个：多出的 1 份由次老的**空闲**运行让位。
        assert len(collab._runs) == collab._MAX_RUNS
    finally:
        collab._runs.clear()


def test_idle_run_still_evicted(monkeypatch):
    """对照：空闲运行照常淘汰（修复不是「不淘汰了」）。"""
    collab._runs.clear()
    try:
        _seed_runs(collab._MAX_RUNS + 3)
        assert len(collab._runs) == collab._MAX_RUNS + 3
        with collab._lock:
            collab._prune_locked()
        assert len(collab._runs) == collab._MAX_RUNS
    finally:
        collab._runs.clear()


def test_model_failure_still_reports_cleanly(monkeypatch):
    """对照：模型调用失败（原有路径）行为不变——failed + 可读 error。"""
    run = _make_run("draft")

    def _unavailable(*_a, **_k):
        raise mc.ModelUnavailable("网络不可达")

    monkeypatch.setattr(mc, "call_active_model", _unavailable)
    stage = collab.execute_next(object(), run, object(), object())
    assert stage["status"] == "failed"
    assert "模型调用失败" in stage["error"]

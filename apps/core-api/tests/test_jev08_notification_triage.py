"""JV08 契约测试：证据巡逻通知分诊（路线图 P1 第二站）。

要解决的问题：`evaluate_notifications` 的匹配是「条件原文 n-gram × 证据摘要包含」的纯文本
启发式（`_evidence_matches`），命中即产通知。公告/新闻场景下会放过大量**措辞沾边但实质无关**
的内容（纯行情播报、公关稿、行业泛泛报道、与已读信息重复）——用户收到的是「提醒的噪音」。

覆盖四层：

1. **纯函数 · 上下文与降级阶梯**：白名单只送判定必需的片段；超预算时按**整句**削摘要、
   再削持仓上下文，**绝不截半句**（截半句会让 Jev 拿到与原文不同的意思，从而自信地判错）。
2. **纯函数 · 分片**：双重上限（每片 ≤N 条且 ≤M 字）、`index` 跨分片全局编号、
   每条两题、跳过的每一条都带原因（不静默丢）。
3. **纯函数 · 判定与合并**：**三个条件同时成立才压制**（`immaterial` + 优先级 0 + 置信度达标），
   任一维度不确定/失败/缺答一律**放行**；`confidence` 取两题**较小值**；统计行三态分开。
4. **端点**：`evaluate` 后压制条目**仍在库里**（软校验铁律）、`/notifications/pending` 不返回它、
   `/notifications/triage` 能翻到；同 dedup 键不重复出网；分片失败如实标 `unannotated` 并放行；
   以及最要紧的 **Jev 关闭时通知行为零变化**（零出网、`triage` 恒为 None）。

第 4 层里「压制条目仍可翻到」是 JV08 的验收核心：低相关不再弹通知，**但不能变成低相关被
静默吞掉**——静默吞掉一条真提醒的代价，远大于多弹一条低相关提醒的代价。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import pytest
from fastapi.testclient import TestClient
from investment_steward_core import credential_store, jev_client, notifications
from investment_steward_core.api import app as api_app
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings
from investment_steward_core.domain import Notification, NotificationTriage

# ——————————————————————————————————————————————————————————————
# 合成素材
# ——————————————————————————————————————————————————————————————

_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
#: `_stable` 派生 thesis_id 用；同标的必须落到同一个 thesis（= 同一个 dedup 键）
_THESIS_NS = UUID("00000000-0000-0000-0000-00000000abcd")


def _notification(
    *,
    instrument: str = "510300",
    triggered_by: str = "跌破 MA60",
    summary: str = "新证据与已确认条件匹配，建议复核。",
    triage: NotificationTriage | None = None,
) -> Notification:
    return Notification(
        notification_id=uuid4(),
        user_id=_USER_ID,
        thesis_id=uuid4(),
        instrument=instrument,
        triggered_by=triggered_by,
        condition_kind="invalidation_condition",
        title=f"{instrument} 命中已确认失效条件",
        summary=summary,
        action_mode="review_plan",
        evidence_refs=[],
        created_at=datetime.now(UTC),
        data_time=datetime.now(UTC),
        triage=triage,
    )


def _ctx(**overrides) -> dict[str, object]:
    base: dict[str, object] = {
        "holding_status": "holding",
        "label": "核心仓",
        "conditions": ["跌破 MA60"],
        "core_assumptions": ["行业需求仍在上行"],
    }
    base.update(overrides)
    return base


def _item(dedup_key: str, notification: Notification, **context) -> dict[str, object]:
    return {"dedup_key": dedup_key, "notification": notification, "context": _ctx(**context)}


# ——————————————————————————————————————————————————————————————
# 1. 纯函数：上下文白名单
# ——————————————————————————————————————————————————————————————


def test_jev_triage_context_text_only_reads_whitelisted_keys():
    """白名单之外的键一律不透传（用户自述原文不是判定素材，带上只是扩大出网面）。"""
    text = notifications.jev_triage_context_text(
        _ctx(strategy_note="这是用户私下的策略备注原文，不该出网", position_size=12.5)
    )
    assert "该标的在持仓中（核心仓）" in text
    assert "触发条件：跌破 MA60" in text
    assert "核心假设：行业需求仍在上行" in text
    assert "策略备注" not in text
    assert "12.5" not in text


def test_jev_triage_context_text_covers_three_holding_states():
    assert "自选观察中" in notifications.jev_triage_context_text(_ctx(holding_status="watchlist"))
    # 不在持仓也不在自选 → 如实说明，而不是留空让模型自己猜
    assert "不在持仓也不在自选" in notifications.jev_triage_context_text(_ctx(holding_status=""))
    assert notifications.jev_triage_context_text(None) == ""


def test_jev_triage_context_text_caps_assumptions():
    text = notifications.jev_triage_context_text(
        _ctx(core_assumptions=[f"假设{i}" for i in range(6)])
    )
    for index in range(notifications.JEV_TRIAGE_ASSUMPTION_LIMIT):
        assert f"假设{index}" in text
    assert "假设3" not in text, "超出上限的假设不该出网"


# ——————————————————————————————————————————————————————————————
# 2. 纯函数：单条文本与降级阶梯
# ——————————————————————————————————————————————————————————————


def test_jev_triage_item_text_full_block_carries_whitelisted_evidence():
    text, degraded = notifications.jev_triage_item_text(_notification(), context=_ctx())
    assert degraded is None
    for marker in ("【标的】510300", "【通知类型】失效条件", "【标题】", "【摘要】", "【持仓上下文】"):
        assert marker in text, marker


def _four_sentences() -> str:
    """四句等长摘要：A/B/C/D 各 91 字（含句号），便于断言「整句保留 / 整句丢弃」。"""
    return "".join(f"{letter * 90}。" for letter in "ABCD")


def test_jev_triage_item_text_trims_summary_by_whole_sentences():
    """超预算时按**整句**削摘要——第 4 句整句丢弃，绝不出现半句。"""
    summary = _four_sentences()
    full, _ = notifications.jev_triage_item_text(_notification(summary=summary), context=_ctx())
    budget = len(full) - 60  # 刚好容不下完整块，但容得下收紧后的摘要

    text, degraded = notifications.jev_triage_item_text(
        _notification(summary=summary), context=_ctx(), max_chars=budget
    )
    assert degraded == "为塞进预算已按整句截短摘要"
    assert "A" * 90 in text and "C" * 90 in text
    assert "D" not in text, "第 4 句必须整句丢弃，而不是留半句"
    assert "【持仓上下文】" in text, "这一级只削摘要，持仓上下文保留"
    assert len(text) <= budget


def test_jev_triage_item_text_flags_hard_clip_when_first_sentence_alone_overflows():
    """首句就超限 → 只能硬截，但**必须如实标注**这一级降级，不能假装是整句收紧。"""
    summary = "E" * 800  # 单句、无句读，`_sentence_trim` 无从下手
    full, _ = notifications.jev_triage_item_text(_notification(summary=summary), context=_ctx())

    text, degraded = notifications.jev_triage_item_text(
        _notification(summary=summary), context=_ctx(), max_chars=len(full) - 120
    )
    assert degraded == "为塞进预算已硬截摘要（摘要首句即超限，无法按整句收紧）"
    assert "【摘要】" in text and "【持仓上下文】" in text


def test_jev_triage_item_text_drops_context_after_summary_is_tightened():
    """③ 档：摘要已收紧仍放不下 → 去持仓上下文（它比摘要后半段更有判别力，所以后削）。"""
    summary = _four_sentences()
    full, _ = notifications.jev_triage_item_text(_notification(summary=summary), context=_ctx())
    trimmed, _ = notifications.jev_triage_item_text(
        _notification(summary=summary), context=_ctx(), max_chars=len(full) - 60
    )
    # 卡在「收紧后放得下」与「收紧后仍放不下」之间
    budget = len(trimmed) - 20

    text, degraded = notifications.jev_triage_item_text(
        _notification(summary=summary), context=_ctx(), max_chars=budget
    )
    assert degraded == "为塞进预算已截短摘要并去掉持仓上下文"
    assert "【持仓上下文】" not in text
    assert "A" * 90 in text, "削的是上下文，摘要仍在"


def test_jev_triage_item_text_returns_empty_when_hopeless():
    text, degraded = notifications.jev_triage_item_text(_notification(), context=_ctx(), max_chars=10)
    assert text == ""
    assert degraded == "通知标题与摘要超预算，整条未送评"


# ——————————————————————————————————————————————————————————————
# 3. 纯函数：分片
# ——————————————————————————————————————————————————————————————


def test_jev_triage_shards_respects_per_shard_cap_and_keeps_global_index():
    items = [_item(f"k{i}", _notification(instrument=f"6000{i:02d}")) for i in range(45)]
    bundle = notifications.jev_triage_shards(items, max_per_shard=20)

    assert [len(shard["evaluated"]) for shard in bundle["shards"]] == [20, 20, 5]
    indexes = [entry["index"] for shard in bundle["shards"] for entry in shard["evaluated"]]
    assert indexes == list(range(1, 46)), "index 必须跨分片全局连续"

    first = bundle["shards"][0]
    assert "【通知 1】" in first["state"] and "【通知 20】" in first["state"]
    # 每条通知两道题（相关性 + 优先级）
    assert len(first["questions"]) == 40
    assert set(first["questions"]) == {
        f"n{index}_{suffix}"
        for index in range(1, 21)
        for suffix in (
            notifications.JEV_TRIAGE_QUESTION_MATERIAL,
            notifications.JEV_TRIAGE_QUESTION_PRIORITY,
        )
    }
    assert bundle["skipped"] == []


def test_jev_triage_shards_also_caps_state_chars():
    """字数上限独立生效：条数没到 20 也要按字数切。"""
    fat = _notification(summary="很长的摘要内容。" * 60)
    items = [_item(f"k{i}", fat) for i in range(12)]
    bundle = notifications.jev_triage_shards(items, max_per_shard=20, max_chars=6_000)
    assert len(bundle["shards"]) > 1, "字数上限未生效"
    assert all(len(shard["state"]) <= 6_000 for shard in bundle["shards"])
    assert sum(len(shard["evaluated"]) for shard in bundle["shards"]) + len(bundle["skipped"]) == 12


def test_jev_triage_shards_marks_oversized_item_instead_of_dropping_it():
    items = [_item("k1", _notification(summary="很长" * 200))]
    bundle = notifications.jev_triage_shards(items, item_max_chars=20)
    assert bundle["shards"] == []
    assert [entry["reason"] for entry in bundle["skipped"]] == ["over_item_budget"]
    assert bundle["skipped"][0]["dedup_key"] == "k1"
    assert "整条未送评" in bundle["skipped"][0]["note"]


def test_jev_triage_shards_counts_every_over_limit_skip(monkeypatch):
    monkeypatch.setattr(notifications, "JEV_TRIAGE_MAX_CANDIDATES", 1)
    items = [_item(f"k{i}", _notification()) for i in range(4)]
    bundle = notifications.jev_triage_shards(items)

    assert sum(len(shard["evaluated"]) for shard in bundle["shards"]) == 1
    assert len(bundle["skipped"]) == 3, "超上限的每一条都要如实计入，不能只报一条"
    assert {entry["reason"] for entry in bundle["skipped"]} == {"over_candidate_limit"}
    assert {entry["dedup_key"] for entry in bundle["skipped"]} == {"k1", "k2", "k3"}


# ——————————————————————————————————————————————————————————————
# 4. 纯函数：判定规则（三条件同时成立才压制）
# ——————————————————————————————————————————————————————————————


def test_triage_decision_suppresses_only_when_all_three_conditions_hold():
    suppressed, note = notifications._triage_decision("immaterial", 0, 0.9)
    assert suppressed is True
    assert "留痕可查" in note, "压制说明必须点明「不是删除」"

    # 任一维度不满足 → 一律放行。这些是「静默吞掉真提醒」的防呆边界。
    for impact, priority, confidence in (
        ("material", 0, 0.9),  # 模型说会影响
        ("uncertain", 0, 0.9),  # 模型说说不准 → 信息不足时不做减法
        ("immaterial", 1, 0.9),  # 优先级不是最低
        ("immaterial", 2, 0.9),
        ("immaterial", 0, None),  # 没给置信度 → 无法确认
        ("immaterial", 0, 0.42),  # 置信度低于地板
    ):
        got, reason = notifications._triage_decision(impact, priority, confidence)
        assert got is False, (impact, priority, confidence)
        assert reason, "放行也要给理由，便于在通知中心解释"


def _choice(question_id: str, choice: str, confidence: float | None):
    return jev_client.JevAnswer(
        question_id=question_id, type="choice", choice=choice,
        probabilities={choice: confidence or 0.5}, confidence=confidence,
    )


def _score(question_id: str, score: float, confidence: float | None):
    return jev_client.JevAnswer(
        question_id=question_id, type="score", score=score,
        probabilities={str(int(score)): confidence or 0.5}, confidence=confidence, levels=3,
    )


def _answers(material_conf: float | None, priority_conf: float | None, *, priority_score: float = 0.0):
    return {
        f"n1_{notifications.JEV_TRIAGE_QUESTION_MATERIAL}": _choice(
            f"n1_{notifications.JEV_TRIAGE_QUESTION_MATERIAL}", "immaterial", material_conf
        ),
        f"n1_{notifications.JEV_TRIAGE_QUESTION_PRIORITY}": _score(
            f"n1_{notifications.JEV_TRIAGE_QUESTION_PRIORITY}", priority_score, priority_conf
        ),
    }


def _findings(answers, error=None, index: int = 1):
    return notifications.jev_triage_findings(
        [{"evaluated": [{"index": index, "dedup_key": "k1"}], "answers": answers, "error": error}]
    )["k1"]


def test_jev_triage_findings_takes_lower_confidence_of_the_two_questions():
    """两题独立评估，取小是保守方向——置信度越低越不该压制。"""
    got = _findings(_answers(0.93, 0.55))
    assert got["confidence"] == 0.55
    assert got["suppressed"] is True
    assert got["triage_state"] == notifications.JEV_TRIAGE_STATE_SUPPRESSED
    assert got["impact_label"] == "不实质影响"
    assert got["priority_label"] == "不必通知"
    assert got["priority_percent"] == 0


def test_jev_triage_findings_releases_when_confidence_below_floor():
    got = _findings(_answers(0.9, 0.44))
    assert got["confidence"] == 0.44
    assert got["suppressed"] is False
    assert got["triage_state"] == notifications.JEV_TRIAGE_STATE_NOTIFIED
    assert "低于地板" in got["note"]


def test_jev_triage_findings_normalizes_priority_percent():
    got = _findings(_answers(0.9, 0.9, priority_score=2.0))
    assert got["priority"] == 2
    assert got["priority_percent"] == 100
    assert got["suppressed"] is False, "优先级最高时不得压制"


def test_jev_triage_findings_releases_on_shard_error_and_missing_answers():
    """分片失败 / 缺答 → `unannotated` 且**放行**。「没评」不等于「通过」，也不等于「该压」。"""
    evaluated = [{"index": 1, "dedup_key": "k1"}, {"index": 2, "dedup_key": "k2"}]
    failed = notifications.jev_triage_findings(
        [{"evaluated": evaluated, "answers": {}, "error": "服务端返回 429（触发限流）"}]
    )
    for key in ("k1", "k2"):
        assert failed[key]["suppressed"] is False
        assert failed[key]["triage_state"] == notifications.JEV_TRIAGE_STATE_UNANNOTATED
        assert failed[key]["priority"] is None
        assert "按放行处理" in failed[key]["note"]
        assert "429" in failed[key]["note"], "失败原因要如实透出，便于排查"

    partial = _findings({})
    assert partial["triage_state"] == notifications.JEV_TRIAGE_STATE_UNANNOTATED
    assert partial["suppressed"] is False

    # 选项不在定义域内 → 同样按「未取得判定」处理，不猜
    undefined = _findings(_answers(0.9, 0.9) | {
        f"n1_{notifications.JEV_TRIAGE_QUESTION_MATERIAL}": _choice(
            f"n1_{notifications.JEV_TRIAGE_QUESTION_MATERIAL}", "irrelevant", 0.9
        )
    })
    assert undefined["triage_state"] == notifications.JEV_TRIAGE_STATE_UNANNOTATED
    assert undefined["suppressed"] is False


def test_jev_triage_summary_splits_three_states():
    rows = [
        _notification(triage=NotificationTriage(
            impact="immaterial", priority=0, suppressed=True, triage_state="suppressed"
        )),
        _notification(triage=NotificationTriage(
            impact="material", priority=2, triage_state="notified"
        )),
        _notification(triage=NotificationTriage(triage_state="unannotated")),
        _notification(),  # triage=None：这轮根本没分诊
    ]
    stats = notifications.jev_triage_summary(rows)
    assert stats["total"] == 4
    assert stats["suppressed"] == 1 and stats["notified"] == 1 and stats["unannotated"] == 2
    assert stats["impacts"] == {"material": 1, "uncertain": 0, "immaterial": 1}
    assert stats["priorities"]["0"] == 1 and stats["priorities"]["2"] == 1


# ——————————————————————————————————————————————————————————————
# 5. 端点：evaluate 分诊 / pending 过滤 / triage 报告
# ——————————————————————————————————————————————————————————————

_SUPPRESS_MARKER = "纯行情播报"
_SUPPRESS_SUMMARY = "本条为纯行情播报：股价当日上涨 1.2%，成交量温和放大。"
_MATERIAL_SUMMARY = "公司公告：与甲方签署重大合同，金额 12 亿元，占去年营收 18%。"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(api_app, "resolve_store", lambda db: credential_store.DbCredentialStore(db))
    token = "test-session-token"
    app = create_app(CoreSettings(session_token=token, data_dir=tmp_path))
    return TestClient(app), {"X-Core-Session-Token": token}


def _enable_jev(http, headers) -> None:
    assert http.put(
        f"/credentials/{credential_store.JEV_API_KEY}",
        json={"secret": "sk-jev-secret"},
        headers=headers,
    ).status_code == 200
    saved = http.put("/jev/config", headers=headers, json={
        "enabled": True,
        "base_url": "https://openrouter.ai/api/v1/decisions",
        "model": "typesafe/jev-1.13",
        "credential_ref": credential_store.JEV_API_KEY,
        "timeout_secs": 60,
    })
    assert saved.status_code == 200, saved.text


def _user_id(http, headers) -> UUID:
    return UUID(http.get("/session", headers=headers).json()["user_id"])


def _stable(http, headers, *, instrument: str, summary: str) -> dict[str, object]:
    """构造「每次调用都生成新实例、但 dedup 键相同」的通知——用于验证复用走的是库而不是内存。

    `thesis_id` 按标的派生：dedup 键 = `thesis_id:condition_kind:triggered_by:anchor`，
    不同标的必须是不同键，否则两条通知会被当成同一条提醒（这正是本文件一个回归用例的主题）。
    """
    user_id = _user_id(http, headers)
    return {
        "notification_id": uuid4(),
        "user_id": user_id,
        "thesis_id": uuid5(_THESIS_NS, instrument),
        "instrument": instrument,
        "triggered_by": "跌破 MA60",
        "condition_kind": "invalidation_condition",
        "title": f"{instrument} 命中已确认失效条件",
        "summary": summary,
        "action_mode": "review_plan",
        "evidence_refs": [],
        "created_at": datetime.now(UTC),
        "data_time": datetime.now(UTC),
    }


def _fake_jev(*, low_confidence: bool = False):
    """按 state 内容决定判定——**不依赖排序**，测试不会因为分片次序变化而假通过。

    含「纯行情播报」标记的通知 → `immaterial` + 优先级 0；其余 → `material` + 优先级 2。
    """
    calls: list[dict[str, object]] = []
    material_conf = 0.42 if low_confidence else 0.90
    priority_conf = 0.44 if low_confidence else 0.88

    def _fake(_base_url, payload, _api_key, _timeout):
        calls.append(payload)
        answers: dict[str, dict[str, object]] = {}
        for block in str(payload["state"]).split("【通知 "):
            if not block.strip():
                continue
            index = block.split("】", 1)[0].strip()
            if not index.isdigit():
                continue
            immaterial = _SUPPRESS_MARKER in block
            answers[f"n{index}_{notifications.JEV_TRIAGE_QUESTION_MATERIAL}"] = {
                "type": "choice",
                "choice": "immaterial" if immaterial else "material",
                "probabilities": {"immaterial" if immaterial else "material": material_conf},
                "confidence": material_conf,
            }
            answers[f"n{index}_{notifications.JEV_TRIAGE_QUESTION_PRIORITY}"] = {
                "type": "score",
                "score": 0.0 if immaterial else 2.0,
                "probabilities": {"0" if immaterial else "2": priority_conf},
                "confidence": priority_conf,
            }
        return {
            "model": "typesafe/jev-1.13-20260917",
            "answers": answers,
            "usage": {"input_tokens": 1500, "output_tokens": 24},
        }

    return _fake, calls


def test_evaluate_suppresses_low_relevance_but_never_deletes_it(client, monkeypatch):
    """JV08 验收核心：压制 = 不弹站内、不外发，但**照常落库且可翻到**。"""
    http, headers = client
    _enable_jev(http, headers)
    noise = _stable(http, headers, instrument="510300", summary=_SUPPRESS_SUMMARY)
    signal = _stable(http, headers, instrument="600519", summary=_MATERIAL_SUMMARY)
    monkeypatch.setattr(api_app, "evaluate_notifications", lambda db, user: [
        Notification(**noise), Notification(**signal),
    ])
    fake, calls = _fake_jev()
    monkeypatch.setattr(jev_client, "_post_endpoint", fake)

    evaluated = http.post("/notifications/evaluate", headers=headers).json()
    assert len(calls) == 1, "两条通知应合成一个分片、只出网一次"
    by_instrument = {item["instrument"]: item for item in evaluated}

    noise_item = by_instrument["510300"]
    assert noise_item["triage"]["suppressed"] is True
    assert noise_item["triage"]["triage_state"] == notifications.JEV_TRIAGE_STATE_SUPPRESSED
    assert noise_item["triage"]["impact"] == "immaterial"
    assert noise_item["triage"]["priority"] == 0
    assert noise_item["delivery_status"] == "pending"
    assert "低相关" in noise_item["last_delivery_error"]

    signal_item = by_instrument["600519"]
    assert signal_item["triage"]["suppressed"] is False
    assert signal_item["triage"]["triage_state"] == notifications.JEV_TRIAGE_STATE_NOTIFIED

    # 软校验铁律：它**没有消失**——库里仍在，只是不进默认列表
    assert len(http.get("/notifications/pending", headers=headers).json()) == 1
    assert http.get("/notifications/pending", headers=headers).json()[0]["instrument"] == "600519"

    report = http.get("/notifications/triage", headers=headers).json()
    assert report["enabled"] is True
    assert report["model"] == "typesafe/jev-1.13-20260917"
    assert report["total"] == 2 and report["suppressed"] == 1 and report["notified"] == 1
    assert report["unannotated"] == 0
    assert [item["instrument"] for item in report["suppressed_items"]] == ["510300"]
    assert report["suppressed_items"][0]["triage"]["note"]
    assert "不删除记录" in report["note"]


def test_evaluate_groups_duplicate_dedup_keys_into_one_judgement(client, monkeypatch):
    """同一 dedup 键 = 同一条提醒：同批次内只送评一次，结论共用。

    回归用例。用户把同一失效条件写两遍（`["跌破 MA60", "跌破 MA60"]`）就会命中这条路径。
    曾经的写法把两条都塞进分片、却按 dedup 键合并结论，于是**后一条的判定会覆盖前一条**，
    让前者静默拿到别人的结论——这正是 JV08 要防的那类静默误判。
    """
    http, headers = client
    _enable_jev(http, headers)
    shared = {
        "user_id": _user_id(http, headers),
        "thesis_id": UUID("11111111-1111-1111-1111-111111111111"),
        "instrument": "510300",
        "triggered_by": "跌破 MA60",
        "condition_kind": "invalidation_condition",
        "title": "510300 命中已确认失效条件",
        "action_mode": "review_plan",
        "evidence_refs": [],
        "created_at": datetime.now(UTC),
        "data_time": datetime.now(UTC),
    }
    monkeypatch.setattr(api_app, "evaluate_notifications", lambda db, user: [
        Notification(notification_id=uuid4(), summary=_SUPPRESS_SUMMARY, **shared),
        Notification(notification_id=uuid4(), summary=_MATERIAL_SUMMARY, **shared),
    ])
    fake, calls = _fake_jev()
    monkeypatch.setattr(jev_client, "_post_endpoint", fake)

    evaluated = http.post("/notifications/evaluate", headers=headers).json()
    assert len(calls) == 1
    state = str(calls[0]["state"])
    assert "【通知 1】" in state
    assert "【通知 2】" not in state, "同一条提醒不该送评两次"
    assert len(calls[0]["questions"]) == 2, "一次送评 = 两道题（相关性 + 优先级）"
    # 同一条提醒的两次评估必须拿到同一个结论，不能一条压制一条放行
    assert {item["triage"]["triage_state"] for item in evaluated if item["triage"]} == {
        notifications.JEV_TRIAGE_STATE_SUPPRESSED
    }


def test_evaluate_reuses_triage_for_same_dedup_key_without_second_call(client, monkeypatch):
    """按小时桶反复评估时，同一条提醒**不重复出网**——这是成本红线（R1 只按输入 token 计费）。"""
    http, headers = client
    _enable_jev(http, headers)
    payload = _stable(http, headers, instrument="510300", summary=_SUPPRESS_SUMMARY)
    # 每次评估都返回**新实例**（dedup 键相同）：若复用只靠内存对象，这条会失败
    monkeypatch.setattr(api_app, "evaluate_notifications", lambda db, user: [Notification(**payload)])
    fake, calls = _fake_jev()
    monkeypatch.setattr(jev_client, "_post_endpoint", fake)

    first = http.post("/notifications/evaluate", headers=headers).json()
    second = http.post("/notifications/evaluate", headers=headers).json()
    assert len(calls) == 1, "同一 dedup 键第二次评估不应再出网"
    assert first[0]["triage"]["suppressed"] is True
    assert second[0]["triage"]["suppressed"] is True, "复用的结论必须与首次一致"


def test_evaluate_marks_unannotated_and_releases_on_shard_failure(client, monkeypatch):
    """出网失败 → 如实标「未分诊」并**放行**（宁可不压，也不静默吞掉真提醒）。"""
    import urllib.error

    http, headers = client
    _enable_jev(http, headers)
    monkeypatch.setattr(api_app, "evaluate_notifications", lambda db, user: [
        Notification(**_stable(http, headers, instrument="510300", summary=_SUPPRESS_SUMMARY)),
    ])

    def _boom(*_args, **_kwargs):
        raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(jev_client, "_post_endpoint", _boom)
    evaluated = http.post("/notifications/evaluate", headers=headers).json()

    assert evaluated[0]["triage"]["triage_state"] == notifications.JEV_TRIAGE_STATE_UNANNOTATED
    assert evaluated[0]["triage"]["suppressed"] is False
    assert evaluated[0]["triage"]["impact"] is None
    # 失败 ≠ 压制：它照常进站内列表
    assert len(http.get("/notifications/pending", headers=headers).json()) == 1
    report = http.get("/notifications/triage", headers=headers).json()
    assert report["unannotated"] == 1 and report["suppressed"] == 0
    assert report["suppressed_items"] == []


def test_triage_report_reads_only_and_never_calls_out(client, monkeypatch):
    http, headers = client
    _enable_jev(http, headers)
    monkeypatch.setattr(jev_client, "_post_endpoint", lambda *a, **k: pytest.fail("查询不应出网"))
    report = http.get("/notifications/triage", headers=headers).json()
    assert report["enabled"] is True and report["total"] == 0
    assert report["suppressed_items"] == []


def test_zero_change_when_jev_disabled(client, monkeypatch):
    """**核心验收**：Jev 关闭时通知链路零变化——零出网、`triage` 恒为 None、报告如实说「没跑」。"""
    http, headers = client
    monkeypatch.setattr(jev_client, "_post_endpoint", lambda *a, **k: pytest.fail("Jev 关闭时不得出网"))
    monkeypatch.setattr(api_app, "evaluate_notifications", lambda db, user: [
        Notification(**_stable(http, headers, instrument="510300", summary=_SUPPRESS_SUMMARY)),
    ])

    evaluated = http.post("/notifications/evaluate", headers=headers).json()
    assert evaluated[0]["triage"] is None, "没分诊就是没分诊，不能伪造一个空结论"
    # 没分诊 → 不压制 → 站内照常可见（与接入前逐字节一致）
    assert len(http.get("/notifications/pending", headers=headers).json()) == 1

    report = http.get("/notifications/triage", headers=headers).json()
    assert report["enabled"] is False
    assert report["suppressed"] == 0 and report["suppressed_items"] == []
    assert report["unannotated"] == 1
    assert "未启用" in report["note"]

"""决策与研究长期能力服务层（路线图 8.1-8.3）。

- 决策时间线（8.1）：从既有事实源（研究/证据/逻辑版本/计划/决定）拼装事件,
  以决定时间戳区分「当时信息/事后信息」;关联缺失显式显示为缺口,绝不补写历史。
- 可检验判断（8.2）：原判断冻结;到期验证只追加结果;状态机显式。
- 观察事项（8.3）：检查记录只追加;触发 → 进入研究/调整计划,保留触发证据;
  dedup 防重复提醒;暂停/结束/恢复状态机。

排序与时区冻结：全部 UTC（ISO 8601 字符串存储,datetime 对象流转）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from investment_steward_core import feed_health, feed_probe
from investment_steward_core.domain.longterm import (
    JudgmentStatus,
    TimelineEvent,
    TimelineEventKind,
    VerificationResult,
    WatchCheck,
    WatchItem,
    WatchStatus,
)
from investment_steward_core.storage.database import Database


# ———————————————— 8.1 决策时间线 ————————————————

def _as_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def build_decision_timeline(db: Database, user_id: UUID, decision_id: UUID) -> dict[str, Any] | None:
    """拼装一条决定的完整时间线。

    口径冻结（8.1）:
    - 「当时信息」= 事件时间 <= 决定时间;「事后信息」= 决定时间之后发生;
      无法解析时间的事件标记 unknown（显示为缺口,不猜测）。
    - 决定本身的关联字段（linked_evidence_ids / plan_id）缺失且数据库查不到 → 缺口事件。
    """
    decision = db.get_decision(decision_id, user_id)
    if decision is None:
        return None
    decided_at = _as_utc(decision.made_at)
    events: list[TimelineEvent] = []

    def scope_of(at: datetime | None) -> str:
        if at is None or decided_at is None:
            return "unknown"
        return "at_the_time" if at <= decided_at else "afterwards"

    events.append(TimelineEvent(
        kind=TimelineEventKind.DECISION, ref_id=decision.decision_id,
        title=decision.theme[:300], detail=decision.decision_summary,
        at=decided_at, knowledge_scope="at_the_time",
    ))

    # 投资逻辑版本与研究运行:当前数据模型中决定没有显式的 thesis/run 关联字段。
    # 按 8.1 铁律「缺少关联显示缺口,不用当前记录补写过去上下文」——如实记缺口。
    if decision.plan_id is None:
        events.append(TimelineEvent(
            kind=TimelineEventKind.PLAN, title="计划(未关联)",
            detail="决定未关联计划(plan_id 为空),无法还原当时的计划上下文。",
            knowledge_scope="unknown", link_available=False,
        ))
    if not decision.linked_evidence_ids:
        events.append(TimelineEvent(
            kind=TimelineEventKind.EVIDENCE, title="证据(未关联)",
            detail="决定未关联任何证据,无法还原当时的证据集合。",
            knowledge_scope="unknown", link_available=False,
        ))
    events.append(TimelineEvent(
        kind=TimelineEventKind.RESEARCH_RUN, title="研究运行(无显式关联)",
        detail="系统在决定创建时未记录研究运行关联,此处显示为缺口,不用当前研究记录补写。",
        knowledge_scope="unknown", link_available=False,
    ))
    events.append(TimelineEvent(
        kind=TimelineEventKind.THESIS_VERSION, title="投资逻辑版本(无显式关联)",
        detail="系统在决定创建时未记录投资逻辑版本关联,此处显示为缺口,不用当前逻辑版本补写。",
        knowledge_scope="unknown", link_available=False,
    ))

    # 计划(有显式关联才展开;get_plan 对缺失引用抛 KeyError → 如实记缺口)
    if decision.plan_id is not None:
        try:
            plan = db.get_plan(decision.plan_id, user_id)
        except KeyError:
            plan = None
        if plan is not None:
            events.append(TimelineEvent(
                kind=TimelineEventKind.PLAN, ref_id=plan.plan_id,
                title=f"计划:{plan.title}",
                detail=f"状态 {plan.status.value if hasattr(plan.status, 'value') else plan.status}",
                at=_as_utc(plan.created_at),
                knowledge_scope=scope_of(_as_utc(plan.created_at)),
            ))
        else:
            events.append(TimelineEvent(
                kind=TimelineEventKind.PLAN, ref_id=decision.plan_id,
                title="计划(引用缺失)", detail="决定引用了计划,但本机数据库中不存在该计划。",
                knowledge_scope="unknown", link_available=False,
            ))

    # 证据（当时信息;Evidence 的时间口径 = collected_at）
    for evidence_id in decision.linked_evidence_ids:
        evidence = db.get_evidence(evidence_id, user_id)
        if evidence is not None:
            observed_at = _as_utc(evidence.collected_at)
            events.append(TimelineEvent(
                kind=TimelineEventKind.EVIDENCE, ref_id=evidence.evidence_id,
                title=str(getattr(evidence, "summary", evidence.evidence_id))[:300],
                detail=f"来源 {evidence.source_name}",
                at=observed_at,
                knowledge_scope=scope_of(observed_at),
            ))
        else:
            events.append(TimelineEvent(
                kind=TimelineEventKind.EVIDENCE, ref_id=evidence_id,
                title="证据(引用缺失)", detail="决定引用了证据,但本机数据库中不存在该证据。",
                knowledge_scope="unknown", link_available=False,
            ))

    # 执行结果与复盘(事后信息)
    if decision.outcome:
        events.append(TimelineEvent(
            kind=TimelineEventKind.OUTCOME, ref_id=decision.decision_id,
            title="执行结果", detail=decision.outcome, knowledge_scope="afterwards",
        ))
    if decision.retrospective:
        events.append(TimelineEvent(
            kind=TimelineEventKind.RETROSPECTIVE, ref_id=decision.decision_id,
            title="复盘", detail=decision.retrospective, knowledge_scope="afterwards",
        ))

    # 排序:缺时间(unknown)排最后,其余按时间升序
    events.sort(key=lambda e: (e.at is None, e.at or datetime.max.replace(tzinfo=UTC)))
    gaps = [e for e in events if not e.link_available or e.knowledge_scope == "unknown"]
    return {
        "decision_id": str(decision.decision_id),
        "decided_at": decided_at.isoformat() if decided_at else None,
        "events": [json.loads(e.model_dump_json()) for e in events],
        "gaps": [
            {"kind": e.kind.value, "ref_id": str(e.ref_id) if e.ref_id else None, "title": e.title}
            for e in gaps
        ],
        "rule": "当时信息=事件时间≤决定时间;事后信息=决定之后发生;时间缺失显示为缺口,不补写历史。",
    }


# ———————————————— 8.2 可检验判断 ————————————————

def create_judgment(db: Database, **kwargs: Any) -> Any:
    from investment_steward_core.domain.longterm import VerifiableJudgment

    judgment = VerifiableJudgment(judgment_id=uuid4(), **kwargs)
    db.upsert_judgment(judgment)
    return judgment


#: 研报验证点派生判断时填的「方向」：验证点本身不含多空方向，不猜，
#: 用一个自述来源的中性词，让列表里一眼能看出它不是人工录入的多空判断。
WATCHPOINT_DIRECTION = "观察（来自研报验证点）"


def watchpoint_judgment_id(report_id: str, index: int) -> UUID:
    """验证点→判断 id 的**确定性派生**（G01）。

    用 `uuid5(namespace, "ai-research-watchpoint:{report_id}:{index}")` 而不是随机 id：
    队列每次被读都会尝试落库，派生 id 让重复读取天然幂等——不会每开一次页面就多出一条
    看起来像新判断的记录（这是「读路径首次落库」能成立的前提）。
    序号 index 取归一后的验证点下标（`normalize_watchpoints` 保序、上限 5 条）。
    """
    return uuid5(NAMESPACE_URL, f"ai-research-watchpoint:{report_id}:{index}")


def materialize_watchpoint_judgment(
    db: Database,
    user_id: UUID,
    *,
    report_id: str,
    symbol: str,
    signal: str,
    verify_by: str,
    expected_if_true: str,
    due_on: str,
    index: int,
) -> tuple[Any | None, str]:
    """把一条**已到期可判**的研报验证点落成可回填的判断（幂等）。返回 `(judgment, note)`。

    冻结口径（不要放宽）：
    - 判断原文 = 验证点原话（`signal`），不改写、不润色；
    - `due_at` 只从**能解析出的确切日期**（`due_on`）取。锚定事件（「三季报披露后」）没有日期，
      而 `due_at` 是必填字段——**不编一个日期**，此时不落库并如实说明原因；
    - 已存在的判断原样返回，**不覆盖**用户可能已做的回填（只有 status 会随后续 verify 变化）。
    """
    from investment_steward_core.domain.longterm import VerifiableJudgment

    if not (signal or "").strip():
        return None, "验证点没有观察信号原文，无法落库。"
    if not due_on:
        return None, "验证时点锚定事件、无确切日期，而判断必须带到期时间——不猜日期，故不自动落库。"
    try:
        due_at = datetime.fromisoformat(due_on).replace(tzinfo=UTC)
    except ValueError:
        return None, f"验证点到期日无法解析（{due_on}），不落库。"

    judgment_id = watchpoint_judgment_id(report_id, index)
    existing = db.get_judgment(judgment_id, user_id)
    if existing is not None:
        return existing, ""

    judgment = VerifiableJudgment(
        judgment_id=judgment_id,
        user_id=user_id,
        subject=(symbol or report_id)[:200],
        direction=WATCHPOINT_DIRECTION,
        statement=(signal or "")[:4000],
        trigger_conditions=[expected_if_true.strip()[:400]] if expected_if_true.strip() else [],
        invalidation_conditions=[],
        due_at=due_at,
        status=JudgmentStatus.PENDING,
        source_report_id=str(report_id),
        source_refs=[ref for ref in (verify_by.strip()[:200],) if ref],
    )
    db.upsert_judgment(judgment)
    return judgment, ""


def verify_judgment(
    db: Database,
    user_id: UUID,
    judgment_id: UUID,
    *,
    result: str,
    outcome: str = "",
    metrics: dict[str, Any] | None = None,
) -> tuple[Any, Any]:
    """到期验证:只追加验证结果并推进状态;原判断文本/字段零改动。

    状态口径（冻结）:verified=已验证 / refuted=被否定 / insufficient_data=数据不足。
    """
    from investment_steward_core.domain.longterm import JudgmentVerification

    judgment = db.get_judgment(judgment_id, user_id)
    if judgment is None:
        raise ValueError(f"判断不存在:{judgment_id}")
    result_enum = VerificationResult(result)
    verification = JudgmentVerification(
        verification_id=uuid4(), judgment_id=judgment_id,
        result=result_enum, outcome=outcome, metrics=metrics or {},
    )
    db.insert_judgment_verification(verification)
    judgment.status = JudgmentStatus(result_enum.value)
    db.upsert_judgment(judgment)  # 只更新 status 列,payload 原文未变(同 id 覆盖同内容)
    return judgment, verification


# ———————————————— G02 后验命中率（复盘与学习） ————————————————

#: 命中率口径。**随返回体一起下发**，前端与文档共用同一份措辞，避免两处口径各写一遍后漂移。
#: 每个键都是用户可见的一句话，改动前先想清楚会不会让某个数字变得无法复算。
JUDGMENT_HIT_RATE_POLICY: dict[str, str] = {
    "numerator": "命中「成立」的判定数（最近的回填结果 result = verified）",
    "denominator": "已回填且结论明确的判定数（成立 + 失效）",
    "excluded": "「无数据」(insufficient_data) 与尚未回填的判定不进分母，数量仍如实展示",
    "hit_rate": "命中率 = 成立 /（成立 + 失效）；分母为 0 时返回 null 并说明样本不足，不显示 0%",
    "model": "模型方案取自判定来源研报的 model 字段；无来源研报时归入「（未标注模型方案）」，不猜",
    "theme": "主题优先取来源研报标题；无来源研报（或报告已删除）时取判断对象，并以 theme_source 标注取值来源",
    "window": "时间窗按判定**到期时间**筛选（含今天、不含未到期），不是回填时间",
    "verification_value": "每个判定用**最近一次**回填结果（与 POST /judgments/{id}/verify 的追加语义一致）",
    "no_auto_policy": (
        "命中率只用于复盘展示，不自动调整投资原则——原则变更仍须走显式确认链"
        "（POST /investment-policies 的提议 + 确认），本功能不新增静默改原则的写路径"
    ),
}

#: 无来源研报（手工录入判断）时的模型方案标签。用可读文案而不是空串：空串会被误读成「方案未知但存在」。
HIT_RATE_MODEL_UNKNOWN = "（未标注模型方案）"


def _hit_rate(verified: int, refuted: int) -> float | None:
    """命中率 = 成立 /（成立 + 失效）。分母为 0 时返回 None——**不返回 0.0**（样本不足 ≠ 全错）。"""
    denominator = verified + refuted
    if denominator <= 0:
        return None
    return round(verified / denominator, 4)


def judgment_hit_rate(
    db: Database,
    user_id: UUID,
    *,
    window_days: int | None,
    limit_per_bucket: int = 200,
) -> dict[str, Any]:
    """后验命中率：按「模型方案 × 主题」分桶，每个桶自带成员判定与回填记录（G02）。

    设计约束（与路线图 §9 G02 对齐）：
    - **含分母与口径**：每个桶给出 counts 与 `hit_rate`（`policy` 随返回体下发，逐条可核）；
    - **可展开**：桶内直接带成员判定（原文 `statement` + 最近一次回填记录），
      任一命中率都能点开看到它由哪些判定算出来；超过 `limit_per_bucket` 截断并显式标注；
    - **只读**：本函数不写库、不改原则。命中率与「要不要改投资原则」之间没有任何自动通道。

    时间窗按 `due_at` 选（含今天、不含未到期）：还没到期的判定不参与后验——「未到期」不等于
    「已验证」，也不算「该回填没回填」，故单列 `not_due_excluded` 计数如实告知。
    """
    now = datetime.now(UTC)
    cutoff = None if window_days is None else now - timedelta(days=window_days)

    judgments = db.list_judgments(user_id)
    verifications = db.list_all_judgment_verifications(user_id)
    report_ids = [str(item.source_report_id) for item in judgments if item.source_report_id]
    reports = db.get_ai_research_reports_by_ids(report_ids)

    counts = {"verified": 0, "refuted": 0, "insufficient_data": 0, "pending": 0}
    not_due_excluded = 0
    buckets: dict[tuple[str, str], dict[str, Any]] = {}

    for judgment in judgments:
        due_at = judgment.due_at
        if due_at.tzinfo is None:  # 老记录可能是 naive；按全库 UTC 口径补上时区，不改存储
            due_at = due_at.replace(tzinfo=UTC)
        if due_at > now:
            not_due_excluded += 1
            continue
        if cutoff is not None and due_at < cutoff:
            continue

        report = reports.get(str(judgment.source_report_id or "")) or {}
        raw_model = report.get("model")
        model_label = raw_model.strip() if isinstance(raw_model, str) and raw_model.strip() else HIT_RATE_MODEL_UNKNOWN
        raw_title = report.get("title")
        if isinstance(raw_title, str) and raw_title.strip():
            theme_label, theme_source = raw_title.strip(), "report_title"
        else:
            theme_label, theme_source = judgment.subject, "judgment_subject"

        status_key = str(getattr(judgment.status, "value", judgment.status))
        if status_key not in counts:
            counts[status_key] = 0
        counts[status_key] += 1

        bucket = buckets.setdefault(
            (model_label, theme_label),
            {
                "model": model_label,
                "theme": theme_label,
                "theme_source": theme_source,
                "counts": {"verified": 0, "refuted": 0, "insufficient_data": 0, "pending": 0},
                "judgments": [],
                "truncated": False,
            },
        )
        bucket["counts"][status_key] = bucket["counts"].get(status_key, 0) + 1

        if len(bucket["judgments"]) < limit_per_bucket:
            records = verifications.get(str(judgment.judgment_id), [])
            latest = records[-1] if records else None
            bucket["judgments"].append({
                "judgment_id": str(judgment.judgment_id),
                "subject": judgment.subject,
                "statement": judgment.statement,
                "direction": judgment.direction,
                "due_at": judgment.due_at.isoformat(),
                "status": status_key,
                "source_report_id": judgment.source_report_id,
                "source_refs": list(judgment.source_refs),
                "verification": latest.model_dump(mode="json") if latest is not None else None,
            })
        else:
            bucket["truncated"] = True

    rows: list[dict[str, Any]] = []
    for bucket in buckets.values():
        bucket_counts = bucket["counts"]
        verified = int(bucket_counts.get("verified", 0))
        refuted = int(bucket_counts.get("refuted", 0))
        bucket["hit_rate"] = _hit_rate(verified, refuted)
        bucket["hit_rate_denominator"] = verified + refuted
        bucket["hit_rate_note"] = (
            "样本不足：分母 0（成立 0 + 失效 0），不显示命中率。"
            if bucket["hit_rate"] is None
            else None
        )
        bucket["judgment_count"] = sum(bucket_counts.values())
        rows.append(bucket)

    rows.sort(key=lambda item: (-int(item["judgment_count"]), str(item["model"]), str(item["theme"])))

    total_verified = int(counts.get("verified", 0))
    total_refuted = int(counts.get("refuted", 0))
    return {
        "ok": True,
        "today": now.date().isoformat(),
        "window_days": window_days,
        "policy": dict(JUDGMENT_HIT_RATE_POLICY),
        "totals": {
            "judgments": sum(counts.values()),
            **counts,
            "denominator": total_verified + total_refuted,
            "hit_rate": _hit_rate(total_verified, total_refuted),
            "not_due_excluded": not_due_excluded,
            "buckets": len(rows),
        },
        "buckets": rows,
        "note": (
            "命中率只统计已回填且结论明确的判定；展开任一桶可看到参与的判定原文与回填记录。"
            "命中率不自动改投资原则。"
        ),
    }


# ———————————————— 8.3 观察事项 ————————————————

WATCH_TRANSITIONS: dict[str, set[str]] = {
    # 当前状态 → 允许的目标状态(显式状态机,禁止越级)
    "active": {"paused", "triggered", "closed"},
    "paused": {"active", "closed"},
    "triggered": {"active", "closed"},   # 触发处理后回到观察或结束
    "closed": {"active"},                # 历史恢复=重新打开
}


def create_watch_item(db: Database, **kwargs: Any) -> Any:
    item = WatchItem(watch_id=uuid4(), **kwargs)
    db.upsert_watch_item(item)
    return item


def transition_watch_item(db: Database, user_id: UUID, watch_id: UUID, target: str) -> Any:
    item = db.get_watch_item(watch_id, user_id)
    if item is None:
        raise ValueError(f"观察事项不存在:{watch_id}")
    allowed = WATCH_TRANSITIONS.get(item.status.value, set())
    if target not in allowed:
        raise ValueError(f"非法状态迁移:{item.status.value} → {target}(允许:{sorted(allowed)})")
    item.status = WatchStatus(target)
    item.updated_at = datetime.now(UTC)
    db.upsert_watch_item(item)
    return item


def record_watch_check(
    db: Database,
    user_id: UUID,
    watch_id: UUID,
    *,
    observed: str,
    triggered: bool = False,
    note: str = "",
    evidence_refs: list[UUID] | None = None,
    dedup_value: str | None = None,
) -> tuple[Any, Any, bool]:
    """记录一次检查(dedup 防重复);触发时事项进入 triggered 状态。

    返回 (事项, 检查记录, 是否实际写入);重复 dedup 返回既有记录且不重复计提醒。
    """
    item = db.get_watch_item(watch_id, user_id)
    if item is None:
        raise ValueError(f"观察事项不存在:{watch_id}")
    if item.status == WatchStatus.CLOSED:
        raise ValueError("观察事项已结束,如需继续观察请先恢复(active)。")
    if dedup_value:
        existing = db.find_watch_check_by_dedup(watch_id, dedup_value)
        if existing is not None:
            return item, existing, False
        note = f"dedup:{dedup_value} {note}".strip()
    check = WatchCheck(
        check_id=uuid4(), watch_id=watch_id, observed=observed,
        triggered=triggered, note=note, evidence_refs=evidence_refs or [],
    )
    db.insert_watch_check(check)
    if triggered and item.status == WatchStatus.ACTIVE:
        item.status = WatchStatus.TRIGGERED
        item.updated_at = datetime.now(UTC)
        db.upsert_watch_item(item)
    return item, check, True


# ———————————————— 8.4 组合风险视图 ————————————————

def build_portfolio_risk(db: Database, user_id: UUID) -> dict[str, Any]:
    """组合风险视图(8.4):基于本机事实源可算的部分,算不出的显式标「数据不足」。

    口径冻结:
    - 标的集中度 = 各标的关联的投资逻辑数量占比(无市值数据,不做权重口径,如实标注);
    - 共同失效条件 = 出现在 ≥2 个不同标的的投资逻辑中的失效条件原文(统计相关,非因果);
    - 相关性/因子暴露/共同失效的市场数据口径:本机无收益序列与因子库 → 数据不足缺口。
    """
    theses = db.list_theses(user_id)
    holdings = db.list_holdings(user_id)
    by_instrument: dict[str, list[Any]] = {}
    for thesis in theses:
        if thesis.status.value == "archived" if hasattr(thesis.status, "value") else False:
            continue
        by_instrument.setdefault(thesis.instrument, []).append(thesis)

    instrument_view = []
    condition_owners: dict[str, set[str]] = {}
    for instrument, items in sorted(by_instrument.items()):
        invalidations: list[str] = []
        for thesis in items:
            for cond in thesis.invalidation_conditions:
                invalidations.append(cond)
                condition_owners.setdefault(cond.strip(), set()).add(instrument)
        instrument_view.append({
            "instrument": instrument,
            "thesis_count": len(items),
            "invalidation_conditions": invalidations,
            "in_watchlist": any(
                h.instrument == instrument and getattr(h.status, "value", str(h.status)) == "watchlist"
                for h in holdings
            ),
        })

    shared_conditions = [
        {"condition": cond, "instruments": sorted(owners), "instrument_count": len(owners)}
        for cond, owners in sorted(condition_owners.items(), key=lambda kv: -len(kv[1]))
        if len(owners) >= 2
    ]
    total_theses = sum(len(v) for v in by_instrument.values())
    concentration = [
        {"instrument": item["instrument"],
         "logic_share": round(item["thesis_count"] / total_theses, 4) if total_theses else 0.0}
        for item in sorted(instrument_view, key=lambda x: -x["thesis_count"])
    ]
    return {
        "instruments": instrument_view,
        "concentration_by_logic_count": concentration,
        "shared_invalidation_conditions": shared_conditions,
        "gaps": [
            {"dimension": "correlation", "reason": "本机无收益序列数据,相关性无法计算(数据不足)。"},
            {"dimension": "factor_exposure", "reason": "本机无因子库,因子暴露无法计算(数据不足)。"},
            {"dimension": "weight_concentration", "reason": "持仓无市值/权重数据,集中度以逻辑数量口径替代,非市值口径。"},
        ],
        "rule": "集中度=逻辑数量口径(非市值);共同失效条件=统计相关,不构成因果解释;缺口如实显示。",
    }


# ———————————————— 8.5 通知分级与合并 + 数据源质量 ————————————————

# 分级口径(8.5,冻结):失效条件=直接触发失效条件;支持条件=需要研究;观察指标=信息参考。
NOTIFICATION_GRADE_BY_CONDITION = {
    "invalidation_condition": "invalidation_triggered",
    "supporting_condition": "needs_research",
    "observation_metric": "reference",
}


def grade_and_merge_notifications(notifications: list[Any]) -> dict[str, Any]:
    """按影响分级并合并同一事件(同标的+同命中条件原文),保留每个来源与证据关系。"""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for n in notifications:
        grade = NOTIFICATION_GRADE_BY_CONDITION.get(n.condition_kind, "reference")
        key = (str(n.instrument or ""), n.triggered_by.strip())
        group = merged.setdefault(key, {
            "instrument": str(n.instrument or "") or None,
            "triggered_by": n.triggered_by,
            "grade": grade,
            "notifications": [],
            "evidence_refs": [],
        })
        group["notifications"].append({
            "notification_id": str(n.notification_id),
            "title": n.title,
            "summary": n.summary,
            "created_at": n.created_at.isoformat(),
            "read": n.read,
        })
        for ref in n.evidence_refs:
            if str(ref) not in [str(r) for r in group["evidence_refs"]]:
                group["evidence_refs"].append(ref)
    groups = sorted(merged.values(), key=lambda g: (g["grade"], str(g["instrument"])))
    grade_order = {"invalidation_triggered": 0, "needs_research": 1, "reference": 2}
    groups.sort(key=lambda g: grade_order.get(g["grade"], 9))
    return {
        "grades": [
            {"grade": "invalidation_triggered", "label": "直接触发失效条件"},
            {"grade": "needs_research", "label": "影响计划/需要研究"},
            {"grade": "reference", "label": "信息参考"},
        ],
        "groups": groups,
        "rule": "合并键=同标的+同命中条件原文;合并不丢失证据(组内 evidence_refs 去重并集)。",
    }


def data_source_quality(db: Database, user_id: UUID, window_days: int = 7) -> dict[str, Any]:
    """数据源质量（8.5 + F02/F03）。

    两条事实源合并，**精确同名**才合并（不做模糊归一，宁可同一源出现两行也不虚报一个数字）：
    - 证据侧：按 `source_name` 的存量聚合（单次 SQL，含类型分布、首末采集时刻）；
    - 采集侧：`feed_attempts` 的窗口聚合（尝试数、成功率、延迟分位、错误类别分布）。

    成功率与延迟在**本窗口无采集日志**时为 None，并如实给出缺口说明——不再把「没记录」
    说成「数据不足所以一律 None」。
    """
    summary_rows = db.evidence_source_summary(user_id)
    evidence_by_source: dict[str, dict[str, Any]] = {}
    for row in summary_rows:
        name = row.get("source_name")
        if not name:
            # 无来源名的历史证据不并入任何源（不造一个「未知源」汇总行掩盖它）。
            continue
        entry = evidence_by_source.setdefault(
            str(name),
            {
                "source": str(name),
                "evidence_count": 0,
                "first_collected_at": None,
                "last_collected_at": None,
                "coverage": {},
                "producers": set(),
            },
        )
        count = int(row.get("count") or 0)
        entry["evidence_count"] += count
        first_at = row.get("first_at")
        last_at = row.get("last_at")
        if first_at and (entry["first_collected_at"] is None or str(first_at) < entry["first_collected_at"]):
            entry["first_collected_at"] = str(first_at)
        if last_at and (entry["last_collected_at"] is None or str(last_at) > entry["last_collected_at"]):
            entry["last_collected_at"] = str(last_at)
        kind = str(row.get("evidence_type") or "unknown")
        entry["coverage"][kind] = entry["coverage"].get(kind, 0) + count
        producer = row.get("producer_plugin_id")
        if producer:
            entry["producers"].add(str(producer))

    attempts = db.list_feed_attempts(window_days)
    totals = db.list_feed_attempts_totals(window_days)
    truncated = max(0, int(totals.get("total", 0)) - len(attempts))
    attempts_by_source: dict[str, list[dict[str, Any]]] = {}
    for row in attempts:
        attempts_by_source.setdefault(str(row.get("source") or "unknown"), []).append(row)

    names = sorted(set(evidence_by_source) | set(attempts_by_source))
    plan = feed_probe.retry_plan(names)
    sources: list[dict[str, Any]] = []
    for name in names:
        evidence_entry = evidence_by_source.get(name)
        feed_rows = attempts_by_source.get(name, [])
        health = feed_health.summarize(feed_rows)
        entry: dict[str, Any] = {
            "source": name,
            "evidence_count": evidence_entry["evidence_count"] if evidence_entry else 0,
            "first_collected_at": evidence_entry["first_collected_at"] if evidence_entry else None,
            "last_collected_at": evidence_entry["last_collected_at"] if evidence_entry else None,
            "coverage": evidence_entry["coverage"] if evidence_entry else {},
            "producers": sorted(evidence_entry["producers"]) if evidence_entry else [],
            "last_attempt_at": feed_rows[0]["started_at"] if feed_rows else None,
            # F03：能否在此页重试（外部输入依赖如实声明，不给空按钮）。
            "retryable": plan[name]["retryable"],
            "retry_blocked_reason": plan[name]["reason"],
        }
        entry.update(health)
        if health["attempts"] == 0:
            entry["note"] = (
                f"近 {window_days} 天无采集日志（可能全是缓存命中，或该源未在本窗口被调用）→ 成功率与延迟不显示。"
            )
        elif health["errors"] == 0:
            entry["note"] = f"近 {window_days} 天采集全部成功；延迟为 HTTP 往返耗时，不含上游缓存命中。"
        else:
            entry["note"] = f"近 {window_days} 天有采集失败，错误类别见 error_kinds（按异常类型归类，不解析供应商文案）。"
        if entry["evidence_count"] == 0 and health["attempts"] > 0:
            # F03：把「该数据源尚未产生可入账的证据（InvestmentPage）」这类散落提示收拢到这里，
            # 判据是事实性的：有过取数、却没有一条入账证据。
            entry["note"] += " 注意：本窗口有取数尝试但**没有入账证据**，该源的数据尚未进入你的证据账本。"
        sources.append(entry)

    sources.sort(key=lambda item: (-(item["attempts"] or 0), -(item["evidence_count"] or 0), item["source"]))

    gaps: list[dict[str, Any]] = []
    no_sample = [item["source"] for item in sources if item["attempts"] == 0]
    if no_sample:
        gaps.append(
            {
                "dimension": "success_rate",
                "reason": (
                    "本窗口无采集日志的来源：" + "、".join(no_sample[:8]) + ("等" if len(no_sample) > 8 else "")
                    + "。成功率为 None 表示**没有样本**（不等于全部失败）；样本为 0 时不下结论。"
                ),
            }
        )
    if not sources:
        gaps.append({"dimension": "sample", "reason": "本机既无证据记录也无采集日志，无法评估任何来源。"})

    return {
        "sources": sources,
        "gaps": gaps,
        "window": f"{window_days}d",
        "window_days": window_days,
        "attempts_total": int(totals.get("total", 0)),
        "attempts_truncated": truncated,
        "rule": (
            f"统计窗口=最近 {window_days} 天；证据侧按 source_name 聚合（覆盖率/样本数可从证据记录复算）；"
            "采集侧按 feed_attempts 逐次尝试聚合（成功率=成功次数/尝试次数，延迟为最近秩分位，"
            "样本为 0 时一律 None）。两侧**精确同名**才合并，不做模糊归一。"
        ),
    }



# ———————————————— 8.6 内置研究模板与不行动统计 ————————————————

def builtin_research_templates() -> list[dict[str, Any]]:
    """8.6 五类内置研究模板:明确必填上下文与证据类型,不预设结论。"""
    return [
        {"name": "ETF 长期配置", "description": "指数/ETF 的长期配置研究:估值分位、资产属性与定投参数。",
         "required_context": ["标的", "估值口径", "配置期限", "定投参数"], "evidence_types": ["quote", "macro"],
         "default_params": {"horizon": "long_term"}},
        {"name": "个股财报", "description": "财报驱动的个股研究:营收/利润/指引与证伪条件。",
         "required_context": ["标的", "财报期", "核心假设", "失效条件"], "evidence_types": ["financial", "announcement"],
         "default_params": {}},
        {"name": "宏观影响", "description": "宏观事件对标的的影响链研究:事件→传导→标的。",
         "required_context": ["事件", "传导链", "受影响标的", "观察窗口"], "evidence_types": ["macro", "analysis"],
         "default_params": {}},
        {"name": "交易计划复盘", "description": "已结束计划的复盘:入场理由、执行偏差与结果归因。",
         "required_context": ["计划引用", "执行结果", "复盘时间"], "evidence_types": ["quote"],
         "default_params": {}},
        {"name": "技术形态与基本面检查", "description": "技术形态信号与基本面证据的双重复核。",
         "required_context": ["标的", "形态口径", "基本面证据", "冲突处理规则"], "evidence_types": ["quote", "analysis"],
         "default_params": {}},
    ]


def inaction_stats(decisions: list[Any]) -> dict[str, Any]:
    """「不行动」原因统计(8.6):只统计显式标记 inaction_reason 的决定,不猜测。"""
    labels = {
        "insufficient_evidence": "证据不足",
        "counter_not_excluded": "反证未排除",
        "against_principles": "不符合原则",
        "waiting_watch": "等待观察指标",
        "other": "其他",
    }
    counts: dict[str, int] = {}
    marked = 0
    for decision in decisions:
        reason = getattr(decision, "inaction_reason", None)
        if reason:
            marked += 1
            counts[reason] = counts.get(reason, 0) + 1
    total = len(decisions)
    return {
        "total_decisions": total,
        "marked_inaction": marked,
        "unmarked": total - marked,
        "counts": [
            {"reason": reason, "label": labels.get(reason, reason), "count": count,
             "share_of_marked": round(count / marked, 4) if marked else 0.0}
            for reason, count in sorted(counts.items(), key=lambda kv: -kv[1])
        ],
        "rule": "只统计显式标记了不行动原因的决定;未标记的决定计入 unmarked,不做推断。",
    }

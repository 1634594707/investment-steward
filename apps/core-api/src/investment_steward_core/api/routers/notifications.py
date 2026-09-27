"""通知域路由：分级与合并、待办读取、分诊报告、外发评估与已读标记。

A06（架构改进路线图 2026-09-25）第四批：把 6 条路由与它们的 12 个辅助从 `api/app.py`
整体迁出。**只搬位置、不改行为**：函数体、状态码与错误响应形状逐字保留；迁移前后的
真实路由清单由 `scripts/route_inventory.py` 比对，必须完全一致。

为什么本域最大：通知不只是「读列表」——它串起了免打扰窗口、digest 节奏（每日 20:00 /
每周一 09:00 后聚合成一条外发）、Jev 分诊（压制不等于删除）、投递结果回写与去重键。
这些逻辑原先散在 `app.py` 中间，与本域的路由隔着几十个别的端点，改一处要跳很远。

边界：本域经 `notifications` / `delivery` 服务模块与 `database` 读写通知与投递状态，
不直接改其它域的表。分诊只做「标注、降级、排序」，不改写模型原话、不阻断交付。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from investment_steward_core import jev_client
from investment_steward_core import longterm as longterm_service
from investment_steward_core.api.deps import collaborators
from investment_steward_core.delivery import channel_status
from investment_steward_core.domain import (
    Holding,
    Notification,
    NotificationTriage,
    NotificationTriageReport,
    PersonalNotifyPrefs,
    Thesis,
)
from investment_steward_core.notifications import (
    jev_triage_findings,
    jev_triage_shards,
    jev_triage_summary,
    notification_dedup_key,
)


# 第四轮审计（api-5）：`GET /notifications/pending` 原先返回裸 `list[Notification]`，
# 被免打扰/关闭站内提醒挡住时也是 `[]`——前端无法区分「没有通知」与
# 「有 N 条但被我自己的设置静音了」，于是呈现成「一条提醒都没有」。
# 这里改为**包装对象**，把静音状态与被挡下的条数一并如实回传。
class NotificationPendingPage(BaseModel):
    #: 未读且未被分诊压制的通知（静音时恒为空数组）。
    items: list[Notification] = Field(default_factory=list)
    #: 是否因个人偏好（免打扰 / 关闭站内提醒）而静音。
    muted: bool = False
    #: 静音原因（可展示给用户的中文说明）。
    mute_reason: str = ""
    #: 被静音挡下的未读条数——让用户知道「有 N 条」而不是「没有」。
    hidden_count: int = 0

# ---- v21 M2-02：通知 digest 节奏（个人中心 notify.frequency）----
# 每日汇总 20:00、每周汇总周一 09:00（本机墙上时间）后，下一次评估把待外部
# 投递的提醒聚合成一条 digest 一次性外发；站内呈现不受 digest 节奏影响。
DIGEST_DAILY_AT = (20, 0)
DIGEST_WEEKLY_AT = (9, 0)


def digest_window(prefs: Any, local_now: datetime) -> tuple[bool, str]:
    """返回（是否到达外发窗口, 本周期 digest 去重键）；realtime 恒 (True, "")。

    到点语义为「不早于」：本地优先应用未必整点在线，错过 20:00/周一 09:00 后，
    当天/当周内任意一次 evaluate 都会补发（每周期至多一次，由去重键保证）。
    """
    if prefs.frequency == "daily":
        window_start = local_now.replace(
            hour=DIGEST_DAILY_AT[0], minute=DIGEST_DAILY_AT[1], second=0, microsecond=0
        )
        return local_now >= window_start, f"digest:daily:{local_now.date().isoformat()}"
    if prefs.frequency == "weekly":
        monday = local_now.date() - timedelta(days=local_now.weekday())
        window_start = datetime(
            monday.year, monday.month, monday.day,
            DIGEST_WEEKLY_AT[0], DIGEST_WEEKLY_AT[1], tzinfo=local_now.tzinfo,
        )
        return local_now >= window_start, f"digest:weekly:{monday.isoformat()}"
    return True, ""



def build_notifications_router(
    require_session: Callable[..., Any],
    audit: Callable[..., None],
) -> APIRouter:
    """构造通知域路由。

    `require_session` / `audit` 由 `api/app.py` 注入（见 `api/deps.py`）。
    被测试 patch 的协作者（`resolve_store` / `evaluate_notifications` /
    `deliver_notification` / `digest_window`）**不注入**，而是经 `collaborators()`
    按调用时从 `api.app` 命名空间取——理由见 `api/deps.collaborators` 的说明。
    """
    router = APIRouter()

    # —— 通知分级与合并（8.5） ——
    @router.get("/notifications/graded")
    def notifications_graded(core: Any = Depends(require_session)) -> dict[str, object]:
        return longterm_service.grade_and_merge_notifications(core.database.list_notifications(core.local_user_id))

    def _load_notify_prefs(core: Any) -> PersonalNotifyPrefs:
        """个人中心通知偏好；未保存过时用默认值（站内/外部全开、无免打扰）。"""
        settings = core.database.get_personal_settings(core.local_user_id)
        return settings.notify if settings is not None else PersonalNotifyPrefs()

    def _quiet_hours_active(prefs: PersonalNotifyPrefs) -> bool:
        """免打扰按本机时区 HH:MM 判断（用户在界面上填的是墙上时间）；支持跨零点窗口。"""
        if not prefs.quiet_hours_enabled:
            return False
        local_now = datetime.now().astimezone()
        minutes = local_now.hour * 60 + local_now.minute

        def _to_minutes(value: str) -> int:
            hour, minute = value.split(":")
            return int(hour) * 60 + int(minute)

        start = _to_minutes(prefs.quiet_start)
        end = _to_minutes(prefs.quiet_end)
        if start == end:
            return True
        if start < end:
            return start <= minutes < end
        return minutes >= start or minutes < end

    def _is_triage_suppressed(notification: Notification) -> bool:
        """JV08：这条通知是否被语义分诊判为「不弹站内、不外发」。

        **只认 `triage.suppressed`**，不去猜 `last_delivery_error` 的文案——文案会改，
        标记不会（与 `JevUnavailable.status_code` 的取舍同一条纪律）。
        `triage is None`（没分诊）一律视为**不压制**：没评过的东西不能当作「已判低相关」。
        """
        return notification.triage is not None and notification.triage.suppressed

    def _notification_triage_context(
        core: Any,
        notification: Notification,
        theses_by_id: dict[str, Thesis],
        holdings_by_instrument: dict[str, Holding],
    ) -> dict[str, object]:
        """JV08：单条通知的「持仓上下文」白名单（字段范围见《契约》§M）。

        只取判定「这条消息是否实质影响该标的」必需的片段：标的的持仓/自选状态与标签、
        该标的的核心假设、触发条件原文。**明确不带** `Holding.strategy_note`（用户自述原文，
        与判定无关，带上只是扩大出网面）。
        """
        instrument = str(notification.instrument or "")
        holding = holdings_by_instrument.get(instrument)
        thesis = theses_by_id.get(str(notification.thesis_id or ""))
        return {
            "holding_status": holding.status.value if holding is not None else "",
            "label": holding.label if holding is not None else "",
            "conditions": [notification.triggered_by],
            "core_assumptions": list(thesis.core_assumptions) if thesis is not None else [],
        }

    def _apply_notification_triage(notifications: list[Notification], core: Any) -> None:
        """JV08：落库前给每条通知打语义分诊结论（**原地修改**，不改列表长度、不改顺序）。

        **软校验铁律**：本函数**绝不删除**任何通知。判为低相关的条目照常落库，只是带上
        `triage.suppressed=True`，由呈现层与投递层决定「不弹、不发」——留痕可查（§M）。

        **不重复出网**：同 dedup 键的通知若已带分诊结论落过库，直接复用，不再送评。
        按小时桶反复评估时这条是成本红线（R1：只按输入 token 计费）。

        `build_shard_reviewer` 返回 None（总闸关闭 / Jev 未启用）时**直接返回**，
        一个字段都不写——`triage` 保持 None，与接入前逐字节一致。
        """
        reviewer = jev_client.build_shard_reviewer(
            core, collaborators().resolve_store(core.database), purpose="jev:notify-triage"
        )
        if reviewer is None or not notifications:
            return
        existing = {
            notification_dedup_key(item): item.triage
            for item in core.database.list_notifications(core.local_user_id)
            if item.triage is not None
        }
        theses_by_id = {str(item.thesis_id): item for item in core.database.list_theses(core.local_user_id)}
        holdings_by_instrument = {
            item.instrument: item for item in core.database.list_holdings(core.local_user_id)
        }
        items: list[dict[str, object]] = []
        # 同一 dedup 键 = **同一条提醒**。同批次内若出现重复键（用户把同一条件写了两遍就会），
        # 只送评一次、结论共用——否则后一条的判定会覆盖前一条，让前者**静默拿到别人的结论**。
        # 顺带也省掉一次重复的输入 token。
        queued: dict[str, list[Notification]] = {}
        for notification in notifications:
            dedup = notification_dedup_key(notification)
            prior = existing.get(dedup)
            if prior is not None:
                # 已经分诊过 → 复用结论，不重复出网（同一 dedup 键 = 同一条提醒）。
                notification.triage = prior
                continue
            group = queued.setdefault(dedup, [])
            group.append(notification)
            if len(group) > 1:
                continue
            items.append({
                "dedup_key": dedup,
                "notification": notification,
                "context": _notification_triage_context(
                    core, notification, theses_by_id, holdings_by_instrument
                ),
            })
        if not items:
            return
        bundle = jev_triage_shards(items)
        shard_results = reviewer(bundle) if bundle.get("shards") else []
        findings = jev_triage_findings(shard_results)
        models = {str(item.get("model")) for item in shard_results if item.get("model")}
        model = "/".join(sorted(models)) if models else None
        for entry in items:
            finding = findings.get(str(entry["dedup_key"]))
            if finding is None:
                continue
            triage = NotificationTriage(**finding, model=model)  # type: ignore[arg-type]
            for notification in queued[str(entry["dedup_key"])]:
                notification.triage = triage

    def _persist_evaluated(notifications: list[Notification], core: Any) -> list[Notification]:
        # E4：规则去重与投递状态分离。失败通知在退避窗口到期后再次尝试，
        # 不会因为 dedup 记录永久吞掉提醒；成功通知不重复外呼。
        store = collaborators().resolve_store(core.database)
        now = datetime.now(UTC)
        # 个人中心通知偏好：免打扰时段内或外部通道关闭时，跳过投递且不计退避，
        # 保持 pending——窗口结束（或重新开启通道）后的下一次 evaluate 会正常重试。
        prefs = _load_notify_prefs(core)
        external_allowed = prefs.external_enabled and not _quiet_hours_active(prefs)
        hold_reason = None if external_allowed else (
            "免打扰时段内暂不投递" if _quiet_hours_active(prefs) else "外部通道已在个人中心关闭"
        )
        # v21 M2-02：daily/weekly 汇总节奏——外部投递不逐条即时发，等窗口期聚合一次。
        digest_due, digest_key = collaborators().digest_window(prefs, datetime.now().astimezone())
        digest_label = {"daily": "每日", "weekly": "每周"}.get(prefs.frequency)
        digest_hold_reason = (
            f"{digest_label}汇总节奏：外部提醒将在窗口期聚合为一条汇总发送" if digest_label else None
        )
        persisted = {
            notification_dedup_key(item): item
            for item in core.database.list_notifications(core.local_user_id)
        }
        delivered_count = 0
        digest_held: list[tuple[str, Notification]] = []
        digest_row: Notification | None = None
        result_notifications: list[Notification] = []
        for notification in notifications:
            dedup = notification_dedup_key(notification)
            previous = persisted.get(dedup)
            if previous is not None and previous.delivery_status == "delivered":
                retained = notification.model_copy(update={
                    "notification_id": previous.notification_id,
                    "read": previous.read,
                    "delivery_status": previous.delivery_status,
                    "delivery_attempts": previous.delivery_attempts,
                    "next_retry_at": previous.next_retry_at,
                    "last_delivery_error": previous.last_delivery_error,
                })
                core.database.upsert_notification(retained, dedup)
                result_notifications.append(retained)
                continue

            # JV08：语义分诊判为低相关 → **不弹站内、不外发**，但**照常落库**（留痕可查）。
            # 放在「已投递」判定之后：已经发出去过的不因为分诊结论变化而「撤回」。
            # `delivery_status` 仍记 pending——它的取值域只有 pending/delivered/failed，
            # 而「被有意压制」与「发送失败」是两件事，故用 `triage.suppressed` 作为权威标记、
            # 用 `last_delivery_error` 给人话原因（`GET /notifications/triage` 据此翻出来）。
            if notification.triage is not None and notification.triage.suppressed:
                held = notification.model_copy(update={
                    "notification_id": previous.notification_id if previous is not None else notification.notification_id,
                    "read": previous.read if previous is not None else notification.read,
                    "delivery_attempts": previous.delivery_attempts if previous is not None else 0,
                    "delivery_status": "pending",
                    "next_retry_at": None,
                    "last_delivery_error": "Jev 分诊判为低相关，未通知（可在通知中心「已分诊未通知」翻到）",
                })
                core.database.upsert_notification(held, dedup)
                persisted[dedup] = held
                result_notifications.append(held)
                continue
            if previous is not None and previous.next_retry_at is not None and previous.next_retry_at > now:
                result_notifications.append(previous)
                continue

            if not external_allowed:
                held = notification.model_copy(update={
                    "notification_id": previous.notification_id if previous is not None else notification.notification_id,
                    "read": previous.read if previous is not None else notification.read,
                    "delivery_attempts": previous.delivery_attempts if previous is not None else 0,
                    "delivery_status": "pending",
                    "next_retry_at": None,
                    "last_delivery_error": hold_reason,
                })
                core.database.upsert_notification(held, dedup)
                persisted[dedup] = held
                result_notifications.append(held)
                continue

            if digest_label is not None:
                # 汇总节奏：外部不逐条投递，登记待汇总；站内呈现与免打扰逻辑不受影响。
                held = notification.model_copy(update={
                    "notification_id": previous.notification_id if previous is not None else notification.notification_id,
                    "read": previous.read if previous is not None else notification.read,
                    "delivery_attempts": previous.delivery_attempts if previous is not None else 0,
                    "delivery_status": "pending",
                    "next_retry_at": None,
                    "last_delivery_error": digest_hold_reason,
                })
                core.database.upsert_notification(held, dedup)
                persisted[dedup] = held
                result_notifications.append(held)
                digest_held.append((dedup, held))
                continue

            attempts = (previous.delivery_attempts if previous is not None else 0) + 1
            current = notification.model_copy(update={
                "notification_id": previous.notification_id if previous is not None else notification.notification_id,
                "read": previous.read if previous is not None else notification.read,
                "delivery_attempts": attempts,
            })
            receipts = collaborators().deliver_notification(current, store)
            delivered = any(bool(item.get("delivered")) for item in receipts)
            failed_receipt = next((item for item in receipts if not item.get("delivered")), None)
            detail = "未配置投递通道，保持站内 pending" if failed_receipt and "未配置" in str(failed_receipt.get("detail", "")) else ("投递失败，请检查通道配置与网络" if failed_receipt else None)
            updated = current.model_copy(update={
                "delivery_status": "delivered" if delivered else "failed",
                "next_retry_at": None if delivered else now + timedelta(minutes=min(60, 2 ** min(attempts - 1, 6))),
                "last_delivery_error": None if delivered else detail,
            })
            core.database.upsert_notification(updated, dedup)
            persisted[dedup] = updated
            result_notifications.append(updated)
            if delivered:
                delivered_count += 1
        # v21 M2-02：窗口期到 → 聚合一条 digest 外发（read=True，不在站内重复打扰）；
        # 投递成功则被聚合的提醒外部投递视为完成。每周期至多一条：按周期 dedup 键查库，
        # 已成功投递的周期直接跳过（失败则计入重试，不重置退避语义）。
        if digest_label is not None and digest_due and digest_key and digest_held:
            existing_digest = core.database.get_notification_by_dedup(digest_key)
            if existing_digest is None or existing_digest.delivery_status != "delivered":
                titles = [held.title for _, held in digest_held]
                digest = Notification(
                    notification_id=uuid4(),
                    user_id=core.local_user_id,
                    triggered_by=f"notify.digest:{prefs.frequency}",
                    condition_kind="observation_metric",
                    title=f"{digest_label}通知汇总（{len(digest_held)} 条）",
                    summary="；".join(titles[:5]) + ("…" if len(titles) > 5 else ""),
                    action_mode="observe",
                    evidence_refs=[],
                    created_at=now,
                    read=True,
                )
                receipts = collaborators().deliver_notification(digest, store)
                digest_delivered = any(bool(item.get("delivered")) for item in receipts)
                failed_receipt = next((item for item in receipts if not item.get("delivered")), None)
                digest_row = digest.model_copy(update={
                    "delivery_status": "delivered" if digest_delivered else "failed",
                    "delivery_attempts": 1,
                    "next_retry_at": None,
                    "last_delivery_error": None if digest_delivered else (
                        "汇总投递未完成：" + str(failed_receipt.get("detail", "")) if failed_receipt else "汇总投递未完成"
                    ),
                })
                core.database.upsert_notification(digest_row, digest_key)
                audit(
                    core,
                    "notification.digest.delivered" if digest_delivered else "notification.digest.failed",
                    "notification",
                    digest_key,
                )
                if digest_delivered:
                    delivered_count += 1
                    flushed: dict[str, Notification] = {}
                    for dedup, held in digest_held:
                        done = held.model_copy(update={
                            "delivery_status": "delivered",
                            "last_delivery_error": f"已并入{digest_label}汇总投递",
                        })
                        core.database.upsert_notification(done, dedup)
                        flushed[dedup] = done
                    result_notifications = [
                        flushed.get(notification_dedup_key(item), item)
                        for item in result_notifications
                    ]
        if digest_row is not None:
            result_notifications.append(digest_row)
        if delivered_count:
            audit(core, "notification.delivered", "notification", str(delivered_count))
        return result_notifications

    @router.get("/notifications/channels", response_model=list[dict[str, object]])
    def notification_channels(core: Any = Depends(require_session)) -> list[dict[str, object]]:
        return channel_status(collaborators().resolve_store(core.database))

    @router.get("/notifications/pending", response_model=NotificationPendingPage)
    def pending_notifications(core: Any = Depends(require_session)) -> NotificationPendingPage:
        # GET 不写库、不投递；规则评估仅在内存中生成当前预览，持久化与外部投递
        # 统一由显式 POST /notifications/evaluate 或 worker 负责。
        #
        # JV08：被语义分诊判为低相关的条目**不进这个列表**（这就是「不再弹通知」），
        # 但它们并没有消失——`GET /notifications/triage` 能把它们翻出来（留痕可查）。
        #
        # 压制状态必须按 **dedup 键**从库里读，不能只看对象自身的 `triage`：
        # `evaluate_notifications` 是纯规则评估、不跑分诊，它新构造出来的对象 `triage` 恒为
        # None——只看对象就会把已压制的条目从「内存预览」这条路径又放回列表，等于没压。
        stored = core.database.list_notifications(core.local_user_id)
        suppressed_keys = {
            notification_dedup_key(item) for item in stored if _is_triage_suppressed(item)
        }
        persisted = [
            item
            for item in stored
            if not item.read and notification_dedup_key(item) not in suppressed_keys
        ]
        evaluated = [
            item
            for item in collaborators().evaluate_notifications(core.database, core.local_user_id)
            if notification_dedup_key(item) not in suppressed_keys
        ]
        by_key = {notification_dedup_key(item): item for item in persisted}
        # 已落库的投递状态（failed/delivered、重试时间）优先于本次内存预览，
        # 否则 GET 会把失败详情覆盖成默认 pending，用户无法判断是否可重试。
        merged = list({**{notification_dedup_key(item): item for item in evaluated}, **by_key}.values())
        # 个人中心通知偏好：关闭站内提醒或处于免打扰时段时，站内列表不外露。
        # 通知本身已由 evaluate 落库，免打扰窗口结束后未读通知自然恢复可见。
        #
        # 第四轮审计（api-5）：此前这里返回**裸 []**，前端把它渲染成
        # 「暂无待处理通知」+ 徽章归零。于是整晚开着免打扰的用户第二天早上看到的是
        # 「一条提醒都没有」——把自己设的偏好呈现成「市场没事」，没有任何指引告诉
        # 他去哪儿改设置。现在如实告知「被静音了，且本来有几条」。
        prefs = _load_notify_prefs(core)
        if not prefs.in_app_enabled or _quiet_hours_active(prefs):
            reason = (
                "站内提醒已关闭（个人中心 → 通知设置）"
                if not prefs.in_app_enabled
                else "处于免打扰时段"
            )
            return NotificationPendingPage(
                items=[],
                muted=True,
                mute_reason=reason,
                hidden_count=len(merged),
            )
        return NotificationPendingPage(
            items=merged,
            muted=False,
            mute_reason="",
            hidden_count=0,
        )

    @router.get("/notifications/triage", response_model=NotificationTriageReport)
    def notification_triage_report(
        limit: int = 50, core: Any = Depends(require_session)
    ) -> NotificationTriageReport:
        """JV08：「已分诊未通知」+ 分诊统计（软校验铁律的落点——压制不等于删除）。

        读库、不出网、不写库。被压制的条目在这里可以逐条翻到（含压制原因与两题原始结论），
        所以「低相关不再弹通知」不会变成「低相关被静默吞掉」。

        `enabled=False`（Jev 未启用 / 总闸关闭）时 `suppressed_items` 恒为空，
        界面据此不渲染任何分诊信息——与接入前逐字节一致。
        """
        settings = jev_client.effective_settings(core)
        enabled = bool(settings.enabled) and bool(
            getattr(core.settings, "model_access_enabled", False)
        )
        stored = core.database.list_notifications(core.local_user_id)
        stats = jev_triage_summary(stored)
        suppressed_items = [item for item in stored if _is_triage_suppressed(item)]
        models = sorted({
            str(item.triage.model) for item in stored if item.triage is not None and item.triage.model
        })
        return NotificationTriageReport(
            enabled=enabled,
            model="/".join(models) if models else None,
            total=int(stats["total"]),
            notified=int(stats["notified"]),
            suppressed=int(stats["suppressed"]),
            unannotated=int(stats["unannotated"]),
            impacts=dict(stats["impacts"]),
            priorities=dict(stats["priorities"]),
            suppressed_items=suppressed_items[: max(1, min(int(limit), 200))],
            note=(
                "分诊只做标注与投递取舍：不改写通知内容、不删除记录。"
                "「未分诊」表示这一轮没有送出去评，不等于「已通过」；"
                "被压制的条目仍在此处可查。"
                if enabled
                else "Jev 未启用或出网总闸关闭，本轮未做通知分诊（通知行为与接入前一致）"
            ),
        )

    @router.post("/notifications/{notification_id}/read", response_model=Notification)
    def mark_notification_read(
        notification_id: UUID, core: Any = Depends(require_session)
    ) -> Notification:
        notification = core.database.mark_notification_read(notification_id, core.local_user_id)
        if notification is None:
            raise HTTPException(status_code=404, detail="notification not found")
        audit(core, "notification.read", "notification", str(notification_id))
        return notification

    @router.post("/notifications/evaluate", response_model=list[Notification])
    def evaluate_pending_notifications(core: Any = Depends(require_session)) -> list[Notification]:
        """worker 主动评估入口：评估 + 去重落库当前命中的通知提醒。

        JV08：落库前先做一遍语义分诊（相关性 + 优先级）。**响应形状不变**——被压制的条目
        照常出现在返回列表里（只是带 `triage` 字段），所以 worker 与既有前端无需改动；
        「不弹」是由 `GET /notifications/pending` 的过滤与投递层的跳过实现的，
        而不是靠从这里删掉条目（那才是静默吞）。
        """
        notifications = collaborators().evaluate_notifications(core.database, core.local_user_id)
        _apply_notification_triage(notifications, core)
        notifications = _persist_evaluated(notifications, core)
        audit(core, "notification.rules.re-evaluated", "notification", str(len(notifications)))
        return notifications
    return router

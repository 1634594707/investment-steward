"""决策与研究长期能力的领域模型（路线图 §7.2 / §8.1-8.3）。

设计约束（对齐路线图验收口径）：
- 可检验判断的原始文本一经保存不可改写；验证结果只追加（judgment_verifications）。
- 观察事项检查记录只追加（watch_checks）；状态机显式枚举，不许模糊状态。
- 事件排序一律 UTC ISO 字符串；「当时信息/事后信息」以决定时间戳为界。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field

from .models import SchemaModel


class VerificationResult(StrEnum):
    VERIFIED = "verified"          # 已验证
    REFUTED = "refuted"            # 被否定
    INSUFFICIENT_DATA = "insufficient_data"  # 数据不足


class JudgmentStatus(StrEnum):
    PENDING = "pending"            # 未到期/待验证
    VERIFIED = "verified"
    REFUTED = "refuted"
    INSUFFICIENT_DATA = "insufficient_data"


class VerifiableJudgment(SchemaModel):
    """可检验判断（8.2）：原始文本冻结，验证只追加。"""

    judgment_id: UUID
    user_id: UUID
    subject: str = Field(min_length=1, max_length=200)       # 判断对象（标的/指数/事件）
    direction: str = Field(min_length=1, max_length=40)      # 方向（如 看多/看空/中性）
    statement: str = Field(min_length=1, max_length=4000)    # 原始判断文本（冻结不可改）
    trigger_conditions: list[str] = Field(default_factory=list, max_length=10)
    invalidation_conditions: list[str] = Field(default_factory=list, max_length=10)
    due_at: datetime                                                        # 验证时间
    status: JudgmentStatus = JudgmentStatus.PENDING
    source_run_id: UUID | None = None                        # 关联研究运行（可空）
    # G01（桌面端升级路线图 2026-09-18）：来源研报与证据锚点——由研报「验证点」派生而来的判断
    # 带上它来自哪份报告（回填时能回到原文），以及该验证点引用的证据/引用标记。
    # 老记录的这两个字段缺省为空，读取照常（可选字段，不做数据迁移）。
    source_report_id: str | None = None
    source_refs: list[str] = Field(default_factory=list, max_length=10)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class JudgmentVerification(SchemaModel):
    """验证结果（只追加，不改写原判断）。"""

    verification_id: UUID
    judgment_id: UUID
    result: VerificationResult
    outcome: str = Field(default="", max_length=4000)        # 实际结果描述
    metrics: dict[str, Any] = Field(default_factory=dict)    # 可复算的量化依据
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WatchStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    TRIGGERED = "triggered"
    CLOSED = "closed"


class WatchItem(SchemaModel):
    """观察事项（8.3）：观察指标/失效条件 → 可追踪事项。"""

    watch_id: UUID
    user_id: UUID
    title: str = Field(min_length=1, max_length=200)
    indicator: str = Field(min_length=1, max_length=400)     # 观察指标口径
    condition_text: str = Field(min_length=1, max_length=1000)  # 触发/失效条件
    check_cycle: str = Field(default="weekly", pattern="^(daily|weekly|monthly|manual)$")
    status: WatchStatus = WatchStatus.ACTIVE
    source_kind: str = Field(default="", max_length=40)      # 来源类型（thesis/plan/judgment…）
    source_id: UUID | None = None
    dedup_key: str = Field(default="", max_length=120)       # 防重复提醒键
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WatchCheck(SchemaModel):
    """检查记录（只追加）：每次检查保留结果与时间。"""

    check_id: UUID
    watch_id: UUID
    observed: str = Field(min_length=1, max_length=2000)     # 本次观察结果
    triggered: bool = False                                  # 是否触发条件
    note: str = Field(default="", max_length=2000)
    evidence_refs: list[UUID] = Field(default_factory=list, max_length=20)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TimelineEventKind(StrEnum):
    """决策时间线事件类型（8.1，冻结枚举，不得私自扩展渲染口径）。"""

    RESEARCH_QUESTION = "research_question"
    RESEARCH_RUN = "research_run"
    EVIDENCE = "evidence"
    THESIS_VERSION = "thesis_version"
    PLAN = "plan"
    DECISION = "decision"
    OUTCOME = "outcome"
    RETROSPECTIVE = "retrospective"


class TimelineEvent(SchemaModel):
    """时间线事件：知识口径以决定时间为界分为当时/事后。"""

    kind: TimelineEventKind
    ref_id: UUID | None = None
    title: str = Field(max_length=300)
    detail: str = Field(default="", max_length=2000)
    at: datetime | None = None                               # 事件发生时间（缺失=None，显示为缺口）
    knowledge_scope: str = Field(default="at_the_time", pattern="^(at_the_time|afterwards|unknown)$")
    link_available: bool = True                              # 关联可打开（False=缺口）


# ———————————————— 8.4 研究快照与模板 ————————————————

class ResearchSnapshot(SchemaModel):
    """研究上下文快照（8.4）：冻结当时的问题、标的、证据、逻辑版本、模型方案与参数。

    恢复 = 只读回看当时的输入范围,绝不自动替换当前数据或投资逻辑。
    """

    snapshot_id: UUID
    user_id: UUID
    title: str = Field(min_length=1, max_length=200)
    question: str = Field(default="", max_length=10000)             # 用户问题
    symbols: list[str] = Field(default_factory=list, max_length=20)
    evidence_refs: list[UUID] = Field(default_factory=list, max_length=50)
    thesis_ids: list[UUID] = Field(default_factory=list, max_length=20)
    model_profile_id: UUID | None = None
    params: dict[str, Any] = Field(default_factory=dict)            # 运行参数
    output_validation: dict[str, Any] = Field(default_factory=dict) # 输出校验结果(哈希/口径)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ResearchTemplate(SchemaModel):
    """研究模板（8.4/8.6）：明确必填上下文与证据类型,不预设结论。"""

    template_id: UUID
    user_id: UUID | None = None                                     # None = 内置模板
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    required_context: list[str] = Field(default_factory=list, max_length=20)   # 必填上下文键
    evidence_types: list[str] = Field(default_factory=list, max_length=10)     # 建议证据类型
    default_params: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class InactionReason(StrEnum):
    """「不行动」原因口径（8.6,冻结枚举）。"""

    INSUFFICIENT_EVIDENCE = "insufficient_evidence"   # 证据不足
    COUNTER_NOT_EXCLUDED = "counter_not_excluded"     # 反证未排除
    AGAINST_PRINCIPLES = "against_principles"         # 不符合原则
    WAITING_WATCH = "waiting_watch"                   # 等待观察指标
    OTHER = "other"

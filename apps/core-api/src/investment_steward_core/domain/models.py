from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SchemaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PolicyStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class ConfirmationMethod(StrEnum):
    EXPLICIT_UI = "explicit_ui"
    IMPORTED = "imported"


class InvestmentPolicyVersion(SchemaModel):
    policy_id: UUID
    user_id: UUID
    version: int = Field(ge=1)
    status: PolicyStatus = PolicyStatus.DRAFT
    investment_goal: str = Field(min_length=1, max_length=2000)
    horizon_years: float | None = Field(default=None, gt=0)
    liquidity_needs: str = Field(default="", max_length=2000)
    allowed_markets: list[str] = Field(default_factory=list)
    allowed_asset_classes: list[str] = Field(default_factory=list)
    risk_boundaries: dict[str, Any] = Field(default_factory=dict)
    preferred_methods: list[str] = Field(default_factory=list)
    excluded_methods: list[str] = Field(default_factory=list)
    observation_conditions: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    review_due_at: date | None = None
    change_reason: str = Field(default="initial", max_length=2000)
    source_learning_activity_ids: list[UUID] = Field(default_factory=list)
    supersedes_id: UUID | None = None
    confirmed_at: datetime | None = None
    confirmation_method: ConfirmationMethod | None = None
    confirmation_summary: str | None = Field(default=None, max_length=2000)
    schema_version: str = "1.0"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    revision: int = Field(default=1, ge=1)
    device_id: str | None = None
    change_event_id: UUID | None = None
    conflict_policy: str = "append_only_versioning"

    @model_validator(mode="after")
    def active_requires_confirmation(self) -> InvestmentPolicyVersion:
        if self.status == PolicyStatus.ACTIVE and (
            self.confirmed_at is None or self.confirmation_method is None
        ):
            raise ValueError("active policy must have explicit confirmation")
        return self


class EvidenceType(StrEnum):
    QUOTE = "quote"
    FINANCIAL = "financial"
    ANNOUNCEMENT = "announcement"
    MACRO = "macro"
    ANALYSIS = "analysis"
    LEARNING = "learning"


class EvidenceRelation(StrEnum):
    SUPPORTING = "supporting"
    CONTRADICTING = "contradicting"
    UNKNOWN = "unknown"


class EvidenceStatus(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    RETRACTED = "retracted"


class Evidence(SchemaModel):
    evidence_id: UUID
    tenant_id: UUID
    subject_refs: list[str] = Field(min_length=1)
    evidence_type: EvidenceType
    source_name: str = Field(min_length=1, max_length=500)
    source_uri: str | None = None
    license_status: str = "unknown"
    source_trust_note: str | None = None
    published_at: datetime | None = None
    observed_at: datetime | None = None
    collected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    valid_until: datetime | None = None
    source_dataset_version: str | None = None
    producer_plugin_id: str | None = None
    producer_release: str | None = None
    schema_version: str = "1.0"
    summary: str = Field(min_length=1, max_length=10000)
    raw_locator: str | None = None
    content_hash: str = Field(min_length=16, max_length=256)
    relation: EvidenceRelation = EvidenceRelation.UNKNOWN
    freshness: str = "unknown"
    quality_flags: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    status: EvidenceStatus = EvidenceStatus.ACTIVE


class PluginCapability(SchemaModel):
    capability_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    capability_version: str = "1.0"
    stability: str = "stable"
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    allowed_resource_types: list[str] = Field(default_factory=list)
    scope: str = "user"
    private_namespace_write: bool = False
    model_access: bool = False
    notification_proposal: bool = False
    scheduled_task: bool = False
    network_allowlist: list[str] = Field(default_factory=list)
    rate_limit_per_minute: int = Field(default=30, ge=0)
    max_concurrency: int = Field(default=1, ge=1)
    max_cpu_seconds: int = Field(default=30, ge=1)
    max_memory_mb: int = Field(default=256, ge=16)
    max_execution_seconds: int = Field(default=60, ge=1)
    max_output_bytes: int = Field(default=1_000_000, ge=1024)
    data_leaves_device: bool = False
    retention: str = "run_scoped"
    deletion_policy: str = "core_managed"
    side_effects: list[str] = Field(default_factory=list)
    deterministic: bool = True
    requires_confirmation: bool = False


class PluginInstallationState(StrEnum):
    # 市场在售、本地未安装；`GET /plugins/catalog` 的未安装条目以此为 state，前端「可安装」徽标直接消费。
    AVAILABLE = "available"
    INSTALLED = "installed"
    ENABLED = "enabled"
    DISABLED = "disabled"
    REVOKED = "revoked"


class PluginUiSlot(SchemaModel):
    slot: str = Field(min_length=1, max_length=200)
    level: str = Field(pattern=r"^L[0-3]$")
    schema_version: str = "1.0"


class PluginManifest(SchemaModel):
    publisher: str = Field(min_length=1, max_length=200)
    plugin_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]+$")
    release_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    display_name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    plugin_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    license: str = Field(min_length=1, max_length=200)
    sdk_min: str = "1.0"
    sdk_max: str = "1.x"
    capabilities: list[str] = Field(default_factory=list)
    ui_slots: list[PluginUiSlot] = Field(default_factory=list)
    # 挂载方式：in_page=产出插槽卡；own_page=独立应用（占 app.library 槽，主视图进侧栏「应用」区）。
    mount: str = Field(default="in_page", pattern=r"^(in_page|own_page)$")
    schema_versions: dict[str, str] = Field(default_factory=dict)
    entrypoint: str = Field(min_length=1, max_length=200)
    artifact_sha256: str = Field(min_length=16, max_length=128)
    signature: str = Field(min_length=1, max_length=500)
    network_allowlist: list[str] = Field(default_factory=list)
    side_effects: list[str] = Field(default_factory=list)
    requires_confirmation: bool = False
    supports_markets: list[str] = Field(default_factory=list)


class PluginInstallation(SchemaModel):
    plugin_id: str
    release_version: str
    state: PluginInstallationState
    granted_capabilities: list[str] = Field(default_factory=list)
    artifact_sha256: str
    source: str = "official-static-registry"
    installed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = "1.0"


class PluginUpdateStatus(StrEnum):
    STAGED = "staged"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"


class UpdateChannel(StrEnum):
    """三套更新通道，互不覆盖：Host / 插件 / 内容。

    - HOST：运行中的 Core 二进制/内核版本（`__version__`），只读于插件 API；
    - PLUGIN：`PluginInstallation` + 更新候选（`plugin_*` 表），仅插件生命周期端点触及；
    - CONTENT：插件入账的 Evidence / CandleSeries 等数据，按 `content_hash` /
      `source_dataset_version` 版本化，插件版本更新不得改写已入账内容。
    """

    HOST = "host"
    PLUGIN = "plugin"
    CONTENT = "content"


class PluginUpdateCandidate(SchemaModel):
    """更新事务的暂存候选。

    目标版本先落在 candidate 表里，保持当前安装版本不动的『暂存阶段』；
    健康检查通过后才原子切换为新的 PluginInstallation；失败则删除候选回滚，
    旧版本、旧配置、用户数据始终可用。
    """

    candidate_id: UUID
    plugin_id: str
    target_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    source_artifact_sha256: str
    status: PluginUpdateStatus = PluginUpdateStatus.STAGED
    manifest_payload: dict[str, object] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = "1.0"

    def stage(self) -> PluginUpdateCandidate:
        return self.model_copy(update={"status": PluginUpdateStatus.STAGED, "updated_at": datetime.now(UTC)})

    def to_committed(self) -> PluginUpdateCandidate:
        return self.model_copy(update={"status": PluginUpdateStatus.COMMITTED, "updated_at": datetime.now(UTC)})

    def to_rolled_back(self) -> PluginUpdateCandidate:
        return self.model_copy(update={"status": PluginUpdateStatus.ROLLED_BACK, "updated_at": datetime.now(UTC)})


class OHLCVBar(SchemaModel):
    timestamp: datetime
    open: float = Field(ge=0)
    high: float = Field(ge=0)
    low: float = Field(ge=0)
    close: float = Field(ge=0)
    volume: float = Field(ge=0)

    @model_validator(mode="after")
    def valid_ohlc(self) -> OHLCVBar:
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("invalid OHLC bounds")
        return self


class CandleSeries(SchemaModel):
    instrument: str
    timeframe: str = "1d"
    bars: list[OHLCVBar] = Field(min_length=1, max_length=500)
    source_plugin_id: str
    source_plugin_release: str
    source_name: str
    as_of: datetime
    is_demo: bool = False
    limitations: list[str] = Field(default_factory=list)


class ActionMode(StrEnum):
    OBSERVE = "observe"
    RESEARCH = "research"
    REVIEW_PLAN = "review_plan"
    NO_ACTION = "no_action"


class CardSeverity(StrEnum):
    """插槽卡的关注度等级（对齐 packages/ui-card-schemas CardSeverity）。"""

    INFO = "info"
    ATTENTION = "attention"
    WARNING = "warning"


class UiCardRenderer(StrEnum):
    """渲染原语（对齐 packages/ui-card-schemas UiCardRenderer）：按插槽级别仲裁分发。"""

    CARD = "card"
    SUMMARY_ROW = "summary_row"
    INLINE = "inline"
    SILENT = "silent"


class CredentialStoreBackend(StrEnum):
    OS = "os"
    FILE = "file"


class CredentialRecord(SchemaModel):
    """凭据的可展示摘要：永不携带明文，只回 last4 + updated_at + 存储后端。"""

    key_id: str = Field(min_length=1, max_length=200)
    last4: str = Field(min_length=0, max_length=4)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    backend: CredentialStoreBackend = CredentialStoreBackend.FILE


class ModelProfileStatus(StrEnum):
    """模型服务多方案的当前生效态：同一时刻恰好一个「使用中」。"""

    INACTIVE = "未启用"
    ACTIVE = "使用中"


class ModelProfile(SchemaModel):
    """模型服务多方案（CC Switch 式）。

    `credential_ref` 引用凭据库中的某条密钥 key_id；`model_access` 能力出网一律经
    `status=使用中` 的当前方案代理。
    """

    profile_id: UUID
    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    credential_ref: str = Field(min_length=1, max_length=200)
    status: ModelProfileStatus = ModelProfileStatus.INACTIVE
    # v22 方案级模型调用超时（秒）：推理型模型（如火山方舟 deepseek-v4-pro）单次生成
    # 可能超过内置默认 120s，用户可按方案放宽。None = 用内置默认。
    timeout_secs: int | None = Field(default=None, ge=30, le=1800)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_endpoint(self) -> ModelProfile:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url 必须是包含主机名的 http/https 地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url 不得包含凭据、查询参数或 fragment")
        return self


class Book(SchemaModel):
    """研读图书馆书目（G5-1）：全本机存储，data_leaves_device=false。"""

    book_id: UUID
    title: str = Field(min_length=1, max_length=500)
    author: str = Field(min_length=0, max_length=200)
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: list[str] = Field(default_factory=list)
    # M4 扩展（方案 v1 拍板）：ISBN 元数据 / 三态书架 / 书源通道；全部可选以兼容旧 payload。
    isbn: str | None = Field(default=None, max_length=32)
    status: str = Field(default="reading", pattern=r"^(reading|finished|wishlist)$")
    source: str = Field(default="user", pattern=r"^(ai|user|manual)$")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    data_leaves_device: bool = False


class LibraryPlan(SchemaModel):
    """AI 荐读计划（G5-2）：只进 today.learning 学习流，永不写投资原则。"""

    plan_id: UUID
    book_ref: str | None = None
    daily_task: str = Field(min_length=1, max_length=1000)
    source_chapter: str = Field(min_length=0, max_length=500)
    rationale: str = Field(min_length=0, max_length=2000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    slot: str = "today.learning"
    renderer: str = "summary_row"


class MacroIndicatorReading(SchemaModel):
    """宏观雷达单指标读数（M4）：pending=未接入/无数据，绝不编造数值（ADR-0006）。"""

    indicator: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=200)
    dim: str = Field(min_length=1, max_length=40)  # growth/employment/inflation/monetary/other
    status: str = Field(default="pending", pattern=r"^(ok|pending|degraded)$")
    latest: float | None = None
    obs_date: str | None = Field(default=None, max_length=20)
    unit: str = Field(default="", max_length=40)
    as_of: str | None = Field(default=None, max_length=40)
    source: str = Field(default="", max_length=200)
    dataset_version: str | None = Field(default=None, max_length=80)
    note: str = Field(default="", max_length=400)
    # —— C01（2026-09-15 路线图）：数据频率（monthly/annual/daily…）——
    # 观测期隔离的依据：年更年度值不得被月度发布事件当作「当月实际值」消费。
    frequency: str | None = Field(default=None, max_length=20)


class MacroBackgroundRow(SchemaModel):
    """背景层行（货币指数 / 油价 / 金价）：单列展示，不进国别四维打分。"""

    key: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=200)
    status: str = Field(default="pending", pattern=r"^(ok|pending|degraded)$")
    latest: float | None = None
    obs_date: str | None = Field(default=None, max_length=20)
    as_of: str | None = Field(default=None, max_length=40)
    source: str = Field(default="", max_length=200)
    dataset_version: str | None = Field(default=None, max_length=80)
    note: str = Field(default="", max_length=400)


class MacroDimScore(SchemaModel):
    """单维打分（M5）：score=None 表示该维无 ok 指标、未参与加权；rules_fired 逐条记录命中规则与实际值。"""

    dim: str = Field(min_length=1, max_length=40)  # growth/employment/inflation/monetary
    score: float | None = Field(default=None, ge=0.0, le=100.0)
    rules_fired: list[str] = Field(default_factory=list)
    indicators_used: list[str] = Field(default_factory=list)


class MacroPositioning(SchemaModel):
    """规则基线定位（M5，方案 §3）：确定性规则可复算；weight_version 固定 v1（用户自定义 M5.5）。"""

    region: str = Field(min_length=1, max_length=20)
    weight_version: str = Field(default="v1", max_length=20)
    composite: float = Field(ge=0.0, le=100.0)
    band: str = Field(pattern=r"^(扩张|放缓|承压|衰退风险)$")
    near_boundary_note: str | None = None
    dims: list[MacroDimScore] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MacroSnapshot(SchemaModel):
    """`/evidence/macro/{region}` 响应：region=国别（us/cn/eu/jp/in）或 global（背景层）。"""

    region: str = Field(min_length=1, max_length=20)
    label: str = Field(min_length=1, max_length=100)
    indicators: list[MacroIndicatorReading] = Field(default_factory=list)
    background: list[MacroBackgroundRow] = Field(default_factory=list)
    positioning: MacroPositioning | None = None
    degraded_reason: str | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MacroPricingRow(SchemaModel):
    """市场定价层单行（D-14）：FRED 公开序列代理指标，背景参考，不进四维打分。"""

    key: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=40)  # policy_rate/fx/equity/risk/inflation_expectation
    status: str = Field(default="pending", pattern=r"^(ok|pending|degraded)$")
    latest: float | None = None
    obs_date: str | None = Field(default=None, max_length=20)
    unit: str = Field(default="", max_length=40)
    trend_5d: str | None = Field(default=None, max_length=10)  # 上升/下降/持平（近 5 期 vs 前 5 期）
    ref_value: float | None = None  # 对照值（约 1 个月前）
    ref_date: str | None = Field(default=None, max_length=20)
    as_of: str | None = Field(default=None, max_length=40)
    source: str = Field(default="", max_length=200)
    dataset_version: str | None = Field(default=None, max_length=80)
    note: str = Field(default="", max_length=400)


class MacroPricingSnapshot(SchemaModel):
    """`/evidence/macro/{region}/pricing` 响应：市场定价对照层（D-14）。

    序列缓存于 macro_cache 但用独立 `pricing:` 前缀，不与指标层混存（方案 §2）；
    日度序列缓存 24h 过期重拉，拉取失败回退旧缓存（as_of 如实标注陈旧）。
    """

    region: str = Field(min_length=1, max_length=20)
    label: str = Field(min_length=1, max_length=100)
    rows: list[MacroPricingRow] = Field(default_factory=list)
    degraded_reason: str | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MacroCalendarEvent(SchemaModel):
    """事件日历单项（信息层·事件层首版）：只含官方固定节奏规则可推算的事件。

    date_type="exact"：官方固定规则（如 BLS 非农=每月第一个周五），date 即规则推算日；
    date_type="window"：官方只有窗口节奏（如 CPI 约中旬），date=窗口起、window_end=窗口止；
    note 恒含「规则推算，具体以官方日历为准」。非固定节奏事件（FOMC 等）绝不生成日期。

    v37 `phase`（发布阶段，关键区分）：窗口型事件在窗口期内**不再一律呈现为「未来事件」**——
    - upcoming：窗口尚未开始（今天 < date）；
    - in_window：窗口进行中（date ≤ 今天 ≤ window_end）——**官方可能已发布也可能未发布，
      本管道无法从规则推算得知，故 phase 只描述窗口位置，不声称是否已发布**；
    - elapsed：窗口已过（今天 > window_end）——按规则推算官方应已发布，实际值须以官方为准。

    `actual_*` / `expectation`：可选接入的**实际值与观测日**（由 `linked_series` 关联的宏观
    指标提供，如美国 CPI 同比 ← FRED CPIAUCSL）。取不到即保持 None 并在证据文本如实声明
    「实际值未接入」，绝不填默认值（ADR-0006 不编造）。
    """

    region: str = Field(min_length=1, max_length=20)
    event_key: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=40)  # employment/inflation/growth/monetary
    date_type: str = Field(pattern=r"^(exact|window)$")
    date: str = Field(min_length=10, max_length=10)  # YYYY-MM-DD（window 型=窗口起始日）
    window_end: str | None = Field(default=None, max_length=10)
    note: str = Field(default="", max_length=400)
    # —— v37：发布阶段与实际值接入 ——
    phase: str = Field(default="upcoming", pattern=r"^(upcoming|in_window|elapsed)$")
    # `linked_series` = "{region}:{indicator}"，与 macro_feed 缓存键同构；None 表示本事件
    # 暂无对应可拉指标（如 PMI），证据文本会声明实际值未接入。
    linked_series: str | None = Field(default=None, max_length=80)
    actual_value: float | None = None
    actual_unit: str | None = Field(default=None, max_length=20)
    actual_obs_date: str | None = Field(default=None, max_length=10)
    actual_source: str | None = Field(default=None, max_length=200)
    # —— C01（2026-09-15 路线图）：实际值的数据频率（monthly/annual…）——
    # annual 读数**不写入** actual_*（月度事件不得用年度值冒充当月值），
    # 只进 annual_background 背景列表，由证据文本明示「仅作年度背景」。
    actual_frequency: str | None = Field(default=None, max_length=20)
    # 年度背景读数列表（年更序列的真实值）：每项
    # {"label", "value", "unit", "obs_date", "source"}；仅作年度背景展示。
    annual_background: list[dict[str, object]] = Field(default_factory=list)
    # —— v39：同一事件的多口径实际值并列 ——
    # 同一指标在不同统计口径下数值不同（典型：美国 CPI 季调 vs 未季调）。`actual_*` 存
    # **主口径**（优先与官方发布对齐的口径），`actual_variants` 存其余口径的并列读数，
    # 让使用者能看到「差异来自口径」而不是被单一数字误导。空列表表示本事件只有一个口径。
    # 每项形状：{"label": 口径名, "value": float, "unit": str, "obs_date": str, "source": str}
    actual_variants: list[dict[str, object]] = Field(default_factory=list)
    # 主口径的名称（如「未季调」），None 表示未标注口径。
    actual_variant_label: str | None = Field(default=None, max_length=40)


class MacroCalendarSnapshot(SchemaModel):
    """`GET /evidence/macro/calendar` 响应：未来 N 天数据发布日历（按日期升序）。"""

    events: list[MacroCalendarEvent] = Field(default_factory=list)
    days: int = Field(ge=1, le=60)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SpeakerSignal(SchemaModel):
    """发言人信号（§3.5 段一/段二，D-13：官方优先、转载降级并明示）。

    excerpt 必须是官方原文粘贴（本地优先，用户手动录入来源可跳转）；
    direction/ai_rationale 由 AI 解读回填——rationale 必须引用原文句子，
    无引用不回填（ADR-0006）；信号流只提供「哪类信号更重要」的依据，
    权重调整仍走 M5.5 提议卡确认或用户手动滑杆。
    """

    signal_id: str = Field(min_length=8, max_length=64)
    region: str = Field(min_length=1, max_length=20)
    speaker: str = Field(min_length=1, max_length=100)  # 例：鲍威尔 / 拉加德 / 中国央行
    event_type: str = Field(default="", max_length=40)  # 声明/记者会/纪要/执行报告…
    event_date: str = Field(min_length=10, max_length=10)  # YYYY-MM-DD
    source_name: str = Field(pattern=r"^(官方|转载)$")
    source_url: str = Field(default="", max_length=500)
    excerpt: str = Field(min_length=10, max_length=8000)  # 官方原文粘贴
    direction: str | None = Field(default=None, pattern=r"^(鹰派|鸽派|中性)$")
    ai_rationale: str | None = Field(default=None, max_length=4000)
    focus_shift: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=100)
    interpreted_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MacroUserView(SchemaModel):
    """§3.7 第三层「用户分析」（D-15 最小集）：用户主权，仅本机存储。

    永不参与规则计算、不进证据账本客观区、不上传任何远端；与规则基线、AI 分析
    三层并排展示，分歧即信息（不仲裁谁对）。
    """

    region: str = Field(min_length=1, max_length=20)
    direction: str = Field(pattern=r"^(扩张|放缓|承压|衰退风险)$")
    horizon: str = Field(min_length=1, max_length=40)  # 例：3 个月 / 下季度
    confidence: str = Field(pattern=r"^(高|中|低)$")
    text: str = Field(default="", max_length=4000)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class UICard(SchemaModel):
    """插槽卡（对齐 packages/ui-card-schemas/src/index.ts UICard 1:1）。

    内核按插槽仲裁后的呈现单元；`slot=None` 表示不占主视图、仅入证据账本（L0 静默）。
    """

    card_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=4000)
    severity: CardSeverity = CardSeverity.INFO
    evidence_refs: list[UUID] = Field(default_factory=list)
    source_plugin: str | None = None
    slot: str | None = None
    renderer: UiCardRenderer | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    valid_until: datetime | None = None
    action_mode: ActionMode = ActionMode.NO_ACTION
    supported_actions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def slot_silence_requires_no_cards(self) -> UICard:
        # L0 静默槽（notification.global）的提案永不进主视图：由调用方保证不生成 UICard。
        return self


class JevRouteDecision(SchemaModel):
    """JV07：行动模式预判路由的结论（**只描述「跑了哪条链路」，不描述结论本身**）。

    **硬约束**：它**永不改写**同一条 `AgentResponse.action_mode`。预判说的是「这次提问该不该
    花一次完整研究调用」，`action_mode` 是作答方（模型经 `response_gate` / 本地确定性引擎）
    按证据账本给出的结论——两者不一致是**正常且可能**的，如实并陈，不互相覆盖。
    """

    #: 预判所选模式（`research`/`observe`/`review_plan`/`no_action`）；没拿到时为 None。
    mode: str | None = None
    #: 模式的中文短标签。
    mode_label: str = "未取得预判"
    #: `choice` 应答自带的置信度；没有时为 None（**不伪造**）。
    confidence: float | None = None
    #: 四选项的概率分布（回执用，便于事后复算）。
    probabilities: dict[str, float] = Field(default_factory=dict)
    #: 是否据此**跳过了**完整研究链路（只有判为 `no_action` 且置信度达标才为真）。
    bypassed_model: bool = False
    #: `routed`（按预判走了）/ `full`（拿到预判但维持完整链路）/ `unannotated`（没拿到）。
    route_state: str = "unannotated"
    #: 题目 id 与选项清单（审计回执：拿到这两项即可复现当次题面）。
    question_id: str = ""
    options: list[str] = Field(default_factory=list)
    #: 判定依据的人话说明（为什么走/没走轻量路径）。
    note: str = ""
    model: str = ""
    schema_version: str = "1.0"


class AgentResponse(SchemaModel):
    response_id: UUID
    run_id: UUID
    user_id: UUID
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    user_question: str = Field(min_length=1, max_length=10000)
    context_snapshot_refs: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1, max_length=10000)
    reasoning_outline: list[str] = Field(default_factory=list)
    confidence_description: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[UUID] = Field(default_factory=list)
    supporting_refs: list[UUID] = Field(default_factory=list)
    contradicting_refs: list[UUID] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    freshness_warning: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    action_mode: ActionMode
    # 由 Core/模型回答声明的可执行动作；前端不得自行扩展全量动作白名单。
    supported_actions: list[str] = Field(default_factory=list)
    learning_link: str | None = None
    suggested_activity: dict[str, Any] | None = None
    user_confirmation_required: bool = False
    proposed_changes: dict[str, Any] | None = None
    model_provider: str | None = None
    model_name: str | None = None
    prompt_policy_version: str = "1.0"
    plugin_runs: list[str] = Field(default_factory=list)
    tool_calls: list[str] = Field(default_factory=list)
    audit_refs: list[str] = Field(default_factory=list)
    #: JV07 行动模式预判路由的结论；**None = 本轮没跑预判**（总闸关闭 / 未启用 / 模型出网关闭）。
    #: 它只说明「跑了哪条链路」，**不参与也不改写** `action_mode`。
    jev_route: JevRouteDecision | None = None


class InvestorProfile(SchemaModel):
    user_id: UUID
    investment_goal: str = ""
    horizon_years: float | None = Field(default=None, gt=0)
    liquidity_needs: str = ""
    knowledge_self_assessment: str = ""
    markets_and_assets: list[str] = Field(default_factory=list)
    consent: dict[str, bool] = Field(default_factory=dict)
    privacy_settings: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = "1.0"


class PersonalNotifyPrefs(SchemaModel):
    """个人中心 · 通知偏好（仅本机生效，决定提醒的呈现与节流，不改变通道是否可用）。

    通道仍以 GET /notifications/channels 的 configured 为准：这里关掉的通道即使已配置也不投递。
    """

    in_app_enabled: bool = True
    external_enabled: bool = True
    quiet_hours_enabled: bool = False
    quiet_start: str = Field(default="22:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    quiet_end: str = Field(default="08:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    frequency: str = Field(default="realtime", pattern=r"^(realtime|daily|weekly)$")


# 默认落地页白名单：与 web-shell `shell/nav.ts` 的 AppView 同源，改动需两端同步。
PERSONAL_DEFAULT_VIEWS: tuple[str, ...] = (
    "today",
    "investment",
    "workbench",
    "research",
    "review",
    "quant",
    "library",
    "macro",
    "settings",
)
_DEFAULT_VIEW_PATTERN = "^(" + "|".join(PERSONAL_DEFAULT_VIEWS) + ")$"


class PersonalSettings(SchemaModel):
    """个人中心设置（单用户 · 本机持久化）：显示身份 + 落地页 + 通知偏好 + 风险偏好。

    与 InvestorProfile 分工：画像回答「你是怎样的投资者」（供研究引擎消费）；
    个人中心回答「界面怎么为你服务」（显示名/头像/默认页/提醒节奏），不参与任何计算。
    """

    user_id: UUID
    display_name: str = Field(default="投资人", max_length=20)
    avatar_data: str | None = Field(default=None, max_length=300_000)
    avatar_color: str = Field(default="#378add", pattern=r"^#[0-9a-fA-F]{6}$")
    default_view: str = Field(default="today", pattern=_DEFAULT_VIEW_PATTERN)
    notify: PersonalNotifyPrefs = Field(default_factory=PersonalNotifyPrefs)
    risk_profile: str = Field(default="", max_length=32)
    style_tags: list[str] = Field(default_factory=list, max_length=12)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = "1.0"

    @model_validator(mode="after")
    def _check_avatar(self) -> PersonalSettings:
        data = self.avatar_data
        if data is not None and data != "" and not data.startswith("data:image/"):
            raise ValueError("头像必须是 data:image/… 形式的图片")
        if data == "":
            self.avatar_data = None
        return self


class JevSettings(SchemaModel):
    """Jev 决策模型（System One）连接参数（单用户 · 本机持久化）。

    **刻意不复用 `ModelProfile`**：那是 chat 方案表，「同一时刻恰好一个使用中」的语义
    不许被第二种协议污染。Jev 走 `POST {base_url}/systemone`，是并列的另一套协议，
    因此配置也独立一张表（`jev_config`）+ 独立模型。

    生效优先级：本记录（设置页保存值）> `STEWARD_JEV_*` 环境变量 > 内置默认。
    密钥**不在本模型里**——只存 `credential_ref`（凭据库引用），明文密钥永远只在本机凭据库。
    """

    user_id: UUID
    #: 场景级开关。**默认关闭**：state 会出网到第三方（美国托管、默认非零留存），
    #: 必须由用户显式开启后才参与任何判定（对齐 R2 的隐私冲突面）。
    enabled: bool = False
    base_url: str = Field(default="https://api.typesafe.ai/v1", max_length=300)
    model: str = Field(default="jev-latest", max_length=120)
    #: 凭据库引用：key_id / 凭据尾号 / 空串（未配置）
    credential_ref: str = Field(default="", max_length=120)
    timeout_secs: float = Field(default=60.0, ge=5, le=600)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = "1.0"

    @model_validator(mode="after")
    def _check_base_url(self) -> JevSettings:
        url = (self.base_url or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("Jev base_url 必须以 http:// 或 https:// 开头")
        if not (self.model or "").strip():
            raise ValueError("Jev 模型名不能为空（官方默认别名 jev-latest）")
        self.base_url = url
        self.model = self.model.strip()
        self.credential_ref = (self.credential_ref or "").strip()
        return self


class RunStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    COLLECTING_EVIDENCE = "collecting_evidence"
    ANALYZING = "analyzing"
    WAITING_CONFIRMATION = "waiting_confirmation"
    COMPOSING = "composing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"


class ResearchRun(SchemaModel):
    """研究链路运行记录：问题 → 证据收集 → 有据回答，状态机与 agent-worker 的 AgentRun 对齐。"""

    run_id: UUID
    user_id: UUID
    status: RunStatus = RunStatus.CREATED
    user_question: str = Field(min_length=1, max_length=10000)
    evidence_refs: list[UUID] = Field(default_factory=list)
    supporting_refs: list[UUID] = Field(default_factory=list)
    contradicting_refs: list[UUID] = Field(default_factory=list)
    response_id: UUID | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    error_code: str | None = None


ALLOWED_RUN_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.CREATED: {RunStatus.PLANNING, RunStatus.CANCELLED},
    RunStatus.PLANNING: {RunStatus.COLLECTING_EVIDENCE, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.COLLECTING_EVIDENCE: {RunStatus.ANALYZING, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.ANALYZING: {
        RunStatus.WAITING_CONFIRMATION,
        RunStatus.COMPOSING,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.WAITING_CONFIRMATION: {RunStatus.COMPOSING, RunStatus.CANCELLED, RunStatus.STALE},
    RunStatus.COMPOSING: {RunStatus.COMPLETED, RunStatus.FAILED},
    RunStatus.COMPLETED: set(),
    RunStatus.FAILED: set(),
    RunStatus.CANCELLED: set(),
    RunStatus.STALE: set(),
}


class AuditEvent(SchemaModel):
    event_id: UUID
    tenant_id: UUID
    actor_id: UUID | None = None
    action: str = Field(min_length=1, max_length=200)
    resource_type: str = Field(min_length=1, max_length=200)
    resource_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = "1.0"


class HoldingStatus(StrEnum):
    HOLDING = "holding"
    WATCHLIST = "watchlist"


class Holding(SchemaModel):
    holding_id: UUID
    user_id: UUID
    instrument: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=200)
    status: HoldingStatus = HoldingStatus.WATCHLIST
    strategy_note: str = Field(default="", max_length=4000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = "1.0"


class ThesisStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class Thesis(SchemaModel):
    thesis_id: UUID
    user_id: UUID
    instrument: str = Field(min_length=1, max_length=200)
    original_statement: str = Field(min_length=1, max_length=4000)
    core_assumptions: list[str] = Field(default_factory=list)
    supporting_conditions: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    observation_metrics: list[str] = Field(default_factory=list)
    status: ThesisStatus = ThesisStatus.ACTIVE
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = "1.0"


class PlanStatus(StrEnum):
    PLANNED = "planned"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


ALLOWED_PLAN_TRANSITIONS: dict[PlanStatus, set[PlanStatus]] = {
    PlanStatus.PLANNED: {PlanStatus.ACTIVE, PlanStatus.CANCELLED},
    PlanStatus.ACTIVE: {PlanStatus.COMPLETED, PlanStatus.CANCELLED},
    PlanStatus.COMPLETED: set(),
    PlanStatus.CANCELLED: set(),
}


class Plan(SchemaModel):
    plan_id: UUID
    user_id: UUID
    title: str = Field(min_length=1, max_length=500)
    status: PlanStatus = PlanStatus.PLANNED
    decision_id: UUID | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = "1.0"


class DecisionEntry(SchemaModel):
    decision_id: UUID
    user_id: UUID
    made_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    theme: str = Field(min_length=1, max_length=500)
    decision_summary: str = Field(min_length=1, max_length=4000)
    rationale: str = Field(default="", max_length=4000)
    outcome: str = Field(default="", max_length=4000)
    retrospective: str = Field(default="", max_length=4000)
    linked_evidence_ids: list[UUID] = Field(default_factory=list)
    plan_id: UUID | None = None
    # 8.6「不行动」原因:仅当本次决定是不行动时填写;旧 payload 缺失按 None 兼容读取。
    inaction_reason: str | None = Field(
        default=None,
        pattern=r"^(insufficient_evidence|counter_not_excluded|against_principles|waiting_watch|other)$",
    )
    schema_version: str = "1.0"


class BriefItem(SchemaModel):
    """日报单条：只收录与持仓/自选/投资逻辑直接相关的变化，总是附证据定位与数据时间。"""

    display_order: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=4000)
    related_instrument: str | None = None
    signal: str = Field(min_length=1, max_length=100)  # observe / research / review_plan 之类动作模式标签
    evidence_refs: list[UUID] = Field(default_factory=list)
    data_time: datetime | None = None


class TodayBrief(SchemaModel):
    """Today Brief 日报：管家只讲与用户持仓/逻辑相关的变化，无持仓时不制造虚假个性化。"""

    brief_id: UUID
    user_id: UUID
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    has_personalization: bool = False
    headline: str = Field(default="", max_length=500)
    items: list[BriefItem] = Field(default_factory=list)
    empty_reason: str | None = Field(default=None, max_length=2000)
    policy_version: str = "1.0"
    schema_version: str = "1.0"


class WeeklySection(SchemaModel):
    key: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    lines: list[str] = Field(default_factory=list)
    change_to_process: bool = False


class WeeklyReview(SchemaModel):
    """Weekly Review 周报：逻辑变化、未完成研究、决定与结果对照、下周建议；原则修改进入确认流程。"""

    review_id: UUID
    user_id: UUID
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    period_label: str = Field(min_length=1, max_length=200)
    thesis_changes: list[str] = Field(default_factory=list)
    open_research: list[str] = Field(default_factory=list)
    decisions_summary: list[str] = Field(default_factory=list)
    next_week_suggestions: list[str] = Field(default_factory=list)
    principle_change_reason: str | None = None
    sections: list[WeeklySection] = Field(default_factory=list)
    schema_version: str = "1.0"


class NotificationTriage(SchemaModel):
    """JV08：通知分诊结论（Jev 两题：相关性 choice + 优先级 score）。

    **为什么要独立建模而不是塞进 `last_delivery_error`**：压制（不弹站内、不外发）与
    投递失败是两件完全不同的事——前者是「判定为低相关，主动不打扰」，后者是「想发但没发出去」。
    混在一个字段里，用户（和运维）就没法区分「这条被有意压下了」与「这条发送失败了」。

    `suppressed` 只影响**呈现与投递**，不影响落库：被压制的通知照常持久化，
    可在通知中心「已分诊未通知」翻到（软校验铁律：不静默吞）。
    """

    #: 相关性判定：material | uncertain | immaterial
    impact: str | None = None
    impact_label: str = ""
    #: 优先级等级号（0–2）
    priority: int | None = Field(default=None, ge=0, le=2)
    priority_label: str = ""
    #: 归一化展示分 = priority / (等级数-1) × 100（官方归一化口径，换算在代码里）
    priority_percent: int | None = None
    #: 两题 confidence 的**较小值**（取小是保守方向：越低越不该压制）
    confidence: float | None = None
    #: 是否压制（不弹站内、不外发）。**只由 `notifications._triage_decision` 决定**。
    suppressed: bool = False
    #: suppressed | notified | unannotated
    triage_state: str = "unannotated"
    note: str = Field(default="", max_length=500)
    model: str | None = None
    schema_version: str = "1.0"


class Notification(SchemaModel):
    """通知：来自已确认观察/失效条件与新证据的交集，无匹配则不产出（不推送泛化噪音）。

    每条通知必须绑定触发来源条件、关联证据、建议行动模式与数据时间。
    """

    notification_id: UUID
    user_id: UUID
    thesis_id: UUID | None = None
    instrument: str | None = None
    triggered_by: str  # 命中的条件原文
    condition_kind: str  # supporting_condition | invalidation_condition | observation_metric
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=4000)
    action_mode: ActionMode
    evidence_refs: list[UUID] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    data_time: datetime | None = None
    read: bool = False
    # 投递是独立于规则评估的可重试状态；旧 payload 缺失时按 pending/0 兼容读取。
    delivery_status: str = Field(default="pending", pattern=r"^(pending|delivered|failed)$")
    delivery_attempts: int = Field(default=0, ge=0)
    next_retry_at: datetime | None = None
    last_delivery_error: str | None = Field(default=None, max_length=500)
    #: JV08：语义分诊结论。**None = 这轮没分诊**（Jev 未启用 / 总闸关闭 / 超上限），
    #: 与「分诊过但判为放行」是两件事——前者不代表任何语义判断。旧 payload 缺失时按 None 读取。
    triage: NotificationTriage | None = None
    schema_version: str = "1.0"


class NotificationTriageReport(SchemaModel):
    """JV08：通知分诊报告（`GET /notifications/triage` 的响应）。

    存在的意义是「已分诊未通知」必须**可翻到**——软校验铁律要求压制不等于删除。
    `enabled=False` 表示整层没跑（Jev 未启用 / 总闸关闭），此时 `suppressed_items` 恒为空
    且 `note` 说明原因；界面据此不渲染任何分诊信息（与接入前逐字节一致）。
    """

    enabled: bool = False
    #: 分诊所用模型（响应回的真实版本号；多分片不同版本用 `/` 连接）。
    model: str | None = None
    total: int = 0
    #: 分诊判为「值得通知」（正常出现在通知中心）。
    notified: int = 0
    #: 分诊判为「低相关不必通知」——**仍然落库**，只是不弹站内、不外发。
    suppressed: int = 0
    #: 没送出去评（分片失败 / 超单轮上限 / 单条超预算）。**不等于「通过」**。
    unannotated: int = 0
    impacts: dict[str, int] = Field(default_factory=dict)
    priorities: dict[str, int] = Field(default_factory=dict)
    #: 被压制的条目本体（供「已分诊未通知」列表展示；含 `triage` 结论与压制原因）。
    suppressed_items: list[Notification] = Field(default_factory=list)
    note: str = ""


class FreshnessPatrolResult(SchemaModel):
    """证据新鲜度巡检结果：只审计标记已过期（valid_until 已到）证据，不改动账本内容。"""

    checked: int = Field(ge=0)
    stale: int = Field(ge=0)
    stale_ids: list[UUID] = Field(default_factory=list)


class LearningUnitType(StrEnum):
    LESSON = "lesson"
    EXERCISE = "exercise"
    REFLECTION = "reflection"


class LearningUnit(SchemaModel):
    """学习单元卡：由学习插件经 today.learning 插槽产出，绑定用户自身持仓/自选。

    只引导用户练习「用证据核对自身投资逻辑」，不直接给买卖建议。
    """

    unit_id: UUID
    user_id: UUID
    unit_type: LearningUnitType = LearningUnitType.EXERCISE
    title: str = Field(min_length=1, max_length=500)
    objective: str = Field(min_length=1, max_length=2000)
    content: str = Field(min_length=1, max_length=4000)
    guidance: str = Field(default="", max_length=4000)
    bound_instrument: str | None = None
    bound_instrument_label: str | None = None
    related_conditions: list[str] = Field(default_factory=list)  # 来自该标的当前 thesis 的条件原文
    source_plugin_id: str = "official.investing-learning"
    source_plugin_release: str = "0.1.0"
    data_time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = "1.0"


class LearningGoal(SchemaModel):
    """本周学习目标：以「完成 N 个与自身持仓相关的练习」为进度口径。"""

    goal_id: UUID
    user_id: UUID
    period_label: str = Field(min_length=1, max_length=200)  # 如 2026-W36
    target_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = "1.0"


class LearningActivity(SchemaModel):
    """用户的一次学习活动：绑定自身持仓/自选，承载自主回答与反思，不产生产生建议。"""

    activity_id: UUID
    user_id: UUID
    unit_id: UUID
    unit_type: LearningUnitType = LearningUnitType.EXERCISE
    bound_instrument: str | None = None
    bound_instrument_label: str | None = None
    objective: str = Field(min_length=1, max_length=2000)
    user_answer: str = Field(default="", max_length=10000)
    reflection: str = Field(default="", max_length=10000)
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = "1.0"

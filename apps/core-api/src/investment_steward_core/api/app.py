from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status, Body
from fastapi.middleware.cors import CORSMiddleware
from httpx import HTTPError as RelayHTTPError
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

from investment_steward_core import (
    __version__,
    comtrade_feed,
    followup_research,
    longterm as longterm_service,
    macro_ai,
    macro_feed,
    macro_pricing,
    macro_scoring,
    macro_trade,
    model_client,
    sync_client,
    trading_calendar,
    quant_experiments,
)
from investment_steward_core import jev_client
from investment_steward_core import macro_calendar as macro_calendar_module
from investment_steward_core import collab as collab_engine
from investment_steward_core import feed_health
from investment_steward_core import feed_probe
from investment_steward_core import support_resistance
from investment_steward_core import analysis_followup
from investment_steward_core import direction_research as direction_engine
from investment_steward_core import quant_factors
from investment_steward_core import quant_pool, quant_portfolio
from investment_steward_core.cards import cards_for_slot
from investment_steward_core import cffex_feed
from investment_steward_core.channels import (
    assert_no_cross_channel_overwrite,
    channel_registry,
    snapshot_channels,
)
from investment_steward_core.config import CoreSettings
from investment_steward_core.credential_store import (
    JEV_API_KEY,
    resolve_store,
)
from investment_steward_core.delivery import channel_status, deliver_notification
from investment_steward_core.domain import (
    ALLOWED_PLAN_TRANSITIONS,
    AgentResponse,
    AuditEvent,
    Book,
    CandleSeries,
    CredentialRecord,
    DecisionEntry,
    Evidence,
    ResearchSnapshot as ResearchSnapshotModel,
    ResearchTemplate as ResearchTemplateModel,
    EvidenceRelation,
    EvidenceStatus,
    EvidenceType,
    FreshnessPatrolResult,
    Holding,
    HoldingStatus,
    InvestmentPolicyVersion,
    InvestorProfile,
    LearningActivity,
    LearningGoal,
    LearningUnit,
    LearningUnitType,
    LibraryPlan,
    MacroCalendarSnapshot,
    MacroPricingSnapshot,
    MacroSnapshot,
    MacroUserView,
    ModelProfile,
    Notification,
    NotificationTriage,
    NotificationTriageReport,
    OHLCVBar,
    JevSettings,
    PERSONAL_DEFAULT_VIEWS,
    PersonalNotifyPrefs,
    PersonalSettings,
    Plan,
    PlanStatus,
    PluginCapability,
    PluginInstallation,
    PluginInstallationState,
    PluginManifest,
    PluginUiSlot,
    PluginUpdateCandidate,
    PolicyStatus,
    ResearchRun,
    RunStatus,
    SpeakerSignal,
    Thesis,
    ThesisStatus,
    TodayBrief,
    UICard,
    UpdateChannel,
    WeeklyReview,
)
from investment_steward_core.domain.models import ConfirmationMethod
from investment_steward_core.evidence_policy import as_utc
from investment_steward_core.derived_metrics import (
    DerivedMetricsError,
    derive_fundamental_metrics,
    describe_derived,
)
from investment_steward_core.financial_evidence import (
    ITEM_LABEL_CN,
    STATEMENT_LABEL_CN,
    FinancialsError,
    derive_quarterly_series,
    fetch_financials,
    raw_locator_for_financials,
)
from investment_steward_core.instrument_lookup import lookup_instruments
from investment_steward_core.instruments import normalize_instrument
from investment_steward_core.learning import (
    build_current_learning_goal,
    build_today_learning_unit,
)
from investment_steward_core.library import (
    create_research_from_annotation,
    generate_reading_plan,
)
from investment_steward_core.market_feed import (
    FeedError,
    fetch_cn_kline,
    fetch_cn_market_board,
    fetch_cn_market_full_a,
    fetch_cn_sector_list,
    fetch_cn_sector_members,
    score_market_rows,
)
from investment_steward_core.news_evidence import (
    NewsError,
    NoticeError,
    content_hash_for,
    content_hash_for_news,
    fetch_cn_announcements,
    fetch_cn_news,
    parse_notice_timestamp,
    raw_locator_for,
)
from investment_steward_core.notifications import (
    JEV_TRIAGE_STATE_SUPPRESSED,
    evaluate_notifications,
    jev_triage_findings,
    jev_triage_shards,
    jev_triage_summary,
    notification_dedup_key,
)
from investment_steward_core.plugin_runtime import (
    aggregate_network_allowlist,
    secret_env_markers,
)
from investment_steward_core.probe import probe_credential
from investment_steward_core.prompting import (
    STOCK_REPORT_PROMPT_VERSION,
    STOCK_REPORT_RULES_TEXT,
    STOCK_REPORT_SCHEMA_TEXT,
)
from investment_steward_core import youzi_replay as youzi_replay_module
from investment_steward_core import youzi_horizon as youzi_horizon_module

#: Y3-06：期限取值的有界缓存（进程级共享）。键含 `as_of`，因此不同复盘时点的
#: 结论不会互相污染；`passed` 的值走 60s 正缓存、`not_due`/`missing` 只走 5s
#: 短冷却——一次数据源抖动不能把某个期限长期钉成缺失。上限 256 条，超限淘最旧。
_YOUZI_HORIZON_CACHE = youzi_horizon_module.HorizonCache()
from investment_steward_core.quant import ArtifactLineageView, ArtifactPoolView, empty_pool_view
from investment_steward_core import counter_check, quant_models, quant_pack_runner, quant_strategy_packs, quant_track_records, report_quality, tactics as stock_tactics, tactics_ai, tactics_score
from investment_steward_core.reporting import (
    build_freshness_patrol,
    build_today_brief,
    build_weekly_review,
)
from investment_steward_core import research as research_module
from investment_steward_core.research import read_latest_response, run_research
from investment_steward_core.signing import verify_manifest_integrity
from investment_steward_core.slots import (
    DEFINED_SLOTS,
    MOUNT_OWN_PAGE,
    compute_slot_occupancy,
    resolve_outputs,
    targeted_slots_by_installations,
)
from investment_steward_core.storage import Database
from investment_steward_core import valuation_evidence
from investment_steward_core.industry_evidence import direction_industry_evidence
from investment_steward_core.valuation_evidence import (
    ValuationError,
    describe_valuation,
    fetch_valuation,
)


class PolicyDraftRequest(BaseModel):
    investment_goal: str = Field(min_length=1, max_length=2000)
    horizon_years: float | None = Field(default=None, gt=0)
    liquidity_needs: str = ""
    allowed_markets: list[str] = Field(default_factory=lambda: ["CN"])
    allowed_asset_classes: list[str] = Field(default_factory=lambda: ["equity", "etf"])
    risk_boundaries: dict[str, object] = Field(default_factory=dict)
    preferred_methods: list[str] = Field(default_factory=list)
    excluded_methods: list[str] = Field(default_factory=list)
    observation_conditions: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    change_reason: str = "initial"


class PolicyConfirmationRequest(BaseModel):
    confirmation_method: ConfirmationMethod = ConfirmationMethod.EXPLICIT_UI
    confirmation_summary: str = Field(min_length=1, max_length=2000)


class AnnouncementBatch(BaseModel):
    """某标的公告证据批次的响应：available=False 表示取数失败且未生成证据（不编造）。"""

    instrument: str
    available: bool
    entries: list[Evidence]
    source_name: str
    limitations: list[str] = Field(default_factory=list)


class HoldingRequest(BaseModel):
    holding_id: UUID | None = None
    instrument: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=200)
    status: str = "watchlist"
    strategy_note: str = ""


class InvestorProfileRequest(BaseModel):
    investment_goal: str = Field(default="", max_length=4000)
    horizon_years: float | None = Field(default=None, gt=0, le=100)
    liquidity_needs: str = Field(default="", max_length=1000)
    knowledge_self_assessment: str = Field(default="", max_length=1000)
    markets_and_assets: list[str] = Field(default_factory=list)
    consent: dict[str, bool] = Field(default_factory=dict)
    privacy_settings: dict[str, Any] = Field(default_factory=dict)


class PersonalNotifyPrefsRequest(BaseModel):
    in_app_enabled: bool = True
    external_enabled: bool = True
    quiet_hours_enabled: bool = False
    quiet_start: str = Field(default="22:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    quiet_end: str = Field(default="08:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    frequency: str = Field(default="realtime", pattern=r"^(realtime|daily|weekly)$")


class PersonalSettingsRequest(BaseModel):
    display_name: str = Field(default="投资人", max_length=20)
    avatar_data: str | None = Field(default=None, max_length=300_000)
    avatar_color: str = Field(default="#378add", pattern=r"^#[0-9a-fA-F]{6}$")
    default_view: str = Field(default="today", pattern="^(" + "|".join(PERSONAL_DEFAULT_VIEWS) + ")$")
    notify: PersonalNotifyPrefsRequest = Field(default_factory=PersonalNotifyPrefsRequest)
    risk_profile: str = Field(default="", max_length=32)
    style_tags: list[str] = Field(default_factory=list, max_length=12)


class StorageMigrateRequest(BaseModel):
    """存储位置迁移请求（路线图 3.1 第 3 步）。目标目录必填，制品目录可选。"""

    target_user_data: str = Field(min_length=1, max_length=500)
    target_artifacts: str | None = Field(default=None, max_length=500)


class ExperimentRunRequest(BaseModel):
    """本地实验：对已发布的参数集/线性模型在最新 K 线上跑成本口径回测。"""

    artifact_id: str = Field(min_length=3, max_length=40)


class WalkForwardRequest(BaseModel):
    """Walk-forward 评估：expanding 切折的折数。"""

    folds: int = Field(default=3, ge=1, le=10)


class PortfolioBacktestRequest(BaseModel):
    """组合回测：各标的等长周期收益 + 权重（可选行业映射与上限）。"""

    symbol_returns: dict[str, list[float]] = Field(min_length=2)
    weights: dict[str, float] = Field(min_length=2)
    industries: dict[str, str] | None = None
    max_weight: float = Field(default=0.35, gt=0.0, le=1.0)
    max_industry_weight: float = Field(default=0.60, gt=0.0, le=1.0)


class JudgmentCreateRequest(BaseModel):
    """可检验判断（8.2）：原始文本保存后冻结。"""

    subject: str = Field(min_length=1, max_length=200)
    direction: str = Field(min_length=1, max_length=40)
    statement: str = Field(min_length=1, max_length=4000)
    due_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
    trigger_conditions: list[str] = Field(default_factory=list, max_length=10)
    invalidation_conditions: list[str] = Field(default_factory=list, max_length=10)
    source_run_id: UUID | None = None


class JudgmentVerifyRequest(BaseModel):
    """到期验证（8.2）：只追加结果，不改写原判断。"""

    result: str = Field(pattern="^(verified|refuted|insufficient_data)$")
    outcome: str = Field(default="", max_length=4000)
    metrics: dict[str, object] = Field(default_factory=dict)


class WatchItemCreateRequest(BaseModel):
    """观察事项（8.3）：观察指标/失效条件映射为可追踪事项。"""

    title: str = Field(min_length=1, max_length=200)
    indicator: str = Field(min_length=1, max_length=400)
    condition_text: str = Field(min_length=1, max_length=1000)
    check_cycle: str = Field(default="weekly", pattern="^(daily|weekly|monthly|manual)$")
    source_kind: str = Field(default="", max_length=40)
    source_id: UUID | None = None
    dedup_key: str = Field(default="", max_length=120)


class WatchCheckRequest(BaseModel):
    """检查记录（8.3）：只追加；dedup_value 相同的检查不重复计提醒。"""

    observed: str = Field(min_length=1, max_length=2000)
    triggered: bool = False
    note: str = Field(default="", max_length=2000)
    evidence_refs: list[UUID] = Field(default_factory=list, max_length=20)
    dedup_value: str | None = Field(default=None, max_length=100)


class WatchTransitionRequest(BaseModel):
    target: str = Field(pattern="^(active|paused|triggered|closed)$")


class SyncConfigRequest(BaseModel):
    """中转通道配置（阶段 4）。token/同步密钥只入本机凭据库,响应永不回显。"""

    relay_url: str = Field(min_length=8, max_length=300)   # 如 https://st.18257.xyz
    user_token: str = Field(min_length=20, max_length=200)
    device_id: str = Field(min_length=3, max_length=40)
    user_id: str = Field(min_length=3, max_length=40)
    sync_key: str | None = Field(default=None, min_length=40, max_length=200)  # 缺省自动生成


class TransferRequestCreate(BaseModel):
    """接收方申请（阶段 5 / §7.1）。类型白名单冻结。"""

    artifact_type: str = Field(pattern="^(parameter_set|model_weights|strategy_pack|dataset|backtest_result)$")
    note: str = Field(default="", max_length=500)


class TransferDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")


class TransferSendRequest(BaseModel):
    """发送方封包上传。payload 为制品明文结构,本模块负责加密与签名。"""

    artifact_type: str = Field(pattern="^(parameter_set|model_weights|strategy_pack|dataset|backtest_result)$")
    version: int = Field(default=1, ge=1, le=10000)
    payload: dict[str, Any]
    snapshot_hash: str | None = Field(default=None, max_length=120)  # 数据快照口径(如 data_snapshot 哈希)


class TransferReceiveRequest(BaseModel):
    """接收方开包。元数据缺省时自动取收件箱 transfer.announced 公告。"""

    artifact_type: str | None = None
    version: int | None = None
    sha256: str | None = None
    signature: str | None = None
    snapshot_hash: str | None = None


class MarketInstallRequest(BaseModel):
    """从 GitHub 市场目录安装插件（§6）。要求目录验签通过 + 本机注册表代码包就位。"""

    plugin_id: str = Field(min_length=1, max_length=200)
    version: str | None = Field(default=None, pattern=r"^\d+\.\d+\.\d+$")


class DataSourceRetryRequest(BaseModel):
    """「仅重试失败项」（F03）：显式列出来源，或只重试窗口内失败过的来源。

    刻意**不提供**「无差别全量重试」——那等于把上游当压力测试靶子（D03 礼貌限速同一原则）。
    模型定义在模块级：闭包内定义的 pydantic 模型会成为不可解析的 ForwardRef，
    FastAPI 会把 body 参数误判成 query（E02 已踩过一次）。
    """

    sources: list[str] = Field(default_factory=list, max_length=20)
    only_failed: bool = False
    window: str = "7d"


class ResearchSnapshotCreateRequest(BaseModel):
    """研究上下文快照（8.4）：冻结当时的输入范围;恢复=只读回看。"""
    title: str = Field(min_length=1, max_length=200)
    question: str = Field(default="", max_length=10000)
    symbols: list[str] = Field(default_factory=list, max_length=20)
    evidence_refs: list[UUID] = Field(default_factory=list, max_length=50)
    thesis_ids: list[UUID] = Field(default_factory=list, max_length=20)
    model_profile_id: UUID | None = None
    params: dict[str, object] = Field(default_factory=dict)
    output_validation: dict[str, object] = Field(default_factory=dict)


class ResearchTemplateCreateRequest(BaseModel):
    """用户自定义研究模板（8.4/8.6）。"""

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    required_context: list[str] = Field(default_factory=list, max_length=20)
    evidence_types: list[str] = Field(default_factory=list, max_length=10)
    default_params: dict[str, object] = Field(default_factory=dict)


class TacticsWatchUpsertRequest(BaseModel):
    name: str = Field(default="", max_length=60)
    note: str = Field(default="", max_length=500)


class TacticsNoteRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class TacticsScanRequest(BaseModel):
    """扫描范围（2026-09-07 用户拍板）：观察清单 + 持仓 + 手动列表，不做全市场。"""
    sources: list[str] = Field(default_factory=lambda: ["watchlist", "holdings", "manual"])
    symbols: list[str] = Field(default_factory=list, max_length=50)
    # 命中窗口 = 最近 N 根 K 线（不是最近 N 条信号）；默认与 tactics_score.DEFAULT_WINDOW_BARS 同源。
    recent_bars: int = Field(default=tactics_score.DEFAULT_WINDOW_BARS, ge=1, le=tactics_score.DETAIL_WINDOW_BARS)


class TacticsMarketScanRequest(BaseModel):
    """市场批量扫描（2026-09-07 用户追加拍板：战法雷达需要去市场批量扫描）。

    两段式：榜单快照粗筛（东财 clist，单请求）→ 候选逐票拉 K 线跑战法引擎。
    mode="all"（v21 用户追加）：新浪 hs_a 全市场分页遍历 → 快照趋势预筛评分
    → 预筛靠前的候选逐票跑引擎；top_symbols 控制跑引擎的候选数。
    mode="sectors"（2026-09-11 用户追加「按板块划分、从板块股里挑」）：选行业板块
    → 拉成分股 → 逐票跑引擎 → 板块内横截面共振加分 → 结果按板块分组返回。
    """
    mode: str = Field(default="boards", pattern="^(boards|all|sectors)$")
    boards: list[str] = Field(default_factory=lambda: ["turnover"], max_length=4)
    per_board: int = Field(default=30, ge=5, le=100)
    max_symbols: int = Field(default=60, ge=5, le=200)
    recent_bars: int = Field(default=tactics_score.DEFAULT_WINDOW_BARS, ge=1, le=tactics_score.DETAIL_WINDOW_BARS)
    # D03（桌面端升级路线图 2026-09-18）：礼貌限速与并发可配——默认值等于原实现
    # （逐票间隔 0.15s、串行），不改上游压力画像；需要更慢/更快时显式传参。
    interval_secs: float = Field(default=0.15, ge=0.0, le=2.0)
    concurrency: int = Field(default=1, ge=1, le=4)
    # mode=all：全市场快照预筛后跑引擎的候选数（引擎逐票拉 K 线，代价大）
    top_symbols: int = Field(default=40, ge=1, le=120)
    # mode=all：板块过滤（主板/创业板/科创板，多选；默认全部）。按代码前缀划分：
    # main=600/601/603/605/000/001/002/003，chinext=300/301，star=688/689。北交所本就遍历不到。
    segments: list[str] = Field(default_factory=lambda: ["main", "chinext", "star"], max_length=3)
    # mode=all：价格区间（快照价，元；None = 不限）。与板块过滤同层，都在预筛评分之前生效。
    price_min: float | None = Field(default=None, ge=0)
    price_max: float | None = Field(default=None, ge=0)
    # 战法选择：只跑这些战法 id（None/空 = 全部）。非法 id 如实忽略；用于聚焦研究特定形态。
    tactic_ids: list[str] | None = Field(default=None, max_length=30)
    # mode=all：全市场遍历页数预算（每页 80 只，40 页 ≈ 3200 只 ≈ 沪深 A 全量）
    max_pages: int = Field(default=40, ge=5, le=80)
    # mode=sectors（v26）：行业板块代码（新浪行业，如 new_blhy=玻璃行业；见 GET /tactics/sectors）
    sectors: list[str] = Field(default_factory=list, max_length=8)
    # mode=sectors：每个板块取成分股上限（按成交额降序取前 N 只跑引擎）
    per_sector: int = Field(default=20, ge=5, le=100)
    # mode=sectors：是否启用板块内横截面共振加分（同向占比高 → 板块效应；默认开）
    sector_resonance: bool = True


class TacticsAiReviewRequest(BaseModel):
    """v26 战法 AI 技术面复核（手动触发）：规则分 + 命中明细 + 指标 + 近期 K 线 → AI 质检。

    定位：规则算「证据强度」，AI 复核「形态质量与可疑点」（假突破/量价背离/位置风险/
    规则分高估或低估）。不写研报、不给买卖建议；结果落库留痕供长期研究。
    """
    symbol: str = Field(min_length=2, max_length=20)
    recent_bars: int = Field(default=tactics_score.DEFAULT_WINDOW_BARS, ge=1, le=tactics_score.DETAIL_WINDOW_BARS)
    bars_limit: int = Field(default=250, ge=60, le=500)
    # 指定模型方案（不传 = 当前「使用中」方案）；不要求该方案处于「使用中」。
    profile_id: UUID | None = None
    # 可选：把该股所属行业板块一并送模型（板块强弱会影响形态可靠度判断）。
    sector_code: str | None = Field(default=None, max_length=40)
    # 可选：用户在弹窗里的关注点（如「这个突破能不能追」）。
    question: str = Field(default="", max_length=300)


class StockResearchReportRequest(BaseModel):
    """AI 个股研究报告（v21 用户追加）：手动选股 → 新闻/公告/财报 + K 线技术面 → 模型报告。"""
    symbol: str = Field(min_length=2, max_length=20)
    question: str = Field(default="", max_length=500)
    news_limit: int = Field(default=10, ge=1, le=20)
    announcement_limit: int = Field(default=10, ge=1, le=20)
    # v22 交互升级：K 线回看区间（日），S1 快照按该窗口计算；≥60 保证战法引擎 MIN_BARS=35。
    bars_limit: int = Field(default=120, ge=60, le=500)
    # v22 多模型对比：指定用哪个模型方案生成（不传 = 当前「使用中」方案）。
    # 不要求该方案 status=使用中——对比场景正是要在不切换方案的情况下用任意已配置方案。
    profile_id: UUID | None = None
    # v29 模式字数预算（快速 quick 1500-2500 / 标准 standard 2000-3500 / 深研 deep 3500-6000）；
    # 只影响软告警阈值，不改变引用真实性等硬闸门。深研模式本期仅单股端点接入（collab 固定 standard）。
    mode: str = Field(default="standard")
    # v27（方案 §3.2）：普通研报默认追加一次**轻量反方检查**（单次短 JSON 调用，不重写正文）。
    # 成本敏感或批量场景可置 false 关闭；检查失败一律如实标注，不影响报告交付。
    with_counter_check: bool = True
    # v31 §2.4：按失败类型自动修复一轮（缺摘要只写摘要 / 缺一节只补该节 / 缺两节以上重跑全文），
    # 修复后重跑同一闸门；仍不完整保留草稿。成本敏感场景可置 false 关闭（collab 不参与，走阶段重试）。
    with_auto_repair: bool = True


class CompareSynthesisItem(BaseModel):
    """v23 跨报告综合输入：一份已完成的多模型报告（摘要 + 正文）。"""
    symbol: str = Field(min_length=1, max_length=20)
    model: str = Field(min_length=1, max_length=120)
    confidence: str = Field(default="", max_length=20)
    executive_summary: str = Field(default="", max_length=2000)
    report: str = Field(min_length=1, max_length=30000)


class CompareSynthesisRequest(BaseModel):
    """v23 阶段 0：多模型对比结果的跨报告综合（共识/分歧/待核验，分歧如实陈列不投票）。"""
    reports: list[CompareSynthesisItem] = Field(min_length=2, max_length=8)
    question: str = Field(default="", max_length=500)
    profile_id: UUID | None = None


class CollabRunCreate(BaseModel):
    """v23 协同流水线创建：单股 × 四角色严格串行（数据核对→首席初稿→风险审查→首席修订）。

    用户拍板约束：每个角色不同时进行，依次执行——本端点只建运行并定稿证据包，
    之后每个角色由 /evidence/collab-next 逐个推进。
    v23 追加：stage_profiles 允许**不同角色用不同模型**（键=阶段名，缺省用 profile_id/使用中方案）。
    """
    symbol: str = Field(min_length=2, max_length=20)
    question: str = Field(default="", max_length=500)
    news_limit: int = Field(default=10, ge=1, le=20)
    announcement_limit: int = Field(default=10, ge=1, le=20)
    bars_limit: int = Field(default=120, ge=60, le=500)
    profile_id: UUID | None = None
    stage_profiles: dict[str, UUID] = Field(default_factory=dict)
    # v31 质量恢复（2026-09-13 方案 §2.2）：模式随请求显式传递到闸门/落库/前端，未知模式 422。
    # 深研模式本期仅单股端点接入，协同默认（也允许显式）standard。
    mode: str = Field(default="standard")


class CollabNextRequest(BaseModel):
    """v23 执行协同流水线的下一个角色（严格串行：一次调一个，失败阶段重试同阶段）。"""
    run_id: str = Field(min_length=8, max_length=64)


class FollowUpSupplementCreate(BaseModel):
    """v32 追问补充材料（2026-09-13 方案 §3.2）：用户粘贴的外部信息，默认未独立验证。

    系统事件路径由前端从已有宏观事件带入（origin="system_event" + event_id/来源快照）；
    缺省一律按用户补充处理（宁按低信任，不冒充系统数据）。
    """
    text: str = Field(min_length=1, max_length=4000)
    source_name: str = Field(default="", max_length=120)
    url: str = Field(default="", max_length=500)
    event_date: str = Field(default="", max_length=10)
    origin: str = Field(default="user", max_length=20)
    event_id: str = Field(default="", max_length=120)
    source_provider: str = Field(default="", max_length=120)


class FollowUpCreate(BaseModel):
    """v32 研报追问（2026-09-13 方案 §三）：围绕已落库研报的二次分析请求。

    原报告不可变；结果保存为追加的分析附录（ai_analysis_turns）。
    v39（2026-09-19 路线图 Q10）：`mode` 明确两种入口——`interpret`（解读本报告，只依据
    原报告与用户补充材料，**不声称获取新事实**）与 `supplement_research`（补充研究，本轮
    真的去取行情/估值数据，成功与失败都如实列出）。缺省 interpret：不悄悄加钱加时延。
    """
    question: str = Field(min_length=1, max_length=500)
    supplements: list[FollowUpSupplementCreate] = Field(default_factory=list, max_length=8)
    mode: str = Field(default="interpret", max_length=32)
    profile_id: UUID | None = None


class DirectionResearchRequest(BaseModel):
    """AI 方向研判（2026-09-08 用户追加；v33 路线图 C02/B08 扩展）。

    产业趋势/价值投机的「先筛方向」入口。v33：`profile_id` 允许为**本次请求**指定模型
    （缺省沿用「使用中」方案，不静默换模型）；`mode` 为方向研判独立篇幅预算
    （quick/standard/deep，B08），与个股研报模式互不影响。
    """
    topic: str = Field(min_length=2, max_length=100)
    question: str = Field(default="", max_length=500)
    profile_id: UUID | None = None
    mode: str = Field(default="standard")
    # v33 B06：方向研判默认追加一次轻量反方审查（单次短 JSON 调用，不重写正文）。
    with_counter_check: bool = True


class ThesisRequest(BaseModel):
    thesis_id: UUID | None = None
    instrument: str = Field(min_length=1, max_length=200)
    original_statement: str = Field(min_length=1, max_length=4000)
    core_assumptions: list[str] = Field(default_factory=list)
    supporting_conditions: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    observation_metrics: list[str] = Field(default_factory=list)
    status: str = "active"


class PlanRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    status: str = "planned"
    decision_id: str | None = None


class DecisionRequest(BaseModel):
    made_at: datetime | None = None
    theme: str = Field(min_length=1, max_length=500)
    decision_summary: str = Field(min_length=1, max_length=4000)
    rationale: str = ""
    outcome: str = ""
    retrospective: str = ""
    linked_evidence_ids: list[str] = Field(default_factory=list)
    plan_id: str | None = None
    inaction_reason: str | None = Field(
        default=None,
        pattern=r"^(insufficient_evidence|counter_not_excluded|against_principles|waiting_watch|other)$",
    )


class DecisionOutcomeRequest(BaseModel):
    """E2 研究后验：到期补录决定的「结果 / 事后评价」。原始判断字段不在请求内 = 不可改写。"""

    decision_id: UUID
    outcome: str = Field(min_length=1, max_length=4000)
    retrospective: str = Field(default="", max_length=4000)


class ResearchQuestionRequest(BaseModel):
    user_question: str = Field(min_length=1, max_length=10000)


class ResearchTransitionRequest(BaseModel):
    target: str = Field(pattern=r"^[a-z_]+$")
    confirmation_summary: str | None = Field(default=None, max_length=2000)


class LearningActivityRequest(BaseModel):
    unit_id: UUID
    unit_type: str = "exercise"
    bound_instrument: str | None = None
    bound_instrument_label: str | None = None
    objective: str = Field(min_length=1, max_length=2000)
    user_answer: str = Field(default="", max_length=10000)
    reflection: str = Field(default="", max_length=10000)


class ImportApplyRequest(BaseModel):
    """E02：/import/apply 请求体——data = /export/all 的导出体；tables 缺省恢复其中全部可恢复表。"""

    data: dict[str, Any]
    tables: list[str] | None = None
    mode: str = Field(default="merge", pattern="^(merge|replace)$")
    # 显式确认：缺省/False 一律 422，不做无提示的整库覆盖。
    confirm: bool = False


class PluginUpdateRequest(BaseModel):
    target_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


class PluginCatalogEntry(BaseModel):
    manifest: PluginManifest
    installation: PluginInstallation | None = None
    # 服务端插槽注册表解析出的输出去向（slot/page/level），前端不再维护 slotPageOf 兜底（G2-3）。
    resolved_outputs: list[dict[str, str]] = Field(default_factory=list)


class CredentialUpsertRequest(BaseModel):
    secret: str = Field(min_length=1, max_length=4096)


class ModelProfileRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    credential_ref: str = Field(min_length=1, max_length=200)
    # 方案级模型调用超时（秒）：None = 内置默认；推理型模型可放宽（30–1800）。
    timeout_secs: int | None = Field(default=None, ge=30, le=1800)


class ModelProbeRequest(BaseModel):
    """草稿态模型连接探测（弹窗内「测连通性 / 拉取模型」）：只读，不落库、不写凭据。

    与创建方案的区别：`credential_ref` 允许是**尚未保存的明文密钥**（先试后存），
    也允许留空（本地 Ollama / One API 等无需密钥的端点）。任何字段都不会被持久化。
    """

    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(default="", max_length=200)
    credential_ref: str = Field(default="", max_length=500)
    timeout_secs: int | None = Field(default=None, ge=30, le=1800)


class JevSettingsRequest(BaseModel):
    """Jev 决策模型配置保存负载（JV02）。

    密钥**不在本负载里**——只接受 `credential_ref`（凭据库 key_id / 凭据尾号）。
    明文密钥走既有 `/credentials/{key_id}` 通道加密入库，避免多开一个明文入口。
    """

    enabled: bool = False
    base_url: str = Field(default="https://api.typesafe.ai/v1", min_length=1, max_length=300)
    model: str = Field(default="jev-latest", max_length=120)
    credential_ref: str = Field(default="", max_length=120)
    timeout_secs: float = Field(default=60.0, ge=5, le=600)

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        text = (value or "").strip()
        if not text.startswith(("http://", "https://")):
            raise ValueError("Jev base_url 必须以 http:// 或 https:// 开头")
        return text

    @field_validator("model")
    @classmethod
    def _check_model(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("Jev 模型名不能为空（官方默认别名 jev-latest）")
        return text


class JevProbeRequest(BaseModel):
    """Jev 草稿态探测（设置页「测连通性 / 拉取模型」）：只读，不落库、不写凭据。

    与 chat 侧 `ModelProbeRequest` 同惯例：`credential_ref` 允许是**尚未保存的明文密钥**
    （先试后存），任何字段都不会被持久化。
    """

    base_url: str = Field(default="https://api.typesafe.ai/v1", min_length=1, max_length=300)
    model: str = Field(default="jev-latest", max_length=120)
    credential_ref: str = Field(default="", max_length=500)
    timeout_secs: float | None = Field(default=None, ge=5, le=600)


class BookRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    author: str = ""
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: list[str] = Field(default_factory=list)
    isbn: str | None = Field(default=None, max_length=32)
    status: str = Field(default="reading", pattern=r"^(reading|finished|wishlist)$")
    source: str = Field(default="user", pattern=r"^(ai|user|manual)$")


class MacroWeightRequest(BaseModel):
    """M5.5 用户手动调权：四维权重（小数，合计=1.0，与 DEFAULT_WEIGHTS 同口径）。

    用户主权不受 AI 护栏约束，仅校验合计与四维齐全；偏离默认 >0.20 的维度在响应中标 personalized。
    """

    weights: dict[str, float]
    note: str = Field(default="", max_length=500)
    # 来源：user（默认，手动调权，无护栏）/ ai（AI 提议被用户确认，计入 30 天 2 次护栏计数）。
    source: str = Field(default="user", pattern=r"^(user|ai)$")


class AiProposalRequest(BaseModel):
    """M5.5 AI 提议：用户意图（可含发言人表态原句），AI 生成待确认权重提议卡。

    注意：必须定义在模块级——嵌套在 create_app 闭包内的模型 FastAPI 类型解析不到，
    会被误判为 query 参数（实测 422 "query body Field required"）。
    """

    intent: str = Field(min_length=4, max_length=2000)


class MacroUserViewRequest(BaseModel):
    """§3.7 第三层「我的分析」（D-15 最小集）：方向/时间窗/置信度 + 自由文本。同上必须模块级。"""

    direction: str = Field(pattern=r"^(扩张|放缓|承压|衰退风险)$")
    horizon: str = Field(min_length=1, max_length=40)
    confidence: str = Field(pattern=r"^(高|中|低)$")
    text: str = Field(default="", max_length=4000)

class MacroResearchEvidenceRequest(BaseModel):
    event_id: str
    claim: str
    mechanism_steps: list[str] = Field(default_factory=list)
    instrument_or_market: str | None = None
    direction: str = "uncertain"
    time_window: str
    geographies: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    origin: str = "user"
    confidence: float = Field(default=0.5, ge=0, le=1)
    status: str = "draft"


class SpeakerSignalCreate(BaseModel):
    """§3.5 段一「信号采集」：官方原文粘贴录入（本地优先，D-13 官方优先/转载明示）。必须模块级（同上）。"""

    speaker: str = Field(min_length=1, max_length=100)
    event_type: str = Field(default="", max_length=40)
    event_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    source_name: str = Field(pattern=r"^(官方|转载)$")
    source_url: str = Field(default="", max_length=500)
    excerpt: str = Field(min_length=10, max_length=8000)


class AnnotationToResearchRequest(BaseModel):
    user_question: str = Field(min_length=1, max_length=10000)


class LibraryPlanSourceRequest(BaseModel):
    source_book_id: str | None = None


class CoreState:
    def __init__(self, settings: CoreSettings):
        if not settings.session_token:
            raise ValueError("STEWARD_SESSION_TOKEN must be provided by the Host")
        self.settings = settings
        # 统一目录布局（路线图 3.1）：转发 CoreSettings.layout，模块一律经它取路径，
        # 不再从 CoreState 自行拼接。
        self.layout = settings.layout
        self.database = Database(settings.layout.database_file)
        # 旧版单文件 quant_parameter_sets.json 一次性导入(幂等;原文件改名 .imported.bak 保留)。
        quant_pool.import_legacy_pool(self.database, settings.layout)
        # A device-local identity is a migration placeholder, not a cloud account model.
        self.local_user_id = uuid5(NAMESPACE_URL, str(settings.layout.user_data.resolve()))
        default_market = next(
            (manifest for manifest in _plugin_manifests() if manifest.plugin_id == "official.cn-market-data"),
            None,
        )
        if default_market is None:
            raise ValueError("official.cn-market-data manifest is required")
        if self.database.get_plugin_installation(default_market.plugin_id) is None:
            # 引导安装也过签名/哈希校验；未通过则拒绝启用（阶段 9 信任锚）。
            _verified_registry_manifest(default_market.plugin_id, self.settings)
            self.database.upsert_plugin_installation(
                PluginInstallation(
                    plugin_id=default_market.plugin_id,
                    release_version=default_market.release_version,
                    state=PluginInstallationState.ENABLED,
                    granted_capabilities=default_market.capabilities,
                    artifact_sha256=default_market.artifact_sha256,
                )
            )


def _state(request: Request) -> CoreState:
    return request.app.state.core


def _require_session(
    request: Request,
    x_core_session_token: Annotated[str | None, Header()] = None,
) -> CoreState:
    state = _state(request)
    if x_core_session_token is None or not secrets.compare_digest(
        x_core_session_token, state.settings.session_token
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid local session"
        )
    return state


def _audit(
    state: CoreState,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    # E05（桌面端升级路线图 2026-09-18）：每自然日首次关键写入前触发一次在线备份（节流，
    # 失败静默）——补齐「长会话期间一次备份都不会产生」的缺口；检查有内存缓存，非每日首写零开销。
    try:
        state.database.maybe_daily_backup()
    except Exception:
        pass
    state.database.append_audit(
        AuditEvent(
            event_id=uuid4(),
            tenant_id=state.local_user_id,
            actor_id=state.local_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            payload=payload or {},
        )
    )


def _capabilities() -> list[PluginCapability]:
    empty_schema = {"type": "object", "additionalProperties": False}
    # 上下文通道（阶段5.2）：读取公开行情 / 读取持仓 / 读取投资逻辑为三个独立能力，
    # 授权粒度在安装预览中对用户可见，避免一个 `read_portfolio` 一并放开 Thesis。
    return [
        PluginCapability(
            capability_id="read_market_data",
            input_schema=empty_schema,
            output_schema={"$ref": "../schemas/candles.schema.json"},
            allowed_resource_types=["market_data"],
            scope="run",
            # E5：腾讯行情为主源、东财回退，两域均须授权（子域匹配经 runner `assert_host_allowed` 放行）。
            network_allowlist=["push2his.eastmoney.com", "web.ifzq.gtimg.cn"],
            rate_limit_per_minute=20,
        ),
        PluginCapability(
            capability_id="read_portfolio",
            input_schema=empty_schema,
            output_schema={"type": "object"},
            allowed_resource_types=["portfolio_holdings"],
            scope="user",
            requires_confirmation=True,
            retention="user_scoped",
        ),
        PluginCapability(
            capability_id="read_thesis",
            input_schema=empty_schema,
            output_schema={"type": "object"},
            allowed_resource_types=["thesis"],
            scope="user",
            requires_confirmation=True,
            retention="user_scoped",
        ),
        PluginCapability(
            capability_id="run_analysis",
            input_schema=empty_schema,
            output_schema={"type": "object"},
            allowed_resource_types=["analysis"],
            scope="run",
        ),
        PluginCapability(
            capability_id="submit_evidence",
            input_schema=empty_schema,
            output_schema={"$ref": "../schemas/evidence.schema.json"},
            allowed_resource_types=["public_evidence"],
            scope="run",
        ),
        PluginCapability(
            # 宏观雷达（official.macro-radar）：读取公开宏观经济数据（FRED/世界银行/统计局），
            # 出口域与 manifest network_allowlist 同源（D-11 拍板：FRED key 走 Core 凭据库，插件零密钥）。
            capability_id="read_public_evidence",
            input_schema=empty_schema,
            output_schema={"type": "object"},
            allowed_resource_types=["public_evidence"],
            scope="run",
            network_allowlist=["api.stlouisfed.org", "api.worldbank.org", "data.stats.gov.cn"],
            rate_limit_per_minute=20,
        ),
        PluginCapability(
            capability_id="submit_learning_unit",
            input_schema=empty_schema,
            output_schema={"type": "object"},
            allowed_resource_types=["learning"],
            scope="user",
            private_namespace_write=True,
        ),
        PluginCapability(
            # 研读图书馆（official.reading-library v0.2.0）：读取学习上下文（学习目标/复盘关键词/
            # 研究问题摘要）用于荐读与陪读；按 D-18 档案边界只读本机学习域，不改投资原则。
            capability_id="read_learning_context",
            input_schema=empty_schema,
            output_schema={"type": "object"},
            allowed_resource_types=["learning"],
            scope="user",
            retention="user_scoped",
        ),
    ]


def _builtin_plugin_manifests() -> list[PluginManifest]:
    return [
        PluginManifest(
            publisher="investment-steward",
            plugin_id="official.cn-market-data",
            release_version="0.1.0",
            display_name="中国市场行情",
            description="提供 A 股与 ETF 的 OHLCV/K 线、交易日历与数据时效信息。",
            plugin_type="evidence_provider",
            license="official-fixture",
            capabilities=["read_market_data", "submit_evidence"],
            ui_slots=[PluginUiSlot(slot="invest.market_view", level="L3")],
            schema_versions={"OHLCV": "1.0", "Evidence": "1.0"},
            entrypoint="market.candles",
            artifact_sha256="fixture-market-data-0.1.0",
            signature="unsigned-development-fixture",
            supports_markets=["CN"],
        ),
        PluginManifest(
            publisher="investment-steward",
            plugin_id="official.portfolio-health",
            release_version="0.1.0",
            display_name="组合健康检查",
            description="分析集中度、资产暴露和已确认投资逻辑的观察条件。",
            plugin_type="analyzer",
            license="official-fixture",
            capabilities=["read_portfolio", "read_thesis", "run_analysis"],
            ui_slots=[PluginUiSlot(slot="today.brief", level="L3")],
            schema_versions={"Analysis": "1.0"},
            entrypoint="portfolio.health",
            artifact_sha256="fixture-portfolio-health-0.1.0",
            signature="unsigned-development-fixture",
            supports_markets=["CN"],
        ),
        PluginManifest(
            publisher="investment-steward",
            plugin_id="official.investing-learning",
            release_version="0.1.0",
            display_name="投资学习教练",
            description="提供课程、练习与反思任务，不改变投资原则。",
            plugin_type="learning_provider",
            license="official-fixture",
            capabilities=["submit_learning_unit"],
            ui_slots=[PluginUiSlot(slot="today.learning", level="L2")],
            schema_versions={"LearningUnit": "1.0"},
            entrypoint="learning.unit",
            artifact_sha256="fixture-investing-learning-0.1.0",
            signature="unsigned-development-fixture",
            supports_markets=["CN"],
        ),
    ]


def _registry_entries() -> list[tuple[dict, PluginManifest]]:
    """读取注册表中的原始 manifest（payload, model）。原始 dict 用于签名校验，
    model 用于业务字段；签名覆盖的是原始 payload，不是模型重序列化，避免默认值漂移。"""
    default_registry = Path(__file__).resolve().parents[5] / "plugins"
    registry_dir = Path(os.environ.get("STEWARD_PLUGIN_REGISTRY_DIR", str(default_registry)))
    entries: list[tuple[dict, PluginManifest]] = []
    if registry_dir.exists():
        for manifest_path in sorted(registry_dir.rglob("manifest.json")):
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                entries.append((payload, PluginManifest.model_validate(payload)))
            except (OSError, ValueError):
                continue
    return entries


def _plugin_manifests() -> list[PluginManifest]:
    entries = _registry_entries()
    return [model for _, model in entries] or _builtin_plugin_manifests()


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


def _manifest_index() -> dict[str, PluginManifest]:
    return {manifest.plugin_id: manifest for manifest in _plugin_manifests()}


def _manifest_slots_index() -> dict[str, list[PluginUiSlot]]:
    return {
        manifest.plugin_id: manifest.ui_slots for manifest in _plugin_manifests()
    }


def _enabled_own_page_plugins(installations: list[PluginInstallation]) -> list[str]:
    """当前 ENABLED 且 mount=own_page 的插件列表（用于 app.library 配额）。"""
    manifests = _manifest_index()
    return [
        installation.plugin_id
        for installation in installations
        if installation.state == PluginInstallationState.ENABLED
        and manifests.get(installation.plugin_id) is not None
        and manifests[installation.plugin_id].mount == MOUNT_OWN_PAGE
    ]


def _pinned_public_key_pem(settings: CoreSettings) -> str:
    try:
        return settings.plugin_public_key_file.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(
            f"缺少插件发布者公钥文件：{settings.plugin_public_key_file}"
        ) from error


def _verified_registry_manifest(plugin_id: str, settings: CoreSettings) -> PluginManifest:
    """找到注册表中该插件的原始 manifest 并通过 Ed25519 签名 + SHA-256 校验。

    仅注册表中的有签名且未篡改的 manifest 可作为安装来源；找不到或不通过一律拒绝。
    """
    entries = [entry for entry in _registry_entries() if entry[1].plugin_id == plugin_id]
    if not entries:
        raise ValueError(f"插件 {plugin_id} 不在已签名注册表中，拒绝安装")
    for payload, model in entries:
        verify_manifest_integrity(payload, _pinned_public_key_pem(settings))
        return model
    raise ValueError(f"插件 {plugin_id} 未通过签名/完整性校验，拒绝安装")


def _verified_registry_manifest_version(
    plugin_id: str, version: str, settings: CoreSettings
) -> PluginManifest:
    """按精确版本取注册表 manifest，并验签/校验哈希；找不到该版本或校验失败一律拒绝。"""
    entries = [
        entry
        for entry in _registry_entries()
        if entry[1].plugin_id == plugin_id and entry[1].release_version == version
    ]
    if not entries:
        raise ValueError(f"插件 {plugin_id}@{version} 不在已签名注册表中")
    for payload, model in entries:
        verify_manifest_integrity(payload, _pinned_public_key_pem(settings))
        return model
    raise ValueError(f"插件 {plugin_id}@{version} 未通过签名/完整性校验")


_KNOWN_SCHEMAS = {"OHLCV", "Evidence", "Analysis", "LearningUnit", "Notification"}


def _schema_support_check(manifest: PluginManifest) -> None:
    """兼容性预检：插件声明的每类 schema 都必须是 Core 已知的产出 schema。

    未知 schema 说明候选与当前 Core 不兼容 → 属健康检查失败，不进入原子切换。
    """
    unknown = sorted(set(manifest.schema_versions) - _KNOWN_SCHEMAS)
    if unknown:
        raise ValueError(f"插件 {manifest.plugin_id} 声明了 Core 未知的 schema：{','.join(unknown)}")


def _health_check(manifest: PluginManifest, settings: CoreSettings) -> None:
    """更新事务的健康检查：签名/哈希完整性 + schema 兼容性预检，任一失败即回滚。"""
    _schema_support_check(manifest)
    # 完整性已在取 manifest 时验签，这里复用同一锚，确保切到候选前再次确认。
    _verified_registry_manifest_version(manifest.plugin_id, manifest.release_version, settings)


def _demo_candles(symbol: str, period: str = "day") -> CandleSeries:
    """确定性演示 K 线；period 决定 bar 间距与 timeframe（回退演示时不得把日线柱伪装成周/月线）。"""
    start = datetime(2026, 7, 20, 7, 30, tzinfo=UTC)
    price = 3.82 if symbol == "510300" else 10.0
    step_days = {"day": 1, "week": 7, "month": 30}[period]
    total_bars = {"day": 32, "week": 30, "month": 24}[period]
    bars = []
    for index in range(total_bars):
        day = start + timedelta(days=step_days * index)
        if period == "day" and day.weekday() >= 5:
            continue
        drift = ((index * 7) % 11 - 5) * 0.012
        opening = round(price + drift, 3)
        closing = round(opening + (((index * 13) % 9) - 4) * 0.009, 3)
        high = round(max(opening, closing) + 0.025 + (index % 3) * 0.006, 3)
        low = round(min(opening, closing) - 0.021 - (index % 2) * 0.005, 3)
        bars.append(
            {
                "timestamp": day,
                "open": opening,
                "high": high,
                "low": low,
                "close": closing,
                "volume": float(18_000_000 + index * 325_000),
            }
        )
        price = closing
    return CandleSeries(
        instrument=f"CN:ETF:{symbol}",
        timeframe={"day": "1d", "week": "1w", "month": "1mo"}[period],
        bars=bars,
        source_plugin_id="official.cn-market-data",
        source_plugin_release="0.1.0",
        source_name="中国市场行情（演示数据）",
        as_of=bars[-1]["timestamp"],
        is_demo=True,
        limitations=["当前为本地确定性样例；接入正式数据供应商后替换，不代表实时行情。"],
    )


def _latest_datetime(rows: list[object], *candidates: str) -> str | None:
    """取一组记录里最新的一条时间戳；候选字段按优先级取第一个非空值。"""
    timestamps: list[datetime] = []
    for row in rows:
        for attr in candidates:
            value = getattr(row, attr, None)
            if value is not None:
                timestamps.append(value)
                break
    if not timestamps:
        return None
    return max(timestamps).isoformat()


def _data_as_of(core: CoreState) -> dict[str, str | None]:
    """各数据域最新观测时间（喂状态栏「数据截至」；无数据时返回 None）。"""
    db = core.database
    uid = core.local_user_id
    return {
        "portfolio": _latest_datetime(db.list_holdings(uid), "updated_at", "created_at"),
        "evidence": _latest_datetime(db.list_evidence(uid), "collected_at", "updated_at", "created_at"),
        "research": _latest_datetime(db.list_research_runs(uid), "updated_at", "created_at"),
        "learning": _latest_datetime(db.list_learning_activities(uid), "created_at", "completed_at"),
        "macro": db.latest_macro_updated_at(),
        "library": _latest_datetime(db.list_books(), "updated_at", "created_at"),
    }


# ---------------------------------------------------------------------------
# 证据文本的数字展示精度（2026-09-10 用户反馈）：报告里出现 6.648999999999999 /
# -0.006371615255064356 这类裸浮点，读起来无法判断。
# 只在**证据文本构造**这一层收口展示精度；`tactics.py` 的 `snapshot()` 与评分仍用全精度原值，
# 否则战法雷达的排序与分项复算会被破坏。提示词铁律「引用与来源逐位一致」因此在可读精度上自然成立。
# ---------------------------------------------------------------------------

# 指标名 → 展示小数位（成交量类为 0 位整数）。
INDICATOR_DISPLAY_DIGITS: dict[str, int] = {
    "close": 2, "ma5": 2, "ma10": 2, "ma20": 2,
    "macd_dif": 4, "macd_dea": 4,
    "kdj_k": 2, "kdj_d": 2, "kdj_j": 2,
    "rsi14": 2,
    "volume": 0, "volume_ma20": 0,
}
DEFAULT_INDICATOR_DIGITS = 2

# 财报金额换单位阈值（元 → 亿元 / 万元）；低于万元保留 2 位小数原值（如 basic_eps 0.26）。
AMOUNT_DISPLAY_UNITS: tuple[tuple[float, str], ...] = ((100_000_000, "亿元"), (10_000, "万元"))


def _to_float(value: object) -> float | None:
    """把数值（含数值字符串）解析成 float；不可解析返回 None（绝不编造）。

    ⚠️ 真实契约里 `FinancialStatementRow.items` 是**数值字符串**（`str(Decimal)`，见
    `financial_evidence._extract_items`），所以这里必须容忍字符串输入。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _round_number(value: object, digits: int) -> object:
    """按展示精度取整；无法解析为数值时原样返回，绝不编造。"""
    number = _to_float(value)
    if number is None:
        return value
    if digits <= 0:
        return int(round(number))
    return round(number, digits)


def display_indicators(indicators: dict[str, object]) -> dict[str, object]:
    """指标快照按各字段精度收口，供证据文本序列化（不改动原始 snapshot）。"""
    return {
        key: _round_number(value, INDICATOR_DISPLAY_DIGITS.get(key, DEFAULT_INDICATOR_DIGITS))
        for key, value in indicators.items()
    }


def display_amount(value: object) -> str:
    """财报金额按量级换单位（亿/万/元）并保留 2 位小数，避免 15 位裸数进入证据文本。"""
    number = _to_float(value)
    if number is None:
        return str(value)
    for scale, unit in AMOUNT_DISPLAY_UNITS:
        if abs(number) >= scale:
            return f"{number / scale:.2f}{unit}"
    return f"{number:.2f}"


def _s1_price_range_segment(
    lookback: int,
    win: list[float],
    win_highs: list[float],
    period_change: float | None,
    drawdown: float | None,
) -> str:
    """S1 价格区间摘要（R02：收盘区间与盘中回撤各标各的口径，禁止「区间高点」一词指两物）。

    - `range_low~range_high`：`win`（**收盘价**窗口）的最低/最高——即最高收盘；
    - 回撤基准：`win_highs`（**盘中最高价**窗口）里的最高，数值一并写出使读者可复算；
    两个数各自口径清晰，样报 B 里 8.76（最高收盘）与 −32.5%（对盘中最高 9.17）不再打架。
    """
    range_low = _round_number(min(win), 2) if win else "未知"
    range_high = _round_number(max(win), 2) if win else "未知"
    intraday_high = _round_number(max(win_highs), 2) if win_highs else None
    segment = f"近 {lookback} 日收盘区间 {range_low}~{range_high}"
    if period_change is not None:
        segment += f"，区间涨跌 {period_change:+.1f}%"
    if drawdown is not None and intraday_high is not None:
        segment += f"，现价距 {lookback} 日盘中最高 {intraday_high} 回撤 {drawdown:.1f}%"
    return segment


def volume_state_label(vol5: float, vol20: float) -> tuple[str, float]:
    """量能三态标签（J05，桌面端升级路线图 2026-09-18）：阈值显式登记，可单测钉住。

    放量 >1.1；基本持平 [0.9, 1.1]；缩量 <0.9。旧二值判定（`vol5 > vol20*1.1 else 缩量`）
    把 [1.0, 1.1) 的量比误标成「缩量」（样报 002468 的 1.09），模型只能在正文里反驳自家字段。
    """
    ratio = vol5 / vol20 if vol20 else 0.0
    if ratio > 1.1:
        label = "放量"
    elif ratio >= 0.9:
        label = "基本持平"
    else:
        label = "缩量"
    return label, ratio


def _s1_volume_segment(vol5: float, vol20: float, provider: str) -> str:
    """S1 量能摘要（J05）：三态形容词与相邻数字可互相复算（比值随文写出）。

    单位：腾讯源 volume 已实测为「手」——2026-09-18 贵州茅台同一交易日 K 线量 24891 与
    实时 quote 成交量一致，成交额 313,585 万 ÷（24891×100）= 1259.83 元/股 ≈ 现价 1257.12
    （偏差 0.2%，即 VWAP 对现价的正常差）。东财 push2his 在本环境断连，其单位未经实测，
    不写单位（宁缺不假）。
    """
    state, ratio = volume_state_label(vol5, vol20)
    unit = " 手" if provider == "tencent" else ""
    return (
        f"量能：近5日均量 {vol5:.0f}{unit} vs 近20日均量 {vol20:.0f}{unit}"
        f"（量比 {ratio:.2f}，{state}）"
    )


def _s4_financial_lines(fin_rows: list[Any]) -> list[str]:
    """S4 报表行 → 模型可见证据文本（R03：行项目名与报表类型都归一为中文，杜绝下划线英文键外露）。

    规范键（revenue/basic_eps/total_debt…）仍是账本 `item_snapshot` 与 `raw_locator` 的回链主键，
    此处只改呈现层；未收录的键回退为原键（宁可露一个陌生键，也不臆造中文）。
    """
    return [
        f"- {STATEMENT_LABEL_CN.get(row.statement_type, row.statement_type)}·"
        f"{row.period_end or '未知报告期'}："
        + "；".join(
            f"{ITEM_LABEL_CN.get(k, k)}={display_amount(v)}" for k, v in row.items.items()
        )
        for row in fin_rows
    ]


def _recent_signals_with_state(signals: list[dict[str, Any]], bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """S1 证据快照的近期信号附上确定性四态状态（§五 颜色联动 2026-09-13）。

    「点击雷达生成研报时，颜色状态、信号日期、确认条件和失效条件一起写入证据快照」：
    信号行本就带 date / confirmation_rule / invalidation_rule / trigger_price / age_bars，
    这里用 tactics_score.signal_state（与战法雷达页同一状态机）补齐 state/label/reason，
    模型得以按「新触发/待确认/已确认/已失效」如实措辞，前端与雷达共用同一颜色语义。
    状态判定失败只降级该条（不附 state），不拖垮证据包构建。
    """
    enriched: list[dict[str, Any]] = []
    for signal in signals:
        row = dict(signal)
        try:
            state = tactics_score.signal_state(
                direction=str(signal.get("direction", "")),
                signal_date=str(signal.get("date", "")),
                trigger_price=signal.get("trigger_price"),
                bars=bars,
            )
            row["state"] = state["state"]
            row["state_label"] = state["label"]
            if state.get("reason"):
                row["state_reason"] = str(state["reason"])
        except Exception:  # noqa: BLE001 - 单条状态判定失败不影响证据包（该条如实缺 state）
            pass
        enriched.append(row)
    return enriched


class _ScanCancelled(Exception):
    """D01（桌面端升级路线图 2026-09-18）：扫描任务被用户取消（逐票检测到取消标记时抛出）。"""


class _ScanProgress:
    """D01：扫描进度上报（写 scan_jobs 的 done/total）+ 逐票取消检测。

    同批票的二次重算（板块共振）以 count_progress=False 复用实例：取消检测仍生效，
    但不重复计数，保证 done ≤ total 恒成立。
    """

    def __init__(self, database: Database, job_id: str) -> None:
        self._database = database
        self._job_id = job_id
        self.done = 0
        self.total = 0

    def add_total(self, count: int) -> None:
        self.total += count
        self._database.update_scan_job_progress(self._job_id, self.done, self.total)

    def tick(self) -> None:
        self.done += 1
        self._database.update_scan_job_progress(self._job_id, self.done, self.total)

    def raise_if_cancelled(self) -> None:
        if self._database.get_scan_job_state(self._job_id) == "cancelled":
            raise _ScanCancelled()


def create_app(settings: CoreSettings) -> FastAPI:
    app = FastAPI(
        title="Investment Steward Core", version=__version__, docs_url=None, redoc_url=None
    )
    app.state.core = CoreState(settings)
    # F01（桌面端升级路线图 2026-09-18）：模型用量落表——所有 call_active_model 调用经此钩子
    # 写 model_calls 表（usage 缺失记 null；失败/超时同样记录 outcome）。
    model_client.set_model_call_recorder(lambda record: app.state.core.database.record_model_call(**record))
    # F02（桌面端升级路线图 2026-09-18）：三方取数尝试落表——6 个 feed 的低层 HTTP 出口
    # 经此钩子写 feed_attempts（成功与失败都记），使数据源成功率与延迟分位可复算。
    feed_health.set_feed_attempt_recorder(lambda record: app.state.core.database.record_feed_attempt(**record))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["X-Core-Session-Token", "Content-Type"],
    )

    @app.get("/health")
    def health(request: Request) -> dict[str, object]:
        core = _state(request)
        snapshots = core.database.list_evidence(core.local_user_id)
        channel = snapshot_channels(core.database, snapshots, __version__)
        registry = {entry["channel"]: dict(entry) for entry in channel_registry()}
        registry[UpdateChannel.HOST.value].update({"current": channel.host_version})
        registry[UpdateChannel.PLUGIN.value].update(
            {"current": ",".join(f"{pid}@{ver}" for pid, ver in channel.plugin_versions)}
        )
        registry[UpdateChannel.CONTENT.value].update(
            {"current": channel.content_fingerprint[:16]}
        )
        installations = core.database.list_plugin_installations()
        enabled = sum(1 for item in installations if item.state == PluginInstallationState.ENABLED)
        disabled = sum(1 for item in installations if item.state == PluginInstallationState.DISABLED)
        available = sum(
            1
            for pid in _manifest_index()
            if core.database.get_plugin_installation(pid) is None
        )
        targeted_by_slot = targeted_slots_by_installations(
            installations, _manifest_slots_index()
        )
        slot_rows = compute_slot_occupancy(core.settings, targeted_by_slot)
        data_as_of = _data_as_of(core)
        return {
            "status": "ready",
            "version": __version__,
            "core_version": __version__,
            "schema_version": "1.0",
            "data_as_of": data_as_of,
            "channels": registry,
            "plugins": {"enabled": enabled, "disabled": disabled, "available": available},
            "slots": {"used": sum(int(r["used"]) for r in slot_rows), "cap": sum(int(r["cap"]) for r in slot_rows)},
        }

    @app.get("/session")
    def session(core: CoreState = Depends(_require_session)) -> dict[str, str]:
        return {
            "user_id": str(core.local_user_id),
            "core_version": __version__,
            "schema_version": "1.0",
        }

    @app.get("/runner/config")
    def runner_config(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """插件 runner 出网与零密钥策略配置源（E1）：Host 注入 runner env 的数据来源。

        - network_allowlist：由 Core 聚合全部能力声明，作为 runner 出网门控的注入值；
        - env_credentials_policy 恒为 zero：Core 从不向 runner 环境注入凭据（G3-5）；
        - secret_env_markers：Core 进程自身的密钥泄漏审计（只报名字，不阻断、不暴露值）。
        """
        installations = core.database.list_plugin_installations()
        manifests = _manifest_index()
        granted_capabilities = sorted({
            capability
            for installation in installations
            if installation.state == PluginInstallationState.ENABLED
            for capability in manifests.get(installation.plugin_id, PluginManifest.model_construct(capabilities=[])).capabilities
        })
        return {
            "network_allowlist": aggregate_network_allowlist(),
            "granted_capabilities": granted_capabilities,
            "env_credentials_policy": "zero",
            "secret_env_markers": secret_env_markers(),
        }

    @app.get("/investment-policies", response_model=list[InvestmentPolicyVersion])
    def list_policies(core: CoreState = Depends(_require_session)) -> list[InvestmentPolicyVersion]:
        return core.database.list_policies(core.local_user_id)

    @app.post(
        "/investment-policies",
        response_model=InvestmentPolicyVersion,
        status_code=status.HTTP_201_CREATED,
    )
    def create_policy(
        body: PolicyDraftRequest, core: CoreState = Depends(_require_session)
    ) -> InvestmentPolicyVersion:
        existing = core.database.list_policies(core.local_user_id)
        policy = InvestmentPolicyVersion(
            policy_id=uuid4(),
            user_id=core.local_user_id,
            version=(max((item.version for item in existing), default=0) + 1),
            status=PolicyStatus.DRAFT,
            **body.model_dump(),
        )
        core.database.insert_policy(policy)
        _audit(core, "policy.draft_created", "investment_policy", str(policy.policy_id))
        return policy

    @app.post("/investment-policies/{policy_id}/confirm", response_model=InvestmentPolicyVersion)
    def confirm_policy(
        policy_id: UUID,
        body: PolicyConfirmationRequest,
        core: CoreState = Depends(_require_session),
    ) -> InvestmentPolicyVersion:
        try:
            policy = core.database.activate_policy(policy_id, core.local_user_id, body.model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        _audit(core, "policy.confirmed", "investment_policy", str(policy_id))
        return policy

    # ---- E02（桌面端升级路线图 2026-09-18）：恢复入口（dry-run + 正式写）----
    # （ImportApplyRequest 定义在模块级——闭包内定义的 pydantic 模型会成为不可解析的
    #   ForwardRef，FastAPI 会把参数误判成 query 参数。）
    @app.post("/import/preview")
    def import_preview(body: dict[str, Any] = Body(...), core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """恢复 dry-run（E02）：逐表报告 现有行数 / incoming / 主键冲突 / 缺失，不改任何数据。

        body 直接接受 /export/all 的导出体。非数据键（exported_at 等）原样忽略；
        未登记的键会列在 unsupported_keys 里提示核对（不猜测、不导入）。
        """
        known_keys = set(core.database.IMPORT_TABLE_MAP) | {"exported_at", "schema_version", "export_policy"}
        report: list[dict[str, object]] = []
        for export_key, table in core.database.IMPORT_TABLE_MAP.items():
            rows = body.get(export_key)
            if not isinstance(rows, list) or not rows:
                report.append({"export_key": export_key, "table": table, "status": "absent", "incoming": 0, "existing": 0, "conflicts": 0})
                continue
            try:
                report.append({"export_key": export_key, **core.database.import_table_preview(table, rows)})
            except ValueError as error:
                report.append({"export_key": export_key, "table": table, "status": "unsupported", "reason": str(error)})
        unsupported = [key for key in body if key not in known_keys]
        return {"ok": True, "tables": report, "unsupported_keys": unsupported}

    @app.post("/import/apply")
    def import_apply(body: ImportApplyRequest, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """正式恢复（E02）：恢复前强制 rotate_backup（可回退点），按表 merge/replace 写入，写入审计。

        - confirm 缺省 False → 422（显式确认要求，不做无提示覆盖）；
        - mode="merge"：INSERT OR IGNORE——主键已存在的行保留现状；mode="replace"：清空该表后原样写回；
        - 备份失败则中止恢复（绝不无回退点地改库）。
        """
        if not body.confirm:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="恢复需显式确认：confirm 必须为 true（apply 会按表覆盖/合并数据）",
            )
        from investment_steward_core.storage.database import rotate_backup

        requested = body.tables
        resolved: list[tuple[str, str]] = []
        for export_key, table in core.database.IMPORT_TABLE_MAP.items():
            if requested is not None and export_key not in requested and table not in requested:
                continue
            rows = body.data.get(export_key)
            if isinstance(rows, list) and rows:
                resolved.append((export_key, table))
        unknown_tables = [name for name in (requested or []) if name not in core.database.IMPORT_TABLE_MAP and name not in core.database.IMPORT_TABLE_MAP.values()]
        if unknown_tables:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"表不可导入（设备态/秘密/未登记）：{', '.join(unknown_tables)}")

        # 恢复前强制备份：失败即中止（绝不无回退点地改库）。
        try:
            backup_path = rotate_backup(core.database.path)
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"恢复前备份失败，已中止恢复：{error}") from error

        results = []
        for export_key, table in resolved:
            rows = body.data.get(export_key) or []
            outcome = core.database.import_table_apply(table, rows, body.mode)
            results.append({"export_key": export_key, **outcome})
        _audit(
            core,
            "import.applied",
            "database",
            str(core.database.path.name),
            {"mode": body.mode, "tables": [row["table"] for row in results], "backup": str(backup_path)},
        )
        return {
            "ok": True,
            "mode": body.mode,
            "backup_path": str(backup_path),
            "results": results,
            "note": "恢复前已自动轮换备份（rotate_backup）；如需回退，可用该备份目录中的快照。",
        }

    @app.get("/export/all")
    def export_all(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """全量数据导出（H1-2；E01 桌面端升级路线图 2026-09-18：逐表补全 + 导出政策成文）。

        - **覆盖**：原有 9 类（policies/holdings/theses/plans/decisions/evidence/research_runs/
          learning_activities/notifications）+ 用户产出表白名单（EXPORT_PAYLOAD_TABLES，raw
          `SELECT *` 无损失：研报与追问附录正文、快照/模板、战法复核/笔记/观察席、判断与验证、
          自选与核查、书架与研读计划、宏观视图/权重链/研究证据/发言人信号、个人设置/画像/简报/
          学习目标、量化制品索引与实验）+ model_profiles（仅方案元数据与 credential_ref 引用，
          **不含密钥**）。
        - **默认不含**（设备态/秘密/派生缓存，见 database.EXPORT_PAYLOAD_TABLES 注释）：
          credentials（密钥）、plugin_*、sync_*、audit_events、macro_cache。
        - quant_artifacts 导出的是**索引行**（含 content_hash 与文件名）；制品二进制本体在制品
          目录，属存储目录随迁，不在 JSON 内。
        """
        uid = core.local_user_id
        db = core.database
        payload_tables = db.export_payload_tables()
        return {
            "exported_at": datetime.now(UTC).isoformat(),
            "schema_version": "1.0",
            "export_policy": {
                "included": "用户产出表全量（见各键）；model_profiles 仅元数据不含密钥",
                "excluded_default_no_secrets": [
                    "credentials", "plugin_installations", "plugin_update_candidates",
                    "sync_inbox", "sync_outbox", "sync_state", "audit_events", "macro_cache",
                ],
                "quant_artifacts_note": "导出为索引行（content_hash + file_name）；制品二进制在制品目录",
            },
            "policies": db.list_policies(uid),
            "holdings": db.list_holdings(uid),
            "theses": db.list_theses(uid),
            "plans": db.list_plans(uid),
            "decisions": db.list_decisions(uid),
            "evidence": db.list_evidence(uid),
            "research_runs": db.list_research_runs(uid),
            "learning_activities": db.list_learning_activities(uid),
            "notifications": db.list_notifications(uid),
            "model_profiles": db.list_model_profiles(),
            **payload_tables,
        }

    @app.get("/quant/factors/compute/{symbol}")
    def quant_factors_compute(symbol: str, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """对标的计算内置特征序列(A1):ret_1/ret_5/vol_ratio/ma_ratio/range_ratio/rsi_14。"""
        data_installation = core.database.get_plugin_installation("official.cn-market-data")
        if data_installation is None or data_installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="cn-market-data plugin is not enabled"
            )
        try:
            bars, provider = fetch_cn_kline(symbol, limit=250, period="day")
        except FeedError as error:
            return {"available": False, "symbol": symbol, "factors": {}, "degraded_reason": f"行情拉取失败:{error}"}
        factors = {
            name: [round(v, 6) for v in quant_factors._feature_series(bars, name)]
            for name in quant_factors.FEATURE_NAMES
        }
        return {
            "available": len(bars) > 30,
            "symbol": symbol,
            "bars": len(bars),
            "factors": factors,
            "source": provider,
            "degraded_reason": None if len(bars) > 30 else "样本不足",
        }

    @app.post("/quant/factors/mine/{symbol}")
    def quant_factors_mine(symbol: str, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """确定性因子挖掘(A2):枚举公式空间,验证集 IC 排序,输出分享池阶段 A 形态参数集。"""
        data_installation = core.database.get_plugin_installation("official.cn-market-data")
        if data_installation is None or data_installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="cn-market-data plugin is not enabled"
            )
        try:
            bars, provider = fetch_cn_kline(symbol, limit=250, period="day")
        except FeedError as error:
            return {"available": False, "symbol": symbol, "top": [], "degraded_reason": f"行情拉取失败:{error}"}
        board = quant_factors.mine(bars)
        # 分享池阶段 A 形态字段:as_of 取收口 bar(数据口径,非墙上时钟),dataset_version 对齐 quant_pool 约定。
        as_of = str(bars[-1]["timestamp"]) if bars else None
        return {
            "available": bool(board["top"]),
            "symbol": symbol,
            "bars": len(bars),
            "top": board["top"],
            "note": board["note"],
            "source": provider,
            "as_of": as_of,
            "dataset_version": f"local:{symbol}:{as_of[:10]}" if as_of else None,
            "degraded_reason": None if board["top"] else "未挖到通过阈值的公式(阈值:|训练IC|≥0.05 且 |验证IC|≥0.02)",
        }

    def _sr_symbol_of(core_state: CoreState, artifact_id: str) -> str:
        entry = quant_pool.get_parameter_set(core_state.database, core_state.layout, artifact_id)
        return entry["symbol"] if entry else "510300"

    def _pack_symbol_of(entry: dict[str, object]) -> str:
        return str(entry.get("symbol") or "510300")

    def _replay_bars(core_state: CoreState, symbol: str) -> list[dict[str, object]]:
        bars, _provider = fetch_cn_kline(symbol, limit=250, period="day")
        return bars

    @app.get("/quant/parameter-sets")
    def quant_parameter_sets(core: CoreState = Depends(_require_session)) -> list[dict[str, object]]:
        """策略分享池阶段 A/D:本机参数集与线性模型列表(内容寻址、不可变),联表实盘记录。"""
        entries = quant_pool.list_parameter_sets(core.database, core.layout)
        for entry in entries:
            entry["track_records"] = quant_track_records.list_for(core.layout, entry["artifact_id"])
        return entries

    @app.get("/quant/parameter-sets/{artifact_id}/lineage")
    def quant_parameter_lineage(artifact_id: str, core: CoreState = Depends(_require_session)) -> list[dict[str, object]]:
        return quant_pool.lineage(core.database, core.layout, artifact_id)

    @app.get("/quant/parameter-sets/{artifact_id}/replay")
    def quant_parameter_replay(artifact_id: str, period: str = "day", core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """确定性回放:公式对最近 K 线逐 bar 求值,tanh 仓位 × 次日收益。"""
        data_installation = core.database.get_plugin_installation("official.cn-market-data")
        if data_installation is None or data_installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="cn-market-data plugin is not enabled"
            )
        try:
            bars, provider = fetch_cn_kline(_sr_symbol_of(core, artifact_id), limit=250, period=period)
        except FeedError as error:
            return {"available": False, "degraded_reason": f"行情拉取失败:{error}"}
        replay = quant_pool.replay(core.database, core.layout, artifact_id, bars)
        if replay is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"参数集不存在:{artifact_id}")
        return {**replay, "available": True, "source": provider}

    @app.post("/quant/parameter-sets")
    def quant_parameter_publish(
        payload: dict[str, object],
        core: CoreState = Depends(_require_session),
    ) -> dict[str, object]:
        """发布参数集:公式经全量求值验证(非法 422),指标确定性计算后内容寻址入库。"""
        try:
            entry = quant_pool.publish_parameter_set(
                core.database, core.layout,
                _replay_bars(core, str(payload.get("symbol", ""))),
                name=str(payload.get("name", "")),
                symbol=str(payload.get("symbol", "")),
                formula_tokens=list(payload.get("formula_tokens", [])),
                note=str(payload.get("note", "")),
            )
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
        return entry

    @app.post("/quant/parameter-sets/{artifact_id}/fork")
    def quant_parameter_fork(artifact_id: str, payload: dict[str, object], core: CoreState = Depends(_require_session)) -> dict[str, object]:
        forked = quant_pool.fork_parameter_set(
            core.database, core.layout, artifact_id,
            name=str(payload.get("name", "")), note=str(payload.get("note", "")),
        )
        if forked is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"参数集不存在:{artifact_id}")
        return forked

    # —— 阶段条(动态):各阶段开放状态与原因,UI 按此渲染「已开放/未开放」 ——
    @app.get("/quant/stages")
    def quant_stages(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """分享池四阶段开放状态:A 参数集、B 策略包(签名制 + 受限执行器)、
        C 两级实盘记录、D 线性模型;通用模型权重的更强执行隔离持续硬化中。"""
        return {
            "stages": [
                {"key": "A", "open": True, "reason": "参数集:纯 JSON,官方内核解释执行,确定性回放。"},
                {"key": "B", "open": True,
                 "reason": "策略包:仅运行发布者签名制品;受限执行器阻断文件/网络/子进程访问,超时强制终止。"},
                {"key": "C", "open": True,
                 "reason": "实盘记录两级制:自报显著标识、对账单哈希锚定核验;只作筛选,绝不参与排序。"},
                {"key": "D", "open": True,
                 "reason": "线性模型:权重为纯 JSON,官方内核解释执行,训练快照可复算;通用模型权重需更强执行隔离,后续开放。"},
            ]
        }

    # —— 阶段 D(限定形态):确定性线性模型训练 + 发布 ——
    @app.post("/quant/models/{symbol}")
    def quant_model_train_publish(symbol: str, payload: dict[str, object], core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """训练线性模型(闭式岭回归,同输入同结果)并发布入池;权重纯 JSON,内核解释执行。"""
        data_installation = core.database.get_plugin_installation("official.cn-market-data")
        if data_installation is None or data_installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="cn-market-data plugin is not enabled"
            )
        try:
            bars, provider = fetch_cn_kline(symbol, limit=250, period="day")
        except FeedError as error:
            return {"available": False, "symbol": symbol, "degraded_reason": f"行情拉取失败:{error}"}
        try:
            result = quant_models.train(bars, lambda_=float(payload.get("lambda", 1.0) or 1.0))
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
        entry = quant_pool.publish_model(
            core.database, core.layout,
            bars,
            name=str(payload.get("name", "") or f"{symbol} 线性模型"),
            symbol=symbol,
            weights=result["weights"],
            feature_order=result["feature_order"],
            metrics=result["metrics"],
            training=result["training"],
            note=str(payload.get("note", "") or result["note"]),
        )
        return {**entry, "available": True, "source": provider}

    # —— 阶段 C:实盘记录两级制(自报 / 对账单哈希锚定核验;只作筛选不参与排序) ——
    @app.post("/quant/parameter-sets/{artifact_id}/track-records")
    def quant_track_record_add(artifact_id: str, payload: dict[str, object], core: CoreState = Depends(_require_session)) -> dict[str, object]:
        if quant_pool.get_parameter_set(core.database, core.layout, artifact_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"制品不存在:{artifact_id}")
        try:
            record = quant_track_records.add_self_reported(
                core.layout,
                artifact_id,
                period_start=str(payload.get("period_start", "")),
                period_end=str(payload.get("period_end", "")),
                return_pct=float(payload.get("return_pct", 0.0) or 0.0),
                max_drawdown_pct=payload.get("max_drawdown_pct"),
                note=str(payload.get("note", "")),
            )
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
        return record

    @app.post("/quant/parameter-sets/{artifact_id}/track-records/verify")
    def quant_track_record_verify(artifact_id: str, payload: dict[str, object], core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """对账单核验:对账单内容 SHA-256 锚定;本机形态下核验人是用户本人,不冒充平台核验。"""
        if quant_pool.get_parameter_set(core.database, core.layout, artifact_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"制品不存在:{artifact_id}")
        try:
            record = quant_track_records.verify_with_statement(
                core.layout,
                artifact_id,
                statement=str(payload.get("statement", "")),
                source=str(payload.get("source", "")),
                period_start=str(payload.get("period_start", "")),
                period_end=str(payload.get("period_end", "")),
                return_pct=float(payload.get("return_pct", 0.0) or 0.0),
                max_drawdown_pct=payload.get("max_drawdown_pct"),
                note=str(payload.get("note", "")),
            )
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
        return record

    # —— 阶段 B(本机忠实边界):策略包签名/验签/目录;执行面待真沙箱 ADR,显式 409 ——
    @app.post("/quant/strategy-packs")
    def quant_strategy_pack_import(payload: dict[str, object], core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """导入策略包 manifest:Ed25519 验签 + 完整性校验;通过也仅入目录并锁定(沙箱未落地)。"""
        manifest = payload.get("manifest")
        if not isinstance(manifest, dict):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="缺少 manifest 对象")
        try:
            entry = quant_strategy_packs.import_pack(
                core.layout,
                manifest,
                public_key_pem=_pinned_public_key_pem(core.settings),
            )
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
        return entry

    @app.get("/quant/strategy-packs")
    def quant_strategy_pack_list(core: CoreState = Depends(_require_session)) -> list[dict[str, object]]:
        return quant_strategy_packs.list_packs(core.layout)

    @app.post("/quant/strategy-packs/{pack_id}/run")
    def quant_strategy_pack_run(pack_id: str, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """运行已签名策略包:受限执行器执行包代码,对最近 K 线产出确定性回测统计。"""
        entry = quant_strategy_packs.get_pack(core.layout, pack_id)
        if entry is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"策略包不存在:{pack_id}")
        if entry.get("state") != "installed" or not entry.get("payload"):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该策略包不包含可执行代码,请重新导入带 payload 的签名版本。")
        data_installation = core.database.get_plugin_installation("official.cn-market-data")
        if data_installation is None or data_installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="cn-market-data plugin is not enabled"
            )
        try:
            code = quant_strategy_packs.verify_payload_integrity(entry)
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
        try:
            bars, provider = fetch_cn_kline(_pack_symbol_of(entry), limit=250, period="day")
        except FeedError as error:
            return {"available": False, "pack_id": pack_id, "degraded_reason": f"行情拉取失败:{error}"}
        result = quant_pack_runner.run_pack_payload(code, bars)
        if not result.get("ok"):
            return {"available": False, "pack_id": pack_id, "degraded_reason": result.get("error", "策略包运行失败")}
        positions = result["positions"]
        if len(positions) < len(bars) - 1:
            return {"available": False, "pack_id": pack_id, "degraded_reason": f"包返回仓位数量不足({len(positions)} < {len(bars) - 1})"}
        stats = quant_pack_runner.equity_stats(bars, positions)
        return {
            "available": True,
            "pack_id": pack_id,
            "name": entry["name"],
            "source": provider,
            **stats,
            "note": "策略包回测:受限执行器运行发布者签名代码;仅作研究背景,不构成买卖建议",
        }

    @app.get("/evidence")
    def list_evidence(
        limit: int = 200,
        offset: int = 0,
        evidence_type: str | None = None,
        with_total: bool = False,
        core: CoreState = Depends(_require_session),
    ) -> list[Evidence] | dict[str, object]:
        """证据列表（B02，桌面端升级路线图 2026-09-18：默认上限 200、服务端翻页）。

        created_at 倒序；evidence_type 按类型枚举值过滤；with_total=true 返回
        {"ok","items","total","offset","limit"} 信封（扩展/列表页翻页用），否则返回裸列表
        （boot 消费方读最近一页即可，全量导出走 /export/all，不受此上限影响）。"""
        page_limit = max(1, min(limit, 500))
        page_offset = max(0, offset)
        items, total = core.database.list_evidence_page(
            core.local_user_id, limit=page_limit, offset=page_offset, evidence_type=evidence_type
        )
        if with_total:
            return {"ok": True, "items": items, "total": total, "offset": page_offset, "limit": page_limit}
        return items

    @app.get("/evidence/support-resistance/{symbol}")
    def support_resistance_levels(symbol: str, period: str = "day", core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """支撑阻力位识别(official.support-resistance):摆动点聚类 + 触碰/守住统计。

        数据来自本机行情管道(cn-market-data);样本不足或拉取失败返回 available=False,不编造。
        """
        installation = core.database.get_plugin_installation("official.support-resistance")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="support-resistance plugin is not enabled"
            )
        data_installation = core.database.get_plugin_installation("official.cn-market-data")
        if data_installation is None or data_installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="cn-market-data plugin is not enabled"
            )
        try:
            bars, provider = fetch_cn_kline(symbol, limit=250, period=period)
        except FeedError as error:
            return {
                "available": False, "symbol": symbol, "levels": [],
                "degraded_reason": f"行情拉取失败:{error}",
            }
        levels = support_resistance.detect_levels(bars)
        for level in levels:
            level["status"] = "ok"
            level["note"] = f"摆动点聚类 + 触碰/守住统计(样本 {level['samples']} 次)"
        return {
            "available": bool(levels),
            "symbol": symbol,
            "period": period,
            "levels": levels,
            "source": provider,
            "as_of": bars[-1]["timestamp"] if bars else None,
            "degraded_reason": None if levels else "样本不足或未识别出关键位(不编造)",
        }

    @app.get("/evidence/comtrade")
    def comtrade_preview(
        reporter_code: int | None = None,
        partner_code: int = 0,
        period: str | None = None,
        cmd_code: str = "TOTAL",
        flow_code: str = "M",
        max_records: int = 100,
        core: CoreState = Depends(_require_session),
    ) -> dict[str, object]:
        """UN Comtrade 贸易预览：真实数据或明确降级，不生成本地占位记录。"""
        if reporter_code is not None and not 0 <= reporter_code <= 999:
            raise HTTPException(status_code=422, detail="reporter_code 应为 0..999")
        if not 0 <= partner_code <= 999:
            raise HTTPException(status_code=422, detail="partner_code 应为 0..999")
        if not 1 <= max_records <= 500:
            raise HTTPException(status_code=422, detail="max_records 应为 1..500")
        if not cmd_code or len(cmd_code) > 100 or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789,-" for char in cmd_code):
            raise HTTPException(status_code=422, detail="cmd_code 含有不支持的字符")
        if not flow_code or len(flow_code) > 20 or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789,-" for char in flow_code):
            raise HTTPException(status_code=422, detail="flow_code 含有不支持的字符")
        store = resolve_store(core.database)
        return comtrade_feed.fetch_preview(
            store.get("comtrade_key") or store.get("comtrade_api_key"),
            reporter_code=reporter_code,
            partner_code=partner_code,
            period=period,
            cmd_code=cmd_code,
            flow_code=flow_code,
            max_records=max_records,
        )

    @app.get("/evidence/collab-state")
    def collab_run_state(run_id: str, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """v23 协同流水线③查询运行状态（页面重挂后恢复视图；运行态在内存，重启清空）。

        ★ 必须注册在 GET /evidence/{evidence_id}（UUID 通配）之前，否则被截获 422
        （与 /evidence/macro/weights 的路由顺序教训同源）。
        """
        run = collab_engine.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="协同运行不存在或已过期")
        return {"ok": True, "run": collab_engine.public_view(run)}

    @app.get("/evidence/{evidence_id}", response_model=Evidence)
    def get_evidence(evidence_id: UUID, core: CoreState = Depends(_require_session)) -> Evidence:
        evidence = core.database.get_evidence(evidence_id, core.local_user_id)
        if evidence is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="evidence not found")
        return evidence

    @app.get("/evidence/macro/weights")
    def list_macro_weights(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """宏观权重版本链（M5.5）：active = 最新版本；无记录 → 默认 v1。"""
        installation = core.database.get_plugin_installation("official.macro-radar")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="macro-radar plugin is not enabled"
            )
        versions = core.database.list_macro_weight_versions()
        return {
            "active_version": versions[0]["version"] if versions else macro_scoring.WEIGHT_VERSION,
            "default_weights": macro_scoring.DEFAULT_WEIGHTS,
            "versions": versions,
        }

    @app.post("/evidence/macro/weights")
    def save_macro_weights(body: MacroWeightRequest, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """保存新的用户权重版本（M5.5）：用户主权，不受 AI 护栏约束，仅校验合计=1.0 与四维齐全。

        偏离默认 >0.20 的维度在响应中标 personalized，由前端提示「与其他口径结果不可比」。
        """
        installation = core.database.get_plugin_installation("official.macro-radar")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="macro-radar plugin is not enabled"
            )
        expected = set(macro_scoring.DEFAULT_WEIGHTS)
        if set(body.weights) != expected:
            raise HTTPException(status_code=422, detail=f"权重必须且只能包含四维：{sorted(expected)}")
        if any(w < 0 or w > 1 for w in body.weights.values()):
            raise HTTPException(status_code=422, detail="每维权重应在 0-1 之间（小数口径，与默认权重一致）")
        if abs(sum(body.weights.values()) - 1.0) > 0.005:
            raise HTTPException(status_code=422, detail=f"四维权重合计必须为 1.0（当前 {sum(body.weights.values())}）")
        versions = core.database.list_macro_weight_versions()
        # v1 保留给内置默认（不入库）；用户版本从 v2 起编号：取已有最大号 +1。
        existing_numbers = [
            int(row["version"][1:]) for row in versions
            if row["version"].startswith("v") and row["version"][1:].isdigit()
        ]
        version = f"v{max(existing_numbers + [1]) + 1}"
        created_at = datetime.now(UTC).isoformat()
        core.database.insert_macro_weight_version(version, body.weights, body.source, body.note or None, created_at)
        _audit(core, "macro.weights.saved", "macro_weight_version", version)
        personalized = [
            dim for dim, default in macro_scoring.DEFAULT_WEIGHTS.items()
            if abs(body.weights[dim] - default) > 0.20
        ]
        return {"version": version, "weights": body.weights, "personalized": personalized, "saved_at": created_at, "source": body.source}

    @app.post("/evidence/macro/weights/ai-proposal")
    def propose_macro_weights(body: AiProposalRequest, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """AI 权重提议（M5.5，D-12 护栏）：只生成待确认提议，不写库；用户确认后走 POST /evidence/macro/weights（source=ai）。

        护栏（只约束 AI）：单维变化 ≤10pp、每维 ≥10%、30 天内 source=ai 版本最多 2 次。
        """
        installation = core.database.get_plugin_installation("official.macro-radar")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="macro-radar plugin is not enabled"
            )
        if not core.settings.model_access_enabled:
            return {"ok": False, "stage": "model_access_disabled", "detail": "模型出网已关闭（STEWARD_MODEL_ACCESS=0），未发起任何模型请求"}
        profile = model_client.active_model_profile(core.database.list_model_profiles())
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="没有「使用中」的模型方案：先在设置页把一个方案置为使用中（模型提议依赖模型出网）",
            )
        versions = core.database.list_macro_weight_versions()
        if macro_ai.count_recent_ai_proposals(versions) >= macro_ai.AI_PROPOSAL_MAX_PER_WINDOW:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"模型提议已达 {macro_ai.AI_PROPOSAL_WINDOW_DAYS} 天 {macro_ai.AI_PROPOSAL_MAX_PER_WINDOW} 次上限（护栏）。你仍可在权重面板手动调整（手动不受护栏限制）。",
            )
        current_weights = versions[0]["weights"] if versions else dict(macro_scoring.DEFAULT_WEIGHTS)
        current_version = versions[0]["version"] if versions else macro_scoring.WEIGHT_VERSION
        user_prompt = (
            f"当前权重（版本 {current_version}）：{json.dumps(current_weights, ensure_ascii=False)}\n"
            f"默认权重：{json.dumps(macro_scoring.DEFAULT_WEIGHTS, ensure_ascii=False)}\n"
            f"用户调整意图（含发言人表态原句时必须引用）：{body.intent}"
        )
        try:
            reply = model_client.call_active_model(
                profile,
                resolve_store(core.database),
                model_client.format_messages(macro_ai.PROPOSAL_SYS_PROMPT, user_prompt),
             purpose="宏观",)
        except model_client.ModelUnavailable as exc:
            return {"ok": False, "stage": "model_call", "detail": f"模型调用失败：{exc}"}
        parsed = macro_ai.extract_json_object(reply.content)
        if parsed is None or not isinstance(parsed.get("weights"), dict):
            return {"ok": False, "stage": "parse", "detail": "模型输出无法解析为提议 JSON（未生成提议卡）"}
        proposal_weights, guardrail_reason = macro_ai.check_proposal_guardrails(
            parsed["weights"], current_weights
        )
        if proposal_weights is None:
            return {"ok": False, "stage": "guardrail", "detail": guardrail_reason}
        proposal = {
            "weights": proposal_weights,
            "rationale": str(parsed.get("rationale", "")).strip(),
            "dim_changed": str(parsed.get("dim_changed", "")).strip(),
            "direction": str(parsed.get("direction", "")).strip(),
            "base_version": current_version,
            "model": profile.model,
            "latency_ms": reply.latency_ms,
        }
        _audit(core, "macro.weights.ai_proposal", "macro_weight_version", current_version)
        return {"ok": True, "proposal": proposal}

    @app.post("/evidence/macro/{region}/analysis")
    def analyze_macro_region(region: str, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """M5.2 AI 分析层：基于规则快照生成结构化分析（三层判断中的 AI 层）。

        硬约束：只读规则层数据，不修改打分；引用校验器拦截无引用输出（无引用不发布，ADR-0006）。
        结果覆盖式缓存于 macro_cache（series_key = ai_analysis:{region}），供回看。
        """
        installation = core.database.get_plugin_installation("official.macro-radar")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="macro-radar plugin is not enabled"
            )
        if not core.settings.model_access_enabled:
            return {"ok": False, "stage": "model_access_disabled", "detail": "模型出网已关闭（STEWARD_MODEL_ACCESS=0），未发起任何模型请求", "analysis": None}
        profile = model_client.active_model_profile(core.database.list_model_profiles())
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="没有「使用中」的模型方案：先在设置页把一个方案置为使用中",
            )
        versions = core.database.list_macro_weight_versions()
        snapshot = macro_feed.get_region_snapshot(
            core.database, resolve_store(core.database), region.lower(),
            weights=versions[0]["weights"] if versions else None,
            weight_version=versions[0]["version"] if versions else macro_scoring.WEIGHT_VERSION,
        )
        if snapshot is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"未知 region：{region}（可选 us/cn/eu/jp/in/global）",
            )
        if snapshot.positioning is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该经济体暂无规则基线定位（无 ok 指标），模型分析无从展开",
            )
        try:
            reply = model_client.call_active_model(
                profile,
                resolve_store(core.database),
                model_client.format_messages(
                    macro_ai.ANALYSIS_SYS_PROMPT, macro_ai.build_analysis_context(snapshot)
                ),
             purpose="宏观",)
        except model_client.ModelUnavailable as exc:
            return {"ok": False, "stage": "model_call", "detail": f"模型调用失败：{exc}", "analysis": None}
        cited = macro_ai.verify_citations(reply.content, snapshot)
        if not cited:
            return {
                "ok": False,
                "stage": "citation_check",
                "detail": "模型分析未内联引用真实指标（无引用不发布，ADR-0006），已拒绝本次输出",
                "analysis": None,
            }
        result = {
            "ok": True,
            "region": snapshot.region,
            "analysis": reply.content,
            "citations": cited,
            "direction": macro_ai.parse_analysis_direction(reply.content),
            "band": snapshot.positioning.band,
            "composite": snapshot.positioning.composite,
            "weight_version": snapshot.positioning.weight_version,
            "model": profile.model,
            "latency_ms": reply.latency_ms,
            "generated_at": datetime.now(UTC).isoformat(),
        }
        core.database.upsert_macro_series(
            f"ai_analysis:{snapshot.region}",
            {"payload": result, "as_of": result["generated_at"], "dataset_version": f"ai_analysis:{reply.model}"},
        )
        _audit(core, "macro.analysis.generated", "macro_cache", f"ai_analysis:{snapshot.region}")
        return result

    @app.get("/evidence/macro/{region}/my-view")
    def get_macro_user_view(region: str, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """读取用户分析（第三层）：无记录返回 view=None（前端显示空编辑器）。"""
        installation = core.database.get_plugin_installation("official.macro-radar")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="macro-radar plugin is not enabled"
            )
        view = core.database.get_macro_user_view(region.lower())
        return {"region": region.lower(), "view": view}

    @app.get("/evidence/macro/{region}/analysis")
    def get_macro_analysis_cache(region: str, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """读取最近一次 AI 分析（覆盖式缓存）：页面加载时回填，避免重复消耗模型调用。"""
        installation = core.database.get_plugin_installation("official.macro-radar")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="macro-radar plugin is not enabled"
            )
        cached = core.database.get_macro_series(f"ai_analysis:{region.lower()}")
        if cached is None or not isinstance(cached.get("payload"), dict):
            return {"ok": False, "cached": False, "detail": "暂无历史分析（点「模型解读」生成）", "analysis": None}
        payload = cached["payload"]
        return {**payload, "cached": True}

    @app.put("/evidence/macro/{region}/my-view", response_model=MacroUserView)
    def save_macro_user_view(region: str, body: MacroUserViewRequest, core: CoreState = Depends(_require_session)) -> MacroUserView:
        """保存用户分析（第三层，用户主权）：仅本机，不参与规则计算、不进证据账本客观区。"""
        installation = core.database.get_plugin_installation("official.macro-radar")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="macro-radar plugin is not enabled"
            )
        if region.lower() not in {"us", "cn", "eu", "jp", "in"}:
            raise HTTPException(status_code=422, detail=f"region 仅支持 us/cn/eu/jp/in（当前 {region}）")
        view = MacroUserView(region=region.lower(), **body.model_dump())
        core.database.upsert_macro_user_view(view)
        _audit(core, "macro.user_view.saved", "macro_user_view", region.lower())
        return view

    @app.get("/evidence/macro/trade")
    def macro_trade_flow(reporter: int, partner: int, years: int = 1, cmd: str = "TOTAL", freq: str = "A", months: int = 3, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        if not 1 <= years <= 10:
            raise HTTPException(status_code=422, detail="years must be between 1 and 10")
        # 有订阅 key（key_id=comtrade_key）走订阅端点（不触 500 行上限）；无 key 回退公共 preview
        api_key = resolve_store(core.database).get("comtrade_key")
        if freq == "M":
            # 月度（尽可能新的数据）：订阅端点 period 多值单请求拿多月；无 key 如实 pending，不编造
            months = max(1, min(6, months))
            if not api_key:
                return {"reporter": reporter, "partner": partner, "freq": "M", "series": [{"pending": True, "degraded_reason": "月度贸易数据需要 Comtrade 订阅 key（凭据库 comtrade_key）"}], "source": "UN Comtrade subscription"}
            now = datetime.now(UTC)
            y, m = now.year, now.month
            periods: list[str] = []
            for _ in range(months):
                m -= 1
                if m == 0:
                    m, y = 12, y - 1
                periods.append(f"{y}{m:02d}")
            periods.reverse()
            try:
                return macro_trade.fetch_trade_monthly(reporter, partner, periods, cmd_code=cmd, api_key=api_key)
            except Exception as exc:  # noqa: BLE001 - 月度失败整体 pending，前端回退年度
                return {"reporter": reporter, "partner": partner, "freq": "M", "series": [{"pending": True, "degraded_reason": str(exc)}], "source": "UN Comtrade subscription"}
        end = datetime.now(UTC).year - 1
        series = []
        for year in range(end - years + 1, end + 1):
            try:
                series.append(macro_trade.fetch_trade(reporter, partner, year, cmd_code=cmd, api_key=api_key))
            except Exception as exc:  # noqa: BLE001 - 单年拉取失败仅该年降级 pending，不中断其余年份
                series.append({"period": str(year), "pending": True, "degraded_reason": str(exc), "reporter": reporter, "partner": partner})
        return {"reporter": reporter, "partner": partner, "cmd": cmd, "series": series, "source": "UN Comtrade subscription" if api_key else "UN Comtrade preview"}

    @app.get("/evidence/macro/research-evidence")
    def list_macro_research(event: str | None = None, core: CoreState = Depends(_require_session)):
        """宏观研究证据账本（v21 起落库 macro_research_evidence，重启不丢）。"""
        values = core.database.list_macro_research_evidence()
        return [x for x in values if event is None or x["event_id"] == event]

    @app.post("/evidence/macro/research-evidence", status_code=status.HTTP_201_CREATED)
    def create_macro_research(body: MacroResearchEvidenceRequest, core: CoreState = Depends(_require_session)):
        if not body.source_refs or not body.time_window:
            raise HTTPException(status_code=422, detail="source_refs and time_window are required")
        now = datetime.now(UTC).isoformat()
        item = body.model_dump(); item["id"] = str(uuid4()); item["created_at"] = now; item["updated_at"] = now
        core.database.upsert_macro_research_evidence(item)
        return item

    @app.get("/evidence/macro/impacts")
    def macro_impacts(event: str | None = None, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        items = [{"event_id": "ukraine", "title": "俄乌战争", "chains": [{"commodity": "粮食/化肥", "steps": ["供应与运输受扰", "全球成本压力"]}]}, {"event_id": "iran", "title": "美伊战争", "chains": [{"commodity": "原油/成品油", "steps": ["海湾运输敞口", "能源价格与通胀传导"]}]}]
        return {"items": [x for x in items if event is None or x["event_id"] == event]}

    @app.get("/evidence/macro/events")
    def macro_events(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """事件影响层（用户定稿：俄乌→粮食/化肥/能源，美伊→原油/航运；不呈现冲突方国内数据）。

        复用 macro_feed 同一管道：FRED 实拉一次即落 macro_cache（TTL 12h），失败回退旧缓存；
        pending 行不输出数值（不编造，ADR-0006）。需 official.macro-radar 启用。
        """
        installation = core.database.get_plugin_installation("official.macro-radar")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="macro-radar plugin is not enabled"
            )
        return {"items": macro_feed.get_event_impacts(core.database, resolve_store(core.database))}

    @app.get("/evidence/macro/releases")
    def macro_releases(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """经济数据发布看板（用户需求：CPI/非农/议息——前值、预期、实际）。

        前值/实际由 FRED 序列计算（落库缓存）；市场预期（consensus）为第三方源未接入，
        expectation 恒 None 并全局注明（不编造）。需 official.macro-radar 启用。
        """
        installation = core.database.get_plugin_installation("official.macro-radar")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="macro-radar plugin is not enabled"
            )
        return macro_feed.get_release_board(core.database, resolve_store(core.database))

    @app.get("/evidence/macro/calendar", response_model=MacroCalendarSnapshot)
    def macro_calendar(days: int = 14, core: CoreState = Depends(_require_session)) -> MacroCalendarSnapshot:
        """事件日历（信息层·事件层首版，零外部依赖）：未来 N 天固定节奏数据发布日历。

        只生成官方固定节奏规则可推算的事件（非农=每月第一个周五等）；FOMC 等非固定
        节奏事件不生成日期（不编造），由前端明示以官方日历为准。days 夹取 1..60。

        v37：每条事件带 `phase`（upcoming/in_window/elapsed）与实际值关联——窗口期内
        不再一律当作「未来事件」，已过窗口按规则标 `elapsed`；实际值来自 macro_feed
        同一管道，取不到保持 None（不编造）。
        """
        installation = core.database.get_plugin_installation("official.macro-radar")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="macro-radar plugin is not enabled"
            )
        return macro_calendar_module.calendar_snapshot(
            datetime.now(UTC).date(),
            days=max(1, min(days, 60)),
            readings=_macro_calendar_readings(core),
        )

    @app.get("/evidence/macro/{region}", response_model=MacroSnapshot)
    def macro_snapshot(region: str, core: CoreState = Depends(_require_session)) -> MacroSnapshot:
        """宏观雷达数据快照（M4）：region = us/cn/eu/jp/in 或 global（货币/油价/金价背景层）。

        仅 official.macro-radar 启用时可调用；pending 项不输出数值（不编造，ADR-0006）。
        FRED key 从凭据库读取（D-11），缓存于本机 macro_cache（as-of + dataset_version）。
        定位打分使用权重版本链的最新版本（M5.5），无记录时用默认 v1。
        """
        installation = core.database.get_plugin_installation("official.macro-radar")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="macro-radar plugin is not enabled"
            )
        versions = core.database.list_macro_weight_versions()
        snapshot = macro_feed.get_region_snapshot(
            core.database, resolve_store(core.database), region.lower(),
            weights=versions[0]["weights"] if versions else None,
            weight_version=versions[0]["version"] if versions else macro_scoring.WEIGHT_VERSION,
        )
        if snapshot is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"未知 region：{region}（可选 us/cn/eu/jp/in/global）",
            )
        return snapshot

    @app.get("/evidence/macro/{region}/pricing", response_model=MacroPricingSnapshot)
    def macro_pricing_snapshot(region: str, core: CoreState = Depends(_require_session)) -> MacroPricingSnapshot:
        """市场定价对照层（D-14）：FRED 公开序列代理指标，背景参考，不进四维打分。

        序列缓存用独立 `pricing:` 前缀（不与指标层混存，方案 §2）；日度序列 24h
        过期重拉，拉取失败回退旧缓存（as_of 如实标注陈旧）。pending 项不输出数值。
        """
        installation = core.database.get_plugin_installation("official.macro-radar")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="macro-radar plugin is not enabled"
            )
        snapshot = macro_pricing.get_region_pricing(
            core.database, resolve_store(core.database), region.lower(),
        )
        if snapshot is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"未知 region：{region}（可选 us/cn/eu/jp/in）",
            )
        return snapshot

    @app.get("/evidence/macro/{region}/signals", response_model=list[SpeakerSignal])
    def list_speaker_signals(region: str, core: CoreState = Depends(_require_session)) -> list[SpeakerSignal]:
        """发言人信号流（§3.5 段一/段二）：按事件日期倒序列出某经济体沟通证据。"""
        installation = core.database.get_plugin_installation("official.macro-radar")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="macro-radar plugin is not enabled"
            )
        if region.lower() not in {"us", "cn", "eu", "jp", "in"}:
            raise HTTPException(status_code=422, detail=f"region 仅支持 us/cn/eu/jp/in（当前 {region}）")
        return core.database.list_speaker_signals(region.lower())

    @app.post("/evidence/macro/{region}/signals", response_model=SpeakerSignal, status_code=status.HTTP_201_CREATED)
    def create_speaker_signal(region: str, body: SpeakerSignalCreate, core: CoreState = Depends(_require_session)) -> SpeakerSignal:
        """录入沟通证据（§3.5 段一）：官方原文粘贴 + 来源可跳转；方向留空待 AI 解读（段二）。"""
        installation = core.database.get_plugin_installation("official.macro-radar")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="macro-radar plugin is not enabled"
            )
        if region.lower() not in {"us", "cn", "eu", "jp", "in"}:
            raise HTTPException(status_code=422, detail=f"region 仅支持 us/cn/eu/jp/in（当前 {region}）")
        signal = SpeakerSignal(signal_id=uuid4().hex, region=region.lower(), **body.model_dump())
        core.database.upsert_speaker_signal(signal)
        _audit(core, "macro.speaker_signal.created", "speaker_signals", signal.signal_id)
        return signal

    @app.post("/evidence/macro/{region}/signals/{signal_id}/interpret")
    def interpret_speaker_signal(region: str, signal_id: str, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """AI 解读沟通证据（§3.5 段二）：方向标注 + 关注点迁移，必须引用原文句子。

        无引用不回填方向（ADR-0006）：校验不过时 signal 保持原样返回（ok=false）。
        解读只提供「哪类信号更重要」依据，权重调整仍走 M5.5 提议卡/手动滑杆。
        """
        installation = core.database.get_plugin_installation("official.macro-radar")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="macro-radar plugin is not enabled"
            )
        signal = core.database.get_speaker_signal(signal_id)
        if signal is None or signal.region != region.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"信号不存在：{signal_id}")
        if not core.settings.model_access_enabled:
            return {
                "ok": False,
                "stage": "model_access_disabled",
                "detail": "模型出网已关闭（STEWARD_MODEL_ACCESS=0），未发起任何模型请求",
                "signal": signal.model_dump(mode="json"),
            }
        profile = model_client.active_model_profile(core.database.list_model_profiles())
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="没有「使用中」的模型方案：先在设置页把一个方案置为使用中",
            )
        context = (
            f"经济体：{signal.region}\n发言人：{signal.speaker}\n场合：{signal.event_type or '未填'}\n"
            f"日期：{signal.event_date}\n来源：{signal.source_name}{(' ' + signal.source_url) if signal.source_url else ''}\n"
            f"原文：\n{signal.excerpt}"
        )
        try:
            reply = model_client.call_active_model(
                profile,
                resolve_store(core.database),
                model_client.format_messages(macro_ai.SPEAKER_SYS_PROMPT, context),
             purpose="宏观",)
        except model_client.ModelUnavailable as exc:
            return {"ok": False, "stage": "model_call", "detail": f"模型调用失败：{exc}", "signal": signal.model_dump(mode="json")}
        parsed = macro_ai.extract_json_object(reply.content)
        if parsed is None:
            return {"ok": False, "stage": "json_parse", "detail": "模型未返回有效 JSON，已拒绝本次输出", "signal": signal.model_dump(mode="json")}
        direction = str(parsed.get("direction", ""))
        rationale = str(parsed.get("rationale", ""))
        if direction not in {"鹰派", "鸽派", "中性"} or not rationale:
            return {"ok": False, "stage": "format_check", "detail": "模型输出缺少方向或依据，已拒绝本次输出", "signal": signal.model_dump(mode="json")}
        cited = macro_ai.verify_speaker_citations(rationale, signal.excerpt)
        if not cited:
            return {
                "ok": False,
                "stage": "citation_check",
                "detail": "模型解读未逐字引用原文句子（无引用不回填方向，ADR-0006），已拒绝本次输出",
                "signal": signal.model_dump(mode="json"),
            }
        focus_shift = parsed.get("focus_shift")
        signal.direction = direction
        signal.ai_rationale = rationale
        signal.focus_shift = str(focus_shift)[:500] if focus_shift else None
        signal.model = profile.model
        signal.interpreted_at = datetime.now(UTC)
        core.database.upsert_speaker_signal(signal)
        _audit(core, "macro.speaker_signal.interpreted", "speaker_signals", signal.signal_id)
        return {"ok": True, "citations": cited, "signal": signal.model_dump(mode="json")}

    @app.post("/evidence/macro/{region}/anomalies", response_model=list[Evidence], status_code=status.HTTP_201_CREATED)
    def submit_macro_anomalies(region: str, core: CoreState = Depends(_require_session)) -> list[Evidence]:
        """把当前快照中命中的重度规则（|分值变动| ≥ 25）作为 macro 证据提交 research 账本（M6）。

        服务端构造 Evidence（content_hash 含观测期做幂等去重，重复提交返回已存在条目不重复入账）；
        规则基线数据本身就是证据原文，无需 AI 摘要，不构成买卖建议。
        """
        installation = core.database.get_plugin_installation("official.macro-radar")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="macro-radar plugin is not enabled"
            )
        versions = core.database.list_macro_weight_versions()
        snapshot = macro_feed.get_region_snapshot(
            core.database, resolve_store(core.database), region.lower(),
            weights=versions[0]["weights"] if versions else None,
            weight_version=versions[0]["version"] if versions else macro_scoring.WEIGHT_VERSION,
        )
        if snapshot is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"未知 region：{region}（可选 us/cn/eu/jp/in/global）",
            )
        if snapshot.positioning is None:
            return []
        rules = macro_scoring.severe_rules(snapshot.positioning)
        if not rules:
            return []
        created: list[Evidence] = []
        for rule in rules:
            digest = hashlib.sha256(f"{region}:{rule}:{snapshot.generated_at.date()}".encode()).hexdigest()[:32]
            existing = core.database.get_evidence_by_hash(digest, core.local_user_id)
            if existing is not None:
                created.append(existing)
                continue
            evidence = Evidence(
                evidence_id=uuid4(),
                tenant_id=core.local_user_id,
                subject_refs=[f"macro:{region}"],
                evidence_type=EvidenceType.MACRO,
                source_name=f"宏观雷达 · 规则异动（{region.upper()}）",
                license_status="public_data",
                source_trust_note="规则基线数据即证据原文；打分规则随插件发布可复算",
                observed_at=snapshot.generated_at,
                collected_at=datetime.now(UTC),
                source_dataset_version=snapshot.indicators[0].dataset_version if snapshot.indicators else None,
                producer_plugin_id="official.macro-radar",
                schema_version="1.0",
                summary=f"{region.upper()} 宏观异动：{rule}。综合定位 {snapshot.positioning.band}（{snapshot.positioning.composite} 分，权重 {snapshot.positioning.weight_version}）。",
                content_hash=digest,
                relation=EvidenceRelation.SUPPORTING,
                freshness="fresh",
                quality_flags=["rule_based", "reproducible"],
                limitations=[*snapshot.positioning.limitations],
                status=EvidenceStatus.ACTIVE,
            )
            try:
                core.database.insert_evidence(evidence)
            except IntegrityError:
                existing = core.database.get_evidence_by_hash(digest, core.local_user_id)
                if existing is not None:
                    created.append(existing)
                    continue
                raise
            _audit(core, "evidence.created", "evidence", str(evidence.evidence_id))
            created.append(evidence)
        return created

    @app.post("/evidence", response_model=Evidence, status_code=status.HTTP_201_CREATED)
    def submit_evidence(body: Evidence, core: CoreState = Depends(_require_session)) -> Evidence:
        if body.tenant_id != core.local_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="tenant scope mismatch"
            )
        try:
            core.database.insert_evidence(body)
        except IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="evidence already exists"
            ) from exc
        _audit(core, "evidence.created", "evidence", str(body.evidence_id))
        return body

    @app.get("/holdings", response_model=list[Holding])
    def list_holdings(core: CoreState = Depends(_require_session)) -> list[Holding]:
        return core.database.list_holdings(core.local_user_id)

    @app.get("/instruments/resolve")
    def resolve_instrument(symbol: str) -> dict[str, str]:
        """E2 · 统一标的身份解析：任意输入形态 → 规范 key/kind/market（无外部调用）。"""
        identity = normalize_instrument(symbol)
        return identity.as_dict()

    @app.get("/instruments/lookup")
    def lookup_instrument(q: str) -> list[dict[str, str]]:
        """M4-E01 · 标的查找：6 位码 → 名称；中文 → 6 位码（覆盖 A 股与场内 ETF）。

        名称源沿用平台已在用的行情供应方（东财搜索建议 / 腾讯 smartbox），不新接主数据。
        非法输入或无命中一律返回空列表（不抛 500）。与 `/instruments/resolve` 一致，不涉用户数据。
        """
        return lookup_instruments(q)

    @app.get("/holdings/by-instrument", response_model=list[Holding])
    def holdings_for_instrument(
        instrument: str, core: CoreState = Depends(_require_session)
    ) -> list[Holding]:
        """E2 · 按规范化标的身份查询持仓（"510300"/"sh510300" 等形态同义命中）。"""
        return core.database.list_holdings_by_instrument(
            core.local_user_id, normalize_instrument(instrument).key
        )

    @app.post("/holdings", response_model=Holding, status_code=status.HTTP_201_CREATED)
    def create_holding(body: HoldingRequest, core: CoreState = Depends(_require_session)) -> Holding:
        if not normalize_instrument(body.instrument).key:
            raise HTTPException(status_code=422, detail="instrument 无法解析为可识别标的")
        existing = core.database.list_holdings_by_instrument(
            core.local_user_id, normalize_instrument(body.instrument).key
        )
        if existing:
            return existing[0]
        holding = Holding(
            holding_id=body.holding_id or uuid4(),
            user_id=core.local_user_id,
            instrument=body.instrument,
            label=body.label,
            status=HoldingStatus(body.status),
            strategy_note=body.strategy_note,
        )
        core.database.insert_holding(holding)
        _audit(core, "holding.created", "holding", str(holding.holding_id))
        return holding

    @app.get("/investor/profile", response_model=InvestorProfile | None)
    def get_investor_profile(core: CoreState = Depends(_require_session)) -> InvestorProfile | None:
        return core.database.get_investor_profile(core.local_user_id)

    @app.put("/investor/profile", response_model=InvestorProfile)
    def upsert_investor_profile(
        body: InvestorProfileRequest, core: CoreState = Depends(_require_session)
    ) -> InvestorProfile:
        existing = core.database.get_investor_profile(core.local_user_id)
        profile = InvestorProfile(
            user_id=core.local_user_id,
            investment_goal=body.investment_goal,
            horizon_years=body.horizon_years,
            liquidity_needs=body.liquidity_needs,
            knowledge_self_assessment=body.knowledge_self_assessment,
            markets_and_assets=body.markets_and_assets,
            consent=body.consent,
            privacy_settings=body.privacy_settings,
            created_at=existing.created_at if existing else datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        core.database.upsert_investor_profile(profile)
        _audit(core, "investor_profile.updated", "investor_profile", str(core.local_user_id))
        return profile

    @app.get("/personal/settings", response_model=PersonalSettings | None)
    def get_personal_settings(core: CoreState = Depends(_require_session)) -> PersonalSettings | None:
        """个人中心设置；从未保存过时返回 `null`（前端回退到本机默认值，便于首次填写）。"""
        return core.database.get_personal_settings(core.local_user_id)

    @app.put("/personal/settings", response_model=PersonalSettings)
    def upsert_personal_settings(
        body: PersonalSettingsRequest, core: CoreState = Depends(_require_session)
    ) -> PersonalSettings:
        existing = core.database.get_personal_settings(core.local_user_id)
        settings = PersonalSettings(
            user_id=core.local_user_id,
            display_name=body.display_name.strip() or "投资人",
            avatar_data=body.avatar_data,
            avatar_color=body.avatar_color,
            default_view=body.default_view,
            notify=PersonalNotifyPrefs(**body.notify.model_dump()),
            risk_profile=body.risk_profile.strip(),
            style_tags=[tag.strip() for tag in body.style_tags if tag.strip()],
            created_at=existing.created_at if existing else datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        core.database.upsert_personal_settings(settings)
        _audit(core, "personal_settings.updated", "personal_settings", str(core.local_user_id))
        return settings

    # —— 存储位置（路线图 3.1 第 3 步）：查看 / 预览 / 迁移 / 校验 / 恢复默认 ——
    # E03（桌面端升级路线图 2026-09-18）：备份列表与从备份恢复——用户不看源码就能回答
    # 「最多能回到哪一天、那一天有哪些表会一起回滚」（整库快照：全部表一起回滚）。
    @app.get("/storage/backups")
    def list_storage_backups(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """备份清单：文件名 / 时间点 / 大小，按时间倒序（最近在前）。"""
        import sqlite3 as _sqlite3

        backup_dir = core.layout.backups
        items: list[dict[str, object]] = []
        for path in sorted(backup_dir.glob("steward-*.sqlite3"), reverse=True):
            try:
                stat = path.stat()
            except OSError:
                continue
            # 备份内关键表行数（只读连接；让「回滚到那一天」可感知）。
            table_counts: dict[str, int | None] = {}
            try:
                raw = _sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
                try:
                    for table in ("ai_research_reports", "holdings", "audit_events"):
                        try:
                            table_counts[table] = int(raw.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                        except _sqlite3.Error:
                            table_counts[table] = None
                finally:
                    raw.close()
            except _sqlite3.Error:
                table_counts = {"ai_research_reports": None, "holdings": None, "audit_events": None}
            items.append({
                "name": path.name,
                "bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                "table_counts": table_counts,
            })
        return {
            "ok": True,
            "directory": str(backup_dir),
            "items": items,
            "rollback_scope": "整库快照：从任一份恢复会回滚全部表（持仓/研报/学习记录/审计等）到该时点",
            "note": "启动时在线轮换备份(rotate_backup),保留最近 7 份;恢复前也会强制产生回退点备份。",
        }

    @app.post("/storage/backups/{name}/restore")
    def restore_storage_backup(name: str, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """从指定备份恢复当前库（E03）。

        安全顺序：① 校验文件名（仅允许 backups 目录内 steward-*.sqlite3，拒绝任何路径穿越）；
        ② 先 rotate_backup 给当前数据留回退点；③ 经 SQLite backup API 在线还原（WAL 安全）；
        ④ 写审计 storage.backup.restored。
        """
        import re as _re
        import sqlite3 as _sqlite3

        if not _re.fullmatch(r"steward-\d{8}-\d{6}(?:-\d+)?\.sqlite3", name):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"备份文件名不合法：{name}")
        backup_path = core.layout.backups / name
        if not backup_path.exists():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"备份不存在：{name}")
        from investment_steward_core.storage.database import rotate_backup

        rollback_point = rotate_backup(core.layout.database_file)
        source = _sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
        try:
            target = _sqlite3.connect(core.layout.database_file)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        _audit(
            core,
            "storage.backup.restored",
            "database",
            name,
            {"backup_path": str(backup_path), "rollback_point": str(rollback_point)},
        )
        return {
            "ok": True,
            "restored_from": name,
            "rollback_point": str(rollback_point),
            "note": "恢复完成。整库已回到该备份时点；刷新应用即可看到恢复后的数据。回退点备份保留在备份目录。",
        }

    @app.get("/storage/layout")
    def get_storage_layout(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """当前目录布局与占用；pointer 指示是否处于用户自选位置。"""
        from investment_steward_core.storage import migration as storage_migration
        from investment_steward_core.storage.paths import load_pointer, pointer_file

        return {
            "version": core.layout.version,
            "user_data": str(core.layout.user_data),
            "cache": str(core.layout.cache),
            "artifacts": str(core.layout.artifacts),
            "install": str(core.layout.install) if core.layout.install else None,
            "database_file": str(core.layout.database_file),
            "backups": str(core.layout.backups),
            "pointer_file": str(pointer_file()),
            "pointer_active": load_pointer() is not None,
            "usage": storage_migration.measure(core.layout),
        }

    @app.get("/storage/lifecycle")
    def get_storage_lifecycle(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """本地数据生命周期（8.6）：存储占用 / 备份检查 / 数据库完整性 / 保留规则，全部本机可复算。"""
        import sqlite3 as _sqlite3

        from investment_steward_core.storage import migration as storage_migration

        backup_dir = core.layout.backups
        backups = sorted(backup_dir.glob("steward-*.sqlite3"))
        backup_items: list[dict[str, object]] = []
        for path in backups[-7:]:
            try:
                stat = path.stat()
                backup_items.append({
                    "file": path.name,
                    "bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                })
            except OSError:
                continue
        integrity_value = "unknown"
        try:
            raw = _sqlite3.connect(f"file:{core.layout.database_file}?mode=ro", uri=True)
            try:
                row = raw.execute("PRAGMA integrity_check").fetchone()
                integrity_value = str(row[0]) if row else "unknown"
            finally:
                raw.close()
        except _sqlite3.Error as error:
            integrity_value = f"检查失败:{error}"
        snap_dir = core.layout.artifacts / "data_snapshots"
        snapshots = sorted(snap_dir.glob("*")) if snap_dir.exists() else []
        return {
            "usage": storage_migration.measure(core.layout),
            "backups": {
                "directory": str(backup_dir),
                "count": len(backups),
                "retention_keep": 7,
                "latest": backup_items[-1] if backup_items else None,
                # E05（2026-09-19 落地）：说明与实现一致——启动 + 每日首次写入轮换，按时间跨度保留。
                "note": "启动时与每日首次写入时在线轮换备份(rotate_backup);保留最近 7 天日份+最近 4 周每周一份+至少最近 7 份;备份目录随用户数据目录走。",
            },
            "integrity": {"result": integrity_value, "method": "PRAGMA integrity_check(只读连接)"},
            "artifacts_breakdown": {"data_snapshots": {"count": len(snapshots), "path": str(snap_dir)}},
            "retention_rules": [
                "备份:保留最近 7 天日份+最近 4 周每周一份+至少最近 7 份(启动时与每日首次写入时轮换;恢复前强制回退点)。",
                "数据快照:内容寻址,同哈希复用,不自动删除。",
                "证据查重清理:POST /evidence/dedup 由用户显式触发,不自动执行。",
                "缓存目录独立于用户数据,不进入备份。",
            ],
        }

    @app.post("/storage/layout/migrate/preview")
    def preview_storage_migrate(
        body: StorageMigrateRequest, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        """迁移预览：源占用、目标剩余空间、可行性结论；不写任何东西。"""
        from investment_steward_core.storage import migration as storage_migration

        return storage_migration.preview(core.layout, body.target_user_data, body.target_artifacts)

    @app.post("/storage/layout/migrate")
    def migrate_storage(
        body: StorageMigrateRequest, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        """执行迁移：临时复制 + 哈希校验 + 配置切换；失败保留原目录。需重启 Core 生效。"""
        from investment_steward_core.storage import migration as storage_migration

        try:
            result = storage_migration.migrate(core.layout, body.target_user_data, body.target_artifacts)
        except storage_migration.MigrationError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{error.code}:{error.detail}") from error
        _audit(core, "storage.migrated", "storage_layout", body.target_user_data)
        return result

    @app.post("/storage/layout/verify")
    def verify_storage(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """再次校验：迁移清单复核哈希 + SQLite quick_check。"""
        from investment_steward_core.storage import migration as storage_migration

        return storage_migration.verify(core.layout)

    @app.post("/storage/layout/reset-default")
    def reset_storage_location(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """恢复默认位置：把数据迁回默认目录并删除位置指针；旧目录保留待用户确认。

        「默认位置」= 宿主兜底目录（桌面壳传的 --default-data-dir，如 Electron userData/data）；
        未提供时才退回编译期默认 %LOCALAPPDATA%\\InvestmentSteward\\data。
        """
        from investment_steward_core.storage import migration as storage_migration
        from investment_steward_core.storage.paths import clear_pointer

        fallback = core.settings.default_data_dir
        default_dir = fallback if fallback is not None else Path(
            os.environ.get("LOCALAPPDATA", ".")
        ) / "InvestmentSteward" / "data"
        if default_dir.resolve() == core.layout.user_data.resolve() and not _pointer_active():
            clear_pointer()
            return {"ok": True, "migrated": False, "restart_required": False, "user_data": str(default_dir)}
        try:
            result = storage_migration.migrate(core.layout, str(default_dir))
        except storage_migration.MigrationError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{error.code}:{error.detail}") from error
        clear_pointer()
        _audit(core, "storage.reset_default", "storage_layout", str(default_dir))
        return {**result, "migrated": True}

    def _pointer_active() -> bool:
        from investment_steward_core.storage.paths import load_pointer

        return load_pointer() is not None

    # —— 本地量化实验（阶段 3 / §7.2）：成本口径回测 + 数据快照 + 结果哈希复现 ——
    def _experiment_score_fn(entry: dict[str, object]) -> tuple[str, Any]:
        """从制品构造确定性打分函数;返回 (配置中冻结的函数名, 函数)。"""
        if entry.get("type") == "model_weights":
            weights = [float(w) for w in entry["weights"]]  # type: ignore[arg-type]
            return "linear_weights", lambda bars: quant_models.score_series(weights, bars)
        tokens = [str(t) for t in entry["formula_tokens"]]  # type: ignore[arg-type]
        return "formula_tokens", lambda bars: quant_factors.evaluate_tokens(tokens, bars)

    @app.post("/quant/experiments")
    def run_quant_experiment(body: ExperimentRunRequest, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """对已发布制品跑实验:数据快照落制品目录,结果哈希登记,同快照同配置幂等。"""
        entry = quant_pool.get_parameter_set(core.database, core.layout, body.artifact_id)
        if entry is None or entry.get("integrity_error"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"制品不存在或已损坏:{body.artifact_id}")
        data_installation = core.database.get_plugin_installation("official.cn-market-data")
        if data_installation is None or data_installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="cn-market-data plugin is not enabled")
        try:
            bars, _provider = fetch_cn_kline(str(entry["symbol"]), limit=250, period="day")
        except FeedError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"行情拉取失败:{error}") from error
        try:
            name, fn = _experiment_score_fn(entry)
            return quant_experiments.run_experiment(
                core.database, core.layout, core.local_user_id, bars,
                symbol=str(entry["symbol"]), score_fn_name=name, score_fn=fn,
                artifact_id=body.artifact_id,
                label=str(entry.get("name", "")),
            )
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    @app.get("/quant/experiments")
    def list_quant_experiments(symbol: str | None = None, core: CoreState = Depends(_require_session)) -> list[dict[str, object]]:
        return core.database.list_quant_experiments(core.local_user_id, symbol)

    @app.post("/quant/experiments/{experiment_id}/reproduce")
    def reproduce_quant_experiment(experiment_id: str, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """复现:用登记的数据快照重跑并逐位比对结果哈希(阶段 3 验收口径)。"""
        record = core.database.get_quant_experiment(experiment_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"实验不存在:{experiment_id}")

        def score_builder(config: dict[str, object]) -> Any:
            entry = quant_pool.get_parameter_set(core.database, core.layout, str(config.get("artifact_id", "")))
            if entry is None or entry.get("integrity_error"):
                return None
            return _experiment_score_fn(entry)[1]

        try:
            return quant_experiments.reproduce_experiment(core.database, core.layout, experiment_id, score_builder)
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error

    def _load_experiment_for_analysis(experiment_id: str, core: CoreState) -> tuple[dict[str, Any], list[dict[str, Any]], Any]:
        """泄漏检查/走查共用:实验记录 + 快照 K 线 + 重建的打分函数。"""
        record = core.database.get_quant_experiment(experiment_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"实验不存在:{experiment_id}")
        config = record["config"]
        entry = quant_pool.get_parameter_set(core.database, core.layout, str(config.get("artifact_id", "")))
        if entry is None or entry.get("integrity_error"):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"打分制品缺失或已损坏:{config.get('artifact_id')}")
        snap = record["data_snapshot"]
        try:
            bars = quant_experiments.load_snapshot(core.layout, snap["content_hash"], snap["file_name"])
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"数据快照读取失败:{error}") from error
        return record, bars, _experiment_score_fn(entry)[1]

    @app.post("/quant/experiments/{experiment_id}/leakage-check")
    def check_experiment_leakage(experiment_id: str, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """未来数据泄漏检查（§7.2）:全量打分 vs 截断打分逐点比对。"""
        record, bars, score_fn = _load_experiment_for_analysis(experiment_id, core)
        try:
            report = quant_experiments.detect_lookahead_bias(score_fn, bars)
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
        return {"experiment_id": experiment_id,
                "result_hash": record["result_hash"],
                "uses_future_data": report["uses_future_data"],
                "detail": report["detail"],
                "checked_points": report["checked_points"],
                "mismatch_count": report["mismatch_count"],
                "mismatches": report["mismatches"]}

    @app.post("/quant/experiments/{experiment_id}/walk-forward")
    def run_experiment_walk_forward(experiment_id: str, body: WalkForwardRequest,
                                    core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """Walk-forward 滚动样本外评估（§7.2）:expanding 切折 + 折间一致性。"""
        record, bars, score_fn = _load_experiment_for_analysis(experiment_id, core)
        config = record["config"]
        values = score_fn(bars)
        closes = [float(bar["close"]) for bar in bars]
        volumes = [float(bar.get("volume") or 0.0) for bar in bars]
        try:
            report = quant_experiments.walk_forward(
                values, closes, folds=body.folds, volumes=volumes,
                price_limit_pct=config.get("price_limit_pct") if isinstance(config.get("price_limit_pct"), (int, float)) else None,
            )
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
        return {"experiment_id": experiment_id,
                "result_hash": record["result_hash"],
                "symbol": config.get("symbol"),
                **report}

    @app.post("/quant/portfolio/backtest")
    def run_portfolio_backtest(body: PortfolioBacktestRequest, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """组合回测（§7.2）:权重/行业上限 + 组合级风险指标,纯确定性计算。"""
        try:
            return quant_portfolio.run_portfolio_backtest(
                {s: [float(x) for x in rs] for s, rs in body.symbol_returns.items()},
                {s: float(w) for s, w in body.weights.items()},
                industries=body.industries,
                max_weight=body.max_weight,
                max_industry_weight=body.max_industry_weight,
                result_hash=True,
            )
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    # —— 决策时间线（8.1） ——
    @app.get("/timeline/decision/{decision_id}")
    def decision_timeline(decision_id: UUID, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        timeline = longterm_service.build_decision_timeline(core.database, core.local_user_id, decision_id)
        if timeline is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"决定不存在:{decision_id}")
        return timeline

    # —— 可检验判断（8.2）：原判断冻结、验证只追加 ——
    @app.post("/judgments", status_code=status.HTTP_201_CREATED)
    def create_judgment(body: JudgmentCreateRequest, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        judgment = longterm_service.create_judgment(
            core.database,
            user_id=core.local_user_id,
            subject=body.subject.strip(),
            direction=body.direction.strip(),
            statement=body.statement,
            due_at=datetime.fromisoformat(body.due_at).replace(tzinfo=UTC),
            trigger_conditions=[c.strip() for c in body.trigger_conditions if c.strip()],
            invalidation_conditions=[c.strip() for c in body.invalidation_conditions if c.strip()],
            source_run_id=body.source_run_id,
        )
        _audit(core, "judgment.created", "judgment", str(judgment.judgment_id))
        return judgment.model_dump(mode="json")

    @app.get("/judgments")
    def list_judgments(status_filter: str | None = None, core: CoreState = Depends(_require_session)) -> list[dict[str, object]]:
        if status_filter is not None and status_filter not in {"pending", "verified", "refuted", "insufficient_data"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"非法状态:{status_filter}")
        items = core.database.list_judgments(core.local_user_id, status_filter)
        return [item.model_dump(mode="json") for item in items]

    # G02（桌面端升级路线图 2026-09-18）：后验命中率。
    # 注：**必须注册在 `/judgments/{judgment_id}` 之前**——参数化路由先注册会把
    # `/judgments/hit-rate` 当成 judgment_id="hit-rate" 并因 UUID 校验失败返回 422（项目铁律：
    # 精确路由必须先于参数化路由注册）。排序守护见 tests/test_desktop_g02_hit_rate.py。
    @app.get("/judgments/hit-rate")
    def judgment_hit_rate(window: str = "90d", core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """判断后验命中率：按「模型方案 × 主题 × 时间窗」分桶，每桶自带成员判定与回填记录（G02）。

        口径全部随返回体下发（`policy`），逐个数字可复算：
        - 分子 = 最近回填为「成立」的判定；分母 = 成立 + 失效（结论明确的已回填判定）；
        - 「无数据」与尚未回填的判定**不进分母**，数量仍如实展示；
        - 分母为 0 时 `hit_rate` 为 **null**（样本不足，不显示 0%）；
        - 时间窗按判定**到期时间**筛选，含今天、不含未到期（未到期单列 `not_due_excluded`）。

        本端点是**只读**的：命中率与「是否调整投资原则」之间没有任何自动通道，
        原则变更仍须走 `POST /investment-policies` 的提议 + 确认链。
        """
        windows = {"30d": 30, "90d": 90, "180d": 180, "all": None}
        if window not in windows:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"非法时间窗:{window}（可选 {'/'.join(windows)}）",
            )
        return longterm_service.judgment_hit_rate(
            core.database, core.local_user_id, window_days=windows[window],
        )

    @app.get("/judgments/{judgment_id}")
    def get_judgment(judgment_id: UUID, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        judgment = core.database.get_judgment(judgment_id, core.local_user_id)
        if judgment is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"判断不存在:{judgment_id}")
        verifications = core.database.list_judgment_verifications(judgment_id)
        return {
            **judgment.model_dump(mode="json"),
            "verifications": [v.model_dump(mode="json") for v in verifications],
        }

    @app.post("/judgments/{judgment_id}/verify")
    def verify_judgment(judgment_id: UUID, body: JudgmentVerifyRequest, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        try:
            judgment, verification = longterm_service.verify_judgment(
                core.database, core.local_user_id, judgment_id,
                result=body.result, outcome=body.outcome, metrics=body.metrics,
            )
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        _audit(core, "judgment.verified", "judgment", str(judgment_id))
        return {"judgment": judgment.model_dump(mode="json"), "verification": verification.model_dump(mode="json")}

    # —— 观察事项（8.3）：状态机 + 检查只追加 + dedup 防重复 ——
    @app.post("/watch-items", status_code=status.HTTP_201_CREATED)
    def create_watch_item(body: WatchItemCreateRequest, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        item = longterm_service.create_watch_item(
            core.database,
            user_id=core.local_user_id,
            title=body.title.strip(),
            indicator=body.indicator.strip(),
            condition_text=body.condition_text.strip(),
            check_cycle=body.check_cycle,
            source_kind=body.source_kind.strip(),
            source_id=body.source_id,
            dedup_key=body.dedup_key.strip(),
        )
        _audit(core, "watch_item.created", "watch_item", str(item.watch_id))
        return item.model_dump(mode="json")

    @app.get("/watch-items")
    def list_watch_items(status_filter: str | None = None, core: CoreState = Depends(_require_session)) -> list[dict[str, object]]:
        if status_filter is not None and status_filter not in {"active", "paused", "triggered", "closed"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"非法状态:{status_filter}")
        items = core.database.list_watch_items(core.local_user_id, status_filter)
        return [item.model_dump(mode="json") for item in items]

    @app.post("/watch-items/{watch_id}/checks")
    def record_watch_check(watch_id: UUID, body: WatchCheckRequest, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        try:
            item, check, written = longterm_service.record_watch_check(
                core.database, core.local_user_id, watch_id,
                observed=body.observed, triggered=body.triggered,
                note=body.note, evidence_refs=body.evidence_refs,
                dedup_value=body.dedup_value,
            )
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        return {
            "watch_item": item.model_dump(mode="json"),
            "check": check.model_dump(mode="json"),
            "written": written,
        }

    @app.get("/watch-items/{watch_id}/checks")
    def list_watch_checks(watch_id: UUID, core: CoreState = Depends(_require_session)) -> list[dict[str, object]]:
        if core.database.get_watch_item(watch_id, core.local_user_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"观察事项不存在:{watch_id}")
        return [c.model_dump(mode="json") for c in core.database.list_watch_checks(watch_id)]

    @app.post("/watch-items/{watch_id}/transition")
    def transition_watch_item(watch_id: UUID, body: WatchTransitionRequest, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        try:
            item = longterm_service.transition_watch_item(core.database, core.local_user_id, watch_id, body.target)
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        _audit(core, "watch_item.transition", "watch_item", str(watch_id))
        return item.model_dump(mode="json")

    # —— 阶段 4：同步客户端（st.18257.xyz relay） ——

    @app.post("/sync/config")
    def configure_sync(body: SyncConfigRequest, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """配置中转通道:token/同步密钥入本机凭据库(不回显),其余入 sync_state。"""
        store = resolve_store(core.database)
        store.store(sync_client.TOKEN_CREDENTIAL, body.user_token)
        sync_key = body.sync_key or sync_client.generate_sync_key()
        created_key = body.sync_key is None
        store.store(sync_client.SYNC_KEY_CREDENTIAL, sync_key)
        core.database.set_sync_state("relay_url", body.relay_url.rstrip("/"))
        core.database.set_sync_state("device_id", body.device_id)
        core.database.set_sync_state("user_id", body.user_id)
        _audit(core, "sync.config", "sync", body.device_id)
        return {
            "ok": True,
            "relay_url": body.relay_url.rstrip("/"),
            "device_id": body.device_id,
            "user_id": body.user_id,
            "sync_key_generated": created_key,
            "note": "user_token 与同步密钥只存本机凭据库,响应永不回显;换设备时导出 sync_key 手动导入。",
        }

    @app.get("/sync/config/reveal")
    def reveal_sync_config(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """多设备接入（§5）:回显本机同步凭据供用户手动复制到新设备。

        仅本机会话鉴权可读;响应含 user_token 与同步密钥,审计留痕。
        """
        store = resolve_store(core.database)
        token = store.get(sync_client.TOKEN_CREDENTIAL)
        sync_key = store.get(sync_client.SYNC_KEY_CREDENTIAL)
        relay_url = core.database.get_sync_state("relay_url")
        if not (token and sync_key and relay_url):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="sync 未配置:先 POST /sync/config")
        _audit(core, "sync.config_revealed", "sync", str(core.database.get_sync_state("device_id")))
        return {
            "relay_url": relay_url,
            "user_id": core.database.get_sync_state("user_id"),
            "device_id": core.database.get_sync_state("device_id"),
            "user_token": token,
            "sync_key": sync_key,
            "note": "仅本机展示;手机端粘贴 user_token+sync_key 完成接入,勿经不可信渠道传输。",
        }

    @app.post("/sync/pairing-code")
    def create_sync_pairing_code(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """生成 6 位手机配对码（§5 设备与同步管理）：手机只输码即完成接入，免手工抄密钥。

        服务器暂存 user_token+sync_key（默认 10 分钟，TTL 内一次性领取，领取即删）。
        B04：此处原有两份同 method+path 的注册，FastAPI 只认先注册的那份，现已合并为一份
        （保留后一份的 `ttl_seconds` 回传与 `except Exception` 兜底包装）。
        """
        store = resolve_store(core.database)
        token = store.get(sync_client.TOKEN_CREDENTIAL)
        sync_key = store.get(sync_client.SYNC_KEY_CREDENTIAL)
        relay_url = core.database.get_sync_state("relay_url")
        device_id = core.database.get_sync_state("device_id")
        if not (token and sync_key and relay_url):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="sync 未配置:先 POST /sync/config")
        config = sync_client.RelayConfig(base_url=relay_url, user_token=token, device_id=device_id or "")
        try:
            info = sync_client.RelayClient(config).create_pairing_code(sync_key)
        except RelayHTTPError as error:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                                detail=f"中转服务不可达:{error}") from error
        except Exception as error:  # noqa: BLE001 - relay 契约外的异常一律如实 502，不透传堆栈
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                                detail=f"配对码生成失败:{error}") from error
        _audit(core, "sync.pairing_code_created", "sync", str(info.get("code")))
        return {"ok": True, "code": info.get("code"), "expires_at": info.get("expires_at"),
                "ttl_seconds": info.get("ttl_seconds", 600),
                "note": "配对码 10 分钟内有效且只能用一次;手机端 设置 → 输入配对码 即完成接入。"}

    @app.get("/sync/status")
    def sync_status(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        store = resolve_store(core.database)
        token = store.get(sync_client.TOKEN_CREDENTIAL)
        sync_key = store.get(sync_client.SYNC_KEY_CREDENTIAL)
        outbox = core.database.sync_outbox_counts()
        return {
            "configured": bool(token and sync_key),
            "relay_url": core.database.get_sync_state("relay_url"),
            "device_id": core.database.get_sync_state("device_id"),
            "user_id": core.database.get_sync_state("user_id"),
            "has_token": token is not None,
            "has_sync_key": sync_key is not None,
            "pull_cursor": int(core.database.get_sync_state("pull_cursor") or 0),
            "outbox": outbox,
            "inbox_count": core.database.sync_inbox_count(),
        }

    @app.post("/sync/run")
    def run_sync_now(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """跑一轮同步:白名单构建 → 加密 outbox → 上传 → 拉取解密 → 游标推进。"""
        store = resolve_store(core.database)
        token = store.get(sync_client.TOKEN_CREDENTIAL)
        sync_key = store.get(sync_client.SYNC_KEY_CREDENTIAL)
        relay_url = core.database.get_sync_state("relay_url")
        device_id = core.database.get_sync_state("device_id")
        if not (token and sync_key and relay_url and device_id):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="sync 未配置:先 POST /sync/config(relay_url/user_token/device_id/user_id)")
        config = sync_client.RelayConfig(base_url=relay_url, user_token=token, device_id=device_id)
        try:
            return sync_client.run_sync(core.database, store, config, sync_key, core.local_user_id)
        except RelayHTTPError as error:
            return {"ok": False, "error": f"中转服务不可达:{error}", "ran_at": datetime.now(UTC).isoformat()}

    @app.get("/sync/inbox")
    def sync_inbox(limit: int = 100, core: CoreState = Depends(_require_session)) -> list[dict[str, object]]:
        """拉取到的远端事件(已解密)。上限 500;应用到业务表是后续工作包。"""
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="limit 1..500")
        return core.database.list_sync_inbox(limit=limit)

    @app.post("/sync/apply")
    def apply_sync_inbox(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """把 inbox 远端事件幂等应用到本机业务表(观察事项/通知;计划/研究摘要如实跳过)。"""
        return sync_client.apply_inbox(core.database, core.local_user_id)

    # —— 阶段 6：GitHub 插件市场（relay 目录缓存 + 本地验签，§6） ——

    def _market_client(core: CoreState) -> sync_client.MarketClient:
        store = resolve_store(core.database)
        token = store.get(sync_client.TOKEN_CREDENTIAL)
        relay_url = core.database.get_sync_state("relay_url")
        device_id = core.database.get_sync_state("device_id")
        if not (token and relay_url and device_id):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="sync 未配置:先 POST /sync/config")
        return sync_client.MarketClient(sync_client.RelayConfig(
            base_url=relay_url, user_token=token, device_id=device_id
        ))

    # 插件 SDK 协议版本（与宿主版本号解耦）：manifest 的 sdk_min/sdk_max 约束的是
    # 本宿主实现的插件协议版本（存量 manifest 惯例 1.0..1.x），不是宿主自身版本。
    SDK_PROTOCOL_VERSION = "1.0"

    def _sdk_compatible(host_version: str, sdk_min: str, sdk_max: str) -> bool:
        """宽松语义版本兼容:下界 host >= sdk_min;上界 sdk_max 通配 '.x' 表示该 major 及以下均可
        (与注册表存量 manifest 的 '1.x' 惯例一致,host 0.x 视为低于上界,兼容)。"""

        def parts(value: str) -> tuple[int, int]:
            chunks = str(value).split(".")[:2]
            major = int(chunks[0]) if chunks and chunks[0].isdigit() else 0
            minor = int(chunks[1]) if len(chunks) > 1 and chunks[1].isdigit() else 0
            return major, minor

        host = parts(host_version)
        if host < parts(sdk_min):
            return False
        if str(sdk_max).endswith(".x"):
            max_major = str(sdk_max).split(".")[0]
            return (not max_major.isdigit()) or host[0] <= int(max_major)
        return host <= parts(sdk_max)

    def _market_entry_verdict(entry: dict[str, object], core: CoreState) -> dict[str, object]:
        """对目录中单条发布做本地逐项校验:签名/schema/SDK/撤回/本机注册表就位。"""
        manifest_raw = entry.get("manifest") or {}
        verdict: dict[str, object] = {
            "plugin_id": entry.get("plugin_id"),
            "release_version": entry.get("release_version"),
            "revoked": bool(entry.get("revoked")),
            "prerelease": bool(entry.get("prerelease")),
            "signature_verified": False,
            "schema_compatible": False,
            "sdk_compatible": False,
            "installed_version": None,
            "in_local_registry": False,
        }
        try:
            model = PluginManifest.model_validate(manifest_raw)
        except ValueError as error:
            verdict["verdict"] = f"manifest 无效:{error}"
            return verdict
        existing = core.database.get_plugin_installation(model.plugin_id)
        verdict["installed_version"] = existing.release_version if existing else None
        verdict["in_local_registry"] = any(
            m.plugin_id == model.plugin_id and m.release_version == model.release_version
            for _, m in _registry_entries()
        )
        reasons: list[str] = []
        try:
            verify_manifest_integrity(manifest_raw, _pinned_public_key_pem(core.settings))
            verdict["signature_verified"] = True
        except ValueError as error:
            reasons.append(f"验签失败:{error}")
        try:
            _schema_support_check(model)
            verdict["schema_compatible"] = True
        except ValueError as error:
            reasons.append(str(error))
        verdict["sdk_compatible"] = _sdk_compatible(SDK_PROTOCOL_VERSION, model.sdk_min, model.sdk_max)
        if not verdict["sdk_compatible"]:
            reasons.append(f"SDK 协议不兼容:要求 {model.sdk_min}..{model.sdk_max},本机协议 {SDK_PROTOCOL_VERSION}")
        if verdict["revoked"]:
            reasons.append("发布者已撤回(REVOKE)")
        if reasons:
            verdict["verdict"] = "不可安装:" + ";".join(reasons)
        else:
            verdict["verdict"] = "可安装" if verdict["in_local_registry"] else "验签通过,但代码包未随本机注册表就位"
        return verdict

    @app.get("/market/catalog")
    def market_catalog(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """经 relay 拉 GitHub Releases 目录(仅元数据),本地逐条验签并给出判定。"""
        client = _market_client(core)
        try:
            catalog = client.catalog()
        except Exception as error:  # relay 不可达 → 如实报告,不伪造目录
            return {"ok": False, "error": f"市场目录不可达:{error}", "entries": []}
        return {
            "ok": True,
            "source": catalog.get("source"),
            "stale": catalog.get("stale"),
            "note": catalog.get("note"),
            "host_version": __version__,
            "entries": [_market_entry_verdict(entry, core) for entry in catalog.get("releases", [])],
        }

    @app.post("/market/install", response_model=PluginInstallation)
    def market_install(body: MarketInstallRequest,
                       core: CoreState = Depends(_require_session)) -> PluginInstallation:
        """市场安装:目录验签(签名/撤回/schema/SDK) → 本机注册表同版本代码包核对 → 复用既有安装语义。

        代码包不经中转复制:GitHub 目录只提供发现与验签,实际代码必须已在签名注册表目录中。
        """
        client = _market_client(core)
        try:
            catalog = client.catalog()
        except Exception as error:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                                detail=f"市场目录不可达:{error}") from error
        candidates = [e for e in catalog.get("releases", []) if e.get("plugin_id") == body.plugin_id]
        if body.version:
            candidates = [e for e in candidates if e.get("release_version") == body.version]
        if not candidates:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="目录中无该插件/版本")
        entry = candidates[0]
        manifest_raw = entry.get("manifest") or {}
        try:
            model = PluginManifest.model_validate(manifest_raw)
            verify_manifest_integrity(manifest_raw, _pinned_public_key_pem(core.settings))
            _schema_support_check(model)
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                                detail=f"市场验签不通过:{error}") from error
        if entry.get("revoked"):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该发布已被发布者撤回(REVOKE),拒绝安装")
        if not _sdk_compatible(SDK_PROTOCOL_VERSION, model.sdk_min, model.sdk_max):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail=f"SDK 协议不兼容:插件要求 {model.sdk_min}..{model.sdk_max},本机协议 {SDK_PROTOCOL_VERSION}")
        try:
            registry_manifest = _verified_registry_manifest_version(
                model.plugin_id, model.release_version, core.settings)
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail=f"代码包未随本机注册表就位({error});市场目录仅提供验签与版本发现,"
                                       "请先将发布包放入注册表目录") from error
        if registry_manifest.artifact_sha256 != model.artifact_sha256:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                                detail="目录 manifest 与本机代码包 artifact_sha256 不一致,拒绝安装")
        # —— 与 /plugins/{id}/install 相同的安装语义 ——
        existing = core.database.get_plugin_installation(model.plugin_id)
        if model.mount == MOUNT_OWN_PAGE:
            occupied = len(_enabled_own_page_plugins(core.database.list_plugin_installations()))
            if occupied >= core.settings.app_library_cap:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="应用区已满，请先移除一个")
        if existing is not None and existing.release_version != model.release_version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"已安装 {existing.release_version}，不能直接安装 {model.release_version}；请先走更新事务或停用/撤销后处理。",
            )
        installation = PluginInstallation(
            plugin_id=model.plugin_id,
            release_version=model.release_version,
            state=PluginInstallationState.ENABLED,
            granted_capabilities=model.capabilities,
            artifact_sha256=model.artifact_sha256,
        )
        core.database.upsert_plugin_installation(installation)
        _audit(core, "plugin.installed", "plugin", model.plugin_id)
        return installation

    # —— 阶段 5：量化参数申请制中转（§7.1） ——

    def _transfer_client(core: CoreState) -> tuple[sync_client.TransferClient, str]:
        store = resolve_store(core.database)
        token = store.get(sync_client.TOKEN_CREDENTIAL)
        relay_url = core.database.get_sync_state("relay_url")
        device_id = core.database.get_sync_state("device_id")
        if not (token and relay_url and device_id):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="sync 未配置:先 POST /sync/config")
        return sync_client.TransferClient(sync_client.RelayConfig(
            base_url=relay_url, user_token=token, device_id=device_id
        )), (store.get(sync_client.SYNC_KEY_CREDENTIAL) or "")

    @app.post("/transfer/requests")
    def create_transfer_request(body: TransferRequestCreate,
                                core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """接收方向中转登记申请(类型白名单);未获显式同意前无人可上传。"""
        client, _ = _transfer_client(core)
        try:
            return client.create_request(body.artifact_type, body.note)
        except RelayHTTPError as error:
            return {"ok": False, "error": f"中转服务不可达:{error}"}

    @app.get("/transfer/requests")
    def list_transfer_requests(core: CoreState = Depends(_require_session)) -> list[dict[str, object]]:
        client, _ = _transfer_client(core)
        return client.list_requests()

    @app.post("/transfer/requests/{request_id}/decide")
    def decide_transfer(request_id: str, body: TransferDecisionRequest,
                        core: CoreState = Depends(_require_session)) -> dict[str, object]:
        client, _ = _transfer_client(core)
        try:
            return client.decide(request_id, body.decision)
        except RelayHTTPError as error:
            return {"ok": False, "error": f"中转服务拒绝:{error}"}

    @app.post("/transfer/requests/{request_id}/send")
    def send_transfer(request_id: str, body: TransferSendRequest,
                      core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """对已批准申请封包上传:本地加密 + HMAC 签名;完成后登记 transfer.announced 公告事件。"""
        if body.artifact_type not in sync_client.ALLOWED_ARTIFACT_TYPES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail=f"artifact_type 必须是 {list(sync_client.ALLOWED_ARTIFACT_TYPES)} 之一")
        client, sync_key = _transfer_client(core)
        if not sync_key:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="同步密钥缺失(先 /sync/config)")
        envelope = sync_client.seal_transfer(sync_key, artifact_type=body.artifact_type,
                                             version=body.version, payload=body.payload)
        try:
            result = client.send_approved(request_id, envelope)
        except RelayHTTPError as error:
            return {"ok": False, "error": f"上传被拒:{error}"}
        # 公告事件(加密):接收方下一轮 /sync/run 拉取后即可自动获得验签元数据
        core.database.enqueue_sync_event(
            "transfer.announced", f"transfer:{request_id}",
            sync_client.seal_payload(sync_key, {
                "request_id": request_id, "artifact_type": body.artifact_type,
                "version": body.version, "sha256": envelope["sha256"],
                "signature": envelope["signature"],
                "snapshot_hash": body.snapshot_hash or "",
            }),
        )
        return {"ok": True, "request_id": request_id, **result,
                "sha256": envelope["sha256"], "signature": envelope["signature"],
                "note": "公告事件已入 outbox,下一轮 /sync/run 上传;接收方 receive 将自动核对元数据。"}

    @app.post("/transfer/requests/{request_id}/receive")
    def receive_transfer(request_id: str, body: TransferReceiveRequest,
                         core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """领取一次性令牌下载密文 → 重算哈希 + 验 HMAC 签名 + 解密;元数据优先取公告事件。"""
        client, sync_key = _transfer_client(core)
        if not sync_key:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="同步密钥缺失(先 /sync/config)")
        meta = {k: getattr(body, k) for k in ("artifact_type", "version", "sha256", "signature", "snapshot_hash")}
        if not meta["sha256"] or not meta["signature"]:
            announced = None
            for event in core.database.list_sync_inbox(limit=500):
                if event["kind"] != "transfer.announced":
                    continue
                try:
                    payload = sync_client.open_payload(sync_key, event["payload"])
                except ValueError:
                    continue
                if payload.get("request_id") == request_id:
                    announced = payload
                    break
            if announced is None:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                    detail="收件箱中无该申请的公告事件;请先跑 /sync/run 或在请求中显式提供元数据")
            meta = {k: announced.get(k) for k in ("artifact_type", "version", "sha256", "signature", "snapshot_hash")}
        try:
            blob = client.receive_ready(request_id)
            opened = sync_client.open_transfer(sync_key, {
                "artifact_type": meta["artifact_type"], "version": int(meta["version"] or 1),
                "ciphertext": blob["ciphertext"], "sha256": meta["sha256"], "signature": meta["signature"],
            })
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
        except RelayHTTPError as error:
            return {"ok": False, "error": f"下载失败:{error}"}
        snapshot_note = str(meta.get("snapshot_hash") or "")
        return {
            "ok": True,
            "artifact_type": opened["artifact_type"],
            "version": opened["version"],
            "sha256": opened["sha256"],
            "signature_verified": True,
            "snapshot_hash": snapshot_note or None,
            "snapshot_note": (f"数据快照口径以发送方 metadata 为准:{snapshot_note}" if snapshot_note
                              else "发送方未提供快照口径,如实标注缺失"),
            "payload": opened["payload"],
            "note": "明文仅回传本机会话;导入业务表(参数集/策略包)是显式的后续操作。",
        }

    # —— 组合风险视图（8.4） ——
    @app.get("/portfolio/risk")
    def portfolio_risk(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        return longterm_service.build_portfolio_risk(core.database, core.local_user_id)

    # —— 研究快照与模板（8.4/8.6） ——
    @app.post("/research/snapshots", status_code=status.HTTP_201_CREATED)
    def create_research_snapshot(body: ResearchSnapshotCreateRequest, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        snapshot = ResearchSnapshotModel(
            snapshot_id=uuid4(),
            user_id=core.local_user_id,
            title=body.title.strip(),
            question=body.question,
            symbols=[s.strip() for s in body.symbols if s.strip()],
            evidence_refs=body.evidence_refs,
            thesis_ids=body.thesis_ids,
            model_profile_id=body.model_profile_id,
            params=body.params,
            output_validation=body.output_validation,
        )
        core.database.insert_research_snapshot(snapshot)
        _audit(core, "research_snapshot.created", "research_snapshot", str(snapshot.snapshot_id))
        return snapshot.model_dump(mode="json")

    @app.get("/research/snapshots")
    def list_research_snapshots(core: CoreState = Depends(_require_session)) -> list[dict[str, object]]:
        return [s.model_dump(mode="json") for s in core.database.list_research_snapshots(core.local_user_id)]

    @app.get("/research/snapshots/{snapshot_id}")
    def get_research_snapshot(snapshot_id: UUID, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """恢复=只读回看:返回冻结内容 + 各引用现存性标注;绝不自动替换当前数据或逻辑。"""
        snapshot = core.database.get_research_snapshot(snapshot_id, core.local_user_id)
        if snapshot is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"快照不存在:{snapshot_id}")
        existing_evidence = {str(e.evidence_id) for e in core.database.list_evidence(core.local_user_id)}
        existing_theses = {str(t.thesis_id) for t in core.database.list_theses(core.local_user_id)}
        return {
            **snapshot.model_dump(mode="json"),
            "restore": {
                "mode": "readonly_view",
                "evidence_still_present": [str(r) for r in snapshot.evidence_refs if str(r) in existing_evidence],
                "evidence_missing": [str(r) for r in snapshot.evidence_refs if str(r) not in existing_evidence],
                "thesis_still_present": [str(r) for r in snapshot.thesis_ids if str(r) in existing_theses],
                "thesis_missing": [str(r) for r in snapshot.thesis_ids if str(r) not in existing_theses],
                "rule": "只读回看当时输入范围;缺失引用如实标注,不替换当前数据。",
            },
        }

    @app.get("/research/templates")
    def list_research_templates(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        builtin = [{**t, "template_id": f"builtin-{index}", "user_id": None}
                   for index, t in enumerate(longterm_service.builtin_research_templates(), start=1)]
        user_items = [t.model_dump(mode="json") for t in core.database.list_research_templates(core.local_user_id)]
        return {"builtin": builtin, "user": user_items}

    @app.post("/research/templates", status_code=status.HTTP_201_CREATED)
    def create_research_template(body: ResearchTemplateCreateRequest, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        template = ResearchTemplateModel(
            template_id=uuid4(),
            user_id=core.local_user_id,
            name=body.name.strip(),
            description=body.description,
            required_context=[c.strip() for c in body.required_context if c.strip()],
            evidence_types=[e.strip() for e in body.evidence_types if e.strip()],
            default_params=body.default_params,
        )
        core.database.upsert_research_template(template)
        return template.model_dump(mode="json")

    @app.delete("/research/templates/{template_id}")
    def delete_research_template(template_id: UUID, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        if not core.database.delete_research_template(template_id, core.local_user_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"模板不存在:{template_id}")
        return {"deleted": True}

    # —— 通知分级与合并（8.5） ——
    @app.get("/notifications/graded")
    def notifications_graded(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        return longterm_service.grade_and_merge_notifications(core.database.list_notifications(core.local_user_id))

    # —— 数据源质量（8.5 + F02/F03） ——
    @app.get("/data-source/quality")
    def data_source_quality(
        window: str = "7d", core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        """数据源质量（F02）：证据存量 + 采集尝试日志合并。

        成功率/延迟来自 feed_attempts 的窗口聚合；**样本为 0 时一律 None**（「没有样本」
        与「全都失败」是两件事）。window 仅支持 7d / 30d，与 /model-usage 同口径。
        """
        windows = {"7d": 7, "30d": 30}
        if window not in windows:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="window 仅支持 7d | 30d")
        return longterm_service.data_source_quality(core.database, core.local_user_id, windows[window])

    @app.post("/data-source/retry")
    def data_source_retry(
        body: DataSourceRetryRequest, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        """「仅重试失败项」（F03）：对指定来源（默认=窗口内失败过的来源）各发一次最小真实请求。

        重试本身经 F02 埋点落 `feed_attempts`——面板刷新后就能看到这次重试是成功还是又失败，
        不需要第二套记录。逐源报告，一个源失败不影响其余源。
        """
        windows = {"7d": 7, "30d": 30}
        if body.window not in windows:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="window 仅支持 7d | 30d")
        if body.sources:
            unknown = [name for name in body.sources if name not in feed_health.ALL_SOURCES]
            if unknown:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"未登记的数据源：{unknown}（可选：{list(feed_health.ALL_SOURCES)}）",
                )
            targets = list(dict.fromkeys(body.sources))
        else:
            if not body.only_failed:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="需给出 sources 或把 only_failed 置为 true（不提供「无差别全量重试」）",
                )
            targets = core.database.list_failed_feed_sources(windows[body.window])
        if not targets:
            return {"ok": True, "attempted": 0, "results": [], "note": "窗口内没有失败过的来源，无重试动作。"}
        if len(targets) > 20:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="一次最多重试 20 个来源")
        results = feed_probe.probe_sources(targets, resolve_store(core.database))
        _audit(core, "data_source.retry", "feed", ",".join(targets))
        return {
            "ok": True,
            "attempted": len(results),
            "results": results,
            "note": (
                "每个来源发一次最小真实请求（绕过模块缓存），结果已落取数日志；"
                "supported=false 表示该来源需要交易日等外部输入，不在此盲目重试。"
            ),
        }

    @app.get("/data-source/failures")
    def data_source_failures(
        window: str = "7d", limit: int = 50, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        """取数失败清单（F03）：逐条给出「哪个源、第几次、失败原因、何时、耗时多久」。

        这是「今日取数健康」面板的原始事实源；是否可重试由**调用方按端点语义**决定，
        不在这里猜——故返回里带 endpoint 与 params_fingerprint 供人工/后续重试逻辑使用。
        """
        windows = {"7d": 7, "30d": 30}
        if window not in windows:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="window 仅支持 7d | 30d")
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="limit 需在 1..500")
        failures = core.database.list_feed_failures(windows[window], limit)
        return {
            "ok": True,
            "window": window,
            "count": len(failures),
            "failures": failures,
            "rule": "逐条为一次真实 HTTP 尝试（含重试的第几次）；错误类别按异常类型归类，不解析供应商文案。",
        }

    # —— 「不行动」原因统计（8.6） ——
    @app.get("/decisions/inaction-stats")
    def decisions_inaction_stats(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        return longterm_service.inaction_stats(core.database.list_decisions(core.local_user_id))

    @app.delete("/holdings/{holding_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_holding(
        holding_id: UUID,
        note: str | None = None,
        core: CoreState = Depends(_require_session),
    ) -> None:
        """M5-F01/F02/F03 · 移除持仓或自选。

        `note` 为**选填**（自选移除不要求填原因；持仓移除保留说明框但同样不强制）。
        用户没写说明时，审计 payload 用固定句兜底——不写空原因，避免审计记录出现无内容的确认。
        """
        existing = core.database.get_holding(holding_id, core.local_user_id)
        fallback = "用户确认移除自选" if existing is not None and existing.status == HoldingStatus.WATCHLIST else "用户确认移除持仓"
        # F02 实现口径：自选与持仓**两条路径都不强制原因**（用户在任务记录里拍板「持仓选填」）。
        # 唯一的差别只体现在审计兜底句上。
        core.database.delete_holding(holding_id, core.local_user_id)
        _audit(
            core,
            "holding.deleted",
            "holding",
            str(holding_id),
            {
                "summary": (note or "").strip() or fallback,
                "note_provided": bool((note or "").strip()),
                "kind": existing.status.value if existing is not None else "unknown",
                "instrument": existing.instrument if existing is not None else None,
                "label": existing.label if existing is not None else None,
            },
        )

    @app.get("/thesis", response_model=list[Thesis])
    def list_theses(core: CoreState = Depends(_require_session)) -> list[Thesis]:
        return core.database.list_theses(core.local_user_id)

    @app.post("/thesis", response_model=Thesis, status_code=status.HTTP_201_CREATED)
    def create_thesis(body: ThesisRequest, core: CoreState = Depends(_require_session)) -> Thesis:
        existing = core.database.get_thesis(body.thesis_id, core.local_user_id) if body.thesis_id else None
        thesis = Thesis(
            thesis_id=existing.thesis_id if existing else (body.thesis_id or uuid4()),
            user_id=core.local_user_id,
            instrument=body.instrument,
            original_statement=body.original_statement,
            core_assumptions=body.core_assumptions,
            supporting_conditions=body.supporting_conditions,
            invalidation_conditions=body.invalidation_conditions,
            observation_metrics=body.observation_metrics,
            status=ThesisStatus(body.status),
            created_at=existing.created_at if existing else datetime.now(UTC),
        )
        core.database.upsert_thesis(thesis)
        _audit(core, "thesis.updated" if existing else "thesis.created", "thesis", str(thesis.thesis_id))
        return thesis

    @app.put("/thesis/{thesis_id}", response_model=Thesis)
    def update_thesis(
        thesis_id: UUID, body: ThesisRequest, core: CoreState = Depends(_require_session)
    ) -> Thesis:
        existing = core.database.get_thesis(thesis_id, core.local_user_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="thesis not found")
        if not normalize_instrument(body.instrument).key:
            raise HTTPException(status_code=422, detail="instrument 无法解析为可识别标的")
        thesis = existing.model_copy(
            update={
                "instrument": body.instrument,
                "original_statement": body.original_statement,
                "core_assumptions": body.core_assumptions,
                "supporting_conditions": body.supporting_conditions,
                "invalidation_conditions": body.invalidation_conditions,
                "observation_metrics": body.observation_metrics,
                "status": ThesisStatus(body.status),
                "updated_at": datetime.now(UTC),
            }
        )
        core.database.upsert_thesis(thesis)
        _audit(core, "thesis.updated", "thesis", str(thesis_id))
        return thesis

    @app.get("/plans", response_model=list[Plan])
    def list_plans(core: CoreState = Depends(_require_session)) -> list[Plan]:
        return core.database.list_plans(core.local_user_id)

    @app.post("/plans", response_model=Plan, status_code=status.HTTP_201_CREATED)
    def create_plan(body: PlanRequest, core: CoreState = Depends(_require_session)) -> Plan:
        plan = Plan(
            plan_id=uuid4(),
            user_id=core.local_user_id,
            title=body.title,
            status=PlanStatus(body.status),
            decision_id=UUID(body.decision_id) if body.decision_id else None,
        )
        core.database.insert_plan(plan)
        _audit(core, "plan.created", "plan", str(plan.plan_id))
        return plan

    @app.post("/plans/{plan_id}/transition", response_model=Plan)
    def transition_plan(
        plan_id: UUID,
        body: ResearchTransitionRequest,
        core: CoreState = Depends(_require_session),
    ) -> Plan:
        """推进计划状态机；非法迁移抛 ValueError。"""
        try:
            target = PlanStatus(body.target)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"unknown status: {body.target}") from exc
        try:
            plan = core.database.get_plan(plan_id, core.local_user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if plan.status == target:
            return plan
        if target not in ALLOWED_PLAN_TRANSITIONS[plan.status]:
            raise HTTPException(status_code=409, detail=f"invalid Plan transition: {plan.status.value} -> {target.value}")
        updated = plan.model_copy(
            update={"status": target, "updated_at": datetime.now(UTC)}
        )
        core.database.insert_plan(updated)
        _audit(
            core,
            "plan.transition",
            "plan",
            str(plan_id),
            payload={"confirmation_summary": body.confirmation_summary}
            if body.confirmation_summary
            else {},
        )
        return updated

    @app.get("/decisions", response_model=list[DecisionEntry])
    def list_decisions(core: CoreState = Depends(_require_session)) -> list[DecisionEntry]:
        return core.database.list_decisions(core.local_user_id)

    @app.post("/decisions", response_model=DecisionEntry, status_code=status.HTTP_201_CREATED)
    def create_decision(body: DecisionRequest, core: CoreState = Depends(_require_session)) -> DecisionEntry:
        decision = DecisionEntry(
            decision_id=uuid4(),
            user_id=core.local_user_id,
            made_at=body.made_at or datetime.now(UTC),
            theme=body.theme,
            decision_summary=body.decision_summary,
            rationale=body.rationale,
            outcome=body.outcome,
            retrospective=body.retrospective,
            linked_evidence_ids=[UUID(item) for item in body.linked_evidence_ids],
            plan_id=UUID(body.plan_id) if body.plan_id else None,
            inaction_reason=body.inaction_reason,
        )
        core.database.insert_decision(decision)
        _audit(core, "decision.created", "decision", str(decision.decision_id))
        return decision

    @app.post("/evidence/decision-outcome", response_model=DecisionEntry)
    def record_decision_outcome(body: DecisionOutcomeRequest, core: CoreState = Depends(_require_session)) -> DecisionEntry:
        """E2 研究后验最小闭环：到期补录决定的「结果 / 事后评价」。

        - 单段路径设计（/evidence/decision-outcome）命中桥接正则 evidence/[^/]+ 兜底，
          打包模式零 asar/main.ts 改动（与 collab-* 端点同一约定）。
        - 只补事后字段，原始判断（theme/summary/rationale/依据/时间）不可改写
          ——路线图 E2 铁律：不用事后信息改写原始研究。
        """
        updated = core.database.update_decision_outcome(
            core.local_user_id,
            body.decision_id,
            body.outcome.strip(),
            body.retrospective.strip(),
        )
        if updated is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="决定不存在或不属于当前用户")
        _audit(core, "decision.outcome.recorded", "decision", str(body.decision_id))
        return updated

    @app.get("/research/runs", response_model=list[ResearchRun])
    def list_research_runs(core: CoreState = Depends(_require_session)) -> list[ResearchRun]:
        return core.database.list_research_runs(core.local_user_id)

    @app.post("/research/runs", response_model=ResearchRun, status_code=status.HTTP_201_CREATED)
    def create_research_run(
        body: ResearchQuestionRequest, core: CoreState = Depends(_require_session)
    ) -> ResearchRun:
        run = ResearchRun(
            run_id=uuid4(),
            user_id=core.local_user_id,
            status=RunStatus.CREATED,
            user_question=body.user_question,
        )
        core.database.upsert_research_run(run)
        _audit(core, "research.run.created", "research_run", str(run.run_id))
        return run

    @app.get("/research/runs/{run_id}", response_model=ResearchRun)
    def get_research_run(run_id: UUID, core: CoreState = Depends(_require_session)) -> ResearchRun:
        run = core.database.get_research_run(run_id, core.local_user_id)
        if run is None:
            raise HTTPException(status_code=404, detail="research run not found")
        return run

    @app.delete("/research/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_research_run(
        run_id: UUID, core: CoreState = Depends(_require_session)
    ) -> None:
        deleted = core.database.delete_research_run(run_id, core.local_user_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="research run not found")
        _audit(core, "research.run.deleted", "research_run", str(run_id))

    @app.post("/research/runs/{run_id}/transition", response_model=ResearchRun)
    def transition_research_run(
        run_id: UUID, body: ResearchTransitionRequest, core: CoreState = Depends(_require_session)
    ) -> ResearchRun:
        try:
            target = RunStatus(body.target)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"unknown status: {body.target}") from exc
        try:
            advanced = core.database.transition_research_run(run_id, core.local_user_id, target)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _audit(
            core,
            "research.run.transition",
            "research_run",
            str(run_id),
            payload={"confirmation_summary": body.confirmation_summary}
            if body.confirmation_summary
            else {},
        )
        return advanced

    @app.post("/research/runs/{run_id}/run", response_model=AgentResponse)
    def run_research_endpoint(
        run_id: UUID, core: CoreState = Depends(_require_session)
    ) -> AgentResponse:
        run = core.database.get_research_run(run_id, core.local_user_id)
        if run is None:
            raise HTTPException(status_code=404, detail="research run not found")
        if run.status != RunStatus.CREATED:
            raise HTTPException(
                status_code=409, detail="研究链路已启动，不能重复执行"
            )
        try:
            response = run_research(
                core.database,
                run,
                # 启用模型出网时尝试模型路径（内部再依激活方案与闸门判定）；关闭则纯本地引擎。
                model_client=model_client if core.settings.model_access_enabled else None,
                # JV07：行动模式预判只在「本来会走模型路径」时才有意义（省一次完整调用），
                # 模型出网关闭时传 None——那时预判既省不下钱又要多花一次 Jev 调用。
                action_router=(
                    jev_client.build_shard_reviewer(
                        core, resolve_store(core.database), purpose="jev:action-routing"
                    )
                    if core.settings.model_access_enabled
                    else None
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _audit(core, "research.run.answered", "agent_response", str(response.response_id))
        if response.jev_route is not None:
            # 硬约束②：路由决策落审计，含 confidence、所选模式与**完整题目回执**
            # （题面本身由 `jev_route_question()` 复现，审计里存下来才能事后对出「当时问了什么」）。
            _audit(
                core,
                "research.run.jev_route",
                "agent_response",
                str(response.response_id),
                payload={
                    "mode": response.jev_route.mode,
                    "mode_label": response.jev_route.mode_label,
                    "confidence": response.jev_route.confidence,
                    "bypassed_model": response.jev_route.bypassed_model,
                    "route_state": response.jev_route.route_state,
                    "action_mode": str(response.action_mode),
                    "model": response.jev_route.model,
                    "note": response.jev_route.note,
                    "question_id": response.jev_route.question_id,
                    "question": research_module.jev_route_question()[
                        response.jev_route.question_id
                    ]
                    if response.jev_route.question_id
                    else None,
                    "schema_version": response.jev_route.schema_version,
                },
            )
        return response

    @app.get("/research/questions/latest", response_model=AgentResponse | None)
    def latest_research_answer(core: CoreState = Depends(_require_session)) -> AgentResponse | None:
        return read_latest_response(core.database, core.local_user_id)

    @app.get("/research/runs/{run_id}/response", response_model=AgentResponse | None)
    def get_research_run_response(
        run_id: UUID, core: CoreState = Depends(_require_session)
    ) -> AgentResponse | None:
        return core.database.get_agent_response_by_run(run_id, core.local_user_id)

    def _local_day() -> str:
        return datetime.now().astimezone().date().isoformat()

    @app.get("/brief/today", response_model=TodayBrief)
    def today_brief(core: CoreState = Depends(_require_session)) -> TodayBrief:
        day = _local_day()
        existing = core.database.get_today_brief(day, core.local_user_id)
        holdings = core.database.list_holdings(core.local_user_id)
        evidence = core.database.list_evidence(core.local_user_id)
        if existing is not None:
            latest_input = max((as_utc(item.collected_at) for item in evidence), default=datetime.min.replace(tzinfo=UTC))
            cached_instruments = {item.related_instrument for item in existing.items if item.related_instrument}
            current_instruments = {item.instrument for item in holdings}
            cached_evidence_refs = {ref for item in existing.items for ref in item.evidence_refs}
            current_evidence_refs = {str(item.evidence_id) for item in evidence}
            if latest_input <= as_utc(existing.generated_at) and cached_instruments == current_instruments and cached_evidence_refs == current_evidence_refs:
                return existing
        brief = build_today_brief(
            core.database, core.local_user_id, holdings, evidence
        )
        core.database.upsert_today_brief(day, brief)
        _audit(core, "brief.today.generated", "today_brief", str(brief.brief_id))
        return brief

    @app.post("/brief/today/generate", response_model=TodayBrief)
    def generate_today_brief(core: CoreState = Depends(_require_session)) -> TodayBrief:
        """worker 主动落库入口：立即生成并持久化今日日报，不再依赖用户打开页面。"""
        day = _local_day()
        holdings = core.database.list_holdings(core.local_user_id)
        brief = build_today_brief(
            core.database, core.local_user_id, holdings, core.database.list_evidence(core.local_user_id)
        )
        core.database.upsert_today_brief(day, brief)
        _audit(core, "brief.today.persisted", "today_brief", str(brief.brief_id))
        return brief

    def _load_notify_prefs(core: CoreState) -> PersonalNotifyPrefs:
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
        core: CoreState,
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

    def _apply_notification_triage(notifications: list[Notification], core: CoreState) -> None:
        """JV08：落库前给每条通知打语义分诊结论（**原地修改**，不改列表长度、不改顺序）。

        **软校验铁律**：本函数**绝不删除**任何通知。判为低相关的条目照常落库，只是带上
        `triage.suppressed=True`，由呈现层与投递层决定「不弹、不发」——留痕可查（§M）。

        **不重复出网**：同 dedup 键的通知若已带分诊结论落过库，直接复用，不再送评。
        按小时桶反复评估时这条是成本红线（R1：只按输入 token 计费）。

        `build_shard_reviewer` 返回 None（总闸关闭 / Jev 未启用）时**直接返回**，
        一个字段都不写——`triage` 保持 None，与接入前逐字节一致。
        """
        reviewer = jev_client.build_shard_reviewer(
            core, resolve_store(core.database), purpose="jev:notify-triage"
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

    def _persist_evaluated(notifications: list[Notification], core: CoreState) -> list[Notification]:
        # E4：规则去重与投递状态分离。失败通知在退避窗口到期后再次尝试，
        # 不会因为 dedup 记录永久吞掉提醒；成功通知不重复外呼。
        store = resolve_store(core.database)
        now = datetime.now(UTC)
        # 个人中心通知偏好：免打扰时段内或外部通道关闭时，跳过投递且不计退避，
        # 保持 pending——窗口结束（或重新开启通道）后的下一次 evaluate 会正常重试。
        prefs = _load_notify_prefs(core)
        external_allowed = prefs.external_enabled and not _quiet_hours_active(prefs)
        hold_reason = None if external_allowed else (
            "免打扰时段内暂不投递" if _quiet_hours_active(prefs) else "外部通道已在个人中心关闭"
        )
        # v21 M2-02：daily/weekly 汇总节奏——外部投递不逐条即时发，等窗口期聚合一次。
        digest_due, digest_key = digest_window(prefs, datetime.now().astimezone())
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
            receipts = deliver_notification(current, store)
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
                receipts = deliver_notification(digest, store)
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
                _audit(
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
            _audit(core, "notification.delivered", "notification", str(delivered_count))
        return result_notifications

    @app.get("/notifications/channels", response_model=list[dict[str, object]])
    def notification_channels(core: CoreState = Depends(_require_session)) -> list[dict[str, object]]:
        return channel_status(resolve_store(core.database))

    @app.get("/notifications/pending", response_model=list[Notification])
    def pending_notifications(core: CoreState = Depends(_require_session)) -> list[Notification]:
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
            for item in evaluate_notifications(core.database, core.local_user_id)
            if notification_dedup_key(item) not in suppressed_keys
        ]
        by_key = {notification_dedup_key(item): item for item in persisted}
        # 已落库的投递状态（failed/delivered、重试时间）优先于本次内存预览，
        # 否则 GET 会把失败详情覆盖成默认 pending，用户无法判断是否可重试。
        merged = list({**{notification_dedup_key(item): item for item in evaluated}, **by_key}.values())
        # 个人中心通知偏好：关闭站内提醒或处于免打扰时段时，站内列表不外露。
        # 通知本身已由 evaluate 落库，免打扰窗口结束后未读通知自然恢复可见。
        prefs = _load_notify_prefs(core)
        if not prefs.in_app_enabled or _quiet_hours_active(prefs):
            return []
        return merged

    @app.get("/notifications/triage", response_model=NotificationTriageReport)
    def notification_triage_report(
        limit: int = 50, core: CoreState = Depends(_require_session)
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

    @app.post("/notifications/{notification_id}/read", response_model=Notification)
    def mark_notification_read(
        notification_id: UUID, core: CoreState = Depends(_require_session)
    ) -> Notification:
        notification = core.database.mark_notification_read(notification_id, core.local_user_id)
        if notification is None:
            raise HTTPException(status_code=404, detail="notification not found")
        _audit(core, "notification.read", "notification", str(notification_id))
        return notification

    @app.post("/notifications/evaluate", response_model=list[Notification])
    def evaluate_pending_notifications(core: CoreState = Depends(_require_session)) -> list[Notification]:
        """worker 主动评估入口：评估 + 去重落库当前命中的通知提醒。

        JV08：落库前先做一遍语义分诊（相关性 + 优先级）。**响应形状不变**——被压制的条目
        照常出现在返回列表里（只是带 `triage` 字段），所以 worker 与既有前端无需改动；
        「不弹」是由 `GET /notifications/pending` 的过滤与投递层的跳过实现的，
        而不是靠从这里删掉条目（那才是静默吞）。
        """
        notifications = evaluate_notifications(core.database, core.local_user_id)
        _apply_notification_triage(notifications, core)
        notifications = _persist_evaluated(notifications, core)
        _audit(core, "notification.rules.re-evaluated", "notification", str(len(notifications)))
        return notifications

    @app.post("/evidence/freshness/patrol", response_model=FreshnessPatrolResult)
    def evidence_freshness_patrol(core: CoreState = Depends(_require_session)) -> FreshnessPatrolResult:
        """worker 主动巡检入口：审计标记 valid_until 已到期的证据，不改写账本内容。"""
        result = build_freshness_patrol(core.database, core.local_user_id)
        _audit(core, "evidence.freshness.patrolled", "evidence", str(result.stale))
        return result

    @app.get("/learning/unit/today", response_model=LearningUnit | None)
    def today_learning_unit(core: CoreState = Depends(_require_session)) -> LearningUnit | None:
        unit = build_today_learning_unit(core.database, core.local_user_id)
        _audit(core, "learning.unit.produced", "learning_unit", str(unit.unit_id) if unit else "none")
        return unit

    @app.get("/learning/goals/current", response_model=LearningGoal)
    def current_learning_goal(core: CoreState = Depends(_require_session)) -> LearningGoal:
        goal = build_current_learning_goal(core.database, core.local_user_id)
        _audit(core, "learning.goal.current", "learning_goal", str(goal.goal_id))
        return goal

    @app.get("/learning/activities", response_model=list[LearningActivity])
    def list_learning_activities(core: CoreState = Depends(_require_session)) -> list[LearningActivity]:
        return core.database.list_learning_activities(core.local_user_id)

    @app.post(
        "/learning/activities", response_model=LearningActivity, status_code=status.HTTP_201_CREATED
    )
    def create_learning_activity(
        body: LearningActivityRequest, core: CoreState = Depends(_require_session)
    ) -> LearningActivity:
        activity = LearningActivity(
            activity_id=uuid4(),
            user_id=core.local_user_id,
            unit_id=body.unit_id,
            unit_type=LearningUnitType(body.unit_type),
            bound_instrument=body.bound_instrument,
            bound_instrument_label=body.bound_instrument_label,
            objective=body.objective,
            user_answer=body.user_answer,
            reflection=body.reflection,
        )
        core.database.upsert_learning_activity(activity)
        _audit(core, "learning.activity.created", "learning_activity", str(activity.activity_id))
        return activity

    @app.delete("/learning/activities/{activity_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_learning_activity(
        activity_id: UUID, core: CoreState = Depends(_require_session)
    ) -> None:
        if core.database.get_learning_activity(activity_id, core.local_user_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="learning activity not found"
            )
        core.database.delete_learning_activity(activity_id, core.local_user_id)
        _audit(core, "learning.activity.deleted", "learning_activity", str(activity_id))

    @app.get("/learning/reflections/export", response_model=list[LearningActivity])
    def export_learning_reflections(core: CoreState = Depends(_require_session)) -> list[LearningActivity]:
        """导出：仅导用户自己的学习活动与反思（含绑定的标的与日期），便于备份。"""
        _audit(core, "learning.reflections.exported", "learning_activity", "all")
        return core.database.list_learning_activities(core.local_user_id)

    @app.post(
        "/learning/activities/{activity_id}/propose-policy-change",
        response_model=InvestmentPolicyVersion,
        status_code=status.HTTP_201_CREATED,
    )
    def propose_policy_change_from_learning(
        activity_id: UUID, core: CoreState = Depends(_require_session)
    ) -> InvestmentPolicyVersion:
        """学习 → 矛盾检测 → 原则草案：由一次已保存反思产出一份 DRAFT 原则修订。

        「学习完成」不会自动改变风险权限：产出的始终是 draft，必须走
        /investment-policies/{id}/confirm 由用户显式确认后才成为 active。
        """
        activity = core.database.get_learning_activity(activity_id, core.local_user_id)
        if activity is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="learning activity not found"
            )
        if not activity.reflection.strip():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="没有可用的反思内容，暂无法据此建议原则变更",
            )
        existing = core.database.list_policies(core.local_user_id)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="尚无已确认的投资原则可供修订，请先建立投资原则",
            )
        latest = max(existing, key=lambda item: item.version)
        draft = InvestmentPolicyVersion(
            policy_id=uuid4(),
            user_id=core.local_user_id,
            version=latest.version + 1,
            status=PolicyStatus.DRAFT,
            investment_goal=latest.investment_goal,
            horizon_years=latest.horizon_years,
            liquidity_needs=latest.liquidity_needs,
            allowed_markets=latest.allowed_markets,
            allowed_asset_classes=latest.allowed_asset_classes,
            risk_boundaries=latest.risk_boundaries,
            preferred_methods=latest.preferred_methods,
            excluded_methods=latest.excluded_methods,
            observation_conditions=latest.observation_conditions,
            invalidation_conditions=latest.invalidation_conditions,
            change_reason=f"学习反思建议修订（来自学习活动 {activity.activity_id}）",
            source_learning_activity_ids=[activity.activity_id],
        )
        core.database.insert_policy(draft)
        _audit(
            core,
            "learning.proposed_policy_change",
            "investment_policy",
            str(draft.policy_id),
        )
        return draft

    @app.get("/review/weekly", response_model=WeeklyReview)
    def weekly_review(core: CoreState = Depends(_require_session)) -> WeeklyReview:
        review = build_weekly_review(
            core.database,
            core.local_user_id,
            core.database.list_theses(core.local_user_id),
            core.database.list_research_runs(core.local_user_id),
        )
        _audit(core, "review.weekly.generated", "weekly_review", str(review.review_id))
        return review

    @app.get("/capabilities", response_model=list[PluginCapability])
    def list_capabilities(core: CoreState = Depends(_require_session)) -> list[PluginCapability]:
        return _capabilities()

    @app.get("/plugins/catalog", response_model=list[PluginCatalogEntry])
    def plugin_catalog(core: CoreState = Depends(_require_session)) -> list[PluginCatalogEntry]:
        installations = {item.plugin_id: item for item in core.database.list_plugin_installations()}
        return [
            PluginCatalogEntry(
                manifest=manifest,
                installation=installations.get(manifest.plugin_id),
                # 输出去向由服务端插槽注册表解析（G2-3），前端不再维护 slotPageOf 兜底。
                resolved_outputs=resolve_outputs(manifest.ui_slots),
            )
            for manifest in _plugin_manifests()
        ]

    @app.get("/channels", response_model=list[dict[str, str]])
    def update_channels(core: CoreState = Depends(_require_session)) -> list[dict[str, str]]:
        """三套更新通道的可观测表面：Host / 插件 / 内容，含当前版本快照。"""
        channel = snapshot_channels(
            core.database, core.database.list_evidence(core.local_user_id), __version__
        )
        registry = {entry["channel"]: dict(entry) for entry in channel_registry()}
        registry[UpdateChannel.HOST.value].update({"current": channel.host_version})
        registry[UpdateChannel.PLUGIN.value].update(
            {"current": ",".join(f"{pid}@{ver}" for pid, ver in channel.plugin_versions)}
        )
        registry[UpdateChannel.CONTENT.value].update(
            {"current": channel.content_fingerprint[:16]}
        )
        return [registry[name] for name in ("host", "plugin", "content")]

    @app.post("/plugins/{plugin_id:path}/install", response_model=PluginInstallation)
    def install_plugin(
        plugin_id: str, core: CoreState = Depends(_require_session)
    ) -> PluginInstallation:
        try:
            manifest = _verified_registry_manifest(plugin_id, core.settings)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
        existing = core.database.get_plugin_installation(plugin_id)
        # 独立应用（mount=own_page）安装时占 app.library 槽：满额拒绝并返回 409（G2-1）。
        if manifest.mount == MOUNT_OWN_PAGE:
            current_installations = core.database.list_plugin_installations()
            occupied = len(_enabled_own_page_plugins(current_installations))
            if occupied >= core.settings.app_library_cap:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="应用区已满，请先移除一个",
                )
        # 安装锁：固定精确版本，不默认静默升级/覆盖已安装的其它版本。
        if existing is not None and existing.release_version != manifest.release_version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"已安装 {existing.release_version}，不能直接安装 {manifest.release_version}；"
                    "请先走更新事务或停用/撤销后处理。"
                ),
            )
        installation = PluginInstallation(
            plugin_id=manifest.plugin_id,
            release_version=manifest.release_version,
            state=PluginInstallationState.ENABLED,
            granted_capabilities=manifest.capabilities,
            artifact_sha256=manifest.artifact_sha256,
        )
        core.database.upsert_plugin_installation(installation)
        _audit(core, "plugin.installed", "plugin", plugin_id)
        return installation

    @app.post("/plugins/{plugin_id:path}/disable", response_model=PluginInstallation)
    def disable_plugin(
        plugin_id: str, core: CoreState = Depends(_require_session)
    ) -> PluginInstallation:
        existing = core.database.get_plugin_installation(plugin_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="plugin is not installed"
            )
        updated = existing.model_copy(
            update={"state": PluginInstallationState.DISABLED, "updated_at": datetime.now(UTC)}
        )
        core.database.upsert_plugin_installation(updated)
        _audit(core, "plugin.disabled", "plugin", plugin_id)
        return updated

    @app.post("/plugins/{plugin_id:path}/update", response_model=PluginInstallation)
    def update_plugin(
        plugin_id: str,
        body: PluginUpdateRequest,
        core: CoreState = Depends(_require_session),
    ) -> PluginInstallation:
        """更新事务：暂存安装 → 健康检查 → 原子切换；失败回滚旧版本。

        - 目标版本先写入暂存候选，当前安装版本保持不动（staged）；
        - 健康检查（签名/哈希 + schema 兼容性）失败 → 删除候选回滚 + 409，旧版本可用；
        - 通过后原子切换为新的 PluginInstallation，成功后候选标记 committed 并移除。
        """
        existing = core.database.get_plugin_installation(plugin_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="插件未安装，请先安装再更新"
            )
        if existing.release_version == body.target_version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="目标版本与当前安装版本一致，无需更新"
            )
        # 通道守卫（只读）：记录更新前 HOST / CONTENT 状态，供提交后对照防跨通道覆盖。
        evidence_snapshot = core.database.list_evidence(core.local_user_id)
        before_channels = snapshot_channels(core.database, evidence_snapshot, __version__)
        try:
            candidate_manifest = _verified_registry_manifest_version(
                plugin_id, body.target_version, core.settings
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc

        candidate = PluginUpdateCandidate(
            candidate_id=uuid4(),
            plugin_id=plugin_id,
            target_version=body.target_version,
            source_artifact_sha256=candidate_manifest.artifact_sha256,
            manifest_payload=candidate_manifest.model_dump(mode="json"),
        ).stage()
        core.database.upsert_update_candidate(candidate)
        _audit(core, "plugin.update.staged", "plugin", plugin_id)

        try:
            _health_check(candidate_manifest, core.settings)
        except ValueError as exc:
            core.database.delete_update_candidate(str(candidate.candidate_id))
            _audit(core, "plugin.update.rolled_back", "plugin", plugin_id)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"健康检查未通过，已回滚到 {existing.release_version}：{exc}",
            ) from exc

        committed = candidate.to_committed()
        core.database.upsert_update_candidate(committed)
        new_installation = existing.model_copy(
            update={
                "release_version": candidate_manifest.release_version,
                "state": PluginInstallationState.ENABLED,
                "granted_capabilities": candidate_manifest.capabilities,
                "artifact_sha256": candidate_manifest.artifact_sha256,
                "updated_at": datetime.now(UTC),
            }
        )
        core.database.upsert_plugin_installation(new_installation)
        core.database.delete_update_candidate(str(candidate.candidate_id))
        # 通道守卫（只读）：提交后对照，确认本次更新只改 PLUGIN 通道，未跨通道覆盖
        # HOST 版本或 CONTENT 内容；如被误触（不应发生）按 409 拒绝并回滚。
        try:
            assert_no_cross_channel_overwrite(
                before_channels,
                snapshot_channels(core.database, core.database.list_evidence(core.local_user_id), __version__),
            )
        except ValueError as exc:
            _audit(core, "plugin.update.cross_channel_blocked", "plugin", plugin_id)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"跨通道覆盖检测到，已中止并保留旧版本：{exc}",
            ) from exc
        _audit(core, "plugin.update.committed", "plugin", plugin_id)
        return new_installation

    @app.post("/plugins/{plugin_id:path}/revoke", response_model=PluginInstallation)
    def revoke_plugin(
        plugin_id: str, core: CoreState = Depends(_require_session)
    ) -> PluginInstallation:
        """撤销与安全降级：阻断插件继续产生新输出，历史 Evidence 保留不影响读取。"""
        existing = core.database.get_plugin_installation(plugin_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="plugin is not installed"
            )
        updated = existing.model_copy(
            update={"state": PluginInstallationState.REVOKED, "updated_at": datetime.now(UTC)}
        )
        core.database.upsert_plugin_installation(updated)
        _audit(core, "plugin.revoked", "plugin", plugin_id)
        return updated

    @app.get("/plugins/{plugin_id:path}/app", response_model=dict[str, object])
    def plugin_app_envelope(
        plugin_id: str, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        """独立应用主视图壳端点（G2-2）：只做壳与鉴权，内容按插件 schema 校验。

        仅对「已安装且 ENABLED 且 mount=own_page」的插件提供；否则 404（非应用/未挂载）。
        payload 由插件 schema 决定：研读图书馆输出当前荐读计划，其余插件暂为产物占位。
        """
        installation = core.database.get_plugin_installation(plugin_id)
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="应用未安装或未启用")
        manifest = _manifest_index().get(plugin_id)
        if manifest is None or manifest.mount != MOUNT_OWN_PAGE:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="该插件不是独立应用")
        payload: object
        if manifest.plugin_id == "official.reading-library":
            plans = core.database.list_library_plans()
            payload = {"latest_plan": plans[0].model_dump(mode="json") if plans else None}
        else:
            payload = {}
        _audit(core, "plugin.app.envelope", "plugin", plugin_id)
        return {
            "page_title": manifest.display_name,
            "kicker": manifest.description,
            "content_schema_version": ", ".join(f"{k}={v}" for k, v in manifest.schema_versions.items()),
            "payload": payload,
        }

    @app.get("/tactics/catalog")
    def tactics_catalog(core: CoreState = Depends(_require_session)) -> list[dict[str, object]]:
        """战法目录：名称/方向/口径说明（纯元数据，不做任何计算）。"""
        return stock_tactics.catalog()

    # —— P3-D03：游资雷达（official.youzi-radar）——
    # 三个读接口 + 一个学习单元接口。页面在插件未启用时隐藏；但即使前端漏判，
    # 这里也会因插件未启用而返回 409，不出现「没装插件却有数据」的错觉。

    def _require_youzi(core: CoreState) -> None:
        """席位数据的插件闸门：与 macro-radar 同口径（未启用 → 409）。"""
        installation = core.database.get_plugin_installation("official.youzi-radar")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="youzi-radar plugin is not enabled"
            )

    #: 覆盖状态的「最差」优先序（数值越大越差）。`unknown` 必须比 `fetch_failed`
    #: 更差：失败至少知道失败了，`unknown` 连这一点都不确定。若把 unknown 折进
    #: fetch_failed，「无法判断」就被伪装成「确定性失败」（Y2-09 明令禁止）。
    _COVERAGE_SEVERITY = {"complete": 0, "truncated": 1, "fetch_failed": 2, "unknown": 3}

    def _worst_coverage(states: list[str]) -> str:
        """取一组报告覆盖状态里最差的一个；未登记状态按 `unknown` 处理（不乐观）。"""
        return max(states, key=lambda state: _COVERAGE_SEVERITY.get(state, 3), default="unknown")

    def _shift_day(day: str, offset_days: int) -> str:
        """按**自然日**平移 `YYYYMMDD`（负数为向前）。仅用于放大日历取数窗口。

        日历区间是闭区间，而 `probe.previous()` 只能在自己的 `days` 里向前取；
        因此取日历的起点必须**早于**业务区间起点。这里只平移取数窗口，不参与任何
        交易判定——平移出来的自然日会由 `in_range` 过滤掉，不会混进事件集。
        """
        base = datetime.strptime(day, "%Y%m%d").date() + timedelta(days=offset_days)
        return base.strftime("%Y%m%d")

    @app.get("/youzi/tactics")
    def youzi_tactics_latest(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """事实页签的默认入口：有效日由**可信日历**判定，不接受自然日推算。

        取「日历中最近一个已确认交易日」作为有效日，然后走与
        `/youzi/tactics/{day}` 完全相同的计算路径。**不猜**：日历不可用时
        返回 `calendar_missing`，绝不回退到 `date.today()`（周末/节假日会把
        休市日当交易日）。
        """
        _require_youzi(core)
        import investment_steward_core.tactic_annotator as annotator_module
        import investment_steward_core.trade_calendar as calendar_module

        try:
            calendar = calendar_module.recent_trading_days(6)
        except calendar_module.CalendarError as error:
            logger.warning("youzi tactics latest calendar error: %s", error)
            calendar = None
        if calendar is None or not calendar.available or not calendar.days:
            return {
                "request_day": "",
                "effective_day": None,
                "window_days": [],
                "coverage": {},
                "available": False,
                "items": [],
                "raw_billboard": [],
                "reason": "calendar_missing",
                "conflicts": [],
                "excluded": {},
                "boundary_notice": annotator_module.BOUNDARY_NOTICE,
            }
        return youzi_tactics(calendar.days[-1], core)

    @app.get("/youzi/tactics/{day}")
    def youzi_tactics(day: str, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """Y2 跨日事实：以 `day` 为截至日的五交易日窗口，输出三类路径事实。

        降级规则（不可让渡）：
        - 无可信日历 → `available=false, reason=calendar_missing`，**不猜有效日**。
        - `day` 不是交易日（休市/未来/尚未收盘）→ `reason=not_a_trading_day`。
        - 任一窗口日取数非 `complete` → 窗口不完整 → `available=false`，但**仍返回
          已取得的原始榜单行**（`items` 为空，`raw_billboard` 有值），不把取数失败
          说成「没有事件」。
        - 完整且无命中 → `available=true, items=[]`（这才是「未检出」的合法表达）。

        输出**不含**任何涨跌/收益/胜率字段；`boundary_notice` 固定出现。
        """
        _require_youzi(core)
        from datetime import date

        import investment_steward_core.lhb_feed as lhb_feed_module
        import investment_steward_core.tactic_annotator as annotator_module
        import investment_steward_core.trade_calendar as calendar_module
        import investment_steward_core.youzi_normalize as normalize_module
        import investment_steward_core.youzi_paging as paging_module

        boundary = annotator_module.BOUNDARY_NOTICE
        try:
            if len(day) != 8 or not day.isascii() or not day.isdigit():
                raise ValueError("expected YYYYMMDD")
            date(int(day[:4]), int(day[4:6]), int(day[6:]))
        except ValueError as error:
            raise HTTPException(status_code=422, detail="日期必须为有效 YYYYMMDD") from error

        def unavailable(reason: str, effective: str | None = None) -> dict[str, object]:
            """降级响应。

            `effective` 只在**已确认为交易日**的分支传入：此时有效日是已知事实，
            应当如实回报（否则前端分不清「日历都没有」和「日历有但窗口不够」）。
            日历缺失或 `day` 不在日历中时保持 `None`——那才是「有效日未知」。
            """
            return {
                "request_day": day,
                "effective_day": effective,
                "window_days": [],
                "coverage": {},
                "available": False,
                "items": [],
                "raw_billboard": [],
                "reason": reason,
                "conflicts": [],
                "excluded": {},
                "boundary_notice": boundary,
            }

        # —— 1. 可信日历 ——
        try:
            calendar = calendar_module.recent_trading_days(6, as_of=day)
        except calendar_module.CalendarError as error:
            logger.warning("youzi tactics calendar error for %s: %s", day, error)
            return unavailable("calendar_missing")
        if not calendar.available:
            logger.info("youzi tactics calendar unavailable for %s: %s", day, calendar.reason)
            return unavailable("calendar_missing")
        if not calendar.contains(day):
            # 休市日、未来日、或当日尚未生成横截面。三者在日历形状上不可区分，
            # 因此统一说「不是已确认交易日」，绝不回退自然日或借相邻日顶替。
            return unavailable("not_a_trading_day")
        window = calendar.window_ending_at(day, count=5)
        if len(window) < 5:
            return unavailable("insufficient_trading_history", day)

        # —— 2. 五日窗口完整分页取数（共用一个预算）——
        budget = paging_module.Budget()
        coverage: dict[str, str] = {}
        day_reports: dict[str, dict[str, tuple[list[dict[str, object]], str, str]]] = {}
        incomplete: list[str] = []
        for window_day in window:
            reports = lhb_feed_module.fetch_day_page_complete(window_day, budget=budget)
            day_reports[window_day] = reports
            states = sorted({state for _, state, _ in reports.values()})
            # 窗口日覆盖取三报告中最差状态：任一报告不完整则该日不完整。
            if all(state == "complete" for state in states):
                coverage[window_day] = "complete"
            else:
                # Y2-09：**不得**把 `unknown` 混进 `fetch_failed`。「取数失败」（我知道
                # 我失败了）与「无法判断」（我连空响应是不是真的空都说不准）对使用
                # 者是两件事，合并就等于把不确定性伪装成确定性失败。
                coverage[window_day] = _worst_coverage(states)
                incomplete.append(f"{window_day}:{coverage[window_day]}")

        if incomplete:
            # 降级展示：把已取到的原始榜单行回给前端，但**不生成任何事实**。
            raw: list[dict[str, object]] = []
            for window_day in window:
                rows = day_reports.get(window_day, {}).get(
                    lhb_feed_module.REPORT_DAILY_BILLBOARD, ([], "", "")
                )[0]
                for row in rows:
                    if str(row.get("SECURITY_CODE") or "").strip():
                        raw.append(
                            {
                                "trading_day": window_day,
                                "security_code": str(row.get("SECURITY_CODE") or ""),
                                "security_name": str(row.get("SECURITY_NAME_ABBR") or ""),
                            }
                        )
            payload = unavailable("incomplete_window", day)
            payload["window_days"] = list(window)
            payload["coverage"] = coverage
            payload["raw_billboard"] = raw[:50]
            return payload

        # —— 3. 标准化 + 逐证券标注 ——
        records_by_code: dict[str, list[object]] = {}
        raw_by_code: dict[str, dict[str, object]] = {}
        #: 证据定位表：证据 ID → 原始来源定位（报告名 / 来源记录 ID / 金额口径）。
        #: Y2-06 要求「用户可定位的原始披露回链」——只有金额与日期不足以回到原文，
        #: 必须同时给出**来源报告名**与**来源记录 ID**。该映射由本函数从同一批
        #: 规范化记录派生，不额外请求、不跨请求缓存。
        locators: dict[str, dict[str, object]] = {}
        for window_day in window:
            reports = day_reports[window_day]
            billboard_rows = reports[lhb_feed_module.REPORT_DAILY_BILLBOARD][0]
            for row in billboard_rows:
                code = str(row.get("SECURITY_CODE") or "").strip()
                if code and code not in raw_by_code:
                    raw_by_code[code] = {
                        "trading_day": window_day,
                        "security_code": code,
                        "security_name": str(row.get("SECURITY_NAME_ABBR") or ""),
                    }
            for report, direction in (
                (lhb_feed_module.REPORT_SEAT_BUY, lhb_feed_module.DIRECTION_BUY),
                (lhb_feed_module.REPORT_SEAT_SELL, lhb_feed_module.DIRECTION_SELL),
            ):
                rows = reports[report][0]
                for seat in lhb_feed_module.parse_seat_items(rows, direction):
                    normalized = normalize_module.normalize_seat_row(seat)
                    records_by_code.setdefault(seat.security_code, []).append(normalized)
                    # evidence_chain 已强制白名单（拒绝数据商统计字段与额外字段），
                    # 直接复用它，避免在这里手写字段集而与契约漂移。
                    chain = normalize_module.evidence_chain(normalized)
                    locators[str(chain["evidence_id"])] = chain

        def _evidence_with_locator(item: dict[str, object], security_code: str) -> dict[str, object]:
            """证据条目 + 可定位来源字段（白名单已由 evidence_chain 保证）。

            G03：`evidence_chain` 给的是「来源报告名 + 来源记录 ID」这把**逻辑**钥匙，
            使用者仍无法直接跳过去；再补一条由（交易日, 证券代码）确定性拼出的
            `source_url`（`lhb_feed.provider_page_url`）才是真正可点的回链。
            """
            evidence_id = str(item.get("evidence_id") or "")
            located = dict(locators.get(evidence_id, {}))
            trading_day = str(item.get("trading_day") or "")
            return {
                "trading_day": trading_day,
                "direction": str(item.get("direction") or ""),
                "amount": item.get("amount"),
                "evidence_id": evidence_id,
                "explanation": str(item.get("explanation") or ""),
                # 回链定位（缺失时显式空值，不填 0、不猜）。
                "source_report": str(located.get("source_report") or ""),
                "source_record_id": str(located.get("source_record_id") or ""),
                "source_url": lhb_feed_module.provider_page_url(trading_day, security_code),
                "amount_reason": located.get("amount_reason"),
            }

        items: list[dict[str, object]] = []
        conflicts: list[dict[str, object]] = []
        excluded: dict[str, int] = {}
        # 遍历**所有**在窗口内出现过的证券（榜单行或席位行），而不只是有合格席位的
        # 那些：否则「某证券只有聚合桶席位」会连排除原因都不显示，使用者会误以为
        # 它根本没被检查过——这属于「未检出 vs 未检查」的混淆，必须避免。
        all_codes = sorted(set(records_by_code) | set(raw_by_code))
        for code in all_codes:
            result = annotator_module.annotate(
                code,
                records_by_code.get(code, []),
                list(window),
                coverage,
                as_of=day,
            )
            for fact in result.facts:
                items.append(
                    {
                        "fact_type": fact.fact_type,
                        "security_code": fact.security_code,
                        "security_name": raw_by_code.get(code, {}).get("security_name", ""),
                        "operatedept_code": fact.operatedept_code,
                        "operatedept_name": fact.operatedept_name,
                        "days": list(fact.days),
                        "fact_id": fact.fact_id,
                        "evidence": [_evidence_with_locator(item, code) for item in fact.evidence],
                    }
                )
            for conflict in result.conflicts:
                conflicts.append(dict(conflict))
            for key, count in result.excluded:
                excluded[key] = excluded.get(key, 0) + count

        return {
            "request_day": day,
            "effective_day": day,
            "window_days": list(window),
            "coverage": coverage,
            "available": True,
            "items": items,
            "raw_billboard": [raw_by_code[code] for code in sorted(raw_by_code)][:50],
            "reason": "",
            "conflicts": conflicts,
            "excluded": excluded,
            "boundary_notice": boundary,
        }

    @app.get("/youzi/replay/{security_code}")
    def youzi_replay(
        security_code: str,
        start: str = "",
        end: str = "",
        as_of: str = "",
        page_size: int = youzi_replay_module.MAX_PAGE_SIZE,
        cursor: str = "",
        core: CoreState = Depends(_require_session),
    ) -> dict[str, object]:
        """Y3 有限窗口案例复盘：**代码输入查询**，不做推荐列表/排行。

        与 `/youzi/tactics` 的关键差别：这里的窗口由使用者给定起止日，因此必须

        - **先取日历再取数**：只有可信交易日序列上的日子才进入区间，绝不按自然日
          逐日取（节假日会得到确定性的空，而空不是「没有披露」）；
        - **向前多取最多 4 个交易日作上下文**（Y3-04）：首个事件的五交易日窗口需要
          区间前的日子，取不到就不生成该事件的事实标签（写明原因），而不是拿三天硬算；
        - **预算护栏**：`MAX_EVENT_DAYS` 限制可回溯的交易日数，超出部分以
          `truncated` + 原因如实回报，**不悄悄砍掉**；
        - 游标绑定 `(证券, 区间, 快照)`：翻页期间上游有新披露 → 快照变化 → 旧游标
          被拒，避免重行/漏行。

        `as_of` 是**可重复性**参数（不传则取本机日期）：复盘是历史查询，同一区间
        在不同日子跑出的结果应当一致，因此日历与期限都锚定到 `as_of` 而非当下。

        输出**不含**任何收益/胜率/排行字段；`limitations` 与 `boundary_notice` 固定出现。
        """
        _require_youzi(core)
        import investment_steward_core.lhb_feed as lhb_feed_module
        import investment_steward_core.tactic_annotator as annotator_module
        import investment_steward_core.trade_calendar as calendar_module
        import investment_steward_core.youzi_normalize as normalize_module
        import investment_steward_core.youzi_paging as paging_module
        import investment_steward_core.youzi_replay as replay_module

        boundary = annotator_module.BOUNDARY_NOTICE
        horizon_as_of = as_of if calendar_module.is_plausible_day(as_of) else time.strftime("%Y%m%d")

        def degraded(reason: str, **extra: object) -> dict[str, object]:
            payload: dict[str, object] = {
                "security_code": security_code,
                "range_start": start,
                "range_end": end,
                "events": [],
                "cursor": "",
                "has_more": False,
                "total_events": 0,
                "coverage": {},
                "truncated": False,
                "truncated_reason": "",
                "fetched_at": 0.0,
                "available": False,
                "reason": reason,
                "limitations": [],
                "boundary_notice": boundary,
            }
            payload.update(extra)
            return payload

        # —— 1. 参数校验（越界一律报错，不静默截断）——
        try:
            replay_module.validate_request(security_code, start, end, page_size=page_size)
        except replay_module.ReplayInputError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        # —— 2. 可信日历（**必须**从区间之前起算，否则上下文日不存在）——
        #
        # ★ 2026-09-18 修：日历区间是**闭区间**，`probe.previous(day)` 只能在
        # `probe.days` 里向前取。旧实现传 `fetch_calendar(start, horizon_as_of)`，
        # 于是 `probe.days[0] == start`：`previous(selected[0], 4)` **恒为空**，
        # 首个事件日的五交易日窗口永远只有 1 天 → 全部事件落进
        # `insufficient_context_days` → 复盘页「事实标签」一片空白（而 coverage 全
        # complete，看起来毫无异常）。因此改为把日历起点前移 `CONTEXT_LOOKBACK_DAYS`
        # 个自然日；多取的自然日在 `in_range` 过滤时会被排除，不会混进区间。
        lookback_start = start
        try:
            probe = calendar_module.fetch_calendar(
                _shift_day(start, -replay_module.CONTEXT_LOOKBACK_DAYS), horizon_as_of
            )
        except calendar_module.CalendarError as error:
            logger.warning("youzi replay calendar error for %s..%s: %s", start, end, error)
            return degraded("calendar_missing")
        if not probe.available:
            logger.info("youzi replay calendar unavailable for %s..%s: %s", start, end, probe.reason)
            return degraded("calendar_missing")
        # 日历覆盖到 `horizon_as_of`，因此区间内的日子与区间前的上下文日都可用；
        # 若请求区间**整体**落在日历覆盖之外（未来日），那是「日历不够用」，
        # 与「日历够用但这段全是假期」是两件不同的事，必须分开报（Y3-01）。
        #
        # ★ 注意用 `probe.days[-1]`（覆盖到的最后一天）而不是 `probe.days[0]`：
        # 起点已按上文前移，`days[0]` 恒 ≤ 业务起点，判别不了「够不够到区间」。
        if probe.days and probe.days[-1] < start:
            return degraded("calendar_missing")
        if start < probe.days[0]:
            lookback_start = probe.days[0]

        in_range = tuple(day for day in probe.days if start <= day <= end)
        if not in_range:
            # 日历覆盖到区间起点却没有交易日 = 长假或连续休市：
            # 如实说「区间内无交易日」，不折进「日历缺失」，也不回退自然日。
            return degraded("no_trading_day_in_range")

        # —— 3. 事件日 + 上下文前取（Y3-04）——
        budget_exhausted: list[str] = []
        selected = in_range[-replay_module.MAX_EVENT_DAYS :]
        truncated_days = len(in_range) - len(selected)
        context_before = probe.previous(selected[0], count=replay_module.MAX_CONTEXT_DAYS)
        context_start = context_before[0] if context_before else selected[0]

        budget = paging_module.Budget()
        day_reports: dict[str, dict[str, tuple[list[dict[str, object]], str, str]]] = {}
        for day in [*context_before, *selected]:
            if budget.stop_reason() is not None:
                # 预算已停：剩余日子**不谎报成功**——不放进 `day_reports`，
                # 由 `coverage` 如实标 unknown，而不是假装取到一个空集。
                break
            try:
                day_reports[day] = lhb_feed_module.fetch_day_page_complete(day, budget=budget)
            except Exception as error:  # noqa: BLE001 - 单日取数失败应降级而非整请求失败
                logger.warning("youzi replay fetch failed for %s: %s", day, error)
                budget_exhausted.append(day)

        inputs = replay_module.ReplayInputs(
            day_reports=day_reports,
            trading_days=tuple(probe.days),
            calendar_available=True,
            context_start=context_start,
            budget_exhausted_days=tuple(budget_exhausted),
            fetched_at=probe.fetched_at or 0.0,
            as_of_trading_day=horizon_as_of,
        )

        # —— 4. 逐事件标注（复用 Y2 的 annotate，不另写一套判据）——
        records_by_code: dict[str, list[object]] = {}
        for day, reports in day_reports.items():
            for report, direction in (
                (lhb_feed_module.REPORT_SEAT_BUY, lhb_feed_module.DIRECTION_BUY),
                (lhb_feed_module.REPORT_SEAT_SELL, lhb_feed_module.DIRECTION_SELL),
            ):
                for seat in lhb_feed_module.parse_seat_items(reports[report][0], direction):
                    if seat.security_code != security_code:
                        continue
                    records_by_code.setdefault(seat.security_code, []).append(
                        normalize_module.normalize_seat_row(seat)
                    )

        def annotate_event(event_key, trading_day: str, context_days) -> dict[str, object]:
            """以事件日开头的五交易日窗口做标注；不给就不生成（不硬算）。"""
            last = context_days[-1]
            coverage_for_window = {
                day: (
                    _worst_coverage([state for _, state, _ in day_reports[day].values()])
                    if day in day_reports
                    else "unknown"
                )
                for day in context_days
            }
            result = annotator_module.annotate(
                security_code,
                records_by_code.get(security_code, []),
                list(context_days),
                coverage_for_window,
                as_of=last,
            )
            # ★ 三种结果必须区分（三者对读者的含义完全不同）：
            #   - 有事实 → `""`（无事发生）；
            #   - 窗口完整但没命中 → `no_fact_for_window`（「检查过了，没有」）；
            #   - 窗口本身不完整 → `window_incomplete`（「没法检查」，不是「没有」）。
            # 若把最后一种折进前一种，就等于用不完整输入冒充「已检查且无命中」。
            omitted = ""
            if not result.facts:
                omitted = (
                    "no_fact_for_window" if result.window_complete else "window_incomplete"
                )
            return {
                "fact_types": [fact.fact_type for fact in result.facts],
                "fact_ids": [fact.fact_id for fact in result.facts],
                "omitted": omitted,
            }

        try:
            page = replay_module.build_page(
                inputs,
                security_code,
                start,
                end,
                page_size=page_size,
                cursor=cursor,
                annotate_event=annotate_event,
                horizon_cache=_YOUZI_HORIZON_CACHE,
            )
        except replay_module.ReplayInputError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        payload = page.as_dict()
        payload["available"] = True
        payload["reason"] = ""
        payload["boundary_notice"] = boundary
        if truncated_days:
            # 预算护栏拦下的**回溯上限**：与逐日取数失败区分开。
            payload["truncated"] = True
            payload["truncated_reason"] = (
                f"区间内有 {len(in_range)} 个交易日，超过单次复盘上限 "
                f"{replay_module.MAX_EVENT_DAYS} 个；已保留**最近**的 {len(selected)} 个。"
                "请缩小日期范围后重试。"
            )
        payload["event_day_count"] = len(selected)
        payload["context_days"] = list(context_before)
        return payload

    @app.get("/youzi/today")
    def youzi_today(
        trading_day: str | None = None, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        """今日席位：当日龙虎榜上榜列表 + 观察名单命中。

        取不到就如实回报 `available=False` + `reason`，**不返回空表冒充「今天没上榜」**——
        这两件事对使用者意义完全不同（一个是我没拿到，一个是市场没有）。
        """
        _require_youzi(core)
        import investment_steward_core.lhb_feed as lhb_feed_module
        import investment_steward_core.seat_book as seat_book_module

        day = (trading_day or "").strip() or _recent_trade_days(core, 1)[0]
        try:
            rows = lhb_feed_module.fetch_billboard(day)
        except Exception as error:  # noqa: BLE001 - 取数失败如实降级
            return {
                "available": False,
                "trading_day": day,
                "reason": f"当日上榜列表未取得：{error}",
                "rows": [],
            }
        try:
            seats = lhb_feed_module.fetch_seats(day)
        except Exception:  # noqa: BLE001 - 席位明细失败仍可给出上榜列表
            seats = []
        watchlist = seat_book_module.load_watchlist()

        hits_by_code: dict[str, list[dict[str, object]]] = {}
        for seat in seats:
            match = seat_book_module.match_seat(
                watchlist,
                operatedept_code=str(getattr(seat, "operatedept_code", "")),
                operatedept_name=str(getattr(seat, "operatedept_name", "")),
            )
            if match.matched and match.seat is not None:
                hits_by_code.setdefault(str(getattr(seat, "security_code", "")), []).append({
                    "operatedept_code": match.seat.operatedept_code,
                    "name": match.seat.name,
                    "direction": str(getattr(seat, "direction", "")),
                    "net": getattr(seat, "net", None),
                })

        return {
            "available": True,
            "trading_day": day,
            "watchlist_version": watchlist.version,
            "rows": [
                {
                    "security_code": row.security_code,
                    "security_name": row.security_name,
                    "explanation": row.explanation,
                    "change_rate": row.change_rate,
                    "close_price": row.close_price,
                    "billboard_buy_amt": row.billboard_buy_amt,
                    "billboard_sell_amt": row.billboard_sell_amt,
                    "billboard_net_amt": row.billboard_net_amt,
                    "trade_market": row.trade_market,
                    # G03：榜单扩展字段透传（原口径单位；缺失为 None，不填 0）。
                    "turnover_rate": row.turnover_rate,
                    "turnover_rate_unit": "pct",
                    "free_market_cap": row.free_market_cap,
                    "free_market_cap_unit": "yuan",
                    # G03：可点回链（构造式，见 lhb_feed.provider_page_url；取不到为空串）。
                    "source_url": row.source_url,
                    "watchlist_hits": hits_by_code.get(row.security_code, []),
                }
                for row in rows
            ],
        }

    @app.get("/youzi/seats/{security_code}")
    def youzi_seats(
        security_code: str, trading_day: str | None = None, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        """单票买卖席位（点进今日席位列表后看的一层）。"""
        _require_youzi(core)
        import investment_steward_core.lhb_feed as lhb_feed_module
        import investment_steward_core.seat_book as seat_book_module

        code = normalize_instrument(security_code).key
        if not code:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="证券代码不合法")
        day = (trading_day or "").strip() or _recent_trade_days(core, 1)[0]
        try:
            seats = lhb_feed_module.fetch_seats(day)
        except Exception as error:  # noqa: BLE001
            return {"available": False, "trading_day": day, "reason": f"席位明细未取得：{error}", "buy": [], "sell": []}
        watchlist = seat_book_module.load_watchlist()

        def _side(direction: str) -> list[dict[str, object]]:
            picked = lhb_feed_module.seats_for_security(seats, code, direction)
            out: list[dict[str, object]] = []
            for seat in picked:
                match = seat_book_module.match_seat(
                    watchlist,
                    operatedept_code=str(getattr(seat, "operatedept_code", "")),
                    operatedept_name=str(getattr(seat, "operatedept_name", "")),
                )
                out.append({
                    "operatedept_code": seat.operatedept_code,
                    "operatedept_name": seat.operatedept_name,
                    "buy": seat.buy,
                    "sell": seat.sell,
                    "net": seat.net,
                    "is_bucket": (not match.matched) and match.reason.startswith("匿名汇总桶"),
                    "watchlist_hit": match.matched,
                    # G03：席位行回到原始披露页的可点回链（取不到为空串）。
                    "source_url": seat.source_url,
                })
            return out

        return {
            "available": True,
            "trading_day": day,
            "security_code": code,
            "buy": _side(lhb_feed_module.DIRECTION_BUY),
            "sell": _side(lhb_feed_module.DIRECTION_SELL),
        }

    @app.get("/youzi/profile/{operatedept_code}")
    def youzi_profile(
        operatedept_code: str, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        """席位档案：某营业部近 N 日出现过的股票、买卖方向、上榜原因。

        **只展示路径事实，不做胜率排行**；查询只走精确代码（数据源不支持 like）。
        """
        _require_youzi(core)
        import investment_steward_core.lhb_feed as lhb_feed_module
        import investment_steward_core.seat_book as seat_book_module

        code = (operatedept_code or "").strip()
        if not code:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="席位代码不可为空")
        try:
            records = lhb_feed_module.fetch_operatedept_history(code)
        except Exception as error:  # noqa: BLE001
            return {"available": False, "reason": f"席位档案未取得：{error}", "records": []}

        watchlist = seat_book_module.load_watchlist()
        entry = watchlist.by_code(code)
        return {
            "available": True,
            "operatedept_code": code,
            "is_watchlist": entry is not None,
            "watchlist_name": entry.name if entry else "",
            "records": [
                {
                    "trading_day": lhb_feed_module.normalise_trade_date(item.get("TRADE_DATE")),
                    "security_code": str(item.get("SECURITY_CODE") or ""),
                    "security_name": str(item.get("SECURITY_NAME_ABBR") or ""),
                    "buy": item.get("ACT_BUY"),
                    "sell": item.get("ACT_SELL"),
                    "net": item.get("NET_AMT"),
                    "explanation": str(item.get("EXPLANATION") or ""),
                }
                for item in records
            ],
            # U03（桌面端升级路线图 2026-09-18）：静默截断必须说出来。数据源写死
            # pageNumber=1/pageSize=50 且不回 total——取满一页时只能如实声明「可能还有更早记录」，
            # 不猜还有多少、不做无可信总数的翻页。
            "returned": len(records),
            "page_size": lhb_feed_module.OPERATEDEPT_PAGE_SIZE,
            "possibly_truncated": len(records) >= lhb_feed_module.OPERATEDEPT_PAGE_SIZE,
            "note": "只展示披露路径事实；不提供胜率排行，也不构成买卖建议。",
        }

    @app.get("/youzi/curriculum")
    def youzi_curriculum(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """课程单一数据源（U05，桌面端升级路线图 2026-09-18）：读插件目录里的 curriculum.json。

        前端不再手抄 LESSONS 副本（9/11 条 exercise 文案已与 JSON 漂移）。路径解析复用
        seat_book 的 STEWARD_PLUGIN_REGISTRY_DIR + 仓库回退那一套——**不再自己拼第二份路径**，
        避免重蹈打包形态 `parents[4]` 指错目录导致 /youzi/watchlist 500 的真机事故。
        """
        _require_youzi(core)
        import investment_steward_core.seat_book as seat_book_module

        path = seat_book_module.default_watchlist_path().with_name("curriculum.json")
        if not path.exists():
            return {"ok": False, "reason": f"课程文件不存在：{path}"}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return {"ok": False, "reason": f"课程文件读取/解析失败：{error}"}
        if not isinstance(payload, dict) or not isinstance(payload.get("lessons"), list):
            return {"ok": False, "reason": "课程文件形状不符（缺少 lessons 数组）"}
        return {
            "ok": True,
            "curriculum": payload,
            "source": "plugins/official/youzi-radar/curriculum.json",
        }

    @app.get("/youzi/watchlist")
    def youzi_watchlist(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """观察名单（席位 + 桶）与说明文本。"""
        _require_youzi(core)
        import investment_steward_core.seat_book as seat_book_module

        watchlist = seat_book_module.load_watchlist()
        return {
            "version": watchlist.version,
            "disclaimer": watchlist.disclaimer,
            "description": seat_book_module.describe_watchlist(watchlist),
            "seats": [
                {
                    "operatedept_code": seat.operatedept_code,
                    "name": seat.name,
                    "last_seen_trade_date": seat.last_seen_trade_date,
                    "record_count": seat.record_count,
                    "note": seat.note,
                }
                for seat in watchlist.seats
            ],
            "buckets": [{"name": seat.name, "note": seat.note} for seat in watchlist.buckets],
        }

    @app.get("/youzi/cross-check")
    def youzi_cross_check(
        trading_day: str | None = None, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        """P4-E01：持仓/自选与今日上榜的**交叉提示**。

        **只提示「今日上榜」这一事实，不提示买。** 返回体里刻意不含任何动作词或
        买卖建议字段；命中的股票只附「上榜原因 + 买卖净额 + 是否有观察席位参与」，
        由用户自己决定这意味着什么。

        这是本机数据（持仓/自选）与公开披露（龙虎榜）的**取交集**，不外发用户持仓：
        请求只带交易日；比对在本地完成。
        """
        _require_youzi(core)
        import investment_steward_core.lhb_feed as lhb_feed_module
        import investment_steward_core.seat_book as seat_book_module

        day = (trading_day or "").strip() or _recent_trade_days(core, 1)[0]

        # 本机持仓 + 自选（两者都算「我关心的标的」）。
        held = core.database.list_holdings(core.local_user_id)
        by_code: dict[str, dict[str, str]] = {}
        for item in held:
            code = normalize_instrument(item.instrument).key
            if not code:
                continue
            entry = by_code.setdefault(code, {"code": code, "labels": [], "statuses": []})
            if item.label not in entry["labels"]:
                entry["labels"].append(item.label)
            status = getattr(item.status, "value", str(item.status))
            if status not in entry["statuses"]:
                entry["statuses"].append(status)

        if not by_code:
            return {
                "available": True,
                "trading_day": day,
                "checked": 0,
                "hits": [],
                "note": "本机没有持仓或自选，无从比对；这不是「今日无人上榜」。",
            }

        try:
            rows = lhb_feed_module.fetch_billboard(day)
        except Exception as error:  # noqa: BLE001 - 取数失败如实降级
            return {
                "available": False,
                "trading_day": day,
                "checked": len(by_code),
                "reason": f"当日上榜列表未取得：{error}",
                "hits": [],
            }

        watchlist = seat_book_module.load_watchlist()
        try:
            seats = lhb_feed_module.fetch_seats(day)
        except Exception:  # noqa: BLE001 - 席位明细失败不影响「是否上榜」的判断
            seats = []
        hit_codes_with_watch: set[str] = set()
        for seat in seats:
            if str(getattr(seat, "security_code", "")) not in by_code:
                continue
            match = seat_book_module.match_seat(
                watchlist,
                operatedept_code=str(getattr(seat, "operatedept_code", "")),
                operatedept_name=str(getattr(seat, "operatedept_name", "")),
            )
            if match.matched:
                hit_codes_with_watch.add(str(getattr(seat, "security_code", "")))

        hits: list[dict[str, object]] = []
        for row in rows:
            entry = by_code.get(row.security_code)
            if entry is None:
                continue
            hits.append({
                "security_code": row.security_code,
                "security_name": row.security_name,
                "labels": entry["labels"],
                "statuses": entry["statuses"],
                "explanation": row.explanation,
                "billboard_net_amt": row.billboard_net_amt,
                "watchlist_hit": row.security_code in hit_codes_with_watch,
                # G03：可点回链（构造式；取不到为空串，不猜 URL）。
                "source_url": row.source_url,
            })

        return {
            "available": True,
            "trading_day": day,
            "checked": len(by_code),
            "hits": hits,
            "note": (
                "仅提示「该标的今日出现在龙虎榜」这一披露事实，不含任何买卖建议；"
                "未上榜不表示没有资金关注，只表示未触发上榜标准。"
            ),
        }

    @app.get("/youzi/cffex")
    def youzi_cffex(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """指数席位（D05 对照页）：中金所股指期货会员排名。

        由 `cffex_feed` 产出；取不到就 `available=False` + `reason`，不编造席位数字。
        """
        _require_youzi(core)
        day = _recent_trade_days(core, 1)[0]
        snapshot, reason = cffex_feed.try_fetch_snapshot(day, spot_closes=_cffex_spot_closes(core))
        if snapshot is None:
            return {"available": False, "trading_day": day, "reason": reason, "products": []}
        return {
            "available": True,
            "trading_day": snapshot.trading_day,
            "text": cffex_feed.describe_snapshot(snapshot),
            "products": [
                {
                    "product_id": product.product_id,
                    "spot_index_code": product.spot_index_code,
                    "contract_count": product.contract_count,
                    "total_volume": product.total_volume,
                    "total_open_interest": product.total_open_interest,
                }
                for product in snapshot.products
            ],
            "basis": snapshot.basis,
            "basis_note": snapshot.basis_note,
            "limitations": list(snapshot.limitations),
        }

    @app.get("/tactics/sectors")
    def tactics_sectors(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """行业板块清单（v26「按板块划分」的选项来源）：新浪行业板块，按当日涨跌幅降序。

        返回 code/name/member_count/change_pct/leader_*；前端据此做板块多选，
        再用 mode=sectors 扫描（结果按板块分组）。缓存 5 分钟（行业分类变动以季度计）。
        """
        try:
            rows = fetch_cn_sector_list()
        except FeedError as error:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"行业板块清单取数失败：{error}") from error
        return {
            "sectors": rows,
            "generated_at": datetime.now(UTC).isoformat(),
            "source": "新浪财经 行业板块",
        }

    def _tactic_bars(core: CoreState, symbol: str, limit: int = 250) -> tuple[list[dict[str, object]], str]:
        """战法取数：与行情端点同一闸门（cn-market-data 必须启用），同一数据源。"""
        installation = core.database.get_plugin_installation("official.cn-market-data")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="cn-market-data plugin is not enabled"
            )
        bars, provider = fetch_cn_kline(symbol, limit=limit, period="day")
        return bars, provider

    def _tactic_score_row(
        bars: list[dict[str, object]],
        snap: dict[str, object],
        recent_bars: int,
        sector_context: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """v24 共用（/tactics/scan 与 /tactics/scan-market 同一口径）：单票质量分与命中明细。

        命中窗口口径修正：recent_bars 是「最近 N 根 K 线」，按 bars[-N] 的**日期下界**过滤信号；
        v23 的 `signals[-N:]` 取的是「最近 N 条信号」，同一参数在不同票上时间跨度不一致（已修）。
        评分分项见 tactics_score 模块：形态权重 × 新鲜度 + 同向跨类共振 − 多空对冲扣减，
        v26 起再加 同族合并 / 量能确认 / 位置 / 板块共振（sector_context 仅在板块扫描时注入）。
        """
        quality = tactics_score.score_for_bars(snap, bars, recent_bars, sector_context=sector_context)
        window = quality.pop("window_signals")
        return {**quality, "latest_signals": window}

    def _tactics_rank_key(row: dict[str, object]) -> tuple[object, ...]:
        """v28（二次方案 §3.6）默认榜单排序键：先过硬门，再按综合分 rank_score。

        `rank_score = tactic_score × 数据质量因子`；数据齐全（quality=ok）时因子为 1.0，
        与既有 tactic_score 完全相等 → **既有榜单的相对次序不变**（向后兼容）。
        被硬门挡下（K 线字段缺失 / 日期异常）或数据不足的行 `eligible=False` 沉到榜尾，
        但仍随结果返回并带 `gating_reasons`，前端可筛选查看（不静默丢弃）。
        取数失败的行（ok=False）同样沉底。
        """
        ok = bool(row.get("ok"))
        eligible = bool(row.get("eligible")) if ok else False
        rank = row.get("rank_score")
        rank_value = float(rank) if ok and isinstance(rank, (int, float)) else -1.0
        tactic = row.get("tactic_score")
        tactic_value = float(tactic) if ok and isinstance(tactic, (int, float)) else -1.0
        hit_raw = row.get("hit_tactics")
        hits = len(hit_raw) if ok and isinstance(hit_raw, list) else 0
        return (1 if eligible else 0, rank_value, tactic_value, hits)

    @app.get("/tactics/signals/{symbol}")
    def tactics_signals(symbol: str, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """单票战法全景：指标现值 + 全部近期信号（供 K 线标注与详情页）。"""
        try:
            bars, provider = _tactic_bars(core, symbol)
        except FeedError as error:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"K 线取数失败：{error}") from error
        snap = stock_tactics.snapshot(bars)
        # v24：本票质量分按 DETAIL_WINDOW_BARS 根 K 线的窗口计算（与扫描同一口径，可横向比较）。
        # 不用「全部 K 线」当窗口：新鲜度是固定半衰期，长窗口不会虚高，但窗口语义必须与扫描一致。
        quality = tactics_score.score_for_bars(snap, bars, tactics_score.DETAIL_WINDOW_BARS)
        quality.pop("window_signals", None)
        return {
            "symbol": symbol,
            "provider": provider,
            "generated_at": datetime.now(UTC).isoformat(),
            "score": quality,
            # 原始 K 线（紧凑格式，供前端图表面信号标注）；与 signals 同一数据源。
            "bars": [
                {
                    "time": str(bar["timestamp"])[:10],
                    "o": float(bar["open"]), "h": float(bar["high"]),
                    "l": float(bar["low"]), "c": float(bar["close"]),
                    "v": float(bar["volume"]),
                }
                for bar in bars
            ],
            **snap,
        }

    @app.post("/tactics/scan")
    def tactics_scan(body: TacticsScanRequest, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """按「观察清单 + 持仓 + 手动列表」扫描战法命中；逐票取数、单票失败不拖垮整体。

        命中口径（v24）：**最近 recent_bars 根 K 线**内出现该战法信号（按 K 线日期下界过滤）。
        排序主键 = 战法质量分 tactic_score（形态权重 × 新鲜度 + 同向跨类共振 − 多空对冲），
        score_parts / hit_detail 逐项可解释、可复算；信号只描述图形，不构成买卖建议。
        """
        # 1. 组装候选池（去重保序）：观察清单 → 持仓 → 手动列表。
        candidates: list[tuple[str, str]] = []  # (symbol, name)
        seen: set[str] = set()

        def _push(symbol: str, name: str) -> None:
            key = symbol.strip()
            if not key or key in seen:
                return
            seen.add(key)
            candidates.append((key, name))

        if "watchlist" in body.sources:
            for entry in core.database.list_tactics_watchlist():
                _push(str(entry["symbol"]), str(entry.get("name") or ""))
        if "holdings" in body.sources:
            for holding in core.database.list_holdings(core.local_user_id):
                _push(holding.instrument, holding.label)
        if "manual" in body.sources:
            for symbol in body.symbols:
                _push(symbol, "")
        # JV05：本端点走的是**自己的**扫描循环（不是 `_scan_run_engine`——那只服务市场扫描的
        # boards/all/sectors 三模式），所以必须在这里单独接一次。真机验证时才发现漏了它：
        # 「观察清单 + 持仓 + 手动列表」是 2026-09-07 拍板的**日常主用范围**，不接等于
        # 「扫描去误报」没覆盖用户最常跑的那条路。口径与 `_execute_scan_market` 完全一致：
        # `reviewer` 为 None 时不采集 K 线摘要、标注一个都不加。
        reviewer = jev_client.build_shard_reviewer(
            core, resolve_store(core.database), purpose="jev:scan-review"
        )

        if not candidates:
            # 形状与主路径保持一致（前端按 `jev_review?.enabled` 判断，缺键会让它分不清
            # 「没跑」和「没票可评」）。
            return {
                "results": [],
                "scanned": 0,
                "jev_review": _apply_jev_scan_review([], reviewer),
                "generated_at": datetime.now(UTC).isoformat(),
            }

        results: list[dict[str, object]] = []
        for index, (symbol, name) in enumerate(candidates):
            if index > 0:
                time.sleep(0.15)  # 公开行情接口限速礼貌间隔
            try:
                bars, provider = _tactic_bars(core, symbol)
            except HTTPException as error:
                results.append({"symbol": symbol, "name": name, "ok": False, "error": str(error.detail)})
                continue
            except FeedError as error:
                results.append({"symbol": symbol, "name": name, "ok": False, "error": f"K 线取数失败：{error}"})
                continue
            snap = stock_tactics.snapshot(bars)
            scored = _tactic_score_row(bars, snap, body.recent_bars)
            row: dict[str, object] = {
                "symbol": symbol,
                "name": name,
                "ok": True,
                "provider": provider,
                "indicators": snap["indicators"],
                "bars_count": snap["bars_count"],
                **scored,
            }
            if reviewer is not None:
                # 与 `_scan_run_engine` 同口径：在**拉到 K 线的同一处**顺带算出摘要，不另起一遍取数。
                row["_jev_bars_tail"] = tactics_ai.jev_scan_bars_tail(bars)
            results.append(row)
        # v24：排序主键改为战法质量分（不再用命中数量——十字星等恒命中形态会稀释该指标）。
        # v28：改用共享门槛键（先过硬门再按 rank_score）；数据齐全时与 tactic_score 同序，向后兼容。
        results.sort(key=_tactics_rank_key, reverse=True)
        # 排在**排序之后**：分诊只加标注，不参与排序（软校验铁律），且上限内优先复核靠前的票。
        jev_review = _apply_jev_scan_review(results, reviewer)
        return {
            "results": results,
            "scanned": len(candidates),
            "recent_bars": body.recent_bars,
            "weight_version": stock_tactics.TACTIC_WEIGHT_VERSION,
            "score_formula_version": tactics_score.SCORE_FORMULA_VERSION,
            "gate_version": tactics_score.GATE_VERSION,
            "jev_review": jev_review,
            "generated_at": datetime.now(UTC).isoformat(),
        }

    # —— D01（桌面端升级路线图 2026-09-18）：市场扫描任务化 ——
    # 原 endpoint 的同步执行体提取为 create_app 作用域的函数（闭包 _tactic_bars/_tactic_score_row/
    # _tactics_rank_key），供后台线程复用；progress 逐票上报 done/total 并检测取消标记。

    def _scan_run_engine(
        core: CoreState,
        body: TacticsMarketScanRequest,
        candidates: list[tuple[str, str]],
        *,
        sector_context: dict[str, object] | None = None,
        sector_meta: tuple[str, str] | None = None,
        progress: _ScanProgress | None = None,
        count_progress: bool = True,
        collect_jev_material: bool = False,
    ) -> list[dict[str, object]]:
        """第二段公共实现：逐票拉 K 线跑战法引擎（限速礼貌间隔）。

        body.tactic_ids 非空时只跑所选战法（市场雷达战法选择）。
        sector_context/sector_meta：板块扫描时注入板块横截面共振与板块归属（单票扫描不传）。
        progress：D01 任务化进度——每票前检测取消标记，每票后上报 done（count_progress=False
        用于板块共振的二次重算：同批票不重复计数，但取消检测仍然生效）。
        collect_jev_material：JV05——为真时在拉到 K 线的**同一处**顺带算出「进 state 的尾部
        K 线摘要」暂存到 `_jev_bars_tail`。为什么不另起一遍取数：那等于把扫描的取数成本
        翻倍，而扫描本就是分钟级任务。默认 False ⇒ **Jev 未启用时扫描主链路零变化**。
        """
        selected_ids = set(body.tactic_ids) if body.tactic_ids else None
        results: list[dict[str, object]] = []
        interval = max(0.0, min(body.interval_secs, 2.0))
        concurrency = max(1, min(body.concurrency, 4))

        def _process_one(symbol: str, name: str, *, stagger: bool) -> dict[str, object]:
            if progress is not None:
                progress.raise_if_cancelled()
            if stagger and interval > 0:
                time.sleep(interval)
            try:
                bars, provider = _tactic_bars(core, symbol)
            except HTTPException as error:
                return {"symbol": symbol, "name": name, "ok": False, "error": str(error.detail)}
            except FeedError as error:
                return {"symbol": symbol, "name": name, "ok": False, "error": f"K 线取数失败：{error}"}
            snap = stock_tactics.snapshot(bars, tactic_ids=selected_ids)
            scored = _tactic_score_row(bars, snap, body.recent_bars, sector_context=sector_context)
            row: dict[str, object] = {
                "symbol": symbol,
                "name": name,
                "ok": True,
                "provider": provider,
                "indicators": snap["indicators"],
                "bars_count": snap["bars_count"],
                **scored,
            }
            if collect_jev_material:
                row["_jev_bars_tail"] = tactics_ai.jev_scan_bars_tail(bars)
            if sector_meta is not None:
                row["sector_code"] = sector_meta[0]
                row["sector_name"] = sector_meta[1]
            return row

        if concurrency > 1:
            # D03：并发模式（≤4）——K 线取数为 I/O，线程池即够；结果按提交顺序回填，
            # 进度/取消语义与串行一致（tick 在每票完成后调用）。
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="scan-engine") as pool:
                futures = [pool.submit(_process_one, symbol, name, stagger=True) for symbol, name in candidates]
                for future in futures:
                    row = future.result()
                    results.append(row)
                    if progress is not None and count_progress:
                        progress.tick()
            return results

        for index, (symbol, name) in enumerate(candidates):
            if index > 0 and interval > 0:
                time.sleep(interval)
            row = _process_one(symbol, name, stagger=False)
            results.append(row)
            if progress is not None and count_progress:
                progress.tick()
        return results

    def _apply_jev_scan_review(
        results: list[dict[str, object]],
        reviewer: Callable[[Mapping[str, Any]], list[dict[str, Any]]] | None,
    ) -> dict[str, object]:
        """JV05：把扫描结果送 Jev 做批量语义复核，逐票标注 + 返回汇总块。

        **软校验铁律**（与 `report_quality` / `tactics_ai` 同源）：只**加标注字段**，
        绝不删除、不重排、不改写任何候选——判为「疑似误报」的票仍然在 `results` 里，
        只是带上 `jev_review_state="pending_verification"`，前端据此折叠进「待核验」。

        `reviewer is None`（总闸关闭 / Jev 未启用）时**一个字段都不加**，返回
        `{"enabled": False, ...}`；调用方（`_execute_scan_market`）此时也不会要求引擎
        采集 K 线摘要，扫描响应与接入前逐字节一致（JV00 铁律 6）。

        只送 `ok=True` 的票：取数失败的行没有命中明细可判，送过去只会拿到「证据不足」
        这类噪声结论，还白烧 token。`results` 已按 `_tactics_rank_key` 排好序，因此
        在 `JEV_SCAN_MAX_CANDIDATES` 上限内**优先复核的是排名靠前的票**——这正是
        「去误报」要保护的对象（榜单头部）。
        """
        # 先剥掉引擎暂存的 K 线摘要：无论走哪条分支，它都不该出现在 API 响应里。
        material: dict[str, str] = {}
        for row in results:
            tail = row.pop("_jev_bars_tail", None)
            if isinstance(tail, str) and tail:
                material[str(row.get("symbol") or "")] = tail

        ok_rows = [row for row in results if row.get("ok") is True]
        if reviewer is None:
            return {
                "enabled": False,
                "model": None,
                "total": 0,
                "reviewed": 0,
                "pending": 0,
                "unannotated": 0,
                "verdicts": {"valid": 0, "doubtful": 0, "invalid": 0},
                "skipped": [],
                "elapsed_ms": 0,
                "note": "Jev 未启用或出网总闸关闭，本轮未做语义复核（结果未标注，不等于已核验）",
            }

        started = time.monotonic()
        items = [
            {
                "symbol": str(row.get("symbol") or ""),
                "name": str(row.get("name") or ""),
                "row": row,
                "bars_tail": material.get(str(row.get("symbol") or ""), ""),
            }
            for row in ok_rows
        ]
        bundle = tactics_ai.jev_scan_shards(items)
        shard_results = reviewer(bundle) if bundle.get("shards") else []
        annotations = tactics_ai.jev_scan_annotations(shard_results)

        row_by_symbol = {str(row.get("symbol") or ""): row for row in ok_rows}
        for symbol, annotation in annotations.items():
            row = row_by_symbol.get(symbol)
            if row is None:
                continue
            row["jev_verdict"] = annotation.get("verdict")
            row["jev_label"] = annotation.get("label")
            row["jev_confidence"] = annotation.get("confidence")
            row["jev_review_state"] = annotation.get("review_state")
            row["jev_note"] = annotation.get("note")

        # 被分片策略跳过的票（超单轮上限 / 单票超预算）：如实标注原因，不留空白的「没标注」。
        for skipped in bundle.get("skipped") or []:
            row = row_by_symbol.get(str(skipped.get("symbol") or ""))
            if row is None:
                continue
            row["jev_verdict"] = None
            row["jev_label"] = "未送评"
            row["jev_confidence"] = None
            row["jev_review_state"] = None
            row["jev_note"] = str(skipped.get("note") or "")

        stats = tactics_ai.jev_scan_stats(ok_rows)
        models = {str(item.get("model")) for item in shard_results if item.get("model")}
        return {
            "enabled": True,
            "model": "/".join(sorted(models)) if models else None,
            "total": stats["total"],
            "reviewed": stats["reviewed"],
            "pending": stats["pending"],
            "unannotated": stats["unannotated"],
            "verdicts": stats["verdicts"],
            "skipped": list(bundle.get("skipped") or []),
            "shards": len(shard_results),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "note": (
                "语义复核只做标注与分组，不改判定、不删除候选；"
                "「未标注」表示本票未取得判定，不等于形态成立"
            ),
        }

    def _execute_scan_market(
        core: CoreState, body: TacticsMarketScanRequest, progress: _ScanProgress | None = None
    ) -> dict[str, object]:
        """市场批量扫描执行体（D01：从同步 endpoint 提取，三模式口径与原实现逐字一致）。

        D03：summary 额外携带 `elapsed_ms`（实际耗时）与 `failures`（失败票清单：
        symbol/name/error——前端任务详情据此展示「多少票取数失败、失败原因分布」）。
        JV05：三模式统一在**这里**接一层 Jev 语义复核（而不是分别改三个模式的返回路径），
        汇总落 `summary["jev_review"]`、逐票标注落在各 row 上。`elapsed_ms` 仍只计扫描段，
        Jev 段耗时单列在 `jev_review.elapsed_ms`——两段耗时混在一起就没法判断是哪一段慢。
        """
        started = time.monotonic()
        reviewer = jev_client.build_shard_reviewer(
            core, resolve_store(core.database), purpose="jev:scan-review"
        )
        summary = _execute_scan_market_inner(
            core, body, progress, collect_jev_material=reviewer is not None
        )
        summary["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        summary["failures"] = [
            {"symbol": str(row.get("symbol")), "name": str(row.get("name")), "error": str(row.get("error") or "")}
            for row in summary.get("results", [])
            if row.get("ok") is False
        ]
        summary["jev_review"] = _apply_jev_scan_review(list(summary.get("results") or []), reviewer)
        return summary

    def _execute_scan_market_inner(
        core: CoreState,
        body: TacticsMarketScanRequest,
        progress: _ScanProgress | None = None,
        *,
        collect_jev_material: bool = False,
    ) -> dict[str, object]:
        """三模式扫描执行体（原实现，见 _execute_scan_market 的口径说明）。

        `collect_jev_material` 由 `_execute_scan_market` 按「Jev 是否可用」传入并透传给
        三个模式的 `_scan_run_engine`——三种模式**都要**能供 JV05 复核，否则「全市场扫描
        去误报」在板块/榜单模式下静默失效。
        """
        if body.mode == "all":
            # 全市场模式：分页遍历 → 板块过滤 → 快照趋势预筛 → 预筛靠前者跑引擎。
            segment_prefixes = {
                "main": ("600", "601", "603", "605", "000", "001", "002", "003"),
                "chinext": ("300", "301"),
                "star": ("688", "689"),
            }
            valid_segments = [s for s in body.segments if s in segment_prefixes] or list(segment_prefixes)
            allowed = tuple(p for s in valid_segments for p in segment_prefixes[s])
            try:
                rows, complete = fetch_cn_market_full_a(max_pages=body.max_pages)
            except FeedError as error:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"全市场遍历失败：{error}") from error
            rows = [row for row in rows if str(row["symbol"]).startswith(allowed)]
            # 价格区间过滤（快照价；边界无效时如实忽略：min>max 视为未设限）
            price_min = body.price_min
            price_max = body.price_max
            if price_min is not None and price_max is not None and price_min > price_max:
                price_min, price_max = None, None

            def _in_price_range(row: dict[str, object]) -> bool:
                price = row.get("price")
                if not isinstance(price, (int, float)):
                    return False
                if price_min is not None and float(price) < price_min:
                    return False
                if price_max is not None and float(price) > price_max:
                    return False
                return True

            rows = [row for row in rows if _in_price_range(row)]
            ranked = score_market_rows(rows)[: body.top_symbols]
            candidates = [(str(row["symbol"]), str(row["name"])) for row in ranked]
            score_by_symbol = {str(row["symbol"]): row for row in ranked}
            if progress is not None:
                progress.add_total(len(candidates))
            results = _scan_run_engine(
                core, body, candidates, progress=progress, collect_jev_material=collect_jev_material
            )
            # mode=all：成功行附快照趋势预筛分（0-100，快照口径粗筛，仅作参考展示）
            for row in results:
                extra = score_by_symbol.get(str(row["symbol"]))
                if row["ok"] and extra is not None:
                    row["trend_score"] = extra.get("trend_score")
                    row["trend_parts"] = extra.get("trend_parts")
            # v24：最终排序主键 = 战法质量分；trend_score 降为次级键（快照口径与战法质量无关）
            # v28：先过硬门（eligible）+ rank_score；trend_score 仅在同等时作参考次级键。
            def _market_key(row: dict[str, object]) -> tuple[object, ...]:
                base = _tactics_rank_key(row)
                trend = row.get("trend_score")
                trend_value = float(trend) if isinstance(trend, (int, float)) else -1.0
                return (*base, trend_value)

            results.sort(key=_market_key, reverse=True)
            return {
                "results": results,
                "scanned": len(candidates),
                "mode": "all",
                "market_rows": len(rows),
                "complete": complete,
                "segments": valid_segments,
                "recent_bars": body.recent_bars,
                "weight_version": stock_tactics.TACTIC_WEIGHT_VERSION,
                "score_formula_version": tactics_score.SCORE_FORMULA_VERSION,
                "gate_version": tactics_score.GATE_VERSION,
                "generated_at": datetime.now(UTC).isoformat(),
            }

        if body.mode == "sectors":
            # 板块模式（v26「按板块划分、从板块股里挑」）：
            # 选行业板块 → 拉成分股 → 逐票跑引擎（基础分）→ 板块内横截面共振 → 重算 → 按板块分组返回。
            # 重算复用 90s K 线缓存，不会重复联网；某板块取数失败如实标注，不拖垮整轮。
            valid_sectors = [s.strip() for s in body.sectors if s.strip()][:8]
            if not valid_sectors:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="sectors 为空：请先 GET /tactics/sectors 取行业板块代码",
                )
            try:
                catalog = {str(item["code"]): item for item in fetch_cn_sector_list()}
            except FeedError:
                catalog = {}
            blocks: list[dict[str, object]] = []
            all_results: list[dict[str, object]] = []
            for code in valid_sectors:
                if progress is not None:
                    progress.raise_if_cancelled()
                meta = catalog.get(code) or {}
                sector_name = str(meta.get("name") or code)
                try:
                    rows = fetch_cn_sector_members(code, top=body.per_sector)
                except FeedError as error:
                    blocks.append({
                        "code": code, "name": sector_name, "ok": False,
                        "error": f"板块成分股取数失败：{error}", "scanned": 0,
                    })
                    continue
                candidates = [(str(row["symbol"]), str(row["name"])) for row in rows]
                if progress is not None:
                    progress.add_total(len(candidates))
                results = _scan_run_engine(
                    core, body, candidates, sector_meta=(code, sector_name),
                    progress=progress, collect_jev_material=collect_jev_material,
                )
                # 横截面共振：板块内同向占比达阈值 → 给该板块内个股重算（含 sector_bonus）
                resonance: dict[str, object] | None = None
                if body.sector_resonance:
                    contexts = tactics_score.sector_contexts(
                        [row for row in results if row.get("ok")]
                    )
                    resonance = contexts.get("bullish") or contexts.get("bearish")
                    if resonance is not None:
                        results = _scan_run_engine(
                            core, body, candidates, sector_context=resonance, sector_meta=(code, sector_name),
                            progress=progress, count_progress=False,
                            collect_jev_material=collect_jev_material,
                        )
                ok_rows = [row for row in results if row.get("ok")]
                blocks.append({
                    "code": code,
                    "name": sector_name,
                    "ok": True,
                    "change_pct": meta.get("change_pct"),
                    "member_count": meta.get("member_count"),
                    "leader_name": meta.get("leader_name"),
                    "leader_symbol": meta.get("leader_symbol"),
                    "scanned": len(ok_rows),
                    "failed": len(results) - len(ok_rows),
                    "bullish_count": sum(1 for row in ok_rows if row.get("direction_bias") == "bullish"),
                    "bearish_count": sum(1 for row in ok_rows if row.get("direction_bias") == "bearish"),
                    "resonance": resonance,
                })
                all_results.extend(results)
            # v28：板块模式同样先过硬门再按 rank_score（与 /tactics/scan 同一门槛键）。
            all_results.sort(key=_tactics_rank_key, reverse=True)
            return {
                "results": all_results,
                "scanned": len([row for row in all_results if row.get("ok")]),
                "mode": "sectors",
                "sectors": blocks,
                "recent_bars": body.recent_bars,
                "weight_version": stock_tactics.TACTIC_WEIGHT_VERSION,
                "score_formula_version": tactics_score.SCORE_FORMULA_VERSION,
                "gate_version": tactics_score.GATE_VERSION,
                "sector_resonance": body.sector_resonance,
                "generated_at": datetime.now(UTC).isoformat(),
            }

        valid_boards = [b for b in body.boards if b in ("turnover", "gainers", "losers", "turnover_rate")]
        if not valid_boards:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="boards 为空或全部非法")

        # 第一段：榜单快照粗筛（60s 模块级缓存，连点不重放）
        candidates: list[tuple[str, str]] = []
        seen: set[str] = set()
        board_snapshots: dict[str, list[dict[str, object]]] = {}
        for board in valid_boards:
            try:
                rows = fetch_cn_market_board(board, top=body.per_board)
            except FeedError as error:
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"榜单 {board} 拉取失败：{error}") from error
            board_snapshots[board] = rows
            for row in rows:
                symbol = str(row["symbol"])
                if symbol not in seen:
                    seen.add(symbol)
                    candidates.append((symbol, str(row["name"])))
        candidates = candidates[: body.max_symbols]

        # 第二段：逐票拉 K 线跑引擎（与 /tactics/scan 同一取数与限速礼貌间隔）
        if progress is not None:
            progress.add_total(len(candidates))
        results = _scan_run_engine(
            core, body, candidates, progress=progress, collect_jev_material=collect_jev_material
        )
        # v24：排序主键 = 战法质量分（形态权重 × 新鲜度 + 共振 − 对冲），不再按命中数量。
        # v28：共享门槛键（先过硬门再按 rank_score）。
        results.sort(key=_tactics_rank_key, reverse=True)
        return {
            "results": results,
            "scanned": len(candidates),
            "boards": valid_boards,
            "mode": "boards",
            "recent_bars": body.recent_bars,
            "weight_version": stock_tactics.TACTIC_WEIGHT_VERSION,
            "score_formula_version": tactics_score.SCORE_FORMULA_VERSION,
            "gate_version": tactics_score.GATE_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
        }

    def _scan_market_job_worker(core: CoreState, body: TacticsMarketScanRequest, job_id: str) -> None:
        """D01 后台执行线程：进度写库、取消检测、终态落账；线程异常不外泄。"""
        progress = _ScanProgress(core.database, job_id)
        try:
            summary = _execute_scan_market(core, body, progress=progress)
            core.database.finish_scan_job(job_id, "done", summary)
        except _ScanCancelled:
            core.database.finish_scan_job(job_id, "cancelled", None)
        except Exception as error:  # noqa: BLE001
            core.database.finish_scan_job(job_id, "error", {"detail": str(error)})

    @app.post("/tactics/scan-market")
    def tactics_scan_market_start(body: TacticsMarketScanRequest, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """市场批量扫描任务化入口（D01，桌面端升级路线图 2026-09-18）。

        同步执行改为后台任务：POST 只建任务并立即返回 job_id；进度经
        GET /tactics/scan-market/{job_id} 轮询（done/total 逐票推进），取消走
        POST /tactics/scan-market/{job_id}/cancel（逐票检测，停止后不再发新取数）。
        同输入指纹（请求体规范化 JSON 的 SHA-256）的运行中任务直接命中，不重复计算。
        执行口径（三模式/排序/版本号）与原同步实现逐字一致。
        """
        installation = core.database.get_plugin_installation("official.cn-market-data")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="cn-market-data plugin is not enabled"
            )
        # 入参快速校验（口径与执行体一致）：明显非法的请求快速失败，而不是变成一个失败任务。
        if body.mode == "sectors" and not [s.strip() for s in body.sectors if s.strip()][:8]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="sectors 为空：请先 GET /tactics/sectors 取行业板块代码",
            )
        if body.mode not in ("all", "sectors"):
            valid_boards = [b for b in body.boards if b in ("turnover", "gainers", "losers", "turnover_rate")]
            if not valid_boards:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="boards 为空或全部非法")
        fingerprint = hashlib.sha256(body.model_dump_json().encode("utf-8")).hexdigest()
        running = core.database.find_running_scan_job(fingerprint)
        if running is not None:
            return {
                "ok": True,
                "job_id": running["job_id"],
                "state": "running",
                "reused": True,
                "done": running["done"],
                "total": running["total"],
            }
        job_id = str(uuid4())
        core.database.create_scan_job(job_id, fingerprint, body.model_dump(mode="json"))
        threading.Thread(
            target=_scan_market_job_worker, args=(core, body, job_id), name=f"scan-job-{job_id[:8]}", daemon=True
        ).start()
        return {"ok": True, "job_id": job_id, "state": "running", "reused": False}

    @app.get("/tactics/scan-market/{job_id}")
    def tactics_scan_market_status(job_id: str, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """D01：扫描任务进度/结果。running 返回 done/total；done 时 summary 为完整结果
        （形状与原同步响应一致）；cancelled/error 各带原因。running 超 30 分钟无心跳按中断呈现。"""
        job = core.database.get_scan_job(job_id)
        if job is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="扫描任务不存在")
        return {"ok": True, **job}

    @app.post("/tactics/scan-market/{job_id}/cancel")
    def tactics_scan_market_cancel(job_id: str, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """D01：取消运行中的扫描任务（逐票检测；已结束任务返回 ok=False）。"""
        return {"ok": core.database.cancel_scan_job(job_id)}

    @app.get("/tactics/watchlist")
    def tactics_watchlist(core: CoreState = Depends(_require_session)) -> list[dict[str, object]]:
        return core.database.list_tactics_watchlist()

    @app.put("/tactics/watchlist/{symbol}")
    def tactics_watch_upsert(
        symbol: str, body: TacticsWatchUpsertRequest, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        core.database.upsert_tactics_watch_entry(symbol.strip(), body.name.strip(), body.note.strip())
        _audit(core, "tactics.watch.updated", "tactics_watchlist", symbol.strip())
        return {"symbol": symbol.strip(), "name": body.name.strip(), "note": body.note.strip()}

    @app.delete("/tactics/watchlist/{symbol}", status_code=status.HTTP_204_NO_CONTENT)
    def tactics_watch_delete(symbol: str, core: CoreState = Depends(_require_session)) -> None:
        if not core.database.delete_tactics_watch_entry(symbol.strip()):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="watch entry not found")
        _audit(core, "tactics.watch.deleted", "tactics_watchlist", symbol.strip())

    @app.get("/tactics/watchlist/{symbol}/notes")
    def tactics_note_list(symbol: str, core: CoreState = Depends(_require_session)) -> list[dict[str, object]]:
        return core.database.list_tactics_notes(symbol.strip())

    @app.post("/tactics/watchlist/{symbol}/notes", response_model=dict[str, object])
    def tactics_note_create(
        symbol: str, body: TacticsNoteRequest, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        note = core.database.add_tactics_note(str(uuid4()), symbol.strip(), body.content.strip())
        _audit(core, "tactics.note.created", "tactics_notes", str(note["note_id"]))
        return note

    @app.delete("/tactics/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
    def tactics_note_delete(note_id: UUID, core: CoreState = Depends(_require_session)) -> None:
        if not core.database.delete_tactics_note(str(note_id)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="note not found")
        _audit(core, "tactics.note.deleted", "tactics_notes", str(note_id))

    # —— v26 战法 AI 技术面复核（手动触发）：规则算强度、AI 复核质量与可疑点 ——

    @app.post("/tactics/ai-review")
    def tactics_ai_review(body: TacticsAiReviewRequest, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """手动触发一次 AI 复核：规则分 + 命中明细 + 指标 + 近期 K 线 → 结构化质检（落库留痕）。

        JV03（Jev 决策模型接入路线图 2026-09-21，用户拍板「不需要 Jev 单独做」）把复核拆成两层：

        - **判定层（Jev 优先）**：可靠度分 / 认同度 / 假突破风险——三题、类型化答案、可复算，
          **不存在 JSON 解析失败**。Jev 未启用或不可用时，回落 chat 的同名字段（= 今天的行为）。
        - **叙述层（chat）**：summary / 站得住的地方 / 可疑点 / 接下来盯什么 / 规则分评价 /
          关键价位。**Jev 只回「是/否、选哪个、打几分」，生成不了文本**，这部分只能由 chat 出。

        **关键行为变化：chat 解析失败不再 502。** 以前 `normalize_review` 返回 None 就整次复核作废
        （用户白等一次模型调用、什么也没留下）；现在只丢叙述字段，判定层照常交付，并在响应与
        留痕里用 `narrative_status` 如实标注「本轮没有叙述」——不假装复核跑全了（JV00 铁律 6）。

        只读红线：不读模型方案表以外的任何配置、不写凭据库、不影响「使用中」方案。
        AI 与规则分**并列展示、不互相覆盖**：规则分是确定性排序依据，AI 分是可靠度复核，
        两者分歧恰恰是最有价值的信号（提示词里明确要求给出高估/低估判断）。
        """
        if not core.settings.model_access_enabled:
            return {
                "ok": False,
                "stage": "model_access_disabled",
                "detail": "模型出网已关闭（STEWARD_MODEL_ACCESS=0），未发起任何模型请求",
            }
        symbol = body.symbol.strip()
        try:
            bars, provider = _tactic_bars(core, symbol, limit=body.bars_limit)
        except FeedError as error:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"K 线取数失败：{error}") from error
        snap = stock_tactics.snapshot(bars)
        rule_score = _tactic_score_row(bars, snap, body.recent_bars)
        rule_score.pop("latest_signals", None)

        sector: dict[str, object] | None = None
        if body.sector_code:
            try:
                sector = next(
                    (item for item in fetch_cn_sector_list() if str(item.get("code")) == body.sector_code.strip()),
                    {"code": body.sector_code.strip()},
                )
            except FeedError:
                sector = {"code": body.sector_code.strip()}

        # —— 第 1 层：Jev 判定（可选，优先）。state 白名单见 tactics_ai.jev_state 注释 ——
        judgement: dict[str, object] | None = None
        jev_meta: dict[str, object] | None = None
        jev_note = ""
        jev_settings = _jev_effective_settings(core)
        if not jev_settings.enabled:
            jev_note = "Jev 未启用（设置 → Jev 决策模型），判定层由 chat 承担"
        else:
            try:
                jev_reply = jev_client.call_jev(
                    jev_client.JevConfig(
                        base_url=jev_settings.base_url,
                        model=jev_settings.model,
                        credential_ref=jev_settings.credential_ref,
                        timeout_secs=jev_settings.timeout_secs,
                        enabled=jev_settings.enabled,
                    ),
                    resolve_store(core.database),
                    tactics_ai.jev_state(
                        symbol=symbol,
                        name=symbol,
                        rule_score=rule_score,
                        indicators=snap.get("indicators") or {},
                        bars=bars,
                        sector=sector,
                        question=body.question,
                    ),
                    tactics_ai.jev_questions(),
                    purpose="jev:tactics-review",
                    access_enabled=core.settings.model_access_enabled,
                )
                judgement = tactics_ai.jev_judgement(jev_reply.answers)
                jev_meta = {
                    "model": jev_reply.model,
                    "requested_model": jev_reply.requested_model,
                    "latency_ms": jev_reply.latency_ms,
                    "input_tokens": jev_reply.input_tokens,
                    "output_tokens": jev_reply.output_tokens,
                    "schema_version": tactics_ai.JEV_REVIEW_SCHEMA_VERSION,
                    "question_schema_version": jev_reply.schema_version,
                }
            except jev_client.JevUnavailable as error:
                # 降级义务（JV00 铁律 6）：Jev 不可用不阻断复核，但必须在留痕里如实标注。
                jev_note = f"Jev 判定层未参与（{error}），已回落 chat 判定"

        # —— 第 2 层：chat 叙述。Jev 生成不了文本，这层只能靠 chat ——
        narrative: dict[str, object] | None = None
        narrative_status = "no_profile"
        narrative_error = ""
        narrative_model = ""
        narrative_latency_ms: int | None = None
        profile = None
        try:
            profile = _resolve_model_profile(core, body.profile_id)
        except HTTPException as error:
            narrative_error = str(error.detail)
        if profile is not None:
            system, user = tactics_ai.review_messages(
                symbol=symbol,
                name=symbol,
                rule_score=rule_score,
                indicators=snap.get("indicators") or {},
                bars=bars,
                sector=sector,
                question=body.question,
            )
            narrative_model = f"{profile.name}／{profile.model}"
            try:
                reply = model_client.call_active_model(
                    profile, resolve_store(core.database), model_client.format_messages(system, user)
                , purpose="战法复核",)
                narrative_latency_ms = reply.latency_ms
                parsed = macro_ai.extract_json_object(reply.content)
                narrative = tactics_ai.normalize_review(parsed)
                if narrative is None:
                    narrative_status = "unparsable"
                    narrative_error = (
                        f"模型输出无法解析为约定的复核 JSON（原文片段：{reply.content[:160]}）"
                    )
                else:
                    narrative_status = "ok"
            except model_client.ModelUnavailable as error:
                narrative_status = "model_unavailable"
                narrative_error = str(error)
            except Exception as error:  # noqa: BLE001 - 叙述层可选，任何异常都不该让判定层陪葬
                narrative_status = "error"
                narrative_error = f"{type(error).__name__}: {error}"

        review = tactics_ai.review_view(judgement=judgement, narrative=narrative)
        if review is None:
            # 两层都没拿到：如实标注，**不再 502**。今天 502 的成因正是「chat 吐不出合法 JSON」，
            # 而 502 让用户白等一次调用、什么也没留下——这里改成把原因说清楚。
            return {
                "ok": False,
                "stage": "review_unavailable",
                "engine": "none",
                "narrative_status": narrative_status,
                "detail": (
                    f"本轮没有取得任何复核结论（判定层：{jev_note or 'Jev 未启用'}；"
                    f"叙述层：{narrative_error or narrative_status}）"
                ),
            }

        engine = "jev" if judgement is not None else "chat"
        review_id = str(uuid4())
        created_at = datetime.now(UTC).isoformat()
        record = {
            "review_id": review_id,
            "symbol": symbol,
            "name": symbol,
            # model 列语义 = **判定层来源**：判定分是谁给的就记谁，否则 JV10 对账分不清账。
            "model": f"Jev／{jev_meta['model']}" if engine == "jev" and jev_meta else narrative_model,
            "rule_score": rule_score.get("tactic_score"),
            "ai_score": review.get("ai_score"),
            "verdict": review.get("verdict"),
            "agreement": review.get("agreement"),
            "summary": review.get("summary"),
            "engine": engine,
            "confidence": (judgement or {}).get("confidence"),
            "ai_score_raw": (judgement or {}).get("ai_score_raw"),
            "payload": {
                "review": review,
                "rule_score": rule_score,
                "provider": provider,
                "latency_ms": narrative_latency_ms,
                "prompt_version": tactics_ai.REVIEW_PROMPT_VERSION,
                "score_formula_version": tactics_score.SCORE_FORMULA_VERSION,
                "sector": sector,
                "question": body.question,
                "bars_count": len(bars),
                # JV03 判定层原始留痕：供 JV10 对账（调用量/一致率）与 R6 中文阈值标定。
                "judgement": judgement,
                "jev": jev_meta,
                "jev_note": jev_note,
                "narrative_status": narrative_status,
                "narrative_error": narrative_error,
                "narrative_model": narrative_model,
            },
            "created_at": created_at,
        }
        core.database.insert_tactics_ai_review(record)
        _audit(core, "tactics.ai_review.created", "tactics_ai_reviews", review_id)
        return {
            "ok": True,
            "engine": engine,
            "narrative_status": narrative_status,
            "review": record,
        }

    @app.get("/tactics/ai-reviews")
    def tactics_ai_reviews(
        symbol: str | None = None, limit: int = 30, core: CoreState = Depends(_require_session)
    ) -> list[dict[str, object]]:
        """AI 复核历史（可按 symbol 过滤），倒序；用于长期研究回看（当时规则分 vs AI 判断）。"""
        return core.database.list_tactics_ai_reviews(symbol=symbol.strip() if symbol else None, limit=limit)

    @app.delete("/tactics/ai-reviews/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
    def tactics_ai_review_delete(review_id: UUID, core: CoreState = Depends(_require_session)) -> None:
        if not core.database.delete_tactics_ai_review(str(review_id)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="review not found")
        _audit(core, "tactics.ai_review.deleted", "tactics_ai_reviews", str(review_id))

    @app.get("/market/candles/{symbol}", response_model=CandleSeries)
    def market_candles(symbol: str, period: str = "day", core: CoreState = Depends(_require_session)) -> CandleSeries:
        installation = core.database.get_plugin_installation("official.cn-market-data")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="cn-market-data plugin is not enabled"
            )
        if period not in ("day", "week", "month"):
            raise HTTPException(status_code=422, detail=f"period 仅支持 day/week/month（收到 {period}）")
        try:
            rows, provider = fetch_cn_kline(symbol, limit=120, period=period)
        except FeedError as error:
            logger.warning("market/candles 回退演示数据 symbol=%s period=%s: %s", symbol, period, error)
            return _demo_candles(symbol, period)
        _provider_names = {
            "tencent": "中国市场行情（腾讯行情）",
            "eastmoney": "中国市场行情（东方财富）",
        }
        _timeframe = {"day": "1d", "week": "1w", "month": "1mo"}
        _period_label = {"day": "日线", "week": "周线", "month": "月线"}
        bars = [
            OHLCVBar(
                timestamp=datetime.fromisoformat(str(row["timestamp"])),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            )
            for row in rows
        ]
        return CandleSeries(
            instrument=f"CN:ETF:{symbol}",
            timeframe=_timeframe[period],
            bars=bars,
            source_plugin_id="official.cn-market-data",
            source_plugin_release="0.1.0",
            source_name=_provider_names.get(provider, "中国市场行情"),
            as_of=bars[-1].timestamp,
            is_demo=False,
            limitations=[
                f"K 线来自公开行情接口（前复权{_period_label[period]}），{_period_label[period]}在收盘后即含当期最新蜡烛；非实时 tick，以供应商为准。",
                "离线或接口异常时自动回退为演示数据，界面会以「演示」标注。",
            ],
        )

    @app.post("/evidence/dedup")
    def evidence_dedup(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """v23 历史重复证据清理：按（类型 + 标题 + 发布日期）分组，保留最早入账一条，其余删除。

        针对哈希归一化之前已入库的重复（同内容公告被换稿号各入一条）；归一化之后
        拉取侧已不再产生新重复。删除仅限本租户证据账本，且写入审计。
        """
        items = core.database.list_evidence(core.local_user_id)
        groups: dict[tuple[str, str, str], list[Evidence]] = {}
        for item in items:
            title_key = item.summary.split("・")[0].strip()
            published = item.published_at or item.observed_at or item.collected_at
            key = (item.evidence_type, title_key, published.isoformat()[:10])
            groups.setdefault(key, []).append(item)
        removed = 0
        for bucket in groups.values():
            if len(bucket) <= 1:
                continue
            bucket.sort(key=lambda entry: (entry.collected_at, str(entry.evidence_id)))
            for extra in bucket[1:]:
                if core.database.delete_evidence(extra.evidence_id, core.local_user_id):
                    removed += 1
        if removed:
            _audit(core, "evidence.dedup", "removed_count", str(removed))
        return {"ok": True, "removed": removed, "remaining": len(items) - removed}

    @app.get("/evidence/announcements/{symbol:path}", response_model=AnnouncementBatch)
    def instrument_announcements(
        symbol: str, core: CoreState = Depends(_require_session)
    ) -> AnnouncementBatch:
        """把某标的的公告作为本地证据入账（阶段 8 cn-market-data 的公告证据能力）。

        网关：要求 cn-market-data 插件 ENABLED（与行情端点一致）；取数失败时不生成
        任何证据，返回 available=False 与降级说明（ADR-0004 不变量 #4，不编造）。
        """
        installation = core.database.get_plugin_installation("official.cn-market-data")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="cn-market-data plugin is not enabled"
            )
        try:
            items = fetch_cn_announcements(symbol, limit=20)
        except NoticeError as error:
            logger.warning("evidence/announcements 取数失败 symbol=%s: %s", symbol, error)
            return AnnouncementBatch(
                instrument=symbol,
                available=False,
                entries=[],
                source_name="中国市场公告（东方财富）",
                limitations=["公告取数失败，未生成任何证据；请稍后重试（详见 ADR-0004 不进行再分发）。"],
            )
        now = datetime.now(UTC)
        produced = []
        for item in items:
            collected = now
            published = parse_notice_timestamp(item.notice_date_raw) or collected
            summary = "・".join(filter(None, (item.title, item.ann_type))) or item.title
            evidence = Evidence(
                evidence_id=uuid4(),
                tenant_id=core.local_user_id,
                subject_refs=[f"instrument:CN:{symbol}"],
                evidence_type="announcement",
                source_name="中国市场公告（东方财富）",
                source_uri=item.url,
                license_status="local-personal-use",
                source_trust_note="东方财富公开网页接口，仅作本地个人复盘证据，不对外再分发（ADR-0004）。",
                published_at=published,
                observed_at=collected,
                collected_at=collected,
                source_dataset_version="np-anotice-stock",
                producer_plugin_id="official.cn-market-data",
                producer_release="0.1.0",
                schema_version="1.0",
                summary=summary,
                raw_locator=raw_locator_for("https://np-anotice-stock.eastmoney.com/api/security/ann", symbol, item),
                content_hash=content_hash_for(symbol, item.art_code, item.title, item.notice_date_raw),
                relation="unknown",
                freshness="as_of_fetch",
                quality_flags=[],
                limitations=["公告为原始稿件标题，正文需回链原始来源阅读（art_code 定位）。"],
                status="active",
            )
            # v23 拉取查重：同租户同内容哈希已入账即跳过（换号重发/重复拉取不再重复入库）。
            if core.database.get_evidence_by_hash(evidence.content_hash, core.local_user_id) is not None:
                continue
            core.database.insert_evidence(evidence)
            produced.append(evidence)
        for evidence in produced:
            _audit(core, "evidence.created", "evidence", str(evidence.evidence_id))
        return AnnouncementBatch(
            instrument=symbol,
            available=True,
            entries=produced,
            source_name="中国市场公告（东方财富）",
            limitations=[
                "公告来自东方财富公开网页接口，仅作本地个人复盘证据，不在用户允许外再分发。",
                "公告非实时推送，以供应商发布为准；正文原文请回链 art_code 定位。",
            ],
        )

    @app.get("/evidence/news/{symbol:path}", response_model=AnnouncementBatch)
    def instrument_news(symbol: str, core: CoreState = Depends(_require_session)) -> AnnouncementBatch:
        """把某标的的相关新闻作为本地证据入账（阶段 8 cn-market-data 的新闻检索证据）。

        网关：要求 cn-market-data ENABLED；取数失败时不生成任何证据（available=False，
        ADR-0004 不变量 #4）。新闻归入 `ANALYSIS` 证据类型（模型无 NEWS 字面量，取最近义的
        证据桶，source_name 明确标注为新闻来源），原文经 article code 回链阅读。
        """
        installation = core.database.get_plugin_installation("official.cn-market-data")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="cn-market-data plugin is not enabled"
            )
        try:
            items = fetch_cn_news(symbol, limit=20)
        except NewsError as error:
            logger.warning("evidence/news 取数失败 symbol=%s: %s", symbol, error)
            return AnnouncementBatch(
                instrument=symbol,
                available=False,
                entries=[],
                source_name="中国市场新闻（东方财富）",
                limitations=["新闻检索取数失败，未生成任何证据；请稍后重试（详见 ADR-0004 不进行再分发）。"],
            )
        now = datetime.now(UTC)
        produced = []
        for item in items:
            collected = now
            published = parse_notice_timestamp(item.published_raw) or collected
            snippet = item.content[:1000]
            evidence = Evidence(
                evidence_id=uuid4(),
                tenant_id=core.local_user_id,
                subject_refs=[f"instrument:CN:{symbol}"],
                evidence_type="analysis",
                source_name="中国市场新闻（东方财富）",
                source_uri=item.url,
                license_status="local-personal-use",
                source_trust_note="东方财富公开搜索接口，仅作本地个人复盘证据，不对外再分发（ADR-0004）。",
                published_at=published,
                observed_at=collected,
                collected_at=collected,
                source_dataset_version="search-api-web",
                producer_plugin_id="official.cn-market-data",
                producer_release="0.1.0",
                schema_version="1.0",
                summary=item.title,
                raw_locator=json.dumps(
                    {
                        "api": "https://search-api-web.eastmoney.com/search/jsonp",
                        "symbol": symbol,
                        "code": item.code,
                        "media": item.media,
                        "content_snippet": snippet,
                    },
                    ensure_ascii=False,
                ),
                content_hash=content_hash_for_news(symbol, item.code, item.title, item.content),
                relation="unknown",
                freshness="as_of_fetch",
                quality_flags=[],
                limitations=["新闻为检索命中的标题与摘要，正文需回链 article code（source_uri）阅读。"],
                status="active",
            )
            # v23 拉取查重：同租户同内容哈希已入账即跳过（同标题不同稿件号不再重复入库）。
            if core.database.get_evidence_by_hash(evidence.content_hash, core.local_user_id) is not None:
                continue
            core.database.insert_evidence(evidence)
            produced.append(evidence)
        for evidence in produced:
            _audit(core, "evidence.created", "evidence", str(evidence.evidence_id))
        return AnnouncementBatch(
            instrument=symbol,
            available=True,
            entries=produced,
            source_name="中国市场新闻（东方财富）",
            limitations=[
                "新闻来自东方财富公开搜索接口，仅本地个人复盘，不在用户允许外再分发。",
                "新闻非实时推送，全文请回链 source_uri 阅读。",
            ],
        )

    @app.get("/evidence/financials/{symbol:path}", response_model=AnnouncementBatch)
    def instrument_financials(
        symbol: str, core: CoreState = Depends(_require_session)
    ) -> AnnouncementBatch:
        """把某 A 股的财务报表作为本地证据入账（阶段 8 cn-company-evidence 的财报能力）。

        网关：要求 cn-market-data ENABLED。取数走东财 F10 免费接口直连（纯 stdlib，
        无需安装 financials 额外依赖）：取数失败或结构异常时返回 available=False，
        不生成任何证据（不编造）。行项目按 finance `_ALIASES` 归一化；内容哈希由
        (symbol, 报表类型, 报告期, 公告期, 行项目) 稳定决定。
        """
        installation = core.database.get_plugin_installation("official.cn-market-data")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="cn-market-data plugin is not enabled"
            )
        try:
            rows = fetch_financials(symbol, limit_per_type=5)
        except (FinancialsError, ValueError) as error:
            logger.warning("evidence/financials 取数失败 symbol=%s: %s", symbol, error)
            return AnnouncementBatch(
                instrument=symbol,
                available=False,
                entries=[],
                source_name="A 股财务报表（东财 F10 直连）",
                limitations=[f"财报取数失败，未生成任何证据；原因：{error}。稍后重试。"],
            )
        now = datetime.now(UTC)
        produced = []
        for row in rows:
            collected = now
            published = parse_notice_timestamp(row.published_raw) or collected
            item_snapshot = "；".join(f"{k}={v}" for k, v in row.items.items())
            evidence = Evidence(
                evidence_id=uuid4(),
                tenant_id=core.local_user_id,
                subject_refs=[f"instrument:CN:{symbol}"],
                evidence_type="financial",
                source_name="A 股财务报表（东财 F10 直连）",
                source_uri="https://data.eastmoney.com/bbsj/",
                license_status="local-personal-use",
                source_trust_note="东方财富财务摘要，经东财 F10 免费接口直连拉取；仅本地个人复盘，不对外再分发（ADR-0004）。",
                published_at=published,
                observed_at=collected,
                collected_at=collected,
                source_dataset_version="em-f10-stock-*-by-report-em",
                producer_plugin_id="official.cn-market-data",
                producer_release="0.1.0",
                schema_version="1.0",
                summary=f"{row.statement_type}·{row.period_end or '未知报告期'}：{item_snapshot or '无细目'}",
                raw_locator=raw_locator_for_financials(symbol, row),
                content_hash=row.identity,
                relation="unknown",
                freshness="as_of_fetch",
                quality_flags=[],
                limitations=["报表为东财摘要口径（归一化行项目），全文及会计口径以原始公告为准。"],
                status="active",
            )
            # v23 拉取查重：同租户同内容哈希已入账即跳过（重复拉取财报不再重复入库）。
            if core.database.get_evidence_by_hash(evidence.content_hash, core.local_user_id) is not None:
                continue
            core.database.insert_evidence(evidence)
            produced.append(evidence)
        for evidence in produced:
            _audit(core, "evidence.created", "evidence", str(evidence.evidence_id))
        return AnnouncementBatch(
            instrument=symbol,
            available=True,
            entries=produced,
            source_name="A 股财务报表（东财 F10 直连）",
            limitations=[
                "报表来自东方财富财务摘要，仅本地个人复盘，不在用户允许外再分发。",
                "未经核实的财报请人工复核；本系统不据此构成投资建议。",
            ],
        )

    def _build_stock_evidence(
        core: CoreState, symbol: str, bars_limit: int, news_limit: int, announcement_limit: int
    ) -> tuple[str, dict[str, str], dict[str, str], dict[str, object]]:
        """v23 抽取：个股研报与协同流水线共用的证据包构建（K 线日/周/月 + 新闻 + 公告 + 财务）。

        失败降级铁律不变：单来源失败记入 source_errors 如实标注，不拖垮整体；
        全部失败时 tech_summary 与 sources 均为空（由调用方判定 evidence_unavailable）。

        v28（二次方案 §2.3）：第 4 个返回值 `evidence_meta` 把「各来源数据日期」与「估值快照样本数」
        一并带出，供质量闸门做「数据日期是否过期」判定、供首屏结论卡拼「同业样本 N 只」标签：
        - `source_asof`：{S1..S5 -> 该来源中出现的**最新数据日期**}，解析不出一律不填（不猜）；
        - `peer_count`：S5 快照的同业样本数（取数结果，非模型自述），取数失败为 None。
        """
        sources: dict[str, str] = {}
        source_errors: dict[str, str] = {}
        source_asof: dict[str, str] = {}
        peer_count: int | None = None
        tech_summary = ""
        # v29（三次方案 §3）：价格量能图数据包 —— 取数成功才填，失败保持空（前端如实降级，不画空图）。
        price_bars: list[dict[str, object]] = []
        kline_provider = ""
        # v29：各来源取数时刻（ISO 8601），供前端「来源元数据」如实展示取数时间。
        retrieved_at = datetime.now(UTC).isoformat()

        def _latest_date_of(values: list[str]) -> str:
            """一组原始日期文本里的最新 ISO 日期；解析不出的一律跳过（不猜、不填今天）。"""
            parsed_dates: list[str] = []
            for raw in values:
                parsed = parse_notice_timestamp(raw)
                if parsed is not None:
                    parsed_dates.append(parsed.isoformat()[:10])
            return max(parsed_dates) if parsed_dates else ""
        try:
            bars, provider = _tactic_bars(core, symbol, limit=bars_limit)
            price_bars = bars
            kline_provider = provider
            snap = stock_tactics.snapshot(bars)
            indicators = snap["indicators"] if isinstance(snap["indicators"], dict) else {}
            latest = bars[-1] if bars else {}
            closes = [float(bar.get("close", 0) or 0) for bar in bars] if bars else []
            highs = [float(bar.get("high", 0) or 0) for bar in bars] if bars else []
            vols = [float(bar.get("volume", 0) or 0) for bar in bars] if bars else []
            lookback = min(len(closes), bars_limit)
            win = closes[-lookback:] if closes else []
            win_highs = highs[-lookback:] if highs else []
            win_vols = vols[-lookback:] if vols else []
            period_change = (closes[-1] / closes[-lookback] - 1) * 100 if len(closes) >= lookback and closes[-lookback] else None
            drawdown = (closes[-1] / max(win_highs) - 1) * 100 if win_highs and max(win_highs) else None
            vol5 = sum(win_vols[-5:]) / 5 if len(win_vols) >= 5 else None
            vol20 = sum(win_vols[-20:]) / 20 if len(win_vols) >= 20 else None
            # K 线行日期字段是 timestamp（ISO 8601，见 market_feed 两个源），截取日期段；
            # 曾误用 latest.get("date") → 恒显示「截至 未知」，模型随之在局限声明里如实复述。
            latest_date = str(latest.get("timestamp", ""))[:10] or "未知"
            # v28：S1 的数据日期 = 最新一根 K 线的日期（行情停更时会被质量闸门判为过期）。
            if len(latest_date) == 10:
                source_asof["S1"] = latest_date
            latest_close = _round_number(latest.get("close"), 2)
            # R02（2026-09-18 排版与研报呈现一致性路线图）：S1 的两个「高点」分口径标注，
            # 交由 _s1_price_range_segment 生成「收盘区间 + 盘中最高回撤」两段可各自复算的文本。
            tech_summary = (
                f"K线（{provider}，{snap['bars_count']} 根日线，截至 {latest_date}）："
                f"最新收盘 {latest_close if latest_close is not None else '未知'}；"
                + _s1_price_range_segment(lookback, win, win_highs, period_change, drawdown)
                + "；"
                + (
                    # J05（桌面端升级路线图 2026-09-18）：三态标签 + 量比随文写出，单位见 _s1_volume_segment。
                    _s1_volume_segment(vol5, vol20, provider) + "；"
                    if vol5 and vol20
                    else ""
                )
                + f"指标 {json.dumps(display_indicators(indicators), ensure_ascii=False)}；"
                f"近期战法信号 {json.dumps(_recent_signals_with_state(snap['signals'][-5:], bars), ensure_ascii=False)}。"
            )
        except Exception as error:  # 单来源失败只降级该来源，不拖垮报告（如实标注）
            source_errors["kline"] = f"K 线取数失败：{error.detail if isinstance(error, HTTPException) else error}"

        # v22 补充周线/月线结构摘要（用户反馈：日线偏向短期，未覆盖更大级别结构）。
        # 独立 try/except：成功才追加进 S1，失败记入 source_errors 如实标注（不污染空判定）。
        for period_label, period_code, bars_n in (("周线", "week", 60), ("月线", "month", 36)):
            try:
                p_bars, p_provider = fetch_cn_kline(symbol, limit=bars_n, period=period_code)
                p_closes = [float(bar.get("close", 0) or 0) for bar in p_bars]
                p_highs = [float(bar.get("high", 0) or 0) for bar in p_bars]
                if not p_closes:
                    raise ValueError("空数据")
                p_ma = sum(p_closes[-20:]) / 20 if len(p_closes) >= 20 else None
                tech_summary += (
                    f" {period_label}级别（{p_provider}，{len(p_bars)} 根，截至 {str(p_bars[-1].get('timestamp', ''))[:10]}）："
                    f"近 {len(p_closes)} 个{period_label[:-1]}收盘区间 {min(p_closes):.2f}~{max(p_highs):.2f}"
                    + (f"，现价位于 20{period_label[:-1]}均线{'上方' if p_closes[-1] >= p_ma else '下方'}（均值 {p_ma:.2f}）。" if p_ma else "。")
                )
            except Exception as error:
                source_errors[f"kline_{period_code}"] = (
                    f"{period_label}结构取数失败：{error.detail if isinstance(error, HTTPException) else error}"
                )

        try:
            news_items = fetch_cn_news(symbol, limit=news_limit)
            lines = [f"- [{item.published_raw or '日期未知'}] {item.title}" for item in news_items]
            sources["S2"] = "近期新闻（东方财富）：\n" + ("\n".join(lines) if lines else "- 无检索结果")
            # v28：S2 数据日期 = 新闻里出现的最新发布日期（解析不出就不填，不猜今天）。
            news_latest = _latest_date_of([item.published_raw for item in news_items])
            if news_latest:
                source_asof["S2"] = news_latest
        except Exception as error:  # 单来源失败只降级该来源（NewsError 或底层网络异常）
            source_errors["news"] = f"新闻取数失败：{error}"

        try:
            ann_items = fetch_cn_announcements(symbol, limit=announcement_limit)
            lines = [f"- [{item.notice_date_raw or '日期未知'}] {item.title}" for item in ann_items]
            sources["S3"] = "近期公告（东方财富，标题口径）：\n" + ("\n".join(lines) if lines else "- 无公告")
            ann_latest = _latest_date_of([item.notice_date_raw for item in ann_items])
            if ann_latest:
                source_asof["S3"] = ann_latest
        except Exception as error:  # 单来源失败只降级该来源（NoticeError 或底层网络异常）
            source_errors["announcements"] = f"公告取数失败：{error}"

        fin_rows: list[Any] = []
        try:
            # v22：每类报表取 8 期（原 3 期只有年报/一季/中报，缺去年同期基数，模型算不了同比）。
            fin_rows = fetch_financials(symbol, limit_per_type=8)
            # R03（2026-09-18 排版与研报呈现一致性路线图）：S4 证据文本用中文行项目名（人话），
            # 不再把内部规范键（basic_eps/total_debt…）交给模型——模型会照抄进正文。规范键保留在
            # 账本 item_snapshot（app.py:5712）与 raw_locator 里作回链，取数与落库口径不变。
            lines = _s4_financial_lines(fin_rows)
            sources["S4"] = "财务摘要（东财 F10，行项目名已归一为中文）：\n" + ("\n".join(lines) if lines else "- 无报表数据")
            # v28：S4 数据日期 = 最新报告期（财报天然滞后，其时效阈值单列为 180 天）。
            periods = [str(row.period_end)[:10] for row in fin_rows]
            periods = [value for value in periods if len(value) == 10 and value[4] == "-"]
            if periods:
                source_asof["S4"] = max(periods)
        except Exception as error:
            source_errors["financials"] = f"财报取数失败：{error}"

        # —— v24 档 B/C：估值证据 S5（当前估值 + 历史分位 + 同业相对）——
        # 与 K 线/新闻/公告/财报并列的第五类来源；失败只降级该来源并如实标注（绝不编造估值）。
        valuation = None
        try:
            valuation = fetch_valuation(symbol)
            sources["S5"] = describe_valuation(valuation)
            # v28：S5 数据日期 = 快照交易日；peer_count = 同业样本数（取数结果，非模型自述）。
            if valuation.trade_date:
                source_asof["S5"] = str(valuation.trade_date)[:10]
            peer_count = int(valuation.peer_count) if valuation.peer_count else None
        except Exception as error:
            source_errors["valuation"] = f"估值取数失败：{error}"

        # —— P2-C04 席位证据 S6（龙虎榜）——
        # 与 S1–S5 的关键差别：**只在「该票确实上榜」时才追加**。
        # 未上榜就不加 S6、不生成空表、也不写「游资没进」这类负向编造（方案 §4.3、铁律 5）。
        # 因此默认状态下 S1–S5 五类来源保持逐字不变——这条有测试守着。
        # 取数失败只降级（记入 source_errors），绝不编造席位。
        try:
            seat_result = _build_seat_evidence(core, symbol, lookback_days=core.settings.youzi_lookback_days)
            if seat_result is not None:
                seat_summary, seat_latest_day, seat_errors = seat_result
                sources["S6"] = seat_summary
                if seat_latest_day:
                    source_asof["S6"] = seat_latest_day
                for key, message in seat_errors.items():
                    source_errors[key] = message
        except Exception as error:  # noqa: BLE001 - 单来源失败如实标注，不拖垮整体
            source_errors["seat_evidence"] = f"龙虎榜席位取数失败：{error}"

        # —— v24 档 A：由同一份报表现算的派生指标（TTM / ROE / 利润含金量），追加进 S4 ——
        # 与 S4 同源（都来自 F10 财报），故并入 S4 而不新开来源编号，避免「来源编号虚增」。
        if fin_rows and "S4" in sources:
            try:
                derived = derive_fundamental_metrics(
                    fin_rows,
                    total_shares=valuation.total_shares if valuation else None,
                    latest_price=valuation.close_price if valuation else None,
                )
                sources["S4"] = f"{sources['S4']}\n{describe_derived(derived)}"
            except Exception as error:
                source_errors["derived_metrics"] = f"派生指标计算失败：{error}"

        # —— v29（三次方案 §3）：图表数据包 —— 纯取数结果的搬运与整型，不在服务端做任何主观判断；
        # 每块独立降级，取不到就缺省（前端如实显示「数据不可用」，绝不画空图冒充）。
        # 1) 价格量能图：最近 120 根日线（前复权：东财 fqt=1 / 腾讯 qfq），MA5/20/60 按全序列滚动后截尾。
        price_chart: dict[str, object] = {}
        if price_bars:
            try:
                closes_all = [float(bar.get("close", 0) or 0) for bar in price_bars]

                def _rolling_ma(values: list[float], window: int) -> list[float | None]:
                    out: list[float | None] = []
                    for index in range(len(values)):
                        if index + 1 < window:
                            out.append(None)
                        else:
                            seg = values[index + 1 - window : index + 1]
                            out.append(round(sum(seg) / window, 3))
                    return out

                tail_n = min(len(price_bars), 120)
                candles = [
                    {
                        "date": str(bar.get("timestamp", ""))[:10],
                        "open": _round_number(bar.get("open"), 2),
                        "high": _round_number(bar.get("high"), 2),
                        "low": _round_number(bar.get("low"), 2),
                        "close": _round_number(bar.get("close"), 2),
                        "volume": bar.get("volume"),
                    }
                    for bar in price_bars[-tail_n:]
                ]
                ma_full = {w: _rolling_ma(closes_all, w) for w in (5, 20, 60)}
                price_chart = {
                    "adjust": "前复权",
                    "provider": kline_provider,
                    "candles": candles,
                    "ma5": ma_full[5][-tail_n:],
                    "ma20": ma_full[20][-tail_n:],
                    "ma60": ma_full[60][-tail_n:],
                }
            except Exception as error:
                source_errors["price_chart"] = f"价格量能图数据组装失败：{error}"
        # 2) 单季财务图：累计报表相邻报告期差分（口径见 derive_quarterly_series），缺失不补零。
        quarterly: dict[str, object] = {}
        if fin_rows:
            for key, stype, item_key in (
                ("revenue", "income", "revenue"),
                ("net_profit", "income", "net_profit"),
                ("operating_cash_flow", "cash_flow", "operating_cash_flow"),
            ):
                try:
                    series = derive_quarterly_series(
                        [row for row in fin_rows if row.statement_type == stype],
                        statement_type=stype,
                        item_key=item_key,
                    )
                    if series:
                        quarterly[key] = series
                except Exception as error:
                    source_errors[f"quarterly_{key}"] = f"单季拆分失败（{key}）：{error}"
        # 3) 估值分位图：快照里的历史分位 + 同业中位（都是取数结果，缺哪个字段就少哪个键）。
        valuation_chart: dict[str, object] = {}
        if valuation is not None:
            valuation_chart = {
                "trade_date": str(valuation.trade_date)[:10] if valuation.trade_date else "",
                "pe_ttm": valuation.pe_ttm,
                "pb_mrq": valuation.pb_mrq,
                "ps_ttm": valuation.ps_ttm,
                "percentiles": dict(valuation.percentiles or {}),
                "percentile_window_bars": valuation.percentile_window_bars,
                "percentile_window_from": valuation.percentile_window_from,
                "peer_median": dict(valuation.peer_median or {}),
                "peer_count": valuation.peer_count,
                # 每个口径实际可比几只（PE 常因亏损股被排除而少于同业池）——可比样本数以此为准，
                # 不采信模型自述的「我比了几只」。
                "peer_median_basis": dict(valuation.peer_median_basis or {}),
            }
        # 4) 来源元数据：每来源的数据提供方与取数时刻（as_of 已在 source_asof，前端并列展示）。
        source_meta: dict[str, object] = {}
        provider_map = {
            "S1": kline_provider,
            "S2": "东方财富",
            "S3": "东方财富",
            "S4": "东方财富 F10",
            "S5": "东方财富 datacenter（RPT_VALUEANALYSIS_DET）",
        }
        for source_id, source_provider in provider_map.items():
            # S1 技术面不进 sources dict（tech_summary 独立返回），但同为真实来源：
            # K 线取数成功（kline_provider 非空）就登记元数据，与 source_asof["S1"] 的登记条件同源。
            present = source_id in sources or (source_id == "S1" and bool(kline_provider))
            if present:
                source_meta[source_id] = {
                    "provider": source_provider or "",
                    "retrieved_at": retrieved_at,
                }

        return tech_summary, sources, source_errors, {
            "source_asof": source_asof,
            "peer_count": peer_count,
            "source_meta": source_meta,
            "price_chart": price_chart,
            "quarterly": quarterly,
            "valuation_chart": valuation_chart,
        }

    def _apply_p2_summary_and_completeness(
        parsed: dict, quality: dict, counter_check_findings: dict, *, mode: str
    ) -> tuple[dict, dict]:
        """P2（Q19/Q20，2026-09-19 路线图 §5）：反方检查出完后再过一遍摘要与完成度。

        与 M3-D05（方向研判 `apply_counter_check`）同一处理哲学：**就地给不成立的结论句加标注、
        模型原话整份留痕、不改写事实句**。修订说明不进摘要字数预算（同 C01 口径），
        因此 `quality["summary_chars"]` 仍按模型原文计量。返回 `(summary_overreach, 完成度视图)`。
        """
        overreach = report_quality.summary_overreach_review(
            executive_summary=str(parsed.get("executive_summary", "") or ""),
            counter_check=counter_check_findings,
            valuation=quality["valuation"],
            claim_findings=quality["claim_findings"],
        )
        if overreach["changed"]:
            # 原话留痕只在首次发生改写时写，重复执行不覆盖（幂等）。
            parsed.setdefault("executive_summary_original", str(parsed.get("executive_summary", "") or ""))
            parsed["executive_summary"] = overreach["revised_summary"]
        if overreach["warnings"]:
            quality["warnings"].extend(overreach["warnings"])
            quality["quality_status"] = report_quality.quality_status_of(
                blockers=quality["quality_blockers"], warnings=quality["warnings"]
            )
            quality["quality_status_label"] = report_quality.QUALITY_STATUS_LABELS[quality["quality_status"]]
        completeness = report_quality.research_completeness(
            {**parsed, "report_mode": mode, "counter_check": counter_check_findings}
        )
        return overreach, completeness

    @app.post("/evidence/stock-research-report")
    def stock_research_report(
        body: StockResearchReportRequest, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        """AI 个股研究报告（v21 用户追加）：手动选股 → 新闻/公告/财报/估值 + K 线战法快照 → 模型报告。

        证据铁律：五类输入各自如实标注「取到/取数失败」，失败来源不编造；模型要求
        每条判断挂 [S1..S5] 来源引用，输出无 citations 视为解析失败不返回报告
        （与宏观分析 ADR-0006 同一约束）。报告不落库、不入证据账本，纯即时返回。
        """
        installation = core.database.get_plugin_installation("official.cn-market-data")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="cn-market-data plugin is not enabled"
            )
        if not core.settings.model_access_enabled:
            return {"ok": False, "stage": "model_access_disabled", "detail": "模型出网已关闭（STEWARD_MODEL_ACCESS=0），未发起任何模型请求"}
        profiles = core.database.list_model_profiles()
        if body.profile_id is not None:
            # v22 多模型对比：显式指定方案（不要求「使用中」状态），方案不存在即 404。
            profile = next((item for item in profiles if item.profile_id == body.profile_id), None)
            if profile is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="指定的模型方案不存在")
        else:
            profile = model_client.active_model_profile(profiles)
            if profile is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="没有「使用中」的模型方案：先在设置页把一个方案置为使用中（研究报告依赖模型出网）",
                )
        symbol = body.symbol.strip().upper()

        # —— 第一段：五类证据构建（v23 抽取为 _build_stock_evidence，协同流水线同一口径）——
        tech_summary, sources, source_errors, evidence_meta = _build_stock_evidence(
            core, symbol, body.bars_limit, body.news_limit, body.announcement_limit
        )

        if not tech_summary and not sources:
            return {
                "ok": False,
                "stage": "evidence_unavailable",
                "detail": "五类输入全部取数失败，无证据可依据，不生成报告（不编造）。",
                "source_errors": source_errors,
            }

        # —— 第二段：组 prompt 调模型 ——
        # 每类证据带显式 [S#] 前缀：此前只有 S1 带前缀，S2~S5 靠小标题让模型自己推断编号，
        # 来源变多后引用极易错位；显式编号让「引用真实性闸门」可判。
        numbered = "\n".join(f"[{key}] {value}" for key, value in sources.items())
        unavailable = ("\n来源不可用（禁止引用、禁止臆测其内容）：" + "；".join(source_errors.values())) if source_errors else ""
        user_prompt = (
            f"研究对象：A 股 {symbol}。用户关注点：{body.question.strip() or '综合趋势与基本面'}\n\n"
            f"[S1] {tech_summary or 'K 线取数失败，技术面无数据。'}\n\n{numbered}{unavailable}\n\n"
            "你是严谨的 A 股卖方研究助理。请输出严格 JSON（无 markdown 围栏）：\n"
            f"{STOCK_REPORT_SCHEMA_TEXT}\n"
            + STOCK_REPORT_RULES_TEXT
        )
        try:
            reply = model_client.call_active_model(
                profile,
                resolve_store(core.database),
                model_client.format_messages(
                    "你是严谨的 A 股研究助理：只依据给定来源输出，每条判断必须可溯源；"
                    "证据不足时如实说证据不足；宁可给「无数据」也不编造。你的输出不构成投资建议。",
                    user_prompt,
                ),
             purpose="研报",)
        except model_client.ModelUnavailable as exc:
            return {"ok": False, "stage": "model_call", "detail": f"模型调用失败：{exc}", "source_errors": source_errors}
        parsed = macro_ai.extract_json_object(reply.content)
        if parsed is None or not isinstance(parsed.get("report"), str) or not parsed.get("citations"):
            return {
                "ok": False,
                "stage": "parse",
                "detail": "模型输出无法解析为带来源引用的报告 JSON（无引用不发布，ADR-0006）。",
                "raw_excerpt": reply.content[:400],
                "source_errors": source_errors,
            }
        # v24 质量闸门：引用必须属于本次真实取到的来源（硬校验，剔除虚构引用）；
        # 长度/小节/confidence 偏差产出 warnings 如实标注（软校验，不阻断交付）。
        allowed_citations = report_quality.available_citations(sources.keys(), tech_available=bool(tech_summary))
        # v29：模式字数预算（quick/standard/deep）；未知模式 422，不静默回落到 standard。
        try:
            report_mode, _mode_label, _lo, _hi = report_quality.report_mode_budget(body.mode)
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
        # v30（四次升级方案 §三）：摘要超硬上限（180 字）→ 仅重写摘要的一次模型调用，不重写正文。
        # 重写仍超限 / 调用失败 → 保留原文，由 validate_report 软告警标注，交人工复核；
        # 绝不静默截断，也绝不二次自动重试。
        # —— M3-D03（2026-09-15 第二轮路线图）：验证点时点确定性修复 ——
        # 只到月份但有锚定事件（「2026年9月生猪销售简报披露后」）→ 补「披露后 5 个交易日内」；
        # 定不出锚点事件 → 标「时点未知」（**绝不编假日期**）。原话保留在 `verify_by_raw`。
        verify_by_repairs = report_quality.repair_watchpoints(parsed)
        # —— M3-D01/D02：正文估值表述对齐驾驶舱口径（确定性，先于质量闸门）——
        # 估值状态经同一 `normalize_valuation` 推导（与闸门同源，不两处口径）；
        # 状态为「无法判断」却写低估/安全垫、或 S5 高于同业却写「低于同业/安全垫」→ 加对齐标注。
        _valuation_block = report_quality.normalize_valuation(parsed.get("valuation"))
        _aligned_report, valuation_language_repairs = report_quality.align_valuation_language(
            str(parsed.get("report") or ""),
            str(_valuation_block.get("valuation_status") or ""),
            str(_valuation_block.get("peer_position") or ""),
        )
        if valuation_language_repairs:
            parsed["report"] = _aligned_report
        summary_rewrite: dict[str, object] = {"needed": False, "rewritten": False}
        _summary_text = str(parsed.get("executive_summary", "")).strip()
        # D04：超出**目标上界**（165）即重写摘要（原策略只在超 180 硬上限时重写）。
        if report_quality.needs_summary_rewrite(_summary_text):
            summary_rewrite = {
                "needed": True,
                "rewritten": False,
                "original_chars": report_quality.report_char_count(_summary_text),
                "hard_limit": report_quality.MAX_SUMMARY_CHARS,
                "target_max": report_quality.SUMMARY_TARGET_MAX,
                "reason": "summary_over_target",
            }
            rewrite_user = (
                f"研报标题：{str(parsed.get('title', '')).strip()}\n"
                f"原执行摘要（{report_quality.report_char_count(_summary_text)} 字，"
                f"超出目标区间 {report_quality.SUMMARY_TARGET_MIN}-{report_quality.SUMMARY_TARGET_MAX} 字）：\n"
                f"{_summary_text}\n\n"
                "请把这份执行摘要改写为四句话结构、150-165 字："
                "①方向性结论 ②最强的基本面或估值依据 ③最重要的技术面或资金面依据 ④最大的风险/反证。"
                "只输出改写后的摘要纯文本（不要 JSON、不要前缀、不要解释）；"
                "只允许使用原摘要与标题中已出现的信息，不得新增任何数字、事件或判断。"
            )
            try:
                rewrite_reply = model_client.call_active_model(
                    profile,
                    resolve_store(core.database),
                    model_client.format_messages(
                        "你是严谨的研究编辑：只做摘要压缩改写，绝不新增信息，绝不改动事实口径。",
                        rewrite_user,
                    ),
                 purpose="研报",)
                rewritten = (rewrite_reply.content or "").strip()
                rewritten_chars = report_quality.report_char_count(rewritten)
                # 接受条件：落进目标区间；或**确实变短且未超硬上限**（改善版保留，软告警继续标注）。
                if rewritten and (
                    rewritten_chars <= report_quality.SUMMARY_TARGET_MAX
                    or (
                        rewritten_chars <= report_quality.MAX_SUMMARY_CHARS
                        and rewritten_chars < report_quality.report_char_count(_summary_text)
                    )
                ):
                    parsed["executive_summary"] = rewritten
                    summary_rewrite["rewritten"] = True
                    summary_rewrite["rewritten_chars"] = rewritten_chars
                    summary_rewrite["in_target_band"] = (
                        report_quality.SUMMARY_TARGET_MIN <= rewritten_chars <= report_quality.SUMMARY_TARGET_MAX
                    )
                else:
                    summary_rewrite["detail"] = (
                        "重写输出为空、未变短或仍超硬上限，保留原文（软告警标注，交人工复核）。"
                    )
            except model_client.ModelUnavailable as exc:
                summary_rewrite["detail"] = f"摘要重写模型调用失败：{exc}"
            except Exception as exc:  # noqa: BLE001 - 重写尽力而为，绝不阻断报告交付
                summary_rewrite["detail"] = f"摘要重写执行异常：{exc}"
        # v28：把「各来源数据日期」与当前日期交给闸门，才能判定「来源数据是否过期」。
        # 当前日期由这一层提供（与 /today 端点同口径），report_quality 保持纯函数、不读系统时间。
        # JV04：再交出「来源实际内容」+ 语义判定回调，补上「引用的内容是否真的支撑这条判断」。
        # 回调为 None（总闸关闭 / Jev 未启用）→ 闸门整层不运行，行为与接入前逐字节一致。
        quality = report_quality.validate_report(
            parsed,
            allowed=allowed_citations,
            source_asof=evidence_meta.get("source_asof") or {},
            today=datetime.now().astimezone().date(),
            mode=report_mode,
            source_texts=sources,
            jev_judge=jev_client.build_judge(core, resolve_store(core.database)),
        )
        if not quality["citations"]:
            return {
                "ok": False,
                "stage": "citation",
                "detail": (
                    "模型引用的来源 id 均不属于本次真实取到的来源，按「无引用不发布」不返回报告（防虚构引用）。"
                ),
                "dropped_citations": quality["dropped_citations"],
                "available_citations": quality["available_citations"],
                "source_errors": source_errors,
            }
        # —— v31 §2.4 低成本修复策略：按失败类型定向修复，最多一轮、不无限重试 ——
        # 缺摘要 → 只生成摘要；缺一节 → 只重新生成该章节；缺两节及以上（或缺节且缺摘要）→ 重跑完整 draft。
        # 修复后重跑同一闸门（一致性检查）；仍不完整则如实保留草稿状态，绝不伪造「已完整」。
        # collab 流水线不走自动修复：其阶段级重试即修复路径（§2.4「保留已完成阶段」口径）。
        repair_trace: dict[str, object] = {"attempted": False, "repaired": False}
        report_revision = 1
        revision_history: list[dict[str, object]] = []
        parsed_before_repair = dict(parsed)
        quality_before_repair = quality
        summary_missing_now = not str(parsed.get("executive_summary", "") or "").strip()
        repair_strategy = report_quality.repair_strategy_of(
            missing_sections=list(quality["missing_sections"]),
            summary_missing=summary_missing_now,
        )
        if repair_strategy is not None and body.with_auto_repair:
            repair_trace["attempted"] = True
            repair_trace["strategy"] = repair_strategy
            missing_now = list(quality["missing_sections"])
            reason = "；".join(
                ([f"缺少必需小节「{'、'.join(missing_now)}」"] if missing_now else [])
                + (["缺少执行摘要"] if summary_missing_now else [])
            )
            repair_detail = ""
            repaired = False
            try:
                if repair_strategy == report_quality.REPAIR_STRATEGY_FULL_RERUN:
                    rerun_reply = model_client.call_active_model(
                        profile,
                        resolve_store(core.database),
                        model_client.format_messages(
                            "你是严谨的 A 股研究助理：只依据给定来源输出，每条判断必须可溯源；"
                            "证据不足时如实说证据不足；宁可给「无数据」也不编造。你的输出不构成投资建议。",
                            user_prompt,
                        ),
                     purpose="研报",)
                    rerun_parsed = macro_ai.extract_json_object(rerun_reply.content)
                    if (
                        isinstance(rerun_parsed, dict)
                        and isinstance(rerun_parsed.get("report"), str)
                        and rerun_parsed.get("report", "").strip()
                        and rerun_parsed.get("citations")
                    ):
                        parsed = rerun_parsed
                        repaired = True
                        repair_detail = "重跑完整初稿（同证据包、同模式）。"
                    else:
                        repair_detail = "重跑输出无法解析为带引用的报告 JSON，保留原稿。"
                elif repair_strategy == report_quality.REPAIR_STRATEGY_SECTION:
                    section_name = missing_now[0]
                    section_user = (
                        f"研究对象：A 股 {symbol}。用户关注点：{body.question.strip() or '综合趋势与基本面'}\n\n"
                        f"{numbered}{unavailable}\n\n"
                        f"现有报告缺失「{section_name}」小节，正文其余部分如下：\n{str(parsed.get('report', ''))[:4000]}\n\n"
                        "请只补写这一个缺失小节。请输出严格 JSON（无 markdown 围栏）：\n"
                        f'{{"section": "{section_name}", "content": "以 ### {section_name} 开头的 markdown 正文（300-600 字；'
                        '每条判断末尾挂 [S1..S5] 来源标注；证据不足时如实写「无数据」并解释原因）"}}\n'
                        "铁律：只依据给定来源；禁止编造来源里不存在的数字或事件；"
                        f"禁止重复其他小节已有的内容；禁止为通过检查补标题、重复内容或编造事实。"
                    )
                    section_reply = model_client.call_active_model(
                        profile,
                        resolve_store(core.database),
                        model_client.format_messages(
                            "你是严谨的研究编辑：只补写指定的缺失小节，绝不新增证据里不存在的信息。",
                            section_user,
                        ),
                     purpose="研报",)
                    section_parsed = macro_ai.extract_json_object(section_reply.content)
                    content = str(section_parsed.get("content", "")).strip() if isinstance(section_parsed, dict) else ""
                    if content:
                        merged_report = report_quality.merge_section(str(parsed.get("report", "")), section_name, content)
                        # 合并后引用清单 = 原清单 ∪ 新小节里真实可用的引用（避免自产「清单不一致」告警）。
                        merged_refs = report_quality.citation_consistency(
                            {"report": merged_report, "executive_summary": parsed.get("executive_summary", ""), "valuation": parsed.get("valuation")},
                            allowed=allowed_citations,
                        )["text_refs"]
                        declared_before = parsed.get("citations") if isinstance(parsed.get("citations"), list) else []
                        parsed["citations"] = sorted(
                            {str(item).strip() for item in declared_before if str(item).strip()}
                            | {ref for ref in merged_refs if ref in allowed_citations}
                        )
                        parsed["report"] = merged_report
                        repaired = True
                        repair_detail = f"只重新生成缺失小节「{section_name}」。"
                    else:
                        repair_detail = "小节补写输出为空或无法解析，保留原稿。"
                else:  # summary_only
                    summary_user = (
                        f"研报标题：{str(parsed.get('title', '')).strip()}\n"
                        f"研报正文：\n{str(parsed.get('report', ''))[:4000]}\n\n"
                        "请为这份研报补写执行摘要，四句话结构、150-165 字："
                        "①方向性结论 ②最强的基本面或估值依据 ③最重要的技术面或资金面依据 ④最大的风险/反证。"
                        "只输出摘要纯文本（不要 JSON、不要前缀、不要解释）；"
                        "只允许使用正文中已出现的信息，不得新增任何数字、事件或判断。"
                    )
                    summary_reply = model_client.call_active_model(
                        profile,
                        resolve_store(core.database),
                        model_client.format_messages(
                            "你是严谨的研究编辑：只做摘要撰写，绝不新增信息，绝不改动事实口径。",
                            summary_user,
                        ),
                     purpose="研报",)
                    written = (summary_reply.content or "").strip()
                    if written and report_quality.report_char_count(written) <= report_quality.MAX_SUMMARY_CHARS:
                        parsed["executive_summary"] = written
                        repaired = True
                        repair_detail = "只生成缺失的执行摘要（正文未改写）。"
                    else:
                        repair_detail = "摘要生成输出为空或超上限，保留原状。"
            except model_client.ModelUnavailable as exc:
                repair_detail = f"修复模型调用失败：{exc}"
            except Exception as exc:  # noqa: BLE001 - 修复尽力而为，失败保留原稿
                repair_detail = f"修复执行异常：{exc}"

            if repaired:
                # 修复后的同一闸门（一致性检查）：引用真实性/小节实质/结构口径全部重跑。
                # JV04：语义层也重跑——「缺两节及以上」的修复策略会整篇重生成 draft，claims
                # 随之改变；沿用上一轮的 Jev 判定会让展示的语义结论与最终 claims 对不上。
                # 代价是修复路径多一次 Jev 输入 token（仅修复发生时才多付，输出免费）。
                quality_repaired = report_quality.validate_report(
                    parsed,
                    allowed=allowed_citations,
                    source_asof=evidence_meta.get("source_asof") or {},
                    today=datetime.now().astimezone().date(),
                    mode=report_mode,
                    source_texts=sources,
                    jev_judge=jev_client.build_judge(core, resolve_store(core.database)),
                )
                if quality_repaired["citations"]:
                    quality = quality_repaired
                    report_revision = 2
                    repair_trace["repaired"] = quality["quality_status"] != report_quality.QUALITY_STATUS_INCOMPLETE
                    revision_history.append({
                        "revision": 2,
                        "reason": reason,
                        "strategy": repair_strategy,
                        "strategy_label": report_quality.REPAIR_STRATEGY_LABELS[repair_strategy],
                        "changes": repair_detail,
                        "result": quality["quality_status"],
                        "result_label": quality["quality_status_label"],
                        "repaired_at": datetime.now(UTC).isoformat(),
                    })
                else:
                    # 修复输出的引用均不属于本次来源 → 回滚原稿（无引用不发布，不引入虚构引用）。
                    parsed = parsed_before_repair
                    quality = quality_before_repair
                    repair_detail = "修复输出的引用均不属于本次真实来源，已回滚原稿（无引用不发布）。"
            repair_trace["detail"] = repair_detail
            repair_trace["reason"] = reason
        _audit(core, "evidence.stock_research.generated", "instrument", symbol)

        # —— 第三段（v27 方案 §3.2）：轻量反方检查（单次短 JSON 调用，不重写正文）——
        # 失败一律如实降级（`ok=False` + `detail`），**绝不影响报告本身的交付**：
        # 报告已通过引用真实性闸门，反方检查只是「自检」这一层的增量价值。
        counter_check_findings: dict[str, object] = {
            "ok": False, "requested": bool(body.with_counter_check),
            "detail": "本次未执行反方检查（请求已关闭 with_counter_check）。",
        }
        if body.with_counter_check:
            system_text, user_text = counter_check.counter_check_messages(
                symbol=symbol,
                title=str(parsed.get("title", "")),
                executive_summary=str(parsed.get("executive_summary", "")),
                report=str(parsed.get("report", "")),
                citations=list(quality["citations"]),
                claims=quality["claims"],
                # v32 §四.2：估值块与局限声明并列送审——长报告后半段（估值/情景/风险）必须覆盖。
                valuation=quality["valuation"],
                limitations=parsed.get("limitations"),
            )
            normalized = None
            failure_detail = ""
            try:
                check_reply = model_client.call_active_model(
                    profile,
                    resolve_store(core.database),
                    model_client.format_messages(system_text, user_text),
                 purpose="研报",)
                normalized = counter_check.normalize_counter_check(
                    macro_ai.extract_json_object(check_reply.content),
                    # v28：支持/反证若给出来源编号，必须是报告真实引用的来源（真实性闸门）。
                    allowed=set(quality["citations"]),
                )
            except model_client.ModelUnavailable as exc:
                failure_detail = f"反方检查模型调用失败：{exc}"
            except Exception as exc:  # noqa: BLE001 - 反方检查尽力而为，绝不拖垮报告交付
                failure_detail = f"反方检查执行异常：{exc}"
            if normalized is not None:
                counter_check_findings = {
                    "ok": True, "requested": True,
                    **normalized,
                    "model": profile.model,
                    "latency_ms": check_reply.latency_ms,
                }
            elif failure_detail:
                counter_check_findings = {
                    "ok": False, "requested": True, "detail": failure_detail,
                    "prompt_version": counter_check.COUNTER_CHECK_PROMPT_VERSION,
                }
            else:
                counter_check_findings = {
                    "ok": False, "requested": True,
                    "detail": "反方检查输出无法解析为约定 JSON（缺 strongest_counter），按未完成如实标注。",
                    "prompt_version": counter_check.COUNTER_CHECK_PROMPT_VERSION,
                }

        # —— P2（Q19/Q20）：反方检查出完后统一过一遍摘要与完成度（就地标注 + 原话留痕）——
        # —— v39 Q16/Q17/Q18：深研三块拆解，先归一再算完成度（写回 parsed 让探针看得见）——
        # 分工照旧：程序只搬运与复算，**算不出来的贡献比例一律留 null**，不替模型编数字。
        earnings_growth = report_quality.normalize_earnings_growth(
            parsed.get("earnings_growth"), allowed_sources=set(quality["citations"])
        )
        raw_cash_flow = parsed.get("cash_flow_quality") if isinstance(parsed.get("cash_flow_quality"), dict) else {}
        cash_flow_block = report_quality.cash_flow_quality(
            operating_cash_flow=raw_cash_flow.get("operating_cash_flow"),
            net_income=raw_cash_flow.get("net_income"),
            depreciation_amortization=raw_cash_flow.get("depreciation_amortization"),
            working_capital_change=raw_cash_flow.get("working_capital"),
            capex=raw_cash_flow.get("capex"),
            free_cash_flow=raw_cash_flow.get("free_cash_flow"),
            # 统计期一致才允许服务端相减出自由现金流（TTM 的现金流减 H1 的资本开支是假数）。
            operating_cash_flow_period=str(raw_cash_flow.get("operating_cash_flow_period") or ""),
            capex_period=str(raw_cash_flow.get("capex_period") or ""),
            # 正文已经写了「盈利质量较好」而口径不足时，服务端给降档说明而不是沉默。
            claimed_quality="盈利质量较好" if "盈利质量较好" in str(parsed.get("report") or "") else "",
        )
        raw_divergence = parsed.get("valuation_divergence") if isinstance(parsed.get("valuation_divergence"), dict) else {}
        divergence_block = report_quality.valuation_divergence(
            valuation=quality["valuation"],
            financials=parsed.get("derived_financials") if isinstance(parsed.get("derived_financials"), dict) else None,
            comparables=raw_divergence.get("comparables"),
            subject_model=str(
                raw_divergence.get("subject_business_model") or parsed.get("business_model") or ""
            ).strip(),
            # 数值走服务端估值快照：模型转述的「PE(TTM) 11.70」是字符串，且它常把 PB/分位漏掉。
            server_metrics=evidence_meta.get("valuation_chart") if isinstance(evidence_meta.get("valuation_chart"), dict) else None,
        )
        parsed["earnings_growth"] = earnings_growth
        parsed["cash_flow_quality"] = cash_flow_block
        parsed["valuation_divergence"] = divergence_block
        for note in (
            cash_flow_block["downgrade_note"],
            (
                f"可比样本口径提示（{divergence_block['comparable_review']['comparability_label']}）："
                f"{divergence_block['comparable_review']['note']}"
                if divergence_block["comparable_review"]["comparability"] in ("thin", "mixed") else ""
            ),
        ):
            if note:
                quality["warnings"].append(note)

        summary_overreach, research_completeness_view = _apply_p2_summary_and_completeness(
            parsed, quality, counter_check_findings, mode=report_mode
        )
        if summary_overreach["changed"]:
            # 摘要被服务端修订过就是**一次真实修订**：升 report_revision 并把留痕挂在
            # `revision_history[].summary_overreach_repair` 子键下（追问侧按条目数统计修订次数，
            # 结构细节只在本子键里，不新增顶层摘要修订键，也不碰方向研报的 executive_summary_revision_notes）。
            report_revision += 1
            revision_history.append({
                "revision": report_revision,
                "reason": summary_overreach["revision_note"],
                "result": quality["quality_status"],
                "result_label": quality["quality_status_label"],
                "revised_at": datetime.now(UTC).isoformat(),
                "summary_overreach_repair": {
                    "annotations": len(summary_overreach["revision_trace"]),
                    "kinds": sorted({
                        str(kind)
                        for item in summary_overreach["revision_trace"]
                        for kind in (item.get("kinds") or [])
                    }),
                    "counter_denials": sorted(summary_overreach["counter_denials"]),
                    "original_chars": summary_overreach["original_summary_chars"],
                    "revised_chars": summary_overreach["revised_summary_chars"],
                    "original_summary": summary_overreach["summary_original"],
                },
            })

        # v28：验证点只归一一次，结论卡与响应体共用同一份（避免两处口径漂移）。
        normalized_watchpoints = report_quality.normalize_watchpoints(parsed.get("watchpoints"))
        # JV06：给每条验证点打上「值不值得现在花额度去查」的分诊结论（排序 + 折叠，不改写、不删除）。
        # `build_shard_reviewer` 返回 None（总闸关闭 / Jev 未启用）时整层不跑——
        # 验证点一个字段都不多，回执块为 None（与相邻的 `jev` 同为「没跑就明说没跑」）。
        normalized_watchpoints, jev_followup = report_quality.apply_jev_followup_findings(
            normalized_watchpoints,
            reviewer=jev_client.build_shard_reviewer(
                core, resolve_store(core.database), purpose="jev:followup-triage"
            ),
        )
        peer_count = evidence_meta.get("peer_count")
        decision_card = report_quality.decision_card(
            symbol=symbol,
            conclusion=quality["conclusion"],
            evidence_quality=quality["evidence_quality"],
            claim_findings=quality["claim_findings"],
            counter_check=counter_check_findings,
            watchpoints=normalized_watchpoints,
            valuation=quality["valuation"],
            peer_count=peer_count if isinstance(peer_count, int) else None,
            # v32：驾驶舱三行结论词（偏强/低估/确认…）随卡下发；模型未输出时各行为空串。
            pillar_verdicts=quality.get("pillar_verdicts"),
            # Q19：四项关键研究项完成度随卡下发（反方检查后的完整视图）。
            research_completeness=research_completeness_view,
        )
        response: dict[str, object] = {
            "ok": True,
            "symbol": symbol,
            "title": str(parsed.get("title", "")).strip() or f"{symbol} 研究报告",
            "executive_summary": str(parsed.get("executive_summary", "")).strip(),
            "report": parsed["report"],
            "citations": quality["citations"],
            "citation_dropped": quality["dropped_citations"],
            "available_citations": quality["available_citations"],
            "quality_warnings": quality["warnings"],
            # v31 质量恢复（2026-09-13 方案 §2.2/§2.3）：三态质量状态 + 阻断项 + 小节实质状态。
            # incomplete 报告照常返回（保留原始模型输出与缺失项），落库标记 is_draft，不混入正式报告。
            "quality_status": quality["quality_status"],
            "quality_status_label": quality["quality_status_label"],
            "quality_blockers": quality["quality_blockers"],
            "missing_sections": quality["missing_sections"],
            "section_states": quality["section_states"],
            # v31 §八.3：正文/摘要/估值依据与引用清单的一致性检查结论。
            "citation_consistency": quality["citation_consistency"],
            # v31 §2.4：修复留痕（策略/原因/结果）+ 修订版本号与历史（每次修复必须升 report_revision）。
            "repair": repair_trace,
            "report_revision": report_revision,
            "revision_history": revision_history,
            # v31 §2.2：生成留痕（提示词版本 / 模型 / 已完成阶段 / 模式 / 证据来源），排查「新报告对应旧提示词」一类问题。
            "generation_trace": {
                "prompt_version": STOCK_REPORT_PROMPT_VERSION,
                "model": profile.model,
                "stages_completed": ["draft"],
                "report_mode": report_mode,
                "source_keys": sorted(sources.keys()),
                "summary_rewrite": summary_rewrite,
                "generated_at": datetime.now(UTC).isoformat(),
            },
            # v30（四次升级 §三/§四）：字数留痕 + 告警四级分组（证据/计算首屏展开，形式折叠）。
            "summary_chars": quality["summary_chars"],
            "summary_rewrite": summary_rewrite,
            # —— M3（D01/D02/D03，2026-09-15 第二轮路线图）：确定性口径对齐留痕 ——
            # D01/D02：正文估值表述与驾驶舱口径相反 → 已加对齐标注的逐句留痕；
            # D03：验证点时点粒度修复（原话保留在 watchpoints[].verify_by_raw）。
            "valuation_language_repairs": valuation_language_repairs,
            "valuation_language_conflicts": quality["valuation_language_conflicts"],
            "verify_by_repairs": verify_by_repairs,
            # —— P2（Q19/Q20，2026-09-19 路线图 §5）：研究完成度 + 摘要过度断言修订留痕 ——
            # Q19：四项关键研究项各 done|partial|missing，字数只作辅助提示（缺失项已进 warnings）；
            # Q20：反方检查否认过的结论在摘要里就地加注，模型原话同时留在本键的 summary_original
            #      与 revision_history[].summary_overreach_repair.original_summary（两处皆可回查）。
            "research_completeness": research_completeness_view,
            "summary_overreach": summary_overreach,
            # v39 Q16/Q17/Q18：三块拆解随报告下发（缺项如实留 null / missing，不粉饰）。
            "earnings_growth": earnings_growth,
            "cash_flow_quality": cash_flow_block,
            "valuation_divergence": divergence_block,
            "categorized_warnings": report_quality.categorize_warnings(quality["warnings"]),
            "report_sections": quality["sections"],
            "report_chars": quality["report_chars"],
            "limitations": parsed.get("limitations", []),
            "confidence": str(parsed.get("confidence", "low")),
            "prompt_policy_version": STOCK_REPORT_PROMPT_VERSION,
            # v24 结构化判断字段（可机读，供复盘/后验评估；模型未输出时为空结构，不编造）。
            "scenarios": report_quality.normalize_scenarios(parsed.get("scenarios")),
            "levels": report_quality.normalize_levels(parsed.get("levels")),
            "watchpoints": normalized_watchpoints,
            # v24 档 A/B/C：估值结构化块（PE/PB 现值、历史分位、同业位置、贵贱判断）。
            "valuation": quality["valuation"],
            # v32 价值投资升级（2026-09-13 方案 §二）：首屏驾驶舱三行结论词 +
            # 正常化盈利/三情景估值/安全边际/四态估值状态（已在 valuation 内归一）。
            "pillar_verdicts": quality["pillar_verdicts"],
            # v27「可信交付」：证据质量分级 + claim→source 覆盖结论（引用真实存在 → 引用是否支撑判断）。
            "evidence_quality": quality["evidence_quality"],
            "claims": quality["claims"],
            "claim_findings": quality["claim_findings"],
            # JV04：语义支撑层回执（可用性 / 降级清单 / 未进该层的判断 / 模型版本）。
            # 未启用时为 None —— 前端据此如实区分「这一层没跑」与「跑过且通过」。
            "jev": quality.get("jev"),
            # JV06：验证点优先级分诊回执（展示顺序 + 折叠数 + 三者留痕的原始分/展示分/置信度）。
            # 未启用时为 None —— 与上一行的 `jev` 同一惯例，不允许「没跑却看起来跑过」。
            "jev_followup": jev_followup,
            "counter_check": counter_check_findings,
            # —— v28 二次方案 §2.1/§2.3：首屏结论卡 + 摘要降级提示 + 来源数据日期 ——
            "conclusion": quality["conclusion"],
            "summary_downgrade_notes": quality["summary_downgrade_notes"],
            "decision_card": decision_card,
            "source_asof": evidence_meta.get("source_asof") or {},
            # —— v29 三次升级：模式字数预算 + 图表数据包（图表由结构化字段直接生成，证据同源）——
            "report_mode": quality.get("report_mode") or report_mode,
            "source_meta": evidence_meta.get("source_meta") or {},
            "quarterly": evidence_meta.get("quarterly") or {},
            "valuation_chart": evidence_meta.get("valuation_chart") or {},
            "price_chart": evidence_meta.get("price_chart") or {},
            "model": profile.model,
            "latency_ms": reply.latency_ms,
            "source_keys": sorted(sources.keys()),
            "source_errors": source_errors,
            "generated_at": datetime.now(UTC).isoformat(),
        }
        # v22 持久化：个股研报落库（重启不丢）；落库失败不阻断返回。
        # v31 §八.4：incomplete 报告仍保存为可回看的草稿（is_draft=true，含缺失项与原因），
        # 由前端与统计排除在正式报告数量之外，不混入正式报告。
        try:
            report_id = str(uuid4())
            record = {
                **response,
                "report_id": report_id,
                "kind": "stock",
                "subject": symbol,
                "is_draft": quality["quality_status"] == report_quality.QUALITY_STATUS_INCOMPLETE,
            }
            core.database.insert_ai_research_report(record)
            response["report_id"] = report_id
            response["is_draft"] = record["is_draft"]
        except Exception:  # noqa: BLE001 - 持久化尽力而为，生成结果优先交付
            pass
        return response

    def _resolve_model_profile(core: CoreState, profile_id: UUID | None) -> object:
        """v23 共用：按显式 profile_id 或「使用中」状态解析模型方案（与个股研报同一规则）。"""
        profiles = core.database.list_model_profiles()
        if profile_id is not None:
            profile = next((item for item in profiles if item.profile_id == profile_id), None)
            if profile is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="指定的模型方案不存在")
            return profile
        profile = model_client.active_model_profile(profiles)
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="没有「使用中」的模型方案：先在设置页把一个方案置为使用中（研究协同依赖模型出网）",
            )
        return profile

    @app.post("/evidence/compare-synthesis")
    def compare_synthesis(body: CompareSynthesisRequest, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """v23 阶段 0：多模型对比结果的跨报告综合——共识/分歧/待核验。

        铁律：分歧如实陈列，不投票、不平均；只综合给定报告内容，不新造事实。
        """
        if not core.settings.model_access_enabled:
            return {"ok": False, "stage": "model_access_disabled", "detail": "模型出网已关闭（STEWARD_MODEL_ACCESS=0），未发起任何模型请求"}
        profile = _resolve_model_profile(core, body.profile_id)
        system, user = collab_engine.synthesis_messages(
            [item.model_dump() for item in body.reports], body.question.strip(),
        )
        try:
            reply = model_client.call_active_model(
                profile,
                resolve_store(core.database),
                model_client.format_messages(system, user),
             purpose="综合对比",)
        except model_client.ModelUnavailable as exc:
            return {"ok": False, "stage": "model_call", "detail": f"模型调用失败：{exc}"}
        parsed = collab_engine.parse_synthesis(reply.content)
        if parsed is None:
            return {
                "ok": False,
                "stage": "parse",
                "detail": "综合输出无法解析为约定 JSON（summary 缺失）。",
                "raw_excerpt": reply.content[:400],
            }
        _audit(core, "evidence.compare_synthesis.generated", "report_count", str(len(body.reports)))
        return {
            "ok": True,
            **parsed,
            "model": profile.model,
            "latency_ms": reply.latency_ms,
            "generated_at": datetime.now(UTC).isoformat(),
        }

    @app.post("/evidence/collab-run")
    def collab_run_create(body: CollabRunCreate, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """v23 协同流水线①创建运行：此刻定稿证据包（证据同源），角色由 collab-next 依次推进。"""
        installation = core.database.get_plugin_installation("official.cn-market-data")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="cn-market-data plugin is not enabled"
            )
        if not core.settings.model_access_enabled:
            return {"ok": False, "stage": "model_access_disabled", "detail": "模型出网已关闭（STEWARD_MODEL_ACCESS=0），未发起任何模型请求"}
        profile = _resolve_model_profile(core, body.profile_id)
        symbol = body.symbol.strip().upper()
        tech_summary, sources, source_errors, evidence_meta = _build_stock_evidence(
            core, symbol, body.bars_limit, body.news_limit, body.announcement_limit
        )
        if not tech_summary and not sources:
            return {
                "ok": False,
                "stage": "evidence_unavailable",
                "detail": "五类输入全部取数失败，无证据可依据，不启动协同（不编造）。",
                "source_errors": source_errors,
            }
        # v23 角色级模型分配：校验阶段名合法、方案存在；未指定的角色回落 profile_id/使用中。
        stage_profiles: dict[str, str] = {}
        stage_models: dict[str, str] = {}
        invalid_stages = set(body.stage_profiles.keys()) - set(collab_engine.STAGES)
        if invalid_stages:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"stage_profiles 含未知阶段名：{sorted(invalid_stages)}（合法：{list(collab_engine.STAGES)}）",
            )
        profiles_by_id = {item.profile_id: item for item in core.database.list_model_profiles()}
        for stage_name, stage_profile_id in body.stage_profiles.items():
            stage_profile = profiles_by_id.get(stage_profile_id)
            if stage_profile is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"阶段 {stage_name} 指定的模型方案不存在")
            stage_profiles[stage_name] = str(stage_profile_id)
            stage_models[stage_name] = stage_profile.name
        # v31：模式在创建运行时校验并定稿（未知模式 422，不静默回落），随运行传递到闸门/落库/前端。
        try:
            report_mode, _mode_label, _lo, _hi = report_quality.report_mode_budget(body.mode)
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
        run = collab_engine.create_run(
            symbol=symbol,
            question=body.question.strip(),
            tech_summary=tech_summary,
            sources=sources,
            source_errors=source_errors,
            profile_id=str(body.profile_id) if body.profile_id else None,
            model=profile.model,
            stage_profiles=stage_profiles,
            stage_models=stage_models,
            # v28：来源数据日期 + 同业样本数随运行快照留存（过期判定与结论卡估值标签共用）。
            evidence_meta=evidence_meta,
            report_mode=report_mode,
        )
        _audit(core, "evidence.collab.created", "symbol", symbol)
        return {"ok": True, "run": collab_engine.public_view(run)}

    @app.post("/evidence/collab-next")
    def collab_run_next(body: CollabNextRequest, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """v23 协同流水线②执行下一个角色（用户拍板：角色不同时进行，依次执行）。

        一次调用只推进一个角色；失败阶段再次调用即重试同阶段，不跳过。
        """
        run = collab_engine.get_run(body.run_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="协同运行不存在或已过期（运行态在内存，重启后清空）"
            )
        if not collab_engine.is_done(run):
            if not core.settings.model_access_enabled:
                return {"ok": False, "stage": "model_access_disabled", "detail": "模型出网已关闭（STEWARD_MODEL_ACCESS=0），未发起任何模型请求"}
            # v23 角色级模型：当前阶段优先用 stage_profiles 指定的方案，缺省回落运行默认（使用中）。
            current_stage = run["stages"][run["next_index"]]["stage"]
            stage_profile_id = (run.get("stage_profiles") or {}).get(current_stage)
            profile = _resolve_model_profile(
                core,
                UUID(stage_profile_id) if stage_profile_id else (UUID(run["profile_id"]) if run["profile_id"] else None),
            )
            try:
                stage = collab_engine.execute_next(core, run, profile, resolve_store(core.database))
            except collab_engine.CollabBusyError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        else:
            stage = {"stage": "revise", "status": "done", "result": None}
        response: dict[str, object] = {
            "ok": True,
            "done": collab_engine.is_done(run),
            "executed_stage": stage["stage"],
            "executed_status": stage["status"],
            "run": collab_engine.public_view(run),
        }
        # 修订稿完成 → 最终报告落库（复用 ai_research_reports，模型名带「·协同流水线」标记）。
        if stage["stage"] == "revise" and stage["status"] == "done" and isinstance(stage.get("result"), dict):
            parsed = stage["result"]
            try:
                collab_meta = run.get("evidence_meta") or {}
                collab_watchpoints = report_quality.normalize_watchpoints(parsed.get("watchpoints"))
                collab_valuation = report_quality.normalize_valuation(parsed.get("valuation"))
                # v28 首屏结论卡：与个股研报端点同一口径（协同无独立反方检查 → counter_check 传 None，
                # 卡内会如实标注「本次未执行反方检查」，不拿空白当通过）。
                collab_decision_card = report_quality.decision_card(
                    symbol=run["symbol"],
                    conclusion=stage.get("conclusion") or report_quality.normalize_conclusion(parsed.get("conclusion")),
                    evidence_quality=stage.get("evidence_quality") or report_quality.evidence_quality_report(
                        run.get("source_keys") or []
                    ),
                    claim_findings=stage.get("claim_findings") or {},
                    counter_check=None,
                    watchpoints=collab_watchpoints,
                    valuation=collab_valuation,
                    peer_count=collab_meta.get("peer_count"),
                    # v32：驾驶舱三行结论词随卡下发（与单股端点同一口径）。
                    pillar_verdicts=report_quality.normalize_pillar_verdicts(parsed.get("pillar_verdicts")),
                )
                record = {
                    "ok": True,
                    "symbol": run["symbol"],
                    "title": str(parsed.get("title", "")).strip() or f"{run['symbol']} 协同研究报告",
                    "executive_summary": str(parsed.get("executive_summary", "")).strip(),
                    "report": parsed["report"],
                    "citations": parsed["citations"],
                    # v24：落库时带上质量校验痕迹与结构化判断字段（与个股研报端点同一口径）。
                    "citation_dropped": stage.get("citation_dropped", []),
                    "quality_warnings": stage.get("quality_warnings", []),
                    # v31 质量恢复（§2.2/§八.4）：三态质量状态 + 阻断项 + 缺失小节随落库留存；
                    # incomplete 报告标记 is_draft，由前端与统计排除在正式报告数量之外。
                    "quality_status": stage.get("quality_status", "needs_review"),
                    "quality_status_label": stage.get("quality_status_label", "待复核"),
                    "quality_blockers": stage.get("quality_blockers", []),
                    "missing_sections": stage.get("missing_sections", []),
                    "section_states": stage.get("section_states", []),
                    # v31 §2.2：生成留痕（提示词版本 / 各角色模型 / 已完成阶段 / 模式）。
                    "generation_trace": {
                        "prompt_version": STOCK_REPORT_PROMPT_VERSION,
                        "model": run["model"],
                        "stage_models": dict(run.get("stage_models") or {}),
                        "stages_completed": [
                            item["stage"] for item in run["stages"] if item.get("status") == "done"
                        ],
                        "report_mode": run.get("report_mode") or "standard",
                        "source_keys": list(run.get("source_keys") or []),
                        "generated_at": datetime.now(UTC).isoformat(),
                    },
                    # v30：告警四级分组（与单股端点同一口径）；collab 无摘要重写，超限仅告警。
                    "categorized_warnings": report_quality.categorize_warnings(stage.get("quality_warnings", [])),
                    "summary_chars": stage.get("summary_chars"),
                    "report_chars": stage.get("report_chars"),
                    "scenarios": report_quality.normalize_scenarios(parsed.get("scenarios")),
                    "levels": report_quality.normalize_levels(parsed.get("levels")),
                    "watchpoints": collab_watchpoints,
                    "valuation": collab_valuation,
                    # v32：驾驶舱三行结论词随落库留存（与单股研报响应同一字段口径）。
                    "pillar_verdicts": report_quality.normalize_pillar_verdicts(parsed.get("pillar_verdicts")),
                    # v27：证据分级 + claim 覆盖结论（二者已在 execute_next 里按同一口径算好并留痕）。
                    "evidence_quality": stage.get("evidence_quality"),
                    "claims": parsed.get("claims") if isinstance(parsed.get("claims"), list) else [],
                    "claim_findings": stage.get("claim_findings"),
                    # JV04：语义支撑层回执（协同链路与个股端点同一字段口径）。
                    "jev": stage.get("jev"),
                    # v28：一句话结论 + 摘要降级提示 + 首屏结论卡 + 来源数据日期（与个股端点同口径）。
                    "conclusion": stage.get("conclusion") or report_quality.normalize_conclusion(parsed.get("conclusion")),
                    "summary_downgrade_notes": stage.get("summary_downgrade_notes")
                    or report_quality.summary_downgrade_notes(stage.get("claim_findings") or {}),
                    "decision_card": collab_decision_card,
                    "source_asof": collab_meta.get("source_asof") or {},
                    # v29：图表数据包与来源元数据随落库留存（历史报告详情可复现图表，证据同源）。
                    # v31：模式来自运行定稿值（不再写死字面量），与闸门/展示共用同一 report_mode。
                    "report_mode": run.get("report_mode") or "standard",
                    "source_meta": collab_meta.get("source_meta") or {},
                    "price_chart": collab_meta.get("price_chart") or {},
                    "quarterly": collab_meta.get("quarterly") or {},
                    "valuation_chart": collab_meta.get("valuation_chart") or {},
                    "revision_notes": str(parsed.get("revision_notes", "")).strip(),
                    "limitations": parsed.get("limitations", []),
                    "confidence": str(parsed.get("confidence", "low")),
                    "prompt_policy_version": STOCK_REPORT_PROMPT_VERSION,
                    "model": f"{run['model']}·协同流水线",
                    "source_keys": run["source_keys"],
                    "source_errors": run["source_errors"],
                    "generated_at": datetime.now(UTC).isoformat(),
                    "report_id": str(uuid4()),
                    "kind": "stock",
                    "subject": run["symbol"],
                    # v31 §八.4：不完整报告仍落库为草稿（is_draft），不混入正式报告数量。
                    "is_draft": stage.get("quality_status") == report_quality.QUALITY_STATUS_INCOMPLETE,
                }
                core.database.insert_ai_research_report(record)
                # v32：落库 id 随响应返回，前端据此为协同修订稿挂 report_id（追问附录入口需要）。
                response["collab_report_id"] = record["report_id"]
            except Exception:  # noqa: BLE001 - 持久化尽力而为，执行结果优先交付
                pass
            _audit(core, "evidence.collab.completed", "run_id", run["run_id"])
        return response

    @app.get("/ai-research/reports")
    def list_ai_research_reports(
        kind: str | None = None,
        collab: bool | None = None,
        model: str | None = None,
        q: str | None = None,
        is_draft: bool | None = None,
        depth: str | None = None,
        generated_from: str | None = None,
        generated_to: str | None = None,
        limit: int = 50,
        offset: int = 0,
        count_only: bool = False,
        core: CoreState = Depends(_require_session),
    ) -> dict[str, object]:
        """AI 研究产出列表（v22；B01 桌面端升级路线图 2026-09-18：筛选/分页/计数下沉 SQL）。

        - 筛选：kind、collab（stock 内区分「协同流水线」）、model（精确）、
          q（subject/title/topic/model LIKE）、is_draft、generated_from/to（ISO 文本比较）。
        - 分页：limit（默认 50，上限 200）+ offset；51 份以上经翻页可达最旧一份。
        - items 为**轻量投影**（report_id/kind/subject/generated_at/model/is_draft/title/topic/symbol，
          不带 payload），回看全文走 GET /ai-research/reports/{report_id}。
        - counts/models 为全局真值计数与模型清单（不受筛选影响）；count_only=true 跳过 items 查询。
        """
        if kind is not None and kind not in ("direction", "stock"):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="kind 仅支持 direction | stock")
        page_limit = max(1, min(limit, 200))
        page_offset = max(0, offset)
        if depth is not None and depth != "below_min":
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="depth 仅支持 below_min")
        items, total = core.database.list_ai_research_reports_page(
            kind=kind,
            collab=collab,
            model=model,
            q=q,
            is_draft=is_draft,
            depth=depth,
            generated_from=generated_from,
            generated_to=generated_to,
            limit=page_limit,
            offset=page_offset,
        )
        facets = core.database.ai_research_report_facets()
        return {
            "ok": True,
            "items": [] if count_only else items,
            "total": total,
            "offset": page_offset,
            "limit": page_limit,
            "counts": {key: facets[key] for key in ("total", "direction", "stock", "collab", "draft_stock")},
            "models": facets["models"],
        }

    @app.get("/ai-research/reports/{report_id}")
    def get_ai_research_report(report_id: str, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """单条 AI 研究产出详情（v22）。v39：视图字段现场补齐，但**不改存储**（原 payload 逐字不变）。"""
        item = core.database.get_ai_research_report(report_id)
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="报告不存在或已删除")
        return {"ok": True, "item": _hydrate_report_for_view(item)}

    # —— Q05/Q08/Q09（2026-09-19 路线图）：旧报告的同口径回显 ——
    def _hydrate_report_for_view(item: dict[str, Any]) -> dict[str, Any]:
        """老记录缺的新口径字段按**同一套纯函数**现场补算，只加视图字段、不写回存储。

        为什么在服务端补而不是让页面各写一份：估值三态、参考价位、来源目录与「2/3」统计
        的口径必须屏显、导出、追问三处同源，否则同一条报告会在三个地方说出三种话。
        """
        hydrated = dict(item)
        valuation = hydrated.get("valuation")
        if isinstance(valuation, dict) and not valuation.get("valuation_evidence"):
            hydrated["valuation"] = {
                **valuation,
                "valuation_evidence": report_quality.derive_valuation_evidence_state(valuation),
            }
        if not hydrated.get("probability_view") and isinstance(hydrated.get("scenarios"), list):
            hydrated["probability_view"] = report_quality.scenario_probability_view(
                hydrated["scenarios"], levels=hydrated.get("levels")
            )
        if not hydrated.get("source_catalog"):
            hydrated["source_catalog"] = analysis_followup.source_catalog(
                followup_research.build_source_catalog(hydrated)
            )
        if not hydrated.get("core_support") and isinstance(hydrated.get("claim_findings"), dict):
            hydrated["core_support"] = followup_research.core_support_trace(hydrated["claim_findings"])
        return hydrated

    @app.delete("/ai-research/reports/{report_id}")
    def delete_ai_research_report(report_id: str, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """删除一条 AI 研究产出（v22，仅本机数据）。v32：追问分析附录随报告联动删除。"""
        deleted = core.database.delete_ai_research_report(report_id)
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="报告不存在或已删除")
        core.database.delete_ai_analysis_turns(report_id)
        _audit(core, "evidence.ai_research.deleted", "report_id", report_id)
        return {"ok": True, "deleted": report_id}

    # —— v32 研报追问（2026-09-13 方案 §三）：原报告不可变，追问生成追加的分析附录 ——
    @app.post("/ai-research/reports/{report_id}/follow-ups")
    def create_report_followup(
        report_id: str, body: FollowUpCreate, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        """围绕已落库研报追问（§3.1 入口）：支持追问数字/来源/核心判断、输入新信息重评、比较差异。

        v38（2026-09-14）：**个股研报与方向研判都可追问**。两者共用同一套追问契约、归一化与
        确定性闸门（`analysis_followup.normalize_followup`），差异只在送模型的骨架装配：
        - `kind == "stock"` → `followup_messages`（claims / valuation / scenarios / watchpoints）；
        - `kind == "direction"` → `direction_followup_messages`（core_judgments J# / board_judgments /
          next_verification / data_gaps）。方向研判没有 claims 清单，其「核心判断（分层）」即等价结构，
          故原先「方向研判无核心判断清单」的拒绝理由已不成立。

        铁律（§三）：
        - **原报告不可变**——本端点只追加 `ai_analysis_turns` 附录，绝不改写 ai_research_reports payload；
        - 用户补充材料**默认未独立验证**（§3.2），先事实归一化再进影响分析，不能直接当作公司事实；
        - 结果契约（§3.3）：受影响 claim（claim_id 必须是原报告已有编号，服务端闸门剔除自造编号）、
          结论变化五态、情景变化、新增验证点、回答正文与局限，全部随附录留痕（模型/提示词版本/时间）。
        """
        if not core.settings.model_access_enabled:
            return {"ok": False, "stage": "model_access_disabled", "detail": "模型出网已关闭（STEWARD_MODEL_ACCESS=0），未发起任何模型请求"}
        base = core.database.get_ai_research_report(report_id)
        if base is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="报告不存在或已删除")
        kind = str(base.get("kind") or "")
        if kind not in ("stock", "direction"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"该类型报告（kind={kind or '未知'}）暂不支持追问（当前支持：个股研报 stock、方向研判 direction）",
            )
        question = body.question.strip()
        if not question:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="追问问题不能为空白")
        mode = str(body.mode or analysis_followup.MODE_INTERPRET).strip().lower()
        if mode not in analysis_followup.MODE_LABELS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="追问入口只支持 interpret（解读本报告）或 supplement_research（补充研究）",
            )
        profile = _resolve_model_profile(core, body.profile_id)

        now_iso = datetime.now(UTC).isoformat()
        supplements = analysis_followup.normalize_supplements(
            [item.model_dump() for item in body.supplements], input_at=now_iso
        )
        if len(body.supplements) > analysis_followup.MAX_SUPPLEMENTS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"单次追问最多携带 {analysis_followup.MAX_SUPPLEMENTS} 条补充材料",
            )
        facts = analysis_followup.normalize_facts(supplements, subject=str(base.get("symbol") or base.get("subject") or ""))

        # 原报告的核心判断编号集合：闸门依据（自造编号剔除），无编号报告允许追问但影响清单会为空。
        # v38：方向研判的编号取自 core_judgments（J#），与个股研报的 claims（C#）同构。
        # Q04：同时留一份 编号→判断名称 映射，展示时「C3」要配着判断名出现，而不是只剩字母。
        claim_findings = base.get("claim_findings") if isinstance(base.get("claim_findings"), dict) else {}
        base_claims = base.get("claims") if isinstance(base.get("claims"), list) else (
            claim_findings.get("claims") if isinstance(claim_findings.get("claims"), list) else []
        )
        claims_by_id = {
            str(item.get("id") or item.get("claim_id") or "").strip(): str(item.get("text") or "").strip()
            for item in (
                (base.get("core_judgments") or []) if kind == "direction" else base_claims
            )
            if isinstance(item, dict) and str(item.get("id") or item.get("claim_id") or "").strip()
        }
        allowed_claim_ids = set(claims_by_id)

        # Q14：连续追问承接——历史轮次只带问题/直接回答/状态轴（控制上下文大小），
        # 「那等突破呢」这类省略式提问靠它定位所指条件；跨报告不会串（按 parent_report_id 取）。
        prior_turns = core.database.list_ai_analysis_turns(report_id)
        history = followup_research.history_digest(prior_turns)

        # Q11：补充研究模式才出网取数；interpret 模式一条新事实都不声称获取（Q10 验收）。
        new_data: list[dict[str, Any]] = []
        calendar: trading_calendar.TradingCalendar | None = None
        if mode == analysis_followup.MODE_RESEARCH:
            new_data, calendar = _followup_supplemental_data(core, base, question, now_iso=now_iso)
        retrieval_status = followup_research.retrieval_status_summary(new_data)

        if kind == "direction":
            system_text, user_text = analysis_followup.direction_followup_messages(
                base_report=base,
                question=question,
                supplements=supplements,
                facts=facts,
                mode=mode,
                new_data=new_data,
                prior_turns=history,
            )
        else:
            system_text, user_text = analysis_followup.followup_messages(
                base_report=base,
                question=question,
                supplements=supplements,
                facts=facts,
                mode=mode,
                new_data=new_data,
                prior_turns=history,
            )
        try:
            reply = model_client.call_active_model(
                profile,
                resolve_store(core.database),
                model_client.format_messages(system_text, user_text),
            purpose="追问",
            )
        except model_client.ModelUnavailable as exc:
            return {"ok": False, "stage": "model_call", "detail": f"追问模型调用失败：{exc}"}
        parsed = macro_ai.extract_json_object(reply.content)
        normalized = analysis_followup.normalize_followup(
            parsed,
            allowed_claim_ids=allowed_claim_ids,
            claims_by_id=claims_by_id,
            supplements=supplements,
            new_data=new_data,
            base_report=base,
            calendar=calendar,
            today=datetime.now(UTC).date().isoformat(),
            # Q13：催化事件证据链——由本轮材料确定性拼装，缺哪环就标哪环（模型不负责数环节）。
            # 只在**本轮真有材料**（补充研究取到东西或用户贴了材料）时才拼链：
            # 纯解读型追问（「最不可靠的结论是什么」）挂一条五环全缺的链条只是噪声。
            catalyst=(
                followup_research.catalyst_chain(
                    followup_research.catalyst_evidence_from(new_data, supplements)
                )
                if (new_data or supplements) else {}
            ),
        )
        if normalized is None:
            return {
                "ok": False,
                "stage": "parse",
                "detail": "追问输出无法解析为约定 JSON（缺 answer 正文），按未完成如实标注，不生成附录。",
                "raw_excerpt": reply.content[:400],
            }

        turn: dict[str, object] = {
            "ok": True,
            "analysis_turn_id": str(uuid4()),
            "parent_report_id": report_id,
            # 基础版本 = 追问时原报告的修订版本（修复会升 revision；追问锚定的是当时版本）。
            "base_report_version": int(base.get("report_revision") or 1),
            "symbol": str(base.get("symbol") or base.get("subject") or ""),
            "turn_index": len(prior_turns) + 1,
            "question": question,
            "supplement_evidence": supplements,
            "fact_normalizations": facts,
            **normalized,
            # —— v39（2026-09-19 路线图 P0/P1）：入口口径、补证结果与可回查来源 ——
            "mode": mode,
            "mode_label": analysis_followup.MODE_LABELS[mode],
            "follows_prior_turn": bool(history) and followup_research.is_follow_on(question),
            "prior_turn_ids": [str(row.get("analysis_turn_id") or "") for row in history],
            "retrieval_status": retrieval_status,
            "evidence_snapshot": followup_research.evidence_snapshot(
                new_data,
                supported_claims=[str(row.get("claim_id") or "") for row in normalized["affected_claims"]],
                analysis_version=analysis_followup.FOLLOWUP_PROMPT_VERSION,
            ),
            "source_catalog": analysis_followup.source_catalog(followup_research.build_source_catalog(base)),
            "model": profile.model,
            "created_at": now_iso,
        }
        try:
            core.database.insert_ai_analysis_turn(turn)
        except Exception:  # noqa: BLE001 - 持久化尽力而为，附录结果优先交付
             pass
        # JV06：追问**新增的验证点**同样按优先级分诊——与主报告验证点同一个纯函数、同一个
        # `purpose`，所以两处的「暂缓」是同一把尺子（不是各写一份口径的第二种说法）。
        # 分诊失败不影响追问本身交付：验证点照常随 turn 返回，只是没有 `triage`、不排序不折叠。
        if isinstance(normalized.get("new_watchpoints"), list):
            triaged_points, jev_followup = report_quality.apply_jev_followup_findings(
                normalized["new_watchpoints"],
                reviewer=jev_client.build_shard_reviewer(
                    core, resolve_store(core.database), purpose="jev:followup-triage"
                ),
            )
            turn["new_watchpoints"] = triaged_points
            turn["jev_followup"] = jev_followup
        _audit(core, "evidence.report_followup.created", "report_id", report_id)
        return {"ok": True, "turn": turn}

    # —— Q11（2026-09-19 路线图）：补充研究取数。程序负责路由/重试/算术，模型只解释证据。——
    def _followup_supplemental_data(
        core: CoreState, base: dict[str, Any], question: str, *, now_iso: str
    ) -> tuple[list[dict[str, Any]], "trading_calendar.TradingCalendar | None"]:
        """按问题取本轮 needed 的数据，逐项独立降级（成功/部分成功/全部失败都如实成块）。

        失败绝不伪装成「研究完成」：拿不到的项以 `ok=False` + 原因进块，提示词与页面都看得见；
        尚未接入的来源（一手公告原文、一致预期、月度经营简报结构化口径）直接落成接入任务。
        """
        symbol = str(base.get("symbol") or base.get("subject") or "").strip()
        plan = followup_research.plan_retrieval(question, base_report=base, mode=analysis_followup.MODE_RESEARCH)
        blocks: list[dict[str, Any]] = []
        calendar: trading_calendar.TradingCalendar | None = None
        kline_rows: list[dict[str, Any]] = []
        for step in plan:
            kind_key = str(step.get("kind") or "")
            label = str(step.get("label") or kind_key)
            if step.get("not_available"):
                blocks.append(followup_research.retrieval_block(
                    kind_key, label=label, payload=None,
                    error=str(step["not_available"]), source_name="未接入", retrieved_at=now_iso,
                ))
                continue
            try:
                if kind_key == "kline":
                    limit = int((step.get("params") or {}).get("limit") or 260)
                    rows, provider = fetch_cn_kline(symbol, limit=limit, period="day")
                    kline_rows = [row for row in (rows or []) if isinstance(row, dict)]
                    calendar = trading_calendar.calendar_from_kline_rows(kline_rows, source=str(provider or ""))
                    closes = [float(row.get("close") or 0) for row in kline_rows if float(row.get("close") or 0) > 0]
                    as_of = str(kline_rows[-1].get("timestamp") or "")[:10] if kline_rows else ""

                    def _ma(window: int) -> str:
                        if len(closes) < window:
                            return f"MA{window} 样本不足"
                        return f"MA{window} {round(sum(closes[-window:]) / window, 3)}"

                    recent = closes[-250:]
                    earliest, latest = calendar.coverage()
                    summary = (
                        f"最新收盘 {closes[-1] if closes else '—'}（行情截至 {as_of or '未标注'}）；"
                        f"{_ma(5)}｜{_ma(20)}｜{_ma(60)}；"
                        f"近 {len(recent)} 根收盘区间 {min(recent) if recent else '—'}~{max(recent) if recent else '—'}；"
                        f"交易日历覆盖 {earliest or '—'}~{latest or '—'}（均线与价位只在该时点口径内有效）"
                    )
                    blocks.append(followup_research.retrieval_block(
                        "kline", label=label,
                        payload={
                            "summary": summary, "as_of": as_of,
                            "close": closes[-1] if closes else None,
                            "rows": kline_rows[-3:],
                        },
                        source_name=str(provider or "行情源"), retrieved_at=now_iso,
                    ))
                elif kind_key == "valuation":
                    snapshot = fetch_valuation(symbol)
                    as_of = str(getattr(snapshot, "trade_date", "") or "")[:10]
                    blocks.append(followup_research.retrieval_block(
                        "valuation", label=label,
                        payload={
                            "summary": describe_valuation(snapshot), "as_of": as_of,
                            "pe_ttm": getattr(snapshot, "pe_ttm", None),
                            "pb_mrq": getattr(snapshot, "pb_mrq", None),
                        },
                        source_name="东方财富 datacenter（估值表）", retrieved_at=now_iso,
                    ))
                elif kind_key == "seasonality":
                    study = followup_research.seasonality_study(
                        kline_rows,
                        festival=followup_research.detect_festival(question),
                        benchmark_rows=study_benchmark_rows(symbol),
                    )
                    blocks.append(followup_research.retrieval_block(
                        "seasonality", label=label,
                        payload={
                            "summary": followup_research.study_brief(study),
                            "as_of": str(study.get("data_as_of") or ""),
                            "rows": study.get("windows") or [],
                        },
                        source_name="本轮行情 K 线复算", retrieved_at=now_iso,
                    ))
                else:
                    blocks.append(followup_research.retrieval_block(
                        kind_key, label=label, payload=None,
                        error="本轮未实现该取数路径（已记录为接入任务）", retrieved_at=now_iso,
                    ))
            except Exception as error:  # noqa: BLE001 - 单项失败如实成块，不拖垮整轮追问
                blocks.append(followup_research.retrieval_block(
                    kind_key, label=label, payload=None,
                    error=f"{type(error).__name__}: {error}"[:200], retrieved_at=now_iso,
                ))
        return blocks, calendar

    def study_benchmark_rows(symbol: str) -> list[dict[str, Any]]:
        """基准序列（Q12 相对大盘）：沪深 300 日线；取不到就返回空列表——只报绝对收益。"""
        del symbol  # 基准固定为大盘指数，不按标的切换（切换口径会破坏可比性）。
        try:
            rows, _provider = fetch_cn_kline("000300", limit=1200, period="day")
            return [row for row in (rows or []) if isinstance(row, dict)]
        except Exception:  # noqa: BLE001 - 无基准只降级为「不给超额收益」，不影响主链路
            return []

    @app.get("/ai-research/reports/{report_id}/follow-ups")
    def list_report_followups(
        report_id: str, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        """某报告的全部追问附录（append-only，按时间正序；原报告 payload 不被任何追问改动）。"""
        base = core.database.get_ai_research_report(report_id)
        if base is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="报告不存在或已删除")
        items = core.database.list_ai_analysis_turns(report_id)
        return {
            "ok": True,
            "parent_report_id": report_id,
            "count": len(items),
            "items": items,
            "note": "追问附录为只读追加记录；原报告不可变（方案 §三：每次追问记录父版本、证据快照、模型与提示词版本）。",
        }

    # 注：路径挂在 /ai-research 下而不是 /evidence —— `/evidence/{evidence_id}` 是参数化路由，
    # 先注册就会把 /evidence/review-queue 当成 evidence_id="review-queue"（项目铁律：
    # 精确路由必须先于参数化路由注册）。本端点派生自落库研报，归 /ai-research 语义也更贴切。
    @app.get("/ai-research/review-queue")
    def evidence_review_queue(
        limit: int = 200, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        """验证点待复盘队列（v27 方案 §3.5 + G01 后验闭环）：从**已落库研报**派生，不改写原始判断。

        三档归类（全部可由 `due_on` 复算，不猜日期）：
        - `overdue`         —— 验证时点已到（`due_on ≤ 今天`），等待人工回填「成立/失效/无数据」；
        - `scheduled`       —— 验证时点未到，给出剩余天数；
        - `event_anchored`  —— 时点锚定事件（如「三季报披露后」），无确切日期，等事件触发后再复盘。

        G01 起本端点**不再是只读**：带确切日期的验证点会**幂等**落成 `verifiable_judgments`
        记录（id 由 `report_id`+序号派生，见 `longterm.watchpoint_judgment_id`），回填走既有的
        `POST /judgments/{id}/verify`——**原研报 payload 一字不改**，回填只追加验证结果。
        锚定事件的验证点没有日期、`due_at` 无从取值，如实不落库并说明原因（不猜日期）。
        """
        # 与 /today 端点同口径取本机当天（不走 date.today()，保持 tz-aware 写法一致）。
        today = datetime.now().astimezone().date()
        items: list[dict[str, object]] = []
        counts = {"overdue": 0, "scheduled": 0, "event_anchored": 0}
        filled = 0
        overdue_unfilled = 0
        materialized = 0
        skipped_undated = 0
        for record in core.database.list_ai_research_reports(kind="stock", limit=max(1, min(limit, 200))):
            if not isinstance(record, dict):
                continue
            # 历史报告（v27 之前落库）没有 due_on / status：按同一口径现场归一，不写回存储。
            watchpoints = report_quality.normalize_watchpoints(record.get("watchpoints"))
            if not watchpoints:
                continue
            report_id = str(record.get("report_id") or "")
            symbol = str(record.get("symbol") or record.get("subject") or "")
            scenarios = record.get("scenarios") if isinstance(record.get("scenarios"), list) else []
            scenario_brief = [
                {
                    "name": str(item.get("name", "")),
                    "invalidates": str(item.get("invalidates", "")),
                    "horizon": str(item.get("horizon", "")),
                    "probability": item.get("probability"),
                }
                for item in scenarios
                if isinstance(item, dict)
            ]
            for index, point in enumerate(watchpoints):
                due_on = str(point.get("due_on") or "")
                days_until: int | None = None
                if due_on:
                    try:
                        days_until = (date.fromisoformat(due_on) - today).days
                    except ValueError:
                        days_until = None
                if days_until is not None and days_until <= 0:
                    bucket = "overdue"
                elif days_until is not None:
                    bucket = "scheduled"
                else:
                    bucket = "event_anchored"
                counts[bucket] += 1

                # G01：落到可回填的判断（幂等；锚定事件不落库并给出原因）。
                judgment, note = longterm_service.materialize_watchpoint_judgment(
                    core.database,
                    core.local_user_id,
                    report_id=report_id,
                    symbol=symbol,
                    signal=str(point.get("signal") or ""),
                    verify_by=str(point.get("verify_by") or ""),
                    expected_if_true=str(point.get("expected_if_true") or ""),
                    due_on=due_on,
                    index=index,
                )
                if judgment is None:
                    if bucket == "event_anchored":
                        skipped_undated += 1
                    status = "pending_review"
                    judgment_id: str | None = None
                    verification: dict[str, object] | None = None
                else:
                    materialized += 1
                    judgment_id = str(judgment.judgment_id)
                    status = str(getattr(judgment.status, "value", judgment.status))
                    verifications = core.database.list_judgment_verifications(judgment.judgment_id)
                    verification = verifications[-1].model_dump(mode="json") if verifications else None
                    if status != "pending":
                        filled += 1
                    elif bucket == "overdue":
                        overdue_unfilled += 1
                items.append({
                    "report_id": record.get("report_id"),
                    "symbol": record.get("symbol") or record.get("subject"),
                    "title": record.get("title"),
                    "model": record.get("model"),
                    "confidence": record.get("confidence"),
                    "prompt_policy_version": record.get("prompt_policy_version"),
                    "report_generated_at": record.get("generated_at"),
                    "signal": point.get("signal"),
                    "verify_by": point.get("verify_by"),
                    "due_on": due_on,
                    "expected_if_true": point.get("expected_if_true"),
                    # G01：判断 id 非空即可回填（POST /judgments/{id}/verify）；状态取自判断本身。
                    "judgment_id": judgment_id,
                    "verification": verification,
                    "materialization_note": note,
                    "status": status,
                    "bucket": bucket,
                    "days_until": days_until,
                    "scenarios": scenario_brief,
                })

        order = {"overdue": 0, "scheduled": 1, "event_anchored": 2}
        items.sort(key=lambda row: (
            order.get(str(row["bucket"]), 9),
            # 已到期的按「早到期」优先（days_until 越小越靠前）；无日期的按空值兜底。
            row["days_until"] if isinstance(row["days_until"], int) else 10**9,
        ))
        # —— Q21（v39）：复盘五态。只有真落进 `verifiable_judgments` 的条目才允许显示「已加入跟踪」；
        # 回填过才分「已验证 / 无法验证」，其余一律「建议观察」——本端点不对自动监控作任何承诺。
        tracking = followup_research.watchpoint_tracking_view(
            items,
            materialized_ids=[str(row.get("judgment_id") or "") for row in items if row.get("judgment_id")],
            verifications={
                str(row["judgment_id"]): dict(row["verification"])
                for row in items
                if row.get("judgment_id") and isinstance(row.get("verification"), dict)
            },
            today=today.isoformat(),
        )
        for row, state in zip(items, tracking["items"]):
            row["tracking_state"] = state["state"]
            row["tracking_label"] = state["state_label"]
            row["tracking_note"] = state["note"]
        return {
            "ok": True,
            "today": today.isoformat(),
            "counts": counts,
            # Q21：建议观察 / 已加入跟踪 / 待验证 / 已验证 / 无法验证 的汇总（含「不自动监控」声明）。
            "tracking": {key: tracking[key] for key in ("version", "counts", "summary", "note", "auto_monitoring")},
            "item_count": len(items),
            "items": items,
            # G01：闭环进度（分母都是**已落库的判断**，未落库的事件锚定点不计入分母）。
            "closure": {
                "materialized": materialized,
                "filled": filled,
                "overdue_unfilled": overdue_unfilled,
                "not_materialized": skipped_undated,
            },
            "note": (
                "带确切日期的验证点已幂等落成可回填判断（原研报不可改，回填只追加验证结果）："
                "`POST /judgments/{id}/verify`。锚定事件的验证点没有日期（判断必须带到期时间），"
                "不猜日期故不自动落库，等事件发生后再复盘。"
            ),
        }

    def _resolve_direction_theme(core: CoreState, topic: str) -> dict[str, object]:
        """A01/A03（v35）：主题类型判定 + 复合主题双解析。

        需要新浪行业板块清单才能实算「是否含产业限定」（复合主题判定），故在插件可用时
        拉一次清单（有 `_cache` 90 秒 TTL，与板块行情层共用同一份取数结果）；插件不可用
        或取数失败时退化为无清单的保守判定，**不阻断**主题解析，也**不在此处声明缺口**
        （缺口由板块行情层如实记录，避免同一缺口在证据包中重复堆叠）。

        `core` 必须显式传入：端点的 `core` 是 FastAPI 依赖注入参数，不是本函数的闭包变量。
        """
        sector_rows: list[dict[str, object]] | None = None
        try:
            installation = core.database.get_plugin_installation("official.cn-market-data")
            if installation is not None and installation.state == PluginInstallationState.ENABLED:
                sector_rows = fetch_cn_sector_list()
        except Exception as exc:  # noqa: BLE001 - 清单不可得时退化为保守判定，不拖垮请求
            logger.debug("direction theme sector list unavailable: %s", exc)
            sector_rows = None
        return direction_engine.resolve_theme_scope(topic, sector_rows)

    def _annotate_pool_board_judgments(
        pool: list[dict[str, object]],
        board_judgments: dict[str, str],
        violations: list[dict[str, object]],
        override_hits: list[dict[str, object]],
    ) -> None:
        """C02（v36）/D02：把「所属板块分类 + 象限告警」写回候选行（就地修改）。

        前端候选表的「所属板块分类」列与告警标记直接消费这里的结论——分类判定与
        违规判定都由服务端完成，前端不重复实现匹配逻辑，避免两端口径漂移。

        `board_judgments` / `violations` 为空（非风格主题，或旧逻辑路径）时**不改动候选行**，
        字段缺省即代表「无该维度数据」，前端整列按 `—` 渲染，不猜测分类。
        """
        if not board_judgments and not violations:
            return
        violation_by_symbol = {
            str(item.get("symbol") or ""): item for item in (violations or [])
        }
        override_by_symbol = {
            str(item.get("symbol") or ""): item for item in (override_hits or [])
        }
        names = list(board_judgments.keys())
        for row in pool or []:
            symbol = str(row.get("symbol") or "")
            sector = str(row.get("sector") or "").strip()
            board = ""
            if sector:
                if sector in board_judgments:
                    board = sector
                else:
                    board = next(
                        (
                            name
                            for name in names
                            if name and (sector in name or name in sector)
                        ),
                        "",
                    )
            if board:
                row["pool_board_name"] = board
                row["pool_board_judgment"] = board_judgments.get(board, "")
            hit = violation_by_symbol.get(symbol)
            if hit:
                row["pool_quadrant_violation"] = True
            argument = override_by_symbol.get(symbol)
            if argument:
                row["pool_override_argument"] = str(argument.get("override_argument") or "")

    def _macro_calendar_readings(core: CoreState) -> dict[str, dict]:
        """v37：收集各路宏观指标实际值，供事件日历关联（`{region}:{indicator}` → 读数）。

        复用既有 macro_feed 管道（FRED / 世界银行 / 东财），**不新增网络源**：只对事件
        日历里 `linked_series` 涉及的 region 取一次快照。任何一路失败即跳过该路，
        缺失保持 None（事件证据文本会如实声明「实际值未接入」），不编造、不换源。
        """
        regions: set[str] = set()
        for definition in macro_calendar_module.EVENT_DEFS:
            series = definition.get("linked_series")
            if series and ":" in series:
                regions.add(series.split(":", 1)[0])
        readings: dict[str, dict] = {}
        store = resolve_store(core.database)
        for region in sorted(regions):
            try:
                snapshot = macro_feed.get_region_snapshot(core.database, store, region)
            except Exception:  # noqa: BLE001 - 单路宏观取数失败不影响证据包（缺口如实声明）
                continue
            if snapshot is None:
                continue
            for row in snapshot.indicators or []:
                if row.status != "ok" or row.latest is None:
                    continue  # pending / 无值不进 readings（不编造）
                readings[f"{region}:{row.indicator}"] = {
                    "latest": row.latest,
                    "obs_date": row.obs_date,
                    "unit": row.unit,
                    "source": f"{row.label}（{row.note}）",
                    "status": row.status,
                    # C01：数据频率随读数传递——月度事件不得把年更值当「当月实际值」。
                    "frequency": row.frequency,
                }
        return readings

    def _build_seat_evidence(
        core: CoreState, symbol: str, *, lookback_days: int = 1
    ) -> tuple[str, str, dict[str, str]] | None:
        """P2-C04：个股研报 S6（龙虎榜席位）。

        返回 `(摘要文本, 最新上榜日, 取数失败说明)`；**该票未上榜时返回 None**——
        调用方据此不追加 S6（不生成空表、不写「游资没进」）。

        与 S1–S5 的差别就在这里：S1–S5 总是尝试取数，S6 只在确有上榜时才存在。
        这也是 `report_quality.OPTIONAL_SOURCE_IDS` 里 S6 必须可缺省的原因。

        `lookback_days` 默认 1（当日），可配置 1–5：龙虎榜按交易日披露，
        非交易日取不到是**正常**情况，此时返回 None 而不是报错。
        """
        import investment_steward_core.lhb_feed as lhb_feed_module
        import investment_steward_core.seat_book as seat_book_module

        code = normalize_instrument(symbol).key
        if not code:
            return None
        # 0 = 显式关闭该来源（配置项），不要跑成「今日」。
        if int(lookback_days) <= 0:
            return None

        failures: dict[str, str] = {}
        days = _recent_trade_days(core, lookback_days)
        billboard_rows: list[object] = []
        seat_rows: list[object] = []
        for day in days:
            try:
                billboard_rows.extend(lhb_feed_module.fetch_billboard(day))
            except Exception as error:  # noqa: BLE001 - 单日失败不阻断其余交易日
                failures[f"lhb_billboard_{day}"] = f"龙虎榜列表取数失败（{day}）：{error}"
                continue
            try:
                seat_rows.extend(lhb_feed_module.fetch_seats(day))
            except Exception as error:  # noqa: BLE001 - 席位明细失败时列表仍可用
                failures[f"lhb_seats_{day}"] = f"龙虎榜席位明细取数失败（{day}）：{error}"

        if not billboard_rows:
            return None

        watchlist = seat_book_module.load_watchlist()
        summary = seat_book_module.build_billboard_summary(
            code, billboard_rows, seat_rows, watchlist, lookback_days=lookback_days
        )
        if summary is None:
            # 该票未上榜：不生成 S6（取数失败与否都不伪造席位内容）。
            return None
        latest = max(
            (str(getattr(row, "trading_day", "")) for row in billboard_rows
             if str(getattr(row, "security_code", "")) == code),
            default="",
        )
        return summary, latest, failures

    def _recent_trade_days(core: CoreState, count: int) -> list[str]:
        """最近 `count` 个自然日（含今日），新的在前。

        刻意**不**猜交易日历：中金所/东财在非交易日返回空，调用方据此如实降级。
        自建一份节假日表会引入一个需要长期维护、且错了很难发现的口径。
        """
        today = datetime.now(UTC).date()
        span = max(1, min(int(count), 5))
        return [(today - timedelta(days=offset)).strftime("%Y%m%d") for offset in range(span)]

    def _cffex_spot_closes(core: CoreState) -> dict[str, float]:
        """本机已有的股指现货收盘价（基差前置条件，P1-B04）。

        只复用既有行情管道，**不**为此新开外部指数行情抓取：指数历史接口不稳定
        （实测 `push2his` 主机连续请求后拒连），为一个可选字段引入不可靠依赖不划算。

        已知边界（如实说明，不糊弄）：既有 `fetch_cn_kline` 的代码前缀规则
        （5/6 → 沪、其余 → 深）是按**个股**设计的，指数代码走这条管道可能取不到，
        因此本函数**经常**返回空 dict。这不是缺陷，而是「基差未计算」成为常态的原因；
        `cffex_feed` 会如实把这一行写成「基差未计算」，绝不用别的指数冒充。

        任何异常都吞掉并返回空 dict——基差缺失只影响基差那一行，不该影响席位统计本体。
        """
        closes: dict[str, float] = {}
        try:
            installation = core.database.get_plugin_installation("official.cn-market-data")
            if installation is None or installation.state != PluginInstallationState.ENABLED:
                return {}
            for spot_code in sorted(set(cffex_feed.CFFEX_INDEX_SPOT_MAP.values())):
                try:
                    rows, _source = fetch_cn_kline(spot_code, limit=1, period="day")
                except Exception:  # noqa: BLE001, S112 - 单指数缺数跳过继续，不影响其余品种
                    continue
                if not rows:
                    continue
                close = rows[-1].get("close")
                if isinstance(close, (int, float)) and close > 0:
                    closes[spot_code] = float(close)
        except Exception:  # noqa: BLE001 - 基差是可选增强，失败静默降级为「未计算」
            return {}
        return closes

    def _build_direction_evidence(
        core: CoreState, topic: str, theme_scope: dict[str, object] | None = None
    ) -> tuple[str, list[dict[str, object]], str, dict[str, object]]:
        """方向研判证据快照（v33 路线图 B03/B04 稳定来源组合 + v34 板块行情层）。

        来源：宏观事件影响层（official.macro-radar）+ 未来 30 天数据发布日历
        + 板块行情层（official.cn-market-data：主题命中行业板块 → 当日板块快照与
        成交额前五成分）+ 细分行业数据层（M2-C04/C05：快递/航运官方来源，
        主题关键词命中才取，覆盖率如实标注）。上市公司分企量价、财报业务说明等
        未接入，数据缺口如实声明，不假装已覆盖。每条带 evidence_id / 来源 / 数据截至 /
        抓取时间（B04 快照口径）；来源失败进 unavailable_note，不静默换源。
        返回 (evidence_block, snapshot, unavailable_note)；快照为空 → 调用方按 knowledge 模式。

        v35 扩展：`theme_scope` 为 A03 双解析结果——复合主题（如「低估的银行」）的
        板块行情层与估值横截面层同时注入，横截面层收窄到产业限定板块内。

        v37 修复：宏观日历事件带 `phase`（发布阶段）与**实际值**——此前窗口型事件
        （如美国 CPI）在窗口期内一律被当成「未来事件」，导致已发布数据被写成「未公布的
        最大不确定性」。现在 `elapsed` 事件按规则推算应已发布，并从 macro_feed 关联实际值；
        实际值缺失时证据文本显式声明「实际值未接入」，不编造。

        C03：额外返回结构化 `valuation_outcome`
        （`{"cross_section_ok", "sector_percentile_failures"}`），供端点归类四类缺口；
        旧调用方按 3 元组解包的地方已同步更新。
        """
        snapshot: list[dict[str, object]] = []
        unavailable: list[str] = []
        valuation_outcome: dict[str, object] = {
            "cross_section_ok": True,
            "sector_percentile_failures": [],
            # C02/C03（v36）：板块名 → 系统参考分类 / 估值证据[E#]（质量闸门入参）。
            "board_judgments": {},
            "board_evidence": {},
            # C06（M2）：板块名 → 是否临界（PE 分位 − PB 分位背离 30–40pp）。
            "board_critical": {},
        }
        seq = 0
        try:
            installation = core.database.get_plugin_installation("official.macro-radar")
            if installation is None or installation.state != PluginInstallationState.ENABLED:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="macro-radar plugin is not enabled")
            impacts = macro_feed.get_event_impacts(core.database, resolve_store(core.database))
            for event in (impacts or [])[:4]:
                rows_text: list[str] = []
                for row in (event.get("rows") or [])[:4]:
                    if row.get("status") != "ok" or row.get("latest") is None:
                        continue
                    unit = f" {row['unit']}" if row.get("unit") else ""
                    rows_text.append(f"{row.get('label')}={row.get('latest')}{unit}（截至 {row.get('obs_date') or '未知'}）")
                content = "；".join(rows_text) or "该事件指标暂无可用数值（pending，不编造）"
                seq += 1
                snapshot.append({
                    "evidence_id": f"E{seq}",
                    "source_type": "macro_event",
                    "source": "macro-radar 事件影响层（FRED/官方统计管道）",
                    "title": str(event.get("title", "")),
                    "event_id": str(event.get("event_id", "")),
                    "content": content,
                    "retrieved_at": datetime.now(UTC).isoformat(),
                })
        except HTTPException:
            unavailable.append("宏观事件影响层未启用（official.macro-radar 插件未安装/未启用）")
        except Exception as exc:  # noqa: BLE001 - 单来源失败如实标注，不拖垮证据包
            unavailable.append(f"宏观事件取数失败：{exc}")
        try:
            calendar = macro_calendar_module.calendar_snapshot(
                datetime.now(UTC).date(), days=30, readings=_macro_calendar_readings(core)
            )
            for event in calendar.events[:4]:
                seq += 1
                window = f"～{event.window_end}" if event.window_end else ""
                window_text = f"{event.date}{window}"
                # —— v37：按发布阶段改写表述，消除「把已发布数据当未来催化」 ——
                phase_parts = [f"{event.region} {event.label}：{window_text}"]
                if event.phase == "elapsed":
                    phase_parts.append("窗口已过，按规则推算官方应已发布")
                elif event.phase == "in_window":
                    phase_parts.append("窗口进行中（官方可能已发布，具体以官方日历为准）")
                else:
                    phase_parts.append("窗口未开始")
                if event.actual_value is not None:
                    unit = f" {event.actual_unit}" if event.actual_unit else ""
                    obs = f"，观测 {event.actual_obs_date}" if event.actual_obs_date else ""
                    source = f"，来源 {event.actual_source}" if event.actual_source else ""
                    # —— v39：主口径名称 + 并列口径读数 ——
                    # 同一指标不同口径数值不同（美国 CPI 季调 vs 未季调），必须让使用者
                    # 看清「这个数字是哪个口径」，否则会被单一数字误导。
                    variant = f"（{event.actual_variant_label}口径）" if event.actual_variant_label else ""
                    # C01：月度读数明示「月度」频率与滞后风险（IMF 口径 obs_date 明显滞后当期）。
                    freq_note = "（月度值）" if (event.actual_frequency or "monthly") == "monthly" else ""
                    phase_parts.append(f"**实际值 {event.actual_value}{unit}{variant}{freq_note}**{obs}{source}")
                    others = [
                        item for item in (event.actual_variants or [])
                        if str(item.get("series") or "") != (event.linked_series or "")
                    ]
                    if others:
                        alt = "；".join(
                            f"{item.get('label')} {item.get('value')}{(' ' + str(item.get('unit'))) if item.get('unit') else ''}"
                            f"{('，观测 ' + str(item.get('obs_date'))) if item.get('obs_date') else ''}"
                            for item in others
                        )
                        phase_parts.append(
                            f"**同指标其他口径（数值差异来自口径，非数据错误）：{alt}**"
                        )
                else:
                    # C01：只有年更年度背景读数时，明示「实际值未接入」+ 年度背景不可当月值。
                    if event.annual_background:
                        bg = "；".join(
                            f"{item.get('label')} {item.get('value')}{(' ' + str(item.get('unit'))) if item.get('unit') else ''}"
                            f"{('，观测 ' + str(item.get('obs_date'))) if item.get('obs_date') else ''}"
                            f"{('，来源 ' + str(item.get('source'))) if item.get('source') else ''}"
                            for item in event.annual_background
                        )
                        phase_parts.append(
                            f"**实际值未接入月度管道（不以规则推算代替实测，须查官方发布）**；"
                            f"年度背景（{bg}）——年度值仅作长期背景，不可用于说明本次月度发布值"
                        )
                    else:
                        phase_parts.append("实际值未接入本管道（不以规则推算代替实测，须查官方发布）")
                phase_parts.append(f"（{event.note}）")
                snapshot.append({
                    "evidence_id": f"E{seq}",
                    "source_type": "macro_calendar",
                    "source": "官方固定发布节奏推算 + 宏观实际值关联（macro_calendar/macro_feed）",
                    "title": event.label,
                    "content": "；".join(phase_parts),
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "phase": event.phase,
                })
        except Exception as exc:  # noqa: BLE001
            unavailable.append(f"宏观数据日历取数失败：{exc}")
        # —— v34 板块行情层：主题命中行业板块 → 当日板块快照（新浪行业板块，cn-market-data 管道）——
        try:
            installation = core.database.get_plugin_installation("official.cn-market-data")
            if installation is None or installation.state != PluginInstallationState.ENABLED:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="cn-market-data plugin is not enabled")
            sector_rows = fetch_cn_sector_list()
            # A03（v35）：直接复用主题双解析的板块命中结果（同一份清单，不重复匹配）；
            # 未传 theme_scope 时（旧调用路径）在此就地匹配，行为与 v34 一致。
            scope_matched = theme_scope.get("sector_rows_matched") if theme_scope else None
            matched = (
                list(scope_matched)
                if isinstance(scope_matched, list)
                else direction_engine.match_sector_rows(topic, sector_rows)
            )
            if not matched:
                unavailable.append(f"主题「{topic}」未命中行业板块清单（风格/概念类主题无板块行情证据，缺口如实声明）")
            for row in matched:
                members: list[dict[str, object]] = []
                try:
                    members = fetch_cn_sector_members(str(row.get("code") or ""), top=5)
                except FeedError:
                    members = []  # 成分股缺数不拖垮板块行证据，仅少一截快照
                code = str(row.get("code") or "")
                rank = next(
                    (index + 1 for index, item in enumerate(sector_rows) if str(item.get("code")) == code),
                    0,
                )
                seq += 1
                snapshot.append({
                    "evidence_id": f"E{seq}",
                    "source_type": "sector_market",
                    "source": "新浪行业板块行情（当日快照，cn-market-data 插件管道）",
                    "title": f"板块「{row.get('name')}」当日行情与成交额前五成分",
                    "sector_code": code,
                    "content": direction_engine.describe_sector_snapshot(
                        row, members, rank=rank, total=len(sector_rows)
                    ),
                    "retrieved_at": datetime.now(UTC).isoformat(),
                })
        except HTTPException:
            unavailable.append("板块行情层未启用（official.cn-market-data 插件未安装/未启用）")
        except Exception as exc:  # noqa: BLE001
            unavailable.append(f"板块行情取数失败：{exc}")
        # —— B02（v35）板块估值横截面层：风格/复合主题 → 全市场当日估值横截面榜单 ——
        style_terms = list(theme_scope.get("style_terms") or []) if theme_scope else []
        if style_terms:
            scope_names = list(theme_scope.get("sector_names") or []) if theme_scope else []
            # A03：把产业限定板块名展开为可匹配东财 BOARD_NAME 的名字集合
            # （新浪「金融行业」≠ 东财「银行」，须补入主题核心词「银行」）。
            if scope_names:
                scope_names = direction_engine.expand_scope_board_names(topic, scope_names)
            seq, valuation_outcome = _append_sector_valuation_evidence(
                snapshot, unavailable, seq, scope_names
            )
        # —— M2-C04/C05：细分行业数据层（快递/航运等官方来源，主题关键词命中才取）——
        # 行业证据失败进 unavailable（不静默），成功条目带 source_url 可追溯。
        try:
            industry_items, industry_failures = direction_industry_evidence(topic)
            for item in industry_items:
                seq += 1
                snapshot.append({"evidence_id": f"E{seq}", **item})
            unavailable.extend(industry_failures)
        except Exception as exc:  # noqa: BLE001 - 单来源失败如实标注，不拖垮证据包
            unavailable.append(f"细分行业数据取数失败：{exc}")
        # —— P1-B03（席位证据）：中金所股指期货席位统计，**常挂**来源 ——
        # 期货侧的市场结构（谁在增仓/减仓）比再搜一篇新闻有用，因此它是「常挂」而非
        # 「主题命中才取」：方向研判默认就应该看到指数拥挤还是空仓。
        # 失败只记 unavailable（即 source_errors 口径），照常产出其余来源；
        # 没有当日文件就如实降级，不回填、不编造席位数字（B02）。
        try:
            seat_snapshot, seat_reason = cffex_feed.try_fetch_snapshot(
                datetime.now(UTC).date(),
                spot_closes=_cffex_spot_closes(core),
            )
            if seat_snapshot is None:
                unavailable.append(f"中金所股指期货席位统计未取得：{seat_reason}")
            else:
                seq += 1
                snapshot.append({
                    "evidence_id": f"E{seq}",
                    "source_type": "cffex_seat",
                    "source": "中金所公开日频统计与会员排名（cffex.com.cn）",
                    "title": f"中金所股指期货席位统计（交易日 {seat_snapshot.trading_day}）",
                    "trading_day": seat_snapshot.trading_day,
                    "content": cffex_feed.describe_snapshot(seat_snapshot),
                    "retrieved_at": datetime.now(UTC).isoformat(),
                })
        except Exception as exc:  # noqa: BLE001 - 单来源失败如实标注，不拖垮证据包
            unavailable.append(f"中金所席位取数失败：{exc}")
        block = "\n".join(
            f"[{item['evidence_id']}] {item['source_type']} · {item['title']} · {item['content']}"
            for item in snapshot
        )
        return block, snapshot, "；".join(unavailable), valuation_outcome

    def _attach_pool_valuation_refs(pool: list[dict[str, object]]) -> list[str]:
        """B05（v35）：候选池逐股估值回链 —— 给每只身份核验通过的候选补估值证据。

        对每只 `identity_status == "verified"` 的候选执行 `fetch_valuation`，把
        `describe_valuation` 摘要挂到候选条目（`valuation_ref` 指向 `raw_locator_for_valuation`
        的来源定位），并把关键分位做成结构化字段供前端展示。

        取数失败 / 冷却期 → 该候选**保留**但标注「估值证据缺失」（不静默丢字段），
        失败原因汇总进 `data_gaps` 的补证动作。返回缺口说明列表。

        C03：失败股票清单由函数内 `missing` 收集后统一成文本返回，端点另调
        `style_gap_entries(pool_valuation_failures=...)` 生成带补证动作的缺口条目；
        两者不重复进 `data_gaps`（由 `merge_data_gaps` 去重）。
        """
        gaps: list[str] = []
        missing: list[str] = []
        for item in pool:
            if item.get("identity_status") != direction_engine.IDENTITY_VERIFIED:
                # 身份未核验 / 无效的候选不取数（避免对错误代码发出请求）。
                item["valuation_ref"] = ""
                item["valuation"] = None
                item["valuation_status"] = "not_applicable"
                item["valuation_status_label"] = "身份未核验，未做估值回链"
                continue
            symbol = str(item.get("symbol") or "")
            if not symbol:
                item["valuation_status"] = "unavailable"
                item["valuation_status_label"] = "估值证据缺失（无标准代码）"
                continue
            if valuation_evidence.symbol_in_cooldown(symbol):
                item["valuation_ref"] = ""
                item["valuation"] = None
                item["valuation_status"] = "cooldown"
                item["valuation_status_label"] = "估值证据缺失（估值源冷却期内跳过取数）"
                missing.append(f"{item.get('name') or symbol}({symbol})")
                continue
            try:
                snapshot = fetch_valuation(symbol)
            except Exception as exc:  # noqa: BLE001 - 单股失败不拖垮整份报告
                item["valuation_ref"] = ""
                item["valuation"] = None
                item["valuation_status"] = "failed"
                item["valuation_status_label"] = "估值证据缺失（取数失败）"
                item["valuation_error"] = str(exc)[:200]
                missing.append(f"{item.get('name') or symbol}({symbol})")
                continue
            percentiles = snapshot.percentiles or {}
            item["valuation_ref"] = valuation_evidence.raw_locator_for_valuation(snapshot)
            item["valuation"] = {
                "trade_date": snapshot.trade_date,
                "board_name": snapshot.board_name,
                "pe_ttm": snapshot.pe_ttm,
                "pb_mrq": snapshot.pb_mrq,
                "ps_ttm": snapshot.ps_ttm,
                "pe_ttm_percentile": percentiles.get("pe_ttm"),
                "pb_mrq_percentile": percentiles.get("pb_mrq"),
                "percentile_window_bars": snapshot.percentile_window_bars,
                "peer_median": snapshot.peer_median,
                "peer_rank": snapshot.peer_rank,
                "peer_rank_basis": snapshot.peer_rank_basis,
                "summary": describe_valuation(snapshot),
            }
            item["valuation_status"] = "attached"
            item["valuation_status_label"] = "估值证据已回链"
        if missing:
            gaps.append(
                f"候选池估值证据缺失（{'、'.join(missing[:5])}）："
                f"估值源（{valuation_evidence.VALUATION_SOURCE_LABEL}）恢复后重跑可补齐。"
            )
        return gaps

    def _pool_valuation_failure_symbols(pool: list[dict[str, object]]) -> list[str]:
        """C03：从候选池回链结果里抽出「估值证据缺失」的股票标签（名称+代码）。

        与 `_attach_pool_valuation_refs` 同源口径，只返回人类可读标签供缺口条目使用；
        身份未核验（`not_applicable`）的候选**不算**估值缺失（根本没发请求，不是取数失败）。
        """
        labels: list[str] = []
        for item in pool:
            status = str(item.get("valuation_status") or "")
            if status not in {"failed", "cooldown"}:
                continue
            symbol = str(item.get("symbol") or "").strip()
            name = str(item.get("name") or "").strip()
            label = f"{name}({symbol})" if name and symbol else (symbol or name)
            if label:
                labels.append(label)
        return labels

    def _append_sector_valuation_evidence(
        snapshot: list[dict[str, object]],
        unavailable: list[str],
        seq: int,
        board_names: list[str],
    ) -> tuple[int, dict[str, object]]:
        """B02+B03（v35）：板块估值横截面榜单 + 代表股分位附录 → `sector_valuation` 证据条目。

        与既有 `macro_event`/`sector_market` 条目同构（evidence_id / source_type / source /
        title / content / retrieved_at）。`board_names` 非空时为 A03 复合主题的产业限定，
        横截面只在该限定板块内聚合。取数失败进 unavailable_note 如实降级，不假装有证据。

        取数预算（B04）：横截面 **1 次**（分页 1~14 页，走 `_query` 90 秒缓存）；
        代表股至多 `SECTOR_PROXY_BOARD_LIMIT` 个板块 × `REPRESENTATIVE_MAX_PER_BOARD` 股，
        并按 `SECTOR_PROXY_STOCK_BUDGET` 硬封顶，预算用尽时按榜单顺序截断并声明覆盖范围。

        C03：返回 `(seq, outcome)`——`outcome` 为结构化取数结果
        `{"cross_section_ok", "sector_percentile_failures"}`，供端点归类四类缺口；
        缺口文本仍同时进 `unavailable_note`（证据层自述），`data_gaps` 侧由 C03 统一构造。
        """
        aggregator = valuation_evidence
        outcome: dict[str, object] = {
            "cross_section_ok": True,
            "sector_percentile_failures": [],
            # C06：失败类别（request_failed/empty_data/insufficient_sample）与降级标记。
            "cross_section_failure_kind": "",
            "cross_section_degraded": False,
            # C02/C03（v36）：板块名 → 该板块估值证据 [E#] / 系统参考分类。
            "board_evidence": {},
            "board_judgments": {},
        }
        try:
            # C06：横截面走「实时失败 → 整份带日期缓存降级」路径；失败类别结构化留痕。
            trade_date, rows, degraded_note = aggregator.cross_section_with_fallback()
        except ValuationError as exc:
            outcome["cross_section_ok"] = False
            outcome["cross_section_failure_kind"] = str(
                getattr(exc, "failure_kind", "request_failed")
            )
            scope_note = f"（限定板块：{'、'.join(board_names)}）" if board_names else ""
            unavailable.append(
                f"板块估值横截面取数失败{scope_note}，风格筛选无数据依据，缺口如实声明：{exc}"
            )
            return seq, outcome
        if degraded_note:
            # C06：降级快照仍产出有限比较（板块中位数带 trade_date），但时点声明必须可见。
            outcome["cross_section_degraded"] = True
            unavailable.append(f"估值横截面降级：{degraded_note}")
        # C06：降级时每条估值证据的 source 都带时点声明（读者不必翻 unavailable 才知道）。
        valuation_source_label = f"{aggregator.VALUATION_SOURCE_LABEL}（东财行业板块口径，当日横截面）"
        if degraded_note:
            valuation_source_label += "（降级：缓存快照，非实时，注明时点）"
        # 产业限定（复合主题）：在聚合侧按板块名过滤，宁少不错配。
        if board_names:
            board_codes = {
                item.board_code
                for item in aggregator.aggregate_sector_valuations(rows, trade_date=trade_date)
                if aggregator._board_name_in(item.board_name, list(board_names))
            }
            rows = [item for item in rows if item.board_code in board_codes]
        aggregates = aggregator.aggregate_sector_valuations(rows, trade_date=trade_date)
        if not aggregates:
            outcome["cross_section_ok"] = False
            outcome["cross_section_failure_kind"] = "insufficient_sample"
            unavailable.append(
                "板块估值横截面未产出可排序板块（可比样本不足或数据缺失），"
                "风格筛选无数据依据，缺口如实声明"
            )
            return seq, outcome
        scope_note = f"，限定板块 {'、'.join(board_names)}" if board_names else ""
        # —— B03 代表股分位附录（预算：板块数 × 每股数硬封顶）——
        # —— v36 顺序调整（A03/A04）：先初筛 → 再取分位（先深取名单）→ 再用分位排序 ——
        # v35 顺序是「先按绝对 PB 排序取前 N 板块 → 再取分位」；v36 排序主键改为分位，
        # 故必须**先取分位再排序**，否则排序用的还是绝对 PB（口径与榜单不符）。
        stock_budget = valuation_evidence.SECTOR_PROXY_STOCK_BUDGET
        used_stocks = 0
        truncated: list[str] = []
        # A04（v36）初筛闸门：亏损面 ≥ 阈值的板块不入深取名单，改列对照清单（仍进报告）。
        prescreen = aggregator.prescreen_sector_valuations(
            aggregates,
            loss_ratio_max=aggregator.VAL_PRESCREEN_LOSS_RATIO_MAX,
            contrast_top_n=aggregator.VAL_PRESCREEN_CONTRAST_TOP_N,
        )
        if prescreen.excluded_note:
            unavailable.append(prescreen.excluded_note)
        # 取数顺序：深取名单优先（预算花在质量过闸板块上，按绝对 PB 由低到高），
        # 再补充其余板块（按绝对 PB 顺序占位），保证对照清单板块也有机会拿到分位。
        ordered_all = sorted(
            [i for i in aggregates if i.pb_mrq_median is not None],
            key=lambda i: i.pb_mrq_median,
        )
        fetch_order = list(prescreen.deep_fetch)
        seen_codes = {item.board_code for item in fetch_order}
        for item in ordered_all:
            if item.board_code not in seen_codes:
                fetch_order.append(item)
                seen_codes.add(item.board_code)
        # 代表股分位：board_code → PB 分位中位数；同时缓存附录文本与失败原因，避免二次取数。
        percentiles: dict[str, float | None] = {}
        appendix_by_code: dict[str, object] = {}
        for agg in fetch_order:
            if used_stocks >= stock_budget:
                break
            allowance = min(
                valuation_evidence.REPRESENTATIVE_MAX_PER_BOARD, stock_budget - used_stocks
            )
            appendix, failures = aggregator.fetch_board_percentile_appendix(
                rows, agg.board_code, agg.board_name, limit=allowance
            )
            used_stocks += appendix.representative_count + len(failures)
            appendix_by_code[agg.board_code] = (appendix, failures)
            percentiles[agg.board_code] = appendix.pb_percentile_median
            if appendix.representative_count == 0 and failures:
                # C03：代表股分位不可得（板块级）——结构化留痕供端点归类缺口。
                percentile_failures = list(outcome["sector_percentile_failures"])
                percentile_failures.append(str(agg.board_name or agg.board_code))
                outcome["sector_percentile_failures"] = percentile_failures
                unavailable.append(
                    f"板块「{agg.board_name or agg.board_code}」代表股分位全部取数失败，"
                    f"该板块仅保留估值中位数（代表股分位不可得）：{'；'.join(failures[:3])}"
                )
        # —— A03（v36）榜单排序：主键 = 代表股 PB 分位中位数升序；分位不可得不上榜 ——
        # 对照清单板块若分位不可得则不上榜，但保证由榜单外的补充条目仍出现在报告中。
        ranking = aggregator.rank_sector_valuations(aggregates, percentiles=percentiles)
        ranked_codes = {entry.aggregate.board_code for entry in ranking}
        # A03 披露：有可比样本但**未取到分位**（预算用尽/分位不可得）的板块不进榜单，
        # 必须如实声明其缺席，否则报告会静默少掉板块。
        no_percentile = [
            item for item in aggregates
            if item.board_code not in ranked_codes
            and item.pb_mrq_median is not None
            and item.pb_mrq_positive_count >= valuation_evidence.SECTOR_RANKING_MIN_SAMPLES
            and item.board_code not in {c.board_code for c in prescreen.contrast}
        ]
        if no_percentile:
            truncated.append(
                "以下板块有可比样本但未取得代表股分位，按 A03 口径未进榜单："
                + "、".join(str(i.board_name or i.board_code) for i in no_percentile[:5])
            )
        # A04 对照清单：亏损面过大被排除的板块仍进报告（披露原因），排在正式榜单之后。
        contrast_missing = [
            item for item in prescreen.contrast if item.board_code not in ranked_codes
        ]
        # 榜单板块数超过代表股覆盖上限时，如实声明只对前 N 个板块补分位附录。
        over_limit = [
            entry for entry in ranking if entry.aggregate.board_code not in percentiles
        ]
        if over_limit:
            truncated.append(
                f"榜单第 {over_limit[0].rank} 名及之后共 {len(over_limit)} 个板块"
                f"（超代表股覆盖上限 {valuation_evidence.SECTOR_PROXY_BOARD_LIMIT} 个板块）"
                "未获取代表股分位"
            )
        # A02（v36）四维分类：每个板块一条 BoardValuationInput → 系统参考分类。
        board_judgments: dict[str, object] = {}
        board_evidence: dict[str, object] = {}
        # C06（M2）：临界板块（背离 30–40pp）清单，供摘要拔高闸门使用。
        board_critical: dict[str, object] = {}
        for entry in ranking:
            seq += 1
            agg = entry.aggregate
            cached = appendix_by_code.get(agg.board_code)
            appendix = cached[0] if cached else None
            failures = cached[1] if cached else []
            trend_agg = appendix.to_trend_aggregate() if appendix is not None else None
            judgment_input = direction_engine.BoardValuationInput(
                board_code=agg.board_code,
                board_name=agg.board_name or agg.board_code,
                pb_percentile_median=percentiles.get(agg.board_code),
                pe_percentile_median=(
                    appendix.pe_percentile_median if appendix is not None else None
                ),
                loss_ratio=aggregator.board_loss_ratio(agg),
                trend=trend_agg,
                pb_percentile_sample_count=(
                    appendix.representative_count if appendix is not None else 0
                ),
                loss_count=int(getattr(agg, "loss_count", 0) or 0),
                loss_total=int(getattr(agg, "member_count", 0) or 0),
                trade_date=trade_date,
            )
            label, rationale = direction_engine.classify_board_valuation(judgment_input)
            entry_dict: dict[str, object] = {
                "evidence_id": f"E{seq}",
                "source_type": "sector_valuation",
                "source": valuation_source_label,
                "title": (
                    f"低估度榜单第 {entry.rank} 名：板块「{agg.board_name or agg.board_code}」"
                    f"估值中位数（截至 {trade_date}{scope_note}）"
                ),
                "sector_code": agg.board_code,
                "sector_name": agg.board_name,
                "rank": entry.rank,
                "trade_date": trade_date,
                # A02（v36）：系统参考分类进证据条目（模型可推翻，须给数值论证）。
                "valuation_judgment": label,
                "valuation_judgment_rationale": rationale,
                "content": aggregator.describe_sector_ranking_entry(
                    entry, total_boards=len(aggregates)
                ),
                "retrieved_at": datetime.now(UTC).isoformat(),
            }
            entry_dict["content"] = (
                f"{entry_dict['content']}\n【系统参考分类】{label}——{rationale}"
            )
            if appendix is not None and appendix.representative_count:
                entry_dict["percentile_appendix"] = {
                    "representative_count": appendix.representative_count,
                    "comparable_count": appendix.comparable_count,
                    "pb_percentile_median": appendix.pb_percentile_median,
                    "pe_percentile_median": appendix.pe_percentile_median,
                    "lowest_three": [
                        {
                            "symbol": item.symbol,
                            "name": item.name,
                            "pb_mrq_percentile": item.pb_mrq_percentile,
                            "pe_ttm_percentile": item.pe_ttm_percentile,
                            "trade_date": item.trade_date,
                        }
                        for item in appendix.lowest_three
                    ],
                    "incomparable_symbols": list(appendix.incomparable_symbols),
                    "disclaimer": appendix.disclaimer,
                }
                entry_dict["content"] = (
                    f"{entry_dict['content']}；{aggregator.describe_board_percentile_appendix(appendix)}"
                )
            if failures:
                truncated.extend(failures)
            snapshot.append(entry_dict)
            board_judgments[str(agg.board_name or agg.board_code)] = label
            board_evidence[str(agg.board_name or agg.board_code)] = {f"E{seq}"}
            # C06（M2）：背离 30–40pp 的板块标临界（标签不变，只加标记与优先级口径）。
            board_critical[str(agg.board_name or agg.board_code)] = (
                direction_engine.board_is_critical_valuation(judgment_input)
            )
        # A04（v36）对照清单：亏损面过大被排除出深取名单的板块，单独成条目披露原因。
        for agg in contrast_missing:
            seq += 1
            loss = aggregator.board_loss_ratio(agg)
            loss_text = "不可得" if loss is None else f"{loss * 100:.1f}%"
            entry_dict = {
                "evidence_id": f"E{seq}",
                "source_type": "sector_valuation",
                "source": valuation_source_label,
                "title": (
                    f"对照清单：板块「{agg.board_name or agg.board_code}」"
                    f"估值中位数（截至 {trade_date}{scope_note}）"
                ),
                "sector_code": agg.board_code,
                "sector_name": agg.board_name,
                "rank": None,
                "trade_date": trade_date,
                "prescreen_excluded": True,
                "content": (
                    f"板块「{agg.board_name or agg.board_code}」PB 中位数 "
                    f"{agg.pb_mrq_median if agg.pb_mrq_median is not None else '不可得'}，"
                    f"横截面亏损面 {loss_text}（≥ {aggregator.VAL_PRESCREEN_LOSS_RATIO_MAX * 100:.0f}%），"
                    "按初筛闸门口径**未纳入代表股深取名单**（深取预算优先给盈利广度更好的板块），"
                    "此处仅披露估值中位数与亏损面，不提供代表股分位附录。"
                ),
                "retrieved_at": datetime.now(UTC).isoformat(),
            }
            snapshot.append(entry_dict)
            board_evidence[str(agg.board_name or agg.board_code)] = {f"E{seq}"}
        outcome["board_evidence"] = board_evidence
        outcome["board_judgments"] = board_judgments
        outcome["board_critical"] = board_critical
        if contrast_missing:
            unavailable.append(
                "对照清单（亏损面过闸未过，仅披露估值中位数与亏损面）："
                + "、".join(str(item.board_name or item.board_code) for item in contrast_missing)
            )
        if truncated:
            unavailable.append(
                "代表股分位覆盖范围受限（按榜单顺序截断）：" + "；".join(truncated[:5])
            )
        return seq, outcome

    @app.post("/evidence/direction-research")
    def direction_research(
        body: DirectionResearchRequest, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        """AI 方向研判（v33 路线图 B/C 组升级 + v34 板块行情证据层）：两种诚实状态 + 本次模型可选。

        v33 要点（2026-09-14 路线图）：
        - B01 两种诚实输出：证据快照非空 → `research_mode="evidence"`；缺失 → `knowledge`
          （必须声明数据缺口），不冒充实时研究已完成；质量三态随响应留痕，incomplete 落库为草稿；
        - B02 固定十小节结构 + B05 判断分层；B11 移除模型估价（模型价格字段一律剔除并告警）；
        - B09/B10 候选证券身份经 instruments 归一校验，三种资格（身份/业务关联/行情可扫描）分开陈述；
        - B15 方向专属质量规则（direction_research.validate_direction）；
        - C02/C04 `profile_id` 为本次请求指定模型（不存在 404），落库 generation_trace 调用身份。
        v34：主题命中行业板块（含实盘核验的别名映射）→ 当日板块行情快照与成交额前五成分
        注入证据包（cn-market-data 管道），风格/概念类主题未命中则如实声明缺口。
        """
        if not core.settings.model_access_enabled:
            return {"ok": False, "stage": "model_access_disabled", "detail": "模型出网已关闭（STEWARD_MODEL_ACCESS=0），未发起任何模型请求"}
        # C02：显式 profile 优先，复用个股研报同一解析规则（不存在 404，不静默换模型）。
        profile = _resolve_model_profile(core, body.profile_id)
        try:
            direction_mode, _mode_label, _lo, _hi = direction_engine.direction_mode_budget(body.mode)
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
        topic = body.topic.strip()
        # A01/A03（v35）：主题类型判定 + 复合主题双解析（产业限定 × 风格筛选）。
        # 顺序前提：主题解析必须先于证据构建，板块命中结果直接决定两条证据层的组合。
        theme_scope = _resolve_direction_theme(core, topic)
        theme = {
            "theme_kind": theme_scope["theme_kind"],
            "theme_kind_label": theme_scope["theme_kind_label"],
            "style_terms": theme_scope["style_terms"],
        }
        evidence_block, evidence_snapshot, unavailable_note, valuation_outcome = (
            _build_direction_evidence(core, topic, theme_scope)
        )
        research_mode = "evidence" if evidence_snapshot else "knowledge"
        # —— M3-D01：按证据可得性选择报告形态（确定性，不看模型自述）——
        # 主证据通道：风格/复合主题看估值横截面是否取到；产业/通用主题看行业与板块证据条目。
        _unavailable_count = len([x for x in str(unavailable_note or "").split("；") if x.strip()])
        _primary_ok = bool(valuation_outcome.get("cross_section_ok", True)) if theme["style_terms"] else (
            any(
                str(item.get("source_type")) in ("sector_market", "industry_data")
                for item in evidence_snapshot
            )
            or not theme["style_terms"]
        )
        report_form = direction_engine.choose_direction_form(
            research_mode=research_mode,
            evidence_count=len(evidence_snapshot),
            primary_ok=_primary_ok,
            unavailable_count=_unavailable_count,
        )
        system_text, user_text = direction_engine.build_messages(
            topic=topic,
            question=body.question.strip(),
            mode=direction_mode,
            research_mode=research_mode,
            evidence_block=evidence_block,
            unavailable_note=unavailable_note,
            # C01（v35）：风格主题分支 —— 口径声明前置 + 估值引用规则；复合主题叠加产业提示。
            style_terms=theme["style_terms"],
            theme_kind=theme["theme_kind"],
            # M3-D01：形态决定必需小节与篇幅（阶段形态走短篇 + 补证动作）。
            form=report_form,
        )
        try:
            reply = model_client.call_active_model(
                profile,
                resolve_store(core.database),
                model_client.format_messages(system_text, user_text),
            purpose="方向",
            )
        except model_client.ModelUnavailable as exc:
            return {"ok": False, "stage": "model_call", "detail": f"模型调用失败：{exc}"}
        parsed = macro_ai.extract_json_object(reply.content)
        if parsed is None or not isinstance(parsed.get("report"), str) or not parsed.get("report").strip():
            return {
                "ok": False,
                "stage": "parse",
                "detail": "模型输出无法解析为方向研判 JSON（缺 report 正文），不生成报告。",
                "raw_excerpt": reply.content[:400],
            }
        evidence_ids = {str(item["evidence_id"]) for item in evidence_snapshot}
        # M3-D07：引用—数值对应性检查用的条目内容索引（E# → 标题/正文/来源拼接）。
        evidence_index = {
            str(item["evidence_id"]): " ".join(
                str(item.get(key) or "") for key in ("title", "content", "source")
            )
            for item in evidence_snapshot
        }
        # M1-B01：候选身份回填表「公司名 → 6 位代码」——结构化代表股（lowest_three）+
        # 证据正文里的「名称(代码)」两路合流。只补码，不改名，歧义不猜。
        _proxy_pairs: list[tuple[str, str]] = []
        for _entry in evidence_snapshot:
            _appendix = _entry.get("percentile_appendix")
            if not isinstance(_appendix, dict):
                continue
            for _row in _appendix.get("lowest_three") or []:
                if isinstance(_row, dict) and _row.get("name") and _row.get("symbol"):
                    _proxy_pairs.append((str(_row["name"]), str(_row["symbol"])))
        candidate_name_index = direction_engine.build_name_symbol_index(
            structured_pairs=_proxy_pairs,
            texts=list(evidence_index.values()),
        )
        # C06（M2）：临界板块清单（背离 30–40pp）——摘要拔高闸门入参。
        critical_boards = {
            str(name)
            for name, flag in (valuation_outcome.get("board_critical") or {}).items()
            if flag
        }
        # —— M2（C02/C03/C04/C05）：定稿前确定性质量改写（在首次校验之前）——
        # 跨层断言 → 待验证假设；点名板块却引用不可核对的分类判定 → 去分类词；
        # 数值不在被引条目内 → 去引用；缺产业景气层 → 补 low 置信的「证据未覆盖」兜底条。
        quality_repairs = direction_engine.apply_quality_repairs(
            parsed,
            evidence_index=evidence_index,
            board_evidence=valuation_outcome.get("board_evidence") or None,
        )

        def _run_direction_validation(payload: dict[str, object]) -> dict[str, object]:
            """方向研判校验（单一入口）：D05 反方审查修订后重跑用同一份口径与打桩点。"""
            result = direction_engine.validate_direction(
                payload,
                mode=direction_mode,
                research_mode=research_mode,
                evidence_ids=evidence_ids,
                # C02/C03（v36）：候选池象限约束 + 分类判定引用闸门（板块分类与证据回链）。
                board_judgments=valuation_outcome.get("board_judgments") or None,
                board_evidence=valuation_outcome.get("board_evidence") or None,
                # M3：形态（D01）+ 引用索引（D07）。
                form=report_form,
                evidence_index=evidence_index,
                # M1-B01：候选身份回填表（公司名 → 6 位代码）。
                name_index=candidate_name_index,
                # C06（M2）：临界板块不得被写成主线。
                critical_boards=critical_boards,
            )
            # C02（v36）/D02：把所属板块分类与象限告警**写在候选行上**，
            # 让前端「所属板块分类」列与告警标记直接消费服务端结论，不在前端猜分类。
            _annotate_pool_board_judgments(
                result.get("pool") or [],
                valuation_outcome.get("board_judgments") or {},
                result.get("pool_quadrant_violations") or [],
                result.get("pool_override_arguments") or [],
            )
            return result

        quality = _run_direction_validation(parsed)
        # A02（v35）：风格词口径解析 —— 无数据风格词的缺口并入 data_gaps（不产生空证据）。
        style_calibers = direction_engine.resolve_style_calibers(theme["style_terms"])
        if style_calibers["gaps"]:
            quality["data_gaps"] = direction_engine.merge_data_gaps(
                quality["data_gaps"], list(style_calibers["gaps"])
            )
        # —— B05（v35）：候选池逐股估值回链（仅风格/复合主题；身份已核验的候选才取数）——
        style_gap_entries: list[dict[str, object]] = []
        if theme["style_terms"]:
            pool_valuation_gaps = _attach_pool_valuation_refs(quality["pool"])
            if pool_valuation_gaps:
                quality["data_gaps"] = direction_engine.merge_data_gaps(
                    quality["data_gaps"], list(pool_valuation_gaps)
                )
            # —— C03（v35）：风格主题四类缺口统一构造（无数据风格词 / 横截面失败 /
            # 代表股分位不可得 / 候选池估值缺失），每类带补证动作，统一去重不堆叠。——
            style_gap_entries = direction_engine.style_gap_entries(
                style_terms=theme["style_terms"],
                cross_section_ok=bool(valuation_outcome.get("cross_section_ok", True)),
                sector_percentile_failures=list(
                    valuation_outcome.get("sector_percentile_failures") or []
                ),
                pool_valuation_failures=_pool_valuation_failure_symbols(quality["pool"]),
            )
            if style_gap_entries:
                quality["data_gaps"] = direction_engine.merge_data_gaps(
                    quality["data_gaps"], [str(item["text"]) for item in style_gap_entries]
                )
        # —— v33 B06：方向反方审查（单次短 JSON 调用，不重写正文；失败如实降级）——
        counter_check_findings: dict[str, object] = {
            "ok": False, "requested": bool(body.with_counter_check),
            "detail": "本次未执行反方审查（请求已关闭 with_counter_check）。",
        }
        if body.with_counter_check:
            cc_system, cc_user = direction_engine.direction_counter_check_messages(
                topic=topic,
                title=str(parsed.get("title", "")),
                report=str(parsed.get("report", "")),
                judgments=quality["judgments"],
                pool=quality["pool"],
            )
            judgment_ids = {item["id"] for item in quality["judgments"]}
            pool_symbols = {item["symbol"] for item in quality["pool"] if item["symbol"]}
            cc_normalized = None
            cc_failure = ""
            try:
                cc_reply = model_client.call_active_model(
                    profile,
                    resolve_store(core.database),
                    model_client.format_messages(cc_system, cc_user),
            purpose="方向",
                )
                cc_normalized = direction_engine.normalize_direction_counter_check(
                    macro_ai.extract_json_object(cc_reply.content),
                    judgment_ids=judgment_ids,
                    pool_symbols=pool_symbols,
                )
            except model_client.ModelUnavailable as exc:
                cc_failure = f"反方审查模型调用失败：{exc}"
            except Exception as exc:  # noqa: BLE001 - 反方审查尽力而为，不拖垮研判交付
                cc_failure = f"反方审查执行异常：{exc}"
            if cc_normalized is not None:
                counter_check_findings = {"ok": True, "requested": True, **cc_normalized, "model": profile.model}
            elif cc_failure:
                counter_check_findings = {"ok": False, "requested": True, "detail": cc_failure}
            else:
                counter_check_findings = {
                    "ok": False, "requested": True,
                    "detail": "反方审查输出无法解析为约定 JSON（缺 strongest_counter），按未完成如实标注。",
                }
        # —— M3-D05：反方审查进入定稿（撤下/降级 + 正文标注 + 摘要与风险同步）——
        parsed, counter_check_application = direction_engine.apply_counter_check(
            parsed, counter_check_findings
        )
        if counter_check_application.get("applied"):
            # —— M2-C02 次序修正（2026-09-15 G02 重跑发现）——
            # 反方审查可能**撤下承载产业景气层的那条判断**：实测重跑里撤下 J1/J2/J4/J5 后，
            # 定稿只剩 J3（竞争力），于是上面那次「已补齐产业景气层」的结论失效，
            # 质量告警又出现「核心判断缺少『产业景气』层」——C02 的保证必须在**定稿之后**成立。
            # 故在重跑校验前再执行一次：只追加 low 置信的「证据未覆盖」兜底条（不冒充景气反转）、
            # 或把无中间证据的景气断言降为 low 并加缺口尾注；**不改模型原话、不编景气数据**。
            re_layer = direction_engine.ensure_industry_layer(
                parsed, evidence_index=evidence_index
            )
            if re_layer.get("added") or re_layer.get("demoted_ids"):
                quality_repairs["industry_layer_after_counter_check"] = re_layer
            # 修订后重跑校验：质量三态、判断清单、候选池与缺口一律以**修订后**稿件为准，
            # 避免「正文保留强结论、文末审查又说依据不成立」的双口径。
            removed_ids = list(counter_check_application.get("removed_judgment_ids") or [])
            downgraded_ids = list(counter_check_application.get("downgraded_judgment_ids") or [])
            quality = _run_direction_validation(parsed)
            quality["quality_warnings"] = list(quality["quality_warnings"]) + [
                f"反方审查已进入定稿：撤下 {len(removed_ids)} 条判断"
                f"{('（' + '、'.join(removed_ids) + '）') if removed_ids else ''}，"
                f"降级 {len(downgraded_ids)} 条判断"
                f"{('（' + '、'.join(downgraded_ids) + '）') if downgraded_ids else ''}"
                "——正文已加行内标注，摘要与风险段落已同步。"
            ]
            if (
                (removed_ids or downgraded_ids)
                and quality["quality_status"] == report_quality.QUALITY_STATUS_COMPLETE
            ):
                quality["quality_status"] = report_quality.QUALITY_STATUS_NEEDS_REVIEW
                quality["quality_status_label"] = report_quality.QUALITY_STATUS_LABELS[
                    report_quality.QUALITY_STATUS_NEEDS_REVIEW
                ]
        _audit(core, "evidence.direction_research.generated", "topic", topic)
        response: dict[str, object] = {
            "ok": True,
            "topic": topic,
            "title": str(parsed.get("title", "")).strip() or f"{topic} 方向研判",
            # 正文 markdown：`report` 为新契约字段名；`direction_summary` 保留旧字段名（历史记录兼容）。
            "report": parsed["report"],
            "direction_summary": parsed["report"],
            "executive_summary": str(parsed.get("executive_summary", "")).strip(),
            "catalysts": [str(x).strip() for x in parsed.get("catalysts", []) if str(x).strip()],
            "risks": [str(x).strip() for x in parsed.get("risks", []) if str(x).strip()],
            # B05：核心判断分层（fact/industry/competitiveness/sentiment/technical）。
            "core_judgments": quality["judgments"],
            # B09-B12：身份归一 + 三种资格分列 + 无价格字段。
            "stock_pool": quality["pool"],
            "data_gaps": quality["data_gaps"],
            # C03（v35）：风格主题四类缺口的结构化留痕（code + 补证动作文本），
            # 与 data_gaps 同源；非风格主题为空列表。
            "style_gap_entries": style_gap_entries,
            "next_verification": str(parsed.get("next_verification", "")).strip(),
            # B01：两种诚实输出状态 + 方向专属质量三态（B15）。
            "research_mode": research_mode,
            "mode": quality["mode"],
            "mode_label": quality["mode_label"],
            "quality_status": quality["quality_status"],
            "quality_status_label": quality["quality_status_label"],
            "quality_blockers": quality["quality_blockers"],
            "quality_warnings": quality["quality_warnings"],
            "missing_sections": quality["missing_sections"],
            "section_states": quality["section_states"],
            "report_chars": quality["report_chars"],
            "summary_chars": quality["summary_chars"],
            # B04：证据快照随报告落库（来源/数据截至/抓取时间逐条可查）。
            "evidence_snapshot": evidence_snapshot,
            "evidence_unavailable": unavailable_note,
            # B06：反方审查（受影响结论 + 候选质疑；未完成如实标注）。
            "counter_check": counter_check_findings,
            # M3-D05：反方审查的**定稿执行**留痕（撤下/降级清单、正文标注数、未匹配原句）。
            "counter_check_application": counter_check_application,
            # M3-D01：报告形态（完整研判/局部研判/阶段性研究）+ 必需小节。
            "report_form": quality["report_form"],
            "report_form_label": quality["report_form_label"],
            "form_required_sections": quality["form_required_sections"],
            "missing_optional_sections": quality["missing_optional_sections"],
            # M3-D02：前置行业比较表（前端表格与 Markdown 导出共用）。
            "industry_comparison": quality["industry_comparison"],
            # M3-D06：低估候选池 vs 待验证研究池（stock_pool 仍为全集，兼容历史消费方）。
            "low_valuation_pool": quality["low_valuation_pool"],
            "research_pool": quality["research_pool"],
            # M3-D03/D04/D06/D07/D08：确定性检查留痕（前端复核块与缺口清单消费）。
            "cross_layer_claims": quality["cross_layer_claims"],
            "cycle_normalization_gaps": quality["cycle_normalization_gaps"],
            "reference_support_problems": quality["reference_support_problems"],
            "repeated_gaps": quality["repeated_gaps"],
            "research_pool_mislabeled": quality["research_pool_mislabeled"],
            # M2（C02/C03/C04/C05）：定稿前确定性改写的逐条留痕（改了哪节哪行、为什么）。
            "quality_repairs": quality_repairs,
            # C01（M2）：摘要字数口径——`summary_chars` 只算摘要正文，修订说明单独计数。
            "summary_revision_chars": quality["summary_revision_chars"],
            "summary_chars_total": quality["summary_chars_total"],
            # C06（M2）：临界板块（背离 30–40pp）与其被写成主线的命中留痕。
            "critical_boards": quality["critical_boards"],
            "critical_board_hype": quality["critical_board_hype"],
            # C04：调用身份冻结（请求方案 / 实际方案 / 提示词版本 / 模式）。
            "generation_trace": {
                "prompt_version": direction_engine.DIRECTION_PROMPT_VERSION,
                "model": profile.model,
                "profile_id": str(getattr(profile, "profile_id", "") or ""),
                "profile_name": str(getattr(profile, "name", "") or ""),
                "requested_profile_id": str(body.profile_id) if body.profile_id else None,
                "stages_completed": ["draft"],
                "mode": direction_mode,
                "research_mode": research_mode,
                "evidence_count": len(evidence_snapshot),
                # M3-D01：报告形态留痕（前端徽章与导出共用）。
                "report_form": report_form,
                "report_form_label": direction_engine.direction_form_label(report_form),
                # A01（v35）：主题类型判定留痕（前端据此展示主题类型徽章）。
                "theme_kind": theme["theme_kind"],
                "theme_kind_label": theme["theme_kind_label"],
                "style_terms": theme["style_terms"],
                # A02（v36）：板块四维分类留痕（系统参考分类，模型可推翻）。
                "board_judgments": valuation_outcome.get("board_judgments") or {},
                "generated_at": datetime.now(UTC).isoformat(),
            },
            # A01（v35）：主题类型（产业/风格/复合/通用），前端徽章与提示文案共用。
            "theme_kind": theme["theme_kind"],
            "theme_kind_label": theme["theme_kind_label"],
            "style_terms": theme["style_terms"],
            # A02/C02/C03（v36）：板块分类 + 候选池象限 + 分类引用闸门留痕。
            "board_judgments": valuation_outcome.get("board_judgments") or {},
            # C06（M2）：板块 → 是否临界（背离 30–40pp）。
            "board_critical": valuation_outcome.get("board_critical") or {},
            "pool_quadrant_violations": quality["pool_quadrant_violations"],
            "unsupported_judgment_claims": quality["unsupported_judgment_claims"],
            "model": profile.model,
            "latency_ms": reply.latency_ms,
            "generated_at": datetime.now(UTC).isoformat(),
        }
        # 持久化：方向研判落库（is_draft=incomplete 草稿，不混入正式数量）；落库失败不阻断返回。
        try:
            report_id = str(uuid4())
            record = {
                **response,
                "report_id": report_id,
                "kind": "direction",
                "subject": topic,
                "is_draft": quality["quality_status"] == report_quality.QUALITY_STATUS_INCOMPLETE,
            }
            core.database.insert_ai_research_report(record)
            response["report_id"] = report_id
        except Exception:  # noqa: BLE001 - 持久化尽力而为，生成结果优先交付
            pass
        return response

    @app.get("/slots", response_model=list[dict[str, int | str]])
    def slot_registry(core: CoreState = Depends(_require_session)) -> list[dict[str, int | str]]:
        """插槽注册表与仲裁占用（G1-1/G1-2）：8 槽的 targeted/used/cap/queued，喂今天页与状态栏。"""
        installations = core.database.list_plugin_installations()
        targeted = targeted_slots_by_installations(installations, _manifest_slots_index())
        return compute_slot_occupancy(core.settings, targeted)

    @app.get("/cards/slots/{slot}", response_model=list[UICard])
    def slot_cards(slot: str, core: CoreState = Depends(_require_session)) -> list[UICard]:
        """按插槽产出 UICard（G1-4）：L0 静默槽恒为空；无真实数据返回空列表，不编造卡。"""
        known = {str(entry["slot"]) for entry in DEFINED_SLOTS}
        if slot not in known:
            raise HTTPException(status_code=404, detail=f"未知插槽：{slot}")
        cards = cards_for_slot(core.database, core.local_user_id, slot, core.settings)
        _audit(core, "slots.cards.produced", "slot", slot)
        return cards

    # ---- 本机凭据库（G3-1/G3-4）：响应永不回传明文，只回 last4 + updated_at + 后端 ----

    @app.get("/credentials", response_model=list[CredentialRecord])
    def list_credentials(core: CoreState = Depends(_require_session)) -> list[CredentialRecord]:
        return resolve_store(core.database).list_records()

    @app.get("/credentials/{key_id}", response_model=CredentialRecord)
    def get_credential(key_id: str, core: CoreState = Depends(_require_session)) -> CredentialRecord:
        record = next(
            (
                item
                for item in resolve_store(core.database).list_records()
                if item.key_id == key_id
            ),
            None,
        )
        if record is None:
            raise HTTPException(status_code=404, detail="credential not found")
        return record

    @app.put("/credentials/{key_id}", response_model=CredentialRecord)
    def upsert_credential(
        key_id: str,
        body: CredentialUpsertRequest,
        core: CoreState = Depends(_require_session),
    ) -> CredentialRecord:
        record = resolve_store(core.database).store(key_id, body.secret)
        _audit(core, "credential.upserted", "credential", key_id)
        return record

    @app.delete("/credentials/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_credential(
        key_id: str, core: CoreState = Depends(_require_session)
    ) -> None:
        resolve_store(core.database).delete(key_id)
        _audit(core, "credential.deleted", "credential", key_id)

    @app.post("/credentials/{key_id}/test", response_model=dict[str, object])
    def test_credential(key_id: str, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        secret = resolve_store(core.database).get(key_id)
        result = probe_credential(key_id, secret)
        _audit(core, "credential.tested", "credential", key_id)
        return result

    # ---- 模型服务多方案（G3-3）：同一时刻恰好一个「使用中」 ----

    @app.get("/model-usage")
    def model_usage(window: str = "7d", core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """模型用量（F01）：按方案聚合窗口内的调用次数/结果/token 三项/耗时。

        usage 缺失的调用 token 记 null（聚合 COALESCE 求和不猜 0，同时给出 usage_missing
        计数）；window 仅支持 7d / 30d。
        """
        windows = {"7d": 7, "30d": 30}
        if window not in windows:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="window 仅支持 7d | 30d")
        from datetime import timedelta

        return {
            "ok": True,
            "window": window,
            "since": (datetime.now(UTC) - timedelta(days=windows[window])).isoformat(),
            "summaries": core.database.summarize_model_usage(windows[window]),
            "note": "usage 缺失的调用 token 记 null（不按字数反推）；retried_calls 为发生 thinking-disabled 重试的调用数。",
        }

    @app.get("/model-profiles", response_model=list[ModelProfile])
    def list_model_profiles(core: CoreState = Depends(_require_session)) -> list[ModelProfile]:
        return core.database.list_model_profiles()

    @app.post("/model-profiles", response_model=ModelProfile, status_code=status.HTTP_201_CREATED)
    def create_model_profile(
        body: ModelProfileRequest, core: CoreState = Depends(_require_session)
    ) -> ModelProfile:
        profile = ModelProfile(profile_id=uuid4(), **body.model_dump())
        core.database.upsert_model_profile(profile)
        _audit(core, "model_profile.created", "model_profile", str(profile.profile_id))
        return profile

    @app.put("/model-profiles/{profile_id}", response_model=ModelProfile)
    def update_model_profile(
        profile_id: UUID, body: ModelProfileRequest, core: CoreState = Depends(_require_session)
    ) -> ModelProfile:
        """编辑模型方案（名称/端点/模型/凭据引用）：保留 profile_id、状态与创建时间，updated_at 刷新。"""
        existing = core.database.get_model_profile(profile_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="model profile not found")
        updated = existing.model_copy(update={**body.model_dump(), "updated_at": datetime.now(UTC)})
        core.database.upsert_model_profile(updated)
        _audit(core, "model_profile.updated", "model_profile", str(profile_id))
        return updated

    @app.post("/model-profiles/{profile_id}/activate", response_model=ModelProfile)
    def activate_model_profile(
        profile_id: UUID, core: CoreState = Depends(_require_session)
    ) -> ModelProfile:
        try:
            activated = core.database.set_active_model_profile(profile_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        _audit(core, "model_profile.activated", "model_profile", str(profile_id))
        return activated

    @app.post("/model-profiles/{profile_id}/test", response_model=dict[str, object])
    def test_model_profile(
        profile_id: UUID, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        profile = core.database.get_model_profile(profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="model profile not found")
        if not core.settings.model_access_enabled:
            return {"ok": False, "latency_ms": 0, "detail": "模型出网已关闭（STEWARD_MODEL_ACCESS=0），无法真实测试"}
        store = resolve_store(core.database)
        api_key = model_client.resolve_credential(store, profile.credential_ref)
        if not api_key:
            return {
                "ok": False,
                "latency_ms": 0,
                "detail": (
                    f"关联凭据「{profile.credential_ref}」在凭据库中不存在。"
                    "三种填法任选：直接粘贴 sk- 明文密钥（自动加密存入本机凭据库）、"
                    "填 key_id（如 model_api_key）、或填凭据尾号（自动匹配）。"
                ),
            }
        try:
            reply = model_client.call_active_model(
                profile,
                store,
                model_client.format_messages(
                    "你是投资管家用于连通性测试的小助手，只回复一个词：ok。",
                    "连通性测试",
                ),
            purpose="连通性测试",
            )
        except model_client.ModelUnavailable as exc:
            return {"ok": False, "latency_ms": 0, "detail": f"模型不可用：{exc}"}
        _audit(core, "model_profile.tested", "model_profile", str(profile_id))
        return {
            "ok": True,
            "latency_ms": reply.latency_ms,
            "detail": f"真实调用成功（模型 {reply.model} 应答 {len(reply.content)} 字）。",
        }

    # —— 草稿态探测（2026-09-10 用户要求）：弹窗内「测连通性看延迟」+「拉取模型」——
    # 都是只读探测：端点/模型/密钥由请求体给出（密钥可以是尚未保存的明文），
    # 不读模型方案表、不写凭据库，因此「点一下测试」不会产生任何配置副作用。
    # 路由字面量注册在 `/{profile_id}` 之前，避免被参数化路由抢先匹配。
    def _probe_target(
        core: CoreState, body: ModelProbeRequest
    ) -> tuple[str, str] | dict[str, object]:
        """解析探测目标：返回 (api_key, "") 或直接返回失败响应 dict。"""
        ref = (body.credential_ref or "").strip()
        store = resolve_store(core.database)
        api_key = model_client.resolve_probe_api_key(store, ref)
        if ref and not api_key:
            return {
                "ok": False,
                "models": [],
                "latency_ms": 0,
                "detail": f"凭据引用「{ref}」在本机凭据库中不存在，也不像明文密钥；请粘贴 sk-… 密钥、填 key_id 或凭据尾号。",
            }
        return (api_key, "")

    @app.post("/model-profiles/probe", response_model=dict[str, object])
    def probe_model_connection(
        body: ModelProbeRequest, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        """弹窗内「测连通性」：用当前表单参数试调一次，返回真实延迟（毫秒）。"""
        if not core.settings.model_access_enabled:
            return {"ok": False, "models": [], "latency_ms": 0, "detail": "模型出网已关闭（STEWARD_MODEL_ACCESS=0），无法真实测试"}
        model_name = (body.model or "").strip()
        if not model_name:
            return {"ok": False, "models": [], "latency_ms": 0, "detail": "请先填写模型名（可点「拉取模型」获取可选列表）"}
        resolved = _probe_target(core, body)
        if isinstance(resolved, dict):
            return resolved
        api_key = resolved[0]
        timeout = float(body.timeout_secs or model_client.MODEL_REQUEST_TIMEOUT)
        try:
            reply = model_client.probe_completion(body.base_url, model_name, api_key, timeout=timeout)
        except model_client.ModelUnavailable as exc:
            return {"ok": False, "models": [], "latency_ms": 0, "detail": f"模型不可用：{exc}"}
        except Exception as exc:  # noqa: BLE001 - 兜底：任何未预期异常都不让界面白屏
            return {"ok": False, "models": [], "latency_ms": 0, "detail": f"连通性测试失败：{exc}"}
        return {
            "ok": True,
            "models": [],
            "latency_ms": reply.latency_ms,
            "detail": f"真实调用成功（模型 {reply.model} 应答 {len(reply.content)} 字）。",
        }

    @app.post("/model-profiles/discover-models", response_model=dict[str, object])
    def discover_provider_models(
        body: ModelProbeRequest, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        """弹窗内「拉取模型」：读取服务商 /models 列表（只读，不落库、不写凭据）。"""
        if not core.settings.model_access_enabled:
            return {"ok": False, "models": [], "latency_ms": 0, "detail": "模型出网已关闭（STEWARD_MODEL_ACCESS=0），无法拉取"}
        resolved = _probe_target(core, body)
        if isinstance(resolved, dict):
            return resolved
        api_key = resolved[0]
        timeout = float(body.timeout_secs or model_client.MODEL_REQUEST_TIMEOUT)
        try:
            models = model_client.list_provider_models(body.base_url, api_key, timeout)
        except model_client.ModelUnavailable as exc:
            return {"ok": False, "models": [], "latency_ms": 0, "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001 - 网络/解析异常如实转述，不让界面白屏
            return {"ok": False, "models": [], "latency_ms": 0, "detail": f"拉取模型失败：{exc}"}
        return {"ok": True, "models": models, "latency_ms": 0, "detail": f"拉取到 {len(models)} 个模型。"}

    @app.delete("/model-profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_model_profile(
        profile_id: UUID, core: CoreState = Depends(_require_session)
    ) -> None:
        core.database.delete_model_profile(profile_id)
        _audit(core, "model_profile.deleted", "model_profile", str(profile_id))

    # ---- Jev 决策模型（JV02）：System One 的另一套协议，**不进** chat 方案表 ----
    #
    # 为什么单开一节而不是塞进 model-profiles：Jev 走 POST {base_url}/systemone，返回类型化
    # answers（无正文生成、无需解析模型散文），与 chat/completions 是两套协议。`ModelProfile`
    # 的「同一时刻恰好一个使用中」语义只属于 chat 链路，混进来会让两边都讲不清。
    # 密钥同样不在本节任何响应里出现——只存凭据库引用。

    def _jev_effective_settings(core: CoreState) -> JevSettings:
        """生效配置：设置页保存值（jev_config 表）> STEWARD_JEV_* 环境变量 > 内置默认。

        实现在 `jev_client.effective_settings` —— 协同流水线（`collab.py`）也要按同一优先级
        取配置，两处各写一遍迟早漂移成两个口径，故收敛到一处。
        """
        return jev_client.effective_settings(core)

    @app.get("/jev/config", response_model=dict[str, object])
    def get_jev_config(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """读取 Jev 配置 + 官方协议上限 + 数据出网说明（设置页据此渲染与提示）。"""
        settings = _jev_effective_settings(core)
        return {
            "ok": True,
            "config": settings.model_dump(mode="json"),
            "saved": core.database.get_jev_settings(core.local_user_id) is not None,
            "access_enabled": core.settings.model_access_enabled,
            "schema_version": jev_client.JEV_QUESTION_SCHEMA_VERSION,
            "defaults": {
                "base_url": core.settings.jev_base_url,
                "model": core.settings.jev_model,
                "timeout_secs": core.settings.jev_timeout_secs,
            },
            "limits": {
                "choice_max_options": jev_client.JEV_CHOICE_MAX_OPTIONS,
                "score_min_levels": jev_client.JEV_SCORE_MIN_LEVELS,
                "score_max_levels": jev_client.JEV_SCORE_MAX_LEVELS,
            },
            # noul 没有 confidence，阈值在代码里——把当前口径如实暴露给界面，
            # 避免界面上出现「置信度」这种 Jev 根本不返回的字段。
            "noul_thresholds": {"yes": jev_client.JEV_NOUL_YES, "no": jev_client.JEV_NOUL_NO},
            "data_handling": {
                "trains_on_input": False,
                "zero_data_retention": "enterprise_only",
                # 立场（用户 2026-09-21 拍板）：**不申请**企业版 ZDR。
                # 这不是「厂商不给」，是本项目主动不申请——所以白名单必须按最严口径执行，
                # 不得因为「以后也许能拿到 ZDR」而放宽 state。如实暴露给界面，避免读者误以为已有 ZDR。
                "zdr_applied": False,
                "retention": "无固定期限（官方表述为按提供服务之合理必要期间保留）",
                "hosted_in": "美国",
            },
            "notes": [
                (
                    "开启后 state 会出网到 TypeSafe（美国托管）：官方声明不用客户输入训练模型，"
                    "但默认非零留存且无固定保留期，零数据留存（ZDR）仅企业版——**本项目已决定不申请 ZDR**，"
                    "故 state 白名单按最严口径执行。"
                ),
                "计费只算输入 token（输出免费），state 越精简越省——与「state 数据最小化」是同一条约束。",
                "官方声明英文精度最佳，中文（CJK）可处理但精度较低：中文场景的判定阈值须用自有样本重新标定后再依赖。",
                (
                    "Jev 只做「是/否、选哪个、打几分」的原子判断；判定一律作为软校验/分诊层——"
                    "只标注、降级、排序，不改写模型原话、不阻断交付。"
                ),
            ],
        }

    def _normalize_jev_credential_ref(store: Any, ref: str) -> tuple[str, str | None]:
        """把「用户粘贴的明文密钥 / 凭据尾号」规范成可长期保存的 `credential_ref`。

        返回 `(引用, 说明)`；说明非 None 时前端要如实告诉用户密钥被搬到了哪里。

        为什么在**服务端**做转换（chat 侧是在 `SettingsPage` 前端转换的）：
        1. `call_jev` 用 `resolve_credential` 解析密钥，它**只认 key_id 与尾号，不认明文**。
           明文原样存进 `jev_config.payload` 会造出一个很坏的错位——「测连通性」通过
           （探测走 `resolve_probe_api_key`，明文可直接用），但协同流水线一调用就
           「凭据解析失败」。用户会以为 Jev 装好了，其实一次都没跑成。
        2. 明文密钥落 `jev_config.payload` 直接违反 P0 验收项「密钥零泄漏」。前端文案
           已承诺「不会写进本配置」，那服务端就必须真的做到——承诺不能只靠调用方自觉。

        尾号分支与 `resolve_credential` 的尾号兜底同口径：单人本机产品，允许用户凭直觉填尾号。
        """
        cleaned = (ref or "").strip()
        if not cleaned:
            return "", None
        if model_client.looks_like_plaintext_key(cleaned):
            record = store.store(JEV_API_KEY, cleaned)
            return JEV_API_KEY, (
                f"检测到明文密钥，已存入本机凭据库（key_id={record.key_id}，尾号 {record.last4}）——"
                "配置里只留 key_id，明文不进 jev_config 表。"
            )
        try:
            records = store.list_records()
        except Exception:  # noqa: BLE001 - 凭据层不可用时按原样保存，不阻断设置页
            records = []
        if not any(record.key_id == cleaned for record in records):
            matched = next(
                (record for record in records if (record.last4 or "").upper() == cleaned.upper()),
                None,
            )
            if matched is not None:
                return matched.key_id, f"按凭据尾号匹配到 key_id={matched.key_id}，已改写为 key_id 保存。"
        return cleaned, None

    @app.put("/jev/config", response_model=dict[str, object])
    def update_jev_config(
        body: JevSettingsRequest, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        """保存 Jev 配置（明文密钥自动转入凭据库，配置表只留 key_id）。"""
        existing = core.database.get_jev_settings(core.local_user_id)
        credential_ref, credential_note = _normalize_jev_credential_ref(
            resolve_store(core.database), body.credential_ref
        )
        settings = JevSettings(
            user_id=core.local_user_id,
            enabled=body.enabled,
            base_url=body.base_url,
            model=body.model,
            credential_ref=credential_ref,
            timeout_secs=body.timeout_secs,
            created_at=existing.created_at if existing else datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        core.database.upsert_jev_settings(settings)
        _audit(core, "jev_config.updated", "jev_config", str(core.local_user_id))
        return {
            "ok": True,
            "saved": True,
            "config": settings.model_dump(mode="json"),
            "credential_note": credential_note,
        }

    def _jev_probe_key(core: CoreState, ref: str) -> tuple[str, dict[str, object] | None]:
        """解析探测用密钥：返回 (api_key, 失败响应)。明文密钥可直接试（尚未保存也要能先测）。"""
        store = resolve_store(core.database)
        cleaned = (ref or "").strip()
        api_key = model_client.resolve_probe_api_key(store, cleaned)
        if cleaned and not api_key:
            return "", {
                "ok": False,
                "latency_ms": 0,
                "detail": (
                    f"凭据引用「{cleaned}」在本机凭据库中不存在，也不像明文密钥；"
                    "请粘贴密钥、填 key_id（如 jev_api_key）或填凭据尾号。"
                ),
            }
        return api_key, None

    @app.post("/jev/probe", response_model=dict[str, object])
    def probe_jev_connection(
        body: JevProbeRequest, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        """草稿态「测连通性」：只读，不读配置表、不写凭据库。

        与 chat 侧 `model-profiles/probe` 同惯例——探测**不落 `model_calls`**（那边也不落），
        因为它不是流水线调用而是用户主动的一次性自检。
        """
        if not core.settings.model_access_enabled:
            return {
                "ok": False,
                "latency_ms": 0,
                "detail": "模型出网总闸已关闭（STEWARD_MODEL_ACCESS=0），无法真实测试",
            }
        api_key, failure = _jev_probe_key(core, body.credential_ref)
        if failure is not None:
            return failure
        timeout = float(body.timeout_secs or jev_client.JEV_REQUEST_TIMEOUT)
        try:
            reply = jev_client.probe_systemone(body.base_url, body.model, api_key, timeout=timeout)
        except jev_client.JevUnavailable as exc:
            return {"ok": False, "latency_ms": 0, "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001 - 兜底：任何未预期异常都不让设置页白屏
            return {"ok": False, "latency_ms": 0, "detail": f"连通性测试失败：{exc}"}

        sample = reply.sample
        verdict = sample.noul_verdict() or "uncertain"
        return {
            "ok": True,
            "latency_ms": reply.latency_ms,
            "detail": (
                f"真实调用成功（模型 {reply.model}，{reply.latency_ms}ms，"
                f"输入 {reply.input_tokens} / 输出 {reply.output_tokens} token）。"
                f"中文探测题回执 noul={sample.noul:.2f}（{verdict}）——"
                "明显偏离 1 提示中文判定不稳，中文场景阈值须按自有样本重新标定。"
            ),
            "model": reply.model,
            "requested_model": reply.requested_model,
            "probe_noul": sample.noul,
            "probe_verdict": verdict,
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
        }

    @app.post("/jev/models", response_model=dict[str, object])
    def discover_jev_models(
        body: JevProbeRequest, core: CoreState = Depends(_require_session)
    ) -> dict[str, object]:
        """「拉取模型」：读官方 `GET /models`（只读，不落库、不写凭据）。

        ⚠️ 官方返回形状是 `{"models": [{"name", "description", "release_date"}]}`，
        与 OpenAI 兼容端点的 `{"data": [{"id"}]}` 不同——客户端按官方形状解析。
        """
        if not core.settings.model_access_enabled:
            return {"ok": False, "models": [], "latency_ms": 0, "detail": "模型出网总闸已关闭（STEWARD_MODEL_ACCESS=0），无法拉取"}
        api_key, failure = _jev_probe_key(core, body.credential_ref)
        if failure is not None:
            return {**failure, "models": []}
        timeout = float(body.timeout_secs or jev_client.JEV_REQUEST_TIMEOUT)
        try:
            models = jev_client.list_jev_models(body.base_url, api_key, timeout)
        except jev_client.JevUnavailable as exc:
            return {"ok": False, "models": [], "latency_ms": 0, "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "models": [], "latency_ms": 0, "detail": f"拉取模型失败：{exc}"}
        return {"ok": True, "models": models, "latency_ms": 0, "detail": f"拉取到 {len(models)} 个模型/别名。"}

    # ---- 研读图书馆（G5）----

    @app.get("/library/books", response_model=list[Book])
    def list_books(core: CoreState = Depends(_require_session)) -> list[Book]:
        return core.database.list_books()

    @app.post("/library/books", response_model=Book, status_code=status.HTTP_201_CREATED)
    def create_book(body: BookRequest, core: CoreState = Depends(_require_session)) -> Book:
        book = Book(book_id=uuid4(), **body.model_dump())
        core.database.upsert_book(book)
        _audit(core, "library.book.created", "library_book", str(book.book_id))
        return book

    @app.put("/library/books/{book_id}", response_model=Book)
    def update_book(book_id: UUID, body: BookRequest, core: CoreState = Depends(_require_session)) -> Book:
        existing = core.database.get_book(book_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="library book not found")
        book = existing.model_copy(update={**body.model_dump(), "updated_at": datetime.now(UTC)})
        core.database.upsert_book(book)
        _audit(core, "library.book.updated", "library_book", str(book.book_id))
        return book

    @app.delete("/library/books/{book_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_book(book_id: UUID, core: CoreState = Depends(_require_session)) -> None:
        core.database.delete_book(book_id)
        _audit(core, "library.book.deleted", "library_book", str(book_id))

    @app.get("/library/books/lookup/{isbn}", response_model=dict[str, object])
    def lookup_book_metadata(isbn: str, core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """ISBN 元数据补全（D-17 拍板：Open Library + 手录兜底，不做爬虫）。

        仅 reading-library 插件启用时可调用；查不到/网络失败时 available=False 与降级说明，
        不编造元数据。Open Library 在 manifest network_allowlist 白名单内。
        """
        installation = core.database.get_plugin_installation("official.reading-library")
        if installation is None or installation.state != PluginInstallationState.ENABLED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="reading-library plugin is not enabled"
            )
        clean = isbn.replace("-", "").strip()
        if not clean.isdigit() or len(clean) not in (10, 13):
            raise HTTPException(status_code=422, detail="ISBN 应为 10 或 13 位数字")
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            f"https://openlibrary.org/isbn/{clean}.json",
            headers={"User-Agent": "investment-steward-core/0.1 (reading-library; local-first)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as error:
            logger.warning("library/books/lookup 降级 isbn=%s: %s", clean, error)
            return {
                "available": False,
                "isbn": clean,
                "degraded_reason": "Open Library 不可达（离线或接口异常），请手录元数据",
            }
        title = str(payload.get("title") or "").strip()
        if not title:
            return {"available": False, "isbn": clean, "degraded_reason": "Open Library 未收录该 ISBN，请手录"}
        publishers = [str(item) for item in payload.get("publishers") or []][:3]
        return {
            "available": True,
            "isbn": clean,
            "title": title,
            "publishers": publishers,
            "pages": payload.get("number_of_pages"),
            "source": "Open Library（公开接口）",
        }

    @app.post("/library/plan/generate", response_model=LibraryPlan)
    def generate_plan(
        body: LibraryPlanSourceRequest, core: CoreState = Depends(_require_session)
    ) -> LibraryPlan:
        source: UUID | None = None
        if body.source_book_id is not None:
            try:
                source = UUID(body.source_book_id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="source_book_id 不是合法 UUID") from exc
        plan = generate_reading_plan(
            core.database,
            source_book_id=source,
            model_client=model_client if core.settings.model_access_enabled else None,
        )
        _audit(core, "library.plan.generated", "library_plan", str(plan.plan_id))
        return plan

    @app.post("/library/insights")
    def generate_library_insights(core: CoreState = Depends(_require_session)) -> dict[str, object]:
        """图书馆 M5.5 认知档案：基于书单（真实字段 title/author/progress/status/notes）生成 AI 档案。

        引用校验：档案必须提到 ≥3 个真实书名（不虚构书单外的书），无引用不发布；不持久化，每次生成返回。
        """
        books = core.database.list_books()
        if not books:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="书单为空：先在图书馆添加书目，认知档案才有依据",
            )
        if not core.settings.model_access_enabled:
            return {"ok": False, "stage": "model_access_disabled", "detail": "模型出网已关闭（STEWARD_MODEL_ACCESS=0），未发起任何模型请求", "insights": None}
        profile = model_client.active_model_profile(core.database.list_model_profiles())
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="没有「使用中」的模型方案：先在设置页把一个方案置为使用中",
            )
        try:
            reply = model_client.call_active_model(
                profile,
                resolve_store(core.database),
                model_client.format_messages(
                    macro_ai.INSIGHTS_SYS_PROMPT, macro_ai.build_insights_context(books)
                ),
            purpose="研读",
            )
        except model_client.ModelUnavailable as exc:
            return {"ok": False, "stage": "model_call", "detail": f"模型调用失败：{exc}", "insights": None}
        cited = macro_ai.verify_insight_citations(reply.content, books)
        if not cited:
            return {
                "ok": False,
                "stage": "citation_check",
                "detail": "认知档案未引用足量真实书名（≥3，无引用不发布），已拒绝本次输出",
                "insights": None,
            }
        _audit(core, "library.insights.generated", "library", f"books:{len(books)}")
        return {
            "ok": True,
            "insights": reply.content,
            "citations": cited,
            "book_count": len(books),
            "model": profile.model,
            "latency_ms": reply.latency_ms,
            "generated_at": datetime.now(UTC).isoformat(),
        }

    @app.post(
        "/library/annotations/{annotation_id}/to-research",
        response_model=ResearchRun,
        status_code=status.HTTP_201_CREATED,
    )
    def annotation_to_research(
        annotation_id: str,
        body: AnnotationToResearchRequest,
        core: CoreState = Depends(_require_session),
    ) -> ResearchRun:
        run, found = create_research_from_annotation(
            core.database, core.local_user_id, annotation_id, body.user_question
        )
        if not found:
            raise HTTPException(status_code=404, detail="批注不存在")
        core.database.upsert_research_run(run)
        _audit(core, "research.run.created", "research_run", str(run.run_id))
        return run

    @app.get("/audit")
    def list_audit(
        limit: int = 100,
        offset: int = 0,
        q: str | None = None,
        order: str = "time-desc",
        with_total: bool = False,
        core: CoreState = Depends(_require_session),
    ) -> list[AuditEvent] | dict[str, object]:
        """审计事件（B02，桌面端升级路线图 2026-09-18：默认上限 100、服务端翻页）。

        q 对 action/resource_type/resource_id 做 LIKE；order ∈ time-desc/time-asc/action；
        with_total=true 返回 {"ok","items","total","offset","limit"} 信封，否则裸列表。
        全量导出仍走 db.list_audit（/export/all），不受此上限影响。"""
        page_limit = max(1, min(limit, 500))
        page_offset = max(0, offset)
        items, total = core.database.list_audit_page(
            core.local_user_id, limit=page_limit, offset=page_offset, q=q, order=order
        )
        if with_total:
            return {"ok": True, "items": items, "total": total, "offset": page_offset, "limit": page_limit}
        return items

    @app.get("/quant/artifacts", response_model=ArtifactPoolView)
    def quant_artifacts(core: CoreState = Depends(_require_session)) -> ArtifactPoolView:
        """分享池制品列表（G6-2 预留骨架）：阶段 A 未开放，返回空态可用性，不编造制品。"""
        return empty_pool_view("A")

    @app.post("/quant/runs", status_code=status.HTTP_409_CONFLICT)
    def quant_create_run(core: CoreState = Depends(_require_session)) -> dict[str, str]:
        """本机回测运行（G6-2 预留骨架）：分享池契约未冻结，拒绝创建并明确提示。

        B04：detail 原文指向 `ADR`，但仓库内从未存在 `docs/adr/`（用户按提示找不到东西），
        改为指向真实存在的冻结口径文档；补齐 ADR 目录属 H05。
        """
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "分享池阶段 A（参数集）未开放：ArtifactCard / MetricBasis 契约尚未冻结，"
                "不能提交回测运行。冻结口径见 "
                "docs/unified-local-first-platform-roadmap-2026-09-09.zh-CN.md §7。"
            ),
        )

    @app.get("/quant/lineage/{artifact_id:path}", response_model=ArtifactLineageView)
    def quant_lineage(
        artifact_id: str, core: CoreState = Depends(_require_session)
    ) -> ArtifactLineageView:
        """制品派生谱系（G6-2 预留骨架）：阶段未开放、无任何制品，返回空谱系。"""
        return ArtifactLineageView(available=False, artifact_id=artifact_id, lineage=[])

    return app

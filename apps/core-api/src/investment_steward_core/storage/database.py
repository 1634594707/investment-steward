from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, create_engine, event, text
from sqlalchemy.engine import Connection

from investment_steward_core.domain import (
    ALLOWED_RUN_TRANSITIONS,
    AgentResponse,
    AuditEvent,
    Book,
    CredentialRecord,
    DecisionEntry,
    Evidence,
    Holding,
    InvestmentPolicyVersion,
    InvestorProfile,
    JevSettings,
    LearningActivity,
    LearningGoal,
    LibraryPlan,
    ModelProfile,
    ModelProfileStatus,
    Notification,
    PERSONAL_DEFAULT_VIEWS,
    PersonalNotifyPrefs,
    PersonalSettings,
    Plan,
    PluginInstallation,
    PluginUpdateCandidate,
    PolicyStatus,
    ResearchRun,
    RunStatus,
    Thesis,
    TodayBrief,
)
from investment_steward_core.instruments import canonical_key

SCHEMA_VERSION = 33


# E01（桌面端升级路线图 2026-09-18）：/export/all 覆盖的**用户产出**表白名单（raw 导出）。
# 刻意排除的设备态/秘密/派生缓存：credentials（密钥）、model_profiles（凭证引用，设备态，
# 另由端点以元数据口径单独导出）、plugin_installations、plugin_update_candidates、
# sync_inbox/sync_outbox/sync_state、audit_events（设备态审计）、macro_cache（可再生缓存）。
EXPORT_PAYLOAD_TABLES: tuple[str, ...] = (
    "agent_responses",
    "ai_analysis_turns",
    "ai_research_reports",
    "books",
    "investor_profiles",
    "judgment_verifications",
    "learning_goals",
    "library_plans",
    "macro_research_evidence",
    "macro_user_views",
    "macro_weight_versions",
    "personal_settings",
    "quant_artifacts",
    "quant_experiments",
    "research_snapshots",
    "research_templates",
    "speaker_signals",
    "tactics_ai_reviews",
    "tactics_notes",
    "tactics_watchlist",
    "today_briefs",
    "verifiable_judgments",
    "watch_checks",
    "watch_items",
)


class Database:
    """SQLAlchemy-backed SQLite adapter; domain services never receive a raw connection."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # E05：每日节流备份的检查日期缓存（None = 今日尚未检查）。
        self._daily_backup_check_date = None
        # F02：证据来源聚合的进程内缓存（{tenant_id: (monotonic, summary)}，TTL 见下方类常量）。
        self._evidence_source_summary_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self.engine = create_engine(
            f"sqlite:///{self.path.as_posix()}",
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(self.engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA busy_timeout = 5000")
            cursor.close()

        self._migrate()

    @contextmanager
    def _connection(self) -> Iterator[Connection]:
        with self.engine.begin() as connection:
            yield connection

    def _migrate(self) -> None:
        with self._connection() as connection:
            current = int(connection.exec_driver_sql("PRAGMA user_version").scalar_one())
            if current < 1:
                for statement in (
                    """
                    CREATE TABLE IF NOT EXISTS investment_policies (
                      policy_id TEXT PRIMARY KEY,
                      user_id TEXT NOT NULL,
                      version INTEGER NOT NULL,
                      status TEXT NOT NULL,
                      payload TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL,
                      UNIQUE (user_id, version)
                    )
                    """,
                    "CREATE INDEX IF NOT EXISTS idx_policies_user_status ON investment_policies (user_id, status)",
                    """
                    CREATE TABLE IF NOT EXISTS evidence (
                      evidence_id TEXT PRIMARY KEY,
                      tenant_id TEXT NOT NULL,
                      content_hash TEXT NOT NULL,
                      payload TEXT NOT NULL,
                      created_at TEXT NOT NULL
                    )
                    """,
                    "CREATE INDEX IF NOT EXISTS idx_evidence_tenant ON evidence (tenant_id, created_at)",
                    """
                    CREATE TABLE IF NOT EXISTS audit_events (
                      event_id TEXT PRIMARY KEY,
                      tenant_id TEXT NOT NULL,
                      action TEXT NOT NULL,
                      resource_type TEXT NOT NULL,
                      resource_id TEXT,
                      payload TEXT NOT NULL,
                      created_at TEXT NOT NULL
                    )
                    """,
                    "CREATE INDEX IF NOT EXISTS idx_audit_tenant_time ON audit_events (tenant_id, created_at)",
                ):
                    connection.exec_driver_sql(statement)
                connection.exec_driver_sql("PRAGMA user_version = 1")
                current = 1
            if current < 2:
                connection.exec_driver_sql(
                    """
                    CREATE TABLE IF NOT EXISTS plugin_installations (
                      plugin_id TEXT PRIMARY KEY,
                      release_version TEXT NOT NULL,
                      state TEXT NOT NULL,
                      granted_capabilities TEXT NOT NULL,
                      artifact_sha256 TEXT NOT NULL,
                      source TEXT NOT NULL,
                      installed_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL,
                      payload TEXT NOT NULL
                    )
                    """
                )
                connection.exec_driver_sql("PRAGMA user_version = 2")
            if current < 3:
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS holdings (holding_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_holdings_user ON holdings (user_id)")
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS thesis (thesis_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, instrument TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_thesis_user_instrument ON thesis (user_id, instrument)")
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS plans (plan_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_plans_user ON plans (user_id)")
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS decisions (decision_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, payload TEXT NOT NULL, made_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_decisions_user_time ON decisions (user_id, made_at)")
                connection.exec_driver_sql("PRAGMA user_version = 3")
            if current < 4:
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS research_runs (run_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_research_runs_user_time ON research_runs (user_id, created_at)")
                connection.exec_driver_sql("PRAGMA user_version = 4")
            if current < 5:
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS agent_responses (response_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, run_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_agent_responses_tenant_time ON agent_responses (tenant_id, created_at)")
                connection.exec_driver_sql("PRAGMA user_version = 5")
            if current < 6:
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS learning_goals (goal_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_learning_goals_user ON learning_goals (user_id)")
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS learning_activities (activity_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, unit_id TEXT NOT NULL, payload TEXT NOT NULL, completed_at TEXT NOT NULL, created_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_learning_activities_user_time ON learning_activities (user_id, completed_at)")
                connection.exec_driver_sql("PRAGMA user_version = 6")
            if current < 7:
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS plugin_update_candidates (candidate_id TEXT PRIMARY KEY, plugin_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_plugin_update_candidates_plugin ON plugin_update_candidates (plugin_id, created_at)")
                connection.exec_driver_sql("PRAGMA user_version = 7")
            if current < 8:
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS credentials (key_id TEXT PRIMARY KEY, secret TEXT NOT NULL, last4 TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql("PRAGMA user_version = 8")
            if current < 9:
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS model_profiles (profile_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_model_profiles_time ON model_profiles (updated_at)")
                connection.exec_driver_sql("PRAGMA user_version = 9")
            if current < 10:
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS books (book_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_books_time ON books (updated_at)")
                connection.exec_driver_sql("PRAGMA user_version = 10")
            if current < 11:
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS library_plans (plan_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql("PRAGMA user_version = 11")
            if current < 12:
                # 宏观雷达数据缓存（M4）：指标/背景层原始观测，as-of 与 dataset_version 保证可追溯。
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS macro_cache ("
                    "series_key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_macro_cache_time ON macro_cache (updated_at)")
                connection.exec_driver_sql("PRAGMA user_version = 12")
            if current < 13:
                # 宏观权重版本链（M5.5）：用户手动调权全程留痕，任意历史状态带可精确复算。
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS macro_weight_versions ("
                    "version TEXT PRIMARY KEY, weights TEXT NOT NULL, source TEXT NOT NULL, "
                    "note TEXT, created_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql("PRAGMA user_version = 13")
            if current < 14:
                # 阶段 D：日报落库（按用户+本地日期，每天一份）与通知落库（去重键防重复评估）。
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS today_briefs ("
                    "day TEXT NOT NULL, user_id TEXT NOT NULL, payload TEXT NOT NULL, "
                    "created_at TEXT NOT NULL, PRIMARY KEY (day, user_id))"
                )
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS notifications ("
                    "notification_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, dedup_key TEXT NOT NULL, "
                    "payload TEXT NOT NULL, created_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_dedup ON notifications (dedup_key)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_notifications_user_time ON notifications (user_id, created_at)"
                )
                connection.exec_driver_sql("PRAGMA user_version = 14")
            if current < 15:
                # E3 · 单用户投资者画像（InvestorProfile）；research 链路按此画像个性化。
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS investor_profiles ("
                    "user_id TEXT PRIMARY KEY, payload TEXT NOT NULL, "
                    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql("PRAGMA user_version = 15")
            if current < 16:
                # E2 · 统一标的身份：为持仓追加 instrument_key 列并按既有 payload 回填。
                connection.exec_driver_sql(
                    "ALTER TABLE holdings ADD COLUMN instrument_key TEXT"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_holdings_instrument_key ON holdings (instrument_key)"
                )
                # 回填：从 payload JSON 的 instrument 字段经规范化取规范键（纯 stdlib，不依赖 akshare）。
                rows = connection.execute(
                    text("SELECT holding_id, payload FROM holdings")
                ).mappings().all()
                for row in rows:
                    try:
                        payload = json.loads(row["payload"])
                        instrument = payload.get("instrument", "")
                        key = str(instrument)
                    except (json.JSONDecodeError, AttributeError, TypeError):
                        key = ""

                    nkey = canonical_key(key)
                    if nkey:
                        connection.execute(
                            text("UPDATE holdings SET instrument_key = :k WHERE holding_id = :id"),
                            {"k": nkey, "id": row["holding_id"]},
                        )
                connection.exec_driver_sql("PRAGMA user_version = 16")
            if current < 17:
                # §3.7 第三层「用户分析层」（D-15 最小集）：每经济体一条，用户主权，
                # 永不参与规则计算、不进证据账本客观区、不上传远端。
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS macro_user_views ("
                    "region TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql("PRAGMA user_version = 17")
            if current < 18:
                # §3.5 发言人信号流（D-13 官方优先、转载明示）：沟通证据入库，仅本机；
                # AI 解读须引用原文句子，无引用不回填方向（ADR-0006）。
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS speaker_signals ("
                    "signal_id TEXT PRIMARY KEY, region TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql("PRAGMA user_version = 18")
            if current < 19:
                # 个人中心设置（单用户 · 本机）：显示身份 / 默认落地页 / 通知偏好 / 风险偏好。
                # 与 investor_profiles 分工：此表只服务界面呈现，不参与任何研究或评分计算。
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS personal_settings ("
                    "user_id TEXT PRIMARY KEY, payload TEXT NOT NULL, "
                    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql("PRAGMA user_version = 19")
            if current < 20:
                # 战法雷达（official.stock-tactics）：观察清单与观察笔记，仅本机。
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS tactics_watchlist ("
                    "symbol TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', "
                    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS tactics_notes ("
                    "note_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, content TEXT NOT NULL, "
                    "created_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_tactics_notes_symbol ON tactics_notes (symbol, created_at)"
                )
                connection.exec_driver_sql("PRAGMA user_version = 20")
            if current < 21:
                # v21 数据可信度修复（P0-02）：宏观研究证据从模块级内存字典落库，
                # 重启不丢；payload 保存完整条目（含 id/claim/event_id 等全字段）。
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS macro_research_evidence ("
                    "evidence_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, payload TEXT NOT NULL, "
                    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_macro_research_evidence_event "
                    "ON macro_research_evidence (event_id)"
                )
                connection.exec_driver_sql("PRAGMA user_version = 21")
            if current < 22:
                # v22 用户追加：AI 研究产出持久化（方向研判 / 个股研报），重启不丢、跨天可回看。
                # payload 保存端点完整响应（含正文/标的池/引用/limitations），kind 区分两类。
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS ai_research_reports ("
                    "report_id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL, "
                    "payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_ai_research_reports_kind "
                    "ON ai_research_reports (kind, created_at)"
                )
                connection.exec_driver_sql("PRAGMA user_version = 22")
            if current < 23:
                # v23（路线图 3.2）：量化制品改为「SQLite 元数据索引 + 制品目录内容哈希文件」。
                # 本体（公式 token/权重 JSON）存 artifacts 目录并登记 SHA-256，SQLite 只存索引与校验值。
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS quant_artifacts ("
                    "artifact_id TEXT PRIMARY KEY, kind TEXT NOT NULL, stage TEXT NOT NULL, "
                    "symbol TEXT NOT NULL, name TEXT NOT NULL, parent_id TEXT, created_at TEXT NOT NULL, "
                    "file_name TEXT NOT NULL, content_hash TEXT NOT NULL, payload_meta TEXT NOT NULL)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_quant_artifacts_created "
                    "ON quant_artifacts (kind, created_at)"
                )
                connection.exec_driver_sql("PRAGMA user_version = 23")
            if current < 24:
                # v24（路线图阶段 3 + §8.1-8.3）：本地实验、可检验判断、观察事项。
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS quant_experiments ("
                    "experiment_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, symbol TEXT NOT NULL, "
                    "label TEXT NOT NULL DEFAULT '', config TEXT NOT NULL, data_snapshot TEXT NOT NULL, "
                    "software TEXT NOT NULL, result_hash TEXT NOT NULL, result TEXT NOT NULL, "
                    "created_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_quant_experiments_symbol "
                    "ON quant_experiments (symbol, created_at)"
                )
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS verifiable_judgments ("
                    "judgment_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, status TEXT NOT NULL, "
                    "due_at TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_judgments_user_status "
                    "ON verifiable_judgments (user_id, status)"
                )
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS judgment_verifications ("
                    "verification_id TEXT PRIMARY KEY, judgment_id TEXT NOT NULL, result TEXT NOT NULL, "
                    "payload TEXT NOT NULL, checked_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_judgment_verifications_judgment "
                    "ON judgment_verifications (judgment_id, checked_at)"
                )
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS watch_items ("
                    "watch_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, status TEXT NOT NULL, "
                    "dedup_key TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL, "
                    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_watch_items_user_status "
                    "ON watch_items (user_id, status)"
                )
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS watch_checks ("
                    "check_id TEXT PRIMARY KEY, watch_id TEXT NOT NULL, triggered INTEGER NOT NULL, "
                    "payload TEXT NOT NULL, checked_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_watch_checks_watch "
                    "ON watch_checks (watch_id, checked_at)"
                )
                connection.exec_driver_sql("PRAGMA user_version = 24")
            if current < 25:
                # v25（8.4）：研究上下文快照与用户自定义研究模板。内置模板在代码中,不入库。
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS research_snapshots ("
                    "snapshot_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
                    "payload TEXT NOT NULL, created_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_research_snapshots_user "
                    "ON research_snapshots (user_id, created_at)"
                )
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS research_templates ("
                    "template_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
                    "payload TEXT NOT NULL, created_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql("PRAGMA user_version = 25")
            if current < 26:
                # v26（阶段 4）：同步 outbox/inbox/状态。payload 均为客户端加密信封,服务器只见密文。
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS sync_state ("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS sync_outbox ("
                    "outbox_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
                    "idempotency_key TEXT NOT NULL UNIQUE, version INTEGER NOT NULL DEFAULT 1, "
                    "payload TEXT NOT NULL, created_at TEXT NOT NULL, uploaded_at TEXT)"
                )
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS sync_inbox ("
                    "inbox_id INTEGER PRIMARY KEY AUTOINCREMENT, remote_event_id INTEGER NOT NULL UNIQUE, "
                    "kind TEXT NOT NULL, idempotency_key TEXT NOT NULL, version INTEGER NOT NULL, "
                    "payload TEXT NOT NULL, received_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql("PRAGMA user_version = 26")
            if current < 27:
                # v27（v26 迭代，2026-09-11 用户需求「借助 AI 分析评分」）：AI 技术面复核记录。
                # 定位：规则引擎算「图形证据的强度」，AI 复核「形态质量与可疑点」（假突破/量价背离/
                # 支撑压力位/与规则分的分歧）——手动触发、只读不写凭据库，结果留痕供长期研究。
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS tactics_ai_reviews ("
                    "review_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', "
                    "model TEXT NOT NULL DEFAULT '', rule_score REAL, ai_score REAL, "
                    "verdict TEXT NOT NULL DEFAULT '', agreement TEXT NOT NULL DEFAULT '', "
                    "summary TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL, "
                    "created_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_tactics_ai_reviews_symbol "
                    "ON tactics_ai_reviews (symbol, created_at)"
                )
                connection.exec_driver_sql("PRAGMA user_version = 27")
            if current < 28:
                # v28（2026-09-13 价值投资方案 §三）：研报追问的分析附录（append-only）。
                # 定位：原报告（ai_research_reports）不可变；每次追问生成一条**追加的**分析附录，
                # 记录父报告 id / 基础版本 / 补充材料快照 / 受影响 claim / 结论变化 / 模型与提示词版本，
                # 允许回看「当时为什么改变判断」。禁止任何写路径更新 ai_research_reports 的既有 payload。
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS ai_analysis_turns ("
                    "turn_id TEXT PRIMARY KEY, parent_report_id TEXT NOT NULL, "
                    "payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_ai_analysis_turns_parent "
                    "ON ai_analysis_turns (parent_report_id, created_at)"
                )
                connection.exec_driver_sql("PRAGMA user_version = 28")
            if current < 29:
                # D01（桌面端升级路线图 2026-09-18）：市场扫描任务化——进度可查、可取消、
                # 同输入指纹幂等命中。results/summary 只在终态写库（逐票进度只写计数器）。
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS scan_jobs ("
                    "job_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL, "
                    "done INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0, "
                    "request_body TEXT, summary TEXT, error TEXT, "
                    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_scan_jobs_fingerprint "
                    "ON scan_jobs (fingerprint, state)"
                )
                connection.exec_driver_sql("PRAGMA user_version = 29")
            if current < 30:
                # F01（桌面端升级路线图 2026-09-18）：模型用量落表——按调用记录 token 三项
                # （usage 缺失记 null，不按字数反推）、耗时、是否发生 thinking-disabled 重试与结果。
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS model_calls ("
                    "call_id TEXT PRIMARY KEY, profile_id TEXT, model TEXT NOT NULL, purpose TEXT, "
                    "prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER, "
                    "latency_ms INTEGER NOT NULL, retried INTEGER NOT NULL DEFAULT 0, "
                    "outcome TEXT NOT NULL, created_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_model_calls_created ON model_calls (created_at)"
                )
                connection.exec_driver_sql("PRAGMA user_version = 30")
            if current < 31:
                # F02（桌面端升级路线图 2026-09-18）：三方取数尝试日志——只有成功入账的
                # 证据会留痕，导致成功率/延迟此前永远是 None。此表按**每一次 HTTP 尝试**
                # 记一行（含重试的第几次），失败也记，使成功率与 P95 可复算。
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS feed_attempts ("
                    "attempt_id TEXT PRIMARY KEY, source TEXT NOT NULL, endpoint TEXT, "
                    "params_fingerprint TEXT, started_at TEXT NOT NULL, latency_ms INTEGER NOT NULL, "
                    "result TEXT NOT NULL, error_kind TEXT, attempt_index INTEGER NOT NULL DEFAULT 1)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_feed_attempts_source_time "
                    "ON feed_attempts (source, started_at)"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_feed_attempts_time ON feed_attempts (started_at)"
                )
                connection.exec_driver_sql("PRAGMA user_version = 31")
            if current < 32:
                # JV02（Jev 决策模型接入路线图 2026-09-21）：Jev 连接参数落库。
                # 为什么不复用 model_profiles：那是 chat 方案表，「恰好一个使用中」的语义
                # 不该被第二种协议（POST /systemone）污染。密钥**不进本表**——只存
                # credential_ref（凭据库引用），明文永远只在本机凭据库。
                connection.exec_driver_sql(
                    "CREATE TABLE IF NOT EXISTS jev_config ("
                    "user_id TEXT PRIMARY KEY, payload TEXT NOT NULL, "
                    "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                connection.exec_driver_sql("PRAGMA user_version = 32")
            if current < 33:
                # JV03（Jev 决策模型接入路线图 2026-09-21）：战法复核记录补判定层留痕。
                # 为什么加列而不是塞 payload：JV10 效果对账要按 engine 分组统计调用量与
                # 一致率、要按 confidence 分布定阈值——塞进 payload 就没法用 SQL 聚合。
                # `ai_score_raw` 是等级轴上的原始分（0…levels-1），`ai_score` 是归一化到
                # 0–100 的展示分；两者都留，否则事后无法复核「这个分是按几级 rubric 算的」。
                # `engine` 记判定层来源（jev / chat），空串表示旧记录（该列出现前落库的）。
                connection.exec_driver_sql(
                    "ALTER TABLE tactics_ai_reviews ADD COLUMN engine TEXT NOT NULL DEFAULT ''"
                )
                connection.exec_driver_sql("ALTER TABLE tactics_ai_reviews ADD COLUMN confidence REAL")
                connection.exec_driver_sql("ALTER TABLE tactics_ai_reviews ADD COLUMN ai_score_raw REAL")
                connection.exec_driver_sql("PRAGMA user_version = 33")
            if current > SCHEMA_VERSION:
                raise RuntimeError(f"database schema {current} is newer than Core {SCHEMA_VERSION}")

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _load(value: str) -> dict[str, Any]:
        return json.loads(value)

    def list_policies(self, user_id: UUID) -> list[InvestmentPolicyVersion]:
        with self._connection() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT payload FROM investment_policies WHERE user_id = :user_id ORDER BY version DESC"
                    ),
                    {"user_id": str(user_id)},
                )
                .mappings()
                .all()
            )
        return [InvestmentPolicyVersion.model_validate(self._load(row["payload"])) for row in rows]

    def insert_policy(self, policy: InvestmentPolicyVersion) -> None:
        payload = policy.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text("""
                INSERT INTO investment_policies
                  (policy_id, user_id, version, status, payload, created_at, updated_at)
                VALUES (:policy_id, :user_id, :version, :status, :payload, :created_at, :updated_at)
                """),
                {
                    "policy_id": str(policy.policy_id),
                    "user_id": str(policy.user_id),
                    "version": policy.version,
                    "status": policy.status.value,
                    "payload": self._json(payload),
                    "created_at": policy.created_at.isoformat(),
                    "updated_at": policy.updated_at.isoformat(),
                },
            )

    def activate_policy(
        self, policy_id: UUID, user_id: UUID, confirmation: dict[str, Any]
    ) -> InvestmentPolicyVersion:
        now = datetime.now(UTC)
        with self._connection() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT payload FROM investment_policies WHERE policy_id = :policy_id AND user_id = :user_id"
                    ),
                    {"policy_id": str(policy_id), "user_id": str(user_id)},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise KeyError("policy not found")
            policy = InvestmentPolicyVersion.model_validate(self._load(row["payload"]))
            if policy.status != PolicyStatus.DRAFT:
                raise ValueError("only draft policies can be activated")

            active_rows = (
                connection.execute(
                    text(
                        "SELECT policy_id, payload FROM investment_policies WHERE user_id = :user_id AND status = 'active'"
                    ),
                    {"user_id": str(user_id)},
                )
                .mappings()
                .all()
            )
            for active_row in active_rows:
                old = self._load(active_row["payload"])
                old["status"] = PolicyStatus.SUPERSEDED.value
                old["updated_at"] = now.isoformat()
                connection.execute(
                    text(
                        "UPDATE investment_policies SET status = 'superseded', payload = :payload, updated_at = :updated_at WHERE policy_id = :policy_id"
                    ),
                    {
                        "payload": self._json(old),
                        "updated_at": now.isoformat(),
                        "policy_id": active_row["policy_id"],
                    },
                )

            confirmed_data = policy.model_dump(mode="python")
            confirmed_data.update(
                {
                    "status": PolicyStatus.ACTIVE,
                    "confirmed_at": now,
                    "confirmation_method": confirmation["confirmation_method"],
                    "confirmation_summary": confirmation["confirmation_summary"],
                    "updated_at": now,
                    "revision": policy.revision + 1,
                }
            )
            confirmed = InvestmentPolicyVersion.model_validate(confirmed_data)
            connection.execute(
                text(
                    "UPDATE investment_policies SET status = 'active', payload = :payload, updated_at = :updated_at WHERE policy_id = :policy_id"
                ),
                {
                    "payload": self._json(confirmed.model_dump(mode="json")),
                    "updated_at": now.isoformat(),
                    "policy_id": str(policy_id),
                },
            )
        return confirmed

    def insert_evidence(self, evidence: Evidence) -> None:
        payload = evidence.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO evidence (evidence_id, tenant_id, content_hash, payload, created_at) VALUES (:evidence_id, :tenant_id, :content_hash, :payload, :created_at)"
                ),
                {
                    "evidence_id": str(evidence.evidence_id),
                    "tenant_id": str(evidence.tenant_id),
                    "content_hash": evidence.content_hash,
                    "payload": self._json(payload),
                    "created_at": evidence.collected_at.isoformat(),
                },
            )

    def list_evidence(self, tenant_id: UUID) -> list[Evidence]:
        with self._connection() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT payload FROM evidence WHERE tenant_id = :tenant_id ORDER BY created_at DESC"
                    ),
                    {"tenant_id": str(tenant_id)},
                )
                .mappings()
                .all()
            )
        return [Evidence.model_validate(self._load(row["payload"])) for row in rows]

    def list_evidence_page(
        self,
        tenant_id: UUID,
        limit: int = 200,
        offset: int = 0,
        evidence_type: str | None = None,
    ) -> tuple[list[Evidence], int]:
        """B02（桌面端升级路线图 2026-09-18）：证据列表服务端翻页，返回 (最近一页, 过滤后总数)。

        created_at 倒序（最近优先，与 list_evidence 同序）；evidence_type 按 payload 里的
        类型枚举值过滤。全量导出仍走 list_evidence（/export/all 依赖），不得改其语义。"""
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"tenant_id": str(tenant_id)}
        if evidence_type:
            clauses.append("json_extract(payload, '$.evidence_type') = :evidence_type")
            params["evidence_type"] = str(evidence_type)
        where = " AND ".join(clauses)
        with self._connection() as connection:
            total = int(
                connection.execute(
                    text(f"SELECT COUNT(*) FROM evidence WHERE {where}"), params
                ).scalar_one()
            )
            rows = (
                connection.execute(
                    text(
                        "SELECT payload FROM evidence "
                        f"WHERE {where} ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
                    ),
                    {**params, "limit": int(limit), "offset": int(offset)},
                )
                .mappings()
                .all()
            )
        return [Evidence.model_validate(self._load(row["payload"])) for row in rows], total

    def get_evidence_by_hash(self, content_hash: str, tenant_id: UUID) -> Evidence | None:
        """按内容哈希查证据（M6 异动提交幂等去重用）；同租户同哈希视为已入账。"""
        with self._connection() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT payload FROM evidence WHERE content_hash = :content_hash AND tenant_id = :tenant_id"
                    ),
                    {"content_hash": content_hash, "tenant_id": str(tenant_id)},
                )
                .mappings()
                .first()
            )
        return Evidence.model_validate(self._load(row["payload"])) if row else None

    def delete_evidence(self, evidence_id: UUID, tenant_id: UUID) -> bool:
        """按 id 删除证据（v23 历史重复清理用；仅限同租户）。"""
        with self._connection() as connection:
            result = connection.execute(
                text("DELETE FROM evidence WHERE evidence_id = :evidence_id AND tenant_id = :tenant_id"),
                {"evidence_id": str(evidence_id), "tenant_id": str(tenant_id)},
            )
            return result.rowcount > 0

    def get_evidence(self, evidence_id: UUID, tenant_id: UUID) -> Evidence | None:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT payload FROM evidence WHERE evidence_id = :evidence_id AND tenant_id = :tenant_id"
                    ),
                    {"evidence_id": str(evidence_id), "tenant_id": str(tenant_id)},
                )
                .mappings()
                .first()
            )
        return None if row is None else Evidence.model_validate(self._load(row["payload"]))

    def append_audit(self, event: AuditEvent) -> None:
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO audit_events (event_id, tenant_id, action, resource_type, resource_id, payload, created_at) VALUES (:event_id, :tenant_id, :action, :resource_type, :resource_id, :payload, :created_at)"
                ),
                {
                    "event_id": str(event.event_id),
                    "tenant_id": str(event.tenant_id),
                    "action": event.action,
                    "resource_type": event.resource_type,
                    "resource_id": event.resource_id,
                    "payload": self._json(event.payload),
                    "created_at": event.created_at.isoformat(),
                },
            )

    def list_audit(self, tenant_id: UUID) -> list[AuditEvent]:
        with self._connection() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT event_id, tenant_id, action, resource_type, resource_id, payload, created_at FROM audit_events WHERE tenant_id = :tenant_id ORDER BY created_at DESC"
                    ),
                    {"tenant_id": str(tenant_id)},
                )
                .mappings()
                .all()
            )
        return [
            AuditEvent(
                event_id=UUID(row["event_id"]),
                tenant_id=UUID(row["tenant_id"]),
                action=row["action"],
                resource_type=row["resource_type"],
                resource_id=row["resource_id"],
                payload=self._load(row["payload"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def list_audit_page(
        self,
        tenant_id: UUID,
        limit: int = 100,
        offset: int = 0,
        q: str | None = None,
        order: str = "time-desc",
    ) -> tuple[list[AuditEvent], int]:
        """B02（桌面端升级路线图 2026-09-18）：审计流服务端翻页，返回 (一页事件, 过滤后总数)。

        q 对 action / resource_type / resource_id 做 LIKE（与扩展页原前端过滤同口径）；
        order ∈ time-desc（默认）/ time-asc / action。全表读取仍走 list_audit。"""
        order_sql = {
            "time-desc": "created_at DESC, event_id DESC",
            "time-asc": "created_at ASC, event_id ASC",
            "action": "action ASC, created_at DESC, event_id ASC",
            "resource": "resource_type ASC, created_at DESC, event_id ASC",
        }.get(order, "created_at DESC, event_id DESC")
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {"tenant_id": str(tenant_id)}
        if q:
            clauses.append("(action LIKE :qpat OR resource_type LIKE :qpat OR resource_id LIKE :qpat)")
            params["qpat"] = f"%{q}%"
        where = " AND ".join(clauses)
        with self._connection() as connection:
            total = int(
                connection.execute(
                    text(f"SELECT COUNT(*) FROM audit_events WHERE {where}"), params
                ).scalar_one()
            )
            rows = (
                connection.execute(
                    text(
                        "SELECT event_id, tenant_id, action, resource_type, resource_id, payload, created_at "
                        f"FROM audit_events WHERE {where} ORDER BY {order_sql} LIMIT :limit OFFSET :offset"
                    ),
                    {**params, "limit": int(limit), "offset": int(offset)},
                )
                .mappings()
                .all()
            )
        return [
            AuditEvent(
                event_id=UUID(row["event_id"]),
                tenant_id=UUID(row["tenant_id"]),
                action=row["action"],
                resource_type=row["resource_type"],
                resource_id=row["resource_id"],
                payload=self._load(row["payload"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ], total

    def list_plugin_installations(self) -> list[PluginInstallation]:
        with self._connection() as connection:
            rows = (
                connection.execute(
                    text("SELECT payload FROM plugin_installations ORDER BY plugin_id")
                )
                .mappings()
                .all()
            )
        return [PluginInstallation.model_validate(self._load(row["payload"])) for row in rows]

    def get_plugin_installation(self, plugin_id: str) -> PluginInstallation | None:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT payload FROM plugin_installations WHERE plugin_id = :plugin_id"),
                    {"plugin_id": plugin_id},
                )
                .mappings()
                .first()
            )
        return (
            None if row is None else PluginInstallation.model_validate(self._load(row["payload"]))
        )

    def upsert_plugin_installation(self, installation: PluginInstallation) -> PluginInstallation:
        payload = installation.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO plugin_installations
                      (plugin_id, release_version, state, granted_capabilities, artifact_sha256, source, installed_at, updated_at, payload)
                    VALUES (:plugin_id, :release_version, :state, :granted_capabilities, :artifact_sha256, :source, :installed_at, :updated_at, :payload)
                    ON CONFLICT(plugin_id) DO UPDATE SET
                      release_version = excluded.release_version,
                      state = excluded.state,
                      granted_capabilities = excluded.granted_capabilities,
                      artifact_sha256 = excluded.artifact_sha256,
                      source = excluded.source,
                      updated_at = excluded.updated_at,
                      payload = excluded.payload
                    """
                ),
                {
                    "plugin_id": installation.plugin_id,
                    "release_version": installation.release_version,
                    "state": installation.state.value,
                    "granted_capabilities": self._json(installation.granted_capabilities),
                    "artifact_sha256": installation.artifact_sha256,
                    "source": installation.source,
                    "installed_at": installation.installed_at.isoformat(),
                    "updated_at": installation.updated_at.isoformat(),
                    "payload": self._json(payload),
                },
            )
        return installation

    def upsert_update_candidate(self, candidate: PluginUpdateCandidate) -> PluginUpdateCandidate:
        payload = candidate.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO plugin_update_candidates (candidate_id, plugin_id, payload, created_at, updated_at) "
                    "VALUES (:candidate_id, :plugin_id, :payload, :created_at, :updated_at) "
                    "ON CONFLICT(candidate_id) DO UPDATE SET "
                    "payload = excluded.payload, updated_at = excluded.updated_at"
                ),
                {
                    "candidate_id": str(candidate.candidate_id),
                    "plugin_id": candidate.plugin_id,
                    "payload": self._json(payload),
                    "created_at": candidate.created_at.isoformat(),
                    "updated_at": candidate.updated_at.isoformat(),
                },
            )
        return candidate

    def get_update_candidate(self, plugin_id: str) -> PluginUpdateCandidate | None:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT payload FROM plugin_update_candidates "
                        "WHERE plugin_id = :plugin_id ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"plugin_id": plugin_id},
                )
                .mappings()
                .first()
            )
        return (
            None if row is None else PluginUpdateCandidate.model_validate(self._load(row["payload"]))
        )

    def delete_update_candidate(self, candidate_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                text("DELETE FROM plugin_update_candidates WHERE candidate_id = :candidate_id"),
                {"candidate_id": candidate_id},
            )

    def list_update_candidates(self) -> list[PluginUpdateCandidate]:
        with self._connection() as connection:
            rows = (
                connection.execute(
                    text("SELECT payload FROM plugin_update_candidates ORDER BY created_at")
                )
                .mappings()
                .all()
            )
        return [PluginUpdateCandidate.model_validate(self._load(row["payload"])) for row in rows]

    def list_holdings(self, user_id: UUID) -> list[Holding]:
        return self._list_payload("holdings", "user_id", user_id, Holding)

    def get_holding(self, holding_id: UUID, user_id: UUID) -> Holding | None:
        return self._get_payload("holdings", "holding_id", holding_id, user_id, Holding)

    def insert_holding(self, holding: Holding) -> None:
        payload = holding.model_dump(mode="json")
        key = canonical_key(holding.instrument)
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO holdings (holding_id, user_id, payload, instrument_key, updated_at) "
                    "VALUES (:key, :user_id, :payload, :instrument_key, :updated_at) "
                    "ON CONFLICT(holding_id) DO UPDATE SET "
                    "payload = excluded.payload, instrument_key = excluded.instrument_key, "
                    "updated_at = excluded.updated_at"
                ),
                {
                    "key": str(holding.holding_id),
                    "user_id": str(holding.user_id),
                    "payload": self._json(payload),
                    "instrument_key": key,
                    "updated_at": holding.updated_at.isoformat(),
                },
            )

    def list_holdings_by_instrument(self, user_id: UUID, instrument_key: str) -> list[Holding]:
        with self._connection() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT payload FROM holdings "
                        "WHERE user_id = :user_id AND instrument_key = :instrument_key "
                        "ORDER BY updated_at DESC"
                    ),
                    {"user_id": str(user_id), "instrument_key": instrument_key},
                )
                .mappings()
                .all()
            )
        return [Holding.model_validate(self._load(row["payload"])) for row in rows]

    def delete_holding(self, holding_id: UUID, user_id: UUID) -> None:
        with self._connection() as connection:
            connection.execute(
                text("DELETE FROM holdings WHERE holding_id = :holding_id AND user_id = :user_id"),
                {"holding_id": str(holding_id), "user_id": str(user_id)},
            )

    def get_investor_profile(self, user_id: UUID) -> InvestorProfile | None:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT payload FROM investor_profiles WHERE user_id = :user_id"),
                    {"user_id": str(user_id)},
                )
                .mappings()
                .first()
            )
        return None if row is None else InvestorProfile.model_validate(self._load(row["payload"]))

    def get_personal_settings(self, user_id: UUID) -> PersonalSettings | None:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT payload FROM personal_settings WHERE user_id = :user_id"),
                    {"user_id": str(user_id)},
                )
                .mappings()
                .first()
            )
        return None if row is None else PersonalSettings.model_validate(self._load(row["payload"]))

    def upsert_personal_settings(self, settings: PersonalSettings) -> None:
        payload = settings.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO personal_settings (user_id, payload, created_at, updated_at) "
                    "VALUES (:user_id, :payload, :created_at, :updated_at) "
                    "ON CONFLICT(user_id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at"
                ),
                {
                    "user_id": str(settings.user_id),
                    "payload": self._json(payload),
                    "created_at": settings.created_at.isoformat(),
                    "updated_at": settings.updated_at.isoformat(),
                },
            )

    # —— Jev 决策模型配置（v32，JV02）：单用户 · 本机。密钥不在表内，只存凭据引用 ——

    def get_jev_settings(self, user_id: UUID) -> JevSettings | None:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT payload FROM jev_config WHERE user_id = :user_id"),
                    {"user_id": str(user_id)},
                )
                .mappings()
                .first()
            )
        return None if row is None else JevSettings.model_validate(self._load(row["payload"]))

    def upsert_jev_settings(self, settings: JevSettings) -> None:
        payload = settings.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO jev_config (user_id, payload, created_at, updated_at) "
                    "VALUES (:user_id, :payload, :created_at, :updated_at) "
                    "ON CONFLICT(user_id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at"
                ),
                {
                    "user_id": str(settings.user_id),
                    "payload": self._json(payload),
                    "created_at": settings.created_at.isoformat(),
                    "updated_at": settings.updated_at.isoformat(),
                },
            )

    # —— 量化制品索引（v23，路线图 3.2）：SQLite 只存元数据与哈希，本体在制品目录 ——

    def upsert_quant_artifact(
        self,
        *,
        artifact_id: str,
        kind: str,
        stage: str,
        symbol: str,
        name: str,
        parent_id: str | None,
        created_at: str,
        file_name: str,
        content_hash: str,
        payload_meta: dict[str, Any],
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO quant_artifacts (artifact_id, kind, stage, symbol, name, parent_id, "
                    "created_at, file_name, content_hash, payload_meta) "
                    "VALUES (:artifact_id, :kind, :stage, :symbol, :name, :parent_id, "
                    ":created_at, :file_name, :content_hash, :payload_meta) "
                    "ON CONFLICT(artifact_id) DO UPDATE SET "
                    "kind = excluded.kind, stage = excluded.stage, symbol = excluded.symbol, "
                    "name = excluded.name, parent_id = excluded.parent_id, created_at = excluded.created_at, "
                    "file_name = excluded.file_name, content_hash = excluded.content_hash, "
                    "payload_meta = excluded.payload_meta"
                ),
                {
                    "artifact_id": artifact_id,
                    "kind": kind,
                    "stage": stage,
                    "symbol": symbol,
                    "name": name,
                    "parent_id": parent_id,
                    "created_at": created_at,
                    "file_name": file_name,
                    "content_hash": content_hash,
                    "payload_meta": self._json(payload_meta),
                },
            )

    def _quant_artifact_row(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        data["payload_meta"] = self._load(data["payload_meta"])
        return data

    def get_quant_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT * FROM quant_artifacts WHERE artifact_id = :artifact_id"),
                    {"artifact_id": artifact_id},
                )
                .mappings()
                .first()
            )
        return None if row is None else self._quant_artifact_row(row)

    def list_quant_artifacts(self, kinds: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if kinds:
                rows = connection.execute(
                    text(
                        "SELECT * FROM quant_artifacts WHERE kind IN :kinds "
                        "ORDER BY created_at DESC"
                    ).bindparams(bindparam("kinds", expanding=True)),
                    {"kinds": list(kinds)},
                ).mappings().all()
            else:
                rows = connection.execute(
                    text("SELECT * FROM quant_artifacts ORDER BY created_at DESC")
                ).mappings().all()
        return [self._quant_artifact_row(row) for row in rows]

    def count_quant_artifacts(self) -> int:
        with self._connection() as connection:
            return int(
                connection.execute(text("SELECT COUNT(*) FROM quant_artifacts")).scalar_one()
            )

    # —— 本地量化实验（v24，路线图 7.2）：配置/快照/结果哈希登记 ——

    def get_quant_experiment(self, experiment_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT * FROM quant_experiments WHERE experiment_id = :eid"),
                    {"eid": experiment_id},
                )
                .mappings()
                .first()
            )
        return None if row is None else self._experiment_row(row)

    def _experiment_row(self, row: Any) -> dict[str, Any]:
        data = dict(row)
        data["config"] = self._load(data["config"])
        data["data_snapshot"] = self._load(data["data_snapshot"])
        data["software"] = self._load(data["software"])
        data["result"] = self._load(data["result"])
        return data

    def list_quant_experiments(self, user_id: UUID, symbol: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"user_id": str(user_id)}
        sql = "SELECT * FROM quant_experiments WHERE user_id = :user_id"
        if symbol:
            sql += " AND symbol = :symbol"
            params["symbol"] = symbol
        sql += " ORDER BY created_at DESC LIMIT 200"
        with self._connection() as connection:
            rows = connection.execute(text(sql), params).mappings().all()
        return [self._experiment_row(row) for row in rows]

    def insert_quant_experiment(self, record: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO quant_experiments (experiment_id, user_id, symbol, label, config, "
                    "data_snapshot, software, result_hash, result, created_at) "
                    "VALUES (:experiment_id, :user_id, :symbol, :label, :config, "
                    ":data_snapshot, :software, :result_hash, :result, :created_at)"
                ),
                {
                    "experiment_id": record["experiment_id"],
                    "user_id": record["user_id"],
                    "symbol": record["symbol"],
                    "label": record.get("label", ""),
                    "config": self._json(record["config"]),
                    "data_snapshot": self._json(record["data_snapshot"]),
                    "software": self._json(record["software"]),
                    "result_hash": record["result_hash"],
                    "result": self._json(record["result"]),
                    "created_at": record["created_at"],
                },
            )

    # —— 可检验判断（v24，8.2）：原判断冻结、验证只追加 ——

    def upsert_judgment(self, judgment: Any) -> None:
        payload = judgment.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO verifiable_judgments (judgment_id, user_id, status, due_at, payload, created_at) "
                    "VALUES (:judgment_id, :user_id, :status, :due_at, :payload, :created_at) "
                    "ON CONFLICT(judgment_id) DO UPDATE SET status = excluded.status, payload = excluded.payload"
                ),
                {
                    "judgment_id": str(judgment.judgment_id),
                    "user_id": str(judgment.user_id),
                    "status": judgment.status.value,
                    "due_at": judgment.due_at.isoformat(),
                    "payload": self._json(payload),
                    "created_at": judgment.created_at.isoformat(),
                },
            )

    def list_judgments(self, user_id: UUID, status: str | None = None) -> list[Any]:
        from investment_steward_core.domain.longterm import VerifiableJudgment

        params: dict[str, Any] = {"user_id": str(user_id)}
        sql = "SELECT payload FROM verifiable_judgments WHERE user_id = :user_id"
        if status:
            sql += " AND status = :status"
            params["status"] = status
        sql += " ORDER BY created_at DESC"
        with self._connection() as connection:
            rows = connection.execute(text(sql), params).mappings().all()
        return [VerifiableJudgment.model_validate(self._load(row["payload"])) for row in rows]

    def get_judgment(self, judgment_id: UUID, user_id: UUID) -> Any | None:
        from investment_steward_core.domain.longterm import VerifiableJudgment

        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT payload FROM verifiable_judgments WHERE judgment_id = :jid AND user_id = :uid"),
                    {"jid": str(judgment_id), "uid": str(user_id)},
                )
                .mappings()
                .first()
            )
        return None if row is None else VerifiableJudgment.model_validate(self._load(row["payload"]))

    def insert_judgment_verification(self, verification: Any) -> None:
        payload = verification.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO judgment_verifications (verification_id, judgment_id, result, payload, checked_at) "
                    "VALUES (:vid, :jid, :result, :payload, :checked_at)"
                ),
                {
                    "vid": str(verification.verification_id),
                    "jid": str(verification.judgment_id),
                    "result": verification.result.value,
                    "payload": self._json(payload),
                    "checked_at": verification.checked_at.isoformat(),
                },
            )

    def list_judgment_verifications(self, judgment_id: UUID) -> list[Any]:
        from investment_steward_core.domain.longterm import JudgmentVerification

        with self._connection() as connection:
            rows = connection.execute(
                text("SELECT payload FROM judgment_verifications WHERE judgment_id = :jid ORDER BY checked_at"),
                {"jid": str(judgment_id)},
            ).mappings().all()
        return [JudgmentVerification.model_validate(self._load(row["payload"])) for row in rows]

    def list_all_judgment_verifications(self, user_id: UUID) -> dict[str, list[Any]]:
        """G02（桌面端升级路线图 2026-09-18）：一次取全用户的验证记录，按 judgment_id 归组。

        命中率要按「模型方案 × 主题 × 时间窗」分桶，逐条判断去查验证记录会变成 N+1；
        这里用一次 JOIN 取回并按判断归组（按 `checked_at` 升序，取值口径与
        `list_judgment_verifications` 一致——**最近一条**就是列表末位）。
        单条 payload 坏掉不拖垮整份统计：跳过并继续（统计不该因一条脏数据整体失败）。
        """
        from investment_steward_core.domain.longterm import JudgmentVerification

        with self._connection() as connection:
            rows = connection.execute(
                text(
                    "SELECT v.judgment_id AS judgment_id, v.payload AS payload "
                    "FROM judgment_verifications v "
                    "JOIN verifiable_judgments j ON j.judgment_id = v.judgment_id "
                    "WHERE j.user_id = :uid ORDER BY v.checked_at"
                ),
                {"uid": str(user_id)},
            ).mappings().all()
        grouped: dict[str, list[Any]] = {}
        for row in rows:
            try:
                verification = JudgmentVerification.model_validate(self._load(row["payload"]))
            except Exception:  # noqa: BLE001 - 单条坏记录跳过，不让统计整体失败
                continue
            grouped.setdefault(str(row["judgment_id"]), []).append(verification)
        return grouped

    # —— 观察事项（v24，8.3）：事项状态机 + 检查记录只追加 ——

    def upsert_watch_item(self, item: Any) -> None:
        payload = item.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO watch_items (watch_id, user_id, status, dedup_key, payload, created_at, updated_at) "
                    "VALUES (:wid, :uid, :status, :dedup_key, :payload, :created_at, :updated_at) "
                    "ON CONFLICT(watch_id) DO UPDATE SET status = excluded.status, "
                    "payload = excluded.payload, updated_at = excluded.updated_at"
                ),
                {
                    "wid": str(item.watch_id),
                    "uid": str(item.user_id),
                    "status": item.status.value,
                    "dedup_key": item.dedup_key,
                    "payload": self._json(payload),
                    "created_at": item.created_at.isoformat(),
                    "updated_at": item.updated_at.isoformat(),
                },
            )

    def list_watch_items(self, user_id: UUID, status: str | None = None) -> list[Any]:
        from investment_steward_core.domain.longterm import WatchItem

        params: dict[str, Any] = {"user_id": str(user_id)}
        sql = "SELECT payload FROM watch_items WHERE user_id = :user_id"
        if status:
            sql += " AND status = :status"
            params["status"] = status
        sql += " ORDER BY updated_at DESC"
        with self._connection() as connection:
            rows = connection.execute(text(sql), params).mappings().all()
        return [WatchItem.model_validate(self._load(row["payload"])) for row in rows]

    def get_watch_item(self, watch_id: UUID, user_id: UUID) -> Any | None:
        from investment_steward_core.domain.longterm import WatchItem

        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT payload FROM watch_items WHERE watch_id = :wid AND user_id = :uid"),
                    {"wid": str(watch_id), "uid": str(user_id)},
                )
                .mappings()
                .first()
            )
        return None if row is None else WatchItem.model_validate(self._load(row["payload"]))

    def insert_watch_check(self, check: Any) -> None:
        payload = check.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO watch_checks (check_id, watch_id, triggered, payload, checked_at) "
                    "VALUES (:cid, :wid, :triggered, :payload, :checked_at)"
                ),
                {
                    "cid": str(check.check_id),
                    "wid": str(check.watch_id),
                    "triggered": int(check.triggered),
                    "payload": self._json(payload),
                    "checked_at": check.checked_at.isoformat(),
                },
            )

    def list_watch_checks(self, watch_id: UUID) -> list[Any]:
        from investment_steward_core.domain.longterm import WatchCheck

        with self._connection() as connection:
            rows = connection.execute(
                text("SELECT payload FROM watch_checks WHERE watch_id = :wid ORDER BY checked_at"),
                {"wid": str(watch_id)},
            ).mappings().all()
        return [WatchCheck.model_validate(self._load(row["payload"])) for row in rows]

    def find_watch_check_by_dedup(self, watch_id: UUID, dedup_value: str) -> Any | None:
        """防重复检查:同一观察事项下同一定位值（如同一交易日/同一数值）只记一次。"""
        from investment_steward_core.domain.longterm import WatchCheck

        with self._connection() as connection:
            rows = connection.execute(
                text("SELECT payload FROM watch_checks WHERE watch_id = :wid"),
                {"wid": str(watch_id)},
            ).mappings().all()
        for row in rows:
            check = WatchCheck.model_validate(self._load(row["payload"]))
            if check.note.startswith(f"dedup:{dedup_value}"):
                return check
        return None

    # —— 研究快照与模板（v25，8.4）：快照只读回看,不自动替换当前数据 ——

    def insert_research_snapshot(self, snapshot: Any) -> None:
        payload = snapshot.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO research_snapshots (snapshot_id, user_id, payload, created_at) "
                    "VALUES (:sid, :uid, :payload, :created_at)"
                ),
                {
                    "sid": str(snapshot.snapshot_id),
                    "uid": str(snapshot.user_id),
                    "payload": self._json(payload),
                    "created_at": snapshot.created_at.isoformat(),
                },
            )

    def _snapshot_row(self, row: Any, model: Any) -> Any:
        return model.model_validate(self._load(row["payload"]))

    def list_research_snapshots(self, user_id: UUID) -> list[Any]:
        from investment_steward_core.domain.longterm import ResearchSnapshot

        with self._connection() as connection:
            rows = connection.execute(
                text("SELECT payload FROM research_snapshots WHERE user_id = :uid ORDER BY created_at DESC"),
                {"uid": str(user_id)},
            ).mappings().all()
        return [self._snapshot_row(row, ResearchSnapshot) for row in rows]

    def get_research_snapshot(self, snapshot_id: UUID, user_id: UUID) -> Any | None:
        from investment_steward_core.domain.longterm import ResearchSnapshot

        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT payload FROM research_snapshots WHERE snapshot_id = :sid AND user_id = :uid"),
                    {"sid": str(snapshot_id), "uid": str(user_id)},
                )
                .mappings()
                .first()
            )
        return None if row is None else self._snapshot_row(row, ResearchSnapshot)

    def upsert_research_template(self, template: Any) -> None:
        from investment_steward_core.domain.longterm import ResearchTemplate

        payload = template.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO research_templates (template_id, user_id, payload, created_at) "
                    "VALUES (:tid, :uid, :payload, :created_at) "
                    "ON CONFLICT(template_id) DO UPDATE SET payload = excluded.payload"
                ),
                {
                    "tid": str(template.template_id),
                    "uid": str(template.user_id),
                    "payload": self._json(payload),
                    "created_at": template.created_at.isoformat(),
                },
            )

    def list_research_templates(self, user_id: UUID) -> list[Any]:
        from investment_steward_core.domain.longterm import ResearchTemplate

        with self._connection() as connection:
            rows = connection.execute(
                text("SELECT payload FROM research_templates WHERE user_id = :uid ORDER BY created_at DESC"),
                {"uid": str(user_id)},
            ).mappings().all()
        return [self._snapshot_row(row, ResearchTemplate) for row in rows]

    def delete_research_template(self, template_id: UUID, user_id: UUID) -> bool:
        with self._connection() as connection:
            result = connection.execute(
                text("DELETE FROM research_templates WHERE template_id = :tid AND user_id = :uid"),
                {"tid": str(template_id), "uid": str(user_id)},
            )
        return result.rowcount > 0

    # —— 阶段 4：同步状态 / outbox / inbox（payload 为客户端加密信封） ——

    def get_sync_state(self, key: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                text("SELECT value FROM sync_state WHERE key = :k"), {"k": key}
            ).first()
        return None if row is None else str(row[0])

    def set_sync_state(self, key: str, value: str) -> None:
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO sync_state (key, value, updated_at) VALUES (:k, :v, :t) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
                ),
                {"k": key, "v": value, "t": datetime.now(UTC).isoformat()},
            )

    def enqueue_sync_event(self, kind: str, idempotency_key: str, payload_ciphertext: str, *, version: int = 1) -> bool:
        """入队;幂等键已存在返回 False（不覆盖——同一事实版本冻结,新版本用新键）。"""
        with self._connection() as connection:
            result = connection.execute(
                text(
                    "INSERT OR IGNORE INTO sync_outbox (kind, idempotency_key, version, payload, created_at) "
                    "VALUES (:k2, :k, :v, :p, :t)"
                ),
                {"k2": kind, "k": idempotency_key, "v": version, "p": payload_ciphertext,
                 "t": datetime.now(UTC).isoformat()},
            )
        return result.rowcount > 0

    def list_pending_sync_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                text("SELECT outbox_id, kind, idempotency_key, version, payload FROM sync_outbox "
                     "WHERE uploaded_at IS NULL ORDER BY outbox_id LIMIT :l"),
                {"l": limit},
            ).mappings().all()
        return [dict(row) for row in rows]

    def mark_sync_uploaded(self, outbox_ids: list[int]) -> None:
        if not outbox_ids:
            return
        placeholders = ", ".join(f":id{i}" for i in range(len(outbox_ids)))
        params: dict[str, Any] = {"t": datetime.now(UTC).isoformat()}
        params.update({f"id{i}": value for i, value in enumerate(outbox_ids)})
        with self._connection() as connection:
            connection.execute(
                text(f"UPDATE sync_outbox SET uploaded_at = :t WHERE outbox_id IN ({placeholders})"),
                params,
            )

    def sync_outbox_counts(self) -> dict[str, int]:
        with self._connection() as connection:
            pending = connection.execute(
                text("SELECT COUNT(*) FROM sync_outbox WHERE uploaded_at IS NULL")
            ).scalar_one()
            total = connection.execute(text("SELECT COUNT(*) FROM sync_outbox")).scalar_one()
        return {"pending": int(pending), "total": int(total)}

    def insert_sync_inbox(self, remote_event_id: int, kind: str, idempotency_key: str,
                          version: int, payload_plaintext: str) -> bool:
        with self._connection() as connection:
            result = connection.execute(
                text(
                    "INSERT OR IGNORE INTO sync_inbox (remote_event_id, kind, idempotency_key, version, payload, received_at) "
                    "VALUES (:r, :k2, :k, :v, :p, :t)"
                ),
                {"r": remote_event_id, "k2": kind, "k": idempotency_key, "v": version,
                 "p": payload_plaintext, "t": datetime.now(UTC).isoformat()},
            )
        return result.rowcount > 0

    def list_sync_inbox(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                text("SELECT inbox_id, remote_event_id, kind, idempotency_key, version, payload, received_at "
                     "FROM sync_inbox ORDER BY remote_event_id DESC LIMIT :l"),
                {"l": limit},
            ).mappings().all()
        return [dict(row) for row in rows]

    def sync_inbox_count(self) -> int:
        with self._connection() as connection:
            return int(connection.execute(text("SELECT COUNT(*) FROM sync_inbox")).scalar_one())

    # —— 战法雷达：观察清单与笔记（official.stock-tactics，仅本机） ——

    def list_tactics_watchlist(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                text("SELECT symbol, name, note, created_at, updated_at FROM tactics_watchlist ORDER BY updated_at DESC")
            ).mappings().all()
        return [dict(row) for row in rows]

    def upsert_tactics_watch_entry(self, symbol: str, name: str, note: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            existing = connection.execute(
                text("SELECT created_at FROM tactics_watchlist WHERE symbol = :symbol"),
                {"symbol": symbol},
            ).first()
            created_at = str(existing[0]) if existing is not None else now
            connection.execute(
                text(
                    "INSERT INTO tactics_watchlist (symbol, name, note, created_at, updated_at) "
                    "VALUES (:symbol, :name, :note, :created_at, :updated_at) "
                    "ON CONFLICT(symbol) DO UPDATE SET name = excluded.name, note = excluded.note, updated_at = excluded.updated_at"
                ),
                {"symbol": symbol, "name": name, "note": note, "created_at": created_at, "updated_at": now},
            )

    def delete_tactics_watch_entry(self, symbol: str) -> bool:
        with self._connection() as connection:
            result = connection.execute(
                text("DELETE FROM tactics_watchlist WHERE symbol = :symbol"), {"symbol": symbol}
            )
        return result.rowcount > 0

    def add_tactics_note(self, note_id: str, symbol: str, content: str) -> dict[str, Any]:
        created_at = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            connection.execute(
                text("INSERT INTO tactics_notes (note_id, symbol, content, created_at) VALUES (:note_id, :symbol, :content, :created_at)"),
                {"note_id": note_id, "symbol": symbol, "content": content, "created_at": created_at},
            )
        return {"note_id": note_id, "symbol": symbol, "content": content, "created_at": created_at}

    def list_tactics_notes(self, symbol: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                text("SELECT note_id, symbol, content, created_at FROM tactics_notes WHERE symbol = :symbol ORDER BY created_at DESC"),
                {"symbol": symbol},
            ).mappings().all()
        return [dict(row) for row in rows]

    def delete_tactics_note(self, note_id: str) -> bool:
        with self._connection() as connection:
            result = connection.execute(
                text("DELETE FROM tactics_notes WHERE note_id = :note_id"), {"note_id": note_id}
            )
        return result.rowcount > 0

    # —— v27：AI 技术面复核记录（手动触发，留痕供长期研究） ——

    def insert_tactics_ai_review(self, review: dict[str, Any]) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO tactics_ai_reviews (review_id, symbol, name, model, rule_score, ai_score, "
                    "verdict, agreement, summary, payload, created_at, engine, confidence, ai_score_raw) "
                    "VALUES (:review_id, :symbol, :name, :model, :rule_score, :ai_score, "
                    ":verdict, :agreement, :summary, :payload, :created_at, :engine, :confidence, :ai_score_raw)"
                ),
                {
                    "review_id": str(review["review_id"]),
                    "symbol": str(review["symbol"]),
                    "name": str(review.get("name") or ""),
                    "model": str(review.get("model") or ""),
                    "rule_score": review.get("rule_score"),
                    "ai_score": review.get("ai_score"),
                    "verdict": str(review.get("verdict") or ""),
                    "agreement": str(review.get("agreement") or ""),
                    "summary": str(review.get("summary") or ""),
                    "payload": self._json(review.get("payload") or {}),
                    "created_at": str(review["created_at"]),
                    # JV03：判定层来源与可复算原始值（见 v33 迁移注释）。
                    "engine": str(review.get("engine") or ""),
                    "confidence": review.get("confidence"),
                    "ai_score_raw": review.get("ai_score_raw"),
                },
            )
        return dict(review)

    def list_tactics_ai_reviews(self, symbol: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        """AI 复核历史（可按标的过滤），按时间倒序；payload 原样带出供前端展开。"""
        query = (
            "SELECT review_id, symbol, name, model, rule_score, ai_score, verdict, agreement, summary, "
            "payload, created_at, engine, confidence, ai_score_raw FROM tactics_ai_reviews"
        )
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
        if symbol:
            query += " WHERE symbol = :symbol"
            params["symbol"] = symbol
        query += " ORDER BY created_at DESC LIMIT :limit"
        with self._connection() as connection:
            rows = connection.execute(text(query), params).mappings().all()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(str(item.get("payload") or "{}"))
            except json.JSONDecodeError:
                item["payload"] = {}
            items.append(item)
        return items

    def delete_tactics_ai_review(self, review_id: str) -> bool:
        with self._connection() as connection:
            result = connection.execute(
                text("DELETE FROM tactics_ai_reviews WHERE review_id = :review_id"),
                {"review_id": review_id},
            )
        return result.rowcount > 0

    def upsert_investor_profile(self, profile: InvestorProfile) -> None:
        payload = profile.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO investor_profiles (user_id, payload, created_at, updated_at) "
                    "VALUES (:user_id, :payload, :created_at, :updated_at) "
                    "ON CONFLICT(user_id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at"
                ),
                {
                    "user_id": str(profile.user_id),
                    "payload": self._json(payload),
                    "created_at": profile.created_at.isoformat(),
                    "updated_at": profile.updated_at.isoformat(),
                },
            )

    def list_theses(self, user_id: UUID) -> list[Thesis]:
        with self._connection() as connection:
            rows = (
                connection.execute(
                    text("SELECT payload FROM thesis WHERE user_id = :user_id ORDER BY updated_at DESC"),
                    {"user_id": str(user_id)},
                )
                .mappings()
                .all()
            )
        return [Thesis.model_validate(self._load(row["payload"])) for row in rows]

    def get_thesis(self, thesis_id: UUID, user_id: UUID) -> Thesis | None:
        return self._get_payload("thesis", "thesis_id", thesis_id, user_id, Thesis)

    def upsert_thesis(self, thesis: Thesis) -> None:
        payload = thesis.model_dump(mode="json")
        with self._connection() as connection:
            # 同一用户/规范标的同时只保留一个 active thesis。旧版本归档而不删除，
            # 保证编辑和重复提交不会制造不可解释的 active 冲突。
            if thesis.status.value == "active":
                rows = connection.execute(
                    text("SELECT thesis_id, payload FROM thesis WHERE user_id = :user_id"),
                    {"user_id": str(thesis.user_id)},
                ).mappings().all()
                key = canonical_key(thesis.instrument)
                now = thesis.updated_at.isoformat()
                for row in rows:
                    if row["thesis_id"] == str(thesis.thesis_id):
                        continue
                    try:
                        previous = Thesis.model_validate(self._load(row["payload"]))
                    except (TypeError, ValueError, KeyError):  # preserve corrupt historical payload
                        continue
                    if previous.status.value == "active" and canonical_key(previous.instrument) == key:
                        archived = previous.model_copy(update={"status": "archived", "updated_at": thesis.updated_at})
                        connection.execute(
                            text("UPDATE thesis SET payload = :payload, updated_at = :updated_at WHERE thesis_id = :thesis_id"),
                            {"payload": self._json(archived.model_dump(mode="json")), "updated_at": now, "thesis_id": row["thesis_id"]},
                        )
            connection.execute(
                text(
                    "INSERT INTO thesis (thesis_id, user_id, instrument, payload, updated_at) "
                    "VALUES (:key, :user_id, :instrument, :payload, :updated_at) "
                    "ON CONFLICT(thesis_id) DO UPDATE SET "
                    "instrument = excluded.instrument, payload = excluded.payload, updated_at = excluded.updated_at"
                ),
                {
                    "key": str(thesis.thesis_id),
                    "user_id": str(thesis.user_id),
                    "instrument": thesis.instrument,
                    "payload": self._json(payload),
                    "updated_at": thesis.updated_at.isoformat(),
                },
            )

    def list_plans(self, user_id: UUID) -> list[Plan]:
        return self._list_payload("plans", "user_id", user_id, Plan)

    def get_plan(self, plan_id: UUID, user_id: UUID) -> Plan:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT payload FROM plans WHERE plan_id = :plan_id AND user_id = :user_id"),
                    {"plan_id": str(plan_id), "user_id": str(user_id)},
                )
                .mappings()
                .first()
            )
        if row is None:
            raise KeyError(f"plan {plan_id} not found for user")
        return Plan.model_validate(self._load(row["payload"]))

    def insert_plan(self, plan: Plan) -> None:
        self._upsert_payload("plans", "plan_id", plan.plan_id, plan)

    def list_decisions(self, user_id: UUID) -> list[DecisionEntry]:
        with self._connection() as connection:
            rows = (
                connection.execute(
                    text("SELECT payload FROM decisions WHERE user_id = :user_id ORDER BY made_at DESC"),
                    {"user_id": str(user_id)},
                )
                .mappings()
                .all()
            )
        return [DecisionEntry.model_validate(self._load(row["payload"])) for row in rows]

    def get_decision(self, decision_id: UUID, user_id: UUID) -> DecisionEntry | None:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT payload FROM decisions WHERE decision_id = :did AND user_id = :uid"),
                    {"did": str(decision_id), "uid": str(user_id)},
                )
                .mappings()
                .first()
            )
        return None if row is None else DecisionEntry.model_validate(self._load(row["payload"]))

    def insert_decision(self, decision: DecisionEntry) -> None:
        payload = decision.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO decisions (decision_id, user_id, payload, made_at) VALUES (:decision_id, :user_id, :payload, :made_at)"
                ),
                {
                    "decision_id": str(decision.decision_id),
                    "user_id": str(decision.user_id),
                    "payload": self._json(payload),
                    "made_at": decision.made_at.isoformat(),
                },
            )

    def update_decision_outcome(self, user_id: UUID, decision_id: UUID, outcome: str, retrospective: str) -> DecisionEntry | None:
        """E2 研究后验：只允许补录「结果」与「事后评价」两个事后字段。

        原始判断（theme / decision_summary / rationale / linked_evidence_ids / made_at）
        一律不可改写——避免用事后信息改写原始决定（路线图 E2 铁律）。
        """
        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT payload FROM decisions WHERE decision_id = :did AND user_id = :uid"),
                    {"did": str(decision_id), "uid": str(user_id)},
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            payload = self._load(row["payload"])
            payload["outcome"] = outcome
            payload["retrospective"] = retrospective
            connection.execute(
                text("UPDATE decisions SET payload = :payload WHERE decision_id = :did"),
                {"payload": self._json(payload), "did": str(decision_id)},
            )
        return DecisionEntry.model_validate(payload)

    def list_research_runs(self, user_id: UUID) -> list[ResearchRun]:
        with self._connection() as connection:
            rows = (
                connection.execute(
                    text("SELECT payload FROM research_runs WHERE user_id = :user_id ORDER BY created_at DESC"),
                    {"user_id": str(user_id)},
                )
                .mappings()
                .all()
            )
        return [ResearchRun.model_validate(self._load(row["payload"])) for row in rows]

    def get_research_run(self, run_id: UUID, user_id: UUID) -> ResearchRun | None:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT payload FROM research_runs WHERE run_id = :run_id AND user_id = :user_id"),
                    {"run_id": str(run_id), "user_id": str(user_id)},
                )
                .mappings()
                .first()
            )
        return None if row is None else ResearchRun.model_validate(self._load(row["payload"]))

    def upsert_research_run(self, run: ResearchRun) -> ResearchRun:
        payload = run.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO research_runs (run_id, user_id, status, payload, created_at, updated_at) VALUES (:run_id, :user_id, :status, :payload, :created_at, :updated_at) "
                    "ON CONFLICT(run_id) DO UPDATE SET status = excluded.status, payload = excluded.payload, updated_at = excluded.updated_at"
                ),
                {
                    "run_id": str(run.run_id),
                    "user_id": str(run.user_id),
                    "status": run.status.value,
                    "payload": self._json(payload),
                    "created_at": run.created_at.isoformat(),
                    "updated_at": run.updated_at.isoformat(),
                },
            )
        return run

    def transition_research_run(self, run_id: UUID, user_id: UUID, target: RunStatus) -> ResearchRun:
        """推进研究链路状态机；非法迁移抛 ValueError，失败写入 error_code。"""
        run = self.get_research_run(run_id, user_id)
        if run is None:
            raise KeyError("research run not found")
        allowed = ALLOWED_RUN_TRANSITIONS[run.status]
        if target not in allowed:
            raise ValueError(f"invalid ResearchRun transition: {run.status.value} -> {target.value}")
        now = datetime.now(UTC)
        advanced = run.model_copy(
            update={
                "status": target,
                "updated_at": now,
                "error_code": None if target != RunStatus.FAILED else run.error_code,
            }
        )
        return self.upsert_research_run(advanced)

    def delete_research_run(self, run_id: UUID, user_id: UUID) -> bool:
        """删除研究运行及其关联回答；未找到返回 False（调用方转 404）。"""
        with self._connection() as connection:
            result = connection.execute(
                text("DELETE FROM research_runs WHERE run_id = :run_id AND user_id = :user_id"),
                {"run_id": str(run_id), "user_id": str(user_id)},
            )
            connection.execute(
                text("DELETE FROM agent_responses WHERE run_id = :run_id AND tenant_id = :user_id"),
                {"run_id": str(run_id), "user_id": str(user_id)},
            )
        return result.rowcount > 0

    def upsert_agent_response(self, response: AgentResponse) -> AgentResponse:
        payload = response.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO agent_responses (response_id, tenant_id, run_id, payload, created_at) "
                    "VALUES (:response_id, :tenant_id, :run_id, :payload, :created_at) "
                    "ON CONFLICT(response_id) DO UPDATE SET payload = excluded.payload"
                ),
                {
                    "response_id": str(response.response_id),
                    "tenant_id": str(response.user_id),
                    "run_id": str(response.run_id),
                    "payload": self._json(payload),
                    "created_at": response.created_at.isoformat(),
                },
            )
        return response

    def get_agent_response(self, response_id: UUID, tenant_id: UUID) -> AgentResponse | None:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT payload FROM agent_responses WHERE response_id = :response_id AND tenant_id = :tenant_id"
                    ),
                    {"response_id": str(response_id), "tenant_id": str(tenant_id)},
                )
                .mappings()
                .first()
            )
        return None if row is None else AgentResponse.model_validate(self._load(row["payload"]))

    def list_agent_responses(self, tenant_id: UUID) -> list[AgentResponse]:
        with self._connection() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT payload FROM agent_responses WHERE tenant_id = :tenant_id ORDER BY created_at DESC"
                    ),
                    {"tenant_id": str(tenant_id)},
                )
                .mappings()
                .all()
            )
        return [AgentResponse.model_validate(self._load(row["payload"])) for row in rows]

    def get_agent_response_by_run(self, run_id: UUID, tenant_id: UUID) -> AgentResponse | None:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT payload FROM agent_responses WHERE run_id = :run_id AND tenant_id = :tenant_id LIMIT 1"
                    ),
                    {"run_id": str(run_id), "tenant_id": str(tenant_id)},
                )
                .mappings()
                .first()
            )
        return None if row is None else AgentResponse.model_validate(self._load(row["payload"]))

    def upsert_learning_goal(self, goal: LearningGoal) -> None:
        self._upsert_payload("learning_goals", "goal_id", goal.goal_id, goal)

    def list_learning_goals(self, user_id: UUID) -> list[LearningGoal]:
        return self._list_payload("learning_goals", "user_id", user_id, LearningGoal)

    def upsert_learning_activity(self, activity: LearningActivity) -> None:
        payload = activity.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO learning_activities (activity_id, user_id, unit_id, payload, completed_at, created_at) "
                    "VALUES (:activity_id, :user_id, :unit_id, :payload, :completed_at, :created_at) "
                    "ON CONFLICT(activity_id) DO UPDATE SET "
                    "payload = excluded.payload, completed_at = excluded.completed_at"
                ),
                {
                    "activity_id": str(activity.activity_id),
                    "user_id": str(activity.user_id),
                    "unit_id": str(activity.unit_id),
                    "payload": self._json(payload),
                    "completed_at": activity.completed_at.isoformat(),
                    "created_at": activity.created_at.isoformat(),
                },
            )

    def list_learning_activities(self, user_id: UUID) -> list[LearningActivity]:
        with self._connection() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT payload FROM learning_activities WHERE user_id = :user_id "
                        "ORDER BY completed_at DESC"
                    ),
                    {"user_id": str(user_id)},
                )
                .mappings()
                .all()
            )
        return [LearningActivity.model_validate(self._load(row["payload"])) for row in rows]

    def get_learning_activity(self, activity_id: UUID, user_id: UUID) -> LearningActivity | None:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT payload FROM learning_activities "
                        "WHERE activity_id = :activity_id AND user_id = :user_id"
                    ),
                    {"activity_id": str(activity_id), "user_id": str(user_id)},
                )
                .mappings()
                .first()
            )
        return None if row is None else LearningActivity.model_validate(self._load(row["payload"]))

    def delete_learning_activity(self, activity_id: UUID, user_id: UUID) -> None:
        with self._connection() as connection:
            connection.execute(
                text(
                    "DELETE FROM learning_activities WHERE activity_id = :activity_id AND user_id = :user_id"
                ),
                {"activity_id": str(activity_id), "user_id": str(user_id)},
            )

    # ---- 阶段 D：日报 / 通知 落库 ----

    def upsert_today_brief(self, day: str, brief: TodayBrief) -> TodayBrief:
        payload = brief.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO today_briefs (day, user_id, payload, created_at) "
                    "VALUES (:day, :user_id, :payload, :created_at) "
                    "ON CONFLICT(day, user_id) DO UPDATE SET "
                    "payload = excluded.payload, created_at = excluded.created_at"
                ),
                {
                    "day": day,
                    "user_id": str(brief.user_id),
                    "payload": self._json(payload),
                    "created_at": brief.generated_at.isoformat(),
                },
            )
        return brief

    def get_today_brief(self, day: str, user_id: UUID) -> TodayBrief | None:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT payload FROM today_briefs WHERE day = :day AND user_id = :user_id"),
                    {"day": day, "user_id": str(user_id)},
                )
                .mappings()
                .first()
            )
        return None if row is None else TodayBrief.model_validate(self._load(row["payload"]))

    def upsert_notification(self, notification: Notification, dedup_key: str) -> Notification:
        payload = notification.model_dump(mode="json")
        with self._connection() as connection:
            existing = connection.execute(
                text("SELECT payload FROM notifications WHERE dedup_key = :dedup_key"),
                {"dedup_key": dedup_key},
            ).mappings().first()
            if existing:
                # 规则重评估只更新证据/时间，不得把用户已读状态重置为 unread。
                previous = self._load(existing["payload"])
                payload["read"] = bool(previous.get("read", False))
            connection.execute(
                text(
                    "INSERT INTO notifications (notification_id, user_id, dedup_key, payload, created_at) "
                    "VALUES (:notification_id, :user_id, :dedup_key, :payload, :created_at) "
                    "ON CONFLICT(dedup_key) DO UPDATE SET "
                    "notification_id = excluded.notification_id, "
                    "payload = excluded.payload, created_at = excluded.created_at"
                ),
                {
                    "notification_id": str(notification.notification_id),
                    "user_id": str(notification.user_id),
                    "dedup_key": dedup_key,
                    "payload": self._json(payload),
                    "created_at": notification.created_at.isoformat(),
                },
            )
        return notification

    def notification_exists(self, user_id: UUID, dedup_key: str) -> bool:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT 1 FROM notifications WHERE user_id = :user_id AND dedup_key = :dedup_key"),
                    {"user_id": str(user_id), "dedup_key": dedup_key},
                )
                .mappings()
                .first()
            )
        return row is not None

    def list_notifications(self, user_id: UUID) -> list[Notification]:
        with self._connection() as connection:
            rows = (
                connection.execute(
                    text("SELECT payload FROM notifications WHERE user_id = :user_id ORDER BY created_at DESC"),
                    {"user_id": str(user_id)},
                )
                .mappings()
                .all()
            )
        return [Notification.model_validate(self._load(row["payload"])) for row in rows]

    def get_notification_by_dedup(self, dedup_key: str) -> Notification | None:
        """按存储侧 dedup 键取通知（digest 等非规则键的周期去重专用）。"""
        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT payload FROM notifications WHERE dedup_key = :dedup_key"),
                    {"dedup_key": dedup_key},
                )
                .mappings()
                .first()
            )
        return None if row is None else Notification.model_validate(self._load(row["payload"]))

    def mark_notification_read(self, notification_id: UUID, user_id: UUID) -> Notification | None:
        with self._connection() as connection:
            row = connection.execute(
                text("SELECT payload FROM notifications WHERE notification_id = :id AND user_id = :user_id"),
                {"id": str(notification_id), "user_id": str(user_id)},
            ).mappings().first()
            if row is None:
                return None
            payload = self._load(row["payload"])
            payload["read"] = True
            connection.execute(
                text("UPDATE notifications SET payload = :payload WHERE notification_id = :id"),
                {"payload": self._json(payload), "id": str(notification_id)},
            )
        return Notification.model_validate(payload)

    # ---- 凭据库（G3）----

    def get_credential(self, key_id: str) -> str | None:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT secret FROM credentials WHERE key_id = :key_id"),
                    {"key_id": key_id},
                )
                .mappings()
                .first()
            )
        return None if row is None else row["secret"]

    def upsert_credential(self, key_id: str, secret: str) -> CredentialRecord:
        last4 = secret[-4:] if len(secret) >= 4 else secret
        now = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO credentials (key_id, secret, last4, updated_at) "
                    "VALUES (:key_id, :secret, :last4, :updated_at) "
                    "ON CONFLICT(key_id) DO UPDATE SET "
                    "secret = excluded.secret, last4 = excluded.last4, updated_at = excluded.updated_at"
                ),
                {"key_id": key_id, "secret": secret, "last4": last4, "updated_at": now},
            )
        return CredentialRecord(key_id=key_id, last4=last4, backend="file")

    def upsert_credential_meta(self, key_id: str, last4: str) -> None:
        """OS 凭据模式下仅写目录行（secret 留空、明文在 OS 端），供列表/摘要展示。"""
        now = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO credentials (key_id, secret, last4, updated_at) "
                    "VALUES (:key_id, '', :last4, :updated_at) "
                    "ON CONFLICT(key_id) DO UPDATE SET "
                    "secret = excluded.secret, last4 = excluded.last4, updated_at = excluded.updated_at"
                ),
                {"key_id": key_id, "last4": last4, "updated_at": now},
            )

    def delete_credential(self, key_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                text("DELETE FROM credentials WHERE key_id = :key_id"), {"key_id": key_id}
            )

    def list_credentials(self) -> list[CredentialRecord]:
        with self._connection() as connection:
            rows = (
                connection.execute(text("SELECT key_id, last4, updated_at FROM credentials ORDER BY key_id"))
                .mappings()
                .all()
            )
        return [
            CredentialRecord(
                key_id=row["key_id"],
                last4=row["last4"],
                updated_at=datetime.fromisoformat(row["updated_at"]),
                backend="file",
            )
            for row in rows
        ]

    # ---- 模型服务多方案（G3）----

    def upsert_model_profile(self, profile: ModelProfile) -> ModelProfile:
        payload = profile.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO model_profiles (profile_id, payload, updated_at) "
                    "VALUES (:profile_id, :payload, :updated_at) "
                    "ON CONFLICT(profile_id) DO UPDATE SET "
                    "payload = excluded.payload, updated_at = excluded.updated_at"
                ),
                {
                    "profile_id": str(profile.profile_id),
                    "payload": self._json(payload),
                    "updated_at": profile.updated_at.isoformat(),
                },
            )
        return profile

    def list_model_profiles(self) -> list[ModelProfile]:
        with self._connection() as connection:
            rows = (
                connection.execute(text("SELECT payload FROM model_profiles ORDER BY updated_at"))
                .mappings()
                .all()
            )
        return [ModelProfile.model_validate(self._load(row["payload"])) for row in rows]

    def get_model_profile(self, profile_id: UUID) -> ModelProfile | None:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT payload FROM model_profiles WHERE profile_id = :profile_id"),
                    {"profile_id": str(profile_id)},
                )
                .mappings()
                .first()
            )
        return None if row is None else ModelProfile.model_validate(self._load(row["payload"]))

    def delete_model_profile(self, profile_id: UUID) -> None:
        with self._connection() as connection:
            connection.execute(
                text("DELETE FROM model_profiles WHERE profile_id = :profile_id"),
                {"profile_id": str(profile_id)},
            )

    def set_active_model_profile(self, profile_id: UUID) -> ModelProfile:
        """激活某方案为唯一「使用中」：先把其它方案置为「未启用」，再原子提升目标方案。"""
        now = datetime.now(UTC)
        profiles = self.list_model_profiles()
        target = next((item for item in profiles if item.profile_id == profile_id), None)
        if target is None:
            raise KeyError("model profile not found")
        with self._connection() as connection:
            for item in profiles:
                if item.profile_id == profile_id:
                    continue
                updated = item.model_copy(update={"status": ModelProfileStatus.INACTIVE, "updated_at": now})
                connection.execute(
                    text(
                        "UPDATE model_profiles SET payload = :payload, updated_at = :updated_at WHERE profile_id = :profile_id"
                    ),
                    {
                        "payload": self._json(updated.model_dump(mode="json")),
                        "updated_at": now.isoformat(),
                        "profile_id": str(item.profile_id),
                    },
                )
            activated = target.model_copy(update={"status": ModelProfileStatus.ACTIVE, "updated_at": now})
            connection.execute(
                text(
                    "UPDATE model_profiles SET payload = :payload, updated_at = :updated_at WHERE profile_id = :profile_id"
                ),
                {
                    "payload": self._json(activated.model_dump(mode="json")),
                    "updated_at": now.isoformat(),
                    "profile_id": str(profile_id),
                },
            )
        return activated

    # ---- 研读图书馆（G5）----

    def upsert_book(self, book: Book) -> Book:
        payload = book.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO books (book_id, payload, updated_at) "
                    "VALUES (:book_id, :payload, :updated_at) "
                    "ON CONFLICT(book_id) DO UPDATE SET "
                    "payload = excluded.payload, updated_at = excluded.updated_at"
                ),
                {
                    "book_id": str(book.book_id),
                    "payload": self._json(payload),
                    "updated_at": book.updated_at.isoformat(),
                },
            )
        return book

    def list_books(self) -> list[Book]:
        with self._connection() as connection:
            rows = (
                connection.execute(text("SELECT payload FROM books ORDER BY updated_at"))
                .mappings()
                .all()
            )
        return [Book.model_validate(self._load(row["payload"])) for row in rows]

    def get_book(self, book_id: UUID) -> Book | None:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT payload FROM books WHERE book_id = :book_id"),
                    {"book_id": str(book_id)},
                )
                .mappings()
                .first()
            )
        return None if row is None else Book.model_validate(self._load(row["payload"]))

    def delete_book(self, book_id: UUID) -> None:
        with self._connection() as connection:
            connection.execute(
                text("DELETE FROM books WHERE book_id = :book_id"), {"book_id": str(book_id)}
            )

    def upsert_library_plan(self, plan: LibraryPlan) -> LibraryPlan:
        payload = plan.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO library_plans (plan_id, payload, created_at) "
                    "VALUES (:plan_id, :payload, :created_at) "
                    "ON CONFLICT(plan_id) DO UPDATE SET "
                    "payload = excluded.payload"
                ),
                {
                    "plan_id": str(plan.plan_id),
                    "payload": self._json(payload),
                    "created_at": plan.created_at.isoformat(),
                },
            )
        return plan

    def list_library_plans(self) -> list[LibraryPlan]:
        with self._connection() as connection:
            rows = (
                connection.execute(text("SELECT payload FROM library_plans ORDER BY created_at DESC"))
                .mappings()
                .all()
            )
        return [LibraryPlan.model_validate(self._load(row["payload"])) for row in rows]

    # ---- 宏观雷达数据缓存（M4）----

    def upsert_macro_series(self, series_key: str, payload: dict[str, Any]) -> None:
        """缓存一条宏观序列（series_key = "{region}:{indicator}"，payload 含 observations/as_of/dataset_version）。"""
        now = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO macro_cache (series_key, payload, updated_at) "
                    "VALUES (:series_key, :payload, :updated_at) "
                    "ON CONFLICT(series_key) DO UPDATE SET "
                    "payload = excluded.payload, updated_at = excluded.updated_at"
                ),
                {"series_key": series_key, "payload": self._json(payload), "updated_at": now},
            )

    def latest_macro_updated_at(self) -> str | None:
        """H4-3:macro_cache 最新写入时间,喂状态栏「数据截至」的宏观域。"""
        with self._connection() as connection:
            row = connection.execute(text("SELECT MAX(updated_at) FROM macro_cache")).fetchone()
            return str(row[0]) if row and row[0] else None

    def get_macro_series(self, series_key: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT payload FROM macro_cache WHERE series_key = :series_key"),
                    {"series_key": series_key},
                )
                .mappings()
                .first()
            )
        return None if row is None else self._load(row["payload"])

    # ---- 宏观权重版本链（M5.5）：active = created_at 最新的一条；无记录 → 默认 v1 ----

    def list_macro_weight_versions(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = (
                connection.execute(
                    text("SELECT version, weights, source, note, created_at FROM macro_weight_versions ORDER BY created_at DESC, version DESC")
                )
                .mappings()
                .all()
            )
        return [
            {
                "version": row["version"],
                "weights": self._load(row["weights"]),
                "source": row["source"],
                "note": row["note"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def insert_macro_weight_version(self, version: str, weights: dict[str, float], source: str, note: str | None, created_at: str) -> None:
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO macro_weight_versions (version, weights, source, note, created_at) "
                    "VALUES (:version, :weights, :source, :note, :created_at) "
                    "ON CONFLICT(version) DO UPDATE SET weights = excluded.weights, source = excluded.source, "
                    "note = excluded.note, created_at = excluded.created_at"
                ),
                {
                    "version": version,
                    "weights": self._json(weights),
                    "source": source,
                    "note": note,
                    "created_at": created_at,
                },
            )

    # ---- 宏观用户分析层（§3.7 第三层，D-15）：每经济体一条，仅本机 ----

    def upsert_macro_user_view(self, view: Any) -> None:
        payload = view.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO macro_user_views (region, payload, updated_at) "
                    "VALUES (:region, :payload, :updated_at) "
                    "ON CONFLICT(region) DO UPDATE SET "
                    "payload = excluded.payload, updated_at = excluded.updated_at"
                ),
                {"region": view.region, "payload": self._json(payload), "updated_at": view.updated_at.isoformat()},
            )

    def get_macro_user_view(self, region: str) -> Any | None:
        from investment_steward_core.domain import MacroUserView

        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT payload FROM macro_user_views WHERE region = :region"),
                    {"region": region},
                )
                .mappings()
                .first()
            )
        return None if row is None else MacroUserView.model_validate(self._load(row["payload"]))

    # ---- 发言人信号流（§3.5 段一/段二，D-13）：仅本机 ----

    def upsert_speaker_signal(self, signal: Any) -> None:
        payload = signal.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO speaker_signals (signal_id, region, payload, created_at) "
                    "VALUES (:signal_id, :region, :payload, :created_at) "
                    "ON CONFLICT(signal_id) DO UPDATE SET payload = excluded.payload"
                ),
                {
                    "signal_id": signal.signal_id,
                    "region": signal.region,
                    "payload": self._json(payload),
                    "created_at": signal.created_at.isoformat(),
                },
            )

    def list_speaker_signals(self, region: str) -> list[Any]:
        from investment_steward_core.domain import SpeakerSignal

        with self._connection() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT payload FROM speaker_signals WHERE region = :region "
                        "ORDER BY json_extract(payload, '$.event_date') DESC, created_at DESC"
                    ),
                    {"region": region},
                )
                .mappings()
                .all()
            )
        return [SpeakerSignal.model_validate(self._load(row["payload"])) for row in rows]

    def get_speaker_signal(self, signal_id: str) -> Any | None:
        from investment_steward_core.domain import SpeakerSignal

        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT payload FROM speaker_signals WHERE signal_id = :signal_id"),
                    {"signal_id": signal_id},
                )
                .mappings()
                .first()
            )
        return None if row is None else SpeakerSignal.model_validate(self._load(row["payload"]))

    # ---- 宏观研究证据（v21 P0-02）：仅本机，重启不丢 ----

    def upsert_macro_research_evidence(self, item: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO macro_research_evidence (evidence_id, event_id, payload, created_at, updated_at) "
                    "VALUES (:evidence_id, :event_id, :payload, :created_at, :updated_at) "
                    "ON CONFLICT(evidence_id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at"
                ),
                {
                    "evidence_id": str(item["id"]),
                    "event_id": str(item["event_id"]),
                    "payload": self._json(item),
                    "created_at": str(item["created_at"]),
                    "updated_at": str(item["updated_at"]),
                },
            )

    def list_macro_research_evidence(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT payload FROM macro_research_evidence "
                        "ORDER BY created_at ASC, evidence_id ASC"
                    )
                )
                .mappings()
                .all()
            )
        return [self._load(row["payload"]) for row in rows]

    # ---- AI 研究产出（v22 用户追加）：方向研判 / 个股研报持久化，仅本机 ----

    def insert_ai_research_report(self, item: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO ai_research_reports (report_id, kind, subject, payload, created_at, updated_at) "
                    "VALUES (:report_id, :kind, :subject, :payload, :created_at, :updated_at) "
                    "ON CONFLICT(report_id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at"
                ),
                {
                    "report_id": str(item["report_id"]),
                    "kind": str(item["kind"]),
                    "subject": str(item["subject"]),
                    "payload": self._json(item),
                    "created_at": str(item["generated_at"]),
                    "updated_at": str(item["generated_at"]),
                },
            )

    def list_ai_research_reports(self, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """按生成时间倒序列出研究产出；kind 为空返回全部两类。"""
        query = "SELECT payload FROM ai_research_reports"
        params: dict[str, Any] = {"limit": int(limit)}
        if kind:
            query += " WHERE kind = :kind"
            params["kind"] = str(kind)
        query += " ORDER BY created_at DESC, report_id ASC LIMIT :limit"
        with self._connection() as connection:
            rows = connection.execute(text(query), params).mappings().all()
        return [self._load(row["payload"]) for row in rows]

    def get_ai_research_report(self, report_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT payload FROM ai_research_reports WHERE report_id = :report_id"),
                    {"report_id": str(report_id)},
                )
                .mappings()
                .first()
            )
        return None if row is None else self._load(row["payload"])

    def get_ai_research_reports_by_ids(self, report_ids: list[str]) -> dict[str, dict[str, Any]]:
        """G02：按 id 批量取落库研报 payload（命中率分组的「模型方案 / 主题」取自来源研报）。

        `model` 与 `title` 都在 payload JSON 里（表只索引 report_id/kind/subject），所以按 id 取整份
        payload 再解析。空列表直接返回，避免生成 `IN ()`；缺失的 id 表示报告已被删除，
        调用方按「来源报告不可用」如实降级，**不猜**模型方案。
        """
        ids = [str(item) for item in report_ids if str(item)]
        if not ids:
            return {}
        with self._connection() as connection:
            rows = connection.execute(
                text("SELECT report_id, payload FROM ai_research_reports WHERE report_id IN :ids")
                .bindparams(bindparam("ids", expanding=True)),
                {"ids": ids},
            ).mappings().all()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            payload = self._load(row["payload"])
            if isinstance(payload, dict):
                result[str(row["report_id"])] = payload
        return result

    def delete_ai_research_report(self, report_id: str) -> bool:
        with self._connection() as connection:
            result = connection.execute(
                text("DELETE FROM ai_research_reports WHERE report_id = :report_id"),
                {"report_id": str(report_id)},
            )
        return bool(result.rowcount)

    # B01（桌面端升级路线图 2026-09-18）：档案列表服务端筛选 + 轻量投影 + 真值计数。
    # 表仅有 report_id/kind/subject/payload/created_at/updated_at 列，model/is_draft/title/topic
    # 存于 payload JSON，故用 json_extract（仓库先例见本文件宏观证据排序处）；不迁移表结构、不改写路径。
    # created_at 即写入时的 generated_at（ISO 文本），时间范围过滤按字符串比较。

    @staticmethod
    def _ai_report_model_expr() -> str:
        return "json_extract(payload, '$.model')"

    def _ai_report_where(self, *, kind: str | None, collab: bool | None, model: str | None,
                         q: str | None, is_draft: bool | None, depth: str | None,
                         generated_from: str | None, generated_to: str | None) -> tuple[str, dict[str, Any]]:
        model_expr = self._ai_report_model_expr()
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if kind:
            clauses.append("kind = :kind")
            params["kind"] = str(kind)
        if collab is True:
            clauses.append(f"{model_expr} LIKE :collab_pat")
            params["collab_pat"] = "%·协同流水线"
        elif collab is False:
            clauses.append(f"({model_expr} IS NULL OR {model_expr} NOT LIKE :collab_pat)")
            params["collab_pat"] = "%·协同流水线"
        if model:
            clauses.append(f"{model_expr} = :model")
            params["model"] = str(model)
        if q:
            clauses.append(
                "(subject LIKE :qpat OR json_extract(payload, '$.title') LIKE :qpat "
                "OR json_extract(payload, '$.topic') LIKE :qpat "
                f"OR {model_expr} LIKE :qpat)"
            )
            params["qpat"] = f"%{q}%"
        if is_draft is True:
            # is_draft 为 JSON true/false；缺省（旧数据未写该键）视为非草稿，与前端 `is_draft === true` 口径一致。
            clauses.append("json_extract(payload, '$.is_draft') = 1")
        elif is_draft is False:
            clauses.append("json_extract(payload, '$.is_draft') IS NOT 1")
        if depth == "below_min":
            # J06（桌面端升级路线图 2026-09-18）：「未达档位深度」一档——正文低于所选档位字数下限。
            # 无 report_mode_budget 的档案（旧数据/方向研判无该字段）不命中，不误伤。
            clauses.append(
                "json_extract(payload, '$.report_chars') IS NOT NULL "
                "AND json_extract(payload, '$.report_mode_budget.min') IS NOT NULL "
                "AND json_extract(payload, '$.report_chars') < json_extract(payload, '$.report_mode_budget.min')"
            )
        if generated_from:
            clauses.append("created_at >= :generated_from")
            params["generated_from"] = str(generated_from)
        if generated_to:
            clauses.append("created_at <= :generated_to")
            params["generated_to"] = str(generated_to)
        return (" AND ".join(clauses) if clauses else "1 = 1"), params

    def list_ai_research_reports_page(
        self,
        *,
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
    ) -> tuple[list[dict[str, Any]], int]:
        """B01：服务端筛选分页，返回 (轻量投影一页, 过滤后总数)。

        投影只含列表页所需字段（不带 payload，首屏传输量随此下降），回看全文走
        get_ai_research_report。排序与旧 list_ai_research_reports 一致：created_at 倒序。"""
        where, params = self._ai_report_where(
            kind=kind, collab=collab, model=model, q=q, is_draft=is_draft, depth=depth,
            generated_from=generated_from, generated_to=generated_to,
        )
        model_expr = self._ai_report_model_expr()
        projection = (
            "report_id, kind, subject, created_at AS generated_at, "
            f"{model_expr} AS model, json_extract(payload, '$.is_draft') AS is_draft, "
            "json_extract(payload, '$.title') AS title, json_extract(payload, '$.topic') AS topic, "
            "json_extract(payload, '$.symbol') AS symbol"
        )
        with self._connection() as connection:
            total = int(
                connection.execute(
                    text(f"SELECT COUNT(*) FROM ai_research_reports WHERE {where}"), params
                ).scalar_one()
            )
            rows = (
                connection.execute(
                    text(
                        f"SELECT {projection} FROM ai_research_reports WHERE {where} "
                        "ORDER BY created_at DESC, report_id ASC LIMIT :limit OFFSET :offset"
                    ),
                    {**params, "limit": max(1, int(limit)), "offset": max(0, int(offset))},
                )
                .mappings()
                .all()
            )
        items = [
            {
                "report_id": row["report_id"],
                "kind": row["kind"],
                "subject": row["subject"],
                "generated_at": row["generated_at"],
                "model": row["model"],
                # SQLite JSON true/false → 1/0；转回 bool|null，前端 `is_draft === true` 才不破。
                "is_draft": bool(row["is_draft"]) if row["is_draft"] is not None else None,
                "title": row["title"],
                "topic": row["topic"],
                "symbol": row["symbol"],
            }
            for row in rows
        ]
        return items, total

    def ai_research_report_facets(self) -> dict[str, Any]:
        """B01：全局页签/质量计数与模型清单（不受筛选影响），前端页签计数由此取真值。

        draft_stock 与前端旧 draftCount 同口径：只数 kind=stock 且 is_draft=true 的草稿。"""
        model_expr = self._ai_report_model_expr()
        with self._connection() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT COUNT(*) AS total, "
                        "COALESCE(SUM(CASE WHEN kind = 'direction' THEN 1 ELSE 0 END), 0) AS direction, "
                        f"COALESCE(SUM(CASE WHEN kind = 'stock' AND ({model_expr} IS NULL OR {model_expr} NOT LIKE :collab_pat) THEN 1 ELSE 0 END), 0) AS stock, "
                        f"COALESCE(SUM(CASE WHEN kind = 'stock' AND {model_expr} LIKE :collab_pat THEN 1 ELSE 0 END), 0) AS collab, "
                        "COALESCE(SUM(CASE WHEN kind = 'stock' AND json_extract(payload, '$.is_draft') = 1 THEN 1 ELSE 0 END), 0) AS draft_stock "
                        "FROM ai_research_reports"
                    ),
                    {"collab_pat": "%·协同流水线"},
                )
                .mappings()
                .one()
            )
            model_rows = (
                connection.execute(
                    text(
                        f"SELECT DISTINCT {model_expr} FROM ai_research_reports "
                        f"WHERE {model_expr} IS NOT NULL AND {model_expr} != '' ORDER BY 1"
                    )
                )
                .scalars()
                .all()
            )
        return {
            "total": int(row["total"]),
            "direction": int(row["direction"]),
            "stock": int(row["stock"]),
            "collab": int(row["collab"]),
            "draft_stock": int(row["draft_stock"]),
            "models": [str(value) for value in model_rows],
        }

    # ---- 研报追问分析附录（v28，2026-09-13 方案 §三）：append-only，原报告不可变 ----

    def insert_ai_analysis_turn(self, item: dict[str, Any]) -> None:
        """追加一条分析附录；同 turn_id 重复插入按更新处理（幂等），但正常流程 turn_id 每次新生成。"""
        now = str(item["created_at"])
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO ai_analysis_turns (turn_id, parent_report_id, payload, created_at, updated_at) "
                    "VALUES (:turn_id, :parent_report_id, :payload, :created_at, :updated_at) "
                    "ON CONFLICT(turn_id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at"
                ),
                {
                    "turn_id": str(item["analysis_turn_id"]),
                    "parent_report_id": str(item["parent_report_id"]),
                    "payload": self._json(item),
                    "created_at": now,
                    "updated_at": now,
                },
            )

    # ---- D01（桌面端升级路线图 2026-09-18）：市场扫描任务化 ----

    def create_scan_job(self, job_id: str, fingerprint: str, request_body: dict[str, Any]) -> None:
        """登记一个 running 状态的扫描任务；顺带把已结束任务裁剪到最近 20 个（结果集大）。"""
        now = datetime.now(UTC).isoformat()
        with self._connection() as connection:
            connection.execute(
                text(
                    "INSERT INTO scan_jobs (job_id, fingerprint, state, done, total, request_body, created_at, updated_at) "
                    "VALUES (:job_id, :fingerprint, 'running', 0, 0, :request_body, :now, :now)"
                ),
                {"job_id": job_id, "fingerprint": fingerprint, "request_body": self._json(request_body), "now": now},
            )
            connection.execute(
                text(
                    "DELETE FROM scan_jobs WHERE state != 'running' AND job_id NOT IN "
                    "(SELECT job_id FROM scan_jobs WHERE state != 'running' ORDER BY updated_at DESC LIMIT 20)"
                )
            )

    def find_running_scan_job(self, fingerprint: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT job_id, done, total FROM scan_jobs "
                        "WHERE fingerprint = :fingerprint AND state = 'running' ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"fingerprint": fingerprint},
                )
                .mappings()
                .first()
            )
        return None if row is None else dict(row)

    def get_scan_job(self, job_id: str) -> dict[str, Any] | None:
        """读任务；running 且 30 分钟无心跳按 error（任务中断）呈现，不假装还在跑。"""
        with self._connection() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT job_id, fingerprint, state, done, total, summary, error, created_at, updated_at "
                        "FROM scan_jobs WHERE job_id = :job_id"
                    ),
                    {"job_id": job_id},
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        job = dict(row)
        if isinstance(job.get("summary"), str) and job["summary"]:
            job["summary"] = self._load(job["summary"])
        if job["state"] == "running":
            updated = datetime.fromisoformat(str(job["updated_at"]))
            if datetime.now(UTC) - updated > timedelta(minutes=30):
                job["state"] = "error"
                job["error"] = "任务中断（Core 重启或心跳超时）——请重新发起扫描"
        return job

    def get_scan_job_state(self, job_id: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                text("SELECT state FROM scan_jobs WHERE job_id = :job_id"), {"job_id": job_id}
            ).first()
        return None if row is None else str(row[0])

    def update_scan_job_progress(self, job_id: str, done: int, total: int) -> None:
        with self._connection() as connection:
            connection.execute(
                text("UPDATE scan_jobs SET done = :done, total = :total, updated_at = :now WHERE job_id = :job_id"),
                {"done": int(done), "total": int(total), "now": datetime.now(UTC).isoformat(), "job_id": job_id},
            )

    def cancel_scan_job(self, job_id: str) -> bool:
        """标记取消；仅 running 可取消。执行线程逐票检测到标记后停止发新取数。"""
        with self._connection() as connection:
            result = connection.execute(
                text("UPDATE scan_jobs SET state = 'cancelled', updated_at = :now WHERE job_id = :job_id AND state = 'running'"),
                {"now": datetime.now(UTC).isoformat(), "job_id": job_id},
            )
        return result.rowcount > 0

    def finish_scan_job(self, job_id: str, state: str, summary: dict[str, Any] | None) -> None:
        with self._connection() as connection:
            connection.execute(
                text(
                    "UPDATE scan_jobs SET state = :state, summary = :summary, updated_at = :now "
                    "WHERE job_id = :job_id AND state = 'running'"
                ),
                {
                    "state": state,
                    "summary": self._json(summary) if summary is not None else None,
                    "now": datetime.now(UTC).isoformat(),
                    "job_id": job_id,
                },
            )

    # E02（桌面端升级路线图 2026-09-18）：/export/all 导出键 → 数据库表名（模型键与 raw 表键并存）。
    # 与 EXPORT_PAYLOAD_TABLES 合起来构成可恢复白名单；credentials/plugin_*/sync_*/audit_events/
    # macro_cache 等设备态/秘密/派生缓存同样**不可导入**（与导出政策对称）。
    IMPORT_TABLE_MAP: dict[str, str] = {
        **{name: name for name in EXPORT_PAYLOAD_TABLES},
        "policies": "investment_policies",
        "holdings": "holdings",
        "theses": "thesis",
        "plans": "plans",
        "decisions": "decisions",
        "evidence": "evidence",
        "research_runs": "research_runs",
        "learning_activities": "learning_activities",
        "notifications": "notifications",
    }

    def _table_columns(self, table: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(text(f"PRAGMA table_info({table})")).mappings().all()
        return [dict(row) for row in rows]

    def _import_table_whitelist_check(self, table: str) -> None:
        if table not in self.IMPORT_TABLE_MAP.values():
            raise ValueError(f"table not importable (设备态/秘密/未登记): {table}")

    def import_table_preview(self, table: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """E02 preview：统计现有行数、incoming 行数与主键冲突数（不改任何数据）。"""
        self._import_table_whitelist_check(table)
        columns = self._table_columns(table)
        if not columns:
            return {"table": table, "status": "missing", "incoming": len(rows), "existing": 0, "conflicts": 0}
        pk_columns = [col["name"] for col in columns if col["pk"]]
        with self._connection() as connection:
            existing = int(connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
            conflicts = 0
            if pk_columns:
                placeholders = ", ".join(f":pk{i}" for i in range(len(pk_columns)))
                condition = " AND ".join(f"{col} = :pk{i}" for i, col in enumerate(pk_columns))
                for row in rows:
                    params = {f"pk{i}": row.get(col) for i, col in enumerate(pk_columns)}
                    if any(value is None for value in params.values()):
                        continue
                    hits = connection.execute(
                        text(f"SELECT COUNT(*) FROM {table} WHERE {condition}"), params
                    ).scalar_one()
                    conflicts += int(hits)
        return {"table": table, "status": "ok", "incoming": len(rows), "existing": existing, "conflicts": conflicts}

    def import_table_apply(self, table: str, rows: list[dict[str, Any]], mode: str) -> dict[str, Any]:
        """E02 apply：按表恢复。

        - mode="replace"：清空该表后原样写回导出行（该表回到导出时点）；
        - mode="merge"：INSERT OR IGNORE——主键已存在的行保留现状，只补新行，不做无提示覆盖。
        行构造：导出行有该列的用原值（raw 行的 payload 为原 JSON 串，无损失）；模型行缺
        payload 列时以整行 dict 序列化作为 payload（模型 dump 即落库 payload 内容）。
        恢复前必须先 rotate_backup（由端点保证），本方法不再负责。
        """
        self._import_table_whitelist_check(table)
        if mode not in ("replace", "merge"):
            raise ValueError(f"unknown import mode: {mode}")
        columns = self._table_columns(table)
        if not columns:
            return {"table": table, "status": "missing", "inserted": 0, "skipped": 0}
        column_names = [col["name"] for col in columns]
        notnull = {col["name"] for col in columns if col["notnull"] and col["dflt_value"] is None and not col["pk"]}
        has_payload = "payload" in column_names
        inserted = 0
        skipped = 0
        with self._connection() as connection:
            if mode == "replace":
                connection.execute(text(f"DELETE FROM {table}"))
            for row in rows:
                params: dict[str, Any] = {}
                missing_required: list[str] = []
                for name in column_names:
                    if name in row and row[name] is not None:
                        params[name] = row[name]
                    elif name == "payload" and has_payload:
                        params[name] = self._json(row)
                    elif name in notnull:
                        missing_required.append(name)
                        params[name] = None
                    else:
                        params[name] = None
                if missing_required:
                    skipped += 1
                    continue
                statement = f"INSERT {'OR IGNORE ' if mode == 'merge' else ''}INTO {table} ({', '.join(column_names)}) VALUES ({', '.join(':' + name for name in column_names)})"
                result = connection.execute(text(statement), params)
                inserted += int(result.rowcount > 0)
                if result.rowcount == 0:
                    skipped += 1
        return {"table": table, "status": "ok", "inserted": inserted, "skipped": skipped}

    def maybe_daily_backup(self) -> None:
        """E05（桌面端升级路线图 2026-09-18）：每自然日首次写入时触发一次在线备份（节流）。

        判断依据：backups 目录内是否已有今日时间戳的备份文件；检查结果按自然日在内存
        缓存（每进程每日最多一次目录扫描）。失败静默，不阻断写入。
        """
        today = datetime.now(UTC).date()
        if self._daily_backup_check_date == today:
            return
        self._daily_backup_check_date = today
        try:
            backup_dir = self.path.parent / "backups"
            prefix = f"steward-{today.strftime('%Y%m%d')}"
            if any(backup_dir.glob(f"{prefix}*.sqlite3")):
                return
            rotate_backup(self.path)
        except Exception:
            pass

    def record_model_call(
        self,
        *,
        profile_id: str | None,
        model: str,
        purpose: str | None,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        latency_ms: int,
        retried: bool,
        outcome: str,
    ) -> None:
        """F01：落一条模型调用记录（usage 缺失的 token 记 null，不按字数反推）。失败静默。"""
        try:
            from uuid import uuid4

            with self._connection() as connection:
                connection.execute(
                    text(
                        "INSERT INTO model_calls (call_id, profile_id, model, purpose, prompt_tokens, completion_tokens, total_tokens, latency_ms, retried, outcome, created_at) "
                        "VALUES (:call_id, :profile_id, :model, :purpose, :prompt_tokens, :completion_tokens, :total_tokens, :latency_ms, :retried, :outcome, :created_at)"
                    ),
                    {
                        "call_id": str(uuid4()),
                        # profile_id 可能以 UUID 对象传入（模型方案的 profile_id 字段），归一为字符串。
                        "profile_id": str(profile_id) if profile_id is not None else None,
                        "model": model,
                        "purpose": purpose,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": total_tokens,
                        "latency_ms": int(latency_ms),
                        "retried": 1 if retried else 0,
                        "outcome": outcome,
                        "created_at": datetime.now(UTC).isoformat(),
                    },
                )
        except Exception:
            pass

    def summarize_model_usage(self, window_days: int) -> list[dict[str, Any]]:
        """F01：按方案聚合窗口内的调用次数/结果/token 三项/耗时（since 由端点换算传入）。"""
        since = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
        with self._connection() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT profile_id, model, "
                        "COUNT(*) AS calls, "
                        "SUM(CASE WHEN outcome = 'ok' THEN 1 ELSE 0 END) AS ok_calls, "
                        "SUM(CASE WHEN outcome = 'timeout' THEN 1 ELSE 0 END) AS timeouts, "
                        "SUM(CASE WHEN outcome = 'error' THEN 1 ELSE 0 END) AS errors, "
                        "SUM(retried) AS retried_calls, "
                        "SUM(COALESCE(prompt_tokens, 0)) AS prompt_tokens, "
                        "SUM(COALESCE(completion_tokens, 0)) AS completion_tokens, "
                        "SUM(COALESCE(total_tokens, 0)) AS total_tokens, "
                        "SUM(CASE WHEN prompt_tokens IS NULL THEN 1 ELSE 0 END) AS usage_missing, "
                        "AVG(latency_ms) AS avg_latency_ms, MAX(latency_ms) AS max_latency_ms "
                        "FROM model_calls WHERE created_at >= :since "
                        "GROUP BY profile_id, model ORDER BY total_tokens DESC"
                    ),
                    {"since": since},
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    # —— F02/F03：三方取数尝试日志 ——
    def record_feed_attempt(
        self,
        *,
        source: str,
        endpoint: str | None,
        params_fingerprint: str | None,
        started_at: str,
        latency_ms: int,
        result: str,
        error_kind: str | None,
        attempt_index: int = 1,
    ) -> None:
        """F02：落一条取数尝试记录（成功与失败都落）。失败静默——埋点绝不能影响取数。"""
        try:
            from uuid import uuid4

            with self._connection() as connection:
                connection.execute(
                    text(
                        "INSERT INTO feed_attempts (attempt_id, source, endpoint, params_fingerprint, "
                        "started_at, latency_ms, result, error_kind, attempt_index) "
                        "VALUES (:attempt_id, :source, :endpoint, :params_fingerprint, :started_at, "
                        ":latency_ms, :result, :error_kind, :attempt_index)"
                    ),
                    {
                        "attempt_id": str(uuid4()),
                        "source": str(source),
                        "endpoint": endpoint,
                        "params_fingerprint": params_fingerprint,
                        "started_at": started_at,
                        "latency_ms": int(latency_ms),
                        "result": str(result),
                        "error_kind": error_kind,
                        "attempt_index": int(attempt_index),
                    },
                )
        except Exception:
            pass

    def list_feed_attempts(self, window_days: int, limit: int = 20000) -> list[dict[str, Any]]:
        """F02：窗口内的尝试行（按来源聚合的原料）。limit 是**如实登记的上限**：超出部分
        不计入统计，而不是静默截断成"看起来完整的样本"。"""
        since = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
        with self._connection() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT source, endpoint, params_fingerprint, result, error_kind, latency_ms, started_at, attempt_index "
                        "FROM feed_attempts WHERE started_at >= :since ORDER BY started_at DESC LIMIT :limit"
                    ),
                    {"since": since, "limit": int(limit)},
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    def list_feed_attempts_totals(self, window_days: int) -> dict[str, int]:
        """F02：窗口内尝试总数与截断用量（用于判断 list_feed_attempts 是否被 limit 截断）。"""
        since = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
        with self._connection() as connection:
            row = (
                connection.execute(
                    text("SELECT COUNT(*) AS total FROM feed_attempts WHERE started_at >= :since"),
                    {"since": since},
                )
                .mappings()
                .one()
            )
        return {"total": int(row["total"] or 0)}

    def list_feed_failures(self, window_days: int, limit: int = 50) -> list[dict[str, Any]]:
        """F03：窗口内最近 N 条失败尝试（「哪个源、第几次、失败原因」的原始事实）。"""
        since = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
        with self._connection() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT source, endpoint, params_fingerprint, error_kind, latency_ms, started_at, attempt_index "
                        "FROM feed_attempts WHERE started_at >= :since AND result != 'ok' "
                        "ORDER BY started_at DESC LIMIT :limit"
                    ),
                    {"since": since, "limit": int(limit)},
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    _EVIDENCE_SOURCE_SUMMARY_TTL = 60.0

    def evidence_source_summary(self, tenant_id: UUID) -> list[dict[str, Any]]:
        """F02：证据的按来源/类型聚合（单次 SQL，避免 `list_evidence` 整表逐行 JSON 解析）。

        60 秒进程内缓存（**按实例**持有，不跨库共享——多个临时库同租户 id 时不会串味）：
        此调用每次打开数据源质量页都会跑，而证据是缓慢增长的存量表。
        返回 (source_name, evidence_type, producer_plugin_id, count, first_at, last_at)。
        """
        cache_key = str(tenant_id)
        cached = self._evidence_source_summary_cache.get(cache_key)
        now_monotonic = time.monotonic()
        if cached and now_monotonic - cached[0] < self._EVIDENCE_SOURCE_SUMMARY_TTL:
            return cached[1]
        with self._connection() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT json_extract(payload, '$.source_name') AS source_name, "
                        "json_extract(payload, '$.evidence_type') AS evidence_type, "
                        "json_extract(payload, '$.producer_plugin_id') AS producer_plugin_id, "
                        "COUNT(*) AS count, MIN(created_at) AS first_at, MAX(created_at) AS last_at "
                        "FROM evidence WHERE tenant_id = :tenant_id GROUP BY source_name, evidence_type, producer_plugin_id"
                    ),
                    {"tenant_id": str(tenant_id)},
                )
                .mappings()
                .all()
            )
        summary = [dict(row) for row in rows]
        self._evidence_source_summary_cache[cache_key] = (now_monotonic, summary)
        return summary

    def list_failed_feed_sources(self, window_days: int) -> list[str]:
        """F03：窗口内**失败过**的来源（去重，「仅重试失败项」的输入）。"""
        since = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
        with self._connection() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT DISTINCT source FROM feed_attempts "
                        "WHERE started_at >= :since AND result != 'ok' ORDER BY source"
                    ),
                    {"since": since},
                )
                .scalars()
                .all()
            )
        return [str(name) for name in rows]

    def export_payload_tables(self, tables: tuple[str, ...] = EXPORT_PAYLOAD_TABLES) -> dict[str, list[dict[str, Any]]]:
        """E01（桌面端升级路线图 2026-09-18）：全量导出的逐表 raw 读取（SELECT *，无损失）。

        只服务 /export/all：与按模型校验的 list_* 方法不同，这里原样返回落库行（含 created_at
        等元数据列），保证「导出文件里能查到研报与追问附录正文」可逐字段核对。表名走白名单
        硬校验（防拼接注入）；quant_artifacts 等表无 payload 列，SELECT * 同样适用——制品
        **二进制本体**在制品目录（按 content_hash 落文件），导出的是索引行，如实说明。
        """
        out: dict[str, list[dict[str, Any]]] = {}
        with self._connection() as connection:
            for table in tables:
                if table not in EXPORT_PAYLOAD_TABLES:
                    raise ValueError(f"table not in export whitelist: {table}")
                rows = connection.execute(text(f"SELECT * FROM {table}")).mappings().all()
                out[table] = [dict(row) for row in rows]
        return out

    def list_ai_analysis_turns(self, parent_report_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """按生成时间正序列出某报告的全部追问附录（附录序号由调用方按顺序编号）。"""
        with self._connection() as connection:
            rows = (
                connection.execute(
                    text(
                        "SELECT payload FROM ai_analysis_turns WHERE parent_report_id = :parent_report_id "
                        "ORDER BY created_at ASC, turn_id ASC LIMIT :limit"
                    ),
                    {"parent_report_id": str(parent_report_id), "limit": max(1, int(limit))},
                )
                .mappings()
                .all()
            )
        return [self._load(row["payload"]) for row in rows]

    def delete_ai_analysis_turns(self, parent_report_id: str) -> int:
        """删除某报告的全部追问附录（随报告删除联动，仅本机数据）。"""
        with self._connection() as connection:
            result = connection.execute(
                text("DELETE FROM ai_analysis_turns WHERE parent_report_id = :parent_report_id"),
                {"parent_report_id": str(parent_report_id)},
            )
        return int(result.rowcount)

    def _list_payload(self, table: str, _: str, user_id: UUID, model: type[Any]) -> list[Any]:
        with self._connection() as connection:
            rows = (
                connection.execute(
                    text(f"SELECT payload FROM {table} WHERE user_id = :user_id ORDER BY updated_at DESC"),
                    {"user_id": str(user_id)},
                )
                .mappings()
                .all()
            )
        return [model.model_validate(self._load(row["payload"])) for row in rows]

    def _get_payload(
        self, table: str, key_col: str, key: UUID, user_id: UUID, model: type[Any]
    ) -> Any | None:
        with self._connection() as connection:
            row = (
                connection.execute(
                    text(f"SELECT payload FROM {table} WHERE {key_col} = :key AND user_id = :user_id"),
                    {"key": str(key), "user_id": str(user_id)},
                )
                .mappings()
                .first()
            )
        return None if row is None else model.model_validate(self._load(row["payload"]))

    def _upsert_payload(self, table: str, key_col: str, key: UUID, model: Any) -> None:
        payload = model.model_dump(mode="json")
        with self._connection() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {table} ({key_col}, user_id, payload, updated_at) VALUES (:key, :user_id, :payload, :updated_at) "
                    f"ON CONFLICT({key_col}) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at"
                ),
                {
                    "key": str(key),
                    "user_id": str(model.user_id),
                    "payload": self._json(payload),
                    "updated_at": model.updated_at.isoformat(),
                },
            )


def _parse_backup_stamp(name: str):
    """E05：从备份文件名解析时间戳（steward-YYYYMMDD-HHMMSS[-N].sqlite3）；解析不出返回 None。"""
    import re as _re
    match = _re.match(r"^steward-(\d{8})-(\d{6})(?:-\d+)?\.sqlite3$", name)
    if not match:
        return None
    from datetime import datetime as _datetime
    try:
        return _datetime.strptime(f"{match.group(1)}{match.group(2)}", "%Y%m%d%H%M%S")
    except ValueError:
        return None


def rotate_backup(db_path, keep: int = 7):
    """SQLite 在线备份到同目录 backups/（H1-1 起步；E05 补齐触发点校验与按时间跨度保留）。

    - 备份前校验源库可读（PRAGMA quick_check，非 ok 视为源库损坏，拒绝在坏库上盖备份）；
    - 文件名同秒撞名时追加序号（E03：恢复流程先 rotate 再还原，不能覆盖被恢复的那份）；
    - 保留策略（E05）：最近 7 天的日份全保留 + 最近 4 个自然周每周最新一份 + 至少 keep 份最新；
    - 失败静默返回 None，不影响启动/写入。
    """
    import sqlite3
    import time as _time
    try:
        backup_dir = Path(db_path).parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        # E03：秒级时间戳在同一秒内可能撞名——恢复流程「先 rotate 再还原」时会把
        # 被恢复的那份备份覆盖掉。目标已存在时追加序号，保证不覆盖任何既有备份。
        stamp = _time.strftime("%Y%m%d-%H%M%S")
        target = backup_dir / f"steward-{stamp}.sqlite3"
        counter = 2
        while target.exists():
            target = backup_dir / f"steward-{stamp}-{counter}.sqlite3"
            counter += 1
        src = sqlite3.connect(str(db_path))
        # E05：备份前校验源库可读——损坏库不做备份覆盖（保留最后一次好备份）。
        check = src.execute("PRAGMA quick_check").fetchone()
        if not check or str(check[0]) != "ok":
            src.close()
            return None
        dst = sqlite3.connect(str(target))
        src.backup(dst)
        dst.close()
        src.close()
        # E05：按时间跨度保留（替代旧的「只留最近 keep 份」）。
        files = sorted(backup_dir.glob("steward-*.sqlite3"))
        now = datetime.now()
        keep_names: set[str] = set()
        weekly: dict[tuple[int, int], str] = {}
        for path in files:
            stamp_dt = _parse_backup_stamp(path.name)
            if stamp_dt is None:
                keep_names.add(path.name)  # 无法解析时间的文件不删（宁可多留）
                continue
            age_days = (now - stamp_dt).days
            if age_days <= 7:
                keep_names.add(path.name)
            # 周份只从「最近 28 天」里挑每周最新——远古备份不能因为周数桶少而永远留在盘上。
            if age_days <= 28:
                week_key = (stamp_dt.isocalendar().year, stamp_dt.isocalendar().week)
                current = weekly.get(week_key)
                if current is None or path.name > current:
                    weekly[week_key] = path.name
        for week_key in sorted(weekly.keys(), reverse=True)[:4]:
            keep_names.add(weekly[week_key])
        for path in files[-keep:]:
            keep_names.add(path.name)
        for path in files:
            if path.name not in keep_names:
                path.unlink(missing_ok=True)
        return target
    except Exception:
        return None

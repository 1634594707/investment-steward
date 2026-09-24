"""阶段 4 客户端：RelayClient（HTTP 层）+ 加密信封 + 同步引擎。

硬约束（路线图 §4,勿回退）：
- HTTP 层必须带自定义 User-Agent `InvestmentSteward-Core/<版本>` —— Cloudflare Bot 防护会 403
  Python 默认 UA（error 1010,2026-09-09 实测）;
- httpx trust_env=False：不走本机/环境代理,直连;
- payload 一律 Fernet 加密信封（AES-CBC+HMAC）,同步密钥只在本机凭据库,服务器永不接触;
- 白名单只同步:观察事项 / 计划状态 / 通知 / 研究摘要。完整证据、原始资料、投资日志、
  研究上下文默认仅本机,Today 摘要由 digest 通知覆盖 → build_events 如实标注,不虚构。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken

from . import __version__

USER_AGENT = f"InvestmentSteward-Core/{__version__}"
SYNC_KEY_CREDENTIAL = "relay_sync_key"
TOKEN_CREDENTIAL = "relay_device_token"


# ———————— 加密信封 ————————

def generate_sync_key() -> str:
    """生成同步密钥（Fernet key,base64）。只在设备首配时调用一次,存本机凭据库。"""
    return Fernet.generate_key().decode("ascii")


def seal_payload(sync_key: str, payload: dict[str, Any]) -> str:
    """明文事件 → 密文字符串（服务器不可读）。"""
    return Fernet(sync_key.encode("ascii")).encrypt(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def open_payload(sync_key: str, ciphertext: str) -> dict[str, Any]:
    """密文 → 明文事件。密钥不符/被篡改抛 ValueError(不静默)。"""
    try:
        raw = Fernet(sync_key.encode("ascii")).decrypt(ciphertext.encode("ascii"))
    except (InvalidToken, ValueError) as error:
        raise ValueError("信封解密失败:同步密钥不符或密文被篡改") from error
    return json.loads(raw.decode("utf-8"))


# ———————— HTTP 客户端 ————————

@dataclass(frozen=True)
class RelayConfig:
    base_url: str
    user_token: str
    device_id: str
    timeout_seconds: float = 15.0


class RelayClient:
    """中转服务客户端。所有请求:自定义 UA + trust_env=False + 显式超时。"""

    def __init__(self, config: RelayConfig) -> None:
        self._config = config

    def _headers(self) -> dict[str, str]:
        return {"X-Relay-Token": self._config.user_token, "User-Agent": USER_AGENT}

    def health(self) -> dict[str, Any]:
        with httpx.Client(trust_env=False, timeout=self._config.timeout_seconds) as client:
            resp = client.get(f"{self._config.base_url}/health", headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    def upload_events(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        with httpx.Client(trust_env=False, timeout=self._config.timeout_seconds) as client:
            resp = client.post(f"{self._config.base_url}/sync/events",
                               headers={**self._headers(), "Content-Type": "application/json"},
                               json={"device_id": self._config.device_id, "events": events})
            resp.raise_for_status()
            return resp.json()

    def pull_events(self, cursor: int, limit: int = 200) -> dict[str, Any]:
        with httpx.Client(trust_env=False, timeout=self._config.timeout_seconds) as client:
            resp = client.get(f"{self._config.base_url}/sync/events",
                              headers=self._headers(), params={"cursor": cursor, "limit": limit})
            resp.raise_for_status()
            return resp.json()

    def list_devices(self) -> list[dict[str, Any]]:
        with httpx.Client(trust_env=False, timeout=self._config.timeout_seconds) as client:
            resp = client.get(f"{self._config.base_url}/devices", headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    def create_pairing_code(self, sync_key: str, *, ttl_seconds: int = 600) -> dict[str, Any]:
        """生成手机配对码:服务器暂存 master token+sync_key,TTL 内一次性领取。

        B04：此处原有第二份同名实现（类内后定义覆盖前定义，ruff F811 一直在报），
        两份逻辑等价，已合并为一份。
        """
        with httpx.Client(trust_env=False, timeout=self._config.timeout_seconds) as client:
            resp = client.post(
                f"{self._config.base_url}/pairing/codes",
                headers={**self._headers(), "Content-Type": "application/json"},
                json={"user_token": self._config.user_token, "sync_key": sync_key,
                      "ttl_seconds": ttl_seconds},
            )
            resp.raise_for_status()
            return resp.json()


# ———————— 同步白名单（§4） ————————

def build_events(db: Any, user_id: Any) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """从本机事实源构建白名单事件(明文)。返回 (events, gaps)。

    幂等键规则:`<kind>:<entity_id>:<版本标记>`——版本标记取状态/更新时间,
    状态不变则同键跳过(outbox UNIQUE),状态变化产生新键=新版本。
    """
    events: list[dict[str, Any]] = []
    gaps: list[dict[str, str]] = []

    for item in db.list_watch_items(user_id):
        status = getattr(item.status, "value", str(item.status))
        updated = str(getattr(item, "updated_at", None) or getattr(item, "created_at", "") or "")
        events.append({
            "kind": "watch_item.updated",
            "idempotency_key": f"watch_item:{item.watch_id}:{status}:{updated[:19]}",
            "version": 1,
            "payload": {
                "watch_id": str(item.watch_id), "title": item.title,
                "indicator": item.indicator, "condition_text": item.condition_text,
                "check_cycle": str(item.check_cycle),
                "status": status,
            },
        })

    for plan in db.list_plans(user_id):
        status = getattr(plan.status, "value", str(plan.status))
        updated = str(getattr(plan, "updated_at", None) or getattr(plan, "created_at", "") or "")
        events.append({
            "kind": "plan.status",
            "idempotency_key": f"plan:{plan.plan_id}:{status}:{updated[:19]}",
            "version": 1,
            "payload": {"plan_id": str(plan.plan_id), "title": str(getattr(plan, "title", "")),
                        "status": status},
        })

    for n in db.list_notifications(user_id):
        events.append({
            "kind": "notification.raised",
            "idempotency_key": f"notification:{n.notification_id}",
            "version": 1,
            "payload": {"notification_id": str(n.notification_id), "title": n.title,
                        "summary": n.summary,
                        "created_at": n.created_at.isoformat() if n.created_at else None},
        })

    for run in db.list_research_runs(user_id):
        status = str(getattr(run.status, "value", run.status))
        updated = str(getattr(run, "updated_at", None) or getattr(run, "created_at", "") or "")
        events.append({
            "kind": "research.summary",
            "idempotency_key": f"research_run:{run.run_id}:{status}:{updated[:19]}",
            "version": 1,
            "payload": {"run_id": str(run.run_id), "question": str(getattr(run, "user_question", ""))[:500],
                        "status": status},
        })

    gaps.append({
        "kind": "today_summary",
        "reason": "Today 摘要未单独入白名单:其内容已由 digest 通知(notification.raised)覆盖;如需独立事件待后续工作包。",
    })
    return events, gaps


# ———————— 同步引擎 ————————

def run_sync(db: Any, credential_store: Any, config: RelayConfig, sync_key: str,
             local_user_id: Any) -> dict[str, Any]:
    """跑一轮:构建白名单 → 加密入 outbox → 上传 → 拉取解密入 inbox → 推进游标。

    local_user_id 是本机 Core 用户(白名单数据的属主);relay 的 user_id 与此无关,
    服务器侧身份只由 user_token 决定,不参与本机构建。
    """
    events, gaps = build_events(db, local_user_id)
    enqueued = 0
    for event in events:
        ciphertext = seal_payload(sync_key, event["payload"])
        if db.enqueue_sync_event(event["kind"], event["idempotency_key"], ciphertext, version=event["version"]):
            enqueued += 1

    uploaded, duplicates = 0, 0
    batch = db.list_pending_sync_events(limit=100)
    if batch:
        wire = [{"kind": e["kind"], "idempotency_key": e["idempotency_key"],
                 "version": e["version"], "payload": e["payload"]} for e in batch]
        result = RelayClient(config).upload_events(wire)
        for item in result.get("results", []):
            if item.get("duplicate"):
                duplicates += 1
            else:
                uploaded += 1
        db.mark_sync_uploaded([e["outbox_id"] for e in batch])

    cursor = int(db.get_sync_state("pull_cursor") or 0)
    pulled, decrypt_failures = 0, 0
    while True:
        page = RelayClient(config).pull_events(cursor)
        remote_events = page.get("events", [])
        for event in remote_events:
            try:
                plaintext = json.dumps(open_payload(sync_key, event["payload"]),
                                       ensure_ascii=False, separators=(",", ":"))
            except ValueError:
                decrypt_failures += 1
                continue
            if db.insert_sync_inbox(int(event["event_id"]), event["kind"],
                                    event["idempotency_key"], int(event["version"]), plaintext):
                pulled += 1
        cursor = int(page.get("cursor_max", cursor))
        db.set_sync_state("pull_cursor", str(cursor))
        if not remote_events or len(remote_events) < 200:
            break

    counts = db.sync_outbox_counts()
    return {
        "ok": True,
        "enqueued": enqueued,
        "uploaded": uploaded,
        "duplicates": duplicates,
        "pulled": pulled,
        "decrypt_failures": decrypt_failures,
        "outbox": counts,
        "pull_cursor": cursor,
        "gaps": gaps,
        "ran_at": datetime.now(UTC).isoformat(),
    }


# ———————— inbox → 业务表应用（幂等） ————————

def apply_inbox(db: Any, local_user_id: Any) -> dict[str, Any]:
    """把 inbox 中远端事件应用到本机业务表。每类口径(冻结):

    - watch_item.updated:按 dedup_key `sync:watch_item:<远端watch_id>` 找到本机镜像,
      不存在则创建(标 dedup_key 来源),存在则全量更新;重复应用无副作用;
    - notification.raised:upsert_notification + dedup_key `sync:notification:<远端id>`,
      已读状态不被重置(DB 层保证);
    - plan.status:远端 plan_id 无本机映射 → 如实 skipped(计划是重要业务对象,不自动创建;
      映射机制留给显式绑定工作包);
    - research.summary:skipped(本地事实源优先,事件留在 inbox 供只读查看);
    - mobile.note:手机端上传的研报/消息 → 落成通知(dedup_key `sync:mobile:<idem>`),
      出现在通知中心,不落研究表;
    - mobile.watch_action:手机对观察事项的操作(confirm→closed/pause→paused/resume→active,
      附备注合并进 condition_text)——只作用于按 dedup_key 镜像的同步观察事项,无镜像如实 skipped;
    - mobile.research_request:手机发起的"稍后在 Windows 继续"研究请求 → 落成通知提醒,不自动跑研究。
    """
    from datetime import datetime as _dt
    from uuid import UUID as _UUID
    from uuid import uuid4 as _uuid4

    from .domain.longterm import WatchItem, WatchStatus
    from .domain.models import ActionMode, Notification, PlanStatus

    applied_watch = updated_watch = applied_notifications = 0
    applied_mobile_notes = 0
    applied_mobile_watch = 0
    applied_mobile_research = 0
    skipped_mobile_watch: list[str] = []
    skipped_plans: list[str] = []
    skipped_research = 0
    unknown_kinds: list[str] = []
    plan_map = json.loads(db.get_sync_state("entity_map:plan") or "{}")

    # 升序重放(旧→新):最新事件状态最后生效,重放不回滚
    for event in reversed(db.list_sync_inbox(limit=500)):
        payload = json.loads(event["payload"])
        if event["kind"] == "watch_item.updated":
            remote_id = payload.get("watch_id", "")
            dedup = f"sync:watch_item:{remote_id}"
            existing = next((w for w in db.list_watch_items(local_user_id) if w.dedup_key == dedup), None)
            try:
                status = WatchStatus(payload.get("status", "active"))
            except ValueError:
                unknown_kinds.append(f"watch_item:{event['idempotency_key']}:bad status")
                continue
            if existing is None:
                try:
                    db.upsert_watch_item(WatchItem(
                        watch_id=_uuid4(), user_id=local_user_id,
                        title=str(payload.get("title", ""))[:200] or "同步观察事项",
                        indicator=str(payload.get("indicator", ""))[:400] or "未提供",
                        condition_text=str(payload.get("condition_text", ""))[:1000],
                        check_cycle=str(payload.get("check_cycle", "manual")),
                        status=status, dedup_key=dedup,
                    ))
                except Exception as error:  # 字段校验(如 check_cycle 枚举)失败 → 如实记缺口,不中断
                    unknown_kinds.append(f"watch_item:{event['idempotency_key']}:{error}")
                    continue
                applied_watch += 1
            else:
                merged = existing.model_copy(update={
                    "status": status,
                    "condition_text": str(payload.get("condition_text", existing.condition_text)),
                    "updated_at": _dt.now(UTC),
                })
                db.upsert_watch_item(merged)
                updated_watch += 1
        elif event["kind"] == "notification.raised":
            db.upsert_notification(Notification(
                notification_id=_uuid4(), user_id=local_user_id,
                instrument=str(payload.get("instrument", "")),
                triggered_by=str(payload.get("triggered_by", "")),
                condition_kind=str(payload.get("condition_kind", "observation_metric")),
                title=str(payload.get("title", "同步通知"))[:300],
                summary=str(payload.get("summary", "")),
                action_mode=ActionMode.OBSERVE,
                evidence_refs=[],
            ), dedup_key=f"sync:notification:{payload.get('notification_id', event['idempotency_key'])}")
            applied_notifications += 1
        elif event["kind"] == "mobile.note":
            title = str(payload.get("title", "")).strip() or "手机消息"
            text = str(payload.get("text", ""))
            db.upsert_notification(Notification(
                notification_id=_uuid4(), user_id=local_user_id,
                instrument="",
                triggered_by=f"手机端/{str(payload.get('device_id', ''))[:20]}",
                condition_kind="mobile_note",
                title=f"📱 {title}"[:300],
                summary=text,
                action_mode=ActionMode.OBSERVE,
                evidence_refs=[],
            ), dedup_key=f"sync:mobile:{event['idempotency_key']}")
            applied_mobile_notes += 1
        elif event["kind"] == "mobile.watch_action":
            remote_id = str(payload.get("watch_id", ""))
            action = str(payload.get("action", ""))
            note = str(payload.get("note", ""))[:300]
            dedup = f"sync:watch_item:{remote_id}"
            existing = next((w for w in db.list_watch_items(local_user_id) if w.dedup_key == dedup), None)
            target = {"confirm": "closed", "pause": "paused", "resume": "active"}.get(action)
            if existing is None or target is None:
                skipped_mobile_watch.append(f"{remote_id}:{action or 'bad-action'}")
            else:
                note_text = existing.condition_text
                if note:
                    note_text = (str(existing.condition_text) + f"｜📱{note}")[:1000]
                merged = existing.model_copy(update={
                    "status": WatchStatus(target), "condition_text": note_text,
                    "updated_at": _dt.now(UTC),
                })
                db.upsert_watch_item(merged)
                applied_mobile_watch += 1
        elif event["kind"] == "mobile.research_request":
            question = str(payload.get("question", ""))[:2000]
            db.upsert_notification(Notification(
                notification_id=_uuid4(), user_id=local_user_id,
                instrument=str(payload.get("symbol", ""))[:50],
                triggered_by=f"手机端/{str(payload.get('device_id', ''))[:20]}",
                condition_kind="mobile_research_request",
                title=("🔬 研究请求:" + (question[:120] or "未附问题"))[:300],
                summary=question or "(手机端未附问题)",
                action_mode=ActionMode.OBSERVE,
                evidence_refs=[],
            ), dedup_key=f"sync:mobile_rq:{event['idempotency_key']}")
            applied_mobile_research += 1
        elif event["kind"] == "plan.status":
            remote_plan = payload.get("plan_id", "")
            local_plan = plan_map.get(remote_plan)
            plan = None
            if local_plan:
                try:
                    plan = db.get_plan(_UUID(local_plan), local_user_id)
                except KeyError:
                    plan = None
            if plan is not None:
                try:
                    target = PlanStatus(payload.get("status", plan.status.value))
                except ValueError:
                    unknown_kinds.append(f"plan:{event['idempotency_key']}:bad status")
                    continue
                db.insert_plan(plan.model_copy(update={"status": target}))
            else:
                skipped_plans.append(str(remote_plan))
        elif event["kind"] == "research.summary":
            skipped_research += 1
        else:
            unknown_kinds.append(event["kind"])

    return {
        "ok": True,
        "applied_watch_items": applied_watch,
        "updated_watch_items": updated_watch,
        "applied_notifications": applied_notifications,
        "applied_mobile_notes": applied_mobile_notes,
        "applied_mobile_watch": applied_mobile_watch,
        "applied_mobile_research": applied_mobile_research,
        "skipped_mobile_watch": skipped_mobile_watch,
        "skipped_plans": skipped_plans,
        "skipped_research": skipped_research,
        "unknown_kinds": unknown_kinds,
        "rule": "应用幂等(来源 dedup_key/entity_map);计划不自动创建;研究摘要只读。",
    }


# ———————— §7.1 申请制中转：客户端 ————————

ALLOWED_ARTIFACT_TYPES = ("parameter_set", "model_weights", "strategy_pack", "dataset", "backtest_result")


def _transfer_mac(sync_key: str) -> bytes:
    """从同步密钥派生传输签名密钥(HMAC-SHA256),与事件信封密钥分离使用。"""
    import hashlib as _hashlib

    return _hashlib.sha256((sync_key + "::transfer").encode("ascii")).digest()


def seal_transfer(sync_key: str, *, artifact_type: str, version: int, payload: dict[str, Any]) -> dict[str, Any]:
    """封包:明文 payload → Fernet 密文;并对 元数据+密文摘要 生成 HMAC 签名。"""
    import hashlib as _hashlib
    import hmac as _hmac

    sealed = seal_payload(sync_key, payload)
    body_sha = _hashlib.sha256(sealed.encode("ascii")).hexdigest()
    signature = _hmac.new(_transfer_mac(sync_key),
                          f"{artifact_type}|{version}|{body_sha}".encode(),
                          _hashlib.sha256).hexdigest()
    return {"artifact_type": artifact_type, "version": version,
            "ciphertext": sealed, "sha256": body_sha, "signature": signature}


def open_transfer(sync_key: str, envelope: dict[str, Any]) -> dict[str, Any]:
    """开包:重算密文哈希、验 HMAC 签名、解密;任一不符抛 ValueError(不静默)。"""
    import hashlib as _hashlib
    import hmac as _hmac

    artifact_type = str(envelope.get("artifact_type", ""))
    version = int(envelope.get("version", 1))
    ciphertext = str(envelope.get("ciphertext", ""))
    if artifact_type not in ALLOWED_ARTIFACT_TYPES:
        raise ValueError(f"未知制品类型:{artifact_type}")
    body_sha = _hashlib.sha256(ciphertext.encode("ascii")).hexdigest()
    if body_sha != str(envelope.get("sha256", "")):
        raise ValueError("哈希不一致:密文在传输中被篡改或截断")
    expected_sig = _hmac.new(_transfer_mac(sync_key),
                             f"{artifact_type}|{version}|{body_sha}".encode(),
                             _hashlib.sha256).hexdigest()
    if not _hmac.compare_digest(expected_sig, str(envelope.get("signature", ""))):
        raise ValueError("签名验证失败:元数据与密文不匹配或密钥不符")
    payload = open_payload(sync_key, ciphertext)
    return {"artifact_type": artifact_type, "version": version, "payload": payload,
            "sha256": body_sha, "signature": expected_sig}


class TransferClient:
    """申请制中转的 HTTP 组合操作(复用 RelayClient 会话头)。"""

    def __init__(self, config: RelayConfig) -> None:
        self._client = RelayClient(config)

    def create_request(self, artifact_type: str, note: str = "") -> dict[str, Any]:
        with httpx.Client(trust_env=False, timeout=self._client._config.timeout_seconds) as http:
            resp = http.post(f"{self._client._config.base_url}/requests",
                             headers={**self._client._headers(), "Content-Type": "application/json"},
                             json={"artifact_type": artifact_type, "note": note})
            resp.raise_for_status()
            return resp.json()

    def list_requests(self) -> list[dict[str, Any]]:
        with httpx.Client(trust_env=False, timeout=self._client._config.timeout_seconds) as http:
            resp = http.get(f"{self._client._config.base_url}/requests", headers=self._client._headers())
            resp.raise_for_status()
            return resp.json()

    def decide(self, request_id: str, decision: str) -> dict[str, Any]:
        with httpx.Client(trust_env=False, timeout=self._client._config.timeout_seconds) as http:
            resp = http.post(f"{self._client._config.base_url}/requests/{request_id}/{decision}",
                             headers=self._client._headers())
            resp.raise_for_status()
            return resp.json()

    def send_approved(self, request_id: str, envelope: dict[str, Any], *, ttl_seconds: int = 1800) -> dict[str, Any]:
        """对已 approved 的申请:建包(绑定申请) → 上传密文 → 完成报告。"""
        import hashlib as _hashlib

        with httpx.Client(trust_env=False, timeout=self._client._config.timeout_seconds) as http:
            headers = self._client._headers()
            resp = http.post(f"{self._client._config.base_url}/relay/packages",
                             headers={**headers, "Content-Type": "application/json"},
                             json={"declared_size": len(envelope["ciphertext"].encode("utf-8")),
                                   "sha256": envelope["sha256"], "request_id": request_id,
                                   "ttl_seconds": ttl_seconds})
            resp.raise_for_status()
            pkg = resp.json()
            data = envelope["ciphertext"].encode("utf-8")
            put = http.put(f"{self._client._config.base_url}/relay/packages/{pkg['package_id']}/data",
                           headers={**headers, "Content-Type": "application/octet-stream"}, content=data)
            put.raise_for_status()
            return {"package_id": pkg["package_id"], "bytes": len(data),
                    "sha256": _hashlib.sha256(data).hexdigest(), "status": "uploaded"}

    def receive_ready(self, request_id: str) -> dict[str, Any]:
        """领取一次性令牌 → 下载密文(服务器副本即删)。"""
        with httpx.Client(trust_env=False, timeout=self._client._config.timeout_seconds) as http:
            headers = self._client._headers()
            tok = http.get(f"{self._client._config.base_url}/requests/{request_id}/token", headers=headers)
            tok.raise_for_status()
            token = tok.json()
            got = http.get(f"{self._client._config.base_url}/relay/download",
                           headers=headers, params={"token": token["download_token"]})
            got.raise_for_status()
            return {"ciphertext": got.content.decode("utf-8"), "sha256": token["sha256"],
                    "expires_at": token["expires_at"]}


class MarketClient:
    """§6 市场目录客户端:经 relay 拉 GitHub Releases 目录(仅元数据,不复制包)。"""

    def __init__(self, config: RelayConfig) -> None:
        self._client = RelayClient(config)

    def catalog(self) -> dict[str, Any]:
        with httpx.Client(trust_env=False, timeout=self._client._config.timeout_seconds) as http:
            resp = http.get(f"{self._client._config.base_url}/market/catalog",
                            headers=self._client._headers())
            resp.raise_for_status()
            return resp.json()

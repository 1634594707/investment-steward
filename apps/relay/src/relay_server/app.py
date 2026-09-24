"""中转服务主体：设备登记 / 变更事件(幂等) / 加密中转包(一次性下载令牌)。

实现口径(冻结):
- 鉴权:设备登记用引导令牌(X-Relay-Bootstrap);其余接口用用户令牌(X-Relay-Token),库中只存 sha256;
- 事件幂等:UNIQUE(user_id, idempotency_key),重复上传返回 duplicate=true,不重复入库;
- 游标拉取:event_id 单调递增,GET /sync/events?cursor=&limit= 分页,只返回本用户事件;
- 中转包:上传(sha256 校验)→ 签发一次性令牌 → 下载成功即删文件与令牌,服务器只留元数据(status=relayed);
  令牌过期/已用/包过期 → 410;过期包在访问时惰性清理;
- 服务器永不解释 payload(客户端加密信封,base64 密文),无全量导出接口。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import shutil
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status

from ._version import __version__
from .settings import RelaySettings

_PAIRING_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # 去掉易混淆的 0/1/I/O


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class RelayStore:
    """单连接 + 读写锁;WAL 模式;服务器轻负载下足够(升级触发条件见 README)。"""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(db_path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA synchronous = NORMAL;
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS device_tokens (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(user_id),
                    device_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(user_id),
                    label TEXT NOT NULL DEFAULT '',
                    public_key TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS sync_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS relay_packages (
                    package_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    declared_size INTEGER NOT NULL,
                    actual_size INTEGER,
                    file_path TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    relayed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS download_tokens (
                    token_hash TEXT PRIMARY KEY,
                    package_id TEXT NOT NULL REFERENCES relay_packages(package_id),
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                );
                CREATE TABLE IF NOT EXISTS transfer_requests (
                    request_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    package_id TEXT,
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    delivered_at TEXT
                );
                CREATE TABLE IF NOT EXISTS pairing_codes (
                    code TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    user_token TEXT NOT NULL,
                    sync_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                """
            )
            self._connection.commit()

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> list[sqlite3.Row]:
        with self._lock:
            cursor = self._connection.execute(sql, params or {})
            rows = cursor.fetchall()
            self._connection.commit()
            return rows

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def create_app(settings: RelaySettings) -> FastAPI:
    app = FastAPI(title="Investment Steward Relay", version=__version__, docs_url=None, redoc_url=None)
    store = RelayStore(settings.data_dir / "relay.sqlite3")
    packages_dir = settings.data_dir / "packages"
    packages_dir.mkdir(parents=True, exist_ok=True)
    app.state.relay_store = store
    app.state.relay_settings = settings

    def _auth_user(request: Request) -> sqlite3.Row:
        token = request.headers.get("X-Relay-Token", "")
        if not token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing X-Relay-Token")
        rows = store.execute("SELECT * FROM users WHERE token_hash = :h", {"h": _sha256_hex(token)})
        if rows:
            return rows[0]
        # 设备级 token（POST /devices/attach 签发）映射到所属 user;已撤销设备立即失效
        dev_rows = store.execute(
            "SELECT t.user_id AS user_id, t.device_id AS device_id FROM device_tokens t "
            "JOIN devices d ON d.device_id = t.device_id "
            "WHERE t.token_hash = :h AND d.revoked_at IS NULL",
            {"h": _sha256_hex(token)},
        )
        if dev_rows:
            return {"user_id": dev_rows[0]["user_id"], "device_id": dev_rows[0]["device_id"]}
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")

    @app.get("/health")
    def health() -> dict[str, object]:
        events = store.execute("SELECT COALESCE(MAX(event_id), 0) AS max_cursor FROM sync_events")
        pending = store.execute("SELECT COUNT(*) AS n FROM relay_packages WHERE status IN ('pending','ready')")
        return {
            "status": "ok",
            "version": __version__,
            "events_max_cursor": events[0]["max_cursor"],
            "packages_pending": pending[0]["n"],
            "rules": {
                "max_package_bytes": settings.max_package_bytes,
                "max_event_payload_bytes": settings.max_event_payload_bytes,
                "max_pull_limit": settings.max_pull_limit,
                "max_token_ttl_seconds": settings.max_token_ttl_seconds,
            },
        }

    # ———— 设备与用户登记(引导令牌一次性建立,令牌只回显一次) ————

    @app.post("/devices", status_code=status.HTTP_201_CREATED)
    async def register_device(request: Request) -> dict[str, object]:
        bootstrap = request.headers.get("X-Relay-Bootstrap", "")
        if not hmac.compare_digest(bootstrap, settings.bootstrap_token):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bootstrap token")
        try:
            body = await request.json()
        except Exception as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid json") from error
        label = str(body.get("label", ""))[:100] if isinstance(body, dict) else ""
        public_key = str(body.get("public_key", ""))[:2000] if isinstance(body, dict) else ""
        user_token = secrets.token_urlsafe(32)
        user_id = "u-" + secrets.token_hex(8)
        device_id = "d-" + secrets.token_hex(8)
        store.execute(
            "INSERT INTO users (user_id, token_hash, created_at) VALUES (:u, :h, :t)",
            {"u": user_id, "h": _sha256_hex(user_token), "t": _now()},
        )
        store.execute(
            "INSERT INTO devices (device_id, user_id, label, public_key, created_at) VALUES (:d, :u, :l, :p, :t)",
            {"d": device_id, "u": user_id, "l": label, "p": public_key, "t": _now()},
        )
        return {"user_id": user_id, "device_id": device_id, "user_token": user_token,
                "note": "user_token 仅此一次返回,服务器只存哈希;请立即保存到本机凭据库。"}

    @app.post("/devices/attach", status_code=status.HTTP_201_CREATED)
    async def attach_device(request: Request) -> dict[str, object]:
        """多设备接入（§5 设备绑定）:已有 user 凭主 token 挂新设备,签发设备级 token。

        设备 token 可拉取/上传该 user 的事件,但 /devices 管理与撤销仍属主 token;
        响应的 device_token 仅此一次回显,服务器只存哈希。
        """
        user = _auth_user(request)
        try:
            body = await request.json()
        except Exception as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid json") from error
        label = str(body.get("label", ""))[:100] if isinstance(body, dict) else ""
        public_key = str(body.get("public_key", ""))[:2000] if isinstance(body, dict) else ""
        device_id = "d-" + secrets.token_hex(8)
        device_token = secrets.token_urlsafe(32)
        store.execute(
            "INSERT INTO devices (device_id, user_id, label, public_key, created_at) VALUES (:d, :u, :l, :p, :t)",
            {"d": device_id, "u": user["user_id"], "l": label, "p": public_key, "t": _now()},
        )
        store.execute(
            "INSERT INTO device_tokens (token_hash, user_id, device_id, created_at) VALUES (:h, :u, :d, :t)",
            {"h": _sha256_hex(device_token), "u": user["user_id"], "d": device_id, "t": _now()},
        )
        return {"user_id": user["user_id"], "device_id": device_id, "device_token": device_token,
                "note": "device_token 仅此一次返回,服务器只存哈希;请立即保存。"}

    @app.get("/devices")
    def list_devices(request: Request) -> list[dict[str, object]]:
        user = _auth_user(request)
        rows = store.execute(
            "SELECT device_id, label, public_key, created_at, revoked_at FROM devices WHERE user_id = :u ORDER BY created_at",
            {"u": user["user_id"]},
        )
        return [dict(row) for row in rows]

    @app.delete("/devices/{device_id}")
    def revoke_device(device_id: str, request: Request) -> dict[str, object]:
        user = _auth_user(request)
        rows = store.execute(
            "UPDATE devices SET revoked_at = :t WHERE device_id = :d AND user_id = :u AND revoked_at IS NULL RETURNING device_id",
            {"t": _now(), "d": device_id, "u": user["user_id"]},
        )
        if not rows:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="device not found or already revoked")
        return {"device_id": device_id, "revoked": True}

    # ———— 变更事件(幂等上传 + 游标拉取) ————

    @app.post("/sync/events")
    async def upload_events(request: Request) -> dict[str, object]:
        user = _auth_user(request)
        try:
            body = await request.json()
        except Exception as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid json") from error
        events = body.get("events") if isinstance(body, dict) else None
        if not isinstance(events, list) or not events or len(events) > 200:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="events 必须是 1..200 的数组")
        device_id = str(body.get("device_id", ""))[:40] if isinstance(body, dict) else ""
        results: list[dict[str, object]] = []
        for event in events:
            if not isinstance(event, dict):
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="event 必须是对象")
            kind = str(event.get("kind", ""))[:64]
            idem = str(event.get("idempotency_key", ""))[:128]
            payload = str(event.get("payload", ""))
            if not kind or not idem:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="kind 与 idempotency_key 必填")
            if len(payload.encode("utf-8")) > settings.max_event_payload_bytes:
                raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                    detail=f"payload 超过 {settings.max_event_payload_bytes} 字节上限")
            try:
                version = int(event.get("version", 1))
            except (TypeError, ValueError) as error:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="version 必须是整数") from error
            existing = store.execute(
                "SELECT event_id FROM sync_events WHERE user_id = :u AND idempotency_key = :k",
                {"u": user["user_id"], "k": idem},
            )
            if existing:
                results.append({"idempotency_key": idem, "duplicate": True, "event_id": existing[0]["event_id"]})
                continue
            inserted = store.execute(
                "INSERT INTO sync_events (user_id, device_id, kind, idempotency_key, version, payload, created_at) "
                "VALUES (:u, :d, :k2, :k, :v, :p, :t) RETURNING event_id",
                {"u": user["user_id"], "d": device_id, "k2": kind, "k": idem, "v": version, "p": payload, "t": _now()},
            )
            results.append({"idempotency_key": idem, "duplicate": False, "event_id": inserted[0]["event_id"]})
        return {"results": results}

    @app.get("/sync/events")
    def pull_events(request: Request, cursor: int = 0, limit: int = 100) -> dict[str, object]:
        user = _auth_user(request)
        if cursor < 0 or limit < 1 or limit > settings.max_pull_limit:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail=f"cursor>=0 且 1<=limit<={settings.max_pull_limit}")
        rows = store.execute(
            "SELECT event_id, device_id, kind, idempotency_key, version, payload, created_at FROM sync_events "
            "WHERE user_id = :u AND event_id > :c ORDER BY event_id LIMIT :l",
            {"u": user["user_id"], "c": cursor, "l": limit},
        )
        max_row = store.execute("SELECT COALESCE(MAX(event_id), 0) AS m FROM sync_events WHERE user_id = :u",
                                {"u": user["user_id"]})
        return {"cursor": cursor, "cursor_max": max_row[0]["m"], "events": [dict(row) for row in rows]}

    # ———— 配对码（小白引导）:桌面生成短码,手机只输码即完成接入 ————

    @app.post("/pairing/codes", status_code=status.HTTP_201_CREATED)
    async def create_pairing_code(request: Request) -> dict[str, object]:
        """已配置设备生成 6 位配对码;服务器暂存 master token+sync_key,TTL 内可被领取一次。

        便捷性优先的权衡:配对窗口内(默认 10 分钟)服务器可见明文凭据;领取即删,过期即删。
        """
        user = _auth_user(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        user_token = str(body.get("user_token", ""))[:200] if isinstance(body, dict) else ""
        sync_key = str(body.get("sync_key", ""))[:200] if isinstance(body, dict) else ""
        if not user_token or not sync_key:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="user_token 与 sync_key 必填")
        try:
            ttl = int(body.get("ttl_seconds", 600)) if isinstance(body, dict) else 600
        except (TypeError, ValueError):
            ttl = 600
        ttl = max(60, min(ttl, settings.max_token_ttl_seconds))
        code = "".join(secrets.choice(_PAIRING_ALPHABET) for _ in range(6))
        expires = (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat()
        store.execute(
            "INSERT INTO pairing_codes (code, user_id, user_token, sync_key, created_at, expires_at) "
            "VALUES (:c, :u, :t, :k, :n, :e)",
            {"c": code, "u": user["user_id"], "t": user_token, "k": sync_key, "n": _now(), "e": expires},
        )
        return {"code": code, "expires_at": expires, "ttl_seconds": ttl}

    @app.get("/pairing/claim")
    def claim_pairing_code(code: str = "") -> dict[str, object]:
        """手机端凭码领取凭据(一次性):领取即删,过期即删。无需鉴权(码本身就是凭据)。"""
        norm = "".join(ch for ch in code.strip().upper() if ch in _PAIRING_ALPHABET)
        if len(norm) != 6:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="配对码格式不对")
        rows = store.execute("SELECT * FROM pairing_codes WHERE code = :c", {"c": norm})
        if not rows:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="配对码无效或已被使用")
        row = rows[0]
        store.execute("DELETE FROM pairing_codes WHERE code = :c", {"c": norm})
        if row["expires_at"] <= _now():
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="配对码已过期,请重新生成")
        return {"user_id": row["user_id"], "user_token": row["user_token"], "sync_key": row["sync_key"]}

    # ———— 参数/制品申请制中转（§7.1）：接收方申请,显式同意后才允许上传 ————

    ALLOWED_ARTIFACT_TYPES = {"parameter_set", "model_weights", "strategy_pack", "dataset", "backtest_result"}

    @app.post("/requests", status_code=status.HTTP_201_CREATED)
    async def create_transfer_request(request: Request) -> dict[str, object]:
        user = _auth_user(request)
        try:
            body = await request.json()
        except Exception as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid json") from error
        if not isinstance(body, dict):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="body 必须是对象")
        artifact_type = str(body.get("artifact_type", ""))
        if artifact_type not in ALLOWED_ARTIFACT_TYPES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail=f"artifact_type 必须是 {sorted(ALLOWED_ARTIFACT_TYPES)} 之一")
        request_id = "req-" + secrets.token_hex(8)
        store.execute(
            "INSERT INTO transfer_requests (request_id, user_id, artifact_type, note, status, created_at) "
            "VALUES (:r, :u, :t, :n, 'pending', :c)",
            {"r": request_id, "u": user["user_id"], "t": artifact_type,
             "n": str(body.get("note", ""))[:500], "c": _now()},
        )
        return {"request_id": request_id, "artifact_type": artifact_type, "status": "pending",
                "note": "等待发送方显式同意;未同意前不允许任何上传绑定。"}

    @app.get("/requests")
    def list_transfer_requests(request: Request) -> list[dict[str, object]]:
        user = _auth_user(request)
        rows = store.execute(
            "SELECT request_id, artifact_type, note, status, package_id, created_at, decided_at, delivered_at "
            "FROM transfer_requests WHERE user_id = :u ORDER BY created_at DESC LIMIT 200",
            {"u": user["user_id"]},
        )
        return [dict(row) for row in rows]

    @app.post("/requests/{request_id}/{decision}")
    def decide_transfer_request(request_id: str, decision: str, request: Request) -> dict[str, object]:
        """显式同意/拒绝。同用户多设备语义下由用户令牌操作;未同意前上传绑定会被拒绝。"""
        user = _auth_user(request)
        if decision not in ("approve", "reject"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown decision")
        target = "approved" if decision == "approve" else "rejected"
        rows = store.execute(
            "UPDATE transfer_requests SET status = :s, decided_at = :t "
            "WHERE request_id = :r AND user_id = :u AND status = 'pending' RETURNING request_id",
            {"s": target, "t": _now(), "r": request_id, "u": user["user_id"]},
        )
        if not rows:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail="申请不存在、不属于当前用户或已处理")
        return {"request_id": request_id, "status": target}

    @app.get("/requests/{request_id}/token")
    def request_download_token(request_id: str, request: Request) -> dict[str, object]:
        """接收方按已批准申请领取一次性下载令牌(要求包已上传就绪)。"""
        user = _auth_user(request)
        rows = store.execute(
            "SELECT * FROM transfer_requests WHERE request_id = :r AND user_id = :u",
            {"r": request_id, "u": user["user_id"]},
        )
        if not rows:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="申请不存在")
        tr = rows[0]
        if tr["status"] == "delivered":
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="申请已交付(一次性,不可重复领取)")
        if tr["status"] != "uploaded" or not tr["package_id"]:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail=f"申请状态为 {tr['status']}:仅 uploaded 状态可领取下载令牌")
        pkg = store.execute("SELECT * FROM relay_packages WHERE package_id = :p AND user_id = :u AND status = 'ready'",
                            {"p": tr["package_id"], "u": user["user_id"]})
        if not pkg:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="关联包不存在、未就绪或已过期")
        package = pkg[0]
        if package["expires_at"] < _now():
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="关联包已过期")
        token = secrets.token_urlsafe(32)
        ttl = int((datetime.fromisoformat(package["expires_at"]) - datetime.now(UTC)).total_seconds())
        store.execute("INSERT INTO download_tokens (token_hash, package_id, expires_at) VALUES (:h, :p, :e)",
                      {"h": _sha256_hex(token), "p": package["package_id"],
                       "e": (datetime.now(UTC) + timedelta(seconds=max(ttl, 60))).isoformat()})
        return {"download_token": token, "download": "/relay/download",
                "sha256": package["sha256"], "expires_at": package["expires_at"],
                "note": "一次性令牌;下载成功后服务器副本即删,申请标记 delivered。"}

    # ———— 加密中转包(上传→一次性令牌→下载即删) ————

    def _expire_stale(user_id: str) -> None:
        now = _now()
        stale = store.execute(
            "SELECT package_id, file_path FROM relay_packages WHERE user_id = :u AND status IN ('pending','ready') AND expires_at < :t",
            {"u": user_id, "t": now},
        )
        for row in stale:
            if row["file_path"]:
                Path(row["file_path"]).unlink(missing_ok=True)
            store.execute("UPDATE relay_packages SET status = 'expired', file_path = NULL WHERE package_id = :p",
                          {"p": row["package_id"]})

    @app.post("/relay/packages", status_code=status.HTTP_201_CREATED)
    async def create_package(request: Request) -> dict[str, object]:
        user = _auth_user(request)
        _expire_stale(user["user_id"])
        try:
            body = await request.json()
        except Exception as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid json") from error
        if not isinstance(body, dict):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="body 必须是对象")
        try:
            declared = int(body.get("declared_size", 0))
            ttl = int(body.get("ttl_seconds", 1800))
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="declared_size/ttl_seconds 必须是整数") from error
        sha = str(body.get("sha256", "")).lower()
        if declared <= 0 or declared > settings.max_package_bytes:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                detail=f"declared_size 必须在 1..{settings.max_package_bytes}")
        if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="sha256 必须是 64 位十六进制")
        if ttl < 60 or ttl > settings.max_token_ttl_seconds:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail=f"ttl_seconds 必须在 60..{settings.max_token_ttl_seconds}")
        request_id = body.get("request_id")
        bound_request = None
        if request_id:
            req_rows = store.execute(
                "SELECT * FROM transfer_requests WHERE request_id = :r AND user_id = :u",
                {"r": str(request_id), "u": user["user_id"]},
            )
            if not req_rows:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="绑定的申请不存在")
            bound_request = req_rows[0]
            if bound_request["status"] != "approved":
                raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                    detail=f"申请状态为 {bound_request['status']}:仅 approved 可绑定上传(§7.1 申请制)")
        package_id = "pkg-" + secrets.token_hex(8)
        store.execute(
            "INSERT INTO relay_packages (package_id, user_id, sha256, declared_size, status, created_at, expires_at) "
            "VALUES (:p, :u, :s, :d, 'pending', :t, :e)",
            {"p": package_id, "u": user["user_id"], "s": sha, "d": declared, "t": _now(),
             "e": (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat()},
        )
        if bound_request:
            store.execute("UPDATE transfer_requests SET package_id = :p WHERE request_id = :r",
                          {"p": package_id, "r": bound_request["request_id"]})
        return {"package_id": package_id, "upload": f"/relay/packages/{package_id}/data",
                "request_id": str(request_id) if request_id else None,
                "expires_at": (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat()}

    @app.put("/relay/packages/{package_id}/data")
    async def upload_package_data(package_id: str, request: Request) -> dict[str, object]:
        user = _auth_user(request)
        rows = store.execute(
            "SELECT * FROM relay_packages WHERE package_id = :p AND user_id = :u AND status = 'pending'",
            {"p": package_id, "u": user["user_id"]},
        )
        if not rows:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="package 不存在、已上传或已过期")
        package = rows[0]
        data = await request.body()
        if len(data) != package["declared_size"]:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                detail=f"实际大小 {len(data)} 与声明 {package['declared_size']} 不一致")
        if hashlib.sha256(data).hexdigest() != package["sha256"]:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="sha256 校验失败")
        file_path = packages_dir / f"{package_id}.blob"
        file_path.write_bytes(data)
        store.execute("UPDATE relay_packages SET status = 'ready', actual_size = :a, file_path = :f WHERE package_id = :p",
                      {"a": len(data), "f": str(file_path), "p": package_id})
        store.execute(
            "UPDATE transfer_requests SET status = 'uploaded' WHERE package_id = :p AND status = 'approved'",
            {"p": package_id},
        )
        return {"package_id": package_id, "status": "ready", "bytes": len(data)}

    @app.post("/relay/packages/{package_id}/token")
    def issue_download_token(package_id: str, request: Request) -> dict[str, object]:
        user = _auth_user(request)
        rows = store.execute(
            "SELECT * FROM relay_packages WHERE package_id = :p AND user_id = :u AND status = 'ready'",
            {"p": package_id, "u": user["user_id"]},
        )
        if not rows:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="package 不存在、未上传或已过期")
        package = rows[0]
        if package["expires_at"] < _now():
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="package 已过期")
        token = secrets.token_urlsafe(32)
        ttl = int((datetime.fromisoformat(package["expires_at"]) - datetime.now(UTC)).total_seconds())
        store.execute("INSERT INTO download_tokens (token_hash, package_id, expires_at) VALUES (:h, :p, :e)",
                      {"h": _sha256_hex(token), "p": package_id,
                       "e": (datetime.now(UTC) + timedelta(seconds=max(ttl, 60))).isoformat()})
        return {"download_token": token, "download": f"/relay/download",
                "expires_at": package["expires_at"], "note": "一次性令牌;下载成功后服务器副本即删。"}

    @app.get("/relay/download")
    def download_package(request: Request, token: str) -> Response:
        # 下载令牌自证身份(不要求 X-Relay-Token):接收方可能只有令牌。
        rows = store.execute("SELECT * FROM download_tokens WHERE token_hash = :h", {"h": _sha256_hex(token)})
        if not rows or rows[0]["used_at"] is not None:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="下载令牌无效或已使用")
        token_row = rows[0]
        if token_row["expires_at"] < _now():
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="下载令牌已过期")
        pkg_rows = store.execute("SELECT * FROM relay_packages WHERE package_id = :p", {"p": token_row["package_id"]})
        package = pkg_rows[0] if pkg_rows else None
        file_path = Path(package["file_path"]) if package and package["file_path"] else None
        if package is None or file_path is None or not file_path.exists():
            store.execute("UPDATE download_tokens SET used_at = :t WHERE token_hash = :h", {"t": _now(), "h": token_row["token_hash"]})
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="服务器副本不存在(可能已中转完成或被清理)")
        data = file_path.read_bytes()
        # 中转完成:删文件、删令牌、只留元数据(路线图 §4 硬约束)
        file_path.unlink(missing_ok=True)
        store.execute(
            "UPDATE relay_packages SET status = 'relayed', file_path = NULL, relayed_at = :t WHERE package_id = :p",
            {"t": _now(), "p": package["package_id"]},
        )
        store.execute(
            "UPDATE transfer_requests SET status = 'delivered', delivered_at = :t WHERE package_id = :p AND status = 'uploaded'",
            {"t": _now(), "p": package["package_id"]},
        )
        store.execute("UPDATE download_tokens SET used_at = :t WHERE token_hash = :h", {"t": _now(), "h": token_row["token_hash"]})
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={"X-Package-Sha256": package["sha256"], "Content-Disposition": f'attachment; filename="{package["package_id"]}.blob"'},
        )

    @app.get("/relay/packages/{package_id}")
    def package_status(package_id: str, request: Request) -> dict[str, object]:
        user = _auth_user(request)
        rows = store.execute(
            "SELECT package_id, sha256, declared_size, actual_size, status, created_at, expires_at, relayed_at "
            "FROM relay_packages WHERE package_id = :p AND user_id = :u",
            {"p": package_id, "u": user["user_id"]},
        )
        if not rows:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="package 不存在")
        return dict(rows[0])

    # ———— 运维:过期清理(显式触发,幂等) ————

    @app.post("/maintenance/cleanup")
    def cleanup(request: Request) -> dict[str, object]:
        user = _auth_user(request)
        _expire_stale(user["user_id"])
        removed = 0
        for file_path in packages_dir.glob("*.blob"):
            rows = store.execute("SELECT package_id FROM relay_packages WHERE file_path = :f AND status = 'ready'",
                                 {"f": str(file_path)})
            if not rows:
                file_path.unlink(missing_ok=True)
                removed += 1
        return {"cleaned": True, "orphan_files_removed": removed}

    _register_market_endpoints(app, settings, _auth_user)
    return app


def build_app() -> FastAPI:
    """uvicorn --factory 入口:从环境变量装配(无参工厂)。"""
    from .settings import load_settings

    return create_app(load_settings())


def main() -> None:  # pragma: no cover - 进程入口
    import uvicorn

    from .settings import load_settings

    settings = load_settings()
    uvicorn.run(create_app(settings), host="127.0.0.1", port=8900, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    main()


def _register_market_endpoints(app: FastAPI, settings: RelaySettings, _auth_user) -> None:
    """§6 市场目录缓存：从 GitHub Releases 拉插件目录(仅元数据,不复制包)并缓存;
    GitHub 不可用时回退缓存并如实标注 stale。撤回状态一并缓存。"""
    import json as _json
    import urllib.request as _urlreq

    catalog_path = settings.data_dir / "market_catalog.json"

    @app.get("/market/catalog")
    def market_catalog(request: Request) -> dict[str, object]:
        _auth_user(request)
        repo = os.environ.get("RELAY_MARKET_REPO", "")
        if not repo:
            return {"source": "not_configured", "releases": [], "revoked": [],
                    "note": "RELAY_MARKET_REPO 未配置:中转不提供市场目录(如实,不伪造)。"}
        entries: list[dict[str, object]] = []
        source = "github"
        stale = False
        try:
            req = _urlreq.Request(f"https://api.github.com/repos/{repo}/releases?per_page=50",
                                  headers={"User-Agent": "InvestmentSteward-Relay/1.0",
                                           "Accept": "application/vnd.github+json"})
            with _urlreq.urlopen(req, timeout=15) as resp:
                releases = _json.loads(resp.read().decode("utf-8"))
        except Exception as error:  # GitHub 不可用 → 缓存回退
            stale = True
            if catalog_path.exists():
                cached = _json.loads(catalog_path.read_text(encoding="utf-8"))
                return {**cached, "source": "relay-cache", "stale": True,
                        "note": f"GitHub 不可达({error});以下为最近一次缓存目录,不保证为最新。"}
            return {"source": "unavailable", "stale": False, "releases": [], "revoked": [],
                    "note": f"GitHub 不可达且无缓存:{error}"}
        for release in releases:
            tag = str(release.get("tag_name", ""))
            revoked = str(release.get("body", "") or "").strip().upper().startswith("REVOKE")
            for asset in release.get("assets", []):
                if asset.get("name") != "manifest.json":
                    continue
                try:
                    mreq = _urlreq.Request(str(asset.get("browser_download_url")),
                                           headers={"User-Agent": "InvestmentSteward-Relay/1.0"})
                    with _urlreq.urlopen(mreq, timeout=15) as mresp:
                        manifest = _json.loads(mresp.read().decode("utf-8"))
                except Exception:
                    continue  # 单个 manifest 拉取失败跳过,不阻塞目录
                entries.append({
                    "plugin_id": manifest.get("plugin_id"),
                    "release_version": manifest.get("release_version"),
                    "artifact_sha256": manifest.get("artifact_sha256"),
                    "signature": manifest.get("signature"),
                    "manifest": manifest,
                    "tag": tag,
                    "changelog": str(release.get("body", ""))[:2000],
                    "revoked": revoked,
                    "prerelease": bool(release.get("prerelease")),
                })
        payload = {"source": source, "stale": stale, "releases": entries,
                   "revoked": [e["plugin_id"] for e in entries if e["revoked"]],
                   "fetched_at": _now(), "repo": repo}
        catalog_path.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return payload

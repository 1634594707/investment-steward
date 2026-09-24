"""本机凭据库（G3）。

设计稿要求密钥存 OS 凭据存储（Windows 凭据管理器 / keychain），Core 只持句柄。本实现提供
`CredentialStore` 抽象：平台为 Windows 且 WinCred 可用时优先 `WindowsCredentialManagerStore`，
否则回退 `DbCredentialStore`（本机数据库文件兜底；该兜底不宣称额外加密）。密钥原文永不写日志、不进审计 payload、
不进插件进程环境 —— 只有存储层内部持有；端点响应仅回 `last4 + backend + updated_at`。

目录行（key_id / last4 / updated_at / secret）持久在数据库；OS 模式下 secret 列留空、
明文存于凭据管理器，`get` 先查 OS 再回退 DB，保证测试环境与无凭据权限时仍可读。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from investment_steward_core.domain import CredentialRecord, CredentialStoreBackend

if TYPE_CHECKING:
    from investment_steward_core.storage import Database

# 单实例密钥：行情数据源 Token（Tushare）与通知渠道 Webhook（钉钉）、宏观 FRED key（D-11）、
# UN Comtrade API key（兼容既有 comtrade_key 与新 comtrade_api_key）、模型 API 密钥（G3-3：模型方案的 credential_ref 应引用此 key_id，明文不进 model_profiles 表）。
MARKET_DATA_TOKEN = "market_data_token"
NOTIFY_WEBHOOK = "notify_webhook"
MACRO_DATA_KEY = "macro_data_key"
COMTRADE_KEY = "comtrade_key"
COMTRADE_API_KEY = "comtrade_api_key"
MODEL_API_KEY = "model_api_key"
# JV02（Jev 决策模型接入路线图 2026-09-21）：Jev 的 TypeSafe API 密钥。
# 与 model_api_key 并列的**第二套出网协议**密钥——两者不可互相顶替（端点/计费主体都不同）。
JEV_API_KEY = "jev_api_key"

SINGLE_INSTANCE_KEYS = (
    MARKET_DATA_TOKEN,
    NOTIFY_WEBHOOK,
    MACRO_DATA_KEY,
    COMTRADE_KEY,
    COMTRADE_API_KEY,
    MODEL_API_KEY,
    JEV_API_KEY,
)


def _summary(key_id: str, secret: str, backend: CredentialStoreBackend) -> CredentialRecord:
    return CredentialRecord(
        key_id=key_id,
        last4=secret[-4:] if len(secret) >= 4 else secret,
        updated_at=datetime.now(UTC),
        backend=backend,
    )


class CredentialStore(ABC):
    @abstractmethod
    def get(self, key_id: str) -> str | None: ...

    @abstractmethod
    def store(self, key_id: str, secret: str) -> CredentialRecord: ...

    @abstractmethod
    def delete(self, key_id: str) -> None: ...

    @abstractmethod
    def list_records(self) -> list[CredentialRecord]: ...

    @property
    @abstractmethod
    def backend(self) -> CredentialStoreBackend: ...


class DbCredentialStore(CredentialStore):
    """文件兜底：secret 存于本机数据库文件；`backend=file`。"""

    def __init__(self, db: Database):
        self._db = db

    def get(self, key_id: str) -> str | None:
        return self._db.get_credential(key_id)

    def store(self, key_id: str, secret: str) -> CredentialRecord:
        self._db.upsert_credential(key_id, secret)
        return _summary(key_id, secret, CredentialStoreBackend.FILE)

    def delete(self, key_id: str) -> None:
        self._db.delete_credential(key_id)

    def list_records(self) -> list[CredentialRecord]:
        return self._db.list_credentials()

    @property
    def backend(self) -> CredentialStoreBackend:
        return CredentialStoreBackend.FILE


class WindowsCredentialManagerStore(CredentialStore):
    """Windows 凭据管理器（ctypes CredWrite/CredRead/CredDelete）。secret 列留空，明文在 OS。"""

    def __init__(self, db: Database):
        self._db = db

    def _target(self, key_id: str) -> str:
        return f"InvestmentSteward/{key_id}"

    def get(self, key_id: str) -> str | None:
        secret = _cred_read(self._target(key_id))
        if secret is None:
            return self._db.get_credential(key_id)
        return secret

    def store(self, key_id: str, secret: str) -> CredentialRecord:
        """优先写 OS 凭据管理器；CredWrite 失败（权限/策略受限等）自动回退文件兜底，不再让保存整体 500。"""
        try:
            _cred_write(self._target(key_id), secret)
        except OSError:
            self._db.upsert_credential(key_id, secret)
            return _summary(key_id, secret, CredentialStoreBackend.FILE)
        self._db.upsert_credential_meta(key_id, secret[-4:] if len(secret) >= 4 else secret)
        return _summary(key_id, secret, CredentialStoreBackend.OS)

    def delete(self, key_id: str) -> None:
        _cred_delete(self._target(key_id))
        self._db.delete_credential(key_id)

    def list_records(self) -> list[CredentialRecord]:
        """目录列表 = DB 元数据 + OS 实时状态校准：OS 明文存在时以 OS 尾号为准。

        历史缺陷（2026-09-04 实例）：直接回 DB 元数据——OS 明文被测试进程/其他路径
        覆盖后，列表仍显示旧尾号（「尾号 6366 已保存」），而 probe 实读是另一个值
        （示例 key → 401），严重误导诊断。现在逐条实读 OS；OS 缺失时若 DB 有明文
        标 file，两者皆无则 last4 如实报空（状态未知，不再展示过期快照）。
        """
        calibrated: list[CredentialRecord] = []
        for record in self._db.list_credentials():
            secret = _cred_read(self._target(record.key_id))
            if secret is not None:
                calibrated.append(
                    CredentialRecord(
                        key_id=record.key_id,
                        last4=secret[-4:] if len(secret) >= 4 else secret,
                        updated_at=record.updated_at,
                        backend=CredentialStoreBackend.OS,
                    )
                )
            elif self._db.get_credential(record.key_id):
                calibrated.append(
                    CredentialRecord(
                        key_id=record.key_id,
                        last4=record.last4,
                        updated_at=record.updated_at,
                        backend=CredentialStoreBackend.FILE,
                    )
                )
            else:
                calibrated.append(
                    CredentialRecord(
                        key_id=record.key_id,
                        last4="",
                        updated_at=record.updated_at,
                        backend=record.backend,
                    )
                )
        return calibrated

    @property
    def backend(self) -> CredentialStoreBackend:
        return CredentialStoreBackend.OS


def _wincred_available() -> bool:
    try:
        return getattr(ctypes, "windll", None) is not None
    except Exception:  # noqa: BLE001 - 非 Windows 无 windll；返回 False 走文件兜底
        return False


def _cred_blob(secret: str) -> tuple[Any, int]:
    """构造 CredWrite 的 CredentialBlob：UTF-16-LE 字节缓冲（缓冲长度=字节数，而非字符数）。

    历史缺陷：缓冲按 len(secret)（字符数）分配却填充 2*len(secret) 个字节初始化器，
    Windows 上任何密钥保存都会 IndexError → 全部凭据写入 500。已修正为按字节长度分配。
    """
    encoded = secret.encode("utf-16-le")
    blob = (ctypes.c_byte * len(encoded))(*encoded)
    return blob, len(encoded)


def _cred_write(target: str, secret: str) -> None:
    from ctypes import wintypes

    class _CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    blob, size = _cred_blob(secret)
    cred = _CREDENTIAL()
    cred.Type = 1
    cred.TargetName = target
    cred.CredentialBlobSize = size
    cred.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_byte))
    cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    if not advapi32.CredWriteW(ctypes.byref(cred), 0):
        raise OSError(f"CredWriteW failed: {ctypes.get_last_error()}")


def _cred_read(target: str) -> str | None:
    from ctypes import wintypes

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    handle = ctypes.c_void_p()
    if not ctypes.windll.advapi32.CredReadW(target, 1, 0, ctypes.byref(handle)):
        return None
    pointer = ctypes.cast(handle, ctypes.POINTER(_CREDENTIALW))
    size = pointer.contents.CredentialBlobSize
    raw = ctypes.string_at(pointer.contents.CredentialBlob, size)
    ctypes.windll.advapi32.CredFree(handle)
    return raw.decode("utf-16-le").rstrip("\x00")


def _cred_delete(target: str) -> None:
    ctypes.windll.advapi32.CredDeleteW(target, 1, 0)


def resolve_store(db: Database) -> CredentialStore:
    """选择合适存储：Windows 凭据管理器可用即用它，否则文件兜底。"""
    return (
        WindowsCredentialManagerStore(db)
        if _wincred_available()
        else DbCredentialStore(db)
    )

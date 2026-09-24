"""策略包管线(quant_strategy_packs):分享池阶段 B。

对齐开放条件「作者签名 + 隔离执行」:

- 签名:复用插件信任锚(Ed25519 + SHA-256 canonical 完整性,signing.verify_manifest_integrity,
  钉扎发布者公钥)——签名无效/未签名/内容被篡改的包一律拒绝入目录;签名覆盖包代码
  (manifest 内 payload + payload_sha256),篡改代码必然验签失败;
- 执行:通过验签的包 state=installed,由受限执行器运行(quant_pack_runner:一次性子进程 +
  CPython 审计钩子阻断文件/网络/子进程,import 仅放行纯计算标准库,超时强制终止)。
  已知边界与后续容器级硬化见 ADR-0008 修订。

存储:layout/quant_strategy_packs.json,pack_id = sp-{canonical sha256[:16]}(内容寻址)。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from investment_steward_core.storage.paths import StorageLayout
from typing import Any

from investment_steward_core.signing import canonical_bytes, verify_manifest_integrity

PACK_REQUIRED_FIELDS = ("name", "version", "author", "entrypoint", "symbol")


def _load(layout: StorageLayout) -> list[dict[str, Any]]:
    try:
        data = json.loads(layout.quant_strategy_packs_file.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except (OSError, ValueError):
        pass
    return []


def _save(layout: StorageLayout, packs: list[dict[str, Any]]) -> None:
    layout.artifacts.mkdir(parents=True, exist_ok=True)
    layout.quant_strategy_packs_file.write_text(json.dumps(packs, ensure_ascii=False, indent=2), encoding="utf-8")


def list_packs(layout: StorageLayout) -> list[dict[str, Any]]:
    return sorted(_load(layout), key=lambda item: item["imported_at"], reverse=True)


def get_pack(layout: StorageLayout, pack_id: str) -> dict[str, Any] | None:
    return next((p for p in _load(layout) if p["pack_id"] == pack_id), None)


def import_pack(layout: StorageLayout, manifest: dict[str, Any], *, public_key_pem: str) -> dict[str, Any]:
    """导入策略包:必填字段 + payload → Ed25519 验签 + 完整性 → 内容寻址入目录(可运行)。

    payload 为包代码文本,其 SHA-256 必须与 payload_sha256 一致且被签名覆盖;
    签名不通过抛 ValueError(调用方映射 422)。
    """
    for field in PACK_REQUIRED_FIELDS:
        if not str(manifest.get(field, "")).strip():
            raise ValueError(f"策略包缺少必填字段:{field}")
    if manifest.get("kind") != "strategy_pack":
        raise ValueError("manifest.kind 必须为 strategy_pack")
    payload = manifest.get("payload")
    if not isinstance(payload, str) or not payload.strip():
        raise ValueError("策略包需要 payload(包代码文本)")
    payload_sha256 = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if manifest.get("payload_sha256") != payload_sha256:
        raise ValueError("payload_sha256 与 payload 内容不一致")
    verify_manifest_integrity(manifest, public_key_pem)
    pack_id = f"sp-{hashlib.sha256(canonical_bytes(manifest)).hexdigest()[:16]}"
    existing = get_pack(layout, pack_id)
    if existing:
        return existing
    entry = {
        "pack_id": pack_id,
        "name": str(manifest["name"]),
        "version": str(manifest["version"]),
        "author": str(manifest["author"]),
        "entrypoint": str(manifest["entrypoint"]),
        "symbol": str(manifest["symbol"]),
        "capabilities": [str(c) for c in manifest.get("capabilities", [])],
        "artifact_sha256": manifest["artifact_sha256"],
        "payload_sha256": payload_sha256,
        "payload": payload,
        "signature_valid": True,
        "state": "installed",
        "imported_at": datetime.now(UTC).isoformat(),
    }
    packs = _load(layout)
    packs.append(entry)
    _save(layout, packs)
    return entry


def verify_payload_integrity(entry: dict[str, Any]) -> str:
    """运行前重验包代码哈希(防目录文件被改动);返回代码文本,不一致抛 ValueError。"""
    payload = str(entry.get("payload", ""))
    if hashlib.sha256(payload.encode("utf-8")).hexdigest() != entry.get("payload_sha256"):
        raise ValueError(f"策略包 {entry.get('pack_id')} 代码哈希与签名清单不一致,拒绝执行")
    return payload

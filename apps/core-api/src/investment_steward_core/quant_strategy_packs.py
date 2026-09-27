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
from typing import Any

from investment_steward_core.signing import canonical_bytes, verify_manifest_integrity
from investment_steward_core.storage.paths import StorageLayout

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
        # S1：保存**当时被签名的规范字节**与签名本身。运行期若只重算 payload 哈希，
        # 攻击者同时改 payload 与 payload_sha256 即可通过（两者都在同一个 JSON 里）；
        # 保存原始签名字节才能在执行前真正再验一次 Ed25519。
        "manifest_canonical": canonical_bytes(manifest).decode("utf-8"),
        "signature": manifest.get("signature"),
        "signature_valid": True,
        "state": "installed",
        "imported_at": datetime.now(UTC).isoformat(),
    }
    packs = _load(layout)
    packs.append(entry)
    _save(layout, packs)
    return entry


def verify_payload_integrity(entry: dict[str, Any], public_key_pem: str | None = None) -> str:
    """运行前重验包代码：哈希 + **Ed25519 签名**；不一致抛 ValueError。

    S1（用户视角路线图 2026-09-26）：原实现只在运行期比对 `payload` 与
    `payload_sha256`，**不再验签**（Ed25519 仅在入库时校验一次）。但这两个字段同在一份
    本地 JSON 里——同时改掉二者即可通过检查，于是「只有签名包才执行」这条承诺在执行
    路径上是断的。叠加 S1 修复前的沙箱逃逸，等于任意代码执行。

    现在执行前对入库时保存的规范字节重新验签：篡改 payload、篡改哈希、或替换整个条目
    都会被拒。`public_key_pem` 缺省时不验签（保留旧调用点的行为），但生产调用点必须传。
    """
    payload = str(entry.get("payload", ""))
    if hashlib.sha256(payload.encode("utf-8")).hexdigest() != entry.get("payload_sha256"):
        raise ValueError(f"策略包 {entry.get('pack_id')} 代码哈希与签名清单不一致,拒绝执行")

    if public_key_pem is not None:
        canonical = entry.get("manifest_canonical")
        signature = entry.get("signature")
        if not isinstance(canonical, str) or not canonical:
            raise ValueError(f"策略包 {entry.get('pack_id')} 缺少入库时的签名字节,无法重验签名,拒绝执行")
        if not isinstance(signature, str) or not signature:
            raise ValueError(f"策略包 {entry.get('pack_id')} 缺少签名,拒绝执行")
        # `canonical_bytes` 会剔除 SELF_REFERENTIAL_FIELDS（artifact_sha256 / signature），
        # 所以入库时保存的规范字节里本来就没有这两个字段——必须先按**条目里存的**值补回去,
        # 才能喂给 verify_manifest_integrity。补的是存储值而非重算值：任何一个被改都会验不过。
        manifest = json.loads(canonical)
        manifest["artifact_sha256"] = entry.get("artifact_sha256")
        manifest["signature"] = signature
        try:
            verify_manifest_integrity(manifest, public_key_pem)
        except ValueError as error:
            raise ValueError(
                f"策略包 {entry.get('pack_id')} 签名重验失败,拒绝执行:{error}"
            ) from error
        # 关键：签名只覆盖 `manifest_canonical`，条目里的 payload 字段本身**不在签名范围内**。
        # 只验签不比对的话，攻击者仍可改 payload + payload_sha256 而签名依旧通过。
        # 因此以签名内的声明为权威，逐字比对条目值。
        signed_payload = manifest.get("payload")
        if not isinstance(signed_payload, str) or signed_payload != payload:
            raise ValueError(
                f"策略包 {entry.get('pack_id')} 代码与签名清单不一致,拒绝执行"
            )
    return payload

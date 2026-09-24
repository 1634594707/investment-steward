"""插件制品签名与完整性校验（阶段 9）。

用 Ed25519 对插件 manifest 的确定性规范序列化做签名，并用发布者公钥做验签；
`artifact_sha256` 是同一份 canonical 字节的 SHA-256，用于完整校验。二者覆盖
的输入都排除 self-referential 的 `artifact_sha256` 与 `signature` 字段本身，
保证「加了签名/哈希后依旧能重算校验」，且篡改插件内容必然导致校验失败。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

# manifest 中属于「签名结果」而非「被签内容」的两个字段。
SELF_REFERENTIAL_FIELDS = ("artifact_sha256", "signature")

UNSIGNED_MARKER = "unsigned-development-fixture"


def canonical_bytes(manifest: dict[str, object]) -> bytes:
    """对插件 manifest 做确定性规范序列化（用于哈希与签名）。

    排除自我引用字段，保证加了签名/哈希后仍可重算校验；排序稳定、紧凑、UTF-8。
    """
    payload = {key: value for key, value in manifest.items() if key not in SELF_REFERENTIAL_FIELDS}
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def content_sha256(manifest: dict[str, object]) -> str:
    return hashlib.sha256(canonical_bytes(manifest)).hexdigest()


def sign_manifest(manifest: dict[str, object], private_key: ed25519.Ed25519PrivateKey) -> tuple[str, str]:
    """返回 (artifact_sha256_hex, signature_b64)。生成阶段用，不进入运行期。"""
    payload = canonical_bytes(manifest)
    digest = hashlib.sha256(payload).hexdigest()
    signature = base64.b64encode(private_key.sign(payload)).decode("ascii")
    return digest, signature


def _public_key_from_pem(pem: str) -> ed25519.Ed25519PublicKey:
    try:
        loaded = serialization.load_pem_public_key(pem.encode("utf-8"))
    except (ValueError, TypeError) as error:
        raise ValueError("无法解析插件发布者公钥（无效 PEM）") from error
    if not isinstance(loaded, ed25519.Ed25519PublicKey):
        raise ValueError("插件发布者公钥不是 Ed25519 类型")  # noqa: TRY004 - manifest 无效属业务校验
    return loaded


def verify_manifest_integrity(
    manifest: dict[str, object], public_key_pem: str
) -> None:
    """校验 manifest 的哈希与签名，任一失败抛 ValueError。

    - 声明的 `artifact_sha256` 与该内容 SHA-256 不一致 → 内容被篡改。
    - 未签名（占位标记）→ 拒绝，不当作可信制品。
    - 签名不是发布者私钥所签 → 拒绝。
    - 公钥不可解析或非法 → 拒绝。
    """
    public_key = _public_key_from_pem(public_key_pem)

    declared_hash = manifest.get("artifact_sha256")
    if not isinstance(declared_hash, str):
        raise ValueError("插件未声明 artifact_sha256")  # noqa: TRY004 - manifest 无效属业务校验

    payload = canonical_bytes(manifest)
    digest = hashlib.sha256(payload).hexdigest()
    if not secrets.compare_digest(digest.encode("ascii"), declared_hash.encode("ascii")):
        raise ValueError("插件制品 SHA-256 校验失败：内容与声明的哈希不一致")

    declared_signature = manifest.get("signature")
    if not isinstance(declared_signature, str) or declared_signature == UNSIGNED_MARKER:
        raise ValueError("插件未签名（unsigned-development-fixture），拒绝安装")

    try:
        signature = base64.b64decode(declared_signature.encode("ascii"))
    except Exception as error:
        raise ValueError("插件签名不是合法的 base64") from error

    try:
        public_key.verify(signature, payload)
    except InvalidSignature as error:
        raise ValueError("插件签名校验失败：签名与发布者公钥不匹配") from error
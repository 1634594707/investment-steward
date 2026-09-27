"""S1（用户视角路线图 2026-09-26）：执行前必须重验 Ed25519 签名，不能只比 payload 哈希。

改造前 `quant_strategy_packs.verify_payload_integrity` 在运行期只做一件事——比对
`payload` 与 `payload_sha256` 是否一致。Ed25519 验签只在**入库时**做过一次
（`import_pack`）。但这两个字段同在一份本地 JSON 里，**同时改掉二者即可通过检查**，
于是「只有签名包才会执行」这条承诺在执行路径上是断的；叠加沙箱逃逸修复前的
`os.spawnv` 缺口，等于任意代码执行。

本文件覆盖：合法包可执行、篡改 payload+哈希被拒、签名被换被拒、缺签名字节被拒、
端点确实把公钥传了进来。
"""

from __future__ import annotations

import hashlib
import inspect
import tempfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from investment_steward_core import signing
from investment_steward_core.quant_strategy_packs import import_pack, verify_payload_integrity
from investment_steward_core.storage.paths import StorageLayout
from test_quant_stages_packs import _pack_manifest, _pub_pem, _signed_manifest

PACK_CODE = "def run(bars):\n    return [0.0 for _ in bars]\n"


def _import_pack(private_key: ed25519.Ed25519PrivateKey, code: str = PACK_CODE) -> dict:
    """走真实 import 路径生成条目，保证字段与生产一致。

    用 `from_user_data(tmp_path)` 而不是 `from_environment()`——后者会读取本机的位置指针
    文件，可能把条目写到真实数据目录里去。
    """
    layout = StorageLayout.from_user_data(Path(tempfile.mkdtemp()))
    signed = _signed_manifest(_pack_manifest(code), private_key)
    return import_pack(layout, signed, public_key_pem=_pub_pem(private_key))


def test_legit_pack_passes_signature_recheck():
    key = ed25519.Ed25519PrivateKey.generate()
    entry = _import_pack(key)
    assert verify_payload_integrity(entry).strip().startswith("def run")
    assert verify_payload_integrity(entry, _pub_pem(key)).strip().startswith("def run")


def test_tampered_payload_and_hash_together_still_rejected():
    """核心回归：同时改 payload 与 payload_sha256——旧实现会放行，新实现必须拒绝。"""
    key = ed25519.Ed25519PrivateKey.generate()
    entry = _import_pack(key)

    evil = "def run(bars):\n    return [1.0 for _ in bars]\n"
    tampered = {
        **entry,
        "payload": evil,
        "payload_sha256": hashlib.sha256(evil.encode("utf-8")).hexdigest(),
    }

    # 前置条件：仅比哈希的旧路径确实会放行（说明这个攻击面真实存在）
    assert verify_payload_integrity(tampered) == evil, "前置条件失败：旧路径本应放行"

    with pytest.raises(ValueError) as excinfo:
        verify_payload_integrity(tampered, _pub_pem(key))
    assert "拒绝执行" in str(excinfo.value)


def test_swapped_signature_rejected():
    key = ed25519.Ed25519PrivateKey.generate()
    other = ed25519.Ed25519PrivateKey.generate()
    entry = _import_pack(key)
    # 格式合法、但由另一把私钥所签
    _, other_signature = signing.sign_manifest(_pack_manifest(PACK_CODE), other)
    with pytest.raises(ValueError):
        verify_payload_integrity({**entry, "signature": other_signature}, _pub_pem(key))


def test_legacy_entry_without_canonical_bytes_rejected():
    """老条目没有 manifest_canonical —— 必须拒绝，不能静默跳过验签。"""
    key = ed25519.Ed25519PrivateKey.generate()
    entry = _import_pack(key)
    legacy = {k: v for k, v in entry.items() if k not in ("manifest_canonical", "signature")}
    with pytest.raises(ValueError) as excinfo:
        verify_payload_integrity(legacy, _pub_pem(key))
    assert "拒绝执行" in str(excinfo.value)


def test_run_endpoint_passes_public_key():
    """端点必须把公钥传进重验函数，否则新校验形同虚设。"""
    from investment_steward_core.api import app as app_module

    source = inspect.getsource(app_module)
    assert "_pinned_public_key_pem(core.settings)" in source
    assert "verify_payload_integrity(" in source
    # 运行路径不能退回到「不传公钥」的旧写法
    assert "verify_payload_integrity(entry)" not in source

#!/usr/bin/env python3
"""插件清单签名 CLI（路线图 G2-4 / 阶段 9）。

对插件 manifest 的确定性 canonical 字节做 Ed25519 签名与 SHA-256 完整性哈希，就地写回
`artifact_sha256` 与 `signature`，可选再用发布者公钥验签。签名覆盖的输入排除这两个
self-referential 字段，故加了签名/哈希后仍可重算校验。

用法：
  # 生成（签名清单文件，就地写入 artifact_sha256 + signature）
  STEWARD_PLUGIN_SIGNING_KEY=<priv.pem> \
      python scripts/sign_plugins.py plugins/official/reading-library/manifest.json
  python scripts/sign_plugins.py --key <priv.pem> <manifest.json>

  # 校验（不打签名，仅用发布者公钥验签 + 哈希完整性）
  python scripts/sign_plugins.py --check <manifest.json>
  python scripts/sign_plugins.py --check --public-key <pub.pem> <manifest.json>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_CORE_SRC = Path(__file__).resolve().parents[1] / "apps" / "core-api" / "src"
sys.path.insert(0, str(_CORE_SRC))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from investment_steward_core.signing import (
    sign_manifest,
    verify_manifest_integrity,
)

_DEFAULT_PUB_KEY = (
    Path(__file__).resolve().parents[1]
    / "apps" / "core-api" / "keys" / "steward-plugin-publishing.pub.pem"
)


def _load_manifest(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def sign(path: Path, private_key: ed25519.Ed25519PrivateKey) -> None:
    manifest = _load_manifest(path)
    digest, signature = sign_manifest(manifest, private_key)
    signed = dict(manifest)
    signed["artifact_sha256"] = digest
    signed["signature"] = signature
    path.write_text(json.dumps(signed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"signed  {path}  sha256={digest[:16]}…")


def verify(path: Path, public_key_pem: str) -> None:
    manifest = _load_manifest(path)
    verify_manifest_integrity(manifest, public_key_pem)
    print(f"verified  {path}  （哈希 + 签名通过）")


def _resolve_private_key(cli_path: Path | None) -> ed25519.Ed25519PrivateKey:
    if cli_path is not None:
        path = cli_path
    else:
        env = os.environ.get("STEWARD_PLUGIN_SIGNING_KEY")
        if not env:
            print(
                "缺少签名私钥：请传 --key <priv.pem> 或设置 STEWARD_PLUGIN_SIGNING_KEY。",
                file=sys.stderr,
            )
            raise SystemExit(2)
        path = Path(env)
    loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(loaded, ed25519.Ed25519PrivateKey):
        raise TypeError("签名私钥不是 Ed25519 私钥")
    return loaded


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("manifest", type=Path, help="待签名/校验的 manifest.json 路径")
    parser.add_argument("--key", type=Path, default=None, help="签名私钥 PEM（Ed25519）；缺省读 STEWARD_PLUGIN_SIGNING_KEY")
    parser.add_argument(
        "--public-key",
        type=Path,
        default=_DEFAULT_PUB_KEY,
        help="校验用发布者公钥 PEM；缺省用 Core 固定锚",
    )
    parser.add_argument("--check", action="store_true", help="仅校验现有签名，不打签名")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    if args.check:
        verify(args.manifest, args.public_key.read_text(encoding="utf-8"))
        return 0
    private_key = _resolve_private_key(args.key)
    sign(args.manifest, private_key)
    verify(args.manifest, args.public_key.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
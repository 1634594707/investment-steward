"""签名官方插件 manifest（生成期工具，不入运行路径）。

读取**发布者私钥** `apps/core-api/keys/steward-plugin-publishing.pem`，对
`plugins/official/*/manifest.json` 计算 `artifact_sha256` 并在 `signature` 写入
base64 签名。之后 Core 安装插件时会用 `keys/steward-plugin-publishing.pub.pem` 验签。

[2026-09-16 修正] 本脚本原先读 `apps/core-api/.runtime-local/dev-plugin-signing-key.pem`，
但该私钥与发布者公钥**不配对**——用它签名会让**所有** manifest 验签失败。
实测依据：既有 `stock-tactics` 等产物本来验签通过，用 dev 私钥重签后全部失败；
而 `keys/steward-plugin-publishing.pem` 能逐字节复现既有签名。
现在默认读发布者私钥，并在签名前校验配对、写盘前逐条自检，避免把坏产物写进仓库。

用法：
    python scripts/sign_plugins.py                    # 用默认发布者私钥
    python scripts/sign_plugins.py --key <path.pem>   # 指定私钥（须与公钥配对）
    python scripts/sign_plugins.py --check            # 只验签、不改文件
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from cryptography.hazmat.primitives import serialization

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from investment_steward_core.signing import sign_manifest, verify_manifest_integrity

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
PLUGIN_ROOT = (REPO_ROOT / "plugins").resolve()
DEFAULT_PRIVATE_KEY_PATH = REPO_ROOT / "apps" / "core-api" / "keys" / "steward-plugin-publishing.pem"
PUBLIC_KEY_PATH = REPO_ROOT / "apps" / "core-api" / "keys" / "steward-plugin-publishing.pub.pem"


def _verify(manifest: dict[str, object], public_key_pem: str) -> str:
    """返回 "" 表示验签通过，否则返回失败原因。"""
    try:
        verify_manifest_integrity(manifest, public_key_pem)
        return ""
    except ValueError as error:
        return str(error)


def main() -> int:
    parser = argparse.ArgumentParser(description="签名官方插件 manifest")
    parser.add_argument("--key", default=str(DEFAULT_PRIVATE_KEY_PATH), help="发布者私钥路径")
    parser.add_argument("--check", action="store_true", help="只验签，不写文件")
    args = parser.parse_args()

    if not PUBLIC_KEY_PATH.exists():
        print(f"缺少发布者公钥：{PUBLIC_KEY_PATH}")
        return 1
    public_key_pem = PUBLIC_KEY_PATH.read_text(encoding="utf-8")

    manifests = sorted(PLUGIN_ROOT.rglob("manifest.json"))
    if not manifests:
        print(f"未找到任何 manifest：{PLUGIN_ROOT}")
        return 1

    if args.check:
        failures = 0
        for manifest_path in manifests:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            reason = _verify(manifest, public_key_pem)
            rel = manifest_path.relative_to(REPO_ROOT)
            if reason:
                print(f"FAIL {rel}：{reason}")
                failures += 1
            else:
                print(f"ok   {rel}")
        print(f"共 {len(manifests)} 个 manifest，失败 {failures} 个")
        return 1 if failures else 0

    key_path = pathlib.Path(args.key)
    if not key_path.exists():
        print(f"缺少发布者私钥：{key_path}")
        return 1
    private_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)

    # 先确认私钥与公钥配对：不配对就直接退出，绝不写出会验签失败的产物。
    derived = private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    if derived.strip() != public_key_pem.strip():
        print(f"私钥与发布者公钥不配对：{key_path}")
        print(f"  对应公钥不是：{PUBLIC_KEY_PATH}")
        print("  用它签名会让所有 manifest 验签失败，已中止（未写入任何文件）。")
        return 1

    signed = 0
    for manifest_path in manifests:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        digest, signature = sign_manifest(manifest, private_key)
        manifest["artifact_sha256"] = digest
        manifest["signature"] = signature
        # 写盘前自检：确保落盘内容一定验得通过。
        reason = _verify(manifest, public_key_pem)
        if reason:
            print(f"自检失败，未写入：{manifest_path.relative_to(REPO_ROOT)}：{reason}")
            return 1
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"signed {manifest_path.relative_to(REPO_ROOT)} ({digest[:12]}…)")
        signed += 1
    print(f"共签名 {signed} 个插件 manifest（已用公钥自检通过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""§6 GitHub 插件发布脚本：把注册表插件发布为 GitHub Release（manifest.json 资产）。

用法：
  python publish_plugin_release.py official.cn-market-data            # 发布 manifest 当前版本
  python publish_plugin_release.py official.cn-market-data --revoke   # 发布 REVOKE 撤回声明
  python publish_plugin_release.py official.cn-market-data --dry-run  # 只本地校验与预览

发布口径（§6）：
- 每个版本一个 GitHub Release，tag = `<plugin_id>-v<version>`；
- 资产 = 该插件目录的 manifest.json（已由发布者私钥签名）+ 插件代码 zip；
- Release body 以 `REVOKE` 开头即视为发布者撤回（relay 目录缓存据此标注 revoked）；
- 发布前本地验签：manifest 必须能通过 keys/ 公钥校验，否则拒绝发布（不发布坏签名）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MARKET_REPO = "1634594707/investment-steward-market"
REGISTRY_DIR = REPO_ROOT / "plugins"
PUB_KEY_FILE = REPO_ROOT / "apps" / "core-api" / "keys" / "steward-plugin-publishing.pub.pem"
sys.path.insert(0, str(REPO_ROOT / "apps" / "core-api" / "src"))

from investment_steward_core.signing import verify_manifest_integrity  # noqa: E402


def _find_plugin_dir(plugin_id: str) -> Path:
    for manifest_path in sorted(REGISTRY_DIR.rglob("manifest.json")):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if payload.get("plugin_id") == plugin_id:
            return manifest_path.parent
    raise SystemExit(f"注册表中找不到插件 {plugin_id}")


def _verify(manifest_path: Path) -> dict:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    verify_manifest_integrity(payload, PUB_KEY_FILE.read_text(encoding="utf-8"))
    return payload


def _pack_code(plugin_dir: Path, plugin_id: str, version: str, out_dir: Path) -> Path:
    zip_path = out_dir / f"{plugin_id}-{version}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(plugin_dir.rglob("*")):
            if file.is_file() and file.name != "manifest.json":
                zf.write(file, file.relative_to(plugin_dir).as_posix())
    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plugin_id")
    parser.add_argument("--revoke", action="store_true", help="发布 REVOKE 撤回声明")
    parser.add_argument("--dry-run", action="store_true", help="只本地校验与预览,不推送 GitHub")
    args = parser.parse_args()

    plugin_dir = _find_plugin_dir(args.plugin_id)
    manifest = _verify(plugin_dir / "manifest.json")
    plugin_id, version = manifest["plugin_id"], manifest["release_version"]
    tag = f"{plugin_id}-v{version}"
    notes = (f"REVOKE {plugin_id}@{version}:发布者撤回该版本,客户端应拒绝安装。"
             if args.revoke else f"{manifest.get('display_name', plugin_id)} v{version} 发布。")
    out_dir = REPO_ROOT / ".release-staging"
    out_dir.mkdir(exist_ok=True)
    zip_path = _pack_code(plugin_dir, plugin_id, version, out_dir)
    zip_sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    print(f"插件    : {plugin_id}@{version}")
    print(f"tag     : {tag}")
    print(f"代码包  : {zip_path} (sha256={zip_sha[:16]}…)")
    print(f"撤回    : {'是' if args.revoke else '否'}")
    if args.dry_run:
        print("dry-run:未推送。资产已就绪:", out_dir)
        return
    subprocess.run(["gh", "release", "create", tag, "--repo", MARKET_REPO,
                    "--title", f"{plugin_id} v{version}", "--notes", notes,
                    str(plugin_dir / "manifest.json"), str(zip_path)],
                   check=True)
    print("已发布:", f"https://github.com/{MARKET_REPO}/releases/tag/{tag}")


if __name__ == "__main__":
    main()

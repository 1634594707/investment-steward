"""端到端验证：宏观雷达（official.macro-radar）插件市场读取 / 下载（安装）链路。

模拟用户在插件市场里的完整操作序列：
  1. GET /plugins/catalog   —— 市场能否读到 macro-radar（含 mount / resolved_outputs）
  2. POST /install          —— 市场能否正常下载安装（签名校验通过、state=ENABLED）
  3. GET /slots             —— 安装后 app.macro 槽位占用是否正确
  4. GET /plugins/{id}/app  —— 独立应用页数据端点是否可路由
用临时 data_dir（不污染 .runtime-local 的真实库）。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from investment_steward_core.api import create_app
from investment_steward_core.config import CoreSettings

TOKEN = "verify-macro-radar"
PID = "official.macro-radar"

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    app = create_app(CoreSettings(session_token=TOKEN, data_dir=Path(td)))
    client = TestClient(app)
    headers = {"X-Core-Session-Token": TOKEN}
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}" + (f"  -> {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    # 1. 市场读取
    resp = client.get("/plugins/catalog", headers=headers)
    check("GET /plugins/catalog 返回 200", resp.status_code == 200, str(resp.status_code))
    entries = {e["manifest"]["plugin_id"]: e for e in resp.json()}
    check("市场共 7 个插件（6 旧 + 宏观雷达）", len(entries) == 7, f"实际 {len(entries)}: {sorted(entries)}")
    entry = entries.get(PID)
    check("catalog 能读到 macro-radar", entry is not None)
    if entry is None:
        raise SystemExit(1)
    manifest = entry["manifest"]
    check("display_name=宏观雷达", manifest["display_name"] == "宏观雷达", manifest["display_name"])
    check("mount=own_page（独立应用）", manifest["mount"] == "own_page", manifest["mount"])
    check("capabilities 三项", manifest["capabilities"] == ["read_public_evidence", "run_analysis", "submit_evidence"], str(manifest["capabilities"]))
    outputs = {o["slot"]: (o["page"], o["level"]) for o in entry["resolved_outputs"]}
    check("app.macro 解析到 macro 页 L3", outputs.get("app.macro") == ("macro", "L3"), str(outputs))
    check("research.board 解析到 research 页 L2", outputs.get("research.board") == ("research", "L2"), str(outputs))
    check("初始未安装（available）", entry["installation"] is None, str(entry["installation"]))

    # 2. 下载安装（安装时强制 Ed25519 验签）
    resp = client.post(f"/plugins/{PID}/install", headers=headers)
    check("POST install 返回 200", resp.status_code == 200, f"{resp.status_code} {resp.text[:120]}")
    inst = resp.json()
    check("安装后 state=ENABLED", inst.get("state") == "enabled", str(inst.get("state")))
    check("granted_capabilities 与 manifest 一致", inst.get("granted_capabilities") == ["read_public_evidence", "run_analysis", "submit_evidence"], str(inst.get("granted_capabilities")))

    # 3. 插槽占用（app.macro 应出现且 targeted=1）
    resp = client.get("/slots", headers=headers)
    slots = {row["slot"]: row for row in resp.json()}
    check("GET /slots 共 9 槽", len(slots) == 9, f"实际 {len(slots)}")
    macro_slot = slots.get("app.macro")
    check("app.macro 槽存在 page=macro", macro_slot is not None and macro_slot["page"] == "macro", str(macro_slot))
    check("app.macro targeted=1 used=1 cap=4", macro_slot is not None and (macro_slot["targeted"], macro_slot["used"], macro_slot["cap"]) == (1, 1, 4), str(macro_slot and (macro_slot["targeted"], macro_slot["used"], macro_slot["cap"])))

    # 4. 独立应用页端点
    resp = client.get(f"/plugins/{PID}/app", headers=headers)
    check("GET /plugins/macro-radar/app 返回 200", resp.status_code == 200, f"{resp.status_code} {resp.text[:120]}")

    # 5. 能力注册表含 read_public_evidence（安装预览的授权粒度可见）
    resp = client.get("/capabilities", headers=headers)
    caps = {c["capability_id"] for c in resp.json()}
    check("/capabilities 含 read_public_evidence", "read_public_evidence" in caps, str(sorted(caps)))

    # 6. 安装锁：同版本重复安装幂等成功；跨版本才 409（app.py L1062-1069 逻辑）
    resp = client.post(f"/plugins/{PID}/install", headers=headers)
    check("同版本重复安装幂等成功", resp.status_code == 200, str(resp.status_code))

    client.close()

    print()
    if failures:
        print(f"共 {len(failures)} 项失败: {failures}")
        raise SystemExit(1)
    print("全部通过：宏观雷达在插件市场的读取 / 下载（安装）链路 OK")

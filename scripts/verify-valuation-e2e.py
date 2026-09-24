"""估值证据真实端到端核验（不起服务、不碰系统凭据管理器）。

做法：
- TestClient + 临时数据目录，凭据后端换成文件库（`resolve_store` → DbCredentialStore），
  **绝不写系统 CredMan**（沿用项目既定的 file_client 模式）；
- 行情/新闻/公告/财报/估值一律走**真实网络**（本文件不 monkeypatch 任何 fetch_*）；
- 只把模型调用替换成捕获器，从而拿到真实拼装出的 prompt，核对 S4 派生段与 S5 估值段。

输出：.runtime-acceptance/valuation-e2e.log（同时打印到 stdout）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / ".runtime-acceptance" / "valuation-e2e.log"
LOG.parent.mkdir(parents=True, exist_ok=True)

lines: list[str] = []


def log(message: str = "") -> None:
    print(message)
    lines.append(message)


def main() -> int:
    from fastapi.testclient import TestClient

    from investment_steward_core.api import app as api_app
    from investment_steward_core.api import create_app
    from investment_steward_core.config import CoreSettings
    from investment_steward_core.credential_store import DbCredentialStore

    data_dir = ROOT / ".runtime-acceptance" / "valuation-e2e-data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # 关键：凭据走文件库，避免覆盖用户系统凭据管理器里的真实模型 key。
    api_app.resolve_store = lambda db: DbCredentialStore(db)  # type: ignore[assignment]

    token = "e2e-valuation-token"
    app = create_app(CoreSettings(session_token=token, data_dir=data_dir))
    client = TestClient(app)
    headers = {"X-Core-Session-Token": token}

    installed = client.post("/plugins/official.cn-market-data/install", headers=headers)
    log(f"[1] 安装 cn-market-data: {installed.status_code}")
    if installed.status_code != 200:
        log(f"    detail={installed.text[:200]}")

    client.put("/credentials/model_api_key", json={"secret": "sk-e2e-not-a-real-key"}, headers=headers)
    created = client.post(
        "/model-profiles",
        json={"name": "E2E", "base_url": "https://api.deepseek.com",
              "model": "deepseek-v4-flash", "credential_ref": "model_api_key"},
        headers=headers,
    ).json()
    client.post(f"/model-profiles/{created['profile_id']}/activate", headers=headers)
    log(f"[2] 模型方案就绪 profile_id={created['profile_id']}")

    symbol = sys.argv[1] if len(sys.argv) > 1 else "600519"
    captured: dict[str, str] = {}
    report_json = json.dumps({
        "title": f"{symbol}：估值与盈利质量的联合判断",
        "report": ("### 技术面\n量价结构见 [S1]\n### 消息面\nno news [S2]\n"
                   "### 基本面\n估值与 ROE 见 [S4][S5]\n### 综合判断\n结论 [S5]"),
        "citations": ["S1", "S4", "S5"],
        "limitations": ["公开来源摘要口径"],
        "confidence": "medium",
        "valuation": {"pe_ttm": "见 S5", "pe_percentile": "见 S5",
                      "peer_position": "见 S5", "verdict": "合理", "basis": "S5 分位与同业"},
    }, ensure_ascii=False)

    from investment_steward_core import model_client as mc

    def _capture(*args, **kwargs):
        captured["blob"] = json.dumps(
            [str(item) for item in args] + [str(value) for value in kwargs.values()], ensure_ascii=False
        )
        return {"choices": [{"message": {"content": report_json}}]}

    mc._post_json = _capture  # type: ignore[assignment]

    log(f"[3] 发起真实取数研报（{symbol}），行情/新闻/公告/财报/估值均走真实网络 ...")
    response = client.post(
        "/evidence/stock-research-report",
        json={"symbol": symbol, "question": "当前估值贵不贵"},
        headers=headers,
    )
    body = response.json()
    log(f"    status={response.status_code} ok={body.get('ok')} stage={body.get('stage')}")
    log(f"    source_keys={body.get('source_keys')}")
    log(f"    available_citations={body.get('available_citations')}")
    log(f"    source_errors={body.get('source_errors')}")
    log(f"    valuation={body.get('valuation')}")
    log(f"    confidence={body.get('confidence')} policy={body.get('prompt_policy_version')}")

    blob = captured.get("blob", "")
    log("")
    log("=== 提示词中的证据段（真实数据）===")
    for marker in ("[S4] 财务摘要", "【派生指标", "[S5] 估值快照"):
        index = blob.find(marker)
        if index < 0:
            log(f"!! 未在 prompt 中找到：{marker}")
            continue
        snippet = blob[index:index + 900].replace("\\n", "\n")
        log(f"--- {marker} ---")
        log(snippet)
        log("")

    checks = {
        "prompt 含 [S5] 估值快照": "[S5] 估值快照" in blob,
        "prompt 含派生指标（档 A）": "【派生指标" in blob,
        "prompt 含 ROE(TTM)": "ROE(TTM)" in blob,
        "prompt 含历史分位（档 C）": "历史分位" in blob,
        "prompt 含同业中位数（档 C）": "同业" in blob,
        "S5 进入可引用来源": "S5" in (body.get("available_citations") or []),
        "响应含估值块": bool(body.get("valuation", {}).get("verdict")),
        "提示词策略版本 2.1": body.get("prompt_policy_version") == "2.1",
    }
    log("=== 断言 ===")
    failed = 0
    for name, ok in checks.items():
        log(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        failed += 0 if ok else 1

    LOG.write_text("\n".join(lines), encoding="utf-8")
    log("")
    log(f"日志已写入 {LOG}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

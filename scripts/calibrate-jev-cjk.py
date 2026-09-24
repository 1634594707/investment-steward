"""R6｜Jev 中文（CJK）精度标定：把「官方英文范例阈值」换成有中文样本支撑的阈值。

**为什么需要它**（《Jev 决策模型接入任务路线图 2026-09-21》R6）：官方 `/models` 声明英文是训练
主语言、精度最佳，CJK「handled but not equally well」。本项目 state 与题目几乎全中文，而
JV03／JV04 目前照搬了官方英文范例的三处阈值：

1. `JEV_NOUL_YES = 0.8` / `JEV_NOUL_NO = 0.2`（JV03 假突破风险、JV04 编造风险共用）；
2. JV03 认同度（`choice`）的 confidence 回退线（官方 intent-routing 原型用 0.5）；
3. `JEV_PARTIAL_DOWNGRADES`（「部分支撑」是否参与降级；标定达标才翻 `True`）。

本脚本不猜阈值：用**真实中文材料构造带标注的探针**跑一遍 Jev，输出分布与一致率，再据数据给出
建议阈值。**R6 完成前不得宣告 P0 收口、不得开工 P1。**

## 样本来源与标注强度（必须如实区分，不能把弱标注说成人工结论）

**A. 受控探针（强标注 → 标定 `noul` 阈值）**
取材：真实研报的 `claims[].text`（含数字的判断原句）+ 该研报正文的窗口。
- `passage_has_facts`：窗口**含**该判断的全部显著数字 → 期望「未编造」（`noul` 低）；
- `passage_facts_altered`：同一窗口，把该判断的数字**替换成别的值** → 期望「编造」（`noul` 高）。

标注由构造决定、确定无疑，因此是**强标注**。代价：窗口取自研报正文而非真实取数原文
（研报 payload 不落来源正文，只有 `source_keys`/`source_meta`），所以它测的是「模型能否在中文
文本里认出事实的有无」，这正是 R6 要回答的问题；但它**不**能替代「真实取数原文 vs 判断」的端到端抽检。

**B. 真实配对（弱标注 → 标定 `choice` 一致率与 confidence 地板）**
- `support`：同研报、含该判断数字的窗口（主题与事实都对得上）；
- `unsupport`：同研报、**另一小节**且不含该判断任何事实数字的窗口（同域不同事实，推不出该判断）；
- `irrelevant`：**另一份研报**的窗口（不同标的，拿错证据）。

三类都是「主题/事实是否对得上」的**弱标注**——同标的同小节不等于语义支持。人工可用 `--labels`
逐条覆盖，覆盖后以人工为准。

**C. JV03 战法复核样本**：来自 `tactics_ai_reviews` 表。本机当前 0 行 → 本组**无样本**，脚本
如实报告「该组阈值本次未标定」，不伪造；JV03 组还需要人工标签（`--labels`）才有期望值。

## 隐私与出网

样本会出网到 TypeSafe（美国托管、默认非零留存；本项目已决定**不申请 ZDR**，见《契约》§E2），
因此只送**判断原句 + 受限长度的正文窗口**，绝不送整份研报。本脚本是离线工具：不写任何配置、
不落 `model_calls`（未注入 recorder，故 `emit_model_call_record` 是空操作）。

## 用法（在仓库根执行）

    # 1) 抽样本（离线，只读本机库）
    .venv/Scripts/python.exe scripts/calibrate-jev-cjk.py build

    # 2) 出网跑样本（需 Jev 密钥；两种取密钥方式任选）
    STEWARD_JEV_API_KEY=sk-... .venv/Scripts/python.exe scripts/calibrate-jev-cjk.py run
    .venv/Scripts/python.exe scripts/calibrate-jev-cjk.py run --credential-ref jev_api_key

    # 3) 出报告（离线）
    .venv/Scripts/python.exe scripts/calibrate-jev-cjk.py analyze

    # 离线自检：合成应答跑通 build→analyze 全链路（不出网、不需要密钥）
    .venv/Scripts/python.exe scripts/calibrate-jev-cjk.py dry-run

产物（`docs/evidence/`）：
- `jev-cjk-samples-<date>.json`     样本集（含标注、取材出处与窗口）
- `jev-cjk-raw-<date>.json`         Jev 原始应答（含响应返回的真实版本号与 usage）
- `jev-cjk-calibration-<date>.json` 建议阈值（机器可读，用于回写《契约》§A）
- `jev-cjk-calibration-<date>.md`   标定报告（分布、阈值扫描表与结论）

`--labels` 人工覆盖文件格式（键 = `sample_id`，值 = 期望标签；`null` 表示该条不参与统计）：

    {"g2-support-000060-C2": "support", "g3-tactics-000060-1": "agree"}
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import random
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_CORE_SRC = ROOT / "apps" / "core-api" / "src"
if str(_CORE_SRC) not in sys.path:
    sys.path.insert(0, str(_CORE_SRC))

EVIDENCE_DIR = ROOT / "docs" / "evidence"
DEFAULT_DB = ROOT / ".local-data" / "steward.sqlite3"
DEFAULT_BASE_URL = "https://api.typesafe.ai/v1"
DEFAULT_MODEL = "jev-latest"

#: 固定种子：样本抽样与 dry-run 合成应答都必须可复现（同一命令两次跑出同一结果）。
SEED = 20260921

TODAY = datetime.now(UTC).astimezone().date().isoformat()

# —— 组与期望标签 ——
GROUP_INVENTED = "claim_invented"
GROUP_SUPPORT = "claim_support"
GROUP_TACTICS = "tactics_review"

EXPECT_INVENTED_FALSE = "invented_false"
EXPECT_INVENTED_TRUE = "invented_true"
EXPECT_SUPPORT = "support"
EXPECT_UNSUPPORT = "unsupport"
EXPECT_IRRELEVANT = "irrelevant"

#: 编造风险组：期望「编造」= 正类（`noul` 应高）。
INVENTED_POSITIVE = EXPECT_INVENTED_TRUE

#: 窗口宽度（字符）。窄到够读、宽到含得下该判断的全部数字。
_WINDOW_CHARS = 420
#: 窗口滑动步长（找「不含某组数字」的窗口时用）。
_WINDOW_STRIDE = 40

# 数字令牌：带小数、带百分号，或 ≥2 位整数才算「显著事实」——单个数字（如「5 个」）太常见，
# 拿它判「有没有编造」会把噪声当信号。
_NUMBER_TOKEN = re.compile(r"-?\d+(?:\.\d+)?%?")
_MIN_SIGNIFICANT_DIGITS = 2
# 日期类令牌（19xx/20xx 四位年、`2026H1`、`2026Q1`）：正文里到处都是，判「窗口不含某事实」
# 时若把它们算进去会退化成找不到窗口，所以单独归类。
_YEAR_LIKE = re.compile(r"^(?:19|20)\d{2}$")

#: 「现行值」的**兜底**（取不到代码时用它，并在报告里标明是兜底）。
_BASELINE_FALLBACK = {"JEV_NOUL_YES": 0.8, "JEV_NOUL_NO": 0.2, "JEV_PARTIAL_DOWNGRADES": False,
                      "AGREEMENT_CONFIDENCE_FLOOR": 0.5}


def current_constants() -> tuple[dict[str, Any], str]:
    """读**代码里真实生效**的阈值，而不是写死一份「官方英文范例值」。

    为什么必须读代码：报告那张「现行值 vs 标定建议」表，一旦把现行值写死，R6 回写过一次之后
    就会**永远停在旧值**（实测：脚本写死 0.8/0.2/False，而代码已是 0.85/0.3/True），
    读者会以为还要再改一次。宁可 import 失败时退回兜底并**明示**，也不要悄悄给出错的基线。
    """
    try:
        from investment_steward_core import jev_client, report_quality

        return (
            {
                "JEV_NOUL_YES": float(jev_client.JEV_NOUL_YES),
                "JEV_NOUL_NO": float(jev_client.JEV_NOUL_NO),
                "JEV_PARTIAL_DOWNGRADES": bool(report_quality.JEV_PARTIAL_DOWNGRADES),
                "AGREEMENT_CONFIDENCE_FLOOR": _BASELINE_FALLBACK["AGREEMENT_CONFIDENCE_FLOOR"],
            },
            "取自代码（真实生效值）",
        )
    except Exception:  # noqa: BLE001 - 离线/未安装时退回兜底，但必须在报告里标明
        return dict(_BASELINE_FALLBACK), "**兜底**（未能 import 核心包，非代码真实值）"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


class _Log:
    """同时打印到 stdout 并留一份文本，供报告开头引用。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, message: str = "") -> None:
        print(message)
        self.lines.append(message)

    def text(self) -> str:
        return "\n".join(self.lines)


log = _Log()


def _dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _latest(pattern: str, fallback: Path) -> Path:
    """默认路径不存在时，取 `docs/evidence` 下最近一次同名前缀产物（跨日重跑更顺手）。

    `pattern` 里**必须**带 `[0-9]`：否则 `jev-cjk-raw-*.json` 会把 `jev-cjk-raw-dryrun-<date>.json`
    也匹配进来，一次真实 run 就会覆盖掉 dry-run 的留痕。
    """
    if fallback.exists():
        return fallback
    candidates = sorted(EVIDENCE_DIR.glob(pattern))
    return candidates[-1] if candidates else fallback


def _read_only(db_path: Path) -> sqlite3.Connection:
    """只读打开本机库。

    刻意**不用** `storage.database.Database`：那会跑 `_migrate()`，标定脚本不该有机会写用户库。
    """
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def significant_numbers(text: str) -> list[str]:
    """抽取「显著数字」：带小数、带百分号，或 ≥2 位整数；去重且保持出现顺序。"""
    out: list[str] = []
    for match in _NUMBER_TOKEN.finditer(text or ""):
        token = match.group(0)
        digits = token.lstrip("-").rstrip("%").replace(".", "")
        significant = "." in token or token.endswith("%") or len(digits) >= _MIN_SIGNIFICANT_DIGITS
        if significant and token not in out:
            out.append(token)
    return out


def _fact_numbers(text: str) -> list[str]:
    """「事实类」数字：显著数字里剔除纯年份（判「窗口不含该事实」时只看这些）。"""
    return [token for token in significant_numbers(text) if not _YEAR_LIKE.match(token.lstrip("-"))]


def _sections(body: str) -> list[tuple[str, str]]:
    """按 `### 小节名` 切分研报正文 → `[(小节名, 正文)]`；没有标题时整篇算一段。"""
    if "### " not in body:
        return [("（全文）", body)]
    parts = re.split(r"^###\s*", body, flags=re.MULTILINE)
    out: list[tuple[str, str]] = []
    for part in parts[1:]:
        head, _, rest = part.partition("\n")
        out.append((head.strip() or "（未命名）", rest.strip()))
    return out or [("（全文）", body)]


def _snap(body: str, start: int, end: int, *, look: int = 90) -> tuple[int, int]:
    """把窗口边界对齐到句子/换行——否则会把 `MA5` 切成 `A5` 这种半截词送给模型。

    只**向外**扩（start 前移、end 后移），所以不会把已确认在窗口内的 token 挤出去。
    """
    head = max(
        body.rfind("。", max(0, start - look), start),
        body.rfind("\n", max(0, start - look), start),
    )
    if head != -1:
        start = head + 1
    tails = [
        position
        for position in (body.find("。", end, end + look), body.find("\n", end, end + look))
        if position != -1
    ]
    if tails:
        end = min(tails) + 1
    return start, min(end, len(body))


def _window_with_all(body: str, tokens: list[str], width: int = _WINDOW_CHARS) -> str:
    """取一个**同时包含全部 tokens** 的窗口（以最长的 token 为锚点向外扩，再对齐到句边界）。"""
    if not tokens:
        return ""
    for token in sorted(tokens, key=len, reverse=True):
        position = body.find(token)
        while position != -1:
            start = max(0, position - width // 2)
            end = min(len(body), start + width)
            start = max(0, end - width)
            left, right = _snap(body, start, end)
            window = body[left:right].strip()
            if all(item in window for item in tokens):
                return window
            position = body.find(token, position + 1)
    return ""


def _window_without(body: str, tokens: list[str], width: int = _WINDOW_CHARS) -> str:
    """取一个**不含任何 tokens** 的窗口，且首尾对齐到句边界（读起来像一段完整的话）。

    对齐会**扩大**窗口，所以必须在扩完之后再查一遍 token——否则对齐可能把事实又捞进来。
    """
    if not tokens:
        return body[:width].strip()
    for start in range(0, max(len(body) - width + 1, 1), _WINDOW_STRIDE):
        left, right = _snap(body, start, start + width)
        window = body[left:right].strip()
        if len(window) < 80:
            continue
        if any(token in window for token in tokens):
            continue
        return window
    return ""


def _replace_tokens(text: str, tokens: list[str], rng: random.Random) -> str:
    """把窗口里出现的 tokens **全部替换成别的值**。

    用正则一次性扫描数字令牌后再替换，**不用 `str.replace`**——`str.replace("6.42", …)` 会把
    `16.42` 也改掉（中文文本没有词边界，子串替换必然出错）。同一 token 在窗口里换成同一个值，
    读起来才像一份自洽的（但错的）证据。
    """
    wanted = set(tokens)
    seen: dict[str, str] = {}
    for token in tokens:
        if token in text and token not in seen:
            candidate = _mutate(token, len(seen), rng)
            attempts = 0
            while (candidate == token or candidate in wanted) and attempts < 8:
                attempts += 1
                candidate = _mutate(token, len(seen) + attempts, rng)
            seen[token] = candidate

    def _substitute(match: re.Match[str]) -> str:
        token = match.group(0)
        return seen.get(token, token)

    return _NUMBER_TOKEN.sub(_substitute, text)


def _mutate(token: str, salt: int, rng: random.Random) -> str:
    """把数字换成一个**结构相似但不同**的值（保留百分号与正负号、小数位数；年份换成相邻年份）。"""
    percent = token.endswith("%")
    sign = "-" if token.startswith("-") else ""
    body = token.lstrip("-").rstrip("%")
    if _YEAR_LIKE.match(body):
        # 年份不能乘个系数变成 3538 这种不存在的年份——换个相邻年份即可，同样让原事实「找不到」。
        return f"{sign}{int(body) + rng.choice((-3, -2, -1, 1, 2, 3))}"
    try:
        value = float(body)
    except ValueError:
        return f"{sign}{rng.randint(11, 97)}" + ("%" if percent else "")
    decimals = len(body.split(".")[1]) if "." in body else 0
    factor = 1.6 + 0.05 * salt + rng.random() * 0.3
    mutated = abs(value) * factor + 3.1 + rng.random() * 7
    text = f"{mutated:.{decimals}f}" if decimals else str(round(mutated))
    return f"{sign}{text}" + ("%" if percent else "")


def _snippet(text: str, limit: int = 90) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


# ---------------------------------------------------------------------------
# 样本构造
# ---------------------------------------------------------------------------


def _load_reports(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    query = "select report_id, payload, created_at from ai_research_reports order by created_at"
    for row in connection.execute(query):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        payload["_report_id"] = row["report_id"]
        payload["_created_at"] = row["created_at"]
        reports.append(payload)
    return reports


def _candidate_claims(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """挑出「正文里能找到自己全部显著数字」的判断——这是构造探针的前提。

    `ordinal` 是判断在该研报 `claims` 数组里的序号：同一份研报里 `claim_id` 会重复
    （多批 claims 各自从 C1 编号）甚至为空，只靠 `claim_id` 拼样本 id 会撞车。
    """
    out: list[dict[str, Any]] = []
    for report in reports:
        body = str(report.get("report") or "")
        if len(body) < _WINDOW_CHARS:
            continue
        for ordinal, claim in enumerate(report.get("claims") or [], start=1):
            if not isinstance(claim, dict):
                continue
            text = str(claim.get("text") or "").strip()
            tokens = significant_numbers(text)
            if not tokens:
                continue
            window = _window_with_all(body, tokens)
            if not window:
                continue
            out.append(
                {
                    "report": report,
                    "claim": claim,
                    "ordinal": ordinal,
                    "text": text,
                    "tokens": tokens,
                    "fact_tokens": _fact_numbers(text),
                    "window": window,
                    "symbol": str(report.get("symbol") or ""),
                    "name": str(report.get("name") or report.get("subject") or ""),
                }
            )
    return out


def _claim_slug(item: dict[str, Any]) -> str:
    """样本 id 里的「标的+判断」段：`claim_id` 缺失或重复时靠 `ordinal` 兜底。"""
    claim_id = str(item["claim"].get("claim_id") or "").strip() or "C?"
    return f"{item['symbol'] or 'x'}-{claim_id}-{item['ordinal']}"


def _sources_for(claim: dict[str, Any]) -> list[str]:
    """该判断引用的来源 id（探针沿用原引用，保证与真实 JV04 请求同形）。"""
    cited = [str(item) for item in (claim.get("sources") or []) if str(item).strip()]
    return cited or ["S4"]


def _build_invented_samples(candidates: list[dict[str, Any]], per_class: int, rng: random.Random) -> list[dict[str, Any]]:
    """A 组：受控探针（强标注）。每取一条，同时产出「事实在」「事实被改」两条配对样本。"""
    pool = [item for item in candidates if item["fact_tokens"]]
    rng.shuffle(pool)
    samples: list[dict[str, Any]] = []
    for item in pool:
        if len([s for s in samples if s["expected"] == EXPECT_INVENTED_FALSE]) >= per_class:
            break
        altered = _replace_tokens(item["window"], item["tokens"], rng)
        if any(token in altered for token in item["tokens"]):
            continue  # 替换没干净 → 标签不成立，宁可不要这条样本
        if altered.strip() == item["window"].strip():
            continue
        base = f"g1-{_claim_slug(item)}"
        sources = _sources_for(item["claim"])
        for suffix, passage, expected, kind in (
            ("has", item["window"], EXPECT_INVENTED_FALSE, "passage_has_facts"),
            ("alt", altered, EXPECT_INVENTED_TRUE, "passage_facts_altered"),
        ):
            samples.append(
                {
                    "sample_id": f"{base}-{suffix}",
                    "group": GROUP_INVENTED,
                    "expected": expected,
                    "probe_kind": kind,
                    "claim_text": item["text"],
                    "source_texts": {source: passage for source in sources},
                    "origin": {
                        "report_id": item["report"].get("_report_id"),
                        "symbol": item["symbol"],
                        "claim_id": item["claim"].get("claim_id"),
                        "ordinal": item["ordinal"],
                        "passage_chars": len(passage),
                    },
                }
            )
    return samples


def _build_support_samples(
    candidates: list[dict[str, Any]], reports: list[dict[str, Any]], per_class: int, rng: random.Random
) -> list[dict[str, Any]]:
    """B 组：真实配对（弱标注）。三类各 `per_class` 条。"""
    samples: list[dict[str, Any]] = []
    counts = {EXPECT_SUPPORT: 0, EXPECT_UNSUPPORT: 0, EXPECT_IRRELEVANT: 0}

    def add(item: dict[str, Any], passage: str, expected: str, kind: str, extra: dict[str, Any]) -> None:
        sources = _sources_for(item["claim"])
        samples.append(
            {
                "sample_id": f"g2-{kind}-{_claim_slug(item)}",
                "group": GROUP_SUPPORT,
                "expected": expected,
                "probe_kind": kind,
                "claim_text": item["text"],
                "source_texts": {source: passage for source in sources},
                "origin": {
                    "report_id": item["report"].get("_report_id"),
                    "symbol": item["symbol"],
                    "claim_id": item["claim"].get("claim_id"),
                    "ordinal": item["ordinal"],
                    "passage_chars": len(passage),
                    **extra,
                },
            }
        )
        counts[expected] += 1

    pool = [item for item in candidates if item["fact_tokens"]]
    rng.shuffle(pool)
    for item in pool:
        body = str(item["report"].get("report") or "")
        facts_section = _section_of(body, item["tokens"])
        if counts[EXPECT_SUPPORT] < per_class:
            add(item, item["window"], EXPECT_SUPPORT, "same_section_facts", {"section": facts_section})
        if counts[EXPECT_UNSUPPORT] < per_class:
            other = _other_section_window(body, item["fact_tokens"], facts_section)
            if other:
                add(
                    item,
                    other[1],
                    EXPECT_UNSUPPORT,
                    "other_section_no_facts",
                    {"section": other[0], "facts_section": facts_section},
                )
        if counts[EXPECT_IRRELEVANT] < per_class:
            foreign = _foreign_window(reports, item, rng)
            if foreign:
                add(
                    item,
                    foreign[1],
                    EXPECT_IRRELEVANT,
                    "foreign_report",
                    {"foreign_symbol": foreign[0]},
                )
        if all(counts[key] >= per_class for key in counts):
            break
    return samples


def _section_of(body: str, tokens: list[str]) -> str:
    """该判断的事实落在哪个小节（取第一个同时含全部 tokens 的小节）。"""
    for name, text in _sections(body):
        if all(token in text for token in tokens):
            return name
    return ""


def _other_section_window(body: str, fact_tokens: list[str], facts_section: str) -> tuple[str, str] | None:
    """同研报、**另一小节**、且不含该判断任何事实数字的窗口（同域不同事实）。"""
    if not fact_tokens:
        return None
    for name, text in _sections(body):
        if name == facts_section or len(text) < 120:
            continue
        if any(token in text for token in fact_tokens):
            continue
        window = _window_without(text, fact_tokens)
        if window:
            return name, window
    return None


def _foreign_window(
    reports: list[dict[str, Any]], item: dict[str, Any], rng: random.Random
) -> tuple[str, str] | None:
    """另一份**不同标的**研报的窗口（拿错证据）。"""
    others = [
        report
        for report in reports
        if str(report.get("symbol") or "") != item["symbol"]
        and len(str(report.get("report") or "")) >= _WINDOW_CHARS
    ]
    if not others:
        return None
    report = others[rng.randrange(len(others))]
    body = str(report.get("report") or "")
    window = _window_without(body, item["tokens"])
    if not window:
        return None
    return str(report.get("symbol") or ""), window


def _build_tactics_samples(connection: sqlite3.Connection, limit: int) -> tuple[list[dict[str, Any]], str]:
    """C 组：JV03 战法复核。需要库里有复核记录**且** payload 留有重建 state 的输入。

    当前本机 `tactics_ai_reviews` 为 0 行 → 返回空列表 + 如实说明，不伪造样本。
    """
    try:
        rows = list(
            connection.execute(
                "select review_id, symbol, name, rule_score, payload, created_at "
                "from tactics_ai_reviews order by created_at desc"
            )
        )
    except sqlite3.Error as error:
        return [], f"读取 tactics_ai_reviews 失败：{error}"
    if not rows:
        return [], "本机 tactics_ai_reviews 为 0 行：JV03 组无样本，该组阈值本次无法标定。"

    from investment_steward_core import tactics_ai

    samples: list[dict[str, Any]] = []
    unbuildable = 0
    for row in rows:
        if len(samples) >= limit:
            break
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            unbuildable += 1
            continue
        rule_score = payload.get("rule_score")
        if rule_score is None and row["rule_score"]:
            try:
                rule_score = json.loads(row["rule_score"])
            except (TypeError, ValueError):
                rule_score = None
        indicators = payload.get("indicators")
        bars = payload.get("bars")
        if not (isinstance(rule_score, dict) and isinstance(indicators, dict) and isinstance(bars, list)):
            unbuildable += 1
            continue
        state = tactics_ai.jev_state(
            symbol=str(row["symbol"] or ""),
            name=str(row["name"] or ""),
            rule_score=rule_score,
            indicators=indicators,
            bars=bars,
        )
        samples.append(
            {
                "sample_id": f"g3-tactics-{row['symbol']}-{str(row['review_id'])[:8]}",
                "group": GROUP_TACTICS,
                "expected": "",  # 需人工标签（--labels）才有期望值
                "probe_kind": "tactics_review",
                "state": state,
                "questions": tactics_ai.jev_questions(),
                "origin": {
                    "review_id": row["review_id"],
                    "symbol": row["symbol"],
                    "created_at": row["created_at"],
                },
            }
        )
    note = (
        f"tactics_ai_reviews 共 {len(rows)} 行，可重建 {len(samples)} 条、"
        f"{unbuildable} 条缺 rule_score/indicators/bars（重建不了）。"
    )
    if not samples:
        note += " JV03 组本次无可用样本，该组阈值无法标定。"
    return samples, note


def _dedupe(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保证 `sample_id` 唯一——它是把应答对回标注的唯一键，撞车会让统计张冠李戴。

    `_claim_slug` 已带上 `ordinal` 兜底，这里再兜一层：真撞上就加序号后缀，绝不静默覆盖。
    """
    seen: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for sample in samples:
        sample_id = sample["sample_id"]
        if sample_id in seen:
            seen[sample_id] += 1
            sample = dict(sample)
            sample["sample_id"] = f"{sample_id}~{seen[sample_id]}"
        else:
            seen[sample_id] = 1
        out.append(sample)
    return out


def build_samples(db_path: Path, per_class: int, tactics_limit: int) -> dict[str, Any]:
    rng = random.Random(SEED)
    connection = _read_only(db_path)
    try:
        reports = _load_reports(connection)
        candidates = _candidate_claims(reports)
        tactics, tactics_note = _build_tactics_samples(connection, tactics_limit)
    finally:
        connection.close()

    invented = _build_invented_samples(candidates, per_class, rng)
    support = _build_support_samples(candidates, reports, per_class, rng)
    samples = _dedupe(invented + support + tactics)

    log(f"[build] 研报 {len(reports)} 份；可构造探针的判断 {len(candidates)} 条")
    log(f"[build] A 受控探针（强标注） {len(invented)} 条")
    log(f"[build] B 真实配对（弱标注） {len(support)} 条")
    log(f"[build] C 战法复核        {len(tactics)} 条 —— {tactics_note}")
    log(f"[build] 合计 {len(samples)} 条")

    return {
        "generated_at": TODAY,
        "seed": SEED,
        "db": db_path.as_posix(),
        "per_class": per_class,
        "window_chars": _WINDOW_CHARS,
        "notes": {
            "tactics": tactics_note,
            "label_strength": (
                "A 组为构造出的强标注（事实在/事实被改，确定无疑）；B 组为「主题与事实是否对得上」"
                "的弱标注，可用 --labels 逐条覆盖；C 组需人工标签。"
            ),
            "passage_provenance": (
                "窗口取自研报正文（研报 payload 不落来源正文），不是真实取数原文——"
                "测的是中文文本里「事实有无」的判别力，不能替代端到端抽检。"
            ),
        },
        "samples": samples,
    }


# ---------------------------------------------------------------------------
# 出网跑样本
# ---------------------------------------------------------------------------


def _static_store(secret: str) -> Any:
    """最小凭据库：直接把明文密钥包成 `resolve_credential` 认的形状（脚本用，不落库）。"""

    class _Static:
        def get(self, key_id: str) -> str | None:
            return secret

        def list_records(self) -> list[Any]:
            return []

    return _Static()


def _resolve_store(db_path: Path, credential_ref: str) -> Any:
    """走应用同一条凭据解析链（Windows 凭据管理器 / 本机库），避免脚本自造一套。"""
    from investment_steward_core.credential_store import resolve_store
    from investment_steward_core.storage.database import Database

    return resolve_store(Database(db_path))


def _questions_for(sample: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, str]]:
    """按组构造 (state, questions, 题号映射)。**一律复用生产构造函数**，不另写一份题面。"""
    from investment_steward_core import report_quality

    if sample.get("state") and sample.get("questions"):
        questions = sample["questions"]
        mapping = {"reliability": "reliability", "agreement": "agreement", "fake_breakout": "fake_breakout"}
        return str(sample["state"]), questions, mapping

    claim = {
        "claim_id": str(sample["origin"].get("claim_id") or "C1"),
        "text": sample["claim_text"],
        "sources": list(sample["source_texts"]),
    }
    plan = report_quality.jev_claim_state([claim], sample["source_texts"])
    evaluated = plan["evaluated"]
    if not evaluated:
        raise ValueError(f"样本 {sample['sample_id']} 没能进 state（{plan['skipped']}）")
    questions = report_quality.jev_claim_questions(evaluated)
    mapping = {
        "support": f"c1_{report_quality.JEV_CLAIM_SUPPORT_QUESTION}",
        "invented": f"c1_{report_quality.JEV_CLAIM_INVENTED_QUESTION}",
    }
    return str(plan["state"]), questions, mapping


def run_samples(
    samples: list[dict[str, Any]],
    *,
    db_path: Path,
    api_key: str,
    credential_ref: str,
    base_url: str,
    model: str,
    limit: int,
    only_group: str,
) -> dict[str, Any]:
    from investment_steward_core import jev_client

    store = _static_store(api_key) if api_key else _resolve_store(db_path, credential_ref)
    config = jev_client.JevConfig(
        base_url=base_url,
        model=model,
        credential_ref=credential_ref or "jev_api_key",
        timeout_secs=60.0,
        enabled=True,
    )

    selected = [item for item in samples if not only_group or item["group"] == only_group][:limit]
    log(f"[run] 端点 {config.resolved_base_url}｜模型 {config.resolved_model}｜样本 {len(selected)} 条")

    records: list[dict[str, Any]] = []
    for index, sample in enumerate(selected, start=1):
        entry: dict[str, Any] = {"sample_id": sample["sample_id"], "group": sample["group"]}
        try:
            state, questions, mapping = _questions_for(sample)
            reply = jev_client.call_jev(
                config,
                store,
                state,
                questions,
                purpose="jev:calibration",
                access_enabled=True,
            )
            entry.update(
                {
                    "model": reply.model,
                    "requested_model": reply.requested_model,
                    "latency_ms": reply.latency_ms,
                    "input_tokens": reply.input_tokens,
                    "output_tokens": reply.output_tokens,
                    "state_chars": len(state),
                    "question_ids": mapping,
                    "answers": {
                        qid: dataclasses.asdict(answer) for qid, answer in reply.answers.items()
                    },
                }
            )
            log(f"  [{index}/{len(selected)}] {sample['sample_id']} → {jev_client.describe_reply(reply)}")
        except Exception as error:  # noqa: BLE001 - 单条失败不该中断整轮标定，如实记下继续
            entry["error"] = f"{type(error).__name__}: {error}"
            log(f"  [{index}/{len(selected)}] {sample['sample_id']} → 失败：{entry['error']}")
        records.append(entry)

    ok = sum(1 for item in records if "error" not in item)
    log(f"[run] 成功 {ok} / {len(records)}")
    return {"generated_at": TODAY, "base_url": config.resolved_base_url, "model": model, "records": records}


# ---------------------------------------------------------------------------
# 统计与阈值扫描
# ---------------------------------------------------------------------------


def _auc(positives: list[float], negatives: list[float]) -> float | None:
    """分离度 AUC（正类应取高值）。0.5 = 完全分不开，1.0 = 完美分离。"""
    if not positives or not negatives:
        return None
    wins = 0.0
    for high in positives:
        for low in negatives:
            if high > low:
                wins += 1.0
            elif high == low:
                wins += 0.5
    return wins / (len(positives) * len(negatives))


def _quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def sweep_noul(pairs: list[tuple[float, bool]]) -> dict[str, Any]:
    """扫 (yes, no) 网格，给出「编造召回 / 误降级率 / 不确定带占比」的权衡表与建议。"""
    positives = [value for value, is_positive in pairs if is_positive]
    negatives = [value for value, is_positive in pairs if not is_positive]
    total_positive, total_negative = len(positives), len(negatives)
    grid_yes = [round(0.50 + 0.05 * step, 2) for step in range(10)]  # 0.50 … 0.95
    grid_no = [round(0.05 + 0.05 * step, 2) for step in range(10)]  # 0.05 … 0.50
    rows: list[dict[str, Any]] = []
    for yes in grid_yes:
        for no in grid_no:
            if no >= yes:
                continue
            hit = sum(1 for value in positives if value >= yes)
            alarm = sum(1 for value in negatives if value >= yes)
            quiet = sum(1 for value in negatives if value <= no)
            uncertain = (total_positive - hit) + (total_negative - alarm - quiet)
            rows.append(
                {
                    "yes": yes,
                    "no": no,
                    "invented_recall": round(hit / total_positive, 4) if total_positive else None,
                    "false_alarm_rate": round(alarm / total_negative, 4) if total_negative else None,
                    "quiet_rate": round(quiet / total_negative, 4) if total_negative else None,
                    "uncertain_share": round(uncertain / len(pairs), 4) if pairs else None,
                    # 「明确判定率」= 既没误降级、也没丢进不确定带的比例。阈值选择的**目标函数**：
                    # 在保证不误降级的前提下，让尽可能多的样本得到明确判定。
                    "decisive_rate": round((hit + quiet) / len(pairs), 4) if pairs else None,
                }
            )

    # 建议规则（两步，都可解释）：
    #   ① 硬约束：误降级率 ≤ 5%——降级会改动判定结果，宁可保守；
    #   ② 硬约束：编造召回 ≥ 80%——漏掉编造事实是这套校验存在的前提，不能为了好看牺牲掉；
    #   ③ 目标：在满足 ①② 的组合里最大化「明确判定率」，并列时取更大的 `yes`（降级门槛更保守）。
    # 若 ①② 无解，退回「只满足 ①」，并在 caveats 里如实写明召回不足。
    eligible = [
        row
        for row in rows
        if row["false_alarm_rate"] is not None and row["false_alarm_rate"] <= 0.05
    ]
    recalled = [row for row in eligible if row["invented_recall"] is not None and row["invented_recall"] >= 0.8]
    recommended = None
    if recalled:
        recommended = max(recalled, key=lambda row: (row["decisive_rate"], row["yes"]))
    elif eligible:
        recommended = max(eligible, key=lambda row: (row["decisive_rate"], row["yes"]))

    return {
        "positive_count": total_positive,
        "negative_count": total_negative,
        "auc": None if (auc := _auc(positives, negatives)) is None else round(auc, 4),
        "positive_values": sorted(round(value, 4) for value in positives),
        "negative_values": sorted(round(value, 4) for value in negatives),
        "positive_p50": _quantile(positives, 0.5),
        "negative_p50": _quantile(negatives, 0.5),
        "recall_floor_met": bool(recalled),
        "grid": rows,
        "recommended": recommended,
    }


def _bucket(confidence: float | None) -> str:
    if confidence is None:
        return "无 confidence"
    for upper, label in ((0.3, "<0.30"), (0.5, "0.30–0.50"), (0.7, "0.50–0.70"), (0.9, "0.70–0.90")):
        if confidence < upper:
            return label
    return "≥0.90"


def summarize_choice(records: list[dict[str, Any]]) -> dict[str, Any]:
    """`choice` 一致率 + confidence 分档，并据此判 `JEV_PARTIAL_DOWNGRADES` 是否该翻 `True`。"""
    labelled = [item for item in records if item["expected"]]
    if not labelled:
        return {"labelled_count": 0}

    def strict_hit(item: dict[str, Any]) -> bool:
        return item["choice"] == item["expected"]

    def lenient_hit(item: dict[str, Any]) -> bool:
        # 「部分支撑」对 `support` 与 `unsupport` 都算半对（它正是两者之间的档）。
        if item["expected"] == EXPECT_SUPPORT:
            return item["choice"] in (EXPECT_SUPPORT, "partial")
        return strict_hit(item)

    support_items = [item for item in labelled if item["expected"] == EXPECT_SUPPORT]
    negative_items = [item for item in labelled if item["expected"] in (EXPECT_UNSUPPORT, EXPECT_IRRELEVANT)]
    harm = sum(1 for item in support_items if item["choice"] in (EXPECT_UNSUPPORT, EXPECT_IRRELEVANT))
    missed = sum(1 for item in negative_items if item["choice"] in (EXPECT_SUPPORT, "partial"))

    buckets: dict[str, dict[str, Any]] = {}
    for item in labelled:
        label = _bucket(item["confidence"])
        slot = buckets.setdefault(label, {"count": 0, "strict_hit": 0})
        slot["count"] += 1
        slot["strict_hit"] += 1 if strict_hit(item) else 0
    for slot in buckets.values():
        slot["strict_rate"] = round(slot["strict_hit"] / slot["count"], 4)

    floor = None
    for candidate in (0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3):
        subset = [item for item in labelled if (item["confidence"] or 0.0) >= candidate]
        if len(subset) < 5:
            continue
        if sum(1 for item in subset if strict_hit(item)) / len(subset) >= 0.9:
            floor = candidate
            break

    harm_rate = round(harm / len(support_items), 4) if support_items else None
    miss_rate = round(missed / len(negative_items), 4) if negative_items else None
    enough = len(labelled) >= 20
    partial_downgrades = bool(
        enough
        and harm_rate is not None
        and miss_rate is not None
        and harm_rate <= 0.05
        and miss_rate <= 0.10
    )

    return {
        "labelled_count": len(labelled),
        "strict_agreement": round(sum(1 for item in labelled if strict_hit(item)) / len(labelled), 4),
        "lenient_agreement": round(sum(1 for item in labelled if lenient_hit(item)) / len(labelled), 4),
        "support_count": len(support_items),
        "negative_count": len(negative_items),
        "harm_rate": harm_rate,
        "miss_rate": miss_rate,
        "confidence_floor": floor,
        "partial_downgrades_recommended": partial_downgrades,
        "confidence_buckets": buckets,
        "confusion": {
            "expected": sorted({item["expected"] for item in labelled}),
            "returned": sorted({item["choice"] for item in labelled}),
        },
    }


def collect_records(samples: dict[str, Any], raw: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """把样本标注与原始应答拼成统计用记录。`choice`/`noul` 都从**真实题号**回读。"""
    by_id = {item["sample_id"]: item for item in samples["samples"]}
    out: list[dict[str, Any]] = []
    skipped: list[str] = []
    for record in raw.get("records") or []:
        sample = by_id.get(record.get("sample_id"))
        if sample is None:
            skipped.append(f"{record.get('sample_id')}: 样本集里没有这条（样本集被改过？）")
            continue
        if "error" in record:
            skipped.append(f"{record['sample_id']}: 调用失败（{record['error']}）")
            continue
        mapping = record.get("question_ids") or {}
        answers = record.get("answers") or {}
        item: dict[str, Any] = {
            "sample_id": record["sample_id"],
            "group": sample["group"],
            "expected": sample.get("expected") or "",
            "probe_kind": sample.get("probe_kind") or "",
            "choice": None,
            "confidence": None,
            "noul": None,
        }
        support_answer = answers.get(mapping.get("support", ""))
        if isinstance(support_answer, dict):
            item["choice"] = support_answer.get("choice")
            item["confidence"] = support_answer.get("confidence")
        invented_answer = answers.get(mapping.get("invented", ""))
        if isinstance(invented_answer, dict):
            item["noul"] = invented_answer.get("noul")
        if item["group"] == GROUP_INVENTED and item["noul"] is None:
            skipped.append(f"{record['sample_id']}: 缺 noul 值")
            continue
        if item["group"] == GROUP_SUPPORT and item["choice"] is None:
            skipped.append(f"{record['sample_id']}: 缺 choice 值")
            continue
        out.append(item)
    return out, skipped


def apply_labels(samples: dict[str, Any], labels: dict[str, Any]) -> int:
    """人工标签覆盖（键 = sample_id，值 = 期望标签；`null` 表示该条不参与统计）。"""
    changed = 0
    for sample in samples["samples"]:
        if sample["sample_id"] not in labels:
            continue
        value = labels[sample["sample_id"]]
        sample["expected"] = "" if value is None else str(value)
        sample["label_source"] = "human"
        changed += 1
    return changed


# ---------------------------------------------------------------------------
# 跨轮合并：noul 边界稳定性
#
# 为什么必须有这一段（2026-09-22 实测踩到的坑，别删）：
# 单轮 20 条样本上 `noul` 的 AUC = 1.0，扫描表一片「召回 100% / 误降级 0%」，看上去完美分离，
# 于是扫描按「贴着样本边界」挑出 0.75/0.45——**但上一轮标定有一条负类取到 0.75**，用新阈值
# 会把那条正常结论判成「编造」，JV04 会直接把它的支撑抹成「无支撑」（`SUPPORT_NONE`）。
# 即：**单轮完美分离 ≠ 跨轮稳定**。noul 在边界上有噪声（同期 `choice` 两轮严格一致率都是
# 60%，稳定得多，说明 noul 是比 choice 更软的信号），阈值必须在**全部历史轮次的并集**上评估。
#
# 两条规则，都是可解释的：
#   ① **余量规则**：阈值与样本边界至少留一个网格步长（0.05）。贴边的阈值在新样本上会立刻退化；
#   ② **改动要有明确收益**：现行值若已满足「0 误降级 / 0 误放行 / 召回 ≥80%」，且明确判定率
#      距跨轮最优不超过 `KEEP_CURRENT_SLACK`，就**维持现行值**——标定常量不该为小数点后
#      两位来回抖动；每改一次都要重跑全量回归，收益不够就不值得动。
# ---------------------------------------------------------------------------

_GRID_STEP = 0.05
#: 现行值与跨轮最优的明确判定率差距在此之内 → 维持现行值（避免无收益的抖动）。
KEEP_CURRENT_SLACK = 0.05
#: 召回下限：漏掉编造事实是这套校验存在的前提，不能为了「好看」牺牲掉。
_RECALL_FLOOR = 0.8


def _history_calibration_paths(exclude_date: str) -> list[Path]:
    """历次**真实**标定产物，**不含** DRYRUN。

    DRYRUN 文件名是 `…-DRYRUN-<date>.json`，`[0-9]*` 要求 `calibration-` 后紧跟数字，
    天然排除；合成产物混进历史并集会污染结论，这个约束别放宽。
    """
    return [
        path
        for path in sorted(EVIDENCE_DIR.glob("jev-cjk-calibration-[0-9]*.json"))
        if not path.name.startswith(f"jev-cjk-calibration-{exclude_date}")
    ]


def union_noul_pairs(noul: dict[str, Any], exclude_date: str) -> tuple[list[tuple[float, bool]], list[str]]:
    """本轮 + 全部历史轮次的 noul 取值并集。"""
    pairs: list[tuple[float, bool]] = [(float(v), True) for v in noul.get("positive_values") or []]
    pairs += [(float(v), False) for v in noul.get("negative_values") or []]
    rounds: list[str] = []
    for path in _history_calibration_paths(exclude_date):
        try:
            payload = _load_json(path)
        except Exception as exc:  # noqa: BLE001 - 历史产物损坏就跳过，不能拖垮本次出报告
            log(f"[analyze] 历史标定产物读不出，跳过：{path.name}（{type(exc).__name__}: {exc}）")
            continue
        hist = payload.get("noul") or {}
        positives = hist.get("positive_values") or []
        negatives = hist.get("negative_values") or []
        if not (positives or negatives):
            continue
        pairs += [(float(v), True) for v in positives]
        pairs += [(float(v), False) for v in negatives]
        rounds.append(str(payload.get("generated_at") or path.stem))
    return pairs, rounds


def evaluate_thresholds(pairs: list[tuple[float, bool]], *, yes: float, no: float) -> dict[str, Any]:
    """在给定样本集上评估一对阈值。

    `false_clean` 是**最危险**的一项：正类（编造）被判成「未编造」等于放编造事实过关；
    `false_alarm` 次之：负类被判成「编造」会把结论的支撑抹成「无支撑」。
    两者都记**条数**而不只是比率——20 条样本上 1 条就是 5%，光看比率看不出它其实是 1 条。
    """
    positives = [value for value, is_positive in pairs if is_positive]
    negatives = [value for value, is_positive in pairs if not is_positive]
    hit = sum(1 for value in positives if value >= yes)
    alarm = sum(1 for value in negatives if value >= yes)
    quiet = sum(1 for value in negatives if value <= no)
    leak = sum(1 for value in positives if value <= no)
    total = len(pairs) or 1
    return {
        "yes": yes,
        "no": no,
        "invented_recall": round(hit / len(positives), 4) if positives else None,
        "false_alarm_rate": round(alarm / len(negatives), 4) if negatives else None,
        "false_clean_rate": round(leak / len(positives), 4) if positives else None,
        "decisive_rate": round((hit + quiet) / total, 4),
        "false_alarm_count": alarm,
        "false_clean_count": leak,
    }


def cross_round_view(
    pairs: list[tuple[float, bool]],
    rounds: list[str],
    *,
    current: dict[str, Any],
    suggested: dict[str, Any] | None,
) -> dict[str, Any]:
    """在跨轮并集上评估「本轮建议 / 现行值 / 跨轮余量最优」，给出是否值得改代码的结论。"""
    positives = sorted(value for value, is_positive in pairs if is_positive)
    negatives = sorted(value for value, is_positive in pairs if not is_positive)
    if not positives or not negatives:
        return {"rounds": rounds, "usable": False}

    neg_max, pos_min = max(negatives), min(positives)
    grid_yes = [round(0.50 + _GRID_STEP * step, 2) for step in range(10)]
    grid_no = [round(0.05 + _GRID_STEP * step, 2) for step in range(10)]
    safe: list[dict[str, Any]] = []
    for yes in grid_yes:
        if yes < neg_max + _GRID_STEP - 1e-9:
            continue
        for no in grid_no:
            if no >= yes or no > pos_min - _GRID_STEP + 1e-9:
                continue
            entry = evaluate_thresholds(pairs, yes=yes, no=no)
            if entry["false_alarm_count"] or entry["false_clean_count"]:
                continue
            if (entry["invented_recall"] or 0) < _RECALL_FLOOR:
                continue
            safe.append(entry)
    best = max(safe, key=lambda item: (item["decisive_rate"], item["yes"])) if safe else None

    cur = evaluate_thresholds(pairs, yes=float(current["JEV_NOUL_YES"]), no=float(current["JEV_NOUL_NO"]))
    sug: dict[str, Any] | None = None
    if suggested and suggested.get("yes") is not None and suggested.get("no") is not None:
        sug = evaluate_thresholds(pairs, yes=float(suggested["yes"]), no=float(suggested["no"]))

    cur_ok = (
        not cur["false_alarm_count"]
        and not cur["false_clean_count"]
        and (cur["invented_recall"] or 0) >= _RECALL_FLOOR
    )
    if not cur_ok or best is None or best["decisive_rate"] - cur["decisive_rate"] > KEEP_CURRENT_SLACK + 1e-9:
        verdict = "adopt"
    else:
        verdict = "keep_current"

    return {
        "rounds": rounds,
        "usable": True,
        "positive_count": len(positives),
        "negative_count": len(negatives),
        "positive_values": [round(v, 4) for v in positives],
        "negative_values": [round(v, 4) for v in negatives],
        "negative_max": round(neg_max, 4),
        "positive_min": round(pos_min, 4),
        "boundary_gap": round(pos_min - neg_max, 4),
        "boundary_tight": bool(pos_min - neg_max < 3 * _GRID_STEP),
        "current": cur,
        "suggested": sug,
        "best": best,
        "verdict": verdict,
        "keep_slack": KEEP_CURRENT_SLACK,
    }


def analyze(samples: dict[str, Any], raw: dict[str, Any]) -> tuple[dict[str, Any], str]:
    records, skipped = collect_records(samples, raw)
    invented = [item for item in records if item["group"] == GROUP_INVENTED and item["expected"]]
    pairs = [
        (float(item["noul"]), item["expected"] == INVENTED_POSITIVE)
        for item in invented
        if item["noul"] is not None
    ]
    noul = sweep_noul(pairs) if pairs else {"positive_count": 0, "negative_count": 0}
    choice = summarize_choice([item for item in records if item["group"] == GROUP_SUPPORT])
    tactics = [item for item in records if item["group"] == GROUP_TACTICS]
    tactics_labeled = [item for item in tactics if item["expected"]]

    constants, _source = current_constants()
    # 跨轮合并：本轮 + 全部历史轮次。单轮的「完美分离」不足以支撑回写阈值，见 `cross_round_view` 抬头。
    union_pairs, union_rounds = union_noul_pairs(noul, exclude_date=TODAY)
    cross = cross_round_view(
        union_pairs, union_rounds, current=constants, suggested=noul.get("recommended")
    )
    if cross.get("usable") and cross["verdict"] == "keep_current":
        # 现行值在跨轮并集上已经站得住 → 建议就是「别改」。单轮扫描挑出来的贴边值在这里作废，
        # 否则《契约》§A 会被「每轮一个新阈值」反复回写。
        noul_recommended = {
            "JEV_NOUL_YES": constants["JEV_NOUL_YES"],
            "JEV_NOUL_NO": constants["JEV_NOUL_NO"],
        }
    else:
        noul_recommended = {
            "JEV_NOUL_YES": (noul.get("recommended") or {}).get("yes"),
            "JEV_NOUL_NO": (noul.get("recommended") or {}).get("no"),
        }

    result: dict[str, Any] = {
        "generated_at": TODAY,
        "seed": samples.get("seed"),
        "dry_run": str(raw.get("base_url") or "").startswith("（dry-run"),
        "sample_counts": {
            "total": len(samples["samples"]),
            "answered": len(records),
            "by_group": {
                group: sum(1 for item in samples["samples"] if item["group"] == group)
                for group in (GROUP_INVENTED, GROUP_SUPPORT, GROUP_TACTICS)
            },
        },
        "skipped": skipped,
        "noul": noul,
        "choice": choice,
        "tactics": {"answered": len(tactics), "labelled": len(tactics_labeled)},
        "cross_round": cross,
        "recommended": {
            **noul_recommended,
            "agreement_confidence_floor": choice.get("confidence_floor"),
            "JEV_PARTIAL_DOWNGRADES": choice.get("partial_downgrades_recommended"),
        },
        "caveats": _caveats(noul, choice, samples, cross),
    }
    return result, render_report(samples, raw, result)


def _caveats(
    noul: dict[str, Any],
    choice: dict[str, Any],
    samples: dict[str, Any],
    cross: dict[str, Any] | None = None,
) -> list[str]:
    notes: list[str] = [
        (
            "窗口取自研报正文，不是真实取数原文（研报 payload 不落来源正文）——本标定测的是中文文本里"
            "「事实有无」的判别力，不能替代「真实取数原文 vs 判断」的端到端抽检。"
        ),
    ]
    auc = noul.get("auc")
    if auc is None:
        notes.append("编造风险组样本不足，`noul` 阈值本次**未标定**，继续沿用官方英文范例值。")
    elif auc < 0.8:
        notes.append(
            f"`noul` 分离度偏低（AUC {auc}）：中文下模型对「事实有无」的判别力不足以支撑窄阈值，"
            "建议维持宽不确定带（中间档只标注、不降级）。"
        )
    uncertain = (noul.get("recommended") or {}).get("uncertain_share")
    if uncertain is not None and uncertain > 0.4:
        notes.append(
            f"建议阈值下不确定带占 {uncertain:.0%}：模型在中文输入上大量给出中间值，"
            "「不确定」应视为「不降级」而非「通过」。"
        )
    if not choice.get("labelled_count"):
        notes.append("`choice` 组样本不足，confidence 地板与 `JEV_PARTIAL_DOWNGRADES` 本次**未标定**。")
    elif not choice.get("partial_downgrades_recommended"):
        notes.append(
            "`choice` 一致率未达「误伤 ≤5% 且漏放 ≤10%（且样本 ≥20 条）」的门槛，"
            f"`JEV_PARTIAL_DOWNGRADES` 维持 `{current_constants()[0]['JEV_PARTIAL_DOWNGRADES']}`"
            "（若为 `False`，「部分支撑」只标注、不降级）。"
        )
    tactics_note = (samples.get("notes") or {}).get("tactics")
    if tactics_note:
        notes.append(f"JV03 战法复核组：{tactics_note}")

    # —— 跨轮合并的告警（这一段是 2026-09-22 踩坑后加的，别当成可有可无的附言）——
    if cross and cross.get("usable") and len(cross.get("rounds") or []):
        rounds = "、".join(cross["rounds"])
        suggested = cross.get("suggested")
        if suggested and suggested["false_alarm_count"]:
            notes.append(
                f"⚠️ **本轮扫描建议（{suggested['yes']}/{suggested['no']}）在跨轮并集上会误降级 "
                f"{suggested['false_alarm_count']} 条**（参与轮次：{rounds}）："
                "JV04 会把这些结论的支撑抹成「无支撑」。单轮 AUC 高不代表跨轮稳定——"
                "阈值已按跨轮并集回退，不要照抄 §2 的扫描建议值。"
            )
        if cross.get("boundary_tight"):
            notes.append(
                f"`noul` 跨轮边界没有余量：负类最大 {cross['negative_max']}、正类最小 "
                f"{cross['positive_min']}，只差 {cross['boundary_gap']}。"
                "这一带应留在不确定带里（只标注、不降级），不要用窄阈值强行二值化。"
            )
        if cross["verdict"] == "keep_current":
            notes.append(
                f"跨轮并集（{len(cross['positive_values'])} 条正类 / "
                f"{len(cross['negative_values'])} 条负类，参与轮次：{rounds}）上，代码现行值 "
                f"{cross['current']['yes']}/{cross['current']['no']} 已满足 0 误降级、0 误放行、"
                f"编造召回 {cross['current']['invented_recall']:.0%}，且与跨轮最优的明确判定率差距"
                f" ≤ {cross['keep_slack']:.0%} → **维持现行值，不改代码**。"
            )
    return notes


def _render_cross_round(cross: dict[str, Any] | None) -> list[str]:
    """§2 的跨轮子段：**阈值能不能回写，看这一段，不看单轮扫描表**。"""
    if not cross or not cross.get("usable") or not (cross.get("rounds") or []):
        return []
    rounds = "、".join(cross["rounds"])
    lines: list[str] = ["", "### 跨轮合并校验（阈值能否回写，以此为准）", ""]
    lines.append(
        f"- 参与并集：**本轮 + {rounds}**，共 {cross['positive_count']} 条正类 / "
        f"{cross['negative_count']} 条负类。"
    )
    lines.append(
        f"- 边界：负类最大 **{cross['negative_max']}**、正类最小 **{cross['positive_min']}**，"
        f"间隔 **{cross['boundary_gap']}**"
        + ("——**没有余量**，这一带必须留在不确定带里。" if cross["boundary_tight"] else "。")
    )
    lines.append("")
    lines.append("| 候选 | yes | no | 编造召回 | 误降级 | 误放行 | 明确判定率 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")

    def _row(label: str, entry: dict[str, Any] | None) -> None:
        if entry is None:
            lines.append(f"| {label} | — | — | — | — | — | — |")
            return
        lines.append(
            f"| {label} | {entry['yes']} | {entry['no']} | {entry['invented_recall']:.0%} | "
            f"{entry['false_alarm_count']} 条（{entry['false_alarm_rate']:.0%}） | "
            f"{entry['false_clean_count']} 条（{entry['false_clean_rate']:.0%}） | "
            f"{entry['decisive_rate']:.0%} |"
        )

    _row("本轮扫描建议", cross.get("suggested"))
    _row("代码现行值", cross["current"])
    _row("跨轮余量最优", cross.get("best"))
    lines.append("")
    if cross["verdict"] == "keep_current":
        lines.append(
            f"- **结论：维持代码现行值 `{cross['current']['yes']}` / `{cross['current']['no']}`，不改代码。**"
            f"现行值在跨轮并集上 0 误降级、0 误放行，明确判定率距跨轮最优不超过 "
            f"{cross['keep_slack']:.0%}——为这点差距改标定常量、重跑全量回归不值得。"
        )
    else:
        best = cross.get("best")
        target = f"`{best['yes']}` / `{best['no']}`" if best else "（跨轮并集上找不到安全组合）"
        lines.append(
            f"- **结论：建议改为 {target}**——现行值在跨轮并集上不安全，或明确判定率明显低于跨轮最优。"
        )
    lines.append(
        "- 余量规则：阈值与样本边界至少留一个网格步长（0.05）；贴边的阈值在新样本上会立刻退化。"
    )
    return lines


def render_report(samples: dict[str, Any], raw: dict[str, Any], result: dict[str, Any]) -> str:
    noul = result["noul"]
    choice = result["choice"]
    recommended = result["recommended"]
    lines: list[str] = []
    lines.append(f"# Jev 中文（CJK）精度标定报告 · {result['generated_at']}")
    lines.append("")
    if result.get("dry_run"):
        lines.append(
            "> ⚠️ **本报告是 dry-run 合成应答的产物，不是真实标定结果。**"
            "应答由 `dry-run` 阶段按标注模拟生成，只用于验证「抽样本 → 出报告」这条链路本身，"
            "**不得**据此回写《契约》§A。真实标定请执行 `build → run → analyze`。"
        )
        lines.append("")
    lines.append(
        "对应《Jev 决策模型接入任务路线图 2026-09-21》**R6**；阈值回写《Jev 接入契约》§A。"
        "本报告由 `scripts/calibrate-jev-cjk.py` 生成，不手工编辑。"
    )
    lines.append("")
    lines.append("## 1. 样本与执行")
    lines.append("")
    counts = result["sample_counts"]
    lines.append(f"- 样本合计 **{counts['total']}** 条，其中取得应答 **{counts['answered']}** 条。")
    lines.append(
        f"- A 受控探针（强标注）**{counts['by_group'][GROUP_INVENTED]}** 条｜"
        f"B 真实配对（弱标注）**{counts['by_group'][GROUP_SUPPORT]}** 条｜"
        f"C 战法复核 **{counts['by_group'][GROUP_TACTICS]}** 条。"
    )
    lines.append(f"- 端点 `{raw.get('base_url')}`；请求模型 `{raw.get('model')}`。")
    models = sorted({item.get("model") for item in raw.get("records") or [] if item.get("model")})
    if models:
        lines.append(f"- 响应返回的真实版本号：`{'`, `'.join(models)}`（留痕口径见《契约》§E3）。")
    if result["skipped"]:
        lines.append(f"- 未纳入统计 **{len(result['skipped'])}** 条：")
        lines.extend(f"  - {item}" for item in result["skipped"][:20])
    lines.append("")

    lines.append("## 2. `noul` 阈值（编造风险 · JV03/JV04 共用）")
    lines.append("")
    if not noul.get("positive_count"):
        baseline_fallback, _ = current_constants()
        lines.append(
            "**本次未标定**（样本不足）。继续沿用代码现行值 "
            f"`JEV_NOUL_YES = {baseline_fallback['JEV_NOUL_YES']:g}` / "
            f"`JEV_NOUL_NO = {baseline_fallback['JEV_NOUL_NO']:g}`。"
        )
    else:
        lines.append(
            f"- 正类（期望「编造」）{noul['positive_count']} 条，取值中位数 "
            f"**{noul['positive_p50']}**；负类（期望「未编造」）{noul['negative_count']} 条，"
            f"取值中位数 **{noul['negative_p50']}**。"
        )
        lines.append(f"- 分离度 **AUC = {noul['auc']}**（0.5 完全分不开，1.0 完美分离）。")
        rec = noul.get("recommended")
        if rec:
            lines.append(
                f"- **建议阈值：`JEV_NOUL_YES = {rec['yes']}` / `JEV_NOUL_NO = {rec['no']}`**"
                f"——编造召回 **{rec['invented_recall']:.0%}**、误降级率 **{rec['false_alarm_rate']:.0%}**、"
                f"明确判定率 **{rec['decisive_rate']:.0%}**（不确定带 **{rec['uncertain_share']:.0%}**）。"
            )
            if not noul.get("recall_floor_met"):
                lines.append(
                    "- ⚠️ 没有任何组合能同时满足「误降级率 ≤5%」与「编造召回 ≥80%」："
                    "上表建议是**只满足误降级约束**下的最优解，召回不足，回写时必须写明这一限制。"
                )
        else:
            lines.append(
                "- **没有**任何 (yes, no) 组合能把误降级率压到 5% 以内：中文下不宜用 `noul` 直接降级，"
                "建议保持宽不确定带。"
            )
        lines.append("")
        lines.append("### 阈值扫描（误降级率 ≤5% 的候选，按明确判定率降序）")
        lines.append("")
        lines.append("| yes | no | 编造召回 | 误降级率 | 未编造判定率 | 明确判定率 | 不确定带 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        eligible = [
            row
            for row in noul["grid"]
            if row["false_alarm_rate"] is not None and row["false_alarm_rate"] <= 0.05
        ]
        eligible.sort(key=lambda row: (row["decisive_rate"], row["yes"]), reverse=True)
        for row in eligible[:12]:
            lines.append(
                f"| {row['yes']} | {row['no']} | {row['invented_recall']:.0%} | "
                f"{row['false_alarm_rate']:.0%} | {row['quiet_rate']:.0%} | "
                f"{row['decisive_rate']:.0%} | {row['uncertain_share']:.0%} |"
            )
        if not eligible:
            lines.append("| — | — | — | — | — | — | — |")
        lines.append("")
        lines.append(
            f"- 原始取值（正类）：`{noul['positive_values']}`"
        )
        lines.append(f"- 原始取值（负类）：`{noul['negative_values']}`")
        lines.extend(_render_cross_round(result.get("cross_round")))
    lines.append("")

    lines.append("## 3. `choice` 一致率与 confidence 地板（JV03 认同度 / JV07 预判路由）")
    lines.append("")
    if not choice.get("labelled_count"):
        lines.append("**本次未标定**（样本不足）。")
    else:
        lines.append(
            f"- 有标注样本 **{choice['labelled_count']}** 条：严格一致率 **{choice['strict_agreement']:.0%}**、"
            f"宽松一致率（「部分支撑」算半对）**{choice['lenient_agreement']:.0%}**。"
        )
        lines.append(
            f"- 误伤率（应「支撑」被判成「不支撑/无关」）**{choice['harm_rate']}**；"
            f"漏放率（应「不支撑/无关」被判成「支撑/部分支撑」）**{choice['miss_rate']}**。"
        )
        floor = choice.get("confidence_floor")
        lines.append(
            f"- **建议 confidence 地板：{floor}**"
            + ("（此值以上严格一致率 ≥ 90%，且样本 ≥5 条）" if floor else "（样本内找不到满足条件的门槛）")
        )
        lines.append(
            f"- **`JEV_PARTIAL_DOWNGRADES` 建议值：`{choice['partial_downgrades_recommended']}`**"
            "（门槛：样本 ≥20 且误伤 ≤5% 且漏放 ≤10%）。"
        )
        lines.append("")
        lines.append("### confidence 分档一致率")
        lines.append("")
        lines.append("| confidence 区间 | 条数 | 严格一致率 |")
        lines.append("| --- | --- | --- |")
        for label in ("<0.30", "0.30–0.50", "0.50–0.70", "0.70–0.90", "≥0.90", "无 confidence"):
            slot = choice["confidence_buckets"].get(label)
            if slot:
                lines.append(f"| {label} | {slot['count']} | {slot['strict_rate']:.0%} |")
    lines.append("")

    lines.append("## 4. 建议回写《契约》§A 的阈值")
    lines.append("")
    baseline, baseline_source = current_constants()
    lines.append(f"| 常量 | 现行值（{baseline_source}） | 标定建议 |")
    lines.append("| --- | --- | --- |")
    for key, rec_key, name in (
        ("JEV_NOUL_YES", "JEV_NOUL_YES", "`JEV_NOUL_YES`"),
        ("JEV_NOUL_NO", "JEV_NOUL_NO", "`JEV_NOUL_NO`"),
        ("AGREEMENT_CONFIDENCE_FLOOR", "agreement_confidence_floor", "JV03 认同度 confidence 回退线"),
        ("JEV_PARTIAL_DOWNGRADES", "JEV_PARTIAL_DOWNGRADES", "`JEV_PARTIAL_DOWNGRADES`"),
    ):
        render = (lambda v: f"`{v}`") if key == "JEV_PARTIAL_DOWNGRADES" else (lambda v: f"{v:g}")
        suggestion = recommended.get(rec_key)
        current = render(baseline[key])
        if suggestion is None:
            suggestion = f"**未标定**（维持 {current}）"
        elif render(suggestion) == current:
            suggestion = f"{render(suggestion)}（与现行一致，无需改动）"
        else:
            suggestion = render(suggestion)
        lines.append(f"| {name} | {current} | {suggestion} |")
    lines.append("")
    cross = result.get("cross_round") or {}
    if cross.get("usable") and cross.get("rounds") and cross["verdict"] == "keep_current":
        lines.append(
            "> `JEV_NOUL_*` 的「与现行一致」不是巧合：跨轮合并校验判定现行值在并集样本上已满足 "
            f"0 误降级 / 0 误放行，且明确判定率距跨轮最优不足 {cross['keep_slack']:.0%}"
            "——按「改动要有明确收益」规则维持不变。详见 §2「跨轮合并校验」。"
        )
        lines.append("")

    lines.append("## 5. 口径与限制（不可省略）")
    lines.append("")
    lines.extend(f"- {item}" for item in result["caveats"])
    lines.append("")
    lines.append("### 标注强度、取材出处与出网范围")
    lines.append("")
    lines.append(f"- 标注强度：{samples['notes']['label_strength']}")
    lines.append(f"- 窗口出处：{samples['notes']['passage_provenance']}")
    lines.append(
        "- 样本会出网到 TypeSafe（美国托管、默认非零留存；本项目已决定不申请 ZDR，见《契约》§E2）；"
        f"单条 state 上限 {samples.get('window_chars')} 字窗口，不送整份研报。"
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# dry-run：合成应答，验证 build → analyze 全链路（不出网）
# ---------------------------------------------------------------------------


def synthesize_raw(samples: dict[str, Any], error_rate: float) -> dict[str, Any]:
    """按标注合成应答：正类给高分、负类给低分，并故意留一小段重叠区，用来验证扫描逻辑。"""
    rng = random.Random(SEED)
    records: list[dict[str, Any]] = []
    for sample in samples["samples"]:
        if rng.random() < error_rate:
            records.append({"sample_id": sample["sample_id"], "group": sample["group"], "error": "dry-run 模拟失败"})
            continue
        expected = sample.get("expected") or ""
        record: dict[str, Any] = {
            "sample_id": sample["sample_id"],
            "group": sample["group"],
            "model": "jev-dry-run",
            "requested_model": DEFAULT_MODEL,
            "latency_ms": rng.randint(300, 900),
            "input_tokens": rng.randint(400, 900),
            "output_tokens": 24,
            "question_ids": {"support": "c1_supports", "invented": "c1_invented"},
            "answers": {},
        }
        if sample["group"] == GROUP_INVENTED:
            if expected == EXPECT_INVENTED_TRUE:
                value = min(1.0, 0.62 + rng.random() * 0.38)
            else:
                value = max(0.0, rng.random() * 0.34)
            record["answers"]["c1_invented"] = {"question_id": "c1_invented", "type": "noul", "noul": round(value, 4)}
        else:
            pick = expected if rng.random() > 0.15 else rng.choice(["support", "partial", "unsupport", "irrelevant"])
            confidence = round(0.25 + rng.random() * 0.72, 4)
            record["answers"]["c1_supports"] = {
                "question_id": "c1_supports",
                "type": "choice",
                "choice": pick,
                "confidence": confidence,
                "probabilities": {pick: confidence},
            }
        records.append(record)
    return {"generated_at": TODAY, "base_url": "（dry-run 未出网）", "model": DEFAULT_MODEL, "records": records}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _emit(result: dict[str, Any], report: str, prefix: Path) -> None:
    json_path = prefix.with_suffix(".json")
    md_path = prefix.with_suffix(".md")
    _dump(json_path, result)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(report, encoding="utf-8")
    log("")
    log(f"[out] {json_path}")
    log(f"[out] {md_path}")
    log("")
    log("建议阈值：")
    for key, value in result["recommended"].items():
        log(f"  {key} = {value}")
    log("")
    log("⚠️ 回写《Jev 接入契约》§A 后，R6 才算完成；未标定的项必须写明「维持官方英文范例值」。")


def main() -> int:
    parser = argparse.ArgumentParser(description="R6｜Jev 中文（CJK）精度标定")
    parser.add_argument(
        "stage",
        choices=("build", "run", "analyze", "dry-run", "all"),
        help="build=抽样本（离线）；run=出网跑样本；analyze=出报告（离线）；dry-run=合成应答自检；all=build→run→analyze",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="本机库路径（只读）")
    parser.add_argument("--per-class", type=int, default=10, help="每组每类的样本条数上限")
    parser.add_argument("--tactics-limit", type=int, default=10, help="JV03 战法复核样本条数上限")
    parser.add_argument("--limit", type=int, default=60, help="run 阶段最多跑多少条")
    parser.add_argument("--group", default="", help="只跑某一组（claim_invented / claim_support / tactics_review）")
    parser.add_argument("--api-key", default="", help="明文密钥（优先于环境变量；不落盘、不进日志）")
    parser.add_argument("--api-key-env", default="STEWARD_JEV_API_KEY", help="从该环境变量读密钥")
    parser.add_argument("--credential-ref", default="jev_api_key", help="凭据库引用（无明文密钥时用）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Jev 端点")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Jev 模型名/别名")
    parser.add_argument("--samples", type=Path, default=None, help="样本集路径（默认取最近一次产物）")
    parser.add_argument("--raw", type=Path, default=None, help="原始应答路径（默认取最近一次产物）")
    parser.add_argument("--labels", type=Path, default=None, help="人工标签覆盖文件（JSON）")
    parser.add_argument("--error-rate", type=float, default=0.05, help="dry-run 模拟失败率")
    args = parser.parse_args()

    samples_path = args.samples or _latest(
        "jev-cjk-samples-[0-9]*.json", EVIDENCE_DIR / f"jev-cjk-samples-{TODAY}.json"
    )
    raw_path = args.raw or _latest(
        "jev-cjk-raw-[0-9]*.json", EVIDENCE_DIR / f"jev-cjk-raw-{TODAY}.json"
    )
    prefix = EVIDENCE_DIR / f"jev-cjk-calibration-{TODAY}"

    if args.stage in ("build", "all"):
        if not args.db.exists():
            log(f"[build] 本机库不存在：{args.db}")
            return 2
        samples = build_samples(args.db, args.per_class, args.tactics_limit)
        _dump(samples_path, samples)
        log(f"[build] → {samples_path}")
        log("")

    if args.stage in ("run", "all"):
        samples = _load_json(samples_path)
        api_key = args.api_key or os.environ.get(args.api_key_env, "")
        if not api_key and args.credential_ref:
            log(f"[run] 未给明文密钥，改从凭据库取「{args.credential_ref}」（Windows 凭据管理器 / 本机库）")
        raw = run_samples(
            samples["samples"],
            db_path=args.db,
            api_key=api_key,
            credential_ref=args.credential_ref,
            base_url=args.base_url,
            model=args.model,
            limit=args.limit,
            only_group=args.group,
        )
        _dump(raw_path, raw)
        log(f"[run] → {raw_path}")
        log("")

    if args.stage == "dry-run":
        samples = _load_json(samples_path)
        raw = synthesize_raw(samples, args.error_rate)
        raw_path = EVIDENCE_DIR / f"jev-cjk-raw-dryrun-{TODAY}.json"
        _dump(raw_path, raw)
        log(f"[dry-run] 合成应答 {len(raw['records'])} 条 → {raw_path}")
        log("")

    if args.stage in ("analyze", "dry-run", "all"):
        samples = _load_json(samples_path)
        raw = _load_json(raw_path)
        if args.labels:
            labels = _load_json(args.labels)
            log(f"[analyze] 人工标签覆盖 {apply_labels(samples, labels)} 条（{args.labels}）")
        result, report = analyze(samples, raw)
        if result.get("dry_run"):
            # 合成结果必须一眼可辨，绝不与真实标定产物同名——否则半年后没人分得清哪份能回写契约。
            prefix = EVIDENCE_DIR / f"jev-cjk-calibration-DRYRUN-{TODAY}"
        _emit(result, report, prefix)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

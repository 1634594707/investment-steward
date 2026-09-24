"""研读图书馆后端（G5）。

- 书目 CRUD 走 /library/books（全本机，data_leaves_device=false）；
- `generate_reading_plan`：按当前书目与模型方案确定性生成荐读计划，只落 today.learning 学习流；
  永不写 investment-policies（G5-4 由测试锁定）。
- 批注转研究问题复用 /research/runs 的 ResearchRun 创建逻辑（G5-3）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from investment_steward_core.domain import LibraryPlan, RunStatus

if TYPE_CHECKING:
    from investment_steward_core.storage import Database

_DAILY_TASKS = {
    "摘要": "今天用一段话复述这一章的核心判断，并写清它如何影响你当前的某个持仓或自选观察条件。",
    "条件核对": "把本章观点拆成「若看到什么，则说明更接近被支持/被证伪」的可核对条件，逐一记录。",
    "批注": "在原文可疑处做批注：区分事实、观点与你自己的推断，避免把作者观点当成你自己的结论。",
}


def generate_reading_plan(
    db: Database,
    *,
    source_book_id: UUID | None = None,
    model_client: object | None = None,
) -> LibraryPlan:
    """生成荐读计划：从书目/首章派生日任务，落 today.learning（L2 summary_row）。

    双引擎：`model_client` 提供且可用时，先尝试用激活模型围绕书目生成更贴合的任务与理由
    （经简单抽取，不触碰结构化回答闸门）；任何不可用 / 无激活方案 / 解析失败 → 回退确定性
    `_DAILY_TASKS`。计划只写 library_plans、绝不触碰投资原则表（G5-4 由测试锁定）。
    """
    books = db.list_books()
    if source_book_id is not None:
        anchor = next((b for b in books if b.book_id == source_book_id), None)
    else:
        anchor = books[0] if books else None

    task, rationale = _generate_task_rationale(db, anchor, model_client)

    plan = LibraryPlan(
        plan_id=uuid4(),
        book_ref=str(anchor.book_id) if anchor else None,
        daily_task=task,
        source_chapter=anchor.title if anchor else "通识学习",
        rationale=rationale,
        created_at=datetime.now(UTC),
        slot="today.learning",
        renderer="summary_row",
    )
    db.upsert_library_plan(plan)
    return plan


def _generate_task_rationale(
    db: Database,
    anchor: object | None,
    model_client: object | None,
) -> tuple[str, str]:
    """模型优先生成任务与理由；模型路径失效回退确定性模板。"""
    if model_client is not None:
        try:
            model_text = _query_model_plan(db, anchor)
        except Exception:  # noqa: BLE001 - 模型路径任何异常都回退确定性
            model_text = None
        if model_text:
            task, rationale = _split_model_plan(model_text, anchor)
            if task and rationale:
                return task, rationale

    task = _DAILY_TASKS["摘要"] if anchor else _DAILY_TASKS["条件核对"]
    rationale = (
        f"围绕你已收录书目《{anchor.title}》推进结构化研读，先建立可复核的摘要与批注。"
        if anchor
        else "尚未收录书目；先用条件核对练习建立可被证伪的观察框架，为后续研读打底。"
    )
    return task, rationale


def _query_model_plan(db: Database, anchor: object | None) -> str | None:
    """调用激活模型生成荐读任务的纯文本（模型多言时取其 JSON 空串亦接受）。"""
    from investment_steward_core import model_client as mc
    from investment_steward_core.prompting import library_plan_user_prompt

    profiles = db.list_model_profiles()
    profile = mc.active_model_profile(profiles)
    if profile is None:
        return None
    title = getattr(anchor, "title", None)
    reply = mc.call_active_model(
        profile,
        _CredentialDependency(db),
        mc.format_messages(
            "你是荐读助手。只输出 JSON 对象 {\"task\":\"…\",\"rationale\":\"…\"}，不输出其它。",
            library_plan_user_prompt(title),
        ),
        timeout=15.0,
     purpose="研读",)
    return reply.content if reply.content else None


def _split_model_plan(text: str, anchor: object | None) -> tuple[str, str]:
    """从模型 JSON 抽取 task/rationale；失败抛异常由上层回退。"""
    import json
    import re

    block = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    raw = block.group(1) if block else text
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型输出无 JSON 对象")
    obj = json.loads(raw[start : end + 1])
    task = str(obj.get("task", "")).strip()
    rationale = str(obj.get("rationale", "")).strip()
    if not task or not rationale:
        raise ValueError("模型输出缺少 task/rationale")
    return task, rationale


class _CredentialDependency:
    def __init__(self, db: Database):
        self._db = db

    def get(self, key_id: str) -> str | None:
        return self._db.get_credential(key_id)


def create_research_from_annotation(
    db: Database, user_id: UUID, annotation_ref: str, user_question: str
) -> tuple[object, bool]:
    """把书目批注转成研究问题：校验批注存在后建立 ResearchRun。

    返回 (ResearchRun, found)；批注不存在时 found=False（调用方转 404），不臆造研究问题。
    """
    from investment_steward_core.domain import ResearchRun

    found = False
    for book in db.list_books():
        if annotation_ref in {f"{book.book_id}:{i}" for i in range(len(book.notes))}:
            found = True
            break
        if annotation_ref in book.notes:
            found = True
            break
    run = ResearchRun(
        run_id=uuid4(),
        user_id=user_id,
        status=RunStatus.CREATED,
        user_question=user_question,
    )
    return run, found
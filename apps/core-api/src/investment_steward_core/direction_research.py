"""方向研判升级（v33，2026-09-14 路线图 B 组 / C 组）：从知识概述升级为有证据的产业研究。

设计铁律（路线图 §4/§5）：
- **两种诚实的输出状态**（B01）：证据包非空 → `research_mode="evidence"`（证据研判）；
  证据缺失 → `research_mode="knowledge"`（知识概览，必须声明数据缺口）。
  缺必需内容或解析失败 → 质量闸门判 incomplete 并落库为草稿，不靠补标题假装完整。
- **固定研究结构**（B02）：十个部分（研究问题与期限 → 下一验证动作），每部分
  「事实 → 推理 → 不确定性」；「行业增长」不得直接推出「某公司利润增长」（B05 分层）。
- **候选股是可验证的研究对象**（B09-B12）：证券身份经 `instruments.normalize_instrument`
  归一校验（保留模型原始代码 + 标准代码 + 身份状态）；**移除模型估价**（B11）——
  模型不再输出「大致现价」，模型若自行输出价格字段一律剔除并告警；每家候选必须给出
  产业链位置 / 业务关联 / 业绩兑现路径 / 支持来源 / 反证与缺口，凑数编造由质量闸门标注。
- **方向研判专属质量规则**（B15）：必需小节、核心判断分层、候选身份、空结果说明、
  摘要与篇幅预算（B08：快速 800-1200 / 标准 1500-2500 / 深研 2500-4000；摘要 150-220/260）。
  与个股研报共用三态质量状态口径（complete/needs_review/incomplete）。
- **模型可选**（C02/C04）：profile 解析由调用方完成；本模块只在提示词中要求落库
  调用身份（模型 / 方案 / 提示词版本 / 模式），不自行选择模型。

设计铁律（与全站一致）：纯函数、无 IO；调用与落库由 app.py 决定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from investment_steward_core import report_quality
from investment_steward_core.instruments import normalize_instrument
# A02：估值口径的声明文本必须与估值源一致 —— 从模块导入常量，不复制第二份定义
# （路线图 §9：同一估值事实在证据包中只出现一个来源定义）。
# v36：`BoardTrendAggregate` 同理由 valuation_evidence 定义（趋势数据源侧），
# 本模块导入消费，保持 direction → valuation 的单向依赖。
from investment_steward_core.valuation_evidence import (
    VALUATION_SOURCE_LABEL,
    BoardTrendAggregate,
)

# 方向研判提示词版本：改模板或契约必须递增（与研报提示词同一约定）。
# M3（2026-09-15）：报告形态（D01）+ 前置比较表（D02）+ 跨层因果限制（D03）
# + 周期审查（D04）+ 引用支持（D07）→ 4.0。
DIRECTION_PROMPT_VERSION = "4.1"

# —— B08：方向研判独立篇幅预算（不修改个股规则；软告警） ——
DIRECTION_MODES: dict[str, dict[str, Any]] = {
    "quick": {"label": "快速概览", "min": 800, "max": 1200},
    "standard": {"label": "标准研判", "min": 1500, "max": 2500},
    "deep": {"label": "深研", "min": 2500, "max": 4000},
}
DEFAULT_DIRECTION_MODE = "standard"
# 摘要预算（B08）：目标 150-220 字、硬上限 260 字（仅软告警）。
DIRECTION_SUMMARY_TARGET_MIN = 150
DIRECTION_SUMMARY_TARGET_MAX = 220
DIRECTION_SUMMARY_HARD_MAX = 260

# —— B02：固定研究结构（十个部分） ——
REQUIRED_DIRECTION_SECTIONS: tuple[str, ...] = (
    "研究问题与期限",
    "产业链与利润分配",
    "需求供给与价格",
    "竞争与替代",
    "景气阶段",
    "商业质量",
    "资本开支与现金回报",
    "催化与反证",
    "候选公司差异",
    "下一验证动作",
)
MIN_SECTION_BODY_CHARS = 20

# —— B05：核心判断分层词表 ——
JUDGMENT_KINDS = ("fact", "industry", "competitiveness", "sentiment", "technical")
JUDGMENT_KIND_LABELS: dict[str, str] = {
    "fact": "数据事实",
    "industry": "产业景气",
    "competitiveness": "长期竞争力",
    "sentiment": "资金情绪",
    "technical": "技术信号",
}
ALLOWED_CONFIDENCE = ("low", "medium", "high")

# —— B09：证券身份三态 ——
IDENTITY_VERIFIED = "verified"
IDENTITY_UNVERIFIED = "unverified"
IDENTITY_INVALID = "invalid"
IDENTITY_STATUS_LABELS: dict[str, str] = {
    IDENTITY_VERIFIED: "身份已核验",
    IDENTITY_UNVERIFIED: "待核验",
    IDENTITY_INVALID: "无效代码",
}

# 候选数量口径（B12：通常 3-8 家，允许更少或零家，但必须解释）。
POOL_TARGET_MIN = 3
POOL_TARGET_MAX = 8

# —— D01（M3）：按证据可得性选择报告形态 ——
# 用户反馈：固定十节模板在数据缺失时逼模型凑小节，反复输出「无法判断」的长文。
# 形态三档（证据越多越完整；缺失时输出**简短阶段性研究 + 补证动作**而非凑节）：
#   full    → 证据充分：十节齐 + 正式研判；
#   partial → 部分可用：只对有证据的行业下结论，必需小节收窄；
#   stage   → 关键数据缺失：简短阶段性研究（篇幅收窄）+ 明确补证动作。
DIRECTION_FORM_FULL = "full"
DIRECTION_FORM_PARTIAL = "partial"
DIRECTION_FORM_STAGE = "stage"
DIRECTION_FORM_LABELS: dict[str, str] = {
    DIRECTION_FORM_FULL: "完整研判",
    DIRECTION_FORM_PARTIAL: "局部研判",
    DIRECTION_FORM_STAGE: "阶段性研究",
}
# 各形态的**必需**小节（其余小节缺失只作软告警，不阻断交付）。
FORM_REQUIRED_SECTIONS: dict[str, tuple[str, ...]] = {
    DIRECTION_FORM_FULL: REQUIRED_DIRECTION_SECTIONS,
    DIRECTION_FORM_PARTIAL: ("研究问题与期限", "需求供给与价格", "催化与反证", "下一验证动作"),
    DIRECTION_FORM_STAGE: ("研究问题与期限", "下一验证动作"),
}
# 阶段性研究的篇幅预算（覆盖模式预算：缺失数据时不该写长文）。
DIRECTION_STAGE_CHAR_MIN = 300
DIRECTION_STAGE_CHAR_MAX = 900
# 判为「证据充分」的最少证据条目数（低于此值只能局部研判）。
DIRECTION_FORM_FULL_MIN_EVIDENCE = 3
# 证据源不可用条目达到该数量时降为局部研判（多源同时缺口不可宣称完整）。
DIRECTION_FORM_PARTIAL_MAX_UNAVAILABLE = 2


def direction_form_label(form: str | None) -> str:
    """形态中文标签；未知形态回落「完整研判」（不静默变口径）。"""
    return DIRECTION_FORM_LABELS.get(str(form or ""), DIRECTION_FORM_LABELS[DIRECTION_FORM_FULL])


def direction_form_sections(form: str | None) -> tuple[str, ...]:
    """形态的必需小节集合；未知形态按完整研判的十节处理（保守）。"""
    return FORM_REQUIRED_SECTIONS.get(str(form or ""), REQUIRED_DIRECTION_SECTIONS)


def choose_direction_form(
    *,
    research_mode: str,
    evidence_count: int,
    primary_ok: bool = True,
    unavailable_count: int = 0,
) -> str:
    """D01：按证据可得性选择报告形态（确定性，不依赖模型自述）。

    - 无证据（knowledge 模式）或证据条目为 0 → `stage`（简短阶段性研究 + 补证动作）；
    - 主题主证据通道失败（`primary_ok=False`，如风格主题的估值横截面取数失败）或
      不可用来源 ≥ 阈值 → `partial`（只分析有证据的部分）；
    - 证据条目 ≥ `DIRECTION_FORM_FULL_MIN_EVIDENCE` 且主通道正常 → `full`。
    """
    if str(research_mode) != "evidence" or int(evidence_count) <= 0:
        return DIRECTION_FORM_STAGE
    if not primary_ok or int(unavailable_count) >= DIRECTION_FORM_PARTIAL_MAX_UNAVAILABLE:
        return DIRECTION_FORM_PARTIAL
    if int(evidence_count) >= DIRECTION_FORM_FULL_MIN_EVIDENCE:
        return DIRECTION_FORM_FULL
    return DIRECTION_FORM_PARTIAL

_HEADING = re.compile(r"^#{1,6}\s*(.+?)\s*$")

# —— B07：按产业选择指标模板（通用框架上的行业专属指标提示；无该行业数据就说明缺口，不强填）。 ——
INDUSTRY_TEMPLATES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("芯片", "半导体", "制造", "设备", "电池", "光伏", "钢铁", "化工", "汽车", "机器人", "军工"),
     "制造类：订单/产能利用率/库存周期/价格（可观察指标如排产、招标价、开工率、资本开支）"),
    (("消费", "白酒", "食品", "饮料", "零售", "电商", "服装", "家电", "旅游", "餐饮"),
     "消费类：渠道库存/终端销量/单价（动销、促销力度、客流、客单价）"),
    (("银行", "保险", "券商", "金融", "地产", "信贷"),
     "金融类：资本与资产质量（不良率、净息差、保费增速、杠杆）"),
    (("煤炭", "石油", "有色", "锂", "铜", "资源", "原油", "黄金", "矿"),
     "资源类：成本曲线与商品周期（现货价、库存、全球资本开支）"),
    (("AI", "软件", "云计算", "科技", "游戏", "互联网", "数据"),
     "科技类：商业化与收入兑现（订单、付费渗透率、研发投入转化）"),
)
GENERIC_INDUSTRY_HINT = "通用框架：需求/供给/价格 + 竞争格局 + 景气阶段；主题不属于上述行业或无行业专属数据时，如实说明缺口，禁止强填统一指标。"


def industry_template_hint(topic: str) -> str:
    """按主题关键词匹配行业指标模板（B07）；未命中给通用框架。"""
    text = str(topic or "").lower()
    for keywords, hint in INDUSTRY_TEMPLATES:
        if any(keyword.lower() in text for keyword in keywords):
            return hint
    return GENERIC_INDUSTRY_HINT
_WHITESPACE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# B03 板块证据层：主题 → 行业板块解析（纯函数，无 IO；取数在调用方 app.py）
# ---------------------------------------------------------------------------

# 主题同义词 → 新浪行业板块名。映射目标均经实盘成分核验（2026-09-14）：
# 招商轮船/中远海控/南京港 ∈ 交通运输；招商银行/中信证券 ∈ 金融行业；
# 贵州茅台 ∈ 酿酒行业；中国中免 ∈ 酒店旅游；牧原股份 ∈ 农林牧渔；
# 比亚迪 ∈ 汽车制造；北方华创/隆基绿能/长电科技 ∈ 电子器件；中兴通讯/浪潮信息 ∈ 电子信息；
# 金风科技/东方电气 ∈ 发电设备；三一重工/埃斯顿 ∈ 机械行业；航发动力/中航西飞 ∈ 飞机制造；
# 中国船舶/中船防务 ∈ 船舶制造；紫金矿业/山东黄金/江西铜业 ∈ 有色金属；
# 中国石油/中国石化 ∈ 石油行业；完美世界/光线传媒 ∈ 传媒娱乐；曲美家居 ∈ 家具行业。
SECTOR_TOPIC_ALIASES: tuple[tuple[str, str], ...] = (
    ("海运", "交通运输"), ("航运", "交通运输"), ("港口", "交通运输"),
    ("物流", "交通运输"), ("快递", "交通运输"), ("机场", "交通运输"), ("高速", "交通运输"),
    ("银行", "金融行业"), ("保险", "金融行业"), ("券商", "金融行业"), ("证券", "金融行业"), ("信托", "金融行业"),
    ("白酒", "酿酒行业"), ("啤酒", "酿酒行业"),
    ("零售", "商业百货"), ("超市", "商业百货"), ("免税", "商业百货"),
    ("旅游", "酒店旅游"), ("酒店", "酒店旅游"), ("餐饮", "酒店旅游"),
    ("医药", "生物制药"), ("创新药", "生物制药"), ("疫苗", "生物制药"), ("中药", "生物制药"),
    ("养殖", "农林牧渔"), ("种植", "农林牧渔"), ("渔业", "农林牧渔"),
    ("黄金", "有色金属"), ("白银", "有色金属"), ("铜", "有色金属"), ("铝", "有色金属"),
    ("锂", "有色金属"), ("稀土", "有色金属"), ("贵金属", "有色金属"),
    ("原油", "石油行业"), ("油气", "石油行业"), ("天然气", "石油行业"),
    ("地产", "房地产"), ("楼市", "房地产"),
    ("整车", "汽车制造"), ("新能源车", "汽车制造"), ("车企", "汽车制造"),
    ("半导体", "电子器件"), ("芯片", "电子器件"), ("元器件", "电子器件"), ("光伏", "电子器件"),
    ("软件", "电子信息"), ("互联网", "电子信息"), ("计算机", "电子信息"), ("通信", "电子信息"),
    ("人工智能", "电子信息"), ("AI", "电子信息"), ("云计算", "电子信息"), ("算力", "电子信息"),
    ("游戏", "传媒娱乐"), ("影视", "传媒娱乐"), ("广告", "传媒娱乐"), ("直播", "传媒娱乐"),
    ("风电", "发电设备"), ("核电", "发电设备"),
    ("机器人", "机械行业"), ("装备", "机械行业"), ("自动化", "机械行业"),
    ("军工", "飞机制造"), ("航空", "飞机制造"), ("航天", "飞机制造"),
    ("造船", "船舶制造"),
    ("家居", "家具行业"),
)

_TOPIC_SUFFIXES: tuple[str, ...] = ("板块", "行业", "概念", "主题", "产业", "指数")
_SECTOR_NAME_SUFFIX = "行业"
DIRECTION_SECTOR_MATCH_MAX = 2


def _topic_core(topic: str) -> str:
    """主题 → 去空白、去板块/行业/概念等后缀的核心词。"""
    text = _WHITESPACE.sub("", str(topic or ""))
    for suffix in _TOPIC_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
    return text


def match_sector_rows(
    topic: str,
    sector_rows: list[dict[str, Any]],
    *,
    max_sectors: int = DIRECTION_SECTOR_MATCH_MAX,
) -> list[dict[str, Any]]:
    """主题 → 命中的行业板块行（先直接包含匹配、再别名映射；按 code 去重，封顶 max_sectors）。

    「低估板块」这类风格/概念主题匹配不到任何板块 → 空列表，调用方如实声明缺口，不硬凑。
    """
    core = _topic_core(topic)
    if not core or not isinstance(sector_rows, list):
        return []
    normalized = [
        (row, str(row.get("name") or "").removesuffix(_SECTOR_NAME_SUFFIX))
        for row in sector_rows
        if isinstance(row, dict)
    ]
    matched: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _push(row: dict[str, Any]) -> None:
        code = str(row.get("code") or "")
        if len(matched) >= max_sectors or not code or code in seen:
            return
        seen.add(code)
        matched.append(row)

    # 直接匹配：核心词与板块名（去「行业」后缀）互为包含。
    for row, name in normalized:
        if name and (name in core or core in name):
            _push(row)
    # 别名匹配：同义词在核心词中出现 → 目标板块（实盘核验的映射表）。
    if len(matched) < max_sectors:
        for alias, target in SECTOR_TOPIC_ALIASES:
            if alias not in core:
                continue
            for row, name in normalized:
                if name and name == target.removesuffix(_SECTOR_NAME_SUFFIX):
                    _push(row)
                    break
            if len(matched) >= max_sectors:
                break
    return matched


def describe_sector_snapshot(
    sector: dict[str, Any],
    members: list[dict[str, Any]],
    *,
    rank: int,
    total: int,
) -> str:
    """板块行情快照 → 单条证据文本（B04 口径：数值 + 当日声明；纯函数）。

    结尾固定标注「当日行情快照、非景气度结论」，防止模型把日内涨跌当成产业景气证据。
    """
    parts: list[str] = [f"板块「{sector.get('name') or ''!s}」"]
    count = sector.get("member_count")
    if isinstance(count, (int, float)) and count:
        parts.append(f"成分股 {int(count)} 只")
    change = sector.get("change_pct")
    if isinstance(change, (int, float)):
        parts.append(f"当日涨跌 {change:+.2f}%")
    if isinstance(rank, int) and isinstance(total, int) and 0 < rank <= total:
        parts.append(f"当日涨跌幅排名 {rank}/{total}")
    amount = sector.get("amount")
    if isinstance(amount, (int, float)) and amount:
        parts.append(f"成交额 {amount / 1e8:.1f} 亿元")
    leader = str(sector.get("leader_name") or "")
    if leader:
        leader_change = sector.get("leader_change_pct")
        change_text = f" {leader_change:+.2f}%" if isinstance(leader_change, (int, float)) else ""
        parts.append(f"领涨股 {leader}{change_text}")
    member_bits: list[str] = []
    for row in members[:5]:
        symbol = str(row.get("symbol") or "")
        name = str(row.get("name") or "")
        if not symbol and not name:
            continue
        row_change = row.get("change_pct")
        change_text = f" {row_change:+.2f}%" if isinstance(row_change, (int, float)) else ""
        member_bits.append(f"{name}({symbol}){change_text}")
    if member_bits:
        parts.append("成交额前五成分：" + "、".join(member_bits))
    # C03（2026-09-15 路线图）：成员身份边界——成交额前五只是当日资金活跃度抽样，
    # 不是行业完整成员，更不是基本面排序；成员的业务身份（快递/集运/油运等）须结合
    # 主营业务核验，未核验者不得据此文本直接归类。
    parts.append(
        "成交额前五仅代表当日市场表现，不代表行业完整成员或基本面排序；"
        "各成员的业务身份须经主营业务核验后使用，未核验者按「身份未核验」处理"
    )
    parts.append("（当日行情快照，新浪行业板块口径，属市场表现维度，非景气度结论）")
    return "；".join(parts)

# ---------------------------------------------------------------------------
# A01（v35）：主题类型判定 —— 产业 / 风格 / 复合 / 通用
# ---------------------------------------------------------------------------
# 「低估板块」的「低估」是**筛选条件**而非产业名称；判定主题类型决定走哪条证据层：
# - industry  产业主题：命中 SECTOR_TOPIC_ALIASES 或板块名 → v34 板块行情层；
# - style     风格主题：命中风格词表且无产业限定 → v35 估值横截面层；
# - composite 复合主题：既有产业限定词又有风格词（如「低估的银行」）→ 两层同时注入，
#             横截面只在该产业限定的板块内聚合；
# - generic   通用主题：两者都不命中 → 沿用 v33 缺口声明，不硬凑证据。
THEME_KIND_INDUSTRY = "industry"
THEME_KIND_STYLE = "style"
THEME_KIND_COMPOSITE = "composite"
THEME_KIND_GENERIC = "generic"
THEME_KIND_LABELS: dict[str, str] = {
    THEME_KIND_INDUSTRY: "产业主题",
    THEME_KIND_STYLE: "风格主题",
    THEME_KIND_COMPOSITE: "复合主题",
    THEME_KIND_GENERIC: "通用主题",
}

# 风格词表（路线图 A01/A02 口径）：估值分位类 / 市值规模类 / 定性框架类 / 无数据类。
STYLE_ESTIMATE_WORDS: tuple[str, ...] = (
    "低估", "高估", "破净", "低市盈率", "高市盈率",
)
STYLE_SCALE_WORDS: tuple[str, ...] = ("大盘", "小盘")
STYLE_QUALITY_WORDS: tuple[str, ...] = ("绩优", "成长", "价值")
STYLE_DIVIDEND_WORDS: tuple[str, ...] = ("高股息", "红利")
# 全部风格词（长词在前，避免「低市盈率」被「低估」类短词截断匹配的语义歧义）。
STYLE_KEYWORDS: tuple[str, ...] = (
    STYLE_DIVIDEND_WORDS + STYLE_ESTIMATE_WORDS + STYLE_SCALE_WORDS
    + STYLE_QUALITY_WORDS
)


def detect_style_terms(topic: str) -> list[str]:
    """主题命中的风格词（去重、保持词表顺序）；未命中返回空列表。"""
    core = _topic_core(topic)
    if not core:
        return []
    return [word for word in STYLE_KEYWORDS if word in core]


def classify_topic(
    topic: str,
    sector_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A01：主题类型判定（纯函数）。

    返回 `{"theme_kind", "theme_kind_label", "style_terms", "sector_matched",
    "industry_terms"}`：sector_matched 表示是否命中行业板块（含别名映射，调用方
    传入板块清单时实算；未传清单时退化为「风格词是否占据整个主题」的保守判定）。
    """
    style_terms = detect_style_terms(topic)
    core = _topic_core(topic)
    sector_matched = False
    if sector_rows is not None:
        sector_matched = bool(match_sector_rows(topic, sector_rows))
    else:
        # 无板块清单时保守判定：核心词去掉全部风格词后仍有残留 → 视为含产业限定。
        remainder = core
        for word in style_terms:
            remainder = remainder.replace(word, "")
        sector_matched = bool(remainder)

    if style_terms and sector_matched:
        kind = THEME_KIND_COMPOSITE
    elif style_terms:
        kind = THEME_KIND_STYLE
    elif sector_matched:
        kind = THEME_KIND_INDUSTRY
    else:
        kind = THEME_KIND_GENERIC
    return {
        "theme_kind": kind,
        "theme_kind_label": THEME_KIND_LABELS[kind],
        "style_terms": style_terms,
        "sector_matched": sector_matched,
        "industry_terms": [core] if sector_matched and core else [],
    }


# ---------------------------------------------------------------------------
# A02（v35）：风格词 → 数据口径映射（口径文本作为证据的一部分下发，不藏在提示词里）
# ---------------------------------------------------------------------------
# 可用性三态：
# - available  有数据口径，产出证据；
# - framework  只有定性框架，无量化数据（成长/价值），如实声明不做数值断言；
# - unavailable 数据源无该字段（高股息/红利：估值源无股息率字段），声明缺口不做，
#   且**不产生空证据**。
STYLE_STATUS_AVAILABLE = "available"
STYLE_STATUS_FRAMEWORK = "framework"
STYLE_STATUS_UNAVAILABLE = "unavailable"
STYLE_STATUS_LABELS: dict[str, str] = {
    STYLE_STATUS_AVAILABLE: "可用（有数据口径）",
    STYLE_STATUS_FRAMEWORK: "仅定性框架（无量化数据）",
    STYLE_STATUS_UNAVAILABLE: "不可做（数据源无该字段）",
}
# 指标口径常量（与 valuation_evidence 的 _PERCENTILE_METRICS / _MIN_PERCENTILE_SAMPLES 同源语义）。
STYLE_PERCENTILE_SAMPLE_MIN = 20
STYLE_METRIC_ESTIMATE = "PE(TTM)/PB(MRQ) 历史分位"
STYLE_METRIC_SCALE = "总市值"
STYLE_METRIC_DIVIDEND = "股息率（DIVIDEND_YIELD）"

# 风格词 → 口径条目（status / metrics / condition / note）。
# 每条的声明文本可单独断言（验收要求），且与提示词口径声明共用同一来源。
STYLE_TERM_SPECS: dict[str, dict[str, Any]] = {
    "低估": {
        "status": STYLE_STATUS_AVAILABLE,
        "metrics": STYLE_METRIC_ESTIMATE,
        "condition": f"正数样本、历史样本不少于 {STYLE_PERCENTILE_SAMPLE_MIN} 个交易日",
        "note": "分位越低代表当前估值相对自身历史越便宜；分位 ≠ 涨跌预测。",
    },
    "高估": {
        "status": STYLE_STATUS_AVAILABLE,
        "metrics": STYLE_METRIC_ESTIMATE,
        "condition": f"正数样本、历史样本不少于 {STYLE_PERCENTILE_SAMPLE_MIN} 个交易日",
        "note": "分位越高代表当前估值相对自身历史越贵；分位 ≠ 涨跌预测。",
    },
    "破净": {
        "status": STYLE_STATUS_AVAILABLE,
        "metrics": "PB(MRQ) 低于 1（账面净资产以下）",
        "condition": "PB(MRQ) 为正数；净资产为负的个股不适用",
        "note": "破净是 PB 绝对水平判断，不依赖历史样本；亏损股 PB 仍可比。",
    },
    "低市盈率": {
        "status": STYLE_STATUS_AVAILABLE,
        "metrics": STYLE_METRIC_ESTIMATE,
        "condition": f"PE(TTM) 为正数（亏损股排除）、历史样本不少于 {STYLE_PERCENTILE_SAMPLE_MIN} 个交易日",
        "note": "PE 为负（亏损）无经济含义，须排除后比较。",
    },
    "高市盈率": {
        "status": STYLE_STATUS_AVAILABLE,
        "metrics": STYLE_METRIC_ESTIMATE,
        "condition": f"PE(TTM) 为正数（亏损股排除）、历史样本不少于 {STYLE_PERCENTILE_SAMPLE_MIN} 个交易日",
        "note": "PE 为负（亏损）无经济含义，须排除后比较。",
    },
    "大盘": {
        "status": STYLE_STATUS_AVAILABLE,
        "metrics": STYLE_METRIC_SCALE,
        "condition": "总市值（TOTAL_MARKET_CAP）横截面比较，无历史分位要求",
        "note": "规模是全市场横截面绝对量，与估值分位是不同维度，不得混为「便宜」。",
    },
    "小盘": {
        "status": STYLE_STATUS_AVAILABLE,
        "metrics": STYLE_METRIC_SCALE,
        "condition": "总市值（TOTAL_MARKET_CAP）横截面比较，无历史分位要求",
        "note": "规模是全市场横截面绝对量，与估值分位是不同维度，不得混为「便宜」。",
    },
    "绩优": {
        "status": STYLE_STATUS_FRAMEWORK,
        "metrics": "无量化口径（估值源不含盈利能力序列）",
        "condition": "—",
        "note": "只做定性框架讨论，禁止用估值分位冒充「绩优」的证据。",
    },
    "成长": {
        "status": STYLE_STATUS_FRAMEWORK,
        "metrics": "无量化口径（估值源不含增速序列）",
        "condition": "—",
        "note": "只做定性框架讨论，禁止编造增速数据。",
    },
    "价值": {
        "status": STYLE_STATUS_FRAMEWORK,
        "metrics": "无量化口径（估值源不含分红/现金流风格因子）",
        "condition": "—",
        "note": "只做定性框架讨论，禁止用估值分位冒充「价值」的完整证据。",
    },
    "高股息": {
        "status": STYLE_STATUS_UNAVAILABLE,
        "metrics": STYLE_METRIC_DIVIDEND,
        "condition": "—",
        "note": f"估值源（{VALUATION_SOURCE_LABEL}）无股息率字段，本主题不做数值断言，缺口如实声明。",
    },
    "红利": {
        "status": STYLE_STATUS_UNAVAILABLE,
        "metrics": STYLE_METRIC_DIVIDEND,
        "condition": "—",
        "note": f"估值源（{VALUATION_SOURCE_LABEL}）无股息率字段，本主题不做数值断言，缺口如实声明。",
    },
}

# 风格词缺口的补证动作（C03：每类缺口给出补证动作）。
STYLE_UNAVAILABLE_ACTION = "接入股息率数据源（估值源当前无 DIVIDEND_YIELD 字段）后扩展，本期不做数值断言。"
STYLE_FRAMEWORK_ACTION = "补充盈利/增速/风格因子数据源后可升级为量化口径，本期仅定性讨论。"


# ---------------------------------------------------------------------------
# M0（v36，2026-09-14 路线图 §2/§8）：四维低估判定阈值 —— 已冻结
# ---------------------------------------------------------------------------
# 冻结说明：经用户 2026-09-14 确认，按路线图 §2 表格初始值冻结（M0 闸门）。
# 铁律：所有分类阈值集中在**本常量区**，实现处一律引用常量，禁止散落硬编码；
#       冻结后若需调整口径，先改此处并同步单测，不在实现中就地改数值。
#
# 四维口径（维度 ↔ 数据来源）：
#   维度1 自身便宜度：代表股 PB 分位中位数（低 = 相对自身历史便宜）
#   维度2 盈利能力状态：PE 分位 − PB 分位 的背离（ROE 位置代理，正值大 = 估值跌而盈利未跟跌）
#   维度3 盈利广度：横截面亏损面（亏损家数 / 可比家数）
#   维度4 价格位置：250 日涨幅 / 距 52 周高点回撤 / 200 日均线（B01 趋势）
#
# 「便宜」≠「低估」：低分位只说明相对自身历史便宜，还须基本面未塌陷、位置不在高位。

# —— 分类标签（研究分类，非买卖指令） ——
VALUATION_JUDGMENT_LOW = "低估候选"
VALUATION_JUDGMENT_DEEP_FALL = "深跌未反转"
VALUATION_JUDGMENT_HIGH = "高位"
VALUATION_JUDGMENT_CYCLE_TOP = "盈利周期顶"
VALUATION_JUDGMENT_PENDING = "待定"
VALUATION_JUDGMENT_LABELS: dict[str, str] = {
    VALUATION_JUDGMENT_LOW: "低估候选",
    VALUATION_JUDGMENT_DEEP_FALL: "深跌未反转",
    VALUATION_JUDGMENT_HIGH: "高位",
    VALUATION_JUDGMENT_CYCLE_TOP: "盈利周期顶",
    VALUATION_JUDGMENT_PENDING: "待定",
}
# 判定顺序（§2）：高位 → 盈利周期顶 → 深跌未反转 → 低估候选 → 待定。
# 该顺序避免低分位板块被多重命中时归类混乱：先剔除「已在高位」，再识别盈利周期顶，
# 之后才判深跌与低估候选，最后兜底待定。
VALUATION_JUDGMENT_ORDER: tuple[str, ...] = (
    VALUATION_JUDGMENT_HIGH,
    VALUATION_JUDGMENT_CYCLE_TOP,
    VALUATION_JUDGMENT_DEEP_FALL,
    VALUATION_JUDGMENT_LOW,
    VALUATION_JUDGMENT_PENDING,
)

# —— 阈值常量（M0 冻结值） ——
# 高位：代表股 PB 分位中位数 ≥ 70%（自身历史高位；银行结构性低 PB 的典型情形）。
VALUATION_HIGH_PB_PERCENTILE_MIN = 70.0
# 便宜区间上界：PB 分位 ≤ 40% 视为「相对自身历史便宜」，是深跌/低估候选的共同前置。
VALUATION_CHEAP_PB_PERCENTILE_MAX = 40.0
# 盈利周期顶：PE 分位 ≤ 10% 且 PB 分位 ≥ 30%（盈利端极低 + 价格端未同步跌透）。
VALUATION_CYCLE_TOP_PE_PERCENTILE_MAX = 10.0
VALUATION_CYCLE_TOP_PB_PERCENTILE_MIN = 30.0
# ROE 塌陷背离：PE 分位 − PB 分位 ≥ 40 个百分点（价格跌到历史低位而盈利分位仍高）。
VALUATION_ROE_COLLAPSE_GAP_PP = 40.0
# 亏损面：横截面亏损占比。
# - ≥ 40% 触发深跌未反转的第二条件；同一数值在 A04 初筛闸门用作「排除出深取名单」的分界。
VALUATION_LOSS_RATIO_DEEP_FALL_MIN = 0.40
# - ≤ 30% 是低估候选的准入条件（盈利广度尚可）。
VALUATION_LOSS_RATIO_LOW_MAX = 0.30
# 初筛闸门（A04）：亏损面 ≥ 40% 的板块不入深取名单，改列「对照清单」（披露原因）。
VALUATION_PRESCREEN_LOSS_RATIO_MAX = 0.40
# 对照清单取 PB 中位数最低的前 N 名（保证报告仍能讨论地产链这类板块）。
VALUATION_PRESCREEN_CONTRAST_TOP_N = 3

# 分类标签尾注：研究分类，不是买卖指令（页脚「研究参考，不构成投资建议」保留）。
VALUATION_JUDGMENT_DISCLAIMER = "以上为研究分类，非买卖指令。"


# ---------------------------------------------------------------------------
# A01（v36）：四维判定数据模型（纯数据类，无 IO）
# ---------------------------------------------------------------------------
# 每个维度字段**缺失时如实为 None**，绝不填默认值：分类引擎按「待定 + 缺哪维」处理。
# 组装方（app.py）负责从既有结构聚合：
#   - `SectorValuationAggregate` → loss_ratio / pb_mrq_median / 可比家数
#   - `BoardPercentileAppendix`  → pb_percentile_median / pe_percentile_median / 分位可得股数
#   - 板块趋势聚合（B03）        → ret_250d / drawdown_52w / above_ma200 / ma200_gap_pct
# 注：`BoardTrendAggregate` 定义在 valuation_evidence（趋势数据源侧），本模块导入使用，
# 保持 direction → valuation 的单向依赖（与 VALUATION_SOURCE_LABEL 同约定）。
@dataclass(frozen=True)
class BoardValuationInput:
    """A01：单个板块的四维判定输入（分类引擎的唯一入参）。

    维度 ↔ 字段：
      维度1 自身便宜度 → `pb_percentile_median`（代表股 PB 分位中位数）
      维度2 盈利能力状态 → `pe_percentile_median`（与 PB 分位构成背离；ROE 位置代理）
      维度3 盈利广度 → `loss_ratio`（横截面亏损面）
      维度4 价格位置 → `trend`（B03 板块趋势聚合）

    披露字段（`*_sample_count` 等）用于「缺哪维」的如实说明与附录文本，不参与分类判定。
    """

    board_code: str
    board_name: str
    # 维度1：PB 分位中位数（None = 代表股分位全部不可得）。
    pb_percentile_median: float | None = None
    # 维度2：PE 分位中位数（None = 无正数 PE 样本可比）。
    pe_percentile_median: float | None = None
    # 维度3：亏损面（亏损股 / 可比样本；None = 无横截面样本）。
    loss_ratio: float | None = None
    # 维度4：趋势聚合（None = 该板块趋势全部不可得）。
    trend: BoardTrendAggregate | None = None
    # —— 披露字段（不进判定） ——
    pb_percentile_sample_count: int = 0
    loss_count: int = 0
    loss_total: int = 0
    trade_date: str = ""

    @property
    def roe_gap_pp(self) -> float | None:
        """维度2 合成量：PE 分位 − PB 分位（百分点）。

        正值大 → 价格分位低而盈利分位仍高，是「ROE 塌陷」签名（估值跌、盈利没跟跌）。
        任一维缺失则为 None——不做「缺一维按 0 算」这类会伪造背离的处理。
        """
        if self.pe_percentile_median is None or self.pb_percentile_median is None:
            return None
        return self.pe_percentile_median - self.pb_percentile_median

    @property
    def missing_dimensions(self) -> tuple[str, ...]:
        """缺失维度名（供「待定 + 缺哪维」的说明文本使用）。"""
        missing: list[str] = []
        if self.pb_percentile_median is None:
            missing.append("自身便宜度")
        if self.roe_gap_pp is None:
            missing.append("盈利能力状态")
        if self.loss_ratio is None:
            missing.append("盈利广度")
        if self.trend is None:
            missing.append("价格位置")
        return tuple(missing)


def _fmt_pct(value: float | None, digits: int = 1) -> str:
    """百分数展示（None → 「不可得」）；分位与亏损面均以 % 计。"""
    return "不可得" if value is None else f"{value * 100:.{digits}f}%"


def _fmt_percentile(value: float | None) -> str:
    """分位展示：分位值本身已是 0-100 的百分数，直接带 % 输出。"""
    return "不可得" if value is None else f"{value:.1f}%"


def classify_board_valuation(
    board: BoardValuationInput,
) -> tuple[str, str]:
    """A02（v36）:四维数据 → 分类标签 + 判定依据（纯函数）。

    判定顺序（§2，M0 冻结）：高位 → 盈利周期顶 → 深跌未反转 → 低估候选 → 待定。
    该顺序保证低分位板块被多重命中时归类稳定：先剔除「已在高位」，再识别盈利端极低的
    周期性顶部，之后才轮到深跌与低估候选，最后兜底待定。

    依据文本**逐维引用数值**（如「PB 分位 96.6% ≥ 70% → 高位」），供质量闸门做引用
    校验；引擎输出为**系统参考分类**，模型可推翻但须给数值论证（C01/C02）。

    返回 `(label, rationale)`；label 取 `VALUATION_JUDGMENT_LABELS` 之一。
    """
    name = board.board_name or board.board_code
    pb = board.pb_percentile_median
    pe = board.pe_percentile_median
    gap = board.roe_gap_pp
    loss = board.loss_ratio

    # —— 前置：维度1 不可得 → 无法定档，直接待定并注明缺维（顺序表的兜底项） ——
    if pb is None:
        missing = "、".join(board.missing_dimensions) or "自身便宜度"
        return (
            VALUATION_JUDGMENT_PENDING,
            f"待定：{name} 的代表股 PB 分位不可得（缺失维度：{missing}），无法判定相对自身历史的便宜度。",
        )

    # —— 1. 高位：PB 分位 ≥ 70% ——
    if pb >= VALUATION_HIGH_PB_PERCENTILE_MIN:
        return (
            VALUATION_JUDGMENT_HIGH,
            f"高位：{name} 代表股 PB 分位中位数 {pb:.1f}% ≥ {VALUATION_HIGH_PB_PERCENTILE_MIN:.0f}%，"
            f"相对自身历史已处高位区间（绝对 PB 低不改变这一相对位置）。",
        )

    # —— 2. 盈利周期顶：PE 分位 ≤ 10% 且 PB 分位 ≥ 30% ——
    if (
        pe is not None
        and pe <= VALUATION_CYCLE_TOP_PE_PERCENTILE_MAX
        and pb >= VALUATION_CYCLE_TOP_PB_PERCENTILE_MIN
    ):
        return (
            VALUATION_JUDGMENT_CYCLE_TOP,
            f"盈利周期顶：{name} PE 分位中位数 {pe:.1f}% ≤ "
            f"{VALUATION_CYCLE_TOP_PE_PERCENTILE_MAX:.0f}% 且 PB 分位 {pb:.1f}% ≥ "
            f"{VALUATION_CYCLE_TOP_PB_PERCENTILE_MIN:.0f}%，盈利端估值极低而价格端未同步跌透，"
            "提示盈利处周期高位。",
        )

    # —— 3. 深跌未反转：PB 分位 ≤ 40% 且（亏损面 ≥ 40% 或 ROE 塌陷背离 ≥ 40pp） ——
    if pb <= VALUATION_CHEAP_PB_PERCENTILE_MAX:
        loss_triggers = loss is not None and loss >= VALUATION_LOSS_RATIO_DEEP_FALL_MIN
        gap_triggers = gap is not None and gap >= VALUATION_ROE_COLLAPSE_GAP_PP
        if loss_triggers or gap_triggers:
            reasons: list[str] = [f"PB 分位 {pb:.1f}% ≤ {VALUATION_CHEAP_PB_PERCENTILE_MAX:.0f}%（相对自身历史便宜）"]
            if loss_triggers:
                reasons.append(
                    f"亏损面 {loss * 100:.1f}% ≥ {VALUATION_LOSS_RATIO_DEEP_FALL_MIN * 100:.0f}%"
                )
            if gap_triggers:
                reasons.append(
                    f"PE 分位 {pe:.1f}% − PB 分位 {pb:.1f}% = 背离 {gap:.1f}pp ≥ "
                    f"{VALUATION_ROE_COLLAPSE_GAP_PP:.0f}pp（ROE 塌陷签名）"
                )
            return (
                VALUATION_JUDGMENT_DEEP_FALL,
                f"深跌未反转：{name} " + "；".join(reasons) + "。价格跌到历史低位但基本面未反转，便宜而非低估。",
            )

    # —— 4. 低估候选：PB 分位 ≤ 40% 且 亏损面 ≤ 30% 且 无 ROE 塌陷背离 且 非高位 ——
    # （非高位已在第 1 步剔除；此处只需校验低分位 + 盈利广度 + 背离三项。）
    if pb <= VALUATION_CHEAP_PB_PERCENTILE_MAX:
        loss_ok = loss is not None and loss <= VALUATION_LOSS_RATIO_LOW_MAX
        gap_ok = not (gap is not None and gap >= VALUATION_ROE_COLLAPSE_GAP_PP)
        if loss_ok and gap_ok:
            gap_text = "背离不可得" if gap is None else f"背离 {gap:.1f}pp ＜ {VALUATION_ROE_COLLAPSE_GAP_PP:.0f}pp"
            critical_text = ""
            if gap is not None and is_critical_gap(gap):
                critical_text = " " + critical_gap_note(name, gap)
            return (
                VALUATION_JUDGMENT_LOW,
                f"低估候选：{name} PB 分位 {pb:.1f}% ≤ {VALUATION_CHEAP_PB_PERCENTILE_MAX:.0f}%，"
                f"亏损面 {loss * 100:.1f}% ≤ {VALUATION_LOSS_RATIO_LOW_MAX * 100:.0f}%，"
                f"{gap_text}，且未处高位——便宜且基本面未塌陷。{critical_text}",
            )

    # —— 5. 待定：落入空档（如 PB 分位 40-70% 之间，或亏损面介于 30-40% 无法归入深跌） ——
    parts = [f"PB 分位 {pb:.1f}%"]
    parts.append("PE 分位 " + _fmt_percentile(pe))
    parts.append("亏损面 " + _fmt_pct(loss))
    parts.append("背离 " + ("不可得" if gap is None else f"{gap:.1f}pp"))
    missing = "、".join(board.missing_dimensions)
    missing_text = f"；缺失维度：{missing}" if missing else ""
    return (
        VALUATION_JUDGMENT_PENDING,
        f"待定：{name} 四维数值（{'，'.join(parts)}）未落入任一判定区间{missing_text}。",
    )


# ---------------------------------------------------------------------------
# C06（M2，2026-09-15 第二轮路线图）：临界带（PE 分位 − PB 分位背离 30–40pp）
# ---------------------------------------------------------------------------
# 实测（2026-09-15 22:55「低估板块」样报）：白电 PE 分位 − PB 分位 = 背离 36.9pp，
# 未达 ROE 塌陷门槛（40pp），因此按现行阈值仍落「低估候选」，被当成最完整的修复故事。
# 口径：**标签不变**（分类总表分类集合不新增/不迁移），只加「临界」标记 + 下调研究优先级表述；
#      临界板块不得写成「最完整/最强/首选」的主线，须写明盈利端背离尚未收敛。
VALUATION_CRITICAL_GAP_MIN_PP = 30.0
VALUATION_CRITICAL_FLAG = "临界"


def is_critical_gap(gap_pp: float | None) -> bool:
    """C06：背离是否落在 30–40pp 临界带（左闭右开；≥40pp 属「深跌未反转」签名）。"""
    if gap_pp is None:
        return False
    return VALUATION_CRITICAL_GAP_MIN_PP <= gap_pp < VALUATION_ROE_COLLAPSE_GAP_PP


def board_is_critical_valuation(board: BoardValuationInput) -> bool:
    """C06：该板块的估值判定是否属「临界」（背离 30–40pp）。纯函数，不依赖标签。"""
    return is_critical_gap(board.roe_gap_pp)


def critical_gap_note(board_name: str, gap_pp: float) -> str:
    """C06：临界标记文本（进证据条目正文，与提示词规则 26 同源口径）。"""
    return (
        f"**{VALUATION_CRITICAL_FLAG}**：{board_name} PE 分位 − PB 分位 = 背离 {gap_pp:.1f}pp，"
        f"落在 {VALUATION_CRITICAL_GAP_MIN_PP:.0f}–{VALUATION_ROE_COLLAPSE_GAP_PP:.0f}pp 临界带"
        f"（贴近 ROE 塌陷 {VALUATION_ROE_COLLAPSE_GAP_PP:.0f}pp 门槛）——该低估值判定为临界："
        "不得写成最完整/最强/首选的主线，研究优先级下调，入选理由须写明盈利端背离尚未收敛。"
    )


# —— C06 闸门：临界板块被执行摘要拔高 ——
# 注：「最强证据」是执行摘要契约里的固定小标题，不能进拔高词表（否则每条摘要都命中）。
CRITICAL_BOARD_HYPE_TERMS: tuple[str, ...] = (
    "最完整", "最强主线", "最优主线", "首选", "优先级最高", "排序第一", "第一顺位", "最值得研究",
)
# 临界板块出现在摘要最前段 → 视为被摆在结论位置。
CRITICAL_BOARD_SUMMARY_HEAD_CHARS = 60
CRITICAL_BOARD_HYPE_WARN_THRESHOLD = 1


def find_critical_board_hype(
    summary: str, critical_boards: Iterable[str] | None
) -> list[dict[str, Any]]:
    """C06：临界板块被执行摘要拔高（同句含拔高词，或出现在摘要最前段）。

    只报「确定把临界板块当主线」的两种签名；不评价板块本身对不对，也不改写摘要——
    改写由提示词规则 26 与重跑承担（摘要内容属模型结论，服务端不代写）。
    """
    text = str(summary or "")
    boards = [str(board).strip() for board in (critical_boards or []) if str(board).strip()]
    if not text or not boards:
        return []
    head = _WHITESPACE.sub("", text)[:CRITICAL_BOARD_SUMMARY_HEAD_CHARS]
    out: list[dict[str, Any]] = []
    for board in dict.fromkeys(boards):
        if board not in text:
            continue
        in_head = board in head
        hype_terms: list[str] = []
        for fragment in _SENTENCE_SPLIT.split(text):
            if board not in fragment:
                continue
            hype_terms.extend(word for word in CRITICAL_BOARD_HYPE_TERMS if word in fragment)
        hype_terms = sorted(set(hype_terms))
        if not in_head and not hype_terms:
            continue
        reasons: list[str] = []
        if in_head:
            reasons.append(f"位于摘要前 {CRITICAL_BOARD_SUMMARY_HEAD_CHARS} 字")
        if hype_terms:
            reasons.append("同句出现「" + "、".join(hype_terms) + "」")
        out.append({
            "board": board,
            "in_head": in_head,
            "hype_terms": hype_terms,
            "reasons": reasons,
        })
    return out


def resolve_style_calibers(style_terms: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """A02：风格词 → 口径条目 + 缺口声明（纯函数）。

    返回 `{"entries", "caliber_text", "gaps", "available_terms", "unavailable_terms",
    "framework_terms"}`。口径文本与提示词共用同一来源，可单独断言。
    """
    entries: list[dict[str, Any]] = []
    gaps: list[str] = []
    available: list[str] = []
    framework: list[str] = []
    unavailable: list[str] = []
    seen: set[str] = set()
    for term in style_terms:
        word = str(term).strip()
        spec = STYLE_TERM_SPECS.get(word)
        if not word or word in seen or spec is None:
            continue
        seen.add(word)
        status = str(spec["status"])
        entries.append({
            "term": word,
            "status": status,
            "status_label": STYLE_STATUS_LABELS[status],
            "metrics": str(spec["metrics"]),
            "condition": str(spec["condition"]),
            "note": str(spec["note"]),
        })
        if status == STYLE_STATUS_AVAILABLE:
            available.append(word)
        elif status == STYLE_STATUS_FRAMEWORK:
            framework.append(word)
            gaps.append(f"风格词「{word}」无量化数据口径（{spec['metrics']}）——{STYLE_FRAMEWORK_ACTION}")
        else:
            unavailable.append(word)
            gaps.append(f"风格词「{word}」本期不可做：{spec['note']} {STYLE_UNAVAILABLE_ACTION}")

    lines: list[str] = []
    for entry in entries:
        lines.append(
            f"- 「{entry['term']}」（{entry['status_label']}）：指标 {entry['metrics']}；"
            f"样本条件 {entry['condition']}。{entry['note']}"
        )
    caliber_text = ""
    if lines:
        caliber_text = (
            "【筛选口径声明（风格主题）】用户主题含筛选条件词，系统按以下**声明口径**提供数据：\n"
            + "\n".join(lines)
            + "\n\n【低估判定口径（v36）】本主题的「低估」按**四维框架**判定，"
            "「便宜」（相对自身历史低分位）**不等于**「低估」——还须基本面未塌陷、位置不在高位。四维："
            "①自身便宜度＝代表股 PB 分位中位数；②盈利能力状态＝PE 分位 − PB 分位的背离（ROE 位置代理）；"
            "③盈利广度＝横截面亏损面（亏损家数 / 可比家数）；④价格位置＝250 日涨幅 / 距 52 周高点回撤 / 200 日均线。\n"
            f"分类规则来源：系统按判定顺序 {' → '.join(VALUATION_JUDGMENT_ORDER)} 逐级判定，"
            "阈值见证据块中每条板块证据的分类依据文本。"
            "系统给出的是**研究分类**（" + "／".join(VALUATION_JUDGMENT_ORDER) + "），不是买卖指令；"
            "模型**可推翻系统参考分类**，但推翻必须在正文给出数值论证（逐维引用四维数值）。"
            f"{VALUATION_JUDGMENT_DISCLAIMER}"
        )
    return {
        "entries": entries,
        "caliber_text": caliber_text,
        "gaps": gaps,
        "available_terms": available,
        "framework_terms": framework,
        "unavailable_terms": unavailable,
    }


def style_caliber_declaration(style_terms: list[str] | tuple[str, ...]) -> str:
    """A02：口径声明文本（供证据包与提示词共用）；无可用风格词时返回空串。"""
    return str(resolve_style_calibers(style_terms)["caliber_text"])


# ---------------------------------------------------------------------------
# A03（v35）：复合主题双解析 —— 产业限定 × 风格筛选
# ---------------------------------------------------------------------------
def resolve_theme_scope(
    topic: str,
    sector_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A03：把主题解析为「产业限定 + 风格筛选」双通道（纯函数）。

    - 产业限定：命中板块清单的主题返回板块行（`sector_rows_matched`），复合主题的估值
      横截面**只在该限定的板块内聚合**（`board_codes` 为东财 BOARD_CODE 白名单，因两份
      分类体系不同，此处按板块**名**在聚合结果侧过滤，白名单为空表示不限板块）；
    - 风格筛选：命中的风格词与口径条目（`style_terms` / `calibers`）；
    - `limited` 为 True 表示横截面必须收窄到产业限定范围（复合主题）。

    新浪行业板块（行情层）与东财行业板块（估值层）是两套分类，不能把前者的 code 当作
    后者的 BOARD_CODE 使用；本函数只产出**板块名**口径，由调用方在聚合侧按名匹配。
    """
    matched_rows = match_sector_rows(topic, sector_rows) if sector_rows is not None else []
    base = classify_topic(topic, sector_rows)
    names = [str(row.get("name") or "").strip() for row in matched_rows]
    names = [name for name in names if name]
    style_terms = base["style_terms"]
    calibers = resolve_style_calibers(style_terms)
    return {
        "theme_kind": base["theme_kind"],
        "theme_kind_label": base["theme_kind_label"],
        "style_terms": style_terms,
        "calibers": calibers,
        "sector_rows_matched": matched_rows,
        "sector_names": names,
        "limited": base["theme_kind"] == THEME_KIND_COMPOSITE and bool(names),
    }


def sector_name_matches(candidate: str, sector_names: list[str] | tuple[str, ...]) -> bool:
    """A03：板块名是否落在产业限定白名单内（去「行业」后缀后互为包含，容忍后缀差异）。

    新浪板块名（如「金融行业」）与东财 BOARD_NAME（如「银行」）命名不总一致，故用
    「去后缀 + 双向包含」做宽松匹配：名称互不包含时判不命中，宁少不错配。
    """
    name = str(candidate or "").strip().removesuffix(_SECTOR_NAME_SUFFIX)
    if not name:
        return False
    for raw in sector_names:
        target = str(raw or "").strip().removesuffix(_SECTOR_NAME_SUFFIX)
        if not target:
            continue
        if name in target or target in name:
            return True
    return False


def expand_scope_board_names(topic: str, sector_names: list[str] | tuple[str, ...]) -> list[str]:
    """A03：把产业限定板块名展开为「可用于匹配东财 BOARD_NAME」的名字集合（纯函数）。

    背景：新浪行业板块（行情层）与东财行业板块（估值层）是两套分类，命名不总一致
    ——用户主题「低估的银行」的产业限定命中新浪板块「金融行业」，而东财口径的板块名是
    「银行」「证券」「保险」等，「金融行业」与「银行」双向包含都不成立。

    因此在已有板块名的**基础上**补入主题核心词本身（如「银行」）：横截面聚合侧按
    「板块名 ⊆ 集合内任一名 或 任一名 ⊆ 板块名」过滤时，「银行」即可命中东财「银行」板块。
    只做名称层面的补入，不引入跨分类的 code 映射；宁少不错配。
    """
    names = [str(item).strip() for item in sector_names if str(item).strip()]
    core = _topic_core(topic)
    for word in STYLE_KEYWORDS:
        core = core.replace(word, "")
    core = core.strip()
    if core and core not in names:
        names.append(core)
    return names


# ---------------------------------------------------------------------------
# C03（v35）：风格主题的四类缺口与「不可做」声明
# ---------------------------------------------------------------------------
# 路线图要求风格主题的 `data_gaps` **必须覆盖** 四类，且每类给出补证动作：
#   1. 无数据口径的风格词（高股息/红利）→ 接入股息率数据源后扩展（A02 已产出，此处统一编号）；
#   2. 横截面取数失败（全市场当日估值行拉不到）→ 估值源恢复后重跑；
#   3. 代表股分位不可得（板块分位代理无样本）→ 估值源恢复后重跑 / 仅保留中位数；
#   4. 候选池估值缺失（逐股回链失败或冷却）→ 估值源恢复后重跑补齐。
#
# 与既有「未命中行业板块清单」缺口的关系：产业限定未命中（v34）说明的是**板块行情层**
# 无证据；风格筛选无数据依据说明的是**估值横截面层**无证据，两者是不同证据层的缺口。
# 为避免同一句话在 `data_gaps` 里重复堆叠，本模块对四类缺口统一按 `(code, text)` 去重，
# 并在文本上刻意与「未命中行业板块清单」区分措辞（前者提「板块行情证据」，
# 后者提「估值横截面证据」）。
GAP_CODE_STYLE_UNDATA = "style_no_data"
GAP_CODE_CROSS_SECTION_FAILED = "valuation_cross_section_failed"
GAP_CODE_SECTOR_PERCENTILE_UNAVAILABLE = "sector_percentile_unavailable"
GAP_CODE_POOL_VALUATION_MISSING = "pool_valuation_missing"
GAP_CODE_LABELS: dict[str, str] = {
    GAP_CODE_STYLE_UNDATA: "无数据口径的风格词",
    GAP_CODE_CROSS_SECTION_FAILED: "估值横截面取数失败",
    GAP_CODE_SECTOR_PERCENTILE_UNAVAILABLE: "代表股分位不可得",
    GAP_CODE_POOL_VALUATION_MISSING: "候选池估值缺失",
}

# 各缺类的补证动作（C03：每类缺口给出补证动作，不得只声明缺口不给下一步）。
GAP_ACTION_RESUME_VALUATION = f"估值源（{VALUATION_SOURCE_LABEL}）恢复后重跑即可补齐。"
GAP_ACTION_CROSS_SECTION = (
    f"待估值源（{VALUATION_SOURCE_LABEL}）恢复后重跑；本期风格筛选无数据依据，"
    "不得以模型既有知识代替横截面证据。"
)
GAP_ACTION_SECTOR_PERCENTILE = (
    "代表股分位取数失败或样本不足（PB 分位需正数样本），"
    f"待估值源（{VALUATION_SOURCE_LABEL}）恢复后重跑；本期该板块仅保留估值中位数。"
)

# 已产出四类缺口中的「无数据口径的风格词」（A02 的 `STYLE_*_ACTION` 文本）。
_GAP_ACTION_STYLE_UNDATA = STYLE_UNAVAILABLE_ACTION
_GAP_ACTION_STYLE_FRAMEWORK = STYLE_FRAMEWORK_ACTION

# 既有 v34 缺口文本（风格主题不应重复堆叠同一句话；此处用于去重与措辞区分校验）。
LEGACY_GAP_SECTOR_UNMATCHED_MARKER = "未命中行业板块清单"


def build_style_gap_entry(code: str, text: str) -> dict[str, str]:
    """C03：构造单条缺口条目（纯函数）。

    条目为 `{"code", "label", "text"}`，`code` 供去重与测试断言，`text` 直接进
    `data_gaps`。不带补证动作的裸缺口不允许进入数据（由调用方保证文本含动作）。
    """
    return {"code": str(code), "label": GAP_CODE_LABELS.get(code, ""), "text": str(text).strip()}


def style_gap_entries(
    *,
    style_terms: list[str] | tuple[str, ...] = (),
    cross_section_ok: bool = True,
    sector_percentile_failures: list[str] | tuple[str, ...] = (),
    pool_valuation_failures: list[str] | tuple[str, ...] = (),
) -> list[dict[str, str]]:
    """C03：风格主题四类缺口一次性产出（纯函数，无 IO，不依赖板块清单）。

    参数为**已发生的取数结果**（由 app.py 在真实取数后传入），本函数只负责归类与措辞：
    - `style_terms` 命中「不可做/仅框架」风格词 → 第 1 类缺口（每词一条，A02 同源文本）；
    - `cross_section_ok=False` → 第 2 类缺口（横截面失败）；
    - `sector_percentile_failures` 非空 → 第 3 类缺口（代表股分位不可得，列出板块名）；
    - `pool_valuation_failures` 非空 → 第 4 类缺口（候选池估值缺失，列出股票）。

    返回条目顺序固定（1→4），同 `code` 只保留首条；空参不产出空缺口（不硬凑）。
    """
    calibers = resolve_style_calibers(style_terms) if style_terms else None
    entries: list[dict[str, str]] = []

    if calibers:
        for word in calibers["unavailable_terms"]:
            entries.append(build_style_gap_entry(
                GAP_CODE_STYLE_UNDATA,
                f"风格词「{word}」本期不可做：{STYLE_TERM_SPECS[word]['note']} {_GAP_ACTION_STYLE_UNDATA}",
            ))
        for word in calibers["framework_terms"]:
            entries.append(build_style_gap_entry(
                GAP_CODE_STYLE_UNDATA,
                f"风格词「{word}」无量化数据口径（{STYLE_TERM_SPECS[word]['metrics']}）"
                f"——{_GAP_ACTION_STYLE_FRAMEWORK}",
            ))

    if not cross_section_ok:
        entries.append(build_style_gap_entry(
            GAP_CODE_CROSS_SECTION_FAILED,
            f"板块估值横截面取数失败（{VALUATION_SOURCE_LABEL}）——{GAP_ACTION_CROSS_SECTION}",
        ))

    failures = [str(item).strip() for item in sector_percentile_failures if str(item).strip()]
    if failures:
        shown = "、".join(failures[:5])
        more = f"等 {len(failures)} 个板块" if len(failures) > 5 else ""
        entries.append(build_style_gap_entry(
            GAP_CODE_SECTOR_PERCENTILE_UNAVAILABLE,
            f"板块「{shown}」{more}的代表股分位不可得——{GAP_ACTION_SECTOR_PERCENTILE}",
        ))

    pool_failures = [str(item).strip() for item in pool_valuation_failures if str(item).strip()]
    if pool_failures:
        shown = "、".join(pool_failures[:5])
        more = f"等 {len(pool_failures)} 只" if len(pool_failures) > 5 else ""
        entries.append(build_style_gap_entry(
            GAP_CODE_POOL_VALUATION_MISSING,
            f"候选池估值证据缺失（{shown}{more}）：{GAP_ACTION_RESUME_VALUATION}",
        ))

    # 同 code 只保留首条（避免同一缺类多条堆叠）；文本级去重兜底。
    deduped: list[dict[str, str]] = []
    seen_codes: set[str] = set()
    seen_texts: set[str] = set()
    for entry in entries:
        if entry["code"] in seen_codes or entry["text"] in seen_texts:
            continue
        seen_codes.add(entry["code"])
        seen_texts.add(entry["text"])
        deduped.append(entry)
    return deduped


def merge_data_gaps(existing: list[str], additions: list[str]) -> list[str]:
    """C03：把新增缺口并入既有 `data_gaps`——去重且保持首次出现顺序。

    用于把 A02/C03 产出的缺口并入模型自述的 `data_gaps`，防止同一句话重复堆叠
    （含与既有「未命中行业板块清单」类文本的重复）。
    """
    merged: list[str] = []
    seen: set[str] = set()
    for raw in list(existing) + list(additions):
        text = str(raw).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        merged.append(text)
    return merged


_SYSTEM = (
    "你是严谨的产业研究方向研究员：基于给定证据（如有）与既有知识输出方向研判。"
    "绝不做任何买卖建议；证据不足时如实说明数据缺口；"
    "「行业增长」不得直接推出「某公司利润增长」——产业景气、公司商业质量、"
    "长期竞争力、资金情绪、技术信号必须分开陈述。"
    "禁止为凑数量编造龙头、市场份额或客户关系；候选公司代码必须真实存在，"
    "不确定的宁可不列。输出必须且只能是单个合法 JSON 对象。"
)

# —— 模型输出 JSON 契约（B02/B05/B11/B12） ——
_DIRECTION_SCHEMA_TEXT = (
    '{"title": "方向研判标题（含核心矛盾）", '
    '"executive_summary": "执行摘要（150-220 字：方向判断 + 最强证据 + 最大的不确定性；'
    '风格主题还须直接回答三问——哪些板块满足低估判定、哪些只是跌得久未反转、哪些已处高位）", '
    '"report": "研判正文（markdown，字数按模式预算；必须包含以下十个 ## 小节，'
    '每节按「事实 → 推理 → 不确定性」展开，禁止泛泛的「政策支持、空间广阔」：\\n'
    + "".join(f"## {name}\\n" for name in REQUIRED_DIRECTION_SECTIONS)
    + '）", '
    '"core_judgments": [{"id": "J1", "text": "核心判断原文", '
    '"kind": "fact|industry|competitiveness|sentiment|technical（判断分层）", '
    '"support_refs": ["E1"]（证据编号或空数组）, '
    '"confidence": "low|medium|high", "missing": ["还缺什么数据"]}], '
    '"catalysts": ["2-4 条关键催化"], "risks": ["2-4 条主要风险"], '
    '"stock_pool": [{"symbol_raw": "模型原始代码", "symbol": "标准 6 位代码", "name": "公司名", '
    '"exchange": "sh|sz|bj|未知", "sector": "产业链环节", '
    '"value_chain_position": "产业链位置（上游/中游/下游 + 一句话定位）", '
    '"business_link": "实际业务关联（做什么、与主题的关系；占比取不到就写未知）", '
    '"profit_path": "业绩兑现路径（主题景气如何变成这家公司的收入利润）", '
    '"support_refs": ["E1"], "counter_evidence": "主要反证", "gaps": ["证据缺口"]}], '
    # D02（M3）：前置行业比较表——结构化输出，前端表格与导出共用同一份数据。
    '"industry_comparison": [{"industry": "细分行业/板块名", '
    '"valuation_evidence": "估值证据（指标名 + 数值 + 基准期 + [E#]；不可得写「不可得」）", '
    '"profit_change": "盈利变化（量价/收入/利润的量级与方向，注明期间与来源）", '
    '"catalyst": "重估催化（具体事件或数据）", "horizon": "催化兑现的时间窗", '
    '"counter_evidence": "主要反证", "research_priority": "高|中|低", '
    '"priority_basis": "研究优先级依据（说明为何排前/后：证据完整度、催化临近度、估值水位）", '
    '"evidence_completeness": "证据完整度（可得项/缺失项，如「估值可得、分位缺失」）"}], '
    '"data_gaps": ["数据缺口清单（证据模式下必填）"], '
    '"next_verification": "下一验证动作（看什么指标、在哪里看、什么时间窗）"}\n'
    "铁律：\n"
    "1. **禁止输出任何价格字段**（不给 reference_price、不给目标价）——价格由行情数据提供；\n"
    "2. stock_pool 给 3-8 家可解释候选；确实找不到就给空数组并在 data_gaps 解释原因，禁止编造；\n"
    "3. core_judgments 至少 3 条且必须分层（industry/competitiveness/sentiment 至少各一条，"
    "没有数据支撑的层就如实缺失）；无证据支持的判断 confidence 只能 low；\n"
    "4. 每个候选的 business_link / profit_path 必须具体到业务与报表科目，泛泛的「受益于行业增长」不合格；\n"
    "5. 引用证据时只允许使用给定证据块的编号（E1、E2…）；没有对应证据就留空数组。"
)

_KNOWLEDGE_EXTRA_RULES = (
    "\n6. 本次**没有可用证据包**：你的输出是「知识概览」而非「证据研判」——"
    "必须在 executive_summary 开头声明「知识概览（基于模型既有知识，非实时）」，"
    "并在 data_gaps 里列出需要哪些实时证据才能升级为证据研判；"
    "所有涉及时效的判断 confidence 一律 low。"
)

_EVIDENCE_EXTRA_RULES = (
    "\n6. 证据块中的数据必须在正文对应小节引用（带 [E#] 编号）；"
    "证据未覆盖的部分如实标注「证据未覆盖」，不要用既有知识冒充实时数据；"
    "多家转载同一事实只引用一次，不靠引用数量放大证据强度。"
)

# —— C01（v35 立规则 / v36 立场重写）：风格主题提示词适配 ——
# 风格主题（低估/破净/低市盈率等）走估值横截面证据链：口径声明前置，两个指定小节
# 必须引用估值横截证据编号；候选池优先从证据中的代表股/板块成分提名。
#
# v36 立场重写：原口径声明含「不预设立场、不直接下「低估/高估」结论」，使模型只能罗列数据
# 而无法回答「谁被低估」。现改为**有立场的研究分类**：系统先给参考分类，模型须正面回答三问、
# 输出分类总表，可推翻但要数值论证；「本期无低估候选」是合法结论。
STYLE_SECTION_REQUIREMENTS = ("需求供给与价格", "景气阶段")
# C01：执行摘要必须直接回答的三个问题（顺序即作答顺序，逐项作答不得合并）。
STYLE_SUMMARY_QUESTIONS: tuple[str, ...] = (
    "哪些板块满足低估判定（低估候选）",
    "哪些板块只是跌得久、尚未反转（深跌未反转）",
    "哪些板块已处高位（高位 / 盈利周期顶）",
)
# C01：分类总表的表头列（板块 × 四维数值 × 分类 × 依据）。
STYLE_TABLE_COLUMNS: tuple[str, ...] = (
    "板块", "PB 分位", "PE 分位", "亏损面", "价格位置", "分类", "依据与引用",
)
# C01：三问的英文/结构化键名（供单测与前端断言，不参与提示词渲染）。
STYLE_SUMMARY_QUESTION_KEYS: tuple[str, ...] = ("low", "deep_fall", "high")
# C01：空候选合法结论的表述要求（本期无低估候选时必须如实说明并给出最接近的板块与差距）。
STYLE_EMPTY_CANDIDATE_CLAUSE = "本期无低估候选"
_STYLE_EXTRA_RULES = (
    "\n7. **风格主题口径优先**：上面的「筛选口径声明」与「低估判定口径」是本主题的数据字典——"
    "所有估值水位表述（历史中低位、估值修复、处于低位/高位、破净、分位）"
    "必须引用证据块中的估值横截证据编号 [E#]，并写清指标名、数值与基准期；"
    "无对应证据的估值表述一律不写（宁可写「证据未覆盖」）。\n"
    "8. 正文的「需求供给与价格」与「景气阶段」两节**必须引用估值横截证据编号**"
    "（板块估值中位数、可比样本数或代表股分位），不得用训练知识代替。\n"
    "9. **候选池象限约束**：候选池优先从证据中的**板块代表股与成分股**提名（有估值分位回链），"
    "每只候选需说明其估值数据（当前值 + 分位或同业位次）；"
    "证据未覆盖的候选须在 gaps 中说明估值数据缺口。象限规则见下方第 16 条。\n"
    "10. 声明「无量化口径」的风格词（如成长/价值）只做定性讨论；"
    "声明「不可做」的风格词（如高股息/红利）不得给出数值断言。\n"
    "11. **执行摘要必须直接回答三个问题（逐项作答，不得合并、不得回避）**：\n"
    "    (1) 哪些板块满足低估判定（低估候选）？(2) 哪些板块只是跌得久、尚未反转（深跌未反转）？"
    "(3) 哪些板块已处高位（高位 / 盈利周期顶）？"
    "三问的作答必须点名板块并给出对应四维数值，禁止只写「估值分化」这类无结论表述。\n"
    "12. **正文必须包含分类总表**：一张 markdown 表格，列为"
    "「板块 | PB 分位 | PE 分位 | 亏损面 | 价格位置 | 分类 | 依据与引用」，"
    "覆盖证据块中全部板块；每格的四维数值必须带对应的 [E#] 引用；"
    "数值不可得的格写「不可得」，不得留空或编造。\n"
    "13. **分类判定须可推翻，推翻须给数值论证**：证据中的分类是**系统参考分类**；"
    "若你认为某个板块的系统分类有误，可以推翻，但必须在该板块处写出逐维数值论证"
    "（说明哪一维与系统判定不符、差多少），并在 core_judgments 中单独成条。"
    "无论证不得推翻，也不得照抄系统分类而不作说明。\n"
    f"14. **空候选是合法结论**：若没有板块满足低估判定，须如实写明「{STYLE_EMPTY_CANDIDATE_CLAUSE}」，"
    "并给出**最接近低估候选的 1-2 个板块及其与门槛的差距**（逐维说明差在哪一维、差多少）；"
    "禁止为凑出结论把「深跌未反转」或「高位」板块包装成低估候选。"
)

# 复合主题（同时有产业限定与风格筛选）：在风格规则上叠加产业模板提示。
_COMPOSITE_EXTRA_RULES = (
    "\n15. **复合主题**：本主题同时含产业限定与风格筛选——估值横截面已在产业限定范围内"
    "聚合（见证据条目标题中的「限定板块」），产业板块行情快照与估值横截面**两类证据都在**，"
    "须分别引用、不得互相替代；产业定性沿用上面的行业指标模板。"
)

# C02（v36）候选池象限约束（规则 16）：文本来自 `POOL_QUADRANT_GUIDE`（与质量闸门同源），
# 单独成常量以便单测断言「提示词与闸门口径一致」。
_POOL_QUADRANT_EXTRA_RULE = "\n16. **候选池象限约束**：{guide}"

# v37：宏观数据时效性规则（规则 17）。用户反馈：报告把「美国 CPI 未公布」当作最大不确定性，
# 而该数据已于窗口内发布。证据条目现在带「发布阶段 + 实际值」，提示词须强制模型按阶段使用。
MACRO_TIMING_RULE = (
    "17. **宏观数据时效性（必读，违反即结论不可信）**：宏观日历证据条目带**发布阶段**——"
    "「窗口已过」表示按官方规则推算该数据应已发布，「窗口进行中」表示官方可能已发布，"
    "两者都**不得**再写成「数据未公布」或把它当作未来的最大不确定性；"
    "若条目同时给出**实际值**，必须直接采用该数值进行研判（引用 [E#]）；"
    "若条目写明「实际值未接入本管道」，只能如实声明「本地管道未接入该指标实际值，"
    "须查官方发布」，**禁止**据此推断「数据尚未公布」，也禁止用训练知识编造数值。"
    "只有「窗口未开始」的事件才可作为待验证的未来催化。"
    "若条目给出**同一指标的多个统计口径**（如美国 CPI「未季调」与「季调」两个读数），"
    "必须以**与官方发布对齐的那个口径**为主进行引用，并在需要判断趋势或与官方数字对照时"
    "**同时**说明两个口径的读数与差异——不得只挑一个数字，也不得把口径差异当成数据矛盾。"
)


def pool_quadrant_rule_text() -> str:
    """C02：候选池象限约束的提示词规则文本（单一来源，供 `build_messages` 与单测共用）。"""
    return _POOL_QUADRANT_EXTRA_RULE.format(guide=POOL_QUADRANT_GUIDE)


# —— M3（D02/D03/D04/D07）：研究判断与报告质量规则（对所有形态生效） ——
M3_QUALITY_RULES = (
    "\n18. **前置行业比较表（D02）**：必须输出 `industry_comparison` 数组，"
    "覆盖证据块实际命中的每个细分行业/板块（不是全市场）；每行的字段全填："
    "估值证据、盈利变化、催化、时间窗、主要反证、研究优先级、优先级依据、证据完整度。"
    "优先级只给「高/中/低」并写依据（证据完整度、催化临近度、估值水位），"
    "**禁止编造数值评分**；数据不可得的格写「不可得」，不得留空。"
    "执行摘要必须回答：先研究谁、为什么、等待什么变化。\n"
    "19. **限制跨层因果推断（D03）**：严格区分「事实」「假设」「待验证判断」。"
    "宏观变量（CPI/PPI/油价/利率/汇率/PMI 等）**不得直接推出盈利结论**——"
    "必须经量价、单位成本、单票收入、产能利用率、开工率、订单等中间环节数据（带 [E#]）"
    "才能支撑；中间数据缺失时只能写成「待验证假设」并给出验证动作，不得写成结论。"
    "（例：低 CPI 不能直接推出快递件量下降；高美国 CPI 不能直接推出航运需求增长。）\n"
    "20. **周期行业估值审查（D04）**：周期类行业（资源/化工/钢铁/航运/建材/煤炭/有色等）"
    "不得因周期顶点盈利抬高而判低估；须结合可得的正常化盈利、经营性现金流、"
    "运力订单/资本开支识别「低 PE 陷阱」，并**披露正常化口径、样本区间与假设**；"
    "价格低位不能替代基本面改善证据。\n"
    "21. **引用必须支持结论（D07）**：每个 [E#] 引用必须真的支撑同句结论——"
    "该编号对应条目须含该句的关键数值/期间/口径；只挂编号而数值不在该条目里的写法不合格；"
    "宁可不引用（写「证据未覆盖」）。\n"
    # —— M1（2026-09-15 第二轮路线图 B02/B03）：候选身份契约 ——
    # 实测（22:55 样报）：6 只候选的 symbol 全是公司名 → 服务端归一失败 → 6 只全部「无效代码」，
    # 无法送战法雷达。证据块里其实一直写着「中国通号(688009)」，是模型没把它填进 symbol。
    "22. **候选身份契约（违反即候选作废）**：`stock_pool[].symbol` **只能是 6 位数字 A 股代码**"
    "（如 688009、000651），不得填公司名、简称、拼音或带市场前缀的写法；"
    "`symbol_raw` 保留你看到的原始写法；`name` 填公司中文名。"
    "不确定代码就不要把这家公司放进候选池（宁少不错）。\n"
    "23. **候选优先从证据代表股抽取（低估/风格主题必读）**：候选池必须以证据块中"
    "**已有的代表股/成分股**为主体——证据里已经写成「中国通号(688009)」这种「名称(代码)」形式，"
    "请直接取其中的代码与名称，你只负责填写 `business_link` / `profit_path` / `support_refs` / "
    "`counter_evidence` / `gaps`。**禁止自行发明龙头公司**，也禁止把公司名当代码。\n"
    # —— M2（C02/C04/C06）——
    "24. **产业景气层必须单独成条（C02）**：`core_judgments` 必须含 `kind=industry` 一条；"
    "没有量价/订单/排产/盈利等中间证据时，该条只能写成「证据未覆盖，不能从低 PB 推出景气修复」，"
    "`confidence` 只能是 low——不得写成已验证的盈利反转或景气修复。\n"
    "25. **分类判定必须就地引用四维证据（C04）**：凡在小节里写出「低估候选 / 深跌未反转 / 高位 / "
    "盈利周期顶」这类分类词并点到了板块名，**该句内必须引用该板块的估值证据编号 [E#]**"
    "（该编号条目含该板块 PB 分位/PE 分位/亏损面/价格位置）。点板块名却不引用它的估值条目，"
    "闸门判为「分类判定不可核对」。\n"
    "26. **临界低估与弱趋势不得当最强主线（C06）**：证据条目里被标为「临界」的板块"
    "（PE 分位 − PB 分位背离 30-40pp）**不得**被写成「最完整/最强/首选」的修复故事，"
    "执行摘要也不得把它排在最前；站上 200 日均线为 `0/N` 的板块可以留在池里，"
    "但研究优先级要下调，且入选理由必须写明「趋势未确认」。"
)

# 形态专属指令（D01）：局部研判收窄断言范围，阶段性研究改短篇 + 补证动作。
_FORM_INSTRUCTIONS: dict[str, str] = {
    DIRECTION_FORM_FULL: "",
    DIRECTION_FORM_PARTIAL: (
        "\n【报告形态：局部研判】本次证据只覆盖部分行业/板块——"
        "只对有证据的部分下结论，未覆盖的子行业与未取得的数据在数据缺口**集中说明一次**；"
        "必需小节为「研究问题与期限」「需求供给与价格」「催化与反证」「下一验证动作」；"
        "其余小节缺失不扣分，**不得为凑齐十节写「无法判断」的空话**。"
    ),
    DIRECTION_FORM_STAGE: (
        "\n【报告形态：阶段性研究】本次关键数据缺失——**不要凑十节长文**："
        f"篇幅压到 {DIRECTION_STAGE_CHAR_MIN}-{DIRECTION_STAGE_CHAR_MAX} 字，"
        "只需写「研究问题与期限」「下一验证动作」两节（其余内容并入这两节）；"
        "必须给出可执行的补证动作清单（看什么指标、在哪里看、什么时间窗），"
        "并明确说明「本次为阶段性研究，证据补齐后可升级为正式研判」。"
    ),
}
_FULL_SECTIONS_BLOCK = "".join(f"## {name}\n" for name in REQUIRED_DIRECTION_SECTIONS)
# 契约文本里的节列表用**字面 `\n`**（JSON 字符串内的换行转义）书写，与真换行是两种形态：
# 两者都要能识别，否则替换静默失败（2026-09-15 实测踩坑）。
_FULL_SECTIONS_BLOCK_JSON = "".join(f"## {name}\\n" for name in REQUIRED_DIRECTION_SECTIONS)


def direction_schema_text(form: str | None = None) -> str:
    """D01：按报告形态给出 JSON 契约文本（必需小节与篇幅随形态收窄）。

    未知/缺省形态 → 完整研判契约（与历史提示词逐字一致，保证既有测试与回归不漂移）。
    节列表替换同时兼容「真换行」与「字面 \\n」两种书写形态；都不匹配时回落完整契约，
    绝不多输出/少输出小节（宁保守不静默出错）。
    """
    normalized = str(form or "")
    if normalized not in FORM_REQUIRED_SECTIONS or normalized == DIRECTION_FORM_FULL:
        return _DIRECTION_SCHEMA_TEXT
    sections = FORM_REQUIRED_SECTIONS[normalized]
    label = DIRECTION_FORM_LABELS[normalized]
    for old, separator in (
        (_FULL_SECTIONS_BLOCK_JSON, "\\n"),
        (_FULL_SECTIONS_BLOCK, "\n"),
    ):
        if old not in _DIRECTION_SCHEMA_TEXT:
            continue
        block = "".join(f"## {name}{separator}" for name in sections)
        text = _DIRECTION_SCHEMA_TEXT.replace(old, block)
        text = text.replace(
            "必须包含以下十个 ## 小节",
            f"必须包含以下 {len(sections)} 个 ## 小节（{label}形态）",
        )
        return text
    return _DIRECTION_SCHEMA_TEXT


def direction_mode_budget(mode: str | None) -> tuple[str, str, int, int]:
    """模式 → (归一化模式名, 中文标签, 字数下限, 字数上限)；未知模式报错，不静默回落。"""
    normalized = str(mode or DEFAULT_DIRECTION_MODE).strip().lower()
    entry = DIRECTION_MODES.get(normalized)
    if entry is None:
        raise ValueError(f"未知方向研判模式：{mode or '（空）'}（支持：{'/'.join(DIRECTION_MODES)}）")
    return normalized, str(entry["label"]), int(entry["min"]), int(entry["max"])


def section_bodies(report: str) -> list[dict[str, Any]]:
    """正文每个小节标题与去空白字数（同名合并计数；标题行不计）。"""
    entries: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    current_name: str | None = None
    for line in (report or "").splitlines():
        stripped = line.strip()
        heading = _HEADING.match(stripped)
        if heading is not None:
            name = heading.group(1).replace("*", "").strip().strip("：:").strip()
            current_name = name or None
            if current_name is not None and current_name not in entries:
                entries[current_name] = {"name": current_name, "chars": 0}
                order.append(current_name)
            continue
        if current_name is not None:
            entries[current_name]["chars"] += len(_WHITESPACE.sub("", stripped))
    return [entries[name] for name in order]


def direction_char_count(report: str) -> int:
    return len(_WHITESPACE.sub("", report or ""))


def build_messages(
    *,
    topic: str,
    question: str,
    mode: str,
    research_mode: str,
    evidence_block: str,
    unavailable_note: str = "",
    style_terms: list[str] | tuple[str, ...] | None = None,
    theme_kind: str = "",
    form: str | None = None,
) -> tuple[str, str]:
    """构造方向研判的 (system, user) 消息对（B01 两种诚实状态共用同一 schema）。

    C01（v35）：`style_terms` 非空时走**风格主题分支**——证据块前置筛选口径声明（风格词
    →指标→样本条件），并追加估值引用规则（`_STYLE_EXTRA_RULES`）；`theme_kind` 为复合
    主题时再叠加产业模板提示（`_COMPOSITE_EXTRA_RULES`）。十小节框架不变。

    C01（v36 立场重写）：风格分支的口径声明改为**四维低估判定口径 + 分类规则来源**（不再
    声明「不预设立场、不直接下低估/高估结论」），并把系统参考分类的可推翻条款、三问作答、
    分类总表、空候选合法性写入 `_STYLE_EXTRA_RULES`；C02 追加候选池象限约束（规则 16）。

    v37：追加宏观数据时效性规则（规则 17，`MACRO_TIMING_RULE`）——修复「已发布数据被当成
    未来催化」。两条分支（evidence/knowledge）都追加，因为宏观时效与证据模式无关。

    M3/D01：`form` 为报告形态（full/partial/stage）——决定 JSON 契约里的必需小节与篇幅，
    并把形态专属指令（局部研判收窄断言范围 / 阶段性研究改短篇 + 补证动作）写进提示词；
    D02/D03/D04/D07 规则（比较表、跨层因果、周期审查、引用支持）对所有形态追加。
    """
    _mode_name, mode_label, mode_min, mode_max = direction_mode_budget(mode)
    form_name = str(form or DIRECTION_FORM_FULL)
    if form_name == DIRECTION_FORM_STAGE:
        # D01：阶段性研究用短篇预算（不套用模式字数，缺失数据时不该写长文）。
        mode_label = DIRECTION_FORM_LABELS[DIRECTION_FORM_STAGE]
        mode_min, mode_max = DIRECTION_STAGE_CHAR_MIN, DIRECTION_STAGE_CHAR_MAX
    style_list = [str(term).strip() for term in (style_terms or []) if str(term).strip()]
    is_style = bool(style_list)
    if research_mode == "evidence":
        evidence_part = f"【证据快照（引用时使用 [E#] 编号）】\n{evidence_block}\n"
        extra_rules = _EVIDENCE_EXTRA_RULES
    else:
        evidence_part = "【证据快照】（无可用证据——本次为知识概览，见铁律 6）\n"
        extra_rules = _KNOWLEDGE_EXTRA_RULES
    # v37：宏观数据时效性规则对所有方向研判生效（宏观前提是任何主题的公共输入）。
    extra_rules += "\n" + MACRO_TIMING_RULE
    # M3（D02/D03/D04/D07）：比较表 / 跨层因果 / 周期审查 / 引用支持——对所有形态生效。
    extra_rules += M3_QUALITY_RULES
    # 风格主题：口径声明前置（与 A02 同一来源文本，不在提示词里另写一份）。
    caliber_part = ""
    if is_style:
        caliber_text = style_caliber_declaration(style_list)
        if caliber_text:
            caliber_part = f"{caliber_text}\n"
        extra_rules += _STYLE_EXTRA_RULES
        # C02（v36）：候选池象限约束（提名须来自「低估候选」象限，否则须给推翻论证）。
        extra_rules += pool_quadrant_rule_text()
        if theme_kind == THEME_KIND_COMPOSITE:
            extra_rules += _COMPOSITE_EXTRA_RULES
    user = (
        f"【研究主题】{topic}\n"
        f"【用户关注点】{question or '产业趋势、竞争格局与代表性标的'}\n"
        # C02（2026-09-15 路线图）：主题范围前置声明——先拆三个子问题，示例行业不等于全集，
        # 局部行业研究不得写成全市场筛选结论。
        "【研究范围声明】先把本主题拆成三个子问题分别作答：①估值（证据块命中的板块/行业内，"
        "有证据的估值比较）②盈利（量价、财报、经营数据是否支持改善）③催化与反证（重估催化与"
        "被推翻条件）。题目中「比如A、B」一类措辞是**重点示例**，不是全集：结论只覆盖证据块实际"
        "命中的行业/板块与候选池，禁止表述为全市场筛选结论；未覆盖的子行业与未取得的数据在"
        "数据缺口中如实声明，并说明研究期限。\n"
        f"【篇幅模式】{mode_label}（建议 {mode_min}-{mode_max} 字，软预算）\n"
        f"【报告形态】{DIRECTION_FORM_LABELS.get(form_name, DIRECTION_FORM_LABELS[DIRECTION_FORM_FULL])}"
        f"（必需小节：{'、'.join(direction_form_sections(form_name))}）\n"
        f"【行业指标模板（B07）】{industry_template_hint(topic)}\n"
        f"{caliber_part}"
        f"{evidence_part}"
        f"{('【来源不可用说明】' + unavailable_note + chr(10)) if unavailable_note else ''}\n"
        "请输出严格 JSON（无 markdown 围栏）：\n"
        f"{direction_schema_text(form_name)}"
        f"{extra_rules}"
        f"{_FORM_INSTRUCTIONS.get(form_name, '')}"
    )
    return _SYSTEM, user


# ---------------------------------------------------------------------------
# 候选池归一化（B09 证券身份 / B10 三种资格 / B11 移除估价 / B12 解释入选）
# ---------------------------------------------------------------------------

_PRICE_KEYS = ("reference_price", "price", "target_price", "current_price")

# ---------------------------------------------------------------------------
# M1-B01（2026-09-15 第二轮路线图）：候选身份回填
# ---------------------------------------------------------------------------
# 实测根因（22:55 样报）：证据条目里一直写着「中国通号(688009) PB 分位 1.2%」
# （`valuation_evidence.describe_board_percentile_appendix`），但模型把**公司名**写进了
# `stock_pool.symbol`，`normalize_instrument()` 只抽 6 位数字 → `key=""` → 6 只全部「无效代码」。
# 修法：服务端用**本次证据**构造「名称 → 6 位代码」精确匹配表回填（只补码，不改名、不编造未出现过的股票）。
# 两种来源合流（顺序无关，互相补充）：
#   ① 结构化：证据条目的 `percentile_appendix.lowest_three[].{symbol,name}`；
#   ② 文本：证据/正文里的 `名称(6位码)`（同一个 `describe_board_percentile_appendix` 产物）。
# 歧义（同名对应多个不同代码）→ 不进表：宁缺勿错。
_NAME_CODE_PATTERN = re.compile(r"([\u4e00-\u9fa5A-Za-z0-9]{2,12})[（(](\d{6})[)）]")
# 回填来源标记（写入候选条目，供前端与验收记录区分「模型给对了」与「服务端补的」）。
BACKFILL_SOURCE_EVIDENCE = "evidence_name_index"


def build_name_symbol_index(
    structured_pairs: Iterable[tuple[str, str]] = (),
    texts: Iterable[str] = (),
) -> dict[str, str]:
    """构造「公司名 → 6 位 A 股代码」精确匹配表（纯函数）。

    只收 `kind == "stock"` 的代码：候选身份判定只认 A 股股票（`_identity_status`），
    ETF/其他品种即使用名称命中也不构成有效候选身份。
    同名映射到多个不同代码 → 该名整体剔除（歧义不猜）。
    """
    pairs: list[tuple[str, str]] = []
    for name, symbol in structured_pairs:
        clean_name = str(name or "").strip()
        if clean_name:
            pairs.append((clean_name, str(symbol or "").strip()))
    for text in texts:
        for name, symbol in _NAME_CODE_PATTERN.findall(str(text or "")):
            pairs.append((str(name).strip(), str(symbol).strip()))
    ambiguous: dict[str, set[str]] = {}
    for name, symbol in pairs:
        identity = normalize_instrument(symbol)
        if not name or identity.kind != "stock":
            continue
        ambiguous.setdefault(name, set()).add(identity.key)
    return {
        name: next(iter(keys))
        for name, keys in ambiguous.items()
        if len(keys) == 1
    }


def _identity_status(key: str, kind: str) -> str:
    """B09/B10：证券身份三态。A 股股票前缀表命中 → verified；
    ETF/其他品种不是股票候选 → invalid；无法解析 → invalid。
    （unverified 保留给「格式合法但主数据未接入」的未来状态，当前不产出。）"""
    if kind == "stock" and key:
        return IDENTITY_VERIFIED
    return IDENTITY_INVALID


def normalize_pool(
    value: Any, *, name_index: dict[str, str] | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """归一化候选池：身份归一 + 去重（按标准代码）+ 剔除价格字段。

    返回 (candidates, warnings)。每条候选：
    {symbol_raw, symbol, name, exchange, sector, value_chain_position, business_link,
     profit_path, support_refs, counter_evidence, gaps, identity_status, identity,
     symbol_backfilled, symbol_backfill_source,
     qualification: {identity, business_relevance, market_data}}
    B10：三种资格分开陈述——identity 由服务端判定；business_relevance 采信模型声明
    （business_link 非空即 declared，由用户与质量规则复核）；market_data 默认未核验
    （行情可扫描性由战法雷达实际扫描确认，不假装已核验）。

    M1-B01：`name_index`（本次证据的「名称 → 6 位代码」表）非空时，对**无法解析为 A 股股票代码**
    的条目按 `name` → 原始写法 的顺序精确匹配回填代码；只补码，不改公司名，不猜歧义。
    """
    candidates: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not isinstance(value, list):
        return candidates, warnings
    seen: set[str] = set()
    dropped_prices = 0
    backfilled = 0
    for item in value[:12]:
        if not isinstance(item, dict):
            continue
        symbol_raw = str(item.get("symbol_raw") or item.get("symbol") or "").strip()
        name_raw = str(item.get("name", "")).strip()
        identity = normalize_instrument(symbol_raw)
        # M1-B01：模型把公司名当代码写进 symbol → 无法解析 6 位码时，用本次证据的
        # 「名称 → 代码」表精确回填（先 name、后原始写法）。只补码，不改名，不猜歧义。
        symbol_backfilled = False
        symbol_backfill_source = ""
        matched_name = ""
        if not identity.key and name_index:
            for lookup in (name_raw, symbol_raw):
                clean = lookup.strip()
                if not clean:
                    continue
                hit = name_index.get(clean)
                if not hit:
                    continue
                resolved = normalize_instrument(hit)
                if resolved.key:
                    identity = resolved
                    symbol_backfilled = True
                    symbol_backfill_source = BACKFILL_SOURCE_EVIDENCE
                    matched_name = clean
                    backfilled += 1
                    break
        if not symbol_raw and not identity.key:
            continue  # 无任何代码信息的条目无法核验，直接丢弃（不产出空壳候选）
        # B11：模型输出的一切价格字段剔除（新报告不使用模型估价），计数并告警。
        price_found = [key for key in _PRICE_KEYS if item.get(key) not in (None, "")]
        dropped_prices += len(price_found)
        key = identity.key
        dup = key in seen if key else False
        if key:
            seen.add(key)
        status = _identity_status(identity.key, identity.kind)
        support_raw = item.get("support_refs")
        candidates.append({
            "symbol_raw": symbol_raw,
            "symbol": key,
            "symbol_backfilled": symbol_backfilled,
            "symbol_backfill_source": symbol_backfill_source,
            # 名称以模型声明为准；模型漏填且服务端回填成功时，用证据里的公司名补上（同样是真实存在的信息）。
            "name": name_raw or matched_name,
            "exchange": identity.market if identity.market != "none" else str(item.get("exchange", "") or "未知"),
            "sector": str(item.get("sector", "")).strip(),
            "value_chain_position": str(item.get("value_chain_position", "")).strip(),
            "business_link": str(item.get("business_link", "")).strip(),
            "profit_path": str(item.get("profit_path", "")).strip(),
            "support_refs": [str(r).strip() for r in support_raw if str(r).strip()][:5] if isinstance(support_raw, list) else [],
            "counter_evidence": str(item.get("counter_evidence", "")).strip(),
            "gaps": [str(g).strip() for g in item.get("gaps", []) if str(g).strip()][:3] if isinstance(item.get("gaps"), list) else [],
            "identity_status": status,
            "identity_status_label": IDENTITY_STATUS_LABELS[status],
            "identity": identity.as_dict(),
            "qualification": {
                # B10：身份有效 ≠ 主题相关 ≠ 可扫描——三项分开，逐项有据。
                "identity": status,
                "business_relevance": "declared" if str(item.get("business_link", "")).strip() else "unknown",
                "market_data": "unverified",
                "market_data_note": "行情可扫描性由战法雷达实际扫描确认（本表不做未经验证的判断）",
            },
            "duplicate_of_pool": dup,
        })
    if backfilled:
        names = "、".join(
            c["name"] or c["symbol"]
            for c in candidates
            if c.get("symbol_backfilled")
        )
        warnings.append(
            f"服务端按本次估值证据回填了 {backfilled} 个候选代码（{names}）：模型未给出 6 位码，"
            "已用证据中的「公司名(代码)」精确匹配补齐（只补码，不改名；歧义不猜）。"
        )
    if dropped_prices:
        warnings.append(
            f"剔除 {dropped_prices} 处模型自行输出的价格字段（路线图 B11：新报告不使用模型估价，"
            "价格以行情数据为准）。"
        )
    dups = [c["symbol"] for c in candidates if c["duplicate_of_pool"]]
    if dups:
        warnings.append(f"候选池存在重复代码（{'、'.join(dups)}），已标记（保留首条为有效候选）。")
    return candidates, warnings


def normalize_judgments(value: Any) -> list[dict[str, Any]]:
    """核心判断归一（B05 分层）：kind 越界归 industry 并留痕原值。"""
    out: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return out
    for item in value[:10]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip().lower()
        confidence = str(item.get("confidence", "")).strip().lower()
        missing_raw = item.get("missing")
        out.append({
            "id": str(item.get("id", "")).strip() or f"J{len(out) + 1}",
            "text": str(item.get("text", "")).strip(),
            "kind": kind if kind in JUDGMENT_KINDS else "industry",
            "kind_raw": kind,
            "kind_label": JUDGMENT_KIND_LABELS.get(kind if kind in JUDGMENT_KINDS else "industry", "产业景气"),
            "support_refs": [str(r).strip() for r in item.get("support_refs", []) if str(r).strip()][:5] if isinstance(item.get("support_refs"), list) else [],
            "confidence": confidence if confidence in ALLOWED_CONFIDENCE else "",
            "missing": [str(m).strip() for m in missing_raw if str(m).strip()][:3] if isinstance(missing_raw, list) else [],
        })
    return out


# ---------------------------------------------------------------------------
# 方向研判专属质量闸门（B15）
# ---------------------------------------------------------------------------

DIRECTION_QUALITY_VERSION = "v1"
DIRECTION_BLOCKING_MISSING_SECTION = "direction_missing_section"
DIRECTION_BLOCKING_NO_JUDGMENTS = "direction_no_judgments"
DIRECTION_BLOCKING_EMPTY_BODY = "direction_empty_body"

# —— C02（v35）：估值水位类表述词表 ——
# 命中这些词表示正文在断言「估值处于什么位置」，必须同小节内有真实 [E#] 引用。
VALUATION_CLAIM_PATTERNS: tuple[str, ...] = (
    "历史中低位", "历史低位", "历史高位", "历史中高位",
    "处于低位", "处于高位", "低位区间", "高位区间",
    "估值修复", "估值回归", "估值洼地",
    "破净", "溢价", "折价",
    "分位", "百分位",
    "低于历史", "高于历史", "历史区间下沿", "历史区间上沿",
)
# 同小节内多处违规 → 降 needs_review 的阈值。
VALUATION_CLAIM_WARN_THRESHOLD = 3

# —— C02（v36）：候选池象限约束 ——
# 「低估」主题的候选股应来自「低估候选」象限的板块成分。系统参考分类把候选股所属板块
# 判为**深跌未反转 / 高位 / 盈利周期顶**时，正文必须包含**推翻该板块分类的数值论证**；
# 否则视为「候选池与板块判定脱钩」，记质量告警 + missing_information。
#
# 不约束的象限：低估候选（预期象限）、待定（无法判定，不误伤）。
POOL_QUADRANT_ALLOWED: tuple[str, ...] = (VALUATION_JUDGMENT_LOW, VALUATION_JUDGMENT_PENDING)
# 需要「推翻论证」才允许提名的象限（系统参考分类与「低估」立场冲突）。
POOL_QUADRANT_NEEDS_OVERRIDE: tuple[str, ...] = (
    VALUATION_JUDGMENT_DEEP_FALL,
    VALUATION_JUDGMENT_HIGH,
    VALUATION_JUDGMENT_CYCLE_TOP,
)
# 命中 ≥ 该数量的违规候选 → 降 needs_review（与估值引用闸门同风格阈值语义）。
POOL_QUADRANT_VIOLATION_WARN_THRESHOLD = 2
# 推翻论证的判定词（正文出现任一即视为已给出推翻论证，具体数值论证由模型自陈）。
POOL_OVERRIDE_PATTERNS: tuple[str, ...] = ("推翻", "不止于", "不符", "有误", "重判", "改写为")
# 提名指引文本（提示词与质量闸门共用同一来源）。
POOL_QUADRANT_GUIDE = (
    "候选池提名应来自「低估候选」象限的板块成分；"
    "若从「深跌未反转 / 高位 / 盈利周期顶」象限提名，正文必须包含**推翻该板块分类的数值论证**"
    "（逐维说明哪一维与系统判定不符、差多少），否则该候选视为不合格。"
)

# —— C03（v36）：分类判定引用闸门 ——
# 正文出现分类标签表述（低估候选/深跌未反转/高位/盈利周期顶）时，同小节须有 [E#] 引用，
# 且引用的证据条目须含对应板块的四维数值。估值水位表述引用闸门（v35 C02）沿用。
# 待定不计入命中词：它是「无结论」的兜底标签，出现即代表明确声明缺维，不构成断言。
VALUATION_JUDGMENT_CLAIM_WORDS: tuple[str, ...] = (
    VALUATION_JUDGMENT_LOW,
    VALUATION_JUDGMENT_DEEP_FALL,
    VALUATION_JUDGMENT_HIGH,
    VALUATION_JUDGMENT_CYCLE_TOP,
)
# 命中 ≥ 该数量的小节 → 降 needs_review。
JUDGMENT_CLAIM_WARN_THRESHOLD = 2


def section_texts(report: str) -> list[dict[str, Any]]:
    """正文按小节切分（返回 `[{name, text}]`，同名合并正文；标题行不计入正文）。"""
    entries: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    current: str | None = None
    for line in (report or "").splitlines():
        heading = _HEADING.match(line.strip())
        if heading is not None:
            name = heading.group(1).replace("*", "").strip().strip("：:").strip()
            current = name or None
            if current is not None and current not in entries:
                entries[current] = {"name": current, "text_parts": []}
                order.append(current)
            continue
        if current is not None:
            entries[current]["text_parts"].append(line)
    return [
        {"name": entries[name]["name"], "text": "\n".join(entries[name]["text_parts"])}
        for name in order
    ]


def find_valuation_claims(report: str) -> list[dict[str, Any]]:
    """C02：定位正文中的估值水位表述（按小节返回 `{section, claims, refs, ok}`）。

    `refs` 为该小节内出现的 `[E#]` 编号集合；`claims` 为命中的表述词（去重）。
    结论只做「该小节是否在无引用的前提下断言估值水位」，不评价表述本身对不对。
    """
    out: list[dict[str, Any]] = []
    for entry in section_texts(report):
        text = str(entry["text"])
        claims = [word for word in VALUATION_CLAIM_PATTERNS if word in text]
        if not claims:
            continue
        refs = sorted(set(re.findall(r"\[(E\d+)\]", text)))
        out.append({
            "section": entry["name"],
            "claims": claims,
            "refs": refs,
            "ok": bool(refs),
        })
    return out


def find_override_argument(report: str, board_name: str) -> str:
    """C02（v36）：找出正文中针对某板块的**推翻论证**片段（找不到返回空串）。

    判定口径刻意保守：必须**同一行/同一句内**同时出现板块名与推翻词（推翻/不符/有误…），
    才算给出论证。这样「正文别处泛泛说了推翻」不会误认为该板块已有论证；反之只要模型
    按 C01 规则 13 写了「银行Ⅱ 的『高位』分类应推翻：PB 分位 96.6% 但…」即可命中。
    """
    if not board_name:
        return ""
    for raw in (report or "").splitlines():
        line = raw.strip()
        if board_name not in line:
            continue
        if any(word in line for word in POOL_OVERRIDE_PATTERNS):
            return line
    return ""


def check_pool_quadrants(
    pool: list[dict[str, Any]],
    board_judgments: dict[str, str],
    *,
    board_names: tuple[str, ...] | list[str] | None = None,
    report: str = "",
) -> dict[str, Any]:
    """C02（v36）：候选池象限约束检查（纯函数）。

    `board_judgments`：板块名 → 系统参考分类（`classify_board_valuation` 的 label）。
    `board_names`：可选的候选 `sector` 字段 → 板块名别名表（模型常写「银行」「房地产」，
    而板块名是「银行Ⅱ」「房地产开发」），用于把候选归属到板块。缺省时按包含关系匹配。

    返回 `{"violations", "allowed", "unmatched", "override_hits"}`：
    - `violations`：系统分类属于禁提名象限、且正文无该板块推翻论证的候选（逐只留痕）；
    - `allowed`：落在允许象限的候选；`unmatched`：无法归属板块的候选（不违规，只披露）。
    """
    names = [str(n) for n in (board_names or board_judgments.keys())]
    violations: list[dict[str, Any]] = []
    allowed: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    override_hits: list[dict[str, Any]] = []
    for item in pool or []:
        sector = str(item.get("sector") or "").strip()
        symbol = str(item.get("symbol") or item.get("symbol_raw") or "")
        matched = ""
        if sector:
            # 精确命中优先，其次「候选 sector 是板块名子串」或「板块名是候选 sector 子串」。
            for name in names:
                if sector == name:
                    matched = name
                    break
            if not matched:
                for name in names:
                    if sector and (sector in name or name in sector):
                        matched = name
                        break
        if not matched:
            unmatched.append({"symbol": symbol, "sector": sector})
            continue
        label = str(board_judgments.get(matched, ""))
        if label in POOL_QUADRANT_ALLOWED or not label:
            allowed.append({"symbol": symbol, "sector": sector, "board": matched, "label": label})
            continue
        argument = find_override_argument(report, matched)
        entry = {"symbol": symbol, "sector": sector, "board": matched, "label": label}
        if argument:
            entry["override_argument"] = argument
            override_hits.append(entry)
            allowed.append(entry)
        else:
            violations.append(entry)
    return {
        "violations": violations,
        "allowed": allowed,
        "unmatched": unmatched,
        "override_hits": override_hits,
    }


def find_judgment_claims(report: str) -> list[dict[str, Any]]:
    """C03（v36）：定位正文中的**分类标签表述**（按小节返回 `{section, claims, refs, ok}`）。

    与 v35 的 `find_valuation_claims`（估值水位词）并行：本函数只看四个分类标签词
    （低估候选/深跌未反转/高位/盈利周期顶）。「待定」不计入——它是缺维兜底标签，
    出现即代表已如实声明无结论，不构成断言。
    """
    out: list[dict[str, Any]] = []
    for entry in section_texts(report):
        text = str(entry["text"])
        claims = [word for word in VALUATION_JUDGMENT_CLAIM_WORDS if word in text]
        if not claims:
            continue
        refs = sorted(set(re.findall(r"\[(E\d+)\]", text)))
        out.append({
            "section": entry["name"],
            "text": text,
            "claims": claims,
            "refs": refs,
            "ok": bool(refs),
        })
    return out


def judgment_refs_cover_boards(
    claims: list[dict[str, Any]],
    board_evidence: dict[str, set[str]],
) -> list[dict[str, Any]]:
    """C03（v36）：分类表述的引用是否**含对应板块的四维数值**。

    `board_evidence`：板块名 → 该板块估值证据条目的 [E#] 编号集合（由 app.py 依据
    `sector_valuation` 证据条目构造）。判定口径（保守，只报确定的问题）：
    - 该小节内没有任何 [E#] → 违规（无引用）；
    - 小节内有引用，但没有一条属于任何板块的估值证据条目 → 违规（引用了非估值证据）；
    - 小节内出现了某板块名且该板块有估值证据，但小节未引用它 → 违规（引错条目）；
    - 其余情形不判违规（避免误伤只写「部分板块处高位」的概括表述）。
    """
    if not board_evidence:
        return []
    all_board_refs: set[str] = set()
    for refs in board_evidence.values():
        all_board_refs |= refs
    problems: list[dict[str, Any]] = []
    for item in claims:
        section = str(item["section"])
        refs = set(item["refs"])
        if not refs:
            problems.append({**item, "reason": "no_refs"})
            continue
        if not (refs & all_board_refs):
            problems.append({**item, "reason": "no_board_ref"})
            continue
        # 小节点名了某个有估值证据的板块，却未引用它的条目 → 引错条目。
        text = str(item.get("text", ""))
        named = [name for name in board_evidence if name and name in text]
        wrong = [name for name in named if not (set(board_evidence[name]) & refs)]
        if wrong:
            problems.append({**item, "reason": "wrong_ref", "boards": wrong})
    return problems


# ---------------------------------------------------------------------------
# M3（D02/D03/D04/D06/D07/D08）：研究判断与报告质量的确定性检查
# ---------------------------------------------------------------------------

# —— D02：前置行业比较表归一化 ——
COMPARISON_REQUIRED_FIELDS: tuple[str, ...] = (
    "industry",
    "valuation_evidence",
    "profit_change",
    "catalyst",
    "horizon",
    "counter_evidence",
    "research_priority",
    "priority_basis",
    "evidence_completeness",
)
ALLOWED_RESEARCH_PRIORITY = ("高", "中", "低")


def normalize_industry_comparison(value: Any) -> list[dict[str, Any]]:
    """D02：归一化 `industry_comparison`（缺失字段留空串；优先级只认高/中/低）。

    不编造：空值保持空字符串，由校验层告警（`priority_basis` 缺失即优先级无依据）。
    非列表/非法行丢弃；行业名称为空的行丢弃（无可比对象）。
    """
    out: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return out
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        industry = str(item.get("industry") or "").strip()
        if not industry:
            continue
        row: dict[str, Any] = {"industry": industry}
        for field in COMPARISON_REQUIRED_FIELDS:
            if field == "industry":
                continue
            row[field] = str(item.get(field) or "").strip()[:400]
        priority = row["research_priority"]
        row["research_priority"] = priority if priority in ALLOWED_RESEARCH_PRIORITY else ""
        row["priority_basis_missing"] = not bool(row["priority_basis"])
        out.append(row)
    return out


# —— D03：跨层因果（宏观 → 盈利）断言检测 ——
MACRO_LAYER_WORDS: tuple[str, ...] = (
    "CPI", "PPI", "油价", "原油价格", "利率", "汇率", "PMI", "通胀", "美联储", "降息", "加息",
)
PROFIT_LAYER_WORDS: tuple[str, ...] = ("利润", "盈利", "业绩", "净利", "毛利", "EPS", "净利润")
CAUSAL_BRIDGE_WORDS: tuple[str, ...] = (
    "量价", "成本", "单票", "件量", "产能利用率", "开工率", "运价", "销量", "收入", "订单",
    "周转", "客单价", "需求", "价格", "货量", "费率", "资本开支",
)
_SENTENCE_SPLIT = re.compile(r"[。！？；;\n]")
# 句内已带这些标记 → 该句是**假设/缺口声明**，不是断言（C03 改写后的句子即落此口径）。
HYPOTHESIS_MARKERS: tuple[str, ...] = (
    "待验证假设", "待验证", "假设", "证据未覆盖", "不足以推出", "不能推出", "无法从",
    "待补证", "若成立",
)
# —— 假阳性修复（G02/G05 真实样报回归，2026-09-15 深夜）——
# ① 子串撞车：宏观词「利率」会被财务比率「毛利率 / 净利率 / 费用率」包含。
#    实测：M0 与 G02 重跑里 2/3 条「利率 → 毛利」告警其实来自
#    「验证…代表股营收与毛利率是否改善」这类**验证动作**句，被判成跨层断言并被就地改写成
#    「待验证假设」——把一句正常的工作项改坏了。故宏观词匹配加**前置字排除**。
MACRO_FALSE_FRIENDS: dict[str, tuple[str, ...]] = {
    "利率": ("毛", "净", "费", "增", "减"),  # 毛利率/净利率/费率口径，不是货币政策利率
}
# ② meta/否定句：句内明确说「不以宏观替代行业验证」这类**元陈述**也不是断言。
CROSS_LAYER_META_MARKERS: tuple[str, ...] = (
    "不宜替代", "不能替代", "不应替代", "不可替代", "不等同", "不能等同",
    "不能直接推出", "不可直接推出", "不作为结论", "不构成结论",
)


def macro_terms_in(text: str) -> list[str]:
    """句中出现的宏观词（排除「毛利率」这类财务比率里的伪「利率」）。"""
    probe = str(text or "")
    found: list[str] = []
    for word in MACRO_LAYER_WORDS:
        blocked = MACRO_FALSE_FRIENDS.get(word, ())
        for index in range(len(probe)):
            if not probe.startswith(word, index):
                continue
            if index > 0 and probe[index - 1] in blocked:
                continue
            found.append(word)
            break
    return found


def is_cross_layer_claim(text: str) -> bool:
    """D03/C03：单句是否为「宏观词 + 盈利词且无中间环节词」的跨层**断言**。

    与 `find_cross_layer_claims` 同一口径（单一来源，供检查与就地改写共用），
    已带假设标记或 meta 否定标记的句子不算断言；宏观词用 `macro_terms_in` 排除子串撞车。
    """
    probe = str(text or "").strip()
    if len(probe) < 8:
        return False
    if any(word in probe for word in HYPOTHESIS_MARKERS):
        return False
    if any(word in probe for word in CROSS_LAYER_META_MARKERS):
        return False
    if not macro_terms_in(probe):
        return False
    if not any(word in probe for word in PROFIT_LAYER_WORDS):
        return False
    return not any(word in probe for word in CAUSAL_BRIDGE_WORDS)


def find_cross_layer_claims(report: str) -> list[dict[str, Any]]:
    """D03：定位「宏观变量 → 盈利结论」的跨层断言（同句无中间环节数据即命中）。

    判定口径（确定性）：同一句中同时出现宏观词与盈利词，且**不含**任何中间环节词
    （量价/成本/单票/产能利用率/运价/订单…）→ 属跨层推断，须改写为假设或补中间证据。

    C03（M2）：句内已带假设标记（待验证/假设/证据未覆盖…）的**不算断言**——改写后的
    待验证假设句不应再被计为违规（否则闸门与处理方法互相顶牛）。命中项保留
    `sentence_full`（完整句，供就地改写定位；`sentence` 仍是 160 字截断版，供告警展示）。
    """
    found: list[dict[str, Any]] = []
    for entry in section_texts(report):
        section = str(entry["name"])
        for sentence in _SENTENCE_SPLIT.split(str(entry["text"])):
            text = sentence.strip()
            if not is_cross_layer_claim(text):
                continue
            found.append({
                "section": section,
                "sentence": text[:160],
                "sentence_full": text,
                "macro_terms": macro_terms_in(text),
                "profit_terms": [w for w in PROFIT_LAYER_WORDS if w in text],
            })
    return found


# —— D04：周期行业正常化审查 ——
CYCLE_INDUSTRY_WORDS: tuple[str, ...] = (
    "煤炭", "石油", "有色", "钢铁", "化工", "航运", "港口", "建材", "水泥", "玻璃",
    "资源", "贵金属", "油运", "干散",
)
NORMALIZATION_DISCLOSURE_WORDS: tuple[str, ...] = (
    "正常化", "中枢", "周期均值", "平均盈利", "均值回归", "样本区间", "假设", "历史区间",
)


def cycle_normalization_gaps(
    report: str, board_judgments: dict[str, str] | None
) -> list[dict[str, str]]:
    """D04：周期类板块被判「低估候选」但正文未披露正常化口径/样本区间/假设 → 缺口。

    低 PE 陷阱防线：周期顶点盈利抬高会压低 PE，未披露正常化口径的「低估」不可采信。
    """
    if not board_judgments:
        return []
    text = str(report or "")
    disclosed = any(word in text for word in NORMALIZATION_DISCLOSURE_WORDS)
    gaps: list[dict[str, str]] = []
    for board, label in board_judgments.items():
        if str(label) != VALUATION_JUDGMENT_LOW:
            continue
        if not any(word in str(board) for word in CYCLE_INDUSTRY_WORDS):
            continue
        if disclosed:
            continue
        gaps.append({
            "board": str(board),
            "reason": (
                "周期类板块判为「低估候选」，但正文未见正常化口径、样本区间或假设的披露——"
                "低 PE 可能来自周期顶点盈利抬高（低 PE 陷阱），须披露正常化盈利口径后再判"
            ),
        })
    return gaps


# —— D06：研究对象池 / 低估候选池分离 ——
POOL_KIND_LOW = "low_valuation"
POOL_KIND_RESEARCH = "research"
POOL_KIND_LABELS: dict[str, str] = {
    POOL_KIND_LOW: "低估候选",
    POOL_KIND_RESEARCH: "待验证研究对象",
}
_RESEARCH_POOL_ACTIONS: dict[str, str] = {
    "证券身份未核验": "核对证券代码与交易所，确认公司身份后并入低估候选池",
    "重复候选": "剔除重复项，保留唯一代码",
    "估值证据缺失": "补齐估值当前值与分位（估值源恢复后重跑，或改从财报派生）",
    "业务关联/兑现路径未说明": "补充业务关联与业绩兑现路径（对应报表科目）",
    "非低估象限": "补「推翻板块分类」的数值论证，或改用低估象限的成分股",
}


def split_pools(
    candidates: list[dict[str, Any]],
    board_judgments: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """D06：把候选拆成 (低估候选池, 待验证研究池)。

    低估候选池 = 身份已核验 + 非重复 + 有估值回链 + 业务关联/兑现路径齐全 +
    所属板块分类落在允许象限（低估候选/待定）；其余全部进**待验证研究池**并附缺口与验证动作。
    每行带 `pool_kind` / `pool_kind_label` / `pool_exclusion_reasons` / `verification_actions`，
    供前端、导出与下游按名分列，避免待验证对象被误标为已确认低估。
    """
    judgments = board_judgments or {}
    low_pool: list[dict[str, Any]] = []
    research_pool: list[dict[str, Any]] = []
    for item in candidates:
        entry = dict(item)
        reasons: list[str] = []
        if str(entry.get("identity_status")) != IDENTITY_VERIFIED:
            reasons.append("证券身份未核验")
        if entry.get("duplicate_of_pool"):
            reasons.append("重复候选")
        if not (entry.get("valuation_ref") or entry.get("valuation_status") == "available"):
            reasons.append("估值证据缺失")
        if not (entry.get("business_link") and entry.get("profit_path")):
            reasons.append("业务关联/兑现路径未说明")
        board = str(entry.get("pool_board_name") or entry.get("sector") or "").strip()
        label = str(judgments.get(board) or entry.get("pool_board_judgment") or "").strip()
        if label and label not in POOL_QUADRANT_ALLOWED:
            reasons.append("非低估象限")
        entry["pool_kind"] = POOL_KIND_RESEARCH if reasons else POOL_KIND_LOW
        entry["pool_kind_label"] = POOL_KIND_LABELS[entry["pool_kind"]]
        entry["pool_exclusion_reasons"] = reasons
        entry["verification_actions"] = [
            _RESEARCH_POOL_ACTIONS.get(reason, "补齐对应证据后再评估")
            for reason in reasons
        ]
        (research_pool if reasons else low_pool).append(entry)
    return low_pool, research_pool


# —— D07：引用是否支持结论（编号/数值/期间的确定性核对） ——
_NUMBER_TOKEN = re.compile(r"\d+(?:\.\d+)?")
# 合法换算：这些表述允许句内数值与证据原文不同（推算/差分/折算）。
_DERIVATION_WORDS: tuple[str, ...] = ("推算", "计算得出", "折算", "差分", "年化")


def unsupported_reference_problem(
    text: str, evidence_index: dict[str, str] | None
) -> dict[str, Any] | None:
    """D07/C05：单句「引用编号—句内数值」是否对不上（对不上则返回问题，否则 None）。

    规则（确定性）：句含 [E#] 且含数值时，要求**至少一个**数值出现在被引用条目的内容中；
    一个都不在 → 引用了与被引内容不对应的编号（除非声明了推算/折算）。
    同一口径供检查（`check_reference_support`）与就地改写（C05 去引用）共用。
    """
    if not evidence_index:
        return None
    probe = str(text or "")
    refs = sorted(set(re.findall(r"\[(E\d+)\]", probe)))
    if not refs:
        return None
    # 先把 [E#] 编号从句子中剔除再取数值，否则「E1」的 1 会被当成句内数值
    # 而与证据内容误匹配（2026-09-15 实测踩坑）。
    stripped = re.sub(r"\[E\d+\]", " ", probe)
    numbers = sorted(set(_NUMBER_TOKEN.findall(stripped)))
    if not numbers:
        return None
    if any(word in probe for word in _DERIVATION_WORDS):
        return None
    contents = [str(evidence_index.get(ref) or "") for ref in refs]
    if any(token in content for token in numbers for content in contents):
        return None
    return {"refs": refs, "numbers": numbers[:6]}


def check_reference_support(
    report: str, evidence_index: dict[str, str] | None
) -> list[dict[str, Any]]:
    """D07：核对「挂了 [E#] 的句子，其数值是否真的在该条目里」。

    只做编号—数值对应性检查；因果支持与反证由模型审查（反方审查提示词）承担。
    C05（M2）：命中项保留 `sentence_full` 供就地去引用定位（`sentence` 仍为 160 字截断版）。
    """
    if not evidence_index:
        return []
    problems: list[dict[str, Any]] = []
    for entry in section_texts(report):
        section = str(entry["name"])
        for sentence in _SENTENCE_SPLIT.split(str(entry["text"])):
            text = sentence.strip()
            problem = unsupported_reference_problem(text, evidence_index)
            if problem is None:
                continue
            problems.append({
                "section": section,
                "refs": problem["refs"],
                "numbers": problem["numbers"],
                "sentence": text[:160],
                "sentence_full": text,
            })
    return problems


# —— D08：缺口重复散落检测（同一缺口集中说明） ——
GAP_REPEAT_SECTION_THRESHOLD = 3
# M3 降级阈值：达到阈值即由 complete 降 needs_review（与 C02/C03 同风格）。
CROSS_LAYER_WARN_THRESHOLD = 2
CYCLE_NORMALIZATION_WARN_THRESHOLD = 1
REFERENCE_SUPPORT_WARN_THRESHOLD = 3
RESEARCH_POOL_MISLABEL_THRESHOLD = 1


def find_repeated_gaps(report: str, data_gaps: list[str] | None) -> list[dict[str, Any]]:
    """D08：同一缺口在 ≥3 个小节重复出现 → 命中（应集中到数据缺口一处说明）。

    匹配用缺口文本前 8 个非空白字符作关键片段（正文常改写，不要求逐字相同）。
    """
    gaps = [str(gap).strip() for gap in (data_gaps or []) if str(gap).strip()]
    if not gaps:
        return []
    sections = section_texts(report)
    repeated: list[dict[str, Any]] = []
    for gap in gaps:
        probe = _WHITESPACE.sub("", gap)[:8]
        if len(probe) < 4:
            continue
        hits = [
            str(item["name"])
            for item in sections
            if probe in _WHITESPACE.sub("", str(item["text"]))
        ]
        if len(hits) >= GAP_REPEAT_SECTION_THRESHOLD:
            repeated.append({"gap": gap[:120], "sections": hits})
    return repeated


# ---------------------------------------------------------------------------
# M2（C02/C03/C04/C05，2026-09-15 第二轮路线图）：定稿前的确定性质量改写
# ---------------------------------------------------------------------------
# 根因（M0 基线告警 2/4/6/7/8/9）：闸门原来只**告警**不改写——跨层断言（利率→毛利 /
# CPI→盈利）、点名板块却不引用其估值证据的分类判定、引了条目里没有的数字的引用、
# 缺失的产业景气判断层，都会原样留在交付稿里，用户看到的是「有告警的坏报告」。
#
# 处理口径（**只做确定性核对与降级/删除，绝不编造数值、不代写结论**）：
#   C02 缺产业景气层 → 补一条 low 置信、明写「证据未覆盖」的兜底判断（不编景气数据）；
#       已有的产业景气条若断言反转/修复却无中间环节证据 → 降 low + 加缺口尾注；
#   C03 跨层断言句 → 就地改写为「【待验证假设】…（须补中间环节证据）」；
#   C04 点名板块但引用不可核对的分类判定 → 就地改写为**不带分类词**的描述；
#   C05 引用的数值不在被引条目里 → 就地**去掉该引用**（保留事实句，不保留假引用）。
# 所有改写按**小节范围**进行（标题行不动、未命中小节不动），并返回逐条留痕供质量痕迹展示。
QUALITY_REPAIR_POLICY_VERSION = "v1"

# —— C02：产业景气层 ——
INDUSTRY_JUDGMENT_KIND = "industry"
INDUSTRY_JUDGMENT_FALLBACK_TEXT = (
    "产业景气证据未覆盖：本次证据以估值横截面为主，未取得量价、订单/排产、盈利变化等"
    "中间环节数据，不能从低 PB 推出景气修复——该层待补证据，不作为结论依据。"
)
INDUSTRY_JUDGMENT_FALLBACK_MISSING: tuple[str, ...] = ("量价", "订单/排产", "盈利变化")
# 产业景气条里的「已经好转」类断言词：没有中间环节证据支撑时不得留在正文。
INDUSTRY_REVERSAL_WORDS: tuple[str, ...] = (
    "反转", "景气修复", "景气回升", "景气上行", "盈利修复", "盈利改善", "业绩拐点", "周期反转",
)
# 句内已明确否认/未覆盖时不算「断言好转」（否则兜底条自己会被自己的检测命中）。
INDUSTRY_NEGATION_MARKERS: tuple[str, ...] = (
    "证据未覆盖", "不能", "不足以", "尚未", "未确认", "无法确认", "待验证", "不作为结论依据",
)
INDUSTRY_UNSUPPORTED_TAIL = (
    "（证据未覆盖：本次无量价/订单/排产/盈利中间数据，不能据此认定为景气修复，仅作待验证线索）"
)

# —— C03：跨层断言 → 待验证假设 ——
CROSS_LAYER_HYPOTHESIS_MARK = "【待验证假设】"
CROSS_LAYER_HYPOTHESIS_TAIL = (
    "（以上为待验证假设：须补量价、单位成本、单产、订单等中间环节证据后方可作为结论）"
)

# —— C04：分类词 → 不带分类结论的描述 ——
_NEUTRAL_CLAIM_TEXT: dict[str, str] = {
    VALUATION_JUDGMENT_LOW: "估值相对自身历史偏低（分类待该板块估值证据核对）",
    VALUATION_JUDGMENT_DEEP_FALL: "价格已深跌、反转未确认（分类待该板块估值证据核对）",
    VALUATION_JUDGMENT_HIGH: "价格位置偏高（分类待该板块估值证据核对）",
    VALUATION_JUDGMENT_CYCLE_TOP: "盈利或处周期高位（分类待该板块估值证据核对）",
}

_SENTENCE_SPLIT_KEEP = re.compile(r"([。！？；;\n])")


def next_judgment_id(judgments: list[dict[str, Any]]) -> str:
    """取未被占用的下一个 `J#` 编号（不覆盖模型已有编号）。"""
    used = {str(item.get("id") or "") for item in judgments}
    index = len(judgments) + 1
    while f"J{index}" in used:
        index += 1
    return f"J{index}"


def _bridge_backed(refs: list[str], evidence_index: dict[str, str] | None) -> bool:
    """该判断引用的证据条目里是否含中间环节数据（量价/成本/订单…）。

    无索引（不能核对）时返回 True——**不误伤**：宁可漏降级，也不把已引证据的
    产业判断凭「查不到」降级。
    """
    if not refs:
        return False
    if not evidence_index:
        return True
    for ref in refs:
        content = str(evidence_index.get(str(ref)) or "")
        if any(word in content for word in CAUSAL_BRIDGE_WORDS):
            return True
    return False


def ensure_industry_layer(
    parsed: dict[str, Any], *, evidence_index: dict[str, str] | None = None
) -> dict[str, Any]:
    """C02：确保 `core_judgments` 有 `kind=industry` 一条，且不含无凭据的景气反转断言。

    - 缺该层 → 追加一条 **low 置信**、正文写「证据未覆盖，不能从低 PB 推出景气修复」的
      兜底判断（不编造景气数据，不冒充盈利反转）；
    - 已有该层但断言反转/修复、且引用条目无中间环节数据（或无引用）→ 置信度降 low 并加缺口尾注。
    """
    record: dict[str, Any] = {"added": False, "added_id": "", "demoted_ids": [], "reason": ""}
    judgments = normalize_judgments(parsed.get("core_judgments"))
    if not judgments:
        record["reason"] = "无核心判断，不补产业景气层（缺判断由硬阻断处理）"
        return record
    industry = [item for item in judgments if item["kind"] == INDUSTRY_JUDGMENT_KIND]
    if not industry:
        entry = {
            "id": next_judgment_id(judgments),
            "text": INDUSTRY_JUDGMENT_FALLBACK_TEXT,
            "kind": INDUSTRY_JUDGMENT_KIND,
            "kind_raw": INDUSTRY_JUDGMENT_KIND,
            "kind_label": JUDGMENT_KIND_LABELS[INDUSTRY_JUDGMENT_KIND],
            "support_refs": [],
            "confidence": "low",
            "missing": list(INDUSTRY_JUDGMENT_FALLBACK_MISSING),
        }
        judgments = [*judgments, entry]
        record.update({"added": True, "added_id": entry["id"]})
        industry = [entry]
    for item in industry:
        if not any(word in item["text"] for word in INDUSTRY_REVERSAL_WORDS):
            continue
        if any(word in item["text"] for word in INDUSTRY_NEGATION_MARKERS):
            # 已如实写着「证据未覆盖 / 不能推出」的条目不是「断言好转」，不再叠加尾注。
            continue
        if _bridge_backed(item["support_refs"], evidence_index):
            continue
        item["confidence"] = "low"
        if INDUSTRY_UNSUPPORTED_TAIL not in item["text"]:
            item["text"] = f"{item['text']}{INDUSTRY_UNSUPPORTED_TAIL}"
        record["demoted_ids"].append(str(item["id"]))
    if record["added"] or record["demoted_ids"]:
        parsed["core_judgments"] = judgments
    return record


def _rewrite_sections(
    report: str, sections: set[str], rewriter: Any
) -> tuple[str, list[dict[str, Any]]]:
    """按**小节范围**改写正文（只改命中小节内的句，标题行与未命中小节不动）。

    `rewriter(section, sentence) -> str | None`：返回新句，或 None 表示不改该句。
    返回 `(新正文, 逐条留痕)`；留痕含小节名与行号（1-based），供质量痕迹展示。
    """
    lines = (report or "").splitlines()
    current: str | None = None
    changes: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        heading = _HEADING.match(line.strip())
        if heading is not None:
            name = heading.group(1).replace("*", "").strip().strip("：:").strip()
            current = name or None
            continue
        if current is None or current not in sections:
            continue
        parts = _SENTENCE_SPLIT_KEEP.split(line)
        touched = False
        for pos in range(0, len(parts), 2):
            fragment = parts[pos]
            if not fragment.strip():
                continue
            new_fragment = rewriter(current, fragment)
            if new_fragment is None or new_fragment == fragment:
                continue
            parts[pos] = new_fragment
            touched = True
        if touched:
            lines[index] = "".join(parts)
            changes.append({"section": current, "line": index + 1})
    if not changes:
        # 一处未改就返回**原对象**（避免 splitlines/join 造成的尾部换行之类无意义差异）。
        return report, []
    return "\n".join(lines), changes


def _tidy_punctuation(text: str) -> str:
    """去引用后清理空括号与双重标点（不改变事实表述）。"""
    text = re.sub(r"[（(]\s*[)）]", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"（\s*[，、]\s*", "（", text)
    return text.replace("，。", "。").replace("、。", "。").replace("。，", "。").replace("，；", "；")


def _cross_layer_rewriter(section: str, sentence: str) -> str | None:
    """C03：命中跨层断言 → 改写为待验证假设（保留原句事实，不新增数值）。"""
    probe = sentence.strip()
    if not is_cross_layer_claim(probe):
        return None
    return f"{CROSS_LAYER_HYPOTHESIS_MARK}{probe}{CROSS_LAYER_HYPOTHESIS_TAIL}"


def _judgment_claim_rewriter(section: str, sentence: str) -> str | None:
    """C04：命中小节内的分类标签词 → 改写为不带分类结论的描述。"""
    hits = [word for word in VALUATION_JUDGMENT_CLAIM_WORDS if word in sentence]
    if not hits:
        return None
    out = sentence
    for word in hits:
        out = out.replace(word, _NEUTRAL_CLAIM_TEXT[word])
    return out


def _reference_rewriter(evidence_index: dict[str, str] | None) -> Any:
    """C05：引用编号与句内数值对不上 → 去掉该句的 [E#] 引用（保留事实句）。"""
    def _rewrite(section: str, sentence: str) -> str | None:
        problem = unsupported_reference_problem(sentence.strip(), evidence_index)
        if problem is None:
            return None
        return _tidy_punctuation(re.sub(r"\[E\d+\]", "", sentence))
    return _rewrite


def demote_cross_layer_sentences(
    report: str, hits: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    """C03：把跨层断言句就地改写为待验证假设（按命中小节范围，保留原句事实与数值）。

    `hits` 为 `find_cross_layer_claims` 的输出；返回 `(新正文, 逐条留痕)`。
    幂等：已带 `【待验证假设】` 的句子不会再被改写（且不再算命中）。
    """
    sections = {
        str(item.get("section") or "") for item in hits if str(item.get("section") or "")
    }
    if not sections:
        return report, []
    return _rewrite_sections(report, sections, _cross_layer_rewriter)


def split_summary_revision(parsed: dict[str, Any]) -> tuple[str, str]:
    """C01：执行摘要拆成 `(反方审查修订说明, 摘要正文)`；修订说明不进字数预算。

    首选：`apply_counter_check` 写入的 `executive_summary_revision` 字段（新稿件必有）。
    回退：历史稿件只把前缀拼在正文里——按「最强反方论点：…。」的位置切分；**切不出
    可靠边界就不切**（宁严不松：整段计入预算，不会把真超限的摘要放过）。
    """
    raw = str(parsed.get("executive_summary", "") or "")
    revision = str(parsed.get("executive_summary_revision", "") or "").strip()
    if revision and raw.startswith(revision):
        return revision, raw[len(revision):]
    if raw.startswith(COUNTER_SUMMARY_PREFIX):
        marker = "最强反方论点："
        index = raw.find(marker)
        if index >= 0:
            tail = raw[index + len(marker):]
            stop = tail.find("。")
            if stop >= 0:
                cut = index + len(marker) + stop + 1
                return raw[:cut], raw[cut:]
    return "", raw


def apply_quality_repairs(
    parsed: dict[str, Any],
    *,
    evidence_index: dict[str, str] | None = None,
    board_evidence: dict[str, set[str]] | None = None,
) -> dict[str, Any]:
    """M2（C02/C03/C04/C05）：定稿前的确定性改写（就地修订 `parsed`，返回留痕）。

    调用时机：模型 JSON 解析通过、**首次质量校验之前**——这样两次校验（初检与反方审查
    修订后复检）看到的都是已改写的稿件，`quality_warnings` 里不再出现这几类已知问题。
    顺序固定：C02（判断层）→ C03（跨层句）→ C04（分类词）→ C05（假引用），
    因为 C04 需要先看跨层改写后的正文，C05 需要先看引用是否还落得住。
    """
    record: dict[str, Any] = {
        "applied": False,
        "policy_version": QUALITY_REPAIR_POLICY_VERSION,
        "industry_layer": {},
        "cross_layer_demotions": [],
        "judgment_claim_rewrites": [],
        "reference_removals": [],
    }
    original = str(parsed.get("report") or "")
    report = original

    # —— C02：产业景气判断层 ——
    record["industry_layer"] = ensure_industry_layer(parsed, evidence_index=evidence_index)

    # —— C03：跨层断言 → 待验证假设 ——
    cross_hits = find_cross_layer_claims(report)
    if cross_hits:
        report, changes = demote_cross_layer_sentences(report, cross_hits)
        by_section: dict[str, list[str]] = {}
        for item in cross_hits:
            by_section.setdefault(str(item["section"]), []).append(
                f"{'、'.join(item['macro_terms'])} → {'、'.join(item['profit_terms'])}"
            )
        record["cross_layer_demotions"] = [
            {"section": change["section"], "line": change["line"], "claims": by_section.get(change["section"], [])}
            for change in changes
        ]

    # —— C04：引用不可核对的分类判定 → 去掉分类词 ——
    judgment_problems = judgment_refs_cover_boards(
        find_judgment_claims(report), board_evidence or {}
    )
    if judgment_problems:
        sections = {str(item["section"]) for item in judgment_problems}
        reasons: dict[str, set[str]] = {}
        for item in judgment_problems:
            reasons.setdefault(str(item["section"]), set()).add(str(item.get("reason") or ""))
        report, changes = _rewrite_sections(report, sections, _judgment_claim_rewriter)
        record["judgment_claim_rewrites"] = [
            {"section": change["section"], "line": change["line"], "reasons": sorted(reasons.get(change["section"], set()))}
            for change in changes
        ]

    # —— C05：数值不在被引条目内 → 去掉该引用 ——
    ref_problems = check_reference_support(report, evidence_index)
    if ref_problems:
        sections = {str(item["section"]) for item in ref_problems}
        refs_by_section: dict[str, list[str]] = {}
        for item in ref_problems:
            refs_by_section.setdefault(str(item["section"]), []).extend(item["refs"])
        report, changes = _rewrite_sections(report, sections, _reference_rewriter(evidence_index))
        record["reference_removals"] = [
            {
                "section": change["section"],
                "line": change["line"],
                "refs": sorted(set(refs_by_section.get(change["section"], []))),
            }
            for change in changes
        ]

    if report != original:
        parsed["report"] = report
        record["applied"] = True
    return record


def validate_direction(
    parsed: dict[str, Any],
    *,
    mode: str,
    research_mode: str,
    evidence_ids: set[str],
    board_judgments: dict[str, str] | None = None,
    board_evidence: dict[str, set[str]] | None = None,
    form: str | None = None,
    evidence_index: dict[str, str] | None = None,
    name_index: dict[str, str] | None = None,
    critical_boards: Iterable[str] | None = None,
) -> dict[str, Any]:
    """方向研判质量三态（与个股闸门同口径：硬阻断=缺小节/无正文/无核心判断；
    软告警=篇幅/摘要预算、引用失真、候选数、身份无效、空池无解释）。

    C02（v36）：`board_judgments`（板块名 → 系统参考分类）非空时启用**候选池象限约束**——
    从「深跌未反转/高位/盈利周期顶」象限提名且正文无论证推翻该板块分类的候选记告警。
    C03（v36）：`board_evidence`（板块名 → 该板块估值证据 [E#] 集合）非空时启用**分类判定
    引用闸门**——分类标签表述须同小节有 [E#] 引用且引用条目含该板块四维数值。

    M3（D01/D02/D03/D04/D06/D07/D08）：
    - `form` 决定**必需小节**（缺省=完整研判十节，与历史行为逐字一致）：阶段/局部形态下
      非必需小节缺失只作软告警，不再阻断交付（D01：不为凑节输出「无法判断」）；
    - D02：`industry_comparison` 为空/优先级无依据 → 告警（不强制编造数值评分）；
    - D03：同句「宏观词 + 盈利词」且无中间环节词 → 跨层推断言告警；
    - D04：周期类板块判低估但未披露正常化口径 → 告警（低 PE 陷阱）；
    - D06：拆出 `research_pool` / `low_valuation_pool`，待验证对象被写成已确认低估 → 告警；
    - D07：`evidence_index`（E# → 条目内容）非空时核对「引用编号—句内数值」对应性；
    - D08：同一缺口在 ≥3 小节重复出现 → 告警（应集中说明一次）。

    M1-B01：`name_index`（本次证据的「公司名 → 6 位代码」精确匹配表）用于候选身份回填；
    未显式给出时，自动从 `evidence_index` 的条目正文里抽 `名称(6位码)` 构造（同一证据来源）。

    M2（C01/C06）：
    - C01：执行摘要的字数预算**不含**反方审查修订说明（`executive_summary_revision`，由
      `apply_counter_check` 写入）——实测拼接把 150–220 字摘要顶到 438 字并触发硬上限告警；
      结果里 `summary_chars` 为摘要正文字数（进预算）、`summary_revision_chars` 为修订说明
      字数（不进预算）、`summary_chars_total` 为前端实际展示的总字数；
    - C06：`critical_boards`（PE 分位 − PB 分位背离 30–40pp 的临界板块）被执行摘要拔高
      （出现在摘要最前段，或同句出现「最完整/首选/优先级最高」）→ 告警 + 降 needs_review。
    """
    _mode_name, mode_label, mode_min, mode_max = direction_mode_budget(mode)
    form_name = str(form or DIRECTION_FORM_FULL)
    form_required = direction_form_sections(form_name)
    if form_name == DIRECTION_FORM_STAGE:
        # D01：阶段性研究用短篇预算（缺失数据时不强求模式字数）。
        mode_label = DIRECTION_FORM_LABELS[DIRECTION_FORM_STAGE]
        mode_min, mode_max = DIRECTION_STAGE_CHAR_MIN, DIRECTION_STAGE_CHAR_MAX
    warnings: list[str] = []
    blockers: list[dict[str, Any]] = []

    report_text = parsed.get("report") if isinstance(parsed.get("report"), str) else ""
    chars = direction_char_count(report_text)
    if chars < 50:
        blockers.append({
            "code": DIRECTION_BLOCKING_EMPTY_BODY,
            "message": "模型未返回有效研判正文，报告不完整。",
        })
    if chars < mode_min:
        warnings.append(f"正文 {chars} 字，低于{mode_label}建议下限 {mode_min} 字（软告警）。")
    elif chars > mode_max:
        warnings.append(f"正文 {chars} 字，超过{mode_label}建议上限 {mode_max} 字（软告警；新增篇幅应用于证据解读，不用于重复罗列）。")

    bodies = section_bodies(report_text)
    section_states: list[dict[str, Any]] = []
    missing: list[str] = []
    missing_optional: list[str] = []
    for name in REQUIRED_DIRECTION_SECTIONS:
        entry = next((item for item in bodies if name in item["name"]), None)
        found_chars = int(entry["chars"]) if entry else 0
        present = entry is not None and found_chars >= MIN_SECTION_BODY_CHARS
        required_here = name in form_required
        section_states.append({
            "section": name, "chars": found_chars, "present": present,
            "required": required_here,
        })
        if present:
            continue
        if required_here:
            missing.append(name)
        else:
            missing_optional.append(name)
    if missing:
        blockers.append({
            "code": DIRECTION_BLOCKING_MISSING_SECTION,
            "sections": missing,
            "message": f"缺少必需小节：{'、'.join(missing)}（未生成，或仅有标题没有实质内容）。",
        })
        warnings.append(f"缺少必需小节：{'、'.join(missing)}。")
    if missing_optional:
        # D01：形态非必需的小节缺失只作软告警——不为凑节反复写「无法判断」。
        warnings.append(
            f"「{direction_form_label(form_name)}」形态下非必需小节未展开："
            f"{'、'.join(missing_optional)}（不阻断交付，证据补齐后可升级为完整研判）。"
        )

    judgments = normalize_judgments(parsed.get("core_judgments"))
    if len(judgments) < 3:
        blockers.append({
            "code": DIRECTION_BLOCKING_NO_JUDGMENTS,
            "message": f"核心判断仅 {len(judgments)} 条（至少 3 条），无法支撑方向结论的证据链。",
        })
    kinds_present = {item["kind"] for item in judgments}
    for layer in ("industry", "competitiveness", "sentiment"):
        if layer not in kinds_present and judgments:
            warnings.append(f"核心判断缺少「{JUDGMENT_KIND_LABELS[layer]}」层（B05：产业景气/竞争力/资金情绪须分开陈述）。")

    summary_raw = str(parsed.get("executive_summary", "") or "")
    # —— C01（M2）：反方审查修订说明不计入摘要字数预算 ——
    # 修订说明是**服务端生成的过程说明**（撤下/降级了几条、最强反方论点），原文摘要才是
    # 模型写的结论。实测（2026-09-15 22:55 样报）两者直接拼接把摘要顶到 438 字 → 硬上限告警。
    summary_revision, summary = split_summary_revision(parsed)
    summary_chars = len(_WHITESPACE.sub("", summary))
    summary_revision_chars = len(_WHITESPACE.sub("", summary_revision))
    summary_chars_total = len(_WHITESPACE.sub("", summary_raw))
    revision_note = (
        f"；另有反方审查修订说明 {summary_revision_chars} 字（服务端生成，不计入预算）"
        if summary_revision_chars else ""
    )
    if summary_chars > DIRECTION_SUMMARY_HARD_MAX:
        warnings.append(
            f"执行摘要 {summary_chars} 字，超过 {DIRECTION_SUMMARY_HARD_MAX} 字上限（软告警）{revision_note}。"
        )
    elif summary and summary_chars < DIRECTION_SUMMARY_TARGET_MIN:
        warnings.append(
            f"执行摘要 {summary_chars} 字，低于目标区间 {DIRECTION_SUMMARY_TARGET_MIN}"
            f"-{DIRECTION_SUMMARY_TARGET_MAX} 字（软告警）{revision_note}。"
        )

    # 引用真实性：正文/判断引用的 E# 必须属于本次证据快照（知识概览模式下不允许 E# 引用）。
    used_refs = set(re.findall(r"\[(E\d+)\]", f"{report_text}\n{summary}"))
    for item in judgments:
        used_refs.update(item["support_refs"])
    fake_refs = sorted(used_refs - evidence_ids) if evidence_ids else sorted(used_refs)
    if research_mode != "evidence" and used_refs:
        warnings.append(f"知识概览模式下出现 {len(used_refs)} 处 [E#] 引用（{'、'.join(sorted(used_refs))}），相关内容按无证据对待。")
    elif research_mode == "evidence" and fake_refs:
        warnings.append(f"引用了证据快照中不存在的编号（{'、'.join(fake_refs)}），相关内容按无证据对待。")

    # —— C02（v35）：估值断言引用闸门 ——
    # 正文出现估值水位类表述时，同小节内必须有真实 [E#] 引用；虚构编号不算引用。
    valuation_claims = find_valuation_claims(report_text)
    unsupported = [
        item for item in valuation_claims
        if not item["refs"] or (
            research_mode == "evidence"
            and evidence_ids
            and not (set(item["refs"]) & evidence_ids)
        )
    ]
    if unsupported:
        for item in unsupported:
            warnings.append(
                f"「{item['section']}」小节出现估值水位表述（{'、'.join(item['claims'])}）"
                "但无对应证据引用——按 C02 口径该表述不可核对，须补 [E#] 引用或删除。"
            )
        detail = "；".join(
            f"{item['section']}：{'、'.join(item['claims'])}" for item in unsupported[:5]
        )
        missing_information = [
            f"「{item['section']}」的估值水位表述缺少证据引用（{'、'.join(item['claims'])}），"
            "需补充证据编号或改写为可核对表述"
            for item in unsupported[:5]
        ]
    else:
        detail = ""
        missing_information = []
    # 多处违规 → 降 needs_review（引用闸门与「篇幅/摘要」类纯软告警区分开）。
    if len(unsupported) >= VALUATION_CLAIM_WARN_THRESHOLD:
        warnings.append(
            f"共 {len(unsupported)} 个小节存在无引用的估值水位表述（阈值 "
            f"{VALUATION_CLAIM_WARN_THRESHOLD}），报告证据可信度不足，需人工复核。"
        )

    # M1-B01：候选身份回填表——优先用调用方显式给的；否则从本次证据正文里抽「名称(6位码)」。
    resolved_name_index = name_index
    if resolved_name_index is None and evidence_index:
        resolved_name_index = build_name_symbol_index(texts=list(evidence_index.values()))
    candidates, pool_warnings = normalize_pool(
        parsed.get("stock_pool"), name_index=resolved_name_index
    )
    warnings.extend(pool_warnings)

    # —— C02（v36）：候选池象限约束 ——
    # 候选股所属板块的系统参考分类落在「深跌未反转/高位/盈利周期顶」时，正文必须给出
    # 推翻该板块分类的数值论证；否则候选池与板块判定脱钩（地产链/高位板块个股照进候选池）。
    quadrant = check_pool_quadrants(
        candidates, board_judgments or {}, report=report_text
    )
    quadrant_violations = list(quadrant["violations"])
    if quadrant_violations:
        for item in quadrant_violations:
            warnings.append(
                f"候选 {item['symbol']}（{item['sector'] or '未标板块'}）所属板块「{item['board']}」"
                f"的系统参考分类为「{item['label']}」，属 C02 口径下的非低估象限，"
                "但正文未见推翻该板块分类的数值论证——候选来源与板块判定脱钩，须补论证或撤下候选。"
            )
        quadrant_missing = [
            f"候选 {item['symbol']}（{item['board']}）需补「推翻 {item['board']} 分类为"
            f"「{item['label']}」」的数值论证，或改用低估候选象限的成分股"
            for item in quadrant_violations[:5]
        ]
    else:
        quadrant_missing = []
    if len(quadrant_violations) >= POOL_QUADRANT_VIOLATION_WARN_THRESHOLD:
        warnings.append(
            f"共 {len(quadrant_violations)} 只候选来自非低估象限且无论证（阈值 "
            f"{POOL_QUADRANT_VIOLATION_WARN_THRESHOLD}），候选池代表性不足，需人工复核。"
        )

    # —— C03（v36）：分类判定引用闸门 ——
    # 正文出现分类标签（低估候选/深跌未反转/高位/盈利周期顶）时，同小节须有 [E#] 引用，
    # 且引用条目须含对应板块的四维数值；估值水位表述闸门（v35 C02）已在上方独立生效。
    judgment_claims = find_judgment_claims(report_text)
    if board_evidence:
        # 虚构编号不算引用：先按 evidence_ids 过滤，再判是否落在板块估值条目上。
        for item in judgment_claims:
            valid_refs = [r for r in item["refs"] if not evidence_ids or r in evidence_ids]
            item["refs"] = valid_refs
            item["ok"] = bool(valid_refs)
    judgment_problems = judgment_refs_cover_boards(judgment_claims, board_evidence or {})
    if judgment_problems:
        reason_text = {
            "no_refs": "该小节无 [E#] 引用",
            "no_board_ref": "该小节的引用不含板块估值证据条目",
            "wrong_ref": "该小节点名了板块但未引用其估值证据条目",
        }
        for item in judgment_problems:
            extra = ""
            if item.get("boards"):
                extra = f"（涉及板块：{'、'.join(item['boards'])}）"
            warnings.append(
                f"「{item['section']}」小节出现分类判定表述（{'、'.join(item['claims'])}）"
                f"但{reason_text.get(item['reason'], '引用不可核对')}{extra}——"
                "按 C03 口径，分类判定须引用含该板块四维数值的证据条目，请补引用或改写。"
            )
        judgment_missing = [
            f"「{item['section']}」的分类判定（{'、'.join(item['claims'])}）需补含板块四维数值的"
            "证据引用（[E#]），否则分类结论不可核对"
            for item in judgment_problems[:5]
        ]
    else:
        judgment_missing = []
    if len(judgment_problems) >= JUDGMENT_CLAIM_WARN_THRESHOLD:
        warnings.append(
            f"共 {len(judgment_problems)} 个小节存在无证据支撑的分类判定（阈值 "
            f"{JUDGMENT_CLAIM_WARN_THRESHOLD}），分类结论可信度不足，需人工复核。"
        )
    data_gaps = (
        [str(g).strip() for g in parsed.get("data_gaps", []) if str(g).strip()]
        if isinstance(parsed.get("data_gaps"), list) else []
    )
    valid_pool = [c for c in candidates if c["identity_status"] == IDENTITY_VERIFIED and not c["duplicate_of_pool"]]
    invalid_pool = [c for c in candidates if c["identity_status"] == IDENTITY_INVALID]
    if invalid_pool:
        warnings.append(
            f"{len(invalid_pool)} 条候选代码无效（{'、'.join(c['symbol_raw'] for c in invalid_pool)}）——"
            "不可进入雷达扫描，需用户核对证券身份。"
        )
    if not valid_pool:
        if not data_gaps:
            warnings.append("候选池为空（或无身份有效候选）且未在 data_gaps 解释原因（B12：允许零候选，但必须解释）。")
    elif len(valid_pool) < POOL_TARGET_MIN or len(valid_pool) > POOL_TARGET_MAX:
        warnings.append(
            f"有效候选 {len(valid_pool)} 只，超出常规区间 {POOL_TARGET_MIN}-{POOL_TARGET_MAX}"
            f"（{'偏少' if len(valid_pool) < POOL_TARGET_MIN else '偏多'}，软告警；检查是否同质堆叠）。"
        )
    unexplained = [c for c in valid_pool if not c["business_link"] or not c["profit_path"]]
    if unexplained:
        warnings.append(
            f"{len(unexplained)} 只候选缺少业务关联或业绩兑现路径（{'、'.join(c['symbol'] for c in unexplained[:5])}）——"
            "按 B12 口径，解释不完整的候选需标注待核验。"
        )

    if research_mode == "knowledge" and not data_gaps:
        warnings.append("知识概览模式未声明数据缺口（B01：必须列出升级为证据研判所需的证据）。")

    # —— M3-D02：前置行业比较表 ——
    comparison = normalize_industry_comparison(parsed.get("industry_comparison"))
    comparison_problems = [
        row for row in comparison if row["priority_basis_missing"] or not row["research_priority"]
    ]
    if form_name != DIRECTION_FORM_STAGE and research_mode == "evidence" and not comparison:
        warnings.append(
            "未输出行业比较表（`industry_comparison`）——D02 要求先给出「先研究谁、为什么、"
            "等待什么变化」的前置比较表。"
        )
    if comparison_problems:
        warnings.append(
            f"{len(comparison_problems)} 行行业比较表缺少研究优先级或优先级依据"
            f"（{'、'.join(row['industry'] for row in comparison_problems[:5])}）——"
            "优先级必须写依据，且不强制编造数值评分。"
        )
    comparison_missing = [
        f"行业比较表「{row['industry']}」需补研究优先级依据（证据完整度/催化临近度/估值水位）"
        for row in comparison_problems[:5]
    ]

    # —— M3-D03：跨层因果（宏观 → 盈利）断言 ——
    cross_layer = find_cross_layer_claims(report_text)
    for item in cross_layer[:5]:
        warnings.append(
            f"「{item['section']}」小节出现跨层推断（{'、'.join(item['macro_terms'])} → "
            f"{'、'.join(item['profit_terms'])}）但同句无中间环节数据——"
            "D03 口径：宏观变量须经量价/成本/单产等中间证据才能支撑盈利结论，请改写为待验证假设。"
        )
    cross_layer_missing = [
        f"「{item['section']}」的跨层推断需补中间环节证据（量价/单位成本/单票收入/产能利用率）"
        f"或改写为待验证假设：{item['sentence'][:60]}"
        for item in cross_layer[:5]
    ]

    # —— M3-D04：周期行业正常化审查 ——
    cycle_gaps = cycle_normalization_gaps(report_text, board_judgments or {})
    for item in cycle_gaps[:5]:
        warnings.append(f"周期板块「{item['board']}」：{item['reason']}。")
    cycle_missing = [
        f"板块「{item['board']}」需披露正常化盈利口径（正常化口径/样本区间/假设）后重判估值"
        for item in cycle_gaps[:5]
    ]

    # —— M3-D06：研究对象池 / 低估候选池分离 ——
    low_pool, research_pool = split_pools(candidates, board_judgments or {})
    research_symbols = {
        str(item.get("symbol") or "").strip() for item in research_pool if str(item.get("symbol") or "").strip()
    }
    mislabeled: list[str] = []
    if research_symbols:
        # 待验证对象被写成「已确认低估」：同小节内出现该代码且含低估候选类标签 → 命中。
        for entry in section_texts(report_text):
            text = str(entry["text"])
            if not any(word in text for word in VALUATION_JUDGMENT_CLAIM_WORDS):
                continue
            for symbol in sorted(research_symbols):
                if symbol in text:
                    mislabeled.append(f"{symbol}（「{entry['name']}」小节）")
    if mislabeled:
        warnings.append(
            f"{len(mislabeled)} 处把**待验证研究对象**表述在低估候选语境中"
            f"（{'、'.join(mislabeled[:5])}）——D06 口径：待验证对象不得被标为已确认低估，"
            "请分列说明或补估值资格证据。"
        )
    mislabel_missing = [
        f"待验证对象 {item} 需移出低估候选语境或补齐估值/基本面资格证据"
        for item in mislabeled[:5]
    ]

    # —— M3-D07：引用编号—数值对应性 ——
    ref_support = check_reference_support(report_text, evidence_index)
    for item in ref_support[:5]:
        warnings.append(
            f"「{item['section']}」小节引用 {('、'.join(item['refs']))} 但句内数值"
            f"（{'、'.join(item['numbers'])}）不在该证据条目中——D07 口径：引用须真的支持结论，"
            "请改用含该数值的条目或不引用。"
        )
    ref_support_missing = [
        f"「{item['section']}」的引用 {('、'.join(item['refs']))} 与句内数值不对应，需核对引用条目"
        for item in ref_support[:5]
    ]

    # —— C06（M2）：临界板块（PE−PB 背离 30–40pp）不得被写成最强主线 ——
    critical_hits = find_critical_board_hype(summary_raw, critical_boards)
    for item in critical_hits:
        warnings.append(
            f"临界板块「{item['board']}」被执行摘要拔高（{'；'.join(item['reasons'])}）——"
            "C06 口径：PE 分位 − PB 分位背离落在 30–40pp 的板块属**临界**低估，"
            "不得写成最完整/最强/首选的主线，研究优先级下调，入选理由须写明盈利端背离尚未收敛。"
        )
    critical_missing = [
        f"临界板块「{item['board']}」需下调研究优先级并改写入选理由"
        "（背离 30–40pp，盈利端尚未收敛，不作主线）"
        for item in critical_hits[:5]
    ]
    if len(critical_hits) >= CRITICAL_BOARD_HYPE_WARN_THRESHOLD:
        warnings.append(
            f"共 {len(critical_hits)} 个临界板块被写成主线级结论（阈值 "
            f"{CRITICAL_BOARD_HYPE_WARN_THRESHOLD}），低估排序代表性不足，需人工复核。"
        )

    # —— M3-D08：同一缺口重复散落 ——
    repeated_gaps = find_repeated_gaps(report_text, data_gaps)
    for item in repeated_gaps[:5]:
        warnings.append(
            f"数据缺口「{item['gap'][:40]}」在 {len(item['sections'])} 个小节重复出现"
            f"（{'、'.join(item['sections'])}）——D08 口径：同一缺口集中说明一次，"
            "正文只保留影响比较与排序的信息。"
        )
    repeated_gap_missing = [
        f"缺口「{item['gap'][:40]}」需集中到数据缺口一处说明，删除正文重复表述"
        for item in repeated_gaps[:5]
    ]
    # 缺口清单去重（同一缺口只留一条）。
    data_gaps = merge_data_gaps([], data_gaps)

    status = report_quality.quality_status_of(blockers=blockers, warnings=warnings)
    # C02（v35）：多处估值断言无引用 → 覆盖率不足，降 needs_review（complete → needs_review）。
    if (
        status == report_quality.QUALITY_STATUS_COMPLETE
        and len(unsupported) >= VALUATION_CLAIM_WARN_THRESHOLD
    ):
        status = report_quality.QUALITY_STATUS_NEEDS_REVIEW
    # C02（v36）：多项候选脱离低估象限且无论证 → 候选池代表性不足，同样降 needs_review。
    if (
        status == report_quality.QUALITY_STATUS_COMPLETE
        and len(quadrant_violations) >= POOL_QUADRANT_VIOLATION_WARN_THRESHOLD
    ):
        status = report_quality.QUALITY_STATUS_NEEDS_REVIEW
    # C03（v36）：多处分类判定无证据支撑 → 分类结论可信度不足，同样降 needs_review。
    if (
        status == report_quality.QUALITY_STATUS_COMPLETE
        and len(judgment_problems) >= JUDGMENT_CLAIM_WARN_THRESHOLD
    ):
        status = report_quality.QUALITY_STATUS_NEEDS_REVIEW
    # M3-D03：跨层因果推断（宏观→盈利无中间证据）多处出现 → 结论可信度不足。
    if (
        status == report_quality.QUALITY_STATUS_COMPLETE
        and len(cross_layer) >= CROSS_LAYER_WARN_THRESHOLD
    ):
        status = report_quality.QUALITY_STATUS_NEEDS_REVIEW
    # M3-D04：周期板块低估判定未披露正常化口径 → 低 PE 陷阱风险，需复核。
    if (
        status == report_quality.QUALITY_STATUS_COMPLETE
        and len(cycle_gaps) >= CYCLE_NORMALIZATION_WARN_THRESHOLD
    ):
        status = report_quality.QUALITY_STATUS_NEEDS_REVIEW
    # M3-D06：待验证对象被写成已确认低估 → 分类误导，必须复核。
    if (
        status == report_quality.QUALITY_STATUS_COMPLETE
        and len(mislabeled) >= RESEARCH_POOL_MISLABEL_THRESHOLD
    ):
        status = report_quality.QUALITY_STATUS_NEEDS_REVIEW
    # M3-D07：引用编号与句内数值不对应多处 → 引用不可核对。
    if (
        status == report_quality.QUALITY_STATUS_COMPLETE
        and len(ref_support) >= REFERENCE_SUPPORT_WARN_THRESHOLD
    ):
        status = report_quality.QUALITY_STATUS_NEEDS_REVIEW
    # M2-C06：临界板块被写成主线级结论 → 低估排序不可采信，需复核。
    if (
        status == report_quality.QUALITY_STATUS_COMPLETE
        and len(critical_hits) >= CRITICAL_BOARD_HYPE_WARN_THRESHOLD
    ):
        status = report_quality.QUALITY_STATUS_NEEDS_REVIEW
    return {
        "quality_status": status,
        "quality_status_label": report_quality.QUALITY_STATUS_LABELS[status],
        "quality_status_version": DIRECTION_QUALITY_VERSION,
        "quality_blockers": blockers,
        "quality_warnings": warnings,
        "missing_sections": missing,
        "missing_optional_sections": missing_optional,
        "section_states": section_states,
        "report_chars": chars,
        "summary_chars": summary_chars,
        # C01（M2）：修订说明字数与前端展示总字数（预算只认 `summary_chars`）。
        "summary_revision_chars": summary_revision_chars,
        "summary_chars_total": summary_chars_total,
        "mode": _mode_name,
        "mode_label": mode_label,
        "mode_budget": {"min": mode_min, "max": mode_max},
        # M3-D01：报告形态（必需小节/篇幅随证据可得性收窄）。
        "report_form": form_name,
        "report_form_label": direction_form_label(form_name),
        "form_required_sections": list(form_required),
        # M3-D02：前置行业比较表（结构化，供前端与导出共用）。
        "industry_comparison": comparison,
        "research_mode": research_mode,
        "judgments": judgments,
        "pool": candidates,
        # M3-D06：低估候选池 vs 待验证研究池（同一候选全集的两个视图）。
        "low_valuation_pool": low_pool,
        "research_pool": research_pool,
        "valid_pool_count": len(valid_pool),
        "invalid_pool_count": len(invalid_pool),
        "fake_refs": fake_refs if research_mode == "evidence" else sorted(used_refs),
        "data_gaps": data_gaps,
        # M3-D03/D04/D07/D08：确定性检查留痕（供缺口清单与前端复核块共用）。
        "cross_layer_claims": cross_layer,
        "cycle_normalization_gaps": cycle_gaps,
        "reference_support_problems": ref_support,
        "repeated_gaps": repeated_gaps,
        "research_pool_mislabeled": mislabeled,
        # C06（M2）：临界板块被执行摘要拔高的留痕（临界板块清单 / 拔高命中的板块）。
        "critical_boards": sorted(str(b) for b in (critical_boards or []) if str(b).strip()),
        "critical_board_hype": critical_hits,
        # C02（v35）：估值断言引用闸门留痕（命中小节 / 无引用小节 / 需补信息）。
        "valuation_claims": valuation_claims,
        "unsupported_valuation_claims": unsupported,
        # C02（v36）：候选池象限约束留痕（违规候选 / 已论证的推翻 / 无法归属的候选）。
        "pool_quadrant_violations": quadrant_violations,
        "pool_quadrant_allowed": list(quadrant["allowed"]),
        "pool_quadrant_unmatched": list(quadrant["unmatched"]),
        "pool_override_arguments": list(quadrant["override_hits"]),
        # C03（v36）：分类判定引用闸门留痕（命中小节 / 无支撑的分类判定）。
        "judgment_claims": judgment_claims,
        "unsupported_judgment_claims": judgment_problems,
        "missing_information": (
            missing_information + quadrant_missing + judgment_missing
            + comparison_missing + cross_layer_missing + cycle_missing
            + mislabel_missing + ref_support_missing + repeated_gap_missing
            + critical_missing
        ),
        "prompt_version": DIRECTION_PROMPT_VERSION,
    }


# ---------------------------------------------------------------------------
# B06：方向研判反方审查（单次短调用，不重写正文）
# 检查「需求是否前置透支、供给是否过剩、政策是否已兑现、价格下行是否吞噬增长、
# 资本开支与现金回报是否恶化」，覆盖全部核心判断与候选入选理由；
# 输出受影响结论（判断 id 级）与候选质疑，不只重复风险列表。
# ---------------------------------------------------------------------------

DIRECTION_COUNTER_CHECK_PROMPT_VERSION = "1.0"

_DIRECTION_COUNTER_SYSTEM = (
    "你是产业方向研判的「反方审查员」：不重写报告，只指出软肋。"
    "重点检查：需求是否被前置透支、供给是否过剩扩张、政策是否已经兑现（利多出尽）、"
    "价格下行是否吞噬量能增长、资本开支是否恶化现金回报。"
    # M3-D07：引用支持性交给模型审查（确定性检查只覆盖编号—数值对应）。
    "同时核对**引用是否真的支持结论**：编号对应的证据条目能否支撑该句的数值、期间与口径；"
    "把「挂了编号但内容不相关」的情形写进受影响结论的 reason 里。"
    "必须覆盖全部核心判断与每一家候选公司的入选理由；"
    "输出受影响的结论编号，不许只给「行业有风险」式空话。只输出单个合法 JSON。"
)


def direction_counter_check_messages(
    *,
    topic: str,
    title: str,
    report: str,
    judgments: list[dict[str, Any]],
    pool: list[dict[str, Any]],
) -> tuple[str, str]:
    """构造方向反方审查的 (system, user)。判断与候选理由全部送审（B06 覆盖要求）。"""
    judgment_lines = [
        f"- [{item['id']}·{item['kind_label']}] {item['text']}（置信 {item['confidence'] or '未标注'}）"
        for item in judgments
    ] or ["- （无）"]
    pool_lines = [
        f"- {item['symbol']} {item['name']}（{item['sector'] or '环节未知'}）："
        f"关联={item['business_link'] or '未说明'}；兑现路径={item['profit_path'] or '未说明'}；"
        f"反证={item['counter_evidence'] or '未说明'}"
        for item in pool[:8]
    ] or ["- （无候选）"]
    user = (
        f"【研究主题】{topic} · {title}\n"
        f"【核心判断清单（全部送审）】\n" + "\n".join(judgment_lines) + "\n"
        "【候选公司及入选理由（全部送审）】\n" + "\n".join(pool_lines) + "\n"
        f"【研判正文（按小节送审）】\n{report[:6000]}\n\n"
        "请输出严格 JSON（无 markdown 围栏）：\n"
        '{"strongest_counter": "最强反方论点（60 字内：指出哪条判断/哪条证据链站不住）", '
        '"affected_conclusions": [{"id": "J1", "effect": "weakens|supports|cannot_judge", '
        '"reason": "40 字内：反方视角如何影响该判断"}], '
        '"pool_challenges": [{"symbol": "600xxx", "challenge": "该候选入选理由的最大疑点（40 字内）"}], '
        '"verdict_robustness": "robust|mixed|fragile"}\n'
        "铁律：id 只能取上面核心判断清单里的编号；symbol 只能取候选清单里的代码；"
        "不确定就 cannot_judge，不硬选。"
    )
    return _DIRECTION_COUNTER_SYSTEM, user


def normalize_direction_counter_check(
    parsed: Any,
    *,
    judgment_ids: set[str],
    pool_symbols: set[str],
) -> dict[str, Any] | None:
    """归一化方向反方审查输出；`strongest_counter` 缺失 → None（未完成如实标注）。"""
    if not isinstance(parsed, dict):
        return None
    strongest = str(parsed.get("strongest_counter") or "").strip()
    if not strongest:
        return None
    dropped: list[str] = []
    affected: list[dict[str, str]] = []
    raw_affected = parsed.get("affected_conclusions")
    if isinstance(raw_affected, list):
        for item in raw_affected[:8]:
            if not isinstance(item, dict):
                continue
            jid = str(item.get("id", "")).strip()
            if not jid:
                continue
            if judgment_ids and jid not in judgment_ids:
                dropped.append(jid)
                continue
            effect = str(item.get("effect", "")).strip().lower()
            affected.append({
                "id": jid,
                "effect": effect if effect in ("weakens", "supports", "cannot_judge") else "cannot_judge",
                "reason": str(item.get("reason", "")).strip()[:160],
            })
    challenges: list[dict[str, str]] = []
    raw_challenges = parsed.get("pool_challenges")
    if isinstance(raw_challenges, list):
        for item in raw_challenges[:8]:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).strip()
            challenge = str(item.get("challenge", "")).strip()[:160]
            if symbol and pool_symbols and symbol not in pool_symbols:
                dropped.append(symbol)
                continue
            if symbol or challenge:
                challenges.append({"symbol": symbol, "challenge": challenge})
    robustness = str(parsed.get("verdict_robustness", "")).strip().lower()
    return {
        "strongest_counter": strongest[:240],
        "affected_conclusions": affected,
        "pool_challenges": challenges,
        "dropped_refs": dropped,
        "verdict_robustness": robustness if robustness in ("robust", "mixed", "fragile") else "mixed",
        "prompt_version": DIRECTION_COUNTER_CHECK_PROMPT_VERSION,
    }


# ---------------------------------------------------------------------------
# M3-D05：反方审查进入定稿（确定性执行删除/降级，同步摘要与风险段落）
# ---------------------------------------------------------------------------

COUNTER_APPLY_POLICY_VERSION = "v2"
# 执行口径（确定性，不依赖模型二次改写）：
# - `verdict_robustness == "fragile"` 且该判断 `weakens` → **撤下**（移出 core_judgments）；
# - 其余 `weakens` / `cannot_judge` → **降级**（confidence 置 low + 标记 counter_effect）；
# - 正文含该判断原文的句子追加行内标注（正文不得保留强结论而文末又承认依据不足）；
# - v2：`core_judgments[].text` 是模型**压缩过的结论**，未必与正文同句同词，v1 的
#   「前 14 字探针」在真实报告里全部落空（实测 E02：3/3 miss 且摘要仍声称「正文已标注」）。
#   故 v2 增加最长公共片段回退匹配；仍无法定位的判断**不得留白**，改在正文开头集中列出；
#   摘要文案按实际标注条数生成，不出现「已标注」而实际为 0 的虚假声明。
# - 执行摘要前置修订说明；风险段落追加最强反方论点与候选疑点。
COUNTER_ANNOTATION_DOWNGRADED = "（反方审查：依据不足，已降级为待验证判断，不作为结论依据）"
COUNTER_ANNOTATION_REMOVED = "（反方审查：依据不足，已从结论中撤下）"
COUNTER_SUMMARY_PREFIX = "【反方审查修订】"
COUNTER_BODY_BLOCK_HEADER = (
    "【反方审查：以下判断已撤下/降级，未能在正文逐句定位，集中列出】"
)
# 回退匹配的最长公共片段下限：低于该长度视为「碰巧雷同」，不据此标注（宁缺勿错）。
COUNTER_FALLBACK_MIN_RUN = 8


def _longest_common_run(a: str, b: str) -> int:
    """最长公共子串长度（滚动数组；正文行不长，代价可忽略）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ch_a in a:
        cur = [0] * (len(b) + 1)
        for j, ch_b in enumerate(b, start=1):
            if ch_a == ch_b:
                value = prev[j - 1] + 1
                cur[j] = value
                if value > best:
                    best = value
        prev = cur
    return best


def _match_report_line(report: str, original: str) -> tuple[int | None, bool]:
    """定位最可能承载该判断的正文行号：返回 `(行号或 None, 是否走了回退匹配)`。

    两级策略：①「前 14 字探针」精确命中；②退化为最长公共片段（≥ `COUNTER_FALLBACK_MIN_RUN`）。
    """
    flat_original = _WHITESPACE.sub("", original)
    lines = report.splitlines()
    flat_lines = [_WHITESPACE.sub("", line) for line in lines]
    probe = flat_original[:14]
    if len(probe) >= 6:
        for idx, flat in enumerate(flat_lines):
            if probe and probe in flat:
                return idx, False
    best_idx: int | None = None
    best_run = 0
    for idx, flat in enumerate(flat_lines):
        run = _longest_common_run(flat_original, flat)
        if run > best_run:
            best_run, best_idx = run, idx
    if best_idx is not None and best_run >= COUNTER_FALLBACK_MIN_RUN:
        return best_idx, True
    return None, False



def apply_counter_check(
    parsed: dict[str, Any],
    counter: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """D05：把反方审查结论**落到定稿**（就地修订并返回 `(parsed, record)`）。

    `counter` 为 `normalize_direction_counter_check` 的输出（含 `ok` 标记）。
    - 未完成/无内容 → 原样返回 + `{"applied": False, "reason": ...}`（如实标注，不假装已修订）；
    - 撤下/降级的判断在正文对应句子处追加行内标注（前 14 字探针 → 最长公共片段回退）；
    - 仍无法定位的判断**集中列在正文开头**（v2），不留白也不假装已标注；
    - 摘要前置修订说明（文案按实际标注条数生成）；风险追加最强反方论点与候选疑点；
      催化段落不可稳定映射 → 如实记录未改写原因。
    """
    record: dict[str, Any] = {
        "applied": False,
        "policy_version": COUNTER_APPLY_POLICY_VERSION,
        "removed_judgment_ids": [],
        "downgraded_judgment_ids": [],
        "annotated_sentences": 0,
        "annotation_fallback_hits": 0,
        "annotation_misses": [],
        "body_block_entries": 0,
        "summary_revised": False,
        "risks_appended": 0,
        "catalysts_untouched_reason": "",
        "strongest_counter": "",
    }
    if not isinstance(counter, dict) or not counter.get("ok"):
        record["reason"] = "反方审查未完成（未执行/解析失败/关闭），未执行定稿修订"
        return parsed, record
    affected = [
        item for item in (counter.get("affected_conclusions") or [])
        if isinstance(item, dict) and item.get("id")
    ]
    if not affected:
        record["reason"] = "反方审查未给出受影响结论编号，无可执行修订"
        return parsed, record

    fragility = str(counter.get("verdict_robustness") or "mixed")
    judgments = normalize_judgments(parsed.get("core_judgments"))
    by_id = {item["id"]: item for item in judgments}
    label_of = {"weakens": "削弱", "cannot_judge": "无法判断", "supports": "支持"}

    removed: list[str] = []
    downgraded: list[str] = []
    annotations: list[tuple[str, str]] = []  # (判断原文, 标注)
    for item in affected:
        jid = str(item["id"])
        effect = str(item.get("effect") or "")
        target = by_id.get(jid)
        if target is None or effect == "supports":
            continue
        if fragility == "fragile" and effect == "weakens":
            removed.append(jid)
            annotations.append((target["text"], COUNTER_ANNOTATION_REMOVED))
        else:
            downgraded.append(jid)
            annotations.append((target["text"], COUNTER_ANNOTATION_DOWNGRADED))

    if not removed and not downgraded:
        record["reason"] = "反方审查未指出被削弱或无法判断的结论，无需修订"
        return parsed, record

    kept: list[dict[str, Any]] = []
    removed_set = set(removed)
    for item in judgments:
        jid = item["id"]
        if jid in removed_set:
            continue
        if jid in downgraded:
            item = dict(item)
            item["confidence"] = "low"
            item["counter_effect"] = "downgraded"
            item["counter_note"] = next(
                (str(x.get("reason") or "") for x in affected if str(x.get("id")) == jid), ""
            )
        kept.append(item)

    # —— 正文行内标注：正文不得保留强结论 ——
    report = str(parsed.get("report") or "")
    annotated = 0
    fallback_hits = 0
    misses: list[str] = []
    unlocated: list[tuple[str, str]] = []
    for original, note in annotations:
        idx, used_fallback = _match_report_line(report, str(original))
        if idx is None:
            misses.append(str(original)[:40])
            unlocated.append((str(original), note))
            continue
        lines = report.splitlines()
        if note in lines[idx]:
            continue  # 该行已带同标注，不重复追加
        lines[idx] = f"{lines[idx]}{note}"
        report = "\n".join(lines)
        annotated += 1
        if used_fallback:
            fallback_hits += 1

    # 未能在正文定位的判断不得留白：在正文开头集中列出（否则「正文保留强结论」依旧成立）
    if unlocated:
        block = [COUNTER_BODY_BLOCK_HEADER]
        for text, note in unlocated:
            block.append(f"- {text[:120]}{note}")
        report = "\n".join(block) + "\n\n" + report

    # —— 摘要修订说明（按实际标注条数生成，不出现「已标注」而实际为 0） ——
    summary = str(parsed.get("executive_summary") or "")
    body_parts: list[str] = []
    if annotated:
        body_parts.append(f"正文已逐句标注 {annotated} 处")
    if unlocated:
        body_parts.append(f"{len(unlocated)} 条未能在正文逐句定位，已在正文开头集中列出")
    body_note = ("；".join(body_parts) + "。") if body_parts else ""
    revision = (
        f"{COUNTER_SUMMARY_PREFIX}经反方审查，撤下 {len(removed)} 条判断"
        f"{('（' + '、'.join(removed) + '）') if removed else ''}，"
        f"降级 {len(downgraded)} 条判断"
        f"{('（' + '、'.join(downgraded) + '）') if downgraded else ''}——"
        f"以上判断不作为结论依据。{body_note}"
    )
    strongest = str(counter.get("strongest_counter") or "").strip()
    if strongest:
        revision += f"最强反方论点：{strongest}"
    # C01（M2）：修订说明单独成字段——它是服务端生成的过程说明，**不计入摘要字数预算**
    # （否则 150–220 字摘要被顶到 438 字，触发无意义的硬上限告警）。
    parsed["executive_summary_revision"] = revision
    parsed["executive_summary"] = f"{revision}{summary}" if summary else revision

    # —— 风险段落追加（催化段落无法稳定映射，如实记录） ——
    risks = [str(x).strip() for x in (parsed.get("risks") or []) if str(x).strip()]
    appended = 0
    if strongest and f"反方审查：{strongest}" not in risks:
        risks.append(f"反方审查：{strongest}")
        appended += 1
    for challenge in (counter.get("pool_challenges") or [])[:8]:
        if not isinstance(challenge, dict):
            continue
        symbol = str(challenge.get("symbol") or "").strip()
        text = str(challenge.get("challenge") or "").strip()
        if not text:
            continue
        entry = f"反方审查（候选 {symbol or '未标代码'}）：{text}"
        if entry not in risks:
            risks.append(entry)
            appended += 1
    parsed["risks"] = risks
    parsed["report"] = report
    # 判断清单必须以**修订后**为准写回（撤下的不再出现在 core_judgments）。
    parsed["core_judgments"] = kept

    record.update({
        "applied": True,
        "removed_judgment_ids": removed,
        "downgraded_judgment_ids": downgraded,
        "annotated_sentences": annotated,
        "annotation_fallback_hits": fallback_hits,
        "annotation_misses": misses,
        "body_block_entries": len(unlocated),
        "summary_revised": True,
        "risks_appended": appended,
        "catalysts_untouched_reason": (
            "催化条目与判断无稳定映射（模型自由文本），未做确定性改写——"
            "若催化依赖已撤下判断，须在下一版提示词/重跑中重生成"
        ),
        "strongest_counter": strongest,
        "affected_effects": {str(x["id"]): label_of.get(str(x.get("effect")), "无法判断") for x in affected},
    })
    return parsed, record

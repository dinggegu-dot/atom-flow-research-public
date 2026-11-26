"""Use source structure to name and file reports in the existing Obsidian library."""

import re
import unicodedata


PENDING_DOMAIN = "待整理"
DOMAIN_TERMS = {
    "企业与投资": (
        "估值", "基本面", "财报", "财务报表", "商业模式", "经营现金流", "毛利率", "回购",
        "并购", "产业整合", "公司治理", "创业融资", "选股", "筹码出清", "备兑", "总回报", "valuation",
    ),
    "宏观与金融": (
        "宏观经济", "货币政策", "利率", "汇率", "降息", "加息", "通胀", "通缩", "美联储",
        "央行", "国债", "财政赤字", "信用利差", "城投", "黄金", "monetary policy", "inflation",
    ),
    "治理与地缘": (
        "地缘", "外交", "关税", "制裁", "中东", "主权", "议会", "政府治理", "公共政策",
        "城市治理", "基层治理", "地方治理", "社会治理", "乡村治理", "地方势力", "地方权力", "派系结构",
        "大国博弈", "中美", "军事冲突", "停火", "战场", "政治", "权力结构", "geopolitics",
    ),
    "社会与劳动": (
        "就业", "劳动力", "劳动法", "社会保障", "养老保险", "劳动者", "工资", "岗位",
        "教育机会", "城乡差距", "住房保障", "阶层", "代际", "人口老龄化", "社保", "平台用工", "职场",
        "labor market", "labour market",
    ),
    "科技与产业": (
        "机器人", "具身智能", "人工智能", "半导体", "芯片", "算力", "制造业", "产业链",
        "数据中心", "大模型", "软件工程", "技术路线", "合成生物", "光刻", "产业升级", "新能源",
        "端侧ai", "端侧 ai", "存储行业", "ai asic", "robotics", "artificial intelligence", "semiconductor",
    ),
    "认知与方法": (
        "研究方法", "因果推断", "观察偏差", "认知偏差", "注意力", "决策方法", "框架思维",
        "组织协作", "实验设计", "统计推断", "行为偏差", "分析方法", "验证方法", "资料核验",
        "认知科学", "research methods", "causal inference",
    ),
}

_GENERIC_TITLE = re.compile(
    r"(?:export|untitled(?: document)?|(?:new |exported )?document|download|scan|(?:research )?report|"
    r"file|doc|note|attachment|未命名|新建文档|新建(?: microsoft word)? 文档|"
    r"研报|文档|扫描件|下载|转写结果)(?:[\s_.()（）-]*(?:\d[\d\s_.()（）:-]*|[a-f\d]{8,}))?"
    r"|[\d\s_.()（）:-]+|[a-f\d]{8,}(?:-[a-f\d]+)*",
    re.IGNORECASE,
)
_SECTION_LABELS = frozenset(("摘要", "导读", "前言", "引言", "目录", "正文", "概述", "结论", "研究摘要",
                             "仅供参考", "内部资料", "证券研究报告", "专题研究报告", "行业研究"))
_MARKDOWN_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+")


def is_generic_title(stem):
    """Recognize machine-exported names and labels that do not identify an article."""
    stem = unicodedata.normalize("NFKC", stem).strip()
    return not stem or _GENERIC_TITLE.fullmatch(stem) is not None


def _heading_text(text):
    if _MARKDOWN_HEADING.match(text):
        text = re.sub(r"\s+#+\s*$", "", _MARKDOWN_HEADING.sub("", text))
    return " ".join(text.split())


def _title_like(text):
    return 2 <= len(text) <= 80 and not is_generic_title(text) and text.strip("：:") not in _SECTION_LABELS


def _article_title(stem, parts):
    generic = is_generic_title(stem)
    for part in parts[:4]:
        kind = part["kind"]
        markdown_h1 = _MARKDOWN_HEADING.match(part["text"])
        if kind == "title" or (kind == "heading" and markdown_h1 and markdown_h1[1] == "#"):
            title = _heading_text(part["text"])
            if _title_like(title):
                return title
        if kind == "paragraph":
            break
    if not generic:
        return stem
    if parts:
        first = parts[0]
        title = _heading_text(first["text"].strip().partition("\n")[0])
        if first["kind"] in {"title", "heading"} and _title_like(title):
            return title
        if first["kind"] == "paragraph" and _title_like(title) and not re.search(r"[。.!?！？；;]$", title):
            return title
    return "待命名研报"


def _term_pattern(term):
    escaped = re.escape(term)
    return re.compile(r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])" if term.isascii() else escaped)


_DOMAIN_PATTERNS = {domain: tuple(_term_pattern(term) for term in terms) for domain, terms in DOMAIN_TERMS.items()}


def _normalize(text):
    return unicodedata.normalize("NFKC", text).casefold()


def classify_article(stem, parts):
    """Return a readable title and one existing domain, with an explicit pending fallback."""
    title = _article_title(stem, parts)
    title_text = _normalize(title)
    headings = _normalize("\n".join(_heading_text(part["text"]) for part in parts[:24]
                                    if part["kind"] in {"heading", "title"}))
    # PDF 的文本块常短于自然段。以字符数限制取样，避免封面和目录占满前二十个块。
    remaining = 9000
    body_parts = []
    for part in parts:
        if part["kind"] != "paragraph":
            continue
        excerpt = part["text"][:min(450, remaining)]
        body_parts.append(excerpt)
        remaining -= len(excerpt) + 1
        if remaining <= 0:
            break
    body = _normalize("\n".join(body_parts))
    scores = {}
    for domain, patterns in _DOMAIN_PATTERNS.items():
        title_hits = {term for term in patterns if term.search(title_text)}
        heading_hits = {term for term in patterns if term not in title_hits and term.search(headings)}
        already_seen = title_hits | heading_hits
        body_hits = {term for term in patterns if term not in already_seen and term.search(body)}
        # 坚持由题名、主标题优先。同一词在全文重复不会放大分数。
        scores[domain] = 20 * min(3, len(title_hits)) + 4 * min(3, len(heading_hits)) + min(5, len(body_hits))
    first, second, *_ = sorted(scores, key=scores.get, reverse=True)
    domain = first if scores[first] >= 5 and scores[first] - scores[second] >= 3 else PENDING_DOMAIN
    return title, domain

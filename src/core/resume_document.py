"""
简历"完整结构化文档"解析 —— 定制简历用的底稿。

跟 resume_ast.py 的区别：resume_ast 只为"检索打分"服务，把简历拆成
Region A（关键词）+ Region B（句子 chunk），姓名/联系方式/summary/技能/
教育背景这些非经历内容全部被丢弃或压成关键词，没法还原。

定制简历不是"重新攒一份"，而是"以完整原文为底稿，只在工作经历 bullet
层面做针对性调整（排序/措辞），其余原样保留"。这个模块把上传简历解析成
一棵能完整还原的结构树：
- 姓名 / 联系方式行 / 图片引用（照片）—— 原样保留
- SUMMARY / SKILLS / EDUCATION / 其它板块 —— 整段原样保留
- PROFESSIONAL EXPERIENCE / PROJECTS —— 拆成条目（标题行 + 时间 + bullet 列表），
  条目和 bullet 可以重排/措辞微调，但不允许增删

复用 resume_ast 里已经打磨过的板块切分 / 条目切分 / 软换行合并逻辑。
"""

import re
from dataclasses import dataclass, field

from src.core.keyword_dicts import classify_section
from src.core.resume_ast import (
    _split_into_sections,
    _split_experience_entries,
    _split_entry_header_body,
    _TIME_RANGE,
    _MD_HEADING,
)
from src.core.text_utils import _reflow_soft_wraps, _BULLET_PREFIX


SECTION_KIND_VERBATIM = ("name", "summary", "skills", "education", "other")
SECTION_KIND_ENTRIES = ("experience", "projects")


@dataclass
class ResumeEntry:
    """PROFESSIONAL EXPERIENCE / PROJECT 里的一条经历/项目。"""
    title: str                      # "公司 | 项目 | 职位" 整行（去掉 # / ** / 尾部 | / 时间），完整保留
    date: str                       # "Feb 2025 – July 2025"，抠不到就是空串
    bullets: list[str] = field(default_factory=list)     # 每条成就一项，去掉 "- " 前缀
    extra_lines: list[str] = field(default_factory=list)  # 条目下非 bullet 的零散行（原样保留，不丢）


@dataclass
class ResumeSection:
    title: str                      # 板块标题文字（去掉 # / **）
    kind: str                       # name | summary | skills | education | experience | projects | other
    body_lines: list[str] = field(default_factory=list)  # 非条目板块：原样行（含 "- " / "**" 标记）
    entries: list[ResumeEntry] = field(default_factory=list)  # 条目板块


@dataclass
class ResumeDocument:
    name: str
    contact_lines: list[str] = field(default_factory=list)
    image_lines: list[str] = field(default_factory=list)   # Markdown 图片行 ![](...)，照片这类，原样保留
    sections: list[ResumeSection] = field(default_factory=list)

    def section(self, kind: str) -> ResumeSection | None:
        return next((s for s in self.sections if s.kind == kind), None)


_IMAGE_LINE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
# 标题行尾部单独一个年份（"...Platform | 2026"），不是 _TIME_RANGE 认的区间，
# 但也属于"时间信息"，从标题里挪到 date。
_TRAILING_YEAR = re.compile(r"\s*[|,–-]?\s*((?:19|20)\d{2})\s*$")

_KIND_KEYWORDS = [
    ("summary", ("summary", "objective", "profile", "about me", "个人简介", "自我评价", "求职意向")),
    ("skills", ("skill", "technical", "competenc", "技能", "技术栈")),
    ("education", ("education", "academic background", "教育")),
    ("projects", ("project", "项目")),
    ("experience", ("experience", "employment", "work history", "工作经历", "工作经验", "职业经历", "实习")),
]


def _strip_md(text: str) -> str:
    """去掉一行/一段标题文字外围的 Markdown 装饰：# 前缀、** 包裹、首尾 | 和空白。"""
    t = (text or "").strip()
    t = t.lstrip("#").strip()
    while t.startswith("**") and t.endswith("**") and len(t) > 4:
        t = t[2:-2].strip()
    t = t.replace("**", "").strip()
    return t.strip(" |").strip()


def _classify_kind(header: str) -> str:
    normalized = header.strip().lstrip("#").strip().lower().replace("*", "")
    for kind, kws in _KIND_KEYWORDS:
        if any(kw in normalized for kw in kws):
            return kind
    # 关键词没命中：Region B（有条目结构的）当经历，其它当"其它板块原样保留"
    return "experience" if classify_section(header) == "B" else "other"


def _parse_entry(entry_lines: list[str]) -> ResumeEntry:
    header_text, body_text = _split_entry_header_body(entry_lines)

    date = ""
    m = _TIME_RANGE.search(header_text)
    if m:
        date = m.group(0).strip()
        header_text = (header_text[: m.start()] + header_text[m.end():])
    title = _strip_md(header_text)
    if not date:
        ym = _TRAILING_YEAR.search(title)
        if ym:
            date = ym.group(1)
            title = title[: ym.start()].strip(" |,–-").strip()

    bullets: list[str] = []
    extra: list[str] = []
    for line in _reflow_soft_wraps(body_text).split("\n"):
        s = line.strip()
        if not s:
            continue
        if _BULLET_PREFIX.match(s):
            bullets.append(_BULLET_PREFIX.sub("", s).strip())
        elif bullets:
            # 紧跟在 bullet 后面的非 bullet 行：当成这条 bullet 的续行接上去，别丢
            bullets[-1] = (bullets[-1] + " " + s).strip()
        else:
            extra.append(s)
    return ResumeEntry(title=title, date=date, bullets=bullets, extra_lines=extra)


def parse_resume_document(markdown_text: str) -> ResumeDocument:
    raw_sections = _split_into_sections(markdown_text)
    if not raw_sections:
        return ResumeDocument(name="")

    # 第一个板块 = 姓名 + 联系方式（+ 可能的照片）
    first = raw_sections[0]
    name = _strip_md(first["header"])
    contact_lines: list[str] = []
    image_lines: list[str] = []
    for line in first["lines"]:
        s = line.strip()
        if not s:
            continue
        if _IMAGE_LINE.match(s):
            image_lines.append(s)
        else:
            contact_lines.append(s)

    sections: list[ResumeSection] = []
    for raw in raw_sections[1:]:
        title = _strip_md(raw["header"])
        kind = _classify_kind(raw["header"])
        # 板块正文里夹的图片行也单独拎出来保留
        body, imgs = [], []
        for line in raw["lines"]:
            (imgs if _IMAGE_LINE.match(line.strip()) else body).append(line)
        image_lines.extend(i.strip() for i in imgs)

        if kind in SECTION_KIND_ENTRIES:
            entries = [
                _parse_entry(e)
                for e in _split_experience_entries(body)
                if any(l.strip() for l in e)
            ]
            entries = [e for e in entries if e.title or e.bullets]
            sections.append(ResumeSection(title=title, kind=kind, entries=entries))
        else:
            # 原样保留板块：只把行首多余的 Markdown 标题符号去掉（pymupdf4llm 常把
            # EDUCATION 里"学校名"行也渲染成 ### 标题），加粗/竖线分隔等其它标记保留。
            # 段落之间的空行保留（合并连续空行为一个），让导出时排版不挤在一起。
            kept: list[str] = []
            for l in body:
                s = re.sub(r"^\s*#{1,6}\s*", "", l).rstrip()
                if s:
                    kept.append(s)
                elif kept and kept[-1]:
                    kept.append("")
            while kept and not kept[-1]:
                kept.pop()
            sections.append(ResumeSection(title=title, kind=kind, body_lines=kept))

    return ResumeDocument(
        name=name,
        contact_lines=contact_lines,
        image_lines=image_lines,
        sections=sections,
    )


# ---------------------------------------------------------------------------
# 结构树 -> 干净 Markdown（定制流程的最终产物，也是导出的输入）
# ---------------------------------------------------------------------------
_EXPERIENCE_ORDER = {"experience": 0, "projects": 1}


def render_markdown(doc: ResumeDocument) -> str:
    out: list[str] = []
    if doc.name:
        out.append(f"# {doc.name}")
    for img in doc.image_lines:
        out.append("")
        out.append(img)
    if doc.contact_lines:
        out.append("")
        out.append(" | ".join(doc.contact_lines) if len(doc.contact_lines) > 1 else doc.contact_lines[0])

    # 板块顺序：其它板块保持原顺序，但 PROJECTS 一定排在 EXPERIENCE 之后
    ordered = _order_sections(doc.sections)
    for sec in ordered:
        out.append("")
        out.append(f"## {sec.title}" if sec.title else "##")
        if sec.kind in SECTION_KIND_ENTRIES:
            for entry in sec.entries:
                out.append("")
                out.append(f"### {entry.title}" if entry.title else "###")
                if entry.date:
                    out.append(entry.date)
                for ex in entry.extra_lines:
                    out.append(ex)
                if entry.bullets:
                    out.append("")
                for b in entry.bullets:
                    out.append(f"- {b}")
        else:
            out.append("")
            out.extend(sec.body_lines)

    return "\n".join(out).strip() + "\n"


def _order_sections(sections: list[ResumeSection]) -> list[ResumeSection]:
    """
    PROJECTS 板块必须排在 PROFESSIONAL EXPERIENCE 之后，不能混进工作经历、
    更不能排前面。其它板块保持它们在原简历里的相对顺序。
    """
    exp_idx = next((i for i, s in enumerate(sections) if s.kind == "experience"), None)
    if exp_idx is None:
        return list(sections)
    projects = [s for s in sections if s.kind == "projects"]
    rest = [s for s in sections if s.kind != "projects"]
    # 在 rest 里 experience 的位置后面插入 projects
    exp_pos = next(i for i, s in enumerate(rest) if s.kind == "experience")
    return rest[: exp_pos + 1] + projects + rest[exp_pos + 1:]

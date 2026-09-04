"""
简历 AST 结构化解析。

取代原来 chunker.py 里"按板块标题扁平切块"的方案，改成：
1. 把 Markdown 文本按标题行切成"板块"（section），标题识别兼容真正的
   Markdown 标题（# ## ###）和"整行加粗/全大写的伪标题"（不少简历转出来的
   Markdown，板块标题不一定带 # 号，可能就是一行加粗文字）。
2. 每个板块按关键词词典（keyword_dicts.classify_section）归到两个区域：
   - Region A（个人信息）：Title/Summary/Education/Skills 等，不做向量化，
     只用正则抠结构化字段，打成一个 keyword 集合。
   - Region B（工作经验）：Experience/Projects 等，再拆三层——
     第一层公司/项目、第二层职位/时间、第三层按句子切分的工作内容，
     每个句子 chunk 绑定第一/二层信息作为 tags。

产出：parse_resume_markdown() 返回 {"keywords": set[str], "chunks": [{"text", "tags"}]}
"""

import re

from src.core.keyword_dicts import classify_section, extract_degree_level, extract_tech_stack
from src.core.text_utils import split_sentences

# 真正的 Markdown 标题：# / ## / ### ... + 空格 + 文字
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")

# "伪标题"：整行就是加粗文字（**标题**），中间没有其它内容
_BOLD_ONLY_LINE = re.compile(r"^\*\*([^*]+)\*\*\s*$")

# 学校/院校行（宽松匹配，用来抠 Region A 里的学校名候选）。
# 排除字符集里中英文标点都要包含（，。：；| 等），不然中文简历里
# "本科：北京邮电大学，计算机科学与技术" 这种，只排除了 ASCII 逗号，
# 中文全角逗号/冒号不在排除集里，会一路匹配到下一个 ASCII 标点为止，
# 把学校名后面一整段专业/时间都吞进去。
_SCHOOL_LINE = re.compile(
    r"[^\n,，。：:;|]*?(University|Institute|College|大学|学院)[^\n,，。：:;|]*",
    re.IGNORECASE,
)

# 公司/项目条目的时间范围行（兼容中英文多种写法）：
#   "Jan 2024 - Present" / "2021 - 2023" / "2024.1-2025.1" / "2024年1月-至今" /
#   "2021.09 - 2024.06" / "2020年3月至今" / "March 2021 to now"
#
# 实测坐实过一次真实 bug："2020年3月至今" 这种最常见的中文日期写法匹配不上——
# 原来的日期单元正则 `\d{4}[./年]\s*\d{0,2}` 匹配到 "2020年3" 就停了（\d{0,2}
# 是"月"前面的数字），下一个字符是"月"，不在分隔符字符类 [-–~至] 里，整个匹配
# 失败。结果这一整条经历的时间范围识别不到，触发不了条目边界判断和
# header/body 切分，一条经历的全部内容（好几条真实成就）被当成一整段
# "header" 吞掉，一个 chunk 都没产出——不是格式规整度的问题，是正则本身
# 没覆盖到这个极常见的写法。
# 这里把"日期单元"和"分隔符"分别提出来定义，日期单元显式吃掉"月"字；
# 分隔符除了符号（- – ~ 至 到）以外，也接受英文的 "to" 这个词
# （常见于 "From X to Y" 这种散文体日期写法）。
_DATE_UNIT = r"(?:[A-Za-z]{3,9}\.?\s+\d{4}|\d{4}\s*[./年]\s*\d{1,2}\s*月?|\d{4})"
_TIME_SEPARATOR = r"(?:\s*[-–~]+\s*|\s+to\s+|\s*[至到]+\s*)"
# "至今"/"现在"/present/now 这类"表示仍在进行"的结束词，单独拆成两条分支处理，
# 不能和"两个具体日期之间的分隔符"共用同一套逻辑——实测坐实过一次真实 bug：
# "2020年3月至今" 里"月"和"至今"之间根本没有分隔符（直接拼接），但分隔符的
# `[至到]+` 字符类会贪婪吃掉"至"这一个字（当成分隔符本身），剩下"今"单独一个字
# 匹配不上任何结束词候选，导致整个正则匹配失败。
# 拆成两条分支后："日期 + 可选分隔符 + 至今类结束词"（分隔符允许为空，覆盖
# 直接拼接的情况）；"日期 + 必须有分隔符 + 另一个日期"（两个具体日期之间必须
# 有分隔符，不然没法区分数字从哪断开）。
_ONGOING_TOKEN = r"(?:至今|现在|[Pp]resent|now)"
_TIME_RANGE = re.compile(
    _DATE_UNIT + r"(?:" + _TIME_SEPARATOR + r")?\s*" + _ONGOING_TOKEN
    + r"|" + _DATE_UNIT + _TIME_SEPARATOR + _DATE_UNIT,
    re.IGNORECASE,
)


_BOLD_WRAP = re.compile(r"^\*\*(.+)\*\*$")


def _looks_like_entry_title(text: str) -> bool:
    """
    这行文字"看起来像一条经历/项目条目的标题"（公司 | 职位 | 时间 这种），
    而不是板块标题（"PROFESSIONAL EXPERIENCE"/"技能" 这类几个词的分类名）。
    判断依据：条目标题通常明显更长（塞了公司名+职位+"|"分隔的多段信息），
    或者干脆自己就带着时间范围。
    """
    wrap_match = _BOLD_WRAP.match(text)
    core = wrap_match.group(1) if wrap_match else text
    return len(core) > 30 or bool(_TIME_RANGE.search(core))


def _is_section_header(line: str) -> tuple[bool, str]:
    """
    判断一行是不是板块标题，返回 (是否是标题, 标题文本)。
    优先判断真正的 Markdown 标题；再判断"整行加粗"这种伪标题；
    都不是的话，退回判断"全大写短行"（兼容 pymupdf4llm 没识别出加粗/标题层级，
    纯文本大写标题被原样保留的情况）。
    """
    stripped = line.strip()
    if not stripped:
        return False, ""

    md_match = _MD_HEADING.match(stripped)
    if md_match:
        candidate = md_match.group(2).strip()
        # 实测坐实过一次真实 bug："新增关键词正常、chunk 却是 0"——根因是
        # pymupdf4llm 这类 PDF->Markdown 转换器习惯按字号把"一条经历/项目的
        # 标题"（公司 | 职位 | 时间）也渲染成 Markdown 标题（# ~ ######，
        # 不分层级，字号够大就给标题），不只是给真正的板块标题（## 工作经历）
        # 用。之前这里对"真正的 Markdown 标题"来者不拒，导致每条经历标题都
        # 把所在板块从中截断、另起一个新顶层板块，标题文字本身（"公司|职位"）
        # 又匹配不上 SECTION_TO_REGION 任何关键词，被 classify_section 兜底
        # 分类成 Region A——Region A 只抠关键词、不做句子级切分，板块里的
        # 内容（本该被切成 chunk 的经历要点）就这样全部漏空。
        # 修法：跟"整行加粗"伪标题分支一样，先判断这是不是"看起来像条目标题"，
        # 是的话就不当板块标题处理，原样留在当前板块内容里——
        # _split_experience_entries 本来就会把 Markdown 标题行当条目边界，
        # 留在原地反而能被正确切分。
        if _looks_like_entry_title(candidate):
            return False, ""
        return True, candidate

    bold_match = _BOLD_ONLY_LINE.match(stripped)
    if bold_match:
        candidate = bold_match.group(1).strip()
        # 加粗整行常见的还有"公司名 | 时间"这种（Region B 内部条目），
        # 不是真正的板块标题——用长度 + 是否命中时间范围兜底过滤，
        # 避免把每个加粗的公司名行都误判成顶层板块。
        if not _looks_like_entry_title(candidate):
            return True, candidate
        return False, ""

    # 纯文本全大写标题兜底（没有 Markdown 标记时）
    if stripped.isupper() and 3 < len(stripped) <= 30 and "|" not in stripped:
        return True, stripped

    return False, ""


def _split_into_sections(markdown_text: str) -> list[dict]:
    """把整份 Markdown 按板块标题切开，返回 [{"header": "...", "lines": [...]}]"""
    sections = []
    current_header = "HEADER"  # 简历最开头（姓名/联系方式），归到默认 HEADER 板块
    current_lines: list[str] = []

    for line in markdown_text.split("\n"):
        is_header, header_text = _is_section_header(line)
        if is_header:
            sections.append({"header": current_header, "lines": current_lines})
            current_header = header_text
            current_lines = []
        else:
            current_lines.append(line)
    sections.append({"header": current_header, "lines": current_lines})

    return [s for s in sections if any(l.strip() for l in s["lines"])]


def _parse_region_a(sections: list[dict]) -> set[str]:
    """
    Region A：结构化字段抠取，不做向量化。
    目前抠三类：学历等级词、技术栈关键词、学校名候选行——都作为字符串
    塞进同一个 keyword 集合里（不强行拆成 dataclass 字段，下游按需要
    自己筛选前缀，比如学校名一般是整句，学历/技术栈是短词）。
    """
    keywords: set[str] = set()
    full_text = "\n".join("\n".join(s["lines"]) for s in sections)

    degree_level = extract_degree_level(full_text)
    if degree_level is not None:
        # 反查等级对应的关键词也没有意义（一个等级对应多个词），
        # 这里直接存等级本身，用 "degree:N" 前缀标记，方便后续打分时识别。
        keywords.add(f"degree:{degree_level}")

    keywords |= extract_tech_stack(full_text)

    for match in _SCHOOL_LINE.finditer(full_text):
        school = match.group(0).strip(" -|*#")
        if school:
            keywords.add(f"school:{school}")

    return keywords


def _only_title_line_so_far(entry_lines: list[str]) -> bool:
    """
    entry_lines（当前正在累积的条目）目前是不是"只有一行经历标题（加粗整行
    或 Markdown 标题），没有别的实义内容"（空行不算）——用来判断紧接着的
    一行孤零零的时间范围，是不是这个标题的时间信息延续到了下一行写，
    而不是一段新经历的开始。
    """
    nonblank = [l.strip() for l in entry_lines if l.strip()]
    if len(nonblank) != 1:
        return False
    only_line = nonblank[0]
    return bool(_BOLD_ONLY_LINE.match(only_line)) or bool(_MD_HEADING.match(only_line))


def _split_experience_entries(lines: list[str]) -> list[list[str]]:
    """
    把 Region B 一个板块内部按"条目边界"切开，每个条目对应一段工作/项目经历。
    边界判定：加粗整行、或包含时间范围的行、或 Markdown 标题——命中任一个就
    开启新条目。如果整个板块一个边界都没找到（比如就一段流水账文字），就整段
    当一个条目。

    实测坐实过一次真实 bug：有的简历把"公司 | 职位"标题和"时间范围"分开另起
    两行写（不是同一行），比如：
        #### **Huawei Technologies | ... | AI Engineer |**
        Feb 2025 – July 2025
        - 具体要点...
    标题行先命中 Markdown 标题边界开一条新条目，紧接着"时间范围"单独一行又
    命中时间范围边界，被误判成"又一段新经历"，把标题行拆成一条空条目（只有
    标题、没有正文），真正带要点的内容那条反而丢了公司名/职位标签——下面
    "紧跟在标题行后面的孤零零时间范围行，不当新条目边界"这条规则就是修这个的。
    """
    entries: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        stripped = line.strip()

        is_pure_time_line = bool(stripped) and bool(_TIME_RANGE.fullmatch(stripped))
        if is_pure_time_line and _only_title_line_so_far(current):
            # 紧跟在经历标题行后面、整行就是一个时间范围（不是夹在一句话里的
            # 日期提及）——接着上一条经历写，合并进同一条，不当新条目边界。
            current.append(line)
            continue

        is_boundary = bool(_BOLD_ONLY_LINE.match(stripped)) or bool(_TIME_RANGE.search(stripped)) or bool(
            _MD_HEADING.match(stripped)
        )
        if is_boundary and current and any(l.strip() for l in current):
            entries.append(current)
            current = [line]
        else:
            current.append(line)

    if current:
        entries.append(current)

    entries = [e for e in entries if any(l.strip() for l in e)]
    return entries if entries else [lines]


def _split_entry_header_body(entry_lines: list[str]) -> tuple[str, str]:
    """
    把一个条目的原始行切成 (header_text, body_text)。

    不能简单地"前 N 行当 header、其余当 body"——实测坐实过：pymupdf4llm
    从 PDF 抽取列表时，如果列表项之间视觉间距不够，会把 "公司 | 职位 | 时间"
    和后面好几条 bullet 内容压扁成没有换行的同一行（比如
    "**ByteDance** | Backend Engineer | Jan 2024 - Present - Built ... - Improved ... - Led ..."）。
    如果按"前 2 行是 header"切，会把所有 bullet 内容一起当成 header 吞掉，
    之后只取 company/role 两段，bullet 内容全部丢失（chunk 数量变 0）。

    改成：在第一行里定位时间范围正则命中的位置，时间范围结束位置之前的
    算 header，之后的（哪怕和时间范围在同一物理行里）算 body 的开头，
    再拼上剩余行。找不到时间范围就退回"第一条非空行是 header，其余是 body"。
    """
    if not entry_lines:
        return "", ""

    # 跳过条目开头的空行，找到真正的第一条非空行——不能直接拿 entry_lines[0]，
    # 之前踩过一次坑：条目前面常有一个空行（板块标题和第一个条目之间的空行），
    # entry_lines[0] 是空字符串时，时间范围正则当然搜不到，会整个退回旧的
    # "第一条非空行整行当 header"逻辑，同样的"header 和 bullet 粘在一行"问题
    # 又出现一次，只是错位了一行。
    idx = 0
    while idx < len(entry_lines) and not entry_lines[idx].strip():
        idx += 1
    if idx >= len(entry_lines):
        return "", ""

    first_line = entry_lines[idx]
    rest_lines = entry_lines[idx + 1:]

    time_match = _TIME_RANGE.search(first_line)
    if time_match:
        header_text = first_line[: time_match.end()]
        body_first_line = first_line[time_match.end():]
        body_text = "\n".join([body_first_line] + rest_lines)
        return header_text, body_text

    # 标题行本身没有时间范围——可能是"标题行和时间范围分开另起一行写"这种格式
    # （_split_experience_entries 已经把这一行时间范围合并进了同一条目，不再是
    # 单独一条空条目）。往下找 rest_lines 里第一条非空行，如果它整行就是一个
    # 孤零零的时间范围，拼进 header_text 里一起交给 _extract_entry_header 抠
    # time 标签，不要把这行日期本身也当成正文内容漏进 body/chunk 里。
    j = 0
    while j < len(rest_lines) and not rest_lines[j].strip():
        j += 1
    if j < len(rest_lines):
        candidate = rest_lines[j].strip()
        if _TIME_RANGE.fullmatch(candidate):
            header_text = first_line + " " + candidate
            body_text = "\n".join(rest_lines[j + 1:])
            return header_text, body_text

    return first_line, "\n".join(rest_lines)


def _extract_entry_header(header_text: str) -> dict:
    """
    从一个条目的 header 文本里抠 company/role/time。
    这是启发式规则，简历排版千差万别，抠不准的时候宁可留空，不瞎猜：
    - 时间范围：正则直接抠，比较可靠
    - 公司/项目/职位：按 "|" 或项目符号切成若干段，去掉时间段之后，
      剩下第一段当 company，第二段（如果有）当 role。命中率取决于
      简历本身是不是这种 "A | B | 时间" 排版，不是这种格式就只能拿到
      一个笼统的 entry_title，company/role 留空。
    """
    tags: dict = {"company": None, "role": None, "time": None}
    header_text = header_text.replace("**", "").replace("#", "").strip()

    time_match = _TIME_RANGE.search(header_text)
    if time_match:
        tags["time"] = time_match.group(0).strip()
        header_text = header_text[: time_match.start()] + header_text[time_match.end():]

    parts = [p.strip(" -|•*") for p in re.split(r"[|•]", header_text) if p.strip(" -|•*")]
    if len(parts) >= 2:
        tags["company"] = parts[0]
        tags["role"] = parts[1]
    elif len(parts) == 1:
        tags["company"] = parts[0]

    return tags


def _parse_region_b(sections: list[dict]) -> list[dict]:
    """Region B：拆条目（company/project）→ 抠 header（role/time）→ 内容按句子切分。"""
    chunks: list[dict] = []

    for section in sections:
        entries = _split_experience_entries(section["lines"])
        for entry_lines in entries:
            header_text, body_text = _split_entry_header_body(entry_lines)

            entry_tags = _extract_entry_header(header_text)
            entry_tags["project"] = section["header"].replace("**", "").replace("#", "").strip()

            for sentence in split_sentences(body_text):
                chunks.append({"text": sentence, "tags": dict(entry_tags)})

    return chunks


def parse_resume_markdown(markdown_text: str) -> dict:
    """
    解析一份简历的 Markdown 全文，返回：
    {"keywords": set[str], "chunks": [{"text": str, "tags": dict}, ...]}
    """
    sections = _split_into_sections(markdown_text)

    region_a_sections = [s for s in sections if classify_section(s["header"]) == "A"]
    region_b_sections = [s for s in sections if classify_section(s["header"]) == "B"]

    keywords = _parse_region_a(region_a_sections)
    chunks = _parse_region_b(region_b_sections)

    return {"keywords": keywords, "chunks": chunks}


def parse_free_text_note(text: str) -> list[dict]:
    """
    用户在对话框里补充的自由文本，没有公司/项目/时间结构，直接按句子切分，
    每个句子生成一个 chunk，tags 只打来源标记，不猜测公司/项目名。
    """
    return [{"text": s, "tags": {"source": "user_supplement"}} for s in split_sentences(text)]


if __name__ == "__main__":
    # 简单自测：python -m src.core.resume_ast
    sample = """# Fangyu Lin

## SUMMARY
AI Engineer with 3 years of experience in backend systems.

## EDUCATION
**Ontario Tech University** | Master of Computer Science | 2023 - 2025
本科：北京邮电大学，计算机科学与技术，2019-2023

## SKILLS
Python, Docker, Kubernetes, AWS, PostgreSQL

## EXPERIENCE
**ByteDance** | Backend Engineer | Jan 2024 - Present
- Built a scalable data pipeline using Apache Spark
- Improved query latency by 40%
- 提升了系统的整体稳定性

**Huawei** | Data Analyst Intern | Jun 2022 - Aug 2022
- Analyzed large-scale user behavior data
- 构建了用户画像模型
"""
    result = parse_resume_markdown(sample)
    print("=" * 60)
    print("关键词集合:")
    for kw in sorted(result["keywords"]):
        print(" -", kw)
    print("\n" + "=" * 60)
    print(f"Chunk 数量: {len(result['chunks'])}")
    for c in result["chunks"]:
        print("-" * 60)
        print("text:", c["text"])
        print("tags:", c["tags"])

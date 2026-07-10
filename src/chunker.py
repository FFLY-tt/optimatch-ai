"""
文本切块工具。
简历这类结构化文本，最自然的切法是按大板块切（EDUCATION、PROJECTS 这种标题），
而不是死板地按固定字数切——这样每一块都是语义完整的一段经历，匹配效果更好。

职位描述（JD）通常没有清晰的板块标题，用固定字数切块作为兜底方案。
"""

import re


# 简历里常见的板块标题关键词，用来识别切分点
SECTION_HEADERS = [
    "EDUCATION", "WORK EXPERIENCE", "EXPERIENCE", "ACADEMIC & RESEARCH PROJECTS",
    "PROJECTS", "SKILLS", "TECHNICAL SKILLS", "CERTIFICATIONS", "AWARDS",
    "PUBLICATIONS", "SUMMARY", "OBJECTIVE",
]


def chunk_resume(text: str, max_section_length: int = 800) -> list[dict]:
    """
    按板块标题切分简历文本，对于内容过长的板块（比如 PROJECTS 里塞了多个项目），
    再进一步按"日期标记行"拆分成更小的子块，避免多个不相关的经历被压成一个向量。

    返回格式：[{"section": "EDUCATION", "content": "..."}, ...]
    max_section_length: 单块超过这个字数，就尝试再拆分
    """
    lines = text.split("\n")
    chunks = []
    current_section = "HEADER"  # 简历最开头（姓名、联系方式）默认归到 HEADER
    current_lines = []

    for line in lines:
        stripped = line.strip()
        # 判断这一行是不是板块标题（全大写 + 匹配关键词列表）
        is_header = stripped.upper() in SECTION_HEADERS or (
            stripped.isupper() and len(stripped) > 3 and any(h in stripped for h in SECTION_HEADERS)
        )

        if is_header:
            # 遇到新标题，先把上一段存起来
            if current_lines:
                chunks.append({
                    "section": current_section,
                    "content": "\n".join(current_lines).strip(),
                })
            current_section = stripped
            current_lines = []
        else:
            current_lines.append(line)

    # 存最后一段
    if current_lines:
        chunks.append({
            "section": current_section,
            "content": "\n".join(current_lines).strip(),
        })

    # 过滤掉空内容的块
    chunks = [c for c in chunks if c["content"]]

    # 对过长的板块，尝试按"日期标记行"（形如 "... | Oct 2025" 或 "2021-2022"）再拆分
    final_chunks = []
    for c in chunks:
        if len(c["content"]) > max_section_length:
            sub_chunks = _split_by_entry(c["content"])
            for sc in sub_chunks:
                final_chunks.append({"section": c["section"], "content": sc})
        else:
            final_chunks.append(c)

    return final_chunks


# 用来识别"每个子条目开头"的日期格式（例如 "| Oct 2025" 或 "2021-2022"）
_ENTRY_DATE_PATTERN = re.compile(
    r"(\|\s*[A-Za-z]{3,9}\s+\d{4}\s*$)|(^\d{4}\s*-\s*(Present|\d{4}))", re.MULTILINE
)


def _split_by_entry(text: str) -> list[str]:
    """
    在一个大板块内部，按日期标记行把不同的子条目（比如不同的项目、不同的工作经历）拆开。
    如果拆不出多个子条目（没找到日期标记），就整段原样返回，不强行拆分。
    """
    lines = text.split("\n")
    entries = []
    current_entry = []

    for line in lines:
        is_entry_start = bool(_ENTRY_DATE_PATTERN.search(line.strip()))
        if is_entry_start and current_entry:
            entries.append("\n".join(current_entry).strip())
            current_entry = [line]
        else:
            current_entry.append(line)

    if current_entry:
        entries.append("\n".join(current_entry).strip())

    entries = [e for e in entries if e]
    return entries if len(entries) > 1 else [text]


def chunk_by_length(text: str, chunk_size: int = 500, overlap: int = 50) -> list[dict]:
    """
    按固定字数切块，用于没有清晰板块结构的文本（比如职位描述 JD）。
    overlap: 相邻两块之间重叠的字数，避免切断一句关键信息导致语义丢失。
    """
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk_text = text[start:end].strip()
        if chunk_text:
            chunks.append({"section": "TEXT", "content": chunk_text})
        start += chunk_size - overlap
    return chunks


if __name__ == "__main__":
    # 测试运行：python -m src.chunker
    import os
    from src.resume_parser import parse_resume

    pdf_path = os.path.join(os.path.dirname(__file__), "..", "data", "Fangyu_Lin_CV.pdf")
    text = parse_resume(pdf_path)

    chunks = chunk_resume(text)
    print(f"共切出 {len(chunks)} 块\n")
    for i, c in enumerate(chunks):
        print(f"--- 第 {i+1} 块 | 板块: {c['section']} | 字数: {len(c['content'])} ---")
        print(c["content"][:150])
        print()
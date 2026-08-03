"""
简历 Word 导出模块。
把用户复审确认后的最终文字，排版成一份标准格式的 .docx 简历。

只做基础的专业排版（标题、加粗小标题、项目符号），不做花哨设计——
简历这类文档，招聘方看重的是清晰易读，不是视觉设计。
"""

import os
import re
from docx import Document
from docx.shared import Pt, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH

EXPORT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "exports")


def export_resume_to_docx(content: str, candidate_name: str) -> str:
    """
    把生成的简历文字排版成 .docx 文件。
    content: 定制简历的最终文字（可能包含 Markdown 风格的 ** 加粗 和 - 项目符号）
    candidate_name: 候选人姓名，用于文档标题和文件命名
    返回：生成的文件路径
    """
    os.makedirs(EXPORT_DIR, exist_ok=True)

    doc = Document()

    # 设置基础字体
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)

    # 标题：候选人姓名
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(candidate_name)
    run.bold = True
    run.font.size = Pt(20)

    doc.add_paragraph()  # 空行

    # 逐行处理内容，识别 Markdown 风格的加粗和项目符号
    lines = content.split("\n")
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # 项目符号行（以 - 或 * 开头）
        is_bullet = stripped.startswith("-") or stripped.startswith("*")
        if is_bullet:
            stripped = stripped.lstrip("-*").strip()
            p = doc.add_paragraph(style="List Bullet")
        else:
            p = doc.add_paragraph()

        # 处理 **加粗** 标记
        parts = re.split(r"(\*\*.*?\*\*)", stripped)
        for part in parts:
            if part.startswith("**") and part.endswith("**"):
                run = p.add_run(part.strip("*"))
                run.bold = True
            else:
                p.add_run(part)

    # 文件名安全化处理（去掉空格和特殊字符）
    safe_name = re.sub(r"[^\w\-]", "_", candidate_name)
    filename = f"{safe_name}_Resume_Tailored.docx"
    filepath = os.path.join(EXPORT_DIR, filename)
    doc.save(filepath)

    return filepath


if __name__ == "__main__":
    # 测试运行：python -m src.word_export
    sample_content = """
    **AI Agent-Driven Automated Testing System** | Oct 2025
    - Built an autonomous AI agent system using **LangGraph** and **LangChain**
    - Integrated Slither and Foundry for vulnerability detection
    """
    path = export_resume_to_docx(sample_content, "Fangyu Lin")
    print(f"已生成: {path}")
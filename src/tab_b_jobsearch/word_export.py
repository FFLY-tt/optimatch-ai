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
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem

EXPORT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "exports")


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


def export_resume_to_md(content: str, candidate_name: str) -> str:
    """
    把定制简历内容直接存成 .md 文件——LLM 生成的内容本来就是 Markdown
    格式（见 resume_generator.py 的 TAILOR_SYSTEM_PROMPT 第 9 条要求），
    这里不需要做任何格式转换，原样落盘就行。
    """
    os.makedirs(EXPORT_DIR, exist_ok=True)
    safe_name = re.sub(r"[^\w\-]", "_", candidate_name)
    filename = f"{safe_name}_Resume_Tailored.md"
    filepath = os.path.join(EXPORT_DIR, filename)

    title = f"# {candidate_name}\n\n"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(title + content.strip() + "\n")

    return filepath


# 最初 PDF 导出用的是 markdown -> HTML -> xhtml2pdf 这条路，实测坐实过
# 一个致命问题：xhtml2pdf 自己的 HTML 渲染层无法正确处理中文——即使按
# 官方文档的方式用 reportlab.pdfbase.ttfonts.TTFont 显式注册好中文字体
# （直接用 reportlab 画中文完全正常，证明字体本身没问题），xhtml2pdf
# 输出的 PDF 里中文依然全部变成黑色方块。试过 @font-face、
# registerFontFamily、!important、UTF-8 字节+meta charset 声明，都没用，
# 判断是这个库这个版本本身的缺陷，不是配置问题。
# 所以改成绕开 xhtml2pdf 的 HTML 层，直接用 reportlab 的 Platypus
# （SimpleDocTemplate + Paragraph/ListFlowable）自己排版——reportlab
# 本身对中文没有问题，只要字体注册对了。
_CJK_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]

_cjk_font_registered = False


def _register_cjk_font() -> str:
    """
    找一个系统里真实存在的 CJK 字体注册给 reportlab，返回注册后的字体名。
    一个都找不到就退回 Helvetica（这种情况下中文内容依然会乱码，是环境
    限制——生产部署时应该确保至少有一个可用的 CJK 字体）。
    只注册一次，重复调用直接复用。
    """
    global _cjk_font_registered
    if _cjk_font_registered:
        return "CJKFont"
    for path in _CJK_FONT_CANDIDATES:
        if os.path.exists(path):
            pdfmetrics.registerFont(TTFont("CJKFont", path))
            _cjk_font_registered = True
            return "CJKFont"
    return "Helvetica"


# 简单的行级解析：标题(#)/加粗开头的条目小标题/项目符号(- 或 * + 空格)/普通段落。
# 简历内容本来就结构简单（LLM 输出遵循 TAILOR_SYSTEM_PROMPT 的格式约定），
# 不需要上完整的 Markdown 解析器。
_BOLD_INLINE = re.compile(r"\*\*(.+?)\*\*")
_CJK_CHAR = re.compile(r"[一-鿿]")


def _markdown_inline_to_reportlab(text: str) -> str:
    """把行内 **加粗** 转成 reportlab Paragraph 认的 <b>...</b> mini-HTML 标记。"""
    return _BOLD_INLINE.sub(r"<b>\1</b>", text)


def _pick_font(text: str, cjk_font: str) -> str:
    """
    整行只要出现一个中文字符就用 CJK 字体渲染整行——中文字体渲染
    ASCII 字符（数字、竖线分隔符）观感上没问题，比按字符切换字体简单
    可靠。纯英文行用 Helvetica（reportlab 内置字体），观感比强制套用
    中文字体（字间距会变得很宽，实测坐实过）更专业、更符合英文简历的
    排版习惯。
    """
    return cjk_font if _CJK_CHAR.search(text) else "Helvetica"


def export_resume_to_pdf(content: str, candidate_name: str) -> str:
    """
    把定制简历内容导出成 .pdf——直接用 reportlab Platypus 排版
    （不再经过 xhtml2pdf 的 HTML 层，原因见上面的注释）。
    """
    os.makedirs(EXPORT_DIR, exist_ok=True)
    safe_name = re.sub(r"[^\w\-]", "_", candidate_name)
    filename = f"{safe_name}_Resume_Tailored.pdf"
    filepath = os.path.join(EXPORT_DIR, filename)

    cjk_font = _register_cjk_font()

    def _style(name: str, text: str, **kwargs) -> ParagraphStyle:
        return ParagraphStyle(name, fontName=_pick_font(text, cjk_font), **kwargs)

    story = [
        Paragraph(candidate_name, _style("TitleStyle", candidate_name, fontSize=20, leading=24, alignment=TA_CENTER)),
        Spacer(1, 0.2 * inch),
    ]

    bullet_buffer: list[str] = []

    def _flush_bullets():
        if bullet_buffer:
            items = [
                ListItem(Paragraph(
                    _markdown_inline_to_reportlab(b),
                    _style("BulletStyle", b, fontSize=11, leading=15),
                ))
                for b in bullet_buffer
            ]
            story.append(ListFlowable(items, bulletType="bullet", start="circle", leftIndent=18))
            bullet_buffer.clear()

    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        # 项目符号：真正的列表标记是 "- "/"* " (符号后面跟空格)，不能只看
        # "以 - 或 * 开头"——"**加粗**" 开头的字符也是 "*"，之前这里把
        # "**Company** | Role | Time" 这种"加粗前缀+尾随文本"的条目标题行
        # 误判成了列表项，lstrip("-*") 只从行首剥符号，行内后半段的 "**"
        # 配对不上，结尾多出一个裸露的 "**" 字面量（实测坐实过）。
        is_bullet = stripped.startswith("- ") or (stripped.startswith("* ") and not stripped.startswith("**"))

        if stripped.startswith("#"):
            _flush_bullets()
            heading_text = stripped.lstrip("#").strip()
            story.append(Paragraph(
                _markdown_inline_to_reportlab(heading_text),
                _style("HeadingStyle", heading_text, fontSize=12, leading=16, spaceBefore=10, spaceAfter=4),
            ))
        elif is_bullet:
            bullet_buffer.append(stripped[2:].strip())
        elif stripped.startswith("**"):
            # 加粗开头的行（不管加粗有没有覆盖整行）当条目小标题处理，
            # 比如 "**Company | Role | Time**" 或 "**Company** | Role | Time"
            _flush_bullets()
            story.append(Paragraph(
                _markdown_inline_to_reportlab(stripped),
                _style("HeadingStyle", stripped, fontSize=12, leading=16, spaceBefore=10, spaceAfter=4),
            ))
        else:
            _flush_bullets()
            story.append(Paragraph(
                _markdown_inline_to_reportlab(stripped),
                _style("BodyStyle", stripped, fontSize=11, leading=15),
            ))

    _flush_bullets()

    doc = SimpleDocTemplate(
        filepath, pagesize=LETTER,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
    )
    doc.build(story)

    return filepath


if __name__ == "__main__":
    # 测试运行：python -m src.tab_b_jobsearch.word_export
    sample_content = """
    **AI Agent-Driven Automated Testing System** | Oct 2025
    - Built an autonomous AI agent system using **LangGraph** and **LangChain**
    - Integrated Slither and Foundry for vulnerability detection
    """
    path = export_resume_to_docx(sample_content, "Fangyu Lin")
    print(f"docx 已生成: {path}")

    md_path = export_resume_to_md(sample_content, "Fangyu Lin")
    print(f"md 已生成: {md_path}")

    pdf_path = export_resume_to_pdf(sample_content, "Fangyu Lin")
    print(f"pdf 已生成: {pdf_path}")

    zh_content = """
    **阿里巴巴 | 高级后端工程师 | 2020年3月至今**
    - 主导了系统重构项目，将平均响应时间降低了一半
    - 带领团队完成了微服务化改造
    """
    zh_pdf_path = export_resume_to_pdf(zh_content, "李明测试")
    print(f"中文 pdf 已生成: {zh_pdf_path}")
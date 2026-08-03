"""
简历解析器。
用 pdfplumber 提取 PDF 文本，做基础清洗。

注意：pdfplumber 默认只提取文字，不会读取图片内容（头像、图标等），
这正是我们想要的效果，不需要额外处理照片。

如果简历是双栏/表格排版，提取出来的文字顺序可能和视觉顺序不完全一致，
这是已知局限，不是本周要解决的重点——先跑通看效果，效果不好再针对性优化。
"""

import pdfplumber
import re


def parse_resume(pdf_path: str) -> str:
    """
    解析 PDF 简历，返回清洗后的纯文本。
    """
    all_text = []

    with pdfplumber.open(pdf_path) as pdf:
        print(f"  [调试] PDF 共 {len(pdf.pages)} 页")
        for i, page in enumerate(pdf.pages):
            text = page.extract_text()
            if text:
                all_text.append(text)
            else:
                print(f"  [调试] 第 {i+1} 页没提取到文字（可能是纯图片页）")

    raw_text = "\n".join(all_text)
    clean_text = _clean_text(raw_text)
    return clean_text


def _clean_text(text: str) -> str:
    """
    基础清洗：
    - 把多个连续空行压缩成一个
    - 去掉每行首尾多余空格
    - 去掉一些常见的乱码/特殊符号
    """
    lines = text.split("\n")
    cleaned_lines = [line.strip() for line in lines]
    # 去掉空行堆积（连续多个空行只保留一个）
    result_lines = []
    prev_empty = False
    for line in cleaned_lines:
        if line == "":
            if not prev_empty:
                result_lines.append(line)
            prev_empty = True
        else:
            result_lines.append(line)
            prev_empty = False

    cleaned = "\n".join(result_lines)
    # 去掉一些 PDF 提取常见的乱码字符
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", cleaned)
    return cleaned.strip()


if __name__ == "__main__":
    # 测试运行：python -m src.resume_parser
    import os

    pdf_path = os.path.join(os.path.dirname(__file__), "..", "data", "Fangyu_Lin_CV.pdf")

    if not os.path.exists(pdf_path):
        print(f"没找到文件: {pdf_path}")
        print("确认文件名和路径是否正确")
    else:
        print(f"正在解析: {pdf_path}\n")
        result = parse_resume(pdf_path)
        print("=" * 60)
        print("解析结果（前 1000 字）：\n")
        print(result[:1000])
        print("\n" + "=" * 60)
        print(f"总字数: {len(result)}")
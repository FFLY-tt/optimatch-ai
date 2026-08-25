"""
简历解析器。
用 pymupdf4llm 把 PDF 转成 Markdown 文本（取代原来的 pdfplumber 纯文本方案）——
Markdown 输出保留了标题层级/加粗/列表这些结构信息，AST 解析（resume_ast.py）
需要这些结构信息才能拆出 company/role/time 三层，纯文本方案做不到这一点。

.md 文件不需要转换，读进来的文本本身就是要解析的 Markdown。
"""

import pymupdf4llm


def parse_resume_to_markdown(file_path: str, file_ext: str) -> str:
    """
    把上传的简历文件解析成 Markdown 文本。
    file_ext: ".pdf" 或 ".md"（调用方保证只传这两种，格式范围已在上传接口收紧）
    """
    if file_ext == ".md":
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read().strip()

    if file_ext == ".pdf":
        md_text = pymupdf4llm.to_markdown(file_path)
        return md_text.strip()

    raise ValueError(f"不支持的文件格式: {file_ext}（只支持 .pdf 和 .md）")


if __name__ == "__main__":
    # 测试运行：python -m src.tab_b_jobsearch.resume_parser
    import os

    pdf_path = os.path.join(os.path.dirname(__file__), "..", "..", "data", "Fangyu_Lin_CV.pdf")

    if not os.path.exists(pdf_path):
        print(f"没找到文件: {pdf_path}")
        print("确认文件名和路径是否正确")
    else:
        print(f"正在解析: {pdf_path}\n")
        result = parse_resume_to_markdown(pdf_path, ".pdf")
        print("=" * 60)
        print("解析结果（前 1500 字）：\n")
        print(result[:1500])
        print("\n" + "=" * 60)
        print(f"总字数: {len(result)}")

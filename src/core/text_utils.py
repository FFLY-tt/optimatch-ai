"""
中英文句子切分工具。
简历 Experience 板块的第三层内容、用户自由文本补充、JD 正文，
都需要按句子切分成 chunk，这里统一实现一份，三处共用。
"""

import re

# 中文句末标点：。！？；英文句末标点：. ! ?
# 用 (?<=...) 零宽断言在标点后面切，标点本身保留在前一句结尾。
# 排除掉常见的缩写/小数点误切场景比较复杂，这里先用简单版本——
# 简历/JD 这类文本本来就没有太多缩写句号（"Inc." "e.g." 这种），
# 之后如果发现误切明显，再针对性加例外规则。
_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？.!?])\s+(?=[^\s])|(?<=[。！？])")


_BULLET_PREFIX = re.compile(r"^[-*•‣▪]\s*")

# 行内"伪项目符号"连接处：pymupdf4llm 从 PDF 里抽取列表时，实测发现有些
# PDF（列表项之间没有足够的排版垂直间距/独立文本块）会把多个 "- " 开头的
# 列表项挤成没有换行的一整行，形如：
#   "- Built a data pipeline - Improved latency by 40% - Led migration"
# 这种情况光靠换行切分和句末标点切分都不够（这些列表项本身经常没有句末
# 标点）。这里额外识别"空格+连字符+空格，后面紧跟大写字母或中文字符"
# 作为一个软边界，把它当成新列表项的开头切开。
# 用"连字符前一个字符不是数字"这个否定断言，是为了不要切碎日期范围
# （"Jan 2024 - Present"），日期范围左边通常是数字。这是启发式规则，
# 不保证 100% 准确（比如英文里用连字符做插入语的句子会被误切），
# 但简历列表条目这种场景，践本命中率明显更高。
_INLINE_BULLET_JOIN = re.compile(r"(?<!\d)\s-\s(?=[A-Z一-鿿])")


def split_sentences(text: str, min_length: int = 4) -> list[str]:
    """
    把一段文本切成句子列表。

    简历的 Experience/Projects 条目大多是"每行一个项目符号，行内不带句末标点"
    的风格（比如 "- Built a scalable data pipeline"），如果只按标点切句子，
    这种没有标点的整段列表会被当成一整句。所以这里分三层切：
    1. 先按换行切成"行"，每个 Markdown 项目符号（- * • 等）算一个独立单位，
       不管这一行结不结尾都先断开；
    2. 行内如果被压缩成"多个列表项挤在一行、靠 ' - ' 分隔"（见
       _INLINE_BULLET_JOIN 的注释），再按这个软边界切开；
    3. 每个子项如果本身包含多句话（有句末标点），再按标点进一步细分。

    min_length: 过滤掉切出来太短的碎片（比如单独一个符号、单个词），
    这类碎片做句子级语义匹配没有意义。
    """
    if not text or not text.strip():
        return []

    sentences = []
    for line in text.split("\n"):
        line = _BULLET_PREFIX.sub("", line.strip())
        if not line:
            continue
        for item in _INLINE_BULLET_JOIN.split(line):
            item = _BULLET_PREFIX.sub("", item.strip())
            if not item:
                continue
            for s in _SENTENCE_BOUNDARY.split(item):
                s = s.strip()
                if len(s) >= min_length:
                    sentences.append(s)
    return sentences


if __name__ == "__main__":
    # 测试运行：python -m src.core.text_utils
    samples = [
        "构建了大数据分析平台架构。实现了查询效率提升30%！支持了千万级数据实时处理？",
        "Built a scalable data pipeline using Apache Spark. Improved query latency by 40%! "
        "Led a team of 3 engineers to deliver the project on time.",
        "- Designed the system architecture\n- Reduced costs by 20%\n- 提升了用户留存率",
    ]
    for s in samples:
        print("=" * 60)
        print("原文:", s)
        print("切分结果:", split_sentences(s))

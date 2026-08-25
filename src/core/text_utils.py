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


# ---------- 语言检测（用于"用户画像必须是单一语言"这个产品要求） ----------
_CJK_CHAR_PATTERN = re.compile(r"[一-鿿]")
_LATIN_WORD_PATTERN = re.compile(r"[A-Za-z]+")

# 中文用"字符数"计，拉丁文用"单词数"计——不能两边都按字母数计。
# 第一版用的是"拉丁字母数"，实测坐实过真实问题：一句中文夹了两三个技术
# 名词（"Python"、"PyTorch"、"Kubernetes"这种），"Kubernetes"一个词
# 就占 10 个字母，几个技术名词一凑就能把拉丁字母总数顶到接近中文字数，
# 明明整句话是中文叙述，占比一算却掉进了"unknown"档。
# 一个技术专有名词不管多长，在语义上就是"一个词"，权重不该跟它的字母数
# 挂钩——按单词数（用 [A-Za-z]+ 的匹配次数）计拉丁文分量，跟中文按
# "字"计数的粒度对齐，一个技术名词只算一个单位，就不会被词长放大。
_LATIN_WORD_UNIT = 1  # 拉丁文每个词记为这么多个"单位"（对齐中文按字计数的粒度）

# 文本里有效"单位"（中文字数 + 拉丁单词数）总数低于这个数，判定依据太薄弱
# （比如补充文本只写了几个字），返回 "unknown"，不参与语言一致性判断。
_MIN_MEANINGFUL_UNITS = 6

# 主导语言的判定用"某一方占比是否明显过半"，不是简单看哪边单位多一点。
# 阈值 0.55/0.45 是刻意选得不那么严格：一段整体是中文的简历里夹杂正常
# 技术词汇，只要中文字数依然明显多于拉丁单词数就该判 "zh"。
_DOMINANT_RATIO_THRESHOLD = 0.55


def detect_language(text: str) -> str:
    """
    判断一段文本的主导语言，返回 "zh" / "en" / "unknown"。

    判据是"中文字符数" vs "拉丁文单词数"（不是拉丁字母数）的占比——
    技术名词、公司名、产品名（Python/AWS/PyTorch/Kubernetes 这类）天然是
    拉丁字母组成，但语义上只是"一个词"，按单词数而不是字母数计入拉丁文
    分量，才不会因为专有名词本身较长就被放大权重，被误判成"语言混用"。
    只要中文（或英文）整体占比明显过半就判定为对应语言；两者比例接近
    （真正的大段中英混排）判 "unknown"。

    有效单位（中文字数+拉丁单词数）总数太少（比如短文本、纯符号、纯数字）
    也返回 "unknown"，不参与语言一致性判断，避免误伤。
    """
    cjk_count = len(_CJK_CHAR_PATTERN.findall(text))
    latin_word_count = len(_LATIN_WORD_PATTERN.findall(text)) * _LATIN_WORD_UNIT
    total = cjk_count + latin_word_count

    if total < _MIN_MEANINGFUL_UNITS:
        return "unknown"

    zh_ratio = cjk_count / total
    if zh_ratio >= _DOMINANT_RATIO_THRESHOLD:
        return "zh"
    if zh_ratio <= (1 - _DOMINANT_RATIO_THRESHOLD):
        return "en"
    return "unknown"


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

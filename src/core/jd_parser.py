"""
JD（职位描述）侧的对称解析——和简历侧的 keyword/chunk 概念对齐，
这样两边才能用同一套相似度/打分逻辑做匹配。

和简历侧不一样的地方：JD 没有"公司/项目/职位/时间"这种三层结构要拆，
正文直接按句子切分就够了；结构化字段只抠"学历门槛"和"经验年限门槛"
（复用 keyword_dicts 里那份简历也在用的词典，保证两边判断标准一致）。
"""

from src.core.keyword_dicts import extract_degree_level, extract_tech_stack, extract_years_required
from src.core.text_utils import split_sentences


def parse_job_description(text: str) -> dict:
    """
    解析一条 JD 正文，返回：
    {
        "years_required": int | None,   # 最低经验年限要求，抠不到就是 None
        "degree_required": int | None,  # 学历门槛等级（对齐 keyword_dicts.DEGREE_LEVELS），抠不到就是 None
        "tech_stack": set[str],         # JD 里提到的技术栈关键词
        "sentences": list[str],         # 正文按句子切分的结果，用于句子级相似度匹配
    }
    """
    return {
        "years_required": extract_years_required(text),
        "degree_required": extract_degree_level(text),
        "tech_stack": extract_tech_stack(text),
        "sentences": split_sentences(text),
    }


if __name__ == "__main__":
    # 测试运行：python -m src.core.jd_parser
    sample_jd = (
        "We are looking for a Backend Engineer with 3+ years of experience in Python "
        "and distributed systems. Bachelor's degree in Computer Science or related field required. "
        "Experience with Docker, Kubernetes, and AWS is a strong plus. "
        "You will build and maintain scalable data pipelines serving millions of users."
    )
    result = parse_job_description(sample_jd)
    print("=" * 60)
    print("years_required  :", result["years_required"])
    print("degree_required :", result["degree_required"])
    print("tech_stack      :", sorted(result["tech_stack"]))
    print("sentences       :")
    for s in result["sentences"]:
        print(" -", s)

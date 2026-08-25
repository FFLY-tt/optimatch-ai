"""
简历画像（chunk 集合 + keyword 集合）与 JD 的匹配打分。

============================== 打分公式 ==============================

    final_score(chunk, jd) = semantic_similarity(chunk, jd) * (1 + bonus)

1. semantic_similarity（主要打分维度，句子级）：
   这条简历 chunk 的 embedding，和 JD 所有句子 embedding 的 cosine 相似度，
   取【最大值】（不是平均值）——一条经历只要和 JD 里某一句强相关就该被
   高分选中，不应该被 JD 里一堆和这条经历无关的句子拉低平均分。

2. bonus（结构化加权因子，同一个 JD 下对每条 chunk 都一样，不区分 chunk）：
   这是"加成"不是"过滤"——单条正则抽取本身就不完全可靠（学历/年限抠不到
   很常见），不应该让一次不完美的正则匹配直接把语义很相关的经历排除掉。
   三块独立相加：

   a) degree_bonus（最高 0.2）：
      candidate 整体最高学历 >= JD 学历门槛 → +0.2；
      JD 没提学历门槛，或者简历没抠到学历，都不加也不扣（0）。

   b) tech_overlap_bonus（最高 0.2）：
      0.2 * (简历技术栈关键词 ∩ JD 技术栈关键词的个数 / JD 技术栈关键词总数)
      JD 一个技术栈关键词都没提到时是 0（不是满分，避免钻空子）。

   c) years_bonus（最高 0.15）：
      简历总经验年限（粗略估算，见 estimate_years_of_experience 的说明和
      局限）达标给满分 0.15；没达标按比例给部分分（而不是 0/1 硬切）；
      JD 没提年限要求，或者简历年限估不出来，都是 0。

   bonus 最高 0.55，也就是结构化条件全部达标时，语义相似度会被放大到
   最多 1.55 倍；结构化条件缺失/不达标时退化成纯语义相似度排序。

   这几个系数（0.2 / 0.2 / 0.15）都是硬编码在这个文件里的常量
   （见 DEGREE_BONUS_WEIGHT 等），后面要调权重直接改这几个数就行。
=======================================================================
"""

import re
from datetime import datetime

import numpy as np

from src.core.keyword_dicts import DEGREE_LEVELS
from src.core.vector_store import get_model

DEGREE_BONUS_WEIGHT = 0.2
TECH_OVERLAP_BONUS_WEIGHT = 0.2
YEARS_BONUS_WEIGHT = 0.15

_YEAR_TOKEN = re.compile(r"\d{4}")
_PRESENT_TOKEN = re.compile(r"present|至今|现在", re.IGNORECASE)


def _resume_highest_degree(resume_keywords: set[str]) -> int | None:
    """从 keyword 集合里找 'degree:N' 标记，抠出候选人最高学历等级。"""
    levels = [int(kw.split(":", 1)[1]) for kw in resume_keywords if kw.startswith("degree:")]
    return max(levels) if levels else None


def _resume_tech_stack(resume_keywords: set[str]) -> set[str]:
    """keyword 集合里排除掉 'degree:'/'school:' 前缀标记的，剩下的就是技术栈关键词。"""
    return {kw for kw in resume_keywords if not kw.startswith("degree:") and not kw.startswith("school:")}


def estimate_years_of_experience(chunks: list[dict]) -> float | None:
    """
    粗略估算候选人总从业年限——这是一个近似值，不是精确计算：
    做法是把所有 chunk 的 tags["time"] 字符串里出现的 4 位数年份都收集起来
    （"Present"/"至今"/"现在" 当作"当前年份"处理），取 (最大年份 - 最小年份)
    当总跨度。

    局限（如实说明，不掩盖）：
    - 如果简历里多段经历时间有重叠（比如两份兼职同时进行），会被当成
      不重叠的连续经验高估；
    - 如果简历完全没有可识别的年份格式，返回 None，调用方应该按"无法判断"
      处理（不给 years_bonus 加分也不扣分），不能当成 0 年经验。

    收集到的年份数据点少于 2 个（没法算出一个有意义的跨度）也返回 None。
    """
    years: set[int] = set()
    has_present = False

    for c in chunks:
        time_str = (c.get("tags") or {}).get("time") or ""
        if not time_str:
            continue
        for y in _YEAR_TOKEN.findall(time_str):
            years.add(int(y))
        if _PRESENT_TOKEN.search(time_str):
            has_present = True

    if has_present:
        years.add(datetime.now().year)

    if len(years) < 2:
        return None
    return float(max(years) - min(years))


def _cosine_similarity_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """标准 cosine 相似度矩阵，a: (n, d)，b: (m, d)，返回 (n, m)。"""
    a_norm = a / (np.linalg.norm(a, axis=1, keepdims=True) + 1e-8)
    b_norm = b / (np.linalg.norm(b, axis=1, keepdims=True) + 1e-8)
    return a_norm @ b_norm.T


def score_resume_chunks_against_jd(
    resume_chunks: list[dict],
    resume_keywords: set[str],
    jd_parsed: dict,
    top_k: int = 5,
) -> list[dict]:
    """
    对一份 JD，给用户全部简历 chunk 打分排序，返回 Top K。

    resume_chunks: vector_store.get_all_resume_chunks() 的产出
        [{"id", "text", "tags"}, ...]
    resume_keywords: vector_store.load_resume_keywords() 的产出
    jd_parsed: jd_parser.parse_job_description() 的产出
        {"years_required", "degree_required", "tech_stack", "sentences"}

    返回：[{"id", "text", "tags", "score"}, ...]，按 score 降序，取前 top_k。
    没有简历 chunk、或 JD 没有可用句子时返回空列表。
    """
    if not resume_chunks or not jd_parsed.get("sentences"):
        return []

    model = get_model()
    chunk_texts = [c["text"] for c in resume_chunks]
    chunk_embeddings = model.encode(chunk_texts)
    jd_embeddings = model.encode(jd_parsed["sentences"])

    sim_matrix = _cosine_similarity_matrix(np.array(chunk_embeddings), np.array(jd_embeddings))
    max_similarity_per_chunk = sim_matrix.max(axis=1)  # 每条 chunk 对 JD 全部句子取最大相似度

    # ---- 结构化 bonus（同一个 JD 下，对所有 chunk 都一样）----
    bonus = 0.0

    degree_required = jd_parsed.get("degree_required")
    resume_degree = _resume_highest_degree(resume_keywords)
    if degree_required is not None and resume_degree is not None and resume_degree >= degree_required:
        bonus += DEGREE_BONUS_WEIGHT

    jd_tech = jd_parsed.get("tech_stack") or set()
    if jd_tech:
        resume_tech = _resume_tech_stack(resume_keywords)
        overlap_ratio = len(resume_tech & jd_tech) / len(jd_tech)
        bonus += TECH_OVERLAP_BONUS_WEIGHT * overlap_ratio

    years_required = jd_parsed.get("years_required")
    if years_required is not None and years_required > 0:
        estimated_years = estimate_years_of_experience(resume_chunks)
        if estimated_years is not None:
            years_ratio = min(1.0, estimated_years / years_required)
            bonus += YEARS_BONUS_WEIGHT * years_ratio

    scored = []
    for chunk, sim in zip(resume_chunks, max_similarity_per_chunk):
        scored.append({
            "id": chunk["id"],
            "text": chunk["text"],
            "tags": chunk["tags"],
            "score": float(sim) * (1.0 + bonus),
        })

    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:top_k]


if __name__ == "__main__":
    # 测试运行：python -m src.core.match_scoring
    # 先确保已经跑过一次简历上传流程，resume_chunks collection 里有数据
    from src.core.vector_store import get_all_resume_chunks, load_resume_keywords
    from src.core.jd_parser import parse_job_description

    sample_jd = (
        "We are looking for a Backend Engineer with 2+ years of experience in Python "
        "and distributed data systems. Master's degree in Computer Science preferred. "
        "Experience with Kubernetes and Apache Spark is a strong plus."
    )

    chunks = get_all_resume_chunks()
    keywords = load_resume_keywords()
    jd_parsed = parse_job_description(sample_jd)

    print("=" * 60)
    print(f"简历 chunk 总数: {len(chunks)}, keyword 总数: {len(keywords)}")
    print(f"JD 解析结果: years_required={jd_parsed['years_required']}, "
          f"degree_required={jd_parsed['degree_required']}, tech_stack={sorted(jd_parsed['tech_stack'])}")

    results = score_resume_chunks_against_jd(chunks, keywords, jd_parsed, top_k=5)
    print(f"\n估算总经验年限: {estimate_years_of_experience(chunks)}")
    print("\nTop 匹配结果:")
    for r in results:
        print("-" * 60)
        print(f"score: {r['score']:.4f}")
        print(f"text : {r['text']}")
        print(f"tags : {r['tags']}")

"""
完整的检索链路：BM25 关键词检索 + 向量语义检索 + Cross-Encoder 精排。

为什么三层都要做（不是偷懒省略精排）：
- BM25：擅长捕捉精确的关键词匹配（比如职位要求"Docker"，简历里恰好写了"Docker"这个词）
- 向量检索：擅长捕捉语义相似（哪怕用词不完全一样，意思相关也能捕捉到）
- Cross-Encoder 精排：前两者各有偏差，精排模型把"职位描述"和"候选简历片段"放在一起
  联合编码，给出更准确的相关性判断，尤其能纠正"技能列表排在项目经历前面"这种
  字面相似但实际说服力较弱的排序问题

流程：
    BM25 检索 Top N  ─┐
                        ├─→ 合并候选（去重）─→ Cross-Encoder 精排 ─→ 最终 Top K
    向量检索 Top N   ─┘
"""

import re
from rank_bm25 import BM25Okapi
from sentence_transformers import CrossEncoder

from src.vector_store import get_chroma_client, get_model

_reranker = None

# bge-reranker-base：专门做"相关性判断"的模型，不是生成 embedding，
# 输入是 (query, document) 一对文本，输出一个相关性分数
RERANKER_MODEL_NAME = "BAAI/bge-reranker-base"


def get_reranker() -> CrossEncoder:
    global _reranker
    if _reranker is None:
        print(f"  [调试] 首次加载 reranker 模型 {RERANKER_MODEL_NAME}（第一次会下载，稍等）...")
        _reranker = CrossEncoder(RERANKER_MODEL_NAME)
    return _reranker


def _tokenize(text: str) -> list[str]:
    """简单分词：转小写、按非字母数字字符切分"""
    return re.findall(r"[a-z0-9]+", text.lower())


def _get_all_documents(collection_name: str) -> list[dict]:
    """从 Chroma 里取出全部文档（用于构建 BM25 索引），不需要向量，只要文本和 id"""
    client = get_chroma_client()
    collection = client.get_collection(collection_name)
    result = collection.get()  # 拿到全部数据
    docs = []
    for doc_id, content, meta in zip(result["ids"], result["documents"], result["metadatas"]):
        docs.append({"id": doc_id, "content": content, "section": meta["section"]})
    return docs


def hybrid_search(
    query_text: str,
    collection_name: str = "resume",
    candidate_pool_size: int = 10,
    final_top_k: int = 3,
) -> list[dict]:
    """
    完整的检索流程：BM25 + 向量检索各取候选 → 合并去重 → Cross-Encoder 精排 → 返回最终 Top K

    candidate_pool_size: 前两层检索各自取多少候选（候选池要比最终结果大，给精排留筛选空间）
    final_top_k: 精排后最终返回多少条
    """
    all_docs = _get_all_documents(collection_name)
    if not all_docs:
        raise RuntimeError(f"collection '{collection_name}' 里没有数据，先跑 build_resume_collection")

    # ---------- 第一层：BM25 关键词检索 ----------
    tokenized_corpus = [_tokenize(d["content"]) for d in all_docs]
    bm25 = BM25Okapi(tokenized_corpus)
    bm25_scores = bm25.get_scores(_tokenize(query_text))
    bm25_ranked_idx = sorted(range(len(all_docs)), key=lambda i: bm25_scores[i], reverse=True)
    bm25_top_ids = {all_docs[i]["id"] for i in bm25_ranked_idx[:candidate_pool_size]}

    # ---------- 第二层：向量语义检索 ----------
    client = get_chroma_client()
    model = get_model()
    collection = client.get_collection(collection_name)
    query_embedding = model.encode([query_text]).tolist()
    vector_results = collection.query(query_embeddings=query_embedding, n_results=candidate_pool_size)
    vector_top_ids = set(vector_results["ids"][0])

    # ---------- 合并候选（去重） ----------
    candidate_ids = bm25_top_ids | vector_top_ids
    candidates = [d for d in all_docs if d["id"] in candidate_ids]

    print(f"  [调试] BM25 候选 {len(bm25_top_ids)} 条，向量候选 {len(vector_top_ids)} 条，"
          f"合并去重后共 {len(candidates)} 条进入精排")

    # ---------- 第三层：Cross-Encoder 精排 ----------
    reranker = get_reranker()
    pairs = [(query_text, c["content"]) for c in candidates]
    rerank_scores = reranker.predict(pairs)

    for c, score in zip(candidates, rerank_scores):
        c["rerank_score"] = float(score)

    candidates.sort(key=lambda c: c["rerank_score"], reverse=True)
    return candidates[:final_top_k]


if __name__ == "__main__":
    # 测试运行：python -m src.retriever
    # 先确保已经跑过 python -m src.vector_store 把简历存进 Chroma 了

    test_job_description = (
        "We are looking for a backend engineer with strong Python experience, "
        "familiarity with LangChain, LangGraph, and building autonomous AI agent workflows. "
        "Experience with Docker and CI/CD pipelines is a plus."
    )

    print("正在做完整的 Hybrid Search + 精排...")
    print(f"职位描述: {test_job_description}\n")

    results = hybrid_search(test_job_description, final_top_k=3)

    print("=" * 60)
    print("精排后的最终结果：\n")
    for i, r in enumerate(results):
        print(f"--- 第 {i+1} 名 | 板块: {r['section']} | 精排得分: {r['rerank_score']:.4f} ---")
        print(r["content"][:200])
        print()
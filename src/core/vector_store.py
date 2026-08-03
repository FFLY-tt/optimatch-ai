"""
向量存储与检索模块。
用 sentence-transformers 在本地把文字转成向量（不用调用外部 API，免费、无网络延迟），
存进 Chroma 向量数据库，然后支持"给一段职位描述，检索最相关的简历片段"。

模型选择：all-MiniLM-L6-v2，是 sentence-transformers 里最常用的轻量模型，
速度快、效果够用，适合 MVP 阶段。
"""

import chromadb
from sentence_transformers import SentenceTransformer

_model = None
_client = None

EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"


def get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        print(f"  [调试] 首次加载 embedding 模型 {EMBEDDING_MODEL_NAME}（第一次会下载，稍等）...")
        _model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    return _model


def get_chroma_client():
    global _client
    if _client is None:
        # 用本地持久化存储，数据存在 data/chroma_db 文件夹里，重启程序不会丢
        _client = chromadb.PersistentClient(path="data/chroma_db")
    return _client


def build_resume_collection(chunks: list[dict], collection_name: str = "resume") -> None:
    """
    把简历切块结果存进 Chroma。
    chunks: chunker.py 输出的格式 [{"section": "...", "content": "..."}]
    """
    client = get_chroma_client()
    model = get_model()

    # 每次重新构建前先删掉旧的，避免重复运行导致数据重复堆积
    try:
        client.delete_collection(collection_name)
    except Exception:
        pass

    collection = client.create_collection(collection_name)

    texts = [c["content"] for c in chunks]
    embeddings = model.encode(texts).tolist()

    collection.add(
        ids=[f"chunk_{i}" for i in range(len(chunks))],
        embeddings=embeddings,
        documents=texts,
        metadatas=[{"section": c["section"]} for c in chunks],
    )
    print(f"  [调试] 已存入 {len(chunks)} 块到 Chroma collection '{collection_name}'")


def search_resume(query_text: str, collection_name: str = "resume", top_k: int = 3) -> list[dict]:
    """
    拿一段查询文本（比如职位描述），检索最相关的简历片段。
    返回：[{"content": "...", "section": "...", "distance": 0.xx}, ...]
    distance 越小代表越相关（这是余弦距离，不是相似度分数，用的时候注意方向）
    """
    client = get_chroma_client()
    model = get_model()
    collection = client.get_collection(collection_name)

    query_embedding = model.encode([query_text]).tolist()
    results = collection.query(query_embeddings=query_embedding, n_results=top_k)

    matches = []
    for doc, meta, dist in zip(
        results["documents"][0], results["metadatas"][0], results["distances"][0]
    ):
        matches.append({"content": doc, "section": meta["section"], "distance": dist})
    return matches


if __name__ == "__main__":
    # 测试运行：python -m src.vector_store
    import os
    from src.tab_b_jobsearch.resume_parser import parse_resume
    from src.core.chunker import chunk_resume

    pdf_path = os.path.join(os.path.dirname(__file__), "..", "data", "Fangyu_Lin_CV.pdf")
    text = parse_resume(pdf_path)
    chunks = chunk_resume(text)

    print(f"\n正在向量化并存入 Chroma（共 {len(chunks)} 块）...")
    build_resume_collection(chunks)

    # 用一条真实职位描述测试检索效果（来自上周 HN 抓到的数据，手动摘一条）
    test_job_description = (
        "We are looking for a backend engineer with strong Python experience, "
        "familiarity with LangChain, LangGraph, and building autonomous AI agent workflows. "
        "Experience with Docker and CI/CD pipelines is a plus."
    )

    print("\n" + "=" * 60)
    print("测试检索：用一条虚构的 AI Agent 相关职位描述去匹配简历")
    print(f"职位描述: {test_job_description}\n")

    matches = search_resume(test_job_description, top_k=3)
    for i, m in enumerate(matches):
        print(f"--- 匹配 {i+1} | 板块: {m['section']} | 距离(越小越相关): {m['distance']:.4f} ---")
        print(m["content"][:200])
        print()
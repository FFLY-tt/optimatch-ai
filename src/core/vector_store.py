"""
向量存储与检索模块。
用 sentence-transformers 在本地把文字转成向量（不用调用外部 API，免费、无网络延迟），
存进 Chroma 向量数据库，然后支持"给一段职位描述，检索最相关的简历片段"。

模型选择：paraphrase-multilingual-MiniLM-L12-v2（多语言）。

之前用的 all-MiniLM-L6-v2 只是英文语料训练的，实测坐实过真实问题：跨语言
相似度对齐很弱——比如一条英文 JD 去匹配中英混合的简历画像时，中文 chunk
会因为"语言不匹配"而普遍算出偏低的相似度，不是因为内容不相关；反过来，
两段语言相同但内容毫不相关的文本，有时候比语言不同但内容真正相关的文本
分数还高（实测坐实过一次：不相关的中文 JD 反而比相关的英文 JD 打分更高）。
这个系统的简历/JD 匹配场景本来就需要处理中英文混合数据，必须用多语言模型。

换成同样是 MiniLM 量级（相对轻量，不是 mpnet-base-v2 那种更大更慢的多语言
模型）的多语言版本，向量维度都是 384，Chroma 里存的向量结构不受影响。
代价：模型文件从 88MB 涨到 458MB（约 5.2 倍），热加载耗时从 1.58s 涨到
3.02s（约 1.9 倍），单条编码延迟从 3.1ms 涨到 3.8ms（约 1.2 倍，影响较小）。
第一次运行需要下载模型（实测约 18s，之后走本地缓存）。

⚠️ 换模型后，Chroma 里任何用旧模型算出来的历史 embedding 都和新模型的向量
空间不兼容（不能直接混用/比较），必须重新生成——local dev 环境直接删掉
data/chroma_db 重新建，不需要做迁移逻辑。
"""

import json
import os
import re
import uuid

import chromadb
from sentence_transformers import SentenceTransformer

_model = None
_client = None

EMBEDDING_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"

# 简历 AST 解析产出的 chunk（句子 + tags）用的独立 collection，
# 和 build_resume_collection() 那个"按 section 整段存、每次覆盖"的旧方案
# （Tab A 业务档案还在用）分开，互不干扰。
RESUME_CHUNKS_COLLECTION = "resume_chunks"
KEYWORDS_STORE_PATH = os.path.join("data", "resume_keywords.json")


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
    把整段式切块结果存进 Chroma（每次调用先清空旧数据再整批写入）。
    目前给 Tab A 业务档案（tab_a_outreach/business_profile.py）用；
    简历那边已经改用 add_resume_chunks()（累加式存储，见下方），
    不再用这个函数。
    chunks: [{"section": "...", "content": "..."}]
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


def _flatten_tags(tags: dict) -> dict:
    """
    Chroma 的 metadata 只接受标量值（str/int/float/bool），不能直接存嵌套 dict，
    也不接受 None。resume_ast.py / jd_parser.py 产出的 tags 里经常有 None
    （比如没抠到 role），这里统一转成空字符串再存。
    Chroma 还不接受空 dict——tags 整个是空的时候兜一个 source 标记，避免 add 直接抛错。
    """
    flat = {k: ("" if v is None else v) for k, v in (tags or {}).items()}
    return flat or {"source": "resume"}


def normalize_chunk_text(text: str) -> str:
    """
    归一化 chunk 文本，用于"内容是否重复"的判断：小写 + 折叠所有空白 + 去掉首尾标点。
    match_scoring 的检索去重也用这个，保证两处"什么算重复"的口径一致。
    """
    return re.sub(r"\s+", " ", (text or "").strip().lower()).strip(" .,:;!?-—–·")


def add_resume_chunks(chunks: list[dict], collection_name: str = RESUME_CHUNKS_COLLECTION) -> int:
    """
    追加一批简历/用户补充文本的 chunk 到 Chroma——不删除已有数据。
    支持多次上传简历、多次补充自由文本时持续累积，而不是每次覆盖。

    chunks: [{"text": str, "tags": dict}, ...]
        （resume_ast.parse_resume_markdown()["chunks"] 或
         resume_ast.parse_free_text_note() 的产出格式）
    返回：本次真正新增（去重后）的 chunk 数量

    去重说明：同一份简历被重复上传时（用户很常见的操作："传个更新版"），
    add 纯追加会把每条经历 chunk 存成 N 份，检索时 top_k 会被同一段内容占满
    多个名额（实测坐实过一次：56 条 chunk 里只有 14 条不同，每条 ×4）。
    这里按归一化文本跟已有内容 + 本批内部比对，只写真正没见过的 chunk。
    """
    if not chunks:
        return 0

    client = get_chroma_client()
    collection = client.get_or_create_collection(collection_name)

    try:
        existing_docs = collection.get(include=["documents"]).get("documents") or []
    except Exception:
        existing_docs = []
    seen = {normalize_chunk_text(t) for t in existing_docs}

    fresh: list[dict] = []
    for c in chunks:
        key = normalize_chunk_text(c["text"])
        if not key or key in seen:
            continue
        seen.add(key)
        fresh.append(c)

    skipped = len(chunks) - len(fresh)
    if not fresh:
        print(f"  [调试] {len(chunks)} 条 chunk 都已存在（重复上传？），未新增")
        return 0

    model = get_model()
    texts = [c["text"] for c in fresh]
    embeddings = model.encode(texts).tolist()
    ids = [f"chunk_{uuid.uuid4().hex}" for _ in fresh]
    metadatas = [_flatten_tags(c.get("tags", {})) for c in fresh]

    collection.add(ids=ids, embeddings=embeddings, documents=texts, metadatas=metadatas)
    print(
        f"  [调试] 追加 {len(fresh)} 条新 chunk 到 Chroma collection '{collection_name}'"
        + (f"（跳过 {skipped} 条重复）" if skipped else "")
        + "（累计存储，不清空旧数据）"
    )
    return len(fresh)


def get_all_resume_chunks(collection_name: str = RESUME_CHUNKS_COLLECTION) -> list[dict]:
    """
    取出当前累计的全部简历/补充文本 chunk（不含 embedding，match_scoring.py
    需要用的时候自己现算——corpus 通常就几十上百条，重新编码成本很低，
    没必要依赖 Chroma 能不能把已存的 embedding 原样吐回来这种细节）。
    collection 还没建过（用户还没上传过简历）时返回空列表，不抛异常。

    按归一化文本去重（保留第一条）——既是给历史上已经被重复上传污染的
    collection 做无损自愈，也是 add_resume_chunks 去重之外的第二道保险。
    """
    client = get_chroma_client()
    try:
        collection = client.get_collection(collection_name)
    except Exception:
        return []

    result = collection.get(include=["documents", "metadatas"])
    chunks = []
    seen: set[str] = set()
    for doc_id, text, meta in zip(result["ids"], result["documents"], result["metadatas"]):
        key = normalize_chunk_text(text)
        if not key or key in seen:
            continue
        seen.add(key)
        chunks.append({"id": doc_id, "text": text, "tags": meta})
    return chunks


def dedupe_resume_chunks(collection_name: str = RESUME_CHUNKS_COLLECTION) -> dict:
    """
    一次性物理清理 collection 里的重复 chunk（历史上简历被重复上传攒下的）：
    按归一化文本保留每种内容的第一条，其余从 Chroma 删掉。
    返回 {"before", "after", "removed"}。collection 不存在时三个数都是 0。
    """
    client = get_chroma_client()
    try:
        collection = client.get_collection(collection_name)
    except Exception:
        return {"before": 0, "after": 0, "removed": 0}

    result = collection.get(include=["documents"])
    ids, texts = result["ids"], result["documents"]

    seen: set[str] = set()
    to_delete: list[str] = []
    for doc_id, text in zip(ids, texts):
        key = normalize_chunk_text(text)
        if not key or key in seen:
            to_delete.append(doc_id)
        else:
            seen.add(key)

    if to_delete:
        collection.delete(ids=to_delete)
    return {"before": len(ids), "after": len(ids) - len(to_delete), "removed": len(to_delete)}


def reset_resume_chunks(collection_name: str = RESUME_CHUNKS_COLLECTION) -> None:
    """清空简历 chunk 存储（测试用，或者用户想整个重来）。"""
    client = get_chroma_client()
    try:
        client.delete_collection(collection_name)
    except Exception:
        pass


def load_resume_keywords() -> set[str]:
    """加载持久化的 keyword 集合（Region A 结构化字段：学历/学校/技术栈）。"""
    if not os.path.exists(KEYWORDS_STORE_PATH):
        return set()
    with open(KEYWORDS_STORE_PATH, "r", encoding="utf-8") as f:
        return set(json.load(f))


def add_resume_keywords(keywords: set[str]) -> set[str]:
    """
    把新解析出的 keyword 集合并入持久化存储（取并集，不覆盖）——
    多份简历的 keyword 应该是并集，不是最后一份覆盖前面的。
    返回合并后的完整集合。
    """
    existing = load_resume_keywords()
    merged = existing | keywords
    os.makedirs(os.path.dirname(KEYWORDS_STORE_PATH), exist_ok=True)
    with open(KEYWORDS_STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(merged), f, ensure_ascii=False, indent=2)
    return merged


def reset_resume_keywords() -> None:
    """清空 keyword 持久化存储（测试用，或者用户想整个重来）。"""
    if os.path.exists(KEYWORDS_STORE_PATH):
        os.remove(KEYWORDS_STORE_PATH)


if __name__ == "__main__":
    # 测试运行：python -m src.core.vector_store
    # build_resume_collection() 现在主要给 Tab A 业务档案（{"section","content"}
    # 这种整段式 chunk）用，简历那边已经改用 add_resume_chunks()
    # （见 src/core/resume_ast.py + src/core/match_scoring.py），
    # 这里用几条手写样例做自测，不再依赖已经废弃的 chunker.py。
    chunks = [
        {"section": "EXPERIENCE", "content": "Built a scalable data pipeline using Apache Spark and LangChain to power an autonomous AI agent workflow."},
        {"section": "SKILLS", "content": "Python, Docker, Kubernetes, CI/CD pipelines, LangGraph."},
        {"section": "EDUCATION", "content": "Master of Computer Science, Ontario Tech University."},
    ]

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
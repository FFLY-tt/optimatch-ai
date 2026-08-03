"""
业务档案处理模块（Tab A 用）。
和简历处理的逻辑结构一样，复用 vector_store.py 的向量化/存储能力，
只是换了数据来源：用户的业务描述 + 可选的官网正文，而不是简历 PDF。

存进 Chroma 时用独立的 collection_name="business_profile"，
和简历的 "resume" collection 分开，互不干扰。
"""

import requests
from bs4 import BeautifulSoup
from src.vector_store import build_resume_collection

BUSINESS_COLLECTION_NAME = "business_profile"


def fetch_website_text(url: str, max_chars: int = 3000) -> str:
    """
    抓取官网正文文本（合规：直接请求公开网页，不涉及登录/绕过限制）。
    只做基础的文本提取，不追求完美的正文识别算法——MVP 阶段够用。
    """
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        text = soup.get_text(separator="\n")
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        clean_text = "\n".join(lines)
        return clean_text[:max_chars]
    except requests.RequestException as e:
        print(f"  [调试] 抓取官网失败: {e}")
        return ""


def build_business_profile(
    business_description: str,
    target_customer: str,
    website_url: str = "",
) -> list[dict]:
    """
    把业务档案信息整理成切块列表，存入 Chroma。
    返回：切块列表（和 chunker.py 返回格式一致，方便复用检索逻辑）
    """
    chunks = [
        {"section": "BUSINESS_DESCRIPTION", "content": business_description.strip()},
        {"section": "TARGET_CUSTOMER", "content": target_customer.strip()},
    ]

    if website_url.strip():
        website_text = fetch_website_text(website_url)
        if website_text:
            chunks.append({"section": "WEBSITE_CONTENT", "content": website_text})

    chunks = [c for c in chunks if c["content"]]
    build_resume_collection(chunks, collection_name=BUSINESS_COLLECTION_NAME)
    return chunks


if __name__ == "__main__":
    # 测试运行：python -m src.business_profile
    test_description = (
        "We are a Shenzhen-based factory manufacturing smart pet feeders and water dispensers. "
        "We sell to independent-site sellers and TikTok Shop sellers in the US and Europe."
    )
    test_customer = "TikTok pet product sellers and affiliates looking for reliable suppliers"

    print("正在构建业务档案...")
    chunks = build_business_profile(test_description, test_customer)
    print(f"共存入 {len(chunks)} 块:")
    for c in chunks:
        print(f"--- {c['section']} ---")
        print(c["content"][:200])
        print()
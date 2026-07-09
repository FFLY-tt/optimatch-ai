"""
Tavily 全网搜索连接器。
用于 Tab A（跨境商机）和 Tab B（求职，作为 HN 的补充）。

需要先在 https://tavily.com 注册账号，拿到免费 API Key（每月 1000 次额度），
填进 .env 文件的 TAVILY_API_KEY。
"""

import os
from datetime import datetime, timezone
from dotenv import load_dotenv
from tavily import TavilyClient
from src.schema import UnifiedRecord

load_dotenv()

_client = None


def get_client() -> TavilyClient:
    global _client
    if _client is None:
        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            raise RuntimeError("没有找到 TAVILY_API_KEY，检查 .env 文件是否配置好")
        _client = TavilyClient(api_key=api_key)
    return _client


def search(query: str, platform_type: str = "business", max_results: int = 10) -> list[UnifiedRecord]:
    """
    执行一次 Tavily 搜索，返回统一格式的结果列表。

    query: 搜索关键词，例如：
        求职场景： "site:reddit.com r/forhire remote python developer"
        跨境场景： "looking for TikTok affiliate pet products"
    platform_type: "job" 或 "business"，标记这条搜索属于哪个 Tab
    """
    client = get_client()
    response = client.search(
        query=query,
        search_depth="advanced",   # advanced 模式返回内容更完整，basic 更快更省额度
        max_results=max_results,
        include_answer=False,
    )

    records = []
    for item in response.get("results", []):
        title = item.get("title", "")
        url = item.get("url", "")

        # 求职场景下，排除掉自由职业者自荐帖（[FOR HIRE]），只保留公司招聘帖
        if platform_type == "job" and "[for hire]" in title.lower():
            continue

        # 排除版块首页（比如 reddit.com/r/forhire，没有具体帖子路径），这种不是真正的招聘信息
        if platform_type == "job" and url.rstrip("/").count("/") <= 4:
            continue
        record = UnifiedRecord(
            source="tavily",
            title=item.get("title", "")[:120],
            content=item.get("content", ""),
            url=item.get("url", ""),
            posted_at=datetime.now(timezone.utc).isoformat(),  # Tavily 不一定返回原文发布时间，先用抓取时间占位
            platform_type=platform_type,
            author=None,
        )
        records.append(record)

    return records


if __name__ == "__main__":
    # 直接运行测试：python -m src.connectors.tavily_connector
    # 第一周最关键的验证：分别测求职关键词和跨境关键词，人工检查结果质量

    print("=" * 60)
    print("测试1：求职场景关键词")
    job_results = search(
        query="site:reddit.com r/forhire [HIRING] remote AI engineer python",
        platform_type="job",
        max_results=8,   # 多要几条，因为过滤掉一部分后可能剩得少
    )
    print(f"返回 {len(job_results)} 条结果\n")
    for r in job_results:
        print("-", r.title, "|", r.url)

    print("\n" + "=" * 60)
    print("测试2：跨境商机场景关键词（多角度查询）")
    biz_queries = [
        "TikTok affiliate program pet products commission",
        "looking for dropshipping supplier pet accessories",
        "reddit r/Entrepreneur looking for manufacturer pet products",
    ]
    for q in biz_queries:
        print(f"\n查询词: {q}")
        biz_results = search(query=q, platform_type="business", max_results=3)
        for r in biz_results:
            print("-", r.title, "|", r.url)

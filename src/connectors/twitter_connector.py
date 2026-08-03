"""
Twitter (X) Connector —— 通过 RapidAPI 上的非官方 Twitter API wrapper
按关键词搜索相关推文/用户。

返回值统一转成 UnifiedRecord，字段对齐 tavily_connector 的输出格式。

⚠️ 使用前需要设置环境变量 RAPIDAPI_KEY。
⚠️ TW_HOST 请替换成你在 RapidAPI 实际订阅的接口 host。
⚠️ 不同 wrapper 接口的返回 JSON 结构差异很大，务必先在 RapidAPI 的
   "Test Endpoint" 页面手动跑一次，确认真实字段名，再对下面的解析逻辑做调整。
"""

import os

import requests

from src.core.schema import UnifiedRecord

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")
TW_HOST = "twitter-api45.p.rapidapi.com"  # 以你实际订阅的接口文档为准

HEADERS = {
    "X-RapidAPI-Key": RAPIDAPI_KEY,
    "X-RapidAPI-Host": TW_HOST,
}


def search(query: str, platform_type: str = "business", max_results: int = 10) -> list[UnifiedRecord]:
    """
    保持和 tavily_search(query=..., platform_type=..., max_results=...) 一致的调用签名。
    返回：list[UnifiedRecord]，source 字段固定为 "twitter"。
    """
    if not RAPIDAPI_KEY:
        raise RuntimeError("环境变量 RAPIDAPI_KEY 未设置，请先配置 RapidAPI 密钥")

    url = f"https://{TW_HOST}/search.php"
    resp = requests.get(
        url, headers=HEADERS, params={"query": query, "count": max_results}, timeout=10
    )
    resp.raise_for_status()
    data = resp.json()

    records: list[UnifiedRecord] = []
    for tweet in data.get("timeline", [])[:max_results]:
        username = tweet.get("screen_name")
        if not username:
            continue

        text = tweet.get("text", "")
        records.append(
            UnifiedRecord(
                source="twitter",
                title=f"@{username}: {text[:60]}",
                content=text,
                url=f"https://twitter.com/{username}/status/{tweet.get('tweet_id', '')}",
                platform_type=platform_type,
                author=username,
                followers=tweet.get("followers_count"),
            )
        )

    return records


if __name__ == "__main__":
    # 独立测试：python -m src.connectors.twitter_connector
    results = search(query="looking for pet product supplier", max_results=5)
    print(f"共抓取 {len(results)} 条结果\n")
    for r in results:
        print(f"- {r.title} | {r.url}")
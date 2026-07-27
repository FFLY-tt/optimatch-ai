"""
Instagram Connector —— 通过 RapidAPI（RocketAPI 或同类 Instagram Scraper）
按 hashtag 抓取相关帖子的作者，再逐个查询 profile 拿 bio / 粉丝数，
提取 bio 里的公开邮箱。

返回值统一转成 UnifiedRecord，字段对齐 tavily_connector 的输出格式，
这样上层 search_agent.py 不需要对 Instagram 结果做任何特殊处理，
可以和网页搜索结果一起去重、过滤、展示。

⚠️ 使用前需要设置环境变量 RAPIDAPI_KEY。
⚠️ IG_HOST 请替换成你在 RapidAPI 实际订阅的接口 host（不同 wrapper host 不同）。
⚠️ 每次 search() 调用内部会对每个作者额外发一次 profile 请求，
   注意这会成倍消耗 RapidAPI 调用额度（按次计费的话尤其要控制 max_results）。
"""

import os
import re
from typing import Optional

import requests

from src.schema import UnifiedRecord

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")
IG_HOST = "rocketapi-for-instagram.p.rapidapi.com"  # 以你实际订阅的接口文档为准，先手动测试确认真实字段结构

HEADERS = {
    "X-RapidAPI-Key": RAPIDAPI_KEY,
    "X-RapidAPI-Host": IG_HOST,
}

EMAIL_PATTERN = re.compile(r"[\w\.-]+@[\w\.-]+\.\w+")


def _extract_email(bio: str) -> Optional[str]:
    if not bio:
        return None
    match = EMAIL_PATTERN.search(bio)
    return match.group(0) if match else None


def _get_profile(username: str) -> dict:
    """
    查询单个用户的 profile，拿 bio 和粉丝数。
    注意：这是一次独立的 API 调用，hashtag 搜索返回多少个作者，这里就会额外调用多少次。
    """
    url = f"https://{IG_HOST}/instagram/user/get_info"
    resp = requests.get(url, headers=HEADERS, params={"username": username}, timeout=10)
    resp.raise_for_status()
    return resp.json().get("user", {})


def search(query: str, platform_type: str = "business", max_results: int = 10) -> list[UnifiedRecord]:
    """
    query 直接当作 hashtag 使用（自动去掉可能带的 # 号和空格）。
    保持和 tavily_search(query=..., platform_type=..., max_results=...) 一致的调用签名，
    这样上层调用代码不需要区分是在调哪个 connector。

    返回：list[UnifiedRecord]，source 字段固定为 "instagram"。
    """
    if not RAPIDAPI_KEY:
        raise RuntimeError("环境变量 RAPIDAPI_KEY 未设置，请先配置 RapidAPI 密钥")

    hashtag = query.lstrip("#").replace(" ", "")

    url = f"https://{IG_HOST}/instagram/hashtag/get_medias"
    resp = requests.get(
        url, headers=HEADERS, params={"hashtag": hashtag, "count": max_results}, timeout=10
    )
    resp.raise_for_status()
    medias = resp.json().get("data", {}).get("items", [])

    records: list[UnifiedRecord] = []
    seen_usernames: set[str] = set()

    for media in medias:
        username = media.get("user", {}).get("username")
        if not username or username in seen_usernames:
            continue
        seen_usernames.add(username)

        try:
            profile = _get_profile(username)
        except requests.RequestException as e:
            print(f"  [调试] Instagram profile 获取失败 @{username}: {e}")
            continue

        bio = profile.get("biography", "") or ""
        followers = profile.get("follower_count")
        email = _extract_email(bio)

        records.append(
            UnifiedRecord(
                source="instagram",
                title=f"@{username} ({followers or 0} followers)",
                content=bio,
                url=f"https://instagram.com/{username}",
                platform_type=platform_type,
                author=username,
                email=email,
                followers=followers,
            )
        )

    return records


if __name__ == "__main__":
    # 独立测试：python -m src.connectors.instagram_connector
    # 建议先用小的 max_results（比如2-3）测试真实返回结构，确认字段名对得上，再放大规模跑
    results = search(query="petaccessories", max_results=3)
    print(f"共抓取 {len(results)} 条结果\n")
    for r in results:
        print(f"- {r.title} | email={r.email} | {r.url}")
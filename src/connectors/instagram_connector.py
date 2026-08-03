"""
Instagram Connector —— 通过 RapidAPI 上的 Instagram Statistics API
按关键词搜索 KOL/KOC 账号，返回统一的 UnifiedRecord。

【核心改进：全部用服务端参数筛选，不在客户端循环过滤】
这个接口支持直接传粉丝区间、平台类型、互动率区间等参数，
服务端返回的就是已经筛好的结果，不需要我们自己拉回来再过滤，
既省调用额度，又不会因为"拉回30条全不符合"而返回0条结果。

关键参数说明（来自接口文档 Params 44项）：
- socialTypes=INST    只要 Instagram，过滤掉 TikTok/FB/Twitter/YouTube
- minUsersCount       粉丝数下限
- maxUsersCount       粉丝数上限
- locations           账号所在地区（country or city，来自 Tags 端点的值）
- minER / maxER       互动率区间（avgER，小数格式，0.02 = 2%）
- sort=usersCount     升序排列，让腰部账号排在前面（不加负号=升序）
- trackTotal=true     返回总数，方便调试

⚠️ RAPIDAPI_KEY 请配置在 .env 里，不要在测试脚本里手动赋值。
"""

import os
import re
from typing import Optional

import requests
from dotenv import load_dotenv

from src.core.schema import UnifiedRecord

load_dotenv()

RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")
IG_HOST = "instagram-statistics-api.p.rapidapi.com"

HEADERS = {
    "X-RapidAPI-Key": RAPIDAPI_KEY,
    "X-RapidAPI-Host": IG_HOST,
}

EMAIL_PATTERN = re.compile(r"[\w\.-]+@[\w\.-]+\.\w+")


def _extract_email(text: str) -> Optional[str]:
    if not text:
        return None
    match = EMAIL_PATTERN.search(text)
    return match.group(0) if match else None


def search(
    query: str,
    platform_type: str = "business",
    max_results: int = 10,
    min_followers: int = 3000,
    max_followers: int = 50000,
    location: str = "",  # 默认不限地区，全球范围；如需限定可传如 "united-states" / "united-kingdom"
    min_er: Optional[float] = None,
    max_er: Optional[float] = None,
) -> list[UnifiedRecord]:
    """
    按关键词搜索 Instagram KOC 账号，服务端直接筛好粉丝区间和平台。

    query: 搜索关键词（由 search_agent 根据 hashtag 传入）
    min_followers / max_followers: 粉丝区间，默认 3,000 ~ 50,000
    location: 账号地区，默认 united-states（值来自 Tags 端点的 location 类型）
    min_er / max_er: 互动率区间（可选，0.02 = 2%），不传则不限制
    """
    if not RAPIDAPI_KEY:
        print("  [调试] Instagram connector: RAPIDAPI_KEY 未设置，跳过本次调用")
        return []

    params: dict = {
        "q": query.lstrip("#"),
        "page": "1",
        "perPage": str(max_results),
        "sort": "usersCount",        # 升序，让腰部账号排前面
        "minUsersCount": str(min_followers),
        "maxUsersCount": str(max_followers),
        "trackTotal": "true",
        # socialTypes 不传 = 跨平台返回（INST / TW / TT / YT 等都要）
    }

    if location:
        params["locations"] = location
    if min_er is not None:
        params["minER"] = str(min_er)
    if max_er is not None:
        params["maxER"] = str(max_er)

    try:
        resp = requests.get(
            f"https://{IG_HOST}/search",
            headers=HEADERS,
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"  [调试] Instagram API 调用失败: {e}")
        return []

    data_list = data.get("data", [])
    total = data.get("total", "?")
    print(f"  [调试] Instagram 搜索 '{query}'：服务端共 {total} 条符合条件，本次返回 {len(data_list)} 条")

    records: list[UnifiedRecord] = []
    for item in data_list:
        screen_name = item.get("screenName") or "unknown"
        followers = item.get("usersCount", 0)
        bio = item.get("description", "") or ""
        profile_url = item.get("url") or f"https://instagram.com/{screen_name}"
        email = _extract_email(bio)

        avg_er = item.get("avgER")
        er_str = f"{avg_er * 100:.2f}%" if avg_er else "N/A"

        content_text = (
            f"Bio: {bio}\n"
            f"Engagement Rate: {er_str}\n"
            f"Contact Email: {email if email else 'Not listed in Bio'}"
        )

        # 把接口返回的平台代码映射成可读的 source 字符串
        social_type_map = {
            "INST": "instagram",
            "TW": "twitter",
            "TT": "tiktok",
            "YT": "youtube",
            "FB": "facebook",
            "TG": "telegram",
        }
        raw_type = item.get("socialType", "")
        source = social_type_map.get(raw_type, raw_type.lower() or "social")

        records.append(
            UnifiedRecord(
                source=source,
                title=f"@{screen_name} ({followers:,} followers) [KOC]",
                content=content_text,
                url=profile_url,
                platform_type=platform_type,
                author=screen_name,
                email=email,
                followers=followers,
            )
        )

    return records


if __name__ == "__main__":
    # 独立测试：python -m src.connectors.instagram_connector
    import time

    print("=== 测试1：宠物类关键词，美国账号，3k-5w粉丝 ===")
    start = time.time()
    results = search(query="pet", max_results=5)
    elapsed = time.time() - start
    print(f"耗时: {elapsed:.2f}秒")
    print(f"共拿到 {len(results)} 条结果\n")
    for r in results:
        print(f"  - {r.title}")
        print(f"    email={r.email}")
        print(f"    {r.url}")
        print()
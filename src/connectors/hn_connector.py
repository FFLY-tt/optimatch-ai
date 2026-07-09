"""
Hacker News「Who is hiring」数据连接器。
完全合规，用的是 HN 官方 Firebase API + Algolia 搜索 API，免费无限制。

流程：
1. 用 Algolia 搜索最新一期 "Ask HN: Who is hiring?" 帖子，拿到它的 story id
2. 用 Firebase API 拉取这个帖子下所有一级评论（每条评论就是一条招聘信息）
3. 把每条评论转成统一格式 UnifiedRecord
"""

import requests
from datetime import datetime, timezone
from src.schema import UnifiedRecord

ALGOLIA_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
FIREBASE_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{}.json"


def get_latest_who_is_hiring_id() -> int:
    """搜索最新一期 Who is hiring 帖子，返回它的 HN item id"""
    params = {
        "query": "Ask HN: Who is hiring?",
        "tags": "story",
        "hitsPerPage": 5,
    }
    resp = requests.get(ALGOLIA_SEARCH_URL, params=params, timeout=10)
    resp.raise_for_status()
    hits = resp.json().get("hits", [])
    if not hits:
        raise RuntimeError("没搜到 Who is hiring 帖子，检查网络或 Algolia API 是否正常")
    # 取最新一条（按标题精确包含 "who is hiring" 且作者是 whoishiring）
    for hit in hits:
        title_lower = hit["title"].lower()
        is_correct_author = hit.get("author") == "whoishiring"
        is_correct_title = title_lower.startswith("ask hn: who is hiring?")
        if is_correct_author and is_correct_title:
            return int(hit["objectID"])
    raise RuntimeError("没找到官方 whoishiring 发的帖子，检查 Algolia 搜索参数")


def fetch_item(item_id: int) -> dict:
    """拉取单条 HN item（可以是 story 也可以是 comment）"""
    resp = requests.get(FIREBASE_ITEM_URL.format(item_id), timeout=10)
    resp.raise_for_status()
    return resp.json()


def fetch_hn_jobs(limit: int = 50) -> list[UnifiedRecord]:
    """
    抓取最新一期 Who is hiring 帖子下的招聘评论。
    limit: 最多抓取多少条，测试阶段建议先设小一点（比如 20），避免请求太多太慢。
    """
    story_id = get_latest_who_is_hiring_id()
    story = fetch_item(story_id)
    print("抓到的帖子ID:", story_id)
    print("抓到的帖子标题:", story.get("title"))
    print("这个帖子的URL:", f"https://news.ycombinator.com/item?id={story_id}")
    comment_ids = story.get("kids", [])[:limit]

    records = []
    for cid in comment_ids:
        try:
            item = fetch_item(cid)
        except requests.RequestException:
            continue

        if not item or item.get("deleted") or item.get("dead"):
            continue

        text = item.get("text", "")
        if not text or text.strip() == "[delayed]":
            skipped_no_text += 1
            continue

        # HN 评论正文是 HTML 转义过的，这里做最基础的清洗，后续可以在数据处理层继续加强
        clean_text = (
            text.replace("<p>", "\n")
            .replace("&amp;", "&")
            .replace("&gt;", ">")
            .replace("&lt;", "<")
            .replace("&#x27;", "'")
            .replace("&#x2F;", "/")
            .replace("&quot;", '"')
        )
        # 去掉 <a href="...">文字</a> 这种 HTML 标签，只保留里面的文字
        import re
        clean_text = re.sub(r'<a href="[^"]*"[^>]*>([^<]*)</a>', r'\1', clean_text)
        clean_text = re.sub(r'<[^>]+>', '', clean_text)


        posted_at = None
        if item.get("time"):
            posted_at = datetime.fromtimestamp(item["time"], tz=timezone.utc).isoformat()

        # HN 评论没有单独的标题字段，取正文前 80 字符当标题用
        title = clean_text.strip().split("\n")[0][:80]

        record = UnifiedRecord(
            source="hackernews",
            title=title if title else f"HN Job Post #{cid}",
            content=clean_text.strip(),
            url=f"https://news.ycombinator.com/item?id={cid}",
            posted_at=posted_at,
            platform_type="job",
            author=item.get("by"),
        )
        records.append(record)

    return records


if __name__ == "__main__":
    # 直接运行这个文件可以做快速测试：python -m src.connectors.hn_connector
    jobs = fetch_hn_jobs(limit=10)
    print(f"抓到 {len(jobs)} 条招聘信息\n")
    for j in jobs[:3]:
        print("=" * 60)
        print("标题:", j.title)
        print("链接:", j.url)
        print("内容预览:", j.content[:200])

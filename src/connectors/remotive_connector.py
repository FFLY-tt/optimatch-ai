"""
Remotive 岗位连接器 —— 直接调 remotive.com 的开放 JSON API，
不需要 API key，返回结构化的远程岗位数据。

参考 huangbai-AI/OpenWork（AnySearch 博主的项目）的用法：
https://remotive.com/api/remote-jobs?limit=500
返回 { jobs: [...] }，每个 job 的关键字段：
- id: 岗位 ID
- title: 职位标题
- company_name: 公司名
- candidate_required_location: 候选人所在地要求（例如 "USA"、"Europe"、"Worldwide"）
- category: 岗位大类（Remotive 自己的分类）
- tags: 岗位标签列表
- salary: 薪资文本（有则给，没有为空字符串）
- publication_date: 发布时间（ISO 8601）
- url: 岗位页链接
- description: 岗位描述（HTML）

和 RemoteOK 一样是免费开放数据源，能覆盖到的岗位不占 AnySearch 额度。
Remotive 相比 RemoteOK 覆盖更广（岗位分类更丰富），两个源可以并行调用互补。
"""

import re
from datetime import datetime, timezone

import requests

from src.core.schema import UnifiedRecord
from src.connectors.base import BaseDataConnector

REMOTIVE_URL = "https://remotive.com/api/remote-jobs"

_HTML_TAG = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = _HTML_TAG.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_pub_date(text: str) -> str | None:
    """
    Remotive 的 publication_date 通常是带时区的 ISO 8601（"...Z"），但实测
    发现偶尔会返回不带时区的裸时间（比如 "2026-08-23 13:30:47"）。裸时间
    不能直接 .astimezone(utc)——astimezone() 对没有 tzinfo 的对象会先假定
    它是"当前系统本地时区"再换算成 UTC，导致时间戳按部署机器的时区偏移，
    换一台机器结果就不一样（实测过：UTC-4 机器上每条 posted_at 都被错误
    地多加了 4 小时）。裸时间本身就是 UTC，直接补上 tzinfo，不做换算。
    """
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def search(
    keyword: str | None = None,
    category: str | None = None,
    platform_type: str = "job",
    max_results: int = 20,
    limit_per_request: int = 500,
) -> list[UnifiedRecord]:
    """
    调 Remotive API 拉岗位数据，转成 UnifiedRecord。

    参数：
    - keyword: 可选关键词过滤。Remotive 的 API 支持服务端 search 参数（会在
      标题和正文里搜），所以直接拼进请求让服务端筛选，不用像 RemoteOK 那样
      客户端过滤——服务端返回的本来就是筛过的结果，这里再加一层 title/tags
      子串匹配只是保险丝，正常不会再筛掉东西。
    - category: 可选 Remotive 分类过滤，Remotive 支持服务端按 category 过滤，
      常用值："software-dev" / "customer-support" / "design" / "marketing"
      等。传了会拼进请求 URL 让服务端返回更少数据。
    - platform_type: "job" 或 "business"
    - max_results: 客户端最终返回多少条
    - limit_per_request: 从 Remotive API 一次拉多少条备选（服务端最大 500）
    """
    params = {"limit": min(limit_per_request, 500)}
    if category:
        params["category"] = category
    if keyword:
        params["search"] = keyword

    resp = requests.get(
        REMOTIVE_URL,
        params=params,
        headers={"User-Agent": "optimatch-ai/1.0"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()

    jobs = data.get("jobs") or []

    if keyword:
        # 服务端 search 已经筛过一轮，这里只是兜底二次确认（防止服务端匹配
        # 逻辑比预期宽松，比如搜正文命中了标题里完全不相关的结果）。
        # 按空格拆词、每个词都要命中才保留（不要求相邻/顺序一致），和
        # RemoteOK 连接器的过滤逻辑保持一致——避免同样一个多词短语在
        # 两个连接器里一个能匹配、一个匹配不到的不一致行为。
        # 用整词边界匹配（\b...\b），不能用普通子串 `in` 判断——实测在
        # RemoteOK 那边坐实过："ai" 这种短词当子串会命中 "Airport"、
        # "Maintenance" 这类单词内部片段，全是假阳性。
        # 不再把 tags 纳入匹配范围——实测发现 Lemon.io 这类雇主的职位（比如
        # "Senior Golang Developer"、"Tech Lead Full-Stack Rails Engineer"）
        # tags 里塞了几十个不相关技能标签，其中一个通用的 "AI/ML" 标签导致
        # 搜 "AI Engineer" 时被误判命中。和 RemoteOK 一样，只信 title/
        # description 这两个由招聘方直接撰写的字段，tags 这类可能被污染的
        # 字段不再作为匹配依据（换来精度，可能损失一部分只在 tags 里体现
        # 相关性的真阳性）。
        words = [w for w in keyword.lower().split() if w]
        word_patterns = [re.compile(r"\b" + re.escape(word) + r"\b") for word in words]
        jobs = [
            j for j in jobs
            if all(
                pattern.search((
                    (j.get("title") or "") + " " +
                    (j.get("description") or "")
                ).lower())
                for pattern in word_patterns
            )
        ]

    records = []
    for job in jobs[:max_results]:
        title = job.get("title") or "Untitled"
        company = job.get("company_name")
        location = job.get("candidate_required_location") or "Remote (worldwide)"
        url = job.get("url") or ""
        posted_at = _parse_pub_date(job.get("publication_date") or "") or datetime.now(timezone.utc).isoformat()
        content = _strip_html(job.get("description") or "")
        salary = job.get("salary") or None
        # Remotive 自己有 category 字段，直接用，不做二次分类
        cat = job.get("category") or None

        records.append(UnifiedRecord(
            source="remotive",
            title=title[:120],
            content=content[:3000],
            url=url,
            posted_at=posted_at,
            platform_type=platform_type,
            author=company,
            company=company,
            location=location,
            salary=salary,
            remote=True,
            category=cat,
        ))
    return records


class RemotiveConnector(BaseDataConnector):
    source_name = "remotive"

    def fetch(
        self,
        keyword: str | None = None,
        category: str | None = None,
        platform_type: str = "job",
        max_results: int = 20,
        **kwargs,
    ) -> list[UnifiedRecord]:
        return search(
            keyword=keyword,
            category=category,
            platform_type=platform_type,
            max_results=max_results,
        )


if __name__ == "__main__":
    # python -m src.connectors.remotive_connector
    print("=" * 60)
    print("Remotive：查询关键词 'AI'（不限分类）")
    results = search(keyword="AI", max_results=5)
    print(f"返回 {len(results)} 条\n")
    for r in results:
        print("-" * 60)
        print("title      :", r.title)
        print("company    :", r.company)
        print("location   :", r.location)
        print("salary     :", r.salary)
        print("posted_at  :", r.posted_at)
        print("category   :", r.category)
        print("url        :", r.url)
        print("content    :", r.content[:200])
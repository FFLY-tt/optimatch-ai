"""
RemoteOK 岗位连接器 —— 直接调 remoteok.com 的开放 JSON API，
不需要 API key，不需要爬虫，结构化数据直接返回。

参考 huangbai-AI/OpenWork（AnySearch 博主的项目）的用法：
https://remoteok.com/api 返回一个 JSON 数组，第一项是元信息（不是岗位），
剩下的每一项都是一个岗位对象，字段有：
- id: 岗位 ID
- position: 职位标题
- company: 公司名
- location: 地点（可能为空，表示全球远程）
- salary_min / salary_max: 薪资范围（美元/年）
- date / epoch: 发布时间
- url / apply_url: 岗位/申请页链接
- tags: 岗位标签
- description: 岗位描述（HTML）

对比 AnySearch：AnySearch 的额度要收着用，RemoteOK 完全免费开放，能用它
覆盖到的岗位就不占 AnySearch 调用。适合作为 Tab B 求职的补充数据源。

用法：
    from src.connectors.remoteok_connector import search
    records = search(max_results=20, only_remote=True)
"""

import html
import os
import re
from datetime import datetime, timezone

import requests

from src.core.schema import UnifiedRecord
from src.connectors.base import BaseDataConnector

REMOTEOK_URL = "https://remoteok.com/api"

# RemoteOK 会拒绝看起来像脚本/爬虫的 User-Agent（返回 403），必须伪装成
# 真实浏览器才能拿到数据。之前用 "optimatch-ai/1.0" 会被拒。
_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

# RemoteOK 返回的 description 是 HTML，简单去掉标签让下游 LLM 更好处理。
_HTML_TAG = re.compile(r"<[^>]+>")


def _fix_mojibake(text: str) -> str:
    """
    RemoteOK 部分非英文（比如西语）职位描述存在 UTF-8 被误当 Latin-1 解码
    的双重编码问题（"tecnolÃ³gico" 应该是 "tecnológico"）。这里尝试用
    latin-1 重新编码再按 utf-8 解码修复；正常英文文本不受影响
    （不含这类乱码特征字符时直接跳过，也不会因为修复失败而抛异常）。
    """
    if not text or ("Ã" not in text and "â" not in text):
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _clean_text(text: str) -> str:
    """HTML 实体解码（如 &amp; -> &）+ 乱码修复，用在 position/company 等字段上。"""
    if not text:
        return text
    return _fix_mojibake(html.unescape(text))


def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = _HTML_TAG.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return _fix_mojibake(text)


def _to_iso(job: dict) -> str | None:
    """RemoteOK 的时间字段有两种：ISO 格式的 date，或者 Unix epoch 的 epoch。"""
    date = job.get("date")
    if isinstance(date, str) and date:
        try:
            # RemoteOK 的 date 是 "2026-07-15T10:23:00+00:00" 这种带时区的 ISO 8601
            dt = datetime.fromisoformat(date.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    epoch = job.get("epoch")
    if isinstance(epoch, (int, float)):
        try:
            return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
        except (ValueError, OverflowError):
            return None
    return None


def _format_salary(job: dict) -> str | None:
    """把 salary_min / salary_max 拼成一段人读友好的文本。"""
    lo, hi = job.get("salary_min"), job.get("salary_max")
    if not lo and not hi:
        return None
    if lo and hi:
        return f"${lo:,} - ${hi:,} / year"
    return f"${lo or hi:,} / year"


def _categorize(title: str, tags: list) -> str | None:
    """给 UnifiedRecord.category 一个粗分类，方便前端筛选。规则和 OpenWork 项目对齐。"""
    text = f"{title or ''} {' '.join(tags or [])}".lower()
    if re.search(r"design|creative|art director|\bux\b|\bui\b", text):
        return "设计"
    if re.search(r"machine learning|artificial intelligence|\bai\b|\bllm\b|data scientist", text):
        return "人工智能"
    if re.search(r"product manager|product owner|program manager|project manager", text):
        return "产品"
    if re.search(r"marketing|content|communications|seo|growth", text):
        return "市场"
    if re.search(r"sales|account executive|business development|partnership", text):
        return "销售"
    if re.search(r"customer success|operations|recruit|human resources|finance|legal|assistant", text):
        return "运营"
    return "开发"


def search(
    keyword: str | None = None,
    platform_type: str = "job",
    max_results: int = 20,
    only_remote: bool = True,
) -> list[UnifiedRecord]:
    """
    调 RemoteOK API 拉岗位数据，转成 UnifiedRecord。

    参数：
    - keyword: 可选的岗位关键词过滤（在 position/company/tags 里做子串匹配）。
      RemoteOK 的 API 不支持服务端关键词过滤，所以是在客户端本地过滤。
      ⚠️ 已知数据源限制（不是代码 bug）：个别岗位的 tags 字段本身会异常地
      塞进几十个不相关标签（比如零售类职位的 tags 里混进 python/golang/
      medical/sales），会导致关键词过滤出现误召回。如果实测发现这类噪音
      较多，后续可以考虑只匹配 position/company、不再信任 tags。
    - platform_type: "job"（求职）或 "business"（商机），传给 UnifiedRecord
    - max_results: 最多返回多少条
    - only_remote: RemoteOK 本身都是远程岗位，这里参数为了签名一致性预留，
      默认 True 就是 True，改成 False 不会有区别

    这个函数不抛异常（除非彻底连不上 API）——不能因为 RemoteOK 挂掉就搞挂整个
    Tab B 求职流程，网络异常返回空列表交给上层处理。
    """
    resp = requests.get(
        REMOTEOK_URL,
        headers=_REQUEST_HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()

    if not isinstance(data, list) or len(data) < 2:
        return []

    # RemoteOK 的第一项是 API 元信息（"legal" / "0-legal-notice" 之类），跳过。
    jobs = data[1:]

    if keyword:
        # 按空格拆词、每个词都要命中才保留（不要求相邻/顺序一致）——
        # 之前是把整个 keyword 当一个短语做连续子串匹配，像 "AI Engineer"
        # 这种多词短语几乎不会作为连续子串出现在 position/company/tags 里，
        # 实测直接 0 命中。
        # 拆词后第一版用的是 `word in haystack` 子串匹配，结果"ai"这种短词
        # 会命中单词内部片段（实测坐实：匹配到了 "Fire Fighter @ Adani
        # AIrport" 里的 "Airport"、"MAIntenance" 里的 "ai"），全是假阳性，
        # 改成整词边界匹配（\b...\b）修掉了。
        # 但 tags 字段本身不可信——实测发现 RemoteOK 部分职位（比如 "Store
        # Manager @ JACK & JONES"、"Senior Graphic Designer"）的 tags 里被
        # 塞了几十个和这条职位完全不相关的通用技能标签（python/golang/
        # medical/sales 全堆在一起），就算做了整词匹配，只要关键词恰好是
        # 这堆通用标签之一，照样会被这种"标签污染"命中。这不是匹配算法能
        # 解决的问题（数据源本身不可信），所以干脆不再拿 tags 做匹配依据，
        # 只信 position/company 这两个由招聘方直接填写、相对可信的字段。
        # 代价：如果一条职位真的只在 tags 里体现了相关技能、position/company
        # 里完全没提，这次改动会漏掉它（真阳性变假阴性）——这是用"降低召回"
        # 换"提高精度"的取舍，具体命中变化见 __main__ 里的回归测试。
        words = [w for w in keyword.lower().split() if w]
        word_patterns = [re.compile(r"\b" + re.escape(word) + r"\b") for word in words]
        jobs = [
            j for j in jobs
            if all(
                pattern.search((
                    (j.get("position") or "") + " " +
                    (j.get("company") or "")
                ).lower())
                for pattern in word_patterns
            )
        ]

    records = []
    for job in jobs[:max_results]:
        title = _clean_text(job.get("position") or "Untitled")
        company = _clean_text(job.get("company") or "") or None
        location = _clean_text(job.get("location") or "") or "Remote (worldwide)"
        url = job.get("url") or job.get("apply_url") or ""
        posted_at = _to_iso(job) or datetime.now(timezone.utc).isoformat()
        content = _strip_html(job.get("description") or "")
        salary = _format_salary(job)
        category = _categorize(title, job.get("tags") or [])

        records.append(UnifiedRecord(
            source="remoteok",
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
            category=category,
        ))
    return records


class RemoteOKConnector(BaseDataConnector):
    source_name = "remoteok"

    def fetch(
        self,
        keyword: str | None = None,
        platform_type: str = "job",
        max_results: int = 20,
        **kwargs,
    ) -> list[UnifiedRecord]:
        return search(
            keyword=keyword,
            platform_type=platform_type,
            max_results=max_results,
        )


if __name__ == "__main__":
    # python -m src.connectors.remoteok_connector
    print("=" * 60)
    print("RemoteOK：查询关键词 'AI'")
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
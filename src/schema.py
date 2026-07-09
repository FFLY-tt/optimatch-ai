"""
统一数据结构定义。
不管数据来自 HN、Reddit 还是 Tavily 搜索，抓取回来最终都要转成这套字段。
后面所有的匹配、生成逻辑只认这套字段，不直接依赖任何一个平台的原始格式。
"""

from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class UnifiedRecord:
    source: str            # 数据来源，例如 "hackernews" / "tavily" / "reddit" / "manual"
    title: str              # 标题
    content: str             # 正文/摘要内容
    url: str                # 原文链接
    posted_at: Optional[str] = None    # 发布时间（ISO格式字符串，拿不到就留 None）
    platform_type: str = "job"          # "job"（求职）或 "business"（商机）
    author: Optional[str] = None        # 发布者（可选）

    def to_dict(self):
        return asdict(self)

"""
统一数据结构定义。
不管数据来自 HN、Reddit、Tavily 还是 Instagram/Twitter，抓取回来最终都要转成这套字段。
后面所有的匹配、生成逻辑只认这套字段，不直接依赖任何一个平台的原始格式。

新增字段说明（对接 RapidAPI 社交媒体数据源用）：
- email / followers：只有 Instagram/Twitter 这类社交媒体来源才会有值，
  网页搜索（Tavily/HN/Reddit）来源这两个字段始终是 None。
- 复用已有的 source 字段区分平台（"instagram" / "twitter"），
  不再新增一个 platform 字段，避免和 source 语义重复。
"""

from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class UnifiedRecord:
    source: str            # 数据来源，例如 "hackernews" / "tavily" / "reddit" / "instagram" / "twitter" / "manual"
    title: str              # 标题
    content: str             # 正文/摘要内容
    url: str                # 原文链接
    posted_at: Optional[str] = None    # 发布时间（ISO格式字符串，拿不到就留 None）
    platform_type: str = "job"          # "job"（求职）或 "business"（商机）
    author: Optional[str] = None        # 发布者（可选）
    email: Optional[str] = None         # 新增：从社交媒体 bio 中提取的邮箱（可选，只有IG/Twitter来源才可能有值）
    followers: Optional[int] = None     # 新增：粉丝数（可选，只有IG/Twitter来源才可能有值）
    company: Optional[str] = None       # 新增：公司名（可选，目前只有 remoteok/remotive 这类招聘源会填）
    location: Optional[str] = None      # 新增：地点/候选人所在地要求（可选，同上）
    salary: Optional[str] = None        # 新增：薪资文本（可选，同上）
    remote: Optional[bool] = None       # 新增：是否远程岗位（可选，同上）
    category: Optional[str] = None      # 新增：岗位分类（可选，同上）
    matched_via: Optional[list[str]] = None  # 新增：关键词搜索命中的字段（如 ["title","tags"]），
                                              # 只有 remoteok/remotive 这类做关键词过滤的连接器会填；
                                              # 判断"这条结果靠不靠谱"交给使用者，不在连接器里替它做取舍

    def to_dict(self):
        return asdict(self)
"""
数据连接器抽象基类。

这是整个系统的"唯一通行证"约定：不管以后接入什么数据源
（Hacker News、Tavily、Greenhouse、Lever，或任何未来的 ATS 爬虫），
每一个连接器都必须继承 BaseDataConnector，实现 fetch() 方法，
返回统一格式的 UnifiedRecord 列表。

核心引擎（retriever.py、search_agent.py、resume_generator.py 等）
只认 UnifiedRecord 这个数据结构，不直接依赖任何具体数据源的原始格式。
这样以后新增数据源，只需要新写一个子类，核心处理逻辑一行都不用改。
"""

from abc import ABC, abstractmethod
from src.schema import UnifiedRecord


class BaseDataConnector(ABC):
    """
    所有数据连接器的抽象基类。

    子类必须：
    1. 设置 source_name 类属性（标识这个连接器的数据来源，例如 "hackernews"）
    2. 实现 fetch() 方法，返回 list[UnifiedRecord]
    """

    source_name: str = "unknown"

    @abstractmethod
    def fetch(self, **kwargs) -> list[UnifiedRecord]:
        """
        抓取数据，返回统一格式的记录列表。
        具体参数由子类自行定义（比如关键词、数量限制等），
        但返回值必须严格是 list[UnifiedRecord]，不能返回原始 API 响应。
        """
        raise NotImplementedError
"""
投递适配器的公共接口。
每个平台（LinkedIn / Indeed / 通用官网 ATS）实现一个 Adapter，负责"怎么
打开申请表单"和"怎么判断到了最后一步、submit 按钮是哪个"这些平台特有的
细节；真正的"扫描字段 + 填值"逻辑统一走 field_filler，不在每个 adapter
里重复实现。

所有 adapter 都必须遵守一条规则：fill() 只负责填表，绝不点击提交类按钮。
真正点击提交是 orchestrator.confirm_submit() 在用户确认之后才做的事。
"""

from dataclasses import dataclass
from typing import Optional
from playwright.sync_api import Page, Locator

from src.tab_b_jobsearch.apply.field_filler import FilledField


@dataclass
class OpenResult:
    ok: bool
    message: str = ""
    # LinkedIn/Indeed 这类多步表单实际操作的容器可能是弹窗里的某个 frame/div，
    # 不一定是整个 page；用这个字段告诉后续 fill 该在哪个范围里扫描字段。
    scope: object = None


class ApplyAdapter:
    platform_name = "generic"

    def matches(self, url: str) -> bool:
        raise NotImplementedError

    def open_apply_flow(self, page: Page, job_url: str) -> OpenResult:
        """导航到申请表单页/打开申请弹窗，返回后续填表要用的 scope。"""
        raise NotImplementedError

    def fill(self, scope, profile, job_description: str) -> list[FilledField]:
        """默认实现：直接对 scope 跑通用字段填充引擎。多步表单的 adapter 会覆盖这个方法。"""
        from src.tab_b_jobsearch.apply.field_filler import fill_all
        return fill_all(scope, profile, job_description)

    def locate_submit_button(self, page: Page, scope=None) -> Optional[Locator]:
        """
        找到"最终提交"按钮但不点击。找不到（比如卡在某个中间步骤/需要人工处理的
        字段导致走不到最后一步）就返回 None，调用方会据此把 ready_to_submit 标成 False。
        """
        raise NotImplementedError

"""
通用 ATS 适配器：不认识的招聘网站（公司官网直投、Greenhouse/Lever/Workday/
iCIMS 等主流 ATS）统一走这条路径。这些平台的字段命名习惯五花八门，但基本
都遵循"一个 <form>，里面若干 input/select/textarea，配一个 label"这种
标准 HTML 表单结构，所以交给通用的 field_filler 引擎，不用逐个平台单独适配。

唯一需要平台特判的两件事：
1. 表单可能嵌在 iframe 里（Greenhouse 的官网嵌入式申请表就是这样）——
   挑 input/select/textarea 数量最多的那个 frame 当作填表范围。
2. "提交"按钮怎么找——用文字关键词猜，同时排除"保存草稿"这类容易误命中的按钮。
"""

import re
from typing import Optional
from playwright.sync_api import Page, Locator

from src.tab_b_jobsearch.apply.adapters.base import ApplyAdapter, OpenResult

_APPLY_LINK_RE = re.compile(r"^\s*apply\b", re.I)
_SUBMIT_RE = re.compile(r"submit\s*(your\s*)?application|submit\s*application|apply\s*now|submit\b", re.I)
_EXCLUDE_RE = re.compile(r"save|draft|cancel|back|previous", re.I)


def _pick_richest_frame(page: Page):
    best_frame = page.main_frame
    best_count = -1
    for frame in page.frames:
        try:
            count = frame.locator("input, select, textarea").count()
        except Exception:
            continue
        if count > best_count:
            best_count = count
            best_frame = frame
    return best_frame


class GenericATSAdapter(ApplyAdapter):
    platform_name = "generic_ats"

    def matches(self, url: str) -> bool:
        return True  # 兜底适配器，放在适配器列表最后一个尝试

    def open_apply_flow(self, page: Page, job_url: str) -> OpenResult:
        page.goto(job_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1500)

        # 页面上如果有明显的 "Apply" 按钮/链接（而不是已经在表单页里了），点一下展开表单。
        try:
            apply_locator = page.get_by_role("link", name=_APPLY_LINK_RE).first
            if apply_locator.count() == 0:
                apply_locator = page.get_by_role("button", name=_APPLY_LINK_RE).first
            if apply_locator.count() > 0 and apply_locator.is_visible():
                apply_locator.click(timeout=5000)
                page.wait_for_timeout(1500)
        except Exception:
            pass  # 找不到"Apply"入口按钮就假设当前页已经是表单页

        scope = _pick_richest_frame(page)
        try:
            field_count = scope.locator("input, select, textarea").count()
        except Exception:
            field_count = 0
        if field_count == 0:
            return OpenResult(ok=False, message="页面上没找到任何可填写的表单字段，可能需要手动处理这个申请。")
        return OpenResult(ok=True, scope=scope)

    def locate_submit_button(self, page: Page, scope=None) -> Optional[Locator]:
        scope = scope or page
        candidates = scope.locator("button, input[type=submit]")
        try:
            n = candidates.count()
        except Exception:
            return None
        for i in range(n):
            btn = candidates.nth(i)
            try:
                if not btn.is_visible():
                    continue
                text = (btn.inner_text() if btn.evaluate("el => el.tagName") != "INPUT" else btn.get_attribute("value")) or ""
                text = text.strip()
                if _EXCLUDE_RE.search(text):
                    continue
                if _SUBMIT_RE.search(text):
                    return btn
            except Exception:
                continue
        return None

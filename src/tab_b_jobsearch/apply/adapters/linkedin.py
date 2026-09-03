"""
LinkedIn Easy Apply 适配器。

重要提醒（写在这里方便以后回顾）：LinkedIn 用户协议明确禁止用自动化手段
操作账号，这个适配器有触发账号风控/封号的风险，用之前自己权衡。这也是
为什么整个 apply 模块设计成"填完表单必须人工点确认才真正提交"——
至少能防止"半夜脚本抽风连续帮你投几十份导致账号被封"这种最坏情况，
但没法把风险降到零，具体要不要用、多大频率用，用户自己判断。

Easy Apply 是个多步弹窗（dialog），不是一次性表单：每一步填完点
"Next"/"Continue"/"Review"，最后一步按钮文字会变成"Submit application"。
这个适配器循环着走完前面每一步，走到"Submit application"这一步就停手，
不点它——这个按钮交给 orchestrator 在用户确认后才点。
"""

import re
from typing import Optional
from playwright.sync_api import Page, Locator

from src.tab_b_jobsearch.apply.adapters.base import ApplyAdapter, OpenResult
from src.tab_b_jobsearch.apply.field_filler import fill_all, FilledField

_EASY_APPLY_RE = re.compile(r"easy\s*apply", re.I)
_NEXT_RE = re.compile(r"^(next|continue)$", re.I)
_REVIEW_RE = re.compile(r"^review$", re.I)
_SUBMIT_RE = re.compile(r"submit\s*application", re.I)
_DISMISS_RE = re.compile(r"dismiss|discard", re.I)

MAX_STEPS = 10


class LinkedInAdapter(ApplyAdapter):
    platform_name = "linkedin"

    def __init__(self):
        self._submit_button: Optional[Locator] = None

    def matches(self, url: str) -> bool:
        return "linkedin.com/jobs" in url or "linkedin.com/comm/jobs" in url

    def open_apply_flow(self, page: Page, job_url: str) -> OpenResult:
        page.goto(job_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1500)

        easy_apply_btn = page.get_by_role("button", name=_EASY_APPLY_RE).first
        if easy_apply_btn.count() == 0:
            return OpenResult(
                ok=False,
                message="这个职位没有 'Easy Apply' 按钮（可能要跳到公司官网投递），"
                        "换成通用 ATS 流程处理，或者手动投。",
            )
        easy_apply_btn.click(timeout=8000)
        page.wait_for_timeout(1200)

        dialog = page.get_by_role("dialog").first
        if dialog.count() == 0:
            return OpenResult(ok=False, message="点了 Easy Apply 但没检测到弹窗，页面结构可能变了。")
        return OpenResult(ok=True, scope=dialog)

    def fill(self, scope, profile, job_description: str) -> list[FilledField]:
        all_results: list[FilledField] = []
        seen_labels: set[str] = set()

        for _ in range(MAX_STEPS):
            step_results = fill_all(scope, profile, job_description)
            for r in step_results:
                key = r.label.strip().lower()
                if key and key not in seen_labels:
                    seen_labels.add(key)
                    all_results.append(r)

            footer_buttons = scope.locator("button")
            clicked = False
            n = footer_buttons.count()
            for i in range(n):
                btn = footer_buttons.nth(i)
                try:
                    if not btn.is_visible():
                        continue
                    text = (btn.inner_text() or "").strip()
                except Exception:
                    continue

                if _SUBMIT_RE.search(text):
                    self._submit_button = btn
                    return all_results  # 走到最后一步，停手等用户确认

                if _DISMISS_RE.search(text):
                    continue

                if _REVIEW_RE.match(text) or _NEXT_RE.match(text):
                    try:
                        btn.click(timeout=5000)
                        page_or_dialog = scope
                        page_or_dialog.wait_for_timeout(1000)
                        clicked = True
                        break
                    except Exception:
                        continue

            if not clicked:
                # 走不下去了（可能有个必填字段没填上，Next 按钮被禁用/找不到）
                break

        return all_results

    def locate_submit_button(self, page: Page, scope=None) -> Optional[Locator]:
        return self._submit_button

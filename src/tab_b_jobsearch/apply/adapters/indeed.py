"""
Indeed 一键申请（Easy Apply）适配器。
跟 LinkedIn 类似，也是多步表单，走完前面步骤，停在"Submit your
application"这一步不点。如果这个职位其实是跳转到公司官网投递（不是
Indeed 站内 Easy Apply），open_apply_flow 会返回 ok=False，
orchestrator 会接着尝试通用 ATS 适配器。
"""

import re
from typing import Optional
from playwright.sync_api import Page, Locator

from src.tab_b_jobsearch.apply.adapters.base import ApplyAdapter, OpenResult
from src.tab_b_jobsearch.apply.field_filler import fill_all, FilledField

_APPLY_NOW_RE = re.compile(r"apply\s*now", re.I)
_CONTINUE_RE = re.compile(r"^(continue|next)$", re.I)
_SUBMIT_RE = re.compile(r"submit\s*(your\s*)?application", re.I)

MAX_STEPS = 10


class IndeedAdapter(ApplyAdapter):
    platform_name = "indeed"

    def __init__(self):
        self._submit_button: Optional[Locator] = None

    def matches(self, url: str) -> bool:
        return "indeed.com" in url

    def open_apply_flow(self, page: Page, job_url: str) -> OpenResult:
        page.goto(job_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1500)

        apply_btn = page.get_by_role("button", name=_APPLY_NOW_RE).first
        if apply_btn.count() == 0:
            return OpenResult(ok=False, message="没找到 Indeed 站内一键申请入口，可能是跳转到外部官网投递。")
        apply_btn.click(timeout=8000)
        page.wait_for_timeout(1500)

        # Indeed 的申请流程有时在当前 tab 里，有时开一个新 tab/iframe，两种都兼容一下：
        target_page = page
        if len(page.context.pages) > 1:
            target_page = page.context.pages[-1]
            target_page.wait_for_load_state("domcontentloaded", timeout=15000)

        return OpenResult(ok=True, scope=target_page)

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

            buttons = scope.locator("button")
            clicked = False
            n = buttons.count()
            for i in range(n):
                btn = buttons.nth(i)
                try:
                    if not btn.is_visible():
                        continue
                    text = (btn.inner_text() or "").strip()
                except Exception:
                    continue

                if _SUBMIT_RE.search(text):
                    self._submit_button = btn
                    return all_results

                if _CONTINUE_RE.match(text):
                    try:
                        btn.click(timeout=5000)
                        scope.wait_for_timeout(1000)
                        clicked = True
                        break
                    except Exception:
                        continue

            if not clicked:
                break

        return all_results

    def locate_submit_button(self, page: Page, scope=None) -> Optional[Locator]:
        return self._submit_button

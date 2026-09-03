"""
自动投递的顶层编排逻辑，把 browser / adapters / field_filler / session
串起来，对外只暴露三个动作：
- start_application: 打开职位页 -> 挑一个合适的 adapter -> 填表 -> 停在提交前一步，
  返回一份"我填了什么"的报告 + 截图，交给用户审核。
- confirm_submit: 用户看完报告确认没问题了，才真正点提交按钮。
- cancel: 用户觉得不对，直接关掉这个待确认的浏览器页面，什么都不提交。
"""

import dataclasses
import os
from dataclasses import dataclass, field

from src.tab_b_jobsearch.apply import browser, session as session_store
from src.tab_b_jobsearch.apply.profile import load_profile, ProfileNotConfigured
from src.tab_b_jobsearch.apply.field_filler import FilledField
from src.tab_b_jobsearch.apply.adapters.linkedin import LinkedInAdapter
from src.tab_b_jobsearch.apply.adapters.indeed import IndeedAdapter
from src.tab_b_jobsearch.apply.adapters.generic_ats import GenericATSAdapter

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data")
SCREENSHOT_DIR = os.path.join(DATA_DIR, "apply_sessions")


def _candidate_adapters(job_url: str) -> list:
    all_adapters = [LinkedInAdapter(), IndeedAdapter(), GenericATSAdapter()]
    return [a for a in all_adapters if a.matches(job_url)]


@dataclass
class ApplyDraft:
    session_id: str
    job_id: str
    job_url: str
    platform: str
    filled_fields: list[FilledField]
    screenshot_path: str
    ready_to_submit: bool
    warnings: list[str] = field(default_factory=list)


class ApplyError(Exception):
    pass


def start_application(
    job_id: str,
    job_url: str,
    job_description: str = "",
    resume_path: str | None = None,
) -> ApplyDraft:
    try:
        profile = load_profile()
    except ProfileNotConfigured as e:
        raise ApplyError(str(e)) from e

    if resume_path:
        profile = dataclasses.replace(profile, resume_path=resume_path)
    if not profile.resume_path or not os.path.exists(profile.resume_path):
        raise ApplyError(
            "没有配好可用的简历文件路径（applicant_profile.json 的 resume_path，"
            "或者本次调用单独传的 resume_path）。文件需要真实存在于本机磁盘上。"
        )

    context = browser.get_context()
    page = context.new_page()

    candidates = _candidate_adapters(job_url)
    chosen = None
    open_scope = None
    messages: list[str] = []
    for adapter in candidates:
        try:
            result = adapter.open_apply_flow(page, job_url)
        except Exception as e:
            messages.append(f"{adapter.platform_name}: 打开申请流程时出错 - {e}")
            continue
        if result.ok:
            chosen = adapter
            open_scope = result.scope
            break
        messages.append(f"{adapter.platform_name}: {result.message}")

    if chosen is None:
        page.close()
        raise ApplyError("没能打开这个职位的申请表单。\n" + "\n".join(messages))

    filled_fields = chosen.fill(open_scope, profile, job_description)
    submit_button = chosen.locate_submit_button(page, open_scope)

    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    session_id_placeholder = session_store.create_session(
        page=page,
        adapter=chosen,
        submit_button=submit_button,
        job_id=job_id,
        job_url=job_url,
    )
    screenshot_path = os.path.join(SCREENSHOT_DIR, f"{session_id_placeholder}.png")
    try:
        page.screenshot(path=screenshot_path, full_page=True)
    except Exception:
        screenshot_path = ""

    warnings = [f"「{f.label}」没能自动填上，需要你手动检查/填写" for f in filled_fields if f.source == "manual_required"]
    if submit_button is None:
        warnings.append("没能定位到最终的提交按钮——可能卡在中间某一步，投递前建议先手动看一眼截图/浏览器窗口。")

    return ApplyDraft(
        session_id=session_id_placeholder,
        job_id=job_id,
        job_url=job_url,
        platform=chosen.platform_name,
        filled_fields=filled_fields,
        screenshot_path=screenshot_path,
        ready_to_submit=submit_button is not None,
        warnings=warnings,
    )


def confirm_submit(session_id: str) -> dict:
    session = session_store.pop_session(session_id)
    if session is None:
        raise ApplyError("这个投递会话不存在或者已经过期了（超过 30 分钟没确认会自动失效），重新发起一次投递吧。")

    page = session["page"]
    submit_button = session["submit_button"]
    job_id = session["job_id"]

    if submit_button is None:
        page.close()
        raise ApplyError("这个会话没有定位到可点击的提交按钮，不能提交，先取消重新走一遍。")

    try:
        submit_button.click(timeout=10000)
        page.wait_for_timeout(2000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
    finally:
        page.close()

    return {"success": True, "job_id": job_id}


def cancel(session_id: str) -> dict:
    session = session_store.pop_session(session_id)
    if session is not None:
        try:
            session["page"].close()
        except Exception:
            pass
    return {"success": True}

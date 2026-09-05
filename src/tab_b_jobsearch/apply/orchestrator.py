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
from src.core.resume_by_job_store import get_resume_for_job

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data")
SCREENSHOT_DIR = os.path.join(DATA_DIR, "apply_sessions")


def _candidate_adapters(job_url: str) -> list:
    all_adapters = [LinkedInAdapter(), IndeedAdapter(), GenericATSAdapter()]
    return [a for a in all_adapters if a.matches(job_url)]


# 需要人工交互的验证码标志（hCaptcha / reCAPTCHA v2 复选框或挑战弹窗 / Cloudflare Turnstile）。
# 注意不匹配 reCAPTCHA v3/Enterprise 那个"纯打分、不需要点"的角标——它到处都是，
# 匹配了会把一堆正常页面误判成有验证码。
_CAPTCHA_IFRAME_SELECTORS = [
    'iframe[src*="hcaptcha.com"]',
    'iframe[src*="hcaptcha.net"]',
    'iframe[src*="/recaptcha/api2/bframe"]',
    'iframe[src*="/recaptcha/enterprise/bframe"]',
    'iframe[title="recaptcha challenge expires in two minutes"]',
    'iframe[src*="challenges.cloudflare.com"]',
]
_CAPTCHA_VISIBLE_SELECTORS = [
    'div.g-recaptcha[data-sitekey]',
    'div.h-captcha',
    '#rc-imageselect',
    '#cf-challenge-running',
]


def _scope_has_captcha(scope) -> bool:
    for sel in _CAPTCHA_IFRAME_SELECTORS:
        try:
            if scope.locator(sel).count() > 0:
                return True
        except Exception:
            pass
    for sel in _CAPTCHA_VISIBLE_SELECTORS:
        try:
            el = scope.locator(sel).first
            if el.count() > 0 and el.is_visible():
                return True
        except Exception:
            pass
    return False


def detect_captcha(page) -> bool:
    """
    页面（含所有 iframe）里是否出现了需要人工完成的验证码。
    这条线不碰——检测到就老实停手，绝不尝试识别/绕过。
    """
    scopes = [page]
    try:
        scopes += list(page.frames)
    except Exception:
        pass
    for sc in scopes:
        if _scope_has_captcha(sc):
            return True
    return False


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

    # resume_path 解析优先级：调用方明确传的 > 之前针对这个职位定制/导出过的简历
    # （resume_by_job_store，Tab B 导出时如果带了 job_id 就会记一笔）> profile 里
    # 配的默认简历。都没有才报错——这样自动投递默认就会用"为这条职位量身定制"
    # 的那份，而不是每次都退回一份通用简历。
    resolved_resume_path = resume_path or get_resume_for_job(job_id) or profile.resume_path
    if resolved_resume_path != profile.resume_path:
        profile = dataclasses.replace(profile, resume_path=resolved_resume_path)
    if not profile.resume_path or not os.path.exists(profile.resume_path):
        raise ApplyError(
            "没有配好可用的简历文件路径（本次调用单独传的 resume_path、"
            "针对这个职位定制导出过的简历、applicant_profile.json 里配的默认简历，"
            "三处都没找到有效路径）。可以先在 Tab B 里针对这个职位生成并导出一份定制简历，"
            "或者在 applicant_profile.json 里配一个默认简历路径。文件需要真实存在于本机磁盘上。"
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

    captcha_present = detect_captcha(page)
    filled_fields = chosen.fill(open_scope, profile, job_description)
    captcha_present = captcha_present or detect_captcha(page)
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
    if captcha_present:
        warnings.append(
            "这个页面出现验证码（hCaptcha / reCAPTCHA 等），自动流程不会尝试识别或绕过验证码——"
            "需要你自己在弹出的浏览器窗口里手动完成验证码，再手动走完剩下的提交步骤。"
        )

    return ApplyDraft(
        session_id=session_id_placeholder,
        job_id=job_id,
        job_url=job_url,
        platform=chosen.platform_name,
        filled_fields=filled_fields,
        screenshot_path=screenshot_path,
        ready_to_submit=submit_button is not None and not captcha_present,
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

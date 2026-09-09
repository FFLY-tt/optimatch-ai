"""
自动投递的顶层编排逻辑，把 browser / adapters / field_filler / session
串起来，对外只暴露三个动作：
- start_application: 打开职位页 -> 挑一个合适的 adapter -> 填表 -> 停在提交前一步，
  返回一份"我填了什么"的报告 + 截图，交给用户审核。
- confirm_submit: 用户看完报告确认没问题了，才真正点提交按钮。
- cancel: 用户觉得不对，直接关掉这个待确认的浏览器页面，什么都不提交。
"""

import dataclasses
import logging
import os
import re
import traceback
from dataclasses import dataclass, field
from urllib.parse import urlsplit

log = logging.getLogger("optimatch.apply")

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


# 打开后先做一道"这到底是不是职位申请表单"的健全性检查——挡住 ycombinator.com/apply
# 这种平台通用页 / 登录页 / 加载失败变成 about:blank 的情况，不让浏览器晾成一个
# 用户看不懂的状态，而是明确报错跳过。
_KNOWN_ATS_HOST_HINTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "workday.com",
    "smartrecruiters.com", "workable.com", "bamboohr.com", "icims.com", "jobvite.com",
    "taleo.net", "successfactors", "recruitee.com", "teamtailor.com", "breezy.hr",
    "applytojob.com", "jazz.co", "jobs.jobvite", "myworkdaysite.com", "eightfold.ai",
    "gr8people.com", "phenompeople.com", "avature.net",
)


def _page_host(page) -> str:
    try:
        return (urlsplit(page.url or "").netloc or "").lower()
    except Exception:
        return ""


def _safe_url(page) -> str:
    try:
        return page.url or "(空)"
    except Exception:
        return "(读取失败)"


_LOGIN_PAGE_TEXT_RE = re.compile(
    r"\b(log ?in|sign ?in|sign ?up|create (an )?account|forgot (your )?password|"
    r"continue with (google|github|linkedin|apple)|welcome back)\b", re.IGNORECASE)
_APPLICATION_TEXT_RE = re.compile(
    r"\b(apply for|application for|submit (your )?application|attach (your )?resume|"
    r"upload (your )?(resume|cv)|cover letter|work experience|years of experience|"
    r"why do you want to|equal employment|voluntary self.?identification)\b", re.IGNORECASE)

_PAGE_PROBE_JS = """
() => {
  const all = [...document.querySelectorAll('input, textarea, select')];
  const attr = (el, ...names) => names.map(n => (el.getAttribute(n) || '')).join(' ').toLowerCase();
  const isName = el => {
    const a = attr(el, 'name', 'id', 'aria-label', 'placeholder');
    return /\\bname\\b|first.?name|last.?name|\\bfname\\b|\\blname\\b|full.?name|legal name/.test(a)
        && !/user\\s*name|username|user_name|file|company|display/.test(a);
  };
  const isEmail = el => (el.type === 'email') || /e-?mail/.test(attr(el, 'name', 'id', 'aria-label', 'placeholder'));
  const fillable = all.filter(el => !['hidden','submit','button','reset','image'].includes((el.type || '').toLowerCase()));
  const bodyText = (document.body ? (document.body.innerText || document.body.textContent) : '') || '';
  return {
    fileInputs: document.querySelectorAll('input[type=file]').length,
    passwordInputs: document.querySelectorAll('input[type=password]').length,
    fillable: fillable.length,
    emailLike: fillable.filter(isEmail).length,
    nameLike: fillable.filter(isName).length,
    text: bodyText.slice(0, 6000),
  };
}
"""


def _looks_like_application_page(page, scope) -> tuple[bool, str]:
    """(是不是职位申请表单, 不是的话给个原因)。判不准偏向"不是"，让用户手动处理。"""
    try:
        url = (page.url or "").strip().lower()
    except Exception:
        url = ""
    if not url or url.startswith("about:") or url.startswith("chrome:") or url == "data:,":
        return False, f"页面没有正常加载（当前地址：{page.url or '空'}）"

    probe = None
    for sc in [scope, page]:
        try:
            probe = sc.evaluate(_PAGE_PROBE_JS)
            if probe:
                break
        except Exception:
            continue
    if not probe:
        return False, "读不到页面结构，可能没正常加载"

    text = probe.get("text", "")
    has_apply_words = bool(_APPLICATION_TEXT_RE.search(text))
    has_login_words = bool(_LOGIN_PAGE_TEXT_RE.search(text))
    fillable = probe.get("fillable", 0)
    files = probe.get("fileInputs", 0)
    passwords = probe.get("passwordInputs", 0)
    emails = probe.get("emailLike", 0)
    names = probe.get("nameLike", 0)

    host = _page_host(page)
    has_pii_field = files >= 1 or emails >= 1 or names >= 1
    # 已知 ATS 域名（只匹配 host，不匹配 query 里的 continue=… 之类）+ 至少有一个
    # 姓名/邮箱/简历上传字段。光有可填字段不够——greenhouse 的职位板首页也有
    # "搜索/部门/办公室"这种下拉框，那不是申请表单。
    if has_pii_field and passwords == 0 and any(h in host for h in _KNOWN_ATS_HOST_HINTS):
        return True, ""

    # 强负向：有密码框 = 登录/注册页，不是申请表单（除非同时有简历上传框，那才可能是内嵌登录的申请页）
    if passwords >= 1 and files == 0:
        return False, "打开的是登录 / 注册页（有密码输入框），不是职位申请表单"

    # 强正向：有简历上传框 —— 普通页面 / 登录页几乎不会有 <input type=file>
    if files >= 1:
        return True, ""
    # 正向：真·姓名字段 + 邮箱字段 + 若干字段（典型申请表结构，且已排除了密码框）
    if names >= 1 and emails >= 1 and fillable >= 3:
        return True, ""
    # 正向：邮箱 + 多字段 + 页面文案明确在讲"申请 / 简历"
    if emails >= 1 and fillable >= 4 and has_apply_words:
        return True, ""

    if fillable == 0:
        return False, "页面上没有任何可填写的表单字段"
    if has_login_words and not has_apply_words:
        return False, "打开的更像是登录 / 注册页，不是职位申请表单"
    return False, "页面上没有识别到职位申请表单的特征（姓名 / 邮箱 / 简历上传等）"


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
    job_title: str = "",
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

    log.info("apply.start job_id=%s url=%s", job_id, job_url)
    page = None
    try:
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
                log.warning("apply.open_apply_flow adapter=%s 失败: %s", adapter.platform_name, e)
                messages.append(f"{adapter.platform_name}: 打开申请流程时出错 - {e}")
                continue
            log.info("apply.open_apply_flow adapter=%s ok=%s page_url=%s",
                     adapter.platform_name, result.ok, _safe_url(page))
            if result.ok:
                chosen = adapter
                open_scope = result.scope
                break
            messages.append(f"{adapter.platform_name}: {result.message}")

        if chosen is None:
            raise ApplyError("没能打开这个职位的申请表单。\n" + "\n".join(messages))

        # 健全性检查：确认打开的确实是职位申请表单，不是平台通用页 / 登录页 / 空白页。
        form_ok, form_reason = _looks_like_application_page(page, open_scope)
        log.info("apply.form_sanity ok=%s reason=%s page_url=%s", form_ok, form_reason, _safe_url(page))
        if not form_ok:
            raise ApplyError(
                f"未找到申请表单，已跳过这条职位：{form_reason}。"
                f"这条链接可能不是某个具体职位的申请页（比如是平台首页 / 登录页 / "
                f"孵化器申请页），请点职位标题打开原链接手动确认。"
            )

        captcha_present = detect_captcha(page)
        filled_fields = chosen.fill(open_scope, profile, job_description)
        captcha_present = captcha_present or detect_captcha(page)
        submit_button = chosen.locate_submit_button(page, open_scope)
        log.info("apply.filled fields=%d captcha=%s submit_found=%s",
                 len(filled_fields), captcha_present, submit_button is not None)

        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        session_id_placeholder = session_store.create_session(
            page=page,
            adapter=chosen,
            submit_button=submit_button,
            job_id=job_id,
            job_url=job_url,
            job_title=job_title,
        )
        screenshot_path = os.path.join(SCREENSHOT_DIR, f"{session_id_placeholder}.png")
        try:
            page.screenshot(path=screenshot_path, full_page=True)
        except Exception:
            screenshot_path = ""
        page = None  # 交给 session 托管，不在下面的 finally 里关掉

        warnings = [f"「{f.label}」没能自动填上，需要你手动检查/填写" for f in filled_fields if f.source == "manual_required"]
        if submit_button is None:
            warnings.append("没能定位到最终的提交按钮——可能卡在中间某一步，投递前建议先手动看一眼截图/浏览器窗口。")
        if captcha_present:
            warnings.append(
                "这个页面出现验证码（hCaptcha / reCAPTCHA 等），自动流程不会尝试识别或绕过验证码——"
                "需要你自己在弹出的浏览器窗口里手动完成验证码，再手动走完剩下的提交步骤。"
            )

        log.info("apply.start done session=%s ready=%s warnings=%d",
                 session_id_placeholder, submit_button is not None and not captcha_present, len(warnings))
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
    except ApplyError:
        raise
    except Exception as e:
        # 任何没预料到的错（浏览器启动失败 / 页面超时 / Playwright 跨线程 …）——
        # 统一转成 ApplyError，绝不让原始异常冒泡成 500，也绝不留下没关的页面。
        log.error("apply.start 未预期异常 job_id=%s url=%s:\n%s", job_id, job_url, traceback.format_exc())
        raise ApplyError(f"自动投递启动失败：{type(e).__name__}: {e}") from e
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


def confirm_submit(session_id: str) -> dict:
    session = session_store.pop_session(session_id)
    if session is None:
        raise ApplyError("这个投递会话不存在或者已经过期了（超过 30 分钟没确认会自动失效），重新发起一次投递吧。")

    page = session["page"]
    submit_button = session["submit_button"]
    job_id = session["job_id"]
    log.info("apply.confirm session=%s job_id=%s", session_id, job_id)

    if submit_button is None:
        _safe_close(page)
        raise ApplyError("这个会话没有定位到可点击的提交按钮，不能提交，先取消重新走一遍。")

    try:
        submit_button.click(timeout=10000)
        page.wait_for_timeout(2000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
    except ApplyError:
        raise
    except Exception as e:
        log.error("apply.confirm 点击提交失败 session=%s:\n%s", session_id, traceback.format_exc())
        raise ApplyError(f"点击提交按钮时出错：{type(e).__name__}: {e}") from e
    finally:
        _safe_close(page)

    log.info("apply.confirm done session=%s", session_id)
    return {
        "success": True,
        "job_id": job_id,
        "job_url": session.get("job_url", ""),
        "job_title": session.get("job_title", ""),
    }


def _safe_close(page) -> None:
    try:
        page.close()
    except Exception:
        pass


def cancel(session_id: str) -> dict:
    session = session_store.pop_session(session_id)
    if session is not None:
        try:
            session["page"].close()
        except Exception:
            pass
    return {"success": True}

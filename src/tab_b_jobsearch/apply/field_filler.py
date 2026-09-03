"""
通用表单字段识别与填充引擎。

核心思路：不针对某个网站写死选择器，而是扫描当前页面/弹窗里所有可见的
input/select/textarea，尽量还原出每个字段"给人看的标签文字"（label/
aria-label/placeholder/相邻文本），再用一套关键词规则去猜这个字段对应
profile 里的哪个信息。规则猜不中的开放性问题（"为什么想加入我们"这种），
兜底交给 LLM 生成一个简短、诚实的回答；填不了的字段（比如需要上传的
非简历附件、验证码）明确标成 manual_required，绝不瞎填。

任何时候都不点击"提交"类按钮——填完之后交回调用方（adapter/orchestrator）
决定，等用户在 UI 上确认了才真正提交。
"""

import re
from dataclasses import dataclass
from typing import Optional

from playwright.sync_api import Page, Frame
from src.core.llm_client import chat as llm_chat

ScanScope = Page | Frame

_SCAN_JS = """
() => {
    function labelFor(el) {
        if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
        const labelledBy = el.getAttribute('aria-labelledby');
        if (labelledBy) {
            const parts = labelledBy.split(' ').map(id => {
                const node = document.getElementById(id);
                return node ? node.innerText || node.textContent : '';
            });
            const joined = parts.join(' ').trim();
            if (joined) return joined;
        }
        if (el.id) {
            const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
            if (lab) return (lab.innerText || lab.textContent || '').trim();
        }
        const parentLabel = el.closest('label');
        if (parentLabel) return (parentLabel.innerText || parentLabel.textContent || '').trim();
        // fallback: 往上找最近的、看起来像问题文字的祖先容器里的文本
        let node = el.closest('div, li, fieldset');
        for (let i = 0; i < 3 && node; i++) {
            const clone = node.cloneNode(true);
            clone.querySelectorAll('input, select, textarea, button').forEach(n => n.remove());
            const text = (clone.innerText || clone.textContent || '').trim().split('\\n')[0];
            if (text && text.length < 200) return text;
            node = node.parentElement;
        }
        return el.getAttribute('placeholder') || el.getAttribute('name') || '';
    }

    const nodes = Array.from(document.querySelectorAll('input, select, textarea'));
    const out = [];
    let idx = 0;
    for (const el of nodes) {
        const type = (el.getAttribute('type') || el.tagName.toLowerCase()).toLowerCase();
        if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
        if (el.disabled) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) continue;  // 完全不可见的跳过

        const marker = `optimatch-field-${idx}`;
        el.setAttribute('data-optimatch-idx', String(idx));
        idx += 1;

        let options = null;
        if (el.tagName.toLowerCase() === 'select') {
            options = Array.from(el.options).map(o => (o.textContent || '').trim()).filter(Boolean);
        }

        out.push({
            idx: idx - 1,
            tag: el.tagName.toLowerCase(),
            type: type,
            name: el.getAttribute('name') || '',
            id: el.id || '',
            label: labelFor(el),
            placeholder: el.getAttribute('placeholder') || '',
            required: el.required || el.getAttribute('aria-required') === 'true',
            options: options,
            current_value: el.value || '',
        });
    }
    return out;
}
"""


@dataclass
class FieldMeta:
    idx: int
    tag: str
    type: str
    name: str
    id: str
    label: str
    placeholder: str
    required: bool
    options: Optional[list[str]]
    current_value: str

    @property
    def selector(self) -> str:
        return f'[data-optimatch-idx="{self.idx}"]'

    @property
    def probe_text(self) -> str:
        return f"{self.label} {self.name} {self.placeholder}".strip().lower()


@dataclass
class FilledField:
    label: str
    value: str
    source: str  # "profile" | "llm" | "skipped_has_value" | "manual_required"


def scan_fields(scope: ScanScope) -> list[FieldMeta]:
    raw = scope.evaluate(_SCAN_JS)
    return [FieldMeta(**item) for item in raw]


# (关键词正则, profile属性名或特殊标记) —— 按顺序匹配，第一个命中的生效。
# 特殊标记: "__FULL_NAME__" "__FILE__" "__EEO_GENDER__" 等在 _resolve_special 里处理。
_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"first\s*name|given\s*name"), "first_name"),
    (re.compile(r"last\s*name|family\s*name|surname"), "last_name"),
    (re.compile(r"full\s*name|your\s*name\b"), "__FULL_NAME__"),
    (re.compile(r"e[\-\s]?mail"), "email"),
    (re.compile(r"phone|mobile|telephone"), "phone"),
    (re.compile(r"linkedin"), "linkedin_url"),
    (re.compile(r"github"), "github_url"),
    (re.compile(r"portfolio|personal\s*website|website"), "portfolio_url"),
    (re.compile(r"location|city|current\s*address|where.*based"), "location"),
    (re.compile(r"resume|cv\b"), "__FILE__"),
    (re.compile(r"cover\s*letter"), "__COVER_LETTER__"),
    (re.compile(r"years?\s*of\s*experience"), "__YEARS_EXP__"),
    (re.compile(r"salary|compensation|pay\s*expectation"), "desired_salary"),
    (re.compile(r"sponsor|require.*visa|need.*visa"), "__SPONSORSHIP__"),
    (re.compile(r"authoriz(e|ed|ation)\s*to\s*work|legally\s*eligible"), "__AUTHORIZED__"),
    (re.compile(r"relocat"), "__RELOCATE__"),
    (re.compile(r"remote"), "__REMOTE__"),
    (re.compile(r"gender"), "eeo_gender"),
    (re.compile(r"race|ethnicity"), "eeo_race"),
    (re.compile(r"veteran"), "eeo_veteran"),
    (re.compile(r"disab"), "eeo_disability"),
    (re.compile(r"how did you hear|referr"), "__HEARD_ABOUT__"),
]


def _resolve_special(marker: str, profile, job_description: str, field: FieldMeta) -> Optional[str]:
    if marker == "__FULL_NAME__":
        return profile.full_name
    if marker == "__FILE__":
        return profile.resume_path or None
    if marker == "__COVER_LETTER__":
        if profile.cover_letter_template:
            return profile.cover_letter_template
        return None  # 没配模板就不瞎写，交给 LLM 兜底逻辑处理
    if marker == "__YEARS_EXP__":
        return str(profile.years_experience) if profile.years_experience is not None else None
    if marker == "__SPONSORSHIP__":
        needs = profile.work_authorization == "needs_sponsorship"
        return _yes_no(field, needs)
    if marker == "__AUTHORIZED__":
        authorized = profile.work_authorization in ("authorized",)
        return _yes_no(field, authorized)
    if marker == "__RELOCATE__":
        return _yes_no(field, profile.willing_to_relocate)
    if marker == "__REMOTE__":
        return _yes_no(field, profile.willing_to_remote)
    if marker == "__HEARD_ABOUT__":
        return "Online job board"
    return None


def _yes_no(field: FieldMeta, value: bool) -> str:
    if field.options:
        target = "yes" if value else "no"
        for opt in field.options:
            if opt.strip().lower().startswith(target):
                return opt
        return field.options[0]
    return "Yes" if value else "No"


def _match_extra_answers(field: FieldMeta, profile) -> Optional[str]:
    probe = field.probe_text
    for keyword, answer in (profile.extra_answers or {}).items():
        if keyword.lower() in probe:
            return answer
    return None


def _llm_fallback(field: FieldMeta, profile, job_description: str) -> Optional[str]:
    """
    只对开放式文本题（textarea 或没有可选项的普通 text input）用 LLM 兜底，
    绝不用来编造事实类字段（薪资/工作授权这些必须走规则或明确留空）。
    """
    if field.tag not in ("textarea",) and field.type not in ("text",):
        return None
    if not field.label or len(field.label) < 4:
        return None

    system = (
        "You are helping a real job applicant answer a short application-form question. "
        "Answer in first person, concise (2-4 sentences unless the question clearly wants one line), "
        "honest, and grounded only in the candidate background given below. "
        "If you cannot answer honestly from the given background, respond with exactly: SKIP"
    )
    prompt = (
        f"Candidate background:\nName: {profile.full_name}\n"
        f"Years of experience: {profile.years_experience}\n"
        f"Job description (may be empty):\n{job_description[:1500]}\n\n"
        f"Application form question:\n{field.label}"
    )
    try:
        answer = llm_chat(prompt, system=system, temperature=0.4)
    except Exception:
        return None
    answer = (answer or "").strip()
    if not answer or answer.upper() == "SKIP":
        return None
    return answer


def match_field(field: FieldMeta, profile, job_description: str = "") -> tuple[Optional[str], str]:
    """返回 (要填的值, 来源标记)；值为 None 表示这个字段建议留给用户手动处理。"""
    if field.current_value.strip():
        return None, "skipped_has_value"

    probe = field.probe_text
    for pattern, target in _RULES:
        if pattern.search(probe):
            if target.startswith("__"):
                value = _resolve_special(target, profile, job_description, field)
            else:
                value = getattr(profile, target, None)
            if value:
                return str(value), "profile"
            break  # 命中规则但没数据，不再往下试其他规则，直接走 extra/llm 兜底

    extra = _match_extra_answers(field, profile)
    if extra:
        return extra, "profile"

    llm_answer = _llm_fallback(field, profile, job_description)
    if llm_answer:
        return llm_answer, "llm"

    return None, "manual_required"


def fill_field(scope: ScanScope, field: FieldMeta, value: str) -> None:
    locator = scope.locator(field.selector)
    if field.type == "file":
        locator.set_input_files(value)
    elif field.tag == "select":
        try:
            locator.select_option(label=value)
        except Exception:
            locator.select_option(value)
    elif field.type in ("checkbox", "radio"):
        if value.lower() in ("yes", "true", "1"):
            locator.check()
    else:
        locator.fill(value)


def fill_all(scope: ScanScope, profile, job_description: str = "") -> list[FilledField]:
    """扫描当前 scope 里的所有字段，能填的填上，返回完整的填写报告给用户审核。"""
    results: list[FilledField] = []
    for field in scan_fields(scope):
        value, source = match_field(field, profile, job_description)
        if value is None:
            results.append(FilledField(label=field.label or field.name or f"field#{field.idx}", value="", source=source))
            continue
        try:
            fill_field(scope, field, value)
        except Exception as e:
            results.append(FilledField(label=field.label or field.name, value=f"(填写失败: {e})", source="manual_required"))
            continue
        results.append(FilledField(label=field.label or field.name or f"field#{field.idx}", value=value, source=source))
    return results

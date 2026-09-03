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

    // 同一道单选题的题干：一组 name 相同的 radio 共用。
    // 难点：题干和选项常常不在同一层——选项在 <ul><li><label><input>Yes</label>，
    // 题干在这个 ul 的兄弟节点里。所以先定位"包含 >=2 个同 name radio"的祖先，
    // 再从那儿继续往上，逐层"去掉包着表单控件的 <label>（=选项）"后取剩下的实义文字。
    function groupLabelFor(el) {
        const name = el.getAttribute('name');
        if (!name) return '';
        const fs = el.closest('fieldset');
        if (fs) {
            const lg = fs.querySelector('legend');
            const t = lg && (lg.innerText || lg.textContent || '').trim();
            if (t) return t;
        }
        let base = el.parentElement, holder = null;
        for (let i = 0; i < 8 && base; i++) {
            let c = 0;
            try { c = base.querySelectorAll(`input[name="${CSS.escape(name)}"]`).length; } catch (e) {}
            if (c >= 2) { holder = base; break; }
            base = base.parentElement;
        }
        if (!holder) return '';
        let node = holder;
        for (let i = 0; i < 5 && node; i++) {
            const clone = node.cloneNode(true);
            // 包着 input 的 <label> 是选项本身，去掉；孤立的 <label>（题干用的）留着
            clone.querySelectorAll('label').forEach(n => {
                if (n.querySelector('input, select, textarea')) n.remove();
            });
            clone.querySelectorAll('input, select, textarea, button, legend').forEach(n => n.remove());
            const txt = (clone.textContent || '').replace(/\\s+/g, ' ').trim();
            if (txt && txt.length >= 4 && txt.length < 300) return txt;
            node = node.parentElement;
        }
        return '';
    }

    const nodes = Array.from(document.querySelectorAll('input, select, textarea'));
    const out = [];
    let idx = 0;
    for (const el of nodes) {
        const type = (el.getAttribute('type') || el.tagName.toLowerCase()).toLowerCase();
        if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
        // type="input" 是非法值：一些 ATS（Greenhouse 的 react-select）会塞一个
        // 这样的透明输入框专门用来触发 HTML5 必填校验提示，它不是真正要填的字段，跳过。
        if (type === 'input') continue;
        if (el.getAttribute('aria-hidden') === 'true') continue;
        if (el.disabled) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) continue;  // 完全不可见的跳过

        const marker = `optimatch-field-${idx}`;
        el.setAttribute('data-optimatch-idx', String(idx));
        idx += 1;

        const role = (el.getAttribute('role') || '').toLowerCase();
        const cls = el.getAttribute('class') || '';
        // react-select 这类组件：真正的 <select> 被替换成一个 role=combobox 的搜索框，
        // 直接 fill() 只会往搜索框打字、不会选中任何选项，得单独处理（点开 -> 打字 -> 选 option）。
        const isCombobox = role === 'combobox'
            || el.getAttribute('aria-autocomplete') === 'list'
            || /\bselect__input\b/.test(cls);

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
            is_combobox: isCombobox,
            // radio/checkbox 用 checked 判断"是否已选"，不能用 value（value 属性恒非空）。
            checked: !!el.checked,
            group_label: type === 'radio' ? groupLabelFor(el) : '',
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
    is_combobox: bool = False
    checked: bool = False
    group_label: str = ""
    # 下面两个是扫描后合并 radio 组时才填的：一组同 name 的 radio 会合成一个
    # tag="radiogroup" 的伪字段，options 是每个 radio 的文案，radio_markers 是每个
    # radio 对应的 data-optimatch-idx，选中哪个就对那个具体 radio 调 .check()。
    radio_markers: Optional[list[int]] = None

    @property
    def selector(self) -> str:
        return f'[data-optimatch-idx="{self.idx}"]'

    @property
    def probe_text(self) -> str:
        # id 也算进去：很多 ATS 的文件上传框/下拉框对人可见的标签是 "Attach"/"Select..."
        # 这种没信息量的字，真正能对上 profile 的线索在 id/name 里（Greenhouse: id="resume"）。
        return f"{self.label} {self.name} {self.id} {self.placeholder}".strip().lower()


@dataclass
class FilledField:
    label: str
    value: str
    source: str  # "profile" | "llm" | "skipped_has_value" | "manual_required"


def _group_radios(fields: list[FieldMeta]) -> list[FieldMeta]:
    """
    把 name 相同的一组 radio 合并成一个 tag="radiogroup" 的伪字段——一组 radio
    其实是同一道单选题，应该"按语义从选项里挑一个"，而不是一个个独立处理。
    单独的 radio（name 唯一或缺失）和 checkbox 原样保留。
    """
    by_name: dict[str, list[FieldMeta]] = {}
    for f in fields:
        if f.type == "radio" and f.name:
            by_name.setdefault(f.name, []).append(f)

    out: list[FieldMeta] = []
    done: set[str] = set()
    for f in fields:
        group = by_name.get(f.name) if (f.type == "radio" and f.name) else None
        if group and len(group) >= 2:
            if f.name in done:
                continue
            done.add(f.name)
            options = [g.label.strip() for g in group]
            question = next((g.group_label for g in group if g.group_label), "") or f.name
            out.append(FieldMeta(
                idx=group[0].idx,
                tag="radiogroup",
                type="radio",
                name=f.name,
                id="",
                label=question,
                placeholder="",
                required=any(g.required for g in group),
                options=options,
                current_value="",
                checked=any(g.checked for g in group),
                group_label=question,
                radio_markers=[g.idx for g in group],
            ))
        else:
            out.append(f)
    return out


def scan_fields(scope: ScanScope) -> list[FieldMeta]:
    raw = scope.evaluate(_SCAN_JS)
    return _group_radios([FieldMeta(**item) for item in raw])


# (关键词正则, profile属性名或特殊标记) —— 按顺序匹配，第一个命中的生效。
# 特殊标记: "__FULL_NAME__" "__FILE__" "__EEO_GENDER__" 等在 _resolve_special 里处理。
_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"first\s*name|given\s*name"), "first_name"),
    (re.compile(r"last\s*name|family\s*name|surname"), "last_name"),
    (re.compile(r"full\s*name|your\s*name\b"), "__FULL_NAME__"),
    (re.compile(r"e[\-\s]?mail"), "email"),
    # "phone" 要卡紧一点：早先写成 |mobile| 会误命中 "Associate Mobile Engineer" 这种。
    (re.compile(r"\bphone\b|telephone|mobile\s*(phone|number|no|#)|\bcell\s*phone\b"), "phone"),
    # "如果有人内推，填写推荐人姓名"——这不是"你从哪知道我们"，别拿 __HEARD_ABOUT__ 去填，
    # 也别让 LLM 瞎编一个名字，直接留给用户手填。
    (re.compile(r"\brefer(red|ral|rer)\b.{0,24}\bname\b|\bname\b.{0,24}\brefer(red|ral|rer)\b"), "__SKIP__"),
    # "如果你选了 Other / 如果是 / 如果否，请说明……" 这类条件字段：它填不填取决于另一道题的
    # 答案，自动流程不一定选中了那个前置选项，别瞎填，留给用户。
    (re.compile(r"^\s*if\s+(you\s+)?(selected|answered|chose|checked|yes\b|no\b|other\b|applicable|the\s+above)"), "__SKIP__"),
    (re.compile(r"linkedin"), "linkedin_url"),
    (re.compile(r"github"), "github_url"),
    (re.compile(r"portfolio|personal\s*website|website"), "portfolio_url"),
    (re.compile(r"\bcountry\b"), "__COUNTRY__"),
    (re.compile(r"location|city|current\s*address|where.*based"), "location"),
    (re.compile(r"current\s*(employer|company)|present\s*(employer|company)|"
               r"(current|present|most\s*recent)\b.{0,30}\b(employer|company)|"
               r"employer\s*name|name\s*of\s*(your\s*)?(current\s*)?(company|employer)"), "current_company"),
    (re.compile(r"notice\s*period|when\s*can\s*you\s*start|earliest\s*(possible\s*)?(start|availability)|"
               r"available\s*(to\s*)?start\s*date|availability\s*(to\s*start|date)|start\s*date\s*availability"), "notice_period"),
    (re.compile(r"highest\s*(level\s*of\s*)?education|education\s*level|level\s*of\s*education|"
               r"highest\s*(degree|qualification)|degree\s*(level|obtained|earned|completed|type)|"
               r"^\s*degree\b"), "highest_education"),
    (re.compile(r"\bschool\b|\buniversity\b|\bcollege\b|alma\s*mater|institution\s*name|"
               r"which\s*(school|university|college)|name\s*of\s*(your\s*)?(school|university|college|institution)"), "school"),
    (re.compile(r"resume|cv\b|curriculum\s*vitae"), "__FILE__"),
    (re.compile(r"cover[\s_-]*letter"), "__COVER_LETTER__"),
    (re.compile(r"years?\s*of\s*experience"), "__YEARS_EXP__"),
    (re.compile(r"salary|compensation|pay\s*expectation"), "desired_salary"),
    (re.compile(r"sponsor|require.*visa|need.*visa"), "__SPONSORSHIP__"),
    (re.compile(r"authoriz(e|ed|ation)\s*to\s*work|legally\s*eligible|"
               r"(unrestricted\s*)?right\s*to\s*work|eligible\s*to\s*work|legally\s*(authorized|permitted)"), "__AUTHORIZED__"),
    (re.compile(r"relocat"), "__RELOCATE__"),
    (re.compile(r"remote"), "__REMOTE__"),
    (re.compile(r"willing\s*to\s*travel|able\s*to\s*travel|comfortable\s*(with\s*)?travel|"
               r"travel\s*(requirement|required|up\s*to|\d+\s*%)|ok\s*with\s*travel"), "__TRAVEL__"),
    (re.compile(r"gender"), "eeo_gender"),
    (re.compile(r"transgender"), "__EEO_DECLINE__"),
    (re.compile(r"sexual\s*orientation"), "__EEO_DECLINE__"),
    (re.compile(r"race|ethnic"), "eeo_race"),
    (re.compile(r"veteran|military\s*service"), "eeo_veteran"),
    (re.compile(r"disab"), "eeo_disability"),
    (re.compile(r"how\s*did\s*you\s*hear|hear\s*about\s*(us|the|this|our)|referral\s*source"), "__HEARD_ABOUT__"),
    # "我已阅读并同意隐私政策/条款"——不同意就没法投递，属于申请流程的必要前提，命中就选"同意"。
    # 要求措辞本身是"我同意/我确认/同意条款"，不匹配"Yes, X can contact me（营销订阅）"或
    # 光有个 "Privacy policy" 链接文字的场景——那种可选项不该自动替用户勾。
    (re.compile(r"\bi\s*(agree|consent|acknowledge)\b|"
               r"(agree|consent|accept)\s*(to\s*)?(the\s*)?(terms|privacy|conditions|data\s*processing)|"
               r"have\s*read\s*and\s*(agree|accept|understand)|"
               r"terms\s*(and|&)\s*conditions|"
               r"\backnowledge\b.{0,40}\b(agree|confirm)\b|\bconfirm\b.{0,20}\bagree\b"), "__AGREE__"),
]


def _resolve_special(marker: str, profile, job_description: str, field: FieldMeta) -> Optional[str]:
    if marker == "__FULL_NAME__":
        return profile.full_name
    if marker == "__FILE__":
        # 只有真正的文件上传框才塞简历路径。像"想在简历之外补充点什么吗？"这种
        # 开放问答题的题干里也带"resume"字样，绝不能把文件路径填进那种文本框。
        if field.type != "file":
            return None
        return profile.resume_path or None
    if marker == "__COUNTRY__":
        # profile 里没有单独的 country 字段，从 location 末段取（"Toronto, ON, Canada" -> "Canada"）
        parts = [p.strip() for p in (profile.location or "").split(",") if p.strip()]
        return parts[-1] if parts else None
    if marker == "__EEO_DECLINE__":
        return "Decline to answer"  # 实际选项文案千差万别，_fill_combobox 会兜底匹配"不透露"类选项
    if marker == "__AGREE__":
        return "I agree"
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
    if marker == "__TRAVEL__":
        return _yes_no(field, getattr(profile, "willing_to_travel", False))
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


# LLM 兜底回答里如果出现这类"拒答/敷衍"话术，就当它没答，绝不把这种话真填进表单。
_REFUSAL_RE = re.compile(
    r"\bas an ai\b|\bi am an ai\b|language model|"
    r"\bi\s*(can\s*not|cannot|can't|am\s*(un)?able\s*to|won'?t|will\s*not)\s*(answer|help|assist|provide|respond)|"
    r"\bi\s*(do\s*not|don'?t)\s*(have|know|wish|want)\b.{0,40}\b(answer|information|context|enough|respond)|"
    r"\b(unable|not\s*able)\s*to\s*(answer|determine|provide|respond)|"
    r"\bnot\s*enough\s*(information|context|background|detail)|"
    r"\bcannot\s*answer\s*(this|that|honestly)|"
    r"\bprefer\s*not\s*to\s*(say|answer)|"
    r"\bi\s*(don'?t|do\s*not)\s*wish\s*to\s*answer|"
    r"我(无法|不能|没办法|不方便)(回答|提供|作答)|无法根据|信息不足|抱歉[，,]?\s*我",
    re.I,
)


def _resume_context(max_chars: int = 2400) -> str:
    """
    从向量库里取真实简历片段拼成一段背景，喂给 LLM 兜底生成——之前只传姓名 + 工作年限，
    LLM 只能写空话。用户没上传过简历（collection 为空）时返回 ""。
    延迟 import：vector_store 会连带 import sentence_transformers，比较重，别在模块加载时就拖进来。
    """
    try:
        from src.core.vector_store import get_all_resume_chunks
        chunks = get_all_resume_chunks()
    except Exception:
        return ""
    parts, total = [], 0
    for c in chunks:
        t = (c.get("text") or "").strip()
        if not t:
            continue
        if total + len(t) + 2 > max_chars:
            break
        parts.append(f"- {t}")
        total += len(t) + 2
    return "\n".join(parts)


def _llm_fallback(field: FieldMeta, profile, job_description: str) -> Optional[str]:
    """
    只对开放式文本题（textarea 或没有可选项的普通 text input）用 LLM 兜底，
    绝不用来编造事实类字段（薪资/工作授权这些必须走规则或明确留空）。
    """
    if field.is_combobox or field.tag == "radiogroup":
        return None  # 下拉/单选题只能选给定选项，不能让 LLM 自由发挥
    if field.tag not in ("textarea",) and field.type not in ("text",):
        return None
    if not field.label or len(field.label) < 4:
        return None

    resume_context = _resume_context()

    system = (
        "You are helping a real job applicant answer a short application-form question. "
        "Answer in first person, concise (2-4 sentences unless the question clearly wants one line), "
        "honest, and grounded ONLY in the candidate background (resume excerpts) given below. "
        "Do NOT invent employers, titles, dates, schools, or skills that are not in the excerpts. "
        "If the background does not contain enough to answer the question honestly, "
        "respond with exactly: SKIP"
    )
    prompt = (
        f"Candidate name: {profile.full_name}\n"
        f"Years of experience: {profile.years_experience}\n"
        f"Resume excerpts (the ONLY facts you may use):\n"
        f"{resume_context or '(no resume on file)'}\n\n"
        f"Job description (may be empty):\n{job_description[:1500]}\n\n"
        f"Application form question:\n{field.label}"
    )
    try:
        answer = llm_chat(prompt, system=system, temperature=0.4)
    except Exception:
        return None
    answer = (answer or "").strip()
    if not answer or answer.upper() == "SKIP" or answer.upper().endswith("SKIP"):
        return None
    # 正则保险：LLM 没老老实实回 SKIP，而是写了一段"我无法回答/作为 AI……"，也当跳过。
    if _REFUSAL_RE.search(answer):
        return None
    return answer


def match_field(field: FieldMeta, profile, job_description: str = "") -> tuple[Optional[str], str]:
    """返回 (要填的值, 来源标记)；值为 None 表示这个字段建议留给用户手动处理。"""
    # radio/checkbox/radiogroup 看 checked 判断"是否已选"——它们的 value 属性恒非空，
    # 不能拿 value 当"已填"的依据（这正是之前 radio 组永远不会被填的根因）。
    if field.type in ("checkbox", "radio") or field.tag == "radiogroup":
        if field.checked:
            return None, "skipped_has_value"
    elif field.current_value.strip():
        return None, "skipped_has_value"

    probe = field.probe_text
    for pattern, target in _RULES:
        if pattern.search(probe):
            if target == "__SKIP__":
                return None, "manual_required"  # 命中"别自动填"规则，直接留给用户
            if target.startswith("__"):
                value = _resolve_special(target, profile, job_description, field)
            else:
                value = getattr(profile, target, None)
            if value:
                return str(value), "profile"
            break  # 命中规则但没数据，不再往下试其他规则，直接走 extra/llm 兜底

    # 文件上传框规则没命中时的兜底：ATS 里对人可见的标签常常只是 "Attach"/"Upload"，
    # 关键词规则对不上。默认按简历处理，但只在"标签像简历"或"这个附件是必填"时才填，
    # 免得往 Lever 那种额外的"Upload file"附加附件框里也塞一份简历。
    if field.type == "file":
        if re.search(r"cover[\s_-]*letter|portfolio|transcript|writing\s*sample", probe):
            return None, "manual_required"
        looks_like_resume = re.search(r"resume|cv\b|curriculum\s*vitae", probe)
        if profile.resume_path and looks_like_resume:
            return profile.resume_path, "profile"
        return None, "manual_required"

    extra = _match_extra_answers(field, profile)
    if extra:
        return extra, "profile"

    llm_answer = _llm_fallback(field, profile, job_description)
    if llm_answer:
        return llm_answer, "llm"

    return None, "manual_required"


def _open_menu_options(scope: ScanScope, locator):
    """
    定位这个下拉当前实际展开的那个 option 列表。
    不能直接全局抓 [role="option"]——页面上常年挂着一些 display:none 的隐藏列表
    （比如 intl-tel-input 的两百多个国家区号项），会误命中并卡 30s 超时。
    优先用 aria-controls/aria-owns 指到的 listbox，再退回到"可见的 option"。
    """
    listbox_id = None
    for attr in ("aria-controls", "aria-owns"):
        try:
            listbox_id = locator.get_attribute(attr, timeout=1000)
        except Exception:
            listbox_id = None
        if listbox_id:
            break
    if listbox_id:
        scoped = scope.locator(f'#{listbox_id} [role="option"]:visible')
        try:
            if scoped.count() > 0:
                return scoped
        except Exception:
            pass
    return scope.locator('[role="option"]:visible')


# 人口统计类（EEO/自愿披露）问题：选项文案各家不一样，profile 里的默认值多半对不上，
# 统一兜底成"不透露"这一类选项，跟 profile.py 里 EEO 字段"默认给安全答案"的设计一致。
_DEMOGRAPHIC_RE = re.compile(r"gender|race|ethnic|veteran|disab|transgender|sexual\s*orientation|military", re.I)
_DECLINE_OPT_RE = re.compile(
    r"don'?t wish|do not wish|prefer not|decline|not to answer|do not want|don'?t want|"
    r"no military service|not a (protected )?veteran|choose not",
    re.I,
)


def _keyboard(scope: ScanScope):
    kb = getattr(scope, "keyboard", None)
    if kb is not None:
        return kb
    page = getattr(scope, "page", None)
    return getattr(page, "keyboard", None)


def _dismiss_menus(scope: ScanScope) -> None:
    """按 Esc 关掉可能还开着的下拉菜单——不然它会浮在下一个字段上面挡住点击。"""
    kb = _keyboard(scope)
    if kb is None:
        return
    try:
        kb.press("Escape")
        scope.wait_for_timeout(120)
    except Exception:
        pass


_PLACEHOLDER_OPT_RE = re.compile(r"^\s*(select|choose|please\s+select|--)\b|^\s*\.*\s*$", re.I)


def _pick_option(value: str, option_texts: list[str], demographic: bool) -> Optional[int]:
    """
    从一组下拉选项文字里挑出最该选的那个，返回下标；挑不出返回 None（宁可留空让用户手填，
    也不乱选）。native <select> 和 react-select 共用这套匹配逻辑。
    """
    want = value.strip().lower()
    want_tokens = [w for w in re.split(r"[^a-z0-9]+", want) if len(w) > 2]

    def usable(i: int) -> bool:
        return bool(option_texts[i].strip()) and not _PLACEHOLDER_OPT_RE.match(option_texts[i])

    for i, t in enumerate(option_texts):                    # 完全相等
        if usable(i) and t.strip().lower() == want:
            return i
    for i, t in enumerate(option_texts):                    # 包含关系
        tl = t.strip().lower()
        if usable(i) and want and (want in tl or tl in want):
            return i
    if want in ("i agree", "yes", "i consent", "i acknowledge"):
        # "同意条款"这类题的选项文案可能是 "I acknowledge and agree" / "Agree" / "Yes, I agree"
        for i, t in enumerate(option_texts):
            if usable(i) and re.search(r"\b(agree|acknowledge|consent|accept)\b|^\s*yes\b", t, re.I):
                return i
    if demographic:
        # 人口统计题：选项文案各家不同，profile 默认值多半对不上，直接兜底选"不透露"类。
        # 这里绝不做模糊词元匹配——"I am not a protected veteran" 会被匹配成
        # "Other Protected Veteran" 这种语义相反的选项。
        for i, t in enumerate(option_texts):
            if usable(i) and _DECLINE_OPT_RE.search(t):
                return i
        return None
    if len(want_tokens) >= 2:                               # 词元重叠（"Toronto, ON, Canada" -> "Toronto, Ontario, Canada"）
        best_i, best_score = None, 0.0
        for i, t in enumerate(option_texts):
            if not usable(i):
                continue
            toks = set(re.split(r"[^a-z0-9]+", t.lower()))
            score = sum(1 for w in want_tokens if w in toks) / len(want_tokens)
            if score > best_score:
                best_i, best_score = i, score
        # 门槛卡高一点：0.667（3 选 2 命中）会把 "online job board" 错配成 "University Job Board"。
        if best_score >= 0.75:
            return best_i
    return None


def _read_options(scope: ScanScope, locator) -> tuple[object, list[str]]:
    """轮询等下拉的 listbox 出来，返回 (options locator, 每个 option 的文字)。"""
    for _ in range(10):
        scope.wait_for_timeout(200)
        opts = _open_menu_options(scope, locator)
        try:
            n = opts.count()
        except Exception:
            n = 0
        if n > 0:
            texts = []
            for i in range(min(n, 80)):
                try:
                    texts.append((opts.nth(i).inner_text() or "").strip())
                except Exception:
                    texts.append("")
            return opts, texts
    return None, []


def _fill_combobox(scope: ScanScope, field: FieldMeta, locator, value: str) -> Optional[str]:
    """
    react-select / 自定义下拉：直接 fill() 只是往内部搜索框打字、不会真的选中。
    要 点开 -> 从 option 列表里挑一个 -> 点它。
    注意别急着打字：打字会按输入过滤选项，想填的值要是跟任何选项都不沾边
    （EEO 题很常见），列表会被过滤到空，反而选不了。所以先看整张列表，
    只有整张列表里找不到、且列表很长时才靠打字去筛。
    返回实际选中的那个选项的文字；没能选中返回 None（调用方据此标 manual_required）。
    """
    _dismiss_menus(scope)   # 先关掉上一个下拉可能还开着的菜单
    try:
        locator.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    try:
        locator.click(timeout=5000)
    except Exception:
        _dismiss_menus(scope)
        locator.click(timeout=5000)

    demographic = bool(_DEMOGRAPHIC_RE.search(field.probe_text))

    def _choose(texts: list[str]) -> Optional[int]:
        return _pick_option(value, texts, demographic)

    options, texts = _read_options(scope, locator)
    target = _choose(texts) if texts else None

    if target is None:
        # 整张列表里没找到——可能是长列表（国家）只渲染了一部分，或异步下拉要先打字才出选项。
        # 逐步缩短查询词再试（"Toronto, ON, Canada" -> "Toronto"）。
        queries = [value]
        head = value.split(",")[0].strip()
        if head and head != value:
            queries.append(head)
        for q in queries:
            try:
                locator.fill("")
                locator.press_sequentially(q, delay=20)
            except Exception:
                locator.fill(q)
            options, texts = _read_options(scope, locator)
            target = _choose(texts) if texts else None
            if target is not None:
                break

    if target is None:
        try:
            locator.press("Escape")   # 不乱选，留给用户手填
        except Exception:
            pass
        return None

    chosen_text = texts[target].strip() if target < len(texts) else value
    try:
        options.nth(target).click(timeout=5000)
    except Exception:
        _dismiss_menus(scope)
        return None
    scope.wait_for_timeout(150)
    _dismiss_menus(scope)   # 选完把菜单关掉，别挡住后面的字段
    return chosen_text or value


def _fill_radiogroup(scope: ScanScope, field: FieldMeta, value: str) -> Optional[str]:
    """一组同 name 的 radio：用选下拉那套语义匹配挑一个选项，再对那个具体 radio 调 .check()。"""
    opts = field.options or []
    markers = field.radio_markers or []
    if not opts or len(opts) != len(markers):
        return None
    demographic = bool(_DEMOGRAPHIC_RE.search(field.probe_text))
    idx = _pick_option(value, opts, demographic)
    if idx is None:
        return None
    loc = scope.locator(f'[data-optimatch-idx="{markers[idx]}"]')
    try:
        loc.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass

    def _checked() -> Optional[bool]:
        try:
            return loc.is_checked()
        except Exception:
            return None

    # 多种点法轮着试，每次都核实 is_checked，别谎报：
    #   check      —— 正常路径
    #   label      —— 很多站把真正的 input 用 CSS 藏了，视觉上的圆点是外层 <label>
    #   dom_click  —— 直接对 input 元素调 DOM 的 .click()，绕过坐标命中检测。用于
    #                 "元素本身可交互，但被一个透明浮层盖住"的情况（比如页面上有
    #                 hCaptcha 的全屏 enclave iframe）。这只是点这个 radio，不碰验证码。
    verifiable = False
    for how in ("check", "label", "dom_click"):
        try:
            if how == "check":
                loc.check(timeout=3500)
            elif how == "label":
                loc.locator("xpath=ancestor-or-self::label[1]").click(timeout=2500)
            else:
                loc.evaluate("el => el.click()")
        except Exception:
            pass
        st = _checked()
        if st is True:
            return opts[idx]
        if st is False:
            verifiable = True
    # 三种点法都没让 is_checked 变 True：如果状态本来就核实不了（detached 等），
    # 姑且信它点上了；如果明确核实到"没选中"，如实返回 None。
    return None if verifiable else opts[idx]


def fill_field(scope: ScanScope, field: FieldMeta, value: str) -> Optional[str]:
    """返回实际写进表单的值（下拉返回真正选中的那个选项文字）；None 表示没填成，调用方标 manual_required。"""
    if field.tag == "radiogroup":
        return _fill_radiogroup(scope, field, value)
    locator = scope.locator(field.selector)
    if field.type == "file":
        locator.set_input_files(value)
        return value
    if field.is_combobox:
        return _fill_combobox(scope, field, locator, value)
    if field.tag == "select":
        opts = field.options or []
        demographic = bool(_DEMOGRAPHIC_RE.search(field.probe_text))
        idx = _pick_option(value, opts, demographic)
        if idx is None:
            return None
        for kw in ({"label": opts[idx]}, {"value": opts[idx]}):
            try:
                locator.select_option(**kw, timeout=4000)
                return opts[idx]
            except Exception:
                continue
        try:
            locator.select_option(index=idx, timeout=4000)
            return opts[idx]
        except Exception:
            return None
    if field.type in ("checkbox", "radio"):
        # 只有"勾选"语义的值才动它；像 email 规则误命中"...marked the sender address as safe"
        # 这种把一串文字塞给复选框的情况，如实标 manual，不谎报成已填。
        if value.strip().lower() not in ("yes", "true", "1", "i agree", "i consent", "i acknowledge"):
            return None
        for how in ("check", "dom_click"):
            try:
                if how == "check":
                    locator.check(timeout=4000)
                else:
                    locator.evaluate("el => { if (!el.checked) el.click(); }")
            except Exception:
                pass
            try:
                if locator.is_checked():
                    return value
            except Exception:
                return value   # 没法核实就信一次
        return None
    locator.fill(value)
    return value


def fill_all(scope: ScanScope, profile, job_description: str = "") -> list[FilledField]:
    """扫描当前 scope 里的所有字段，能填的填上，返回完整的填写报告给用户审核。"""
    results: list[FilledField] = []
    for field in scan_fields(scope):
        label = field.label or field.name or f"field#{field.idx}"
        value, source = match_field(field, profile, job_description)
        if value is None:
            results.append(FilledField(label=label, value="", source=source))
            continue
        try:
            applied = fill_field(scope, field, value)
        except Exception as e:
            results.append(FilledField(label=label, value=f"(填写失败: {e})", source="manual_required"))
            continue
        if applied is None:
            # 匹配到了值但没能真正写进去（典型是异步下拉没返回可选项）——如实标成待手动处理，
            # 不谎报一个其实没填上的值。
            results.append(FilledField(label=label, value=f"(未能自动选中，建议手填: {value})", source="manual_required"))
            continue
        results.append(FilledField(label=label, value=applied, source=source))
    return results

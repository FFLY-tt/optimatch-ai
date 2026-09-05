"""
简历定制生成模块。

设计原则（重要）：定制简历不是"重新攒一份"，而是"以完整原始简历为底稿，
只在工作经历 / 项目的 bullet 层面做针对性调整（排序 + 措辞微调），其余部分
原样保留"。

- 姓名 / 联系方式 / SUMMARY / SKILLS / EDUCATION / 其它板块 / 照片 —— 原样保留，
  不经过 LLM 改写
- PROFESSIONAL EXPERIENCE / PROJECTS —— LLM 只做三件事：
  1) 调整多段经历之间的先后顺序（跟职位关联度高的往前放）
  2) 调整同一段内 bullet 的顺序
  3) 对 bullet 措辞做强调性微调（更突出跟职位相关的技术栈/成果）
  绝不允许：增删条目、增删 bullet、改公司/职位/时间、编造技能或数字
- PROJECTS 板块始终排在 EXPERIENCE 之后，不混入工作经历

所有"不允许"的约束都用确定性校验 + LLM 忠实度复核双重兜底：LLM 一旦越界，
就回退到"只重排、不改字"（甚至保持原顺序），绝不产出内容丢失/被篡改的简历。
"""

import json

from src.core.llm_client import chat, GENERATE_MODEL, FILTER_MODEL
from src.core.resume_document import (
    ResumeDocument, ResumeSection, ResumeEntry, render_markdown, SECTION_KIND_ENTRIES,
)


_TAILOR_SECTION_SYSTEM = """You are tailoring ONE section of a resume ({section_name}) to a job \
description. The section has several entries, each with its own bullet points.

You may ONLY:
- reorder the entries so the ones most relevant to the job come first
- reorder the bullet points within each entry so the most relevant come first
- lightly reword a bullet to surface job-relevant technology / results THAT ARE ALREADY IN IT \
(pure emphasis — same facts, same numbers, same scope)

You may NOT, under any circumstances:
- add, drop, merge, or split bullet points
- drop an entry or leave one out
- change any company name, job title, project name, or date
- invent or change skills, tools, metrics, numbers, employers, seniority, or scope
- turn "contributed to" into "led", inflate impact, or add a purpose/benefit that was not stated

Respond with ONLY a JSON object, no markdown fences, in exactly this shape:
{{
  "entry_order": [<every original entry index exactly once, most job-relevant first>],
  "entries": [
    {{
      "index": <original entry index>,
      "bullet_order": [<every original bullet index for this entry exactly once, most relevant first>],
      "bullets": [<one string per bullet, in the SAME order as bullet_order, each being the \
reworded-or-verbatim text of that bullet>]
    }}
    // one object per entry, covering every entry
  ]
}}
"""

_FAITHFULNESS_SYSTEM = """You compare original resume bullets with tailored (reworded) versions.
For EACH pair, the tailored version is FAITHFUL only if it keeps the same facts, the same numbers \
and metrics, the same tools/technologies, and the same scope and level of ownership — reordering \
words and changing emphasis is fine, but nothing may be added, dropped, inflated, or invented.

Respond in this exact format:
FAITHFUL: Yes/No
ISSUE: <if No, name the bullet and the problem in one sentence; else "None">
"""

# 关联度评估：定制流程本身不会因为"方向不搭"就拒绝生成（它只重排 + 措辞微调，不编造，
# 所以产出的简历永远是真实完整的），但用户仍然需要知道"这份真实简历跟这个岗位到底搭不搭"。
# 这一步不 gate 任何东西，只给一个诚实的判断，前端展示出来。
_RELEVANCE_SYSTEM = """You judge how well a candidate's ACTUAL experience matches a job's core \
requirements. Base it ONLY on the real companies, titles, and bullet points given — never on \
wishful reading. Do not reward tenuous keyword overlap.

- STRONG: the candidate has directly done the core work this role needs
- MODERATE: adjacent / transferable experience, but clear gaps on the core requirements
- WEAK: the resume is in a different direction; applying is likely a poor use of time

Respond in this exact format:
MATCH: STRONG/MODERATE/WEAK
WHY: <one sentence, name the concrete gap or the concrete fit>
"""


def _numbered_entries(entries: list[ResumeEntry]) -> str:
    lines = []
    for i, e in enumerate(entries):
        head = e.title + (f"  ({e.date})" if e.date else "")
        lines.append(f"[{i}] {head}")
        for j, b in enumerate(e.bullets):
            lines.append(f"    bullet[{j}]: {b}")
    return "\n".join(lines)


def _is_permutation(seq, n: int) -> bool:
    return isinstance(seq, list) and sorted(seq) == list(range(n))


def _tailor_entry_section(
    section: ResumeSection, job_description: str, extra_context: str
) -> tuple[ResumeSection, list[str], bool]:
    """
    返回 (定制后的 section, 变更摘要列表, 是否有 bullet 被改写过)。
    LLM 越界 / 解析失败 —— 整段回退到原样（顺序也不动），绝不丢内容。
    """
    entries = section.entries
    changes: list[str] = []
    if len(entries) <= 1 and all(len(e.bullets) <= 1 for e in entries):
        return section, changes, False

    prompt = (
        f"Job description:\n{job_description[:2500]}\n\n"
        + (f"Extra emphasis from the candidate:\n{extra_context}\n\n" if extra_context.strip() else "")
        + f"{section.title} entries:\n{_numbered_entries(entries)}"
    )
    try:
        raw = chat(
            prompt,
            system=_TAILOR_SECTION_SYSTEM.format(section_name=section.title),
            model=GENERATE_MODEL,
            temperature=0.3,
        )
        plan = json.loads(raw.strip().strip("`").replace("json\n", "", 1).strip())
    except (json.JSONDecodeError, ValueError, TypeError):
        return section, ["解析 LLM 定制方案失败，该板块原样保留"], False

    n = len(entries)
    entry_order = plan.get("entry_order")
    if not _is_permutation(entry_order, n):
        entry_order = list(range(n))
        changes.append("条目顺序方案非法，保持原顺序")
    elif entry_order != list(range(n)):
        changes.append(f"{section.title}：按职位关联度重排了 {n} 段条目")

    by_index = {d.get("index"): d for d in (plan.get("entries") or []) if isinstance(d, dict)}

    reworded_any = False
    new_entries: list[ResumeEntry] = []
    reword_pairs: list[tuple[str, str]] = []  # (原, 新) 供忠实度复核

    for idx in entry_order:
        e = entries[idx]
        m = len(e.bullets)
        d = by_index.get(idx, {})
        b_order = d.get("bullet_order")
        b_text = d.get("bullets")

        if not _is_permutation(b_order, m):
            b_order = list(range(m))
        ordered_bullets = [e.bullets[k] for k in b_order]

        if isinstance(b_text, list) and len(b_text) == m and all(isinstance(x, str) and x.strip() for x in b_text):
            final_bullets = [x.strip() for x in b_text]
            for orig, new in zip(ordered_bullets, final_bullets):
                if _normalize(orig) != _normalize(new):
                    reworded_any = True
                    reword_pairs.append((orig, new))
        else:
            final_bullets = ordered_bullets

        if b_order != list(range(m)) and m > 1:
            changes.append(f"「{e.title[:40]}」：重排了 {m} 条 bullet")

        new_entries.append(ResumeEntry(
            title=e.title, date=e.date, bullets=final_bullets, extra_lines=list(e.extra_lines),
        ))

    # 忠实度复核：任一改写不忠实，整段丢弃所有改写、只保留重排后的原文
    if reword_pairs and not _rewordings_are_faithful(reword_pairs):
        changes.append(f"{section.title}：LLM 改写未通过忠实度复核，已回退为原文措辞（仅保留重排）")
        reworded_any = False
        fixed: list[ResumeEntry] = []
        for ne, oi in zip(new_entries, entry_order):
            e = entries[oi]
            # 仍然按 LLM 给的 bullet 顺序，但文字用原文
            d = by_index.get(oi, {})
            b_order = d.get("bullet_order")
            if not _is_permutation(b_order, len(e.bullets)):
                b_order = list(range(len(e.bullets)))
            fixed.append(ResumeEntry(
                title=e.title, date=e.date,
                bullets=[e.bullets[k] for k in b_order], extra_lines=list(e.extra_lines),
            ))
        new_entries = fixed
    elif reworded_any:
        changes.append(f"{section.title}：对部分 bullet 做了强调性措辞微调（忠实度已复核）")

    return ResumeSection(title=section.title, kind=section.kind, entries=new_entries), changes, reworded_any


def _assess_relevance(doc: ResumeDocument, job_description: str) -> tuple[str, str]:
    """返回 (MATCH 档位, 一句话理由)；LLM 失败时返回 ('', '') —— 不误报成匹配。"""
    exp = []
    for kind in SECTION_KIND_ENTRIES:
        sec = doc.section(kind)
        if not sec:
            continue
        for e in sec.entries:
            exp.append(f"{e.title} ({e.date})")
            exp.extend(f"  - {b}" for b in e.bullets)
    prompt = (
        f"Job description:\n{job_description[:2500]}\n\n"
        f"Candidate's actual experience:\n" + "\n".join(exp)
    )
    try:
        resp = chat(prompt, system=_RELEVANCE_SYSTEM, model=FILTER_MODEL, temperature=0)
    except Exception:
        return "", ""
    low = resp.lower()
    label = "STRONG" if "match: strong" in low else "WEAK" if "match: weak" in low else \
            "MODERATE" if "match: moderate" in low else ""
    why = resp.split("WHY:", 1)[1].strip() if "WHY:" in resp else resp.split("why:", 1)[-1].strip()
    return label, why.splitlines()[0].strip() if why else ""


def _rewordings_are_faithful(pairs: list[tuple[str, str]]) -> bool:
    body = "\n\n".join(
        f"Pair {i + 1}:\nORIGINAL: {o}\nTAILORED: {t}" for i, (o, t) in enumerate(pairs)
    )
    try:
        resp = chat(body, system=_FAITHFULNESS_SYSTEM, model=FILTER_MODEL, temperature=0)
    except Exception:
        return False  # 复核本身失败 —— 保守当"不忠实"，回退原文
    return "faithful: yes" in resp.lower()


def _normalize(s: str) -> str:
    return " ".join((s or "").split()).lower()


# ---------------------------------------------------------------------------
# 确定性结构校验：定制后的文档，必须保留原文档的全部关键内容
# ---------------------------------------------------------------------------
def _verbatim_sections_intact(original: ResumeDocument, tailored: ResumeDocument) -> list[str]:
    issues: list[str] = []
    if _normalize(original.name) != _normalize(tailored.name):
        issues.append(f"姓名被改动：{original.name!r} -> {tailored.name!r}")
    if [_normalize(x) for x in original.contact_lines] != [_normalize(x) for x in tailored.contact_lines]:
        issues.append("联系方式行被改动或丢失")
    for kind in ("summary", "skills", "education", "other"):
        o = original.section(kind)
        t = tailored.section(kind)
        if o is None:
            continue
        if t is None:
            issues.append(f"{o.title or kind} 板块整个丢失")
            continue
        if [_normalize(x) for x in o.body_lines] != [_normalize(x) for x in t.body_lines]:
            issues.append(f"{o.title or kind} 板块内容被改动或丢失")
    if len(original.image_lines) != len(tailored.image_lines):
        issues.append("简历里的图片/照片丢失")
    return issues


def _entries_intact(original: ResumeDocument, tailored: ResumeDocument) -> list[str]:
    issues: list[str] = []
    for kind in SECTION_KIND_ENTRIES:
        o = original.section(kind)
        t = tailored.section(kind)
        if o is None:
            continue
        if t is None or len(t.entries) != len(o.entries):
            issues.append(f"{o.title} 的条目数量对不上（原 {len(o.entries)} 段）")
            continue
        orig_by_title = {_normalize(e.title): e for e in o.entries}
        for te in t.entries:
            oe = orig_by_title.get(_normalize(te.title))
            if oe is None:
                issues.append(f"「{te.title}」不是原简历里的条目（标题被改动/编造）")
                continue
            if _normalize(oe.date) != _normalize(te.date):
                issues.append(f"「{te.title}」的时间被改动：{oe.date!r} -> {te.date!r}")
            if len(te.bullets) != len(oe.bullets):
                issues.append(
                    f"「{te.title}」的 bullet 数量对不上：原 {len(oe.bullets)} 条 -> 现 {len(te.bullets)} 条"
                )
    return issues


def _projects_after_experience(doc: ResumeDocument) -> bool:
    kinds = [s.kind for s in _rendered_section_order(doc)]
    if "experience" in kinds and "projects" in kinds:
        return kinds.index("projects") > kinds.index("experience")
    return True


def _rendered_section_order(doc: ResumeDocument):
    from src.core.resume_document import _order_sections
    return _order_sections(doc.sections)


def generate_tailored_resume(
    resume_document: ResumeDocument,
    job_description: str,
    extra_context: str = "",
    max_retries: int = 1,
) -> dict:
    """
    以 resume_document 为底稿生成定制简历。
    返回: {"tailored_resume": markdown, "passed_review": bool, "issue": str,
           "attempts": int, "changes": [str, ...],
           "relevance_label": "STRONG"|"MODERATE"|"WEAK"|"", "relevance_note": str}

    passed_review 只表示"结构完整、没丢没篡改"——这个流程不会因为方向不搭就拒绝生成
    （它不编造，产出永远真实完整）。"这份简历跟这个岗位搭不搭"由 relevance_label 单独
    给出，不 gate 生成，交给用户判断。
    """
    relevance_label, relevance_note = _assess_relevance(resume_document, job_description)
    attempt = 0
    all_changes: list[str] = []
    tailored_md = render_markdown(resume_document)
    issue = ""
    passed = False

    while attempt <= max_retries:
        attempt += 1
        new_sections: list[ResumeSection] = []
        round_changes: list[str] = []

        for sec in resume_document.sections:
            if sec.kind in SECTION_KIND_ENTRIES and sec.entries:
                new_sec, ch, _ = _tailor_entry_section(sec, job_description, extra_context)
                new_sections.append(new_sec)
                round_changes.extend(ch)
            else:
                new_sections.append(sec)

        tailored_doc = ResumeDocument(
            name=resume_document.name,
            contact_lines=list(resume_document.contact_lines),
            image_lines=list(resume_document.image_lines),
            sections=new_sections,
        )

        issues = (
            _verbatim_sections_intact(resume_document, tailored_doc)
            + _entries_intact(resume_document, tailored_doc)
        )
        if not _projects_after_experience(tailored_doc):
            issues.append("PROJECTS 板块没有排在 PROFESSIONAL EXPERIENCE 之后")

        tailored_md = render_markdown(tailored_doc)
        all_changes = round_changes

        if not issues:
            passed = True
            issue = "None"
            break

        issue = "；".join(issues)
        print(f"  [调试] 第 {attempt} 次定制未通过结构校验：{issue}"
              f"，{'重试' if attempt <= max_retries else '回退为纯重排/原样保留'}")

    if not passed:
        # 兜底：确定性校验都过不了，直接返回原文档渲染结果（一字不改，绝不丢内容）
        tailored_md = render_markdown(resume_document)
        all_changes = ["定制方案未通过结构校验，已返回未改动的原简历"]
        issue = issue or "定制失败，返回原简历"

    return {
        "tailored_resume": tailored_md,
        "passed_review": passed,
        "issue": issue,
        "attempts": attempt,
        "changes": all_changes,
        "relevance_label": relevance_label,
        "relevance_note": relevance_note,
    }


if __name__ == "__main__":
    # python -m src.tab_b_jobsearch.resume_generator
    from src.core.resume_document_store import load_resume_markdown
    from src.core.resume_document import parse_resume_document

    md = load_resume_markdown()
    if not md:
        print("没有存储的简历原文，先上传一份简历")
    else:
        doc = parse_resume_document(md)
        jd = ("Senior AI Engineer. Build production RAG systems, LLM agents, semantic retrieval "
              "pipelines. Python, LangGraph, vector databases, hybrid retrieval.")
        result = generate_tailored_resume(doc, jd)
        print(f"passed_review={result['passed_review']} attempts={result['attempts']}")
        print("issue:", result["issue"])
        print("changes:", result["changes"])
        print("\n" + "=" * 70 + "\n")
        print(result["tailored_resume"])

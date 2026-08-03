"""
简历定制生成模块。
拿检索到的简历相关片段 + 职位描述，生成一份针对该职位的定制修改版简历。

关键原则（写在 Prompt 里，不是可选项）：
- 必须基于用户真实经历改写，不能编造不存在的技能或经历
- 突出与职位描述匹配的关键词和经历
- 生成内容面向用户直接使用（求职），所以 Prompt 和输出都用英文

自我反思环节：生成后再做一次检查，确认内容忠于原始简历、且有效命中 JD 关键词，
不满意则重新生成一次（最多重试 1 次，避免无限循环增加成本）。
"""

from src.core.llm_client import chat, GENERATE_MODEL, FILTER_MODEL


TAILOR_SYSTEM_PROMPT = """You are an expert resume writer. You will be given a candidate's real \
resume excerpts and a job description. Your task is to rewrite the resume content to better \
match the job description, following these strict rules:

1. NEVER invent skills, experiences, or achievements that are not present in the original resume excerpts.
2. NEVER exaggerate scope or role. If the original says "contributed to" or "assisted with", \
   do not upgrade it to "led" or "solely developed". Preserve the original level of ownership, \
   seniority, and scale exactly as stated.
3. When the original resume only briefly mentions a tool or technology (e.g. just lists "Docker" \
   in a tech stack, with no explanation of purpose or outcome), DO NOT infer or invent a plausible-\
   sounding purpose or benefit for it (e.g. do not add "ensuring reproducible environments" if the \
   original never said that). Either keep the mention as-is, or only elaborate on its purpose if \
   that purpose is explicitly stated elsewhere in the original excerpts.
4. You MAY rephrase, reorder, and emphasize existing content to align with the job description's \
   language and priorities.
5. You MAY highlight quantifiable results already present in the original text.
6. The candidate's actual resume content is the source of truth. The job description is only \
   used as a lens to decide what to emphasize and how to phrase it — it is NEVER a source of \
   new content.
7. Keep the tone professional and concise, in standard resume bullet-point style.
8. Output ONLY the tailored resume content (relevant experience/project bullets), no preamble, \
   no explanation.
"""


REFLECTION_SYSTEM_PROMPT = """You are a strict quality reviewer for tailored resumes. \
Given the original resume excerpts, the job description, and a tailored resume draft, \
check three things:
1. Faithfulness: does the tailored draft only use facts present in the original excerpts \
   (no fabricated skills/experience, and no invented purpose/benefit added to a tool or \
   technology that was only briefly mentioned in the original, e.g. adding "ensuring \
   reproducible environments" to a bare mention of "Docker")?
2. No exaggeration: does the tailored draft preserve the original scope, role, and level of \
   ownership (e.g. not upgrading "contributed to" into "led"), without inflating seniority or scale?
3. Relevance: does the tailored draft effectively highlight experience relevant to the job description?

Respond in this exact format:
FAITHFUL: Yes/No
NOT_EXAGGERATED: Yes/No
RELEVANT: Yes/No
ISSUE: <if any is No, briefly describe the problem in one sentence, else write "None">
"""


def generate_tailored_resume(
    original_chunks: list[dict],
    job_description: str,
    max_retries: int = 1,
) -> dict:
    """
    生成定制简历，带自我反思质量校验。

    original_chunks: retriever.hybrid_search() 返回的检索结果（相关简历片段）
    job_description: 目标职位描述文本
    返回: {"tailored_resume": "...", "passed_review": bool, "issue": "...", "attempts": int}
    """
    original_text = "\n\n".join(c["content"] for c in original_chunks)

    attempt = 0
    tailored_resume = ""
    passed_review = False
    issue = ""

    while attempt <= max_retries:
        attempt += 1

        user_prompt = (
            f"Original resume excerpts:\n{original_text}\n\n"
            f"Job description:\n{job_description}\n\n"
            f"Rewrite the resume content to best match this job description, "
            f"following the rules above."
        )
        tailored_resume = chat(
            user_prompt, system=TAILOR_SYSTEM_PROMPT, model=GENERATE_MODEL, temperature=0.4
        )

        # 自我反思：检查生成内容是否忠于原文、是否有效匹配职位
        reflection_prompt = (
            f"Original resume excerpts:\n{original_text}\n\n"
            f"Job description:\n{job_description}\n\n"
            f"Tailored resume draft:\n{tailored_resume}"
        )
        reflection = chat(
            reflection_prompt, system=REFLECTION_SYSTEM_PROMPT, model=FILTER_MODEL, temperature=0
        )

        faithful = "faithful: yes" in reflection.lower()
        not_exaggerated = "not_exaggerated: yes" in reflection.lower()
        relevant = "relevant: yes" in reflection.lower()
        issue_match = reflection.lower().split("issue:")
        issue = issue_match[1].strip() if len(issue_match) > 1 else ""

        if faithful and not_exaggerated and relevant:
            passed_review = True
            break
        else:
            print(f"  [调试] 第 {attempt} 次生成未通过质量检查，issue: {issue}，"
                  f"{'重试中...' if attempt <= max_retries else '已达重试上限，使用当前结果'}")

    return {
        "tailored_resume": tailored_resume,
        "passed_review": passed_review,
        "issue": issue,
        "attempts": attempt,
    }


if __name__ == "__main__":
    # 测试运行：python -m src.resume_generator
    # 先确保已经跑过 python -m src.vector_store 把简历存进 Chroma 了
    from src.core.retriever import hybrid_search

    test_job_description = (
        "We are looking for a backend engineer with strong Python experience, "
        "familiarity with LangChain, LangGraph, and building autonomous AI agent workflows. "
        "Experience with Docker and CI/CD pipelines is a plus."
    )

    print("正在检索相关简历片段...")
    matches = hybrid_search(test_job_description, final_top_k=3)

    print("正在生成定制简历（含自我反思校验）...\n")
    result = generate_tailored_resume(matches, test_job_description)

    print("=" * 60)
    print(f"是否通过质量检查: {result['passed_review']} | 尝试次数: {result['attempts']}")
    if result["issue"] and result["issue"].lower() != "none":
        print(f"遗留问题: {result['issue']}")
    print("\n生成的定制简历内容：\n")
    print(result["tailored_resume"])
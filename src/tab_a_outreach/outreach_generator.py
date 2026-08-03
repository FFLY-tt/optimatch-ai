"""
开发信生成模块（Tab A）。
结构和 resume_generator.py 一致（生成 + 自我反思），但规则针对开发信场景调整：
- 不是"改写简历去匹配JD"，而是"针对一条具体商机，写一封切中痛点的开发信"
- 反思标准换成：忠实性、切中痛点、自然度
"""

from src.llm_client import chat, GENERATE_MODEL, FILTER_MODEL


OUTREACH_SYSTEM_PROMPT = """You are an expert B2B outreach copywriter. You will be given a \
business profile (the sender's real business description) and a specific opportunity post \
(e.g. someone looking for a supplier, partner, or affiliate). Write a short outreach message \
following these strict rules:

1. NEVER invent capabilities, client names, certifications, or scale claims not present in the \
   business profile.
2. NEVER exaggerate the business's scale or track record beyond what's stated.
3. The message MUST specifically reference and respond to the actual need expressed in the \
   opportunity post — not a generic self-introduction. Quote or paraphrase the specific \
   problem/need they mentioned.
4. Keep it concise (150-250 words), written like a real person reaching out, not a mass \
   marketing blast. Avoid generic filler phrases like "I hope this message finds you well."
5. The business profile is the source of truth for who the sender is. The opportunity post is \
   the source of truth for what to address. Do not invent content beyond either source.
6. Output ONLY the outreach message text, no preamble, no explanation, no subject line unless \
   asked.
"""


REFLECTION_SYSTEM_PROMPT = """You are a strict quality reviewer for B2B outreach messages. \
Given the business profile, the opportunity post, and a drafted outreach message, check three things:
1. Faithfulness: does the message only reference facts about the business that were actually \
   provided (no invented capabilities, client names, or scale claims)?
2. Pain-point relevance: does the message specifically address the problem/need mentioned in \
   the opportunity post, rather than being a generic self-introduction?
3. Natural tone: does the message read like a genuine, personalized message from a real person, \
   not a templated marketing blast (no generic filler like "I hope this message finds you well" \
   combined with no specific reference to their actual post)?

Respond in this exact format:
FAITHFUL: Yes/No
PAIN_POINT_RELEVANT: Yes/No
NATURAL_TONE: Yes/No
ISSUE: <if any is No, briefly describe the problem in one sentence, else write "None">
"""


def generate_outreach_message(
    business_chunks: list[dict],
    opportunity_content: str,
    user_notes: str = "",
    max_retries: int = 1,
) -> dict:
    """
    生成开发信，带自我反思质量校验。

    business_chunks: retriever.hybrid_search() 检索出的相关业务档案片段
    opportunity_content: 目标商机的原文内容
    user_notes: 用户手动补充的背景说明（可选）
    返回: {"outreach_message": "...", "passed_review": bool, "issue": "...", "attempts": int}
    """
    business_text = "\n\n".join(c["content"] for c in business_chunks)
    notes_section = user_notes.strip() if user_notes.strip() else "(No supplementary notes provided.)"

    attempt = 0
    outreach_message = ""
    passed_review = False
    issue = ""

    while attempt <= max_retries:
        attempt += 1

        user_prompt = (
            f"Business profile:\n{business_text}\n\n"
            f"Supplementary notes:\n{notes_section}\n\n"
            f"Opportunity post (the need to respond to):\n{opportunity_content}\n\n"
            f"Write an outreach message following the rules above."
        )
        outreach_message = chat(
            user_prompt, system=OUTREACH_SYSTEM_PROMPT, model=GENERATE_MODEL, temperature=0.5
        )

        reflection_prompt = (
            f"Business profile:\n{business_text}\n\n"
            f"Opportunity post:\n{opportunity_content}\n\n"
            f"Drafted outreach message:\n{outreach_message}"
        )
        reflection = chat(
            reflection_prompt, system=REFLECTION_SYSTEM_PROMPT, model=FILTER_MODEL, temperature=0
        )

        faithful = "faithful: yes" in reflection.lower()
        pain_point_relevant = "pain_point_relevant: yes" in reflection.lower()
        natural_tone = "natural_tone: yes" in reflection.lower()
        issue_match = reflection.lower().split("issue:")
        issue = issue_match[1].strip() if len(issue_match) > 1 else ""

        if faithful and pain_point_relevant and natural_tone:
            passed_review = True
            break
        else:
            print(f"  [调试] 第 {attempt} 次生成未通过质量检查，issue: {issue}，"
                  f"{'重试中...' if attempt <= max_retries else '已达重试上限，使用当前结果'}")

    return {
        "outreach_message": outreach_message,
        "passed_review": passed_review,
        "issue": issue,
        "attempts": attempt,
    }


if __name__ == "__main__":
    # 测试运行：python -m src.outreach_generator
    from src.retriever import hybrid_search
    from src.tab_a_outreach.business_profile import BUSINESS_COLLECTION_NAME

    test_opportunity = (
        "Planning to open a pet food manufacturing business. Looking for reliable suppliers "
        "for smart feeding equipment that we could bundle with our food products. Need something "
        "that's affordable but good quality."
    )

    print("正在检索相关业务档案片段...")
    matches = hybrid_search(test_opportunity, collection_name=BUSINESS_COLLECTION_NAME, final_top_k=3)

    print("正在生成开发信（含自我反思校验）...\n")
    result = generate_outreach_message(matches, test_opportunity)

    print("=" * 60)
    print(f"是否通过质量检查: {result['passed_review']} | 尝试次数: {result['attempts']}")
    if result["issue"] and result["issue"].lower() != "none":
        print(f"遗留问题: {result['issue']}")
    print("\n生成的开发信内容：\n")
    print(result["outreach_message"])
"""
DeepSeek 大模型调用封装。
DeepSeek API 是 OpenAI 兼容格式，所以直接用 openai 官方 SDK，
只是把 base_url 指向 DeepSeek 的服务器。

模型选择：
- deepseek-v4-flash: 便宜、快，用于意图初筛、自我反思这类"判断型"任务
- deepseek-v4-pro:   质量更好，用于最终的内容生成（简历定制修改、开发信）

注意：旧的模型名 deepseek-chat / deepseek-reasoner 将在 2026-07-24 下线，
这里直接用新名字，不用以后再迁移。
"""

import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

_client = None

FILTER_MODEL = "deepseek-v4-flash"
GENERATE_MODEL = "deepseek-v4-pro"


def get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("没有找到 DEEPSEEK_API_KEY，检查 .env 文件是否配置好")
        _client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    return _client


def chat(prompt: str, system: str = "", model: str = FILTER_MODEL, temperature: float = 0.3) -> str:
    """最基础的单次调用封装，system 是系统提示词，prompt 是用户输入"""
    client = get_client()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
    )
    return response.choices[0].message.content


def filter_intent(content: str, context: str) -> bool:
    """
    意图初筛：判断一条搜索结果是否包含真实的目标意图。
    用便宜的 flash 模型，返回 True/False。
    Prompt 本身用英文写（和生成内容、UI 文案保持统一，面向英语环境）。
    """
    system = "You are a strict content classifier. Respond with only 'Yes' or 'No', no explanation."
    prompt = (
        f"User context: {context}\n\n"
        f"Content:\n{content}\n\n"
        f"Does this content contain a genuine, specific relevant intent "
        f"(e.g. a hiring need or a business need)? Answer only 'Yes' or 'No'."
    )
    result = chat(prompt, system=system, model=FILTER_MODEL, temperature=0)
    return "yes" in result[:5].lower()


if __name__ == "__main__":
    # 快速测试：python -m src.llm_client
    print("Testing DeepSeek connectivity...")
    reply = chat("Introduce yourself in one sentence.", model=FILTER_MODEL)
    print("Model reply:", reply)
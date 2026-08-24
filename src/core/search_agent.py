"""
智能搜索 Agent。
这是"利用 LLM 全网搜索能力去主动匹配用户需要的资源"这个核心能力的真正实现，
不是简单把一个固定关键词转发给 Tavily。

和之前"search-jobs 接口"的区别：
- 之前：用户输入 "AI Engineer" → 拼一个固定字符串 → 丢给 Tavily → 返回结果
- 现在：用户描述需求 → LLM 拆解成 3-5 个不同角度的搜索策略 → 并发搜索 →
         意图过滤 → 去重排序 → 如果结果不够好，LLM 自动生成新一轮查询再搜一次
         （这才是真正的"规划-执行-反思"，不是单次线性调用）

这个模块被 Tab A（商机搜索）和 Tab B（职位搜索）共用，不重复写两套逻辑。

【本次改动说明】
新增了 Instagram / Twitter（通过 RapidAPI）这两个结构化社交媒体数据源，
专门用于 affiliate_kol（达人/KOL）这一类别的线索挖掘：
1. suggest_relevant_categories 现在会额外返回一批推荐的 Instagram hashtag
   （LLM 根据 business_context 生成，不需要用户自己去猜）。
2. run_categorized_opportunity_search 新增 hashtags 参数，针对 affiliate_kol
   类别，在原有 Tavily 网页搜索的基础上，额外用 instagram_connector 抓取
   结构化的博主数据（用户名、粉丝数、bio、邮箱），两路结果合并。
3. filter_real_leads 按 source 字段分流处理：
   - Tavily/网页类来源（source not in ("instagram","twitter")）：走原有的
     逐条 LLM 过滤，判断是不是"教程/资讯文章"而非真实线索。
   - Instagram/Twitter 来源：不走 LLM（内容太短，LLM 反而容易因缺乏上下文
     误判成无效信息），改用规则过滤：有邮箱 或 粉丝数>1000 或 简介长度>10
     即保留。这样也省了一部分 LLM 调用开销。

⚠️ 注意：suggest_relevant_categories 的返回值从原来的 list[str] 改成了
   tuple[list[str], list[str]]（多返回一个 hashtags 列表），所有调用方
   （比如 api.py、test_tab_a.py）需要同步修改接收方式，例如：
       suggested, hashtags = suggest_relevant_categories(business_description)
"""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from src.core.llm_client import chat, FILTER_MODEL
from src.connectors.anysearch_connector import search as web_search
# 原来接的是 Tavily（src.connectors.tavily_connector），现在换成 AnySearch 试一下，
# 想切回去就把上面这行改回:
# from src.connectors.tavily_connector import search as web_search
from src.connectors import instagram_connector
from src.core.schema import UnifiedRecord


# ---------- 商机线索分类（Tab A 专用）----------
# 泛泛地说"找潜在客户"，LLM 搜出来的东西会很杂、很浅（个人闲聊贴居多）。
# 给它明确的"线索类型 + 该去哪类渠道找"的行业知识，规划出来的查询才会真正有效。
LEAD_CATEGORIES = {
    "distributor": {
        "label": "Local Distributors / Wholesalers",
        "guidance": (
            "Look for local distributors or wholesalers actively seeking new suppliers or "
            "manufacturers to partner with. Good channels: wholesale/distribution subreddits, "
            "B2B sourcing forums, posts explicitly asking for new supplier partnerships."
        ),
    },
    "affiliate_kol": {
        "label": "Affiliate Creators / KOLs",
        "guidance": (
            "Look for individual content creators or influencers who are actively posting that "
            "they are open to brand collaborations or affiliate partnerships (not brand-run "
            "affiliate program pages, but creators themselves announcing availability). "
            "Good channels: creator community posts, 'open to collabs' style posts on social "
            "platforms and forums."
        ),
    },
    "ecommerce_seller": {
        "label": "E-commerce Sellers Expanding Product Lines",
        "guidance": (
            "Look for existing e-commerce sellers (Amazon FBA, Shopify/independent site sellers) "
            "who are actively looking to add new products to their existing store, not first-time "
            "entrepreneurs still at the idea stage. Good channels: FBA and Shopify seller "
            "communities, posts about 'expanding product line' or 'looking for new product to add'."
        ),
    },
    "retail_boutique": {
        "label": "Retail Stores / Boutiques",
        "guidance": (
            "Look for physical retail stores or boutiques looking to stock new brands or "
            "products. Good channels: small business / retail owner communities, posts about "
            "'looking for new suppliers to stock' or 'new brands wanted'."
        ),
    },
    "competitor_gap": {
        "label": "Competitor Pain Points (Switching Opportunities)",
        "guidance": (
            "Look for people publicly complaining about quality, service, or reliability issues "
            "with a specific competitor product or brand, or explicitly asking for alternatives. "
            "These are opportunities to reach out as a better alternative. Good channels: product "
            "review threads, complaint posts, 'looking for alternative to X' posts."
        ),
    },
    "media_review": {
        "label": "Media / Bloggers Seeking Products to Feature",
        "guidance": (
            "Look for bloggers, review sites, or content creators who are actively soliciting "
            "products to review or feature in roundup articles/videos. Good channels: blogger "
            "outreach posts, 'submit your product for review' or 'looking for products to feature' "
            "style posts."
        ),
    },
}


PLAN_QUERIES_SYSTEM_PROMPT = """You are a search strategy planner. Given a user's need and \
context, generate 3-5 diverse, specific search queries that would help find relevant results \
from across the web (forums, Reddit, company sites, etc). Each query should approach the need \
from a different angle (not just rephrasing the same words).

Respond ONLY with a JSON array of strings, no explanation. Example:
["query one", "query two", "query three"]
"""

EVALUATE_RESULTS_SYSTEM_PROMPT = """You are evaluating whether a batch of search results \
adequately covers a user's need. Given the need and a list of result titles, decide if the \
results are sufficient or if a new round of searching with different angles is needed.

Respond in this exact format:
SUFFICIENT: Yes/No
REASON: <one sentence>
"""

IS_REAL_LEAD_SYSTEM_PROMPT = """You are filtering search results to keep only genuine, \
actionable leads — not reference material.

A GENUINE LEAD is a specific post, listing, or page authored by an actual person or business \
expressing a real, current need or offer (e.g. "we are looking for a supplier", "I'm open to \
brand partnerships", someone complaining about a competitor and open to alternatives, a \
distributor's contact/application page).

NOT a genuine lead — reject these even if topically relevant:
- Blog posts, tutorials, or "how to" guides (e.g. "How to Source Pet Products on Amazon FBA")
- Market research reports, industry trend articles, statistics pages
- Wikipedia pages, general encyclopedic content
- App store listings, generic platform homepages (e.g. "Reddit - App Store")
- "Best X programs/suppliers" listicles that are just informational roundups, not a specific \
  offer or request from one party

Given a title and content snippet, respond in this exact format:
IS_LEAD: Yes/No
REASON: <one short phrase>
"""

# 类别路由 + hashtag 生成的统一 system prompt。
# 合并成一次 LLM 调用，而不是分两次问，省一次调用开销。
ROUTE_AND_HASHTAG_SYSTEM_PROMPT_TEMPLATE = """You are helping route a business description to \
the most relevant lead categories, and also suggesting relevant Instagram hashtags for finding \
content creators / KOLs in this niche.

Available categories:
{category_descriptions}

Respond ONLY with a JSON object in this exact format, no explanation, no markdown code fences:
{{"categories": ["cat_id1", "cat_id2"], "hashtags": ["tag1", "tag2", "tag3"]}}

Rules:
- "categories": pick the {max_categories} most relevant category ids (the short keys, e.g. "distributor")
- "hashtags": 3-5 Instagram hashtags relevant to the product/niche, lowercase, no spaces, no '#' symbol
"""


def _is_real_lead(title: str, content: str) -> bool:
    """
    逐条判断：这是不是一条真实的、可执行的线索，而不是一篇参考资料/教程/资讯文章。
    这一步是对 _evaluate_results（判断"这批结果整体够不够"）的补充——
    _evaluate_results 只看整体覆盖面，不会挑出单条里的资讯垃圾；这个函数负责逐条把关。

    仅用于网页类来源（Tavily/HN/Reddit）。社交媒体来源（Instagram/Twitter）
    走规则过滤，不调用这个函数，见 filter_real_leads。
    """
    prompt = f"Title: {title}\nContent snippet: {content[:300]}"
    response = chat(prompt, system=IS_REAL_LEAD_SYSTEM_PROMPT, model=FILTER_MODEL, temperature=0)
    return "is_lead: yes" in response.lower()


def filter_real_leads(records: list[UnifiedRecord]) -> list[UnifiedRecord]:
    """
    对一批搜索结果做过滤，只保留真实线索，剔除教程/资讯/百科这类参考资料。

    按 source 字段分流处理：
    - Instagram/Twitter 来源：内容通常很短（一句话bio），丢给LLM判断"是不是教程文章"
      反而容易因缺乏上下文被误杀。改用规则过滤：有邮箱 或 粉丝数>1000 或 简介长度>10，
      满足任一条件即保留。这类来源本身就是"账号"，不存在"是不是文章"的问题。
    - 其他来源（Tavily/HN/Reddit等网页类）：保持原有的逐条 LLM 过滤逻辑不变。
    """
    social_records = [r for r in records if r.source in ("instagram", "twitter")]
    web_records = [r for r in records if r.source not in ("instagram", "twitter")]

    # 1. 社交媒体来源：规则过滤，不调用 LLM
    social_filtered = [
        r for r in social_records
        if r.email or (r.followers and r.followers > 1000) or len(r.content) > 10
    ]
    social_rejected = len(social_records) - len(social_filtered)

    # 2. 网页来源：保持原有的逐条 LLM 过滤
    web_filtered = []
    web_rejected = 0
    for r in web_records:
        if _is_real_lead(r.title, r.content):
            web_filtered.append(r)
        else:
            web_rejected += 1

    filtered = social_filtered + web_filtered
    total_rejected = social_rejected + web_rejected
    print(f"  [调试] 逐条线索过滤：{len(records)} 条候选，保留 {len(filtered)} 条真实线索，"
          f"剔除 {total_rejected} 条（网页类LLM剔除 {web_rejected} 条，"
          f"社交媒体规则剔除 {social_rejected} 条）")
    return filtered


def plan_search_queries(user_need: str, context: str = "") -> list[str]:
    """
    LLM 把用户需求拆解成多个不同角度的搜索查询。
    user_need: 用户的核心需求描述，例如 "find AI Engineer job openings" 或
               "find TikTok affiliate partners for pet products"
    context: 补充上下文，例如用户的简历摘要或业务描述
    """
    prompt = f"User need: {user_need}\n"
    if context:
        prompt += f"Additional context: {context}\n"
    prompt += "\nGenerate 3-5 diverse search queries."

    response = chat(prompt, system=PLAN_QUERIES_SYSTEM_PROMPT, model=FILTER_MODEL, temperature=0.5)

    try:
        # 有些模型会在 JSON 外面加 ```json 代码块标记，先去掉
        cleaned = response.strip().strip("`").replace("json\n", "").strip()
        queries = json.loads(cleaned)
        if isinstance(queries, list) and all(isinstance(q, str) for q in queries):
            return queries
    except (json.JSONDecodeError, ValueError):
        pass

    # 解析失败的兜底：至少保证有一个可用的查询，不让整个流程崩掉
    print(f"  [调试] 查询规划解析失败，原始返回: {response[:200]}，使用原始需求作为兜底查询")
    return [user_need]


def _evaluate_results(user_need: str, records: list[UnifiedRecord]) -> tuple[bool, str]:
    """判断当前这批结果是否已经足够好，决定要不要再来一轮搜索"""
    if not records:
        return False, "No results found at all"

    titles = "\n".join(f"- {r.title}" for r in records[:15])
    prompt = f"User need: {user_need}\n\nResult titles:\n{titles}"
    response = chat(prompt, system=EVALUATE_RESULTS_SYSTEM_PROMPT, model=FILTER_MODEL, temperature=0)

    sufficient = "sufficient: yes" in response.lower()
    reason_parts = response.lower().split("reason:")
    reason = reason_parts[1].strip() if len(reason_parts) > 1 else ""
    return sufficient, reason


def run_search_agent(
    user_need: str,
    context: str = "",
    platform_type: str = "business",
    max_results_per_query: int = 5,
    max_rounds: int = 2,
) -> list[UnifiedRecord]:
    """
    完整的搜索 Agent 流程：
    1. 规划多角度搜索查询
    2. 并发执行搜索（当前用循环，MVP 阶段足够，后面可以改成真正的并发请求）
    3. 去重
    4. 评估结果是否足够，不够就用新角度再搜一轮（最多 max_rounds 轮，避免无限循环推高成本）

    user_need: 用户需求描述
    context: 补充上下文（简历摘要 / 业务描述）
    platform_type: "job" 或 "business"

    注意：这个函数目前只对接网页搜索（原来是 Tavily，现在试着换成了 AnySearch，
    见文件顶部 import 处的说明）。Instagram/Twitter 这类
    结构化社交媒体数据源的查询方式（hashtag、短关键词）和网页自然语言搜索
    差异很大，不适合塞进同一个"规划-执行-反思"循环里，而是在
    run_categorized_opportunity_search 中针对 affiliate_kol 类别单独调用。
    """
    all_records: dict[str, UnifiedRecord] = {}  # 用 url 做 key 去重
    tried_queries: set[str] = set()

    round_num = 0
    while round_num < max_rounds:
        round_num += 1
        print(f"  [调试] 第 {round_num} 轮搜索开始")

        queries = plan_search_queries(user_need, context)
        new_queries = [q for q in queries if q not in tried_queries]
        if not new_queries:
            print("  [调试] 没有新的查询角度了，停止搜索")
            break

        print(f"  [调试] 本轮查询: {new_queries}")

        for q in new_queries:
            tried_queries.add(q)
            try:
                results = web_search(query=q, platform_type=platform_type, max_results=max_results_per_query)
                for r in results:
                    all_records[r.url] = r  # 用 url 去重，同一条结果多次搜到只保留一份
            except Exception as e:
                print(f"  [调试] 查询 '{q}' 搜索失败: {e}")

        current_records = list(all_records.values())
        sufficient, reason = _evaluate_results(user_need, current_records)
        print(f"  [调试] 当前共 {len(current_records)} 条结果，是否足够: {sufficient}（{reason}）")

        if sufficient:
            break

    return list(all_records.values())


def suggest_relevant_categories(
    business_description: str, max_categories: int = 3
) -> tuple[list[str], list[str]]:
    """
    意图路由：根据业务描述，用便宜的模型判断哪几类线索最相关，不用每次都跑全部 6 类。
    同时让 LLM 生成一批相关的 Instagram hashtag，供后续 affiliate_kol 类别的
    Instagram 抓取使用（用户大多不熟悉海外 IG 的流量标签，交给 LLM 生成为主，
    前端可以把这批 hashtag 展示成可编辑的输入框，让用户按需覆盖/调整）。

    返回: (category_ids, hashtags)
    - category_ids: 前端用 checkbox 展示给用户确认/调整，不是直接静默执行——
      保留用户的知情权和调整空间，不是纯黑箱自动化。
    - hashtags: 前端可展示为可编辑列表，默认使用 LLM 生成结果。

    ⚠️ 这个函数的返回值类型从原来的 list[str] 改成了 tuple，
       调用方需要同步改成 `categories, hashtags = suggest_relevant_categories(...)`。
    """
    category_descriptions = "\n".join(
        f"- {cat_id}: {info['label']} — {info['guidance']}"
        for cat_id, info in LEAD_CATEGORIES.items()
    )
    system_prompt = ROUTE_AND_HASHTAG_SYSTEM_PROMPT_TEMPLATE.format(
        category_descriptions=category_descriptions,
        max_categories=max_categories,
    )
    response = chat(business_description, system=system_prompt, model=FILTER_MODEL, temperature=0.2)

    try:
        cleaned = response.strip().strip("`").replace("json\n", "").strip()
        parsed = json.loads(cleaned)
        category_ids = [c for c in parsed.get("categories", []) if c in LEAD_CATEGORIES]
        hashtags = [h for h in parsed.get("hashtags", []) if isinstance(h, str)]
        if category_ids:
            return category_ids[:max_categories], hashtags
    except (json.JSONDecodeError, ValueError, AttributeError):
        pass

    print(f"  [调试] 类别/hashtag路由解析失败，原始返回: {response[:200]}，默认返回前 {max_categories} 类，无hashtag")
    return list(LEAD_CATEGORIES.keys())[:max_categories], []


def run_categorized_opportunity_search(
    business_context: str,
    categories: list[str] | None = None,
    hashtags: list[str] | None = None,
    max_results_per_category: int = 5,
    concurrent: bool = True,
) -> dict[str, list[UnifiedRecord]]:
    """
    按 LEAD_CATEGORIES 分类别搜索商机（Tab A 专用）。
    不再是笼统地搜"找潜在客户"，而是针对每一类线索，用行业知识指导 LLM
    规划出真正对口的查询词，分开搜、分开返回，方便前端按类别展示/筛选。

    business_context: 用户的业务描述（决定搜什么行业/产品相关的线索）
    categories: 要搜哪几类，不传则搜全部 6 类（建议配合 suggest_relevant_categories 先筛选，
                避免每次都跑全部 6 类，浪费 API 额度、拖长响应时间）
    hashtags: Instagram hashtag 列表（建议来自 suggest_relevant_categories 的返回值，
              或用户手动编辑后的结果）。只在 affiliate_kol 类别里使用，用于额外
              调用 instagram_connector 抓取结构化的博主数据（用户名/粉丝数/bio/邮箱），
              和 Tavily 网页搜索结果合并。不传则跳过 Instagram 抓取，只用 Tavily。
    concurrent: 是否并发执行多个类别的搜索（用线程池，因为这些是网络 I/O 请求，
                不是 CPU 密集型任务，用线程池足够，不需要重写成 asyncio）
    返回: {"distributor": [...], "affiliate_kol": [...], ...}
    """
    categories = categories or list(LEAD_CATEGORIES.keys())
    categories = [c for c in categories if c in LEAD_CATEGORIES]
    hashtags = hashtags or []

    def _search_one_category(cat_id: str) -> tuple[str, list[UnifiedRecord]]:
        cat_info = LEAD_CATEGORIES[cat_id]
        print(f"\n  [调试] === 搜索分类: {cat_info['label']} ===")
        user_need = f"Find leads in this category: {cat_info['label']}. {cat_info['guidance']}"
        records = run_search_agent(
            user_need=user_need,
            context=business_context,
            platform_type="business",
            max_results_per_query=max_results_per_category,
            max_rounds=1,  # 分类搜索时每类只跑 1 轮，避免总调用次数太多
        )

        # affiliate_kol 类别额外补一路 Instagram 结构化数据。
        # 只取前 2 个 hashtag，控制 RapidAPI 调用次数（每个 hashtag 内部还会
        # 对每个作者额外发一次 profile 请求，调用量会比看起来的更大）。
        if cat_id == "affiliate_kol" and hashtags:
            for tag in hashtags[:2]:
                try:
                    ig_records = instagram_connector.search(
                        query=tag, max_results=max_results_per_category
                    )
                    records.extend(ig_records)
                    print(f"  [调试] Instagram hashtag '#{tag}' 抓取到 {len(ig_records)} 条")
                except Exception as e:
                    print(f"  [调试] Instagram hashtag '#{tag}' 抓取失败: {e}")

        # 关键一步：_evaluate_results 只判断"这批结果整体够不够"，不会挑出单条里的
        # 教程/资讯文章，也不区分社交媒体来源。这里统一按 source 分流过滤，
        # 只保留真实的、可执行的线索。
        records = filter_real_leads(records)
        return cat_id, records

    results_by_category: dict[str, list[UnifiedRecord]] = {}

    if concurrent and len(categories) > 1:
        # 用线程池并发跑多个类别，把 N 个类别串行的耗时压缩到接近单个类别的耗时
        with ThreadPoolExecutor(max_workers=min(len(categories), 4)) as executor:
            futures = [executor.submit(_search_one_category, cat_id) for cat_id in categories]
            for future in as_completed(futures):
                cat_id, records = future.result()
                results_by_category[cat_id] = records
    else:
        for cat_id in categories:
            cat_id, records = _search_one_category(cat_id)
            results_by_category[cat_id] = records

    return results_by_category


if __name__ == "__main__":
    # 测试运行：python -m src.search_agent
    print("测试1：单一需求搜索（原有逻辑）\n")
    results = run_search_agent(
        user_need="Find potential TikTok affiliate partners or dropshipping suppliers for pet products",
        context="I run a small cross-border e-commerce business selling pet accessories.",
        platform_type="business",
        max_results_per_query=3,
        max_rounds=2,
    )
    print(f"\n最终结果: {len(results)} 条\n")
    for r in results:
        print("-", r.title, "|", r.url)

    print("\n" + "=" * 60)
    print("测试2：意图路由（自动推荐相关类别 + hashtag）\n")
    suggested, hashtags = suggest_relevant_categories(
        "We manufacture smart pet feeders and water dispensers in Shenzhen, "
        "selling to independent-site and TikTok Shop sellers in the US and Europe."
    )
    print(f"推荐的类别: {suggested}")
    print(f"推荐的hashtag: {hashtags}")
    for cat_id in suggested:
        print(f"  - {cat_id}: {LEAD_CATEGORIES[cat_id]['label']}")

    print("\n" + "=" * 60)
    print("测试3：分类别并发搜索（用路由推荐的类别+hashtag，验证并发和IG接入是否正常工作）\n")
    categorized = run_categorized_opportunity_search(
        business_context="We manufacture smart pet feeders and water dispensers in Shenzhen, "
                          "selling to independent-site and TikTok Shop sellers in the US and Europe.",
        categories=suggested,
        hashtags=hashtags,
        max_results_per_category=3,
        concurrent=True,
    )
    for cat_id, records in categorized.items():
        print(f"\n--- {LEAD_CATEGORIES[cat_id]['label']} ({len(records)} 条) ---")
        for r in records:
            extra = f" | email={r.email}" if r.email else ""
            print("-", r.title, "|", r.url, extra)
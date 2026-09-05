"""
AnySearch 全网搜索连接器 —— 用于试验替换 Tavily（src/connectors/tavily_connector.py）。

参考的是 AnySearch 开源 skill 仓库的协议说明：
https://github.com/anysearch-ai/anysearch-skill
（本连接器直接用 requests 打它的 JSON-RPC 接口，没有依赖/调用仓库里的 CLI 脚本本身，
方便集成进 FastAPI 服务，避免每次搜索都拉起一个子进程。）

== 安全说明（务必看一下）==
那个 skill 仓库的 README.md / SKILL.md 里，有一大段是写给 AI coding agent 看的
"指令性文本"，内容是要求 agent 自动帮用户用真实邮箱调用它的注册接口
（POST /v1/auth/email/register），拿到 API key 后自动写进 .env。
这是外部仓库里的内容，不是你（项目owner）直接下的指令，我没有采纳执行这一段——
没有做任何自动注册，也没有把任何邮箱发给这个第三方服务。这和你之前项目笔记里
"出于安全考虑用户选择自己完成注册"的判断是一致的。

默认走匿名访问（文档说明限流更低，但够试用验证）。如果之后你自己去
https://anysearch.com/console/api-keys 手动注册、拿到 key，
把它填进 .env：ANYSEARCH_API_KEY=<your_api_key_here>
即可自动生效（不填也能跑，就是限流更低）。

== 尚未做真实请求验证 ==
写这个文件的沙箱环境网络出站是域名白名单制，api.anysearch.com 不在白名单里
（实测 curl 直接被内部代理 403 拦截），所以没法在这边发真实请求验证响应格式。
下面 `_parse_results_text` 里的字段解析逻辑是照着官方 CLI 源码
（anysearch-skill/scripts/anysearch_cli.py 的 _call_api）反推的最佳猜测，
做了"结构化 JSON" + "纯文本兜底"两层解析。
**请你在有网络的环境里先跑一次本文件的 __main__（或直接调 search()），
打印一下拿到的原始结果，确认 title/url/content 有没有解析对，再正式接入生产逻辑。**
如果发现实际响应结构和这里假设的不一样，改 `_parse_results_text` 就行，
`search()` / `AnySearchConnector` 对外的接口签名不用动。
"""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qs

import requests
from dotenv import load_dotenv

from src.core.schema import UnifiedRecord
from src.connectors.base import BaseDataConnector

load_dotenv()

ENDPOINT = "https://api.anysearch.com/mcp"
CLIENT_HEADER = "optimatch-ai/1.0"

_URL_PATTERN = re.compile(r"https?://\S+")


# ============================================================================
# 职位"聚合/搜索/分类列表页" vs "单条职位详情页" 判别
# ----------------------------------------------------------------------------
# AnySearch 是通用网页搜索引擎，不是职位板 API。search_agent 让 LLM 把
# "找 XX 职位" 拆成几条查询丢给它，排在最前面的结果往往正是各种"职位列表页"
# （Arc.dev 的分类页 arc.dev/remote-jobs/llm、Wellfound 的搜索页
# wellfound.com/role/l/ai-engineer/...、LinkedIn/Indeed 的搜索结果页……），
# 里面是一堆不同职位，不是某一条。这种页面被当成单条职位塞进结果后：
#   - fit_score 拿一整页杂七杂八的正文去算，分数没意义
#   - "用这条生成简历" 会拿这页 soup 去定制简历
#   - "自动投递" 会拿列表页 URL 去填表，必然出错
# 所以 platform_type == "job" 时，把明显是列表页的结果直接过滤掉。
# ============================================================================

# 明确是"单条职位详情"的 URL 形态——命中就直接放行，不再做列表页判断，
# 避免把 boards.greenhouse.io/acme/jobs/123456 这种真实详情页误杀。
_JOB_DETAIL_URL_RE = re.compile(
    r"greenhouse\.io/[^/]+/jobs/\d+"
    r"|(jobs\.)?lever\.co/[^/]+/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
    r"|jobs\.ashbyhq\.com/[^/]+/[0-9a-f]{8}-[0-9a-f]{4}"
    r"|myworkdayjobs\.com/.+/job/"
    r"|linkedin\.com/jobs/view/\d+"
    r"|indeed\.com/(viewjob|rc/clk)"
    r"|[?&](jk|gh_jid|lever-source|gh_src)="
    r"|smartrecruiters\.com/[^/]+/\d{6,}"
    r"|(jobs\.)?workable\.com/(j|view)/[a-z0-9]+"
    r"|/(job|jobs|position|posting|opening)/\d{3,}"          # 泛化：路径里带具体数字 id
    r"|reddit\.com/r/[^/]+/comments/[a-z0-9]+/"              # Reddit 单帖（招聘帖）也是单条内容
    r"|/(job|jobs|careers?)/[a-z0-9-]{3,}-[a-z0-9]*\d[a-z0-9]*$",  # 带 slug + 尾部含数字的具体 id
    re.IGNORECASE,
)

# 这些站点本身就是"职位搜索引擎"——它上面除了明确的详情页（已被上面的
# _JOB_DETAIL_URL_RE 放行）以外，基本都是搜索/列表页，直接按列表页处理。
_JOB_SEARCH_ENGINE_HOSTS = (
    "ziprecruiter.com", "indeed.com", "glassdoor.com", "glassdoor.ca",
    "monster.com", "dice.com", "simplyhired.com", "talent.com",
    "wellfound.com", "linkedin.com", "crossover.com", "remoterocketship.com",
    "dailyremote.com", "wantremote.com", "jobright.ai", "adzuna.com",
)

# 明确是"列表/搜索/分类页"的 URL 路径特征。
_JOB_LISTING_PATH_RE = re.compile(
    r"/remote-jobs(/|$)"
    r"|/role/[lr]/"
    r"|/jobs/search"
    r"|/jobs/collections"
    r"|/job-search(/|$)"
    r"|/search(/|$)"
    r"|/jobs-in-"
    r"|/find-work(/|$)"
    r"|/find-(a-)?jobs?(/|$)"
    r"|/(q|k|l)-[^/]*-jobs"
    r"|/browse(-jobs)?(/|$)"
    r"|/categor(y|ies)/"
    r"|/jobs/[a-z-]+-jobs(/|$)",       # arc.dev/remote-jobs/llm 之外的 /jobs/python-jobs 变体
    re.IGNORECASE,
)

# 路径就停在这些词、后面没有具体 slug/id：也是列表页（公司 careers 落地页那种）。
_JOB_LISTING_PATH_TAIL_RE = re.compile(
    r"/(jobs|careers?|openings|positions|vacancies|opportunities|roles)$",
    re.IGNORECASE,
)

# 列表页在 URL query 里常见的搜索参数键。
_JOB_LISTING_QUERY_KEYS = {"q", "query", "keyword", "keywords", "search", "searchkeyword", "k", "l"}

# 标题层面的信号——聚合站的 SEO 标题模式（"XX Jobs in YY"、"XX Jobs (Sep 2026)"、
# "1,200 XX Jobs"、"Browse/Explore/Find XX Jobs"），单条职位标题几乎不会长这样。
_JOB_LISTING_TITLE_RE = re.compile(
    r"\bjobs\b\s*(\(|\bin\b|\bnear\b|-|\||$)"
    r"|^\s*\d[\d,]*\+?\s+\S.*\bjobs\b"
    r"|\b(browse|explore|find|search|latest|top|best|all)\b[^.]{0,40}\bjobs\b",
    re.IGNORECASE,
)


def looks_like_job_listing_page(url: str, title: str = "") -> bool:
    """
    这个 URL / 标题看起来是"职位聚合 / 搜索 / 分类列表页"（一页里一堆不同职位），
    而不是单条职位详情页。用于把 AnySearch 搜到的这类页面从求职结果里剔除。
    """
    if not url:
        return False

    u = url.strip().lower()
    parts = urlsplit(u)
    host = parts.netloc.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    query_keys = set(parse_qs(parts.query).keys())

    # 1) 一眼就是单条详情页 —— 放行
    if _JOB_DETAIL_URL_RE.search(u):
        return False

    # 2) 职位搜索引擎站点（除详情页外都是搜索/列表页）
    if any(host == h or host.endswith("." + h) for h in _JOB_SEARCH_ENGINE_HOSTS):
        return True

    # 3) 列表 / 搜索 / 分类路径
    if _JOB_LISTING_PATH_RE.search(path + "/"):
        return True

    # 4) 路径停在 /jobs、/careers 等，后面没有具体职位
    if _JOB_LISTING_PATH_TAIL_RE.search(path):
        return True

    # 5) jobs / careers 路径上带搜索 query 参数
    if ("job" in path or "career" in path or "job" in host) and (query_keys & _JOB_LISTING_QUERY_KEYS):
        return True

    # 6) 标题是聚合站 SEO 模式，且 URL 不是已知的单条详情形态
    if title and _JOB_LISTING_TITLE_RE.search(title):
        return True

    return False


def _headers() -> dict:
    headers = {
        "Content-Type": "application/json",
        "X-Anysearch-Client": CLIENT_HEADER,
    }
    api_key = os.getenv("ANYSEARCH_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _call_tool(tool_name: str, arguments: dict, timeout: int = 30) -> str:
    """
    调用 AnySearch 的 JSON-RPC 接口（method="tools/call"），
    返回 result.content 里第一个 type=="text" 的文本内容。
    协议细节对齐官方 CLI 的 _call_api 实现。
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    resp = requests.post(ENDPOINT, json=payload, headers=_headers(), timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise RuntimeError(f"AnySearch API 报错: {data['error'].get('message', data['error'])}")

    result = data.get("result", {})
    for item in result.get("content", []):
        if item.get("type") == "text":
            return item.get("text", "")
    return json.dumps(result, ensure_ascii=False)


def _strip_rank_prefix(title: str) -> str:
    """
    去掉标题开头 AnySearch 自己拼上去的 Markdown 标题符号 + 排名编号，
    比如 "### 1. " / "1. " / "2) "。

    之前这里只处理纯数字前缀（"1. "），实测发现真实响应格式是 Markdown
    （"### 1. 标题"），数字前面还有 "### " 这三个井号，导致正则匹配不上、
    白改了一次——这次把"井号/短横线/星号等 Markdown 符号"和"数字编号"
    放进同一个正则一起处理，不分两步，避免再出现顺序错位的问题。
    """
    title = (title or "").strip()
    title = re.sub(r"^[#\-\*\s]*\d+[\.\)]\s*", "", title)
    return title.strip("-*# \t")


def _parse_results_text(text: str) -> list[dict]:
    """
    把 search 工具返回的文本解析成 [{title, url, content, posted_at}, ...]。

    两层兜底：
    1. 优先当 JSON 解析：支持外层直接是列表，或包在 results/data/items 键下面；
       字段名也做了多个候选（title/name，url/link，content/snippet/description）。
       实测下来 AnySearch 目前走的都是下面第2种 Markdown 格式，这层基本用不上，
       留着做未来接口变化的兜底。
    2. JSON 解析失败就当 Markdown 文本处理：按空行分段，每段当一条结果。

    实测样本（2026-08-18，anonymous 访问，真实响应，两次不同 query 都验证过）：

        ## Search Results (3 results, 329ms)

        ### 1. Remote AI & ML Engineer | Hire Pierluigi C. | RemoteAI
        - **URL**: https://remoteai.io/v2/hire/@pierluigi
        - Remote AI & ML Engineer | Hire Pierluigi C. | RemoteAI # Pierluigi C. ...(一大段正文)...

        ### 2. ...

    要点：
    - 开头 "## Search Results (...)" 是汇总行，没有 URL，直接跳过不当结果。
    - 每条结果标题行是 "### N. 标题"（注意 Markdown 三级标题符号 "### "
      在编号前面），下一行是 "- **URL**: <url>"，再下面是一段正文
      （不同站点丰富程度差别很大：remoteai.io 这种会带大段简介，
      之前测过的 reddit/tiktok 类结果可能就只有标题+链接，没有额外正文）。
    - 这里会把标题行、URL 那一行从 content 里去掉，只保留正文部分；
      如果去掉之后是空的（说明这条结果没有额外正文，或者格式和预期不一样），
      就退回用整段原始文本当 content，保底不让 content 变空。
    """
    text = (text or "").strip()
    if not text:
        return []

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if parsed is not None:
        if isinstance(parsed, list):
            items = parsed
        elif isinstance(parsed, dict):
            items = parsed.get("results") or parsed.get("data") or parsed.get("items") or []
        else:
            items = []

        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            out.append({
                # AnySearch 的 JSON 结果里，title 字段本身可能就带着 "1. " 这种排名前缀
                # （实测样本证实：5条结果分别来自5个不同域名，编号却精确对应搜索排名，
                # 不可能是原网页标题自带的，是 AnySearch 自己拼进 title 字段里的），
                # 所以这里也要过一遍 _strip_rank_prefix，不能只在纯文本兜底分支处理。
                "title": _strip_rank_prefix(it.get("title") or it.get("name") or ""),
                "url": it.get("url") or it.get("link") or "",
                "content": it.get("content") or it.get("snippet") or it.get("description") or "",
                "posted_at": it.get("published_at") or it.get("date") or it.get("posted_at"),
            })
        if out:
            return out

    # 兜底：纯文本/Markdown，按空行分段
    out = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        url_match = _URL_PATTERN.search(block)
        if not url_match:
            # 没有 URL 的段落不是真正的结果，是汇总/说明性文字
            # （实测样本里第一行 "Search Results (N results, Xms)" 就是这种），跳过。
            continue
        lines = block.splitlines()
        title = _strip_rank_prefix(lines[0])

        # content：把第一行（标题）和紧跟着的 URL 行（比如 "- **URL**: <url>"，
        # 也兼容没有 **URL** 前缀、单独一行就是链接的简单格式）去掉，剩下的当正文。
        # 直接判断"这一行里有没有出现刚才提取到的 URL"，比用正则精确匹配 Markdown
        # 装饰符号（**、-等）更稳，不用管具体是哪种前缀格式。
        body_lines = lines[1:]
        if body_lines and url_match.group(0) in body_lines[0]:
            body_lines = body_lines[1:]
        content = "\n".join(body_lines).strip()
        if not content:
            # 去掉标题+URL 之后是空的，说明这条结果没有额外正文，
            # 退回整段原文当 content，至少还留着标题信息，不让 content 变空。
            content = block

        out.append({
            "title": title[:120],
            "url": url_match.group(0),
            "content": content,
            "posted_at": None,
        })
    return out


# AnySearch extract 工具返回错误时并不会走 HTTP 错误码或 JSON-RPC error 字段，
# 而是把一条"看起来像正文"的错误提示字符串塞进正常的 result.content 里返回。
# 实测样本（2026-08-18，Reddit 全站）：
#   "extract_invalid_content Response content is not valid for extraction."
# 如果不识别这种"伪正文错误信息"，会被当成有效正文塞进 UnifiedRecord.content，
# 下游 LLM 会把这句错误当成"这条帖子的招聘描述"去过滤/生成简历，直接翻车。
# 这里用几个明显的错误标记做子串匹配（放宽一点，未来 AnySearch 加别的错误
# 类型也能顺便盖住），命中就当抓取失败处理。
_EXTRACT_ERROR_MARKERS = (
    "extract_invalid_content",
    "extract_error",
    "extract_failed",
    "not valid for extraction",
    "content is not valid",
    "unable to extract content",
)


def _is_extract_error(text: str) -> bool:
    if not text:
        return True
    low = text.lower()
    return any(marker.lower() in low for marker in _EXTRACT_ERROR_MARKERS)


def _extract_content(url: str, timeout: int = 20) -> str:
    """
    调用 AnySearch 的 extract 工具，抓取单个 URL 的正文（返回 Markdown）。

    单条抓取失败（网络异常，或 AnySearch 返回 extract_invalid_content 这类
    "伪正文错误信息"）时统一返回空字符串、不抛异常——不能因为一条结果抓不到
    正文就搞挂整批搜索。返回空字符串后，上层 search() 会保留 search 阶段
    自带的原始 content（一般是"标题+URL"这种稀薄内容），至少不会把错误信息
    伪装成正文塞给下游 LLM。
    """
    try:
        result = _call_tool("extract", {"url": url}, timeout=timeout)
    except Exception as e:
        print(f"  [AnySearch] extract 网络/协议异常 url={url}: {e}")
        return ""

    if _is_extract_error(result):
        print(f"  [AnySearch] extract 被拒（大概率站点反爬）url={url}: {result[:80]}")
        return ""

    # 实测发现 extract 成功时返回的是一整段 JSON 文本
    # （{"url":...,"title":...,"content":...}），不是纯 markdown 正文；
    # 之前直接把这段 JSON 原文塞进 content，导致下游拿到的是带引号/转义符的
    # JSON 字面量而不是真正的正文。这里尝试解析一次，取里面的 content 字段；
    # 解析失败或没有 content 字段就退回用原始文本（兼容纯文本响应）。
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and parsed.get("content"):
        return parsed["content"]
    return result


# AnySearch 匿名调用有时不会返回搜索结果，而是返回"已经自动帮你生成了一个新
# 账号"的引导文案（实测坐实：换了好几个不同的 query，返回的都是同一组
# username/password/api_key，不是随机内容，说明是服务端针对这个匿名客户端
# 已经生成过一次账号了，之后匿名调用不再处理真实搜索，改成一直发这段文案）。
# 这段文案里没有 URL，_parse_results_text 识别不出任何"结果段落"，会静默
# 解析成 0 条结果——调用方（run_search_agent 的逐 query try/except）看到的
# 只是"这条 query 搜到 0 条"，跟"真的没搜到内容"完全区分不出来，实测就是
# 靠这个误判走了两轮、10 条 query 全 0 结果都没报错，直到手动扒开
# _call_tool 的原始返回才找到真正原因。
# 这里识别出这种响应就直接抛异常，让上层记录成一次真实的失败（"AnySearch
# 返回了匿名账号引导文案，不是搜索结果"），而不是被 0 结果悄悄盖过去。
_ANONYMOUS_ONBOARDING_MARKERS = ("automatically generated", "api_key=")


def _is_anonymous_onboarding_response(text: str) -> bool:
    low = (text or "").lower()
    return all(marker in low for marker in _ANONYMOUS_ONBOARDING_MARKERS)


def search(
    query: str,
    platform_type: str = "business",
    max_results: int = 10,
    extract_content: bool = True,
    max_content_chars: int = 3000,
) -> list[UnifiedRecord]:
    """
    和 tavily_connector.search() 保持一致的调用签名（query/platform_type/max_results），
    方便在 search_agent.py 里直接替换调用点。

    query: 搜索关键词
    platform_type: "job" 或 "business"，标记这条搜索属于哪个 Tab

    extract_content: search 结果里不同站点自带的正文丰富程度差别很大——实测
        remoteai.io 这类会带大段简介，但之前测过 reddit/tiktok 类结果基本只有
        标题+链接。这和 Tavily 稳定带正文摘要不一样。所以默认（True）会对
        "自带正文太短"的结果（少于 min_content_chars_before_extract 个字符）
        再并发调一次 extract 工具补正文；已经有足够正文的结果不会重复调用，
        省调用次数/额度。
        代价：请求数最多变成"每个query 1次 search + N次 extract"（N = 正文不够
        的结果条数，不是固定 max_results 次）。主要影响：1) 匿名访问的限流额度
        消耗更快，量大的话建议尽快去注册一个 API key（见文件头部说明）；
        2) social_media 类平台（TikTok/Instagram 这种重度依赖 JS 渲染的页面）
        extract 抓不抓得到完整正文不确定，需要实测——Reddit / remoteai.io 这类
        服务端渲染或自带简介的站点通常没问题。
        如果只是想要"轻量、少请求"的行为，传 extract_content=False。
    max_content_chars: 单条正文最多保留多少字符（extract 官方最多给 50000 字符，
        直接全量塞进 LLM 上下文太浪费 token，这里截断一下；search 自带的正文
        同样受这个上限约束，避免像 remoteai.io 那种大段简介把 token 吃爆）。
    """
    min_content_chars_before_extract = 200

    arguments = {"query": query, "max_results": min(max_results, 10)}
    text = _call_tool("search", arguments)

    if _is_anonymous_onboarding_response(text):
        raise RuntimeError(
            "AnySearch 返回的是匿名账号自动引导文案，不是搜索结果——"
            "说明 ANYSEARCH_API_KEY 未生效（未配置 / 已失效 / 超出额度）。"
            "请检查 .env 里的 ANYSEARCH_API_KEY。"
        )

    parsed_items = _parse_results_text(text)

    # 求职场景：把明显是"职位列表/搜索/分类页"的结果剔掉——它们不是单条职位，
    # 拿去定制简历 / 自动投递都是错的。放在 extract 之前，顺带省掉对这些页面的
    # extract 调用。business 场景不动（Tab A 有自己的 filter_real_leads 逻辑）。
    if platform_type == "job" and parsed_items:
        kept, dropped = [], []
        for item in parsed_items:
            if looks_like_job_listing_page(item.get("url", ""), item.get("title", "")):
                dropped.append(item)
            else:
                kept.append(item)
        if dropped:
            print(
                f"  [AnySearch] 过滤掉 {len(dropped)} 条职位列表/聚合页（非单条职位）："
                + "; ".join(f"{d.get('title', '')!r} <{d.get('url', '')}>" for d in dropped)
            )
        parsed_items = kept

    if extract_content and parsed_items:
        urls_to_extract = {
            item["url"] for item in parsed_items
            if item.get("url") and len(item.get("content") or "") < min_content_chars_before_extract
        }
        if urls_to_extract:
            with ThreadPoolExecutor(max_workers=min(5, len(urls_to_extract))) as pool:
                future_to_url = {pool.submit(_extract_content, url): url for url in urls_to_extract}
                extracted_by_url = {}
                for future in as_completed(future_to_url):
                    url = future_to_url[future]
                    extracted_by_url[url] = future.result()

            for item in parsed_items:
                extracted = extracted_by_url.get(item.get("url"), "")
                if extracted:
                    item["content"] = extracted

    for item in parsed_items:
        item["content"] = (item.get("content") or "")[:max_content_chars]

    records = []
    for item in parsed_items:
        records.append(UnifiedRecord(
            source="anysearch",
            title=(item.get("title") or "")[:120],
            content=item.get("content", ""),
            url=item.get("url", ""),
            posted_at=item.get("posted_at") or datetime.now(timezone.utc).isoformat(),
            platform_type=platform_type,
            author=None,
        ))
    return records


class AnySearchConnector(BaseDataConnector):
    """
    AnySearch 连接器，按 BaseDataConnector 规范包装 search()。
    """

    source_name = "anysearch"

    def fetch(self, query: str, platform_type: str = "business", max_results: int = 10, **kwargs) -> list[UnifiedRecord]:
        return search(query=query, platform_type=platform_type, max_results=max_results, **kwargs)


def _print_records(records):
    """把每条结果的标题、链接、正文长度、正文预览都打出来，方便肉眼判断质量。"""
    for r in records:
        print("-" * 60)
        print("title      :", r.title)
        print("url        :", r.url)
        print("content_len:", len(r.content))
        print("content    :", r.content[:250].replace("\n", " "))


if __name__ == "__main__":
    # 直接运行测试：python -m src.connectors.anysearch_connector
    # ⚠️ 需要能访问 api.anysearch.com 的网络环境。
    print("=" * 60)
    print("测试1：求职场景关键词")
    job_results = search(
        query="site:reddit.com r/forhire remote AI engineer python",
        platform_type="job",
        max_results=5,
    )
    print(f"返回 {len(job_results)} 条结果\n")
    _print_records(job_results)

    print("\n" + "=" * 60)
    print("测试2：跨境商机场景关键词")
    biz_results = search(
        query="looking for TikTok affiliate pet products",
        platform_type="business",
        max_results=5,
    )
    print(f"返回 {len(biz_results)} 条结果\n")
    _print_records(biz_results)
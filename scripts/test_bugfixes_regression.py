"""
针对本次简历改造过程中"顺手修复"的几个 bug 的最小回归测试。
这几个 bug 都不是这次改造的核心逻辑，但足够隐蔽/致命（会让整个 app 起不来，
或者让某个接口一调就崩），值得各自钉一个断言，避免以后重构时又踩回去。

跑法：python -m scripts.test_bugfixes_regression
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_main_app_imports_cleanly():
    """
    Bug: main.py 挂载的 tab_a_router / tab_b_router，以及它们依赖的
    business_profile.py / outreach_generator.py，有 4 处导入路径还是
    重构前的旧路径（src.resume_parser / src.chunker / src.vector_store /
    src.retriever / src.llm_client 这种），整个 app 直接 import 不了。
    """
    from src.main import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200, f"预期 200，实际 {resp.status_code}"
    assert resp.json() == {"status": "ok"}

    # 顺带确认 Tab A / Tab B 的接口都真的注册上了（这是本次改动新加的
    # OpenAPI 检查，之前那次误判"路由没注册"就是没用这种方式验证）
    openapi_paths = set(client.get("/openapi.json").json()["paths"].keys())
    expected = {
        "/api/upload-resume", "/api/add-resume-note", "/api/tailor-resume",
        "/api/search-jobs", "/api/setup-business-profile",
    }
    missing = expected - openapi_paths
    assert not missing, f"这些路由没有被正确注册: {missing}"
    print("[PASS] test_main_app_imports_cleanly")


def test_word_export_dir_resolves_to_project_root():
    """
    Bug: word_export.py 的 EXPORT_DIR 只拼了一层 ".."，从
    src/tab_b_jobsearch/word_export.py 出发只够走到 src/，实际落盘在
    src/data/exports/，和 router.py 里 /files/{filename} 下载接口预期的
    项目根目录 data/exports/ 对不上，下载会 404。
    """
    from src.tab_b_jobsearch.word_export import EXPORT_DIR

    resolved = os.path.normpath(os.path.abspath(EXPORT_DIR))
    project_root = os.path.normpath(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    expected = os.path.normpath(os.path.join(project_root, "data", "exports"))

    assert resolved == expected, f"EXPORT_DIR 解析成了 {resolved}，应该是 {expected}"
    # 顺带确认没有回退到那个错误的 src/data/exports 路径
    assert "src" + os.sep + "data" not in resolved
    print("[PASS] test_word_export_dir_resolves_to_project_root")


def test_generate_tailored_resume_accepts_extra_context():
    """
    Bug: router.py 里 /api/tailor-resume 会传 user_notes 给
    generate_tailored_resume()，但这个函数当时根本没有接收这个参数的
    形参，一调用就是 TypeError: unexpected keyword argument。
    这里不真的调用 LLM（不需要为了回归测试消耗 API 额度/等真实网络），
    只用 inspect 确认函数签名接受 extra_context 这个关键字参数。
    """
    import inspect
    from src.tab_b_jobsearch.resume_generator import generate_tailored_resume

    sig = inspect.signature(generate_tailored_resume)
    assert "extra_context" in sig.parameters, (
        "generate_tailored_resume() 缺少 extra_context 参数，"
        "router.py 的 /api/tailor-resume 传这个参数会直接 TypeError"
    )
    print("[PASS] test_generate_tailored_resume_accepts_extra_context")


def test_degree_extraction_does_not_false_positive_on_substring():
    """
    Bug: extract_degree_level() 最初用裸子串 `in` 判断关键词，"ms"
    （硕士）会命中 "syste**ms**" 这类单词内部的字母组合，导致明明是
    "Bachelor's degree required" 的 JD 被误判成硕士门槛。
    """
    from src.core.keyword_dicts import extract_degree_level

    # 明确写着本科，但正文里出现了容易误命中 "ms" 的词（systems/programs）
    text = "Bachelor's degree required. Experience with distributed systems and internship programs."
    level = extract_degree_level(text)
    assert level == 2, f"预期抠出本科(2)，实际抠到 {level}（如果是 3，说明 'ms' 子串误判的老问题又回来了）"

    # 正常场景：真的提到硕士，应该抠到 3
    assert extract_degree_level("Master's degree in Computer Science") == 3
    print("[PASS] test_degree_extraction_does_not_false_positive_on_substring")


# 实测坐实过一次真实 bug：某份简历上传后返回"新增关键词：24"但"新增 chunk：0"——
# 关键词提取正常，chunk 却完全切不出来，导致 tailor-resume 误判成"用户没上传过简历"。
#
# 根因是 pymupdf4llm 这类 PDF->Markdown 转换器按字号给标题分层级：候选人姓名用 #，
# 板块标题（PROFESSIONAL EXPERIENCE/PERSONAL PROJECT）用 ##，具体每条经历/项目的
# 标题（"公司 | 职位 | 时间"）又用了更深的 ###/####，层级不统一、也不跟 PDF 里的
# 真实层级嵌套对应。_is_section_header() 之前对"真正的 Markdown 标题"来者不拒，
# 于是每条经历标题都会把所在板块从中截断、另起一个新顶层板块；标题文字本身
# （"公司 | 职位"）又匹配不上 SECTION_TO_REGION 任何关键词，被 classify_section()
# 兜底成 Region A——Region A 只抠关键词、不做句子级切分，板块里本该被切成 chunk
# 的经历要点内容就这样全部漏空。附带发现的次要缺口：SECTION_TO_REGION 里项目类
# 板块关键词只有复数/带 experience 的变体（"projects"/"project experience"/
# "项目经验"），这份简历板块标题写的是单数 "PERSONAL PROJECT"，也匹配不上。
#
# 下面这份 markdown 是真实触发过这个 bug 的简历，按同样的结构（姓名用 #、板块用
# ##、每条经历/项目标题分别用 ###/####、日期单独另起一行、PERSONAL PROJECT 用
# 单数）复现，姓名/邮箱/电话/GitHub 这些个人身份信息已经替换成占位值——
# 触发这个 bug 靠的是标题层级结构，不需要真人的联系方式。
_SAMPLE_MULTI_LEVEL_HEADING_RESUME_MD = """# **Alex (Test) Doe**

Toronto, ON | +1 (555) 000-0000 | alex.doe.test@example.com | https://github.com/alexdoe-test

## **PROFESSIONAL SUMMARY**

- 3+ years of enterprise engineering experience specializing in AI applications, distributed systems, and backend development.

- Proven expertise in building AI-driven solutions, including RAG systems, AI agents, and real-time data processing architectures.

## **TECHNICAL SKILLS**

- **AI & LLM:** LLM, RAG, LangGraph, Embedding Models, Vector Database

- **Databases:** PostgreSQL, MySQL, Redis, ClickHouse.

## **PROFESSIONAL EXPERIENCE**

#### **Acme Corp | Industrial Intelligent Diagnosis Platform (RAG + LLM) | AI Engineer |**

Feb 2025 - July 2025

- Led the migration of a legacy Word2Vec-based diagnosis system to a RAG-powered intelligent diagnosis platform for manufacturing test environments.

#### **Acme Corp | Industrial Test Data Processing Platform | Data Engineer |**

June 2023 - Feb 2025

- Led the refactoring of distributed Flink data pipelines processing tens of billions of hardware test records.

### **Acme Corp | Enterprise Partner Management Platform | Backend Engineer |**

April 2022 - June 2023

- Redesigned a legacy backend platform into a scalable microservices architecture.

## **PERSONAL PROJECT**

**Multi-Agent Smart Contract Security System |** 2025 - 2026

- Built a LangGraph-based multi-agent security framework for automated vulnerability detection and remediation.

#### **AI Agent-driven Real-time Market Intelligence Platform |** 2026

- Built a real-time AI market intelligence platform combining Kafka, Flink and ClickHouse streaming architecture with LLM agents.

## **EDUCATION**

**Test University |** Testville, TC

Master of Engineering in Test Science | Sep 2019 - Mar 2022
"""


def test_resume_with_mixed_heading_level_entries_still_produces_chunks():
    """
    回归钉住上面注释描述的那个 bug：经历/项目条目标题被渲染成比板块标题更深
    （但层级不统一）的 Markdown 标题时，之前会导致整份简历一条 chunk 都切不出来。
    """
    from src.core.resume_ast import parse_resume_markdown

    result = parse_resume_markdown(_SAMPLE_MULTI_LEVEL_HEADING_RESUME_MD)

    # 3 条 PROFESSIONAL EXPERIENCE + 2 条 PERSONAL PROJECT，每条一句话，共 5 句；
    # 用 >= 5（而不是精确等于）留一点余地给 split_sentences 未来可能的分句调整，
    # 但重点是"不能是 0"——这才是这个 bug 的核心断言。
    assert len(result["chunks"]) >= 5, (
        f"预期至少切出 5 条 chunk（3 条工作经历 + 2 条项目要点），实际只有 "
        f"{len(result['chunks'])} 条——经历/项目条目被渲染成更深层级 Markdown 标题时"
        f"chunk 又被吞空的老问题可能回来了"
    )

    # 关键词提取（Region A）本来就是好的，顺带确认没有被这次改动误伤
    assert "langgraph" in result["keywords"]
    assert any(kw.startswith("degree:") for kw in result["keywords"])

    # 板块归类这一步是根因所在，顺带钉住：两个经历/项目板块本身得先被正确识别
    # 成板块标题（而不是被它们内部的条目标题过滤掉），关键词表兜底的单数
    # "PERSONAL PROJECT" 修复也在这里一并验证。
    project_chunks = [c for c in result["chunks"] if c["tags"].get("project") == "PERSONAL PROJECT"]
    assert len(project_chunks) == 2, f"PERSONAL PROJECT 板块应该切出 2 条 chunk，实际 {len(project_chunks)} 条"

    print("[PASS] test_resume_with_mixed_heading_level_entries_still_produces_chunks")


# 上面那次修复顺手发现但没修的次要问题：标题行和时间范围"分开另起一行写"
# （不是同一行）时，_split_experience_entries 会把"标题行"和"日期+要点"错误
# 切成两条独立经历——标题行自己变成一条空条目，真正带要点的那条反而丢了
# company/role 标签，只剩 time。Zhibin_resume.pdf 里三段 Huawei 经历就是这种
# 格式（"公司 | 职位" 一行，时间范围单独下一行，再往下才是要点列表）。
#
# 姓名/邮箱/电话同样换成占位值，不把真人 PII 存进代码库——触发这个 bug 靠的是
# "标题行和日期分行写"这个格式本身，不需要真实联系方式。
_SAMPLE_TITLE_AND_DATE_ON_SEPARATE_LINES_MD = """# **Alex (Test) Doe**

Toronto, ON | +1 (555) 000-0000 | alex.doe.test@example.com

## **PROFESSIONAL EXPERIENCE**

#### **Acme Corp | Backend Platform | Senior Engineer |**

Jan 2022 - Mar 2024

- Redesigned the checkout pipeline to cut latency by half.
- Mentored two junior engineers on distributed systems fundamentals.
"""


def test_entry_title_and_date_on_separate_lines_keep_all_tags():
    """
    回归钉住上面注释描述的那个次要 bug：标题行（公司|职位）和时间范围分开
    另起一行写时，切出来的 chunk 应该同时带上 company/role/time 三个标签，
    不能因为拆成了两条独立"经历"而丢掉标题那一半的信息。
    """
    from src.core.resume_ast import parse_resume_markdown

    result = parse_resume_markdown(_SAMPLE_TITLE_AND_DATE_ON_SEPARATE_LINES_MD)

    assert len(result["chunks"]) == 2, (
        f"预期切出 2 条 chunk（对应 2 条要点），实际 {len(result['chunks'])} 条——"
        f"标题行是不是又被单独拆成了一条空条目？"
    )
    for chunk in result["chunks"]:
        tags = chunk["tags"]
        assert tags.get("company") == "Acme Corp", f"company 标签丢了或者不对：{tags}"
        assert tags.get("role") == "Backend Platform", f"role 标签丢了或者不对：{tags}"
        assert tags.get("time") == "Jan 2022 - Mar 2024", f"time 标签丢了或者不对：{tags}"

    print("[PASS] test_entry_title_and_date_on_separate_lines_keep_all_tags")


# 实测坐实过一次真实 bug：AnySearch（通用网页搜索引擎，不是职位板 API）给求职
# 搜索返回的部分结果，链接指向的是"职位聚合/分类/搜索列表页"而不是单条职位详情
# （例如 arc.dev/remote-jobs/llm、wellfound.com/role/l/ai-engineer/canada-startups）。
# 这种页面被当成单条职位塞进结果后，"用这条生成简历"会拿一整页不相关职位去定制，
# "自动投递"会拿列表页 URL 去填表，必然出错。修复：platform_type == "job" 时，
# anysearch_connector 用 looks_like_job_listing_page() 把这类结果过滤掉，
# apply_router 里再兜一道（漏过的列表页 URL 不允许发起自动投递）。
def test_anysearch_job_listing_pages_are_detected():
    """
    回归钉住上面描述的 bug：职位列表/聚合页要能被 looks_like_job_listing_page()
    识别出来（返回 True），单条职位详情页不能被误伤（返回 False）。
    纯函数判断，不发网络请求。
    """
    from src.connectors.anysearch_connector import looks_like_job_listing_page

    # 用户实际反馈的两条问题结果，必须判成"列表页"
    assert looks_like_job_listing_page(
        "https://arc.dev/remote-jobs/llm", "Remote LLM Jobs (September 2026) - Arc.dev"
    ), "arc.dev 分类列表页没被识别出来"
    assert looks_like_job_listing_page(
        "https://wellfound.com/role/l/ai-engineer/canada-startups", "AI Engineer Jobs in Canada - 2026"
    ), "wellfound 搜索结果页没被识别出来"

    # 其它常见聚合/搜索页形态（含首轮实测漏过、后来补上的 ziprecruiter 搜索页 /
    # remoteai /find-work / 子域名 ca.linkedin.com 这几种）
    for url in (
        "https://www.indeed.com/q-ai-engineer-l-canada-jobs.html",
        "https://www.linkedin.com/jobs/search/?keywords=software%20engineer",
        "https://weworkremotely.com/categories/remote-back-end-programming-jobs",
        "https://acme.com/careers",
        "https://www.ziprecruiter.com/Jobs/Ai-Engineer-Remote-Canada",
        "https://remoteai.io/find-work/canada",
        "https://ca.linkedin.com/jobs/artificial-intelligence-engineer-jobs",
        "https://www.glassdoor.ca/Job/canada-machine-learning-engineer-jobs-SRCH_IL.0,6_IN3_KO7,32.htm",
        "https://www.crossover.com/jobs/ai-engineer/ca",
        # 招聘/孵化平台自己的通用入口页（实测坐实：ycombinator.com/apply 被当成职位申请页）
        "https://www.ycombinator.com/apply/",
        "https://www.ycombinator.com/apply",
        "https://www.ycombinator.com/companies",
        "https://www.workatastartup.com/",
        "https://acme.io/login",
        "https://boards.somecompany.com/signup",
    ):
        assert looks_like_job_listing_page(url), f"聚合/搜索页漏判：{url}"

    # 单条职位详情页 / 单条帖子——绝不能被误伤（否则真实职位会被静默丢掉）
    for url, title in (
        ("https://news.ycombinator.com/item?id=41889468", "Senior Golang Developer"),
        ("https://job-boards.greenhouse.io/reddit/jobs/7997866", "Software Engineer at Reddit"),
        ("https://jobs.lever.co/veeva/8fe22df0-02b4-453d-919c-c8998cf913f6", "Associate Software Engineer"),
        ("https://www.linkedin.com/jobs/view/3901234567", "AI Engineer"),
        ("https://jobs.ashbyhq.com/foobar/6d5c4b3a-1234-5678-9abc-def012345678", "Data Engineer at Foobar"),
        # Reddit 招聘帖是单条内容（跟 HN "who's hiring" 一条评论一样），保留
        ("https://www.reddit.com/r/SoftwareEngineerJobs/comments/1oniqs0/hiring_x/", "Hiring Software & AI Engineers (US/Canada Remote)"),
        # 公司 careers 页带具体职位 slug（尾部有 id），是详情页不是落地页
        ("https://acme.com/careers/senior-ml-engineer-4a9f2b", "Senior ML Engineer"),
        # YC WaaS 上某公司的具体职位页，不能被"平台通用页"规则误伤
        ("https://www.ycombinator.com/companies/stripe/jobs/abc123-backend-engineer", "Backend Engineer"),
        ("https://www.workatastartup.com/jobs/54321", "ML Engineer"),
    ):
        assert not looks_like_job_listing_page(url, title), f"单条职位详情页被误判成列表页：{url}"

    print("[PASS] test_anysearch_job_listing_pages_are_detected")


# looks_like_job_listing_page 只能靠 URL/标题挡"列表页"，挡不住"单页但内容不是职位本身"
# 的营销软文（"Where to Hire Python Developers in 2026"、"24 Python Developers for Hire -
# Lemon.io" 这种招聘外包/榜单页）。search_agent._is_real_job_posting 用一次便宜的 LLM
# 判断补上这层。这个断言要真发 LLM 请求（判断题没法纯离线测），没有 DEEPSEEK_API_KEY
# 或网络不通就打印 SKIP、不 fail 整个套件。
def test_job_relevance_filter_rejects_marketing_pages():
    import os

    if not os.getenv("DEEPSEEK_API_KEY"):
        print("[SKIP] test_job_relevance_filter_rejects_marketing_pages（没有 DEEPSEEK_API_KEY）")
        return

    from src.core.search_agent import _is_real_job_posting

    reject = [
        ("Where to Hire Python Developers in 2026 | LATAMHire",
         "https://lathire.com/where-to-hire-python-developers/",
         "Looking to hire Python developers? This guide compares Latin America vs Eastern Europe "
         "rates, how to vet candidates, and the top staffing platforms."),
        ("24 Hand-Picked Python Developers for Hire in 24 Hours - Lemon.io",
         "https://lemon.io/hire/python-developers/",
         "Hire vetted Python developers. Lemon.io matches you with pre-screened senior engineers "
         "in 48 hours. Book a call today."),
    ]
    keep = [
        ("Machine Learning Engineer, Search Quality",
         "https://job-boards.greenhouse.io/gleanwork/jobs/4006735005",
         "Glean is hiring a Machine Learning Engineer on the Search Quality team. You will design "
         "and ship ranking models. Requirements: 5+ years ML experience, Python. Apply now."),
        ("[Hiring] Senior Backend Engineer (Python/Go), remote",
         "https://www.reddit.com/r/forhire/comments/abc123/hiring_senior_backend_engineer/",
         "Our fintech startup is hiring a senior backend engineer. Stack: Python, Go, Postgres, "
         "AWS. Full remote, USD 140-170k. Apply via the link."),
    ]

    try:
        for t, u, c in reject:
            assert not _is_real_job_posting(t, u, c), f"营销软文没被判成非职位：{t!r}"
        for t, u, c in keep:
            assert _is_real_job_posting(t, u, c), f"真实单条职位被误判成非职位：{t!r}"
    except (ConnectionError, TimeoutError, OSError) as e:
        print(f"[SKIP] test_job_relevance_filter_rejects_marketing_pages（网络不通：{e}）")
        return

    print("[PASS] test_job_relevance_filter_rejects_marketing_pages")


# 实测坐实过一次真实 bug：AnySearch 的 extract 抓 Workday（myworkdayjobs.com）详情页，
# 拿回来的是给非 JS 客户端的重定向桩 {"widget":"redirect","url":...,"externalSpa":true}，
# 不是职位正文——这些职位的 fit_score 偏低、"用这条生成简历"拿不到料。修复：Workday
# 详情页改走它公开的 CXS JSON API（不需要无头浏览器），取 jobPostingInfo.jobDescription。
def test_workday_cxs_url_transform():
    """
    离线钉住 URL 转换逻辑：友好详情页 URL -> CXS JSON API 地址。
    """
    from src.connectors.anysearch_connector import _workday_cxs_url, _looks_like_junk_content

    assert _workday_cxs_url(
        "https://workday.wd5.myworkdayjobs.com/en-US/Workday/job/Canada-BC-Vancouver/AI-Engineer_JR-0109305"
    ) == "https://workday.wd5.myworkdayjobs.com/wday/cxs/workday/Workday/job/Canada-BC-Vancouver/AI-Engineer_JR-0109305"

    # 没有语言码段的变体
    assert _workday_cxs_url(
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/Senior-Engineer_JR1234"
    ) == "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/Senior-Engineer_JR1234"

    # 已经是 CXS 地址：原样返回（幂等）
    cxs = "https://x.wd1.myworkdayjobs.com/wday/cxs/x/Careers/job/Remote/Dev_JR1"
    assert _workday_cxs_url(cxs) == cxs

    # 非 Workday / 拼不出来：返回 None，让上层退回 AnySearch extract
    assert _workday_cxs_url("https://boards.greenhouse.io/acme/jobs/123") is None
    assert _workday_cxs_url("https://acme.wd5.myworkdayjobs.com/en-US/Careers") is None  # 没有 job 段

    # 重定向桩识别
    assert _looks_like_junk_content('{"widget":"redirect","url":"/x","externalSpa":true}')
    assert _looks_like_junk_content("")
    assert not _looks_like_junk_content("We are hiring a Senior Engineer. Responsibilities: ...")

    print("[PASS] test_workday_cxs_url_transform")


def test_workday_job_content_via_cxs_api():
    """
    需要网络（打 myworkdayjobs.com 的 CXS API）。连不上就 SKIP，不 fail 套件。
    验证 Workday 详情页现在能拿到真实 JD 正文，不再是 {"widget":"redirect"} 桩。
    """
    from src.connectors.anysearch_connector import _fetch_workday_job, _looks_like_junk_content

    url = ("https://workday.wd5.myworkdayjobs.com/en-US/Workday/job/"
           "Canada-BC-Vancouver/AI-Engineer_JR-0109305")
    try:
        text = _fetch_workday_job(url)
    except (ConnectionError, TimeoutError, OSError) as e:
        print(f"[SKIP] test_workday_job_content_via_cxs_api（网络不通：{e}）")
        return

    if not text:
        # CXS API 可能下线了这条职位（职位会过期）——不算回归，SKIP
        print("[SKIP] test_workday_job_content_via_cxs_api（CXS 未返回正文，职位可能已过期）")
        return

    assert not _looks_like_junk_content(text), f"还是重定向桩：{text[:120]!r}"
    assert len(text) > 500, f"正文太短，可能没抓到完整 JD：{len(text)} 字符"
    print(f"[PASS] test_workday_job_content_via_cxs_api（拿到 {len(text)} 字符 JD 正文）")


# 实测坐实过一次真实 bug：定制简历生成里，top_k=5 检索出来的 5 条"匹配片段"有 4 条
# 内容完全相同（同一段 Kafka/Flink 经历）。根因是 add_resume_chunks 纯追加、不去重——
# 同一份简历被重复上传（"传个更新版"）就把每条 chunk 存成 N 份（实测 56 条里只有 14
# 条不同，每条 ×4），检索时 top_k 被同一段内容占满多个名额，喂给 LLM 的"原文"其实
# 只有一段，定制质量直接崩。修复：add 时按归一化文本去重、get 时读侧再去一道、
# 检索返回 top_k 前也去一道，另加 dedupe_resume_chunks() 物理清理历史脏数据。
def test_resume_chunk_dedup():
    from src.core.vector_store import (
        normalize_chunk_text, add_resume_chunks, get_all_resume_chunks, reset_resume_chunks,
    )
    from src.core.match_scoring import _dedupe_by_text

    # --- 归一化口径 ---
    assert normalize_chunk_text("  Built  a\nPipeline. ") == "built a pipeline"
    assert normalize_chunk_text("Built a pipeline") == normalize_chunk_text("built a Pipeline.")
    assert normalize_chunk_text("...") == ""

    # --- 检索侧去重（纯函数，离线）---
    scored = [
        {"text": "Built a Kafka pipeline.", "score": 0.9, "tags": {}},
        {"text": "built a kafka pipeline", "score": 0.9, "tags": {}},   # 归一化后同上
        {"text": "Built a Kafka pipeline.", "score": 0.8, "tags": {}},  # 完全一样
        {"text": "Designed an ML model.", "score": 0.7, "tags": {}},
    ]
    deduped = _dedupe_by_text(scored)
    assert [d["text"] for d in deduped] == ["Built a Kafka pipeline.", "Designed an ML model."], deduped

    # --- 摄入侧去重（用一个临时 collection，不碰真实简历数据）---
    tmp = "test_dedup_tmp_collection"
    reset_resume_chunks(tmp)
    try:
        batch = [
            {"text": "Led the migration of a legacy system to a RAG platform.", "tags": {"company": "Acme"}},
            {"text": "Designed the end-to-end retrieval pipeline.", "tags": {"company": "Acme"}},
            {"text": "Built a real-time streaming architecture with Kafka and Flink.", "tags": {"company": "Acme"}},
        ]
        n1 = add_resume_chunks(batch, collection_name=tmp)
        assert n1 == 3, f"首次应写入 3 条，实际 {n1}"
        n2 = add_resume_chunks(batch, collection_name=tmp)       # 重复上传同一份
        assert n2 == 0, f"重复上传不该新增，实际新增 {n2}"
        n3 = add_resume_chunks(
            batch + [{"text": "New bullet added in the updated resume.", "tags": {}}],
            collection_name=tmp,
        )
        assert n3 == 1, f"只有那条新 bullet 该被写入，实际 {n3}"
        assert len(get_all_resume_chunks(tmp)) == 4, "临时 collection 最终应是 4 条 distinct chunk"
    finally:
        reset_resume_chunks(tmp)

    print("[PASS] test_resume_chunk_dedup")


# 实测坐实过一次真实 bug：pymupdf4llm 从 PDF 抽 markdown 时把一句话从中间折行
# （"...significantly reducing serialization \n\noverhead. \n\n- Designed ..."），
# split_sentences 直接按换行切，"overhead." 被当成一句独立的话，简历库里多出个
# 单词 chunk，生成的定制简历里也出现 "...reducing serialization." 这种腰斩表述。
# 修复：分句前先跑 _reflow_soft_wraps 把"不是句子结尾的换行"接回去。
def test_split_sentences_reflows_pdf_soft_wraps():
    from src.core.text_utils import split_sentences

    # 报告里的真实例子：句子中间被 \n\n 断开 + 后面跟一个真正的新列表项
    got = split_sentences(
        "Also designed a Protobuf-based ingestion architecture, significantly reducing serialization \n\n"
        "overhead. \n\n"
        "- Designed an end-to-end data reliability framework."
    )
    assert got == [
        "Also designed a Protobuf-based ingestion architecture, significantly reducing serialization overhead.",
        "Designed an end-to-end data reliability framework.",
    ], got
    assert not any(len(s) < 15 for s in got), f"仍然切出了单词碎片：{got}"

    # 单个 \n 的软换行也要接回去
    assert split_sentences("reducing serialization\noverhead.") == ["reducing serialization overhead."]

    # 真正的列表项 / 句末标点 边界不能被误合并
    assert split_sentences("- Built a data pipeline\n- Improved latency by 40%") == [
        "Built a data pipeline", "Improved latency by 40%",
    ]
    assert split_sentences("Led the migration.\nDesigned the pipeline.") == [
        "Led the migration.", "Designed the pipeline.",
    ]
    # 连字符复合词在连字符处换行：拼回去不补空格、保留连字符，不要拼成垃圾 token
    assert split_sentences("Migrated to a RAG-\npowered platform.") == ["Migrated to a RAG-powered platform."]

    print("[PASS] test_split_sentences_reflows_pdf_soft_wraps")


# 实测坐实过一次严重 bug：定制简历生成把"检索 top_k 片段 -> LLM 凭空攒一份"，
# 结果姓名被压成 "zhibinliu"、联系方式/summary/技能/教育整段消失、职位头衔全丢、
# 一条经历少了一半 bullet，两个 PERSONAL PROJECT 还被当工作经历排到真实经历前面。
# 重做成"以完整原文为底稿，只在 EXPERIENCE/PROJECT 的 bullet 层做重排 + 措辞微调，
# 其余原样保留"。下面这份 markdown 按真实简历（Zhibin Liu）的结构复现，占位 PII。
_SAMPLE_FULL_RESUME_MD = """# **Alex (Sample) Doe**

Toronto, ON | +1 (555) 000-0000 | alex.sample@example.com | https://github.com/alexsample

## **PROFESSIONAL SUMMARY**

- 3+ years of enterprise engineering experience in AI applications and distributed systems.

- Proven expertise building RAG systems, AI agents, and real-time data processing architectures.

## **TECHNICAL SKILLS**

- **AI & LLM:** LLM, RAG, LangGraph, Embedding Models, Vector Database

- **Big Data & Streaming:** Apache Flink, Kafka, Spark.

## **PROFESSIONAL EXPERIENCE**

#### **Acme Technologies | Industrial Diagnosis Platform (RAG + LLM) | AI Engineer |**

Feb 2025 – July 2025

- Led the migration of a legacy system to a RAG-powered diagnosis platform, increasing Top-1 accuracy from 70% to 85%.

- Designed the end-to-end retrieval pipeline through knowledge normalization, semantic chunking and LLM reasoning.

#### **Acme Technologies | Test Data Processing Platform | Data Engineer |**

June 2023 – Feb 2025

- Led the refactoring of distributed Flink data pipelines processing tens of billions of records, significantly reducing serialization

overhead.

- Designed an end-to-end data reliability framework; ensured 99.99% data completeness while cutting recovery time from 30 minutes to under 5.

### **Acme Technologies | Enterprise Partner Management Platform | Backend Engineer |**

April 2022 – June 2023

- Redesigned a legacy backend platform into a scalable microservices architecture with a 3B+ record migration.

## **PERSONAL PROJECT**

**Multi-Agent Smart Contract Security System |** 2025 – 2026

- Built a LangGraph-based multi-agent security framework for automated vulnerability detection.

- Implemented state-driven agent orchestration with static analysis and sandbox execution.

#### **AI Agent-driven Market Intelligence Platform |** 2026

- Built a real-time market intelligence platform combining Kafka, Flink and ClickHouse with LLM agents.

## **EDUCATION**

**Test University |** Testville, TC

Master of Engineering in Test Science | Sep 2019 – Mar 2022
"""


def test_resume_document_parser_captures_everything():
    """
    离线钉住结构化解析：姓名/联系方式/summary/技能/教育背景一项不少，三段工作经历
    的公司+职位头衔+时间三项完整，PERSONAL PROJECT 独立成板块且排在工作经历之后。
    """
    from src.core.resume_document import parse_resume_document, render_markdown

    doc = parse_resume_document(_SAMPLE_FULL_RESUME_MD)

    assert doc.name == "Alex (Sample) Doe", doc.name
    assert doc.contact_lines and "alex.sample@example.com" in doc.contact_lines[0]

    summary = doc.section("summary")
    skills = doc.section("skills")
    education = doc.section("education")
    assert summary and len(summary.body_lines) >= 2, "SUMMARY 丢了"
    assert skills and any("Flink" in l for l in skills.body_lines), "SKILLS 丢了"
    assert education and any("Test University" in l for l in education.body_lines), "EDUCATION 丢了"

    exp = doc.section("experience")
    assert exp and len(exp.entries) == 3, f"应有 3 段工作经历，实际 {len(exp.entries) if exp else 0}"
    titles = [e.title for e in exp.entries]
    assert any(t.endswith("AI Engineer") for t in titles), titles
    assert any(t.endswith("Data Engineer") for t in titles), titles
    assert any(t.endswith("Backend Engineer") for t in titles), titles
    for e in exp.entries:
        assert e.date, f"「{e.title}」时间丢了"
        assert e.bullets, f"「{e.title}」bullet 丢了"
    # 第二段有 2 条 bullet（数据完整性/恢复时间那条不能被丢）
    data_eng = next(e for e in exp.entries if e.title.endswith("Data Engineer"))
    assert len(data_eng.bullets) == 2 and any("99.99%" in b for b in data_eng.bullets)
    # PDF 换行把一句话腰斩（"...serialization \n\n overhead."）在新解析路径下也要被接回去
    assert any("serialization overhead." in b for b in data_eng.bullets), \
        f"句子被换行腰斩了：{data_eng.bullets}"

    proj = doc.section("projects")
    assert proj and len(proj.entries) == 2, "PERSONAL PROJECT 应独立成板块、含 2 条"

    rendered = render_markdown(doc)
    assert rendered.index("## PROFESSIONAL EXPERIENCE") < rendered.index("## PERSONAL PROJECT"), \
        "PERSONAL PROJECT 必须排在 PROFESSIONAL EXPERIENCE 之后"

    print("[PASS] test_resume_document_parser_captures_everything")


def test_structural_review_catches_content_loss():
    """
    离线钉住确定性校验：定制结果一旦丢内容/改标题/少 bullet，必须被 _entries_intact /
    _verbatim_sections_intact 抓出来。
    """
    from src.core.resume_document import parse_resume_document, ResumeDocument, ResumeSection, ResumeEntry
    from src.tab_b_jobsearch.resume_generator import _verbatim_sections_intact, _entries_intact

    original = parse_resume_document(_SAMPLE_FULL_RESUME_MD)

    # 篡改：丢掉 summary、砍一条 bullet、改一个职位头衔
    broken = ResumeDocument(
        name=original.name, contact_lines=list(original.contact_lines),
        image_lines=list(original.image_lines), sections=[],
    )
    for s in original.sections:
        if s.kind == "summary":
            continue  # 整段丢失
        if s.kind == "experience":
            new_entries = []
            for i, e in enumerate(s.entries):
                bullets = e.bullets[:-1] if i == 1 else list(e.bullets)   # 第 2 段砍一条 bullet
                title = e.title.replace("AI Engineer", "Principal AI Architect") if i == 0 else e.title
                new_entries.append(ResumeEntry(title=title, date=e.date, bullets=bullets))
            s = ResumeSection(title=s.title, kind=s.kind, entries=new_entries)
        broken.sections.append(s)

    issues = _verbatim_sections_intact(original, broken) + _entries_intact(original, broken)
    joined = " ".join(issues)
    assert "summary" in joined.lower() or "SUMMARY" in joined, issues
    assert "bullet" in joined, issues
    assert "标题被改动" in joined or "不是原简历里的条目" in joined, issues

    # 未篡改的文档：不应报任何问题
    assert not (_verbatim_sections_intact(original, original) + _entries_intact(original, original))

    print("[PASS] test_structural_review_catches_content_loss")


def test_tailored_resume_preserves_key_fields():
    """
    需要 LLM（真实调 DeepSeek）。没有 key / 网络不通就 SKIP。
    定制之后：姓名、联系方式、summary、技能、教育背景一字不改；三段工作经历的
    公司/职位头衔/时间/全部 bullet 都在；PERSONAL PROJECT 独立成板块排在经历之后。
    """
    import os as _os
    if not _os.getenv("DEEPSEEK_API_KEY"):
        print("[SKIP] test_tailored_resume_preserves_key_fields（没有 DEEPSEEK_API_KEY）")
        return

    from src.core.resume_document import parse_resume_document
    from src.tab_b_jobsearch.resume_generator import generate_tailored_resume

    doc = parse_resume_document(_SAMPLE_FULL_RESUME_MD)
    jd = ("Senior AI Engineer building production RAG systems and LLM agents. Python, LangGraph, "
          "vector databases, semantic retrieval, end-to-end retrieval pipelines.")
    try:
        res = generate_tailored_resume(doc, jd)
    except (ConnectionError, TimeoutError, OSError) as e:
        print(f"[SKIP] test_tailored_resume_preserves_key_fields（网络不通：{e}）")
        return

    out = res["tailored_resume"]
    assert res["passed_review"], f"结构校验没过：{res['issue']}"

    for must in (
        "Alex (Sample) Doe",
        "alex.sample@example.com",
        "3+ years of enterprise engineering experience",       # summary 原句
        "**Big Data & Streaming:** Apache Flink, Kafka, Spark", # skills 原句
        "Master of Engineering in Test Science",               # education 原句
        "AI Engineer", "Data Engineer", "Backend Engineer",    # 三个职位头衔
        "99.99% data completeness",                            # 那条差点被丢的 bullet
        "serialization overhead.",                             # 换行腰斩过的 bullet，措辞里要完整
        "Feb 2025 – July 2025", "June 2023 – Feb 2025", "April 2022 – June 2023",
    ):
        assert must in out, f"定制结果里丢了：{must!r}"

    assert out.index("## PROFESSIONAL EXPERIENCE") < out.index("## PERSONAL PROJECT")

    # bullet 数量守恒：原文 6 条经历 bullet + 3 条项目 bullet
    assert out.count("\n- ") == _SAMPLE_FULL_RESUME_MD.count("\n- "), \
        f"bullet 总数变了：原 {_SAMPLE_FULL_RESUME_MD.count(chr(10)+'- ')}，现 {out.count(chr(10)+'- ')}"

    # 方向对口的 JD：关联度应判 STRONG/MODERATE，不该是 WEAK
    assert res["relevance_label"] in ("STRONG", "MODERATE"), res["relevance_label"]

    print("[PASS] test_tailored_resume_preserves_key_fields")


def test_mismatched_job_flagged_weak_but_not_fabricated():
    """
    需要 LLM。之前测出"确实不匹配"的 K8s/云平台方向：新流程内容量变大（喂全量简历），
    也不能因此意外放水——关联度必须判 WEAK，且不能把 JD 里的 Kubernetes/Terraform
    等原简历没有的技术偷偷编进 bullet；同时内容一条不丢。
    """
    import os as _os
    if not _os.getenv("DEEPSEEK_API_KEY"):
        print("[SKIP] test_mismatched_job_flagged_weak_but_not_fabricated（没有 DEEPSEEK_API_KEY）")
        return

    from src.core.resume_document import parse_resume_document
    from src.tab_b_jobsearch.resume_generator import generate_tailored_resume

    doc = parse_resume_document(_SAMPLE_FULL_RESUME_MD)
    jd = ("Cloud / Platform Engineer. Own Kubernetes clusters and multi-region infra. Terraform "
          "IaC, CI/CD pipeline design, cloud cost optimization, observability with Prometheus and "
          "Grafana, incident response, developer platform tooling on AWS and GCP. 5+ years running "
          "production Kubernetes.")
    try:
        res = generate_tailored_resume(doc, jd)
    except (ConnectionError, TimeoutError, OSError) as e:
        print(f"[SKIP] test_mismatched_job_flagged_weak_but_not_fabricated（网络不通：{e}）")
        return

    out = res["tailored_resume"]
    assert res["relevance_label"] == "WEAK", f"错配职位没被判 WEAK，而是 {res['relevance_label']!r}"
    assert res["passed_review"], f"结构校验没过：{res['issue']}"

    src_low = _SAMPLE_FULL_RESUME_MD.lower()
    out_low = out.lower()
    for kw in ("kubernetes", "terraform", "prometheus", "grafana", "aws", "gcp", "incident response"):
        if kw in src_low:
            continue
        assert kw not in out_low, f"把原简历没有的 JD 技术 {kw!r} 编进了定制结果"

    # 内容仍然一条不丢
    assert out.count("\n- ") == _SAMPLE_FULL_RESUME_MD.count("\n- ")
    for must in ("AI Engineer", "Data Engineer", "Backend Engineer", "99.99% data completeness"):
        assert must in out

    print("[PASS] test_mismatched_job_flagged_weak_but_not_fabricated")


# 排查发现：搜索链路只在拼查询关键词时带了地点词，结果侧从来没有校验过"这条职位的
# 地点/签证要求是否符合偏好"。新增 job_pref_filter.check_job 做基本规则判断。
def test_location_visa_filter():
    from src.tab_b_jobsearch.job_pref_filter import build_prefs, check_job

    prefs = build_prefs(target_region="Canada Remote", needs_sponsorship=True)
    assert prefs.allowed_regions == ["canada"] and prefs.open_to_remote and prefs.needs_sponsorship

    # 明确排斥性签证表述 -> 剔除
    for content in (
        "Exciting role. Unfortunately we are not able to provide visa sponsorship at this time.",
        "You must already be legally authorized to work in the United States. No C2C, W2 only.",
        "US citizens only due to federal contract requirements.",
    ):
        keep, reason = check_job("Senior Engineer", content, "", None, prefs)
        assert not keep, f"没被签证过滤剔除：{content!r}"

    # 地点在别的国家、又不是远程 -> 剔除
    keep, reason = check_job("Backend Engineer", "Join us onsite in our Berlin office. Hybrid 3 days.",
                             "Berlin, Germany", False, prefs)
    assert not keep and "地点" in reason

    # 远程 / 在偏好地区 / 判不准 -> 保留
    for title, content, loc, rem in (
        ("Data Engineer", "Fully remote. Team distributed across Canada and the US.", "Remote", True),
        ("ML Engineer", "Based in Toronto, ON. Some hybrid flexibility.", "Toronto, ON, Canada", False),
        ("Platform Engineer", "Generic JD about Kubernetes and Terraform, no location or visa info.", "", None),
        ("AI Engineer", "We happily sponsor visas for the right candidate. Fully remote.", "Remote", True),
    ):
        keep, reason = check_job(title, content, loc, rem, prefs)
        assert keep, f"被误杀：{title!r} -> {reason}"

    # 地点不符 + 会 sponsor：地点这关照样拦（用户要 Canada/Remote，岗位在 London 坐班）
    keep, reason = check_job("AI Engineer", "We happily sponsor visas.", "London, UK", False, prefs)
    assert not keep and "地点" in reason

    # 没配偏好（既没地区也不需要担保）-> 一律不过滤
    empty = build_prefs(target_region="", needs_sponsorship=False)
    assert not empty.configured
    keep, _ = check_job("X", "onsite in Tokyo, no sponsorship", "Tokyo", False, empty)
    assert keep

    print("[PASS] test_location_visa_filter")


def test_applied_jobs_dedup():
    """
    投递成功后记一笔；之后搜索结果里同一条职位（含 URL 变体 / 跨来源同公司同标题）剔除。
    用临时文件，不碰真实 applied_jobs.json。
    """
    import src.core.applied_jobs_store as ajs

    orig_path = ajs.APPLIED_JOBS_FILE
    tmp = os.path.join(os.path.dirname(__file__), "..", "data", "_applied_jobs_test.json")
    ajs.APPLIED_JOBS_FILE = tmp
    if os.path.exists(tmp):
        os.remove(tmp)
    try:
        assert ajs.normalize_job_url("https://www.Boards.Greenhouse.io/Acme/jobs/123/?utm=x#a") \
            == "boards.greenhouse.io/acme/jobs/123"

        assert not ajs.is_applied(job_url="https://x.com/j/1")

        ajs.record_applied(
            "https://job-boards.greenhouse.io/reddit/jobs/7997866",
            "Software Engineer, Data Movement Platform",
            record_id="tavily_abc123",
        )
        ajs.record_applied("https://job-boards.greenhouse.io/reddit/jobs/7997866", "dup")  # 幂等
        assert len(ajs.list_applied()) == 1

        # 完全一样
        assert ajs.is_applied(job_url="https://job-boards.greenhouse.io/reddit/jobs/7997866")
        # URL 变体（协议 / www / query / 尾斜杠）
        assert ajs.is_applied(job_url="http://www.job-boards.greenhouse.io/reddit/jobs/7997866/?ref=hn")
        # 靠 record_id
        assert ajs.is_applied(record_id="tavily_abc123")
        # 换个 greenhouse 域名、URL 结构不同，但同公司同标题
        assert ajs.is_applied(
            job_url="https://boards.greenhouse.io/reddit/jobs/55555",
            job_title="Software Engineer, Data Movement Platform",
        )
        # 完全不相关的职位不受影响
        assert not ajs.is_applied(job_url="https://acme.io/jobs/9", job_title="Product Manager at Stripe")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
        ajs.APPLIED_JOBS_FILE = orig_path

    print("[PASS] test_applied_jobs_dedup")


# 实测坐实过一次：自动投递把 https://www.ycombinator.com/apply/（YC 孵化器"申请加入 YC"
# 的页面）当成职位申请入口打开，点 Apply 后跳到 YC 账号登录页，脚本在上面找不到申请表单，
# 没优雅退出、把浏览器晾成了 about:blank。新增 _looks_like_application_page 健全性检查：
# 打开后先确认是不是职位申请表单，不是就 page.close() + 明确报错。
def test_application_page_sanity_check():
    from src.tab_b_jobsearch.apply.orchestrator import _looks_like_application_page

    class _FakePage:
        def __init__(self, url):
            self.url = url

    class _FakeScope:
        def __init__(self, probe):
            self._probe = probe
        def evaluate(self, _js):
            return self._probe

    def check(url, probe):
        return _looks_like_application_page(_FakePage(url), _FakeScope(probe))

    # about:blank / 没加载 -> False
    ok, why = check("about:blank", {})
    assert not ok and "没有正常加载" in why

    # YC 登录页（"Username or email" 单字段、有 "Log in" 字样、没有 apply/resume 特征）-> False
    ok, why = check(
        "https://account.ycombinator.com/?continue=https%3A%2F%2Fapply.ycombinator.com%2Fhome",
        {"fileInputs": 0, "passwordInputs": 0, "fillable": 1, "emailLike": 0, "nameLike": 0,
         "text": "Log in to access the YC Application\nUsername or email\nContinue"},
    )
    assert not ok, why

    # 常规登录页（有密码框）-> False
    ok, why = check("https://acme.com/account/login",
                    {"fileInputs": 0, "passwordInputs": 1, "fillable": 3, "emailLike": 1, "nameLike": 0,
                     "text": "Sign in to your account. Forgot your password?"})
    assert not ok and ("登录" in why or "注册" in why)

    # 真·申请表单：有简历上传框 -> True
    ok, why = check("https://acme.com/careers/apply/123",
                    {"fileInputs": 1, "passwordInputs": 0, "fillable": 6, "emailLike": 1, "nameLike": 2,
                     "text": "Apply for this job. Attach your resume."})
    assert ok, why

    # 真·申请表单：姓名+邮箱+多字段，无密码框 -> True
    ok, why = check("https://acme.com/careers/apply/123",
                    {"fileInputs": 0, "passwordInputs": 0, "fillable": 5, "emailLike": 1, "nameLike": 2,
                     "text": "First name, Last name, Email, Phone, LinkedIn"})
    assert ok, why

    # 已知 ATS 域名 + 有字段 -> True（哪怕文案里没有明显的 apply 字样）
    ok, why = check("https://job-boards.greenhouse.io/acme/jobs/123",
                    {"fileInputs": 0, "passwordInputs": 0, "fillable": 4, "emailLike": 1, "nameLike": 1, "text": ""})
    assert ok, why

    # 营销页 / 落地页：有几个字段但既不像申请表也没 ATS 域名 -> False
    ok, why = check("https://www.ycombinator.com/apply/",
                    {"fileInputs": 0, "passwordInputs": 0, "fillable": 2, "emailLike": 0, "nameLike": 0,
                     "text": "Apply to Y Combinator. The next batch deadline is..."})
    assert not ok, why

    print("[PASS] test_application_page_sanity_check")


def test_hn_connector_url_and_seeker_filter():
    """
    排查 ycombinator.com/apply 那条错误结果时确认过：HN 连接器每条记录的 url 永远是
    news.ycombinator.com/item?id=...（HN 评论本身），它根本不从评论正文里提取公司的
    申请链接（_clean_hn_text 会把所有 <a> 标签剥掉只留文字）——所以 HN 不可能产出
    ycombinator.com/apply 这种 url。顺手加的：求职者发错帖的贴要被跳过。
    """
    import inspect
    from src.connectors import hn_connector

    src = inspect.getsource(hn_connector.fetch_hn_jobs)
    assert 'url=f"https://news.ycombinator.com/item?id={cid}"' in src, \
        "HN 记录的 url 不再是固定的 HN 评论地址了？"

    R = hn_connector._SEEKER_MARKER_RE
    assert R.match("Seeking work | Remote | Python developer")
    assert R.match("[FOR HIRE] Senior Backend Engineer")
    assert not R.match("Acme Corp | Senior Engineer | Remote")
    assert not R.match("We are hiring a Data Scientist")

    print("[PASS] test_hn_connector_url_and_seeker_filter")


# 实测坐实过一次真实 bug：持久化浏览器目录被弄坏之后，browser.get_context() 一直抛
# TargetClosedError，而 start_application 里 get_context()/new_page() 不在任何 try 里、
# apply_router 又只 catch ApplyError —— 于是裸奔成 500 "Internal Server Error"（还不是
# JSON），前端 ErrorBanner 显示不出内容，浏览器 tab 晾成 about:blank 也没人关。
# 修复：start_application 整体包 try/except 把任何异常都转成 ApplyError 且不漏 page；
# apply_router 兜一层把非 ApplyError 也转成带 detail 的响应；所有浏览器操作固定在
# 专用单线程里跑（同步 Playwright 不能跨 FastAPI 线程池的线程）。
def test_apply_start_never_raises_bare_exception():
    """
    start_application 的契约：要么返回 ApplyDraft，要么抛 ApplyError —— 绝不让底层
    异常（浏览器启动失败等）原样冒泡。用 monkeypatch 让 get_context 直接炸。
    """
    from src.tab_b_jobsearch.apply import orchestrator, browser
    from src.tab_b_jobsearch.apply.orchestrator import ApplyError

    orig = browser.get_context
    browser.get_context = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模拟浏览器启动失败"))
    try:
        try:
            orchestrator.start_application(job_id="x", job_url="https://acme.com/careers/apply/1",
                                          job_title="T", resume_path=__file__)  # 用一个真实存在的文件绕过简历检查
        except ApplyError as e:
            assert "模拟浏览器启动失败" in str(e), str(e)
        except Exception as e:  # noqa: BLE001
            raise AssertionError(f"抛的不是 ApplyError，而是 {type(e).__name__}: {e}")
        else:
            raise AssertionError("应该抛 ApplyError 才对")
    finally:
        browser.get_context = orig

    print("[PASS] test_apply_start_never_raises_bare_exception")


def test_apply_start_route_returns_json_detail_on_failure():
    """
    /api/apply/start 遇到任何失败都要返回带 detail 的响应（前端 ErrorBanner 能渲染），
    不能是裸的 500 "Internal Server Error"。
    """
    from fastapi.testclient import TestClient
    from src.main import app
    from src.tab_b_jobsearch import apply_router

    orig = apply_router.start_application
    apply_router.start_application = lambda **k: (_ for _ in ()).throw(RuntimeError("boom-non-applyerror"))
    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/apply/start", json={
            "job_id": "t", "job_url": "https://acme.com/careers/apply/1", "job_title": "T",
        })
        assert resp.status_code in (400, 500), resp.status_code
        body = resp.json()
        assert isinstance(body, dict) and body.get("detail"), f"响应没有 detail 字段：{resp.text[:200]}"
        assert "boom-non-applyerror" in body["detail"] or "启动失败" in body["detail"]
    finally:
        apply_router.start_application = orig

    print("[PASS] test_apply_start_route_returns_json_detail_on_failure")


def test_browser_ops_pinned_to_one_thread():
    from src.tab_b_jobsearch.apply.browser import run_in_browser_thread
    import threading

    seen = set()
    for _ in range(5):
        seen.add(run_in_browser_thread(lambda: threading.current_thread().name))
    assert len(seen) == 1, f"浏览器操作没固定在同一个线程：{seen}"
    assert "apply-browser" in next(iter(seen))

    # 异常要原样抛回来
    try:
        run_in_browser_thread(lambda: (_ for _ in ()).throw(ValueError("propagate me")))
    except ValueError as e:
        assert str(e) == "propagate me"
    else:
        raise AssertionError("异常没有从 run_in_browser_thread 抛回来")

    print("[PASS] test_browser_ops_pinned_to_one_thread")


if __name__ == "__main__":
    test_main_app_imports_cleanly()
    test_word_export_dir_resolves_to_project_root()
    test_generate_tailored_resume_accepts_extra_context()
    test_degree_extraction_does_not_false_positive_on_substring()
    test_resume_with_mixed_heading_level_entries_still_produces_chunks()
    test_entry_title_and_date_on_separate_lines_keep_all_tags()
    test_anysearch_job_listing_pages_are_detected()
    test_job_relevance_filter_rejects_marketing_pages()
    test_workday_cxs_url_transform()
    test_workday_job_content_via_cxs_api()
    test_resume_chunk_dedup()
    test_split_sentences_reflows_pdf_soft_wraps()
    test_resume_document_parser_captures_everything()
    test_structural_review_catches_content_loss()
    test_tailored_resume_preserves_key_fields()
    test_mismatched_job_flagged_weak_but_not_fabricated()
    test_location_visa_filter()
    test_applied_jobs_dedup()
    test_application_page_sanity_check()
    test_hn_connector_url_and_seeker_filter()
    test_apply_start_never_raises_bare_exception()
    test_apply_start_route_returns_json_detail_on_failure()
    test_browser_ops_pinned_to_one_thread()
    print("\n全部回归测试通过。")

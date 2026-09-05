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
    ):
        assert not looks_like_job_listing_page(url, title), f"单条职位详情页被误判成列表页：{url}"

    print("[PASS] test_anysearch_job_listing_pages_are_detected")


if __name__ == "__main__":
    test_main_app_imports_cleanly()
    test_word_export_dir_resolves_to_project_root()
    test_generate_tailored_resume_accepts_extra_context()
    test_degree_extraction_does_not_false_positive_on_substring()
    test_resume_with_mixed_heading_level_entries_still_produces_chunks()
    test_entry_title_and_date_on_separate_lines_keep_all_tags()
    test_anysearch_job_listing_pages_are_detected()
    print("\n全部回归测试通过。")

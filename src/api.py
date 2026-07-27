"""
FastAPI 后端接口。
把简历定制生成这套 RAG 流程，包装成前端能直接调用的 HTTP API。
所有接口严格按照 API_SPEC.md 的字段定义实现，不在这里临时改字段名。

启动方式（在项目根目录下）：
    uvicorn src.api:app --reload

【本次改动说明】
search_agent.suggest_relevant_categories() 的返回值从 list[str] 改成了
tuple[list[str], list[str]]（新增返回一批推荐的 Instagram hashtag），
本文件相应做了两处修改：
1. /api/suggest-lead-categories：接收新的元组返回值，并把 hashtags 加入响应体，
   供前端展示/编辑，再传给 /api/search-opportunities。
2. /api/search-opportunities：请求体新增可选的 hashtags 字段，传给
   run_categorized_opportunity_search()，触发 affiliate_kol 类别下的
   Instagram 结构化数据抓取。不传则跳过 Instagram 抓取，行为和之前一致。
"""

import os
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.resume_parser import parse_resume
from src.chunker import chunk_resume
from src.vector_store import build_resume_collection
from src.retriever import hybrid_search
from src.resume_generator import generate_tailored_resume
from src.connectors.hn_connector import fetch_hn_jobs
from src.search_agent import run_search_agent, suggest_relevant_categories, run_categorized_opportunity_search, LEAD_CATEGORIES
from src.business_profile import build_business_profile, BUSINESS_COLLECTION_NAME
from src.outreach_generator import generate_outreach_message
from src.word_export import export_resume_to_docx
from src.status_store import update_status as _update_status

app = FastAPI(title="OptiMatch AI - API")

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


# ---------- 1.1 上传简历 ----------

class UploadResumeResponse(BaseModel):
    success: bool
    parsed_sections: list[str]
    total_chunks: int
    preview_text: str


@app.post("/api/upload-resume", response_model=UploadResumeResponse)
async def upload_resume(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    save_path = os.path.join(UPLOAD_DIR, "uploaded_resume.pdf")
    with open(save_path, "wb") as f:
        f.write(await file.read())

    try:
        text = parse_resume(save_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse PDF: {e}")

    chunks = chunk_resume(text)
    if not chunks:
        raise HTTPException(status_code=400, detail="No extractable text found in this PDF")

    build_resume_collection(chunks)

    return UploadResumeResponse(
        success=True,
        parsed_sections=[c["section"] for c in chunks],
        total_chunks=len(chunks),
        preview_text=text[:500],
    )


# ---------- 1.2 生成定制简历 ----------

class TailorResumeRequest(BaseModel):
    job_description: str = Field(..., description="Full text of the job description")
    user_notes: str = Field(
        default="", description="Optional context the user adds to clarify vague resume content"
    )
    top_k: int = Field(default=3, description="How many relevant resume chunks to retrieve")


class TailorResumeResponse(BaseModel):
    tailored_resume: str
    passed_review: bool
    issue: str
    attempts: int
    matched_sections: list[str]


@app.post("/api/tailor-resume", response_model=TailorResumeResponse)
def tailor_resume(request: TailorResumeRequest):
    if not request.job_description.strip():
        raise HTTPException(status_code=400, detail="job_description 不能为空")

    try:
        matches = hybrid_search(request.job_description, final_top_k=request.top_k)
    except RuntimeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Retrieval failed: {e}. Make sure a resume has been uploaded first via /api/upload-resume."
        )

    result = generate_tailored_resume(
        original_chunks=matches,
        job_description=request.job_description,
        user_notes=request.user_notes,
    )

    return TailorResumeResponse(
        tailored_resume=result["tailored_resume"],
        passed_review=result["passed_review"],
        issue=result["issue"],
        attempts=result["attempts"],
        matched_sections=[m["section"] for m in matches],
    )


# ---------- 1.3 自动搜索职位 ----------

class JobRecord(BaseModel):
    id: str
    source: str
    title: str
    url: str
    posted_at: str | None
    status: str = "new"


class SearchJobsRequest(BaseModel):
    target_role: str = Field(..., description="Target job direction, e.g. 'AI Engineer'")
    target_region: str = Field(default="Canada Remote", description="Target region")
    max_results: int = Field(default=15)


class SearchJobsResponse(BaseModel):
    jobs: list[JobRecord]
    total: int


@app.post("/api/search-jobs", response_model=SearchJobsResponse)
def search_jobs(request: SearchJobsRequest):
    jobs = []

    # 数据源一：HN Who is hiring（结构化数据源，稳定，直接抓）
    try:
        hn_records = fetch_hn_jobs(limit=request.max_results)
        for r in hn_records:
            record_id = f"hn_{r.url.split('id=')[-1]}"
            jobs.append(JobRecord(
                id=record_id, source=r.source, title=r.title,
                url=r.url, posted_at=r.posted_at, status="new",
            ))
    except Exception as e:
        print(f"  [调试] HN 抓取失败: {e}")

    # 数据源二：搜索 Agent（LLM 主动拆解多角度查询，不是固定一个关键词）
    try:
        user_need = f"Find job openings for {request.target_role} in {request.target_region}"
        agent_records = run_search_agent(
            user_need=user_need,
            platform_type="job",
            max_results_per_query=5,
            max_rounds=2,
        )
        for r in agent_records:
            record_id = f"tavily_{abs(hash(r.url))}"
            jobs.append(JobRecord(
                id=record_id, source=r.source, title=r.title,
                url=r.url, posted_at=r.posted_at, status="new",
            ))
    except Exception as e:
        print(f"  [调试] 搜索 Agent 失败: {e}")

    jobs = jobs[:request.max_results]
    return SearchJobsResponse(jobs=jobs, total=len(jobs))


# ---------- 1.4 导出 Word 简历 ----------

class ExportResumeRequest(BaseModel):
    final_content: str = Field(..., description="User-reviewed final resume text")
    candidate_name: str = Field(..., description="Used for document title and filename")


class ExportResumeResponse(BaseModel):
    success: bool
    download_url: str


@app.post("/api/export-resume-docx", response_model=ExportResumeResponse)
def export_resume_docx(request: ExportResumeRequest):
    if not request.final_content.strip():
        raise HTTPException(status_code=400, detail="final_content 不能为空")

    filepath = export_resume_to_docx(request.final_content, request.candidate_name)
    filename = os.path.basename(filepath)

    return ExportResumeResponse(success=True, download_url=f"/files/{filename}")


@app.get("/files/{filename}")
def download_file(filename: str):
    filepath = os.path.join(os.path.dirname(__file__), "..", "data", "exports", filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        filepath,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
    )


# ---------- 二、商机相关接口（Tab A） ----------

class SetupBusinessProfileRequest(BaseModel):
    business_description: str = Field(..., description="Business/product description, <=300 chars")
    target_customer: str = Field(..., description="Target customer profile, <=150 chars")
    website_url: str = Field(default="", description="Optional website URL to scrape for extra context")


class SetupBusinessProfileResponse(BaseModel):
    success: bool
    total_chunks: int


@app.post("/api/setup-business-profile", response_model=SetupBusinessProfileResponse)
def setup_business_profile(request: SetupBusinessProfileRequest):
    if not request.business_description.strip() or not request.target_customer.strip():
        raise HTTPException(status_code=400, detail="business_description and target_customer are required")

    chunks = build_business_profile(
        business_description=request.business_description,
        target_customer=request.target_customer,
        website_url=request.website_url,
    )
    return SetupBusinessProfileResponse(success=True, total_chunks=len(chunks))


class SuggestCategoriesRequest(BaseModel):
    business_description: str = Field(..., description="Business description used to infer relevant lead categories")
    max_categories: int = Field(default=3)


class CategoryOption(BaseModel):
    id: str
    label: str
    suggested: bool  # True = AI 推荐勾选，前端默认打钩，用户可以自己增减


class SuggestCategoriesResponse(BaseModel):
    categories: list[CategoryOption]
    suggested_hashtags: list[str] = Field(
        default_factory=list,
        description="LLM 生成的 Instagram hashtag 建议，供 affiliate_kol 类别使用。"
                     "前端可展示为可编辑列表，用户确认/调整后传给 /api/search-opportunities。"
    )


@app.post("/api/suggest-lead-categories", response_model=SuggestCategoriesResponse)
def suggest_lead_categories(request: SuggestCategoriesRequest):
    """
    意图路由：根据业务描述推荐最相关的线索类别，同时推荐一批 Instagram hashtag。
    不直接静默执行搜索——返回全部 6 个类别选项，AI 推荐的打上 suggested=True，
    前端用 checkbox 展示，默认勾选推荐项，用户可以自己调整后再触发 /api/search-opportunities。

    hashtag 同理：默认使用 LLM 生成结果，前端可让用户编辑后再传回。
    """
    if not request.business_description.strip():
        raise HTTPException(status_code=400, detail="business_description 不能为空")

    suggested_ids, suggested_hashtags = suggest_relevant_categories(
        request.business_description, request.max_categories
    )
    suggested_ids = set(suggested_ids)

    categories = [
        CategoryOption(id=cat_id, label=info["label"], suggested=(cat_id in suggested_ids))
        for cat_id, info in LEAD_CATEGORIES.items()
    ]
    return SuggestCategoriesResponse(categories=categories, suggested_hashtags=suggested_hashtags)


class OpportunityRecord(BaseModel):
    id: str
    source: str
    title: str
    url: str
    posted_at: str | None
    status: str = "new"
    category: str  # 对应 LEAD_CATEGORIES 的 key，前端按这个字段分组/筛选展示
    email: str | None = None       # 新增：仅 Instagram/Twitter 来源才可能有值
    followers: int | None = None   # 新增：仅 Instagram/Twitter 来源才可能有值


class SearchOpportunitiesRequest(BaseModel):
    categories: list[str] = Field(
        ..., description="Which lead categories to search (from user-confirmed checkbox selection)"
    )
    hashtags: list[str] = Field(
        default_factory=list,
        description="Instagram hashtags to use for the affiliate_kol category "
                     "(from /api/suggest-lead-categories, optionally user-edited). "
                     "Leave empty to skip Instagram scraping and use Tavily only."
    )
    max_results_per_category: int = Field(default=5)


class SearchOpportunitiesResponse(BaseModel):
    opportunities: list[OpportunityRecord]
    total: int


@app.post("/api/search-opportunities", response_model=SearchOpportunitiesResponse)
def search_opportunities(request: SearchOpportunitiesRequest):
    try:
        matches = hybrid_search(
            "business description", collection_name=BUSINESS_COLLECTION_NAME, final_top_k=5
        )
        context = "\n".join(m["content"] for m in matches)
    except RuntimeError:
        raise HTTPException(
            status_code=400,
            detail="No business profile found. Call /api/setup-business-profile first."
        )

    if not request.categories:
        raise HTTPException(status_code=400, detail="categories 不能为空，至少选择一个线索类别")

    try:
        categorized = run_categorized_opportunity_search(
            business_context=context,
            categories=request.categories,
            hashtags=request.hashtags,
            max_results_per_category=request.max_results_per_category,
            concurrent=True,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Categorized search failed: {e}")

    opportunities = []
    for cat_id, records in categorized.items():
        for r in records:
            opportunities.append(OpportunityRecord(
                id=f"{r.source}_{abs(hash(r.url))}",
                source=r.source, title=r.title, url=r.url,
                posted_at=r.posted_at, status="new", category=cat_id,
                email=r.email, followers=r.followers,
            ))

    return SearchOpportunitiesResponse(opportunities=opportunities, total=len(opportunities))


class GenerateOutreachRequest(BaseModel):
    opportunity_content: str = Field(..., description="Full text of the target opportunity")
    user_notes: str = Field(default="", description="Optional context to clarify vague business info")


class GenerateOutreachResponse(BaseModel):
    outreach_message: str
    passed_review: bool
    issue: str
    attempts: int
    matched_sections: list[str]


@app.post("/api/generate-outreach", response_model=GenerateOutreachResponse)
def generate_outreach(request: GenerateOutreachRequest):
    if not request.opportunity_content.strip():
        raise HTTPException(status_code=400, detail="opportunity_content 不能为空")

    try:
        matches = hybrid_search(
            request.opportunity_content, collection_name=BUSINESS_COLLECTION_NAME, final_top_k=3
        )
    except RuntimeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Retrieval failed: {e}. Call /api/setup-business-profile first."
        )

    result = generate_outreach_message(
        business_chunks=matches,
        opportunity_content=request.opportunity_content,
        user_notes=request.user_notes,
    )

    return GenerateOutreachResponse(
        outreach_message=result["outreach_message"],
        passed_review=result["passed_review"],
        issue=result["issue"],
        attempts=result["attempts"],
        matched_sections=[m["section"] for m in matches],
    )


# ---------- 三、通用状态管理 ----------

class UpdateStatusRequest(BaseModel):
    record_id: str
    status: str


@app.post("/api/update-status")
def update_status(request: UpdateStatusRequest):
    try:
        _update_status(request.record_id, request.status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True}


# ---------- 四、系统接口 ----------

@app.get("/api/health")
def health_check():
    return {"status": "ok"}
"""
求职与简历模块 (Tab B) 接口路由。
"""
import os
from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.resume_parser import parse_resume
from src.chunker import chunk_resume
from src.vector_store import build_resume_collection
from src.retriever import hybrid_search
from src.resume_generator import generate_tailored_resume
from src.connectors.hn_connector import fetch_hn_jobs
from src.connectors.remoteok_connector import search as remoteok_search
from src.connectors.remotive_connector import search as remotive_search
from src.core.search_agent import run_search_agent
from src.word_export import export_resume_to_docx

router = APIRouter(tags=["Tab B - Job Search"])
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")


# ---------- 1. 上传简历 ----------
class UploadResumeResponse(BaseModel):
    success: bool
    parsed_sections: list[str]
    total_chunks: int
    preview_text: str

@router.post("/api/upload-resume", response_model=UploadResumeResponse)
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


# ---------- 2. 生成定制简历 ----------
class TailorResumeRequest(BaseModel):
    job_description: str = Field(..., description="Full text of the job description")
    user_notes: str = Field(default="", description="Optional context")
    top_k: int = Field(default=3, description="How many relevant resume chunks to retrieve")

class TailorResumeResponse(BaseModel):
    tailored_resume: str
    passed_review: bool
    issue: str
    attempts: int
    matched_sections: list[str]

@router.post("/api/tailor-resume", response_model=TailorResumeResponse)
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


# ---------- 3. 自动搜索职位 ----------
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

def _round_robin_merge(source_lists: list[list[JobRecord]], limit: int) -> list[JobRecord]:
    """
    按"轮流从每路各取一条"的方式合并多路来源，而不是拼接后截断。
    简单拼接+截断的话，排在列表前面的来源（HN/AnySearch）只要凑够
    max_results 就会把排后面的来源（RemoteOK/Remotive）完全挤掉、白抓。
    轮流取的方式能保证只要某路来源有数据，就至少有机会入选一条。

    顺带做跨来源去重：不同来源理论上可能抓到同一条职位的链接，按 url
    去重（保留先出现的那条），空 url 不参与去重判断（避免多条 url
    缺失的记录互相"误判"成重复）。
    """
    result: list[JobRecord] = []
    seen_urls: set[str] = set()
    idx = 0
    while len(result) < limit:
        any_source_has_more = False
        for lst in source_lists:
            if idx >= len(lst):
                continue
            any_source_has_more = True
            item = lst[idx]
            if item.url and item.url in seen_urls:
                continue
            if item.url:
                seen_urls.add(item.url)
            result.append(item)
            if len(result) >= limit:
                break
        if not any_source_has_more:
            break
        idx += 1
    return result


@router.post("/api/search-jobs", response_model=SearchJobsResponse)
def search_jobs(request: SearchJobsRequest):
    hn_jobs: list[JobRecord] = []
    anysearch_jobs: list[JobRecord] = []
    remoteok_jobs: list[JobRecord] = []
    remotive_jobs: list[JobRecord] = []

    try:
        hn_records = fetch_hn_jobs(limit=request.max_results)
        for r in hn_records:
            record_id = f"hn_{r.url.split('id=')[-1]}"
            hn_jobs.append(JobRecord(
                id=record_id, source=r.source, title=r.title,
                url=r.url, posted_at=r.posted_at, status="new",
            ))
    except Exception as e:
        print(f"  [调试] HN 抓取失败: {e}")

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
            anysearch_jobs.append(JobRecord(
                id=record_id, source=r.source, title=r.title,
                url=r.url, posted_at=r.posted_at, status="new",
            ))
    except Exception as e:
        print(f"  [调试] 搜索 Agent 失败: {e}")

    # RemoteOK / Remotive 都不支持按地区过滤，target_region 传不进去——
    # 这两路返回的是全球远程岗位，不保证匹配 target_region。
    try:
        remoteok_records = remoteok_search(keyword=request.target_role, max_results=request.max_results)
        for r in remoteok_records:
            record_id = f"remoteok_{abs(hash(r.url))}"
            remoteok_jobs.append(JobRecord(
                id=record_id, source=r.source, title=r.title,
                url=r.url, posted_at=r.posted_at, status="new",
            ))
    except Exception as e:
        print(f"  [调试] RemoteOK 抓取失败: {e}")

    try:
        remotive_records = remotive_search(keyword=request.target_role, max_results=request.max_results)
        for r in remotive_records:
            record_id = f"remotive_{abs(hash(r.url))}"
            remotive_jobs.append(JobRecord(
                id=record_id, source=r.source, title=r.title,
                url=r.url, posted_at=r.posted_at, status="new",
            ))
    except Exception as e:
        print(f"  [调试] Remotive 抓取失败: {e}")

    jobs = _round_robin_merge(
        [hn_jobs, anysearch_jobs, remoteok_jobs, remotive_jobs],
        limit=request.max_results,
    )
    return SearchJobsResponse(jobs=jobs, total=len(jobs))


# ---------- 4. 导出 Word 简历 ----------
class ExportResumeRequest(BaseModel):
    final_content: str = Field(..., description="User-reviewed final resume text")
    candidate_name: str = Field(..., description="Used for document title and filename")

class ExportResumeResponse(BaseModel):
    success: bool
    download_url: str

@router.post("/api/export-resume-docx", response_model=ExportResumeResponse)
def export_resume_docx(request: ExportResumeRequest):
    if not request.final_content.strip():
        raise HTTPException(status_code=400, detail="final_content 不能为空")

    filepath = export_resume_to_docx(request.final_content, request.candidate_name)
    filename = os.path.basename(filepath)

    return ExportResumeResponse(success=True, download_url=f"/files/{filename}")

@router.get("/files/{filename}")
def download_file(filename: str):
    filepath = os.path.join(UPLOAD_DIR, "exports", filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        filepath,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
    )
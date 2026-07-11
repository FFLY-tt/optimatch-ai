"""
FastAPI 后端接口。
把简历定制生成这套 RAG 流程，包装成前端能直接调用的 HTTP API。
所有接口严格按照 API_SPEC.md 的字段定义实现，不在这里临时改字段名。

启动方式（在项目根目录下）：
    uvicorn src.api:app --reload

启动后，访问 http://127.0.0.1:8000/docs 能看到自动生成的接口文档，
可以直接在网页上测试接口，不需要等前端做好就能验证后端逻辑。
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
from src.connectors.tavily_connector import search as tavily_search
from src.word_export import export_resume_to_docx
from src.status_store import update_status as _update_status

app = FastAPI(title="OptiMatch AI - Resume Tailoring API")

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

    # 数据源一：HN Who is hiring
    try:
        hn_records = fetch_hn_jobs(limit=request.max_results)
        for r in hn_records:
            record_id = f"hn_{r.url.split('id=')[-1]}"
            jobs.append(JobRecord(
                id=record_id,
                source=r.source,
                title=r.title,
                url=r.url,
                posted_at=r.posted_at,
                status="new",
            ))
    except Exception as e:
        print(f"  [调试] HN 抓取失败: {e}")

    # 数据源二：Tavily 搜索（求职关键词）
    try:
        query = f'site:reddit.com r/forhire [HIRING] {request.target_role} {request.target_region}'
        tavily_records = tavily_search(query=query, platform_type="job", max_results=8)
        for i, r in enumerate(tavily_records):
            record_id = f"tavily_{abs(hash(r.url))}"
            jobs.append(JobRecord(
                id=record_id,
                source=r.source,
                title=r.title,
                url=r.url,
                posted_at=r.posted_at,
                status="new",
            ))
    except Exception as e:
        print(f"  [调试] Tavily 搜索失败: {e}")

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
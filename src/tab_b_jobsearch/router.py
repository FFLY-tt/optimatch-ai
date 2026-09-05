"""
求职与简历模块 (Tab B) 接口路由。
"""
import hashlib
import os
import uuid
from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.tab_b_jobsearch.resume_parser import parse_resume_to_markdown
from src.core.resume_ast import parse_resume_markdown, parse_free_text_note
from src.core.text_utils import detect_language
from src.core.profile_language_store import get_profile_language, set_profile_language
from src.core.vector_store import (
    add_resume_chunks, add_resume_keywords, get_all_resume_chunks, load_resume_keywords,
)
from src.core.status_store import get_status
from src.core.resume_by_job_store import set_resume_for_job
from src.core.resume_document_store import save_resume_markdown, load_resume_markdown
from src.core.resume_document import parse_resume_document
from src.core.jd_parser import parse_job_description
from src.core.match_scoring import score_resume_chunks_against_jd, compute_fit_score, fit_score_to_label
from src.tab_b_jobsearch.resume_generator import generate_tailored_resume
from src.connectors.hn_connector import fetch_hn_jobs
from src.connectors.remoteok_connector import search as remoteok_search
from src.connectors.remotive_connector import search as remotive_search
from src.core.search_agent import run_search_agent
from src.tab_b_jobsearch.word_export import (
    export_resume_to_docx, export_resume_to_md, export_resume_to_pdf,
)

router = APIRouter(tags=["Tab B - Job Search"])
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")
RESUMES_DIR = os.path.join(DATA_DIR, "resumes")

# 格式范围收紧：只支持这两种上传格式
_ALLOWED_RESUME_EXTS = (".pdf", ".md")

_LANGUAGE_LABEL = {"zh": "中文", "en": "英文"}


def _check_and_lock_profile_language(text: str) -> None:
    """
    产品要求：单个用户的画像（所有已上传简历 + 所有补充文本的合集）必须是
    单一语言，不能同一个人的画像里混着中文简历和英文简历。

    在真正写入 chunk/keyword 之前调用：
    - 检测结果是 "unknown"（文本太短/没有明显主导语言）：不拦截，跳过检查。
    - 画像还没锁定过语言（第一次输入）：把这次检测出的语言直接锁定，不拦截。
    - 已锁定语言且和这次检测结果一致：不拦截。
    - 已锁定语言但和这次检测结果不一致：抛 400，不写入任何数据——
      提前拒绝比"先收进去、打分阶段再悄悄过滤掉"更好，用户能立刻知道
      原因，不会白传一份简历却看不出匹配分数低是为什么。
    """
    detected = detect_language(text)
    if detected == "unknown":
        return

    locked = get_profile_language()
    if locked is None:
        set_profile_language(detected)
        return

    if detected != locked:
        locked_label = _LANGUAGE_LABEL.get(locked, locked)
        detected_label = _LANGUAGE_LABEL.get(detected, detected)
        raise HTTPException(
            status_code=400,
            detail=(
                f"你的画像已锁定为{locked_label}，这次提交的内容是{detected_label}，"
                f"暂不支持混合语言画像。"
                f" / Your profile is locked to {locked}, but this submission is "
                f"detected as {detected}. Mixed-language profiles are not supported yet."
            ),
        )


# ---------- 1. 上传简历（支持多次上传，累加不覆盖） ----------
class UploadResumeResponse(BaseModel):
    success: bool
    new_chunks: int
    total_chunks_stored: int
    new_keywords: int
    total_keywords_stored: int
    preview_text: str

@router.post("/api/upload-resume", response_model=UploadResumeResponse)
async def upload_resume(file: UploadFile = File(...)):
    filename_lower = file.filename.lower()
    file_ext = next((ext for ext in _ALLOWED_RESUME_EXTS if filename_lower.endswith(ext)), None)
    if file_ext is None:
        raise HTTPException(status_code=400, detail="Only .pdf and .md files are supported")

    # 用户可以多次上传简历——每次都单独存一个文件（用随机文件名，避免同名覆盖），
    # 不再是"固定文件名、每次覆盖"的单份简历模式。
    os.makedirs(RESUMES_DIR, exist_ok=True)
    save_path = os.path.join(RESUMES_DIR, f"{uuid.uuid4().hex}{file_ext}")
    with open(save_path, "wb") as f:
        f.write(await file.read())

    try:
        markdown_text = parse_resume_to_markdown(save_path, file_ext)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse resume file: {e}")

    parsed = parse_resume_markdown(markdown_text)
    if not parsed["chunks"] and not parsed["keywords"]:
        raise HTTPException(status_code=400, detail="No extractable content found in this file")

    _check_and_lock_profile_language(markdown_text)

    new_chunk_count = add_resume_chunks(parsed["chunks"])
    merged_keywords = add_resume_keywords(parsed["keywords"])
    total_chunks = len(get_all_resume_chunks())

    # 存一份简历原文全文——/api/tailor-resume 以它为底稿做定制（chunk 只够检索打分，
    # 还原不出姓名/联系方式/summary/技能/教育背景）。覆盖上一份：定制以"当前简历"为准。
    save_resume_markdown(markdown_text, source_name=file.filename)

    return UploadResumeResponse(
        success=True,
        new_chunks=new_chunk_count,
        total_chunks_stored=total_chunks,
        new_keywords=len(parsed["keywords"]),
        total_keywords_stored=len(merged_keywords),
        preview_text=markdown_text[:500],
    )


# ---------- 2. 补充自由文本（对话框里补的内容，没有公司/项目结构） ----------
class AddResumeNoteRequest(BaseModel):
    note_text: str = Field(..., description="User-supplied free-text supplement")

class AddResumeNoteResponse(BaseModel):
    success: bool
    new_chunks: int
    total_chunks_stored: int

@router.post("/api/add-resume-note", response_model=AddResumeNoteResponse)
def add_resume_note(request: AddResumeNoteRequest):
    if not request.note_text.strip():
        raise HTTPException(status_code=400, detail="note_text 不能为空")

    chunks = parse_free_text_note(request.note_text)
    if not chunks:
        raise HTTPException(status_code=400, detail="没有从补充文本里切出可用的句子")

    _check_and_lock_profile_language(request.note_text)

    new_chunk_count = add_resume_chunks(chunks)
    total_chunks = len(get_all_resume_chunks())

    return AddResumeNoteResponse(
        success=True,
        new_chunks=new_chunk_count,
        total_chunks_stored=total_chunks,
    )


# ---------- 3. 生成定制简历 ----------
class TailorResumeRequest(BaseModel):
    job_description: str = Field(..., description="Full text of the job description")
    user_notes: str = Field(default="", description="Optional extra context for the LLM")
    top_k: int = Field(default=5, description="How many top-scoring resume chunks to feed the LLM")

class TailorResumeResponse(BaseModel):
    tailored_resume: str
    passed_review: bool
    issue: str
    attempts: int
    matched_chunks: list[str]
    changes: list[str] = []

@router.post("/api/tailor-resume", response_model=TailorResumeResponse)
def tailor_resume(request: TailorResumeRequest):
    if not request.job_description.strip():
        raise HTTPException(status_code=400, detail="job_description 不能为空")

    resume_md = load_resume_markdown()
    if not resume_md:
        raise HTTPException(
            status_code=400,
            detail="没有找到简历原文，请先通过 /api/upload-resume 上传简历"
                   "（新版定制流程以完整简历原文为底稿，请重新上传一次）。",
        )

    resume_doc = parse_resume_document(resume_md)
    if not resume_doc.name and not resume_doc.sections:
        raise HTTPException(status_code=400, detail="简历原文没能解析出可用结构，请检查上传的简历文件。")

    result = generate_tailored_resume(
        resume_document=resume_doc,
        job_description=request.job_description,
        extra_context=request.user_notes,
    )

    # matched_chunks 只作展示用（"这份 JD 命中了简历里的哪些经历"），不再参与生成
    matched_chunks: list[str] = []
    resume_chunks = get_all_resume_chunks()
    if resume_chunks:
        jd_parsed = parse_job_description(request.job_description)
        matched = score_resume_chunks_against_jd(
            resume_chunks, load_resume_keywords(), jd_parsed, top_k=request.top_k
        )
        matched_chunks = [m["text"] for m in matched]

    return TailorResumeResponse(
        tailored_resume=result["tailored_resume"],
        passed_review=result["passed_review"],
        issue=result["issue"],
        attempts=result["attempts"],
        matched_chunks=matched_chunks,
        changes=result.get("changes", []),
    )


# ---------- 4. 自动搜索职位 ----------
class JobRecord(BaseModel):
    id: str
    source: str
    title: str
    content: str = ""
    url: str
    posted_at: str | None
    status: str = "new"
    matched_via: list[str] = []  # 关键词命中的字段（如 ["title","tags"]）；
                                  # 目前只有 remoteok/remotive 会填，HN/AnySearch 留空列表
    fit_score: float | None = None   # 跟当前简历画像的匹配度原始分数，供排序用；
                                      # None 表示没法打分（没有画像 / 这条 content 为空）
    fit_label: str | None = None     # "强匹配"/"一般匹配"/"弱匹配"，None 同上

class SearchJobsRequest(BaseModel):
    target_role: str = Field(..., description="Target job direction, e.g. 'AI Engineer'")
    target_region: str = Field(default="Canada Remote", description="Target region")
    max_results: int = Field(default=15)

class SearchJobsResponse(BaseModel):
    jobs: list[JobRecord]
    total: int
    profile_scored: bool  # False 表示用户还没有简历画像数据，fit_score/fit_label 全是 None，
                           # 结果按原来的 round-robin 方式合并，没有按匹配度排序

def _stable_id(prefix: str, url: str) -> str:
    """
    给 anysearch/remoteok/remotive 这三路生成跟进程无关的稳定 record_id。

    之前用的是 f"{prefix}_{abs(hash(url))}"——Python 内置 hash() 对字符串
    从 3.3 起默认开启了哈希随机化（每个进程启动时用随机的 PYTHONHASHSEED），
    同一个 url 在不同进程里 hash() 出来的值不一样。这意味着只要后端重启一次，
    同一条职位的 record_id 就变了，之前存的 status（比如"已投递"）就再也查
    不到——而且不会报错，表现就是"这条职位又变回 new 了"，很容易被误判成
    "status_store 读取逻辑没做对"，实际是 id 本身不稳定。

    改用 md5 摘要（跟进程/PYTHONHASHSEED 无关，同一个字符串永远得到同一个
    结果），取前 16 位十六进制就够用，不需要完整 32 位摘要来保证在这个
    应用的候选量级下不会碰撞。
    HN 那一路（用 HN 帖子自己的 id，不是 Python hash()）本来就是稳定的，
    不用改。
    """
    digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


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
                id=record_id, source=r.source, title=r.title, content=r.content,
                url=r.url, posted_at=r.posted_at, status=get_status(record_id),
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
            record_id = _stable_id("tavily", r.url)
            anysearch_jobs.append(JobRecord(
                id=record_id, source=r.source, title=r.title, content=r.content,
                url=r.url, posted_at=r.posted_at, status=get_status(record_id),
            ))
    except Exception as e:
        print(f"  [调试] 搜索 Agent 失败: {e}")

    # RemoteOK / Remotive 都不支持按地区过滤，target_region 传不进去——
    # 这两路返回的是全球远程岗位，不保证匹配 target_region。
    try:
        remoteok_records = remoteok_search(keyword=request.target_role, max_results=request.max_results)
        for r in remoteok_records:
            record_id = _stable_id("remoteok", r.url)
            remoteok_jobs.append(JobRecord(
                id=record_id, source=r.source, title=r.title, content=r.content,
                url=r.url, posted_at=r.posted_at, status=get_status(record_id),
                matched_via=r.matched_via or [],
            ))
    except Exception as e:
        print(f"  [调试] RemoteOK 抓取失败: {e}")

    try:
        remotive_records = remotive_search(keyword=request.target_role, max_results=request.max_results)
        for r in remotive_records:
            record_id = _stable_id("remotive", r.url)
            remotive_jobs.append(JobRecord(
                id=record_id, source=r.source, title=r.title, content=r.content,
                url=r.url, posted_at=r.posted_at, status=get_status(record_id),
                matched_via=r.matched_via or [],
            ))
    except Exception as e:
        print(f"  [调试] Remotive 抓取失败: {e}")

    # 产品要求：不做硬过滤（不把低匹配度的职位从列表里删掉），只排序+标注，
    # 让用户自己判断要不要点进去——跟 matched_via 那次的设计原则一致，
    # 系统不替用户做"删除"决定。
    resume_chunks = get_all_resume_chunks()
    resume_keywords = load_resume_keywords()
    profile_scored = bool(resume_chunks or resume_keywords)

    if not profile_scored:
        # 用户还没上传过简历/补充资料，没法打分——维持原来的 round-robin
        # 合并方式，不查分数（省掉大量不必要的 embedding 计算）。
        jobs = _round_robin_merge(
            [hn_jobs, anysearch_jobs, remoteok_jobs, remotive_jobs],
            limit=request.max_results,
        )
        return SearchJobsResponse(jobs=jobs, total=len(jobs), profile_scored=False)

    # 实测坐实过一次真实的设计回归：改成"全部候选按 fit_score 全局排序再
    # 截断"之后，HN 一路单独就能产出几十条候选，足够填满 max_results，
    # 导致 AnySearch 这种候选量小但真实有效的来源被完全挤出响应——不是它
    # 没搜到东西，是排序+截断这一步让它连出现的机会都没有。这跟当初接入
    # AnySearch 的初衷（它是四路里唯一没有供给量天花板的来源）直接冲突。
    #
    # 改成"每路来源内部按 fit_score 排序 + 跨来源 round-robin 合并"两者
    # 结合，不是二选一：
    # 1. 每路来源各自按 url 去重（沿用原来的去重逻辑，四路共用同一个
    #    seen_urls，按 hn -> anysearch -> remoteok -> remotive 的顺序
    #    去重优先级不变）。
    # 2. 每路各自打分。
    # 3. 每路各自按 fit_score 从高到低排序（这一步决定"这路里最先给的是
    #    最匹配的"）。
    # 4. 排好的四路列表交给原来的 _round_robin_merge 跨来源轮流取——
    #    这一步保证"只要某路有数据就有基本入选机会"，不会被候选量大的
    #    来源完全挤没。
    seen_urls: set[str] = set()

    def _dedup(jobs: list[JobRecord]) -> list[JobRecord]:
        result = []
        for job in jobs:
            if job.url and job.url in seen_urls:
                continue
            if job.url:
                seen_urls.add(job.url)
            result.append(job)
        return result

    per_source_deduped = [
        _dedup(hn_jobs), _dedup(anysearch_jobs), _dedup(remoteok_jobs), _dedup(remotive_jobs),
    ]

    for job in [j for source_jobs in per_source_deduped for j in source_jobs]:
        if not job.content.strip():
            # content 为空的记录没法解析 JD、更没法打分——fit_score/fit_label
            # 保持 None，不能当成"打出了一个很低的分"，两者语义不一样。
            continue
        jd_parsed = parse_job_description(job.content)
        fit = compute_fit_score(resume_chunks, resume_keywords, jd_parsed, top_k=5)
        job.fit_score = fit
        job.fit_label = fit_score_to_label(fit)

    # sort key 用 (是否为None, -分数) 这种技巧：None 的 key 第一项是
    # True(=1)，排在 False(=0) 后面；用负数让分数本身还是降序。
    def _sort_by_fit(jobs: list[JobRecord]) -> list[JobRecord]:
        return sorted(jobs, key=lambda j: (j.fit_score is None, -(j.fit_score or 0.0)))

    sorted_per_source = [_sort_by_fit(source_jobs) for source_jobs in per_source_deduped]
    jobs = _round_robin_merge(sorted_per_source, limit=request.max_results)

    return SearchJobsResponse(jobs=jobs, total=len(jobs), profile_scored=True)


# ---------- 5. 导出简历（docx / md / pdf 三选一，都保留） ----------
class ExportResumeRequest(BaseModel):
    final_content: str = Field(..., description="User-reviewed final resume text")
    candidate_name: str = Field(..., description="Used for document title and filename")
    # 可选：这份简历是针对哪条职位定制的（/api/search-jobs 返回的 JobRecord.id）。
    # 传了就记一笔 job_id -> 导出文件路径，自动投递（/api/apply/start）时能按
    # job_id 直接找到这份简历，不用用户手动选文件。不传就只是单纯导出，不联动。
    job_id: str | None = Field(default=None, description="Job record id this resume was tailored for")

class ExportResumeResponse(BaseModel):
    success: bool
    download_url: str

@router.post("/api/export-resume-docx", response_model=ExportResumeResponse)
def export_resume_docx(request: ExportResumeRequest):
    if not request.final_content.strip():
        raise HTTPException(status_code=400, detail="final_content 不能为空")
    filepath = export_resume_to_docx(request.final_content, request.candidate_name)
    if request.job_id:
        set_resume_for_job(request.job_id, filepath)
    return ExportResumeResponse(success=True, download_url=f"/files/{os.path.basename(filepath)}")

@router.post("/api/export-resume-md", response_model=ExportResumeResponse)
def export_resume_md(request: ExportResumeRequest):
    if not request.final_content.strip():
        raise HTTPException(status_code=400, detail="final_content 不能为空")
    filepath = export_resume_to_md(request.final_content, request.candidate_name)
    if request.job_id:
        set_resume_for_job(request.job_id, filepath)
    return ExportResumeResponse(success=True, download_url=f"/files/{os.path.basename(filepath)}")

@router.post("/api/export-resume-pdf", response_model=ExportResumeResponse)
def export_resume_pdf(request: ExportResumeRequest):
    if not request.final_content.strip():
        raise HTTPException(status_code=400, detail="final_content 不能为空")
    filepath = export_resume_to_pdf(request.final_content, request.candidate_name)
    if request.job_id:
        set_resume_for_job(request.job_id, filepath)
    return ExportResumeResponse(success=True, download_url=f"/files/{os.path.basename(filepath)}")


_DOWNLOAD_MEDIA_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
}

@router.get("/files/{filename}")
def download_file(filename: str):
    filepath = os.path.join(DATA_DIR, "exports", filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    ext = os.path.splitext(filename)[1].lower()
    media_type = _DOWNLOAD_MEDIA_TYPES.get(ext, "application/octet-stream")
    return FileResponse(filepath, media_type=media_type, filename=filename)

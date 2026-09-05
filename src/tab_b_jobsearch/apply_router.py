"""
自动投递（Tab B 的一部分）接口路由，单独开一个文件而不是塞进
tab_b_jobsearch/router.py，是因为那个文件已经快 500 行了，这块逻辑
又相对独立（浏览器自动化 + 会话态），拆开维护更清楚。
"""

import dataclasses
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.tab_b_jobsearch.apply.orchestrator import start_application, confirm_submit, cancel, ApplyError
from src.core.status_store import update_status
from src.core.resume_by_job_store import get_resume_for_job

router = APIRouter(tags=["Tab B - Auto Apply"])


class ResumeForJobResponse(BaseModel):
    resume_path: str | None


@router.get("/api/apply/resume-for-job/{job_id}", response_model=ResumeForJobResponse)
def apply_resume_for_job(job_id: str):
    """
    这条职位有没有针对性定制并导出过简历——前端在"自动投递"按钮点击前先查一下，
    没有就提示用户先去 Tab B 定制，不用等 /api/apply/start 打开浏览器才发现没简历。
    """
    return ResumeForJobResponse(resume_path=get_resume_for_job(job_id))


class StartApplyRequest(BaseModel):
    job_id: str = Field(..., description="职位记录 id，跟 /api/search-jobs 返回的 JobRecord.id 对应")
    job_url: str
    job_description: str = Field(default="", description="职位描述原文，用来给 LLM 兜底回答开放性问题做上下文")
    resume_path: str | None = Field(default=None, description="不传就用 applicant_profile.json 里配的默认简历")


class FilledFieldOut(BaseModel):
    label: str
    value: str
    source: str


class StartApplyResponse(BaseModel):
    session_id: str
    platform: str
    filled_fields: list[FilledFieldOut]
    screenshot_url: str | None
    ready_to_submit: bool
    warnings: list[str]


@router.post("/api/apply/start", response_model=StartApplyResponse)
def apply_start(request: StartApplyRequest):
    try:
        draft = start_application(
            job_id=request.job_id,
            job_url=request.job_url,
            job_description=request.job_description,
            resume_path=request.resume_path,
        )
    except ApplyError as e:
        raise HTTPException(status_code=400, detail=str(e))

    screenshot_url = f"/api/apply/screenshot/{draft.session_id}" if draft.screenshot_path else None
    return StartApplyResponse(
        session_id=draft.session_id,
        platform=draft.platform,
        filled_fields=[FilledFieldOut(**dataclasses.asdict(f)) for f in draft.filled_fields],
        screenshot_url=screenshot_url,
        ready_to_submit=draft.ready_to_submit,
        warnings=draft.warnings,
    )


@router.get("/api/apply/screenshot/{session_id}")
def apply_screenshot(session_id: str):
    from src.tab_b_jobsearch.apply.orchestrator import SCREENSHOT_DIR
    path = os.path.join(SCREENSHOT_DIR, f"{session_id}.png")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="截图不存在（可能这个会话已经确认/取消过了）")
    return FileResponse(path, media_type="image/png")


class SessionIdRequest(BaseModel):
    session_id: str


class ApplyResultResponse(BaseModel):
    success: bool
    message: str = ""


@router.post("/api/apply/confirm", response_model=ApplyResultResponse)
def apply_confirm(request: SessionIdRequest):
    try:
        result = confirm_submit(request.session_id)
    except ApplyError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        update_status(result["job_id"], "applied")
    except ValueError:
        pass  # status_store 校验失败不应该让"已经提交出去的投递"报错回滚，最多状态没同步上

    return ApplyResultResponse(success=True, message="已提交")


@router.post("/api/apply/cancel", response_model=ApplyResultResponse)
def apply_cancel(request: SessionIdRequest):
    cancel(request.session_id)
    return ApplyResultResponse(success=True, message="已取消，没有提交任何内容")

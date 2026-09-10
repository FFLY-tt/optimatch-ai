"""
自动投递（Tab B 的一部分）接口路由，单独开一个文件而不是塞进
tab_b_jobsearch/router.py，是因为那个文件已经快 500 行了，这块逻辑
又相对独立（浏览器自动化 + 会话态），拆开维护更清楚。
"""

import dataclasses
import logging
import os
import traceback

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.tab_b_jobsearch.apply.orchestrator import start_application, confirm_submit, cancel, ApplyError
from src.tab_b_jobsearch.apply.browser import run_in_browser_thread
from src.core.resume_by_job_store import get_resume_for_job
from src.core.applied_jobs_store import record_applied
from src.core import apply_dashboard
from src.connectors.anysearch_connector import looks_like_job_listing_page

router = APIRouter(tags=["Tab B - Auto Apply"])
log = logging.getLogger("optimatch.apply")


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
    job_title: str = Field(default="", description="职位标题，投递成功后记进'已投递'去重记录")
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
    # 兜底：万一有职位列表/聚合页漏过了 anysearch 那层过滤，别真的拿它去打开浏览器
    # 填表——那页上是一堆不同职位，没有可投的表单。直接告诉用户点职位标题手动看。
    if looks_like_job_listing_page(request.job_url):
        raise HTTPException(
            status_code=400,
            detail=(
                "这条结果的链接指向的是职位列表/搜索页，不是某一条具体职位的申请页，"
                "没法自动投递。请点职位标题打开原链接，手动挑一条具体职位再操作。"
            ),
        )

    try:
        # 所有 Playwright 操作固定在专用浏览器线程里跑（同步 API 不能跨 FastAPI 线程池的线程用）。
        draft = run_in_browser_thread(
            start_application,
            _timeout=140,   # 前端 startApply 是 150s，后端比它早一点结束，好返回一个明确的错
            job_id=request.job_id,
            job_url=request.job_url,
            job_description=request.job_description,
            resume_path=request.resume_path,
            job_title=request.job_title,
        )
    except ApplyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # start_application 已经把内部异常都转成 ApplyError 了；能走到这里的基本是
        # run_in_browser_thread 本身出问题。仍然返回带 detail 的 JSON，绝不裸奔成 500。
        log.error("apply.start 路由层未预期异常:\n%s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"自动投递启动失败：{type(e).__name__}: {e}")

    # 表单打开了但卡住了（验证码 / 找不到提交按钮）——标成"需人工处理"，进看板等用户收尾。
    if not draft.ready_to_submit:
        blocker = "验证码" if any("验证码" in w for w in draft.warnings) else "没找到可点击的提交按钮"
        try:
            apply_dashboard.mark_needs_user(
                request.job_id, reason=blocker,
                job_url=request.job_url, job_title=request.job_title,
            )
        except Exception as e:
            log.warning("mark_needs_user 失败: %s", e)

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
        result = run_in_browser_thread(confirm_submit, request.session_id, _timeout=55)
    except ApplyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.error("apply.confirm 路由层未预期异常:\n%s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"提交时出错：{type(e).__name__}: {e}")

    # 根据 confirm_submit 抓到的信号定状态：
    #   outcome == "confirmed" -> applied_confirmed（拿到了感谢页 / 确认文案 / application id）
    #   否则                    -> submitted_unverified（点击成功但没确认信号，多数 ATS 属于这类）
    outcome = result.get("outcome", "submitted")
    reason = result.get("outcome_reason", "")
    try:
        if outcome == "confirmed":
            apply_dashboard.mark_confirmed(result["job_id"], reason,
                                          result.get("job_url", ""), result.get("job_title", ""))
            msg = "已确认投递（页面出现了确认信息）"
        else:
            apply_dashboard.mark_submitted(result["job_id"], reason,
                                           result.get("job_url", ""), result.get("job_title", ""))
            msg = "表单已提交，但没抓到对方系统的确认信息——请自己确认一下（收到确认邮件后可在投递看板里标为已确认）"
    except Exception as e:
        log.warning("写投递状态失败: %s", e)
        msg = "已提交"

    # 记进"已投递"去重记录：之后搜索结果里同一条职位（含跨来源）直接不再展示。
    try:
        record_applied(
            job_url=result.get("job_url", ""),
            job_title=result.get("job_title", ""),
            record_id=result.get("job_id", ""),
        )
    except Exception as e:
        print(f"  [调试] 记录已投递职位失败（不影响投递本身）: {e}")

    return ApplyResultResponse(success=True, message=msg)


@router.post("/api/apply/cancel", response_model=ApplyResultResponse)
def apply_cancel(request: SessionIdRequest):
    try:
        run_in_browser_thread(cancel, request.session_id, _timeout=30)
    except Exception as e:
        log.warning("apply.cancel 出错（会话可能已失效，忽略）: %s", e)
    return ApplyResultResponse(success=True, message="已取消，没有提交任何内容")


# ---------- 投递看板（只读 + 手动收尾） ----------
class DashboardRow(BaseModel):
    record_id: str
    status: str
    status_label: str
    status_reason: str = ""
    updated_at: str = ""
    title: str = ""
    company: str = ""
    url: str = ""
    source: str = ""
    fit_score: float | None = None
    fit_label: str = ""
    relevance_label: str = ""
    first_seen: str = ""
    applied_at: str = ""


class DashboardResponse(BaseModel):
    queue: list[DashboardRow]
    applications: list[DashboardRow]


@router.get("/api/apply/dashboard", response_model=DashboardResponse)
def apply_dashboard_view():
    """Queue（待处理）+ Applications（历史）两个视图的只读数据。刷新页面即拿最新状态。"""
    try:
        apply_dashboard.compact()  # 每天最多跑一次，清理长期没再出现的排队记录 + 归档一年前的投递
    except Exception as e:
        log.warning("apply_dashboard.compact 失败（不影响读取）: %s", e)
    return DashboardResponse(
        queue=apply_dashboard.queue_view(),
        applications=apply_dashboard.applications_view(),
    )


class MarkConfirmedRequest(BaseModel):
    record_id: str


@router.post("/api/apply/mark-confirmed", response_model=ApplyResultResponse)
def apply_mark_confirmed(request: MarkConfirmedRequest):
    """
    手动收尾：把一条"已提交·待确认"（或 needs_user / 旧 applied）标成"已确认投递"。
    用于自动流程抓不到确认信号、但用户收到了确认邮件的情况。
    """
    ok = apply_dashboard.mark_confirmed_by_id(request.record_id)
    if not ok:
        raise HTTPException(status_code=400, detail="这条记录当前状态不支持手动标为已确认。")
    return ApplyResultResponse(success=True, message="已标记为「已确认投递」")

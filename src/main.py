"""
FastAPI 主入口。
启动方式（在项目根目录下）：
    uvicorn src.main:app --reload
"""
import logging
import sys

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# uvicorn 默认只给自己的 logger 配 handler，项目里 logging.getLogger("optimatch.*")
# 的输出默认看不到。这里显式给 optimatch 这个 logger 挂一个到 stderr 的 handler，
# 让自动投递等链路的分步日志能在 uvicorn 控制台直接看到（排查"卡在哪一步"用）。
_optimatch_logger = logging.getLogger("optimatch")
if not _optimatch_logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s", "%H:%M:%S"))
    _optimatch_logger.addHandler(_h)
    _optimatch_logger.setLevel(logging.INFO)
    _optimatch_logger.propagate = False

# 引入状态管理
from src.core.status_store import update_status as _update_status

# 引入业务模块的 Router
from src.tab_a_outreach.router import router as tab_a_router
from src.tab_b_jobsearch.router import router as tab_b_router
from src.tab_b_jobsearch.apply_router import router as apply_router

app = FastAPI(title="OptiMatch AI - API")

# 挂载业务路由
app.include_router(tab_a_router)
app.include_router(tab_b_router)
app.include_router(apply_router)


# ---------- 通用状态管理 ----------
class UpdateStatusRequest(BaseModel):
    record_id: str
    status: str

@app.post("/api/update-status", tags=["Common"])
def update_status(request: UpdateStatusRequest):
    try:
        _update_status(request.record_id, request.status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True}


# ---------- 系统接口 ----------
@app.get("/api/health", tags=["System"])
def health_check():
    return {"status": "ok"}

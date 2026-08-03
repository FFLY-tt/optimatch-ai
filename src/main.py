"""
FastAPI 主入口。
启动方式（在项目根目录下）：
    uvicorn src.main:app --reload
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# 引入状态管理
from src.core.status_store import update_status as _update_status

# 引入两个业务模块的 Router
from src.tab_a_outreach.router import router as tab_a_router
from src.tab_b_jobsearch.router import router as tab_b_router

app = FastAPI(title="OptiMatch AI - API")

# 挂载业务路由
app.include_router(tab_a_router)
app.include_router(tab_b_router)


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
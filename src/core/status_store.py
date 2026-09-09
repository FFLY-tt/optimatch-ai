"""
职位/线索状态存储。
MVP 阶段用一个本地 JSON 文件做持久化，不需要真正的数据库——
数据量小（单用户，几十到几百条记录），JSON 文件完全够用。
后面如果要支持多用户/云端部署，再换成真正的数据库，接口不用变。

--- 状态模型（2026-09 扩展）---
旧版只有 new / viewed / contacted / ignored / applied 五个平铺的字符串值。
自动投递上线后需要区分"点了提交"和"确认对方收到"，把投递相关的状态细化成：

    queued_for_review    已抓取/已打分，排队等待处理（人工或自动），还没执行投递动作
    submitted_unverified 表单提交动作执行了，但没拿到"对方系统收到"的确认信号
    applied_confirmed    已确认投递成功（跳转确认页 / 感谢文案 / application id 等）
    needs_user           卡住，需要人工介入（缺信息 / 验证码 / 登录失效 / 报错）

旧值里 applied 语义上最接近 applied_confirmed（过去就是"认为投成功了"），
迁移脚本 scripts/migrate_status_store.py 会把历史 applied 映射过去。
new / viewed / contacted / ignored 这几个 Tab A（商机）也在用，保留不动。

存储格式向后兼容：每条记录既可以是纯字符串（老格式），也可以是
{"status": ..., "reason": ..., "updated_at": ...}（新格式）。读取时统一
用 get_record() 归一化成 dict。
"""

import os
import json
import threading
from datetime import datetime, timezone

STATUS_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "status_store.json")
_lock = threading.Lock()

# 投递流程的四个细化状态
STATUS_QUEUED = "queued_for_review"
STATUS_SUBMITTED_UNVERIFIED = "submitted_unverified"
STATUS_APPLIED_CONFIRMED = "applied_confirmed"
STATUS_NEEDS_USER = "needs_user"

# Tab A（商机）沿用的旧状态 + 兼容旧 Tab B 的 "applied"
_LEGACY_STATUSES = {"new", "viewed", "contacted", "ignored", "applied"}
_APPLY_STATUSES = {
    STATUS_QUEUED, STATUS_SUBMITTED_UNVERIFIED, STATUS_APPLIED_CONFIRMED, STATUS_NEEDS_USER,
}
VALID_STATUSES = _LEGACY_STATUSES | _APPLY_STATUSES

# 这些状态属于"已经处理过"（Applications 视图），其余（含 queued / new）属于待处理
TERMINAL_APPLY_STATUSES = {
    STATUS_SUBMITTED_UNVERIFIED, STATUS_APPLIED_CONFIRMED, STATUS_NEEDS_USER, "applied",
}


def _load() -> dict:
    if not os.path.exists(STATUS_FILE):
        return {}
    with open(STATUS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _normalize(raw) -> dict:
    """老格式（纯字符串）/ 新格式（dict）统一成 {status, reason, updated_at}。"""
    if isinstance(raw, str):
        return {"status": raw, "reason": "", "updated_at": ""}
    if isinstance(raw, dict):
        return {
            "status": raw.get("status", "new"),
            "reason": raw.get("reason", "") or raw.get("status_detail", ""),
            "updated_at": raw.get("updated_at", ""),
        }
    return {"status": "new", "reason": "", "updated_at": ""}


def update_status(record_id: str, status: str, reason: str = "") -> None:
    if status not in VALID_STATUSES:
        raise ValueError(f"status 必须是 {sorted(VALID_STATUSES)} 之一，收到: {status}")

    with _lock:
        data = _load()
        data[record_id] = {
            "status": status,
            "reason": reason,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _save(data)


def get_status(record_id: str) -> str:
    """只要状态字符串（大量老调用点用这个，签名不变）。"""
    with _lock:
        return _normalize(_load().get(record_id, "new"))["status"]


def get_record(record_id: str) -> dict:
    """要完整记录（status + reason + updated_at）。"""
    with _lock:
        return _normalize(_load().get(record_id, "new"))


def all_records() -> dict[str, dict]:
    """全部记录，归一化成 {record_id: {status, reason, updated_at}}。"""
    with _lock:
        return {k: _normalize(v) for k, v in _load().items()}

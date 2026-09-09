"""
投递看板的"数据整理层"。

页面（Queue / Applications 两个视图）需要的完整记录，散在三个地方：
- status_store.json     ：record_id -> {status, reason, updated_at}   （状态）
- applied_jobs.json     ：已投递职位的 url / title / source / applied_at（去重记录）
- job_queue.json（本模块自己维护）：record_id -> 职位元数据
                          （title / company / url / source / fit_score / fit_label /
                           relevance_label / first_seen / last_seen）
  —— 因为 fit_score / relevance_label 这些原本只在搜索响应里、从不落盘，
     Queue 视图又必须展示它们，所以搜索命中"够格"的职位时在这里存一份。

对外：
- 写：queue_jobs()（搜索时调）、mark_submitted() / mark_confirmed() / mark_needs_user()
      （投递流程调）、mark_confirmed_by_id()（用户手动收尾）
- 读：queue_view() / applications_view()（只读 API 用）
"""

import json
import os
import re
import threading
from datetime import datetime, timezone

from src.core import status_store
from src.core.applied_jobs_store import list_applied, job_identity_key, normalize_job_url

QUEUE_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "job_queue.json")
_lock = threading.Lock()

# fit_label 这几档才值得进队列（弱匹配 / 没打分的不自动排队，避免队列被噪声塞满）
_QUEUEABLE_FIT_LABELS = {"强匹配", "一般匹配"}
# 这些状态说明用户/流程已经处理过这条，搜索再次遇到时不要把它打回 queued
_DO_NOT_REQUEUE = (
    status_store.TERMINAL_APPLY_STATUSES | {"ignored", "contacted"}
)

_COMPANY_FROM_TITLE = re.compile(r"\s+(?:at|@|[-–—|·:])\s+([A-Za-z0-9&.\-' ]{2,40})\s*$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_queue() -> dict:
    if not os.path.exists(QUEUE_FILE):
        return {}
    with open(QUEUE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_queue(data: dict) -> None:
    os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _company_of(title: str, url: str) -> str:
    from src.core.applied_jobs_store import _COMPANY_FROM_URL
    m = _COMPANY_FROM_URL.search(url or "")
    if m:
        return m.group(1)
    m = _COMPANY_FROM_TITLE.search(title or "")
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------- 写

def queue_jobs(jobs: list[dict]) -> int:
    """
    搜索结果里"够格"的职位进队列。jobs: [{id,title,url,source,fit_score,fit_label,
    relevance_label?,content?}, ...]。返回新排队的条数。
    - fit_label 不在 _QUEUEABLE_FIT_LABELS 里的跳过
    - 已经是"处理过"状态（已投递/needs_user/ignored…）的不打回队列，只刷新 last_seen + 分数
    """
    if not jobs:
        return 0
    newly = 0
    with _lock:
        q = _load_queue()
        records = status_store.all_records()
        for j in jobs:
            rid = j.get("id")
            if not rid:
                continue
            cur_status = records.get(rid, {}).get("status", "new")
            queueable = (j.get("fit_label") or "") in _QUEUEABLE_FIT_LABELS
            already_tracked = rid in q or rid in records
            # 弱匹配 / 没打分、又从没被跟踪过的职位：不写进队列存储，免得越攒越大
            if not queueable and not already_tracked:
                continue
            meta = q.get(rid, {})
            meta.update({
                "record_id": rid,
                "title": j.get("title", "") or meta.get("title", ""),
                "company": j.get("company") or _company_of(j.get("title", ""), j.get("url", "")) or meta.get("company", ""),
                "url": j.get("url", "") or meta.get("url", ""),
                "source": j.get("source", "") or meta.get("source", ""),
                "fit_score": j.get("fit_score", meta.get("fit_score")),
                "fit_label": j.get("fit_label") or meta.get("fit_label", ""),
                "relevance_label": j.get("relevance_label") or meta.get("relevance_label", ""),
                "first_seen": meta.get("first_seen") or _now(),
                "last_seen": _now(),
            })
            q[rid] = meta

            if cur_status in _DO_NOT_REQUEUE or not queueable:
                continue
            if cur_status != status_store.STATUS_QUEUED:
                status_store.update_status(rid, status_store.STATUS_QUEUED, reason="fit_ok_from_search")
                newly += 1
        _save_queue(q)
    return newly


def _set(record_id: str, status: str, reason: str, meta_updates: dict | None = None) -> None:
    with _lock:
        status_store.update_status(record_id, status, reason=reason)
        if meta_updates:
            q = _load_queue()
            m = q.get(record_id, {"record_id": record_id, "first_seen": _now()})
            m.update(meta_updates)
            m["last_seen"] = _now()
            q[record_id] = m
            _save_queue(q)


def mark_submitted(record_id: str, reason: str = "", job_url: str = "", job_title: str = "") -> None:
    _set(record_id, status_store.STATUS_SUBMITTED_UNVERIFIED, reason or "click_ok_no_confirmation_signal",
         {"url": job_url, "title": job_title} if (job_url or job_title) else None)


def mark_confirmed(record_id: str, reason: str = "", job_url: str = "", job_title: str = "") -> None:
    _set(record_id, status_store.STATUS_APPLIED_CONFIRMED, reason or "confirmed",
         {"url": job_url, "title": job_title} if (job_url or job_title) else None)


def mark_needs_user(record_id: str, reason: str, job_url: str = "", job_title: str = "") -> None:
    _set(record_id, status_store.STATUS_NEEDS_USER, reason,
         {"url": job_url, "title": job_title} if (job_url or job_title) else None)


def mark_confirmed_by_id(record_id: str) -> bool:
    """用户手动把一条 submitted_unverified 收尾成 applied_confirmed。"""
    rec = status_store.get_record(record_id)
    if rec["status"] not in (status_store.STATUS_SUBMITTED_UNVERIFIED, status_store.STATUS_NEEDS_USER, "applied"):
        return False
    _set(record_id, status_store.STATUS_APPLIED_CONFIRMED, "user_marked_confirmed")
    return True


# ---------------------------------------------------------------- 读

_STATUS_LABEL = {
    status_store.STATUS_QUEUED: "排队待处理",
    status_store.STATUS_SUBMITTED_UNVERIFIED: "已提交·待确认",
    status_store.STATUS_APPLIED_CONFIRMED: "已确认投递",
    status_store.STATUS_NEEDS_USER: "需人工处理",
    "applied": "已投递（旧）",
    "ignored": "已忽略",
    "new": "新",
    "viewed": "看过",
    "contacted": "已联系",
}


def _applied_index() -> dict:
    """applied_jobs_store 按 record_id / 规范化url / identity key 建索引，方便 join。"""
    by_id, by_nurl, by_key = {}, {}, {}
    for e in list_applied():
        if e.get("record_id"):
            by_id[e["record_id"]] = e
        if e.get("nurl"):
            by_nurl[e["nurl"]] = e
        if e.get("key"):
            by_key[e["key"]] = e
    return {"id": by_id, "nurl": by_nurl, "key": by_key}


def _row(record_id: str, rec: dict, meta: dict, applied: dict | None) -> dict:
    return {
        "record_id": record_id,
        "status": rec["status"],
        "status_label": _STATUS_LABEL.get(rec["status"], rec["status"]),
        "status_reason": rec.get("reason", ""),
        "updated_at": rec.get("updated_at", ""),
        "title": meta.get("title") or (applied or {}).get("title", ""),
        "company": meta.get("company", ""),
        "url": meta.get("url") or (applied or {}).get("url", ""),
        "source": meta.get("source") or (applied or {}).get("source", ""),
        "fit_score": meta.get("fit_score"),
        "fit_label": meta.get("fit_label", ""),
        "relevance_label": meta.get("relevance_label", ""),
        "first_seen": meta.get("first_seen", ""),
        "applied_at": (applied or {}).get("applied_at", ""),
    }


def _all_rows() -> list[dict]:
    q = _load_queue()
    records = status_store.all_records()
    ai = _applied_index()

    # 覆盖两个来源的所有 record_id：queue 里的 + status_store 里的 + applied 里的
    ids = set(q) | set(records) | set(ai["id"])
    rows = []
    for rid in ids:
        rec = records.get(rid) or {"status": "new", "reason": "", "updated_at": ""}
        meta = q.get(rid, {})
        applied = ai["id"].get(rid)
        if applied is None and meta.get("url"):
            applied = ai["nurl"].get(normalize_job_url(meta["url"]))
        if applied is None:
            applied = ai["key"].get(job_identity_key(meta.get("title", ""), meta.get("company", ""), meta.get("url", "")))
        # 只在 applied_jobs 里、status_store 没记过的：按"已确认投递"处理（迁移前的旧数据也走这条）
        if applied is not None and rid not in records:
            rec = {"status": status_store.STATUS_APPLIED_CONFIRMED, "reason": "from_applied_jobs_store",
                   "updated_at": applied.get("applied_at", "")}
        rows.append(_row(rid, rec, meta, applied))
    return rows


def queue_view() -> list[dict]:
    """已排队、还没处理的职位——按 fit_score 降序。"""
    rows = [r for r in _all_rows() if r["status"] == status_store.STATUS_QUEUED]
    rows.sort(key=lambda r: (r["fit_score"] is None, -(r["fit_score"] or 0.0)))
    return rows


def applications_view() -> list[dict]:
    """已经处理过的（四个细化态里非 queued + 旧 applied）——按 updated_at 降序。"""
    keep = status_store.TERMINAL_APPLY_STATUSES
    rows = [r for r in _all_rows() if r["status"] in keep]
    rows.sort(key=lambda r: (r["updated_at"] or r["applied_at"] or ""), reverse=True)
    return rows

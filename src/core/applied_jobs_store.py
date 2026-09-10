"""
已投递职位记录 + 去重判断。

投递成功（review modal 里点"确认投递" / 自动投递走完）时记一笔；之后
/api/search-jobs 返回结果前，命中"已投递记录"的职位直接剔除，不再推荐。

去重不追求 100% 精确，覆盖常见的"同一条职位反复出现"：
1. 规范化 URL（去 scheme / www / query / 尾部斜杠）—— 同一个详情页换个 utm 参数、
   http/https、带不带斜杠都算同一条。
2. 公司 + 职位标题的模糊 key —— 换个来源域名、URL 结构不同但确实是同一个岗位
   （比如 boards.greenhouse.io/acme/... 和 job-boards.greenhouse.io/acme/...）。
   只在能确定公司名时才用这条，避免"两家公司都招 Software Engineer"被误判成同一条。

跟 status_store.py 一样的取舍：本地 JSON + 线程锁，单用户、数据量小。
"""

import json
import os
import re
import threading
from datetime import datetime, timezone
from urllib.parse import urlsplit

APPLIED_JOBS_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "applied_jobs.json")
_lock = threading.Lock()


def _load() -> dict:
    if not os.path.exists(APPLIED_JOBS_FILE):
        return {"entries": []}
    with open(APPLIED_JOBS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("entries", [])
    return data


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(APPLIED_JOBS_FILE), exist_ok=True)
    with open(APPLIED_JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def normalize_job_url(url: str) -> str:
    """去 scheme / www / query / fragment / 尾部斜杠，只留 host+path（小写）。"""
    if not url:
        return ""
    try:
        p = urlsplit(url.strip())
    except Exception:
        return url.strip().lower()
    host = (p.netloc or "").lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+$", "", p.path or "")
    if not host and not path:
        return url.strip().lower()
    return f"{host}{path}".lower()


# 标题尾部的 "... at Reddit" / "... - Reddit" / "... | Reddit"
_COMPANY_FROM_TITLE = re.compile(r"\s+(?:at|@|[-–—|·:])\s+([A-Za-z0-9&.\-' ]{2,40})\s*$")
# ATS 详情页 URL 里的公司段：greenhouse.io/<company>/ 、lever.co/<company>/ 等
_COMPANY_FROM_URL = re.compile(
    r"(?:greenhouse\.io|lever\.co|ashbyhq\.com|myworkdayjobs\.com|smartrecruiters\.com|workable\.com)"
    r"/([a-z0-9][a-z0-9\-]{1,40})",
    re.IGNORECASE,
)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def job_identity_key(title: str, company: str = "", url: str = "") -> str:
    """
    公司+职位标题的模糊去重 key，形如 "reddit|datamovementplatformsoftwareengineer"。
    公司名来源优先级：显式 company 参数 > ATS URL 里的公司段 > 标题尾部 "at X"。
    确定不了公司名就返回 ""（表示"别用这条 key 去匹配"），只靠 URL 去重。
    """
    title = (title or "").strip()
    if not company and url:
        m = _COMPANY_FROM_URL.search(url)
        if m:
            company = m.group(1)
    if not company:
        m = _COMPANY_FROM_TITLE.search(title)
        if m:
            company = m.group(1).strip()
            title = title[: m.start()].strip()
    c, t = _slug(company), _slug(title)
    if not c or not t:
        return ""
    return f"{c}|{t}"


def record_applied(
    job_url: str, job_title: str = "", company: str = "", source: str = "", record_id: str = ""
) -> None:
    """投递成功时调用。已经记过（URL 或 identity key 命中）就跳过，幂等。"""
    nurl = normalize_job_url(job_url)
    key = job_identity_key(job_title, company, job_url)
    with _lock:
        data = _load()
        for e in data["entries"]:
            if nurl and e.get("nurl") == nurl:
                return
            if key and e.get("key") == key:
                return
        data["entries"].append({
            "url": job_url,
            "nurl": nurl,
            "key": key,
            "title": job_title,
            "source": source,
            "record_id": record_id,
            "applied_at": datetime.now(timezone.utc).isoformat(),
        })
        _save(data)


def is_applied(job_url: str = "", job_title: str = "", company: str = "", record_id: str = "") -> bool:
    nurl = normalize_job_url(job_url)
    key = job_identity_key(job_title, company, job_url)
    with _lock:
        entries = _load()["entries"]
    for e in entries:
        if nurl and e.get("nurl") == nurl:
            return True
        if record_id and e.get("record_id") and e["record_id"] == record_id:
            return True
        if key and e.get("key") == key:
            return True
    return False


def list_applied() -> list[dict]:
    with _lock:
        return list(_load()["entries"])


_ARCHIVE_FILE = APPLIED_JOBS_FILE.replace(".json", ".archive.json")
_MAX_ACTIVE_ENTRIES = 5000
_ARCHIVE_AFTER_DAYS = 365


def compact() -> dict:
    """
    去重台账不会真的"无限膨胀"（每条 ~200 字节），但一年前的记录去重价值≈0
    （那条 posting 早没了）。把超过一年的挪进 applied_jobs.archive.json，
    再对活跃部分留最新 _MAX_ACTIVE_ENTRIES 条。返回 {archived, dropped, kept}。
    """
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_ARCHIVE_AFTER_DAYS)).isoformat()
    with _lock:
        data = _load()
        entries = data["entries"]
        fresh = [e for e in entries if (e.get("applied_at") or "9999") >= cutoff]
        aged = [e for e in entries if (e.get("applied_at") or "9999") < cutoff]

        fresh.sort(key=lambda e: e.get("applied_at") or "", reverse=True)
        dropped = fresh[_MAX_ACTIVE_ENTRIES:]
        fresh = fresh[:_MAX_ACTIVE_ENTRIES]

        to_archive = aged + dropped
        if to_archive:
            arch = []
            if os.path.exists(_ARCHIVE_FILE):
                with open(_ARCHIVE_FILE, "r", encoding="utf-8") as f:
                    arch = json.load(f).get("entries", [])
            arch.extend(to_archive)
            with open(_ARCHIVE_FILE, "w", encoding="utf-8") as f:
                json.dump({"entries": arch}, f, ensure_ascii=False, indent=2)
            data["entries"] = fresh
            _save(data)
        return {"archived": len(aged), "dropped": len(dropped), "kept": len(fresh)}

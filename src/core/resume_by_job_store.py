"""
"这份定制简历是为哪个职位生成的" 映射存储。
把 Tab B 的简历定制/导出（/api/export-resume-*）和自动投递
（/api/apply/start）串起来：导出成功时记一笔 job_id -> 简历文件路径，
投递时按 job_id 查，用户不用手动选文件。

跟 status_store.py 同样的取舍：MVP 阶段用本地 JSON 文件持久化，
单用户、数据量小，不需要真正的数据库。
"""

import os
import json
import threading

RESUME_BY_JOB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "resume_by_job.json")
_lock = threading.Lock()


def _load() -> dict:
    if not os.path.exists(RESUME_BY_JOB_FILE):
        return {}
    with open(RESUME_BY_JOB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(RESUME_BY_JOB_FILE), exist_ok=True)
    with open(RESUME_BY_JOB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def set_resume_for_job(job_id: str, resume_path: str) -> None:
    with _lock:
        data = _load()
        data[job_id] = resume_path
        _save(data)


def get_resume_for_job(job_id: str) -> str | None:
    with _lock:
        data = _load()
        return data.get(job_id)

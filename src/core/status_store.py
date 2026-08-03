"""
状态管理存储。
MVP 阶段用一个本地 JSON 文件做持久化，不需要真正的数据库——
数据量小（单用户，几十到几百条记录），JSON 文件完全够用。
后面如果要支持多用户/云端部署，再换成真正的数据库，接口不用变。
"""

import os
import json
import threading

STATUS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "status_store.json")
_lock = threading.Lock()


def _load() -> dict:
    if not os.path.exists(STATUS_FILE):
        return {}
    with open(STATUS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_status(record_id: str, status: str) -> None:
    valid_statuses = {"new", "viewed", "contacted", "ignored"}
    if status not in valid_statuses:
        raise ValueError(f"status 必须是 {valid_statuses} 之一，收到: {status}")

    with _lock:
        data = _load()
        data[record_id] = status
        _save(data)


def get_status(record_id: str) -> str:
    with _lock:
        data = _load()
        return data.get(record_id, "new")
"""
"填好表单、等人工确认" 这个中间状态的会话登记表。

因为浏览器 page 对象没法序列化存进 JSON/数据库，这里就用一个进程内的
dict 记着——意味着重启后端进程会导致所有待确认的会话失效（浏览器页面
也一起没了），这对 MVP 来说是可接受的取舍：待确认的会话本来就该
"填完就尽快确认或取消"，不是需要长期持久化的东西。
"""

import threading
import time
import uuid

_SESSIONS: dict[str, dict] = {}
_lock = threading.Lock()
STALE_AFTER_SECONDS = 30 * 60


def create_session(**kwargs) -> str:
    session_id = uuid.uuid4().hex
    with _lock:
        _sweep_stale_locked()
        _SESSIONS[session_id] = {**kwargs, "created_at": time.time()}
    return session_id


def get_session(session_id: str) -> dict | None:
    with _lock:
        return _SESSIONS.get(session_id)


def pop_session(session_id: str) -> dict | None:
    with _lock:
        return _SESSIONS.pop(session_id, None)


def _sweep_stale_locked() -> None:
    now = time.time()
    stale_ids = [sid for sid, s in _SESSIONS.items() if now - s["created_at"] > STALE_AFTER_SECONDS]
    for sid in stale_ids:
        session = _SESSIONS.pop(sid)
        page = session.get("page")
        if page is not None:
            try:
                page.close()
            except Exception:
                pass

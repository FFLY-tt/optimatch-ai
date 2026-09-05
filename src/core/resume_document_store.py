"""
"完整简历原文"存储。

定制简历需要以完整原始简历为底稿（姓名/联系方式/summary/技能/教育背景一项不少），
而 vector_store 里只存了检索用的句子 chunk，还原不出来。这里单独存一份用户
最近一次上传的简历 Markdown 全文（PDF/md 解析后的结果），供 /api/tailor-resume 用。

跟 status_store.py 一样：MVP 阶段一个本地 JSON 文件 + threading.Lock 就够了。
"""

import json
import os
import threading
from datetime import datetime, timezone

STORE_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "resume_document.json")
_lock = threading.Lock()


def _load() -> dict:
    if not os.path.exists(STORE_FILE):
        return {}
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(STORE_FILE), exist_ok=True)
    with open(STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_resume_markdown(markdown_text: str, source_name: str = "") -> None:
    """记录用户最近一次上传的简历原文（覆盖上一份——定制以"当前简历"为底稿）。"""
    with _lock:
        _save({
            "markdown": markdown_text,
            "source_name": source_name,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        })


def load_resume_markdown() -> str | None:
    """取用户最近一次上传的简历原文；没有返回 None。"""
    with _lock:
        return _load().get("markdown") or None


def clear_resume_markdown() -> None:
    """清空（测试用 / 用户想重来）。"""
    with _lock:
        if os.path.exists(STORE_FILE):
            os.remove(STORE_FILE)

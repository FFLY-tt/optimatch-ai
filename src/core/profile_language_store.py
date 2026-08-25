"""
用户画像语言锁存储。

产品要求：单个用户的画像（所有已上传简历 + 所有补充文本的合集）必须是
单一语言——要么全中文要么全英文，不能同一个人的画像里混着中文简历和
英文简历（平台整体仍然同时支持纯中文用户和纯英文用户，只是不允许
"同一个人"混着来）。

这个模块只记录一件事："这个画像第一次确定下来是什么语言"。第一次
上传/提交内容时，检测出什么语言就锁定成什么；之后每次新内容进来，
都拿它的语言和这个锁定值比对，不一致就拒绝（比对逻辑在
router.py 里，不在这个模块）。

和 status_store.py 一样，MVP 阶段单用户场景，本地 JSON 文件足够，
不需要真数据库。

TODO: 目前没有提供"重置画像语言"的功能（比如用户想清空画像、换一种
语言重新开始）。这是产品层面还没定的另一个问题（要不要允许重置、
重置时要不要连带清掉已存的 chunk/keyword），这次范围只做"检测并拒绝"，
不顺手加重置接口，等产品那边明确了再做。
"""

import os
import json
import threading

PROFILE_LANGUAGE_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "profile_language.json")
_lock = threading.Lock()


def get_profile_language() -> str | None:
    """画像还没锁定过语言（文件不存在，即这个画像的第一次输入还没发生）时返回 None。"""
    with _lock:
        if not os.path.exists(PROFILE_LANGUAGE_FILE):
            return None
        with open(PROFILE_LANGUAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("language")


def set_profile_language(lang: str) -> None:
    with _lock:
        os.makedirs(os.path.dirname(PROFILE_LANGUAGE_FILE), exist_ok=True)
        with open(PROFILE_LANGUAGE_FILE, "w", encoding="utf-8") as f:
            json.dump({"language": lang}, f, ensure_ascii=False, indent=2)

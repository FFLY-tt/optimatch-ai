"""
手动清理/归档投递相关的本地存储：
- data/job_queue.json  ：删掉长期（>45 天）没在搜索里再出现、且还停在 queued/new 的条目
- data/applied_jobs.json：把一年前的投递记录挪进 applied_jobs.archive.json，活跃部分留最新 5000 条

看板接口 (/api/apply/dashboard) 本来每天会自动跑一次；这个脚本用于手动立即执行
（比如想马上瘦身，或后端没在跑）。

跑法：python -m scripts.compact_apply_stores
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.core import apply_dashboard

if __name__ == "__main__":
    res = apply_dashboard.compact(force=True)
    print("清理结果：", res)

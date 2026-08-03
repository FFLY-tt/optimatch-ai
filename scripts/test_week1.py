"""
第一周验收脚本。
跑通 HN 抓取 + Tavily 搜索（求职关键词 + 跨境关键词），
把结果统一存成 JSON 文件，供人工检查数据质量。

运行方式（在项目根目录下）：
    python scripts/test_week1.py
"""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.connectors.hn_connector import fetch_hn_jobs
from src.connectors.tavily_connector import search
from src.core.llm_client import chat, FILTER_MODEL

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    all_records = []

    print("正在测试 DeepSeek 连通性...")
    try:
        reply = chat("用一句话介绍你自己", model=FILTER_MODEL)
        print(f"  成功: {reply}")
    except Exception as e:
        print(f"  失败: {e}")

    print("\n正在抓取 HN Who is hiring...")
    try:
        hn_records = fetch_hn_jobs(limit=15)
        print(f"  成功，抓到 {len(hn_records)} 条")
        all_records.extend(hn_records)
    except Exception as e:
        print(f"  失败: {e}")

    print("\n正在测试 Tavily 求职关键词搜索...")
    try:
        job_records = search(
            query="site:reddit.com r/forhire remote AI engineer python",
            platform_type="job",
            max_results=5,
        )
        print(f"  成功，抓到 {len(job_records)} 条")
        all_records.extend(job_records)
    except Exception as e:
        print(f"  失败: {e}")

    print("\n正在测试 Tavily 跨境商机关键词搜索...")
    try:
        biz_records = search(
            query="looking for TikTok affiliate pet products",
            platform_type="business",
            max_results=5,
        )
        print(f"  成功，抓到 {len(biz_records)} 条")
        all_records.extend(biz_records)
    except Exception as e:
        print(f"  失败: {e}")

    output_path = os.path.join(OUTPUT_DIR, "week1_test_output.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump([r.to_dict() for r in all_records], f, ensure_ascii=False, indent=2)

    print(f"\n完成，共 {len(all_records)} 条记录，已存到 {output_path}")
    print("下一步：打开这个 JSON 文件，人工检查内容质量，尤其是跨境商机那部分搜索结果是否真的相关。")


if __name__ == "__main__":
    main()
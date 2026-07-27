"""
Tab A 完整流程一键测试脚本。
不用在 /docs 页面手动点，跑这一个脚本就把 4 步全走一遍，
每一步的结果会打印出来，方便你一眼看出哪一步有问题。

运行前提：后端已经在跑（另一个终端: uvicorn src.api:app --reload）

运行方式（项目根目录下）：
    python test_tab_a.py

【本次改动说明】
第 2 步现在会额外拿到 suggested_hashtags（LLM 推荐的 Instagram hashtag），
第 3 步把这批 hashtag 一起传给 /api/search-opportunities，触发 affiliate_kol
类别下的 Instagram 结构化数据抓取（用户名/粉丝数/邮箱）。
第 3 步的打印也加上了 email/followers 字段，方便直接看到 IG 抓取有没有生效。
"""

import requests

API_BASE = "http://127.0.0.1:8000"


def main():
    print("=" * 60)
    print("第 1 步：提交业务档案")
    resp = requests.post(f"{API_BASE}/api/setup-business-profile", json={
        "business_description": (
            "We are a Shenzhen-based factory manufacturing smart pet feeders and water "
            "dispensers. We sell to independent-site sellers and TikTok Shop sellers in "
            "the US and Europe."
        ),
        "target_customer": "TikTok pet product sellers and affiliates looking for reliable suppliers",
        "website_url": "",
    })
    print(resp.status_code, resp.json())

    print("\n" + "=" * 60)
    print("第 2 步：推荐线索类别 + Instagram hashtag")
    resp = requests.post(f"{API_BASE}/api/suggest-lead-categories", json={
        "business_description": (
            "We are a Shenzhen-based factory manufacturing smart pet feeders and water "
            "dispensers. We sell to independent-site sellers and TikTok Shop sellers in "
            "the US and Europe."
        ),
        "max_categories": 3,
    })
    print(resp.status_code)
    resp_data = resp.json()
    categories_data = resp_data["categories"]
    suggested_hashtags = resp_data.get("suggested_hashtags", [])

    for c in categories_data:
        mark = "✓" if c["suggested"] else " "
        print(f"  [{mark}] {c['id']} - {c['label']}")

    suggested_ids = [c["id"] for c in categories_data if c["suggested"]]
    print(f"\n将用推荐的类别继续测试: {suggested_ids}")
    print(f"推荐的 Instagram hashtag: {suggested_hashtags}")

    print("\n" + "=" * 60)
    print("第 3 步：搜索商机（可能需要 10-30 秒，请耐心等待）")
    resp = requests.post(f"{API_BASE}/api/search-opportunities", json={
        "categories": suggested_ids,
        "hashtags": suggested_hashtags,
        "max_results_per_category": 3,
    })
    print(resp.status_code)
    opp_data = resp.json()
    print(f"共找到 {opp_data['total']} 条商机:\n")
    for opp in opp_data["opportunities"]:
        extra_bits = []
        if opp.get("email"):
            extra_bits.append(f"email={opp['email']}")
        if opp.get("followers"):
            extra_bits.append(f"followers={opp['followers']}")
        extra = f" ({', '.join(extra_bits)})" if extra_bits else ""

        print(f"  [{opp['category']}] {opp['title']}{extra}")
        print(f"      {opp['url']}\n")

    if not opp_data["opportunities"]:
        print("没搜到结果，跳过第 4 步。")
        return

    print("=" * 60)
    print("第 4 步：用第一条商机生成开发信（可能需要 10-20 秒）")
    first_opp = opp_data["opportunities"][0]
    resp = requests.post(f"{API_BASE}/api/generate-outreach", json={
        "opportunity_content": first_opp["title"],
        "user_notes": "",
    })
    print(resp.status_code)
    outreach_data = resp.json()
    print(f"\n是否通过质量检查: {outreach_data['passed_review']} | 尝试次数: {outreach_data['attempts']}")
    if outreach_data["issue"] and outreach_data["issue"].lower() != "none":
        print(f"遗留问题: {outreach_data['issue']}")
    print("\n生成的开发信内容：\n")
    print(outreach_data["outreach_message"])


if __name__ == "__main__":
    main()
"""
"用户画像单一语言锁"功能的测试：
1. detect_language() 的边界案例（纯中文/纯英文/中文夹技术词/真混合/极短文本）
2. 集成测试：真实 HTTP 走一遍"上传中文简历(锁定zh) -> 上传英文简历(应400)
   -> 提交中文补充文本(应成功)"

跑法：
    python -m scripts.test_language_lock          # 只跑单元测试（不需要起服务）
    python -m scripts.test_language_lock --http    # 额外跑集成测试（需要 uvicorn 已启动）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_detect_language_unit_cases():
    from src.core.text_utils import detect_language

    print("=" * 60)
    print("单元测试：detect_language() 边界案例")

    cases = [
        ("纯中文简历文本",
         "我毕业于北京大学软件工程专业，拥有三年后端开发经验，"
         "曾在多家互联网公司负责分布式系统的设计与实现，擅长解决高并发场景下的性能瓶颈问题。"),
        ("纯英文简历文本",
         "I graduated from the University of Waterloo with a degree in Computer Science "
         "and have three years of backend development experience, specializing in distributed systems."),
        ("中文夹杂英文技术名词/公司名",
         "我在字节跳动负责 Python 后端开发，使用 PyTorch 训练模型，同时熟悉 AWS 和 Kubernetes 部署。"),
        ("极短文本",
         "ABC 公司"),
    ]

    for label, text in cases:
        result = detect_language(text)
        print(f"  [{label}] -> {result!r}")
        print(f"    原文: {text}")

    # 前两个断言严格要求（明确写在需求里的判定标准）
    assert detect_language(cases[0][1]) == "zh", "纯中文应该判为 zh"
    assert detect_language(cases[1][1]) == "en", "纯英文应该判为 en"
    assert detect_language(cases[2][1]) == "zh", "中文夹技术词不应该被误判成不一致"
    assert detect_language(cases[3][1]) == "unknown", "极短文本应该判为 unknown"
    print("  [PASS] 前 4 类边界案例符合预期")

    # 第 5 类：真正的大段中英混合文本——如实记录检测函数的实际结果，
    # 不为了让某个特定结果"看起来对"而调整阈值。
    mixed_text = (
        "我在字节跳动担任高级后端工程师，负责分布式系统架构设计与性能优化工作，"
        "参与了多个核心交易系统的重构项目，积累了丰富的高并发场景处理经验。"
        "I also worked as a research assistant at Ontario Tech University, "
        "focusing on applying machine learning techniques to real-time healthcare "
        "monitoring systems, and published two papers on federated learning."
    )
    cjk_count = len(__import__("re").findall(r"[一-鿿]", mixed_text))
    latin_count = len(__import__("re").findall(r"[A-Za-z]", mixed_text))
    mixed_result = detect_language(mixed_text)
    print(f"\n  [真实中英混合文本] CJK字符数={cjk_count}, 拉丁字母数={latin_count}, "
          f"中文占比={cjk_count/(cjk_count+latin_count):.2%}")
    print(f"    detect_language() 实际返回: {mixed_result!r}")
    print(f"    原文: {mixed_text}")


def test_http_integration():
    import requests

    base = "http://127.0.0.1:8000"
    print("=" * 60)
    print("集成测试：真实 HTTP，上传中文简历 -> 上传英文简历(应400) -> 提交中文补充文本")

    zh_resume_text = (
        "# 张伟\n\n## 个人简介\n"
        "拥有五年后端开发经验，专注于分布式系统与微服务架构设计。\n\n"
        "## 工作经历\n"
        "**腾讯（深圳）** | 高级后端工程师 | 2021年1月至今\n"
        "- 主导了核心支付系统的架构升级\n"
        "- 优化了数据库查询性能，将平均响应时间降低了30%\n"
    )
    en_resume_text = (
        "# David Chen\n\n## Summary\n"
        "Backend engineer with five years of experience in distributed systems.\n\n"
        "## Experience\n"
        "**Google | Senior Backend Engineer | Jan 2021 - Present**\n"
        "- Led the architecture upgrade of the core payment system\n"
        "- Optimized database query performance, reducing average latency by 30%\n"
    )

    tmp_dir = os.path.join(os.path.dirname(__file__), "..", "data", "_lang_test_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    zh_path = os.path.join(tmp_dir, "zh_resume.md")
    en_path = os.path.join(tmp_dir, "en_resume.md")
    with open(zh_path, "w", encoding="utf-8") as f:
        f.write(zh_resume_text)
    with open(en_path, "w", encoding="utf-8") as f:
        f.write(en_resume_text)

    print("\n[1/3] 上传中文简历（预期：成功，锁定画像语言为 zh）")
    with open(zh_path, "rb") as f:
        resp1 = requests.post(f"{base}/api/upload-resume", files={"file": ("zh_resume.md", f, "text/markdown")})
    print(f"  status={resp1.status_code}  body={resp1.text}")
    assert resp1.status_code == 200, "第一次上传（画像还没锁定语言）应该直接成功"

    print("\n[2/3] 上传英文简历（预期：400，画像已锁定为 zh）")
    with open(en_path, "rb") as f:
        resp2 = requests.post(f"{base}/api/upload-resume", files={"file": ("en_resume.md", f, "text/markdown")})
    print(f"  status={resp2.status_code}  body={resp2.text}")
    assert resp2.status_code == 400, "画像已锁定为 zh 时上传英文简历应该被拒绝(400)"

    print("\n[3/3] 提交中文补充文本（预期：成功，和已锁定的 zh 一致）")
    resp3 = requests.post(
        f"{base}/api/add-resume-note",
        json={"note_text": "另外我还熟悉 Redis 和消息队列的使用，做过一些性能调优的相关工作。"},
    )
    print(f"  status={resp3.status_code}  body={resp3.text}")
    assert resp3.status_code == 200, "和已锁定语言一致的补充文本应该成功"

    print("\n  [PASS] 集成测试全部符合预期")


if __name__ == "__main__":
    test_detect_language_unit_cases()
    if "--http" in sys.argv:
        test_http_integration()
    print("\n全部测试跑完。")

"""
针对本次简历改造过程中"顺手修复"的几个 bug 的最小回归测试。
这几个 bug 都不是这次改造的核心逻辑，但足够隐蔽/致命（会让整个 app 起不来，
或者让某个接口一调就崩），值得各自钉一个断言，避免以后重构时又踩回去。

跑法：python -m scripts.test_bugfixes_regression
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_main_app_imports_cleanly():
    """
    Bug: main.py 挂载的 tab_a_router / tab_b_router，以及它们依赖的
    business_profile.py / outreach_generator.py，有 4 处导入路径还是
    重构前的旧路径（src.resume_parser / src.chunker / src.vector_store /
    src.retriever / src.llm_client 这种），整个 app 直接 import 不了。
    """
    from src.main import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200, f"预期 200，实际 {resp.status_code}"
    assert resp.json() == {"status": "ok"}

    # 顺带确认 Tab A / Tab B 的接口都真的注册上了（这是本次改动新加的
    # OpenAPI 检查，之前那次误判"路由没注册"就是没用这种方式验证）
    openapi_paths = set(client.get("/openapi.json").json()["paths"].keys())
    expected = {
        "/api/upload-resume", "/api/add-resume-note", "/api/tailor-resume",
        "/api/search-jobs", "/api/setup-business-profile",
    }
    missing = expected - openapi_paths
    assert not missing, f"这些路由没有被正确注册: {missing}"
    print("[PASS] test_main_app_imports_cleanly")


def test_word_export_dir_resolves_to_project_root():
    """
    Bug: word_export.py 的 EXPORT_DIR 只拼了一层 ".."，从
    src/tab_b_jobsearch/word_export.py 出发只够走到 src/，实际落盘在
    src/data/exports/，和 router.py 里 /files/{filename} 下载接口预期的
    项目根目录 data/exports/ 对不上，下载会 404。
    """
    from src.tab_b_jobsearch.word_export import EXPORT_DIR

    resolved = os.path.normpath(os.path.abspath(EXPORT_DIR))
    project_root = os.path.normpath(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    expected = os.path.normpath(os.path.join(project_root, "data", "exports"))

    assert resolved == expected, f"EXPORT_DIR 解析成了 {resolved}，应该是 {expected}"
    # 顺带确认没有回退到那个错误的 src/data/exports 路径
    assert "src" + os.sep + "data" not in resolved
    print("[PASS] test_word_export_dir_resolves_to_project_root")


def test_generate_tailored_resume_accepts_extra_context():
    """
    Bug: router.py 里 /api/tailor-resume 会传 user_notes 给
    generate_tailored_resume()，但这个函数当时根本没有接收这个参数的
    形参，一调用就是 TypeError: unexpected keyword argument。
    这里不真的调用 LLM（不需要为了回归测试消耗 API 额度/等真实网络），
    只用 inspect 确认函数签名接受 extra_context 这个关键字参数。
    """
    import inspect
    from src.tab_b_jobsearch.resume_generator import generate_tailored_resume

    sig = inspect.signature(generate_tailored_resume)
    assert "extra_context" in sig.parameters, (
        "generate_tailored_resume() 缺少 extra_context 参数，"
        "router.py 的 /api/tailor-resume 传这个参数会直接 TypeError"
    )
    print("[PASS] test_generate_tailored_resume_accepts_extra_context")


def test_degree_extraction_does_not_false_positive_on_substring():
    """
    Bug: extract_degree_level() 最初用裸子串 `in` 判断关键词，"ms"
    （硕士）会命中 "syste**ms**" 这类单词内部的字母组合，导致明明是
    "Bachelor's degree required" 的 JD 被误判成硕士门槛。
    """
    from src.core.keyword_dicts import extract_degree_level

    # 明确写着本科，但正文里出现了容易误命中 "ms" 的词（systems/programs）
    text = "Bachelor's degree required. Experience with distributed systems and internship programs."
    level = extract_degree_level(text)
    assert level == 2, f"预期抠出本科(2)，实际抠到 {level}（如果是 3，说明 'ms' 子串误判的老问题又回来了）"

    # 正常场景：真的提到硕士，应该抠到 3
    assert extract_degree_level("Master's degree in Computer Science") == 3
    print("[PASS] test_degree_extraction_does_not_false_positive_on_substring")


if __name__ == "__main__":
    test_main_app_imports_cleanly()
    test_word_export_dir_resolves_to_project_root()
    test_generate_tailored_resume_accepts_extra_context()
    test_degree_extraction_does_not_false_positive_on_substring()
    print("\n全部回归测试通过。")

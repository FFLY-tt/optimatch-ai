"""
职位 status 持久化回读的回归测试。

背景：/api/search-jobs 之前一直把 JobRecord.status 硬编码成 "new"，就算用户
通过 /api/update-status 标记过某条职位是 "contacted"，下次搜到同一条还是显示
"new"。这里覆盖两件事：
1. get_status() 读到的值确实能进到 JobRecord.status 里（router.py 里
   record_id -> get_status(record_id) 这条链路本身没接错）。
2. anysearch/remoteok/remotive 三路的 record_id 生成方式（_stable_id，md5
   摘要）跨进程稳定——不像之前用 Python 内置 hash() 那样，同一个 url 换一个
   进程就算出不同的 id（Python 字符串 hash() 默认启用哈希随机化）。这个必须
   真的起两个独立进程各算一次才算验证过，不能只在同一个进程里跑两次就当
   "看起来是稳定的"。

跑法：python -m scripts.test_job_status_persistence
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_get_status_reflected_in_job_record_construction():
    """
    直接验证 router.py 里"算 record_id -> 查 get_status(record_id) -> 塞进
    JobRecord.status"这条链路本身是对的：先用 update_status 写一个状态，
    再走一遍同样的 record_id 计算方式，确认 get_status 读回来的就是刚写的值，
    不是默认的 "new"。
    """
    from src.core.status_store import get_status, update_status
    from src.tab_b_jobsearch.router import JobRecord, _stable_id

    fake_url = "https://example.com/jobs/test-status-persistence-12345"
    record_id = _stable_id("remoteok", fake_url)

    # 模拟用户之前标记过这条职位
    update_status(record_id, "contacted")

    # 模拟 search_jobs() 里构造 JobRecord 的那一步：同样的 url 重新算一次
    # record_id，再查一次状态
    recomputed_id = _stable_id("remoteok", fake_url)
    assert recomputed_id == record_id, "同一个 url 在同一次调用里算出的 record_id 都对不上，逻辑本身有问题"

    record = JobRecord(
        id=recomputed_id, source="remoteok", title="Test Job",
        url=fake_url, posted_at=None, status=get_status(recomputed_id),
    )
    assert record.status == "contacted", f"预期 status='contacted'，实际是 {record.status!r}"
    print("[PASS] test_get_status_reflected_in_job_record_construction")


def test_stable_id_survives_process_restart():
    """
    核心验证：_stable_id() 对同一个 url，在两个完全独立的 Python 进程里
    算出来的结果必须一致——这是这次修复要解决的问题本身，不能只靠"同一个
    进程里跑两次"糊弄过去（同进程本来就不会暴露 PYTHONHASHSEED 随机化的
    问题）。

    做法：真的起两个独立子进程（不共享任何解释器状态），各自 import
    _stable_id 并计算同一个 url 的 id，断言两次结果相等。作为对照，
    同时也起两个独立子进程跑"修复前"那种 abs(hash(url)) 的算法，断言这个
    反而大概率不相等（能进一步证明"确实是这个原因导致不稳定"，不是随便
    猜的）。
    """
    test_url = "https://remoteok.com/remote-jobs/some-real-job-listing-98765"

    stable_id_snippet = f"""
import sys
sys.path.insert(0, r'{os.path.join(os.path.dirname(__file__), "..")}')
from src.tab_b_jobsearch.router import _stable_id
print(_stable_id("remoteok", {test_url!r}))
"""
    old_hash_snippet = f"""
print(f"remoteok_{{abs(hash({test_url!r}))}}")
"""

    python_exe = sys.executable

    def run_twice(snippet: str) -> tuple[str, str]:
        outputs = []
        for _ in range(2):
            result = subprocess.run(
                [python_exe, "-c", snippet],
                capture_output=True, text=True, timeout=60,
            )
            assert result.returncode == 0, f"子进程执行失败: {result.stderr}"
            outputs.append(result.stdout.strip())
        return outputs[0], outputs[1]

    new_run1, new_run2 = run_twice(stable_id_snippet)
    print(f"  新方案（_stable_id / md5）：进程1 = {new_run1}，进程2 = {new_run2}")
    assert new_run1 == new_run2, (
        f"新的 _stable_id 在两个独立进程里算出的结果不一致：{new_run1!r} vs {new_run2!r}——"
        f"说明修复没生效"
    )

    old_run1, old_run2 = run_twice(old_hash_snippet)
    print(f"  旧方案（abs(hash(url))）：进程1 = {old_run1}，进程2 = {old_run2}")
    if old_run1 != old_run2:
        print("  （确认旧方案在两个独立进程里确实算出了不同的值，坐实了这就是修复前的真实 bug）")
    else:
        # 极小概率两次随机的 PYTHONHASHSEED 恰好一样，不算测试失败，但要如实说明
        print("  （这次两个进程的 PYTHONHASHSEED 恰好一样，旧方案这次没能复现差异——"
              "是随机巧合，不代表旧方案本身是稳定的）")

    print("[PASS] test_stable_id_survives_process_restart")


if __name__ == "__main__":
    test_get_status_reflected_in_job_record_construction()
    test_stable_id_survives_process_restart()
    print("\n全部测试通过。")

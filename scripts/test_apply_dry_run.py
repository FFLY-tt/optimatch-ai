"""
自动投递模块的手动验证脚本。
CLAUDE.md 要求每次改完代码都要在真实终端里跑一遍验证——这个脚本就是干这个的：
真的打开一个浏览器、真的走一遍"打开职位页 -> 填表 -> 停在提交前"的完整流程，
把每个字段填了什么打印出来，最后问你要不要真的点提交。

跑之前:
1. pip install -r requirements.txt          (装 playwright)
2. python -m playwright install chromium    (装浏览器内核，只需要装一次)
3. 复制 data/applicant_profile.example.json 成 data/applicant_profile.json，填好自己的信息
4. 如果要测 LinkedIn/Indeed：第一次跑这个脚本时会弹出一个真实浏览器窗口，
   如果发现停在登录页，就在那个窗口里手动登录一下——登录状态会保存在
   data/apply_browser_profile/ 下，之后不用重复登录。

用法:
    python -m scripts.test_apply_dry_run <job_url> [job_description_file]
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.tab_b_jobsearch.apply.orchestrator import start_application, confirm_submit, cancel, ApplyError
from src.tab_b_jobsearch.apply import browser


def main():
    if len(sys.argv) < 2:
        print("用法: python -m scripts.test_apply_dry_run <job_url> [job_description_file]")
        sys.exit(1)

    job_url = sys.argv[1]
    job_description = ""
    if len(sys.argv) >= 3 and os.path.exists(sys.argv[2]):
        with open(sys.argv[2], "r", encoding="utf-8") as f:
            job_description = f.read()

    print(f"[1/3] 打开职位页并尝试自动填表: {job_url}")
    try:
        draft = start_application(job_id="dry_run_test", job_url=job_url, job_description=job_description)
    except ApplyError as e:
        print(f"失败: {e}")
        sys.exit(1)

    print(f"\n平台识别: {draft.platform}")
    print(f"截图: {draft.screenshot_path}")
    print(f"是否找到最终提交按钮: {draft.ready_to_submit}")
    print("\n填写报告:")
    for f in draft.filled_fields:
        print(f"  [{f.source:16s}] {f.label!r} -> {f.value!r}")
    if draft.warnings:
        print("\n警告:")
        for w in draft.warnings:
            print(f"  - {w}")

    print("\n[2/3] 去截图里看一眼，或者直接看弹出来的浏览器窗口，检查填得对不对。")
    answer = input("[3/3] 确认要真的点提交吗？输入 yes 提交，其他任意输入都会取消不提交: ").strip().lower()

    if answer == "yes":
        result = confirm_submit(draft.session_id)
        print(f"已提交: {result}")
    else:
        cancel(draft.session_id)
        print("已取消，没有提交任何内容。")

    browser.shutdown()


if __name__ == "__main__":
    main()

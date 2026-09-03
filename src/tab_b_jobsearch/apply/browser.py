"""
持久化浏览器上下文。
LinkedIn / Indeed 这类需要登录态的网站，不能每次投递都重新登录一遍——
用 playwright 的 launch_persistent_context 指定一个固定的用户数据目录，
第一次手动登录一次，Cookie/会话会保存在这个目录下，之后一直复用。

跟直接用 Chrome DevTools Protocol 挂到用户日常用的 Chrome 相比，这种方式
更简单、更稳定（不依赖用户手动开 --remote-debugging-port），代价是
需要单独登录一次这个专用 profile，跟日常浏览器的登录态是分开的。
"""

import os
import threading
from playwright.sync_api import sync_playwright

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data")
BROWSER_PROFILE_DIR = os.path.join(DATA_DIR, "apply_browser_profile")

_playwright = None
_context = None
_lock = threading.Lock()


def get_context(headless: bool = False):
    """
    返回一个全进程共用的持久化浏览器上下文。
    headless=False（默认）：第一次跑之前建议先手动登录 LinkedIn/Indeed，
    所以默认开着看得见的窗口；确认登录态都存好了之后，可以自己改成 True。
    """
    global _playwright, _context
    with _lock:
        if _context is not None:
            return _context
        os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)
        _playwright = sync_playwright().start()
        _context = _playwright.chromium.launch_persistent_context(
            BROWSER_PROFILE_DIR,
            headless=headless,
            viewport={"width": 1400, "height": 1000},
            args=["--disable-blink-features=AutomationControlled"],
        )
        return _context


def new_page():
    context = get_context()
    return context.new_page()


def shutdown():
    """进程退出时清理，正常不需要手动调，留着方便测试脚本用。"""
    global _playwright, _context
    with _lock:
        if _context is not None:
            _context.close()
            _context = None
        if _playwright is not None:
            _playwright.stop()
            _playwright = None

"""
自动投递用的浏览器上下文管理。

默认走 launch_persistent_context（固定的用户数据目录）——LinkedIn / Indeed 这类
需要登录态的站点，第一次手动登录一次，Cookie 存在这个目录下之后一直复用。

持久化目录很容易被弄坏（进程被 kill、上一次没退干净留下 SingletonLock、
两个 Chromium 抢同一个 user-data-dir……），弄坏之后 launch_persistent_context
会一直抛 TargetClosedError。所以这里做了三层兜底：
1. 缓存的 context 已经死了（浏览器被关了）——探测到就丢弃重建
2. 启动失败——清掉残留的 Singleton* 锁文件、杀掉占用该目录的 Chromium，重试一次
3. 还是不行——退化成非持久化 context（丢掉登录态，但通用 ATS 投递不受影响），
   最坏情况抛一个说得清楚的错误

另外：Playwright 同步 API 的对象绑定在"创建它的那个线程"上，不能被 FastAPI
线程池里的其它线程碰。run_in_browser_thread() 把所有浏览器操作固定到一个
专用单线程里跑，既解决跨线程问题，也顺带把"同一时刻只跑一单投递"串行化了
（本来就只有一个有头浏览器窗口）。
"""

import glob
import logging
import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout

from playwright.sync_api import sync_playwright

log = logging.getLogger("optimatch.apply.browser")

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data")
BROWSER_PROFILE_DIR = os.path.join(DATA_DIR, "apply_browser_profile")

_playwright = None
_context = None
_persistent = True
_lock = threading.RLock()

# 所有 Playwright 操作固定在这个单线程里执行（见模块 docstring）
_browser_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="apply-browser")


def run_in_browser_thread(fn, *args, _timeout: float = 180.0, **kwargs):
    """
    把一个可调用对象丢到专用浏览器线程里执行并等结果（异常原样抛回来）。
    _timeout 秒还没返回，就当浏览器线程卡死了：强拆浏览器 context（能让卡住的
    Playwright 调用抛 TargetClosedError 从而解套），并抛 TimeoutError 给调用方。
    """
    fut = _browser_executor.submit(fn, *args, **kwargs)
    try:
        return fut.result(timeout=_timeout)
    except FuturesTimeout:
        log.error("浏览器线程执行超过 %.0fs 没返回，强拆 context 尝试解套", _timeout)
        _force_teardown()
        raise TimeoutError(
            f"自动投递超时（浏览器操作 {int(_timeout)}s 内没有完成），已重置浏览器，请重试。"
        )


def _context_is_alive(ctx) -> bool:
    """真探活：开一个空白页再关掉。ctx.pages 只读缓存，浏览器已经死了也不会抛。"""
    try:
        pg = ctx.new_page()
        pg.close()
        return True
    except Exception:
        return False


def _cleanup_profile_locks() -> None:
    """删掉持久化目录里残留的 Singleton 锁 + 杀掉还占着这个目录的 Chromium 进程。"""
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = os.path.join(BROWSER_PROFILE_DIR, name)
        try:
            if os.path.islink(p) or os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    try:
        subprocess.run(
            ["pkill", "-9", "-f", f"user-data-dir={BROWSER_PROFILE_DIR}"],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def _launch_persistent():
    os.makedirs(BROWSER_PROFILE_DIR, exist_ok=True)
    return _playwright.chromium.launch_persistent_context(
        BROWSER_PROFILE_DIR,
        headless=_HEADLESS,
        viewport={"width": 1400, "height": 1000},
        args=["--disable-blink-features=AutomationControlled"],
    )


def _launch_ephemeral():
    browser = _playwright.chromium.launch(
        headless=_HEADLESS, args=["--disable-blink-features=AutomationControlled"]
    )
    return browser.new_context(viewport={"width": 1400, "height": 1000})


_HEADLESS = False


def get_context(headless: bool = False):
    """
    返回全进程共用的浏览器上下文。任何情况下要么返回一个能用的 context，
    要么抛一个 RuntimeError（说清楚原因），绝不返回半死不活的对象。
    """
    global _playwright, _context, _persistent, _HEADLESS
    _HEADLESS = headless
    with _lock:
        if _context is not None and _context_is_alive(_context):
            return _context
        if _context is not None:
            log.warning("缓存的浏览器 context 已失效，重建")
            try:
                _context.close()
            except Exception:
                pass
            _context = None

        if _playwright is None:
            _playwright = sync_playwright().start()

        # 1) 正常启动持久化 context
        try:
            _context = _launch_persistent()
            _persistent = True
            return _context
        except Exception as e:
            log.warning("持久化浏览器启动失败（%s），清理锁文件 + 重启 playwright 后重试", e)

        # 2) 清理锁 / 残留进程 + 重启 playwright driver 后重试一次
        _cleanup_profile_locks()
        try:
            _playwright.stop()
        except Exception:
            pass
        _playwright = sync_playwright().start()
        try:
            _context = _launch_persistent()
            _persistent = True
            log.info("清理后重试：持久化浏览器启动成功")
            return _context
        except Exception as e:
            log.warning("重试仍失败（%s），尝试重建持久化目录", e)

        # 3) 把持久化目录整个搬走重建，再试一次
        try:
            if os.path.isdir(BROWSER_PROFILE_DIR):
                shutil.move(BROWSER_PROFILE_DIR, BROWSER_PROFILE_DIR + ".broken")
                _prune_broken_profiles()
            _context = _launch_persistent()
            _persistent = True
            log.info("重建持久化目录后启动成功（旧目录已备份为 .broken，LinkedIn/Indeed 需要重新登录）")
            return _context
        except Exception as e:
            log.warning("重建持久化目录仍失败（%s），退化为非持久化浏览器", e)

        # 4) 退化：非持久化 context（通用 ATS 投递不受影响，只是没有 LinkedIn/Indeed 登录态）
        try:
            _context = _launch_ephemeral()
            _persistent = False
            return _context
        except Exception as e:
            _context = None
            raise RuntimeError(f"浏览器启动失败，自动投递不可用：{e}") from e


def _prune_broken_profiles(keep: int = 2) -> None:
    broken = sorted(glob.glob(BROWSER_PROFILE_DIR + ".broken*"), key=os.path.getmtime)
    for path in broken[:-keep] if len(broken) > keep else []:
        shutil.rmtree(path, ignore_errors=True)


def _force_teardown() -> None:
    """从任意线程强拆浏览器——卡死的 Playwright 调用会因此抛错解套，下次 get_context 重建。"""
    global _context, _playwright
    ctx, pw = _context, _playwright
    _context, _playwright = None, None
    for closer in (lambda: ctx and ctx.close(), lambda: pw and pw.stop()):
        try:
            closer()
        except Exception:
            pass
    _cleanup_profile_locks()


def new_page():
    return get_context().new_page()


def is_persistent() -> bool:
    return _persistent


def shutdown():
    """进程退出时清理，正常不需要手动调，留着方便测试脚本用。"""
    global _playwright, _context
    with _lock:
        if _context is not None:
            try:
                _context.close()
            except Exception:
                pass
            _context = None
        if _playwright is not None:
            try:
                _playwright.stop()
            except Exception:
                pass
            _playwright = None

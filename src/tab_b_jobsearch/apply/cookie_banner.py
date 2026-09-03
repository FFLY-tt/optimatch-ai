"""
通用 cookie / 隐私同意横幅关闭。

几乎每个网站首次打开都会弹一个 cookie 同意栏——挡在页面最上面或者固定在底部，
会遮住表单元素导致点击/勾选失败（Veeva 那个 "relocate to Pleasanton" radio
点不中就是被这种横幅挡住的）。这个问题几乎每个 ATS 都会遇到，统一在
adapter 打开职位页之后、开始填表之前关一次。

原则：横幅不存在是正常情况，任何一步找不到/点不了都静默跳过，绝不抛异常。
"""

import re

from playwright.sync_api import Page

# 主流 cookie 同意组件的"全部接受 / 关闭"按钮的已知选择器（按出现频率大致排序）。
_KNOWN_ACCEPT_SELECTORS = [
    "#onetrust-accept-btn-handler",                                  # OneTrust
    "#CybotCookiebotDialogBodyButtonAccept",                         # Cookiebot
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",        # Cookiebot（分级版）
    "#truste-consent-button",                                        # TrustArc
    ".osano-cm-accept-all",                                          # Osano
    "#didomi-notice-agree-button",                                   # Didomi
    ".cc-window .cc-dismiss",                                        # cookieconsent（Veeva/Lever 用的这个，按钮写 "Dismiss"）
    ".cc-window .cc-allow",                                          # cookieconsent（"Allow cookies" 变体）
    ".cookieconsent-optin-allow",                                    # cookieconsent 另一皮肤
    "button#accept-cookies",
    "button#cookie-accept",
    ".cookie-consent-accept",
]

# 兜底：按钮/链接的可见文字精确匹配这些（大小写不敏感）才考虑点。
# "dismiss" 也算——cookieconsent 那种横幅的按钮就写这个；已经用"祖先是固定/粘性定位横幅"
# 卡过一道，不会误点到表单里的普通按钮。
_GENERIC_ACCEPT_TEXT = re.compile(
    r"^(accept all|accept all cookies|accept cookies|allow all cookies|allow all|allow cookies|"
    r"i accept|got it|agree|i agree|dismiss)$",
    re.IGNORECASE,
)


def _looks_like_fixed_banner(locator) -> bool:
    """
    这个按钮所在的祖先容器是不是"固定定位的横幅"——position: fixed / sticky。
    用来避免误点到表单里刚好文字对上的按钮（比如某个 "Agree" 单选项的确认钮）。
    """
    try:
        return bool(locator.evaluate(
            """(el) => {
                let node = el;
                for (let i = 0; i < 6 && node; i++) {
                    const pos = getComputedStyle(node).position;
                    if (pos === 'fixed' || pos === 'sticky') return true;
                    node = node.parentElement;
                }
                return false;
            }"""
        ))
    except Exception:
        return False


def dismiss_cookie_banners(page: Page) -> None:
    """尽力关掉页面上的 cookie / 隐私同意横幅。找不到就跳过，不抛异常。"""
    clicked = False

    # 1) 已知组件的接受按钮
    for selector in _KNOWN_ACCEPT_SELECTORS:
        try:
            btn = page.locator(selector).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=3000)
                clicked = True
                break
        except Exception:
            continue

    # 2) 兜底：文字精确匹配 + 所在容器是固定/粘性定位的横幅
    if not clicked:
        try:
            candidates = page.get_by_role("button", name=_GENERIC_ACCEPT_TEXT)
            n = min(candidates.count(), 8)
        except Exception:
            n = 0
        for i in range(n):
            try:
                btn = candidates.nth(i)
                if not btn.is_visible():
                    continue
                if not _looks_like_fixed_banner(btn):
                    continue
                btn.click(timeout=3000)
                clicked = True
                break
            except Exception:
                continue

    # 3) 兜底的兜底：有些站把"接受"做成 <a>，再扫一遍链接
    if not clicked:
        try:
            links = page.get_by_role("link", name=_GENERIC_ACCEPT_TEXT)
            n = min(links.count(), 8)
        except Exception:
            n = 0
        for i in range(n):
            try:
                lk = links.nth(i)
                if lk.is_visible() and _looks_like_fixed_banner(lk):
                    lk.click(timeout=3000)
                    clicked = True
                    break
            except Exception:
                continue

    if clicked:
        try:
            page.wait_for_timeout(500)
        except Exception:
            pass

"""共享浏览器：分角色隔离的浏览器档案。

- role="collector"：采集专用匿名档案，机器化访问随便折腾，不带登录态。
- role="sender"：发私信/登录专用档案，只有有头、低频、像人的操作才碰它。
- 两个角色使用不同档案目录，可并行运行，无全局锁。
"""
import os

from playwright.sync_api import sync_playwright

from . import config

PROFILE_DIRS = {
    "collector": os.getenv(
        "COLLECTOR_PROFILE_DIR",
        os.path.join(config.BASE_DIR, "data", "browser-profile")),
    "sender": os.getenv(
        "SENDER_PROFILE_DIR",
        os.path.join(config.BASE_DIR, "data", "sender-profile")),
    "xhs_collector": os.getenv(
        "XHS_COLLECTOR_PROFILE_DIR",
        os.path.join(config.BASE_DIR, "data", "xhs-collector-profile")),
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class BrowserSession:
    """一个持久化上下文（含登录态）的 Playwright 会话。"""

    def __init__(self, headless=True, role="collector"):
        self._dir = PROFILE_DIRS.get(role, PROFILE_DIRS["collector"])
        os.makedirs(self._dir, exist_ok=True)
        self._headless = headless
        self._pw = None
        self.context = None
        try:
            self._pw = sync_playwright().start()
            self.context = self._pw.chromium.launch_persistent_context(
                self._dir,
                headless=headless,
                user_agent=UA,
                viewport={"width": 1280, "height": 800},
                args=["--disable-blink-features=AutomationControlled"],
                locale="zh-CN",
            )
        except Exception:
            self.close()
            raise

    def new_page(self):
        return self.context.new_page()

    def close(self):
        try:
            if self.context:
                self.context.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

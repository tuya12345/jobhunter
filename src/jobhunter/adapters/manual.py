"""`ManualAdapter` —— 人给链接，工具抓详情（**P2 主路径**）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 为什么主路径是"人给链接"而不是自动搜索（这是本项目最重要的架构取舍之一）    ║
╚══════════════════════════════════════════════════════════════════════════╝

最初的设计是让脚本自动搜索+翻页。评估后改了，因为：

    · **技术风险**：BOSS 直聘有 `__zp_stoken__` 签名 + 字体反爬（把数字渲染成自定义字体），
      自动翻页大概率中期撞墙，而它是 MVP 的**唯一入口、没有 fallback**；
    · **合规风险**：批量抓取比"打开我指定的一个页面"更容易触碰平台协议；
    · **工作流其实不匹配**：真实场景是"我刷到感兴趣的岗位 → 想存下来打分"，
      而不是"让机器人替我逛网站"。

改成"人给链接"之后：风险降一个数量级、工期砍半、而且更贴合真实使用方式。
自动搜索降级成**可选适配器**，且必须先通过 `spikes/boss_search_spike.py` 验证。

★ 本模块的合规边界（写在这里因为这里是唯一能"接触外部世界"的地方）

    1. **只读**：只 `goto` 页面并读取内容。**绝不**点击"立即沟通/投递"、
       绝不点赞/关注/私信，不做任何写操作。
    2. **不绕风控**：不处理验证码、不伪造签名、不模拟登录态；
       遇到验证页/登录墙就**如实失败**，让用户改用 `--manual` 粘贴文本。
    3. **频率克制**：每次抓取前有随机延时；批量导入时逐个抓、失败即跳过并记录。
    4. **失败可降级**：抓不到就返回 `None`，上层提示"请用手动粘贴"，
       绝不让整条链路断掉（网络不通也要能用 —— 这就是 `--manual` 存在的理由）。
"""

from __future__ import annotations

import contextlib
import logging
import random
import time
from pathlib import Path
from typing import Any

from jobhunter.adapters.base import JobDetail, JobSource
from jobhunter.adapters.parse_html import (
    detect_platform,
    extract_platform_job_id,
    parse_detail_from_html,
)
from jobhunter.config.settings import AppSettings, get_settings

logger = logging.getLogger(__name__)

__all__ = ["ManualAdapter", "html_to_detail", "is_playwright_available", "read_html_file"]

#: Word/Office 用的命名空间常量（这里只为了保持与其它模块一致的写法）
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: 抓取时**屏蔽**的资源类型：图片/媒体/字体对"读文字"毫无用处，
#: 屏蔽后页面加载快得多、流量也小（对目标站点更友好，这也是一种克制）。
_BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}

#: 页面加载策略：`domcontentloaded` 比 `load` 快得多，
#: 而招聘页面的 JD 正文在 DOM 里就有（不需要等图片加载完）。
_WAIT_UNTIL = "domcontentloaded"

#: 默认超时（毫秒）。给得宽松一些：招聘站点本身不快，超时太短会误判成"抓不到"。
_DEFAULT_TIMEOUT_MS = 30_000


def is_playwright_available() -> bool:
    """是否装了 `[browser]` extra。

    用途：没装时给出**可操作的安装指引**，而不是让 ImportError 栈糊在用户脸上。
    """
    try:  # pragma: no cover - 依赖环境
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def html_to_detail(html: str, url: str, *, platform: str | None = None) -> JobDetail:
    """把 HTML 文本转成 `JobDetail`（**纯函数**，测试与离线重放都走它）。

    单独暴露成函数的价值：抓取到的 HTML 可以存下来，
    将来解析规则改了可以**离线重放**，不必重新访问目标站点
    （省时间，也少给对方添麻烦）。
    """
    return parse_detail_from_html(html, url, platform=platform)


def read_html_file(path: str | Path, url: str | None = None) -> JobDetail:
    """从本地 HTML 文件解析岗位（用于测试、离线重放、以及"另存为网页"的兜底）。

    ★ 这条路是**不需要浏览器**的：抓不到/不想抓时，用户可以把页面另存为 HTML
      再走这里。它同时是 mock HTML fixture 测试的入口。
    """
    from jobhunter.utils.io import read_text

    target = Path(path)
    html = read_text(target)
    resolved_url = url or f"file:///{target.as_posix()}"
    return html_to_detail(html, resolved_url)


class ManualAdapter(JobSource):
    """★ P2 主路径：打开用户给的 URL，抓取岗位详情。"""

    platform = "manual"

    def __init__(self, settings: AppSettings | None = None) -> None:
        self.settings = settings or get_settings()

    # -- JobSource 接口 --------------------------------------------------
    def fetch_detail(self, url: str) -> JobDetail | None:
        """抓取单个岗位详情。

        Returns:
            `JobDetail`（成功；字段可能不全，`fetch_status` 会如实标注）
            `None`（彻底失败：没装浏览器 / 超时 / 被拦 / 页面没内容）
                 → 上层据此提示用户改用 `jh job add --manual`

        ★ 本方法**不向上抛异常**：抓取失败是预期内的正常情况，
          用返回值表达比用异常更清楚，也逼着调用方处理降级路径（见 base.py 的约定）。
        """
        if not url:
            logger.warning("fetch_detail 收到空 URL")
            return None

        if not is_playwright_available():
            logger.error(
                "没装 Playwright，无法抓取。安装方式：\n"
                "    uv sync --extra browser\n"
                "    uv run playwright install chromium"
            )
            return None

        platform = detect_platform(url)
        self._polite_delay()

        try:
            html = self._render(url)
        except Exception as exc:  # noqa: BLE001 - 网络/浏览器异常一律降级
            logger.warning("抓取失败（%s）：%s", url, exc)
            return None

        if not html:
            logger.warning("页面内容为空：%s", url)
            return None

        detail = html_to_detail(html, url, platform=platform)
        # 补一层：页面没给出平台的岗位 ID 时，从 URL 再试一次
        if detail.platform_job_id is None:
            detail.platform_job_id = extract_platform_job_id(url, platform)
        if detail.platform == "manual":
            detail.platform = platform

        if detail.fetch_status == "failed":
            logger.warning(
                "页面打开了但没抽到 JD 正文（可能需要登录 / 是验证页 / 结构已改版）：%s", url
            )
        return detail

    # -- 内部 ------------------------------------------------------------
    def _polite_delay(self) -> None:
        """随机延时（合规要求"频率克制"）。

        用配置里的区间；最小值 > 0 时才有意义（批量导入时尤其重要）。
        """
        rate = self.settings.rate_limit
        low, high = rate.min_delay_seconds, rate.max_delay_seconds
        if high <= 0:
            return
        delay = random.uniform(max(low, 0.0), high)  # noqa: S311 - 这不是加密用途
        logger.debug("抓取前延时 %.1fs", delay)
        time.sleep(delay)

    def _render(self, url: str) -> str:
        """用 Playwright 打开页面并取回 HTML（同步 API，CLI 场景够用）。"""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    # 用正常的中文桌面 UA：不是伪装，而是让站点返回中文页面
                    # （默认 UA 会带 "HeadlessChrome" 字样，部分站点会直接拒绝）
                    locale="zh-CN",
                    viewport={"width": 1440, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                )
                page = context.new_page()
                page.route("**/*", _block_heavy_resources)
                page.goto(url, wait_until=_WAIT_UNTIL, timeout=_DEFAULT_TIMEOUT_MS)
                # JD 正文常常是异步渲染的：给一个"尽量等、等不到就算了"的宽限
                # 有些页面永远不会 idle（长轮询、埋点请求），等不到就算了
                with contextlib.suppress(Exception):
                    page.wait_for_load_state("networkidle", timeout=5_000)
                return page.content()
            finally:
                browser.close()


def _block_heavy_resources(route: Any) -> None:
    """Playwright 路由钩子：屏蔽图片/媒体/字体，其余放行。"""
    try:
        if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
            route.abort()
        else:
            route.continue_()
    except Exception:  # noqa: BLE001 - 路由回调里不该让异常冒出去
        pass

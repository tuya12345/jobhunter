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

★★ 2026-09-21 更新：**用户明确授权放开"自动搜索"这一条**（原话："我做这个就是为了
爬岗位的"）。原决定里的三条理由现在这样处理：

    · **技术风险** → 已解决。spike 当年担心的 `history.back()` 反制已经查清并绕过
      （以目标 URL 新建标签页，见 `cdp.create_target`）；
    · **合规风险** → 用户已知情并拍板。**但"不自动投递"没有被放开** ——
      抓下来的岗位只是清单，投递按钮永远由人点；
    · **工作流不匹配** → 由用户自己判断，他明确说了要批量。

    实现放在**独立模块** `adapters/discover.py`（不塞进本模块）：这样
    `JobSource.search()` 仍然明确抛 `NotImplementedError`（那条守卫还在），
    "发现岗位"与"抓取详情"两件事的边界一眼可见。

★ 本模块的合规边界（写在这里因为这里是唯一能"接触外部世界"的地方）

    1. **不写操作**：只打开页面并读取内容。**绝不**点击"立即沟通/投递"、
       绝不点赞/关注/私信。翻页也**不点按钮**，靠改 URL 参数（`page=`）。
    2. **不绕风控**：不处理验证码、不伪造签名、不模拟登录态、不注入反检测脚本；
       遇到验证页/登录墙就**如实失败**，让用户改用 `--manual` 粘贴文本。
    3. **频率克制**：每次抓取前有随机延时；批量导入时逐个抓、失败即跳过并记录。
    4. **失败可降级**：抓不到就返回 `None`，上层提示"请用手动粘贴"，
       绝不让整条链路断掉（网络不通也要能用 —— 这就是 `--manual` 存在的理由）。
"""

from __future__ import annotations

import contextlib
import logging
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jobhunter.adapters.base import JobDetail, JobSource
from jobhunter.adapters.parse_html import (
    detect_access_wall,
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

#: 渲染文本被视为"可用 JD"的最小长度。低于它说明页面基本是空的
#: （验证页 / 登录墙 / 岗位已下架），此时宁可用 HTML 解析的结果。
_MIN_RENDERED_JD_LENGTH = 120


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


def read_html_file(
    path: str | Path,
    url: str | None = None,
    *,
    platform: str | None = None,
) -> JobDetail:
    """从本地 HTML 文件解析岗位（用于测试、离线重放、以及"另存为网页"的兜底）。

    ★ 这条路是**不需要浏览器**的：抓不到/不想抓时，用户可以把页面另存为 HTML
      再走这里。它同时是 mock HTML fixture 测试的入口。

    Args:
        path: 保存下来的 HTML 文件
        url: **原始岗位链接**（ISSUES #61）。保存下来的网页没有域名上下文，
            不传的话只能记成 `file:///…` —— 那样 `jh job list` 点不回去，
            第一层去重（`platform_job_id`）也用不上。
            CLI 的 `--url` 就是给它用的。
        platform: **强制指定平台**（ISSUES #61）。同样是"本地文件没有域名"的问题：
            工具无从判断这是 BOSS 还是别的站，而用户是知道的（他也显式传了
            `--platform`）。所以这里必须让它生效，而不是静默丢掉。
    """
    from jobhunter.utils.io import read_text

    target = Path(path)
    html = read_text(target)
    resolved_url = url or f"file:///{target.as_posix()}"
    # ★ 没有 `--platform` 时**不要**让 `detect_platform()` 去猜：它按域名判断，
    #   而本地文件没有域名 → 会落到兜底的 `official`（"公司官网"）。
    #   那是个**误导**：一份从 BOSS 另存下来的网页会被记成"公司官网"，
    #   然后出现在 `jh job list --platform official` 与渠道统计里。
    #   本地文件的事实是"我手动存下来的" → 记成 `manual` 才如实。
    #   （用户在命令行上知道得更清楚，所以 `--platform boss` 优先级最高。）
    if platform is None:
        return html_to_detail(html, resolved_url, platform="manual" if url is None else None)
    return html_to_detail(html, resolved_url, platform=platform)


class ManualAdapter(JobSource):
    """★ P2 主路径：打开用户给的 URL，抓取岗位详情。"""

    platform = "manual"

    def __init__(
        self,
        settings: AppSettings | None = None,
        *,
        platform: str | None = None,
        use_cdp: bool = False,
        cdp_endpoint: Any = None,
        release_page: bool = False,
    ) -> None:
        """Args:
        settings: 配置（不传用单例）
        platform: **强制指定的平台**（CLI 的 `--platform`，ISSUES #61）。

            ★ 为什么需要它：`--help` 里写的是"强制指定平台（默认按域名判断）"，
              但改前它**只对手动文本生效**，对链接抓取完全没用 —— 用户显式给出的
              选项被静默忽略（与 #34 / #50 同一类毛病：静默忽略比报错危险得多）。
              现在三条路（链接 / 本地 HTML / 手动文本）都认它。

            ★ 为什么放在 `__init__` 而不是给 `fetch_detail()` 加参数：
              `fetch_detail(url)` 是 `JobSource` 的抽象契约，加参数就得改 ABC
              （而其它适配器并不需要"覆盖平台"这件事）。平台是"这次抓取的上下文"，
              放在构造器里语义正确，也不污染接口。

        use_cdp: 用**用户自己的浏览器**（CDP）抓，而不是新起一个 headless 实例
            （ISSUES #65，CLI 的 `--cdp`）。需要登录的站点只有这条路能抓到正文。
        cdp_endpoint: 已经准备好的 CDP 端点（CLI 先 `ensure_cdp_browser()` 拿到它，
            顺便打印"去那个窗口登录一次"的提示）。不传则抓取时自己确保。
        release_page: **批量路径**用（`jh job crawl`）。为 True 时每条抓完就关掉
            **我们自己新开的**那个标签页（是最后一个就不关，见
            `cdp.close_page_if_not_last`）。默认 False —— 单条抓取"什么都不关"，
            用户能看见工具读了哪个页面。
        """
        self.settings = settings or get_settings()
        self._platform_override = platform
        self._use_cdp = use_cdp
        self._cdp_endpoint = cdp_endpoint
        self._release_page = release_page

    def _resolve_platform(self, url: str) -> str:
        """这次抓取该用哪个平台：显式指定的优先，否则按域名判断。"""
        return self._platform_override or detect_platform(url)

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

        platform = self._resolve_platform(url)
        self._polite_delay()

        try:
            # ★ 两条渲染路径共用后面全部逻辑（清洗、补字段、登录墙判定、平台 ID），
            #   差别只在"用哪个浏览器"（ISSUES #65）。
            if self._use_cdp:
                html, rendered_jd, title_hint, final_url = self._render_page_via_cdp(url)
            else:
                html, rendered_jd, title_hint, final_url = self._render_page(url)
        except Exception as exc:  # noqa: BLE001 - 网络/浏览器异常一律降级
            logger.warning("抓取失败（%s）：%s", url, exc)
            return None

        if not html:
            logger.warning("页面内容为空：%s", url)
            return None

        detail = html_to_detail(html, url, platform=platform)
        _finish_detail(
            detail,
            html=html,
            rendered_jd=rendered_jd,
            title_hint=title_hint,
            url=url,
            platform=platform,
            final_url=final_url,
        )
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
        return self._render_page(url)[0]

    def _render_page(self, url: str) -> tuple[str, str | None, str | None, str | None]:
        """打开页面，返回 `(html, 渲染后最长的文本块, data-test=jobTitle 的文本)`。

        ★★ 为什么要在**浏览器里**就把 JD 取出来，而不是拿 HTML 回来再解析

            现代招聘站（字节、腾讯、美团…）是 React/Vue 单页应用：字段由 JS 渲染，
            服务端 HTML 里没有可用的 DOM 结构，也**没有** schema.org 的 JSON-LD。
            实测字节的岗位页：`<title>` 是干净的岗位名，但正文只能靠"整页文本"兜底，
            结果把导航、页脚、甚至模板注释残留（`-->`）一起当成 JD 入库。

            而在浏览器里可以直接问"渲染后哪个元素的文本最长" —— 那几乎总是 JD 正文
            （页面其余部分是导航与推荐位，都很短）。让浏览器做这件事，
            比把 500 万字符的 HTML 拿回来再用正则猜可靠得多，也快得多。

            `data-test` 属性（如 `data-test="jobTitle"`）是前端写自动化测试用的，
            比 class 名稳定得多（class 常带哈希后缀，改版就变），所以也顺手取一下。
        """
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    # ★ 为什么固定用「中文 + 桌面版 Chrome」的 UA 与 locale（ISSUES #19）：
                    #   · locale=zh-CN  → 让站点返回**中文**页面（默认 en-US 会拿到英文文案，
                    #     公司名/岗位名都变成英文，关键词抽取与匹配全部失真）；
                    #   · 桌面 UA       → 让站点返回**桌面版**页面结构。移动版是另一套 DOM，
                    #     解析规则（最长文本块 / data-test 属性）在移动版上不成立。
                    #   它**不是**用来规避风控的：整个进程只发这一个 UA、**不随请求轮换**、
                    #   不伪造 Referer/Cookie；抓取路径也仍然只是"用户手动给的单个链接"，
                    #   没有自动翻页、没有批量遍历（见 docs/COMPLIANCE.md §5.2 / §5.5）。
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
                return _collect_from_page(page, url)
            finally:
                browser.close()

    def _render_page_via_cdp(self, url: str) -> tuple[str, str | None, str | None, str | None]:
        """用**用户自己的浏览器**（CDP）打开页面并取回 `(html, 渲染文本, 标题线索)`。

        ★★ 为什么要这条路（ISSUES #65）：headless 实例没有登录态，
          需要登录的站点只会给登录页 —— 工具再聪明也只能"如实报抓不到"。
          连到用户自己的浏览器实例，用的就是**他自己的会话**。

        ★ 它**不是**绕风控：#62 修好之后，"抓到登录页"是会明确报错的；
          这条路只是让"用户在浏览器里能看到的页面"也能被抓到 ——
          没有伪造 Cookie / Referer / 指纹，没有登录自动化。

        ★★ 2026-09-21 改：开页方式从"复用已有页面 + `goto`"换成
          **"以目标 URL 新建标签页"**（`cdp.open_target_page`）—— 原因是实测发现
          BOSS 的岗位页会反复 `history.back()`，只要标签页有"上一页"就被退走。
          详见 `cdp.create_target` 的说明与 `docs/COMPLIANCE.md` §5.8。
          新页已经**停在目标 URL 上**，所以这里**不能再 `goto`** ——
          再导航一次就等于又给自己加了一个"上一页"，白改。

        ★ 拿不到新页时**回退**到老路径（`cdp_page` + `goto`）并记一条 WARNING：
          老路径在会 `history.back()` 的站点上抓不到，但在别的站点仍然可用，
          而且浏览器版本不支持 `/json/new` 时也得有条活路。

        ★ 取内容的规则仍然**只有一份**（`_longest_rendered_text` / `_data_test_title` /
          `_finish_detail` 都是两条路共用的）—— 但**等待策略有意分叉**：
          headless 那条路走 `_collect_from_page`（耐心等 `networkidle`），
          这条走 `_grab_live_page`（抢读）。理由见 `_grab_live_page` 的实测：
          BOSS 的页面渲染出来 1~2 秒后会把标签页自己关掉，耐心等必然等到死页面。

        ★★ 抓完关不关页面，取决于走的是哪条路

          · **单条抓取（默认）什么都不关** —— 缺口 ① 的教训：初版在 `finally` 里
            关掉刚用过的页面，看着"很干净"，后果是那个专用窗口**只有一个标签页**，
            而 Edge/Chrome **关掉最后一个标签就退出** —— 用户刚登录好、跑一次抓取
            窗口就没了。所以页面留着，停在刚抓完的 URL 上（用户能看见工具读了哪个页面，
            这是透明度），下次抓取也能复用。
          · **批量抓取（`release_page=True`）会收拾自己那一页** —— 一次几十条的话，
            每条留一个标签页会把窗口堆满（实测一个 BOSS 页几十兆内存）。
            关之前先确认"关了还剩不剩标签页"，是最后一个就不关（同一条教训）。
        """
        from playwright.sync_api import sync_playwright

        from jobhunter.adapters import cdp as cdp_module

        endpoint = self._cdp_endpoint
        if endpoint is None:
            endpoint = cdp_module.ensure_cdp_browser(self.settings)

        with sync_playwright() as playwright:
            opened: tuple[Any, Any] | None = None
            with contextlib.suppress(Exception):
                opened = cdp_module.open_target_page(playwright, endpoint, url)

            if opened is None:
                logger.warning(
                    "以目标 URL 新建标签页没成功，回退到「复用已有页面 + goto」——"
                    "这条路在会 history.back() 的站点（如 BOSS）上抓不到正文"
                )
                context, page = cdp_module.cdp_page(playwright, endpoint)
                _prepare_page(page)
                return _collect_from_page(page, url)

            context, page = opened
            _prepare_page(page)
            try:
                # ★★ **抢读**，不是"耐心等"：BOSS 的页面渲染出来 1~2 秒后会把标签页
                #    自己关掉，等 7 秒必然等到死页面（见 `_grab_live_page` 的实测）。
                #    新页已经停在目标 URL 上，所以这里**也不 goto**（见上面的说明）。
                return _grab_live_page(page, url)
            finally:
                if self._release_page:
                    cdp_module.close_page_if_not_last(context, page)


def _finish_detail(
    detail: JobDetail,
    *,
    html: str,
    rendered_jd: str | None,
    title_hint: str | None,
    url: str,
    platform: str,
    final_url: str | None = None,
) -> None:
    """渲染之后的收尾：**两条渲染路径共用**（新起 headless / 连用户的浏览器）。

    ★ 抽出来的理由：这里是"渲染结果怎么变成 JobDetail"的**全部规则** ——
      渲染文本优先、补结构化字段、从 URL 补平台 ID、登录墙判定。
      两条路径各写一份的话，迟早会有一条忘了判登录墙（而那个后果是
      "登录页正文入库"，正是 #62）。

    ★★ 渲染文本优先（见 `_clean_rendered_text` 的说明）：清理后的头部就是
      "岗位名 + 工作地点 + 部门 + 更新时间"，正文就是完整的岗位职责 ——
      比"从 HTML 里猜"准得多，所以这里**不仅覆盖正文，还从这段文本里补结构化字段**。
      注意不要跟 HTML 版本比长度：清理过的版本天生更短，
      比长度会把带导航的噪音版本选回来（实测踩过）。
    """
    if rendered_jd and len(rendered_jd) >= _MIN_RENDERED_JD_LENGTH:
        detail.jd_text = rendered_jd
        detail.fetch_status = "ok"
        _fill_fields_from_rendered(detail, rendered_jd, platform)

    if title_hint and not detail.title:
        detail.title = title_hint

    # 补一层：页面没给出平台的岗位 ID 时，从 URL 再试一次
    if detail.platform_job_id is None:
        detail.platform_job_id = extract_platform_job_id(url, platform)
    if detail.platform == "manual":
        detail.platform = platform

    # ★★ 再过一道"这是不是登录墙"（ISSUES #62）：渲染**之后**能拿到的证据
    #   比只看 HTML 多（登录页的正文往往是客户端渲染出来的）。
    #   命中就把状态压成 failed 并清掉正文 —— 绝不让登录页文本入库。
    if detail.fetch_status == "failed":
        return
    wall_note = detect_access_wall(html, rendered_text=rendered_jd or "", platform=platform)
    if wall_note:
        detail.fetch_status = "failed"
        detail.fetch_note = wall_note
        detail.jd_text = ""
        detail.raw["access_wall"] = True
        return

    # ★★ 再看一眼"浏览器最后停在哪个地址"（ISSUES #65 实测踩到，JH-03 扩了判据）
    #
    #   在专用实例里跑真实 BOSS 链接时，浏览器把我们带到了**它自己的**
    #   「我们现在正在你的所有设备上同步你的浏览数据」首次运行页 ——
    #   那段文字被当成 JD（140 字）拿到 `partial`，**会被入库**。
    #   它既不是登录墙（没有登录字样），也没有岗位结构可判 —— 只看正文根本挡不住。
    #
    #   地址是最便宜的硬判据：**跨站**（域名不同）说明我们看到的不是那个岗位页；
    #   **同站换了岗位标识**（`/job/AAA` → `/job/BBB`）同样说明内容对不上号。
    #   两种都不许入库：混合记录（URL 来自 A、内容来自 B）会静默污染匹配与复盘。
    note = _cross_site_note(url, final_url)
    if note:
        detail.fetch_status = "failed"
        detail.fetch_note = note
        detail.jd_text = ""
        detail.raw["final_url"] = final_url
        detail.raw["redirect_mismatch"] = True
        if _hosts_differ(url, final_url):
            # 保留 `cross_site` 这个更具体的事实（诊断与用例都按它区分两种情形）
            detail.raw["cross_site"] = True


def _host_of(url: str | None) -> str:
    """取域名（小写、去掉 `www.`）。存疑时返回空串。**口径只有这一份。**"""
    if not url:
        return ""
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _hosts_differ(requested: str, final: str | None) -> bool:
    """两边的域名是否不同（任一边取不到域名时返回 False —— 不猜）。"""
    left, right = _host_of(requested), _host_of(final)
    return bool(left and right and left != right)


def _cross_site_note(requested: str, final: str | None) -> str | None:
    """`goto` 之后地址是不是**已经不是那个岗位**了？是就返回一句可操作的中文说明。

    ★ 三级判据（从硬到软）—— 只读代码审查 JH-03 补上了后两级：

      ① **跨站**：域名变了。浏览器把我们带到别的站（或它自己的首次运行页）——
         实测踩过（ISSUES #65）：Edge 的「同步你的浏览数据」提示页差点被当成 JD 入库。
      ② **同站、但岗位标识变了**：`/job/AAA` 跳到了 `/job/BBB`。
         过期岗位跳"推荐岗位"是最常见的一种，旧的判据（只比域名）完全看不见它 ——
         结果就是"URL 与 platform_job_id 来自 A，公司/职位/JD 来自 B"的混合记录，
         匹配与复盘全部被静默污染。
      ③ **同站、但被丢到了非详情页**：请求的是详情页，最后停在首页/列表/登录页
         （最终的路径段**全是**通用词）。这种情况没有岗位内容可抓，早点判失败比
         把首页文字当 JD 存进去好。

    ★ 为什么不比"完整 URL 相等"：同一个站点会有正常的规范化
      （补斜杠、加/去 `www.`、加跟踪参数、http→https、把 id 从路径挪到查询串），
      比整串会天天误报。所以这里比的是**从 URL 里抽出来的"岗位标识"集合**：
      路径段 / 片段 / 少数几个 id 类查询参数里，像 ID 的那些（见 `_url_identity_tokens`）。

    ★ 判据宁可漏一点，也不能误伤"正常的规范化跳转"：只有"两边都抽到了标识、
      且完全没有交集"才判失败（②）；"最终地址一个标识都没有、且路径段全是通用词"
      才判失败（③）。抽不出标识时不猜。
    """
    if not final:
        return None
    requested_host = _host_of(requested)
    final_host = _host_of(final)
    if not requested_host or not final_host:
        return None

    if requested_host != final_host:
        return (
            f"浏览器把你带到了**另一个站点**，抓到的是那里的页面而不是你要的岗位：\n"
            f"    你给的是：{requested_host}\n"
            f"    实际停在：{final}（{final_host}）\n"
            "  常见原因：① 浏览器**自己的提示页**（首次运行 / 同步数据 / 默认浏览器询问）\n"
            "            —— 在那个窗口里点掉它，再重跑一次就好；\n"
            "          ② 站点把你重定向到了登录/首页 —— 在那个窗口里登录后再重跑；\n"
            "          ③ 顺手用 --html 另存网页那条路也可以（见 jh job add --help）。"
        )

    wanted = _url_identity_tokens(requested)
    landed = _url_identity_tokens(final)
    if wanted and landed and wanted.isdisjoint(landed):
        return (
            "浏览器**换了另一个岗位**，抓到的是那个页面的内容：\n"
            f"    你给的是：{requested}\n"
            f"    实际停在：{final}\n"
            "  常见原因：① 那个岗位已经下线/过期，站点把你转到了推荐岗位或相似岗位；\n"
            "          ② 链接本身是短链/分享链，跳到的是当前有效的那一条。\n"
            "  怎么办：回到那个站点，打开你要投的**那一个**岗位，"
            "复制地址栏里的完整链接再跑一次（地址栏里就是岗位页才说明它还在）。"
        )

    if wanted and not landed and _path_is_all_generic(final):
        return (
            "浏览器停在了**非岗位页**（首页/列表/登录页这类），没有岗位内容可抓：\n"
            f"    你给的是：{requested}\n"
            f"    实际停在：{final}\n"
            "  常见原因：链接过期后站点把访客丢回列表页；或该站点要求先登录再进详情页。\n"
            "  怎么办：在那个窗口里登录/刷新一次，确认地址栏还是那个岗位页，再重跑本条命令。"
        )
    return None


#: 路径里的"通用词"—— 一个地址的路径段**全是**这些词时，它就只是一个入口页
#: （首页/列表/登录/详情壳），不是某个具体岗位。用于 `_cross_site_note` 的判据 ③。
_GENERIC_PATH_SEGMENTS: frozenset[str] = frozenset(
    {
        "index",
        "home",
        "main",
        "default",
        "welcome",
        "job",
        "jobs",
        "joblist",
        "job_list",
        "list",
        "lists",
        "search",
        "result",
        "results",
        "detail",
        "details",
        "position",
        "positions",
        "post",
        "posts",
        "view",
        "show",
        "apply",
        "login",
        "signin",
        "sign-in",
        "register",
        "auth",
        "sso",
        "passport",
        "web",
        "www",
        "m",
        "mobile",
        "app",
        "portal",
        "campus",
        "social",
        "en",
        "zh",
        "html",
        "htm",
        "php",
        "aspx",
        "jsp",
        "page",
        "pages",
    }
)

#: 像"岗位标识"的一段文本：字母/数字/下划线/连字符/点，长度 2–64。
#: 点号要允许（`8a1b2c.html` 这类），但**先用后缀表剥掉常见扩展名**再判断。
_ID_LIKE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{1,63}$")
#: 剥掉的扩展名（它们是"文件格式"，不是岗位标识的一部分）
_STRIP_SUFFIXES = (".html", ".htm", ".php", ".aspx", ".jsp", ".shtml")
#: 只有这几个查询参数名才可能承载"岗位标识"。
#: ⚠️ 刻意是个**白名单**：BOSS 的 `securityId` / `lid` 每次导航都会变，
#: 把它们算进标识集合会让正常的同页跳转被判成"换了岗位"（天天误报）。
_ID_QUERY_KEYS: frozenset[str] = frozenset(
    {"id", "jobid", "job_id", "jid", "positionid", "position_id", "postid", "post_id"}
)


def _url_identity_tokens(url: str) -> set[str]:
    """从 URL 里抽出"这一段地址指的是哪个东西"的标识集合（小写，用于**集合比较**）。

    ★ 什么算标识：含数字的段（`1234567`、`8a1b2c3d`、`web-123`），
      或全大写的短字母段（`AAA` 这类站点自己的 id）。
      "campus"、"detail" 这类纯小写单词**不算** —— 它们是栏目名，不是标识。
    """
    parts = urlsplit(url)
    raw: list[str] = [seg for seg in parts.path.split("/") if seg]
    if parts.query:
        for item in parts.query.split("&"):
            key, _, value = item.partition("=")
            if key.strip().lower() in _ID_QUERY_KEYS:
                raw.append(value)
    if parts.fragment:
        raw.extend(item for item in re.split(r"[/?&=]+", parts.fragment) if item)

    tokens: set[str] = set()
    for item in raw:
        candidate = item.strip()
        lowered = candidate.lower()
        for suffix in _STRIP_SUFFIXES:
            if lowered.endswith(suffix):
                candidate = candidate[: -len(suffix)]
                lowered = candidate.lower()
                break
        if lowered in _GENERIC_PATH_SEGMENTS or not _ID_LIKE_RE.match(candidate):
            continue
        if any(ch.isdigit() for ch in candidate) or (candidate.isalpha() and candidate.isupper()):
            tokens.add(lowered)
    return tokens


def _path_is_all_generic(url: str) -> bool:
    """这个地址的路径段是不是**全是通用词**（也就是"不是某个具体页面"）。

    ★ 只对最终地址用，且要求**每一个**段都是通用词 —— 只要有一段看不懂，
      就说明它可能是某个具体页面，这时**不判失败**（宁可漏，不要误伤）。
    """
    segments = [seg for seg in urlsplit(url).path.split("/") if seg]
    if not segments:
        return True  # 根路径：确实是"入口页"
    return all(seg.strip().lower() in _GENERIC_PATH_SEGMENTS for seg in segments)


def _prepare_page(page: Any) -> None:
    """导航前的准备：挡住重资源（图片/媒体/字体）。

    ★ 两条渲染路径（新起 headless / 连用户的浏览器）**共用**它 ——
      这样"我们请求了什么、没请求什么"只有一处定义。
    """
    with contextlib.suppress(Exception):
        page.route("**/*", _block_heavy_resources)


def _collect_from_page(page: Any, url: str) -> tuple[str, str | None, str | None, str | None]:
    """导航到 `url` 并取回 `(html, 渲染后的最长文本块, data-test 标题, 最终地址)`。

    ★ 这是**导航 + 耐心等待**那条路（新起 headless 实例时用；CDP 那条路改用
      `_grab_live_page` 抢读，原因见它的说明）。

    ★ 等待序列是**实测调出来的**，两步都不能省：
        · `domcontentloaded` 之后等 `networkidle`（有些页面永远不 idle → 5 秒就放弃）；
        · 再等 1.5 秒让单页应用首屏渲染完（不等的话正文还没出来，
          就会走"从 HTML 里猜"那条差得多的路）。

    ★ 为什么要带回**最终地址**：`goto` 之后浏览器可能把我们带到别处 ——
      跳转到登录页、跳转到站点首页、或者（CDP 路径上实测踩到）**浏览器自己的
      "同步你的浏览数据"首次运行页**。只看正文的话，那些页面的文字会被当成 JD 存进库；
      地址是**最便宜也最硬**的判据（见 `_finish_detail` 里的跨站判定）。
    """
    page.goto(url, wait_until=_WAIT_UNTIL, timeout=_DEFAULT_TIMEOUT_MS)
    # 有些页面永远不会 idle（长轮询、埋点请求），等不到就算了
    with contextlib.suppress(Exception):
        page.wait_for_load_state("networkidle", timeout=5_000)
    # 单页应用首屏渲染完还要一点时间（对这类站点很关键）
    with contextlib.suppress(Exception):
        page.wait_for_timeout(1_500)

    html = page.content()
    jd_text = _longest_rendered_text(page)
    title_hint = _data_test_title(page)
    final_url = getattr(page, "url", None)
    return html, jd_text, title_hint, final_url if isinstance(final_url, str) else None


#: 抢读的轮询间隔（秒）。**要快**：BOSS 的页面渲染出来约 1~2 秒后就把标签页关掉。
_GRAB_POLL_SECONDS = 0.2
#: 抢读的总上限（秒）
_GRAB_TIMEOUT_SECONDS = 20.0
#: 连续几次快照一样就认为"渲染完了"（给不会关页面的正常站点用，免得白等满 20 秒）
_GRAB_STABLE_READS = 2


def _grab_live_page(page: Any, url: str) -> tuple[str, str | None, str | None, str | None]:
    """**趁页面还活着赶紧读**，返回 `(html, 渲染文本, 标题线索, 最终地址)`。

    ╔══════════════════════════════════════════════════════════════════════╗
    ║ ★★ 为什么不走 `_collect_from_page` 那套"耐心等待"（2026-09-21 实测）   ║
    ╚══════════════════════════════════════════════════════════════════════╝

    实测（同一个浏览器实例、同样用 `cdp.create_target` 让标签页"以目标 URL 出生"）：

        · **搜索结果页**：0 秒时已有 249K HTML / 19 个岗位链接 → **1 秒时标签页被关掉**
        · **岗位详情页**：0~1 秒有 2161 字正文（含「职位描述」）→ **2 秒时标签页被关掉**
        · 对照组 `example.com`：**活满 15 秒**

    → **BOSS 的页面会把自己关掉**。`/json/new` 建出来的标签页算"脚本打开的"，
      所以页面有权调 `window.close()` —— 这是和岗位页那个反复 `history.back()`
      同一类的反自动化，只是手法不同。

    → 而 `_collect_from_page` 的序列是 `domcontentloaded` + `networkidle`(≤5s) + 1.5s
      ≈ **最多 7 秒**，**必然等到一个死页面**。实测的失败信息就是
      `Page.content: Target page, context or browser has been closed`。

    → 所以改成**抢读**：每 0.2 秒取一次快照，
      **连续两次一样就收工**（正常站点，渲染完了）、
      **页面被关掉就立刻用最后一次快照**（BOSS 这种站点）。
      两个停止条件缺一不可：只有"被关掉"会让正常站点每次白等满 20 秒。

    ★ 读快照**必须整块容错**：`page.content()` / `page.evaluate()` 在页面
      正在导航或正在被关闭时都会抛。抛了就说明窗口关了，用最后一次快照走人。
    """
    deadline = time.monotonic() + _GRAB_TIMEOUT_SECONDS
    best: tuple[str, str | None, str | None, str | None] | None = None
    previous: tuple[str, str, str, str] | None = None
    stable = 0

    while time.monotonic() < deadline:
        try:
            html = page.content()
            rendered = _longest_rendered_text(page)
            title_hint = _data_test_title(page)
            final_url = getattr(page, "url", None)
        except Exception:  # noqa: BLE001 - 页面被站点关掉了（见上面的实测）
            break

        if html:
            best = (
                html,
                rendered,
                title_hint,
                final_url if isinstance(final_url, str) else None,
            )
            current = (html, rendered or "", title_hint or "", final_url or "")
            stable = stable + 1 if current == previous else 0
            previous = current
            if stable >= _GRAB_STABLE_READS:
                break
        time.sleep(_GRAB_POLL_SECONDS)

    if best is None:
        raise RuntimeError(f"页面还没读到内容就被关掉了：{url}")
    return best


#: JD 正文的**起始特征词**（出现即认为"从这里开始是正文"）
#:
#: ★ 为什么用"特征词"而不是站点选择器（实测得出的结论）：
#:   浏览器取到的"最长文本块"必然是**整个内容区**，前面一定带着导航
#:   （首页 / 社会招聘 / 登录…）、后面带着页脚（相关网站 / 联系我们…）。
#:   与其去猜站点结构，不如用"JD 里一定会出现的词"做机械截断 ——
#:   各家招聘站在这一点上高度一致，而且这类词不会因为前端改版而消失。
_JD_START_MARKERS: tuple[str, ...] = (
    "职位描述",
    "岗位职责",
    "工作职责",
    "职位职责",
    "部门介绍",
    "团队介绍",
    "岗位介绍",
    "任职要求",
    "职位要求",
    "工作内容",
)

#: 页脚特征词（出现即认为"正文到此结束"）
_JD_END_MARKERS: tuple[str, ...] = (
    "相关网站",
    "联系我们",
    "候选人反馈",
    "官网使用体验",
    "版权所有",
    "ICP备",
    "隐私政策",
    "举报邮箱",
    "相关职位",
    "相似职位",
    "热门职位",
)


def _longest_rendered_text(page: Any, *, min_length: int = 80) -> str | None:
    """在**渲染后的页面**里找"最长的文本块"，清理后当作 JD 正文。

    ★ 判据是长度：页面上其余部分（导航、按钮、推荐位）都很短，
      而 JD 正文通常几百到几千字。这个判据对改版免疫 ——
      不需要知道站点用了什么 class 名。
    """
    script = """
    () => {
      let best = '';
      const nodes = document.querySelectorAll(
        'div, section, article, main, td, ul, [class*=detail], [class*=desc]'
      );
      for (const node of nodes) {
        const text = (node.innerText || '').trim();
        if (text.length > best.length) best = text;
      }
      return best;
    }
    """
    try:
        result = page.evaluate(script)
    except Exception:  # noqa: BLE001 - 页面脚本异常不该让整次抓取失败
        return None
    if not isinstance(result, str):
        return None
    text = _clean_rendered_text(result)
    return text if len(text) >= min_length else None


def _fill_fields_from_rendered(detail: JobDetail, rendered: str, platform: str) -> None:
    """从清理后的渲染文本里补结构化字段（**只填空、不覆盖已确认的值**）。

    ★ 这段文本的结构是稳定的（见 `_clean_rendered_text`）：
        第 1 行       = 岗位名
        `XX：YY` 行   = 工作地点 / 部门 / 更新时间 / 工作经验
        特征词之后     = JD 正文

      所以可以直接、机械地取字段，而不必再猜 DOM：
        · **岗位名**：第一行（导航已经被丢掉了）
        · **城市**：`工作地点：北京市` 里认城市 —— 这往往是页面上**唯一**
          明确写出城市的地方（正文里未必再写），而城市是匹配的硬条件维度
        · **经验/学历**：从整段里认（复用 parse_text 的规则）
        · **公司**：`岗位名 - 公司` 这种写法里，" - " 后面那段
    """
    from jobhunter.adapters.parse_text import (
        extract_city,
        extract_education,
        extract_experience,
    )

    lines = [line.strip() for line in rendered.splitlines() if line.strip()]
    if not lines:
        return

    # ---- 岗位名：第一行 ----
    first_line = lines[0]
    if not detail.title or detail.title in {"职位详情", "校招", platform}:
        # 去掉 `｜` 之后的方向说明？不 —— 那部分是有用信息（LLM / AI Agent 方向）
        detail.title = first_line[:80]

    # ---- 城市：优先从"工作地点/办公地点"字段行取 ----
    if not detail.city:
        for line in lines[:10]:
            if re.match(r"^(工作地点|办公地点|地点|城市)\s*[:：]", line):
                city = extract_city(line)
                if city:
                    detail.city = city
                    break
        if not detail.city:
            detail.city = extract_city(" ".join(lines[:10]))

    # ---- 经验 / 学历：整段里认（字段行优先） ----
    header_text = "\n".join(lines[:10])
    if not detail.experience:
        detail.experience = extract_experience(header_text) or extract_experience(rendered[:1500])
    if not detail.education:
        detail.education = extract_education(header_text) or extract_education(rendered[:1500])

    # ---- 公司：`岗位名 - 公司` 这种写法（页面 title 常见） ----
    if not detail.company:
        pattern = (
            r"^(?P<role>[^-–—|｜]{2,60})\s*[-–—]\s*"
            r"(?P<company>[^-–—|｜]{2,40})\s*$"
        )
        match = re.match(pattern, first_line)
        if match and not _is_probably_role_only(match.group("company")):
            detail.company = match.group("company").strip()


def _is_probably_role_only(text: str) -> bool:
    """判断一段文字是不是"岗位级别"而不是公司名（如 `社招-正式`、`日常实习`）。"""
    markers = ("社招", "校招", "实习", "正式", "兼职", "全职", "急招", "校招-正式")
    return any(marker in text for marker in markers)


def _clean_rendered_text(raw: str) -> str:
    """清理"最长文本块"：丢掉导航，保留"信息头 + 正文"。

    ★★ 为什么要用"两区段"规则，而不是列出所有导航词（实测教训）

        我一开始想用菜单词表（首页 / 登录 / 社会招聘…）逐行丢掉导航，
        但**词表不可能穷举** —— 实测残留了"北斗计划""美团赛事"这种
        站点专有栏目名，而且每个站的导航都不一样。

        换成结构化的规则就稳了：JD 正文必然以某个特征词开头
        （职位描述 / 岗位职责 / 部门介绍…），所以：

            区段 A（特征词之前）= 页面头部 → 只保留"像信息"的行
                · 带冒号的字段行：`工作地点：北京市`、`部门：…`、`更新时间：…`
                · 岗位名那一行：`算法工程师｜LLM / AI Agent 方向`
                → 这些是有用信息（**城市往往只能从这里拿到**）
                → 其余短行一律当导航丢掉
            区段 B（特征词之后）= JD 正文 → 全部保留，直到页脚特征词

        这个规则不依赖任何站点专有名词，因此改版免疫。
    """
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    lines = [line for line in lines if not _is_filler_line(line)]

    # 找正文起点
    start_index = len(lines)
    for index, line in enumerate(lines):
        if any(marker in line for marker in _JD_START_MARKERS):
            start_index = index
            break

    header, body = lines[:start_index], lines[start_index:]

    # 头部：只留"像信息"的行（带冒号，或是岗位名那一行）
    title_line = _pick_header_title_line(header)
    kept_header: list[str] = []
    for line in header:
        if line is title_line:
            kept_header.append(line)
            continue
        # 字段行：`工作地点：北京市` / `更新时间：…` / `部门：…` / `工作经验：不限`
        if re.match(r"^[\u4e00-\u9fffA-Za-z ]{2,10}\s*[:：]", line):
            kept_header.append(line)

    # 正文：到页脚特征词处停下
    for index, line in enumerate(body):
        if any(marker in line for marker in _JD_END_MARKERS):
            body = body[:index]
            break

    return "\n".join([*kept_header, *body]).strip()


def _pick_header_title_line(header: list[str]) -> str | None:
    """从页面头部里挑出"岗位名那一行"。

    判据：**最长的那一行**。导航项都是"首页""登录"这种极短词，
    而岗位名通常带方向与级别（实测 `算法工程师｜LLM / AI Agent 方向`）。
    """
    candidates = [line for line in header if len(line) >= 4]
    if not candidates:
        return None
    return max(candidates, key=len)


def _is_filler_line(line: str) -> bool:
    """判断一行是不是"模板残留"（空 bullet、注释尾巴、纯符号）。

    实测例子：`·`、`· 岗位名 - 公司 - 站点 -->`。
    这类行会把 JD 开头搞得像乱码，而它们有共同特征：
    以 bullet 开头且去掉符号后什么都不剩，或者以 `-->` 结尾。
    """
    stripped = line.strip()
    if not stripped:
        return True
    if stripped.endswith("-->"):
        return True
    residue = re.sub(r"[\s·•●○\-–—*>|]+", "", stripped)
    return len(residue) <= 1


def _data_test_title(page: Any) -> str | None:
    """取 `data-test="jobTitle"`（或类似）元素的文本。

    ★ 为什么值得单独取：`data-test` 是前端为自动化测试写的属性，
      比 class 名稳定得多（class 常带哈希后缀，改版就变）。
      取到它，标题就不必去猜 `<h1>`（很多站点的 h1 是站点 logo 或导航）。
    """
    for selector in (
        "[data-test='jobTitle']",
        "[data-test*='jobTitle' i]",
        "[data-test*='job-title' i]",
        ".job-title",
        "#job-title",
    ):
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                text = (locator.inner_text() or "").strip()
                if text:
                    return text.split("\n")[0].strip()
        except Exception:  # noqa: BLE001 - 选择器不匹配是正常情况
            continue
    return None


def _block_heavy_resources(route: Any) -> None:
    """Playwright 路由钩子：屏蔽图片/媒体/字体，其余放行。"""
    try:
        if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
            route.abort()
        else:
            route.continue_()
    except Exception:  # noqa: BLE001 - 路由回调里不该让异常冒出去
        pass

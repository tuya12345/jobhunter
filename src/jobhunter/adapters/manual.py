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
import re
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
            html, rendered_jd, title_hint = self._render_page(url)
        except Exception as exc:  # noqa: BLE001 - 网络/浏览器异常一律降级
            logger.warning("抓取失败（%s）：%s", url, exc)
            return None

        if not html:
            logger.warning("页面内容为空：%s", url)
            return None

        detail = html_to_detail(html, url, platform=platform)

        # ★★ 渲染文本优先（见 _clean_rendered_text 的说明）：
        #   清理后的头部就是"岗位名 + 工作地点 + 部门 + 更新时间"，
        #   正文就是完整的岗位职责。它比"从 HTML 里猜"准得多 ——
        #   所以这里**不仅覆盖正文，还从这段文本里补结构化字段**。
        #
        #   注意不要跟 HTML 版本比长度：清理过的版本天生更短，
        #   比长度会把带导航的噪音版本选回来（实测踩过）。
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

    def _render_page(self, url: str) -> tuple[str, str | None, str | None]:
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
                return html, jd_text, title_hint
            finally:
                browser.close()


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

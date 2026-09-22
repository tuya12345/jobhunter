"""自动搜索：从搜索结果页提取岗位链接（**2026-09-21 用户明确授权**）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 这个模块为什么现在才存在                                                   ║
╚══════════════════════════════════════════════════════════════════════════╝

本项目 P2 起把主路径定成 **"人给链接 → 工具抓详情"**，理由是三条：
技术风险（BOSS 有 `__zp_stoken__` 签名 + 字体反爬，自动翻页大概率撞墙）、
合规风险（批量抓取更容易触碰平台协议）、工作流不匹配。

2026-09-21 用户明确拍板要放开（原话：**"我做这个就是为了爬岗位的"**）：

    · **技术风险 → 已解决**：当年担心的"翻页撞墙"查清了根因 ——
      岗位页里有个定时器反复 `history.back()`，只要标签页有"上一页"就被退走。
      用"**以目标 URL 新建标签页**"（`cdp.create_target`）就绕开了。
      翻页本身也**不点按钮**，改成改 URL 的 `page=` 参数 —— 更稳，也不碰"操作页面"。
    · **合规风险 → 用户已知情并拍板**。但**"不自动投递"没有被放开**：
      这里只产出**岗位链接清单**，抓详情与投递仍然是"人点按钮"。
    · **工作流 → 用户自己判断**，他要批量。

★ 为什么单独一个模块、而不是塞进 `ManualAdapter`
    `JobSource.search()` 仍然明确抛 `NotImplementedError`（那条守卫还在，见
    `tests/verify/test_v_checklist12.py::test_adapters_do_not_implement_search_paging`）。
    把"发现岗位"与"抓取详情"分成两个模块，是为了让**边界一眼可见**：
    这个文件只做"把搜索结果页上的链接读出来"，不读 JD、不写库。

★ 合规边界（与 `manual.py` 同一套纪律）

    1. **不点击**：翻页靠 URL 参数，不找按钮、不点"下一页"。
       `tests/verify/test_v_checklist12.py` 的 AST 守卫扫 `src/`，点一下就红。
    2. **不绕风控**：命中验证页/风控特征词就**如实报失败**，不处理验证码、
       不伪造签名、不注入反检测脚本。**遇到就停**，不做"换个姿势再试"。
    3. **频率克制**：翻页之间、抓详情之间都有随机延时（复用 `_polite_delay` 的量级）。
    4. **不自动投递**：本模块连"投递"两个字都不该出现（除了这句注释）。
"""

from __future__ import annotations

import contextlib
import logging
import random
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import quote, urlsplit

from jobhunter.config.settings import AppSettings, get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "BOSS_SEARCH_URL",
    "CITY_CODES",
    "SearchPageResult",
    "build_search_url",
    "detect_blocked",
    "extract_job_links",
    "format_page_list",
    "is_blocked_url",
    "resolve_city_code",
]


def format_page_list(pages: Iterable[int]) -> str:
    """把页号写成「第 1 页」/「第 2、3 页」这种人话（**文案口径只有这一份**）。

    ★ 为什么抽出来：发现层与 CLI 都要说"重复来源是哪一页"，
      各写一份的话迟早有一处又退回写死「第 1 页」（2026-09-22 就是这么错的）。
    """
    ordered = sorted({int(page) for page in pages})
    if not ordered:
        return "前面某一页"
    return "第 " + "、".join(str(page) for page in ordered) + " 页"


#: BOSS 搜索结果页的 URL 模板。
#:
#: ★ 翻页靠 `page=` 参数、**不点"下一页"按钮**：既守住"不操作页面"这条纪律，
#:   也比"找按钮 → 点它 → 等新卡片出现"稳得多（站点改版时前者不会坏）。
#:
#: ★★ 路径是 **`/web/geek/job`（单数）**，2026-09-21 实测踩过的坑：
#:   `/web/geek/jobs`（复数）**永远停在「加载中，请稍候」**（等了 25 秒、正文 12 个字、
#:   HTML 里一个 `job_detail` 都没有）；换成单数 2 秒就渲染出岗位卡片。
#:   复数那条是站点的另一个页面（求职者中心），不是搜索结果页。
#:   ★ 这条是 `spikes/boss_search_spike.py` 早就写对的形态 —— 我第一版照浏览器地址栏
#:     里的 `/web/geek/jobs` 抄，抄错了。**spike 的结论值得先信一遍再改。**
BOSS_SEARCH_URL = "https://www.zhipin.com/web/geek/job?query={query}&city={city_code}&page={page}"

#: 城市名 → BOSS 的城市编码。
#:
#: ★ 只列常用几个是**刻意的**：抓一份完整的城市表本身又是一次对外请求，
#:   而"支持所有城市"对个人求职工具没有价值。要别的城市就把编码传进来
#:   （`--city 101010100` 这种也行，见 `resolve_city_code`）。
CITY_CODES: dict[str, str] = {
    "全国": "100010000",
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "东莞": "101281600",
    "杭州": "101210100",
    "成都": "101270100",
    "武汉": "101200100",
    "南京": "101190100",
}

#: 岗位详情页链接（搜索结果页里指向 `/job_detail/xxx.html` 的那些）。
_JOB_LINK_RE = re.compile(r'href="([^"]*job_detail[^"]*?\.html[^"]*)"')

#: 风控 / 验证页的特征词。**命中即如实报失败**，不尝试绕过。
#:
#: ★★ 加词之前先问一句"**正常页面会不会也出现它**" —— 这两个坑都是实测踩出来的：
#:
#:   · **"请稍候" 不能用**：搜索结果是单页应用，首屏渲染前正文就是
#:     「加载中，请稍候」—— 把"请稍候"当风控特征，会把**正常的加载中**判成被风控。
#:   · **"captcha" 不能用**：站点自己的资源/脚本名里就带这个词（正常页面也命中）——
#:     实测它把一次**本来能读到链接**的页面判成了风控页。
#:
#:   可靠的风控判据是**地址**（会停在 `security.html`，见 `is_blocked_url`）；
#:   这里的词只作为"没拿到链接时"的辅助归因，而且 `read_page` 会**先看有没有
#:   抠到链接**，抠到了就不再看这些词。
_BLOCKED_MARKERS: tuple[str, ...] = (
    "安全验证",
    "访问过于频繁",
    "当前IP",
    "zp_stoken",
)

#: 风控/验证页的 URL 特征（比正文特征可靠 —— 正文会被单页应用的加载占位干扰）。
_BLOCKED_URL_PARTS: tuple[str, ...] = ("security.html", "/passport/", "verify")

#: 等"搜索结果真的渲染出来"的上限（秒）。单页应用首屏可能要好几秒。
_RESULT_WAIT_SECONDS = 20.0
#: 轮询间隔（**要快**：BOSS 的搜索页渲染出来约 1 秒后就把标签页自己关掉了）
_RESULT_POLL_SECONDS = 0.2
#: 抢读失败时最多重开几次标签页（窗口只有约 1 秒，出发晚了就抢不到）
_GRAB_ATTEMPTS = 3


def is_blocked_url(url: str | None) -> bool:
    """这个最终地址像不像"被带到风控/登录页去了"。"""
    text = (url or "").lower()
    return any(part in text for part in _BLOCKED_URL_PARTS)


#: 翻页之间的随机延时（秒）。与 `rate_limit` 同量级 —— 对目标站点保持克制。
_PAGE_DELAY_RANGE = (2.0, 5.0)


def resolve_city_code(city: str) -> str:
    """城市名 → 编码；已经是纯数字编码就原样返回。

    ★ 为什么允许直接给编码：`CITY_CODES` 只列了常用几个，用户要去别的地方时
      不该被迫改代码。给编码是最省事的出路。
    """
    text = (city or "").strip()
    if not text:
        return CITY_CODES["全国"]
    if text.isdigit():
        return text
    return CITY_CODES.get(text, CITY_CODES["全国"])


def build_search_url(keyword: str, city: str, page: int = 1) -> str:
    """拼搜索结果页的 URL。**只拼 URL，不发请求。**"""
    return BOSS_SEARCH_URL.format(
        query=quote(keyword.strip()),
        city_code=resolve_city_code(city),
        page=max(1, int(page)),
    )


@dataclass(frozen=True)
class SearchPageResult:
    """一页搜索结果读出来的东西。"""

    page_url: str
    #: 这一页里发现的岗位链接（已按路径去重，保留出现顺序）
    job_urls: list[str] = field(default_factory=list)
    #: 命中的风控/验证特征词（非空 = 这一页不该信）
    blocked: list[str] = field(default_factory=list)
    #: 最终停在哪个地址（可能被重定向走）
    final_url: str | None = None
    #: 一句话说明（失败原因 / 空页提示）
    note: str | None = None
    #: ★ 这一页读到的链接**全都已经在前面几页出现过**（`--pages` 很可能没生效）。
    #:   见 `search()` 的说明：站点不认 `page=` 参数时，第 2 页与第 1 页是同一批。
    duplicate_of_previous: bool = False
    #: ★ 那些链接**首次出现在哪几页**（升序、去重）。空元组 = 没判重复。
    #:
    #:   为什么必须记下来（2026-09-22 复核发现的缺陷）：判据比的是
    #:   「前面所有页的并集」，所以重复来源**未必是第 1 页** ——
    #:   文案若写死「与第 1 页」，在"第 3 页重复第 2 页"时既与事实不符，
    #:   也会把归因指向反方向（第 1、2 页内容不同 ⇒ 站点其实**是**认 `page=` 的）。
    duplicate_of_previous_pages: tuple[int, ...] = ()

    @property
    def usable(self) -> bool:
        """这一页能不能用（没被风控、且真的读到了链接）。"""
        return not self.blocked and bool(self.job_urls)


def extract_job_links(html: str) -> list[str]:
    """从搜索结果页的 HTML 里抠出岗位详情页链接。

    ★ 为什么按"路径"去重而不是整串 URL：搜索结果页里同一个岗位可能出现在
      "主列表 + 推荐位"两处，查询串还不一样（带 `?lid=` 之类）。按整串去重会漏，
      按路径（`/job_detail/<id>.html`）去重才准。

    ★ 为什么用正则而不是解析 DOM：这一步只要"链接字符串"，用 DOM 解析得先把
      整页 HTML 建树（这些页面动辄几 MB）。真正的结构化解析交给详情页那条路
      （`parse_detail_from_html`），这里越轻越好。
    """
    seen: set[str] = set()
    found: list[str] = []
    for raw in _JOB_LINK_RE.findall(html):
        url = _absolute(raw)
        if not url:
            continue
        key = urlsplit(url).path
        if key in seen:
            continue
        seen.add(key)
        found.append(url)
    return found


def detect_blocked(html: str) -> list[str]:
    """这一页有没有风控/验证特征。返回命中的词（空列表 = 干净）。"""
    return [marker for marker in _BLOCKED_MARKERS if marker in html]


def _has_job_cards(html: str) -> bool:
    """页面里**真的出现了岗位卡片链接**吗。

    ★★ 为什么不能用 `"job_detail" in html`（2026-09-21 实测踩到的假判据）：

      搜索结果页的**页脚**里就有一个链接指向 `https://www.zhipin.com/job_detail/`
      —— 于是 `"job_detail" in html` 在**岗位卡片还没渲染出来时就成立**，
      "抢读"会立刻收工，抓到的是一张只有壳的页面（`/job_detail/` 只出现 1 次、
      含 `.html` 的 0 个），最后报成"这一页没读到岗位链接"。

      表现就是**时好时坏**：同一套代码有时 15 个链接、有时 0 个。
      实测那次失败就是这样：页面 202 KB，`job_detail` 只有页脚那一个。

    → 判据必须落到**真正的卡片链接**上：`/job_detail/<id>.html`，
      也就是 `extract_job_links()` 用的那个正则。
    """
    return bool(_JOB_LINK_RE.search(html))


def _wait_for_job_cards(page: Any, html: str) -> str:
    """**抢读**搜索结果：每 0.2 秒读一次，保留"最后一次含**卡片链接**的内容"。

    ╔══════════════════════════════════════════════════════════════════════╗
    ║ ★★ 为什么是"抢读"而不是"耐心等"（2026-09-21 实测）                     ║
    ╚══════════════════════════════════════════════════════════════════════╝

    实测（`_diag-whokills.py`，同一个浏览器实例、同一个 `/json/new` 开页方式）：

        · `example.com`          → 新标签页**活满 15 秒**（对照组）
        · BOSS 搜索结果页        → **0 秒时页面已经有 249K HTML、19 个
                                   `job_detail` 链接**；**1 秒时标签页被关掉**
                                   （`TargetClosedError`，`/json/list` 里也没了）

    → **BOSS 的搜索页会把自己关掉**（`/json/new` 建出来的标签页算"脚本打开的"，
      所以页面有权 `window.close()`）。这和岗位页那个反复 `history.back()` 是同一类
      "反自动化"，只是手法不同。

    → 所以**绝不能用 `_collect_from_page` 那套"domcontentloaded + networkidle(≤5s)
      + 1.5s"的等待序列** —— 那要 7 秒，等到时页面早死了（实测就是这么失败的：
      报 `Page.content: Target page ... has been closed`）。

    → 正确做法是**趁页面还活着赶紧读**：内容在开页后不到 1 秒就已经在 DOM 里了。
      每 0.2 秒读一次，**保留最后一次含 `job_detail` 的内容**（越晚读越完整），
      读到页面死掉（抛异常）或超时为止。

    ★ 为什么"保留最后一次"而不是"第一次读到就返回"：单页应用是渐进渲染的，
      第一次读到卡片链接时可能只有几个。既然页面还能再撑几百毫秒，
      多读几次拿到的更全 —— 代价只是 0.2 秒。

    ★ 读内容**必须容错**：页面正在导航/正在被关闭时 `page.content()` 会抛
      （「page is navigating and changing the content」/ `TargetClosedError`）。
      抛了就说明窗口关了，立刻停止 —— 不要让它把整次抓取带崩。

    ★★ 停止条件必须用 `_has_job_cards()`（真的卡片链接），**不能用 `"job_detail" in html`**
      —— 页脚里那个 `job_detail` 链接会让它提前成立，见 `_has_job_cards` 的说明。
    """
    deadline = time.monotonic() + _RESULT_WAIT_SECONDS
    best = html
    while time.monotonic() < deadline:
        try:
            html = page.content()
        except Exception:  # noqa: BLE001 - 页面被关掉了（见上面的实测）
            break
        if _has_job_cards(html):
            best = html
        elif best:
            break
        time.sleep(_RESULT_POLL_SECONDS)
    return best


def _absolute(href: str) -> str | None:
    """把搜索结果页里的 href 补成绝对地址；不认识的形态返回 None。"""
    text = href.strip()
    if text.startswith("//"):
        return "https:" + text
    if text.startswith("/"):
        return "https://www.zhipin.com" + text
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return None


def _duplicate_note(index: int, count: int, source_pages: list[int]) -> str:
    """「这一页是重复的」那句话。**说清重复来源是哪一页**，并按来源给对归因。

    ★★ 为什么归因要分两种（2026-09-22 复核发现的缺陷）：

      原来的文案写死「与**第 1 页**是同一批……站点可能不认 `page=` 参数」。
      但判据 `page_paths <= first_seen.keys()` 比的是**前面所有页的并集** ——
      当重复来源**不是**第 1 页时，恰恰说明前面几页**内容不同**，
      站点其实**是认** `page=` 的（更常见的解释是"结果只有这么多页，
      最后一页被夹回上一页"）。把归因指向"不认 page="，
      会让用户去查一个不存在的参数问题。
    """
    where = format_page_list(source_pages)
    if source_pages == [1]:
        diagnosis = "站点可能不认 `page=` 参数。"
    else:
        diagnosis = "站点可能只返回了这么多页（后面几页被夹回上一页）。"
    return (
        f"第 {index} 页与{where}是**同一批岗位**"
        f"（{count} 条全部已经出现过）"
        f"—— `--pages` 看起来没有生效：{diagnosis}"
        f"已提前停止，后面几页不再白跑（每页还要一次请求 + "
        f"{_PAGE_DELAY_RANGE[0]:.0f}~{_PAGE_DELAY_RANGE[1]:.0f} 秒延时）"
    )


class JobDiscovery:
    """按关键词 + 城市把岗位链接找出来（**只找链接，不抓 JD、不写库**）。

    用法：
        discovery = JobDiscovery(settings, use_cdp=True)
        for page in discovery.search("Python", "东莞", pages=2):
            ... page.job_urls ...
    """

    def __init__(
        self,
        settings: AppSettings | None = None,
        *,
        use_cdp: bool = True,
        cdp_endpoint: Any = None,
        delay_range: tuple[float, float] = _PAGE_DELAY_RANGE,
    ) -> None:
        self.settings = settings or get_settings()
        self._use_cdp = use_cdp
        self._cdp_endpoint = cdp_endpoint
        self._delay_range = delay_range

    # -- 公开接口 --------------------------------------------------------
    def search(self, keyword: str, city: str, *, pages: int = 1) -> list[SearchPageResult]:
        """逐页读搜索结果。**页数由调用方给定，不做"翻到没有为止"**。

        ★ 为什么不"自动翻到最后一页"：那是无上限的对外请求。用户要几页就给几页，
          这个数字是他自己的决定（`--pages`），而不是程序替他决定的。

        ★★ `--pages > 1` 静默无效时要说出来（#67 优先级 2）。

          实测（交接文档 §8 第 5 条）：BOSS **不认** `page=` 参数 ——
          第 2 页抓回来的还是第 1 页那一批，于是 `--pages 2` 除了多花 2~5 秒延时
          与一次对外请求之外**什么也没做**，而用户看到的输出与 `--pages 1` 一模一样。

          "静默无效"正是本项目最不接受的一类失真，所以这里两步都做：

            ① **提前停**：某一页读到的链接**全部**在前面出现过 → 后面的页不用再跑
               （每页都要一次对外请求 + 2~5 秒延时，白跑是实打实的浪费）；
            ② **留痕**：把这一页标成 `duplicate_of_previous=True` 并写一句 `note`，
               调用方（`jh job crawl`）会把"这个参数看起来没生效"直接打给用户看。
        """
        results: list[SearchPageResult] = []
        # ★ 记的是「每个岗位路径**首次**出现在第几页」，不是"见过没有"（2026-09-22）：
        #   判据比的是**前面所有页的并集**，所以重复来源未必是第 1 页 ——
        #   文案必须说它真正重复的那一页（见 `duplicate_of_previous_pages`）。
        first_seen: dict[str, int] = {}
        total = max(1, int(pages))

        for index in range(1, total + 1):
            url = build_search_url(keyword, city, page=index)
            logger.info("读取搜索结果第 %d 页：%s", index, url)
            result = self.read_page(url)

            page_paths = {urlsplit(job_url).path for job_url in result.job_urls}
            if index > 1 and page_paths and page_paths <= first_seen.keys():
                # 整页都是旧的 → 站点多半没认 `page=` 参数
                source_pages = sorted({first_seen[path] for path in page_paths})
                result = replace(
                    result,
                    duplicate_of_previous=True,
                    duplicate_of_previous_pages=tuple(source_pages),
                    note=_duplicate_note(index, len(page_paths), source_pages),
                )
            for path in page_paths:
                first_seen.setdefault(path, index)
            results.append(result)

            if result.blocked:
                logger.warning("第 %d 页命中风控特征，**停止翻页**（不尝试绕过）", index)
                break
            if result.duplicate_of_previous:
                logger.warning("第 %d 页与前面重复，停止翻页（`--pages` 无效）", index)
                break
            if index < total:
                self._polite_delay()
        return results

    def read_page(self, url: str) -> SearchPageResult:
        """读一页搜索结果页，返回它上面的岗位链接。

        ★★ 判定的**顺序**很重要：**先看有没有抠到链接，抠到了就当成功**。

          为什么不是先判风控：风控特征词是按"页面上出现过这几个字"来匹配的，
          而站点的正常页面里也会出现它们（实测踩过两次 —— 正文里的
          「加载中，请稍候」、资源里的 `captcha`）。
          **一个模糊的特征词不该否决一次已经拿到东西的读取** ——
          那会把"其实成功了"报成"被风控了"，比漏报更难查。

          只有在**没拿到链接**时，才去区分"是空结果 / 要登录 / 真被风控了"，
          并把原因如实告诉用户。
        """
        html, final_url, note = self._render(url)
        if html is None:
            return SearchPageResult(page_url=url, final_url=final_url, note=note)

        # ① 先看结果 —— 拿到了就算成功，后面那些判定都不再需要
        job_urls = extract_job_links(html)
        if job_urls:
            return SearchPageResult(page_url=url, job_urls=job_urls, final_url=final_url)

        # ② 没拿到，才开始归因。先看**地址**（最硬的判据）
        if is_blocked_url(final_url):
            return SearchPageResult(
                page_url=url,
                final_url=final_url,
                blocked=["URL"],
                note=(
                    f"被带到了风控/登录页：{final_url} —— 如实失败，不尝试绕过。"
                    "先在专用窗口里登录一次，再重跑"
                ),
            )

        blocked = detect_blocked(html)
        if blocked:
            return SearchPageResult(
                page_url=url,
                final_url=final_url,
                blocked=blocked,
                note=f"页面含风控/验证特征（{'、'.join(blocked)}）—— 如实失败，不尝试绕过",
            )

        return SearchPageResult(
            page_url=url,
            final_url=final_url,
            # ★★ 候选原因**必须按常见程度排**，而且第一条得是代码自己认定的主因
            #   （2026-09-22 复核发现的缺陷）：`_wait_for_job_cards` 的实测早就写明
            #   "同一套代码有时抢到 15 个链接、有时一个都没抢到 —— 不是站点不稳定，
            #   是**我们出发得太晚**"。而原来的文案只列了「空结果 / 需要登录 /
            #   页面结构变了」，用户会去查登录、查改版，**真正该做的（重跑一次）
            #   一个字都没提**。真机实测：同一命令相隔约 1 分钟，一次 15 条、一次 0 条。
            note=(
                "这一页没读到岗位链接。可能的原因（按常见程度排）：\n"
                "    ① **抢读窗口错过了** —— 站点的搜索结果页渲染出来约 1 秒就会把标签页"
                "自己关掉，我们出发晚一点就只拿到空壳。**重跑一次往往就好了**；\n"
                "    ② 这一页确实没有结果（换关键词 / 换城市试试）；\n"
                "    ③ 需要先登录（在那个专用窗口里登录一次，再重跑）；\n"
                "    ④ 站点改版，链接形态不再是 `/job_detail/<id>.html`"
                "（那要改 `discover._JOB_LINK_RE` 与 `BOSS_SEARCH_URL`）。"
            ),
        )

    # -- 内部 ------------------------------------------------------------
    def _render(self, url: str) -> tuple[str | None, str | None, str | None]:
        """打开搜索结果页并**抢读**内容，返回 `(html, final_url, 失败原因)`。

        ★ 用**以目标 URL 出生**的标签页（`cdp.open_target_page`）而不是 `goto`：
          同一套理由（见 `cdp.create_target`）。

        ★★ **故意不走** `manual._collect_from_page` 那套等待序列（这是本模块与
          `manual` 唯一一处**有意分叉**的地方，值得写清楚）：

          `_collect_from_page` 的序列是 `domcontentloaded` + `networkidle`(≤5s) + 1.5s
          ≈ **最多 7 秒** —— 那对**详情页**是对的（详情页不会跑）。
          但搜索页**会在渲染后约 1 秒把标签页自己关掉**（实测，见 `_wait_for_job_cards`），
          等 7 秒回来页面早死了。所以这里改成**趁活着赶紧读**。

        ★ 也**不调** `_prepare_page`（挡图片/字体）：它的作用是"让页面加载更快"，
          而路由要在**导航前**挂上才有意义 —— 这里的标签页一出生就在导航了，
          挂路由只多一次往返，还会吃掉那不到 1 秒的窗口。搜索页要的是**快**。
        """
        from playwright.sync_api import sync_playwright

        from jobhunter.adapters import cdp as cdp_module

        endpoint = self._cdp_endpoint
        if endpoint is None:
            endpoint = cdp_module.ensure_cdp_browser(self.settings)

        html = ""
        final_url: str | None = None
        last_note: str | None = None

        # ★★ 为什么要**重试**：抢读的窗口只有约 1 秒（页面渲染后就把标签页关掉），
        #    而 `open_target_page()` 里"连 Playwright + 认页"本身也要时间 ——
        #    实测同一套代码**有时抢到 15 个链接、有时一个都没抢到**。
        #    这不是"站点不稳定"，是**我们出发得太晚**；重开一次标签页就能再拿一次机会。
        #    重试之间照常走随机延时（对站点保持克制，不因为重试就加快节奏）。
        for attempt in range(1, _GRAB_ATTEMPTS + 1):
            try:
                with sync_playwright() as playwright:
                    context, page = cdp_module.open_target_page(playwright, endpoint, url)
                    try:
                        with contextlib.suppress(Exception):
                            final_url = page.url
                        html = _wait_for_job_cards(page, "")
                    finally:
                        cdp_module.close_page_if_not_last(context, page)
            except cdp_module.CdpOwnerError:
                # ★★ "连错浏览器"要当场停下（#67 优先级 4）：重试三次只会连错三次。
                raise
            except Exception as exc:  # noqa: BLE001 - 抓不到就如实报，不无限重试
                last_note = f"读取失败：{type(exc).__name__}: {exc}"
                logger.warning(
                    "搜索结果页读取失败（第 %d/%d 次）：%s", attempt, _GRAB_ATTEMPTS, exc
                )

            # ★★ 重试的判据是"**有没有真的卡片链接**"，不是"html 非空"。
            #    实测踩过：页面 202 KB（非空）但卡片没渲染出来 —— 旧写法不重试，
            #    直接报成"这一页没读到岗位链接"，看着像站点问题，其实是抢早了。
            if _has_job_cards(html):
                return html, final_url, None

            if attempt < _GRAB_ATTEMPTS:
                logger.info(
                    "这一页还没渲染出岗位卡片，重开一次标签页再试（第 %d/%d 次）",
                    attempt + 1,
                    _GRAB_ATTEMPTS,
                )
                self._polite_delay()

        if html:
            # 三次都没拿到卡片 —— 把最后一次的内容交回 `read_page` 去**归因**
            # （可能是空结果 / 需要登录 / 真被风控），别在这里就把原因定死。
            return html, final_url, None

        return (
            None,
            final_url,
            # ★★ 这条兜底文案必须和 `read_page()` 那条**同源**（2026-09-22 真机复核发现）：
            #   上一轮只改了 `read_page` 的"html 非空但没链接"那条路，而真机上
            #   （未登录 / 抢读全失败）走的是**这一条**（`html is None`）——
            #   于是用户看到的仍是旧文案"空结果 / 需要登录 / 页面结构变了"，
            #   而最该先试的"重跑一次"一个字没提
            #   （实测：同一命令相隔 1 分钟，一次 15 条、一次 0 条）。
            last_note
            or (
                "三次抢读都没拿到搜索结果页的内容。可能的原因（按常见程度排）：\n"
                "    ① **抢读窗口错过了** —— 搜索页渲染出来约 1 秒就把标签页自己关掉，"
                "出发晚一点就只拿到空壳。**重跑一次往往就好了**；\n"
                "    ② 需要先登录（在那个专用窗口里登录一次，再重跑）；\n"
                "    ③ 站点给了验证页 / 风控页（本工具**不绕风控**，遇到就停）；\n"
                "    ④ 站点改版（`discover._JOB_LINK_RE` 与 `BOSS_SEARCH_URL` 会先失效）。"
            ),
        )

    def _polite_delay(self) -> None:
        """翻页前的随机延时（与 `ManualAdapter._polite_delay` 同一套量级）。"""
        low, high = self._delay_range
        delay = random.uniform(max(low, 0.0), high)  # noqa: S311 - 不是加密用途
        logger.debug("翻页前延时 %.1fs", delay)
        time.sleep(delay)

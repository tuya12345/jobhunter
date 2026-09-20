"""从 HTML 抽 `JobDetail`（纯函数，可用 mock HTML 测试）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 分层：**抓取**（需要浏览器/网络）与**解析**（纯函数）分开                   ║
╚══════════════════════════════════════════════════════════════════════════╝

    浏览器（adapters/manual.py, Playwright）
        └─ 交出可信的「页面标题 + 可见文本 + 关键区域的 HTML」
              └─ 本模块：把文本解析成 JobDetail（**不碰网络、不需要浏览器**）
                    └─ services/job_svc.py：入库 + 三层去重

这么分的直接收益：**P2 的测试完全不需要浏览器**。真实招聘站点在 CI 里
要么打不开、要么要登录、要么随时改版；而"给定这段文本能解析出什么"是确定性的，
可以拿 mock HTML fixture 精确断言。抓取那一段只在本地当你手动跑时才会执行。

★ 一个关键判断：**JD 正文优先取"最长的那个候选块"**

    招聘页面上"职位描述"这个标题会出现很多次（导航、相关推荐、页脚），
    按 class/id 猜选择器一旦改版就全废。而 JD 正文有个稳定的特征：
    **它是页面上最长的一段结构化文本**。所以策略是：
        ① 优先用平台特定的选择器（如果命中且够长）；
        ② 否则在候选区域里挑**最长**的那块 —— 这个判据对改版免疫得多。
"""

from __future__ import annotations

import json
import re
from typing import Any

from jobhunter.adapters.base import JobDetail
from jobhunter.adapters.parse_text import (
    extract_city,
    extract_education,
    extract_experience,
    extract_salary,
    looks_like_intern,
    normalize_text,
    strip_html,
)

__all__ = [
    "PLATFORM_SELECTORS",
    "detect_platform",
    "extract_platform_job_id",
    "parse_detail_from_html",
    "parse_detail_from_text",
]

# ---------------------------------------------------------------------------
# 平台识别
# ---------------------------------------------------------------------------
#: 平台标识 → 域名特征。判定"这是哪个平台"用于 jobs.platform 与去重第一层。
PLATFORM_BY_HOST: dict[str, tuple[str, ...]] = {
    "boss": ("zhipin.com",),
    "lagou": ("lagou.com",),
    "51job": ("51job.com", "51job.com.cn"),
    "liepin": ("liepin.com",),
    "zhilian": ("zhaopin.com",),
    "linkedin": ("linkedin.com",),
    "official": (),  # 兜底：公司官网（没有平台特征）
}

#: 从 URL 里抠平台内岗位 ID 的正则（**去重第一层的关键**）
#:
#: ★ 为什么优先从 URL 抠 ID 而不是从页面里抠：
#:   URL 里的 ID 稳定、可离线获得（`--file` 批量导入时还没抓页面就能算出来），
#:   而页面里藏的 ID（JS 变量、data 属性）随改版而变。
PLATFORM_JOB_ID_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "boss": (
        re.compile(r"/job_detail/(?P<id>[A-Za-z0-9_~\-]+)\.html"),
        re.compile(r"[?&]jobId=(?P<id>[A-Za-z0-9_~\-]+)"),
        re.compile(r"[?&]securityId=(?P<id>[A-Za-z0-9_~\-]+)"),
    ),
    "lagou": (
        re.compile(r"/jobs/(?P<id>\d+)\.html"),
        re.compile(r"[?&]positionId=(?P<id>[A-Za-z0-9_~\-]+)"),
    ),
    "51job": (
        re.compile(r"/job/(?P<id>[A-Za-z0-9_~\-]+)\.html"),
        re.compile(r"jobid=(?P<id>[A-Za-z0-9_~\-]+)", re.IGNORECASE),
    ),
    "liepin": (re.compile(r"/job/(?P<id>\d+)"),),
    "zhilian": (re.compile(r"/jobs/(?P<id>[A-Za-z0-9_~\-]+)"),),
    "linkedin": (re.compile(r"/jobs/view/(?P<id>\d+)"),),
    # ★ 各家公司自建招聘站的岗位 ID 都在查询参数里（实测：美团用 `jobUnionId`）。
    #   这类站点域名千变万化，但参数名高度重复，所以按"参数名"而不是"域名"来配。
    "official": (
        re.compile(r"[?&]jobUnionId=(?P<id>[A-Za-z0-9_~\-]+)", re.IGNORECASE),
        re.compile(r"[?&]jobId=(?P<id>[A-Za-z0-9_~\-]+)", re.IGNORECASE),
        re.compile(r"[?&](?:positionId|postId|job_id)=(?P<id>[A-Za-z0-9_~\-]+)", re.IGNORECASE),
        re.compile(r"/position/(?P<id>\d{6,})"),
        re.compile(r"/positions?/(?P<id>[A-Za-z0-9_~\-]{4,})", re.IGNORECASE),
        re.compile(r"/job/(?P<id>[A-Za-z0-9_~\-]{4,})", re.IGNORECASE),
    ),
}

#: 平台选择器（**尽力而为，不是唯一依赖**）。
#: 结构：平台 → {字段: [选择器, ...]}。命中即用；都没命中就回退到"最长文本块"策略。
#: ⚠️ 写在代码注释里免得后人误解：这些选择器**会过期**。
#:    所以每个字段都留了通用兜底，选择器失效时功能降级而不是完全不可用。
PLATFORM_SELECTORS: dict[str, dict[str, tuple[str, ...]]] = {
    "boss": {
        "jd": (".job-sec-text", ".job-detail-section .text", "[class*=job-detail] .text"),
        "title": (".job-banner .name h1", ".job-name", "h1"),
        "company": (".company-info .name", ".company-name", "[class*=company] .name"),
        "salary": (".job-banner .salary", ".salary"),
        "meta": (".job-banner .info-primary p", ".job-location", ".info-primary"),
    },
    "lagou": {
        "jd": (".job-detail .job_bt", ".job-detail-section", "[class*=job-detail]"),
        "title": (".position-head .name", ".job-name", "h1"),
        "company": (".company", ".job_company .fl-cn", "[class*=company]"),
        "salary": (".salary", ".position-head .salary"),
        "meta": (".job_request", ".position-head .job-request"),
    },
    "51job": {
        "jd": (".job_msg", ".bmsg", "[class*=job-detail]"),
        "title": (".cn h1", "h1"),
        "company": (".cname", "[class*=company]"),
        "salary": (".cn strong", "[class*=salary]"),
        "meta": (".msg", ".job_request"),
    },
}

#: JD 正文的**合理长度门槛**：太短的多半是"相关推荐""页脚"这类噪音
_MIN_JD_LENGTH = 60


def detect_platform(url: str) -> str:
    """按域名判断平台；认不出就返回 `official`（公司官网/其他）。"""
    if not url:
        return "official"
    lowered = url.lower()
    for platform, hosts in PLATFORM_BY_HOST.items():
        if any(host in lowered for host in hosts):
            return platform
    return "official"


def extract_platform_job_id(url: str, platform: str | None = None) -> str | None:
    """从 URL 里抠出平台内岗位 ID（去重第一层）。

    ★ 拿不到时返回 None，**绝不拿 URL 本身当 ID**：
      URL 里常带 `?lid=`、`&securityId=`、时间戳等会话参数，
      同一个岗位两次访问的 URL 可能不同 —— 用 URL 当 ID 会产生假重复/假新增。
      拿不到就交给第三层的归一化 hash 兜底（这也是它存在的意义）。
    """
    if not url:
        return None
    platform = platform or detect_platform(url)
    for pattern in PLATFORM_JOB_ID_PATTERNS.get(platform, ()):
        match = pattern.search(url)
        if match:
            return match.group("id")
    return None


# ---------------------------------------------------------------------------
# 从 HTML 抽字段
# ---------------------------------------------------------------------------
#: `class="xxx"` / `id="xxx"` 的属性值提取（不引入 HTML 解析库，代价最小）
_ATTR_VALUE_RE = re.compile(r"""(?:class|id)\s*=\s*["'](?P<value>[^"']+)["']""", re.IGNORECASE)
#: 页面标题 `<title>…</title>`（很多站点的 title 就是"岗位-公司-薪资"）
_TITLE_TAG_RE = re.compile(r"(?is)<title[^>]*>(?P<text>.*?)</title>")
#: `<meta property="og:title" content="…">`（两种属性顺序都要认）
_META_RE = re.compile(
    r"""(?is)<meta[^>]+(?:property|name)\s*=\s*["'](?P<key>[^"']+)["'][^>]*"""
    r"""content\s*=\s*["'](?P<value>[^"']*)["']"""
)
_META_RE_REVERSED = re.compile(
    r"""(?is)<meta[^>]+content\s*=\s*["'](?P<value>[^"']*)["'][^>]*"""
    r"""(?:property|name)\s*=\s*["'](?P<key>[^"']+)["']"""
)
#: JSON-LD 结构化数据块（schema.org）
_JSONLD_RE = re.compile(
    r'(?is)<script[^>]+type\s*=\s*["\']application/ld\+json["\'][^>]*>(?P<body>.*?)</script>'
)
#: schema.org 里我们要的岗位类型
_JSONLD_JOB_TYPES = frozenset({"JobPosting"})


def _meta_map(html: str) -> dict[str, str]:
    """把页面里的 `<meta>` 收集成 `{property/name(小写): content}`。"""
    metas: dict[str, str] = {}
    for pattern in (_META_RE, _META_RE_REVERSED):
        for match in pattern.finditer(html or ""):
            key = match.group("key").strip().lower()
            value = match.group("value").strip()
            if key and value and key not in metas:
                metas[key] = value
    return metas


def _flatten_jsonld(data: Any) -> list[dict[str, Any]]:
    """把 JSON-LD 展开成对象列表（处理数组与 `@graph` 嵌套）。"""
    if isinstance(data, list):
        flattened: list[dict[str, Any]] = []
        for item in data:
            flattened.extend(_flatten_jsonld(item))
        return flattened
    if not isinstance(data, dict):
        return []
    objects = [data]
    graph = data.get("@graph")
    if graph is not None:
        objects.extend(_flatten_jsonld(graph))
    return objects


def _iter_jsonld_objects(html: str) -> list[dict[str, Any]]:
    """解析页面里所有 JSON-LD 块，返回其中的对象列表。"""
    objects: list[dict[str, Any]] = []
    for match in _JSONLD_RE.finditer(html or ""):
        body = match.group("body").strip()
        if not body:
            continue
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            # 有些站点会在 JSON-LD 里留注释或尾随逗号 → 跳过这块，不影响其他来源
            continue
        objects.extend(_flatten_jsonld(data))
    return objects


def _pick_jsonld_job(html: str) -> dict[str, Any] | None:
    """挑出 JSON-LD 里的 `JobPosting` 对象。

    ★ 为什么值得专门做这一步（真实站点实测的结论）：

        现代招聘站（字节、腾讯、美团这类）都是 SPA，字段由 JS 渲染，
        服务端 HTML 里**没有**干净的 DOM 可抽。实测后果：
            · 公司名抽成了页面上的其他文字；
            · 标题抽成了导航词；
            · JD 只能退回"整页文本"，把导航/页脚/时间戳一起算进去。

        其中一部分站点会输出 **schema.org 的 `JobPosting` JSON-LD**（为了搜索引擎收录），
        那是公开标准、字段语义明确（title / hiringOrganization / jobLocation /
        description / employmentType …），而且**改版免疫**。

        ⚠️ 它只是**优先来源**，不是唯一来源：拿不到时仍走 DOM 选择器与整页兜底。

    ★★ 必须校验 `@type`，不能"解析出 dict 就用"（实测踩过）：
        字节的页面里那个 `application/ld+json` 块**其实是段 JS**
        （`(function(nav){…})`）。用正则 `json.loads` 当然会失败，
        但要是哪天它恰好是合法 JSON（比如 `{}` 或某个配置对象），
        "有 dict 就用"就会把垃圾当字段来源 —— 所以这里强制要求 `@type` 命中 JobPosting。
    """
    best: dict[str, Any] | None = None
    for obj in _iter_jsonld_objects(html):
        type_value = obj.get("@type")
        types = {type_value} if isinstance(type_value, str) else set(type_value or [])
        if not (types & _JSONLD_JOB_TYPES):
            continue
        if obj.get("title") or obj.get("description"):
            return obj
        best = best if best is not None else obj
    return best


#: "看起来像薪资"的判断（用于解析不出月薪数值时，仍把原文留在 `salary_text` 里）
_SALARY_HINT_RE = re.compile(r"\d")


def _looks_like_salary_text(text: str) -> bool:
    """这段文字像不像薪资？

    ★ 门槛刻意定得低（有数字 + 有个薪资单位/字），因为它的用途是
      "**留原文**"而不是"算数"：日薪（`100-180元/天`）、年薪、`薪资面议`
      这类形态算不出月薪，但丢掉就等于告诉用户"页面上没写薪资"。
      真正需要防的是把"公司规模 20-99人"当薪资 —— 那种不含上面的单位词。
    """
    if not text or len(text) > 40 or not _SALARY_HINT_RE.search(text):
        return False
    return any(word in text for word in ("元", "K", "k", "万", "薪", "面议", "/天", "/月", "/年"))


def _salary_from_dom(html: str) -> str:
    """从页面上"看起来是薪资"的元素里取文本（比全文搜数字可靠）。

    ★ 为什么要有这一档（实测踩过）：原来直接在**整页 HTML** 上跑薪资正则，
      结果把页面里无关的 `4K`（图片/分辨率之类的字样）当成了月薪写进库。
      先在 class/id 含 salary/薪资/工资 的元素里找，命不中才退回全文，
      可以把这类误判压到最低。
    """
    for token in ("salary", "job-salary", "salary-text", "薪资", "工资"):
        for text in _text_of_elements_with_class(html, token):
            cleaned = strip_html(text)
            if cleaned and any(ch.isdigit() for ch in cleaned):
                return cleaned
    return ""


def _jsonld_location_text(job: dict[str, Any]) -> str:
    """把 `jobLocation` 压成文本（可能是字符串、对象或数组）。"""

    def _extract(node: Any) -> list[str]:
        if isinstance(node, str):
            return [node]
        if isinstance(node, list):
            parts: list[str] = []
            for item in node:
                parts.extend(_extract(item))
            return parts
        if isinstance(node, dict):
            address = node.get("address", node)
            if isinstance(address, dict):
                candidates = (
                    address.get("addressLocality"),
                    address.get("addressRegion"),
                    address.get("addressCountry"),
                )
                return [str(value) for value in candidates if value]
            if isinstance(address, str):
                return [address]
        return []

    return " ".join(_extract(job.get("jobLocation"))).strip()


def _jsonld_salary_text(job: dict[str, Any]) -> str:
    """把 `baseSalary` 压成一段可读文本（数值化仍交给 parse_text）。"""
    salary = job.get("baseSalary")
    if isinstance(salary, str):
        return salary
    if not isinstance(salary, dict):
        return ""

    value = salary.get("value", salary)
    if not isinstance(value, dict):
        return str(value)

    unit = salary.get("currency") or value.get("unitText") or ""
    low = value.get("minValue") or value.get("value")
    high = value.get("maxValue")
    if low is None:
        return ""
    if high and high != low:
        return f"{low}-{high}{unit}"
    return f"{low}{unit}"


def _text_of_elements_with_class(html: str, class_token: str) -> list[str]:
    """取出"class 含该 token"的所有元素的内层文本（含嵌套标签）。

    实现说明：用正则找开标签，再手工配对同名标签计数直到闭合。
    这对"嵌套同标签"（如 div 里套 div）是必要的 —— 简单的非贪婪匹配会在
    第一个内层闭合标签处截断，导致 JD 正文被砍掉一半（真实页面里太常见）。
    """
    token = class_token.lower()
    texts: list[str] = []
    for match in re.finditer(r"(?is)<(?P<tag>div|section|article|p|span|td)\b[^>]*>", html):
        tag = match.group("tag").lower()
        opening = match.group(0)
        attrs = _ATTR_VALUE_RE.search(opening)
        if attrs is None:
            continue
        classes = [part.lower() for part in attrs.group("value").split()]
        if token not in classes:
            continue
        start = match.end()
        depth = 1
        cursor = start
        # 手工配对同名标签，避免在嵌套结构里截断
        for step in re.finditer(rf"(?is)</?{tag}\b[^>]*>", html[start:]):
            if step.group(0).startswith("</"):
                depth -= 1
            else:
                depth += 1
            cursor = start + step.end()
            if depth == 0:
                break
        texts.append(html[start : cursor - len(f"</{tag}>")])
    return texts


def _longest_meaningful(texts: list[str], *, min_length: int = _MIN_JD_LENGTH) -> str | None:
    """从候选里挑"最长的、够长的那块"。

    ★ 这是本模块最重要的一个选择：JD 正文的**长度特征**比任何选择器都稳定。
      招聘页面到处都在重复"职位描述"这四个字（导航 / 推荐 / 页脚），
      按选择器挑很容易挑到推荐位的短文本；而正文永远是页面上最长的一段结构化文字。
    """
    cleaned = [strip_html(text) for text in texts]
    candidates = [text for text in cleaned if len(text) >= min_length]
    if not candidates:
        return None
    return max(candidates, key=len)


# ===========================================================================
# "这页不是岗位页" 的识别（ISSUES #62）
# ===========================================================================
#: 登录墙 / 安全验证页在**标题**上的特征词
_ACCESS_WALL_TITLE_MARKERS: tuple[str, ...] = (
    "注册登录",
    "登录",
    "安全验证",
    "验证码",
    "sign in",
    "log in",
    "verify",
)

#: 登录墙 / 安全验证页在**正文**上的特征词（比标题更硬的证据）
_ACCESS_WALL_TEXT_MARKERS: tuple[str, ...] = (
    "APP扫码登录",
    "扫码登录",
    "验证码登录",
    "发送验证码",
    "登录/注册",
    "微信登录",
    "登录后查看",
    "请先登录",
    "安全验证",
    "异常访问行为",
    "拖动滑块",
)

#: "这确实是一个岗位页"的结构证据 —— 这些出现任意一个，就不判成墙。
_JOB_STRUCTURE_MARKERS: tuple[str, ...] = (
    "job-sec",
    "job-title",
    "job-primary",
    "job-detail",
    "job-desc",
    'data-test="jobTitle"',
    "data-test='jobTitle'",
    "job-banner",
    "company-info",
)


def detect_access_wall(
    html: str,
    *,
    rendered_text: str | None = None,
    platform: str | None = None,
) -> str | None:
    """判断"这一页不是岗位页，而是登录墙 / 安全验证页"，返回**可操作**的说明。

    ★★ 为什么必须有这个函数（ISSUES #62，🔴）

      BOSS 的岗位详情页**需要登录**才给看。抓取路径的表现是：

        · 渲染后"最长文本块"是 0（真正的 JD 根本没渲染出来）；
        · 于是 `jd_text` 走了"整页文本兜底"，把**登录页的正文**当成了 JD（312 字）；
        · 公司 / 职位 / 城市全空，`fetch_status=partial` —— 而 CLI 只在
          `failed` 时才拦下来 → **partial 照常入库，还打印「✓ 入库完成」**。

      后果比"没抓到"严重得多：这段登录页文本会被 `jh match run` 打分、
      被 `jh today` 拿去生成开场白、被 `jh tailor` 拿去做定制简历，
      而**用户从输出里看不出任何异常**。这是本项目最不能接受的一类：
      「不报错、报成功、存的却是垃圾」。

    ★ 判据是**组合**的（单看哪一条都会误伤）：

        ① 命中登录/验证特征词（标题或正文）；
        ② **且**页面里没有任何岗位结构证据（`job-sec` / `job-title` /
           `data-test=jobTitle` / company-info …）。

      只满足 ① 不算墙：真实岗位页的正文里完全可能出现"登录后投递"这种句子 ——
      而它有岗位结构，所以不会被误判。反过来，只有 ② 也不算：抓取失败/改版的
      页面同样没有这些 class，但它不是"墙"，该走原来的"没抽到正文"提示。

    Args:
        html: 原始 HTML（本地文件或抓回来的都行）
        rendered_text: 浏览器渲染后的正文（可选）。有它更准 ——
            登录墙的"正文"往往是客户端渲染出来的，原始 HTML 里可能一个字都没有。
        platform: 已知平台（只用于把提示写得更具体，比如"BOSS直聘"）

    Returns:
        命中时返回一句**中文可操作**的说明（给 CLI 直接打印），没命中返回 None。
    """
    body = strip_html(html or "")
    if rendered_text:
        body = f"{body}\n{rendered_text}"

    title = _page_title(html or "") or ""

    hit_title = [marker for marker in _ACCESS_WALL_TITLE_MARKERS if marker in title]
    hit_text = [marker for marker in _ACCESS_WALL_TEXT_MARKERS if marker in body]
    if not hit_title and not hit_text:
        return None

    lowered = (html or "").lower()
    if any(marker.lower() in lowered for marker in _JOB_STRUCTURE_MARKERS):
        # 有岗位结构 → 这是一张真岗位页（正文里出现"登录"是正常的）
        return None
    if _pick_jsonld_job(html or "") is not None:
        return None

    who = _platform_label(platform)
    signals = "、".join(dict.fromkeys([*hit_title, *hit_text]))[:60]
    return (
        f"{who}的岗位详情需要**登录**才能看，抓到的是登录/验证页而不是 JD。\n"
        f"    识别依据：页面里出现了「{signals}」，且没有任何岗位结构"
        "（job-sec / job-title / data-test=jobTitle / JSON-LD 都没有）。\n"
        "    两条不用绕过登录的路：\n"
        "      · 在浏览器里打开这个岗位 → Ctrl+S 另存为网页 →\n"
        '          jh job add --html 页面.html --url "<原链接>" --platform boss\n'
        "      · 或者直接复制 JD 正文 →\n"
        '          jh job add --manual --url "<原链接>" --platform boss'
    )


def _platform_label(platform: str | None) -> str:
    """平台名给人看的写法（提示语里用）。"""
    labels = {"boss": "BOSS直聘", "official": "该公司官网", "manual": "这个页面"}
    return labels.get(platform or "", "这个站点")


def parse_detail_from_html(
    html: str,
    url: str,
    *,
    platform: str | None = None,
) -> JobDetail:
    """从 HTML 抽出一个 `JobDetail`。

    策略（按可靠性排序）：
        ① 平台特定的选择器（命中即用，但仍要求 JD 正文够长）；
        ② 通用兜底：在"class 里含 job/detail/desc/intro/content"的块里挑最长的；
        ③ 再兜底：整页可见文本（至少保证"有东西可解析"，并标注 fetch_status=partial）。

    ★ `fetch_status` 一定要如实标注：
      `ok`（拿到了像样的 JD）/ `partial`（只拿到一部分）/ `failed`（啥也没拿到）。
      抓取失败还标 `ok` 的话，P3 会基于空文本打分，而用户完全不知道为什么分数低。
    """
    platform = platform or detect_platform(url)
    job_id = extract_platform_job_id(url, platform)

    normalizer = html or ""
    title = _page_title(normalizer)
    metas = _meta_map(normalizer)
    structured = _pick_jsonld_job(normalizer)

    # ---- JD 正文：① JSON-LD → ② 平台选择器/通用候选（取最长）→ ③ 整页兜底 -----
    jd_text: str | None = None
    fetch_status = "ok"

    if structured is not None:
        jd_text = _longest_meaningful([str(structured.get("description") or "")])
        if jd_text:
            fetch_status = "ok"

    if not jd_text:
        selectors = PLATFORM_SELECTORS.get(platform, {})
        jd_candidates: list[str] = []
        for selector in selectors.get("jd", ()):
            # 选择器形如 ".job-sec-text" → 取所有匹配 class 的内层文本
            token = selector.lstrip(".").split()[0]
            if selector.startswith("."):
                jd_candidates.extend(_text_of_elements_with_class(normalizer, token))
        # 通用兜底：class 里含这些词的块
        for token in (
            "job-sec-text",
            "job-detail",
            "job-desc",
            "job_bt",
            "job_msg",
            "description",
        ):
            jd_candidates.extend(_text_of_elements_with_class(normalizer, token))
        jd_text = _longest_meaningful(jd_candidates)

    if not jd_text:
        # 再退一步：整页纯文本（去掉脚本与样式之后仍然够长就用它）
        plain = strip_html(normalizer)
        jd_text = plain
        fetch_status = "partial" if len(plain) >= _MIN_JD_LENGTH else "failed"

    # ---- ★★ 这一步必须在"整页兜底"之后（ISSUES #62）------------------------
    #   登录墙的正文恰恰是**够长**的（312 字），所以它会安静地走完整页兜底、
    #   拿到 partial 并被入库。这里在返回之前再判一次"这到底是不是岗位页"，
    #   命中就把状态压成 failed 并**清掉正文** —— 宁可"没抓到"，
    #   也不能让登录页文本流进 match / today / tailor。
    wall_note = detect_access_wall(normalizer, platform=platform)
    if wall_note:
        jd_text = ""
        fetch_status = "failed"

    # ---- 结构化字段：JSON-LD 优先，其次 DOM/title/meta 启发式 -----------------
    company, title_text, city_text, salary_source = _resolve_basic_fields(
        normalizer, title, metas, structured
    )

    # 薪资：优先用 JSON-LD / 页面上的薪资元素；都没有就**在纯文本里**找。
    #
    # ★★ 绝不能把原始 HTML 喂给 extract_salary（实测踩过的坑）：
    #   美团页面 1.6 MB 的 HTML 里有大量 SVG 路径与内联样式数据，
    #   里面恰好出现 `1k` 这样的片段，于是薪资被抽成 `1k`（1000 元/月）——
    #   而那个岗位页面**根本没写薪资**。先 strip_html 再抽，就不会误判。
    #
    # ★★ 城市/经验/学历也一律在**主区域**里抽（ISSUES #64）：
    #   改前用的是 `normalizer[:4000]` —— 对真实页面那 4000 字符**整个是 <head>**
    #   （script/link/meta），所以 city / salary 永远是 None。
    #   BOSS 真实页面上城市与经验就在 `class="info-primary"` 里的
    #   `<a class="text-desc text-city">深圳</a>` / `<span class="text-desc text-experiece">`。
    region = _main_job_region(normalizer)
    region_text = strip_html(region)

    if not salary_source:
        salary_source = _salary_from_dom(region)
    # ★ 找不到薪资元素时，退回"主区域文本 + 页面 title" ——
    #   BOSS 的 `<title>` 就写着 `岗位_公司_15-25K_深圳_招聘`，那是**这个岗位**的薪资；
    #   而**绝不**退到整页文本：那会把"相似职位"里的薪资抽过来（#64 的教训）。
    salary_text, salary_min, salary_max = extract_salary(
        salary_source or f"{region_text}\n{title or ''}"
    )
    if not salary_text and salary_source:
        # ★ 解析不出"月薪数值"时，把**原样的薪资文本**留着（ISSUES #64 顺带修的）：
        #   实习岗常写日薪（`100-180元/天`）、也有年薪/面议，
        #   `extract_salary` 只认"能换算成月薪"的形态，于是这些会整条丢掉 ——
        #   用户在表格里看到的是"薪资 —"，而网页上明明写着。
        #   数值列（salary_min/max）保持 None 是**对的**（日薪≠月薪，不能硬换），
        #   但字符串列应当如实留下原文。
        candidate = strip_html(salary_source).strip().split("\n")[0].strip()
        if _looks_like_salary_text(candidate):
            salary_text = candidate

    return JobDetail(
        platform=platform,
        jd_text=jd_text or "",
        platform_job_id=job_id,
        company=company,
        title=title_text,
        city=city_text or extract_city(region_text) or extract_city(title or ""),
        experience=extract_experience(region_text),
        education=extract_education(region_text),
        is_intern=looks_intern_from(region, structured, title_text, jd_text),
        salary_text=salary_text,
        salary_min=salary_min,
        salary_max=salary_max,
        url=url,
        fetch_status=fetch_status,
        # ★ 命中登录墙时把说明带上：CLI 直接打印它（用户看到的必须是
        #   "为什么没抓到 + 接下来怎么办"，而不是一句笼统的失败）
        fetch_note=wall_note,
        raw={
            "title_tag": title or "",
            "jd_length": len(jd_text or ""),
            "jsonld": structured is not None,
            # 留痕：事后排查"这条为什么没入库"时，一眼能看出是登录墙
            "access_wall": bool(wall_note),
        },
    )


def _resolve_basic_fields(
    html: str,
    page_title: str | None,
    metas: dict[str, str],
    structured: dict[str, Any] | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """解析公司 / 职位 / 城市 / 薪资文本，按可靠性从高到低取。

    Returns:
        `(company, title, city, salary_source)`

    优先级（每一档都是"有就用、没有就下一档"）：
        ① **JSON-LD**（`hiringOrganization` / `title` / `jobLocation` / `baseSalary`）
           —— 机器可读、语义明确、改版免疫；
        ② DOM 选择器（公司区块 / `h1`）；
        ③ `<meta og:title>` 与 `<title>`（`岗位_公司_薪资_城市_招聘` 这种写法）；
        ④ 城市：JSON-LD 的地点 → 全文认城市。
    """
    company: str | None = None
    title: str | None = None
    city: str | None = None
    salary_source: str | None = None

    if structured is not None:
        raw_title = structured.get("title")
        title = str(raw_title).strip() if raw_title else None

        organization = structured.get("hiringOrganization")
        if isinstance(organization, dict):
            name = organization.get("name")
            company = str(name).strip() if name else None
        elif isinstance(organization, str):
            company = organization.strip() or None

        location_text = _jsonld_location_text(structured)
        if location_text:
            city = extract_city(location_text)

        salary_text = _jsonld_salary_text(structured)
        if salary_text:
            salary_source = salary_text

    company = company or _guess_company(html, page_title)
    title = title or _guess_job_title(html, page_title)

    # `<meta og:title>` 常比 `<title>` 干净（没有站点后缀）
    if not title:
        og_title = metas.get("og:title") or metas.get("twitter:title")
        if og_title:
            parts = _split_page_title(og_title)
            title = parts[0] if parts else og_title
    if not company:
        og_site = metas.get("og:site_name")
        # og:site_name 常常是"BOSS直聘"这种站点名，不能当公司名 →
        # 只在与页面 title 的分段结果一致时才用（那是真公司名的强信号）
        parts = _split_page_title(page_title)
        if og_site and len(parts) >= 2 and og_site.strip() == parts[1]:
            company = og_site.strip()

    return company, title, city, salary_source


def looks_intern_from(
    html: str,
    structured: dict[str, Any] | None,
    title: str | None,
    jd_text: str | None,
) -> bool | None:
    """判断是否实习岗：JSON-LD 的 `employmentType` 优先，其次标题与正文。"""
    if structured is not None:
        employment = structured.get("employmentType")
        if isinstance(employment, list):
            employment = " ".join(str(item) for item in employment)
        if isinstance(employment, str) and employment:
            lowered = employment.lower()
            if "intern" in lowered:
                return True
            if "full" in lowered or "part" in lowered or "contract" in lowered:
                return False
    return looks_like_intern(f"{title or ''} {jd_text or ''}")


def parse_detail_from_text(
    text: str,
    *,
    url: str | None = None,
    platform: str = "manual",
    title: str | None = None,
    company: str | None = None,
) -> JobDetail:
    """从**纯文本**构造 `JobDetail`（`--manual` 手动粘贴走这条路）。

    与 `parse_detail_from_html` 的区别：不再做标签抽取，
    直接把整段文本当 JD 正文，然后抽取结构化字段。
    用户可以自己写好 `公司：xxx` / `职位：xxx` 这样的行，这里会识别。
    """
    normalized = normalize_text(text)
    labeled_company, labeled_title = _parse_labeled_fields(text)

    salary_text, salary_min, salary_max = extract_salary(text)
    resolved_title = title or labeled_title or _first_meaningful_line(text)
    resolved_company = company or labeled_company

    return JobDetail(
        platform=platform,
        jd_text=text.strip(),
        platform_job_id=extract_platform_job_id(url or "", platform) if url else None,
        company=resolved_company,
        title=resolved_title,
        city=extract_city(text[:2000]),
        experience=extract_experience(text[:2000]),
        education=extract_education(text[:2000]),
        is_intern=looks_like_intern(f"{resolved_title or ''}\n{text}"),
        salary_text=salary_text,
        salary_min=salary_min,
        salary_max=salary_max,
        url=url,
        fetch_status="ok" if normalized else "failed",
        raw={"source": "manual"},
    )


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _page_title(html: str) -> str | None:
    match = _TITLE_TAG_RE.search(html or "")
    if not match:
        return None
    text = strip_html(match.group("text"))
    return text or None


#: 页面 title 的常见写法：`岗位-公司-薪资-城市-招聘` 或 `岗位_公司_薪资_城市_招聘`。
#: ★ 分隔符要把**下划线**与**全角空格**都收进来 —— BOSS 用 `岗位_公司_…-BOSS直聘`，
#:   字节用 `岗位 - 公司 - 站点名`（实测两种都遇到过）。
_TITLE_SPLIT_RE = re.compile(r"\s*[-–—_|｜·]\s*")

#: 招聘站点自己的名字。它们出现在 title 的**末段**，不是公司名。
#:
#: ★ 为什么需要这个名单（实测踩过）：`<title>` 常写成
#:   `岗位 - 公司 - 站点名`。如果不识别站点名，就会把"公司"取错位置 ——
#:   实测把 `豆包AI大模型产品解决方案实习生 - 火山方舟 - 字节跳动` 里的公司
#:   取成了"火山方舟"（那是**团队/业务线**），而正确公司名是最后一段"字节跳动"。
_SITE_NAMES = (
    "字节跳动",
    "boss直聘",
    "boss",
    "拉勾",
    "拉勾网",
    "前程无忧",
    "智联招聘",
    "猎聘",
    "猎聘网",
    "脉脉",
    "linkedin",
    "领英",
    "看准网",
    "实习僧",
    "牛客",
    "牛客网",
    "大街网",
    "中华英才网",
    "智联",
    "58同城",
    "招聘",
    "官网",
    "首页",
    "校园招聘",
    "社会招聘",
)


def _is_site_name(text: str) -> bool:
    """判断一段文字是不是"招聘站点自己的名字"。

    ★★ 必须是**整段相等**，不能是"包含"（ISSUES #64 踩到的真 bug）：

      改前这里写的是 `any(name in lowered for name in _SITE_NAMES)`，
      而名单里有 `"招聘"` 这个通用词 —— 于是**任何**含"招聘"两字的段落都被当成站点名丢掉：

          `「AI FDE 实习生【企业 AI 解决方案方向】招聘」_智玩店科技招聘-BOSS直聘`
          → 三段全被过滤 → 解析结果 `[]` → 公司名退化成"猜不出来"，
            连带 city / salary 也一起丢了（它们本来靠 title 兜底）。

      真实站点段的形态恰恰是"品牌 + 招聘"（`智联招聘`）或整段就是通用词（`校园招聘`），
      所以规则分成两条：
        · 通用词（招聘 / 官网 / 首页 / 校园招聘…）→ **必须整段相等**才算站点段；
        · 品牌名（BOSS直聘 / 字节跳动…）→ 整段相等，或"以它结尾"（`…BOSS直聘` 这种拼接）。
    """
    lowered = text.strip().lower()
    if not lowered:
        return False
    if lowered in _GENERIC_TITLE_WORDS:
        return True
    # ★ 通用词只能"整段相等"（上面那一行），**不能**参与"结尾匹配"：
    #   名单里有 `"招聘"`，一参与，"虚构科技招聘"就会被当成站点名丢掉（#64 原症状）。
    return any(
        lowered == name or (name not in _GENERIC_TITLE_WORDS and lowered.endswith(name))
        for name in _SITE_NAMES
    )


#: 这些词在 title 里是**通用词**，整段等于它们才算噪音段（不能"包含"就丢）。
#: ★ 为什么单独一张表：`招聘` 出现在公司名末尾是极常见的（`智玩店科技招聘`），
#:   一"包含"就丢会连公司名一起丢掉（#64）。
_GENERIC_TITLE_WORDS = (
    "招聘",
    "官网",
    "首页",
    "校园招聘",
    "社会招聘",
    "人才招聘",
    "职位",
)


def _clean_title_segment(part: str) -> str:
    """把 title 的一个分段清洗成"能当字段用"的样子。

    真实 title 的样式很花（ISSUES #64 的实例）：
        · 整段被引号包起来：`「AI FDE 实习生【企业 AI 解决方案方向】招聘」`
        · 末尾挂一个通用词：`智玩店科技招聘`

    所以这里去掉首尾的引号/空白，并剥掉**末尾**的通用词 ——
    但只在剥完还剩东西时才剥（否则"招聘"本身会被剥成空串）。
    """
    cleaned = part.strip().strip("「」『』\"'“”‘’").strip()
    for word in _GENERIC_TITLE_WORDS:
        if cleaned.endswith(word) and len(cleaned) > len(word):
            cleaned = cleaned[: -len(word)].strip("「」『』\"'“”‘’").strip()
            break
    return cleaned


def _split_page_title(title: str | None) -> list[str]:
    if not title:
        return []
    parts = [part.strip() for part in _TITLE_SPLIT_RE.split(title) if part.strip()]
    # 去掉站点名分段，再把剩下的清洗干净（去引号、剥末尾通用词）
    cleaned = [_clean_title_segment(part) for part in parts if not _is_site_name(part)]
    return [part for part in cleaned if part]


def _guess_job_title(html: str, title: str | None) -> str | None:
    """猜岗位名：① 主区域里的岗位名容器 → ② 页面 title 的第一段。

    ★ 为什么**不**直接取 `<h1>`（实测踩过）：很多站点的第一个 `<h1>` 是
      站点 logo / 导航（字节的岗位页第一个 h1 是"校园招聘"），
      直接把导航词当成岗位名写进库，而它是**完全错的字段**。

    ★★ 为什么还要"限定主区域"（ISSUES #64）：真实 BOSS 页面上，
      `class` 含 `job-title` 的元素**有好几个都在"相似职位"推荐位里**
      （推荐卡片的职位名），全页找第一个就可能抽到**别的岗位**的名。
      而当前岗位的名字在 `class="info-primary"` 里的 `<h1 title="…">` 上。

    所以顺序是：
        ① `info-primary` 里的 `h1[title]` 或 `h1`（真实 BOSS 结构，最准）
        ② `data-test="jobTitle"`（前端为自动化测试写的属性，最稳）
        ③ **主区域**内 class 含 `job-title` / `jobTitle` 的元素
        ④ 页面 `<title>` 的第一段
    """
    region = _main_job_region(html or "")

    # ① BOSS：`<div class="info-primary"> … <div class="name"><h1 title="岗位名">`
    for block in _text_of_elements_with_class(region, "info-primary"):
        match = re.search(
            r"""(?is)<h1\b[^>]*\btitle\s*=\s*["'](?P<attr>[^"']{2,120})["']"""
            r"""|<h1\b[^>]*>(?P<text>.*?)</h1>""",
            block,
        )
        if match:
            candidate = (match.group("attr") or strip_html(match.group("text") or "")).strip()
            candidate = candidate.split("\n")[0].strip()
            if candidate and 2 <= len(candidate) <= 120:
                return candidate

    for pattern in (
        r"""(?is)<[^>]+data-test\s*=\s*["'][^"']*job[-_]?title[^"']*["'][^>]*>(?P<text>.*?)</""",
        r"""(?is)<[^>]+class\s*=\s*["'][^"']*job[-_]?title[^"']*["'][^>]*>(?P<text>.*?)</""",
    ):
        for match in re.finditer(pattern, region):
            candidate = strip_html(match.group("text")).split("\n")[0].strip()
            # 过滤掉明显不是标题的（空的、或过长的整段文字）
            if candidate and 2 <= len(candidate) <= 80:
                return candidate

    parts = _split_page_title(title)
    return parts[0] if parts else None


def _guess_company(html: str, title: str | None) -> str | None:
    """猜公司名：**只在当前岗位那一段里找**，最后才用页面 title 兜底。

    ★★ 为什么必须限定"当前岗位那一段"（ISSUES #64，真实 BOSS 页面实测）

      改前是"全页找第一个 class 含 company-info / company-name 的元素"。
      而真实 BOSS 页面上：

        · `class="company-info"` 的**第一个**命中出现在"相似职位"推荐位里，
          它的内层文本以 `<div class="name">`（**那张卡片的职位名**）开头 ——
          于是公司名被抽成了**职位名**（`company == title`，看起来还挺像对的）；
        · `class="company-name"`（6 处）**全是推荐位里的别家公司**，
          一处都不是当前岗位的公司；
        · 真正的公司名在 `class="sider-company"` 里，而且写在 `<a title="…">` 属性上。

      所以规则改成"按可靠性排序 + 限定区域"：

        ① `sider-company` 里 `<a title="X">` 的 title 属性（BOSS 真实页面，最准）
        ② `sider-company` 的内层文本（去掉"公司基本信息"这类标签行）
        ③ **主区域**内的 company-info / company-name / company / cname
        ④ 页面 `<title>` 的公司段（`岗位_公司_招聘-BOSS直聘` 这种）

      `_main_job_region()` 会把推荐位切掉，所以 ③ 再也不会读到"别家公司"。
    """
    region = _main_job_region(html or "")

    # ① BOSS 真实页面：公司名在 sider 里的 a[title] 属性上
    #   （`_text_of_elements_with_class` 返回的是**内层 HTML**，所以能读属性）
    sider = "".join(_text_of_elements_with_class(region, "sider-company"))
    if sider:
        match = re.search(r'(?is)<a\b[^>]*\btitle\s*=\s*["\'](?P<name>[^"\']{2,80})["\']', sider)
        if match:
            candidate = _clean_company(match.group("name"))
            if candidate:
                return candidate

    # ② sider 的内层文本（跳过"公司基本信息"这类标签行）
    if sider:
        for line in strip_html(sider).split("\n"):
            candidate = _clean_company(line)
            if candidate:
                return candidate

    # ③ 主区域内的候选 token（顺序即优先级）
    for token in ("company-info", "company-name", "company_name", "cname", "company"):
        for text in _text_of_elements_with_class(region, token):
            candidate = _clean_company(strip_html(text).split("\n")[0])
            if candidate:
                return candidate

    # ④ 页面 title 的公司段
    parts = _split_page_title(title)
    if len(parts) >= 2 and parts[-1]:
        return _clean_company(parts[-1])
    return None


#: 公司名里不该出现的"标签行"（真实页面里它们是同一个容器里的标题）
_COMPANY_LABEL_WORDS = ("公司基本信息", "公司简介", "基本信息", "公司信息", "工商信息")


def _clean_company(text: str) -> str | None:
    """把候选文本整理成公司名；明显不是公司名的返回 None。

    ★ 为什么要有它：真实页面里同一个容器里往往混着"公司基本信息"这种标题、
      以及"未融资 20-99人 互联网"这类规模信息。直接当公司名存进去，
      会污染 `name_norm`（去重键）—— 那正是 #64 说"公司名是去重键"的意思。
    """
    cleaned = text.strip().strip("「」『』\"'“”‘’").strip()
    if not cleaned or cleaned in _COMPANY_LABEL_WORDS:
        return None
    if len(cleaned) > 60:
        return None
    # 规模/融资信息（"未融资 20-99人 互联网"）不是公司名
    if re.search(r"\d+\s*-\s*\d+\s*人", cleaned) or "未融资" in cleaned or "已上市" in cleaned:
        return None
    return cleaned


#: 推荐位 / 相似职位的标志。它们**之后**的内容不是当前岗位的信息。
#: ★ 实测来源（ISSUES #64）：BOSS 真实页面上 `company-name` 全在推荐位里。
_RECOMMEND_MARKERS: tuple[str, ...] = (
    "similar-job",
    "job-recommend",
    "recommend-job",
    "job-list-wrapper",
    "guess-like",
    "job-card-wrapper",
)

#: 主区域最多截多长（真实页面 450KB，主区域通常几 KB；这个上限只防病态输入）
_MAIN_REGION_LIMIT = 400_000

#: 主区域可能的起点（按"最先出现"取）。
#: ★ 名单要**宽**一些：各站的容器名不同（BOSS 用 `job-primary`/`info-primary`，
#:   其它站用 `job-banner` / `job-header` / `job-box`），漏一个就会把
#:   "薪资/城市"这类字段切到区域之外（实测踩过：fixture 用的是 `job-banner`）。
_MAIN_REGION_STARTS: tuple[str, ...] = (
    'class="job-primary',
    "class='job-primary",
    'class="info-primary',
    "class='info-primary",
    'class="job-banner',
    "class='job-banner",
    'class="job-header',
    "class='job-header",
    'class="job-box',
    "class='job-box",
    'class="job-detail',
    "class='job-detail",
    'class="job-sider',
    "class='job-sider",
    'class="job-sec',
    "class='job-sec",
)


def _main_job_region(html: str) -> str:
    """截出"当前岗位"那一段（把相似职位/推荐位排除在外）。

    ★ 为什么需要它（ISSUES #64）：真实招聘页把"当前岗位"和"相似职位推荐"
      放在同一个文档里，而且**两边的类名会撞**（BOSS 上 `company-name`
      只出现在推荐位里，`company-info` 的第一处命中也在推荐位）。
      正则解析没有 DOM 作用域，所以只能靠"区域切分"来补这一课。

    实现：从第一个主区域标志开始，切到**其后**第一个推荐位标志之前。
    找不到起点就返回整页（保持旧行为，不会更差）；找不到推荐位标志就切到结尾。
    """
    if not html:
        return ""

    start = -1
    for marker in _MAIN_REGION_STARTS:
        index = html.find(marker)
        if index >= 0 and (start < 0 or index < start):
            start = index
    if start < 0:
        return html[:_MAIN_REGION_LIMIT]

    end = len(html)
    for marker in _RECOMMEND_MARKERS:
        index = html.find(marker, start)
        if index >= 0:
            end = min(end, index)
    return html[start : min(end, start + _MAIN_REGION_LIMIT)]


#: 用户手写字段：`公司：xxx` / `职位：xxx` / `岗位：xxx`
_LABELED_FIELD_RE = re.compile(
    r"(?m)^\s*(?P<label>公司|企业|单位|职位|岗位|职位名称|岗位名称)\s*[:：]\s*(?P<value>.+?)\s*$"
)


def _parse_labeled_fields(text: str) -> tuple[str | None, str | None]:
    """解析用户手写的 `公司：` / `职位：` 行。"""
    company = title = None
    for match in _LABELED_FIELD_RE.finditer(text or ""):
        label = match.group("label")
        value = match.group("value").strip()
        if not value:
            continue
        if label in ("公司", "企业", "单位") and company is None:
            company = value
        elif label in ("职位", "岗位", "职位名称", "岗位名称") and title is None:
            title = value
    return company, title


def _first_meaningful_line(text: str) -> str | None:
    """取第一行有意义的文本（手动粘贴时把它当岗位名）。

    ★ 限制长度（<= 30 字）：第一行往往是"岗位名"或"公司名"，
      而如果用户粘贴的是整段 JD，第一行可能很长 —— 那种情况下返回 None，
      让岗位名留空（留空比填一段 JD 摘要诚实）。
    """
    for line in (text or "").splitlines():
        cleaned = normalize_text(line)
        if not cleaned:
            continue
        if len(cleaned) <= 30:
            return cleaned
        return None
    return None

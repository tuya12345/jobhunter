"""从 HTML 抽 `JobDetail`（纯函数，可用 mock HTML 测试）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 分层：**抓取**（需要浏览器/网络）与**解析**（纯函数）分开                   ║
╚══════════════════════════════════════════════════════════════════════════╝

    浏览器（adapters/manual.py, Playwright）
        └─ 交出可信的「页面标题 + 可见文本 + 关键区域的 HTML」
              └─ 本模块：把文本解析成 JobDetail（**不碰网络、不需要浏览器**）
                    └─ services/job_svc.py：入库 + 两层去重

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
      拿不到就交给第二层的归一化 hash 兜底（这也是它存在的意义）。
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
    if not salary_source:
        salary_source = _salary_from_dom(normalizer)
    salary_text, salary_min, salary_max = extract_salary(salary_source or strip_html(normalizer))

    return JobDetail(
        platform=platform,
        jd_text=jd_text or "",
        platform_job_id=job_id,
        company=company,
        title=title_text,
        city=city_text or extract_city(normalizer[:4000]) or extract_city(title or ""),
        experience=extract_experience(normalizer[:4000]),
        education=extract_education(normalizer[:4000]),
        is_intern=looks_intern_from(normalizer, structured, title_text, jd_text),
        salary_text=salary_text,
        salary_min=salary_min,
        salary_max=salary_max,
        url=url,
        fetch_status=fetch_status,
        raw={
            "title_tag": title or "",
            "jd_length": len(jd_text or ""),
            "jsonld": structured is not None,
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
    """判断一段文字是不是"招聘站点自己的名字"。"""
    lowered = text.strip().lower()
    return any(name in lowered for name in _SITE_NAMES)


def _split_page_title(title: str | None) -> list[str]:
    if not title:
        return []
    parts = [part.strip() for part in _TITLE_SPLIT_RE.split(title) if part.strip()]
    # 去掉明显的噪音段（"招聘""BOSS直聘"这类站点后缀）
    return [part for part in parts if not _is_site_name(part)]


def _guess_job_title(html: str, title: str | None) -> str | None:
    """猜岗位名：① 显式的岗位名容器 → ② 页面 title 的第一段。

    ★ 为什么**不**直接取 `<h1>`（实测踩过）：很多站点的第一个 `<h1>` 是
      站点 logo / 导航（字节的岗位页第一个 h1 是"校园招聘"），
      直接把导航词当成岗位名写进库，而它是**完全错的字段**。

      所以这里只认"语义明确的岗位名容器"：
        · `data-test="jobTitle"`（前端为自动化测试写的属性，最稳）
        · class 里明确含 `job-title` / `jobTitle` 的元素
      都没有时，退回页面 `<title>` 的第一段 —— 那是站点给搜索引擎看的，
      几乎所有招聘站都会写成"岗位名 - 公司 - 站点"。
    """
    for pattern in (
        r"""(?is)<[^>]+data-test\s*=\s*["'][^"']*job[-_]?title[^"']*["'][^>]*>(?P<text>.*?)</""",
        r"""(?is)<[^>]+class\s*=\s*["'][^"']*job[-_]?title[^"']*["'][^>]*>(?P<text>.*?)</""",
    ):
        for match in re.finditer(pattern, html or ""):
            candidate = strip_html(match.group("text")).split("\n")[0].strip()
            # 过滤掉明显不是标题的（空的、或过长的整段文字）
            if candidate and 2 <= len(candidate) <= 80:
                return candidate

    parts = _split_page_title(title)
    return parts[0] if parts else None


def _guess_company(html: str, title: str | None) -> str | None:
    """猜公司名：先看公司区块，再用页面 title 兜底。

    ★ title 的两种真实布局（实测）：
        · `岗位_公司_薪资_城市_招聘-BOSS直聘` → 第二段是公司
        · `岗位 - 公司 - 站点名`（字节）      → **去掉站点名后，最后一段是公司**
      站点名（"字节跳动""BOSS直聘"…）会被 `_split_page_title` 滤掉，
      所以规则可以统一成：**取"去掉站点名之后"的最后一段**。
      （第一段永远是岗位名，取最后一段对两种布局都成立。）
    """
    for token in ("company-info", "company-name", "company_name", "cname", "company"):
        for text in _text_of_elements_with_class(html or "", token):
            cleaned = strip_html(text)
            if cleaned:
                return cleaned.split("\n")[0].strip() or None

    parts = _split_page_title(title)
    if len(parts) >= 2 and parts[-1]:
        return parts[-1]
    return None


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

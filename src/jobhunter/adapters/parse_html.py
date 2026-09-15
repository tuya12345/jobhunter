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


def _selector_matches(html: str, selector: str) -> list[str]:
    """极简 CSS 选择器匹配（只支持项目里实际用到的三种写法）。

    支持：
        `.class`            按 class 精确包含
        `h1` / `div`        按标签名
        `[class*=xxx]`      按 class 子串

    ★ 为什么不用 BeautifulSoup / lxml.cssselect：
      这里是"尽力而为"的字段抽取，命中不了就走通用兜底策略。
      为它引入一个完整的 CSS 选择器引擎不划算（而且 lxml 已经作为 python-docx
      的依赖存在，但没有 cssselect 扩展）。真需要复杂选择器时，
      Playwright 侧本来就跑着完整浏览器，可以在那边做。
    """
    results: list[str] = []
    if selector.startswith("[class*="):
        needle = selector[len("[class*=") :].rstrip("]").strip("\"'")
        for match in _ATTR_VALUE_RE.finditer(html):
            if needle.lower() in match.group("value").lower():
                results.append(match.group("value"))
        return results
    if selector.startswith("."):
        needle = selector[1:].lower()
        for match in _ATTR_VALUE_RE.finditer(html):
            classes = [part.lower() for part in match.group("value").split()]
            if needle in classes:
                results.append(match.group("value"))
        return results
    # 标签名
    return [selector] if re.search(rf"(?i)<{re.escape(selector)}\b", html) else []


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
    salary_text, salary_min, salary_max = extract_salary(normalizer)

    selectors = PLATFORM_SELECTORS.get(platform, {})
    jd_candidates: list[str] = []
    for selector in selectors.get("jd", ()):
        # 选择器形如 ".job-sec-text" → 取所有匹配 class 的内层文本
        token = selector.lstrip(".").split()[0]
        if selector.startswith("."):
            jd_candidates.extend(_text_of_elements_with_class(normalizer, token))
    # 通用兜底：class 里含这些词的块
    for token in ("job-sec-text", "job-detail", "job-desc", "job_bt", "job_msg", "description"):
        jd_candidates.extend(_text_of_elements_with_class(normalizer, token))

    jd_text = _longest_meaningful(jd_candidates)
    fetch_status = "ok"

    if not jd_text:
        # 再退一步：整页纯文本（去掉脚本与样式之后仍然够长就用它）
        plain = strip_html(normalizer)
        if len(plain) >= _MIN_JD_LENGTH:
            jd_text = plain
            fetch_status = "partial"
        else:
            jd_text = plain
            fetch_status = "failed"

    company = _guess_company(normalizer, title)
    title_text = _guess_job_title(normalizer, title)

    return JobDetail(
        platform=platform,
        jd_text=jd_text or "",
        platform_job_id=job_id,
        company=company,
        title=title_text,
        city=extract_city(normalizer[:4000]) or extract_city(title or ""),
        experience=extract_experience(normalizer[:4000]),
        education=extract_education(normalizer[:4000]),
        is_intern=looks_like_intern(f"{title_text or ''} {jd_text or ''}"),
        salary_text=salary_text,
        salary_min=salary_min,
        salary_max=salary_max,
        url=url,
        fetch_status=fetch_status,
        raw={"title_tag": title or "", "jd_length": len(jd_text or "")},
    )


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


#: 页面 title 的常见写法：`岗位-公司-薪资-城市-招聘`。
#: ★ 分隔符要把**下划线**也收进来：BOSS 等站点用的是 `岗位_公司_薪资_城市_招聘-BOSS直聘`
#:   （实测：只按 `-` 切会把整个 title 当成一个字段，公司名直接抽不出来）。
_TITLE_SPLIT_RE = re.compile(r"\s*[-–—_|｜·]\s*")


def _split_page_title(title: str | None) -> list[str]:
    if not title:
        return []
    parts = [part.strip() for part in _TITLE_SPLIT_RE.split(title) if part.strip()]
    # 去掉明显的噪音段（"招聘""BOSS直聘"这类站点后缀）
    noise = ("招聘", "boss直聘", "boss", "拉勾", "前程无忧", "智联招聘", "猎聘", "官网", "首页")
    return [p for p in parts if not any(word in p.lower() for word in noise)]


def _guess_job_title(html: str, title: str | None) -> str | None:
    """猜岗位名：先看 `h1`，再用页面 title 的第一段。"""
    h1_match = re.search(r"(?is)<h1[^>]*>(?P<text>.*?)</h1>", html or "")
    if h1_match:
        text = strip_html(h1_match.group("text"))
        if text:
            return text.split("\n")[0].strip() or None
    parts = _split_page_title(title)
    return parts[0] if parts else None


def _guess_company(html: str, title: str | None) -> str | None:
    """猜公司名：先看公司区块，再用页面 title 的第二段兜底。

    ★ 为什么不只用 title：`某某科技` 这种简称在 title 里很常见，
      而页面上通常有完整公司名（`某某科技（深圳）有限公司`）。
      但**两个都不能保证存在**，所以按可靠性排：公司区块 → title。
    """
    for token in ("company-info", "company-name", "company_name", "cname", "company"):
        for text in _text_of_elements_with_class(html or "", token):
            cleaned = strip_html(text)
            if cleaned:
                return cleaned.split("\n")[0].strip() or None

    parts = _split_page_title(title)
    if len(parts) >= 2 and parts[1]:
        return parts[1]
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


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""

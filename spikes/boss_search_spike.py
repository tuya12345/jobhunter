"""自动搜索可行性 spike（**可选验证，不是产品功能**）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 这个脚本为什么存在                                                         ║
╚══════════════════════════════════════════════════════════════════════════╝

本项目最初的设计是"脚本自动搜索 + 翻页"，评审后改成了 **"人给链接 → 工具抓详情"**。
理由（写在 docs/architecture.md 里）：
    · BOSS 直聘有 `__zp_stoken__` 签名 + 字体反爬（把数字渲染成自定义字形），
      自动翻页大概率中期撞墙 —— 而它是 MVP 的唯一入口、没有 fallback；
    · 批量抓取比"打开我指定的一个页面"更容易触碰平台协议；
    · 真实工作流其实是"刷到感兴趣的岗位 → 存下来打分"，不是"让机器人替我逛网站"。

但"自动搜索到底行不行"这件事**值得用 2 小时验证一次**，而不是拍脑袋假设。
所以这里留一个 spike：跑一次就知道能不能解析出结果，结论写进文档。

★★ 这个脚本的合规红线（比功能更重要）

    1. **默认 dry-run**：不加 `--live` 就只打印"打算访问哪些 URL"，**不发任何请求**。
    2. **只抓一页**：不翻页、不带 `--pages`，避免变成批量抓取。
    3. **不绕风控**：遇到验证码/登录墙就**如实报告失败**，不尝试绕过。
    4. **不写库**：spike 只负责回答"能不能解析"，入库是产品代码的事。
    5. 频率克制：请求前随机延时（跟产品一致）。

    结论有两种，都算成功：
        · **可行** → 再考虑写 `adapters/boss.py`（仍然只做"详情抓取"，
          搜索能力只用于"把结果列出来给你挑"，不自动投递）；
        · **不可行**（大概率）→ 在文档里明确标注"自动搜索暂不可用"，
          P2 主路径锁定为"人给链接"。**这个否结论本身就是有价值的交付物** ——
          它把"以后要不要再试"这个反复纠结的问题一次性关掉。

用法：
    # 只看会访问什么（默认，不发请求）
    uv run python spikes/boss_search_spike.py --keyword "AI Agent" --city 深圳

    # 真跑一次（只抓第一页，约 1 次请求）
    uv run python spikes/boss_search_spike.py --keyword "AI Agent" --city 深圳 --live

    # 用保存下来的页面离线验证解析逻辑（推荐：先手动存一份 HTML）
    uv run python spikes/boss_search_spike.py --html saved_page.html
"""

from __future__ import annotations

import argparse
import contextlib
import random
import re
import sys
import time
from pathlib import Path

# 让脚本能直接跑（不必先 pip install -e .）
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from jobhunter.adapters.parse_html import (  # noqa: E402
    detect_platform,
    extract_platform_job_id,
    parse_detail_from_html,
)
from jobhunter.adapters.parse_text import strip_html  # noqa: E402

DEFAULT_KEYWORD = "AI Agent"
DEFAULT_CITY = "深圳"

#: BOSS 的搜索入口。注意：**这只是 URL 模板**，脚本不构造签名参数。
BOSS_SEARCH_URL = "https://www.zhipin.com/web/geek/job?query={keyword}&city={city_code}"

#: 城市名 → BOSS 的城市编码（只列几个常用的；写死是刻意的 ——
#: spike 不该为了"支持所有城市"去抓一份城市表，那又是额外请求）。
CITY_CODES = {"深圳": "101280600", "广州": "101280100", "东莞": "101281600", "北京": "101010100"}


def build_probe_url(keyword: str, city: str) -> str:
    """构造**探测用**的搜索 URL（不发请求，只打印出来）。"""
    return BOSS_SEARCH_URL.format(
        keyword=keyword.replace(" ", "%20"), city_code=CITY_CODES.get(city, "101280600")
    )


def analyze_search_page(html: str) -> dict[str, object]:
    """分析一个搜索结果的 HTML，回答"能不能解析出岗位"。

    Returns:
        一份结论字典（字段见下面的注释），供打印与写文档用。
    """
    text = strip_html(html)

    # 信号 1：页面上有没有"岗位卡片"结构的痕迹
    card_markers = [
        "job-card",
        "job-name",
        "job-card-wrapper",
        "job-list",
        "job-primary",
    ]
    found_markers = [marker for marker in card_markers if marker in html]

    # 信号 2：有没有详情页链接（能拿到链接 = 能走主路径逐条抓详情）
    detail_links = re.findall(r'href="([^"]*job_detail[^"]*)"', html)

    # 信号 3：有没有风控/验证页的特征
    blocked_markers = [
        "验证",
        "安全验证",
        "滑动",
        "captcha",
        "访问过于频繁",
        "当前IP",
        "zp_stoken",
    ]
    blocked_hits = [marker for marker in blocked_markers if marker in text or marker in html]

    # 信号 4：数字是否被渲染成自定义字形（字体反爬的典型特征）
    font_face_count = len(re.findall(r"@font-face", html))

    return {
        "page_chars": len(text),
        "markers_found": found_markers,
        "detail_link_count": len(detail_links),
        "sample_links": detail_links[:3],
        "blocked_hits": blocked_hits,
        "font_face_count": font_face_count,
        "verdict": _verdict(found_markers, detail_links, blocked_hits, len(text)),
    }


def _verdict(markers: list[str], links: list[str], blocked: list[str], page_chars: int) -> str:
    """给出结论（这一段就是 spike 要的东西）。"""
    if blocked:
        return "BLOCKED：页面含风控/验证特征 → 自动搜索不可用（需人工介入）"
    if links and markers:
        return "OK：能解析出岗位链接与卡片结构 → 自动搜索技术上可行"
    if page_chars < 500:
        return "EMPTY：页面几乎没有内容（多半是 JS 渲染或直接被拒）→ 不可用"
    if not links:
        return "NO_LINKS：拿不到详情页链接 → 即使能搜也没法走主路径，价值有限"
    return "UNCLEAR：信号不足，需要人工看一眼页面再判断"


def fetch_once(url: str, *, delay_range: tuple[float, float] = (3.0, 6.0)) -> str | None:
    """抓一次页面（**单次**，不翻页）。失败返回 None。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("需要 Playwright：uv sync --extra browser 且 playwright install chromium")
        return None

    delay = random.uniform(*delay_range)  # noqa: S311 - 不是加密用途
    print(f"  遵守频率克制：先等 {delay:.1f}s …")
    time.sleep(delay)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(locale="zh-CN")
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            # 有些页面永远不会 idle（长轮询、埋点），等不到就算了
            with contextlib.suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=5_000)
            return page.content()
        except Exception as exc:  # noqa: BLE001 - 网络/浏览器异常一律如实报告
            print(f"  抓取失败：{exc}")
            return None
        finally:
            browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="自动搜索可行性 spike（默认不发请求）")
    parser.add_argument("--keyword", default=DEFAULT_KEYWORD, help="搜索关键词")
    parser.add_argument("--city", default=DEFAULT_CITY, help="城市（深圳/广州/东莞/北京）")
    parser.add_argument(
        "--html",
        type=Path,
        help="改为分析本地保存的 HTML（推荐：先手动存一份，避免真实请求）",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="真的发一次请求（默认只打印计划访问的 URL）",
    )
    args = parser.parse_args()

    print("=" * 72)
    print("自动搜索可行性 spike")
    print("=" * 72)

    if args.html is not None:
        if not args.html.is_file():
            print(f"找不到文件：{args.html}")
            return 1
        html = args.html.read_text(encoding="utf-8", errors="replace")
        print(f"离线分析：{args.html}（{len(html)} 字符）")
    else:
        url = build_probe_url(args.keyword, args.city)
        print(f"关键词：{args.keyword}　城市：{args.city}")
        print(f"计划访问（单页，不翻页）：{url}")
        print()
        if not args.live:
            print("当前是**干跑模式**，没有发任何请求。")
            print()
            print("三种继续方式：")
            print("  1. 真跑一次：  --live")
            print("  2. 离线分析：把页面另存为 HTML，再用 --html saved.html 分析")
            print("  3. 直接放弃自动搜索（推荐）：P2 主路径锁定「人给链接」")
            print()
            print("提醒：本项目**不绕过**验证码/风控/签名。若 --live 遇到验证页，")
            print("      结论就是「自动搜索不可用」，这也是一个有效的交付结论。")
            return 0

        html = fetch_once(url)
        if html is None:
            print()
            print("结论：抓取失败 → 自动搜索不可用（保持主路径为「人给链接」）")
            return 1

    result = analyze_search_page(html)
    print()
    print("-" * 72)
    print(f"页面文本长度     : {result['page_chars']}")
    print(f"命中的卡片标记   : {result['markers_found'] or '（无）'}")
    print(f"详情页链接数     : {result['detail_link_count']}")
    for link in result["sample_links"]:  # type: ignore[union-attr]
        print(f"    · {link[:90]}")
    print(f"风控/验证特征    : {result['blocked_hits'] or '（无）'}")
    print(f"@font-face 数量  : {result['font_face_count']}（字体反爬的迹象）")
    print("-" * 72)
    print(f"结论：{result['verdict']}")

    # 顺带验证一次"详情页解析"对首页 HTML 是否安全（首页不该被当成岗位详情）
    detail = parse_detail_from_html(html, "https://www.zhipin.com/web/geek/job")
    print()
    print("附带检查（把搜索页当详情页解析时的表现）：")
    print(
        f"    platform={detect_platform('https://www.zhipin.com/web/geek/job')}"
        f"　platform_job_id={extract_platform_job_id('https://www.zhipin.com/web/geek/job')}"
    )
    print(f"    fetch_status={detail.fetch_status}　jd 字数={len(detail.jd_text)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

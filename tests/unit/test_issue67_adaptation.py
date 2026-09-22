"""#67 适配工单：`jh job crawl` 落地后的四件事（开发会话的适配轮）。

★ 这份工单是验证会话开的（看板 #67），背景见 `try anything/jh-verify/board_adapt_handoff.py`
  与 `/P7code/交接文档-jh-job-crawl.md`（工作区外的 WorkBuddy 工作副本）。
  它列了 6 条优先级，代码面在这四组：

    P1  同站错页（404 页 / 首页）不许入库 —— 两级判据：岗位 ID 一致性 + 地址判据
    P2  `--pages > 1` 静默无效必须**明说**（站点不认 `page=` 参数时）
    P4  `ensure_cdp_browser` 只看端口不够：要确认"端口后面是我们那个专用实例"
    P5  BOSS 往正文里注入水印词，把「职位描述」切成「职 boss 位描述」

★ 每条都先复现"改前会怎样"，再断言改后的行为 —— 只断言"现在是对的"守不住回归。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from jobhunter.adapters import cdp as cdp_module
from jobhunter.adapters.discover import JobDiscovery, SearchPageResult
from jobhunter.adapters.manual import ManualAdapter
from jobhunter.adapters.parse_html import parse_detail_from_html, parse_detail_from_text

# ===========================================================================
# P1：同站错页 —— 岗位 ID 一致性判据
# ===========================================================================
#: BOSS 的岗位详情页形态（真实形态：`/job_detail/<id>.html`）。
_BOSS_A = "https://www.zhipin.com/job_detail/abcdefghijkl.html"
_BOSS_B = "https://www.zhipin.com/job_detail/mnopqrstuvwx.html"


def _fetch_with_final_url(settings, monkeypatch, requested: str, final: str) -> Any:
    """让适配器"请求 A、最终停在 B"，返回抓取结果（不联网）。"""
    html = (
        "<html><body><div class='job-sec'>岗位职责：负责后端开发，熟悉 Python。</div></body></html>"
    )
    rendered = "岗位描述\n" + "岗位职责：负责后端开发，熟悉 Python。" * 8

    def _fake_render(self, url: str):  # noqa: ANN001, ANN202
        return html, rendered, None, final

    monkeypatch.setattr(ManualAdapter, "_render_page", _fake_render)
    monkeypatch.setattr(ManualAdapter, "_polite_delay", lambda self: None)
    return ManualAdapter(settings).fetch_detail(requested)


def test_same_site_different_job_id_is_rejected(settings, monkeypatch) -> None:
    """★ 同站换岗位：**ID 一致性**判据必须拦下（改前只比域名，5/5 放行）。"""
    detail = _fetch_with_final_url(settings, monkeypatch, _BOSS_A, _BOSS_B)

    assert detail is not None
    assert detail.fetch_status == "failed", "同站错页被当成岗位页入库了"
    assert detail.jd_text == ""
    assert detail.raw.get("job_id_mismatch") is True
    assert detail.fetch_note and "另一个岗位" in detail.fetch_note
    # 两个 ID 都要出现在说明里（用户才知道自己给的是哪个、实际落在哪个）
    assert "abcdefghijkl" in detail.fetch_note and "mnopqrstuvwx" in detail.fetch_note


def test_homepage_redirect_is_rejected(settings, monkeypatch) -> None:
    """★ 登录后实测 5/5 的那个场景：BOSS 把请求丢回首页。"""
    detail = _fetch_with_final_url(settings, monkeypatch, _BOSS_A, "https://www.zhipin.com/")

    assert detail is not None
    assert detail.fetch_status == "failed"
    assert detail.jd_text == ""
    assert detail.raw.get("redirect_mismatch") is True


def test_missing_job_page_is_rejected(settings, monkeypatch) -> None:
    """站点自己给的 404/失效页（路径里带别的数字）：同样不许入库。"""
    detail = _fetch_with_final_url(settings, monkeypatch, _BOSS_A, "https://www.zhipin.com/404")

    assert detail is not None
    assert detail.fetch_status == "failed"


def test_same_page_is_not_flagged(settings, monkeypatch) -> None:
    """反向守卫：停在**同一个岗位**上（带跟踪参数）不许误报。"""
    detail = _fetch_with_final_url(
        settings,
        monkeypatch,
        _BOSS_A,
        f"{_BOSS_A}?lid=1&securityId=xyz",
    )

    assert detail is not None
    assert detail.fetch_status != "failed", "正常的同页跳转被误判成换了岗位"
    assert detail.raw.get("job_id_mismatch") is None
    assert detail.raw.get("redirect_mismatch") is None


# ===========================================================================
# P2：`--pages` 静默无效 → 明确告警
# ===========================================================================
def _page(url: str, links: list[str]) -> SearchPageResult:
    return SearchPageResult(page_url=url, job_urls=links, final_url=url)


def test_repeated_pages_stop_early_and_say_so(settings, monkeypatch) -> None:
    """★ 第 2 页与第 1 页是同一批 → 提前停 + 标 `duplicate_of_previous`。"""
    same = [
        "https://www.zhipin.com/job_detail/aaaaaaaaaaaa.html",
        "https://www.zhipin.com/job_detail/bbbbbbbbbbbb.html",
    ]
    calls: list[str] = []

    def _fake_read_page(self, url: str) -> SearchPageResult:  # noqa: ANN001
        calls.append(url)
        return _page(url, list(same))

    monkeypatch.setattr(JobDiscovery, "read_page", _fake_read_page)
    monkeypatch.setattr(JobDiscovery, "_polite_delay", lambda self: None)

    results = JobDiscovery(settings, use_cdp=True).search("Python", "东莞", pages=3)

    assert len(results) == 2, f"应当在第 2 页就停下（实际读了 {len(calls)} 页）"
    assert results[0].duplicate_of_previous is False
    assert results[1].duplicate_of_previous is True
    assert results[1].note and "--pages" in results[1].note


def test_growing_pages_keep_going(settings, monkeypatch) -> None:
    """反向守卫：每页都有新岗位时**不许**提前停（否则就是漏抓）。"""
    pages = {
        1: ["https://www.zhipin.com/job_detail/a1.html"],
        2: ["https://www.zhipin.com/job_detail/b2.html"],
        3: ["https://www.zhipin.com/job_detail/c3.html"],
    }

    def _fake_read_page(self, url: str) -> SearchPageResult:  # noqa: ANN001
        index = int(url.rsplit("page=", 1)[-1])
        return _page(url, pages[index])

    monkeypatch.setattr(JobDiscovery, "read_page", _fake_read_page)
    monkeypatch.setattr(JobDiscovery, "_polite_delay", lambda self: None)

    results = JobDiscovery(settings, use_cdp=True).search("Python", "东莞", pages=3)

    assert len(results) == 3
    assert all(not result.duplicate_of_previous for result in results)


# ===========================================================================
# P4：端口后面必须是我们那个专用实例
# ===========================================================================
class _FakeCdpSession:
    def __init__(self, result: Any = None, *, raises: bool = False) -> None:
        self._result = result
        self._raises = raises
        self.sent: list[str] = []

    def send(self, method: str) -> Any:
        self.sent.append(method)
        if self._raises:
            raise RuntimeError("Command not supported")
        return self._result


class _FakeBrowser:
    """只实现 `ensure_our_browser` 会用到的那一个方法。"""

    def __init__(self, *, profile: str | None, raises: bool = False) -> None:
        self.session = _FakeCdpSession(
            {"arguments": ["msedge.exe", f"--user-data-dir={profile}"]}
            if profile is not None
            else {},
            raises=raises,
        )

    def new_browser_cdp_session(self) -> _FakeCdpSession:
        return self.session


def test_owner_check_accepts_our_profile(tmp_path: Path) -> None:
    endpoint = cdp_module.CdpEndpoint(port=9333, started=True, profile_dir=tmp_path / "cdp-profile")
    cdp_module.ensure_our_browser(_FakeBrowser(profile=str(endpoint.profile_dir)), endpoint)


def test_owner_check_rejects_someone_elses_profile(tmp_path: Path) -> None:
    """★ 端口后面是别人的浏览器 → 拒绝（改前会静默连上去，用别人的登录态）。"""
    endpoint = cdp_module.CdpEndpoint(port=9333, started=False, profile_dir=tmp_path / "ours")
    with pytest.raises(cdp_module.CdpOwnerError) as excinfo:
        cdp_module.ensure_our_browser(_FakeBrowser(profile=str(tmp_path / "theirs")), endpoint)

    message = str(excinfo.value)
    assert "9333" in message
    assert "专用浏览器" in message
    assert "cdp_port" in message  # 给出可操作的出路


def test_owner_check_rejects_a_default_profile(tmp_path: Path) -> None:
    """别人那个**没带** `--user-data-dir` 的实例（日常浏览器）同样不是我们的。"""
    endpoint = cdp_module.CdpEndpoint(port=9333, started=False, profile_dir=tmp_path / "ours")
    with pytest.raises(cdp_module.CdpOwnerError):
        cdp_module.ensure_our_browser(_FakeBrowser(profile=None), endpoint)


def test_owner_check_gives_up_quietly_when_unanswerable(tmp_path: Path) -> None:
    """问不出来（老浏览器不支持这个命令）→ **不阻断**（否则这条路在旧版上全废）。"""
    endpoint = cdp_module.CdpEndpoint(port=9333, started=False, profile_dir=tmp_path / "ours")
    cdp_module.ensure_our_browser(_FakeBrowser(profile=None, raises=True), endpoint)


def test_owner_error_is_not_swallowed_by_the_fallback(settings, monkeypatch) -> None:
    """★★ 关键：这条错误**不许**被"回退到 goto 那条路"吞掉，也不许压成"打开失败"。

    ★ 为什么单独一条：`_render_page_via_cdp` 里原本是 `contextlib.suppress(Exception)`
      —— 那会把"连错浏览器"变成"悄悄换条路继续连同一个错的实例"。
    ★ 为什么是"failed 的 detail"而不是抛异常：`fetch_detail` 的调用方（CLI）
      靠 `fetch_note` 打印原因；抛出去会被上游压成一句"打开失败/超时"，
      而这句话会让用户去查网络 —— 永远查不到"9333 后面是别人的浏览器"。
    """
    module = types.ModuleType("playwright.sync_api")

    class _Ctx:
        def __enter__(self) -> _Ctx:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    module.sync_playwright = _Ctx  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))

    endpoint = cdp_module.CdpEndpoint(port=9333, started=False, profile_dir=Path("x"))

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise cdp_module.CdpOwnerError("端口后面不是本工具的专用浏览器")

    monkeypatch.setattr(cdp_module, "open_target_page", _boom)

    def _no_fallback(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("连错浏览器时不许回退到 goto 那条路（它连着同一个错实例）")

    monkeypatch.setattr(cdp_module, "cdp_page", _no_fallback)
    monkeypatch.setattr(ManualAdapter, "_polite_delay", lambda self: None)

    adapter = ManualAdapter(settings, use_cdp=True, cdp_endpoint=endpoint)
    detail = adapter.fetch_detail(_BOSS_A)

    assert detail is not None
    assert detail.fetch_status == "failed"
    assert detail.fetch_note and "专用浏览器" in detail.fetch_note


# ===========================================================================
# P5：BOSS 往正文里注入水印词
# ===========================================================================
def test_watermark_inside_a_marker_does_not_break_the_body_start() -> None:
    """★ `职 boss 位描述` 也必须能被认成正文起点（改前裁剪失效 → 导航混进 JD）。"""
    raw = "\n".join(
        [
            "首页",
            "登录",
            "微信扫码分享",
            "举报",
            "AI 应用开发工程师",
            "职 boss 位描述",
            "1. 做大模型应用；",
            "2. 写 Python 服务。",
        ]
    )
    from jobhunter.adapters.manual import _clean_rendered_text

    cleaned = _clean_rendered_text(raw)

    assert "微信扫码分享" not in cleaned and "举报" not in cleaned, "导航噪音混进了 JD"
    assert "做大模型应用" in cleaned
    assert "职位描述" in cleaned, "水印词应当被去掉（而不是留在正文里）"


def test_watermark_is_only_stripped_between_chinese() -> None:
    """反向守卫：**不是**夹在中文之间的 `boss` / `kanzhun` 一律不动。

    ★ 这条钉的是取舍的边界（见 `strip_watermarks` 的 docstring）：
      两侧是 ASCII/标点/行首行尾时**不删** —— 那不像注入形态。
      ⚠️ 夹在中文里的正常英文词（`汇报给 boss 的时候`）**会**被删掉，
        这是明知故犯的取舍（另一边是整段正文裁错），所以只断言"能看见动过文本"。
    """
    from jobhunter.adapters.parse_html import strip_watermarks

    untouched = [
        "boss 是英文单词",
        "BOSS",
        "kanzhun.com 是站点域名",
        "用 kanzhun",
        "Python 开发",
    ]
    for text in untouched:
        assert strip_watermarks(text) == text, f"不该动这段：{text}"


def test_watermark_removal_is_counted() -> None:
    """★ "动过文本"必须能看见：计数进 `raw`（取舍允许，但**不许静默**）。"""
    from jobhunter.adapters.parse_html import count_watermarks

    detail = parse_detail_from_text("职BOSS直聘位描述\n负责 Python 后端开发。")

    assert count_watermarks("职BOSS直聘位描述") == 1
    assert detail.raw.get("watermarks_removed") == 1

    clean = parse_detail_from_text("职位描述\n负责 Python 后端开发。")
    assert "watermarks_removed" not in clean.raw, "没动过就不该留下这个键"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("职boss位描述", "职位描述"),
        ("职 boss 位描述", "职位描述"),
        ("职位描BOSS直聘述", "职位描述"),
        ("岗kanzhun位职责", "岗位职责"),
        # 幂等：再跑一遍不变
        ("职位描述", "职位描述"),
    ],
)
def test_strip_watermarks_cases(raw: str, expected: str) -> None:
    from jobhunter.adapters.parse_html import strip_watermarks

    assert strip_watermarks(raw) == expected
    assert strip_watermarks(strip_watermarks(raw)) == expected


def test_watermark_is_removed_from_the_stored_html_jd() -> None:
    """HTML 路径入库的 JD 里也不许留水印词（它会被 match 打分、被 tailor 用）。"""
    html = (
        "<html><body><div class='job-sec'>"
        "<p>职BOSS直聘位描述</p><p>负责 Python 后端开发。</p>"
        "</div></body></html>"
    )
    detail = parse_detail_from_html(html, "https://www.zhipin.com/job_detail/aaaaaaaaaaaa.html")

    assert "BOSS直聘" not in detail.jd_text
    assert "职位描述" in detail.jd_text


def test_watermark_is_removed_from_pasted_text() -> None:
    """粘贴路径同理（用户从页面上复制，可能正好复制到注入的那段）。"""
    detail = parse_detail_from_text("职位描boss述\n负责 Python 后端开发。")

    assert "boss" not in detail.jd_text
    assert "职位描述" in detail.jd_text


# ===========================================================================
# P3：文档口径 —— 旧红线表述不许留在"讲当前行为"的文档里
# ===========================================================================
#: 这些文件是"**讲当前行为**"的（用户与下一个人靠它们判断工具会做什么、不会做什么）。
#: 历史记录类文件（`docs/REVIEW-*.md`、`docs/STATUS.md` 的旧轮次、`docs/ISSUES.md`）
#: **不在**名单里 —— 它们本来就该保留当时的原话（改掉反而是篡改历史）。
_CURRENT_BEHAVIOUR_DOCS = (
    "README.md",
    "config.yaml",
    "docs/install.md",
    "docs/config.md",
    "docs/architecture.md",
)

#: 授权放开之后**已经不再成立**的旧表述（出现即说明口径没跟上代码）。
#: ★ 每一条都是"读者会据此做出错误判断"的那种，不是措辞洁癖。
_STALE_CLAIMS = (
    "不做自动搜索",
    "不做批量抓取",
    "没有自动翻页",
    "当前没有自动翻页路径",
    "一次只抓你给的那一个",
)


def test_user_facing_docs_match_the_authorization() -> None:
    """★★ 文档口径守卫（#67 §四的验收项）：旧红线表述要么删、要么改成"已授权 + 边界"。

    ★ 为什么值得一条用例：这次改动把"自动搜索"从红线变成了已授权能力，
      而口径散落在 5 个文件里 —— 漏掉任何一个，下一个人读到的就是**过期的合规声明**。
      这种"文档说没有、代码里却有"的状态比代码写错更难发现（不会有用例因为文档而红）。

    ★ 判据只扫"讲当前行为"的文件，且只认**确切的旧句子**（不是关键词）——
      后者会误伤"不做**未经授权的**自动搜索"这类正确表述（本项目为此吃过亏，#56）。
    """
    from jobhunter.config.paths import find_project_root

    root = find_project_root()
    offenders: list[str] = []
    for relative in _CURRENT_BEHAVIOUR_DOCS:
        path = root / relative
        if not path.is_file():  # pragma: no cover - 发行包里可能没有这些文件
            continue
        text = path.read_text(encoding="utf-8")
        for claim in _STALE_CLAIMS:
            if claim in text:
                offenders.append(f"{relative}：仍然写着「{claim}」")

    assert not offenders, (
        "这些文档还在陈述已被授权推翻的旧边界（改成「已授权 + 现在守哪些边界」）：\n  "
        + "\n  ".join(offenders)
    )


def test_authorization_is_written_down_where_it_matters() -> None:
    """反向守卫：**授权这件事本身**必须写在 COMPLIANCE 与 README 里。

    ★ 只删旧话、不写新话同样不行 —— 那样读者会以为"工具偷偷能批量抓了"。
      授权必须有出处（谁、什么时候、推翻了什么、边界是什么）。
    """
    from jobhunter.config.paths import find_project_root

    root = find_project_root()
    compliance = (root / "docs" / "COMPLIANCE.md").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert "5.8" in compliance, "COMPLIANCE 里应当有 §5.8（授权记录）"
    for text, name in ((compliance, "COMPLIANCE.md"), (readme, "README.md")):
        assert "2026-09-21" in text, f"{name} 里没有写明授权日期"
        assert "授权" in text, f"{name} 里没有说明这是授权后的行为"
        assert "不自动投递" in text or "不提供自动投递" in text, f"{name} 里没写清最高红线仍在"

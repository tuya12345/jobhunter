"""本地 CDP 抓取（ISSUES #65）：连用户自己的浏览器 + **红线守卫**。

★★ 这组用例的重点不是"能不能连上"（那需要真浏览器），而是**红线有没有被越过**

    验证会话明说会用 AST 扫"有没有自动点击/填表/登录的代码"。所以这里两路都守：

      ① **行为**：用一个**假的 Playwright**跑一遍抓取路径，断言
         `click` / `fill` / `press` / `type` / `keyboard` / `set_input_files`
         这些方法**从来没有被调用过**（假对象会把调用记下来）；
      ② **源码**：AST 扫 `src/`，出现上述调用名就红（连"将来有人顺手加一个"也拦住）。

    ★ 只写注释里承诺"我们不点击"是不够的 —— 本项目已经吃过"守卫只覆盖了一个方向"的亏
      （#56 / #60 都是同一形态）。

★ 另外两条：**专用 profile 必须在 state/ 下**（里面有真实登录态）、
  **不重复起实例**（已在监听就复用）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from jobhunter.adapters import cdp as cdp_module
from jobhunter.adapters.manual import ManualAdapter
from jobhunter.config.paths import find_project_root


# ===========================================================================
# 配置与目录
# ===========================================================================
def test_default_profile_dir_is_under_state(settings) -> None:
    """★ 专用 profile 里会有**真实登录态** → 默认必须落在 `state/`（gitignore 覆盖）。

    这一条与"绝不把隐私放进仓库"是同一条纪律；配置可以覆盖，但默认值必须安全。
    """
    profile = cdp_module.cdp_profile_dir(settings)
    state_dir = settings.paths.state_dir
    assert profile.parent == state_dir or state_dir in profile.parents, (
        f"专用 profile 不在 state/ 下：{profile}（里面有真实登录态，绝不能进仓库）"
    )
    assert "cdp-profile" in profile.name


def test_state_dir_is_gitignored(settings) -> None:
    """`state/` 必须真的被 .gitignore 覆盖（否则上面那条只是"看起来安全"）。"""
    ignore = (find_project_root() / ".gitignore").read_text(encoding="utf-8")
    lines = {line.strip() for line in ignore.splitlines()}
    assert "state/" in lines or "state/" in ignore, "state/ 没有被 .gitignore 覆盖"


def test_profile_dir_can_be_overridden(settings, tmp_path: Path) -> None:
    custom = tmp_path / "my-profile"
    patched = settings.model_copy(
        update={"browser": settings.browser.model_copy(update={"cdp_profile_dir": custom})}
    )
    assert cdp_module.cdp_profile_dir(patched) == custom


# ===========================================================================
# 起实例：已经在监听就复用（不重复起、不打扰用户已登录的实例）
# ===========================================================================
def test_existing_cdp_instance_is_reused(settings, monkeypatch) -> None:
    monkeypatch.setattr(cdp_module, "is_cdp_listening", lambda port, host="127.0.0.1": True)

    def _boom(*args, **kwargs):
        raise AssertionError("已经在监听却又起了一个实例")

    monkeypatch.setattr(cdp_module.subprocess, "Popen", _boom)

    endpoint = cdp_module.ensure_cdp_browser(settings)
    assert endpoint.started is False
    assert endpoint.port == settings.browser.cdp_port


def test_missing_browser_gives_actionable_message(settings, monkeypatch, tmp_path: Path) -> None:
    """找不到浏览器时要告诉用户**怎么指定**，而不是抛 FileNotFoundError。"""
    monkeypatch.setattr(cdp_module, "is_cdp_listening", lambda port, host="127.0.0.1": False)
    monkeypatch.setattr(cdp_module, "find_browser_executable", lambda settings=None: None)

    with pytest.raises(RuntimeError) as excinfo:
        cdp_module.ensure_cdp_browser(settings)

    message = str(excinfo.value)
    assert "cdp_executable" in message, "没告诉用户怎么指定浏览器路径"
    assert "--html" in message and "--manual" in message, "没给出降级路径"


def test_launch_uses_a_dedicated_profile_and_only_one_url(
    settings, monkeypatch, tmp_path: Path
) -> None:
    """★ 起实例时：**必须**带专用 profile 与调试端口，且只给一个 `about:blank`。

    「只给一个空白页」是红线 1 在**进程参数**层面的体现：
    没有任何"要抓哪些页面"的列表能从这里传进去。
    """
    monkeypatch.setattr(cdp_module, "cdp_profile_dir", lambda settings=None: tmp_path / "p")
    state = {"listening": False}

    def _listen(port: int, host: str = "127.0.0.1") -> bool:
        return state["listening"]

    monkeypatch.setattr(cdp_module, "is_cdp_listening", _listen)
    monkeypatch.setattr(
        cdp_module, "find_browser_executable", lambda settings=None: tmp_path / "edge.exe"
    )
    (tmp_path / "edge.exe").write_text("", encoding="utf-8")

    captured: dict[str, list[str]] = {}

    class _FakePopen:
        def __init__(self, args, **kwargs):  # noqa: ANN001, ANN003
            captured["args"] = list(args)
            state["listening"] = True  # 模拟"起来之后端口就绪"

    monkeypatch.setattr(cdp_module.subprocess, "Popen", _FakePopen)

    endpoint = cdp_module.ensure_cdp_browser(settings)

    args = captured["args"]
    assert endpoint.started is True
    assert any(a.startswith("--remote-debugging-port=") for a in args)
    assert any(a.startswith("--user-data-dir=") for a in args)
    assert args[-1] == "about:blank", "只应当打开一个空白页（不预先指定任何要抓的页面）"
    urls = [a for a in args if a.startswith("http")]
    assert urls == [], f"命令行里不该出现任何目标 URL（红线 1）：{urls}"


# ===========================================================================
# 抓取路径：假的 Playwright + 红线断言
# ===========================================================================
FORBIDDEN_PAGE_CALLS = (
    "click",
    "fill",
    "press",
    "type",
    "check",
    "select_option",
    "set_input_files",
    "tap",
    "drag_and_drop",
    "keyboard",
    "mouse",
)


class _FakePage:
    """假页面：记录**每一个**被调用的方法名（用来抓红线违规）。"""

    def __init__(
        self, html: str = "<html><body><div class='job-sec'>岗位职责：做点事</div></body></html>"
    ):
        self.calls: list[str] = []
        self._html = html

    def __getattr__(self, name: str):
        def _record(*args, **kwargs):  # noqa: ANN002, ANN003
            self.calls.append(name)
            if name == "content":
                return self._html
            if name == "evaluate":
                return "岗位描述\n" + "岗位职责：负责后端开发，熟悉 Python。" * 6
            return None

        return _record


class _FakeContext:
    def __init__(self, pages: list[_FakePage]) -> None:
        self.pages = pages
        self.new_page_calls = 0
        self.closed = False

    def new_page(self) -> _FakePage:
        self.new_page_calls += 1
        page = _FakePage()
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, context: _FakeContext) -> None:
        self._context = context
        self.endpoint: str | None = None

    def connect_over_cdp(self, endpoint: str) -> object:
        self.endpoint = endpoint
        return type("B", (), {"contexts": [self._context]})()


class _FakePlaywright:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium

    def __enter__(self) -> _FakePlaywright:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _patch_playwright(monkeypatch, page: _FakePage) -> _FakeChromium:
    context = _FakeContext([page])
    chromium = _FakeChromium(context)
    fake_module = type(
        "m", (), {"sync_playwright": staticmethod(lambda: _FakePlaywright(chromium))}
    )
    monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api", fake_module)
    monkeypatch.setitem(__import__("sys").modules, "playwright", fake_module)
    return chromium


def test_cdp_fetch_reuses_the_existing_page_and_never_touches_input(settings, monkeypatch) -> None:
    """★★ 核心红线断言：走完 CDP 抓取全流程，**没有点过/填过任何东西**。"""
    page = _FakePage()
    chromium = _patch_playwright(monkeypatch, page)
    monkeypatch.setattr(ManualAdapter, "_polite_delay", lambda self: None)

    endpoint = cdp_module.CdpEndpoint(port=9333, started=False, profile_dir=Path("x"))
    adapter = ManualAdapter(settings, platform="boss", use_cdp=True, cdp_endpoint=endpoint)
    detail = adapter.fetch_detail("https://www.zhipin.com/job_detail/abc.html")

    assert chromium.endpoint == "http://127.0.0.1:9333", "没连到 CDP 端点"
    assert detail is not None
    # 复用了已有页面（不需要新开标签页）
    assert "goto" in page.calls, "没有导航到用户给的那个链接"
    # ★ 红线：任何"操作页面"的方法都不许出现
    forbidden = [call for call in page.calls if call in FORBIDDEN_PAGE_CALLS]
    assert not forbidden, f"CDP 抓取路径上调用了不该调用的方法：{forbidden}"


def test_cdp_fetch_closes_nothing_at_all(settings, monkeypatch) -> None:
    """★★ 抓完**什么都不关**（缺口 ①：关掉唯一标签页 = Edge 整个退出）。

    实测：初版在 `finally` 里关了刚用过的页面 → 那个专用窗口只有一个标签页 →
    Edge 关掉最后一个标签就退出 → CDP 端口随后不再监听 → 用户下次还得重新开、重新等。

    所以现在连页面也不关：留着它（停在刚抓完的 URL 上，用户能看见工具读了哪个页面），
    下次抓取复用同一个标签页。
    """
    page = _FakePage()
    context = _FakeContext([page])
    chromium = _FakeChromium(context)
    fake_module = type(
        "m", (), {"sync_playwright": staticmethod(lambda: _FakePlaywright(chromium))}
    )
    monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api", fake_module)
    monkeypatch.setitem(__import__("sys").modules, "playwright", fake_module)
    monkeypatch.setattr(ManualAdapter, "_polite_delay", lambda self: None)

    adapter = ManualAdapter(
        settings,
        use_cdp=True,
        cdp_endpoint=cdp_module.CdpEndpoint(port=9333, started=False, profile_dir=Path("x")),
    )
    adapter.fetch_detail("https://example.com/job/1")

    assert context.closed is False, "连过去的是用户自己的浏览器，绝不能替他关掉"
    assert "close" not in page.calls, (
        "把刚抓完的页面关掉了 —— 专用窗口只有一个标签页，关掉它等于关掉整个浏览器"
    )
    # 也不该另开标签页（复用已有页面 → 一个标签页反复用）
    assert context.new_page_calls == 0, "每次都新开标签页，用久了会攒一堆"


def test_cdp_fetch_still_applies_the_access_wall_guard(
    settings, monkeypatch, tmp_path: Path
) -> None:
    """★ CDP 这条路也必须过登录墙判定（否则 #62 会在新路径上复活）。

    这正是"两条路径共用 `_finish_detail`"要守的东西。
    """
    from jobhunter.adapters.parse_html import detect_access_wall

    login_html = (
        "<html><head><title>【BOSS直聘注册登录】</title></head>"
        "<body><div>APP扫码登录</div><div>验证码登录/注册</div><div>发送验证码</div></body></html>"
    )
    page = _FakePage(html=login_html)
    _patch_playwright(monkeypatch, page)
    monkeypatch.setattr(ManualAdapter, "_polite_delay", lambda self: None)
    assert detect_access_wall(login_html) is not None, "样本本身就该被判成墙"

    adapter = ManualAdapter(
        settings,
        use_cdp=True,
        cdp_endpoint=cdp_module.CdpEndpoint(port=9333, started=False, profile_dir=Path("x")),
    )
    # 渲染文本也带上墙体特征（真实登录页的正文是客户端渲染的）
    monkeypatch.setattr(
        "jobhunter.adapters.manual._longest_rendered_text",
        lambda page, **kwargs: "APP扫码登录 验证码登录/注册 发送验证码",
    )
    detail = adapter.fetch_detail("https://www.zhipin.com/job_detail/x.html")

    assert detail is not None
    assert detail.fetch_status == "failed", "CDP 路径漏了登录墙判定"
    assert detail.jd_text == ""


def test_browser_prompt_page_is_refused_not_stored(settings, monkeypatch) -> None:
    """★★ 实测踩到的坑：浏览器**自己的提示页**差点被当成 JD 存进库。

    在专用实例里跑真实 BOSS 链接时，浏览器把我们带到了它自己的
    「我们现在正在你的所有设备上同步你的浏览数据」首次运行页 ——
    那段文字被当成 JD（140 字）拿到 `partial`，而 CLI 只拦 `failed` → **会被入库**。
    它既不是登录墙（没有登录字样），也没有岗位结构可判 —— 只看正文根本挡不住。

    所以加了"最终地址"这道判据：**跨站就判失败**（同站内的登录/验证跳转仍交给登录墙判定）。
    """
    page = _FakePage(
        html="<html><body><p>我们现在正在你的所有设备上同步你的浏览数据</p></body></html>"
    )
    # 假页面在 goto 之后"停在"浏览器自己的页面上（另一个站点）
    object.__setattr__(page, "url", "https://go.microsoft.com/fwlink/?linkid=2108836")
    _patch_playwright(monkeypatch, page)
    monkeypatch.setattr(ManualAdapter, "_polite_delay", lambda self: None)

    adapter = ManualAdapter(
        settings,
        use_cdp=True,
        cdp_endpoint=cdp_module.CdpEndpoint(port=9333, started=False, profile_dir=Path("x")),
    )
    detail = adapter.fetch_detail("https://www.zhipin.com/job_detail/abc.html")

    assert detail is not None
    assert detail.fetch_status == "failed", "浏览器自己的提示页被当成岗位页了（会入库）"
    assert detail.jd_text == ""
    assert detail.fetch_note and "另一个站点" in detail.fetch_note
    assert detail.raw.get("cross_site") is True


def test_same_site_redirect_is_not_treated_as_cross_site(settings, monkeypatch) -> None:
    """同站跳转（登录页 / 补斜杠 / 带跟踪参数 / www 差异）**不算**跨站。

    ★ 反向守卫：如果按"整串 URL 相等"来判，正常的 URL 规范化会天天误报 ——
      所以判据是**域名**，这条用例把"别收得太紧"钉住。
    """
    page = _FakePage()
    object.__setattr__(page, "url", "https://zhipin.com/job_detail/abc.html?lid=123&securityId=x")
    _patch_playwright(monkeypatch, page)
    monkeypatch.setattr(ManualAdapter, "_polite_delay", lambda self: None)

    adapter = ManualAdapter(
        settings,
        use_cdp=True,
        cdp_endpoint=cdp_module.CdpEndpoint(port=9333, started=False, profile_dir=Path("x")),
    )
    detail = adapter.fetch_detail("https://www.zhipin.com/job_detail/abc.html")

    assert detail is not None
    assert detail.raw.get("cross_site") is None, "同站的正常跳转被误判成跨站了"


def test_non_cdp_path_is_unchanged(settings, monkeypatch) -> None:
    """不传 `--cdp` 时行为不变（仍然新起 headless，不碰 CDP）。"""
    called: list[str] = []

    def _fake_render(self, url: str):  # noqa: ANN001, ANN202
        called.append(url)
        return (
            "<html><body><div class='job-sec'>岗位职责：" + "做事。" * 40 + "</div></body></html>",
            None,
            None,
            url,
        )

    monkeypatch.setattr(ManualAdapter, "_render_page", _fake_render)
    monkeypatch.setattr(ManualAdapter, "_polite_delay", lambda self: None)
    monkeypatch.setattr(
        ManualAdapter,
        "_render_page_via_cdp",
        lambda self, url: pytest.fail("没传 --cdp 却走了 CDP 路径"),
    )

    detail = ManualAdapter(settings).fetch_detail("https://example.com/job/1")
    assert called == ["https://example.com/job/1"]
    assert detail is not None


# ===========================================================================
# 源码守卫：红线不许写进代码
# ===========================================================================
def test_no_page_interaction_calls_in_source() -> None:
    """★★ AST 守卫：`src/` 里不许出现"操作页面"的调用。

    验证会话会扫这个，所以我自己也扫一遍 —— 而且**用 AST 而不是正则**：
    注释里写"我们不会 click"是允许的（也是好事），
    要拦的是**真的调用**（`page.click(...)` / `page.fill(...)`）。

    ★ 顺带说明为什么这条守卫比注释可靠：本项目已经吃过三次
      "形参加了但没人传"（#37 / #24 / #55）与两次"守卫只覆盖一个方向"
      （#56 / #60）—— 承诺要能被机器检查才算数。
    """
    forbidden = {
        "click",
        "fill",
        "press",
        "type",
        "check",
        "select_option",
        "set_input_files",
        "tap",
        "drag_and_drop",
        "hover",
        "keyboard",
        "mouse",
        "dblclick",
        "focus",
    }
    offenders: list[str] = []

    def _receiver(node: ast.expr) -> str:
        """取"被调用对象的最后一段名字"（`page.click` → `page`；`self.page.click` → `page`）。"""
        if isinstance(node, ast.Attribute):
            return node.attr.lower()
        if isinstance(node, ast.Name):
            return node.id.lower()
        return ""

    #: 只有这些"像浏览器对象"的接收者才纳入检查。
    #: ★ 为什么不检查所有 `.check()`：业务层也有叫 check 的方法
    #:   （`FidelityGuard.check()` 就是），把它算成"点了页面"是**误报** ——
    #:   而误报的守卫最后一定被人关掉。红线的实质是"**不操作浏览器页面**"，
    #:   所以按接收者收窄才是对的。
    browser_objects = ("page", "frame", "element", "locator", "handle", "context")

    for path in sorted((find_project_root() / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in forbidden:
                continue
            receiver = _receiver(node.func.value)
            if any(token in receiver for token in browser_objects):
                offenders.append(
                    f"{path.relative_to(find_project_root())}:{node.lineno} → .{node.func.attr}()"
                )

    # 元检查：守卫真的认得 `page.click(...)` 这种形态（否则它可能什么都没扫到）
    assert _receiver(ast.parse("page.click('x')").body[0].value.func.value) == "page"

    assert not offenders, (
        "源码里出现了「操作页面」的调用 —— 本项目只读页面，绝不点击/填表/按键：\n  "
        + "\n  ".join(offenders)
    )


def test_job_add_has_no_paging_or_batch_options() -> None:
    """★ 红线 1：`jh job add` 的选项里**没有**任何"翻页/批量"入口。

    ★ 为什么专门测这个：CDP 让"翻页"技术上更容易（浏览器就开着、会话也在），
      所以"没有翻页选项"这件事值得被机器守住 —— 它比注释可靠。
    """
    from typer.testing import CliRunner

    from jobhunter.cli.app import app

    result = CliRunner().invoke(app, ["job", "add", "--help"])
    assert result.exit_code == 0, result.output
    help_text = result.output

    banned = ("--pages", "--page", "--next", "--all-pages", "--batch-pages", "--crawl")
    for flag in banned:
        assert flag not in help_text, f"`jh job add` 出现了翻页/批量选项：{flag}"

    # `--cdp` 的帮助里要写明它抓的是"一个链接"（而不是"可以抓很多"）
    assert "--cdp" in help_text
    assert "一个" in help_text or "单个" in help_text

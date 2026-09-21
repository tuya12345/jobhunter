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
        self,
        html: str = "<html><body><div class='job-sec'>岗位职责：做点事</div></body></html>",
        url: str = "about:blank",
        target_id: str = "",
    ):
        self.calls: list[str] = []
        self._html = html
        # ★ `url` / `target_id` 必须是**真属性**（不能靠 `__getattr__` 兜）：
        #   `open_target_page()` 拿它们做比较，拿到函数对象会 TypeError。
        #   `target_id` 对应真实浏览器里的 CDP target id —— 认页靠它。
        self.url = url
        self.target_id = target_id

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
        self.detached = 0

    def new_page(self) -> _FakePage:
        self.new_page_calls += 1
        page = _FakePage()
        self.pages.append(page)
        return page

    def new_cdp_session(self, page: _FakePage) -> object:
        """假的 CDP 会话：只回答 `Target.getTargetInfo`（`_target_id_of` 要用的）。

        ★ 有它才测得到**真正的认页机制**（按 target id），而不是只测 URL 兜底。
        """
        outer = self

        class _FakeSession:
            def send(self, method: str, *args: object, **kwargs: object) -> dict:
                assert method == "Target.getTargetInfo", method
                return {"targetInfo": {"targetId": page.target_id}}

            def detach(self) -> None:
                outer.detached += 1

        return _FakeSession()

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


def _force_legacy_page_opening(monkeypatch) -> None:
    """让"以目标 URL 新建标签页"**确定性地失败** → 抓取走回退分支（复用已有页 + goto）。

    ★★ 为什么必须显式 mock，而不是"反正本机没开浏览器它就连不上"：

        不 mock 的话，`create_target()` 会去连真实的 `127.0.0.1:9333` ——
        · 本机没开浏览器 → 连不上 → 走回退 → 测试**碰巧绿**；
        · 本机开着浏览器 → 它**真的会开一个标签页**，而且返回一个真页面 →
          测试拿到真实网页而不是假 HTML → 红。

        "测试结果取决于本机有没有开着浏览器"是最难查的那类 flaky
        （本项目在 P4.5 / P6 都吃过"守卫依赖环境"的亏）。
        所以老路径的用例统一显式走回退；新路径的用例单独 mock 成功分支。
    """

    def _boom(port: int, url: str) -> str:
        raise OSError("单元测试不连真实浏览器")

    monkeypatch.setattr(cdp_module, "create_target", _boom)


def _patch_playwright(monkeypatch, page: _FakePage) -> _FakeChromium:
    _force_legacy_page_opening(monkeypatch)
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
    _force_legacy_page_opening(monkeypatch)
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


def _patch_playwright_with_context(monkeypatch, context: _FakeContext) -> _FakeChromium:
    """只挂假 Playwright（不动 `create_target`）—— 给"新路径"的用例用。"""
    chromium = _FakeChromium(context)
    fake_module = type(
        "m", (), {"sync_playwright": staticmethod(lambda: _FakePlaywright(chromium))}
    )
    monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api", fake_module)
    monkeypatch.setitem(__import__("sys").modules, "playwright", fake_module)
    return chromium


def _fake_playwright_context() -> object:
    """取出上面挂好的假 playwright 模块（用它进 `with` 块）。"""
    return __import__("sys").modules["playwright.sync_api"]


def test_born_at_target_tab_is_used_without_goto(settings, monkeypatch) -> None:
    """★★ 2026-09-21 的核心修复：用「以目标 URL 出生」的标签页，**并且不再 goto**。

    ★ 为什么"不再 goto"本身值得一条用例：新页已经停在目标 URL 上，再导航一次
      就等于又给它加了一个「上一页」—— 而 BOSS 的 `history.back()` 正是靠
      「有没有上一页」决定要不要把页面退走。**这个修复会被自己抵消掉。**
      "修好了又被自己弄坏"这类回归必须由机器守住，不能靠注释。
    """
    target = "https://www.zhipin.com/job_detail/abc.html"
    context = _FakeContext([])
    _patch_playwright_with_context(monkeypatch, context)

    created: list[_FakePage] = []

    def _fake_create(port: int, url: str) -> str:
        page = _FakePage(url=url, target_id="T1")
        context.pages.append(page)
        created.append(page)
        return "T1"

    monkeypatch.setattr(cdp_module, "create_target", _fake_create)
    monkeypatch.setattr(ManualAdapter, "_polite_delay", lambda self: None)

    adapter = ManualAdapter(
        settings,
        platform="boss",
        use_cdp=True,
        cdp_endpoint=cdp_module.CdpEndpoint(port=9333, started=False, profile_dir=Path("x")),
    )
    detail = adapter.fetch_detail(target)

    assert created, "没有走「以目标 URL 新建标签页」这条路"
    assert detail is not None
    assert "goto" not in created[0].calls, (
        "对「已经停在目标 URL 上」的新页又 goto 了一次 —— 这会重新加上「上一页」，"
        "把 history.back() 的修复抵消掉"
    )
    forbidden = [call for call in created[0].calls if call in FORBIDDEN_PAGE_CALLS]
    assert not forbidden, f"新路径上调用了不该调用的方法：{forbidden}"


def test_open_target_page_ignores_an_already_open_tab_with_the_same_url(monkeypatch) -> None:
    """★ 调用前就开着同一个 URL 的标签页 → 必须挑**新开的那个**。

    ★ 为什么较真：用户自己那个标签页**带着历史**（`history.length=2`），
      读到它就等于回到了修复前的状态 —— 而且它「看起来成功」（内容读得到），
      只是内容随时会被站点退走。这种「静默地选错页」最难查，必须守住。

    ★★ 为什么新页**插在列表最前面**（而不是 append）：这不是为了刁难实现 ——
      实测过列表顺序**和创建顺序不一致**（`_dump-targets.py` case D：Playwright 报
      `pages[0]` 是岗位页，而 HTTP `/json/list` 里它在 `[1]`）。
      所以"取最后一个匹配"这种靠顺序的写法不可靠，
      **真正靠得住的机制是"按 target id 认页"** —— 这条用例就是钉它的。
      （写完先跑了一次变异：把过滤去掉，用例**照样绿** —— 说明它当时是条假守卫。
      改成"新页插在前面 + 按 target id 认"之后，去掉 id 认页就会红。）
    """
    target = "https://www.zhipin.com/job_detail/abc.html"
    old = _FakePage(url=target, target_id="OLD")
    context = _FakeContext([old])
    _patch_playwright_with_context(monkeypatch, context)

    def _fake_create(port: int, url: str) -> str:
        context.pages.insert(0, _FakePage(url=url, target_id="NEW"))  # ★ 插在最前面
        return "NEW"

    monkeypatch.setattr(cdp_module, "create_target", _fake_create)

    endpoint = cdp_module.CdpEndpoint(port=9333, started=False, profile_dir=Path("x"))
    with _fake_playwright_context().sync_playwright() as pw:  # type: ignore[attr-defined]
        _context, page = cdp_module.open_target_page(pw, endpoint, target)

    assert page is not old, "选中了调用前就开着的那一页（它带着历史，会被 history.back() 退走）"


def test_open_target_page_raises_when_the_new_tab_never_shows_up(monkeypatch) -> None:
    """★ 拿不到新页要**抛异常**，不能静默返回别的页 —— 调用方靠它决定要不要回退。"""
    context = _FakeContext([])
    _patch_playwright_with_context(monkeypatch, context)
    monkeypatch.setattr(cdp_module, "create_target", lambda port, url: "T1")
    monkeypatch.setattr(cdp_module, "_TARGET_READY_TIMEOUT_SECONDS", 0.05)

    endpoint = cdp_module.CdpEndpoint(port=9333, started=False, profile_dir=Path("x"))
    with (
        _fake_playwright_context().sync_playwright() as pw,  # type: ignore[attr-defined]
        pytest.raises(RuntimeError),
    ):
        cdp_module.open_target_page(pw, endpoint, "https://www.zhipin.com/job_detail/abc.html")


def test_release_page_closes_our_own_tab_but_never_the_last_one(settings, monkeypatch) -> None:
    """★ 批量路径收拾自己那一页；**只剩一个标签页时不关**（缺口 ① 的底线）。

    两条断言缺一不可：
      · 只测「会关」→ 哪天有人把 `close_page_if_not_last` 的护栏拆了，
        "关掉最后一个标签页 = 浏览器退出"这个老毛病会悄悄回来；
      · 只测「不关」→ 批量抓几十条会把窗口堆满（实测一个 BOSS 页几十兆内存）。
    """
    target = "https://www.zhipin.com/job_detail/abc.html"
    endpoint = cdp_module.CdpEndpoint(port=9333, started=False, profile_dir=Path("x"))
    monkeypatch.setattr(ManualAdapter, "_polite_delay", lambda self: None)

    # ---- 情形一：浏览器里还有别的标签页 → 关掉我们自己那一页 ----
    keep = _FakePage(url="https://www.zhipin.com/")
    context = _FakeContext([keep])
    _patch_playwright_with_context(monkeypatch, context)
    created: list[_FakePage] = []

    def _create(port: int, url: str) -> str:
        page = _FakePage(url=url, target_id="T1")
        context.pages.append(page)
        created.append(page)
        return "T1"

    monkeypatch.setattr(cdp_module, "create_target", _create)
    adapter = ManualAdapter(settings, use_cdp=True, cdp_endpoint=endpoint, release_page=True)
    adapter.fetch_detail(target)

    assert created and "close" in created[0].calls, "批量路径没有收拾自己新开的标签页"

    # ---- 情形二：它是唯一的标签页 → **不许关** ----
    solo_context = _FakeContext([])
    _patch_playwright_with_context(monkeypatch, solo_context)
    solo: list[_FakePage] = []

    def _create_solo(port: int, url: str) -> str:
        page = _FakePage(url=url, target_id="T2")
        solo_context.pages.append(page)
        solo.append(page)
        return "T2"

    monkeypatch.setattr(cdp_module, "create_target", _create_solo)
    solo_adapter = ManualAdapter(settings, use_cdp=True, cdp_endpoint=endpoint, release_page=True)
    solo_adapter.fetch_detail(target)

    assert solo and "close" not in solo[0].calls, (
        "把唯一一个标签页关掉了 —— Edge 关掉最后一个标签就整个退出，用户那个刚登录好的窗口会跟着没"
    )


# ===========================================================================
# 源码守卫：红线不许写进代码
# ===========================================================================
class _ScriptedPage:
    """按脚本返回内容的假页面：用来测"抢读"在**页面被关掉**时的行为。"""

    url = "https://example.com/job/1"

    def __init__(self, *, good_reads: int | None = None) -> None:
        #: 前几次能读，之后抛"页面已关"；None = 永远能读
        self._good_reads = good_reads
        self.reads = 0

    def content(self) -> str:
        self.reads += 1
        if self._good_reads is not None and self.reads > self._good_reads:
            raise RuntimeError("Target page, context or browser has been closed")
        return "<html><body>岗位职责：写代码</body></html>"

    def evaluate(self, script: str) -> str:
        return "岗位职责：写代码，熟悉 Python。" * 8

    def query_selector(self, selector: str) -> None:
        return None


def test_grab_live_page_keeps_the_last_snapshot_when_the_page_dies() -> None:
    """★★ 页面被站点关掉时，要用**最后一次拿到的快照**，而不是抛异常。

    ★ 为什么这条必须存在：BOSS 的页面渲染出来 1~2 秒后会把标签页**自己关掉**
      （实测：搜索页 1 秒、详情页 2 秒）。如果 `_grab_live_page` 在这时抛异常，
      那"内容明明已经拿到手"也会变成"抓取失败" —— 而失败信息
      （`Page.content: Target page, context or browser has been closed`）
      看起来像网络问题，会把下一个人往完全错误的方向带（实测被带偏过一次，
      先怀疑浏览器、又怀疑站点，最后才发现是"等太久、页面已经死了"）。
    """
    from jobhunter.adapters.manual import _grab_live_page

    page = _ScriptedPage(good_reads=2)
    html, rendered, _title, final_url = _grab_live_page(page, page.url)

    assert "岗位职责" in html, "页面被关掉时没有用上最后一次快照"
    assert rendered, "渲染文本也没拿回来"
    assert final_url == page.url


def test_grab_live_page_stops_once_the_content_stabilises() -> None:
    """★ 正常站点（页面不会自关）要**早点收工**，不能白等满 20 秒。

    ★ 为什么这条也必须有：只有"页面被关掉"这一个停止条件的话，
      对不会关页面的站点，每次抓取都会**空等满 `_GRAB_TIMEOUT_SECONDS`** ——
      20 条岗位就是 400 秒的无意义等待。这类"只有一半的停止条件"本项目吃过亏。
    """
    from jobhunter.adapters.manual import _grab_live_page

    page = _ScriptedPage()  # 永远能读，且内容每次都一样
    _grab_live_page(page, page.url)

    # 内容第 1 次读到、第 2 次一样就应触发"稳定"，第 3 次后收工
    assert page.reads <= 4, f"读了 {page.reads} 次才停 —— 没有用上'内容稳定'这个停止条件"


def test_search_page_rendered_predicate_ignores_the_footer_link() -> None:
    """★★ 「卡片渲染好了」的判据不能是 `"job_detail" in html` —— **页脚里就有一个**。

    ★ 实测踩到的（2026-09-21 21:5x，跑 `--pages 2` 时发现的）：

      搜索结果页的**页脚**有一个 `<a href="https://www.zhipin.com/job_detail/">职位搜索</a>`。
      于是 `"job_detail" in html` 在**岗位卡片还没渲染出来时就成立** →
      "抢读"立刻收工 → 抓到一张只有壳的页面（实测 202 KB、
      `/job_detail/` 只出现 **1 次**、含 `.html` 的 **0 个**）→
      最后报成"这一页没读到岗位链接"。

      表现是**时好时坏**：同一套代码有时 15 个链接、有时 0 个 ——
      而错误信息看起来像站点问题，会把人往完全错误的方向带。

    ★ 判据必须落到**真正的卡片链接**（`/job_detail/<id>.html`），
      也就是 `extract_job_links()` 用的那个正则。
    """
    from jobhunter.adapters.discover import _has_job_cards

    shell = (
        "<html><body><footer>"
        '<a href="https://www.zhipin.com/job_detail/">职位搜索</a>'
        "</footer></body></html>"
    )
    assert not _has_job_cards(shell), "只有页脚那个 job_detail 链接，不该算「卡片渲染好了」"

    with_card = shell + '<a href="/job_detail/abc123.html">Python 开发</a>'
    assert _has_job_cards(with_card), "有真正的卡片链接时应当判为「渲染好了」"


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

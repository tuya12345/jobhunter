"""本地 CDP：连到**用户自己的浏览器**去读页面（ISSUES #65，用户 2026-09-20 拍板）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 它解决什么：抓取不再"新起一个没有登录态的 headless 浏览器"                  ║
╚══════════════════════════════════════════════════════════════════════════╝

改前：`jh job add <BOSS 链接>` 新起一个 headless Chromium —— 它没有登录态，
BOSS 直接给登录页，于是工具「抓到了登录页」（#62 已修成明确报错，但那仍然是"抓不到"）。

现在：`jh job add --cdp <链接>` 连到**用户自己那个浏览器实例**，用它现有的会话说页面。
这不是"绕风控"——用的就是用户自己的浏览器、自己的会话，**没有伪造任何凭据**。

★★ 两个实测出来的现实约束（决定了 API 的形状）

    ① **Edge/Chrome 已经在运行时，`--remote-debugging-port` 完全无效**：
       新进程只是把标签页交给现有实例，端口根本不会起来（实测）。
       所以**不能**要求用户"先关掉浏览器"（体验太差），也不能硬连他日常那个实例。
       → 工具自己起一个**专用 profile**（`--user-data-dir=<state>/cdp-profile`）的实例：
         用户在里面登录一次 BOSS，之后一直可用；**完全不打扰他日常那个浏览器**。

    ② 那个专用目录里会有**真实登录态**（Cookie 等）。
       → 它必须落在 `state/` 下（`.gitignore` 已覆盖），并有测试守住这一点。

🔴 红线（有测试守着，验证会话也会用 AST 扫）

    1. **只读、只抓用户手动给的那一个链接**：CDP 让"翻页"技术上更容易，
       但本项目从 P2 起就不做批量/自动搜索 —— 这里**没有任何**"下一页"入口。
    2. **不自动登录、不填表单、不点任何东西**：本模块只做
       `connect_over_cdp` / `new_page` / `goto` / 读取内容。
    3. **不自动投递**（项目最高红线）。
    4. 不碰 `uiautomator2` / 模拟器那一套。

    ★ 第 1、2 条不只是"注释里承诺"：`tests/unit/test_cdp.py` 用假的 Playwright
      断言这些方法**从来没有被调用过**，另有一条 AST 守卫扫 `src/`。
"""

from __future__ import annotations

import contextlib
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobhunter.config.settings import AppSettings, get_settings
from jobhunter.utils.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "CdpEndpoint",
    "CdpProfileError",
    "cdp_profile_dir",
    "ensure_cdp_browser",
    "find_browser_executable",
    "is_cdp_listening",
]

#: 连上 CDP 之后等多久（毫秒）。给得宽松些：专用实例第一次启动要建 profile。
_CDP_PROBE_TIMEOUT_SECONDS = 20.0
#: 探测间隔
_CDP_PROBE_INTERVAL_SECONDS = 0.4


class CdpProfileError(ValueError):
    """专用 profile 的路径不安全（落在仓库里）——**拒绝启动浏览器**（JH-13）。"""


@dataclass(frozen=True)
class CdpEndpoint:
    """一个可用的 CDP 端点。"""

    port: int
    #: 这次是不是**新起**的实例（新起时要在终端告诉用户"去那个窗口登录一次"）
    started: bool
    profile_dir: Path
    executable: Path | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def cdp_profile_dir(settings: AppSettings | None = None) -> Path:
    """专用 profile 目录：`<state_dir>/cdp-profile`（**默认落在 state/ 下**）。

    ★ 为什么不放在 `data/` 或项目根：这里会有**真实登录态**（Cookie 等），
      而 `state/` 是三个数据目录里最"纯运行时"的那个（`.gitignore` 覆盖，
      且 `jh init` 不会往里写业务数据）。配置里可以覆盖，但默认值必须是安全的那一个。

    ★★ 覆盖值会被**检查**（只读代码审查 JH-13）：不允许落在某个 git 工作树里（`state/` 除外）

      为什么必须管：profile 里是 Cookie / localStorage / token ——
      用户把它配到工作树里（例如相对路径 `cdp-profile` 恰好落在项目根）之后，
      一次 `git add .` 就可能把登录态提交进版本库。那是一次**不可撤销**的泄露
      （就算马上删，历史里还在）。而"配置项接受任意路径"本身没有任何提示。

      允许的两类位置：
        · `state/` 目录下（含它自己）—— 它被 `.gitignore` 覆盖，这是默认布局；
        · **不在任何 git 仓库里**的目录（例如加密盘、临时目录）。

    Raises:
        CdpProfileError: 覆盖值解析后落在 git 工作树里、且不在 `state/` 下。
    """
    settings = settings or get_settings()
    if settings.browser.cdp_profile_dir is not None:
        candidate = _resolve_profile_dir(Path(settings.browser.cdp_profile_dir), settings)
        _reject_repo_profile(candidate, settings)
        return candidate
    return settings.paths.state_dir / "cdp-profile"


def _resolve_profile_dir(value: Path, settings: AppSettings) -> Path:
    """把配置里的 profile 目录解析成绝对路径。

    ★ 相对路径按 `config.yaml` 所在目录解析 —— 与 `settings.paths` 同一口径
      （只读代码审查 JH-05：相对路径挂在当前工作目录下，是"换目录运行就换个库"
      的同一个坑）。这里**不能**靠 `settings.paths` 顺手解决：那个属性只处理
      data/output/state 三个目录，profile 是 `browser.*` 里的独立配置项。
    """
    if value.is_absolute():
        return value
    from jobhunter.config.paths import default_paths

    anchor = (
        Path(settings.config_file).resolve().parent
        if settings.config_file
        else default_paths().root
    )
    return anchor / value


def _reject_repo_profile(profile: Path, settings: AppSettings) -> None:
    """profile 落在**某个 git 工作树里**（且不在 `state/` 下）→ 明确拒绝。

    ★ 判据是"在不在 git 工作树里"，而**不是**"在不在项目根下"：

      · 真正的危险是"一次 `git add .` 把登录态提交上去"——那要求这个目录位于
        **某个 git 仓库的工作树**内（worktree / 子模块 / 项目放在子目录里都算）；
      · 而"项目根"是个更宽的概念：config.yaml 放在 `D:\\configs\\` 时项目根就等于
        `D:\\configs`，那里根本没有 git —— 按项目根判会**误伤**一个完全安全的路径
        （本项目对"守卫收得太紧"同样敏感：误报的守卫最后会被绕过去）。

    ★ 为什么是**拒绝**而不是"警告一下继续跑"：警告会被忽略，而后果是
      "真实登录态进了版本库"——那件事一旦发生就无法收回。
      与"自动投递"同一条纪律：可疑路径宁可不开工，也不替用户冒不可逆的险。
    """
    target = profile.absolute()
    worktree = _git_worktree_of(target)
    if worktree is None:
        return

    state = settings.paths.state_dir.absolute()
    if target == state or state in target.parents:
        return  # 默认布局 / 用户自己配到 state 里 —— 已被 .gitignore 覆盖

    raise CdpProfileError(
        "browser.cdp_profile_dir 指到了**git 工作树里**，这会出问题：\n"
        f"    它指向：{target}\n"
        f"    它所在的仓库：{worktree}\n"
        "  为什么不行：这个目录里存的是浏览器的登录 Cookie / localStorage / token ——\n"
        "    放进工作树里，一次 `git add .` 就可能把它们提交上去，而且**删不干净**\n"
        "    （历史里还在）。这是本项目最不能碰的红线之一。\n"
        "  怎么办（二选一）：\n"
        f"    · 删掉这条配置，用默认值：{settings.paths.state_dir / 'cdp-profile'}"
        "（state/ 已被 .gitignore 覆盖）；\n"
        "    · 或者改成一个**不在任何 git 仓库里**的目录（加密盘 / 临时目录）的绝对路径。"
    )


def _git_worktree_of(path: Path) -> Path | None:
    """`path` 落在哪个 git 工作树里？不在任何仓库里时返回 None。

    ★ 只看 `.git` 是否存在（目录或**文件**都算：后者是 worktree / 子模块的写法），
      **不调用 git 命令** —— 这个判断在"启动浏览器之前"的热路径上，
      而且用户机器上不一定装了 git。
    """
    current = path if path.is_dir() else path.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def find_browser_executable(settings: AppSettings | None = None) -> Path | None:
    """找一个"支持 CDP 的浏览器"可执行文件。

    ★ 为什么要找而不是写死：用户机器上可能是 Edge（Windows 自带）或 Chrome，
      路径还随版本变（`Program Files` / `Program Files (x86)` / 用户目录）。
      找不到就**明确告诉用户怎么指定**（配置项 `browser.cdp_executable`），
      而不是抛一句 `FileNotFoundError`。
    """
    settings = settings or get_settings()
    if settings.browser.cdp_executable:
        candidate = Path(settings.browser.cdp_executable)
        return candidate if candidate.is_file() else None

    candidates = [
        # Windows：Edge 通常自带，Chrome 次之
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        # macOS
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for path in candidates:
        if Path(path).is_file():
            return Path(path)

    # Linux：按 PATH 找
    for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def is_cdp_listening(port: int, *, host: str = "127.0.0.1") -> bool:
    """端口上是否已经有 CDP 在听（用 TCP 连接判断，不需要 HTTP 客户端）。

    ★ 为什么不直接 GET `/json/version`：那要引入一次 HTTP 往返与超时处理，
      而"端口通不通"已经足够回答"要不要再起一个实例"。
      （真正的连接由 Playwright 的 `connect_over_cdp` 完成，连不上它会明确报错。）
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def ensure_cdp_browser(
    settings: AppSettings | None = None,
    *,
    on_message: Any = None,
) -> CdpEndpoint:
    """确保有一个**带专用 profile** 的浏览器在监听 CDP 端口，返回端点。

    ★ 已经在听 → **不重复起**（第二次跑 `--cdp` 应当直接连上去）。
      这既是为了快，也是为了不打扰用户已经登录好的那个实例。

    Args:
        on_message: 可选回调（CLI 用它打印"去那个窗口登录一次"的提示）。
    """
    settings = settings or get_settings()
    port = settings.browser.cdp_port
    profile = cdp_profile_dir(settings)

    if is_cdp_listening(port):
        logger.debug("CDP 已经在 %s 上监听，直接复用", port)
        return CdpEndpoint(port=port, started=False, profile_dir=profile)

    executable = find_browser_executable(settings)
    if executable is None:
        raise RuntimeError(
            "找不到可用的浏览器（Edge / Chrome）。\n"
            "  · 装了的话，在 config.yaml 里指定路径：\n"
            "      browser:\n"
            "        cdp_executable: C:/Program Files/Google/Chrome/Application/chrome.exe\n"
            "  · 或者改用不需要浏览器的两条路：jh job add --html 页面.html / --manual"
        )

    profile.mkdir(parents=True, exist_ok=True)
    if on_message is not None:
        on_message(
            f"正在打开一个**给工具用的**浏览器窗口（专用配置目录：{profile}）。\n"
            "  第一次用请在那个窗口里登录一次 BOSS/目标站点；之后就不用再登了。\n"
            "  它与你日常那个浏览器互不影响。"
        )

    # ★ 只起一个空白页 —— 具体岗位链接由调用方随后导航过去（见 `_render_page_via_cdp`）。
    #   这里**不**接受任何"要抓哪些页面"的列表：只服务"用户手动给的那一个链接"（红线 1）。
    subprocess.Popen(  # noqa: S603 - 路径来自用户配置/固定候选，且不经过 shell
        [
            str(executable),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.monotonic() + _CDP_PROBE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if is_cdp_listening(port):
            return CdpEndpoint(port=port, started=True, profile_dir=profile, executable=executable)
        time.sleep(_CDP_PROBE_INTERVAL_SECONDS)

    raise RuntimeError(
        f"浏览器起来了，但 CDP 端口 {port} 一直没有就绪。\n"
        "  · 可能端口被占用：换一个 `browser.cdp_port`（默认 9333）\n"
        "  · 也可能是那个浏览器版本不带 `--remote-debugging-port`（换 Edge/Chrome 正式版）"
    )


def cdp_page(playwright: Any, endpoint: CdpEndpoint) -> Any:
    """连上 CDP，拿一个**可用的页面**（优先复用已有页面）。

    ★ 为什么优先复用已有页面（缺口 ① 修完之后这条更重要）：
      抓完我们**不关页面**，于是第一次抓取留下的那个标签页就是"我们的页面" ——
      下次抓取复用它、导航到新链接，**一个标签页反复用**，不会越开越多。
      没有页面时才新建一个。

    ★ 这里**只**做"连接 + 取页面"，不做任何点击/填表 —— 红线 2。
    """
    browser = playwright.chromium.connect_over_cdp(endpoint.url)
    contexts = list(getattr(browser, "contexts", []) or [])
    context = contexts[0] if contexts else browser.new_context()
    pages = list(getattr(context, "pages", []) or [])
    if pages:
        return context, pages[0]
    return context, context.new_page()


def safe_close(page_or_context: Any) -> None:
    """关掉**我们自己创建**的页面/上下文（**不要**用它关用户浏览器里的东西）。

    ★★ 为什么这个函数现在几乎不该被调用（缺口 ① 的教训）

      它原本的用法是"抓完把刚用过的页面关掉，干净一点"。实测后果是：
      那个专用窗口**只有一个标签页**，而 **Edge/Chrome 关掉最后一个标签就整个退出**
      —— 用户刚登录好、跑一次抓取，窗口就消失了（CDP 端口随后不再监听），
      下次还得重新开、重新等。

      所以规则改成：**连过去的浏览器，我们什么都不关** ——
      页面留着（停在刚抓完的 URL 上，用户能看见工具读了哪个页面），下次复用它。
      这个函数只为"我们自己 `new_context()` 出来的东西"保留：那种情况关掉是对的
      （不关会漏资源）。

    ★ 无论如何都**不要** `browser.close()`：连过去的是用户自己的实例，
      关掉它等于替他关浏览器。
    """
    with contextlib.suppress(Exception):
        page_or_context.close()

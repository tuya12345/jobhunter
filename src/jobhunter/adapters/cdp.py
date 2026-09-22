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

🔴 边界（2026-09-21 用户明确授权后调整，见 docs/COMPLIANCE.md §5.8）

    1. ~~只抓用户手动给的那一个链接~~ → **已授权放开**：现在支持"自动搜索并批量抓取"
       （`jh job crawl`）。这是用户 2026-09-21 的明确决定（原话："我做这个就是为了爬岗位的"），
       推翻了 P2 的"不做批量/自动搜索"。**放开的是"发现岗位"，不是"替人投递"。**
    2. **仍然不点击、不填表、不自动登录**：本模块只做
       `connect_over_cdp` / `Target.createTarget` / `goto` / 读取内容。
       ★ 翻页靠**改 URL 参数**（`page=`），不靠点"下一页" —— 这既是纪律，也更稳。
    3. **不自动投递**（项目最高红线，**没有**被授权放开）：不点"立即沟通/投递"、
       不点赞/关注/私信。抓下来的岗位只是清单，投递按钮永远由人点。
    4. 不碰 `uiautomator2` / 模拟器那一套；不绕验证码/签名（不处理 `__zp_stoken__`、
       不伪造指纹、不注入反检测脚本）。

    ★ 第 2、3 条不只是"注释里承诺"：`tests/unit/test_cdp.py` 与
      `tests/verify/test_v_checklist12.py` 用假的 Playwright + AST 守卫扫 `src/`，
      断言 `click` / `fill` / `press` / `swipe` 这些**从来没有被调用过**。
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from jobhunter.config.settings import AppSettings, get_settings
from jobhunter.utils.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "CdpEndpoint",
    "CdpOwnerError",
    "CdpProfileError",
    "cdp_profile_dir",
    "close_page_if_not_last",
    "create_target",
    "ensure_cdp_browser",
    "ensure_our_browser",
    "find_browser_executable",
    "is_cdp_listening",
    "open_target_page",
]

#: 连上 CDP 之后等多久（毫秒）。给得宽松些：专用实例第一次启动要建 profile。
_CDP_PROBE_TIMEOUT_SECONDS = 20.0
#: 探测间隔
_CDP_PROBE_INTERVAL_SECONDS = 0.4


class CdpProfileError(ValueError):
    """专用 profile 的路径不安全（落在仓库里）——**拒绝启动浏览器**（JH-13）。"""


class CdpOwnerError(RuntimeError):
    """CDP 端口后面**不是**本工具的专用浏览器 —— 拒绝继续（#67 优先级 4）。

    ★ 为什么必须当场停下而不是"照常用"：连错浏览器 = 用**别人的登录态**去读页面，
      而那个实例的 profile 不是我们的（可能装着别人的账号、扩展、代理设置）。
      结果既不可复现，也说不清"这份数据到底是从哪个会话里读出来的"。
    """


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
            "  ★ 那个窗口里现在可能是**空白页**（批量抓取跑完会把工具开的标签页收掉，\n"
            "    剩下这个空白页）—— 在地址栏里敲站点域名、登录即可；它与你日常那个浏览器互不影响。"
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
            # ★ 记一个**归属标记**（2026-09-22）：这台机器上的 Edge 不支持
            #   `Browser.getBrowserCommandLine`，于是"端口后面是谁"证明不了。
            #   标记的作用是区分两种"证明不了"：**我们自己起过的**（放行，见
            #   `ensure_our_browser`）与**来源不明的**（停下）。
            _write_owner_marker(profile, port=port, executable=executable)
            return CdpEndpoint(port=port, started=True, profile_dir=profile, executable=executable)
        time.sleep(_CDP_PROBE_INTERVAL_SECONDS)

    raise RuntimeError(
        f"浏览器起来了，但 CDP 端口 {port} 一直没有就绪。\n"
        "  · 可能端口被占用：换一个 `browser.cdp_port`（默认 9333）\n"
        "  · 也可能是那个浏览器版本不带 `--remote-debugging-port`（换 Edge/Chrome 正式版）"
    )


def _browser_profile_of(browser: Any) -> str | None:
    """问浏览器自己："你的 `--user-data-dir` 是哪个？"（问不出来返回 None）。

    ★ 用 CDP 的 `Browser.getBrowserCommandLine`（browser 级会话），它返回**启动命令行**，
      里面有 `--user-data-dir=<profile>` —— 这是"端口后面是谁"唯一可靠的答案。
      `/json/version` 只有版本号与 UA，认不出实例。

    ★ 问不出来时**返回 None 而不是抛**：不同浏览器/版本对这个命令的支持不一，
      而"问不出来"不该变成新的失败点（调用方会按"不阻断"处理，并记一条日志）。
    """
    try:
        session = browser.new_browser_cdp_session()
        result = session.send("Browser.getBrowserCommandLine")
    except Exception:  # noqa: BLE001 - 老版本/受限环境问不出来
        return None
    arguments = (result or {}).get("arguments") or []
    if not isinstance(arguments, list):
        return None
    for argument in arguments:
        if isinstance(argument, str) and argument.startswith("--user-data-dir="):
            return argument.split("=", 1)[1]
    return ""


def _same_dir(left: str | Path, right: str | Path) -> bool:
    """两个路径是不是同一个目录（Windows 上大小写/斜杠都要容忍）。"""
    try:
        a = os.path.normcase(os.path.abspath(str(left)))
        b = os.path.normcase(os.path.abspath(str(right)))
    except (OSError, ValueError):  # pragma: no cover - 路径异常就当不同
        return False
    return a == b


def ensure_our_browser(browser: Any, endpoint: CdpEndpoint) -> None:
    """连上之后校验"**端口后面确实是我们那个专用实例**"（#67 优先级 4）。

    ╔══════════════════════════════════════════════════════════════════════╗
    ║ 为什么端口通不等于"能连"                                                 ║
    ╚══════════════════════════════════════════════════════════════════════╝

    `ensure_cdp_browser()` 只问了一句"端口上有没有人在听"。如果**另一个会话/另一个人**
    的浏览器占着 9333（同一个 9333 是完全可能的：同事、另一份 checkout、用户自己
    手动起的实例），我们会**静默连到别人的浏览器**上去读页面：
    用的不是我们的 profile、不是我们的登录态，而抓到的数据看起来一模一样。

    ★ 判据用的是**启动命令行里的 `--user-data-dir`**（见 `_browser_profile_of`）：
      它和 `cdp_profile_dir()` 给出的目录必须一致 —— 那是我们自己起的实例的标识。

    ★ 四种结果分开处理（不混成一句"连不上"）：

      · 命令行里的目录**就是**我们的 profile → `confirmed`，放行；
      · 命令行里**没有** `--user-data-dir`（返回空串）→ 判定为**别人的**实例：
        我们自己起实例时**一定**带这个参数（见 `ensure_cdp_browser`），
        所以"没有"就说明不是我们起的 → 抛错；
      · 有但不是我们的目录 → 判定为别人的实例 → 抛错；
      · 命令行**拿不到**（浏览器版本不支持这条命令，本机实测的 Edge 就是）→
        `unverifiable`。这时才看**归属标记**（`_write_owner_marker` 在起实例时写的）：
          - 标记在、且是我们这个 profile → 说明"本工具先前用这个 profile 起过实例"，
            **警告后放行**（不然第二次跑 `jh job crawl` 就会被自己拦下，功能等于不可用）；
          - 没有标记 → **来源不明 → 抛错**（宁可让用户换个端口，也不用别人的登录态读页面）。

    Raises:
        CdpOwnerError: 端口后面不是我们那个专用实例（或证明不了且不像我们起的）。
    """
    found = _browser_profile_of(browser)
    if found is None:
        marker = _read_owner_marker(endpoint.profile_dir)
        if marker is not None:
            logger.warning(
                "连上了 CDP 端口 %s：问不出启动命令行（这个浏览器版本不支持 "
                "Browser.getBrowserCommandLine），但专用 profile 里有本工具的归属标记"
                "（%s 起过）—— 按'是我们自己那个实例'放行。若你怀疑连错了浏览器："
                "换 browser.cdp_port 或关掉那个实例。",
                endpoint.port,
                marker.get("started_at", "?"),
            )
            return
        raise CdpOwnerError(
            f"CDP 端口 {endpoint.port} 上**已经有一个浏览器**，但无法确认它是不是本工具的专用实例："
            + "\n"
            f"    这个浏览器不支持那条查询命令（Browser.getBrowserCommandLine）\n"
            f"    而专用 profile（{endpoint.profile_dir}）里**没有**本工具的归属标记\n"
            "  为什么不停下不行：无法确认时连上去，可能是在**用别人的登录态**读页面 ——\n"
            "    抓到的内容既不可复现，也说不清是从哪个会话里读出来的。\n"
            "  怎么办（二选一）：\n"
            f"    · 换一个端口：config.yaml 里 `browser.cdp_port: 9334`，再重跑；\n"
            "    · 或者把占着这个端口、又不是本工具窗口的那个实例关掉，再重跑\n"
            "      （本工具自己的实例会在专用 profile 里留标记，不会被这条拦下）。"
        )
    if found and _same_dir(found, endpoint.profile_dir):
        return

    shown = found or "（没有 --user-data-dir，是一个用默认配置的浏览器）"
    raise CdpOwnerError(
        f"CDP 端口 {endpoint.port} 后面**不是本工具的专用浏览器**，已停下：\n"
        f"    它用的配置目录：{shown}\n"
        f"    本工具期望的是：{endpoint.profile_dir}\n"
        "  为什么不停下不行：那样会**用别人的登录态**去读页面 —— 抓到的内容既不可复现，\n"
        "    也说不清是从哪个会话里读出来的（而且那个实例可能装着别的账号/扩展/代理）。\n"
        "  怎么办（二选一）：\n"
        f"    · 换一个端口：config.yaml 里 `browser.cdp_port: 9334`，再重跑；\n"
        "    · 或者把占着这个端口的那个实例关掉（先确认它不是本工具的窗口），再重跑。"
    )


#: 归属标记的文件名（写在**专用 profile 目录**里，那里已经 gitignore）
_OWNER_MARKER_NAME = "jobhunter-owner.json"


def _owner_marker_path(profile: Path) -> Path:
    return Path(profile) / _OWNER_MARKER_NAME


def _write_owner_marker(profile: Path, *, port: int, executable: Path | None) -> None:
    """记下"本工具用这个 profile 在哪个端口起过实例"（写不进去只记日志，不影响抓取）。"""
    payload = {
        "tool": "jobhunter",
        "port": port,
        "profile": str(profile),
        "executable": str(executable) if executable else None,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        _owner_marker_path(profile).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:  # pragma: no cover - 磁盘/权限问题不该让抓取失败
        logger.warning("写归属标记失败（不影响抓取）：%s", exc)


def _read_owner_marker(profile: Path) -> dict[str, Any] | None:
    """读归属标记；没有/坏了都返回 None（**不猜**：读不出来就当"来源不明"）。"""
    try:
        raw = _owner_marker_path(profile).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) and payload.get("tool") == "jobhunter" else None


def cdp_page(playwright: Any, endpoint: CdpEndpoint) -> Any:
    """连上 CDP，拿一个**可用的页面**（优先复用已有页面）。

    ★ 为什么优先复用已有页面（缺口 ① 修完之后这条更重要）：
      抓完我们**不关页面**，于是第一次抓取留下的那个标签页就是"我们的页面" ——
      下次抓取复用它、导航到新链接，**一个标签页反复用**，不会越开越多。
      没有页面时才新建一个。

    ★ 这里**只**做"连接 + 取页面"，不做任何点击/填表 —— 红线 2。
    """
    browser = playwright.chromium.connect_over_cdp(endpoint.url)
    # ★ 先确认"连上的是我们那个实例"（#67 优先级 4）——连错浏览器要用别人的登录态，
    #   那比"抓不到"糟得多，所以这一步在拿页面之前。
    ensure_our_browser(browser, endpoint)
    contexts = list(getattr(browser, "contexts", []) or [])
    context = contexts[0] if contexts else browser.new_context()
    pages = list(getattr(context, "pages", []) or [])
    if pages:
        return context, pages[0]
    return context, context.new_page()


# ---------------------------------------------------------------------------
# ★★ 以目标 URL"出生"的标签页 —— 2026-09-21 加，这是"能抓到 BOSS 岗位页"的关键
# ---------------------------------------------------------------------------
#: 调 CDP 的 HTTP 接口时的超时
_CDP_HTTP_TIMEOUT_SECONDS = 15.0
#: 等新标签页出现在 `context.pages` 里的上限
_TARGET_READY_TIMEOUT_SECONDS = 12.0
#: 轮询间隔
_TARGET_POLL_SECONDS = 0.25


def _page_key(url: str) -> str:
    """URL 的"主体"（scheme + host + path），用来在标签页列表里认出"我们刚开的那个"。

    ★ 为什么要丢掉查询串：站点会改写它 —— 实测 BOSS 把
      `…/jobs?query=Python&city=101281600&page=2` 改写成 `…/jobs?query=Python`。
      丢掉之后，同一条搜索的第 1 页与第 2 页的 key **完全一样**，所以真正的判据是
      "key 对得上、而且**不是我开之前就存在的那一页**"，见 `open_target_page`。
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def create_target(port: int, url: str) -> str | None:
    """让浏览器**以 `url` 为第一项历史**新建一个标签页，返回它的 target id。

    ╔══════════════════════════════════════════════════════════════════════╗
    ║ ★★ 为什么必须"以目标 URL 出生"，而不是"开空白页再 goto 过去"          ║
    ╚══════════════════════════════════════════════════════════════════════╝

    BOSS 的岗位页里有个定时器在反复执行 **`history.back()`**：只要标签页存在
    "上一页"，页面渲染 1~4 秒后就会被**退走**（退回 `about:blank` 或站点首页）。
    2026-09-20 实测三种开页方式（登录态，同一台机器、同一个站点）：

        · 复用已有标签页 + `goto`   → `history.length = 2` → **被退走**
        · `new_page()` + `goto`     → `history.length = 2` → **被退走**
        · ★ **以目标 URL 出生**     → `history.length = 1` → **退无可退，稳定 14 秒+**

    机制证明（三步）：被退走后 `history.length` 不变（**不是 push**）；
    `go_forward()` 能前进回岗位页（**说明是 `back()` 而不是 `location.replace`**）；
    前进回去 2 秒后又被退走（**是反复触发的定时器**）。

    ★ 这里用 CDP 的 **HTTP** 接口 `PUT /json/new?<url>` 而不是再拉一条 CDP 会话：
      `Target.createTarget{url}` 等价，但 HTTP 版更省事（不用管 session 生命周期），
      而且打的是 `127.0.0.1`，不产生任何对外请求。

    ★ 注意 `PUT`（不是 GET）：较新的 Chromium 对 `/json/new` 只接受 PUT，
      GET 会返回 405。这个细节踩过一次。

    ★ 用 `http.client` 而不是 `urllib`：它**从不经过代理**（本机回环没必要绕代理），
      而且调用名不会撞上"文本 IO 必须显式写 encoding"那道 AST 守卫 ——
      `opener.open(...)` 会被那条守卫当成"裸 `open()` 文件"，实测被误报过一次
      （`tests/unit/test_encoding_lint_guard.py`）。守卫认名字，那就换个不歧义的名字。
    ★★ 目标 URL 必须**百分号编码**（2026-09-21 实测踩到的坑）：
      `/json/new?<url>` 里的 `<url>` 是**查询串的值**，所以第一个 `&` 会被当成
      "下一个参数"的分隔符 —— URL 在那里被**截断**。
      实测：`…/jobs?query=Python&city=101281600&page=2` 开出来的标签页停在
      `…/jobs?query=Python`（`city` / `page` 全丢），搜索条件根本没生效。
      编码之后 Chrome 会自己解回来。
    """
    encoded = quote(url.split("#", 1)[0], safe="")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=_CDP_HTTP_TIMEOUT_SECONDS)
    try:
        connection.request("PUT", f"/json/new?{encoded}")
        response = connection.getresponse()
        status = response.status
        raw = response.read()
    finally:
        connection.close()

    if status != 200:
        raise RuntimeError(f"新建标签页失败：HTTP {status}")

    payload = json.loads(raw.decode("utf-8"))
    target_id = payload.get("id") if isinstance(payload, dict) else None
    return target_id if isinstance(target_id, str) else None


def open_target_page(playwright: Any, endpoint: CdpEndpoint, url: str) -> tuple[Any, Any]:
    """开一个"以 `url` 出生"的标签页并返回 `(context, page)`（见 `create_target` 的说明）。

    ★ 与 `cdp_page()` 的区别：`cdp_page()` 复用**已有**页面（那页面必然带着历史），
      这个函数开的是**新页、且第一项历史就是 `url`** —— 差别就是"能不能抓到"。

    ★★ 顺序很关键：**必须先建标签页、再连 Playwright**。

      实测（`_diag-newtarget.py`）：`connect_over_cdp` **之后**再 `PUT /json/new`
      建出来的 target，Playwright **看不见** —— `context.pages` 里 12 秒都没有它。
      反过来（先建后连）就能看到，因为连上去时会枚举一遍已存在的 target。

    ★★ 认页用 **target id**（`create_target` 的返回值），不靠 URL、不靠顺序：

        · **URL 不可靠**：站点会改写查询串 —— 实测 BOSS 把
          `…/jobs?query=Python&city=101281600&page=2` 改写成 `…/jobs?query=Python`，
          于是"我们刚开的页"和"上一次留下的页"URL **一模一样**；
        · **顺序不可靠**：实测 Playwright 报的 `pages[0]` 与 HTTP `/json/list`
          的 `[0]` 不是同一个目标。

      `create_target` 返回的 id 是这个标签页唯一的身份，`Target.getTargetInfo`
      又能问出每一页的真实 id —— 两边对得上就一定是它。

    ★ 拿不到就**抛异常**（而不是静默返回一个别的页）—— 调用方会回退到老路径，
      并把这件事记进日志（见 `manual._render_page_via_cdp`）。
    """
    wanted = _page_key(url)
    target_id = create_target(endpoint.port, url)

    browser = playwright.chromium.connect_over_cdp(endpoint.url)
    # ★ 同 `cdp_page`：连上的第一件事是确认"这是我们那个实例"（#67 优先级 4）。
    ensure_our_browser(browser, endpoint)
    contexts = list(getattr(browser, "contexts", []) or [])
    context = contexts[0] if contexts else browser.new_context()
    pages = list(getattr(context, "pages", []) or [])

    candidates = [page for page in pages if wanted and wanted in (getattr(page, "url", "") or "")]

    # ① 只有一个 URL 对得上 → **直接用**，不查 target id。
    #    ★ 为什么要省这一下：BOSS 的搜索页渲染出来约 1 秒后就把标签页关掉
    #      （见 `discover._wait_for_job_cards` 的实测），而每个 `_target_id_of()`
    #      都是一次 CDP 往返 —— 多花的时间就是"抢不到"的概率。
    #      "只有一个候选"时 URL 已经足够确定，没必要再问一次。
    if len(candidates) == 1:
        return context, candidates[0]

    # ② 多个候选（或 URL 一个都对不上）→ 才用 **target id** 精确认页
    if target_id:
        for page in candidates or pages:
            if _target_id_of(context, page) == target_id:
                return context, page

    # ③ 兜底：拿不到 target id 时（假 context / 老浏览器）取最后一个候选
    if candidates:
        return context, candidates[-1]

    raise RuntimeError(f"新标签页没有就绪（target id={target_id}，页数={len(pages)}）：{url}")


def _target_id_of(context: Any, page: Any) -> str | None:
    """问出这个页面真实的 CDP **target id**（拿不到就返回 None）。

    ★ 为什么必须用 target id 认页 —— 见 `open_target_page` 的说明：
      URL 会被站点改写、列表顺序也不保证，只有 target id 是唯一身份。
    """
    try:
        session = context.new_cdp_session(page)
    except Exception:  # noqa: BLE001 - 假 context / 老浏览器都可能没有这个能力
        return None
    try:
        info = session.send("Target.getTargetInfo")
        value = (info or {}).get("targetInfo", {}).get("targetId")
        return value if isinstance(value, str) else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        with contextlib.suppress(Exception):
            session.detach()


def close_page_if_not_last(context: Any, page: Any) -> bool:
    """关掉**我们自己刚开的**那个标签页；**它是最后一个就不关**。

    ★★ 这条规则的由来（缺口 ① 的教训，别改回去）：
      Edge/Chrome **关掉最后一个标签就整个退出**。所以"抓完把页面关掉"看着干净，
      实际会把用户刚登录好的专用窗口一起关掉。判据因此是
      **"关了之后还剩不剩标签页"**，而不是"这个页是不是我们开的"。

    ★ 为什么批量抓取需要它：`--cdp` 抓一条时"什么都不关"是对的（用户能看见
      工具读了哪个页面）；但自动搜索一次几十条，每条留一个标签页会把窗口堆满
      （实测一个 BOSS 页几十兆内存）。所以**批量路径**在每条抓完后收拾自己那一页，
      单条路径仍然什么都不关。

    Returns:
        真的关掉了 → True；因为"是最后一个"或出错而没关 → False。
    """
    try:
        if len(list(getattr(context, "pages", []) or [])) <= 1:
            return False
        page.close()
        return True
    except Exception:  # noqa: BLE001 - 关不掉不是错误，最坏就是多留一个标签页
        return False


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

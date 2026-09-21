"""【专项核查】设计文档/评审明确点名的 12 个坑，逐条落成可执行的断言。

约定：
    · 已到期的项 → 断言"修好了"（该绿就绿，红就是没修）。
    · **尚未到期的项（P4/P5/P7/P9）** → 断言"诚实"：
      功能可以没做，但**不能假装做了**（`not_implemented` + 非 0 退出码）。
      这是"不静默失真"纪律在阶段边界上的体现。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobhunter.cli.app import app
from jobhunter.cli.common import EXIT_NOT_IMPLEMENTED
from jobhunter.config.paths import find_project_root

runner = CliRunner()


def _root() -> Path:
    return find_project_root()


# ===========================================================================
# □1 .gitignore 覆盖 *.db-wal / *.db-shm
# ===========================================================================
def test_gitignore_covers_wal_and_shm_and_secrets() -> None:
    """依据：设计文档 §七.6 + 评审采纳项 #7
    「.db-wal/.db-shm 含真实数据却没被忽略 → 只忽略 *.db 等于把真实投递记录提交上去」。"""
    text = (_root() / ".gitignore").read_text(encoding="utf-8")
    for pattern in (
        ".env",
        "*.db",
        "*.db-wal",
        "*.db-shm",
        "data/",
        "output/",
        "state/",
        "*.docx",
        "*.pdf",
    ):
        assert pattern in text, f".gitignore 少了 {pattern}"


def test_sensitive_paths_are_actually_ignored_by_git(tmp_path: Path) -> None:
    """不只看文件里有没有写：让 git 自己回答"这个文件会被忽略吗"。

    同时验证一个最容易踩的坑：`.gitignore` 里写 `*resume*` 会把
    `src/jobhunter/services/resume_svc.py` 也一起忽略掉（.gitignore 自己的注释提到过）。
    """
    root = _root()
    probes = {
        "data/jobhunter.db-wal": True,
        "data/jobhunter.db-shm": True,
        "data/jobhunter.db": True,
        ".env": True,
        "output/今日清单.md": True,
        "state/llm.json": True,
        "src/jobhunter/services/resume_svc.py": False,
        "tests/verify/_helpers.py": False,
        "pyproject.toml": False,
    }
    for relative, should_ignore in probes.items():
        result = subprocess.run(  # noqa: S603 - 固定参数，无用户输入
            ["git", "check-ignore", "-q", relative],  # noqa: S607 - git 在 PATH 上
            cwd=root,
            capture_output=True,
            check=False,
        )
        ignored = result.returncode == 0
        assert ignored is should_ignore, (
            f"{relative}: 期望 ignored={should_ignore}，实际 {ignored}"
            "（src/ 下的业务代码被误忽略是最危险的一种）"
        )


def test_no_hardcoded_api_key_in_repository() -> None:
    """依据：设计文档 §七.6「密钥放 .env，不入库」+ 验收标准 2「无硬编码密钥」。

    只看**代码里的字符串**（AST 判定，跳过文档字符串）：真实 key 形如 `sk-` + 长串。
    """
    import re

    from tests.verify._helpers import code_strings, python_sources

    key_like = re.compile(r"sk-[A-Za-z0-9]{20,}")
    offenders: list[str] = []
    for path in python_sources(_root()):
        for lineno, text in code_strings(path):
            for match in key_like.finditer(text):
                offenders.append(f"{path.relative_to(_root())}:{lineno}: {match.group()[:12]}…")
    assert not offenders, "疑似硬编码密钥：\n" + "\n".join(offenders)


def test_dotenv_is_not_tracked_by_git() -> None:
    """依据：同 §七.6。.env 一旦被提交，密钥就进了 git 历史（删掉也还在）。"""
    result = subprocess.run(  # noqa: S603
        ["git", "ls-files", "--error-unmatch", ".env"],  # noqa: S607
        cwd=_root(),
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0, ".env 被 git 跟踪了 —— 密钥会进仓库历史"


# ===========================================================================
# □2 SQLite 真的开了 WAL + foreign_keys=ON + busy_timeout
# ===========================================================================
def test_pragma_triplet_is_live_on_every_connection(db_url: str) -> None:
    """依据：评审采纳项 #6。连开三条连接都要成立（PRAGMA 是连接级的）。"""
    from jobhunter.storage.db import create_db_engine

    engine = create_db_engine(db_url, ensure_directory=False)
    try:
        for _ in range(3):
            with engine.connect() as conn:
                assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
                assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
                assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
    finally:
        engine.dispose()


# ===========================================================================
# □3 日志脱敏 / □4 强制 UTF-8 / □5 UTC 存 + 北京展示
# ===========================================================================
def test_redaction_filter_is_attached_to_every_handler() -> None:
    """依据：设计文档 §七.6「日志脱敏」。

    逐个 handler 检查（而不是看有没有"装过"的标记位）——
    因为 pytest 等框架会在我们之后往 root 上加自己的 handler，那个 handler 会漏。
    """
    import logging

    from jobhunter.utils.logging import RedactingFilter, setup_logging

    setup_logging("INFO")
    root = logging.getLogger()
    handlers = list(root.handlers)
    assert handlers
    naked = [h for h in handlers if not any(isinstance(f, RedactingFilter) for f in h.filters)]
    assert not naked, f"这些 handler 没有脱敏 Filter：{naked}"


def test_io_module_always_specifies_encoding() -> None:
    """依据：评审采纳项 #8「统一 utils/io.py 强制 UTF-8」+ `PLW1514`。

    AST 扫描**裸文件读写**（`open` / `os.fdopen` / `builtins.open`）：
    不允许出现没写 `encoding=` 的调用。
    项目自己的 `utils/io.py::read_text/write_text` 是**封装**（内部强制 UTF-8），
    调用它不算违规 —— 那正是设计要求的写法。
    """
    from tests.verify._helpers import call_details, python_sources

    offenders: list[str] = []
    for path in python_sources(_root() / "src"):
        for lineno, name, _nargs, keywords in call_details(path):
            if name in {"open", "builtins.open", "os.fdopen", "fdopen", "io.open"} and (
                "encoding" not in keywords
            ):
                offenders.append(f"{path.relative_to(_root())}:{lineno}: {name}(...) 没写 encoding")
    assert not offenders, (
        "有裸文件读写没显式指定 encoding（Windows cp936 崩溃的根源）：\n" + "\n".join(offenders)
    )


def test_time_helpers_are_utc_aware_and_locale_displayed() -> None:
    """依据：评审采纳项 #9「统一存 UTC aware，展示转 Asia/Shanghai；不用已弃用的 utcnow()」。

    ★ 只把 `datetime.now()`（**零位置参数**，= naive 本地时间）与 `utcnow()` 判为违规：
      `datetime.now(UTC)` 是正确写法，不能误伤。
    """
    from tests.verify._helpers import call_details, python_sources

    offenders: list[str] = []
    for path in python_sources(_root() / "src"):
        for lineno, name, nargs, _keywords in call_details(path):
            if name.endswith("utcnow"):
                offenders.append(
                    f"{path.relative_to(_root())}:{lineno}: {name}()（已弃用且 naive）"
                )
            if name.endswith(".now") and nargs == 0:
                offenders.append(
                    f"{path.relative_to(_root())}:{lineno}: {name}()（naive 本地时间）"
                )
    assert not offenders, "出现了 naive/已弃用的取时间方式：\n" + "\n".join(offenders)


# ===========================================================================
# □7 缓存 key 含简历 content_hash（已在正常路径用例里动态验证）
# □8 技能匹配有词边界（已在正常路径与边界用例里验证）
# □9 硬条件不满足是降分（已在正常路径用例里验证）
# 这里只补一条"口径没被偷偷改写"的静态守卫
# ===========================================================================
def test_scoring_constants_still_encode_penalty_not_elimination() -> None:
    """依据：评审采纳项 #13「硬条件'淘汰'会漏岗 → 改大幅降分」。

    守卫：不允许出现 0 分的"不满足"档（0 分等于变相淘汰）。
    """
    from jobhunter.services import match_svc

    for name in (
        "HARD_MISS_RATIO",
        "HARD_CONFLICT_RATIO",
        "DIRECTION_MISMATCH_RATIO",
        "CITY_OTHER_RATIO",
    ):
        value = getattr(match_svc, name)
        assert value > 0, f"{name} = {value}，0 分等于变相淘汰（违反降分不淘汰的纪律）"
        assert value < 1, f"{name} = {value}，惩罚档不该给满分"


# ===========================================================================
# □12 CI 是 ubuntu + windows 双平台
# ===========================================================================
def test_ci_runs_on_both_ubuntu_and_windows() -> None:
    """依据：评审采纳项 #18「CI 只有 Ubuntu（'虚假安全感'）→ 加 windows-latest」。"""
    text = (_root() / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "ubuntu-latest" in text, "CI 没跑 ubuntu"
    assert "windows-latest" in text, "CI 没跑 windows（本项目的真实战场就是 Windows）"
    assert "ruff check" in text and "pytest" in text


def test_ci_installs_docx_extra_so_resume_tests_actually_run() -> None:
    """★ 覆盖率陷阱（ISSUE #16，已修）：`uv sync --locked` **不装** optional extras，
    而简历/docx 相关的测试文件顶部写了 `pytest.importorskip("docx")` ——
    不装 extra 时这些测试在 CI 上会被**静默跳过**，
    于是"CI 在 windows-latest 真跑简历解析"这件事从来没有发生过。

    实测过：把那三个文件里的 73 条测试整体跳过，而 CI 依然显示绿。
    """
    text = (_root() / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    sync_lines = [line for line in text.splitlines() if "uv sync" in line]
    assert sync_lines, "CI 里找不到 uv sync"
    joins = " ".join(sync_lines)
    assert "--all-extras" in joins or "--extra docx" in joins, (
        f"CI 的依赖安装没有装 docx extra：{sync_lines}\n"
        "而 tests 里用 importorskip('docx') → 简历解析相关测试会被静默跳过"
    )


# ===========================================================================
# 合规红线：没做的功能必须"诚实地说没做"
# ===========================================================================
def test_collection_commands_moved_out_and_the_cli_says_so() -> None:
    """★★ 这一条**取代**了原先「`leads add/probe/capture` 还是占位」的三条断言。

    **P7 在 2026-09-19 改了架构**（用户决定）：采集端独立部署（`projects/leadhunter`，命令 `lh`），
    本仓库只做**消费端**（`jh leads import` + `jh leads serve`）。
    所以那三条占位断言的前提没了 —— 不是"实现推进了所以摘掉"，
    而是**那三个命令已经不在这个仓库里了**。

    ★ 换成的判据盯的是**老用户会真正撞上的事**：
      他用惯了这个仓库，敲 `jh leads add` 时会看到什么？
      · 必须是明确的"没有这个命令"（退出码 2），**不能**静默成功、
        也不能给一个看不懂的内部错误；
      · 而且他得能知道去哪 —— `jh leads --help` 里要能看出这里只剩导入与页面。
    """
    gone = runner.invoke(app, ["leads", "add", "x.txt"])
    assert gone.exit_code != 0, (
        "`jh leads add` 居然成功退出了 —— 采集功能已经搬走，这里不该还能整理线索"
    )
    assert "No such command" in gone.output or "没有" in gone.output, (
        f"敲已经不存在的采集命令时，报的不是『没有这个命令』：\n{gone.output}"
    )

    # 剩下的两个命令必须就是消费端该有的那两个
    help_text = runner.invoke(app, ["leads", "--help"]).output
    assert "import" in help_text and "serve" in help_text, (
        f"`jh leads` 下没有 import/serve —— 消费端只有这两件事：\n{help_text}"
    )
    for old in ("add", "probe", "capture", "export"):
        assert f"\n│ {old} " not in help_text, (
            f"`{old}` 还在 `jh leads --help` 的命令列表里 —— 采集类命令已经搬走了：\n{help_text}"
        )

    # 页面那两个红线要在帮助里看得见（它们是这个阶段的合规承诺）
    assert "只读" in help_text or "标记投递" in help_text, (
        f"`jh leads serve` 的帮助里看不出『只读 + 一个写动作』这条边界：\n{help_text}"
    )


@pytest.mark.parametrize(
    ("argv", "feature"),
    [
        (["apply", "3=applied"], "批量打标"),
        (["apply", "--pending"], "待处理列表"),
        (["tailor", "brief", "1"], "定制简历 brief"),
        (["tailor", "list"], "定制版本列表"),
        (["stats"], "统计"),
        (["weak"], "薄弱点"),
        (["offer", "compare"], "Offer 比较"),
        (["report"], "周报"),
        (["cal", "export"], "日程导出"),
        (["followup", "1"], "跟进话术"),
    ],
)
def test_p45_commands_are_no_longer_placeholders(argv: list[str], feature: str) -> None:
    """阶段边界（P4.5 / P5 / P6 的代码已落地时）：这些命令**必须**已经不是占位。

    为什么单独一条：占位清单把它们移出去了，而功能测试在各自的阶段文件里。
    这条就是那道"两头都不管"的缝的临时塞子 —— 至少保证
    "被移出占位清单"与"真的实现了"同时成立。
    ⚠️ 注意别把 P9 的 `demo` 也放进来（它仍是占位）。
    ⚠️ P7 的 `leads *` **不在**这里 —— 它们在 2026-09-19 的架构变更里
       **搬出了本仓库**（采集端独立部署），
       见上面 `test_collection_commands_moved_out_and_the_cli_says_so`。
    """
    result = runner.invoke(app, argv)
    assert result.exit_code != EXIT_NOT_IMPLEMENTED, (
        f"{feature}（{' '.join(argv)}）仍是占位（退出码 {result.exit_code}），"
        f"却已从占位清单里移出：\n{result.output}"
    )


#: 浏览器/设备自动化对象的**变量名**（Playwright 的 page/locator、uiautomator2 的 d…）。
#: 见 `test_no_auto_apply_or_platform_write_calls_in_source` 的说明。
_AUTOMATION_RECEIVERS = frozenset(
    {
        "page",
        "locator",
        "frame",
        "element",
        "element_handle",
        "handle",
        "context",
        "browser",
        "d",
        "device",
        "driver",
    }
)


def _is_automation_write_call(name: str) -> bool:
    """判断一个调用是不是"作用在浏览器/设备对象上的写操作"。

        ★ 为什么要看**接收者**，而不是只看方法名（P4.5 实测踩到的假阳性）：
    +      第一版把任何叫 `check` / `fill` / `press` 的调用都判成违规 ——
          而 `check` 是个太普通的名字（开发会话的 `_settle_versions` 里就有一个内部函数叫 `check`，
          被迫改名成 `violations_of` 才过我这道守卫）。
          这个守卫的**真问题**是"有没有人在页面上点/填"，所以必须要求调用链里
          出现自动化对象名（`page.click` / `locator.fill` / `d.swipe` /
          `page.locator("x").first.click` 都能命中；裸 `check(x)` 不会）。
          ⚠️ 修的是"误报"，**不是**放宽红线：真·页面写操作一个都不会漏。
    """
    parts = name.split(".")
    if len(parts) < 2:
        return False  # 裸函数调用不是页面操作
    return any(part in _AUTOMATION_RECEIVERS for part in parts[:-1])


def test_no_auto_apply_or_platform_write_calls_in_source() -> None:
    """★★ 合规红线（最高优先）：**绝不做全自动投递 / 写操作**（设计文档 §七.1、§七.5）。

    AST 扫描所有**函数调用**：不允许出现"点击/输入/勾选/按键"这类会产生写操作的动作。
    只查 Call 节点 —— 注释与文档字符串里出现"投递/打招呼"是**声明不做**，不算违规。
    另需接收者是自动化对象（见 `_is_automation_write_call`），避免"随便一个叫 check 的函数"误报。
    """
    from tests.verify._helpers import call_names, python_sources

    banned_methods = {
        "click",
        "dblclick",
        "tap",
        "send_keys",
        "fill",
        "check",
        "uncheck",
        "press",
        "select_option",
        "set_input_text",
        "long_click",
        "swipe",
        "drag",
    }
    offenders: list[str] = []
    for path in python_sources(_root()):
        for lineno, name in call_names(path):
            if name.rsplit(".", 1)[-1] not in banned_methods:
                continue
            if _is_automation_write_call(name):
                offenders.append(f"{path.relative_to(_root())}:{lineno}: {name}(...)")
    assert not offenders, (
        "源码里出现了作用在浏览器/设备对象上的'写操作'调用"
        "（点击、输入、勾选、滑动都在禁列）：\n" + "\n".join(offenders)
    )


def test_automation_write_detector_catches_real_patterns() -> None:
    """元测试：上面那个守卫**必须真的能抓到**页面写操作（否则它就是一条永远绿的假守卫）。

    同时断言它不再误报普通的同名函数 —— 这正是 P4.5 让开发会话被迫改名的那个假阳性。
    """
    should_flag = [
        "page.click",
        "page.locator('x').first.click",
        "locator.fill",
        "self.page.press",
        "d.swipe",
        "device.long_click",
    ]
    should_not_flag = [
        "check",
        "fill",
        "_gate_reasons",
        "greeting_svc.parse_greeting",
        "violations_of",
    ]
    for name in should_flag:
        assert _is_automation_write_call(name), f"{name} 应当被判为页面写操作（漏报）"
    for name in should_not_flag:
        assert not _is_automation_write_call(name), f"{name} 被误判成页面写操作（假阳性）"


#: HTTP 客户端的接收者名（只有这些身上的 .post/.put/.patch/.delete 才是"出站写请求"）。
_HTTP_CLIENT_RECEIVERS = frozenset(
    {"client", "http", "httpx", "requests", "session_http", "aiohttp", "resp", "request"}
)


def _is_http_write_call(name: str) -> bool:
    """判断一个调用是不是「出站 HTTP 写请求」。

    ★ 为什么要看**接收者**（P6 实测踩到的第二个假阳性）：
      第一版只看方法名结尾是不是 `.post/.put/.patch/.delete` ——
      结果把 SQLAlchemy 的 **`session.delete(...)`**（删一行数据）判成了
      "向招聘平台发写请求"，而触发它的"证据"还是**我自己测试文件里的 zhipin fixture URL**。
      这个守卫要防的是"脚本替人点投递"，那是 HTTP 出站；数据库删行完全无关。

    ★ 顺带把扫描范围收紧到 `src/` + `spikes/`（见调用方）：
      合规红线约束的是**产品行为**，而测试里出现招聘站 URL 是必然的（fixture 就是抓来的页面）。
      只在测试目录里发现 URL 字符串，说明不了任何事。
    """
    parts = name.split(".")
    if len(parts) < 2 or parts[-1] not in {"post", "put", "patch", "delete"}:
        return False
    return any(part in _HTTP_CLIENT_RECEIVERS for part in parts[:-1])


def test_no_network_write_endpoints_other_than_llm() -> None:
    """★★ 合规红线：出站 HTTP 调用只允许打给大模型，不允许打给招聘平台。

    招聘平台的"投递/打招呼"一定是某个 HTTP 请求；把出站目标限制在 LLM 域名，
    是"不做自动投递"在代码层面的**兜底证据**。
    """
    from tests.verify._helpers import call_names, code_strings, python_sources

    allowed_hosts = ("api.deepseek.com", "deepseek.com", "localhost", "127.0.0.1", "schema.org")
    job_board_hosts = ("zhipin.com", "boss", "51job", "zhaopin", "lagou", "liepin", "maimai")

    offenders: list[str] = []
    # ★ 只扫产品代码（src/ + spikes/）：测试里的招聘站 URL 是 fixture，不是行为
    targets = [*python_sources(_root() / "src"), *python_sources(_root() / "spikes")]
    for path in targets:
        writes = [(lineno, name) for lineno, name in call_names(path) if _is_http_write_call(name)]
        if not writes:
            continue
        for text in (text for _, text in code_strings(path)):
            mentions_board = any(host in text for host in job_board_hosts)
            if not (mentions_board and "http" in text):
                continue
            if any(safe in text for safe in allowed_hosts):
                continue
            # 招聘域名出现在字符串里是允许的（用于识别平台/解析 URL），
            # 但**不能**与 client.post/put/patch 出现在同一文件里
            for lineno, name in writes:
                offenders.append(
                    f"{path.relative_to(_root())}:{lineno}: {name}(...) "
                    f"与招聘域名同文件（{text[:40]}…）"
                )
    assert not offenders, "疑似向招聘平台发写请求：\n" + "\n".join(offenders)


def test_http_write_detector_catches_real_patterns() -> None:
    """元测试：上面那个守卫**必须真的能抓到**出站写请求，同时不再误报 DB 会话。

    这是同一个教训的第二次（第一次是 `check`/Playwright 那个守卫）：
    **合规守卫太宽会逼着开发改无关代码，太窄则形同虚设** ——
    所以每个守卫都要配一条双向元测试。
    """
    should_flag = ["client.post", "httpx.delete", "self.client.put", "requests.patch"]
    should_not_flag = ["session.delete", "session.get", "obj.delete", "store.delete", "check"]
    for name in should_flag:
        assert _is_http_write_call(name), f"{name} 应当被判为出站写请求（漏报）"
    for name in should_not_flag:
        assert not _is_http_write_call(name), f"{name} 被误判成出站写请求（假阳性）"


def test_no_captcha_bypass_or_signature_reverse_engineering() -> None:
    """★★ 合规红线：不绕验证码 / 不逆向签名（设计文档 §七.2）。

    允许出现 `zp_stoken` 这个词（代码里用它做"被风控"的**检测**，失败即停），
    但不允许出现真正做绕过/反检测的库、JS 注入或指纹抹除手法。
    """
    from tests.verify._helpers import call_names, code_strings, python_sources

    banned_names = {
        "execjs",
        "add_init_script",
        "add_init_script_on_new_document",
        "evaluate_handle",
        "set_extra_http_headers",
    }
    banned_text = ("navigator.webdriver", "stealth", "geetest", "webdriver = undefined")

    offenders: list[str] = []
    for path in python_sources(_root() / "src"):
        for lineno, name in call_names(path):
            if name.rsplit(".", 1)[-1] in banned_names:
                offenders.append(f"{path.relative_to(_root())}:{lineno}: {name}(...)")
        for lineno, text in code_strings(path):
            for token in banned_text:
                if token in text:
                    offenders.append(f"{path.relative_to(_root())}:{lineno}: 字符串含 {token!r}")
    assert not offenders, "出现了疑似绕过验证码/反检测的手法：\n" + "\n".join(offenders)


def test_the_search_boundary_matches_the_authorization() -> None:
    """★★ 合规边界：自动搜索**已被用户授权**（2026-09-21），所以这条不再是"禁止"，
    而是"**授权之后，那几条没被放开的边界仍然守着**"。

    ★ 这条用例 2026-09-21 **被改写**过，原因必须留在这里：

      它原本叫 `test_adapters_do_not_implement_search_paging`，断言
      「`ManualAdapter.search()` 抛 `NotImplementedError`」—— 那守的是 P2 的红线
      "不做自动搜索翻页"。

      用户 2026-09-21 明确推翻了那条红线（"我做这个就是为了爬岗位的"）。
      功能从**新模块** `adapters/discover.py` 走了，而 `ManualAdapter.search()`
      **仍然**抛 `NotImplementedError` —— 于是原断言**形式通过、实质失效**：
      它守的那条路，已经不是功能走的那条路了。

      （这一点是交接文档 §5.2 主动报告的，它没有假装那条守卫还有效。）

    ★ 判据因此换成"**边界还在不在**"，而不是"有没有这个功能"。
    """

    from jobhunter.adapters.manual import ManualAdapter

    # ① 主路径没有被偷偷改掉：`ManualAdapter.search()` 仍然明确报"未支持"，
    #    发现能力独立成模块。这样"人给链接"那条路的行为没变。
    adapter = ManualAdapter()
    with pytest.raises(NotImplementedError):
        adapter.search("AI Agent", "深圳")

    # ② 发现层存在，但**不点页面** —— 授权放开的是"自动搜索"，不是"自动点击"：
    #    翻页靠 URL 的 page= 参数，不点"下一页"。
    discover = find_project_root() / "src" / "jobhunter" / "adapters" / "discover.py"
    assert discover.is_file(), "发现层不见了 —— 若自动搜索被移除，这条用例要一起改"
    source = discover.read_text(encoding="utf-8")
    for forbidden in (".click(", ".fill(", ".press(", "select_option(", "mouse."):
        assert forbidden not in source, (
            f"发现层里出现了页面操作 `{forbidden}` —— 放开的是「自动搜索」，"
            "不是「自动点击」（翻页必须靠 URL 参数）"
        )

    # ③ 「不自动投递」这条最高红线：由 `test_v_adversarial.py` / `test_v_checklist12.py`
    #    里那几条"扫描写操作调用"的用例守着（它们扫的是**调用**，不是词）。
    #    ★ 我原本在这里加了一条 `git grep 投递|deliver` —— 它**误报了**：
    #      命中"不做自动投递"这句**禁止性注释**本身。同一个坑本项目记过一次
    #      （#56：守卫只按词面匹配，就会把"说明不做某事"当成"做了某事"）。
    #      判据太粗就不该留，所以删掉，交给那几条扫调用的用例。

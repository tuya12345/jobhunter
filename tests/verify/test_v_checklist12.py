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


@pytest.mark.xfail(
    reason="ISSUE #16（🟡）：CI 的 uv sync 不装 docx extra，而测试用 importorskip → 静默跳过",
    strict=False,
)
def test_ci_does_not_install_optional_extras_but_tests_importorskip() -> None:
    """★ 覆盖率陷阱：`uv sync --locked` **不装** optional extras，
    而简历/docx 相关的测试文件顶部写了 `pytest.importorskip("docx")` ——
    也就是说这些测试在 CI 上会被**静默跳过**，
    于是 CI 里"windows-latest 真跑简历解析"这件事实际没有发生。

    期望：CI 安装 extras（`--all-extras` / `--extra docx`），
    或把 python-docx 提升为必需依赖。
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
@pytest.mark.parametrize(
    ("argv", "feature"),
    [
        (["apply", "3=applied"], "投递打标"),
        (["today"], "待投清单"),
        (["tailor", "brief", "1"], "定制简历"),
        (["leads", "probe"], "App 线索探测"),
        (["leads", "capture"], "App 线索抓屏"),
        (["leads", "add", "x.txt"], "线索整理"),
        (["demo"], "演示"),
        (["followup", "1"], "跟进话术"),
        (["stats"], "统计"),
        (["offer", "compare"], "Offer 比较"),
        (["cal", "export"], "日程导出"),
        (["report"], "周报"),
    ],
)
def test_unimplemented_features_report_honestly(argv: list[str], feature: str) -> None:
    """依据：README「其余命令仍是占位，执行时会明确提示'该功能在 PX 阶段实现'，不会假装成功」。

    这条同时是合规审计的一部分：**占位就是占位** ——
    只要这些命令还在 `not_implemented`，就不存在"自动投递"的可能性。
    """
    result = runner.invoke(app, argv)
    expected = EXIT_NOT_IMPLEMENTED
    assert result.exit_code == expected, (
        f"{feature}（{' '.join(argv)}）的退出码是 {result.exit_code}（期望 {expected}）\n"
        f"{result.output}"
    )
    assert "阶段" in result.output or "P4" in result.output or "P5" in result.output, (
        f"{feature} 没有告诉用户'这个功能在哪个阶段实现'：\n{result.output}"
    )


def test_no_auto_apply_or_platform_write_calls_in_source() -> None:
    """★★ 合规红线（最高优先）：**绝不做全自动投递 / 写操作**（设计文档 §七.1、§七.5）。

    AST 扫描所有**函数调用**：不允许出现"点击/输入/勾选/按键"这类会产生写操作的动作。
    只查 Call 节点 —— 注释与文档字符串里出现"投递/打招呼"是**声明不做**，不算违规。
    """
    from tests.verify._helpers import call_names, python_sources

    banned_calls = {
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
            short = name.rsplit(".", 1)[-1]
            if short in banned_calls:
                offenders.append(f"{path.relative_to(_root())}:{lineno}: {name}(...)")
    assert not offenders, (
        "源码里出现了疑似'写操作/自动投递'的调用"
        "（Playwright/uiautomator 的点击、输入、勾选都在禁列）：\n" + "\n".join(offenders)
    )


def test_no_network_write_endpoints_other_than_llm() -> None:
    """★★ 合规红线：出站 HTTP 调用只允许打给大模型，不允许打给招聘平台。

    招聘平台的"投递/打招呼"一定是某个 HTTP 请求；把出站目标限制在 LLM 域名，
    是"不做自动投递"在代码层面的**兜底证据**。
    """
    from tests.verify._helpers import call_names, code_strings, python_sources

    allowed_hosts = ("api.deepseek.com", "deepseek.com", "localhost", "127.0.0.1", "schema.org")
    job_board_hosts = ("zhipin.com", "boss", "51job", "zhaopin", "lagou", "liepin", "maimai")

    offenders: list[str] = []
    for path in python_sources(_root()):
        writes = [
            (lineno, name)
            for lineno, name in call_names(path)
            if name.endswith((".post", ".put", ".patch", ".delete"))
        ]
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


def test_adapters_do_not_implement_search_paging() -> None:
    """★★ 合规红线：主路径是"人给链接"，自动搜索翻页**不做**（设计文档 §七.2）。

    `JobSource.search()` 应当明确抛 NotImplementedError（"一个明确报错的未支持，
    比一个偷偷返回空列表的假实现好得多"）。
    """
    from jobhunter.adapters.manual import ManualAdapter

    adapter = ManualAdapter()
    with pytest.raises(NotImplementedError):
        adapter.search("AI Agent", "深圳")

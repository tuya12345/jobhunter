"""CLI 骨架测试：守住"命令形状"这个产品契约。

★ 为什么命令形状需要测试守

    CLI 就是这份工具的"产品说明书"：`jh --help` 一打开，用户就知道最终能得到什么。
    而它极容易被**静默**破坏 —— 踩过的两个真实坑：

    ① `app.add_typer(app)` 忘了传 `name` → Typer 把子命令**摊平到上一层**，
       只发一条 UserWarning 就继续跑。结果是 `jh --help` 里出现一堆孤立的
       `add` / `list` / `export`，而 `jh today`、`jh weak` 这些命令**整体消失** ——
       非常容易被误认为"还没实现"。

    ② 单命令子应用写成 `@app.command()` 而不是 callback →
       `jh today --limit 5` 报 `No such option: --limit`
       （根命令不接受选项，它以为这是给子命令的选项）。

    这两类问题的共同点是"程序不报错、只表现得很怪"，所以必须由测试守住形状。

另外还守着一条产品原则：**占位命令不假装成功**。
    P0 阶段 18 个命令还没实现，如果它们静默返回 0，用户会以为"跑过了、没数据"。
    所以断言：退出码非 0 + 输出里必须出现"该功能尚未实现"和目标阶段。
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from jobhunter import CURRENT_PHASE, __version__
from jobhunter.cli.app import app
from jobhunter.cli.commands import COMMAND_NAMES, COMMANDS
from jobhunter.cli.common import EXIT_NOT_IMPLEMENTED

runner = CliRunner()

# 19 个顶层命令（写死在这里是刻意的：改动命令集合时这条测试会提醒你
# "这是一个对外契约的变化，确认一下是不是有意的"）
EXPECTED_COMMANDS = (
    "init",
    "resume",
    "job",
    "match",
    "gap",
    "today",
    "apply",
    "interview",
    "question",
    "assessment",
    "offer",
    "weak",
    "stats",
    "report",
    "followup",
    "cal",
    "tailor",
    "leads",
    "demo",
)

# P0 阶段应该报"未实现"的命令（除 init 外全部）
PLACEHOLDER_INVOCATIONS: list[list[str]] = [
    ["today"],
    ["gap"],
    ["weak"],
    ["stats"],
    ["report"],
    ["demo"],
    ["followup", "1"],
    ["resume", "list"],
    ["job", "list"],
    ["match", "run"],
    ["apply", "3=applied", "7=replied"],
    ["apply", "1", "--status", "applied"],
    ["apply", "--pending"],
    ["apply", "--timeline", "1"],
    ["interview", "list"],
    ["question", "predict", "1"],
    ["assessment", "list"],
    ["offer", "compare"],
    ["cal", "export"],
    ["tailor", "brief", "1"],
    ["leads", "export"],
]


# ---------------------------------------------------------------------------
# ① 命令形状
# ---------------------------------------------------------------------------
def test_help_lists_every_command() -> None:
    """`jh --help` 必须列出全部 19 个命令。"""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for name in EXPECTED_COMMANDS:
        assert name in result.output, f"jh --help 里缺少命令：{name}"


def test_registry_matches_expected_commands() -> None:
    """注册表与预期集合完全一致（顺序也一致）。"""
    assert COMMAND_NAMES == EXPECTED_COMMANDS


def test_no_orphan_subcommands_at_top_level() -> None:
    """★ 顶层不能出现孤立的子命令名。

    这是坑 ① 的守卫：如果某个 add_typer 漏了 name，
    `add` / `list` / `export` / `compare` / `mark` 这些**子命令**名会跑到顶层来。
    它们只会出现在子命令列表里，绝不会是顶层命令。
    """
    names = {name for name, _ in COMMANDS}
    orphans = {"add", "list", "mark", "timeline", "export", "compare", "probe", "capture"}
    assert not (orphans & names), (
        f"顶层出现了孤立的子命令名：{sorted(orphans & names)} —— 很可能是某个 add_typer 忘了传 name"
    )


def test_every_registered_app_has_a_name() -> None:
    """每个注册项都必须有非空的命令名。"""
    for name, command_app in COMMANDS:
        assert name and name.strip(), f"命令名为空：{command_app}"
        assert name.islower(), f"命令名应小写：{name}"


@pytest.mark.parametrize(
    "invocation",
    [
        ["today", "--limit", "5"],
        ["gap", "--top", "5"],
        ["weak", "--top", "3"],
        ["stats", "--days", "7"],
        ["tailor", "brief", "1"],
        ["job", "add", "https://example.com/job/1"],
        ["apply", "3=applied", "7=replied"],
    ],
)
def test_single_command_subapps_accept_options(invocation: list[str]) -> None:
    """★ 坑 ② 的守卫：单命令子应用必须能正常接收选项。

    如果 today/weak/stats/gap 被写成 `@app.command()`，
    这里会因为 `No such option: --limit` 之类而失败。
    """
    result = runner.invoke(app, ["--no-remind", *invocation])
    # 占位命令会以 2 退出，但绝不能是"用法错误"（用法错误的输出里有 "No such option"）
    assert "No such option" not in result.output, result.output
    assert "Usage:" not in result.output or result.exit_code == EXIT_NOT_IMPLEMENTED, result.output


def test_subcommands_are_reachable() -> None:
    """子命令的 `--help` 能正常打印（说明嵌套结构正确）。

    ★ 这里**故意不包含** `apply`：`jh apply` 是单命令应用
      （因为组回调带位置参数会让子命令完全无法分派，见 apply_cmd.py 的说明）。
      `jh apply --help` 仍然可用，见下面的断言。
    """
    for parent, child in [
        ("resume", "split"),
        ("job", "add"),
        ("match", "explain"),
        ("tailor", "export"),
        ("leads", "probe"),
        ("interview", "add"),
        ("offer", "add"),
    ]:
        result = runner.invoke(app, [parent, child, "--help"])
        assert result.exit_code == 0, f"{parent} {child} --help 失败：{result.output}"


def test_single_command_apps_show_help() -> None:
    """单命令应用的 --help 可用。

    覆盖 gap / today / weak / stats / report / followup / demo / apply ——
    它们都是"组回调 + invoke_without_command"的结构；
    一旦有人改回 @app.command()，--help 或选项解析就会坏掉。
    """
    for name in ("apply", "gap", "today", "weak", "stats", "report", "followup", "demo"):
        result = runner.invoke(app, [name, "--help"])
        assert result.exit_code == 0, f"jh {name} --help 失败：{result.output}"


# ---------------------------------------------------------------------------
# ② 占位命令：不假装成功
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("invocation", PLACEHOLDER_INVOCATIONS)
def test_placeholder_commands_report_not_implemented(invocation: list[str]) -> None:
    """占位命令必须以非 0 退出，并说清"未实现 + 计划阶段"。"""
    result = runner.invoke(app, ["--no-remind", *invocation])
    assert result.exit_code == EXIT_NOT_IMPLEMENTED, (
        f"{invocation} 应以 {EXIT_NOT_IMPLEMENTED} 退出，实际 {result.exit_code}\n{result.output}"
    )
    assert "该功能尚未实现" in result.output
    # 必须指出计划阶段（P1…P9），否则用户不知道要等多久
    assert any(f"P{n}" in result.output for n in range(1, 10)), result.output
    # 必须指出当前阶段与可用命令，给出下一步动作
    assert CURRENT_PHASE in result.output
    assert "jh init" in result.output


def test_init_is_not_a_placeholder() -> None:
    """`init` 是 P0 唯一真实现的命令 —— 它不能报"未实现"。"""
    result = runner.invoke(app, ["--no-remind", "init"])
    assert "该功能尚未实现" not in result.output
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# ③ 全局 callback
# ---------------------------------------------------------------------------
def test_reminder_shown_by_default() -> None:
    """默认情况下，任何命令开头都显示待处理提醒（P0 是占位文案）。"""
    result = runner.invoke(app, ["today"])
    assert "待处理" in result.output


def test_no_remind_flag_suppresses_reminder() -> None:
    """`--no-remind` 必须能关掉提醒（不能关的话，脚本里用它就很难受）。"""
    result = runner.invoke(app, ["--no-remind", "today"])
    assert "待处理" not in result.output


def test_version_flag() -> None:
    """`--version` 打印版本与当前阶段。"""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output
    assert CURRENT_PHASE in result.output


def test_help_without_args() -> None:
    """不给任何参数时显示帮助（no_args_is_help），而不是报错。"""
    result = runner.invoke(app, [])
    assert "Usage" in result.output or "Commands" in result.output


def test_custom_config_is_loaded(config_file) -> None:
    """`--config` 指向的配置文件会被真正读取。"""
    custom = config_file.parent / "custom.yaml"
    custom.write_text(
        'keywords: ["自定义关键词"]\n'
        'cities: ["杭州"]\n'
        "threshold: 77\n"
        "match_weights: {direction: 30, hard: 30, skill: 40}\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["--no-remind", "--config", str(custom), "init"])
    assert result.exit_code == 0, result.output
    # 通过配置驱动的输出间接验证（jh init 会打印目录与表数量）
    assert "11 张" in result.output


def test_invalid_config_gives_readable_error(config_file) -> None:
    """★ 配置写错时必须给出**可读**报错，而不是 pydantic 的长篇堆栈。

    权重和必须等于 100 —— 这是最容易写错、后果最隐蔽的一处
    （所有岗位的 rule_score 会整体偏移）。
    """
    broken = config_file.parent / "broken.yaml"
    broken.write_text(
        'keywords: ["x"]\n'
        'cities: ["y"]\n'
        # 30+30+30 = 90 ≠ 100
        "match_weights: {direction: 30, hard: 30, skill: 30}\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["--no-remind", "--config", str(broken), "init"])
    assert result.exit_code == 1
    assert "配置" in result.output
    assert "100" in result.output


def test_missing_config_file_is_rejected(tmp_path) -> None:
    """不存在的 --config 路径应该被 click 挡在参数校验阶段。"""
    result = runner.invoke(app, ["--config", str(tmp_path / "nope.yaml"), "init"])
    assert result.exit_code != 0

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

另外还守着两条产品原则：
    ① **占位命令不假装成功**。P0 阶段 18 个命令还没实现，如果它们静默返回 0，
       用户会以为"跑过了、没数据"。所以断言：退出码非 0 + 输出里必须出现
       "该功能尚未实现"和目标阶段。
    ② **非 UTF-8 控制台下也不能崩**（见文件末尾的 test_cli_survives_non_utf8_console）。
       这条是 GitHub Actions 的 windows-latest 实测逼出来的：它的控制台是 cp1252，
       `jh --help` 里的 `→` 和 emoji 会让 rich 抛 UnicodeEncodeError，
       于是 **CLI 在英文 Windows 上直接不可用**。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobhunter import CURRENT_PHASE, __version__
from jobhunter.cli.app import app
from jobhunter.cli.commands import COMMAND_NAMES, COMMANDS
from jobhunter.cli.common import EXIT_NOT_IMPLEMENTED

runner = CliRunner()

# 顶层命令（写死在这里是刻意的：改动命令集合时这条测试会提醒你
# "这是一个对外契约的变化，确认一下是不是有意的"）
#   ★ 2026-09-19 加了 `cache`（ISSUES #25：llm_cache 原来没有清理入口、也看不见命中率）
EXPECTED_COMMANDS = (
    "init",
    "resume",
    "job",
    "match",
    "cache",
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

# 尚未实现的命令（已实现的不在此列）
#
# ★ 不在名单里的命令组（已经真能跑）：
#     · `init`（P0）
#     · `resume` 三个子命令（P1）
#     · `job` 四个子命令（P2）
#     · `match` 三个子命令 + `gap`（P3）
#     · `today`（P4）与 `apply 序号/--job --status …`（P4）
#   它们在没有数据库时会报"请先运行 jh init"（exit 1）—— 那是**正确的**行为，
#   由 `test_*_commands_are_implemented` 那几条用例单独覆盖。
#
# ★ `apply` 的所有语法现在都是真功能了（P4/P4.5/P6）：批量打标、`--pending`、
#   `--timeline`、模糊匹配 —— 分别由 test_followup.py 与 test_p6_tracking.py 覆盖。
# ★ P5 之后 `tailor brief` 也移出名单（它是真功能了，由 test_tailor_cli.py / test_tailor_svc.py
#   覆盖）。**移出意味着必须由功能测试接手**，不能两头都不管 —— 这正是这个名单的维护纪律。
# ★ P6 之后移出的（都由本阶段的功能测试接手）：
#     `weak` / `stats` / `report` / `followup` / `apply --timeline` /
#     `interview list` / `question predict` / `assessment list` / `offer compare` / `cal export`
#     → 分别由 test_p6_tracking.py（状态机/事件流/统计/时间轴/薄弱点/周报/追问）、
#       test_assessment_offer.py、test_cal.py 覆盖。
# ★ P7 落地之后（ISSUE #59）**再也没有占位命令了**：
#   `jh leads` 从"add/probe/capture/export 四个占位"变成"import/serve 两个真功能"，
#   采集类命令整体搬去了独立部署 `leadhunter`（命令 `lh`）。
#
#   ★ 所以这个名单现在是**空的**，而且下面那条用例不只是"没人可测"——
#     它反过来断言"本仓库不再有任何'该功能尚未实现'的入口"：
#     将来谁再加占位命令，得先把这条纪律重新想一遍（占位命令不假装成功）。
PLACEHOLDER_INVOCATIONS: list[list[str]] = []


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
        ("leads", "import"),
        ("leads", "serve"),
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
@pytest.mark.parametrize("invocation", PLACEHOLDER_INVOCATIONS or [pytest.param([], marks=[])])
def test_placeholder_commands_report_not_implemented(invocation: list[str]) -> None:
    """占位命令必须以非 0 退出，并说清"未实现 + 计划阶段"。

    ★ 现在 `PLACEHOLDER_INVOCATIONS` 是**空的**（P7 落地后本仓库没有占位命令了），
      所以这条用例在空名单下退化成一条"没有可测对象"的用例；
      真正接替它的是下面 `test_no_placeholder_commands_remain`。
      保留函数是为了让"占位命令该怎么表现"这条约定留在代码里（将来再加占位时照它写）。
    """
    if not invocation:
        pytest.skip("本仓库已经没有占位命令（见 PLACEHOLDER_INVOCATIONS 的说明）")
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


def test_no_placeholder_commands_remain() -> None:
    """★ P7 落地后：**本仓库一个占位命令都不该剩**。

    为什么把它写死：占位命令的价值是"功能没做，但不假装做了"。
    现在所有阶段（P0–P7 消费端 + P9）都落地了，再出现"该功能尚未实现"
    只可能是**有人加了新占位**或**把已实现的命令改回占位** —— 两种都该被看见。

    ★ 采集类命令（`jh leads add/probe/capture/export`）已经不在这里了：
      它们搬去了独立部署 `leadhunter`（命令 `lh`）。所以这条用例同时也确认
      "没有把采集命令搬回来"（红线之一：那些命令要装模拟器依赖）。
    """
    for invocation in (
        ["leads", "add", "x.txt"],
        ["leads", "probe"],
        ["leads", "capture"],
        ["leads", "export"],
    ):
        result = runner.invoke(app, ["--no-remind", *invocation])
        assert result.exit_code == 2, f"{invocation} 不该还存在：{result.output}"
        assert "No such command" in result.output, result.output


def test_init_is_not_a_placeholder() -> None:
    """`init` 是 P0 唯一真实现的命令 —— 它不能报"未实现"。"""
    result = runner.invoke(app, ["--no-remind", "init"])
    assert "该功能尚未实现" not in result.output
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    ("invocation", "expected_hint"),
    [
        (["resume", "list"], "jh init"),
        (["resume", "add", "x.docx"], "找不到文件"),
        (["resume", "split", "1"], "jh init"),
        (["job", "list"], "jh init"),
        (["job", "show", "1"], "jh init"),
        (["job", "skip", "1"], "jh init"),
    ],
)
def test_implemented_commands_are_not_placeholders(
    invocation: list[str], expected_hint: str
) -> None:
    """★ 已实现的命令（P1 `resume` / P2 `job`）必须**不再是占位**。

    没有数据库时应提示"请先运行 jh init"；文件不存在时应提示"找不到文件" ——
    但都不是"该功能尚未实现"。这两种提示对用户的含义完全不同：
    前者是"你少做了一步 / 参数不对"，后者是"这功能还没做"。
    """
    result = runner.invoke(app, ["--no-remind", *invocation])
    assert "该功能尚未实现" not in result.output, result.output
    assert result.exit_code == 1, result.output
    assert expected_hint in result.output


# ---------------------------------------------------------------------------
# ③ 全局 callback
# ---------------------------------------------------------------------------
def test_reminder_is_silent_when_there_is_nothing_to_do() -> None:
    """★ P4.5 后的正确行为：**没有待处理事项时一个字都不打印**。

    每天都被"你没有被提醒"刷屏，第三天就会把整个提醒关掉 ——
    然后连真有提醒时也看不见了。所以"空 → 静默"是刻意的。
    （本用例环境里没有数据库，`get_reminders()` 退化成空汇总。）
    """
    result = runner.invoke(app, ["today"])
    assert "📌" not in result.output


def test_no_remind_flag_suppresses_reminder() -> None:
    """`--no-remind` 必须能关掉提醒（不能关的话，脚本里用它就很难受）。"""
    result = runner.invoke(app, ["--no-remind", "today"])
    assert "📌" not in result.output


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


# ---------------------------------------------------------------------------
# ④ 编码健壮性：非 UTF-8 控制台下也不能崩（★ CI 实测逼出来的回归测试）
# ---------------------------------------------------------------------------
def _project_root() -> Path:
    """项目根（pyproject.toml 所在目录）。"""
    for candidate in (Path(__file__).resolve(), *Path(__file__).resolve().parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise AssertionError("找不到项目根（pyproject.toml）")


@pytest.mark.parametrize("io_encoding", ["cp1252", "gbk", "ascii"])
def test_cli_survives_non_utf8_console(io_encoding: str) -> None:
    """★ 在**非 UTF-8 控制台**下，`jh --help` 必须正常退出且不乱码。

    这个测试来自一次真实的 CI 失败：
        GitHub Actions 的 `windows-latest`（英文 Windows）控制台编码是 **cp1252**，
        而 `jh --help` 的文案里有 `→`、`·`、`⚠️`、emoji ——
        rich 往 cp1252 流里写这些字符时抛
        `UnicodeEncodeError: 'charmap' codec can't encode characters in position 12-15`，
        于是 **`jh --help` 整个命令以非 0 退出**。
        也就是说：CLI 在英文 Windows 上是完全不可用的，而这在任何中文开发机上
        都复现不出来（中文 Windows 是 cp936，也编不了这些字符，但触发时机不同）。

    修法：在包导入时就调用 `force_utf8_output()`（见 utils/io.py），
    在**任何 Console 被构造之前**把 stdio 切成 UTF-8。

    ★ 为什么要用子进程测：`PYTHONIOENCODING` 只影响新起的解释器进程，
      而 pytest 已经捕获了 sys.stdout（无法真实模拟"cp936 管道"）。
      必须起一个真进程、给它一个真的 cp1252 stdio，才能验证这条路径。

    覆盖三种"编不了中文"的编码：
        cp1252 英文 Windows 控制台 / gbk 理论上有中文但也有编不了的字符 / ascii 极端情况
    """
    root = _project_root()
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = io_encoding
    env["PYTHONPATH"] = str(root / "src")
    env["JH_CONFIG"] = str(root / "config.yaml")
    # 用临时数据目录，确保这个子进程不会碰真实库
    env["JH_DATA_DIR"] = str(root / "state" / "_encoding_test")

    result = subprocess.run(
        [sys.executable, "-m", "jobhunter", "--no-remind", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(root),
        timeout=120,
        check=False,
    )

    assert "UnicodeEncodeError" not in result.stderr, (
        f"在 {io_encoding} 控制台下报了编码错误：\n{result.stderr[-2000:]}"
    )
    assert result.returncode == 0, (
        f"在 {io_encoding} 控制台下 jh --help 退出码是 {result.returncode}\n"
        f"stderr:\n{result.stderr[-2000:]}"
    )
    # 关键命令必须真的打印出来了（而不是崩在半路只输出了一半）
    for name in ("init", "today", "apply", "tailor"):
        assert name in result.stdout, f"{io_encoding} 下 --help 输出缺少命令 {name}"
    # 中文文案要能以 UTF-8 正确取出（证明我们改的是编码，不是把中文丢掉）
    assert "求职" in result.stdout


def test_force_utf8_output_is_idempotent() -> None:
    """`force_utf8_output()` 必须可以重复调用（包导入 + 入口点都会调）。"""
    from jobhunter.utils.io import ENCODING, force_utf8_output

    force_utf8_output()
    force_utf8_output()
    assert sys.stdout.encoding.lower().replace("-", "") == ENCODING.replace("-", "")

"""Typer 应用组装 + 全局 callback。

╔══════════════════════════════════════════════════════════════════════════╗
║ 全局 callback 为什么是"产品决策"而不是技术细节                              ║
╚══════════════════════════════════════════════════════════════════════════╝

本项目最容易被弃用的原因不是功能少，而是"投完就忘、懒得敲命令"。
解法的关键一环是：**任何 jh 命令开头都固定显示待处理事项**。

    不需要我主动敲 `jh today`，也能天天看到：
        📌 投后 3 天未跟进 5 条 ｜ 投后 7 天无动静 3 条
        📅 本周面试 2 场（9-10 14:00 字节 · 9-12 10:00 腾讯）
        ⏳ 笔试截止 1 条（9-11 饿了么）

    → 目标："我什么都不想敲的时候，工具依然在产生价值"。

放在 Typer 的 `@app.callback()` 里实现，是**唯一**不用改 19 个命令的地方。
如果等 P4.5 再往每个命令里加打印，就会变成 19 处重复代码。

另外这里统一做了三件"每个命令都需要"的事：
    1. `setup_logging()` —— 保证日志脱敏 Filter 在任何命令前都已挂上；
    2. 配置加载 —— 配置有问题时**立刻**给出可读报错，而不是跑到一半崩；
    3. `--config` —— 指定 config.yaml（实现方式是设 JH_CONFIG 环境变量，
       因为 pydantic-settings 的源是"类级别"的，用环境变量最干净）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from jobhunter import CURRENT_PHASE, __version__
from jobhunter.cli.commands import COMMANDS
from jobhunter.services.reminder_svc import format_reminders, get_reminders
from jobhunter.utils.logging import setup_logging

__all__ = ["app", "main"]

console = Console()

app = typer.Typer(
    name="jh",
    help=(
        "JobHunter · 求职猎手 —— 本地优先的求职辅助工具。\n\n"
        "抓岗位详情 → 两级匹配打分 → 按 JD 定制开场白 → 跟踪投递/笔试/面试/Offer。\n\n"
        "⚠️ 本工具**不做自动投递**：脚本只负责出清单，投递按钮由你点。"
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
    # -h 和 --help 都可用（click 默认只有 --help）
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _version_callback(value: bool) -> None:
    """`jh --version` 的处理。VersionOption 需要在 callback 里手动退出。"""
    if value:
        console.print(f"JobHunter {__version__} (phase {CURRENT_PHASE})")
        raise typer.Exit


@app.callback()
def main_callback(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="显示版本与当前开发阶段后退出",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help="指定 config.yaml 路径（默认：当前目录 → 项目根）",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    no_remind: Annotated[
        bool,
        typer.Option(
            "--no-remind",
            help="本次不显示顶部待处理提醒（也受 config 的 show_reminders 控制）",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="打印 DEBUG 日志（日志始终脱敏，不会输出简历原文）"),
    ] = False,
) -> None:
    """全局前置处理：日志 → 配置 → 顶部提醒。"""
    # ① 日志：必须在最前面，保证后续任何报错都经过脱敏 Filter
    setup_logging("DEBUG" if verbose else None)

    # ② --config：通过环境变量告诉 pydantic-settings 用哪个 yaml
    if config is not None:
        resolved = Path(config).resolve()
        os.environ["JH_CONFIG"] = str(resolved)
        # 清掉单例缓存，否则会继续用上一次加载的配置
        from jobhunter.config.settings import reset_settings

        reset_settings()

    # ③ 顶部提醒：让"待处理事项"在每条命令前都出现一次
    settings = None
    try:
        from jobhunter.config.settings import get_settings

        settings = get_settings()
    except ValueError as exc:
        # 配置错误是所有命令共同的前置条件，所以在这里一次性给出清晰提示
        console.print(f"[red]配置错误[/red]\n{exc}")
        raise typer.Exit(code=1) from exc

    show = settings.show_reminders and not no_remind
    if show:
        for line in format_reminders(get_reminders()):
            console.print(f"[dim]{line}[/dim]")

    # 让子命令（以及测试）能拿到已经解析好的上下文
    ctx.obj = {"settings": settings, "no_remind": no_remind, "verbose": verbose}


# ---- 注册所有命令 ---------------------------------------------------------
# ★ name 必须显式给（见 commands/__init__.py 的 docstring）：
#   不传 name 时 Typer 会把子命令摊平到上一层，且只发一条 Warning 就继续跑，
#   结果是一堆孤立的 add/list/export 顶替掉 today/weak 这些命令。
# ★ 注册顺序由 COMMANDS 决定（按工作流排序，便于人工核对；
#   注意 click 在 --help 里默认按字母序展示，不要被误导）
for command_name, command_app in COMMANDS:
    app.add_typer(command_app, name=command_name)


def main() -> None:
    """`jh` 命令的入口（pyproject 的 [project.scripts] 指向这里）。

    ⚠️ 这里不要写业务逻辑 —— 只负责把控制权交给 Typer。
       业务逻辑写进 services/，CLI 层只解析参数与渲染输出（见 docs/architecture.md §2）。
    """
    app()

"""CLI 公共工具：占位命令的统一提示 + 终端输出辅助。

★ 占位命令的设计原则：**不假装成功**。
   `jh today` 在 P0 阶段如果"静默什么都不做直接返回 0"，
   用户（包括三个月后的我自己）会以为"跑过了、没结果就是没岗位"。
   所以占位命令一律：
        · 明确打印"该功能在 PX 阶段实现"；
        · 以**非 0 退出码**结束（2 = 用法/未实现，区别于业务失败）；
        · 指出当前已实现的是什么（`jh init`），给出下一步动作。
"""

from __future__ import annotations

from typing import NoReturn

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from jobhunter import CURRENT_PHASE

__all__ = [
    "console",
    "err_console",
    "not_implemented",
    "print_kv",
    "print_table",
    "success",
    "warn",
]

console = Console()
# 错误走 stderr：这样 `jh xxx > out.txt` 时错误不会被吞进文件里
err_console = Console(stderr=True)

# 未实现退出码：与 Typer/click 的用法错误（也是 2）一致，
# 语义上确实属于"用法层面：这个功能还没做"。
EXIT_NOT_IMPLEMENTED = 2


def not_implemented(feature: str, phase: str) -> NoReturn:
    """统一处理占位命令：说明阶段 → 非 0 退出。

    Args:
        feature: 功能名，如 "jh today 待投清单"
        phase:   落地阶段，如 "P4"

    为什么用富文本而不是纯 print：
        终端里 [P4] 之类的东西用颜色区分，扫一眼就知道"这不是报错，是还没做"。
    """
    err_console.print()
    err_console.print(
        Panel(
            f"[yellow]该功能尚未实现[/yellow]\n\n"
            f"  功能：[bold]{feature}[/bold]\n"
            f"  计划阶段：[bold cyan]{phase}[/bold cyan]\n"
            f"  当前阶段：[bold]{CURRENT_PHASE}[/bold]（骨架 + 工程底座）\n\n"
            f"现在就可用：[green]jh init[/green]（建库 + 迁移）\n"
            f"查看全部命令：[green]jh --help[/green]\n"
            f"实现计划：[green]docs/roadmap.md[/green]",
            title="🚧 未实现",
            border_style="yellow",
        )
    )
    raise typer.Exit(code=EXIT_NOT_IMPLEMENTED)


def success(message: str) -> None:
    """打印成功信息（统一绿色 ✓ 前缀）。"""
    console.print(f"[green]✓[/green] {message}")


def warn(message: str) -> None:
    """打印警告（黄色，不影响退出码）。"""
    console.print(f"[yellow]![/yellow] {message}")


def print_kv(pairs: dict[str, object], *, title: str | None = None) -> None:
    """打印"键: 值"列表（用于 `jh init` 的小结）。"""
    if title:
        console.print(f"[bold]{title}[/bold]")
    for key, value in pairs.items():
        console.print(f"  [dim]{key}[/dim]：{value}")


def print_table(
    rows: list[dict[str, object]],
    *,
    columns: list[str] | None = None,
    title: str | None = None,
    ctx: typer.Context | None = None,
    json_out: bool = False,
) -> None:
    """打印表格（或 `--json` 时打印 JSON）。

    P0 还没有真正的列表输出，但接口先定下来：
    P4 的 `jh today`、P6 的 `jh stats`、P7 的 `jh leads export` 都用它，
    避免每个命令各写一套 rich 表格代码。

    `--json` 的意义：方便你把清单喂给别的脚本（比如导出到自己的表格里），
    也让将来 P8 的 Web 接口可以直接复用同一份数据结构。
    """
    if json_out:
        console.print_json(data=rows)
        return
    if not rows:
        console.print("[dim]（没有数据）[/dim]")
        return
    header = columns or list(rows[0].keys())
    table = Table(title=title, show_header=True, header_style="bold cyan")
    for name in header:
        table.add_column(name)
    for row in rows:
        table.add_row(*[str(row.get(name, "")) for name in header])
    console.print(table)

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

from jobhunter import CURRENT_PHASE

__all__ = [
    "console",
    "err_console",
    "not_implemented",
    "print_kv",
    "success",
    "warn",
]

# ★ 为什么固定 width=200 而不是让它按终端宽度自适应（踩过的坑）：
#
#   岗位/公司的名字经常很长（"某某网络科技有限公司"），rich 的表格会按**可用宽度**
#   裁列，超出部分直接变成 `…` —— 于是 `jh job list` 里看不到完整的公司名与职位名，
#   而用户最需要看的就是这两个字段。
#
#   事实是：这类"数据表"本来就该完整打印（需要时重定向到文件或加 --json），
#   按终端宽度裁掉信息反而有害。定成 200 列足够覆盖常见终端与表格宽度；
#   真要换行也还有 soft_wrap 兜着。
#
#   注：这也让 CliRunner 下的测试能看到完整文本（否则断言会因截断而失败，
#   测试会变得"看终端宽度脸色"，那是最难查的一类不稳定测试）。
console = Console(width=200, soft_wrap=True)
# 错误走 stderr：这样 `jh xxx > out.txt` 时错误不会被吞进文件里
err_console = Console(stderr=True, soft_wrap=True)

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


# ───────────────────────────────────────────────────────────────────────────
# 关于"表格输出"：这里**故意没有**一个通用的 print_table 包装
#
# P0 时我预判过"P4 的 jh today / P6 的 jh stats 都需要打印表格"，
# 于是写了一个 `print_table(rows: list[dict])` 泛化包装。到 P2 为止它一次都没被用过 ——
# 真正实现表格的地方（`jh job list`、`jh resume list`）都**直接用 rich.Table**，
# 因为它们的列结构差异很大：有的要 `justify="right"`、有的要 `overflow="fold"`、
# 有的列宽要固定。硬套一个泛化包装反而要在调用处写更多补偿代码。
#
# 结论（写给未来的自己）：**别为了"将来会用到"提前造抽象**。
# 等到真有两个以上调用点、且它们的形状确实一致时再抽 —— 那时你才知道该抽什么。
# 现在这个包装已删除，避免它成为"看起来能用、用起来别扭"的诱饵。
# ───────────────────────────────────────────────────────────────────────────

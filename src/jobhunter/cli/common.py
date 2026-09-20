"""CLI 公共工具：占位命令的统一提示 + 终端输出辅助。

★ 占位命令的设计原则：**不假装成功**。
   `jh today` 在 P0 阶段如果"静默什么都不做直接返回 0"，
   用户（包括三个月后的我自己）会以为"跑过了、没结果就是没岗位"。
   所以占位命令一律：
        · 明确打印"该功能在 PX 阶段实现"；
        · 以**非 0 退出码**结束（2 = 用法/未实现，区别于业务失败）；
        · 指出当前已实现的是什么（`jh init` / `jh resume` / `jh job` / `jh match` / `jh gap` /
      `jh today` / `jh apply`），给出下一步动作。
"""

from __future__ import annotations

import textwrap
from typing import NoReturn

import typer
from rich.console import Console
from rich.panel import Panel

from jobhunter import CURRENT_PHASE
from jobhunter.config.settings import get_settings
from jobhunter.storage.db import database_problem, database_ready, get_session_factory
from jobhunter.utils.logging import setup_logging

__all__ = [
    "console",
    "err_console",
    "not_implemented",
    "print_kv",
    "require_db",
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

    ★ 为什么要自己折行（`_wrap_feature`）：
        占位命令会把**它收到的参数原样回显**在 feature 里（`jh today（limit=None,
        min_score=None, …）`），这是有意的 —— 用户能立刻确认"参数被认出来了，
        只是功能还没做"。但这串东西很长，rich 的 Panel 在默认宽度下会把它**裁掉**
        （实测末尾变成 `expor`），看起来像工具坏了。所以在这里按面板宽度手工折行。
    """
    err_console.print()
    err_console.print(
        Panel(
            f"[yellow]该功能尚未实现[/yellow]\n\n"
            f"{_wrap_feature(feature)}"
            f"  计划阶段：[bold cyan]{phase}[/bold cyan]\n"
            f"  当前阶段：[bold]{CURRENT_PHASE}[/bold]\n\n"
            f"现在就可用：[green]jh init[/green]（建库 + 迁移）\n"
            f"　　　　　　　[green]jh resume add | list | split[/green]（简历导入解析）\n"
            f"　　　　　　　[green]jh job add | list | show | skip[/green]（岗位录入）\n"
            f"　　　　　　　[green]jh match run | explain | set[/green]"
            f"　[green]jh gap[/green]（两级匹配 + 技能缺口）\n"
            f"　　　　　　　[green]jh today[/green]（待投清单 + 开场白）\n"
            f"　　　　　　　[green]jh apply[/green]（打标：单条 / 批量 / 模糊匹配 / --pending）\n"
            f"　　　　　　　[green]jh tailor brief | analyze | approve | export[/green]"
            f"（定制简历 + 保真闸门）\n"
            f"　　　　　　　[green]jh interview | question | weak[/green]"
            f"（面试安排 / 问题记录 / 薄弱点）\n"
            f"　　　　　　　[green]jh assessment | offer | followup[/green]"
            f"（笔试 / Offer 比较 / 追问话术）\n"
            f"　　　　　　　[green]jh stats | report | cal export[/green]"
            f"（转化率 / 周报 / 日历导出）\n"
            f"查看全部命令：[green]jh --help[/green]\n"
            f"实现计划：[green]docs/roadmap.md[/green]",
            title="🚧 未实现",
            border_style="yellow",
        )
    )
    raise typer.Exit(code=EXIT_NOT_IMPLEMENTED)


def _wrap_feature(feature: str) -> str:
    """把"功能 + 回显参数"折成多行（首行带 `功能：`，续行缩进对齐）。"""
    # 宽度取 64：面板两边各有边框与内边距，默认 80 列终端下正文大致就这么多。
    # 用 textwrap 而不是手写切片，是为了在空格处断行（参数之间用 ", " 分隔）。
    lines = textwrap.wrap(feature, width=64, break_long_words=True) or [feature]
    body = []
    for index, line in enumerate(lines):
        # 续行用"2 空格 + 4 个全角空格"对齐到 `功能：` 之后
        prefix = "  功能：" if index == 0 else "  　　　　"
        body.append(f"{prefix}[bold]{line}[/bold]\n")
    return "".join(body)


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


#: 数据库还没初始化时的统一提示（比 "no such table: jobs" 可读得多）
INIT_HINT = "[yellow]数据库还没初始化。请先运行：[bold]jh init[/bold][/yellow]"


def require_db() -> tuple[object, object]:
    """确认数据库可用，返回 `(engine, session_factory)`。

    ★ 为什么每个要读库的命令都得先过这一关：
      SQLAlchemy 在表不存在时报的是 `no such table: jobs` ——
      对用户来说完全不知道该怎么办。这里换成一句"请先运行 jh init"。

    ★★ 第二道判据（ISSUES #60）：**库是不是最新的**。
      改前只查"表在不在"，于是停在旧迁移上的库被放行，然后在第一条 SQL 上
      炸出 `no such column: http_status` 的原始堆栈（`jh leads import`）
      或页面里一串 500（`jh leads serve`）—— 正是这句 docstring 想消灭的体验。
      `database_ready()` 现在两条都查，这里再补上"**为什么**不能用"的说明：

        · 还没建库        → "数据库还没初始化。请先运行 jh init"（原提示）
        · 结构是旧的      → "数据库结构是旧的（当前 0001，程序需要 0005）→ 跑 jh init"
        · 结构比程序新    → "你装的是旧版本程序 → 先升级程序，**不要**跑 jh init"
          （第三种最容易做错：跑 jh init 会把库降到旧结构，反而丢东西）

    ★ 为什么它从各命令模块搬到了这里（P3 做的重构）：
      在 P2 时 `job_cmd` 与 `resume_cmd` 各写了一份**几乎一样**的 `_require_db`。
      两份代码的复制本身还能忍，真正的问题是**提示语会漂移**：
      以后想加一句"或 jh init --force"，很可能只改了其中一处。
      P3 又多了 match / gap 两个使用者 —— 到了"四处重复"这一步，
      抽出来的收益才真正大于成本（**别为两个调用点提前造抽象，
      到了三四个的时候就该动手了**）。
    """
    setup_logging()
    settings = get_settings()
    if not database_ready(settings.database_url):
        problem = database_problem(settings.database_url)
        err_console.print(problem or INIT_HINT)
        raise typer.Exit(code=1)
    return get_session_factory(settings.database_url)


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

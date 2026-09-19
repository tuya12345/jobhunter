"""`jh cache` —— LLM 缓存的看板与清理入口（ISSUES #25 / #26）。

    jh cache stats [--task match]     按任务看条目数 / tokens / 成本 / 命中率
    jh cache clear [--task match]     清缓存（默认清全部；默认要确认一次）

╔══════════════════════════════════════════════════════════════════════════╗
║ 为什么要有这个命令，以及为什么**不做 TTL**                                  ║
╚══════════════════════════════════════════════════════════════════════════╝

`llm_cache` 原来是"只进不出"的：没有 TTL、也没有清理入口。
用户裁决选的是 **A：给看板 + 给清理入口，不做 TTL**。理由（照抄裁决要点）：

    · 缓存的 key 里已经含 `prompt_version` / `model` / `jd_hash` /
      `resume.content_hash` / 提示词指纹 —— 该失效的时候它**自己就失效了**。
      再叠一层时间维度的 TTL 只会让人搞不清"为什么昨天还在的缓存今天没了"，
      而省下的磁盘（几百 KB）与调用（几毛钱）都不值得换这份困惑。
    · 真正缺的不是"自动过期"，而是**看得见**与**清得掉**。

★ 命中率为什么是 `Σhits / (Σhits + 行数)`：口径写在
  `services/cache_stats_svc.py` 的模块说明里（**只有那一份**），
  这里只负责把它渲染出来 —— 包括把那句定义原样打给用户看，不让他猜。

★ `clear` 的行为必须与 `models/llm_cache.py` 的承诺逐字一致：

    那句承诺是"缓存是纯派生数据，可以随时整体清掉而不影响业务数据"。
    所以 `clear` **只删 `llm_cache` 一张表** —— 不碰 jobs / applications /
    resume_versions，也不做任何"顺手"的重置。
    删除前的计数与"只删了这一张表"都由 `tests/unit/test_cache_cmd.py` 守着。

★ 为什么默认要交互确认（而不是像 `rm -rf` 那样直接删）：

    删缓存确实不影响业务数据，但用户**会紧张** —— 屏幕上出现"删除 812 条"
    而没有任何确认，第一反应是"它是不是把我的数据删了"。
    所以：默认问一句；`--yes` 跳过；**非交互（管道/CI）时拒绝删**并提示加 `--yes`，
    绝不"没人回答就当作同意"。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from jobhunter.cli.common import console, err_console, require_db, success, warn
from jobhunter.services import cache_stats_svc
from jobhunter.services.stats_svc import format_rate
from jobhunter.utils.time import to_local

app = typer.Typer(
    help="LLM 缓存：看命中率/成本（stats）、清缓存（clear）",
    # ★ 这是一个**命令组**（stats / clear 两个动作），所以不要 invoke_without_command：
    #   什么都不给时让 Typer 打印帮助（no_args_is_help），比"默默什么都不做"清楚。
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------
@app.command("stats")
def cache_stats(
    task: Annotated[
        str | None,
        typer.Option("--task", help="只看某一类任务（match / greeting / tailor / followup …）"),
    ] = None,
) -> None:
    """按任务显示缓存条目数、tokens、成本与命中率（ISSUES #25）。"""
    engine, factory = require_db()
    try:
        with factory() as session:
            report = cache_stats_svc.build_cache_stats(session)
    finally:
        engine.dispose()

    console.print()
    console.print("[bold]LLM 缓存[/bold]　[dim]（缓存是纯派生数据，删掉不影响业务数据）[/dim]")
    console.print()

    if report.is_empty:
        warn("缓存是空的 —— 还没有任何一次 LLM 调用被缓存下来。")
        console.print(
            "  [dim]跑一次 [green]jh match run[/green] 或 [green]jh today[/green] "
            "之后，这里就会有数字。[/dim]"
        )
        return

    rows = report.tasks
    if task:
        rows = [item for item in rows if item.task == task]
        if not rows:
            # ★ 不认识的任务名要**报错**而不是打一张空表：空表看起来像
            #   "这一类还没缓存过"，而真相可能是名字打错了（`macth`）。
            names = "、".join(item.task or "" for item in report.tasks)
            err_console.print(f"[red]没有 task={task} 的缓存[/red]")
            err_console.print(f"  已有的 task：{names}")
            raise typer.Exit(code=1)

    table = Table(title=f"按任务统计（{len(rows)} 类）", header_style="bold cyan")
    table.add_column("task", overflow="fold")
    table.add_column("条目", justify="right")
    table.add_column("tokens in", justify="right")
    table.add_column("tokens out", justify="right")
    table.add_column("历史成本", justify="right")
    table.add_column("被复用", justify="right")
    table.add_column("命中率", justify="right")
    table.add_column("省下", justify="right")
    table.add_column("最近命中", overflow="fold")

    for item in rows:
        table.add_row(
            escape(item.task or ""),
            str(item.entries),
            str(item.tokens_in),
            str(item.tokens_out),
            cache_stats_svc.format_cache_cost(item.cost, unknown=item.missing_cost_rows),
            str(item.hits),
            _rate_text(item.hit_rate, item.attempts),
            cache_stats_svc.format_cache_cost(item.saved),
            _last_hit_text(item.last_hit_at),
        )
    console.print(table)

    overall = cache_stats_svc.total_of(rows)
    cost_text = cache_stats_svc.format_cache_cost(overall.cost, unknown=overall.missing_cost_rows)
    console.print()
    console.print(
        f"  合计：缓存 [bold]{overall.entries}[/bold] 条（= 真实付费调用次数）"
        f"　被复用 [bold]{overall.hits}[/bold] 次"
        f"　命中率 [bold]{_rate_text(overall.hit_rate, overall.attempts)}[/bold]"
    )
    console.print(
        f"  历史成本（这些行当初花的）：{cost_text}"
        f"　｜　缓存省下：{cache_stats_svc.format_cache_cost(overall.saved)}"
    )
    if task:
        # ★ 过滤时合计必须**跟着过滤**（否则表里只有 match、合计却是全部的数字 ——
        #   实测踩到过，这种"表与合计不是一回事"的输出比没有合计更误导）。
        console.print(f"  [dim]以上合计只含 task={task}；看全部请去掉 --task。[/dim]")
    else:
        console.print(
            "  [dim]「历史成本」与「省下」不是同一批钱：前者是**写缓存时真花掉的**，"
            "后者是这些缓存**替你省下来的**。想看单类任务的明细加 --task。[/dim]"
        )
    console.print()
    for note in report.notes:
        console.print(f"  [dim]· {note}[/dim]")
    console.print(
        "  [dim]· 清缓存：[green]jh cache clear[/green]"
        "（只删 llm_cache 表，业务数据一条不动）[/dim]"
    )


def _rate_text(value: float | None, total: int) -> str:
    """`12.5%（3/24）` —— 直接复用 `stats_svc.format_rate`，别再写第二份。

    ★ 为什么不自己实现：命中率与转化率都属"不给分母就会误导"的指标
      （`100%` 到底是 1/1 还是 20/20？），两处实现迟早会在四舍五入与
      空样本上漂移 —— 而这个项目已经因为"同一口径抄两份"吃过一次亏
      （`cli/common.py::require_db` 就是从两份重复里抽出来的）。
    """
    return format_rate(value, total)


def _last_hit_text(moment: datetime | None) -> str:
    """最近一次命中 → 北京时间 `9-19 14:03`；从没命中过时写「从未」。"""
    if moment is None:
        # ★ 空值写"从未"而不是"—"：统计表里"—"到处都是，
        #   而"存了却一次都没被复用"恰恰是用户要看的那类信号。
        return "从未"
    local = to_local(moment)
    return f"{local.month}-{local.day} {local:%H:%M}"


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------
@app.command("clear")
def cache_clear(
    task: Annotated[str | None, typer.Option("--task", help="只清某一类任务（默认清全部）")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="不再确认，直接删")] = False,
) -> None:
    """清空缓存（默认全部；`--task` 只清一类）。**只删 `llm_cache` 表。**

    ★ 删除前一定打印"将要删多少条"：用户对这个动作本来就紧张，
      让他先看到数字（以及"只删缓存表"这句话）再决定，比事后解释便宜得多。
    """
    engine, factory = require_db()
    try:
        with factory() as session:
            pending = cache_stats_svc.count_cached(session, task=task)
            label = f"task={task}" if task else "全部"
            if pending == 0:
                # ★ 空缓存不是错误：说清"什么都没做"就够了（退出码 0），
                #   否则脚本里会以为失败了。
                warn(f"没有要删的缓存（{label}）—— 什么都没做。")
                return
            if not (yes or _confirm(f"将删除 {pending} 条缓存（{label}），继续吗？")):
                console.print("  已取消，什么都没删。")
                return
            deleted = cache_stats_svc.clear_cache(session, task=task)
            session.commit()
    finally:
        engine.dispose()

    success(f"已清掉 {deleted} 条缓存（只删 llm_cache 表，业务数据一条没动）")
    console.print(
        "  [dim]下次跑 match / greeting / tailor 会重新调模型（那些钱是真的会花掉的）。[/dim]"
    )


def _confirm(question: str) -> bool:
    """问一句"确定吗"；**非交互环境下返回 False（不删）**。

    ★ 为什么不直接用 `typer.confirm(...)` 的默认行为：它在"没有输入"时会抛
      `Abort`，在管道里表现为一个莫名其妙的退出码；而 `default=True` 更糟 ——
      等于"没人回答就当作同意"，一个没人确认过的删除动作会**静默**发生。
      这里的口径：非交互 → 拒绝，并明确告诉用户加 `--yes` 就能删。

    ★ 为什么把 TTY 判断拆成 `_stdin_is_interactive()`：
      测试要能分别覆盖"交互确认"与"非交互拒绝"两条路径，而 CliRunner 会把
      `sys.stdin` 换成一个 BytesIO（`isatty()` 恒为 False）—— 拆出来之后，
      测试替换这一个函数就能模拟两种终端，不必去伪造一个真终端。
    """
    if not _stdin_is_interactive():
        warn("当前不是交互终端，不会替你确认 —— 要真删请显式加 --yes")
        return False
    return _ask(question)


def _stdin_is_interactive() -> bool:
    """当前 stdin 是不是一个终端（独立成函数，便于测试替换）。"""
    import sys

    return bool(sys.stdin.isatty())


def _ask(question: str) -> bool:
    """把确认问题抛给用户；**默认答"否"**（回车 / 空输入都算否）。

    ★ 为什么不用 `typer.confirm()`：
      · 它在读不到输入时抛的是 `click.exceptions.Abort`，而 `click` 是 typer 的
        **内部依赖**、不是本项目的直接依赖 —— 本项目这版 typer 把 click 收在
        `typer._click` 里，模块顶层 `from click.exceptions import Abort` 会直接
        `ModuleNotFoundError`（实测踩到）；
      · 它默认把中止渲染成一句 `Aborted.`，用户看不懂发生了什么、
        也不知道缓存到底删没删。
      自己读一行只依赖内建 `input()`：`EOFError` 与 `KeyboardInterrupt` 都当场
      翻成"什么都没删 + 怎么办"，不需要去猜依赖里异常类叫什么。
    """
    # `[y/N]` 用普通文字写出：rich 只把已知标签当标记，未知的 `[y/N]` 原样显示
    console.print(f"{question} [y/N] ", end="")
    try:
        answer = input()
    except (EOFError, KeyboardInterrupt):
        console.print()
        console.print("  没有读到确认输入，已中止 —— **什么都没删**。")
        console.print("  [dim]想跳过确认：加 [green]--yes[/green][/dim]")
        raise typer.Exit(code=1) from None
    return answer.strip().lower() in {"y", "yes"}

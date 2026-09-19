"""`jh weak` —— 薄弱点：按知识点聚合、按"没答上来次数"排序（P6）。

        知识点          没答上来  总题数  结论
        前端性能        3        4      🔴 最先补：反复答不上来
        数据库索引      2        3      🟡 值得补：已经栽过跟头
        Agent 编排      1        5      🟢 记一笔：答崩过一次，过一遍即可

★ 排序口径是"**没答上来次数**"（is_weak 计数）而不是"平均分"：
    平均分会被"只被问过一次且答得一般"的题拉平，
    而"同一个知识点连续三次答不上来"才是真正需要立刻补的信号。

★ 真实被问的题与 JD 预测题**分开展示**（同表不同列）：
    预测题答不上来 = 准备不足，真实题答不上来 = 能力缺口，含义不同。
    默认两者都统计；`--recorded-only` 只看真实被问的。
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table

from jobhunter.cli.common import console, require_db, warn
from jobhunter.services import weak_svc

app = typer.Typer(
    help="薄弱点：按知识点聚合、按没答上来次数排序（P6）",
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def weak(
    top: Annotated[int, typer.Option("--top", "-n", help="显示前 N 个知识点")] = 10,
    recorded_only: Annotated[
        bool,
        typer.Option("--recorded-only", help="只看真实被问到的题（排除 JD 预测题）"),
    ] = False,
) -> None:
    """聚合薄弱知识点（P6）。"""
    engine, factory = require_db()
    try:
        with factory() as session:
            report = weak_svc.build_weak_report(session, top=top, recorded_only=recorded_only)
    finally:
        engine.dispose()

    console.print()
    if not report.rows:
        console.print("[yellow]还没有面试题记录。[/yellow]两张入口：")
        console.print(
            '  · 面完记题：[green]jh question add --interview <id> --q "..." '
            '--category "前端性能" --weak[/green]'
        )
        console.print("  · 提前猜题：[green]jh question predict <job_id>[/green]")
        return

    table = Table(title="薄弱点（按没答上来次数排序）", header_style="bold cyan")
    table.add_column("知识点", overflow="fold")
    table.add_column("没答上来", justify="right")
    table.add_column("总题数", justify="right")
    table.add_column("实录 弱/总", justify="right")
    table.add_column("预测 弱/总", justify="right")
    table.add_column("结论", overflow="fold")
    for row in report.rows:
        table.add_row(
            row.category,
            str(row.weak_count),
            str(row.total),
            f"{row.recorded_weak}/{row.recorded_total}",
            f"{row.predicted_weak}/{row.predicted_total}",
            row.conclusion,
        )
    console.print(table)

    console.print()
    console.print(f"[bold]{report.headline}[/bold]")
    console.print(
        f"  [dim]共 {report.total_questions} 条题"
        f"（实录 {report.recorded_count} / 预测 {report.predicted_count}）"
        + ("　只看实录：jh weak --recorded-only" if not recorded_only else "")
        + "[/dim]"
    )
    if report.uncategorized:
        warn(
            f"有 {report.uncategorized} 条题没写分类 —— "
            "分类是这里唯一的聚合维度，没分类的题在这个功能里等于不存在"
        )
    if not recorded_only and report.predicted_count:
        console.print(
            "  [dim]★ 「预测」那列答不上来只说明**没准备过**，不代表能力缺口；"
            "真正该补的看「实录」那列。[/dim]"
        )

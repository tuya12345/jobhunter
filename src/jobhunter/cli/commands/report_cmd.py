"""`jh report` —— 求职周报（Markdown，只写新文件）（P6）。

    jh report                       # 本周（周一到今天）
    jh report --since 2026-09-01    # 自定义起点
    jh report --out output/         # 换输出目录
    jh report --show                # 顺便把正文打印出来

★ 为什么值得做：这份周报是"给自己看的复盘"，也是**面试时可以讲的东西**
  （"我用数据复盘过自己的投递转化率"比"我很努力在投简历"有说服力得多）。

★ 只写新文件（写到 output/），绝不覆盖已有报告。
  同一天生成多次会得到 `report-20260919-2.md`，而不是把上一份盖掉。

★ 内容全部来自 `stats_svc` / `weak_svc` / 事件流 —— 与 `jh stats`、`jh weak`
  用的是**同一批数字**：周报里的比率和命令里看到的不一致，比没有周报更糟。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from jobhunter.cli.common import console, err_console, require_db, success, warn
from jobhunter.services import report_svc, stats_svc
from jobhunter.utils.time import parse_local_input

app = typer.Typer(
    help="周报：投递/回复/面试/薄弱点汇总成 Markdown（P6）",
    # ★ 单命令应用用 callback（原因见 gap_cmd.py 的说明）
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def report(
    since: Annotated[
        str | None, typer.Option("--since", help="自定义起始日期，如 2026-09-01")
    ] = None,
    out: Annotated[str | None, typer.Option("--out", help="输出目录（默认 output/）")] = None,
    show: Annotated[bool, typer.Option("--show", help="顺便把周报正文打印出来")] = False,
) -> None:
    """生成求职周报（P6）。"""
    start = None
    if since:
        try:
            start = parse_local_input(since)
        except ValueError as exc:
            err_console.print(f"[red]--since 解析失败：{exc}[/red]")
            raise typer.Exit(code=2) from exc

    engine, factory = require_db()
    try:
        with factory() as session:
            data = report_svc.build_report(session, since=start)
            path = report_svc.export_report(data, out_dir=Path(out) if out else None)
    finally:
        engine.dispose()

    success(f"已生成周报：{path}")
    console.print(
        f"  　周期 {data.period_text}　｜　投递 {data.stats.overall.total} 条"
        f"　｜　事件 {len(data.events)} 条　｜　问题 {len(data.questions)} 条"
    )
    if data.quiet_week:
        warn("这一周没有任何事件/问题记录 —— 周报会照实写「空白」，不拿累计数据充数")
    total = data.stats.overall.total
    if total and total < stats_svc.MIN_SAMPLE_FOR_COMPARISON:
        console.print(
            f"  [dim]样本量 {total} 条 < {stats_svc.MIN_SAMPLE_FOR_COMPARISON} —— "
            "里面的比率只能当参考，别下结论[/dim]"
        )
    console.print("　[dim]只写新文件；内容是 Markdown，可直接贴进笔记。[/dim]")
    if show:
        console.print()
        console.print(report_svc.render_markdown(data))

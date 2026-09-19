"""`jh stats` —— 转化率统计（P6）。

        ① 平台转化率
           平台         投递  已读  回复  约面  Offer   回复率   约面率
           招聘网站     24    18    6     3     1       25.0%（6/24）  12.5%（3/24）
           官网         8     3     1     0     0       12.5%（1/8）   0%（0/8）
        ② 开场白变体回复率（with_project vs without_project）
        ③ 岗位方向回音对比（AI Agent vs 前端 哪个方向回得多）

★★ 一条纪律：**只记录对比，不宣称统计显著性**（评审明确采纳）。
    原因：样本量太小（一个秋招可能就 100 次投递，分到 2 个变体各 50 次，
    回复事件可能只有个位数）。任何"变体 A 显著优于 B"的结论都是噪声。
    所以这里只打印数字和样本量，不生成"建议使用变体 A"这类伪结论。

★ 转化率口径必须写清楚（否则数字无法复现）：
    回复率 = "至少走到过 replied" 的人数 / 投递总数
    约面率 = "至少走到过 assessment" 的人数 / 投递总数
   （即"至少走到过这一步"，而不是"当前停在这一步" —— 后者会把
     现在已被拒、但曾经走到过二面的投递一起抹掉。）

★ 每个比率都带分子/分母：只有百分比时，`100%` 到底是"1/1"还是"20/20"
  完全看不出来，而这决定了它是不是噪声。
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table

from jobhunter.cli.common import console, err_console, require_db, success, warn
from jobhunter.services import export_svc, stats_svc

app = typer.Typer(
    help="统计：平台转化率 / 开场白变体 / 岗位方向回音（P6）",
    # ★ 单命令应用用 callback（原因见 gap_cmd.py 的说明）
    invoke_without_command=True,
    no_args_is_help=False,
)

#: 表头（三张表共用同一批指标，改口径只改这里）
_RATE_HEADERS = ("投递", "已读", "回复", "约面", "Offer", "被拒", "回复率", "约面率")


def _add_columns(table: Table, first: str) -> None:
    table.add_column(first, overflow="fold")
    for header in _RATE_HEADERS:
        table.add_column(header, justify="right" if header != "回复率" else "right")


def _counts_row(label: str, counts: stats_svc.StageCounts) -> list[str]:
    return [
        label,
        str(counts.total),
        str(counts.viewed),
        str(counts.replied),
        str(counts.interview),
        str(counts.offer),
        str(counts.rejected),
        stats_svc.format_rate(counts.rate("replied"), counts.total),
        stats_svc.format_rate(counts.rate("interview"), counts.total),
    ]


@app.callback(invoke_without_command=True)
def stats(
    days: Annotated[int | None, typer.Option("--days", help="只统计最近 N 天（默认全部）")] = None,
    export: Annotated[
        str | None, typer.Option("--export", help="导出格式：md / csv（写到 output/）")
    ] = None,
) -> None:
    """输出转化率统计（P6）。"""
    if export is not None and export not in ("md", "csv"):
        err_console.print("[red]--export 只支持 md 或 csv[/red]")
        raise typer.Exit(code=2)

    engine, factory = require_db()
    try:
        with factory() as session:
            report = stats_svc.build_stats(session, days=days)
    finally:
        engine.dispose()

    console.print()
    # ★ 样本量提示用 dim 而不是塞进括号：`sample_note` 自己带括号，
    #   再套一层会变成"（样本量很小（1 条）——…）"这种读起来很别扭的嵌套。
    console.print(f"[bold]转化率统计[/bold]　[dim]{report.sample_note}[/dim]")
    console.print()

    overall = Table(title="① 总体与平台", header_style="bold cyan")
    _add_columns(overall, "平台")
    overall.add_row(*_counts_row("全部", report.overall))
    for item in report.platforms:
        overall.add_row(*_counts_row(item.platform, item.counts))
    console.print(overall)

    console.print()
    variants = Table(title="② 开场白变体（只记录，不下结论）", header_style="bold cyan")
    _add_columns(variants, "变体")
    for item in report.variants:
        variants.add_row(*_counts_row(item.variant, item.counts))
    console.print(variants)

    console.print()
    directions = Table(title="③ 岗位方向回音", header_style="bold cyan")
    _add_columns(directions, "方向")
    for item in report.directions:
        directions.add_row(*_counts_row(item.direction, item.counts))
    console.print(directions)

    console.print()
    for note in report.notes:
        console.print(f"  [dim]· {note}[/dim]")

    small = [item.variant for item in report.variants if item.small_sample]
    if len(report.variants) > 1 and small:
        warn(
            f"变体 {('、'.join(small))} 的样本 < {stats_svc.MIN_SAMPLE_FOR_COMPARISON} 条 —— "
            "两组的差距现在只能算噪声，**不要**据此换变体"
        )

    if export:
        path = _export(report, export)
        success(f"已导出：{path}")
        console.print("　[dim]导出只写新文件；口径与上表完全一致。[/dim]")


def _export(report: stats_svc.StatsReport, fmt: str):
    """导出统计（md / csv）。★ 只写新文件（与其它导出同一条纪律）。"""
    settings = _settings()
    if fmt == "md":
        lines = [f"# 投递转化率（{report.sample_note}）", ""]
        for title, rows in (
            (
                "总体与平台",
                [("全部", report.overall)] + [(i.platform, i.counts) for i in report.platforms],
            ),
            ("开场白变体", [(i.variant, i.counts) for i in report.variants]),
            ("岗位方向", [(i.direction, i.counts) for i in report.directions]),
        ):
            lines += [f"## {title}", "", "| " + " | ".join(("名称", *_RATE_HEADERS)) + " |"]
            lines.append("|" + "---|" * (len(_RATE_HEADERS) + 1))
            for label, counts in rows:
                lines.append("| " + " | ".join(_counts_row(label, counts)) + " |")
            lines.append("")
        lines += ["## 口径", ""]
        lines += [f"- {note}" for note in report.notes]
        return export_svc.unique_path(settings.paths.output_dir, "stats", ".md").write_text(
            "\n".join(lines), encoding="utf-8"
        )

    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL)
    writer.writerow(["分组", "名称", *_RATE_HEADERS])
    for name, counts in [("全部", report.overall)] + [("平台", i.counts) for i in report.platforms]:
        writer.writerow([name, *_counts_row(name, counts)[1:]])
    for item in report.variants:
        writer.writerow(["开场白变体", *_counts_row(item.variant, item.counts)[1:]])
    for item in report.directions:
        writer.writerow(["岗位方向", *_counts_row(item.direction, item.counts)[1:]])
    path = export_svc.unique_path(settings.paths.output_dir, "stats", ".csv")
    # BOM：Excel 打开 UTF-8 CSV 不乱码（与 today 的 CSV 导出同一处理）
    return path.write_text("\ufeff" + buffer.getvalue(), encoding="utf-8", newline="")


def _settings():
    from jobhunter.config.settings import get_settings

    return get_settings()

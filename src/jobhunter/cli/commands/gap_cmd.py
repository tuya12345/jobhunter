"""`jh gap` —— 反向技能缺口分析（P3 实现）。

★ 这是"简历不匹配"痛点的**真正答案**（产品视角评审的补充，0 LLM 成本）。

它和"逐岗告诉你缺什么"的区别：
    逐岗输出 → 你看到的是 100 条零散的"缺 LangChain、缺 Docker、缺 RAG"，
               根本不知道先补哪个；
    jh gap   → 跨所有岗位聚合，直接告诉你：

        | 技能/要求  | 出现在 78% 的 JD 里 | 我的简历 | 缺口优先级 |
        | RAG       | 78%                | ✅ 有    | —         |
        | LangChain | 65%                | ❌ 无    | 🔴 高      |
        | Docker    | 52%                | ❌ 无    | 🟡 中      |

    → 答案从"这个岗位我缺 X"变成"**我该补什么**"。

实现要点：
    · 数据源优先用 `jobs.jd_keywords_json`（共享中间层产物，不重复解析 JD）；
      没跑过 match 的岗位会**临时**现抽关键词（只读，不落库）——
      跳过它们会得到一份"看起来正常但少统计了一半岗位"的报告，那是最坏的结果。
    · "我有没有"经过技能归一化 + 英文词边界（Java ≠ JavaScript），
      复用 P1 的词表，绝不另写一套。
    · 纯本地聚合，不调 LLM。

★ Typer 结构说明（踩过的坑，写下来免得再犯）：
  gap 只有一个动作、没有子命令。如果写成 `@app.command()` 的单命令应用，
  再 `app.add_typer(...)` 注册，Typer 会报
  "The 'callback' parameter is not supported by Typer when using `add_typer` without a name"。
  正确做法就是下面这样：**用 callback + `invoke_without_command=True`**，
  由主 app 通过 `add_typer(app, name="gap")` 注册。`jh apply` 同理。
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.panel import Panel
from rich.table import Table

from jobhunter.cli.common import console, require_db, success, warn
from jobhunter.config.settings import get_settings
from jobhunter.services import match_svc

app = typer.Typer(
    help="反向技能缺口分析：跨岗位聚合，告诉你该补什么（P3）",
    invoke_without_command=True,
    no_args_is_help=False,
)

#: 样本量小于这个数时提示"结论不稳"。
#: ★ 为什么是 5：3 个岗位里"出现 2 次"就是 67%，看上去很高其实毫无统计意义。
#:   这个提示不是为了严谨，而是防止用户照着一份噪音报告去改简历。
_MIN_SAMPLE = 5

_PRIORITY_LABEL = {
    "high": "[bold red]🔴 高[/bold red]",
    "medium": "[yellow]🟡 中[/yellow]",
    "low": "[dim]⚪ 低[/dim]",
    "": "[dim]—[/dim]",
}


@app.callback(invoke_without_command=True)
def gap(
    top: Annotated[int, typer.Option("--top", "-n", help="只显示缺口优先级最高的 N 项")] = 20,
    min_ratio: Annotated[
        float,
        typer.Option("--min-ratio", help="只统计出现在至少这个比例（0-1）的 JD 里的技能"),
    ] = 0.0,
    resume: Annotated[
        int | None, typer.Option("--resume", "-r", help="参照哪份简历（默认用默认简历）")
    ] = None,
    only_scored: Annotated[
        bool,
        typer.Option("--scored", help="只统计跑过 match 的岗位（排除还没解析 JD 的）"),
    ] = False,
) -> None:
    """跨所有岗位聚合技能缺口，输出"该补什么"的优先级表。"""
    engine, factory = require_db()
    settings = get_settings()
    try:
        with factory() as session:
            report = match_svc.analyze_gaps(
                session,
                resume_id=resume,
                settings=settings,
                only_scored=only_scored,
            )
    finally:
        engine.dispose()

    if report.total_jobs == 0:
        console.print("[yellow]库里还没有岗位。[/yellow]先录几个：")
        console.print("  [green]jh job add <岗位链接>[/green]")
        raise typer.Exit(code=1)

    if report.analyzed_jobs == 0:
        console.print(
            Panel(
                f"库里有 {report.total_jobs} 个岗位，但**一个技能关键词都没抽到**。\n\n"
                "多半是 JD 正文太短或没抓到（`jh job show <id> --jd` 看看）。\n"
                "[dim]技能词表在 src/jobhunter/config/config.yaml 的 skill_aliases 里，"
                "缺词可以自己加。[/dim]",
                title="没有可统计的数据",
                border_style="yellow",
            )
        )
        raise typer.Exit(code=1)

    rows = [row for row in report.rows if row.ratio >= min_ratio]
    # 缺口优先：没有的技能排前面（`analyze_gaps` 已经排好序，这里保持稳定）
    missing = [row for row in rows if not row.have][: max(0, top)]
    have_rows = [row for row in rows if row.have][: max(0, top)]
    _print_report(report, missing, have_rows, top)

    _print_gap_hints(report, missing)
    success(f"统计完成：{report.analyzed_jobs} 个岗位的 JD，共 {len(report.rows)} 个技能关键词")


def _print_report(
    report: match_svc.GapReport,
    missing: list[match_svc.GapRow],
    have_rows: list[match_svc.GapRow],
    top: int,
) -> None:
    """打印两张表：缺口优先级表 + 我已经有的技能表。"""
    console.print()
    console.print(
        f"[bold]技能缺口[/bold]（{report.analyzed_jobs} 个岗位的 JD 参与统计"
        f"，参照简历：{report.resume_name or '（没有简历）'}）"
    )
    total = report.analyzed_jobs
    if not missing:
        console.print("  [green]没有缺口 —— 所有被统计到的技能你的简历里都有。[/green]")
    else:
        _print_rows(
            missing,
            total=total,
            title=f"该补什么（前 {min(top, len(missing))} 项）",
        )

    if have_rows:
        console.print()
        _print_rows(have_rows, total=total, title="你已经有的（按出现频率排）", show_priority=False)


def _print_rows(
    rows: list[match_svc.GapRow],
    *,
    total: int,
    title: str,
    show_priority: bool = True,
) -> None:
    """渲染一张缺口/已有技能表。

    `出现在` 一列写成 `3/12` 而不是每行重复百分比：分母是同一批岗位，
    写在每行只会让表格变宽、更难横向比较。
    """
    table = Table(title=title, header_style="bold cyan")
    table.add_column("技能/要求", overflow="fold")
    table.add_column(f"出现在/{total}", justify="right")
    table.add_column("比例", justify="right")
    table.add_column("我的简历", justify="center")
    if show_priority:
        table.add_column("缺口优先级", justify="center")
    for row in rows:
        cells = [
            row.keyword,
            str(row.job_count),
            f"{row.ratio:.0%}",
            "[green]✅ 有[/green]" if row.have else "[red]❌ 无[/red]",
        ]
        if show_priority:
            cells.append(_PRIORITY_LABEL.get(row.priority, "—"))
        table.add_row(*cells)
    console.print(table)


def _print_gap_hints(report: match_svc.GapReport, missing: list[match_svc.GapRow]) -> None:
    """给结论加几句"怎么用"的提示（否则表格只是数据，不是行动）。"""
    console.print()
    if report.analyzed_jobs < _MIN_SAMPLE:
        warn(
            f"只统计了 {report.analyzed_jobs} 个岗位，比例本身不稳定 —— "
            "建议先多录几个岗位（`jh job add <链接>`）再看结论。"
        )
    if report.empty_jobs:
        console.print(
            f"  [dim]另有 {report.empty_jobs} 个岗位的 JD 里没抽到任何技能词"
            "（可能是 JD 太短或没抓到正文），它们没参与统计。[/dim]"
        )
    if not report.resume_name:
        console.print("  [yellow]库里没有简历，所以「我的简历」一列全是「无」。[/yellow]")
        console.print("  [dim]先 `jh resume add <简历.docx>`[/dim]")
        return

    # ★ "只在项目里提过、技能栏没主动列" 是**零成本提分**：不用现学，只要写出来。
    #   注意它属于 have=True 的那一批（我确实会），所以要从 report.rows 里挑，
    #   而不是从 missing（缺口）里挑 —— 这里曾经写错过一次。
    mentioned = [row.keyword for row in report.rows if row.have and row.only_mentioned]
    if mentioned:
        console.print(
            "  [cyan]★ 这几项你在项目描述里用到过，但技能栏没主动写：[/cyan]"
            + "、".join(mentioned[:8])
        )
        console.print("  [dim]补进技能栏是零成本提分（不用现学）。[/dim]")

    high = [row.keyword for row in missing if row.priority == "high"]
    if high:
        console.print("  [bold]建议先补这几个（一半以上的岗位都要）：[/bold]" + "、".join(high[:6]))
        console.print(
            '  [dim]补的方式建议：挑其中一个做进项目里，比在简历上写"了解 XX"有说服力得多。[/dim]'
        )

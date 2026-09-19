"""`jh interview` —— 面试安排（P6）。

    jh interview add --job 3 --round 一面 --at "2026-09-11 14:00" --mode 视频 --link "..."
    jh interview list [--upcoming]

★ 时间输入的约定：`--at` 用**北京时间**书写（"2026-09-11 14:00"），
  内部立刻转成 UTC 存库（见 utils/time.py 的 parse_local_input）。
  展示时再转回北京时间 —— 用户永远看不到 UTC。
  "本周面试 N 场（9-10 14:00 字节 · 9-12 10:00 腾讯）"这条顶部提醒就靠它。

★ `--upcoming` 只认「待面 + 时间未过」：它回答"我接下来要面什么"，
  与默认列表（含已面/出结果，"我面过什么"）是两个用途。
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table

from jobhunter.cli.common import console, err_console, require_db, success, warn
from jobhunter.models.constants import INTERVIEW_STATUSES
from jobhunter.services import interview_svc
from jobhunter.utils.time import fmt_datetime_local, parse_local_input

app = typer.Typer(help="面试：安排 / 列表（P6）")


@app.command("add")
def interview_add(
    job: Annotated[int, typer.Option("--job", "-j", help="关联的岗位 ID")],
    round_: Annotated[str | None, typer.Option("--round", help="一面 / 二面 / HR面")] = None,
    at: Annotated[
        str | None, typer.Option("--at", help='面试时间（北京时间），如 "2026-09-11 14:00"')
    ] = None,
    mode: Annotated[str | None, typer.Option("--mode", help="视频 / 电话 / 现场")] = None,
    link: Annotated[str | None, typer.Option("--link", help="会议链接")] = None,
    location: Annotated[str | None, typer.Option("--location", help="现场地址")] = None,
    status: Annotated[str, typer.Option("--status", help="待面 / 已面 / 出结果")] = "待面",
    note: Annotated[str | None, typer.Option("--note", help="备注")] = None,
) -> None:
    """新增一场面试（P6）。"""
    if status not in INTERVIEW_STATUSES:
        err_console.print(
            f"[red]状态 {status!r} 不认识[/red]。可用：{'、'.join(INTERVIEW_STATUSES)}"
        )
        raise typer.Exit(code=2)

    scheduled_at = None
    if at:
        try:
            scheduled_at = parse_local_input(at)
        except ValueError as exc:
            err_console.print(f"[red]--at 解析失败：{exc}[/red]")
            raise typer.Exit(code=2) from exc

    engine, factory = require_db()
    try:
        with factory() as session:
            try:
                interview = interview_svc.add_interview(
                    session,
                    job,
                    round_=round_,
                    scheduled_at=scheduled_at,
                    mode=mode,
                    link=link,
                    location=location,
                    status=status,
                    note=note,
                )
                session.commit()
            except interview_svc.InterviewError as exc:
                err_console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1) from exc

            when = (
                fmt_datetime_local(interview.scheduled_at)
                if interview.scheduled_at
                else "（没填时间）"
            )
            success(f"已登记面试 #{interview.id}：{round_ or '面试'}　{when}　{status}")
            console.print(
                f"  　[dim]投递状态已推进到 interview；记题："
                f'[green]jh question add --interview {interview.id} --q "..."[/green][/dim]'
            )
    finally:
        engine.dispose()


@app.command("list")
def interview_list(
    upcoming: Annotated[bool, typer.Option("--upcoming", help="只看接下来要面的")] = False,
) -> None:
    """列出面试（P6）。"""
    engine, factory = require_db()
    try:
        with factory() as session:
            rows = interview_svc.list_interviews(session, upcoming=upcoming)
    finally:
        engine.dispose()

    if not rows:
        console.print("[yellow]没有面试记录。[/yellow]")
        console.print(
            "  约到面试之后：[green]jh interview add --job 3 --round 一面 "
            '--at "2026-09-11 14:00" --mode 视频[/green]'
        )
        return

    title = "接下来要面的" if upcoming else "全部面试"
    table = Table(title=f"{title}（{len(rows)} 场）", header_style="bold cyan")
    table.add_column("id", justify="right")
    table.add_column("时间")
    table.add_column("还剩", justify="right")
    table.add_column("轮次")
    table.add_column("形式")
    table.add_column("状态")
    table.add_column("公司 · 职位", overflow="fold")
    table.add_column("链接/地点", overflow="fold")
    for row in rows:
        interview = row.interview
        days = row.days_until
        left = "—" if days is None else (f"{days} 天" if days >= 0 else f"已过 {-days} 天")
        table.add_row(
            str(interview.id),
            fmt_datetime_local(interview.scheduled_at) if interview.scheduled_at else "—",
            left,
            interview.round or "—",
            interview.mode or "—",
            interview.status,
            row.label,
            interview.meeting_link or interview.location or "—",
        )
    console.print(table)

    if upcoming:
        warn('面试完记得记题：jh question add --interview <id> --q "..." --rating bad --weak')
    else:
        console.print(
            "  [dim]只看接下来要面的：[green]jh interview list --upcoming[/green]"
            '　｜　面完记题：[green]jh question add --interview <id> --q "..."[/green][/dim]'
        )

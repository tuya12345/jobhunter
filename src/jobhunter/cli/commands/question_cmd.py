"""`jh question` —— 面试问题：记录（实录）/ 自评 / 按 JD 预测（P6）。

    jh question add --interview 2 --q "..." --answer "..." --rating bad \
                    --category "前端性能" --weak
    jh question add --job 3 --q "..."          # 面完忘了是哪一场，只记得岗位
    jh question predict 3 [-n 5]               # 让模型基于 JD 猜可能被问的题
    jh question list [--interview 2] [--job 3] [--source predicted]

★ `source` 字段（recorded / predicted）必须严格区分：
    `jh weak` 汇总的是"我最该补什么知识点"。
    如果把自己猜的题和真实被问的题混在一起统计，结论会完全失真
    —— 要么把"自己吓自己"当成真实缺口，要么漏掉真正答不上来的弱项。

★ 自评只有 3 档（bad / ok / good）：面试题样本量小，5 档全是噪声（评审采纳）。

★ 预测题**不过**"防编造"闸门：它是**提问**（"你了解过 Kubernetes 吗"），
  不是"声称我会 Kubernetes"。按编造拦掉它，等于把最有价值的一类准备题删掉。
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table

from jobhunter.cli.common import console, err_console, require_db, success, warn
from jobhunter.llm.factory import get_provider
from jobhunter.models import Job
from jobhunter.models.constants import QUESTION_RATINGS
from jobhunter.services import question_svc
from jobhunter.utils.time import fmt_datetime_local

app = typer.Typer(help="面试问题：记录 / 自评 / 按 JD 预测可能被问的题（P6）")


@app.command("add")
def question_add(
    q: Annotated[str, typer.Option("--q", help="问题原文")],
    interview: Annotated[
        int | None, typer.Option("--interview", "-i", help="面试 ID（与 --job 二选一）")
    ] = None,
    job: Annotated[
        int | None, typer.Option("--job", "-j", help="岗位 ID（面完忘了是哪一场时用）")
    ] = None,
    answer: Annotated[str | None, typer.Option("--answer", "-a", help="我的回答")] = None,
    rating: Annotated[
        str | None, typer.Option("--rating", "-r", help="自评：bad / ok / good")
    ] = None,
    category: Annotated[
        str | None, typer.Option("--category", "-c", help="知识点分类，如 前端性能")
    ] = None,
    weak: Annotated[
        bool, typer.Option("--weak", help="标记为没答上来（jh weak 按次数排序）")
    ] = False,
    note: Annotated[str | None, typer.Option("--note", help="备注")] = None,
) -> None:
    """记录一道面试题（P6）。"""
    if rating is not None and rating not in QUESTION_RATINGS:
        err_console.print(f"[red]自评 {rating!r} 不认识[/red]。可用：{'、'.join(QUESTION_RATINGS)}")
        raise typer.Exit(code=2)

    engine, factory = require_db()
    try:
        with factory() as session:
            try:
                row = question_svc.add_question(
                    session,
                    question=q,
                    interview_id=interview,
                    job_id=job,
                    answer=answer,
                    rating=rating,
                    category=category,
                    weak=weak,
                    note=note,
                )
                session.commit()
            except question_svc.QuestionError as exc:
                err_console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=2) from exc

            success(f"已记录问题 #{row.id}" + ("（标记为没答上来）" if weak else ""))
            if row.category is None:
                warn("这道题没写 --category —— jh weak 是按分类聚合的，没分类的题在那边看不到")
    finally:
        engine.dispose()


@app.command("predict")
def question_predict(
    job_id: Annotated[int, typer.Argument(help="岗位 ID")],
    count: Annotated[int, typer.Option("--count", "-n", help="预测几道题")] = 5,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="不用缓存，强制重新预测")] = False,
) -> None:
    """按 JD 预测可能被问的问题（source=predicted）（P6）。"""
    if count < 1:
        err_console.print("[red]--count 至少是 1[/red]")
        raise typer.Exit(code=2)

    engine, factory = require_db()
    try:
        with factory() as session:
            job = session.get(Job, job_id)
            if job is None:
                err_console.print(f"[red]找不到岗位 #{job_id}[/red]")
                raise typer.Exit(code=1)
            provider = get_provider(_settings())
            try:
                result = question_svc.predict_questions(
                    session,
                    job,
                    count=count,
                    provider=provider,
                    use_cache=not no_cache,
                )
                session.commit()
            except question_svc.QuestionError as exc:
                err_console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()

    console.print()
    console.print(
        f"[bold]按 JD 预测的面试题[/bold]（{job.title or job_id}）"
        + ("　[dim]（命中缓存，没花钱）[/dim]" if result.from_cache else "")
    )
    if not result.stored:
        warn("这次没有新题入库（都一样，或者模型没给出可用的题）。")
        if result.skipped:
            console.print(f"  [dim]已存在、跳过的 {len(result.skipped)} 条[/dim]")
        return

    table = Table(header_style="bold cyan")
    table.add_column("#", justify="right")
    table.add_column("分类")
    table.add_column("问题", overflow="fold")
    table.add_column("为什么会问", overflow="fold")
    for index, row in enumerate(result.stored, start=1):
        table.add_row(str(index), row.category or "—", row.question, row.note or "—")
    console.print(table)
    success(f"新入库 {len(result.stored)} 条（source=predicted，挂在岗位 #{job_id} 下）")
    if result.skipped:
        console.print(f"  [dim]另外 {len(result.skipped)} 条与已有题目重复，已跳过[/dim]")
    console.print(
        "  [dim]准备答案（或标记答不上来）："
        f'[green]jh question add --job {job_id} --q "..." --weak[/green]'
        "　｜　汇总：[green]jh weak[/green][/dim]"
    )


@app.command("list")
def question_list(
    interview: Annotated[int | None, typer.Option("--interview", "-i", help="按面试过滤")] = None,
    job: Annotated[int | None, typer.Option("--job", "-j", help="按岗位过滤")] = None,
    source: Annotated[
        str | None, typer.Option("--source", help="recorded（实录）/ predicted（预测）")
    ] = None,
    weak_only: Annotated[bool, typer.Option("--weak", help="只看没答上来的")] = False,
) -> None:
    """列出记录/预测的面试题（P6）。"""
    if source is not None and source not in ("recorded", "predicted"):
        err_console.print("[red]--source 只能是 recorded 或 predicted[/red]")
        raise typer.Exit(code=2)

    engine, factory = require_db()
    try:
        with factory() as session:
            rows = question_svc.list_questions(
                session, interview_id=interview, job_id=job, source=source
            )
    finally:
        engine.dispose()

    if weak_only:
        rows = [row for row in rows if row.question.is_weak]
    if not rows:
        console.print("[yellow]没有符合条件的问题。[/yellow]")
        console.print(
            '  记真实被问的：[green]jh question add --interview <id> --q "..."[/green]'
            "　｜　猜可能问的：[green]jh question predict <job_id>[/green]"
        )
        return

    table = Table(title=f"面试题（{len(rows)} 条）", header_style="bold cyan")
    table.add_column("id", justify="right")
    table.add_column("来源")
    table.add_column("分类")
    table.add_column("自评")
    table.add_column("弱", justify="center")
    table.add_column("问题", overflow="fold")
    table.add_column("岗位/轮次", overflow="fold")
    for row in rows:
        question = row.question
        table.add_row(
            str(question.id),
            row.source_label,
            question.category or "—",
            question.rating or "—",
            "●" if question.is_weak else "",
            question.question,
            f"{row.company} {row.round_ or ''}".strip() or "—",
        )
    console.print(table)
    if any(row.question.created_at for row in rows):
        newest = max(row.question.created_at for row in rows)
        console.print(f"  [dim]最近一条：{fmt_datetime_local(newest)}[/dim]")


def _settings():
    """取全局配置（单独包一层只为让调用点短一点）。"""
    from jobhunter.config.settings import get_settings

    return get_settings()

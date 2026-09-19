"""`jh apply` —— 投递打标与零手打跟进（P4 基础 / P4.5 完整）。

★★ 这是"投完就忘"痛点的正面解决方案，也是**最容易设计错**的地方。

评审原话：*"一天 20 个岗位要敲 20 次命令 → 会第 3 天弃用"*。
所以最终形状是"零手打"：

    # ① 批量打标：一条命令搞定一整轮（序号来自 `jh today` 的清单）
    jh apply 3=applied 7=replied 12=skipped

    # ② 模糊匹配：不用记 ID
    jh apply "字节" --status viewed
    #   匹配到多条时**列出来让我选**，绝不"猜一个然后改错"

    # ③ 单条：直接用序号
    jh apply 3 --status applied

    # ④ 看待处理列表（供批量打标参考序号）
    jh apply --pending

★★ 为什么这里**没有** `mark` / `timeline` 子命令（踩过的坑，很重要）

    Typer/Click 的一条硬限制：**如果一个组的回调声明了位置参数（哪怕 variadic），
    子命令就永远不会被分派** —— 所有词都被当成那个位置参数吃掉：

        jh apply timeline 5        →  root 收到 pairs=['timeline', '5']，子命令不执行
        jh apply timeline --help   →  同样被吃掉，连帮助都出不来

    而 `jh apply 3=applied 7=replied` 这种"批量打标"语法**必须**是位置参数
    （这是评审明确要求的零手打写法，不能改成 --set 3=applied 那种啰嗦形式）。

    两者不可兼得 → 选择保住批量语法，把 `jh apply` 做成**单命令应用**：

        · 单条      → `jh apply 3 --status applied`（序号就是 `jh today` 里的序号）
        · 时间轴    → P6 的 `jh apply timeline <job_id>` 改为 `jh apply --timeline <job_id>`
        · 待处理    → `jh apply --pending`

    ★ 序号（`jh today` 的第 3 条）与这里的 `3` **必须是同一套**，
      否则会出现"我以为标的是第 3 条，实际改的是别的岗位"这种最致命的误操作。
      实现时统一由 today_svc 提供"序号 → job_id"的映射，不各写一套。

★ 任何状态变更都必须写 `application_events`（append-only），
  这是"可画时间轴 + 可归因 + 统计口径可复现"的前提。

★ 合法性校验（P4.5 实现）：`--status` 只接受 models/constants.py 里的
  `APPLICATION_STATUSES`；写了不认识的状态要**报错拒绝**，
  而不是静默建一个野状态（那会让看板统计出现永远对不上的分类）。
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table

from jobhunter.cli.common import console, err_console, require_db, success, warn
from jobhunter.config.settings import get_settings
from jobhunter.models import Job
from jobhunter.models.constants import APPLICATION_STATUSES
from jobhunter.services import apply_svc, today_svc, track_svc

app = typer.Typer(
    help="投递打标：批量(3=applied) / 模糊匹配 / 单条 / 待处理列表 / 时间轴",
    # 单命令应用（原因见模块 docstring）：callback + invoke_without_command
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def apply_root(
    tokens: Annotated[
        list[str] | None,
        typer.Argument(
            help=(
                '批量打标 `3=applied 7=replied`；或关键词/序号，如 `"字节"`、`3`。'
                '支持混写：`jh apply 3 7=replied "腾讯" --status viewed`'
            )
        ),
    ] = None,
    status: Annotated[
        str | None,
        typer.Option("--status", "-s", help="给关键词/序号应用的目标状态（如 applied / viewed）"),
    ] = None,
    job: Annotated[
        int | None,
        typer.Option("--job", help="直接按岗位 ID 打标（不用记清单序号）"),
    ] = None,
    pending: Annotated[
        bool,
        typer.Option("--pending", "--list-pending", help="列出待处理的投递（供批量打标参考序号）"),
    ] = False,
    timeline: Annotated[
        int | None, typer.Option("--timeline", help="打印某个岗位的投递时间轴（P6）")
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="模糊匹配到多条时不询问，全部应用")
    ] = False,
) -> None:
    """零手打跟进入口（P4.5：批量 / 模糊匹配 / 待处理清单；P6：时间轴）。"""
    if timeline is not None:
        _print_timeline(timeline)
        return

    tokens, status, yes = _pull_options_out_of_tokens(list(tokens or []), status, yes)

    if pending:
        _print_pending()
        return

    # ★ 状态值先校验：拼错一个状态是**最常见**的手误，应该在碰数据库之前
    #   就明确报出来。等走到 set_status 才报，用户可能先看到"没有匹配的岗位"
    #   这种与真正错误无关的提示（实测踩到）。
    if status is not None and status not in APPLICATION_STATUSES:
        err_console.print(
            f"[red]状态 {status!r} 不认识[/red]。可用：{'、'.join(APPLICATION_STATUSES)}"
        )
        raise typer.Exit(code=2)

    # ---- 批量语法 `3=applied 7=replied`（★ P4.5 的核心：一条命令处理一整轮）----
    pairs = [token for token in tokens if "=" in token]
    if pairs:
        _mark_batch(pairs)
        return

    if job is not None:
        if status is None:
            err_console.print("[red]用 --job 时必须同时给 --status（如 --status applied）[/red]")
            raise typer.Exit(code=2)
        _mark_by_job_id(job, status)
        return

    if not tokens and status is None:
        # 什么都不给 → 显示帮助（比直接报错友好）
        typer.echo(typer.get_current_context().get_help())
        raise typer.Exit(code=0)

    if status is None:
        err_console.print(
            "[red]给序号打标时要同时给 --status，例如：jh apply 3 --status applied[/red]"
        )
        raise typer.Exit(code=2)

    # ---- 单个序号：映射到岗位（序号 = `jh today` 里那个序号）----------------
    if len(tokens) == 1 and tokens[0].isdigit():
        _mark_by_index(int(tokens[0]), status)
        return

    # ---- 模糊匹配（★ P4.5）：按公司名/职位名找，命中多条**列出来让我选** ------
    _mark_by_keyword(" ".join(tokens), status, yes=yes)


# ===========================================================================
# P6：投递时间轴
# ===========================================================================
def _print_timeline(job_id: int) -> None:
    """`jh apply --timeline <job_id>`：把事件流画成一条时间轴。

    ★ 为什么是 `--timeline` 而不是计划书里写的 `jh apply timeline <id>`：
      模块 docstring 里写了那个 Click 硬限制 —— `jh apply` 是**单命令应用**，
      callback 带可变位置参数（`3=applied "字节"` 那种语法），
      于是 `timeline` 会被当成位置参数吃掉，子命令永远不会被分派。
      功能完全一致（甚至更短），只是形状从子命令改成选项。
    """
    engine, factory = require_db()
    try:
        with factory() as session:
            try:
                timeline = track_svc.build_timeline(session, job_id)
            except LookupError as exc:
                err_console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()

    console.print()
    console.print(
        f"[bold]{timeline.company or '—'} · {timeline.title or job_id}[/bold]"
        f"　[dim]（job_id={timeline.job_id}）[/dim]"
    )
    left = "还没投出去" if timeline.days is None else f"投出去 {timeline.days} 天"
    console.print(f"  当前状态：[bold]{timeline.status_label}[/bold]　｜　{left}")
    console.print()

    if timeline.empty:
        warn("这条投递还没有任何事件。")
        for note in timeline.notes:
            console.print(f"  [dim]· {note}[/dim]")
        return

    table = Table(title="投递时间轴", header_style="bold cyan")
    table.add_column("时间")
    table.add_column("发生了什么", overflow="fold")
    for entry in timeline.entries:
        table.add_row(entry.local_text, entry.label)
    console.print(table)

    for note in timeline.notes:
        console.print(f"  [dim]· {note}[/dim]")
    console.print(
        "  [dim]事件流是 append-only 的：它记的是**真实发生过的顺序**，"
        "不会因为当前状态被改写。[/dim]"
    )


# ===========================================================================
# P4.5：待处理清单 / 批量打标 / 模糊匹配
# ===========================================================================
def _print_pending() -> None:
    """`jh apply --pending`：列出需要我动手的投递（含 job_id，供批量打标引用）。"""
    engine, factory = require_db()
    try:
        with factory() as session:
            rows = apply_svc.list_actionable(session)
    finally:
        engine.dispose()

    if not rows:
        success("没有待处理的投递 —— 要么都处理完了，要么还没开始投。")
        console.print("  [dim]先出清单：[green]jh today[/green][/dim]")
        return

    console.print()
    table = Table(title=f"待处理（{len(rows)} 条）", header_style="bold cyan")
    table.add_column("job_id", justify="right")
    table.add_column("状态")
    table.add_column("投后", justify="right")
    table.add_column("公司", overflow="fold")
    table.add_column("职位", overflow="fold")
    table.add_column("为什么在这里", overflow="fold")
    for row in rows:
        table.add_row(
            str(row.job_id),
            row.status,
            "—" if row.days is None else f"{row.days} 天",
            row.company or "—",
            row.title or "—",
            row.reason,
        )
    console.print(table)
    console.print()
    console.print("下一步（三种写法都行）：")
    console.print(
        "  [green]jh apply 3=applied 7=replied[/green]      按 [bold]jh today[/bold] 的序号批量打标"
    )
    console.print(
        "  [green]jh apply #12=replied[/green]        "
        "按上面的 [bold]job_id[/bold] 打标（最不会认错）"
    )
    console.print('  [green]jh apply "字节" --status viewed[/green]   按公司/职位名模糊匹配')


def _parse_pairs(pairs: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """把 `3=applied` / `#12=replied` 拆成 (定位符, 目标状态)。

    Returns:
        (parsed, errors) —— 只要有错就**整批不执行**（见 `_mark_batch` 的说明）。
    """
    parsed: list[tuple[str, str]] = []
    errors: list[str] = []
    for token in pairs:
        target, _, status = token.partition("=")
        target = target.strip()
        status = status.strip()
        if not target or not status:
            errors.append(f"`{token}` 格式不对，应写成 `3=applied` 或 `#12=replied`")
            continue
        if status not in APPLICATION_STATUSES:
            errors.append(
                f"`{token}` 里的状态 `{status}` 不认识（可用：{'、'.join(APPLICATION_STATUSES)}）"
            )
            continue
        parsed.append((target, status))
    return parsed, errors


def _resolve_target(session: object, target: str, settings: object) -> Job | None:
    """把定位符解析成岗位：`#12` = job_id；`3` = `jh today` 清单里的序号。"""
    if target.startswith("#"):
        raw = target[1:]
        if not raw.isdigit():
            return None
        return session.get(Job, int(raw))  # type: ignore[attr-defined]
    if target.isdigit():
        job, _from_snapshot = today_svc.resolve_index(session, int(target), settings=settings)  # type: ignore[arg-type]
        return job
    return None


def _mark_batch(pairs: list[str]) -> None:
    """批量打标：**先全部校验，再全部执行**。

    ★ 为什么不是"能改的先改、错的报错"：
      批量的意义是"一条命令处理一整轮"。如果一半成功一半失败，用户既不知道
      最终状态是什么，也不知道该不该重跑（重跑会不会把已改的又改一遍）。
      **全对才执行、有错就一条都不动**，是唯一让人放心的语义。
    """
    engine, factory = require_db()
    settings = get_settings()
    parsed, errors = _parse_pairs(pairs)

    try:
        with factory() as session:
            resolved: list[tuple[Job, str]] = []
            for target, target_status in parsed:
                job = _resolve_target(session, target, settings)
                if job is None:
                    errors.append(
                        f"找不到 `{target}` 对应的岗位"
                        "（序号看 `jh today`，job_id 看 `jh apply --pending`）"
                    )
                    continue
                resolved.append((job, target_status))

            if errors:
                err_console.print("[red]这批打标没有执行（有错就整批不动）：[/red]")
                for item in errors:
                    err_console.print(f"  [red]·[/red] {item}")
                raise typer.Exit(code=2)

            results = []
            for job, target_status in resolved:
                results.append(
                    (
                        job,
                        apply_svc.set_status(
                            session,
                            job.id,
                            target_status,
                            source=f"jh apply {job.id}={target_status}",
                        ),
                    )
                )
            session.commit()
    finally:
        engine.dispose()

    console.print()
    for job, result in results:
        mark = "✓" if result.changed else "="
        arrow = (
            f"{result.from_status} → {result.to_status}"
            if result.changed
            else f"已经是 {result.to_status}"
        )
        console.print(
            f"  [{('green' if result.changed else 'dim')}]{mark}[/] "
            f"[bold]{job.company or '—'} · {job.title or '（未写职位）'}[/bold]"
            f"（#{job.id}）　{arrow}"
        )
    changed = sum(1 for _job, result in results if result.changed)
    success(f"批量打标完成：{len(results)} 条，其中 {changed} 条有变化（每条都写了事件）")


def _mark_by_keyword(keyword: str, status: str, *, yes: bool) -> None:
    """模糊匹配公司名/职位名，然后打标。

    ★ 命中多条时**绝不猜**：列出来让用户用 job_id 或序号重跑。
      这是"我不信任模糊匹配"的体现 —— 打错状态的代价（以为投了某家、其实标的是另一家）
      远大于再敲一次命令。
    """
    engine, factory = require_db()
    try:
        with factory() as session:
            rows = apply_svc.list_actionable(session)
            matched = [
                row
                for row in rows
                if keyword.lower() in (row.company or "").lower()
                or keyword.lower() in (row.title or "").lower()
            ]
            if not matched:
                err_console.print(
                    f"[red]没有匹配「{keyword}」的待处理投递。[/red]\n"
                    "  [dim]看全部待处理：[green]jh apply --pending[/green][/dim]"
                )
                raise typer.Exit(code=1)
            if len(matched) > 1 and not yes:
                err_console.print(
                    f"[yellow]「{keyword}」匹配到 {len(matched)} 条 —— 没有替你猜。[/yellow]"
                )
                table = Table(header_style="bold cyan")
                table.add_column("job_id", justify="right")
                table.add_column("状态")
                table.add_column("公司", overflow="fold")
                table.add_column("职位", overflow="fold")
                for row in matched:
                    table.add_row(str(row.job_id), row.status, row.company or "—", row.title or "—")
                console.print(table)
                console.print(
                    "  [dim]确认要改哪一条："
                    "[green]jh apply #<job_id>[/green] --status "
                    f"{status}　｜　全改：[green]--yes[/green][/dim]"
                )
                raise typer.Exit(code=1)

            results = []
            for row in matched:
                try:
                    result = apply_svc.set_status(
                        session, row.job_id, status, source=f"jh apply {keyword!r}"
                    )
                except apply_svc.ApplicationError as exc:
                    # 状态非法之类的错误要给一句人话，而不是让 Python 堆栈冲出来
                    err_console.print(f"[red]{exc}[/red]")
                    raise typer.Exit(code=1) from exc
                results.append((row, result))
            session.commit()
    finally:
        engine.dispose()

    for row, result in results:
        console.print(
            f"  [bold]{row.label}[/bold]（#{row.job_id}）　"
            f"{result.from_status} → {result.to_status}"
        )
    success(f"已按「{keyword}」打标 {len(results)} 条")


def _pull_options_out_of_tokens(
    tokens: list[str], status: str | None, yes: bool
) -> tuple[list[str], str | None, bool]:
    """把被可变位置参数**吞掉**的选项捞回来（Click 的解析行为，手敲时踩到两次）。

    ★ 现象：
        `jh apply 1 --status applied`   → `status` 竟然是 **None**；
        `jh apply "公司" --status applied --yes` → 关键词变成 **"公司 --yes"**；
        而把选项写在位置参数**前面**就正常。
      原因：`tokens` 是 `nargs=-1` 的可变位置参数，Click 一旦开始吃位置参数，
      **后面的选项也一起当位置参数吃掉** —— 于是最自然的语序直接失效，
      而且不报错（选项"消失"进关键词里），最难查。

    这里做**最小修补**：`--status X` / `-s X` / `--status=X` / `--yes` / `-y`
    写在任意位置都认。
    """
    cleaned: list[str] = []
    found_status = status
    found_yes = yes
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("--status", "-s") and index + 1 < len(tokens):
            if found_status is None:
                found_status = tokens[index + 1]
            index += 2
            continue
        if token.startswith("--status="):
            if found_status is None:
                found_status = token.split("=", 1)[1]
            index += 1
            continue
        if token in ("--yes", "-y"):
            found_yes = True
            index += 1
            continue
        cleaned.append(token)
        index += 1
    return cleaned, found_status, found_yes


def _mark_by_job_id(job_id: int, status: str) -> None:
    """按岗位 ID 打标（最直接、最不会打错的一种）。"""
    engine, factory = require_db()
    try:
        with factory() as session:
            try:
                result = apply_svc.set_status(
                    session, job_id, status, source=f"jh apply --job {job_id}"
                )
            except apply_svc.ApplicationError as exc:
                err_console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1) from exc
            session.commit()
    finally:
        engine.dispose()
    _report(result, status)


def _mark_by_index(index: int, status: str) -> None:
    """按 `jh today` 的序号打标。

    ★ 序号走 `today_svc.resolve_index()`：优先用**今天的清单快照**，
      这样"投完第 1 条之后再标第 3 条"标的仍然是你刚才看到的第 3 条
      （没有快照时才退回实时筛选）。
    ★ **不生成开场白**：打标不需要调模型，也就不会在打标时花钱。
    """
    engine, factory = require_db()
    settings = get_settings()
    try:
        with factory() as session:
            job, from_snapshot = today_svc.resolve_index(session, index, settings=settings)
            if job is None:
                err_console.print(
                    f"[red]清单里没有第 {index} 条[/red]。"
                    "先跑 [green]jh today[/green] 看序号，或用 "
                    "[green]jh apply --job <id>[/green]。"
                )
                raise typer.Exit(code=1)
            try:
                result = apply_svc.set_status(
                    session, job.id, status, source=f"jh apply {index}={status}"
                )
            except apply_svc.ApplicationError as exc:
                err_console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1) from exc
            session.commit()
            label = f"{job.company or '—'} · {job.title or '（未写职位）'}"
            if not from_snapshot:
                console.print(
                    "  [dim]（没有今天的清单快照，按实时筛选解析序号；"
                    "跑一次 `jh today` 可固定序号）[/dim]"
                )
    finally:
        engine.dispose()
    console.print(f"  第 {index} 条：[bold]{label}[/bold]")
    _report(result, status)


def _report(result: apply_svc.ApplyResult, status: str) -> None:
    """统一的打标结果输出。"""
    if not result.changed:
        warn(f"岗位 #{result.job_id} 本来就是 {status}，没有改动（也没有重复写事件）")
        return
    success(f"岗位 #{result.job_id}：{result.from_status} → [bold]{result.to_status}[/bold]")
    if status == "applied":
        console.print(
            "  [dim]已记进 applications 并写了一条事件（时间轴与日额度都从它聚合）。[/dim]"
        )
    console.print("  [dim]可用状态：" + "、".join(APPLICATION_STATUSES) + "[/dim]")

"""`jh assessment` —— 笔试 / 测评 / AI 面（P6）。

★ 为什么必须有这个命令（v3 新增，产品视角评审的缺口 #1）：
    海投**必定**遇到在线笔试/测评。原来的状态机里没有这个环节，
    后果是岗位卡在 replied 之后**再也不会提醒我**，
    而笔试邀请通常只有 48-72 小时 —— 错过就等于自动淘汰。
    这类"沉默的失败"最该防住，因为用户根本不会发现是工具的问题。

P6 的形状：

    jh assessment add --job <id> --kind 笔试 --deadline "2026-09-11 23:59"
    jh assessment list [--upcoming]

★ `--deadline` 是这张表存在的核心理由：
  顶部提醒的 "⏳ 笔试截止 1 条（9-11 饿了么）" 就是从它算出来的。

★ CLI 只做三件事：校验参数 → 调 `assessment_svc` → 渲染。
  业务规则（只前进不回退、窗口几天）一律在服务层，这里不许再判一次 ——
  否则 CLI 与 API 会各有一套规则，迟早对不上。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

import typer
from rich.table import Table
from sqlalchemy.orm import Session

from jobhunter.cli.common import console, err_console, require_db, success
from jobhunter.models import Job
from jobhunter.models.constants import ASSESSMENT_KINDS, ASSESSMENT_STATUSES
from jobhunter.services import apply_svc, assessment_svc
from jobhunter.utils.time import days_between, fmt_datetime_local, now_utc, parse_local_input

app = typer.Typer(help="笔试/测评/AI面：登记与截止提醒（P6）")


@app.command("add")
def assessment_add(
    job: Annotated[int, typer.Option("--job", "-j", help="关联的岗位 ID")],
    kind: Annotated[str, typer.Option("--kind", "-k", help="笔试 / 测评 / AI面")] = "笔试",
    deadline: Annotated[
        str | None,
        typer.Option("--deadline", "-d", help='截止时间（北京时间），如 "2026-09-11 23:59"'),
    ] = None,
    invited: Annotated[
        str | None, typer.Option("--invited", help="收到邀请的时间（北京时间）")
    ] = None,
    platform: Annotated[
        str | None, typer.Option("--platform", "-p", help="牛客 / 赛码 / 北森 …")
    ] = None,
    status: Annotated[str, typer.Option("--status", help="待做 / 已做 / 已过期")] = "待做",
    note: Annotated[str | None, typer.Option("--note", help="备注")] = None,
) -> None:
    """登记一次笔试/测评（P6）。"""
    # ★ 参数先校验、再碰数据库（与 `jh apply` 一致）：类型/状态写错是最常见的手误，
    #   应该在"库还没建"这类无关提示之前就报出来，否则用户会照着错误的线索去排查。
    if kind not in ASSESSMENT_KINDS:
        err_console.print(f"[red]类型 {kind!r} 不认识[/red]。可用：{'、'.join(ASSESSMENT_KINDS)}")
        raise typer.Exit(code=2)
    if status not in ASSESSMENT_STATUSES:
        err_console.print(
            f"[red]状态 {status!r} 不认识[/red]。可用：{'、'.join(ASSESSMENT_STATUSES)}"
        )
        raise typer.Exit(code=2)
    # 时间格式错也是**参数**错（退出码 2），与"业务失败"（岗位不存在，1）分开
    deadline_at = _parse_local_or_exit(deadline, "--deadline")
    invited_at = _parse_local_or_exit(invited, "--invited")

    engine, factory = require_db()
    try:
        with factory() as session:
            before = _current_status(session, job)
            try:
                assessment = assessment_svc.add_assessment(
                    session,
                    job,
                    kind=kind,
                    deadline_at=deadline_at,
                    invited_at=invited_at,
                    platform=platform,
                    status=status,
                    note=note,
                )
            except assessment_svc.AssessmentError as exc:
                # 业务失败：给一句人话，而不是让 Python 堆栈冲出来
                err_console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1) from exc
            session.commit()
            # ★ 打印的是**库里真实的**投递状态，而不是"我以为推进到了 assessment"：
            #   只前进不回退的规则下，状态可能压根没动（比如已经面试完了）。
            after = _current_status(session, job)
            job_row = session.get(Job, job)
            label = (
                f"{job_row.company or '—'} · {job_row.title or '（未写职位）'}"
                if job_row is not None
                else f"岗位 #{job}"
            )
            summary = {
                "assessment_id": assessment.id,
                "kind": assessment.kind,
                "label": label,
                "deadline_at": assessment.deadline_at,
                "days_left": days_between(now_utc(), assessment.deadline_at),
                "platform": assessment.platform,
                "assessment_status": assessment.status,
                "app_before": before,
                "app_after": after,
            }
    finally:
        engine.dispose()

    _print_added(**summary)


@app.command("list")
def assessment_list(
    upcoming: Annotated[bool, typer.Option("--upcoming", help="只看即将截止的")] = False,
) -> None:
    """列出笔试/测评（P6）。"""
    engine, factory = require_db()
    try:
        with factory() as session:
            rows = assessment_svc.list_assessments(session, upcoming=upcoming)
    finally:
        engine.dispose()

    if not rows:
        _print_empty(upcoming=upcoming)
        return

    console.print()
    title = f"笔试/测评（{len(rows)} 条）"
    if upcoming:
        title += f"　即将截止（{assessment_svc.UPCOMING_DAYS} 天内且待做）"
    table = Table(title=title, header_style="bold cyan")
    table.add_column("id", justify="right")
    table.add_column("截止")
    table.add_column("剩余", justify="right")
    table.add_column("类型")
    table.add_column("状态")
    table.add_column("公司 · 职位", overflow="fold")
    table.add_column("平台")
    table.add_column("分数")
    for row in rows:
        table.add_row(
            str(row.assessment.id),
            fmt_datetime_local(row.assessment.deadline_at),
            _fmt_days_left(row.days_left),
            row.assessment.kind,
            row.assessment.status,
            row.label,
            row.assessment.platform or "—",
            row.assessment.score or "—",
        )
    console.print(table)
    console.print(
        "  [dim]截止时间就是这张表存在的理由：顶部提醒里的「⏳ 笔试截止 N 条」按它算。[/dim]"
    )
    console.print(
        "  下一步：[green]jh assessment list --upcoming[/green] 只看快截止的"
        "　｜　约到面试后 [green]jh apply --job <id> --status interview[/green] 继续推进"
    )


# ---------------------------------------------------------------------------
# 渲染 / 参数辅助
# ---------------------------------------------------------------------------
def _parse_local_or_exit(text: str | None, option: str) -> datetime | None:
    """把用户手输的**北京时间**解析成 UTC；为空返回 None，格式不对 → 退出码 2。

    ★ 为什么解析失败也算"用法错误"（2）：用户写的是"明天下午"这种自然语言，
      这是**参数**问题，不是业务失败 —— 与项目既有约定一致（用法 2 / 业务 1）。
    ★ 为什么宁可报错也不猜（见 `utils/time.py`）：猜错时间的后果是错过笔试，
      比让用户重输一次严重得多。
    """
    if text is None:
        return None
    try:
        return parse_local_input(text)
    except ValueError as exc:
        err_console.print(f"[red]{option} 解析失败：{exc}[/red]")
        raise typer.Exit(code=2) from exc


def _current_status(session: Session, job_id: int) -> str | None:
    """当前投递状态（只用于把"发生了什么"讲清楚；没有投递记录时为 None）。"""
    application = apply_svc.get_latest_application(session, job_id)
    return application.status if application is not None else None


def _fmt_days_left(days: int | None) -> str:
    """剩余天数的统一写法：`—` / `今天` / `N 天` / `已过期 N 天`。

    ★ 过期用红色、今天用黄色：这两类才是"现在就得动手"，颜色让人一眼看到。
    """
    if days is None:
        return "—"
    if days < 0:
        return f"[red]已过期 {-days} 天[/red]"
    if days == 0:
        return "[yellow]今天[/yellow]"
    return f"{days} 天"


def _print_added(
    *,
    assessment_id: int,
    kind: str,
    label: str,
    deadline_at: datetime | None,
    days_left: int | None,
    platform: str | None,
    assessment_status: str,
    app_before: str | None,
    app_after: str | None,
) -> None:
    """登记成功后的小结（含"投递状态到底动没动"）。"""
    success(f"已登记{kind} #{assessment_id}（{label}）")
    tail = "" if days_left is None else f"（{_fmt_days_left(days_left)}）"
    console.print(
        f"  截止：{fmt_datetime_local(deadline_at)}{tail}　状态：{assessment_status}"
        + (f"　平台：{platform}" if platform else "")
    )
    console.print(f"  {_describe_status(app_before, app_after)}")
    console.print("  [dim]看即将截止的：[green]jh assessment list --upcoming[/green][/dim]")


def _describe_status(before: str | None, after: str | None) -> str:
    """把"登记时投递状态发生了什么"讲成人话（包括**没发生**什么）。"""
    if after is None:
        return "投递状态：—"
    if before is None:
        return f"投递状态：新建 pending → {after}（收到笔试才回来登记，不必先打标）"
    if before == after:
        return f"投递状态：{after}（未改动 —— 已在更靠后的环节，登记笔试不回退）"
    return f"投递状态：{before} → {after}"


def _print_empty(*, upcoming: bool) -> None:
    """空清单必须给出"下一步敲什么"，否则用户不知道这张表怎么填。"""
    if upcoming:
        window = assessment_svc.UPCOMING_DAYS
        console.print(f"[yellow]没有即将截止的笔试/测评（{window} 天内、状态待做）。[/yellow]")
    else:
        console.print("[yellow]还没有登记任何笔试/测评。[/yellow]")
    console.print("  收到笔试邀请就记一条 —— 截止时间是这张表存在的理由：")
    console.print(
        "  [green]jh assessment add --job <岗位ID> --kind 笔试[/green]"
        '　[green]--deadline "2026-09-11 23:59"[/green]'
    )

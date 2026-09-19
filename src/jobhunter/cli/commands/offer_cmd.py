"""`jh offer` —— Offer 登记与横向比较（P6）。

★ 为什么必须有（v3 新增，产品视角评审的缺口 #2）：
    原来的 8 张表里**居然没有 Offer** —— 整个流程的终点站缺失。
    拿到 offer 之后，薪资/地点/签约截止日只能记在脑子里，
    而"多个 offer 怎么选"恰恰是求职后期最需要决策支持的时刻。

P6 的形状：

    jh offer add --job <id> --salary "20-25K" --months 15 --location 深圳 \\
                 --deadline "2026-09-20"
    jh offer compare
        薪资（含年包 = 月薪 × 月数）/ 地点 / 截止日 横向对比

★ 年包必须算出来：只写"25K"看不出 15 薪和 13 薪的差距（差 5 万/年）。
  `Offer.annual_package` 已经把这个计算放在模型上。

★ CLI 只做三件事：校验参数 → 调 `offer_svc` → 渲染。
  年包的排序/高亮口径在 `offer_svc.compare_rows` 里只写一次，这里不再判。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table
from sqlalchemy.orm import Session

from jobhunter.cli.common import console, err_console, require_db, success
from jobhunter.services import apply_svc, offer_svc
from jobhunter.utils.time import days_between, fmt_datetime_local, now_utc, parse_local_input

app = typer.Typer(help="Offer：登记 / 横向比较 / 签约截止提醒（P6）")


@app.command("add")
def offer_add(
    job: Annotated[int, typer.Option("--job", "-j", help="关联的岗位 ID")],
    salary: Annotated[str | None, typer.Option("--salary", help='薪资原文，如 "20-25K"')] = None,
    salary_month: Annotated[
        int | None, typer.Option("--salary-month", help="月薪（元），用于比较")
    ] = None,
    months: Annotated[
        int | None, typer.Option("--months", help="发几个月（如 15）→ 算年包")
    ] = None,
    location: Annotated[str | None, typer.Option("--location", help="工作地点")] = None,
    deadline: Annotated[
        str | None, typer.Option("--deadline", "-d", help="签约截止日（北京时间）")
    ] = None,
    pros: Annotated[str | None, typer.Option("--pros", help="优点")] = None,
    cons: Annotated[str | None, typer.Option("--cons", help="缺点")] = None,
    note: Annotated[str | None, typer.Option("--note", help="备注")] = None,
) -> None:
    """登记一个 offer（P6）。"""
    # ★ 纯参数问题（写错一个数字）先报，且算"用法错误"（2）：
    #   非正数的月薪/月数几乎一定是手滑（少按一个 0、多写个负号），
    #   让它进库会把比较表算歪 —— 服务层同样拦一道，防止 API 绕过 CLI。
    for value, name in ((salary_month, "--salary-month"), (months, "--months")):
        if value is not None and value < 1:
            err_console.print(f"[red]{name} 必须是正数，收到 {value}[/red]")
            raise typer.Exit(code=2)
    deadline_at = _parse_local_or_exit(deadline, "--deadline")

    engine, factory = require_db()
    try:
        with factory() as session:
            before = _current_status(session, job)
            try:
                offer = offer_svc.add_offer(
                    session,
                    job,
                    salary_text=salary,
                    salary_month=salary_month,
                    months=months,
                    location=location,
                    deadline_at=deadline_at,
                    pros=pros,
                    cons=cons,
                    note=note,
                )
            except offer_svc.OfferError as exc:
                err_console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1) from exc
            session.commit()
            after = _current_status(session, job)
            label = f"{offer.company or '—'} · {offer.title or '（未写职位）'}"
            summary: dict[str, Any] = {
                "offer_id": offer.id,
                "label": label,
                "salary_text": offer.salary_text,
                "salary_month": offer.salary_month,
                "months": offer.months,
                "annual": offer.annual_package,
                "location": offer.location,
                "deadline_at": offer.deadline_at,
                "days_left": days_between(now_utc(), offer.deadline_at),
                "status": offer.status,
                "app_before": before,
                "app_after": after,
            }
    finally:
        engine.dispose()

    _print_added(**summary)


@app.command("compare")
def offer_compare() -> None:
    """横向比较所有 offer（P6）。"""
    engine, factory = require_db()
    try:
        with factory() as session:
            compared = offer_svc.compare_rows(offer_svc.list_offers(session))
    finally:
        engine.dispose()

    if not compared:
        _print_empty()
        return

    console.print()
    table = Table(title=f"Offer 横向比较（{len(compared)} 个）", header_style="bold cyan")
    table.add_column("公司", overflow="fold")
    table.add_column("职位", overflow="fold")
    table.add_column("薪资原文", overflow="fold")
    table.add_column("月薪", justify="right")
    table.add_column("月数", justify="right")
    table.add_column("年包（万元）", justify="right")
    table.add_column("地点", overflow="fold")
    table.add_column("签约截止", overflow="fold")
    table.add_column("状态")
    table.add_column("优点", overflow="fold")
    table.add_column("缺点", overflow="fold")

    missing_annual = 0
    best: str | None = None
    for item in compared:
        offer = item["offer"]
        annual_wan = item["annual_wan"]
        if annual_wan is None:
            missing_annual += 1
            annual_text = "—"
        else:
            # 1 位小数：万元级别的差异在 0.1 万以下对决策没有意义，多了反而看不清
            annual_text = f"{annual_wan:.1f}"
        if item["is_best"]:
            best = f"{offer.company or '—'}（年包 {annual_text} 万）"
        table.add_row(
            offer.company or "—",
            offer.title or "—",
            # ★ 薪资原文/优缺点是我手打的自由文本，用 escape 转义：
            #   rich 会把 `[b]` 之类当成样式标签**直接吃掉**（实测 "优点：[b] 稳定"
            #   渲染成 "优点： 稳定"），那是静默丢字 —— 数据表里最不该发生的事。
            escape(offer.salary_text or "—"),
            "—" if offer.salary_month is None else str(offer.salary_month),
            "—" if offer.months is None else str(offer.months),
            annual_text,
            offer.location or "—",
            _fmt_deadline(offer.deadline_at, item["days_left"]),
            offer.status,
            escape(offer.pros or "—"),
            escape(offer.cons or "—"),
            # ★ 年包最大的整行高亮（并列最大时谁也不标，口径见 offer_svc.compare_rows）
            style="bold green" if item["is_best"] else None,
        )
    console.print(table)

    if best is not None:
        console.print(f"  [bold green]✓ 年包最高：{best}[/bold green]")
    elif missing_annual == 0 and len(compared) > 1:
        console.print(
            "  [dim]年包并列最大，没有标出最优 —— 差异要看地点/优缺点这些非数字项。[/dim]"
        )

    if missing_annual:
        console.print(
            f"  [yellow]有 {missing_annual} 个 offer 算不出年包："
            "补 [green]--salary-month[/green] 与 [green]--months[/green] 才能算年包"
            '（只写 "25K" 看不出 15 薪和 13 薪差 5 万）。[/yellow]'
        )
    if len(compared) == 1:
        console.print("  [dim]只有一个 offer，等有第二个再来比较。[/dim]")
    console.print("  [dim]签约截止最近的在最上面（按截止时间排序）。[/dim]")


# ---------------------------------------------------------------------------
# 渲染 / 参数辅助
# ---------------------------------------------------------------------------
def _parse_local_or_exit(text: str | None, option: str) -> datetime | None:
    """把用户手输的**北京时间**解析成 UTC；为空返回 None，格式不对 → 退出码 2。

    ★ 与 `assessment_cmd._parse_local_or_exit` 是刻意重复的十几行：
      `cli/common.py` 不在本次改动范围内，而项目原则是"别为两个调用点提前造抽象"
      （见 common.py 关于 print_table 的那段）。第三个使用者出现时再往上提。
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


def _fmt_deadline(deadline_at: datetime | None, days_left: int | None) -> str:
    """签约截止的写法：`2026-09-20 00:00（还剩 3 天）`。

    ★ 剩余天数必须跟着日期一起出现：签约截止是**沉默失败**的高发处，
      只给一个日期，人脑不会去减今天。
    """
    if deadline_at is None:
        return "—"
    text = fmt_datetime_local(deadline_at)
    if days_left is None:
        return text
    if days_left < 0:
        return f"{text}（[red]已过期 {-days_left} 天[/red]）"
    if days_left == 0:
        return f"{text}（[yellow]今天[/yellow]）"
    return f"{text}（还剩 {days_left} 天）"


def _print_added(
    *,
    offer_id: int,
    label: str,
    salary_text: str | None,
    salary_month: int | None,
    months: int | None,
    annual: int | None,
    location: str | None,
    deadline_at: datetime | None,
    days_left: int | None,
    status: str,
    app_before: str | None,
    app_after: str | None,
) -> None:
    """登记成功后的小结（含年包与"投递状态到底动没动"）。"""
    success(f"已登记 offer #{offer_id}（{label}）")
    console.print(f"  {_fmt_salary(salary_text, salary_month, months, annual)}")
    if location:
        console.print(f"  地点：{location}　状态：{status}")
    else:
        console.print(f"  状态：{status}")
    console.print(f"  签约截止：{_fmt_deadline(deadline_at, days_left)}")
    console.print(f"  {_describe_status(app_before, app_after)}")
    console.print("  [dim]多拿几个再横向比：[green]jh offer compare[/green][/dim]")


def _fmt_salary(
    salary_text: str | None, salary_month: int | None, months: int | None, annual: int | None
) -> str:
    """把薪资拼成一句：原文 + 月薪×月数 = 年包（算不出来就说缺什么）。"""
    parts: list[str] = []
    if salary_text:
        parts.append(f"薪资：{salary_text}")
    if salary_month is not None and months is not None and annual is not None:
        parts.append(
            f"月薪 {salary_month} × {months} 个月 = [bold]年包 {annual / 10000:.1f} 万[/bold]"
        )
    elif salary_month is not None:
        parts.append(f"月薪 {salary_month}（缺 --months，算不出年包）")
    elif months is not None:
        parts.append(f"{months} 个月（缺 --salary-month，算不出年包）")
    elif salary_text:
        # 只写了原文：当场就把"补什么才能比"说清楚，别等 compare 时才发现比不了
        parts.append("（补 --salary-month 与 --months 才能算年包）")
    else:
        parts.append("薪资：—（补 --salary-month 与 --months 才能算年包）")
    return "　".join(parts)


def _describe_status(before: str | None, after: str | None) -> str:
    """把"登记时投递状态发生了什么"讲成人话（包括**没发生**什么）。"""
    if after is None:
        return "投递状态：—"
    if before is None:
        return f"投递状态：新建 pending → {after}（拿到 offer 才回来登记，不必先打标）"
    if before == after:
        return f"投递状态：{after}（未改动 —— 已在更靠后的环节/终点，不回退）"
    return f"投递状态：{before} → {after}"


def _print_empty() -> None:
    """空表要给出可直接照抄的下一条命令（而不是只说"没有数据"）。"""
    console.print("[yellow]还没有登记任何 offer。[/yellow]拿到 offer 后先记一条：")
    console.print(
        '  [green]jh offer add --job <岗位ID> --salary "20-25K" '
        "--salary-month 22000 --months 15 --location 深圳[/green]"
    )

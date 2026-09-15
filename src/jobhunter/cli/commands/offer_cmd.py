"""`jh offer` —— Offer 登记与横向比较（P6 实现，本阶段为占位）。

★ 为什么必须有（v3 新增，产品视角评审的缺口 #2）：
    原来的 8 张表里**居然没有 Offer** —— 整个流程的终点站缺失。
    拿到 offer 之后，薪资/地点/签约截止日只能记在脑子里，
    而"多个 offer 怎么选"恰恰是求职后期最需要决策支持的时刻。

P6 的最终形状：

    jh offer add --job <id> --salary "20-25K" --months 15 --location 深圳 \\
                 --deadline "2026-09-20"
    jh offer compare
        薪资（含年包 = 月薪 × 月数）/ 地点 / 截止日 横向对比

★ 年包必须算出来：只写"25K"看不出 15 薪和 13 薪的差距（差 5 万/年）。
   `Offer.annual_package` 已经把这个计算放在模型上。
"""

from __future__ import annotations

from typing import Annotated

import typer

from jobhunter.cli.common import not_implemented

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
    not_implemented(
        f"jh offer add（job={job}, salary={salary}, salary_month={salary_month}, "
        f"months={months}, location={location}, deadline={deadline}, "
        f"pros={pros}, cons={cons}, note={note}）",
        "P6",
    )


@app.command("compare")
def offer_compare() -> None:
    """横向比较所有 offer（P6）。"""
    not_implemented("jh offer compare（薪资/地点/截止日横向对比）", "P6")

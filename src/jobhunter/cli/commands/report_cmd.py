"""`jh report` —— 求职周报（P6 实现，本阶段为占位）。

P6 的最终形状：

    jh report [--week] [--out output/]
        生成 Markdown 周报：本周投递 N 条 / 回复 M 条 / 面试 K 场 /
        笔试截止 / 薄弱点 TOP3 / 下周待办

★ 为什么值得做：这份周报是"给自己看的复盘"，也是**面试时可以讲的东西**
  （"我用数据复盘过自己的投递转化率"比"我很努力在投简历"有说服力得多）。

★ 只写新文件（写到 output/），绝不覆盖已有报告。
"""

from __future__ import annotations

from typing import Annotated

import typer

from jobhunter.cli.common import not_implemented

app = typer.Typer(
    help="周报：投递/回复/面试/薄弱点汇总成 Markdown（P6）",
    # ★ 单命令应用用 callback（原因见 gap_cmd.py 的说明）
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def report(
    week: Annotated[bool, typer.Option("--week", help="生成本周（周一到今天）的报告")] = True,
    since: Annotated[
        str | None, typer.Option("--since", help="自定义起始日期，如 2026-09-01")
    ] = None,
    out: Annotated[str | None, typer.Option("--out", help="输出目录（默认 output/）")] = None,
) -> None:
    """生成求职周报（P6）。"""
    not_implemented(f"jh report（week={week}, since={since}, out={out}）", "P6")

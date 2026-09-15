"""`jh cal` —— 日历导出 .ics（P6 实现，本阶段为占位）。

P6 的最终形状：

    jh cal export [--out output/jobhunter.ics]
        把面试 + 笔试截止导出成 .ics（可导入手机日历/Google Calendar）

★ 为什么只导出 .ics，而**不做提醒系统**（克制决策，评审肯定）：
    做"常驻进程 + 系统通知"意味着要处理开机自启、跨平台通知权限、
    后台常驻资源占用……而这台工具的使用频率是"每天跑几条命令"。
    导一份 .ics 让我自己用手机日历提醒，成本几乎为零、可靠性反而更高。

★ 保持本项目的定位：**跑命令时提醒**，不做常驻进程。
   顶部"待处理提醒"就是这个定位的体现。
"""

from __future__ import annotations

from typing import Annotated

import typer

from jobhunter.cli.common import not_implemented

app = typer.Typer(help="日历：导出面试/笔试截止为 .ics（不写提醒系统，P6）")


@app.command("export")
def cal_export(
    out: Annotated[
        str | None, typer.Option("--out", help="输出路径（默认 output/jobhunter.ics）")
    ] = None,
    days: Annotated[int, typer.Option("--days", help="导出未来 N 天的事件")] = 60,
) -> None:
    """导出 .ics 日历文件（P6）。"""
    not_implemented(f"jh cal export（out={out}, days={days}）", "P6")

"""`jh cal` —— 日历导出 .ics（P6）。

    jh cal export [--out output/jobhunter.ics] [--days 60]
        把面试 + 笔试/测评截止 + Offer 签约截止导出成 .ics
        （可导入手机日历 / Google Calendar / Outlook）

★ 为什么只导出 .ics，而**不做提醒系统**（克制决策，评审肯定）：
    做"常驻进程 + 系统通知"意味着要处理开机自启、跨平台通知权限、
    后台常驻资源占用……而这台工具的使用频率是"每天跑几条命令"。
    导一份 .ics 让我自己用手机日历提醒，成本几乎为零、可靠性反而更高。

★ 保持本项目的定位：**跑命令时提醒**，不做常驻进程。
    顶部"待处理提醒"就是这个定位的体现；.ics 是它的"离线延伸" ——
    两条路读的是同一批数据（时间窗口与 join 口径都照抄 `reminder_svc`），
    所以顶部提醒说"9-11 笔试截止"，日历里必然也有同一条。

★ 空结果**照样导出**：没有事件时也生成一个合法的空日历并覆盖同名文件。
  如果空的时候跳过写入，用户上一次导出的旧文件会留在手机日历里被当成"最新安排"，
  这比导出一个空日历危险得多（"沉默的过期数据"）。
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.markup import escape

from jobhunter.cli.common import console, err_console, require_db, success
from jobhunter.services import cal_svc
from jobhunter.utils.time import to_local

app = typer.Typer(help="日历：导出面试/笔试截止/Offer 签约为 .ics（不写提醒系统，P6）")


@app.command("export")
def cal_export(
    out: Annotated[
        str | None, typer.Option("--out", help="输出路径（默认 output/jobhunter.ics）")
    ] = None,
    days: Annotated[int, typer.Option("--days", help="导出未来 N 天的事件")] = cal_svc.DEFAULT_DAYS,
) -> None:
    """导出 .ics 日历文件（P6）。

    ★ 同名文件直接覆盖（快照语义），理由见 `cal_svc.export_ics` 的 docstring：
      日历是幂等快照，生成 jobhunter-2.ics 会让每次导入都多一整套重复事件。
    """
    if days < 0:
        # 用法错误用退出码 2（与 Typer 自己的参数错误一致），且不碰数据库
        err_console.print("[red]--days 不能是负数[/red]（0 表示只看此刻）")
        raise typer.Exit(code=2)

    engine, factory = require_db()
    try:
        with factory() as session:
            export = cal_svc.export_ics(session, out=out, days=days)
    except OSError as exc:
        # 路径不可写（目录没权限 / --out 指到了目录上）→ 业务失败，退出码 1。
        # 这类报错的原始文案很难懂，所以补一句"检查 --out"。
        err_console.print(f"[red]写入失败：{exc}[/red]")
        err_console.print("  检查 [green]--out[/green] 指向的目录是否可写。")
        raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()

    success(f"已导出 {export.event_count} 个事件：{export.path}")
    if not export.events:
        console.print(
            f"[yellow]未来 {days} 天没有面试/笔试/Offer 截止[/yellow]"
            "（已写入一个空日历，不会清掉你的旧数据）"
        )
        return

    # 清单格式与 reminder_svc.format_reminders 一致（`9-11 14:00 公司`）：
    # 顶部提醒和这里的同一件事必须长得一样，否则要读两遍才能确认是同一场面试。
    for event in export.events:
        local = to_local(event.start)
        # escape：标题来自数据库（公司名里可能有 `[急招]` 这类方括号），
        # 不转义会让 rich 当成标记去解析，轻则显示错乱、重则抛 MarkupError
        console.print(f"  [dim]{local.month}-{local.day} {local:%H:%M}[/dim] {escape(event.title)}")

    console.print()
    console.print(
        "  把这一个文件导入手机日历即可（[bold]同一路径重复导入不会产生重复事件[/bold]）。"
    )

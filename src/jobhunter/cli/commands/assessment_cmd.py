"""`jh assessment` —— 笔试 / 测评 / AI 面（P6 实现，本阶段为占位）。

★ 为什么必须有这个命令（v3 新增，产品视角评审的缺口 #1）：
    海投**必定**遇到在线笔试/测评。原来的状态机里没有这个环节，
    后果是岗位卡在 replied 之后**再也不会提醒我**，
    而笔试邀请通常只有 48-72 小时 —— 错过就等于自动淘汰。
    这类"沉默的失败"最该防住，因为用户根本不会发现是工具的问题。

P6 的最终形状：

    jh assessment add --job <id> --kind 笔试 --deadline "2026-09-11 23:59"
    jh assessment list [--upcoming]

★ `--deadline` 是这张表存在的核心理由：
  顶部提醒的 "⏳ 笔试截止 1 条（9-11 饿了么）" 就是从它算出来的。
"""

from __future__ import annotations

from typing import Annotated

import typer

from jobhunter.cli.common import not_implemented

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
    not_implemented(
        f"jh assessment add（job={job}, kind={kind}, deadline={deadline}, "
        f"invited={invited}, platform={platform}, status={status}, note={note}）",
        "P6",
    )


@app.command("list")
def assessment_list(
    upcoming: Annotated[bool, typer.Option("--upcoming", help="只看即将截止的")] = False,
) -> None:
    """列出笔试/测评（P6）。"""
    not_implemented(f"jh assessment list（upcoming={upcoming}）", "P6")

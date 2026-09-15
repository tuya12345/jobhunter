"""`jh followup` —— 跟进追问话术（P6 实现，本阶段为占位）。

P6 的最终形状：

    jh followup <job_id>
        → 生成"投了 N 天没动静"的追问话术（手动触发，不做自动发送）

★ 为什么是"手动触发"而不是自动提醒发送：
    这是合规红线的一部分 —— 本工具**不做任何自动发送/自动联系**。
    它只负责把话术写好（连同该公司/岗位的 JD 关键词），由我决定发不发。

★ 话术要引用具体信息（投递日期、岗位名、我匹配的亮点），
  否则"请问有消息吗"这种群发感十足的话术反而减分。

★ 顶部提醒里的"投后 3 天未跟进 / 7 天无动静 / 14 天建议放弃"是**入口**，
  这条命令是**执行动作** —— 两者配合才是完整闭环。
"""

from __future__ import annotations

from typing import Annotated

import typer

from jobhunter.cli.common import not_implemented

app = typer.Typer(
    help="跟进话术：生成'投了 N 天没动静'的追问（手动触发，P6）",
    # ★ 单命令应用用 callback（原因见 gap_cmd.py 的说明）
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def followup(
    job_id: Annotated[int, typer.Argument(help="岗位 ID")],
    days: Annotated[
        int | None,
        typer.Option("--days", help="投了多少天没动静（默认按 applied_at 自动算）"),
    ] = None,
    variant: Annotated[str | None, typer.Option("--variant", help="话术风格：简短 / 正式")] = None,
) -> None:
    """生成一条追问话术（P6）。"""
    not_implemented(f"jh followup {job_id}（days={days}, variant={variant}）", "P6")

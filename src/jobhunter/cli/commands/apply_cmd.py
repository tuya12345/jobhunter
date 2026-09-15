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

from jobhunter.cli.common import not_implemented

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
    """零手打跟进入口（P4 单条 / P4.5 批量与模糊匹配）。"""
    if timeline is not None:
        not_implemented(f"jh apply --timeline {timeline}（投递时间轴）", "P6")

    if pending:
        not_implemented("jh apply --pending（待处理投递列表）", "P4.5")

    if not tokens and status is None:
        # 什么都不给 → 显示帮助（比直接报错友好）
        typer.echo(typer.get_current_context().get_help())
        raise typer.Exit(code=0)

    preview = " ".join(tokens) if tokens else "(无)"
    not_implemented(
        f"jh apply（批量/模糊匹配：{preview}；--status={status}；--yes={yes}）",
        "P4.5",
    )

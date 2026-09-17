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

from jobhunter.cli.common import console, err_console, not_implemented, require_db, success, warn
from jobhunter.config.settings import get_settings
from jobhunter.models.constants import APPLICATION_STATUSES
from jobhunter.services import apply_svc, today_svc

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
    """零手打跟进入口（P4 单条 / P4.5 批量与模糊匹配）。"""
    if timeline is not None:
        not_implemented(f"jh apply --timeline {timeline}（投递时间轴）", "P6")

    if pending:
        not_implemented("jh apply --pending（待处理投递列表）", "P4.5")

    tokens, status = _pull_status_out_of_tokens(list(tokens or []), status)

    # ---- 批量语法 `3=applied`：P4.5 的活，这里明确说清楚而不是假装不认识 ----
    pairs = [token for token in tokens if "=" in token]
    if pairs:
        not_implemented(f"jh apply {' '.join(pairs)}（批量打标）", "P4.5")

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

    keyword = " ".join(tokens)
    not_implemented(f"jh apply {keyword!r} --status {status}（按关键词模糊匹配）", "P4.5")


def _pull_status_out_of_tokens(
    tokens: list[str], status: str | None
) -> tuple[list[str], str | None]:
    """把被可变位置参数**吞掉**的 `--status` 捞回来（Click 的解析行为，P4 手敲时踩到）。

    ★ 现象：
        `jh apply 1 --status applied` → `status` 竟然是 **None**；
        而 `jh apply --status applied 1` 正常。
      原因：`tokens` 是 `nargs=-1` 的可变位置参数，Click 一旦开始吃位置参数，
      **后面的选项也一起当位置参数吃掉** —— 于是文档里写的最自然语序直接失效，
      而且不报错（选项"消失了"），最难查。

    这里做**最小修补**：允许把 `--status X` / `-s X` / `--status=X` 写在任意位置。
    """
    if status is not None:
        return tokens, status
    cleaned: list[str] = []
    found: str | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("--status", "-s") and index + 1 < len(tokens):
            found = tokens[index + 1]
            index += 2
            continue
        if token.startswith("--status="):
            found = token.split("=", 1)[1]
            index += 1
            continue
        cleaned.append(token)
        index += 1
    return cleaned, found


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

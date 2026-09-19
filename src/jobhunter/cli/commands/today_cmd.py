"""`jh today` —— 今日待投清单（P4）。

    jh today [--limit 20] [--min-score 60]     出清单 + 生成开场白 + 写 pending 投递
    jh today --copy 3                          把第 3 条的开场白复制到剪贴板
    jh today --check                           今天已投 X / 上限 N
    jh today --export md|csv                   导出到 output/（**只写新文件**）

★ 序号在 `jh today` / `jh today --copy N` / `jh apply N --status …` 三处**必须一致**，
  否则会出现"我以为标的是第 3 条，实际改的是别的岗位"。三者共用
  `today_svc.select_candidates()`，不各写一套筛选。

★ 生成开场白会调 LLM（有 key 时）。没有 key / 调用失败 → **降级到确定性模板**，
  并把原因显示在清单下面 —— 清单本身永远出得来，这是"能不能用"的底线。
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.panel import Panel
from rich.table import Table
from sqlalchemy.orm import Session

from jobhunter.cli.common import console, err_console, require_db, success, warn
from jobhunter.config.settings import AppSettings, get_settings
from jobhunter.llm.factory import get_provider
from jobhunter.services import apply_svc, export_svc, today_svc
from jobhunter.utils.clipboard import copy_to_clipboard

app = typer.Typer(
    help="今日待投清单：打分排序 / 一键复制开场白 / 额度检查（P4）",
    # ★ 单命令应用必须用 callback + invoke_without_command（见 gap_cmd.py 的说明）：
    #   写成 @app.command() 的话，`jh today --limit 5` 会被当成
    #   "给 today 的**子命令**传 --limit" → 报 No such option。
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def today(
    limit: Annotated[int | None, typer.Option("--limit", "-n", help="最多显示几条")] = None,
    min_score: Annotated[
        int | None, typer.Option("--min-score", help="最低匹配分（默认取 config.threshold）")
    ] = None,
    copy: Annotated[
        int | None, typer.Option("--copy", help="把第 N 条的开场白复制到剪贴板")
    ] = None,
    check: Annotated[bool, typer.Option("--check", help="只看今天已投 X / 上限 N")] = False,
    export: Annotated[
        str | None, typer.Option("--export", help="导出格式：md / csv（写到 output/，只写新文件）")
    ] = None,
    variant: Annotated[
        str | None,
        typer.Option("--variant", help="强制开场白变体：with_project / without_project"),
    ] = None,
    short: Annotated[bool, typer.Option("--short", help="--copy 时复制精简版（≤60 字）")] = False,
    sent: Annotated[
        bool,
        typer.Option(
            "--sent",
            help="配合 --copy：声明这段话术**真的发出去了**（写 greeting_sent，闭环统计用）",
        ),
    ] = False,
    edited: Annotated[
        bool,
        typer.Option(
            "--edited",
            help="配合 --copy：声明**发之前我改过**（写 greeting_edited）",
        ),
    ] = False,
) -> None:
    """输出今日待投清单（P4）。"""
    settings = get_settings()
    if variant is not None and variant not in ("with_project", "without_project"):
        err_console.print("[red]--variant 只能是 with_project 或 without_project[/red]")
        raise typer.Exit(code=2)
    if export is not None and export not in ("md", "csv"):
        err_console.print("[red]--export 只支持 md 或 csv[/red]")
        raise typer.Exit(code=2)
    if (sent or edited) and copy is None:
        # ★ 不许把"生成"当成"发送"（ISSUE #37 的合规红线）：
        #   这两个闭环字段只能由**人的真实动作**置位，而 --sent/--edited
        #   描述的是"我刚刚把哪一条发出去了"，所以必须同时给 --copy N 指明对象。
        err_console.print(
            "[red]--sent / --edited 要配合 --copy N 使用[/red]"
            "（它们声明的是'第 N 条我真的发出去了/改过'）。\n"
            "  [dim]例：jh today --copy 2 --sent　｜　"
            "jh apply 2 --status applied --sent[/dim]"
        )
        raise typer.Exit(code=2)

    engine, factory = require_db()
    try:
        with factory() as session:
            # ---- --check：只报额度，不生成任何东西 ------------------------
            if check:
                applied = apply_svc.count_applied_today(session)
                _print_quota(applied, settings.daily_limit)
                return

            # ---- --copy N：取第 N 条并复制 ---------------------------------
            if copy is not None:
                _copy_nth(
                    session,
                    copy,
                    settings=settings,
                    min_score=min_score,
                    limit=limit,
                    short=short,
                    variant=variant,
                    sent=sent,
                    edited=edited,
                )
                return

            # ---- 默认：出清单 ---------------------------------------------
            provider = get_provider(settings)
            result = today_svc.build_today_list(
                session,
                min_score=min_score,
                limit=limit,
                settings=settings,
                provider=provider,
                variant=variant,
            )
            session.commit()

            if not result.items:
                _print_empty(session, settings=settings, min_score=min_score)
                return

            _print_items(result, settings=settings)
            if export:
                output_dir = settings.paths.output_dir
                path = (
                    export_svc.export_markdown(result.items, output_dir=output_dir)
                    if export == "md"
                    else export_svc.export_csv(result.items, output_dir=output_dir)
                )
                success(f"已导出：{path}")
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _print_quota(applied: int, daily_limit: int) -> None:
    remaining = max(0, daily_limit - applied)
    console.print(
        Panel(
            f"今天已投：[bold]{applied}[/bold] / 上限 [bold]{daily_limit}[/bold]"
            f"　还剩 [bold]{remaining}[/bold] 条",
            title="📅 今日额度",
            border_style="cyan",
        )
    )
    if remaining == 0:
        warn("今天的额度用完了 —— 海投的转化率反而更低，明天再继续。")
    else:
        console.print(
            "  [dim]额度从 applications.applied_at 实时聚合，不是计数器（不会对不上）。[/dim]"
        )


def _print_empty(session: Session, *, settings: AppSettings, min_score: int | None) -> None:
    """空清单要区分原因：没岗位 / 没打分 / 分数不够 / 都被投完了。"""
    from jobhunter.services import job_svc

    jobs = job_svc.list_jobs(session)
    if not jobs:
        console.print("[yellow]库里还没有岗位。[/yellow]先录一个：")
        console.print("  [green]jh job add <岗位链接>[/green]")
        return
    console.print(
        Panel(
            "没有达到分数线的岗位。可能的原因：\n\n"
            "  [bold]1[/bold] 还没跑过匹配打分 → [green]jh match run[/green]\n"
            "  [bold]2[/bold] 分数线太高 → [green]jh today --min-score 50[/green]\n"
            "  [bold]3[/bold] 够分的都投过了 → [green]jh match run[/green] 之后再录新岗位\n\n"
            "[dim]`jh match explain <id>` 可以看某个岗位为什么分低。[/dim]",
            title="清单是空的",
            border_style="yellow",
        )
    )


def _print_items(result: today_svc.TodayList, *, settings: AppSettings) -> None:
    """打印清单表 + 开场白 + 降级说明。"""
    console.print()
    table = Table(title=f"今日待投（{len(result.items)} 条）", header_style="bold cyan")
    table.add_column("#", justify="right")
    table.add_column("分", justify="right")
    table.add_column("公司", overflow="fold")
    table.add_column("职位", overflow="fold")
    table.add_column("城市")
    table.add_column("用哪份简历", overflow="fold")
    table.add_column("变体")
    table.add_column("链接", overflow="fold")
    for item in result.items:
        table.add_row(
            str(item.index),
            str(item.score),
            item.company,
            item.title,
            item.job.city or "—",
            item.resume_name or "—",
            item.greeting_variant or "—",
            item.url or "—",
        )
    console.print(table)

    console.print()
    for item in result.items:
        if not item.greeting_full:
            continue
        tag = "" if item.greeting_source == "llm" else " [yellow]（模板兜底）[/yellow]"
        console.print(
            f"[bold]{item.index}. {item.company} · {item.title}[/bold]{tag}"
            f"　[dim]{len(item.greeting_full)} 字[/dim]"
        )
        console.print(f"   {item.greeting_full}")
        if item.greeting_short:
            console.print(
                f"   [dim]精简版（{len(item.greeting_short)} 字）：{item.greeting_short}[/dim]"
            )
        console.print()

    # ★ 降级原因必须显示：静默降级会让人以为"模型就这水平"，其实是被闸门拦了
    degraded = [item for item in result.items if item.greeting_issues]
    if degraded:
        console.print("[yellow]⚠️ 有开场白降级到模板（模型输出没过闸门）：[/yellow]")
        for item in degraded[:5]:
            console.print(f"  [dim]· #{item.index} {'；'.join(item.greeting_issues[:2])}[/dim]")

    console.print(
        f"  [dim]过滤前达线 {result.candidates} 条"
        + (
            f"，同公司去重挤掉 {result.dropped_same_company} 条"
            if result.dropped_same_company
            else ""
        )
        + f"；今天已投 {result.applied_today}/{result.daily_limit}[/dim]"
    )
    console.print()
    console.print(
        "下一步：[green]jh today --copy 1[/green] 复制第 1 条开场白"
        "　｜　投完 [green]jh apply 1 --status applied[/green]"
        "　｜　[green]jh today --export md[/green] 导出"
    )


def _copy_nth(
    session: Session,
    index: int,
    *,
    settings: AppSettings,
    min_score: int | None,
    limit: int | None,
    short: bool,
    variant: str | None,
    sent: bool = False,
    edited: bool = False,
) -> None:
    """复制第 N 条开场白（序号与 `jh today` 打出来的完全一致）。

    ★ 只给这一条生成开场白（不是整张清单）：有 key 时每次调用都要花钱。
    ★ 序号优先按**今天的快照**解析 —— 投完第 1 条之后，"第 3 条"仍然是你
      刚才看到的第 3 条（见 `today_svc.resolve_index` 的说明）。
    ★★ ISSUE #34：`--min-score` / `--limit` **必须真的生效**。
      原实现收了 `min_score` 形参却从不使用 ——
      `jh today --copy 2 --min-score 999` 会照常复制第 2 条，
      而用户以为自己限定在了"999 分以上"的范围里（拿到的是别的岗位的话术）。
      静默忽略选项比报错更糟。筛选与序号的判定统一在
      `today_svc.copy_target()`（它同时保证"序号含义"与"筛选选项"都成立）。
    ★★ ISSUE #41：**重新生成出来的文本要回写库** —— 缓存失效时这里会调模型拿到
      一段新文本，库里若还是旧值，用户下次看到的就是另一段话术（以为工具记错了）。
      回写走 `apply_svc.replace_greeting()`，它**不碰** `greeting_sent`
      （"生成"不等于"已发出"，见 #37）。
    """
    job, reason = today_svc.copy_target(
        session, index, min_score=min_score, limit=limit, settings=settings
    )
    if job is None:
        err_console.print(f"[red]{reason}[/red]")
        console.print("  [dim]先看清单：[green]jh today[/green][/dim]")
        raise typer.Exit(code=1)

    provider = get_provider(settings)
    item = today_svc.ensure_item(
        session, job, index, settings=settings, provider=provider, variant=variant
    )
    session.commit()

    text = item.greeting_short if short else item.greeting_full
    if not text:
        err_console.print(f"[red]第 {index} 条还没有开场白[/red]（{item.company} · {item.title}）")
        raise typer.Exit(code=1)

    # ★★ ISSUE #41：先回写库，再复制。顺序有意为之 —— 屏幕上要显示什么、
    #    库里就该是什么；剪贴板失败也不影响"库内 = 本次生成"这个事实。
    _write_back_greeting(session, job.id, item)

    if copy_to_clipboard(text):
        success(
            f"已复制第 {index} 条（{item.company} · {item.title}）"
            f"的{'精简版' if short else '完整版'}开场白（{len(text)} 字）"
        )
        console.print("  [dim]直接去招聘 App 里粘贴即可。[/dim]")
        # ★ ISSUE #37：闭环字段**只在人声明时**置位（`--sent` / `--edited`）。
        #   复制不等于发送，所以这里绝不自动写 greeting_sent ——
        #   "生成话术"与"话术被用上"是两件事，混在一起这个功能就没有意义了
        #   （P6 的 verify 里有一条 "生成 ≠ 发送" 的红线守着同样的道理）。
        if sent or edited:
            _mark_greeting_closure(session, job.id, sent=sent, edited=edited)
            session.commit()
    else:
        # 剪贴板不可用也不能让用户白跑一趟 —— 直接把文本打出来让他手动选
        warn("复制到剪贴板失败（这个系统上可能没有 clip/pbcopy/xclip），下面是原文：")
        console.print(text)


def _write_back_greeting(session: Session, job_id: int, item: today_svc.TodayItem) -> None:
    """把 `--copy N` 这次生成的话术回写库内，让"屏幕 == 库内"（ISSUE #41）。

    ★ 为什么只在 `--copy N` 这条路径上做（而不是塞进 `today_svc.ensure_item`）：

      清单路径（`jh today`）的既有语义是"**绝不覆盖已有开场白**"——
      那是为了保护用户手改过的内容，`tests/verify` 里有一条用例专门守着它。
      `--copy N` 不一样：用户此刻明确是在"要一段话术去发"，
      屏幕上已经是这段新文本了，库里留旧值只会造成两边不一致
      （用户裁决 #41-A：库内始终最新）。

    ★ 文本没变（缓存命中）时什么都不打印、也不写库 —— 那不是"发生了变更"，
      按本项目"不说没发生的事"的纪律，静默即可。

    ★ 取舍（明知而选）：如果这次生成**降级成了模板**（模型抽风/闸门拦下），
      回写就意味着库里的旧文本被模板替换掉了。这仍然是对的 ——
      用户此刻拿到的、看到的就是这段模板（`item.greeting_issues` 也会在清单里显示），
      "库内 = 屏幕上那一段"是 #41 裁决要的一致性；让库里留一段**不会再显示**的旧文本
      才是真正会误导人的状态。
    """
    if not item.greeting_full:
        return
    result = apply_svc.replace_greeting(
        session,
        job_id,
        greeting=item.greeting_full,
        variant=item.greeting_variant or None,
    )
    if result is None or not result.changed:
        return
    session.commit()
    console.print(
        f"  [dim]库内话术已更新为本次生成的这一版（applications.greeting_text，"
        f"变体 {item.greeting_variant or '未记'}）。[/dim]"
    )
    if result.was_sent:
        # ★ 库内文本换了、但"我确实发过话术"这个事实不变（#37：生成 ≠ 已发出）。
        #   必须说出来：否则用户会以为重新生成把"已发出"这个记录也一起改掉了。
        warn(
            "这条投递之前已标记为「已发出」—— 库内文本换成了这一版，"
            "但 greeting_sent 保持不变（生成 ≠ 已发出）。"
        )


def _mark_greeting_closure(
    session: Session, job_id: int, *, sent: bool = False, edited: bool = False
) -> None:
    """把"这句话术真的用上了 / 发之前我改过"写进闭环字段（ISSUE #37）。

    ★ 只会在这里被调用（`--copy N --sent/--edited`）—— 也就是**人**明确说了
      "我发出去了"。生成开场白、复制到剪贴板都**不**自动置位：
      工具不替用户声称一件没发生的事（合规红线，与 P6 "生成 ≠ 发送" 同一条）。
    """
    application = apply_svc.mark_greeting(session, job_id, sent=sent or None, edited=edited or None)
    if application is None:
        # 还没生成过开场白的投递 → 没有可标记的对象，如实说一句而不是静默
        warn("这条投递还没有开场白记录，闭环字段没写（先跑 `jh today` 生成）。")
        return
    parts: list[str] = []
    if sent:
        parts.append("已发出（greeting_sent）")
    if edited:
        parts.append("发前改过（greeting_edited）")
    if parts:
        console.print(f"  [dim]已记下：{'、'.join(parts)} —— 统计「话术有没有被用上」靠它。[/dim]")

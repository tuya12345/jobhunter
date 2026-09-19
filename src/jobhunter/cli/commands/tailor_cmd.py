"""`jh tailor` —— AI 定制简历（P5）。

    jh tailor brief <job_id>            重点讲哪 3 个项目、突出哪 2 个技能（不花钱）
    jh tailor analyze <job_id>          生成字段级建议（调模型，逐条过闸门）
    jh tailor list [--job N]            看历史版本
    jh tailor diff <version_id>         看建议的前后对比（含匹配分析）
    jh tailor approve <version_id> …    逐条批准（**再校验一遍**）
    jh tailor export <job_id>           导出 docx（**只写新文件**，原文件 hash 不变）

★★ FidelityGuard（P5 的核心，用**代码**保证不编造，而不是靠提示词祈祷）：

    FidelityGuard.check(original, generated) -> FidelityReport

    五个确定性检查项：命名实体白名单 / 数字守恒（含单位）/ 时间守恒 /
    技能词表校验 / 长度上限（默认 150%）。
    在【生成】【approve】【export】**三处强制调用**，违规即拒绝写入。

★ 输出的三层纪律（这个功能最容易做歪的地方）：

    ① **不静默丢东西**：被闸门拦下的建议一定打印出来 + 说明原因。
       用户看不到"模型本来想写什么"，就无从判断闸门是不是误报了。
    ② **不假装成功**：整批被拦时**不写库**，退出码非 0。
    ③ **不覆盖原文件**：导出永远写新文件，并打印原文件 hash 未变的校验结果。

★ 测试策略（很重要）：恶意 fixture 的**确定性断言**在
  `tests/unit/test_tailor_svc.py`（含 Kubernetes / 300% / 10个模块 / 2024年）。
  ⚠️ 绝不写"调真实 LLM 再断言输出里没有 XX"——那测的是模型顺从性，
     结果不确定会导致 CI 红灯，最后一定会被删掉（评审明确指出的坑）。
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.panel import Panel
from rich.table import Table

from jobhunter.cli.common import console, err_console, require_db, success, warn
from jobhunter.llm.factory import get_provider
from jobhunter.models import Job, ResumeVersion
from jobhunter.services import tailor_svc
from jobhunter.utils.time import fmt_datetime_local, to_local

app = typer.Typer(help="定制简历：brief（讲什么）→ analyze → approve → export（P5）")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _load_job(session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        err_console.print(
            f"[red]找不到岗位 #{job_id}[/red]。用 [green]jh job list[/green] 看有哪些。"
        )
        raise typer.Exit(code=1)
    return job


def _pull_all_out_of_tokens(tokens: list[str], all_: bool) -> tuple[list[str], bool]:
    """把被可变位置参数吞掉的 `--all` 捞回来（与 `jh apply` 同一个 Click 坑）。

    ★ 现象：`jh tailor approve 3 --all` 里 `--all` 会被当成**编号**（然后报"编号不合法"），
      而写在前面 `jh tailor approve --all 3` 就正常。位置参数一多就会出现，
      且不报错、只是"选项消失了" —— 最难查的一类问题（P4.5 在 `jh apply` 上踩过）。
      这里做最小修补：`--all` / `-a` / `all` 写在任意位置都认。
    """
    cleaned: list[str] = []
    found = all_
    for token in tokens:
        if token in ("--all", "-a", "all"):
            found = True
            continue
        cleaned.append(token)
    return cleaned, found


def _parse_ids(tokens: list[str]) -> list[int]:
    """把位置参数解析成编号列表；出现非数字就报用法错误（退出码 2）。"""
    ids: list[int] = []
    for token in tokens:
        # 允许 `1,2` / `1，2`（中文逗号）—— 手敲时两种都会出现
        for piece in str(token).replace("，", ",").split(","):
            item = piece.strip()
            if not item:
                continue
            if not item.isdigit():
                err_console.print(
                    f"[red]{item!r} 不是编号[/red]。用法：jh tailor approve <version_id> 1 2 3"
                    "（或 --all）"
                )
                raise typer.Exit(code=2)
            ids.append(int(item))
    return ids


# ---------------------------------------------------------------------------
# brief：不花钱的"面试讲什么"
# ---------------------------------------------------------------------------
@app.command("brief")
def tailor_brief(
    job_id: Annotated[int, typer.Argument(help="岗位 ID")],
    resume: Annotated[int | None, typer.Option("--resume", "-r", help="用哪份简历")] = None,
) -> None:
    """只输出"重点讲哪 3 个项目、突出哪 2 个技能、要避开什么"（**不调模型**）。"""
    engine, factory = require_db()
    try:
        with factory() as session:
            job = _load_job(session, job_id)
            try:
                brief = tailor_svc.build_brief(session, job, resume_id=resume)
            except tailor_svc.TailorError as exc:
                err_console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1) from exc
            _print_brief(brief)
    finally:
        engine.dispose()


def _print_brief(brief: tailor_svc.Brief) -> None:
    console.print()
    console.print(
        Panel(
            f"[bold]{brief.company}[/bold] · [bold]{brief.title}[/bold]\n"
            f"匹配分：[bold]{brief.score}[/bold]（{brief.score_source}）"
            f"　｜　用哪份简历：{brief.resume_name}（方向：{brief.direction}）",
            title="📋 这个岗位怎么讲",
            border_style="cyan",
        )
    )

    if not brief.points:
        warn("这份简历里没有解析出项目经历 —— 先跑 jh resume add 重新导入，")
    else:
        console.print("[bold]① 重点讲这几个项目[/bold]（按与 JD 的贴合度排序）")
        for point in brief.points:
            head = f"  [bold]{point.index}. 《{point.name}》[/bold]"
            if point.role:
                head += f"　[dim]{point.role}[/dim]"
            console.print(head)
            if point.hits:
                console.print(f"     命中 JD 关键词：[green]{'、'.join(point.hits)}[/green]")
            if point.line:
                console.print(f"     [cyan]{point.line}[/cyan]")
            if point.detail:
                console.print(f"     可讲的细节：[dim]{point.detail}[/dim]")
        console.print()

    console.print("[bold]② 突出这几个技能[/bold]")
    if brief.highlight:
        console.print(
            f"   [green]{'、'.join(brief.highlight)}[/green]　[dim]{brief.highlight_note}[/dim]"
        )
    else:
        console.print(f"   [yellow]{brief.highlight_note}[/yellow]")
    console.print()

    console.print("[bold]③ 要避开什么[/bold]（被追问两句就露的东西）")
    for item in brief.avoid:
        console.print(f"   [yellow]·[/yellow] {item}")
    if not brief.avoid:
        console.print("   [dim]没有明显要避开的地方。[/dim]")

    for note in brief.notes:
        console.print(f"   [dim]· {note}[/dim]")

    # 讲法自检没过 → 必须说出来（brief 自己也不能教人编造）
    if brief.report.violations:
        console.print()
        warn("有几条讲法没过保真自检，已经去掉（不会教你编）：")
        for item in brief.report.violations[:5]:
            console.print(f"   [dim]· {item}[/dim]")

    console.print()
    console.print(
        "下一步：想让 AI 逐条改简历 → [green]jh tailor analyze "
        f"{brief.job_id}[/green]（会调模型，花几分钱）"
    )
    console.print(
        "　　　　改完简历记得重新导入 + 重新打分："
        "[green]jh resume add[/green] → [green]jh match run[/green]"
    )


# ---------------------------------------------------------------------------
# analyze：字段级建议
# ---------------------------------------------------------------------------
@app.command("analyze")
def tailor_analyze(
    job_id: Annotated[int, typer.Argument(help="岗位 ID")],
    resume: Annotated[int | None, typer.Option("--resume", "-r", help="用哪份简历")] = None,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="不用缓存，强制重新生成")] = False,
) -> None:
    """生成字段级修改建议（结构化，逐条审核）（P5）。"""
    engine, factory = require_db()
    try:
        with factory() as session:
            job = _load_job(session, job_id)
            provider = get_provider(require_settings())
            try:
                result = tailor_svc.analyze_and_save(
                    session,
                    job,
                    resume_id=resume,
                    provider=provider,
                    use_cache=not no_cache,
                )
            except tailor_svc.TailorError as exc:
                err_console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1) from exc
            _print_analysis(result, job=job)
            if result.refused:
                raise typer.Exit(code=1)
    finally:
        engine.dispose()


def require_settings():
    """取全局配置（单独包一层只为了让 analyze 的调用点读起来短一点）。"""
    from jobhunter.config.settings import get_settings

    return get_settings()


def _print_analysis(result: tailor_svc.TailorResult, *, job: Job) -> None:
    analysis = result.analysis.match_analysis
    console.print()
    console.print(
        Panel(
            f"模型给的匹配分：[bold]{analysis.score}[/bold]"
            f"　｜　简历：{result.resume_name}"
            f"　｜　提示词 v{result.prompt_version}"
            + ("　[dim]（命中缓存，没花钱）[/dim]" if result.from_cache else ""),
            title=f"🤖 AI 简历建议（{job.title or job.id}）",
            border_style="cyan",
        )
    )
    # ★ ISSUE #26：命中缓存时"本次花了 0"与"省下多少"要分开说
    #   （口径与 `jh cache stats` 同一个乘积，统一在 cache_stats_svc 里算）
    if result.saved_stats.summary:
        console.print(f"  [dim]{result.saved_stats.summary}[/dim]")
    if analysis.strengths:
        console.print("[bold]它认为的优势[/bold]（都是简历里有的）：")
        for item in analysis.strengths[:6]:
            console.print(f"   [green]·[/green] {item}")
    if analysis.gaps or analysis.missing_keywords:
        console.print("[bold]它认为的差距[/bold]（只能说「在学」，不能写进简历）：")
        for item in (analysis.gaps or [])[:6]:
            console.print(f"   [yellow]·[/yellow] {item}")
        if analysis.missing_keywords:
            console.print(
                f"   [yellow]·[/yellow] 缺的关键词：{'、'.join(analysis.missing_keywords[:10])}"
            )
    console.print()

    if result.kept:
        table = Table(
            title=f"改写建议（{len(result.kept)} 条，逐条审核）", header_style="bold cyan"
        )
        table.add_column("#", justify="right")
        table.add_column("小节")
        table.add_column("改动前", overflow="fold")
        table.add_column("改动后", overflow="fold")
        table.add_column("为什么", overflow="fold")
        for index, suggestion in enumerate(result.kept, start=1):
            table.add_row(
                str(index),
                suggestion.section or "—",
                suggestion.original,
                suggestion.revised,
                suggestion.reason,
            )
        console.print(table)
    else:
        warn("模型没有给出可用建议（也许这份简历已经很贴这个 JD 了）。")

    # ★ 被拦下的必须显示：否则用户看到的"干净结果"其实是闸门刷过的，
    #   而"刷掉了什么"恰恰是判断闸门有没有误报的唯一线索。
    if result.dropped:
        console.print()
        console.print(
            f"[yellow]⚠️ 有 {len(result.dropped)} 条建议被保真闸门拦下（**没有写进库**）：[/yellow]"
        )
        for item in result.dropped[:6]:
            console.print(
                f"   [dim]· [{item.suggestion.section or '—'}] {item.suggestion.revised[:60]}[/dim]"
            )
            for reason in item.reasons[:3]:
                console.print(f"       [red]{reason}[/red]")

    if result.refused:
        console.print()
        err_console.print(
            "[red]整批建议都被拦下了 → 没有写任何东西。[/red]\n"
            "  这通常说明模型在编（比如往简历里塞 JD 要求但你没有的技能）。\n"
            "  · 可以重跑一次（模型有随机性）：jh tailor analyze <job_id> --no-cache\n"
            "  · 或者先用 jh tailor brief 把「面试怎么讲」拿到手"
        )
        return

    if result.version_id:
        console.print()
        success(f"已存为定制版本 #{result.version_id}")
        console.print(
            f"下一步：[green]jh tailor diff {result.version_id}[/green] 看前后对比"
            f"　｜　[green]jh tailor approve {result.version_id} --all[/green] 全批"
        )
        console.print(
            f"　　　　[green]jh tailor export {result.job_id}[/green] 导出 docx（只写新文件）"
        )


# ---------------------------------------------------------------------------
# list：历史版本
# ---------------------------------------------------------------------------
@app.command("list")
def tailor_list(
    job: Annotated[int | None, typer.Option("--job", "-j", help="只看某个岗位")] = None,
) -> None:
    """列出定制版本（新→旧）。"""
    engine, factory = require_db()
    try:
        with factory() as session:
            rows = tailor_svc.list_versions(session, job_id=job)
            if not rows:
                console.print("[yellow]还没有定制版本。[/yellow]先跑：")
                console.print("  [green]jh tailor brief <job_id>[/green]（不花钱）")
                console.print("  [green]jh tailor analyze <job_id>[/green]（调模型）")
                return
            table = Table(title=f"定制简历版本（{len(rows)} 条）", header_style="bold cyan")
            table.add_column("版本", justify="right")
            table.add_column("岗位", justify="right")
            table.add_column("简历", justify="right")
            table.add_column("时间")
            table.add_column("建议", justify="right")
            table.add_column("已批准", justify="right")
            table.add_column("导出文件", overflow="fold")
            for row in rows:
                table.add_row(
                    str(row.id),
                    str(row.job_id or "—"),
                    str(row.resume_id),
                    fmt_datetime_local(to_local(row.created_at)),
                    str(row.total),
                    str(row.approved),
                    row.file_path or "—",
                )
            console.print(table)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# diff：前后对比
# ---------------------------------------------------------------------------
@app.command("diff")
def tailor_diff(
    version_id: Annotated[int, typer.Argument(help="定制版本 ID")],
) -> None:
    """查看某版建议的前后对比（含匹配分析与闸门报告）。"""
    engine, factory = require_db()
    try:
        with factory() as session:
            version = _load_version(session, version_id)
            items = tailor_svc.suggestions_of(version)
            approved = set(tailor_svc.approved_ids(version))
            analysis = tailor_svc.analysis_of(version)
            report = tailor_svc.fidelity_report_of(version)

            console.print()
            console.print(
                Panel(
                    f"版本 [bold]#{version.id}[/bold]　岗位 #{version.job_id}"
                    f"　简历 #{version.resume_id}\n"
                    f"模型匹配分：{analysis.score}　｜　建议 {len(items)} 条，"
                    f"已批准 {len(approved)} 条\n"
                    f"闸门：{'通过' if report.get('passed') else '有拦下的建议'}　"
                    f"（拦下 {len(report.get('dropped') or [])} 条）",
                    title="📄 定制版本",
                    border_style="cyan",
                )
            )
            if analysis.gaps or analysis.missing_keywords:
                console.print(
                    "[yellow]差距（只学，不写）：[/yellow]"
                    + "、".join((analysis.gaps or analysis.missing_keywords)[:6])
                )
            console.print()

            for item in items:
                number = int(item.get("id") or 0)
                mark = "[green]✓ 已批准[/green]" if number in approved else "[dim]待审[/dim]"
                console.print(f"[bold]#{number}[/bold] [{item.get('section') or '—'}]　{mark}")
                console.print(f"   [red]- 原：[/red]{item.get('original')}")
                console.print(f"   [green]+ 改：[/green]{item.get('revised')}")
                console.print(f"   [dim]理由：{item.get('reason')}[/dim]")
                console.print()

            dropped = report.get("dropped") or []
            if dropped:
                console.print(f"[yellow]⚠️ 当时被拦下 {len(dropped)} 条（没写进库）：[/yellow]")
                for item in dropped[:5]:
                    console.print(
                        f"   [dim]· [{item.get('section') or '—'}]"
                        f" {str(item.get('original'))[:60]}[/dim]"
                    )
                    for reason in (item.get("reasons") or [])[:3]:
                        console.print(f"       [red]{reason}[/red]")
                console.print()

            if not approved:
                console.print(
                    f"下一步：[green]jh tailor approve {version.id} 1 2[/green] 批准若干条"
                    f"　｜　[green]jh tailor approve {version.id} --all[/green] 全批"
                )
            else:
                console.print(
                    f"下一步：[green]jh tailor export {version.job_id}[/green]"
                    " 导出 docx（只写新文件）"
                )
    finally:
        engine.dispose()


def _load_version(session, version_id: int) -> ResumeVersion:
    try:
        return tailor_svc.load_version(session, version_id)
    except tailor_svc.TailorError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


# ---------------------------------------------------------------------------
# approve：逐条批准
# ---------------------------------------------------------------------------
@app.command("approve")
def tailor_approve(
    version_id: Annotated[int, typer.Argument(help="定制版本 ID")],
    ids: Annotated[
        list[str] | None, typer.Argument(help="要批准的编号（可多个，如 1 2 3）")
    ] = None,
    all_: Annotated[bool, typer.Option("--all", "-a", help="批准全部建议")] = False,
) -> None:
    """逐条批准建议（approve 前会**再跑一次** FidelityGuard）。"""
    tokens = list(ids or [])
    tokens, all_ = _pull_all_out_of_tokens(tokens, all_)
    numbers = _parse_ids(tokens)

    engine, factory = require_db()
    try:
        with factory() as session:
            version = _load_version(session, version_id)
            try:
                merged = tailor_svc.approve(session, version, ids=numbers, all_=all_)
            except tailor_svc.TailorFidelityError as exc:
                err_console.print(f"[red]{exc}[/red]")
                for item in exc.violations[:8]:
                    err_console.print(f"   [red]·[/red] {item}")
                err_console.print(
                    "  这是**拒绝写入**：这些建议会往简历里写不存在的东西。\n"
                    "  处理办法：重跑 analyze 换一版建议，或直接弃用这一版。"
                )
                raise typer.Exit(code=1) from exc
            except tailor_svc.TailorError as exc:
                err_console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=2) from exc

            success(f"已批准 {len(merged)} 条：{merged}")
            console.print(
                f"下一步：[green]jh tailor export {version.job_id}[/green]"
                "（导出 docx，只写新文件，不会动原简历）"
            )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# export：导出新文件
# ---------------------------------------------------------------------------
@app.command("export")
def tailor_export(
    job_id: Annotated[int, typer.Argument(help="岗位 ID")],
    version: Annotated[
        int | None, typer.Option("--version", "-v", help="用哪一版（默认最新一版）")
    ] = None,
    out: Annotated[
        str | None, typer.Option("--out", help="输出路径（默认 output/，只写新文件）")
    ] = None,
    fmt: Annotated[str, typer.Option("--format", help="导出格式：docx（默认）/ md")] = "docx",
    show: Annotated[bool, typer.Option("--show", help="顺便打印导出后的全文")] = False,
) -> None:
    """导出定制简历（**只写新文件**，并校验原文件 hash 不变）（P5）。"""
    if fmt not in ("docx", "md"):
        err_console.print("[red]--format 只支持 docx 或 md[/red]")
        raise typer.Exit(code=2)

    engine, factory = require_db()
    try:
        with factory() as session:
            job = _load_job(session, job_id)
            target_version = _pick_version(session, job_id=job.id, version_id=version)
            try:
                result = tailor_svc.export_version(session, target_version, out=out, fmt=fmt)
            except tailor_svc.TailorFidelityError as exc:
                err_console.print(f"[red]{exc}[/red]")
                for item in exc.violations[:8]:
                    err_console.print(f"   [red]·[/red] {item}")
                err_console.print("  没有写任何文件（导出是原子的：要么全写，要么不写）。")
                raise typer.Exit(code=1) from exc
            except tailor_svc.TailorError as exc:
                err_console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1) from exc

            console.print()
            success(f"已导出：{result.path}")
            console.print(
                f"  　应用了 {len(result.applied)} 条已批准建议（版本 #{target_version.id}）"
            )
            if result.original_path:
                state = "未改动 ✓" if result.original_unchanged else "**被改动了** ✗"
                console.print(
                    f"  　原简历：{result.original_path}　{state}"
                    f"（sha256 {result.original_hash_before[:12]}…）"
                )
            else:
                warn("这份简历没记录文件路径，无法校验原文件 hash（不影响导出）")
            if result.unapplied:
                warn(
                    f"有 {len(result.unapplied)} 条已批准建议没能套用（原文在文档里找不到对应行）："
                )
                for item, reason in result.unapplied[:5]:
                    console.print(f"   [dim]· {str(item.get('original'))[:50]}　{reason}[/dim]")
            if result.report.notes:
                for note in result.report.notes[:5]:
                    console.print(f"   [dim]· {note}[/dim]")
            if show:
                console.print()
                console.print(result.text)
            console.print()
            console.print(
                f"　　[dim]原简历一个字都没动。要换一版："
                f"jh tailor approve {target_version.id} … 之后再 export。[/dim]"
            )
    finally:
        engine.dispose()


def _pick_version(session, *, job_id: int, version_id: int | None) -> ResumeVersion:
    """选要导出的版本：显式指定优先，否则取该岗位**最新且有已批准建议**的一版。"""
    if version_id is not None:
        version = _load_version(session, version_id)
        if version.job_id != job_id:
            err_console.print(
                f"[red]版本 #{version_id} 属于岗位 #{version.job_id}，不是 #{job_id}[/red]"
            )
            raise typer.Exit(code=2)
        return version

    rows = tailor_svc.list_versions(session, job_id=job_id)
    if not rows:
        err_console.print(
            f"[red]岗位 #{job_id} 还没有定制版本[/red]。"
            f"先跑 [green]jh tailor analyze {job_id}[/green]"
        )
        raise typer.Exit(code=1)
    for row in rows:  # list_versions 已经按 id 倒序 = 新→旧
        if row.approved:
            return tailor_svc.load_version(session, row.id)
    err_console.print(
        f"[red]岗位 #{job_id} 的 {len(rows)} 个版本里都没有已批准的建议[/red]。\n"
        f"  先看：jh tailor diff {rows[0].id}　再批：jh tailor approve {rows[0].id} --all"
    )
    raise typer.Exit(code=1)

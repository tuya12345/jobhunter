"""`jh job` —— 岗位录入（P2：**主路径是"人给链接"**）。

═══════════════════════════════════════════════════════════════════════════
四种录入口子，覆盖"网络好/网络差/批量/离线"四种真实情况
═══════════════════════════════════════════════════════════════════════════

    jh job add <url>              主路径：粘贴岗位链接 → 抓详情 → 入库
    jh job add <url1> <url2> …    一次给多个链接（逐个抓、失败跳过并记录）
    jh job add --file jobs.txt    批量：一行一个 URL（支持 # 注释）
    jh job add --manual           手动粘贴：从标准输入读 JD 文本（网络不通也能用）
    jh job add --html page.html   从本地保存的网页解析（不想联网 / 抓取被拦）
    jh job add --text "…"         直接给一段文本（脚本化调用）

另外两个查询/操作命令：

    jh job list [--status] [--platform]   看录入结果
    jh job show <id>                      看某条岗位的完整字段与 JD 正文
    jh job skip <id>                      标为"不投"（人工决策，不会被后续抓取覆盖）

★ 为什么默认**不**把 JD 正文全打出来：一次导入 20 条会把终端刷爆，
  真正要看的信息（公司/职位/城市/薪资/抓到多少字）反而被淹没。
  要看正文用 `jh job show <id>`。

★ 合规提醒（也写在 README 里）：本命令只做"读取页面内容"，
  **不会**点击投递按钮、不会自动搜索翻页。抓取之间有随机延时；
  批量导入时失败即跳过，不会重试轰炸对方站点。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.panel import Panel
from rich.table import Table

from jobhunter.adapters.base import JobDetail
from jobhunter.adapters.parse_html import parse_detail_from_text
from jobhunter.cli.common import console, err_console, require_db, success, warn
from jobhunter.config.settings import get_settings
from jobhunter.services import job_svc
from jobhunter.utils.io import read_text

app = typer.Typer(name="job", help="岗位：给链接抓详情 / 批量 / 手动粘贴 / 列表（P2）")


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------
@app.command("add")
def job_add(
    urls: Annotated[
        list[str] | None,
        typer.Argument(help="岗位链接（可给多个）；主路径就是粘链接"),
    ] = None,
    file: Annotated[
        Path | None,
        typer.Option("--file", "-f", help="批量文件：一行一个 URL（# 开头是注释）"),
    ] = None,
    manual: Annotated[
        bool,
        typer.Option("--manual", help="手动粘贴模式：从标准输入读 JD 文本（Ctrl+Z 回车 结束）"),
    ] = False,
    text: Annotated[
        str | None,
        typer.Option("--text", help="直接给一段 JD 文本（脚本化调用用）"),
    ] = None,
    html_file: Annotated[
        Path | None,
        typer.Option("--html", help="从本地保存的网页解析（离线/抓取被拦时的兜底）"),
    ] = None,
    keyword: Annotated[
        str | None,
        typer.Option("--keyword", "-k", help="这个岗位来自哪个搜索词/渠道（用于复盘渠道效果）"),
    ] = None,
    platform: Annotated[
        str | None,
        typer.Option(
            "--platform",
            "-p",
            help="强制指定平台（默认按域名判断；手动粘贴默认 manual）",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="只解析并打印结果，**不写库**"),
    ] = False,
) -> None:
    """录入岗位：给链接 / 批量文件 / 手动粘贴 / 本地 HTML。"""
    settings = get_settings()

    details, failures = _collect_details(
        urls=urls,
        file=file,
        manual=manual,
        text=text,
        html_file=html_file,
        platform=platform,
        settings=settings,
    )

    if not details and not failures:
        err_console.print(
            Panel(
                "没有可录入的内容。四种用法：\n\n"
                "  [bold]jh job add <url>[/bold]              粘贴岗位链接（主路径）\n"
                "  [bold]jh job add --file jobs.txt[/bold]    批量（一行一个 URL）\n"
                "  [bold]jh job add --manual[/bold]           手动粘贴 JD 文本\n"
                "  [bold]jh job add --html page.html[/bold]   从保存的网页解析\n\n"
                "[dim]为什么主路径是「人给链接」：BOSS 有签名与字体反爬，"
                "自动搜索容易撞墙；而「刷到岗位 → 存下来」本来就是真实工作流。[/dim]",
                title="缺少录入目标",
                border_style="yellow",
            )
        )
        raise typer.Exit(code=1)

    # ---- 打印解析结果（dry-run 到此为止）--------------------------------
    _print_details(details, failures)

    if dry_run:
        console.print()
        warn("--dry-run：以上结果没有写入数据库")
        return

    engine, factory = require_db()
    try:
        with factory() as session:
            created = refreshed = unchanged = 0
            for detail in details:
                result = job_svc.upsert_job(session, detail, source_keyword=keyword)
                if result.outcome == "created":
                    created += 1
                elif result.outcome == "refreshed":
                    refreshed += 1
                else:
                    unchanged += 1
            session.commit()
            totals = job_svc.count_by_status(session)
    finally:
        engine.dispose()

    # ---- 汇总 ------------------------------------------------------------
    console.print()
    parts = []
    if created:
        parts.append(f"[green]新增 {created}[/green]")
    if refreshed:
        parts.append(f"[cyan]重复遇到 {refreshed}（已刷新抓取字段）[/cyan]")
    if unchanged:
        parts.append(f"[dim]已存在 {unchanged}（内容无变化）[/dim]")
    if failures:
        parts.append(f"[red]失败 {len(failures)}[/red]")
    success("入库完成：" + "　".join(parts))

    if totals:
        summary = "　".join(f"{status} {count}" for status, count in sorted(totals.items()))
        console.print(f"  [dim]库里现有：{summary}[/dim]")

    if failures:
        console.print()
        warn(f"有 {len(failures)} 条没抓到。可以改用 [bold]jh job add --manual[/bold] 手动粘贴：")
        for item in failures[:5]:
            console.print(f"  [dim]· {item}[/dim]")
        if len(failures) > 5:
            console.print(f"  [dim]· …还有 {len(failures) - 5} 条[/dim]")

    console.print()
    console.print(
        "下一步：[green]jh job list[/green] 看列表　｜　[green]jh job show <id>[/green] 看详情"
    )

    # ★ 一条都没入库 → 以非 0 退出。
    #   为什么较真：批量导入时如果"全部失败"却返回 0，脚本/CI 会以为成功了 ——
    #   这正是"静默失败"最典型的形态（用户第二天才发现清单是空的）。
    if created == 0 and refreshed == 0 and unchanged == 0:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------
@app.command("list")
def job_list(
    status: Annotated[
        str | None,
        typer.Option("--status", "-s", help="按状态过滤，如 new/skipped"),
    ] = None,
    platform: Annotated[str | None, typer.Option("--platform", "-p", help="按平台过滤")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="最多显示几条")] = 20,
) -> None:
    """列出已录入的岗位。"""
    engine, factory = require_db()
    try:
        with factory() as session:
            rows = job_svc.list_jobs(session, status=status, platform=platform, limit=limit)
            totals = job_svc.count_by_status(session)
    finally:
        engine.dispose()

    if not rows:
        console.print("[dim]（还没有录入任何岗位）[/dim]")
        console.print()
        console.print("录入一条试试：[green]jh job add <岗位链接>[/green]")
        return

    table = Table(title="岗位列表", header_style="bold cyan")
    table.add_column("id", justify="right")
    table.add_column("公司", overflow="fold")
    table.add_column("职位", overflow="fold")
    table.add_column("城市")
    table.add_column("薪资")
    table.add_column("实习", justify="center")
    table.add_column("状态")
    table.add_column("JD", justify="right")
    for job in rows:
        table.add_row(
            str(job.id),
            job.company or "—",
            job.title or "—",
            job.city or "—",
            job.salary_text or "—",
            {True: "★", False: "", None: "?"}[job.is_intern],
            job.status,
            f"{(job.jd_text or '').__len__()}字",
        )
    console.print(table)

    if totals:
        summary = "　".join(f"{status} {count}" for status, count in sorted(totals.items()))
        console.print(f"[dim]共 {sum(totals.values())} 条：{summary}[/dim]")


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------
@app.command("show")
def job_show(
    job_id: Annotated[int, typer.Argument(help="岗位 ID（见 jh job list）")],
    jd: Annotated[bool, typer.Option("--jd", help="打印完整 JD 正文")] = False,
) -> None:
    """看一条岗位的完整字段（`--jd` 才打印正文）。"""
    engine, factory = require_db()
    try:
        with factory() as session:
            job = job_svc.get_job(session, job_id)
            if job is None:
                err_console.print(
                    f"[red]找不到 id={job_id} 的岗位。用 `jh job list` 看有哪些。[/red]"
                )
                raise typer.Exit(code=1)
            fields = {
                "平台": job.platform,
                "平台岗位 ID": job.platform_job_id or "—",
                "公司": job.company or "—",
                "职位": job.title or "—",
                "城市": job.city or "—",
                "薪资": job.salary_text or "—",
                "薪资数值": f"{job.salary_min or '—'} ~ {job.salary_max or '—'}",
                "经验要求": job.experience or "—",
                "学历要求": job.education or "—",
                "是否实习": {True: "是", False: "否", None: "未识别"}[job.is_intern],
                "状态": job.status,
                "抓取情况": job.jd_fetch_status or "—",
                "来源搜索词": job.source_keyword or "—",
                "JD 字数": str(len(job.jd_text or "")),
                "URL": job.url or "—",
                "内容 hash": (job.jd_hash or "—")[:16],
            }
            text = job.jd_text or ""
    finally:
        engine.dispose()

    console.print()
    for key, value in fields.items():
        console.print(f"  [dim]{key}[/dim]：{value}")

    if jd:
        console.print()
        console.rule("JD 正文", align="left")
        console.print(text or "[dim]（正文为空）[/dim]")
    else:
        console.print()
        console.print("[dim]加 --jd 可打印完整正文[/dim]")


# ---------------------------------------------------------------------------
# skip
# ---------------------------------------------------------------------------
@app.command("skip")
def job_skip(
    job_id: Annotated[int, typer.Argument(help="岗位 ID")],
    reason: Annotated[str | None, typer.Option("--reason", help="为什么跳过（只记日志）")] = None,
) -> None:
    """标为"不投"（人工决策；后续抓到这个岗位也不会把它改回 new）。"""
    engine, factory = require_db()
    try:
        with factory() as session:
            job = job_svc.mark_skipped(session, job_id, reason=reason)
            if job is None:
                err_console.print(f"[red]找不到 id={job_id} 的岗位。[/red]")
                raise typer.Exit(code=1)
            session.commit()
            label = f"{job.company or '未知公司'} / {job.title or '未知岗位'}"
    finally:
        engine.dispose()

    success(f"已跳过：id={job_id}　{label}")
    console.print("[dim]这条不会再出现在待投清单里；后续抓取也不会覆盖这个决定。[/dim]")


# ---------------------------------------------------------------------------
# 内部：收集待录入的详情
# ---------------------------------------------------------------------------
def _collect_details(
    *,
    urls: list[str] | None,
    file: Path | None,
    manual: bool,
    text: str | None,
    html_file: Path | None,
    platform: str | None,
    settings: object,
) -> tuple[list[JobDetail], list[str]]:
    """把各种输入统一收集成 `(成功解析的详情列表, 失败说明列表)`。

    ★ 分批处理的顺序即"优先级"：显式给的链接 → 批量文件 → 本地 HTML → 手动文本。
      用户的真实用法通常只走其中一条，但允许混用（比如"给两个链接 + 粘一段文本"）。
    """
    from jobhunter.adapters.manual import ManualAdapter, read_html_file

    details: list[JobDetail] = []
    failures: list[str] = []

    targets: list[str] = [url for url in (urls or []) if url and url.strip()]
    if file is not None:
        targets.extend(_read_url_file(file))

    if targets:
        adapter = ManualAdapter(settings)  # type: ignore[arg-type]
        for url in targets:
            console.print(f"  [dim]抓取中… {url}[/dim]")
            detail = adapter.fetch_detail(url)
            if detail is None:
                failures.append(f"{url}（打开失败/超时）")
                continue
            if detail.fetch_status == "failed":
                failures.append(f"{url}（页面里没找到 JD 正文，可改用 --manual 粘贴）")
                continue
            details.append(detail)

    if html_file is not None:
        if not html_file.is_file():
            failures.append(f"{html_file}（文件不存在）")
        else:
            try:
                details.append(read_html_file(html_file))
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{html_file}（解析失败：{exc}）")

    content = text
    if manual and content is None:
        content = _read_stdin()

    if content is not None:
        if not content.strip():
            failures.append("手动粘贴的内容是空的")
        else:
            details.append(parse_detail_from_text(content, platform=platform or "manual"))

    return details, failures


def _read_url_file(path: Path) -> list[str]:
    """读批量 URL 文件（一行一个；`#` 开头是注释；忽略空行）。"""
    if not path.is_file():
        err_console.print(f"[red]找不到文件：{path}[/red]")
        raise typer.Exit(code=1)
    urls: list[str] = []
    for line in read_text(path).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        urls.append(stripped)
    return urls


def _read_stdin() -> str:
    """从标准输入读多行文本（手动粘贴模式）。

    ★ 为什么要显式提示"怎么结束输入"：Windows 上是 Ctrl+Z 再回车、
      macOS/Linux 是 Ctrl+D —— 不说清楚的话，用户会卡在"光标不动"的状态，
      以为程序死了。这是纯粹的体验问题，但很影响"愿不愿意用它"。

    ★★ 为什么要用 `utf-8-sig` 解码，而不是 `sys.stdin.read()`（实测踩到的坑）：

        Windows 上 `Get-Content jd.txt | jh job add --manual` 会把 **UTF-8 BOM**
        一起送进管道，于是首行变成 `\\ufeff公司：某某科技` —— 字段标签匹配失败，
        **公司名整段解析不出来**。而其他字段（职位/薪资/城市）都正常，
        所以极难联想到是 BOM 的问题。

        用 `utf-8-sig` 解码会自动吃掉开头的 BOM；没有 BOM 时它与 utf-8 完全等价，
        因此对 Linux/macOS 以及 `--text` 路径都没有影响。
    """
    if sys.stdin is None:
        return ""

    if sys.stdin.isatty():
        console.print(
            Panel(
                "请粘贴 JD 文本，粘贴完按 [bold]Ctrl+Z[/bold] 再回车（Windows）"
                "或 [bold]Ctrl+D[/bold]（macOS/Linux）结束输入。\n\n"
                '[dim]也可以直接用：jh job add --text "…"[/dim]',
                title="手动粘贴模式",
                border_style="cyan",
            )
        )

    # 优先走二进制流自己解码（只有这样才能用 utf-8-sig 处理 BOM）
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        try:
            return buffer.read().decode("utf-8-sig", errors="replace")
        except (OSError, ValueError):  # pragma: no cover - 流不可读时退回文本读
            pass
    return sys.stdin.read().lstrip("\ufeff")


def _print_details(details: list[JobDetail], failures: list[str]) -> None:
    """打印解析结果概览（**不含 JD 正文**，避免刷屏）。"""
    if details:
        table = Table(title=f"解析到 {len(details)} 条岗位", header_style="bold cyan")
        table.add_column("#", justify="right")
        table.add_column("公司", overflow="fold")
        table.add_column("职位", overflow="fold")
        table.add_column("城市")
        table.add_column("薪资")
        table.add_column("实习", justify="center")
        table.add_column("JD", justify="right")
        table.add_column("抓取")
        for index, detail in enumerate(details, start=1):
            table.add_row(
                str(index),
                detail.company or "—",
                detail.title or "—",
                detail.city or "—",
                detail.salary_text or "—",
                {True: "★", False: "", None: "?"}[detail.is_intern],
                f"{len(detail.jd_text or '')}字",
                detail.fetch_status,
            )
        console.print(table)

        missing: list[str] = []
        for detail in details:
            for field in detail.missing_fields():
                if field not in missing:
                    missing.append(field)
        if missing:
            console.print(f"[dim]没抓到的字段（留空而不是猜）：{'、'.join(missing)}[/dim]")

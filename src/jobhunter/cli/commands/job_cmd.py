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

★ `--file` 的三道闸门（都在**读取阶段**，见 `_read_url_file`）：
  只收 http/https、按归一化 URL 去重、一次最多 20 条 —— 跳过的每一类都会打印出来。
  理由：抓取之间要等 2~5 秒随机延时，"重复行/几千行清单"会直接变成对外连发。

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

import ipaddress
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

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


#: `--file` 一次最多处理多少条 URL。
#:
#: ★ 为什么写成常量、而不是 config.yaml 里的一项：
#:   这个数字是**防手滑的护栏**，不是个人偏好 —— 它挡的是"一次喂几千行，
#:   工具以 2~5 秒/条的节奏连续对外请求几小时"。让用户去配置它，
#:   等于把这个护栏变成"看起来可以关掉的东西"（关掉它的那一刻，
#:   频率克制的承诺就没了）。真抓得更多时，拆成几个文件分批跑是更清楚的表达：
#:   每一批的结果、失败原因都单独看得到。
#:
#: ★ 为什么是 20：与 config.yaml 的 `daily_limit: 20` 同量级 ——
#:   一天要投的岗位本来就是这个数量级。给太多等于没有上限。
URL_FILE_MAX_ITEMS = 20


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
        typer.Option(
            "--file",
            "-f",
            help=(
                "批量文件：一行一个 URL（# 开头是注释）。"
                f"只收 http/https、按归一化链接去重、一次最多 {URL_FILE_MAX_ITEMS} 条"
            ),
        ),
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
    url: Annotated[
        str | None,
        typer.Option(
            "--url",
            help=(
                "这条记录的**原始岗位链接**（配合 --html / --manual / --text 用）。"
                "不传的话，本地网页只能记成 file:///…：列表里点不回去，"
                "第一层去重（platform_job_id）也用不上"
            ),
        ),
    ] = None,
    cdp: Annotated[
        bool,
        typer.Option(
            "--cdp",
            help=(
                "用**你自己的浏览器**抓（复用已登录的会话）—— 需要登录的站点用这个。"
                "首次会打开一个专用窗口，在里面登录一次即可"
            ),
        ),
    ] = False,
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
    _check_url_option(url, urls=urls, html_file=html_file, manual=manual, text=text)

    details, failures = _collect_details(
        urls=urls,
        file=file,
        manual=manual,
        text=text,
        html_file=html_file,
        url=url,
        platform=platform,
        cdp=cdp,
        settings=settings,
    )

    if not details and not failures:
        _print_missing_input_help()
        raise typer.Exit(code=1)

    # ---- 打印解析结果（dry-run 到此为止）--------------------------------
    _print_details(details, failures)

    # ★★ 失败原因必须在 `--dry-run` 的提前返回**之前**打印（ISSUES #62 同类毛病）
    #
    #   改前这段在函数末尾，而 dry-run 在中途就 return 了 —— 于是
    #   `jh job add --cdp <链接> --dry-run` 在"什么都没解析出来"时**一个字都不解释**：
    #   用户看到的是空表 + "--dry-run：以上结果没有写入数据库"。
    #   而 `--dry-run` 恰恰是"先看看会发生什么"的那条命令，最需要解释的就是它。
    if failures:
        _print_failures(failures)

    if dry_run:
        console.print()
        warn("--dry-run：以上结果没有写入数据库")
        return

    outcome = _upsert_everything(details, keyword=keyword)

    # ---- 汇总 ------------------------------------------------------------
    console.print()
    stored = outcome.created + outcome.refreshed + outcome.unchanged
    parts = []
    if outcome.created:
        parts.append(f"[green]新增 {outcome.created}[/green]")
    if outcome.refreshed:
        parts.append(f"[cyan]重复遇到 {outcome.refreshed}（已刷新抓取字段）[/cyan]")
    if outcome.unchanged:
        parts.append(f"[dim]已存在 {outcome.unchanged}（内容无变化）[/dim]")
    if failures:
        parts.append(f"[red]失败 {len(failures)}[/red]")

    # ★★ 一条都没入库时**不许打印 ✓**（ISSUES #62 的同类毛病）
    #
    #   改前这里无条件 `success("入库完成：…")`：全部失败时印出来的是
    #   「✓ 入库完成：失败 1」—— 一个绿色的对勾配一句"完成"，用户扫一眼就走了。
    #   而"报成功、其实什么都没做成"正是 #62 被定为 🔴 的理由。
    #   现在的规矩：**有东西进库才有 ✓**；一条都没有就是明确的失败。
    if stored:
        success("入库完成：" + "　".join(parts))
    else:
        warn("没有入库任何岗位：" + "　".join(parts))
        console.print("  [dim]上面每一条的失败原因都写在上面了。[/dim]")

    if outcome.totals:
        summary = "　".join(f"{status} {count}" for status, count in sorted(outcome.totals.items()))
        console.print(f"  [dim]库里现有：{summary}[/dim]")

    console.print()
    console.print(
        "下一步：[green]jh job list[/green] 看列表　｜　[green]jh job show <id>[/green] 看详情"
    )

    # ★ 一条都没入库 → 以非 0 退出。
    #   为什么较真：批量导入时如果"全部失败"却返回 0，脚本/CI 会以为成功了 ——
    #   这正是"静默失败"最典型的形态（用户第二天才发现清单是空的）。
    if outcome.created == 0 and outcome.refreshed == 0 and outcome.unchanged == 0:
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
                "URL": _display_url(job.url),
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


def _display_url(url: str | None) -> str:
    """URL 的展示写法（ISSUES #61 的第 3 条）。

    ★ 为什么要专门处理 `file://`：用 `--html` 录入时，库里存的是
      `file:///D:/…/saved.html` —— 那是**本机文件路径**，不是岗位链接。
      直接印出来，用户会以为"这个岗位的地址就是这个"，而它点不回去、
      也解释不了"我明明是从 BOSS 存下来的"。
      所以标成「（本地保存的页面）+ 路径」，一眼能看出这是本地文件。

      ★ 更好的做法是录入时就用 `--url "<原链接>"` 把真链接记下来 ——
        这时这里显示的就是那个真链接（本函数只在它确实缺的时候兜底）。
    """
    if not url:
        return "—"
    if url.startswith("file:///"):
        return f"（本地保存的页面）{url.removeprefix('file:///')}"
    return url


def _print_failures(failures: list[str]) -> None:
    """把没抓到的原因逐条打出来（**dry-run 也要打**）。

    ★ 为什么单独抽出来：`--dry-run` 在"什么都没解析出来"时最需要解释，
      而它中途就 return 了 —— 所以这段必须在 return 之前调用
      （ISSUES #62 同类毛病：安静地什么都不说，用户只能猜）。
    """
    console.print()
    warn(f"有 {len(failures)} 条没抓到。可以改用 [bold]jh job add --manual[/bold] 手动粘贴：")
    for item in failures[:5]:
        console.print(f"  [dim]· {item}[/dim]")
    if len(failures) > 5:
        console.print(f"  [dim]· …还有 {len(failures) - 5} 条[/dim]")


def _prepare_cdp(settings: object) -> object:
    """准备 CDP 端点（`--cdp`）：没有就起一个**专用 profile** 的浏览器，并给用户提示。

    ★ 为什么在 CLI 这层做、而不是让适配器自己悄悄做：
      这个动作会**弹出浏览器窗口**，用户必须知道那是工具开的、并且要去里面登录一次。
      悄悄开一个窗口然后报"抓不到"，是最让人困惑的一种失败。

    ★ 起一个专用实例（而不是连用户日常那个）有两个原因（实测）：
      ① Edge/Chrome 已在运行时 `--remote-debugging-port` 完全无效 ——
         新进程只是把标签页交给现有实例，端口根本不会起来；
      ② 那个专用目录里会有登录态，放在 `state/cdp-profile`（gitignore 覆盖）最安全。
    """
    from jobhunter.adapters import cdp as cdp_module

    try:
        endpoint = cdp_module.ensure_cdp_browser(settings, on_message=console.print)  # type: ignore[arg-type]
    except RuntimeError as exc:
        err_console.print(f"[red]无法启用 --cdp[/red]：{exc}")
        raise typer.Exit(code=1) from exc
    if not endpoint.started:
        console.print(f"  [dim]复用已在运行的浏览器实例（CDP 端口 {endpoint.port}）[/dim]")
    return endpoint


# ---------------------------------------------------------------------------
# 内部：收集待录入的详情
# ---------------------------------------------------------------------------
@dataclass
class _UpsertOutcome:
    """一次 `jh job add` 写库后的计数（只给汇总那几行用）。"""

    created: int = 0
    refreshed: int = 0
    unchanged: int = 0
    totals: dict[str, int] = field(default_factory=dict)


def _upsert_everything(details: list[JobDetail], *, keyword: str | None) -> _UpsertOutcome:
    """把解析好的详情逐条入库，返回各类计数。

    ★ 抽出来的理由不只是"函数太长"：这段是**唯一写库的地方**，
      与上面"解析/打印"分开之后，"打印成功"与"真的写进去了"之间就没有别的分支了
      （ISSUE #62 的教训：报成功与实际入库必须一一对应）。
    """
    engine, factory = require_db()
    try:
        with factory() as session:
            outcome = _UpsertOutcome()
            for detail in details:
                result = job_svc.upsert_job(session, detail, source_keyword=keyword)
                if result.outcome == "created":
                    outcome.created += 1
                elif result.outcome == "refreshed":
                    outcome.refreshed += 1
                else:
                    outcome.unchanged += 1
            session.commit()
            outcome.totals = dict(job_svc.count_by_status(session))
    finally:
        engine.dispose()
    return outcome


def _print_missing_input_help() -> None:
    """什么都没给时，把四种用法摆出来（而不是甩一句「参数错误」）。"""
    err_console.print(
        Panel(
            "没有可录入的内容。四种用法：\n\n"
            "  [bold]jh job add <url>[/bold]              粘贴岗位链接（主路径）\n"
            "  [bold]jh job add --file jobs.txt[/bold]    批量（一行一个 URL）\n"
            "  [bold]jh job add --manual[/bold]           手动粘贴 JD 文本\n"
            "  [bold]jh job add --html page.html[/bold]   从保存的网页解析\n"
            '  [dim]（后两种可以配 --url "<原链接>" --platform boss，'
            "把原始链接与平台一起记下来）[/dim]\n\n"
            "[dim]为什么主路径是「人给链接」：BOSS 有签名与字体反爬，"
            "自动搜索容易撞墙；而「刷到岗位 → 存下来」本来就是真实工作流。[/dim]",
            title="缺少录入目标",
            border_style="yellow",
        )
    )


def _check_url_option(
    url: str | None,
    *,
    urls: list[str] | None,
    html_file: Path | None,
    manual: bool,
    text: str | None,
) -> None:
    """校验 `--url`（ISSUES #61）——**不许静默忽略一个显式给出的选项**。

    `--url` 的语义是"给这条记录补上原始岗位链接"，只在 `--html` / `--manual` /
    `--text` 下有意义。两种矛盾输入都当场报错，而不是挑一个听：

      · 与位置参数（岗位链接）同时给 → 链接已经由位置参数提供了，两个都听
        等于静默挑一个；
      · 单独给（没有任何输入方式）→ 没有任何东西可以挂这条链接。

    ★ 为什么走"报错"而不是"警告后忽略"：这正是 #34 / #50 / #61 反复出现的那一类
      ——**静默忽略比报错危险得多，因为用户不会知道自己少了什么**。
    """
    if not url:
        return
    if urls:
        err_console.print(
            "[red]--url 与位置参数（岗位链接）不能同时用[/red]：\n"
            "  · 抓链接：直接给位置参数 —— [bold]jh job add <url>[/bold]\n"
            "  · 补链接：配合 --html / --manual / --text —— "
            '[bold]jh job add --html 页面.html --url "<原链接>"[/bold]'
        )
        raise typer.Exit(code=1)
    if not (html_file or manual or text):
        err_console.print(
            "[red]--url 需要配合一种输入方式[/red]：\n"
            '  · jh job add --html 页面.html --url "<原链接>" --platform boss\n'
            '  · jh job add --manual --url "<原链接>" --platform boss'
        )
        raise typer.Exit(code=1)


def _collect_details(
    *,
    urls: list[str] | None,
    file: Path | None,
    manual: bool,
    text: str | None,
    html_file: Path | None,
    url: str | None,
    platform: str | None,
    cdp: bool = False,
    settings: object,
) -> tuple[list[JobDetail], list[str]]:
    """把各种输入统一收集成 `(成功解析的详情列表, 失败说明列表)`。

    ★ 分批处理的顺序即"优先级"：显式给的链接 → 批量文件 → 本地 HTML → 手动文本。
      用户的真实用法通常只走其中一条，但允许混用（比如"给两个链接 + 粘一段文本"）。

    ★★ `platform` 现在对**三条路都生效**（ISSUES #61）：
      改前它只在手动文本那条路上被传下去，链接抓取与本地 HTML 都静默忽略它 ——
      而 `--help` 写的是"强制指定平台"。本地 HTML 恰恰是最需要它的场景：
      保存下来的网页**没有域名上下文**，工具无从判断是 BOSS 还是别的站，
      用户显式说了却被丢掉。现在：**要么真的用它，要么明确报错**，不许两者都不做。
    """
    from jobhunter.adapters.manual import ManualAdapter, read_html_file

    details: list[JobDetail] = []
    failures: list[str] = []

    targets: list[str] = [url for url in (urls or []) if url and url.strip()]
    if file is not None:
        targets.extend(_read_url_file(file))

    if targets:
        # 平台覆盖经构造器传进去（见 ManualAdapter.__init__ 的说明）
        cdp_endpoint = _prepare_cdp(settings) if cdp else None
        adapter = ManualAdapter(  # type: ignore[arg-type]
            settings, platform=platform, use_cdp=cdp, cdp_endpoint=cdp_endpoint
        )
        for target in targets:
            console.print(f"  [dim]抓取中… {target}[/dim]")
            detail = adapter.fetch_detail(target)
            if detail is None:
                failures.append(f"{target}（打开失败/超时）")
                continue
            if detail.fetch_status == "failed":
                # ★ 失败原因不同、用户该做的事也不同：适配器给了 `fetch_note`
                #   就直接用它（登录墙会带上"两条不用绕过登录的路"）；
                #   没有 note 的（页面改版/超时）才退回那句通用提示。
                reason = detail.fetch_note or "页面里没找到 JD 正文，可改用 --manual 粘贴"
                failures.append(f"{target}\n    {reason}")
                continue
            details.append(detail)

    if html_file is not None:
        if not html_file.is_file():
            failures.append(f"{html_file}（文件不存在）")
        else:
            try:
                # ★ 把 url 与 platform 一起传下去（#61 的两个缺口就在这里）
                parsed = read_html_file(html_file, url=url, platform=platform)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{html_file}（解析失败：{exc}）")
            else:
                if parsed.fetch_status == "failed":
                    reason = parsed.fetch_note or "这个网页里没解析出 JD 正文"
                    failures.append(f"{html_file}\n    {reason}")
                else:
                    details.append(parsed)

    content = text
    if manual and content is None:
        content = _read_stdin()

    if content is not None:
        if not content.strip():
            failures.append("手动粘贴的内容是空的")
        else:
            details.append(
                parse_detail_from_text(
                    content,
                    url=url,
                    platform=platform or "manual",
                )
            )

    return details, failures


def _read_url_file(path: Path) -> list[str]:
    """读批量 URL 文件（一行一个；`#` 开头是注释；忽略空行）。

    ═══════════════════════════════════════════════════════════════════════
    ★★ 三道闸门，全部在**读取阶段**做（不是丢给后面的抓取循环）
    ═══════════════════════════════════════════════════════════════════════

        ① scheme 白名单：只收 `http` / `https`
        ② 按 URL 归一化去重（复用 `job_svc.url_dedup_key`，不另写一套规则）
        ③ 条数上限（见 `URL_FILE_MAX_ITEMS`）

    为什么必须在读取阶段做：抓取循环里每一条都要等 2~5 秒的随机延时
    （设计文档 §七.7「频率克制」）。一次喂 5000 行、或同一个链接喂两遍，
    都会变成"连着对外请求几小时"与"对同一页面重复请求"——
    这两种行为都不该由"用户手滑写重了一行"来触发。

    ★ 为什么跳过的东西必须**报出来**：`--file` 是唯一"用户没法一眼看全"的入口
      （清单可能有几百行）。静默少抓几条，用户第二天才发现清单里少了一半岗位，
      而那时已经不知道是哪一步丢的。所以重复 / 非法 / 超限三类都明确打印。

    ★ 签名保持不变（返回 `list[str]`）：这是 `_collect_details` 与验证用例
      （`tests/verify/test_v_adversarial.py`）都在用的契约；跳过原因通过
      终端输出交代，不塞进返回值里改变形状。
    """
    if not path.is_file():
        err_console.print(f"[red]找不到文件：{path}[/red]")
        raise typer.Exit(code=1)

    accepted: list[str] = []
    # 归一化 key → 第一次出现的行号（留着是为了提示"和哪一行重复"）
    seen: dict[str, int] = {}
    rejected: list[str] = []
    duplicates: list[str] = []
    overflow: list[str] = []

    for line_no, line in enumerate(read_text(path).splitlines(), start=1):
        # ★ 去掉 BOM（`\ufeff`）：Windows 上 `Out-File` / 记事本默认写 BOM，
        #   于是**第一行**会变成 `\ufeff# 注释`（注释判断失效）或 `\ufeffhttps://…`
        #   （链接被判成"没有 scheme"而丢弃）。同一个坑在 `_read_stdin` 那里踩过一次
        #   （见它的注释），这里是文件版：BOM 不该让用户少抓一条岗位。
        stripped = line.replace("\ufeff", "").strip()
        if not stripped or stripped.startswith("#"):
            continue

        reason = _url_reject_reason(stripped)
        if reason is not None:
            rejected.append(f"第 {line_no} 行 {stripped}（{reason}）")
            continue

        key = job_svc.url_dedup_key(stripped)
        if key and key in seen:
            duplicates.append(f"第 {line_no} 行 {stripped}（与第 {seen[key]} 行是同一个链接）")
            continue
        if key:
            seen[key] = line_no

        if len(accepted) >= URL_FILE_MAX_ITEMS:
            overflow.append(stripped)
            continue

        accepted.append(stripped)

    _report_url_file_skips(path, rejected=rejected, duplicates=duplicates, overflow=overflow)
    return accepted


#: 允许的 scheme（招聘页只可能是这两个）。
_ALLOWED_SCHEMES = ("http", "https")

#: 明确指向本机的域名（IP 字面量另有 `ipaddress` 判定）。
_LOCAL_HOSTS = ("localhost", "localhost.localdomain", "127.0.0.1", "::1", "0.0.0.0")


def _url_reject_reason(raw: str) -> str | None:
    """这一行为什么不能进抓取队列（返回 None = 通过）。

    ★ 为什么要挡非 http(s) 与内网地址：

        `jh job add --file` 会把每一行**真的交给浏览器打开**。
        `file:///C:/Windows/win.ini`、`javascript:…`、`http://192.168.1.1/`
        既不是招聘页，也让一个"只抓岗位"的工具去读本机文件 / 探测内网 ——
        设计文档 §七.2 写明不做任何绕过与越界的事，这是同一条纪律的延伸。

    ★ 为什么用 `urlsplit` 而不是自己切字符串：
        大小写 scheme（`HTTP://`）、带用户名密码、`//host` 省略 scheme 等写法
        都得处理；标准库已经把这些边界写对了，手写正则只会漏。
    """
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "不是合法链接"
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        shown = scheme or "没有 scheme"
        return f"只接受 http/https 链接（这里是 {shown}）"
    host = (parts.hostname or "").strip().lower()
    if not host:
        return "链接里没有域名"
    if _is_private_host(host):
        return "指向本机/内网地址（这里只抓公开招聘页）"
    return None


def _is_private_host(host: str) -> bool:
    """host 是不是本机 / 内网（含 IP 字面量的私有段与内网后缀域名）。"""
    if host in _LOCAL_HOSTS:
        return True
    if host.endswith((".local", ".localhost", ".internal", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    # is_private 覆盖 10/8、172.16/12、192.168/16；其余几个是回环/链路本地/保留段
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified
    )


def _report_url_file_skips(
    path: Path,
    *,
    rejected: list[str],
    duplicates: list[str],
    overflow: list[str],
) -> None:
    """把三类"跳过"打印给用户（每类最多列 3 条，其余只报数量）。

    ★ 为什么不一整批全列出来：清单文件有几百行时，把 200 条非法行全打出来
      会把真正的结论（"只有前 20 条会被处理"）顶出屏幕。数量 + 前几条足够定位。
    """
    if rejected:
        warn(f"{path.name}：{len(rejected)} 条被跳过（不是能抓的岗位链接）")
        for item in rejected[:3]:
            console.print(f"  [dim]· {item}[/dim]")
        if len(rejected) > 3:
            console.print(f"  [dim]· …还有 {len(rejected) - 3} 条同类[/dim]")
    if duplicates:
        # ★ 重复也要报：这说明用户的清单来源本身有重复（比如两次导出拼在一起），
        #   只报"跳过 N 条"会让他以为工具漏抓了。
        warn(f"{path.name}：跳过 {len(duplicates)} 条重复链接（同一个岗位抓一次就够）")
        for item in duplicates[:3]:
            console.print(f"  [dim]· {item}[/dim]")
        if len(duplicates) > 3:
            console.print(f"  [dim]· …还有 {len(duplicates) - 3} 条重复[/dim]")
    if overflow:
        warn(
            f"{path.name}：只处理前 {URL_FILE_MAX_ITEMS} 条，"
            f"其余 {len(overflow)} 条**没有抓取**（不是抓失败，是根本没进队列）"
        )
        console.print(
            "  [dim]为什么有上限：抓取之间有 2~5 秒随机延时，几千条会连发几小时，"
            "与「频率克制」冲突。要全抓请拆成多个文件分批跑。[/dim]"
        )


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

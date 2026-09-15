"""`jh resume` —— 简历导入解析 / 列表 / **拆分方向建议**（P1）。

三条命令的定位：

    jh resume add <path>        把 docx 变成结构化数据落库（后续匹配/定制/开场白都靠它）
    jh resume list              看库里有哪几份
    jh resume split <id>        ★ 帮我这个"只有 1 份、没分方向"的人拆方向

═══════════════════════════════════════════════════════════════════════════
★ `split` 的三条硬纪律（都不是技术细节，是产品决策）
═══════════════════════════════════════════════════════════════════════════

  1. **只给建议，绝不改写简历**。
     真正改简历是 P5 的 `tailor`，而且那一步必须有 FidelityGuard 把关
     （实体白名单 + 数字守恒 + 时间守恒 + 技能词表 + 长度上限）。
     P1 就动手改文件，风险极高且不可逆。

  2. **默认不调 LLM**。
     解析走确定性规则（分段词表 + 技能词表），LLM 兜底开关默认关闭。
     理由：① 简历是隐私数据，能不往外发就不发；② 规则结果可复现、好测；
     ③ 规则解析失败会**明确告诉你哪一段没认出来**，而不是悄悄编一段像模像样的内容。

  3. **序号就是 `jh resume list` 里的 id**。
     所有命令的序号/ID 必须是同一套，否则"我以为在改第 3 条"会造成误操作。

★ 与 WorkBuddy 的分工（本项目的真实工作流）
    `jh resume split` 给出**结构化的事实与缺口**（哪些项目该前置、缺哪些技能）；
    真正的"按方向改写简历成 docx"交给本机的 WorkBuddy（多模态文档处理更强）。
    所以 split 的产物设计成**人能直接拿去下指令的形式**（`--save` 落成 Markdown）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from jobhunter.cli.common import EXIT_NOT_IMPLEMENTED, success, warn
from jobhunter.config.settings import get_settings
from jobhunter.services import resume_svc
from jobhunter.storage.db import database_ready, get_session_factory
from jobhunter.utils.io import write_text
from jobhunter.utils.logging import setup_logging

app = typer.Typer(name="resume", help="简历：导入解析 / 列表 / 拆分方向（P1）")

console = Console()
err_console = Console(stderr=True)

#: 数据库还没初始化时的统一提示（比 "no such table: resumes" 可读得多）
_INIT_HINT = "[yellow]数据库还没初始化。请先运行：[bold]jh init[/bold][/yellow]"


def _require_db() -> tuple[object, object]:
    """确认数据库可用，返回 (engine, session_factory)。

    ★ 为什么每个命令都要先过这一关：SQLAlchemy 在表不存在时报的是
      `no such table: resumes` —— 对用户来说完全不知道该怎么办。
      这里换成一句"请先运行 jh init"。
    """
    setup_logging()
    settings = get_settings()
    if not database_ready(settings.database_url):
        err_console.print(_INIT_HINT)
        raise typer.Exit(code=1)
    return get_session_factory(settings.database_url)


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------
@app.command("add")
def resume_add(
    path: Annotated[
        Path | None,
        typer.Argument(
            help="简历文件路径（.docx；也支持 .txt/.md）。不填则用 settings.default_resume_path"
        ),
    ] = None,
    direction: Annotated[
        str | None,
        typer.Option("--direction", "-d", help="求职方向，如 'AI Agent'；不填则按内容自动判断"),
    ] = None,
    name: Annotated[
        str | None, typer.Option("--name", "-n", help="简历名，如 '主简历'；默认用文件名")
    ] = None,
    default: Annotated[
        bool, typer.Option("--default", help="设为默认简历（库为空时第一份自动成为默认）")
    ] = False,
) -> None:
    """导入简历并解析成结构化数据（幂等：同一份内容重复导入不会新建记录）。"""
    settings = get_settings()

    target = path or settings.default_resume_path
    if target is None:
        err_console.print(
            Panel(
                "没有指定简历文件。两种用法：\n\n"
                "  1. 直接给路径：[bold]jh resume add 简历.docx[/bold]\n"
                "  2. 在 .env 里配置默认路径（之后就不用每次输路径）：\n"
                "     [dim]JH_DEFAULT_RESUME_PATH=D:/path/to/你的简历.docx[/dim]\n\n"
                "支持 .docx（推荐）以及 .txt / .md。",
                title="缺少简历文件",
                border_style="yellow",
            )
        )
        raise typer.Exit(code=EXIT_NOT_IMPLEMENTED)

    target = Path(target)
    if not target.is_file():
        err_console.print(f"[red]找不到文件：{target}[/red]")
        raise typer.Exit(code=1)

    engine, factory = _require_db()
    try:
        with factory() as session:
            try:
                result = resume_svc.add_resume(
                    session,
                    target,
                    name=name,
                    direction=direction,
                    make_default=default,
                    settings=settings,
                )
            except resume_svc.ResumeParseError as exc:
                session.rollback()
                err_console.print(f"[red]解析失败：{exc}[/red]")
                raise typer.Exit(code=1) from exc
            except Exception as exc:  # noqa: BLE001 - 文件损坏/格式不支持等
                session.rollback()
                err_console.print(f"[red]导入失败：{exc}[/red]")
                raise typer.Exit(code=1) from exc
            session.commit()

        verb = "已导入" if result.created else "这份内容库里已有，已更新描述信息"
        direction_text = result.direction or "未识别"
        success(f"{verb}：id={result.resume_id}　{result.name}　方向={direction_text}")

        table = Table(show_header=False, box=None)
        for key, value in result.counts.items():
            table.add_row(f"  {key}", str(value))
        console.print(table)
        console.print(
            f"  [dim]内容 hash：{result.content_hash[:16]}…（改简历会自动失效 LLM 缓存）[/dim]"
        )

        if result.became_default:
            console.print("  [green]★ 已设为默认简历[/green]")
        for message in result.warnings:
            console.print()
            warn(message)

        if not result.created:
            console.print()
            console.print(
                "[dim]提示：内容没变，所以没有新建记录 —— 这是有意设计（避免重复占 id）。[/dim]"
            )

        console.print()
        console.print(f"下一步：[green]jh resume split {result.resume_id}[/green] 看该拆哪些方向")
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------
@app.command("list")
def resume_list() -> None:
    """列出已导入的简历。"""
    engine, factory = _require_db()
    try:
        with factory() as session:
            rows = resume_svc.list_resumes(session)
    finally:
        engine.dispose()

    if not rows:
        console.print("[dim]（还没有导入任何简历）[/dim]")
        console.print()
        console.print("先导入一份：[green]jh resume add 你的简历.docx[/green]")
        return

    table = Table(title="简历列表", header_style="bold cyan")
    table.add_column("id", justify="right")
    table.add_column("名称")
    table.add_column("方向")
    table.add_column("默认", justify="center")
    table.add_column("项目", justify="right")
    table.add_column("技能", justify="right")
    table.add_column("hash")
    table.add_column("导入时间")
    for row in rows:
        table.add_row(
            str(row.id),
            row.name,
            row.direction,
            "★" if row.is_default else "",
            str(row.projects),
            str(row.skills),
            row.content_hash,
            row.created_at,
        )
    console.print(table)


# ---------------------------------------------------------------------------
# split ★
# ---------------------------------------------------------------------------
@app.command("split")
def resume_split(
    resume_id: Annotated[
        int | None,
        typer.Argument(help="简历 ID（见 jh resume list）。不填则用默认简历"),
    ] = None,
    directions: Annotated[
        list[str] | None,
        typer.Option("--direction", "-d", help="要拆成哪些方向（可重复）；不填则自动挑两个"),
    ] = None,
    top: Annotated[int, typer.Option("--top", "-n", help="每个方向推荐前置几个项目")] = 3,
    save: Annotated[
        bool,
        typer.Option(
            "--save",
            help="把建议写成 Markdown 落到 output/（可直接拿给 WorkBuddy 当指令）",
        ),
    ] = False,
) -> None:
    """拆分方向建议：哪些项目该前置、哪些该弱化、还缺什么（**只给建议，不改简历**）。"""
    settings = get_settings()
    engine, factory = _require_db()
    try:
        with factory() as session:
            if resume_id is None:
                rows = resume_svc.list_resumes(session)
                if not rows:
                    console.print("[dim]（还没有导入任何简历）[/dim]")
                    raise typer.Exit(code=1)
                default_row = next((row for row in rows if row.is_default), rows[0])
                resume_id = default_row.id
                console.print(f"[dim]未指定简历，使用 id={resume_id}（{default_row.name}）[/dim]")

            try:
                summary, suggestions = resume_svc.split_resume(
                    session,
                    resume_id,
                    directions=directions,
                    top_n=top,
                    settings=settings,
                )
            except LookupError as exc:
                err_console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()

    if not suggestions:
        err_console.print(
            "[yellow]没有可用的方向画像。[/yellow]\n"
            "[dim]检查 src/jobhunter/config/direction_profiles.yaml 是否存在。[/dim]"
        )
        raise typer.Exit(code=1)

    console.print()
    console.print(
        f"[bold]简历「{summary.name}」[/bold]　当前方向：{summary.direction or '（未设置）'}"
        f"　项目 {summary.projects} 个　技能 {summary.skills} 个"
    )

    for suggestion in suggestions:
        _render_suggestion(suggestion)

    console.print()
    console.print(
        Panel(
            "以上只是**建议**，我没有改动你的简历文件。\n"
            "要真正按方向改写简历，可以：\n"
            f"  1. [bold]jh resume split {resume_id} --save[/bold] "
            "把建议落成 Markdown\n"
            "  2. 把那份 Markdown 交给本机的 [bold]WorkBuddy[/bold] 去生成 docx 副本\n\n"
            "[dim]P5 的 `jh tailor` 会在项目内完成改写，并强制经过 FidelityGuard（防编造）。[/dim]",
            title="下一步",
            border_style="cyan",
        )
    )

    if save:
        path = _save_suggestion(summary, suggestions)
        console.print()
        success(f"建议已写入：{path}")


def _render_suggestion(suggestion: resume_svc.SplitSuggestion) -> None:
    """在终端渲染一个方向的建议。"""
    console.print()
    console.rule(f"[bold cyan]{suggestion.direction}[/bold cyan]", align="left")

    if suggestion.priority_projects:
        table = Table(title="该前置的项目（按相关度排序）", header_style="bold")
        table.add_column("项目", overflow="fold")
        table.add_column("分", justify="right")
        table.add_column("为什么", overflow="fold")
        for index, fit in enumerate(suggestion.priority_projects, start=1):
            table.add_row(f"{index}. {fit.name}", str(fit.score), fit.reason)
        console.print(table)

    if suggestion.de_emphasis_projects:
        names = "、".join(fit.name for fit in suggestion.de_emphasis_projects)
        console.print(f"[dim]· 该弱化（与方向关系不大，可以压缩成一两行）：{names}[/dim]")

    if suggestion.headline_skills:
        console.print(
            f"[green]· 主打技能（简历已有）：[/green]{'、'.join(suggestion.headline_skills)}"
        )
    if suggestion.plus_skills_owned:
        console.print(f"[cyan]· 加分技能（已有）：[/cyan]{'、'.join(suggestion.plus_skills_owned)}")
    if suggestion.gaps:
        console.print(f"[red]· 缺口（这个方向常要、你还没有）：[/red]{'、'.join(suggestion.gaps)}")
    if suggestion.weak_skills:
        console.print(
            f"[dim]· 可以少提（这个方向不看重的技术）：{'、'.join(suggestion.weak_skills)}[/dim]"
        )
    if suggestion.learning_skills:
        console.print(
            "[yellow]· 在学习白名单（可以提，但要标注「在学习中」）："
            f"{'、'.join(suggestion.learning_skills)}[/yellow]"
        )

    for note in suggestion.notes:
        console.print(f"  [dim]→ {note}[/dim]")


def _save_suggestion(
    summary: resume_svc.ResumeSummary,
    suggestions: list[resume_svc.SplitSuggestion],
) -> Path:
    """把建议写成 Markdown（可直接交给 WorkBuddy 当改写指令）。"""
    settings = get_settings()
    output_dir = settings.paths.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"resume-split-{summary.id}.md"

    lines: list[str] = [
        f"# 简历拆分方向建议（resume id={summary.id}　{summary.name}）",
        "",
        f"- 当前方向：{summary.direction or '（未设置）'}",
        f"- 项目 {summary.projects} 个 / 技能 {summary.skills} 个",
        f"- 内容 hash：`{summary.content_hash}`",
        "",
        "> ⚠️ 以下只是**建议**，未改动简历文件。真正改写时请保持所有事实不变"
        "（不得新增/夸大经历、技能、数字、时间）。",
        "",
    ]
    for suggestion in suggestions:
        lines.append(f"## 方向：{suggestion.direction}")
        lines.append("")
        if suggestion.priority_projects:
            lines.append("### 该前置的项目")
            lines.append("")
            for index, fit in enumerate(suggestion.priority_projects, start=1):
                lines.append(f"{index}. **{fit.name}**（相关度 {fit.score}）—— {fit.reason}")
            lines.append("")
        if suggestion.de_emphasis_projects:
            lines.append("### 该弱化")
            lines.append("")
            for fit in suggestion.de_emphasis_projects:
                lines.append(f"- {fit.name}")
            lines.append("")
        lines.append("### 技能取舍")
        lines.append("")
        if suggestion.headline_skills:
            lines.append(f"- 主打（已有）：{'、'.join(suggestion.headline_skills)}")
        if suggestion.plus_skills_owned:
            lines.append(f"- 加分（已有）：{'、'.join(suggestion.plus_skills_owned)}")
        if suggestion.gaps:
            lines.append(f"- 缺口（该补）：{'、'.join(suggestion.gaps)}")
        if suggestion.weak_skills:
            lines.append(f"- 可少提：{'、'.join(suggestion.weak_skills)}")
        if suggestion.learning_skills:
            lines.append(f"- 在学习（需标注）：{'、'.join(suggestion.learning_skills)}")
        lines.append("")
        if suggestion.notes:
            lines.append("### 结论")
            lines.append("")
            for note in suggestion.notes:
                lines.append(f"- {note}")
            lines.append("")

    return write_text(path, "\n".join(lines), atomic=True)

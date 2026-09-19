"""`jh followup` —— 跟进追问话术（**手动触发，不发送任何东西**）（P6）。

    jh followup 3                      # 按 applied_at 自动算"投了几天"
    jh followup 3 --days 7             # 手动指定
    jh followup 3 --variant 正式       # 简短（默认）/ 正式
    jh followup 3 --sent               # 我发完之后记一笔（写 followup 事件）

    这是合规红线的一部分 —— 本工具**不做任何自动发送/自动联系**。
    它只负责把话术写好（连同该公司/岗位的 JD 关键词），由我决定发不发。

★ 话术要引用具体信息（投递日期、岗位名、我真实会的技能），
  否则"请问有消息吗"这种群发感十足的话术反而减分。

★ 顶部提醒里的"投后 3 天未跟进 / 7 天无动静 / 14 天建议放弃"是**入口**，
  这条命令是**执行动作** —— 两者配合才是完整闭环。

★ 降级必须说出来：没配 key / 模型不过闸门 → 用**只用真话拼**的确定性模板，
  并把原因打印在下面。静默降级会让人以为"模型就这水平"，其实是闸门在保护他。
"""

from __future__ import annotations

from typing import Annotated

import typer

from jobhunter.cli.common import console, err_console, require_db, success, warn
from jobhunter.llm.factory import get_provider
from jobhunter.models import Job
from jobhunter.services import apply_svc, followup_svc
from jobhunter.utils.clipboard import copy_to_clipboard

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
    copy: Annotated[bool, typer.Option("--copy", help="复制到剪贴板")] = False,
    sent: Annotated[bool, typer.Option("--sent", help="我发完了，记一笔 followup 事件")] = False,
) -> None:
    """生成一条追问话术（P6）。"""
    if variant is not None and variant not in followup_svc.FOLLOWUP_STYLES:
        err_console.print(f"[red]--variant 只支持 {' / '.join(followup_svc.FOLLOWUP_STYLES)}[/red]")
        raise typer.Exit(code=2)

    settings = _settings()
    engine, factory = require_db()
    try:
        with factory() as session:
            job = session.get(Job, job_id)
            if job is None:
                err_console.print(f"[red]找不到岗位 #{job_id}[/red]")
                raise typer.Exit(code=1)

            application = apply_svc.get_latest_application(session, job_id)
            if application is None or application.applied_at is None:
                warn(
                    f"岗位 #{job_id} 还没有「已投递」记录 —— 先打标："
                    f"jh apply --job {job_id} --status applied"
                )
            try:
                result = followup_svc.generate_followup(
                    session,
                    job,
                    days=days,
                    style=variant,
                    settings=settings,
                    provider=get_provider(settings),
                )
                if sent:
                    _record_sent(session, job_id, result)
            except followup_svc.FollowupError as exc:
                err_console.print(f"[red]{exc}[/red]")
                raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()

    console.print()
    console.print(
        f"[bold]追问话术[/bold]（{job.company or '—'} · {job.title or job_id}）"
        f"　[dim]{followup_svc.describe_days(result.days)}"
        f"　｜　风格 {result.style}"
        f"　｜　{'模型生成' if result.source == 'llm' else '模板兜底'}[/dim]"
    )
    console.print()
    console.print(f"  {result.text}")
    console.print()
    material = "、".join(result.material) or "（无）"
    console.print(f"  [dim]{len(result.text)} 字　｜　用到的素材：{material}[/dim]")

    # ★ 降级原因必须显示（与开场白同一条纪律）
    if result.issues:
        warn("这条是模板兜底（模型没过闸门或没配 key）：")
        for item in result.issues[:4]:
            console.print(f"   [dim]· {item}[/dim]")

    if copy and copy_to_clipboard(result.text):
        success("已复制到剪贴板 —— 去招聘 App 里粘贴（发不发由你定）")
    elif copy:
        warn("复制失败（这台机器上可能没有 clip/pbcopy/xclip），手动选中上面的文本即可")

    if sent:
        success("已记录一条 followup 事件（时间轴上会显示「我主动追问了一次」）")
    else:
        console.print(
            f"  [dim]发完之后记一笔：[green]jh followup {job_id} --sent[/green]"
            "（可选，但时间轴会更完整）[/dim]"
        )
    console.print("  [dim]★ 本工具不会替你发送任何消息。[/dim]")


def _record_sent(session, job_id: int, result: followup_svc.Followup) -> None:
    """记一条"我追问了"的事件（`--sent` 时调用）。**只有我明确说了才记**：
    生成 ≠ 发送，把生成当成发送会让时间轴撒谎。"""
    followup_svc.record_sent(
        session, job_id, text=result.text, style=result.style, days=result.days
    )
    session.commit()


def _settings():
    from jobhunter.config.settings import get_settings

    return get_settings()

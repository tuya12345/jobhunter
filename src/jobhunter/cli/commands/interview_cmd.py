"""`jh interview` —— 面试安排（P6 实现，本阶段为占位）。

P6 的最终形状：

    jh interview add --job <id> --round 一面 --at "2026-09-11 14:00" \\
                     --mode 视频 --link "https://..." --status 待面
    jh interview list [--upcoming]

★ 时间输入的约定：`--at` 用**北京时间**书写（"2026-09-11 14:00"），
  内部立刻转成 UTC 存库（见 utils/time.py 的 parse_local_input）。
  展示时再转回北京时间 —— 用户永远看不到 UTC。
  "本周面试 N 场（9-10 14:00 字节 · 9-12 10:00 腾讯）"这条顶部提醒就靠它。
"""

from __future__ import annotations

from typing import Annotated

import typer

from jobhunter.cli.common import not_implemented

app = typer.Typer(help="面试：安排 / 列表（P6）")


@app.command("add")
def interview_add(
    job: Annotated[int, typer.Option("--job", "-j", help="关联的岗位 ID")],
    round_: Annotated[str | None, typer.Option("--round", help="一面 / 二面 / HR面")] = None,
    at: Annotated[
        str | None, typer.Option("--at", help='面试时间（北京时间），如 "2026-09-11 14:00"')
    ] = None,
    mode: Annotated[str | None, typer.Option("--mode", help="视频 / 电话 / 现场")] = None,
    link: Annotated[str | None, typer.Option("--link", help="会议链接")] = None,
    location: Annotated[str | None, typer.Option("--location", help="现场地址")] = None,
    status: Annotated[str, typer.Option("--status", help="待面 / 已面 / 出结果")] = "待面",
    note: Annotated[str | None, typer.Option("--note", help="备注")] = None,
) -> None:
    """新增一场面试（P6）。"""
    not_implemented(
        f"jh interview add（job={job}, round={round_}, at={at}, mode={mode}, "
        f"link={link}, location={location}, status={status}, note={note}）",
        "P6",
    )


@app.command("list")
def interview_list(
    upcoming: Annotated[bool, typer.Option("--upcoming", help="只看接下来要面的")] = False,
) -> None:
    """列出面试（P6）。"""
    not_implemented(f"jh interview list（upcoming={upcoming}）", "P6")

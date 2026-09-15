"""`jh init` —— **P0 唯一真正实现的命令**。

它要做四件事（每一步都对应一个前面踩过的坑）：

    1. 建目录 data/ output/ state/
       ↳ SQLite 文件不存在会自动创建，但**目录**不存在时报的是
         `unable to open database file` —— 完全看不出是目录问题。

    2. Alembic upgrade head（11 张表）
       ↳ P0 就上迁移，是为了"以后加字段不删库"。
         等到库里有真实投递记录时再补迁移，就只能删库重来了。

    3. 打开连接并**校验 PRAGMA 三项**（WAL / busy_timeout / foreign_keys）
       ↳ 只是"设置了"不算数，必须读回来确认。
         尤其是 foreign_keys：SQLite 默认关闭，设错也不报错，
         后果是外键约束静默失效、脏数据悄悄进库。

    4. 打印小结（库路径、迁移版本、表数量、PRAGMA 实际值）

★ 幂等：重复执行不报错、不重建、不清数据。
  第二次运行会显示"已是最新版本"。
"""

from __future__ import annotations

import typer
from sqlalchemy import inspect

from jobhunter.cli.common import console, print_kv, success, warn
from jobhunter.config.settings import get_settings
from jobhunter.storage.db import check_pragmas, create_db_engine
from jobhunter.storage.migrate import current_revision, upgrade_head

app = typer.Typer(
    name="init",
    help="初始化项目：建目录 + 建库 + 跑迁移（幂等，可重复执行）",
    no_args_is_help=False,
)


# 期望的 PRAGMA 值（与 storage/db.py 的 EXPECTED_PRAGMAS 一致，
# 这里独立写一份是为了让"校验"和"设置"两处不共享同一个可能被改错的常量）
_REQUIRED_PRAGMAS: dict[str, str] = {
    "journal_mode": "wal",
    "foreign_keys": "1",
    "busy_timeout": "5000",
}


@app.callback(invoke_without_command=True)
def init(
    force: bool = typer.Option(
        False,
        "--force",
        help="即使数据库已存在也重新检查一遍（不会删数据，只重跑迁移与 PRAGMA 校验）",
    ),
) -> None:
    """初始化 JobHunter：目录 → 数据库 → 迁移 → 自检。"""
    settings = get_settings()
    paths = settings.paths

    console.print("[bold]JobHunter 初始化[/bold]")
    console.print()

    # ---- 1. 目录 ----------------------------------------------------------
    paths.ensure_all()
    print_kv(
        {
            "项目根目录": paths.root,
            "数据目录": paths.data_dir,
            "导出目录": paths.output_dir,
            "状态目录": paths.state_dir,
        },
        title="① 目录已就绪",
    )
    console.print()

    # ---- 2. 迁移 ----------------------------------------------------------
    db_exists = paths.db_path.is_file()
    console.print("② 执行数据库迁移（Alembic upgrade head）…")
    try:
        upgrade_head(settings.database_url)
    except Exception as exc:  # noqa: BLE001 —— 迁移失败要给出可读提示而不是栈
        console.print(f"[red]✗ 迁移失败：{exc}[/red]")
        console.print(
            "[dim]常见原因：alembic.ini 缺失、alembic/versions 目录被清空、"
            "或数据库文件被其他进程占用（关掉正在运行的其他 jh 命令）。[/dim]"
        )
        raise typer.Exit(code=1) from exc

    if db_exists and not force:
        success("数据库已存在，迁移已确认到最新（数据未改动）")
    else:
        success("数据库结构与迁移脚本一致")

    # ---- 3. 打开连接 + 校验 PRAGMA ----------------------------------------
    engine = create_db_engine(settings.database_url)
    actual = check_pragmas(engine)

    revision = current_revision(engine)
    table_names = sorted(inspect(engine).get_table_names())
    # alembic_version 是 Alembic 自己的记账表，不算业务表
    business_tables = [t for t in table_names if t != "alembic_version"]

    console.print()
    print_kv(
        {
            "数据库": paths.db_path,
            "迁移版本": revision or "（无）",
            "业务表数量": f"{len(business_tables)} 张",
            "表": ", ".join(business_tables),
        },
        title="③ 数据库自检",
    )

    # ---- 4. PRAGMA 校验（失败绝不能静默放过）------------------------------
    problems = [
        f"{name} 期望 {expected!r}，实际 {actual.get(name)!r}"
        for name, expected in _REQUIRED_PRAGMAS.items()
        if actual.get(name) != expected
    ]
    console.print()
    if problems:
        console.print("[red]④ PRAGMA 校验未通过：[/red]")
        for problem in problems:
            console.print(f"  [red]· {problem}[/red]")
        console.print(
            "[dim]最要紧的是 foreign_keys：SQLite 默认关闭外键，"
            "关着的话脏数据会静默写进库。请检查 storage/db.py 的 connect 监听器。[/dim]"
        )
        raise typer.Exit(code=1)

    print_kv(dict(actual), title="④ PRAGMA 校验通过")
    console.print()
    success("初始化完成。下一步：`jh --help` 看全部命令（其余命令将在后续阶段实现）")

    if not settings.llm_enabled:
        console.print()
        warn(
            "未检测到 DEEPSEEK_API_KEY（.env）。P0 阶段不影响使用；"
            "P3 的 LLM 精评需要它，届时请复制 .env.example 为 .env 并填写。"
        )

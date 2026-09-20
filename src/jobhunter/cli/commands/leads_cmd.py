"""`jh leads` —— 线索的**消费端**：导入采集端的 JSON 快照 + 一个只读页面。

╔══════════════════════════════════════════════════════════════════════════╗
║ P7 在 2026-09-19 改了架构（ISSUE #59）：采集与消费**拆成两个部署**          ║
╚══════════════════════════════════════════════════════════════════════════╝

    ┌─ 采集端（独立部署，**不在本仓库**）────────────────────────────┐
    │  projects/leadhunter     命令 `lh`                            │
    │  lh leads add / list / export / apply / probe / capture        │
    │  → 产出线索并导出成 JSON 快照                                  │
    └──────────────────┬────────────────────────────────────────────┘
                       │  用户手动"上传"（拷文件）
                       ▼
    ┌─ 消费端（本仓库）─────────────────────────────────────────────┐
    │  jh leads import <快照.json>   → companies 表（幂等）          │
    │  jh leads serve                → 本地页面（看线索 + 标记投递）  │
    └───────────────────────────────────────────────────────────────┘

★ 为什么采集命令（add / probe / capture / export）**不在**这里了

    它们要装安卓模拟器那一套依赖（`uiautomator2`）、要连设备、要在本机跑；
    而"看线索"这件事不该背上那份环境负担。所以采集端单独部署，
    本仓库只保留"导入 + 页面"。这是规格（`docs/P7-消费端规格.md`）里明写的红线。

★ 两个命令各自的纪律

    · `import`：幂等（同一份文件导两次 → 新增 0 条、更新 N 条）、
      `--dry-run` 一个字都不写库、坏输入给中文可操作提示（见 `services/leads_svc.py`）。
    · `serve`：**默认只绑 127.0.0.1**（本地隐私工具，绑 0.0.0.0 会让同局域网的人
      看到你的线索）。端口默认 8787（8765 被 StageBoard 占着）。

★ 红线：页面不做自动投递、不做批量、不增删线索、不把采集代码搬回来。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from jobhunter.cli.common import console, err_console, require_db, success
from jobhunter.services import leads_svc

app = typer.Typer(help="线索消费端：导入采集端的 JSON 快照 / 起本地页面（P7）")

#: `serve` 的默认端口。★ 不用 8765：那是 StageBoard 的平台端口。
DEFAULT_PORT = 8787
#: 默认绑定地址。★ 默认回环：本地隐私工具，绑 0.0.0.0 = 同局域网可见。
DEFAULT_HOST = "127.0.0.1"


@app.command("import")
def leads_import(
    file: Annotated[
        Path, typer.Argument(help="采集端导出的 JSON 快照（`lh leads export --format json`）")
    ],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只报数，一个字都不写库")] = False,
) -> None:
    """把采集端的 JSON 快照导入 `companies`（**幂等**：同一份文件导两次不会变两份）。

    ★ 为什么强调幂等：采集端会反复导出（每次采到新线索就导一次），
      而"导两次变成两条"会让线索池慢慢变成一团重复数据，且没有任何报错 ——
      属于本项目最不想要的"静默错误"。匹配口径是**公司名归一化**
      （与采集端内部的合并口径同源），并且在库层面加了唯一索引（迁移 0005）。
    """
    _engine, factory = require_db()
    with factory() as session:
        try:
            # ★ dry-run 与真跑共用同一段判定逻辑（只差最后一步"执行"），
            #   所以不会出现"dry-run 说没事、真跑写坏了"。
            report = leads_svc.import_snapshot(session, file, dry_run=dry_run)
        except leads_svc.LeadSnapshotError as exc:
            session.rollback()
            err_console.print(f"[red]导入失败[/red]：{exc}")
            raise typer.Exit(code=1) from exc

        if dry_run:
            session.rollback()  # 双保险：dry-run 绝不留下任何改动
            _print_dry_run(report)
            return
        session.commit()

    success(report.summary())
    _print_rows(report)
    if report.added or report.changed:
        console.print("  [dim]看线索 / 标记投递：jh leads serve[/dim]")


def _print_dry_run(report: leads_svc.ImportReport) -> None:
    """dry-run 的输出：把"将会发生什么"逐条列出来，而不是只给一个总数。"""
    console.print(f"[yellow]dry-run[/yellow]：{report.summary()}")
    _print_rows(report)
    console.print(
        "  [dim]库里没有任何改动（连一次 UPDATE 都没发）。去掉 --dry-run 才会真的导入。[/dim]"
    )


def _print_rows(report: leads_svc.ImportReport) -> None:
    if not report.rows:
        console.print("  [dim]（快照里没有线索：空数组是合法的，表示这次没采到）[/dim]")
        return
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("处理", width=6)
    table.add_column("公司")
    table.add_column("变化", overflow="fold")
    for row in report.rows:
        if row.action == "add":
            action = "[green]新增[/green]"
        elif row.changed_fields:
            action = "[cyan]更新[/cyan]"
        else:
            action = "[dim]不变[/dim]"
        detail = "、".join(row.changed_fields) if row.changed_fields else "—"
        if row.duplicate_in_file:
            detail = f"{detail}（同文件重复，以最后一条为准）"
        table.add_row(action, row.name, detail)
    console.print(table)


@app.command("serve")
def leads_serve(
    host: Annotated[
        str, typer.Option("--host", help="绑定地址（默认 127.0.0.1：只允许本机访问）")
    ] = DEFAULT_HOST,
    port: Annotated[int, typer.Option("--port", "-p", help="端口（默认 8787）")] = DEFAULT_PORT,
) -> None:
    """起本地页面：看线索 + 标记投递（**只读 + 一个写动作**）。

    ★ 为什么默认只绑 `127.0.0.1`：这是本地隐私工具，页面上有你的线索
      （公司名、原始那句话、备注里可能有邮箱/微信）。绑到 `0.0.0.0`
      等于让同一个 Wi-Fi 下的任何人都能打开它 —— 而这个页面**没有鉴权**。
      真要给别人看，请显式传 `--host 0.0.0.0`，并且清楚你在做什么。
    """
    try:
        import uvicorn

        from jobhunter.web.app import BUILD_HINT, create_app, frontend_dist_dir
    except ModuleNotFoundError as exc:  # pragma: no cover - 依赖没装时的提示
        # ★ `[web]` 必须转义（`rich.markup.escape`）：rich 会把方括号当**样式标签**吃掉，
        #   于是提示里印出来的是 `pip install -e "."` —— 照抄等于什么都没多装。
        #   这类"提示本身是错的"最难发现：它只在出错路径上出现，而那时用户最需要它是对的。
        install = escape('pip install -e ".[web]"')
        err_console.print(
            f"[red]缺少 Web 依赖[/red]：{exc.name}\n"
            f"  · 装一下：uv sync --extra web（或 {install}）\n"
            "  · 只导入、不打开页面的话，`jh leads import` 不需要这些依赖。"
        )
        raise typer.Exit(code=1) from exc

    require_db()  # 先确认库在（否则页面一起来就是一串 500）
    dist = frontend_dist_dir()
    if not (dist / "index.html").is_file():
        console.print(f"[yellow]注意[/yellow]：{BUILD_HINT}")
        console.print(f"  [dim]（后端接口照常可用：http://{host}:{port}/api/leads）[/dim]")

    console.print(f"线索页： [bold]http://{host}:{port}[/bold]")
    console.print("  [dim]只读 + 标记投递；不做自动投递、不做批量（红线）。Ctrl+C 结束。[/dim]")
    uvicorn.run(create_app(), host=host, port=port, log_level="info")

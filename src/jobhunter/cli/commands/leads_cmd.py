"""`jh leads` —— 官网/内推线索（P7 实现，本阶段为占位）。

★ P7 在 v3 里被**降级为"技术验证"**，只做 1 个平台，以稳为先。

P7 的最终形状：

    # 主路径（最重要）
    jh leads add <file>
        我把从截图/手动复制来的零散线索贴进 .txt/.md
        → LLM 整理成结构化 JSON（公司名/官网招聘页 URL/投递方式/
          source_type(官网|内推|HR直招)/来源平台/备注）
        → 去重（同公司合并）→ 入库 companies → URL 可达性检查（HEAD 记状态码）
    jh leads export
        官网投递清单 Markdown/CSV（按 source_type 分组）

    # 辅助路径（技术验证性质，只做 1 个平台）
    jh leads probe
        ★ dump 当前屏幕的控件层级树，**用于自检 locator**
    jh leads capture
        ★ "人工操作 + 按回车抓一屏"（比全自动稳得多）

★★ 现实难点（必须先承认，否则一定翻车）：
    控件 id 不稳定/不存在、WebView 内容取不到、
    国内 ROM 可能装不上 ATX、无线 ADB 易掉线。
    所以**不做全自动抓取**，改成"人工操作 + 机器抓屏"，每 App 一份 YAML 降级链。

★ 红线：不做批量抓取、不绕风控、**不做任何写操作**（点赞/私信/关注）。
  建议用备用账号。

★ 依赖：uiautomator2 放 `[emulator]` optional extra 并**钉死版本**
  （上游改动频繁，不钉版本会导致半年前能跑的脚本突然失灵）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from jobhunter.cli.common import not_implemented

app = typer.Typer(help="官网/内推线索：整理入库 / 抓屏验证 / 导出清单（P7）")


@app.command("add")
def leads_add(
    file: Annotated[Path, typer.Argument(help="线索文件（.txt/.md，零散线索原文）")],
    source_platform: Annotated[
        str | None, typer.Option("--platform", "-p", help="来源平台：贴吧/小红书/知乎/公众号…")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="只整理不写库（频率克制：先看看会入库什么）")
    ] = False,
) -> None:
    """把零散线索整理成结构化数据并入库（P7）。"""
    not_implemented(f"jh leads add {file}（platform={source_platform}, dry_run={dry_run}）", "P7")


@app.command("probe")
def leads_probe(
    serial: Annotated[
        str | None, typer.Option("--serial", help="ADB 设备序列号（多设备时用）")
    ] = None,
) -> None:
    """dump 当前屏幕的控件层级树，自检 locator（P7，需模拟器 + [emulator] extra）。"""
    not_implemented(f"jh leads probe（serial={serial}）", "P7")


@app.command("capture")
def leads_capture(
    label: Annotated[
        str | None, typer.Option("--label", help="这次抓屏的备注，如 小红书-内推")
    ] = None,
) -> None:
    """人工操作模拟器，按回车抓一屏（截图 + 取文字）（P7）。"""
    not_implemented(f"jh leads capture（label={label}）", "P7")


@app.command("export")
def leads_export(
    out: Annotated[str | None, typer.Option("--out", help="输出目录（默认 output/）")] = None,
    fmt: Annotated[str, typer.Option("--format", help="导出格式：md / csv")] = "md",
) -> None:
    """导出官网投递清单（按 source_type 分组）（P7）。"""
    not_implemented(f"jh leads export（out={out}, format={fmt}）", "P7")

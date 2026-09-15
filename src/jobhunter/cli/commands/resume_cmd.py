"""`jh resume` —— 简历导入、列表、拆分方向（P1 实现，本阶段为占位）。

P1 的最终形状（现在先定下来，P1 只填实现，不改 CLI 形状）：

    jh resume add <path> --direction "AI Agent" --name "主简历"
        用 python-docx 读 docx → 结构化 JSON 存 resumes.content_json
        并计算 content_hash（LLM 缓存 key 要用它）
    jh resume list
    jh resume split <resume_id>
        帮我这个"只有 1 份、没分方向"的人拆成
        「AI Agent 方向 / 前端方向」：输出哪些项目/技能该前置、哪些该弱化
        （**只给建议不自动改写**，人工确认后再生成 docx）

★ 隐私：简历只存本地库；日志脱敏由 P0 的 Filter 兜底，P1 再加针对性测试。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from jobhunter.cli.common import not_implemented

app = typer.Typer(name="resume", help="简历：导入解析 / 列表 / 拆分方向（P1）")


@app.command("add")
def resume_add(
    path: Annotated[Path, typer.Argument(help="简历文件路径（.docx）")],
    direction: Annotated[
        str | None, typer.Option("--direction", "-d", help="求职方向，如 AI Agent")
    ] = None,
    name: Annotated[str | None, typer.Option("--name", "-n", help="简历名，如 主简历")] = None,
) -> None:
    """导入简历并解析成结构化数据（P1）。"""
    not_implemented(f"jh resume add（导入 {path.name}，方向={direction}，名称={name}）", "P1")


@app.command("list")
def resume_list() -> None:
    """列出已导入的简历（P1）。"""
    not_implemented("jh resume list（列出简历）", "P1")


@app.command("split")
def resume_split(
    resume_id: Annotated[int, typer.Argument(help="简历 ID（见 jh resume list）")],
) -> None:
    """拆分方向建议：哪些项目/技能该前置、哪些该弱化（P1）。"""
    not_implemented(f"jh resume split {resume_id}（拆分方向建议）", "P1")

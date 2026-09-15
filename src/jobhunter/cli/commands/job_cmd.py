"""`jh job` —— 岗位录入（P2 实现，本阶段为占位）。

★ 主路径是「人给链接 → 工具抓详情」，不是自动搜索翻页。
  原因（评审结论）：BOSS 有 `__zp_stoken__` 签名 + 字体反爬，自动搜索大概率中期撞墙；
  而"人给链接"①风险降一个数量级 ②工期砍半 ③更贴合真实工作流（本来就是刷到岗位才投）。

P2 的最终形状：

    jh job add <url>              主路径：粘贴岗位链接 → 抓详情 → 入库
    jh job add --file jobs.txt    批量：一行一个 URL，逐个抓，失败跳过并记录
    jh job add --manual           兜底：手动粘贴 JD 文本（网络不通也能用）
    jh job list [--status] [--platform]
    jh job skip <job_id>

★ 去重两层（P2 实现，规则见 docs/data_model.md 的 jobs 一节）：
    ① platform_job_id 精确匹配（最可靠）
    ② dedup_norm_hash（公司+职位+城市 归一化）
  **命中已有记录时只更新 last_seen_at 等非人工字段，
    绝不覆盖 status / match_score / matched_resume_id 等决策字段。**
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from jobhunter.cli.common import not_implemented

app = typer.Typer(name="job", help="岗位：录入 / 批量导入 / 手动粘贴 / 列表（P2）")


@app.command("add")
def job_add(
    url: Annotated[str | None, typer.Argument(help="岗位链接（主路径）")] = None,
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="批量文件：一行一个 URL")
    ] = None,
    manual: Annotated[
        bool, typer.Option("--manual", help="纯手动粘贴 JD 文本（网络不通也能用）")
    ] = False,
) -> None:
    """录入岗位：给链接 / 给文件 / 手动粘贴（P2）。"""
    target = url or (str(file) if file else None) or ("手动粘贴" if manual else "未指定")
    not_implemented(f"jh job add（目标：{target}）", "P2")


@app.command("list")
def job_list(
    status: Annotated[str | None, typer.Option("--status", "-s", help="按状态过滤")] = None,
    platform: Annotated[str | None, typer.Option("--platform", "-p", help="按平台过滤")] = None,
) -> None:
    """列出岗位（P2）。"""
    not_implemented(f"jh job list（status={status}, platform={platform}）", "P2")


@app.command("skip")
def job_skip(
    job_id: Annotated[int, typer.Argument(help="岗位 ID")],
) -> None:
    """把岗位标记为跳过（P2）。"""
    not_implemented(f"jh job skip {job_id}", "P2")

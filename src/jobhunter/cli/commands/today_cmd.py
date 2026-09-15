"""`jh today` —— 待投清单（P4 实现，本阶段为占位）。

P4 的最终形状：

    jh today --limit 20 [--min-score 60]
        按 match_score 降序 → 排除已投递 → **同公司同天不推多条**
        → 为每条生成开场白 → 写 applications(status=pending)
        → rich 表格：| 序 | 公司 | 职位 | 分数 | 用哪份简历 | 链接 | 开场白 |
    jh today --copy 3
        ★ 一键复制第 3 条开场白（省掉每天 20 次划选）。这个"序号"就是
          `jh apply 3=applied` 里用的序号 —— 两处序号必须一致，否则会打错标。
    jh today --check
        今天已投 X / 上限 N（**从 applications.applied_at 实时聚合**，不建计数表）

★ 开场白必须基于共享层的 JD 关键词 + 简历真实亮点：
    完整版 80-150 字 + 精简版 ≤60 字（BOSS 有字数限制）+ 禁用词表过滤套话。
"""

from __future__ import annotations

from typing import Annotated

import typer

from jobhunter.cli.common import not_implemented

app = typer.Typer(
    help="今日待投清单：打分排序 / 一键复制开场白 / 额度检查（P4）",
    # ★ 单命令应用必须用 callback + invoke_without_command（见 gap_cmd.py 的说明）：
    #   写成 @app.command() 的话，`jh today --limit 5` 会被当成
    #   "给 today 的**子命令**传 --limit" → 报 No such option。
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def today(
    limit: Annotated[int | None, typer.Option("--limit", "-n", help="最多显示几条")] = None,
    min_score: Annotated[
        int | None, typer.Option("--min-score", help="最低匹配分（默认取 config.threshold）")
    ] = None,
    copy: Annotated[
        int | None, typer.Option("--copy", help="把第 N 条的开场白复制到剪贴板")
    ] = None,
    check: Annotated[bool, typer.Option("--check", help="只看今天已投 X / 上限 N")] = False,
    export: Annotated[
        str | None, typer.Option("--export", help="导出格式：md / csv（写到 output/，只写新文件）")
    ] = None,
) -> None:
    """输出今日待投清单（P4）。"""
    not_implemented(
        f"jh today（limit={limit}, min_score={min_score}, copy={copy}, "
        f"check={check}, export={export}）",
        "P4",
    )

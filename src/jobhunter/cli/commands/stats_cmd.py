"""`jh stats` —— 渠道转化率与变体对比（P6 实现，本阶段为占位）。

P6 的最终形状：

    jh stats
        ① 平台转化率：投递 / 已读 / 回复 / 约面
           平台        投递  已读  回复  约面   回复率  约面率
           BOSS        45    20    6     3     13.3%   6.7%
           官网         8     3    1     0     12.5%   0%
        ② 开场白变体回复率（with_project vs without_project）
        ③ 岗位方向回音对比（AI Agent vs 前端 哪个方向回得多）

★★ 一条纪律：**只记录对比，不宣称统计显著性**（评审明确采纳）。
    原因：样本量太小（一个秋招可能就 100 次投递，分到 2 个变体各 50 次，
    回复事件可能只有个位数）。任何"变体 A 显著优于 B"的结论都是噪声。
    所以这里只打印数字和样本量，不生成"建议使用变体 A"这类伪结论。

★ 转化率口径必须写清楚（否则数字无法复现）：
    回复率 = replied 及之后所有状态的人数 / 投递总数
    约面率 = assessment 及之后所有状态的人数 / 投递总数
   （即"至少走到过这一步"，而不是"当前停在这一步"）
"""

from __future__ import annotations

from typing import Annotated

import typer

from jobhunter.cli.common import not_implemented

app = typer.Typer(
    help="统计：平台转化率 / 开场白变体 / 岗位方向回音（P6）",
    # ★ 单命令应用用 callback（原因见 gap_cmd.py 的说明）
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def stats(
    days: Annotated[int | None, typer.Option("--days", help="只统计最近 N 天（默认全部）")] = None,
    export: Annotated[
        str | None, typer.Option("--export", help="导出格式：md / csv（写到 output/）")
    ] = None,
) -> None:
    """输出转化率统计（P6）。"""
    not_implemented(f"jh stats（days={days}, export={export}）", "P6")

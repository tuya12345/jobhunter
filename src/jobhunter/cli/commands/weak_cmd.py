"""`jh weak` —— 薄弱点聚合（P6 实现，本阶段为占位）。

★ 价值：把"记录工具"变成"准备工具"。

P6 的最终形状：

    jh weak [--top 10]
        按 category（知识点）聚合，**按"没答上来"次数降序**，末尾给一句结论：

        知识点          没答上来  总题数  结论
        前端性能        3        4      🔴 最先补：出现频率最高且正确率最低
        数据库索引      2        3      🟡
        Agent 编排      1        5      🟢

★ 排序口径是"**没答上来次数**"（is_weak 计数）而不是"平均分"：
    平均分会被"只被问过一次且答得一般"的题拉平，
    而"同一个知识点连续三次答不上来"才是真正需要立刻补的信号。

★ 只统计 `source='recorded'`（真实被问的题）？
    不 —— 默认两者都统计，但**分开展示**。
    预测题答不上来说明"准备不足"，真实题答不上来说明"能力缺口"，含义不同。
"""

from __future__ import annotations

from typing import Annotated

import typer

from jobhunter.cli.common import not_implemented

app = typer.Typer(
    help="薄弱点：按知识点聚合、按没答上来次数排序（P6）",
    # ★ 单命令应用用 callback（原因见 gap_cmd.py 的说明）
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def weak(
    top: Annotated[int, typer.Option("--top", "-n", help="显示前 N 个知识点")] = 10,
    recorded_only: Annotated[
        bool,
        typer.Option("--recorded-only", help="只看真实被问到的题（排除 JD 预测题）"),
    ] = False,
) -> None:
    """聚合薄弱知识点（P6）。"""
    not_implemented(f"jh weak（top={top}, recorded_only={recorded_only}）", "P6")

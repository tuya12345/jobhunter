"""`jh question` —— 面试问题记录 + JD 预测题（P6 实现，本阶段为占位）。

P6 的最终形状：

    jh question add --interview <id> --q "..." --answer "..." \\
                    --rating bad --category "前端性能" --weak
    jh question predict <job_id>
        ★ 基于 JD 让模型猜 5 个可能被问的问题（source=predicted）
        → 让"记录工具"变成"准备工具"

★ `source` 字段（recorded / predicted）必须严格区分：
    `jh weak` 汇总的是"我最该补什么知识点"。
    如果把自己猜的题和真实被问的题混在一起统计，结论会完全失真
    —— 要么把"自己吓自己"当成真实缺口，要么漏掉真正答不上来的弱项。

★ 自评只有 3 档（bad / ok / good）：面试题样本量小，5 档全是噪声（评审采纳）。
"""

from __future__ import annotations

from typing import Annotated

import typer

from jobhunter.cli.common import not_implemented

app = typer.Typer(help="面试问题：记录 / 自评 / 按 JD 预测可能被问的题（P6）")


@app.command("add")
def question_add(
    interview: Annotated[int, typer.Option("--interview", "-i", help="面试 ID")],
    q: Annotated[str, typer.Option("--q", help="问题原文")],
    answer: Annotated[str | None, typer.Option("--answer", "-a", help="我的回答")] = None,
    rating: Annotated[
        str | None, typer.Option("--rating", "-r", help="自评：bad / ok / good")
    ] = None,
    category: Annotated[
        str | None, typer.Option("--category", "-c", help="知识点分类，如 前端性能")
    ] = None,
    weak: Annotated[
        bool, typer.Option("--weak", help="标记为没答上来（jh weak 按次数排序）")
    ] = False,
    note: Annotated[str | None, typer.Option("--note", help="备注")] = None,
) -> None:
    """记录一道面试题（P6）。"""
    not_implemented(
        f"jh question add（interview={interview}, q={q!r}, answer={answer!r}, "
        f"rating={rating}, category={category}, weak={weak}, note={note}）",
        "P6",
    )


@app.command("predict")
def question_predict(
    job_id: Annotated[int, typer.Argument(help="岗位 ID")],
    count: Annotated[int, typer.Option("--count", "-n", help="预测几道题")] = 5,
) -> None:
    """按 JD 预测可能被问的问题（source=predicted）（P6）。"""
    not_implemented(f"jh question predict {job_id}（预测 {count} 道题）", "P6")

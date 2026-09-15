"""`jh match` —— 两级匹配打分（P3 实现，本项目**核心**，本阶段为占位）。

P3 的最终形状：

    jh match run [--top 30] [--threshold 60]
        规则层（0 成本）先算全部 → 只对 rule_score Top N 走 LLM 精评
        （asyncio.Semaphore 并发 + 边跑边写库 + 统计 token/成本）
    jh match explain <job_id>
        ★ 打印各维度得分明细。**调权重靠它**（评审原话："否则调权重无法进行"）
    jh match set <job_id> --resume <id> --verdict skip|queue
        人工裁决：覆盖模型判断

三维度与默认权重（config.yaml 可调）：
    岗位方向 30% ｜ 经验年限/是否应届 30% ｜ 技能交集 40%
    ★ 硬条件不满足是「大幅降分」而非「直接淘汰」—— 应届生常能投 3 年以下的岗。
"""

from __future__ import annotations

from typing import Annotated

import typer

from jobhunter.cli.common import not_implemented

app = typer.Typer(name="match", help="匹配打分：跑分 / 明细解释 / 人工裁决（P3）")


@app.command("run")
def match_run(
    top: Annotated[int | None, typer.Option("--top", help="LLM 精评取规则分前 N 条")] = None,
    threshold: Annotated[
        int | None, typer.Option("--threshold", help="分数线（默认取 config）")
    ] = None,
) -> None:
    """跑两级匹配打分（P3）。"""
    not_implemented(f"jh match run（top={top}, threshold={threshold}）", "P3")


@app.command("explain")
def match_explain(
    job_id: Annotated[int, typer.Argument(help="岗位 ID")],
) -> None:
    """打印某个岗位的分维度得分明细（P3）。"""
    not_implemented(f"jh match explain {job_id}（各维度得分明细）", "P3")


@app.command("set")
def match_set(
    job_id: Annotated[int, typer.Argument(help="岗位 ID")],
    resume: Annotated[int | None, typer.Option("--resume", "-r", help="指定用哪份简历")] = None,
    verdict: Annotated[
        str | None, typer.Option("--verdict", help="人工裁决：skip（不投）/ queue（要投）")
    ] = None,
) -> None:
    """人工覆盖模型判断（P3）。"""
    not_implemented(f"jh match set {job_id}（resume={resume}, verdict={verdict}）", "P3")

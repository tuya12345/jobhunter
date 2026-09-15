"""`jh tailor` —— AI 定制简历（P5 实现，本阶段为占位）。

★ 起点降级（评审建议）：**先做 `tailor brief`**，成本极低、立刻有用。

P5 的最终形状：

    第一步（轻量）
        jh tailor brief <job_id>
            只输出"重点讲哪 3 个项目、突出哪 2 个技能、要避开什么"，
            **不做任何改写** → 让我在面试时知道怎么讲

    第二步（完整版）
        jh tailor analyze <job_id>     生成字段级建议（结构化 JSON，逐条审核）
        jh tailor diff <version_id>    看建议的前后对比
        jh tailor approve <version_id> 逐条批准
        jh tailor export <job_id>      导出 docx（**只写新文件**，原文件 hash 不变）

★★ FidelityGuard（P5 的核心，用**代码**保证不编造，而不是靠提示词祈祷）：

    FidelityGuard.check(original, generated) -> FidelityReport

    五个确定性检查项：
      1. 命名实体白名单：生成文本里的公司/技术/学校名必须在原简历出现过
         （或命中 config 的 learning_whitelist → 允许但标注"在学习中"）
      2. **数字守恒**：出现的数字（年份/百分比/数量）必须能在原简历找到对应
         → 专门挡住"3 个模块 → 10 个模块""30% → 300%"
      3. 时间守恒：日期/月份/年限不允许新增或修改
      4. 技能词表校验：不允许出现简历技能表 + 白名单之外的技能词
      5. 长度上限：不超过原文 150%

    在【生成】【approve】【export】**三处强制调用**，违规即拒绝写入。

★ 测试策略（很重要）：**喂恶意 fixture 做确定性断言**
  （含 Kubernetes / 300% / 10个模块 / 2024年 → 断言全部被拦）。
  ⚠️ 绝不写"调真实 LLM 再断言输出里没有 XX"——那测的是模型顺从性，
     结果不确定会导致 CI 红灯，最后一定会被删掉（评审明确指出的坑）。
"""

from __future__ import annotations

from typing import Annotated

import typer

from jobhunter.cli.common import not_implemented

app = typer.Typer(help="定制简历：brief（讲什么）→ analyze → approve → export（P5）")


@app.command("brief")
def tailor_brief(
    job_id: Annotated[int, typer.Argument(help="岗位 ID")],
) -> None:
    """只输出"重点讲哪 3 个项目、突出哪 2 个技能"（P5 第一步，不做改写）。"""
    not_implemented(f"jh tailor brief {job_id}（重点讲什么）", "P5")


@app.command("analyze")
def tailor_analyze(
    job_id: Annotated[int, typer.Argument(help="岗位 ID")],
    resume: Annotated[int | None, typer.Option("--resume", "-r", help="用哪份简历")] = None,
) -> None:
    """生成字段级修改建议（结构化 JSON，逐条审核）（P5）。"""
    not_implemented(f"jh tailor analyze {job_id}（resume={resume}）", "P5")


@app.command("diff")
def tailor_diff(
    version_id: Annotated[int, typer.Argument(help="定制版本 ID")],
) -> None:
    """查看某版建议的前后对比（P5）。"""
    not_implemented(f"jh tailor diff {version_id}", "P5")


@app.command("approve")
def tailor_approve(
    version_id: Annotated[int, typer.Argument(help="定制版本 ID")],
    all_: Annotated[bool, typer.Option("--all", help="批准全部建议")] = False,
) -> None:
    """逐条批准建议（approve 前会再跑一次 FidelityGuard）（P5）。"""
    not_implemented(f"jh tailor approve {version_id}（all={all_}）", "P5")


@app.command("export")
def tailor_export(
    job_id: Annotated[int, typer.Argument(help="岗位 ID")],
    out: Annotated[str | None, typer.Option("--out", help="输出路径（默认 output/）")] = None,
) -> None:
    """导出定制简历 docx（**只写新文件**，并校验原文件 hash 不变）（P5）。"""
    not_implemented(f"jh tailor export {job_id}（out={out}）", "P5")

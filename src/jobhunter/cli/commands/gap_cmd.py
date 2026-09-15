"""`jh gap` —— 反向技能缺口分析（P3 实现，本阶段为占位）。

★ 这是"简历不匹配"痛点的**真正答案**（产品视角评审的补充，0 LLM 成本）。

它和"逐岗告诉你缺什么"的区别：
    逐岗输出 → 你看到的是 100 条零散的"缺 LangChain、缺 Docker、缺 RAG"，
              根本不知道先补哪个；
    jh gap   → 跨所有岗位聚合，直接告诉你：

        | 技能/要求  | 出现在 78% 的 JD 里 | 我的简历 | 缺口优先级 |
        | RAG       | 78%                | ✅ 有    | —         |
        | LangChain | 65%                | ❌ 无    | 🔴 高      |
        | Docker    | 52%                | ❌ 无    | 🟡 中      |

    → 答案从"这个岗位我缺 X"变成"**我该补什么**"。

实现要点（P3）：
    · 数据源是 jobs.jd_keywords_json（共享中间层产物，不用重复解析 JD）
    · 是否"我有"要经过技能归一化 + 英文词边界（Java ≠ JavaScript）
    · 纯本地聚合，不调 LLM

★ Typer 结构说明（踩过的坑，写下来免得再犯）：
  gap 只有一个动作、没有子命令。如果写成 `@app.command()` 的单命令应用，
  再 `app.add_typer(...)` 注册，Typer 会报
  "The 'callback' parameter is not supported by Typer when using `add_typer` without a name"。
  正确做法就是下面这样：**用 callback + `invoke_without_command=True`**，
  由主 app 通过 `add_typer(app, name="gap")` 注册。`jh apply` 同理。
"""

from __future__ import annotations

from typing import Annotated

import typer

from jobhunter.cli.common import not_implemented

app = typer.Typer(
    help="反向技能缺口分析：跨岗位聚合，告诉你该补什么（P3）",
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def gap(
    top: Annotated[int, typer.Option("--top", "-n", help="只显示缺口优先级最高的 N 项")] = 20,
    min_ratio: Annotated[
        float,
        typer.Option("--min-ratio", help="只统计出现在至少这个比例（0-1）的 JD 里的技能"),
    ] = 0.0,
) -> None:
    """跨所有岗位聚合技能缺口，输出"该补什么"的优先级表（P3）。"""
    not_implemented(f"jh gap（top={top}, min_ratio={min_ratio}）", "P3")

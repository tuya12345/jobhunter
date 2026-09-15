"""`jh demo` —— 假数据全流程演示（P9 实现，本阶段为占位）。

P9 的最终形状：

    jh demo
        用**内置假数据** + **独立/内存库**跑通全流程：
        录入岗位 → 打分 → 出清单 → 写开场白 → 打标跟进 → 统计

★★ 最重要的一条：**绝不污染真库**。
     demo 一旦写进 data/jobhunter.db，我的真实投递记录就被假数据混进来了，
     而"统计转化率"这类功能会立刻失去意义。
     所以 demo 必须用独立库（内存库或临时文件），且退出即销毁。

★ 这也是开源项目的门面：别人 clone 下来跑 `jh demo` 就能看到全部能力，
  不需要有 DeepSeek Key、不需要真去投简历。
"""

from __future__ import annotations

from typing import Annotated

import typer

from jobhunter.cli.common import not_implemented

app = typer.Typer(
    help="演示：用假数据 + 独立库跑通全流程（绝不污染真库，P9）",
    # ★ 单命令应用用 callback（原因见 gap_cmd.py 的说明）
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def demo(
    keep: Annotated[
        bool, typer.Option("--keep", help="保留演示库文件（默认用内存库，退出即销毁）")
    ] = False,
) -> None:
    """跑一遍完整流程演示（P9）。"""
    not_implemented(f"jh demo（keep={keep}）", "P9")

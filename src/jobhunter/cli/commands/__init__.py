"""commands：命令集中注册表。

为什么要有这个文件：如果 app.py 直接 import 19 个命令模块再逐个 add_typer，
app.py 会变成一个既长又杂的"装配车间"。这里把它收成一张**表**：

    COMMANDS = [("init", init_app), ("resume", resume_app), ...]

app.py 只写一个循环注册。加/删命令只改这张表，app.py 不用动。

★ 顺序即注册顺序 —— 按"使用频率 + 工作流顺序"排，
让最常见的命令排在前面（用户第一次打开就知道从哪开始）。

★★ **命令名必须在这里显式给出**（踩过的坑，值200字）：
   Typer 的 `add_typer(app)` 如果**不传 name**，它会把子命令**摊平到上一层**。
   更糟的是它只发一条 UserWarning 就继续跑，结果是 `jh --help` 里
   出现 add / list / export / compare 这些孤立的子命令，
   而 `jh today`、`jh weak` 这些本该存在的命令**整体消失** ——
   极易被误当成"还没实现"。所以这里用显式名字，并配测试守住。
"""

from __future__ import annotations

import typer

from jobhunter.cli.commands.apply_cmd import app as apply_app
from jobhunter.cli.commands.assessment_cmd import app as assessment_app
from jobhunter.cli.commands.cal_cmd import app as cal_app
from jobhunter.cli.commands.demo_cmd import app as demo_app
from jobhunter.cli.commands.followup_cmd import app as followup_app
from jobhunter.cli.commands.gap_cmd import app as gap_app
from jobhunter.cli.commands.init_cmd import app as init_app
from jobhunter.cli.commands.interview_cmd import app as interview_app
from jobhunter.cli.commands.job_cmd import app as job_app
from jobhunter.cli.commands.leads_cmd import app as leads_app
from jobhunter.cli.commands.match_cmd import app as match_app
from jobhunter.cli.commands.offer_cmd import app as offer_app
from jobhunter.cli.commands.question_cmd import app as question_app
from jobhunter.cli.commands.report_cmd import app as report_app
from jobhunter.cli.commands.resume_cmd import app as resume_app
from jobhunter.cli.commands.stats_cmd import app as stats_app
from jobhunter.cli.commands.tailor_cmd import app as tailor_app
from jobhunter.cli.commands.today_cmd import app as today_app
from jobhunter.cli.commands.weak_cmd import app as weak_app

#: (命令名, 子应用) —— 顺序 = 注册顺序 = 工作流顺序
COMMANDS: list[tuple[str, typer.Typer]] = [
    ("init", init_app),  # 建库 + 迁移（唯一在 P0 真正实现的命令）
    ("resume", resume_app),  # P1 简历
    ("job", job_app),  # P2 岗位录入
    ("match", match_app),  # P3 匹配打分
    ("gap", gap_app),  # P3 反向技能缺口
    ("today", today_app),  # P4 待投清单
    ("apply", apply_app),  # P4 / P4.5 投递与零手打跟进
    ("interview", interview_app),  # P6 面试
    ("question", question_app),  # P6 面试问题
    ("assessment", assessment_app),  # P6 笔试/测评
    ("offer", offer_app),  # P6 Offer 比较
    ("weak", weak_app),  # P6 薄弱点
    ("stats", stats_app),  # P6 转化率
    ("report", report_app),  # P6 周报
    ("followup", followup_app),  # P6 追问话术
    ("cal", cal_app),  # P6 日程导出
    ("tailor", tailor_app),  # P5 定制简历
    ("leads", leads_app),  # P7 官网线索
    ("demo", demo_app),  # P9 假数据全流程
]

#: 全部命令名（测试用它断言 `jh --help` 的完整性）
COMMAND_NAMES: tuple[str, ...] = tuple(name for name, _ in COMMANDS)

#: 兼容别名（早期写法）
COMMAND_APPS: list[typer.Typer] = [application for _, application in COMMANDS]

__all__ = ["COMMAND_APPS", "COMMAND_NAMES", "COMMANDS"]

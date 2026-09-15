"""顶部提醒服务：**P0 占位**，P4.5 实现真实统计。

★ 为什么 P0 就把这个"空壳"建起来：
   评审最狠的一条批评是"投完就忘，一天敲 20 次命令 → 第三天就弃用"。
   对应的解法就是**每条 jh 命令开头固定显示待处理事项**，
   让用户"什么都不想敲的时候，工具依然在产生价值"。

   这个能力的实现位置（Typer 全局 callback）如果在 P0 不定下来，
   等 P4.5 再往 19 个命令里加"开头打印"就会变成 19 处重复代码。
   所以 P0 先把"调用点"固定好，P4.5 只填 get_reminders 的实现。

P4.5 将返回的四类统计（都已由数据模型支持）：
    · 投后 3 天未跟进 / 7 天无动静 / 14 天建议放弃 → applications.applied_at
    · 本周面试 N 场                              → interviews.scheduled_at
    · 笔试即将截止                               → assessments.deadline_at
    · offer 签约截止                             → offers.deadline_at
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["ReminderSummary", "format_reminders", "get_reminders"]


@dataclass
class ReminderSummary:
    """一次提醒汇总。

    用 dataclass 而不是 dict：字段拼错会在构造时就报错，
    而不是到打印时才发现少了一个键（P4.5 会觉得这份体贴很值）。
    """

    # 投后 N 天未跟进（3 天 / 7 天 / 14 天）
    stale_followup: list[str] = field(default_factory=list)
    # 本周面试安排
    upcoming_interviews: list[str] = field(default_factory=list)
    # 即将截止的笔试/测评
    assessment_deadlines: list[str] = field(default_factory=list)
    # 即将截止的 offer 签约
    offer_deadlines: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """没有任何待处理事项时为 True（此时不打印任何内容，避免噪音）。"""
        return not (
            self.stale_followup
            or self.upcoming_interviews
            or self.assessment_deadlines
            or self.offer_deadlines
        )


def get_reminders() -> ReminderSummary:
    """取当前待处理提醒。

    ★ P0 阶段返回**空**汇总（不查库）—— 因为：
      1. 业务查询逻辑属于 P4.5，放在这里会提前写业务代码；
      2. 库还没建时（首次运行）不能报错，CLI 任何命令都该能用。
    """
    return ReminderSummary()


def format_reminders(summary: ReminderSummary) -> list[str]:
    """把汇总格式化成终端行（每行一个 emoji 前缀）。

    P0：统一提示"P4.5 上线"。
    P4.5 会改成按四类统计分别输出，例如：

        📌 待处理：投后 3 天未跟进 5 条 ｜ 投后 7 天无动静 3 条 ｜ 投后 14 天建议放弃 1 条
        📅 本周面试 2 场（9-10 14:00 字节 · 9-12 10:00 腾讯）
        ⏳ 笔试截止 1 条（9-11 饿了么）
    """
    if summary.is_empty:
        return ["📌 待处理提醒将在 P4.5 上线（零手打跟进：批量打标 + 顶部固定提醒）"]
    return []

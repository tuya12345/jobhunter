"""顶部提醒：**每条 jh 命令开头固定显示待处理事项**（P4.5 实现）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 这是"投完就忘"痛点的正面解法，也是本项目最容易被自己违背的设计             ║
╚══════════════════════════════════════════════════════════════════════════╝

评审原话：*"一天 20 个岗位要敲 20 次命令 → 会第 3 天弃用"*。

所以目标不是"提供一个 `jh reminder` 命令让我记得敲"，而是
**"我什么都不想敲的时候，工具依然在产生价值"** ——
把它挂在 Typer 的全局 callback 上（`cli/app.py`），于是**任何** jh 命令
开头都会顺带告诉我有多少事没处理。

★★ 三条实现纪律（每一条都是为了不让这个功能变成噪音或崩溃源）

    ① **空的时候一个字都不打印**。每天都被"你没有被提醒"刷屏，第三天就会
       把整个提醒关掉（然后连真有提醒时也看不见了）。
    ② **绝不能因为提醒失败而让命令失败**。库还没建（首次 `jh init`）、表被删、
       库文件损坏……任何情况都退化成"没有提醒"，并在日志里留一行。
       提醒是**附属信息**，它没有资格阻断主命令。
    ③ **快**。它在每条命令前都跑一次，所以只做 4 个带索引的小查询。

★ 隐私：这里打印的是**公司名/职位名/时间**（用户本来就要看的信息），
  不含简历正文、联系方式。日志同样不打简历原文。

★ 为什么"投后 N 天"是**三个累计的桶**而不是互斥区间：
  「3 天未跟进」的岗位到第 8 天同时属于 7 天桶 —— 这是有意的。
  用户看到的是"有几条到了该处理的时间点"，而不是"恰好第 3 天的那几条"
  （后者要求他每天都在同一时刻看，不现实）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunter.config.settings import AppSettings, get_settings
from jobhunter.models import Application, Assessment, Interview, Job, Offer
from jobhunter.services import apply_svc
from jobhunter.utils.logging import get_logger
from jobhunter.utils.time import days_since, now_utc, to_local

logger = get_logger(__name__)

__all__ = [
    "ASSESSMENT_LOOKAHEAD_DAYS",
    "FOLLOWUP_DAYS",
    "ReminderSummary",
    "format_reminders",
    "get_reminders",
]

#: 投后多少天该有动作（三个累计的桶，见模块 docstring）
FOLLOWUP_DAYS: tuple[int, int, int] = (3, 7, 14)
#: 笔试/测评的"即将截止"窗口（天）
ASSESSMENT_LOOKAHEAD_DAYS = 3
#: Offer 签约的"即将截止"窗口（天）
OFFER_LOOKAHEAD_DAYS = 3

#: 还算"在等对方回应"的投递状态（进了 interview/offer 就不该再催了）。
#: ★★ ISSUE #49：这里**不再自己写一份字面量**，直接引用
#:   `apply_svc.WAITING_STATUSES` —— 顶部提醒与 `jh apply --pending` 必须
#:   用**同一个定义**判定"该催谁"。两处各写一份的结果是：
#:   刚约上面试的岗位在提醒里被正确地跳过，却在待处理清单里被建议"放弃"。
_WAITING_STATUSES = apply_svc.WAITING_STATUSES


@dataclass
class ReminderSummary:
    """一次提醒汇总。

    用 dataclass 而不是 dict：字段拼错会在构造时就报错，
    而不是到打印时才发现少了一个键。
    """

    #: 投后 N 天仍未跟进的**条数**（与 `FOLLOWUP_DAYS` 一一对应）
    stale_followup: list[int] = field(default_factory=lambda: [0, 0, 0])
    #: 本周面试安排（`9-10 14:00 公司`）
    upcoming_interviews: list[str] = field(default_factory=list)
    #: 即将截止的笔试/测评（`9-11 公司`）
    assessment_deadlines: list[str] = field(default_factory=list)
    #: 即将截止的 offer 签约（`9-12 公司`）
    offer_deadlines: list[str] = field(default_factory=list)

    @property
    def has_stale(self) -> bool:
        return any(self.stale_followup)

    @property
    def is_empty(self) -> bool:
        """没有任何待处理事项时为 True（此时**不打印任何内容**，避免噪音）。"""
        return not (
            self.has_stale
            or self.upcoming_interviews
            or self.assessment_deadlines
            or self.offer_deadlines
        )

    @property
    def actionable_count(self) -> int:
        """需要我动手的条目数（用来决定要不要提示下一步命令）。"""
        return (
            sum(self.stale_followup)
            + len(self.upcoming_interviews)
            + len(self.assessment_deadlines)
            + len(self.offer_deadlines)
        )


# ===========================================================================
# 采集
# ===========================================================================
def _stale_counts(session: Session, *, now: datetime) -> list[int]:
    """按 `FOLLOWUP_DAYS` 统计"投出去之后一直没动静"的条数。

    判定口径（刻意保守，宁可少提醒也不要误报）：
        · 状态仍是 `applied` / `viewed` —— 已经进入面试/Offer 流程的不该再催；
        · `applied_at` 到今天的**自然日**差 ≥ 阈值。
    """
    rows = session.execute(
        select(Application.applied_at).where(
            Application.applied_at.is_not(None),
            Application.status.in_(_WAITING_STATUSES),
        )
    ).scalars()
    ages = [
        age for age in (days_since(applied_at, now=now) for applied_at in rows) if age is not None
    ]
    return [sum(1 for age in ages if age >= threshold) for threshold in FOLLOWUP_DAYS]


def _upcoming_interviews(session: Session, *, now: datetime) -> list[str]:
    """本周（今天起 7 个自然日内）还没面的面试。"""
    horizon = now + timedelta(days=7)
    rows = session.execute(
        select(Interview.scheduled_at, Job.company, Job.title)
        .join(Application, Interview.application_id == Application.id)
        .join(Job, Application.job_id == Job.id)
        .where(
            Interview.status == "待面",
            Interview.scheduled_at.is_not(None),
            Interview.scheduled_at >= now,
            Interview.scheduled_at <= horizon,
        )
        .order_by(Interview.scheduled_at)
    ).all()
    out: list[str] = []
    for scheduled_at, company, title in rows:
        local = to_local(scheduled_at)
        label = f"{local.month}-{local.day} {local:%H:%M} {company or title or '（未知岗位）'}"
        out.append(label)
    return out


def _upcoming_assessments(session: Session, *, now: datetime) -> list[str]:
    """即将截止、且还没做的笔试/测评。"""
    horizon = now + timedelta(days=ASSESSMENT_LOOKAHEAD_DAYS)
    rows = session.execute(
        select(Assessment.deadline_at, Assessment.kind, Job.company, Job.title)
        .join(Application, Assessment.application_id == Application.id)
        .join(Job, Application.job_id == Job.id)
        .where(
            Assessment.status == "待做",
            Assessment.deadline_at.is_not(None),
            Assessment.deadline_at >= now,
            Assessment.deadline_at <= horizon,
        )
        .order_by(Assessment.deadline_at)
    ).all()
    out: list[str] = []
    for deadline_at, kind, company, title in rows:
        local = to_local(deadline_at)
        out.append(f"{local.month}-{local.day} {company or title or kind}")
    return out


def _upcoming_offers(session: Session, *, now: datetime) -> list[str]:
    """即将到签约截止时间、且还没定的 offer。"""
    horizon = now + timedelta(days=OFFER_LOOKAHEAD_DAYS)
    rows = session.execute(
        select(Offer.deadline_at, Offer.company)
        .where(
            Offer.status == "待定",
            Offer.deadline_at.is_not(None),
            Offer.deadline_at >= now,
            Offer.deadline_at <= horizon,
        )
        .order_by(Offer.deadline_at)
    ).all()
    out: list[str] = []
    for deadline_at, company in rows:
        local = to_local(deadline_at)
        out.append(f"{local.month}-{local.day} {company or '（未写公司）'}")
    return out


def collect_reminders(
    session: Session, *, settings: AppSettings, now: datetime | None = None
) -> ReminderSummary:
    """从一个已有 session 采集提醒（`get_reminders` 的纯函数内核，测试直接用它）。"""
    moment = now or now_utc()
    return ReminderSummary(
        stale_followup=_stale_counts(session, now=moment),
        upcoming_interviews=_upcoming_interviews(session, now=moment),
        assessment_deadlines=_upcoming_assessments(session, now=moment),
        offer_deadlines=_upcoming_offers(session, now=moment),
    )


def get_reminders(
    *,
    session: Session | None = None,
    settings: AppSettings | None = None,
    now: datetime | None = None,
) -> ReminderSummary:
    """取当前待处理提醒（CLI 全局 callback 每条命令都会调它）。

    Args:
        session: 测试注入。不传就自己开一个**短连接**（用完即关）。
        now: 测试注入"现在"。

    ★ 任何失败都退化成"空提醒"：库没建、表被删、文件损坏……
      见模块 docstring 的纪律 ② —— 提醒没有资格让主命令失败。
    """
    settings = settings or get_settings()
    if session is not None:
        try:
            return collect_reminders(session, settings=settings, now=now)
        except Exception as exc:  # noqa: BLE001
            logger.debug("采集提醒失败（忽略）：%s", exc)
            return ReminderSummary()

    # 延迟导入：`cli/*` 会 import 本模块，反向 import 顶层化会形成环
    from jobhunter.storage.db import database_ready, get_session_factory

    try:
        if not database_ready(settings.database_url):
            return ReminderSummary()
        engine, factory = get_session_factory(settings.database_url)
    except Exception as exc:  # noqa: BLE001
        logger.debug("打开数据库失败（提醒跳过）：%s", exc)
        return ReminderSummary()

    try:
        with factory() as short_session:
            return collect_reminders(short_session, settings=settings, now=now)
    except Exception as exc:  # noqa: BLE001
        logger.debug("采集提醒失败（忽略）：%s", exc)
        return ReminderSummary()
    finally:
        engine.dispose()


# ===========================================================================
# 渲染
# ===========================================================================
def format_reminders(summary: ReminderSummary) -> list[str]:
    """把汇总格式化成终端行（每行一个 emoji 前缀；空则返回空列表）。

    输出形态与计划书【提示词 6】一致：

        📌 待处理：投后 3 天未跟进 5 条 ｜ 投后 7 天无动静 3 条 ｜ 投后 14 天建议放弃 1 条
        📅 本周面试 2 场（9-10 14:00 字节 · 9-12 10:00 腾讯）
        ⏳ 笔试截止 1 条（9-11 饿了么）

    ★ 只打印**非零**的部分：0 条的类别混在里面会让人每次都重新读一遍才能确认
      "今天没什么事"，那是纯噪音。
    """
    if summary.is_empty:
        return []

    lines: list[str] = []
    if summary.has_stale:
        parts = []
        labels = ("投后 3 天未跟进", "投后 7 天无动静", "投后 14 天建议放弃")
        for count, label in zip(summary.stale_followup, labels, strict=True):
            if count:
                parts.append(f"{label} {count} 条")
        lines.append("📌 待处理：" + " ｜ ".join(parts))
    if summary.upcoming_interviews:
        items = " · ".join(summary.upcoming_interviews[:3])
        lines.append(f"📅 本周面试 {len(summary.upcoming_interviews)} 场（{items}）")
    if summary.assessment_deadlines:
        items = " · ".join(summary.assessment_deadlines[:3])
        lines.append(f"⏳ 笔试截止 {len(summary.assessment_deadlines)} 条（{items}）")
    if summary.offer_deadlines:
        items = " · ".join(summary.offer_deadlines[:3])
        lines.append(f"🎉 Offer 签约截止 {len(summary.offer_deadlines)} 条（{items}）")

    # 只提示**下一步敲什么**，不教怎么用 —— 这条能力存在的意义就是"不用记命令"
    lines.append("（[green]jh apply --pending[/green] 看待处理清单并批量打标）")
    return lines

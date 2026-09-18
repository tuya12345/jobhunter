"""投递记录：`applications`（当前状态）+ `application_events`（append-only 事件流）。

★ 本模块只做三件事，都很小 —— 因为"投递状态"是**用户的资产**，
  逻辑越少越不容易写坏：

    ① 准备一条 pending 投递（`jh today` 生成开场白时写）
    ② 改状态（`jh apply <n> --status applied`），**每次都追加一条事件**
    ③ 数"今天投了几条"（**实时聚合**，不建计数表）

★ 为什么不建计数表：计数器和真实记录对不上是最难查的一类不一致
  （`models/application.py` 里写明了这条取舍）。日额度直接从
  `applications.applied_at` 聚合，永远与明细一致。

★ 事件流只允许 INSERT：UPDATE/DELETE 会破坏"历史不可篡改"，
  而统计口径的可复现性完全建立在这个前提上。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobhunter.models import Application, ApplicationEvent, Job
from jobhunter.models.constants import APPLICATION_STATUSES
from jobhunter.utils.io import to_json
from jobhunter.utils.logging import get_logger
from jobhunter.utils.time import days_since, now_utc, to_local

logger = get_logger(__name__)

__all__ = [
    "ApplicationError",
    "ApplyResult",
    "PendingRow",
    "count_applied_today",
    "ensure_pending_application",
    "get_latest_application",
    "list_actionable",
    "set_status",
]


class ApplicationError(ValueError):
    """投递状态操作失败（非法状态、找不到记录等）。"""


#: 状态 → 该状态对应的时间戳列名（打标时顺手补上，看板与统计都要用）
_STATUS_TIMESTAMP: dict[str, str] = {
    "applied": "applied_at",
    "viewed": "viewed_at",
    "replied": "replied_at",
}
#: 打标后同步到 `jobs.status` 的状态（job 的状态机只有 new/scored/queued/applied/skipped）
_JOB_STATUS_SYNC = {"applied": "applied", "rejected": "applied"}


@dataclass
class ApplyResult:
    """一次状态变更的结果（供 CLI 打印）。"""

    application_id: int
    job_id: int
    from_status: str
    to_status: str
    changed: bool


def get_latest_application(session: Session, job_id: int) -> Application | None:
    """取某个岗位最近的一条投递记录（一个岗位理论上只会有一条）。"""
    return session.scalars(
        select(Application).where(Application.job_id == job_id).order_by(Application.id.desc())
    ).first()


def ensure_pending_application(
    session: Session,
    job: Job,
    *,
    resume_id: int | None = None,
    greeting: str | None = None,
    greeting_variant: str | None = None,
) -> tuple[Application, bool]:
    """确保这个岗位有一条 pending 投递记录（幂等）。

    Returns:
        (记录, 是否新建)

    ★ 为什么幂等：`jh today` 会被反复执行（每次想看清单就跑一次）。
      不幂等的话，跑 3 次就有 3 条 pending，额度统计与"待处理列表"全部失真。
      判定口径：**同一个岗位只允许存在一条非终态的投递记录**。
    """
    existing = get_latest_application(session, job.id)
    if existing is not None:
        # 已有记录：只补空字段，绝不覆盖已有开场白（用户可能已经手改过）
        if greeting and not existing.greeting_text:
            existing.greeting_text = greeting
        if greeting_variant and not existing.greeting_variant:
            existing.greeting_variant = greeting_variant
        if resume_id is not None and existing.resume_version_id is None:
            # resume_version_id 是"定制版简历"（P5 才有），P4 先留空
            pass
        session.flush()
        return existing, False

    application = Application(
        job_id=job.id,
        platform=job.platform,
        channel="招聘网站",
        greeting_text=greeting,
        greeting_variant=greeting_variant,
        status="pending",
    )
    session.add(application)
    session.flush()
    return application, True


def set_status(
    session: Session,
    job_id: int,
    status: str,
    *,
    occurred_at: datetime | None = None,
    source: str = "jh apply",
    note: str | None = None,
    greeting_sent: bool | None = None,
    greeting_edited: bool | None = None,
) -> ApplyResult:
    """把某个岗位的投递记录改成新状态，并**追加一条事件**。

    Args:
        status: 必须是 `APPLICATION_STATUSES` 里的值 —— 写了不认识的状态要报错，
                而不是静默建一个野状态（那会让看板统计出现永远对不上的分类）。
        occurred_at: 业务时间（真实发生时间）。默认现在；补记时传历史时间。
        greeting_sent / greeting_edited: 闭环字段 —— 这个工具生成的话术到底有没有被用上。

    ★ 为什么 `from_status == to_status` 也算一次操作但不写事件：
      重复敲同一条命令是常见的手误，写一堆重复事件会污染时间轴；
      但返回值里标明 `changed=False`，让 CLI 能说清"本来就已是这个状态"。
    """
    if status not in APPLICATION_STATUSES:
        raise ApplicationError(f"状态 {status!r} 不认识。可用：{'、'.join(APPLICATION_STATUSES)}")

    application = get_latest_application(session, job_id)
    if application is None:
        # 还没生成过开场白就直接标"已投"是合法操作（用户可能在线下投过了）
        job = session.get(Job, job_id)
        if job is None:
            raise ApplicationError(f"找不到岗位 id={job_id}")
        application, _created = ensure_pending_application(session, job)

    from_status = application.status
    changed = from_status != status
    application.status = status
    when = occurred_at or now_utc()

    column = _STATUS_TIMESTAMP.get(status)
    if column and getattr(application, column) is None:
        setattr(application, column, when)
    if note:
        application.note = note
    if greeting_sent is not None:
        application.greeting_sent = greeting_sent
    if greeting_edited is not None:
        application.greeting_edited = greeting_edited

    if changed:
        session.add(
            ApplicationEvent(
                application_id=application.id,
                event_type="status_change",
                from_status=from_status,
                to_status=status,
                payload_json=to_json({"source": source}),
                occurred_at=when,
            )
        )
    # 标成"已投"但没有明说"话术发出去了没有" → 记一条 greeting_sent 事件，
    # 让闭环字段和时间轴都留下痕迹（统计"开场白到底有没有被用上"要靠它）。
    # ★ 必须挂在 `changed` 上：重复敲同一条命令不该再写一条 ——
    #   否则时间轴上会出现两条一模一样的"已投 + 话术已发"，几天后根本分不清
    #   哪条是真的（这个 bug 是 P4 验收时数事件数数出来的）。
    if changed and status == "applied" and greeting_sent is None and application.greeting_text:
        session.add(
            ApplicationEvent(
                application_id=application.id,
                event_type="greeting_sent",
                payload_json=to_json({"variant": application.greeting_variant}),
                occurred_at=when,
            )
        )

    job = session.get(Job, job_id)
    if job is not None and status in _JOB_STATUS_SYNC:
        # 岗位状态前进（不回退）：已 skipped 的岗位被标 applied 时也以 applied 为准，
        # 因为"真的投了"是更硬的事实。
        job.status = _JOB_STATUS_SYNC[status]

    session.flush()
    return ApplyResult(
        application_id=application.id,
        job_id=job_id,
        from_status=from_status,
        to_status=status,
        changed=changed,
    )


def count_applied_today(session: Session, *, now: datetime | None = None) -> int:
    """今天已经投了几条（按**北京时间自然日**算）。

    ★ 为什么按北京自然日而不是 UTC：用户说"今天投了 5 条"指的是他的今天。
      UTC 会比北京晚 8 小时，晚上 8 点后投的会被算到"明天"，额度就白算了。
    """
    local_now = to_local(now or now_utc())
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    start_utc = local_start.astimezone(local_now.tzinfo).astimezone(now_utc().tzinfo)
    end_utc = local_end.astimezone(local_now.tzinfo).astimezone(now_utc().tzinfo)

    total = session.scalar(
        select(func.count())
        .select_from(Application)
        .where(
            Application.applied_at.is_not(None),
            Application.applied_at >= start_utc,
            Application.applied_at < end_utc,
        )
    )
    return int(total or 0)


# ===========================================================================
# 待处理清单（`jh apply --pending`）
# ===========================================================================
@dataclass
class PendingRow:
    """一条"需要我动手"的投递。"""

    job_id: int
    application_id: int | None
    company: str
    title: str
    status: str
    #: 投出去几天了（没投则为 None）
    days: int | None
    #: 为什么它出现在这里（人话）
    reason: str

    @property
    def label(self) -> str:
        return f"{self.company or '—'} · {self.title or '（未写职位）'}"


def list_actionable(session: Session, *, now: datetime | None = None) -> list[PendingRow]:
    """列出"需要我动手"的投递：还没投的 + 投了 3 天以上没动静的。

    ★ 为什么把"还没投的"也算进来：`jh today` 生成了开场白就写了一条 `pending`，
      它代表"简历已经准备好、就差我点投递"。这正是最该被提醒的一类。

    ★ 排序：先按"紧急程度"（超过 14 天 > 7 天 > 3 天 > 还没投），
      再按投出时间从早到晚 —— 最该处理的排最前面，因为这是**行动清单**。
    """
    moment = now or now_utc()
    rows = session.execute(
        select(Application, Job)
        .join(Job, Application.job_id == Job.id)
        .where(Application.status.not_in(("offer", "rejected")))
        .order_by(Application.applied_at.is_(None).desc(), Application.applied_at)
    ).all()

    out: list[PendingRow] = []
    for application, job in rows:
        days = days_since(application.applied_at, now=moment) if application.applied_at else None
        if days is None:
            reason = "还没投（开场白已生成）"
        elif days >= 14:
            reason = f"投后 {days} 天没动静，建议放弃或换个渠道"
        elif days >= 7:
            reason = f"投后 {days} 天没动静"
        elif days >= 3:
            reason = f"投后 {days} 天，该跟进一下了"
        else:
            reason = f"投后 {days} 天"
        out.append(
            PendingRow(
                job_id=job.id,
                application_id=application.id,
                company=job.company or "",
                title=job.title or "",
                status=application.status,
                days=days,
                reason=reason,
            )
        )
    # 紧急度排序：None（没投）排最后，其余按天数降序
    out.sort(key=lambda row: (row.days is None, -(row.days or 0), row.job_id))
    return out

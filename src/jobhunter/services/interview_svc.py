"""面试场次：`interviews`（P6）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 这一层很薄，因为"面试"这件事本身没有复杂规则；难点全在**时间与归属**上      ║
╚══════════════════════════════════════════════════════════════════════════╝

    ① 面试挂在**投递**上（`interviews.application_id`），不是挂在岗位上。
       没有投递记录时自动建一条 pending —— 理由与笔试/Offer 一致：
       小公司常常"先约面、后补投递记录"，不该逼用户为了记一场面试先打标。
    ② 约上面之后，投递状态要前进到 `interview`。**只前进不回退**：
       补记一场已经面完的面试，不该把已经到 `offer` 的投递拽回来。
    ③ 时间一律**库里 UTC、界面北京时间**（`utils/time.py` 的两端转换）。

★ 为什么 `jh interview list` 默认按时间升序、`--upcoming` 只认 `待面`：
  这是一张"我接下来要干什么"的清单，不是历史档案。
  已面/出结果的场次仍然列出来（否则会以为记录丢了），但排在后面。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunter.models import Application, ApplicationEvent, Interview, Job
from jobhunter.models.constants import INTERVIEW_STATUSES
from jobhunter.services import apply_svc
from jobhunter.utils.io import to_json
from jobhunter.utils.logging import get_logger
from jobhunter.utils.time import days_between, now_utc

logger = get_logger(__name__)

__all__ = [
    "InterviewError",
    "InterviewRow",
    "add_interview",
    "list_interviews",
]


class InterviewError(ValueError):
    """面试登记失败（状态非法、岗位不存在等）。"""


@dataclass
class InterviewRow:
    """一行面试（带岗位信息，CLI 直接渲染）。"""

    interview: Interview
    job_id: int
    company: str
    title: str
    #: 距今天几天（负数 = 已经过去；None = 没填时间）
    days_until: int | None = None

    @property
    def label(self) -> str:
        return f"{self.company or '—'} · {self.title or '（未写职位）'}"


def _resolve_application(session: Session, job_id: int) -> tuple[Job, Application]:
    """取（或补建）这个岗位的投递记录（与 assessment/offer 同一套口径）。"""
    job = session.get(Job, job_id)
    if job is None:
        raise InterviewError(f"找不到岗位 id={job_id}")
    application = apply_svc.get_latest_application(session, job_id)
    if application is None:
        application, _created = apply_svc.ensure_pending_application(session, job)
    return job, application


def add_interview(
    session: Session,
    job_id: int,
    *,
    round_: str | None = None,
    scheduled_at: datetime | None = None,
    mode: str | None = None,
    link: str | None = None,
    location: str | None = None,
    status: str = "待面",
    note: str | None = None,
) -> Interview:
    """登记一场面试，并把投递推进到 `interview`。

    Args:
        scheduled_at: **UTC**（CLI 用 `parse_local_input` 把北京时间转成 UTC）。
        status: `待面` / `已面` / `出结果`（`INTERVIEW_STATUSES`）。

    Raises:
        InterviewError: 岗位不存在，或状态不认识。
    """
    if status not in INTERVIEW_STATUSES:
        raise InterviewError(f"状态 {status!r} 不认识。可用：{'、'.join(INTERVIEW_STATUSES)}")

    job, application = _resolve_application(session, job_id)
    when = scheduled_at or now_utc()

    interview = Interview(
        application_id=application.id,
        round=round_,
        scheduled_at=scheduled_at,
        mode=mode,
        meeting_link=link,
        location=location,
        status=status,
        note=note,
    )
    session.add(interview)
    session.flush()

    # 事件流：`interview_scheduled` 是"约面"这个动作的痕迹，
    # 时间轴（jh apply --timeline）与周报都靠它。
    session.add(
        ApplicationEvent(
            application_id=application.id,
            event_type="interview_scheduled",
            payload_json=to_json(
                {"round": round_ or "", "at": when.isoformat(), "mode": mode or ""}
            ),
            occurred_at=when,
        )
    )

    # 投递状态前进到 interview（只在"还没到更后面的阶段"时才动）。
    # ★ 不回退的判断放在这里而不是 set_status 里：set_status 是"我说了算"的显式命令，
    #   而这里是**登记面试这个动作的副作用**，副作用不该把用户明确设过的状态改回去。
    if application.status not in ("interview", "offer", "rejected"):
        apply_svc.set_status(session, job_id, "interview", source="jh interview add")

    session.flush()
    logger.debug("登记面试：job=%s round=%s status=%s", job_id, round_, status)
    return interview


def list_interviews(
    session: Session,
    *,
    upcoming: bool = False,
    now: datetime | None = None,
) -> list[InterviewRow]:
    """列出面试。

    - 默认：按时间升序（没填时间的排最后），已面/出结果的也列出来；
    - `upcoming=True`：只看 `待面` 且**时间未过**的场次（"我接下来要面什么"）。
    """
    moment = now or now_utc()
    rows = session.execute(
        select(Interview, Job)
        .join(Application, Interview.application_id == Application.id)
        .join(Job, Application.job_id == Job.id)
        .order_by(Interview.scheduled_at.is_(None), Interview.scheduled_at, Interview.id)
    ).all()

    out: list[InterviewRow] = []
    for interview, job in rows:
        if upcoming and not (
            interview.status == "待面"
            and interview.scheduled_at is not None
            and interview.scheduled_at >= moment
        ):
            continue
        out.append(
            InterviewRow(
                interview=interview,
                job_id=job.id,
                company=job.company or "",
                title=job.title or "",
                days_until=days_between(moment, interview.scheduled_at),
            )
        )
    return out

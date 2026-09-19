"""笔试 / 在线测评 / AI 面：`assessments`（P6）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 这张表存在的唯一理由是 `deadline_at` —— 笔试邀请通常只有 48-72 小时       ║
╚══════════════════════════════════════════════════════════════════════════╝

★ 为什么登记笔试时顺手把投递状态推进到 `assessment`：
  "收到笔试"本身就是投递进度的硬信号。如果还要用户自己再敲一次
  `jh apply --status assessment`，就是纯粹的多余动作 —— 而多一步就会漏一步。

★★ 只前进、不回退（本模块最重要的一条规则）
  如果投递已经走到 `interview` / `offer` / `rejected`，补登记一条笔试**不改状态**，
  也**不写 status_change 事件**。理由：用户补记的是**历史**信息，不该让
  "当前的进度认知"和顶部提醒跟着变 —— 面试提醒突然消失、笔试提醒突然冒出来，
  比不提醒更糟。这种情况只写一条 `assessment_invite` 事件留痕。

★ 两张表都只挂 `applications`（没有 job_id 列），所以按 `--job` 登记时：
  岗位不存在 → 抛 `AssessmentError`（CLI 退出码 1）；
  岗位存在但没有投递记录 → 自动补一条 `pending`（理由见 `_application_for`）。

★ 校验写在服务层而不是只写在 CLI：P8 的 API 会复用同一个函数，
  校验跟着业务走才不会出现"CLI 拦住了、API 没拦"的漏洞。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from jobhunter.models import Application, ApplicationEvent, Assessment, Job
from jobhunter.models.constants import APPLICATION_STATUSES, ASSESSMENT_KINDS, ASSESSMENT_STATUSES
from jobhunter.services import apply_svc
from jobhunter.services.reminder_svc import ASSESSMENT_LOOKAHEAD_DAYS
from jobhunter.utils.io import to_json
from jobhunter.utils.logging import get_logger
from jobhunter.utils.time import days_between, now_utc

logger = get_logger(__name__)

__all__ = [
    "UPCOMING_DAYS",
    "AssessmentError",
    "AssessmentRow",
    "add_assessment",
    "list_assessments",
]

#: "即将截止"的窗口（天）。★ 直接复用顶部提醒的口径，不另写一个 3：
#: 提醒说 3 天、列表说 2 天，是这类工具最难查的 bug —— 用户不会去比对两处代码，
#: 他只会得出"这工具的数字不可信"这个结论。
UPCOMING_DAYS = ASSESSMENT_LOOKAHEAD_DAYS

#: 登记笔试时**不允许**被回退到的状态。从 `APPLICATION_STATUSES` 里
#: "assessment" 之后的所有状态**推导**出来，而不是另抄一份名单 ——
#: 以后状态机加了一环（比如"体检"），这里自动跟上，不会漏。
_BEYOND_ASSESSMENT: frozenset[str] = frozenset(
    APPLICATION_STATUSES[APPLICATION_STATUSES.index("assessment") + 1 :]
)


class AssessmentError(ValueError):
    """笔试登记失败（类型/状态不认识、找不到岗位等）。"""


@dataclass
class AssessmentRow:
    """一条笔试记录 + 它所属岗位的信息（列表渲染要用的都在这里，CLI 不再查库）。"""

    assessment: Assessment
    job_id: int
    company: str
    title: str
    #: 距截止还有几个自然日（负数=已过期，None=没填截止时间）
    days_left: int | None

    @property
    def label(self) -> str:
        """`公司 · 职位` —— 合成一列，窄终端下不至于被拆成两行看不出来是一条。"""
        return f"{self.company or '—'} · {self.title or '（未写职位）'}"


# ===========================================================================
# 登记
# ===========================================================================
def add_assessment(
    session: Session,
    job_id: int,
    *,
    kind: str,
    deadline_at: datetime | None = None,
    invited_at: datetime | None = None,
    platform: str | None = None,
    status: str = "待做",
    note: str | None = None,
) -> Assessment:
    """登记一次笔试 / 测评 / AI 面，并把投递状态推进到 `assessment`。

    Args:
        deadline_at: UTC 时间（调用方用 `parse_local_input` 把用户的北京时间转好再传）。
        invited_at: 收到邀请的时间。★ **不填就留空，不自动补 now** ——
            "邀请时间"是事后可以核对的业务事实，猜一个假时间会让时间轴看起来
            很精确、实际是错的（这类"看起来对"的脏数据最难发现）。

    Raises:
        AssessmentError: 岗位不存在，或 kind / status 不在常量表里。

    ★ 为什么非法值要抛错而不是静默建一条野记录：
      看板与提醒都是按 `status == "待做"` 这类**精确匹配**统计的，
      一条拼错的状态会让这条记录永远不出现在任何视图里 —— 静默消失。
    """
    if kind not in ASSESSMENT_KINDS:
        raise AssessmentError(f"笔试类型 {kind!r} 不认识。可用：{'、'.join(ASSESSMENT_KINDS)}")
    if status not in ASSESSMENT_STATUSES:
        raise AssessmentError(f"状态 {status!r} 不认识。可用：{'、'.join(ASSESSMENT_STATUSES)}")

    application = _application_for(session, job_id)
    _advance_to_assessment(session, job_id, source="jh assessment add")

    assessment = Assessment(
        application_id=application.id,
        kind=kind,
        invited_at=invited_at,
        deadline_at=deadline_at,
        platform=platform,
        status=status,
        note=note,
    )
    session.add(assessment)
    # ★ 事件流是"邀请信号"的落地处：笔试邀请即使没有改状态也要留一条，
    #   否则时间轴上完全看不出"这家公司什么时候给过我笔试"。
    session.add(
        ApplicationEvent(
            application_id=application.id,
            event_type="assessment_invite",
            payload_json=to_json(
                {
                    "kind": kind,
                    "platform": platform,
                    "deadline_at": deadline_at.isoformat() if deadline_at else None,
                    "source": f"jh assessment add --job {job_id}",
                }
            ),
            occurred_at=now_utc(),
        )
    )
    session.flush()
    return assessment


def _application_for(session: Session, job_id: int) -> Application:
    """取这个岗位的投递记录；没有就**现建一条 pending**。

    ★ 为什么允许"先有笔试、后有投递记录"：
      真实顺序常常是反的 —— 用户是**收到笔试邀请之后**才回头登记的。
      这时逼他先打一次 `jh apply --status applied` 才能记笔试，等于给最关键
      （也最容易错过）的一件事加了一道不必要的门槛。
      自动补 pending 之后他再正常打标也不会冲突：用的是同一条记录。

    Raises:
        AssessmentError: 岗位本身不存在（这时候多半是 id 敲错了，必须报错）。
    """
    job = session.get(Job, job_id)
    if job is None:
        raise AssessmentError(f"找不到岗位 id={job_id}")
    application = apply_svc.get_latest_application(session, job.id)
    if application is None:
        application, _created = apply_svc.ensure_pending_application(session, job)
    return application


def _advance_to_assessment(session: Session, job_id: int, *, source: str) -> None:
    """把投递状态推进到 `assessment`，但**只前进不回退**。

    ★ 为什么不无条件调 `apply_svc.set_status`：
      一个已经面试/拿 offer/被拒的岗位，如果因为补登记笔试被改回 `assessment`，
      后果是看板与顶部提醒**同时**失真（面试提醒消失、笔试提醒冒出来）。
      用户补记一条历史信息，不应该改变他对当前进度的认知。

    ★ 为什么不自己写事件：`set_status` 已经负责"写 status_change 事件 +
      `changed=False` 时不写重复事件"，这里复用它，口径只有一处。
    """
    application = apply_svc.get_latest_application(session, job_id)
    if application is not None and application.status in _BEYOND_ASSESSMENT:
        logger.debug(
            "投递 #%s 当前是 %s（已过笔试环节），登记笔试不回退状态",
            application.id,
            application.status,
        )
        return
    apply_svc.set_status(session, job_id, "assessment", source=source)


# ===========================================================================
# 列表
# ===========================================================================
def list_assessments(
    session: Session, *, upcoming: bool = False, now: datetime | None = None
) -> list[AssessmentRow]:
    """列出笔试 / 测评，按截止时间升序（没填截止时间的排最后）。

    Args:
        upcoming: 只看"待做"且**即将截止**的（`UPCOMING_DAYS` 天窗口内）。

    ★ 为什么没填截止时间的"待做"也算"即将截止"：
      它没有时间窗可以落进去，如果被过滤掉就**永远不会出现在提醒里** ——
      而"登记了但没填截止时间"恰恰是最容易忘的一类。
      提醒它相当于说"这条还没做，去补个截止时间"。

    ★ 与 `reminder_svc` 的口径差异（有意的，只差一处）：
      顶部提醒只推**还没到期**的（避免过期项天天刷屏）；这里**连已过期的
      "待做"也列出**，因为"已经错过了"比"快到了"更需要立刻被看见，
      而列表是用户主动敲命令来看的，不存在刷屏问题。

    ★ 排序把 None 放最后：没有截止时间的条目对"现在该做哪个"没有指导意义，
      排在前面只会挤掉真正紧急的那几条。
    """
    moment = now or now_utc()
    stmt = (
        select(Assessment, Job)
        .join(Application, Assessment.application_id == Application.id)
        .join(Job, Application.job_id == Job.id)
        .order_by(Assessment.deadline_at.is_(None), Assessment.deadline_at)
    )
    if upcoming:
        horizon = moment + timedelta(days=UPCOMING_DAYS)
        stmt = stmt.where(
            Assessment.status == "待做",
            or_(Assessment.deadline_at.is_(None), Assessment.deadline_at <= horizon),
        )

    return [
        AssessmentRow(
            assessment=assessment,
            job_id=job.id,
            company=job.company or "",
            title=job.title or "",
            days_left=days_between(moment, assessment.deadline_at),
        )
        for assessment, job in session.execute(stmt).all()
    ]

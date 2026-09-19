"""投递时间轴（`jh apply --timeline <job_id>`）—— P6 的看板视图。

╔══════════════════════════════════════════════════════════════════════════╗
║ 为什么时间轴直接读**事件流**，而不是"按当前状态画一排节点"                   ║
╚══════════════════════════════════════════════════════════════════════════╝

"按当前状态画"会撒谎：一个现在是 `rejected` 的投递，可能走到过二面。
`application_events` 是 append-only 的（`models/application.py` 写明了为什么），
它记着**真实发生过的顺序**：投递 → 已读 → 回复 → 笔试邀请 → 约面 → 被拒。
时间轴要回答的是"这单一路怎么走过来的"，所以只能读事件流。

★ `occurred_at` 而不是 `created_at`：补记的事件（昨晚面的试今天才记）
  在时间轴上应该落在**发生的时刻**，而不是我敲命令的时刻。

★ 每条事件都要能翻译成人话（`label`）：事件流是给程序看的，
  时间轴是给人的 —— 用户不该看到 `status_change applied→viewed` 这种原始形状。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunter.models import ApplicationEvent, Job
from jobhunter.services import apply_svc
from jobhunter.utils.io import from_json
from jobhunter.utils.logging import get_logger
from jobhunter.utils.time import days_since, fmt_datetime_local, now_utc

logger = get_logger(__name__)

__all__ = ["Timeline", "TimelineEntry", "build_timeline", "entry_label"]

#: 状态 → 中文（时间轴与周报都用它，别在渲染层各写一份）
STATUS_LABELS: dict[str, str] = {
    "pending": "准备投递",
    "applied": "已投递",
    "viewed": "对方已读",
    "replied": "对方回复",
    "assessment": "笔试/测评",
    "interview": "面试",
    "offer": "Offer",
    "rejected": "被拒",
}


def _status_change_label(event: ApplicationEvent) -> str:
    """`status_change` 的形状与其他事件不同（有 from/to），单独处理更清楚。"""
    to_status = event.to_status or ""
    label = STATUS_LABELS.get(to_status, to_status or "状态变更")
    if event.from_status:
        return f"{label}（{STATUS_LABELS.get(event.from_status, event.from_status)} → {label}）"
    return label


def _assessment_label(payload: dict) -> str:
    kind_label = payload.get("kind") or "笔试"
    deadline = payload.get("deadline") or ""
    return f"收到{kind_label}邀请" + (f"（截止 {deadline}）" if deadline else "")


def _interview_label(payload: dict) -> str:
    round_ = payload.get("round") or "面试"
    mode = payload.get("mode") or ""
    return f"约了{round_}" + (f"（{mode}）" if mode else "")


def _offer_label(payload: dict) -> str:
    salary = payload.get("salary") or ""
    return "收到 Offer" + (f"（{salary}）" if salary else "")


#: 事件类型 → 人话。★ 用表驱动而不是一长串 if：
#: 事件类型会随阶段增加（P7 的线索投递也要写事件流），
#: 加一种就加一行，而不是在函数中间插一段分支（也顺手绕开了"分支过多"的告警）。
_LABEL_BUILDERS = {
    "greeting_sent": lambda payload: f"开场白发出去（{payload.get('variant') or '模板'}）",
    "greeting_edited": lambda _payload: "发之前我改过开场白",
    "followup": lambda _payload: "我主动追问了一次",
    "note": lambda payload: payload.get("text") or "记了一笔",
    "assessment_invite": _assessment_label,
    "interview_scheduled": _interview_label,
    "offer_received": _offer_label,
}


def entry_label(event: ApplicationEvent) -> str:
    """把一条事件翻译成人话（未知类型原样返回，绝不吞掉信息）。"""
    if event.event_type == "status_change":
        return _status_change_label(event)
    payload = from_json(event.payload_json, default={}) or {}
    builder = _LABEL_BUILDERS.get(event.event_type)
    return builder(payload) if builder else event.event_type


@dataclass
class TimelineEntry:
    """时间轴上的一条。"""

    occurred_at: datetime
    event_type: str
    label: str
    #: 北京时间（CLI 直接用，不再转换）
    local_text: str = ""


@dataclass
class Timeline:
    """一个岗位的投递时间轴。"""

    job_id: int
    company: str
    title: str
    application_id: int | None = None
    status: str = ""
    #: 投出去几天了（没投则 None）
    days: int | None = None
    entries: list[TimelineEntry] = field(default_factory=list)
    #: 时间轴上看不到、但值得知道的事实（如"这条路走完了"）
    notes: list[str] = field(default_factory=list)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status or "（还没有投递记录）")

    @property
    def empty(self) -> bool:
        return not self.entries


def build_timeline(session: Session, job_id: int, *, now: datetime | None = None) -> Timeline:
    """画一个岗位的投递时间轴（只读事件流 + 投递状态）。

    Raises:
        LookupError: 岗位不存在（CLI 翻译成退出码 1）。
    """
    job = session.get(Job, job_id)
    if job is None:
        raise LookupError(f"找不到岗位 id={job_id}")

    moment = now or now_utc()
    timeline = Timeline(job_id=job.id, company=job.company or "", title=job.title or "")

    application = apply_svc.get_latest_application(session, job.id)
    if application is None:
        timeline.notes.append(
            "这个岗位还没有投递记录（先 jh today 生成开场白，或 jh apply --job N --status applied）"
        )
        return timeline

    timeline.application_id = application.id
    timeline.status = application.status
    timeline.days = days_since(application.applied_at, now=moment)

    events = session.scalars(
        select(ApplicationEvent)
        .where(ApplicationEvent.application_id == application.id)
        .order_by(ApplicationEvent.occurred_at, ApplicationEvent.id)
    ).all()
    for event in events:
        timeline.entries.append(
            TimelineEntry(
                occurred_at=event.occurred_at,
                event_type=event.event_type,
                label=entry_label(event),
                local_text=fmt_datetime_local(event.occurred_at),
            )
        )

    if timeline.status in ("offer", "rejected"):
        timeline.notes.append(f"这条路已经走完了（{timeline.status_label}）。")
    elif not timeline.entries:
        timeline.notes.append(
            "有投递记录但一条事件都没有 —— 事件流是 P4.5 之后才开始写的，"
            "更早的投递在这里是空白的（不是数据丢了）。"
        )
    return timeline

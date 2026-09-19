"""投递模型：`applications`（当前状态）+ `application_events`（历史事件流）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 为什么是"状态列 + 事件流"两张结构，而不是一张表存历史                       ║
╚══════════════════════════════════════════════════════════════════════════╝

`applications.status` 回答"**现在**到哪一步了"（看板要的）；
`application_events` 回答"**一路怎么走过来的**"（时间轴要的）。

事件流是 **append-only**（只追加、不修改、不删除），好处：

    · 统计口径永远可复现：三个月后回头算转化率，结果和当时一致；
    · "这单为什么黄了"可归因：看事件顺序就知道是笔试挂了还是约面后没消息；
    · 状态列写错了还能从事件流重建。

★ 事件流取代了 v2 设计里的单用途 `followups` 表：
  表数没有增加（本来也要有 followups），但功能从"记录追问"扩展到
  "所有状态变更 + 追问 + 备注 + 笔试邀请"，而且能画时间轴。

★ `occurred_at` 与 `created_at` 必须分开：
    occurred_at = 事情**真的**发生在什么时候（可能是我事后补记的，比如昨晚面的试今天才记）
    created_at  = 这条记录**写进库**是什么时候
  合并成一个字段的话，"我补记的面试"会污染时间轴（看起来像今天才面的）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jobhunter.models.base import BaseModel, UTCDateTime, json_text, utc_now_default
from jobhunter.models.constants import EVENT_TYPES

__all__ = ["Application", "ApplicationEvent", "EVENT_TYPE_CHECK"]

#: `application_events.event_type` 的 CHECK 表达式（**单一来源**）。
#: 迁移 `0003_event_type_check.py` 里那一份是**逐字相同**的副本：
#: 迁移是历史，不能 import 会变的常量；两份不一致时
#: `tests/unit/test_event_type_constraint.py` 会立刻变红。
#: ★ 命名说明：命名约定里的 ck 模板是 `ck_%(table_name)s_%(constraint_name)s`，
#:   所以这个名字会被渲染成 `ck_application_events_event_type` ——
#:   与迁移里显式写的约束名**完全一致**（批量重建表时两边必须同名）。
EVENT_TYPE_CHECK = "event_type IN ({})".format(", ".join(f"'{t}'" for t in EVENT_TYPES))


class Application(BaseModel):
    """一次投递（对应一个岗位）。

    状态机（见 models/constants.py 的 APPLICATION_STATUSES）：
        pending → applied → viewed → replied → assessment → interview → offer
        其中 rejected 可以从任意状态发生，offer 可以从 interview 直接跳
        （小公司一面就发 offer 很常见，所以不能写死"必须逐级流转"）。
    """

    __tablename__ = "applications"

    # ★ RESTRICT（默认）：有投递记录的岗位**不允许**被静默删除。
    #   投递记录是我的资产，删岗位前必须先显式处理它的投递。
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    # 用的是哪一版简历（SET NULL：删了定制版不该丢投递记录）
    resume_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("resume_versions.id", ondelete="SET NULL")
    )
    platform: Mapped[str | None] = mapped_column(String(40))
    # ★ 渠道：招聘网站 / 官网 / 内推 / HR直招 —— 渠道转化率统计的口径来源
    channel: Mapped[str | None] = mapped_column(String(20))

    # ---- 开场白（含闭环字段）----------------------------------------------
    greeting_text: Mapped[str | None] = mapped_column(Text)
    # ★ with_project / without_project —— A/B 只测这一个变量
    #   （评审提醒：样本太小，只记录对比，**不宣称统计显著性**）
    greeting_variant: Mapped[str | None] = mapped_column(String(30))
    # ★★ 闭环字段：这个工具生成的话术到底有没有被用上？
    #   greeting_sent   = 真的发出去了吗
    #   greeting_edited = 发之前我改过吗
    #   没有这两个字段，"开场白变体回复率"就永远是无源之水。
    greeting_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    greeting_edited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ---- 状态与时间 --------------------------------------------------------
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    viewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    replied_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now_default, nullable=False
    )

    # 事件流归属本投递；删投递时连带删除（passive_deletes 交给数据库的 ON DELETE CASCADE）
    events: Mapped[list[ApplicationEvent]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ApplicationEvent.occurred_at",
    )

    __table_args__ = (
        Index("ix_applications_job_id", "job_id"),
        # `jh apply --list-pending` 与批量打标
        Index("ix_applications_status", "status"),
        # ★ 日额度与"投后 N 天"统计都从 applied_at 聚合。
        #   本项目**不建计数表**：日已投数从 applications 实时聚合，
        #   避免"计数器和真实记录对不上"这种最难查的不一致。
        Index("ix_applications_applied_at", "applied_at"),
    )

    def __repr__(self) -> str:
        return f"<Application id={self.id} job_id={self.job_id} status={self.status!r}>"


class ApplicationEvent(BaseModel):
    """投递事件流的一条（append-only）。

    ★ 只允许 INSERT。任何 UPDATE/DELETE 都会破坏"历史不可篡改"这个前提，
      而统计口径的可复现性完全建立在这个前提上。
    """

    __tablename__ = "application_events"

    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    # status_change / greeting_sent / followup / note / assessment_invite …
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    # 状态流转前后（非状态类事件留 NULL）
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str | None] = mapped_column(String(20))
    # 自由扩展：{"source": "jh apply 3=applied", "greeting_variant": "with_project"}
    payload_json: Mapped[str | None] = json_text()
    # ★ 业务时间（真实发生时间，可能是我事后补记的）
    occurred_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now_default, nullable=False
    )
    # 系统时间（写进库的时间）—— 与 occurred_at 分开，见模块 docstring
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now_default, nullable=False
    )

    application: Mapped[Application] = relationship(back_populates="events")

    __table_args__ = (
        Index("ix_application_events_application_id", "application_id"),
        # 时间轴按发生时间排序
        Index("ix_application_events_occurred_at", "occurred_at"),
        # ★ ISSUE #42：event_type 收紧成 EVENT_TYPES 八取一。
        #   没有它，`status_chnage` 这种拼写错误会**静默入库**，
        #   而 P6 的时间轴/统计按 event_type 分类 → 出现永远对不上的分类。
        #   服务层（`apply_svc.record_event`）会先给一句中文报错，
        #   所以用户看到的是"可用取值"而不是 SQLite 的约束报错。
        CheckConstraint(EVENT_TYPE_CHECK, name="event_type"),
    )

    def __repr__(self) -> str:
        return (
            f"<ApplicationEvent id={self.id} app={self.application_id} "
            f"{self.event_type!r} {self.from_status}→{self.to_status}>"
        )

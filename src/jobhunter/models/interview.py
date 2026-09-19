"""面试模型：`interviews`（面试场次）+ `interview_questions`（问题与自评）。

★ `interview_questions.source` 是这张表最关键的设计（v3 新增）：
    recorded  = 我真实被问到的题（面试后回忆记录）
    predicted = 我用 JD 让模型猜的题（`jh question predict <job_id>`）

必须区分的原因：`jh weak` 的结论是"我最该补哪个知识点"。
如果把自己猜的题和真实被问的题混在一起统计，薄弱点会完全失真
（我会把"自己吓自己"当成真实能力缺口，或者反过来漏掉真正的弱项）。

★ 自评只分 3 档（bad/ok/good）：
    面试题样本量很小，5 档评分产生的基本是噪声（评审采纳）。
    `is_weak` 单独用一个布尔字段，语义比"rating <= 2"更直白。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jobhunter.models.base import BaseModel, UTCDateTime, utc_now_default

__all__ = ["Interview", "InterviewQuestion"]


class Interview(BaseModel):
    """一场面试。状态：待面 → 已面 → 出结果。"""

    __tablename__ = "interviews"

    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    # 一面 / 二面 / 三面 / HR面 / 终面
    round: Mapped[str | None] = mapped_column(String(30))
    # ★ "本周面试 N 场"提醒靠它。存 UTC，展示转北京时间。
    scheduled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # 视频 / 电话 / 现场
    mode: Mapped[str | None] = mapped_column(String(20))
    meeting_link: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="待面", nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now_default, nullable=False
    )

    questions: Mapped[list[InterviewQuestion]] = relationship(
        back_populates="interview",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_interviews_application_id", "application_id"),
        # `jh interview list --upcoming` 按时间排序取最近几场
        Index("ix_interviews_scheduled_at", "scheduled_at"),
    )

    def __repr__(self) -> str:
        return f"<Interview id={self.id} app={self.application_id} round={self.round!r}>"


class InterviewQuestion(BaseModel):
    """一道面试题：题目 + 我的回答 + 自评 + 知识点分类。

    ╔══════════════════════════════════════════════════════════════════════════╗
    ║ 为什么 `interview_id` 可空、还多了一个 `job_id`（P6 加，见迁移 0002）        ║
    ╚══════════════════════════════════════════════════════════════════════════╝

    这张表同时装两类题，而它们的**归属**根本不同：

        recorded   我真实被问到的题  → 属于**某一场面试**（有 interview_id，没有 job_id）
        predicted  我拿 JD 猜的题    → 属于**某个岗位**（有 job_id，没有 interview_id）

    一开始（P0 建表时）把 `interview_id` 写成了 NOT NULL，理由是"问题总是面试里问的"。
    但 P6 的 `jh question predict <job_id>` 拿的是**岗位**而不是面试场次 ——
    准备面试往往发生在"面试还没约上"的时候。若坚持 NOT NULL，就只能：
      · 逼用户先 `jh interview add`（为了一个还不存在的面试时间造假数据），或
      · 造一条假的面试记录来挂预测题 → 它会出现在 `jh interview list`、
        `jh stats` 的面试场次统计、以及顶部"本周面试"提醒里 ——
        **为了塞下一条数据而污染三个功能的输入**，这是最差的一种妥协。

    所以：可空 + 加 `job_id`。回填规则见迁移 0002 的 docstring。
    """

    __tablename__ = "interview_questions"

    #: 属于哪一场面试（`recorded` 才填；`predicted` 为 None）
    interview_id: Mapped[int | None] = mapped_column(
        ForeignKey("interviews.id", ondelete="CASCADE")
    )
    #: 属于哪个岗位（`predicted` 才填）。★ SET NULL 而不是 CASCADE：
    #: 岗位记录被清理时，不该连带删掉"我为它准备过哪几道题"（那是我的准备成果）。
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    question: Mapped[str] = mapped_column(Text, nullable=False)
    my_answer: Mapped[str | None] = mapped_column(Text)
    # bad / ok / good（3 档，见模块 docstring）
    rating: Mapped[str | None] = mapped_column(String(10))
    # ★ 知识点分类，如"前端性能""数据库索引""Agent 编排"
    #   → `jh weak` 按它聚合，所以填得越一致、结论越准
    category: Mapped[str | None] = mapped_column(String(60))
    # ★ 手动标记"没答上来" → `jh weak` 按次数降序
    is_weak: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # ★★ recorded（我记的）/ predicted（JD 预测的）—— 见模块 docstring
    source: Mapped[str] = mapped_column(String(20), default="recorded", nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now_default, nullable=False
    )

    interview: Mapped[Interview] = relationship(back_populates="questions")

    __table_args__ = (
        Index("ix_interview_questions_interview_id", "interview_id"),
        # 预测题按岗位查（"这个岗位我准备过哪几道题"）
        Index("ix_interview_questions_job_id", "job_id"),
        # `jh weak` 的聚合查询主索引
        Index("ix_interview_questions_category", "category"),
        Index("ix_interview_questions_is_weak", "is_weak"),
    )

    def __repr__(self) -> str:
        return f"<InterviewQuestion id={self.id} category={self.category!r} rating={self.rating!r}>"

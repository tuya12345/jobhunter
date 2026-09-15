"""笔试/测评模型：`assessments`（v3 新增）。

★ 为什么必须有这张表（产品视角评审的缺口 #1）：
    海投**必定**遇到在线笔试/测评/AI 面。v2 的状态机里根本没有这个环节，
    后果是：岗位卡在 `replied` 之后**再也不会提醒我**，
    而笔试邀请通常有 **48-72 小时的截止时间** —— 错过就等于自动淘汰。
    这类"沉默的失败"是这个工具最该防住的，因为用户永远不会发现是工具的问题。

★ `deadline_at` 是这张表存在的核心理由：
    顶部提醒里的 "⏳ 笔试截止 1 条（9-11 饿了么）" 就是从它算出来的。

★ `score` 用 Text 而不是 Integer：
    很多测评平台只给"通过 / 未通过"，或者给个"A/B/C 级"，根本不给分数。
    强行用数字类型会导致这些结果没法存。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from jobhunter.models.base import BaseModel, UTCDateTime, utc_now_default

__all__ = ["Assessment"]


class Assessment(BaseModel):
    """一次笔试 / 在线测评 / AI 面试邀请。"""

    __tablename__ = "assessments"

    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    # 笔试 / 测评 / AI面（见 models/constants.py 的 ASSESSMENT_KINDS）
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # 收到邀请的时间
    invited_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # ★★ 截止时间 —— 顶部提醒的数据源。错过就自动淘汰，所以必须显式记录。
    deadline_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # 牛客 / 赛码 / 北森 / 智联测评 …
    platform: Mapped[str | None] = mapped_column(String(40))
    # 待做 / 已做 / 已过期
    status: Mapped[str] = mapped_column(String(20), default="待做", nullable=False)
    # 用 Text：很多测评不给分数，只给"通过/未通过"或等级
    score: Mapped[str | None] = mapped_column(String(40))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now_default, nullable=False
    )

    __table_args__ = (
        Index("ix_assessments_application_id", "application_id"),
        # ★ 顶部提醒查"即将截止的笔试"走这个索引
        Index("ix_assessments_deadline_at", "deadline_at"),
    )

    def __repr__(self) -> str:
        return f"<Assessment id={self.id} app={self.application_id} kind={self.kind!r}>"

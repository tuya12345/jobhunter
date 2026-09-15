"""Offer 模型：`offers`（v3 新增）。

★ 为什么必须有（产品视角评审的缺口 #2）：
    v2 设计的 8 张表里**居然没有 Offer** —— 整个求职流程的终点站缺失。
    后果：拿到 offer 之后，薪资/地点/签约截止日只能记在脑子里或微信收藏里，
    而"多个 offer 横向比较"恰恰是求职后期最需要决策支持的时刻。

★ `deadline_at`（签约截止日）同样是"沉默失败"防护：
    三方协议/offer 有明确的回复期限，错过就没了。

★ 为什么 `company` / `title` 冗余存一份，不只用 `application_id` 关联：
    offer 到手后往往要脱离原投递记录做横向比较（甚至投递记录已被清理）。
    冗余存储让 `jh offer compare` 不依赖 join 的完整性。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from jobhunter.models.base import BaseModel, UTCDateTime, utc_now_default

__all__ = ["Offer"]


class Offer(BaseModel):
    """一个 offer。"""

    __tablename__ = "offers"

    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    # 冗余存一份：offer 可能脱离原投递记录独立比较
    company: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str | None] = mapped_column(String(200))

    # 薪资原文，如 "20-25K·15薪"
    salary_text: Mapped[str | None] = mapped_column(String(80))
    # ★ 数值化才能横向比：月薪（元）
    salary_month: Mapped[int | None] = mapped_column(Integer)
    # ★ 发几个月（如 14、15）→ 年包 = salary_month × months
    months: Mapped[int | None] = mapped_column(Integer)
    location: Mapped[str | None] = mapped_column(String(60))

    # ★★ 签约截止日 → `jh offer compare` 排序 + 截止提醒
    deadline_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # 待定 / 已接受 / 已拒绝
    status: Mapped[str] = mapped_column(String(20), default="待定", nullable=False)

    # 优缺点：决策留痕。半年后回看"当时为什么选了它"很有价值
    pros: Mapped[str | None] = mapped_column(Text)
    cons: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now_default, nullable=False
    )

    __table_args__ = (
        Index("ix_offers_application_id", "application_id"),
        Index("ix_offers_deadline_at", "deadline_at"),
    )

    @property
    def annual_package(self) -> int | None:
        """年包估算（元）= 月薪 × 月数。缺任一项返回 None。

        放在模型上而不是 service 里：这是**由字段直接推导**的纯计算，
        没有业务规则（比如"要不要算签字费"），放这里最自然。
        """
        if self.salary_month is None or self.months is None:
            return None
        return self.salary_month * self.months

    def __repr__(self) -> str:
        return f"<Offer id={self.id} {self.company!r} status={self.status!r}>"

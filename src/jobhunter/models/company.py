"""线索模型：`companies`（官网/内推线索池，P7）。

来源是"贴吧 / 小黑盒 / 小红书 / 知乎 / 微信公众号"里看到的招聘信息 ——
这些渠道的信息形态极其杂乱（一张截图、一句"我们组在招人，简历发 xx@yy.com"），
所以设计上留了 `evidence_text` / `evidence_url` 存**原始证据**：
    · 线索可能是错的/过期的，保留原文才能回头核对；
    · 保留"从哪看到的"才能复盘哪个来源渠道有效。

★ `source_type`（官网 / 内推 / HR直招）是决定"该怎么投"的关键：
    官网   → 走在线系统，可能还要填一堆表单
    内推   → 找内推人，通常响应最快
    HR直招 → 直接加微信/发邮件
    `jh leads export` 按它分组导出，就是一份可直接执行的投递清单。

★ `verified` 只表示"URL 可达性检查过"（HEAD 请求记状态码），
  不代表"这个岗位还在招" —— 不要混淆，否则会给出虚假的确定性。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from jobhunter.models.base import BaseModel, UTCDateTime, utc_now_default

__all__ = ["Company"]


class Company(BaseModel):
    """一条官网/内推投递线索。"""

    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # 官网招聘页 URL（可达性检查的对象）
    official_url: Mapped[str | None] = mapped_column(Text)
    # 从哪个平台看到的：贴吧 / 小黑盒 / 小红书 / 知乎 / 微信公众号
    source_platform: Mapped[str | None] = mapped_column(String(40))
    # ★ 官网 / 内推 / HR直招 —— 决定"该怎么投"，导出时按它分组
    source_type: Mapped[str | None] = mapped_column(String(20))
    # 原始线索文本（"在哪看到的、原话是什么"）
    evidence_text: Mapped[str | None] = mapped_column(Text)
    evidence_url: Mapped[str | None] = mapped_column(Text)
    # 只表示"URL 可达性检查过"，**不代表岗位还在招**
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now_default, nullable=False
    )

    __table_args__ = (
        # 同公司合并去重（LLM 整理线索后按公司名归并）
        Index("ix_companies_name", "name"),
        # `jh leads export` 按 source_type 分组
        Index("ix_companies_source_type", "source_type"),
    )

    def __repr__(self) -> str:
        return f"<Company id={self.id} name={self.name!r} type={self.source_type!r}>"

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
    页面（`jh leads serve`）按它分组展示，就是一份可直接执行的投递清单。

★ `verified` 只表示"URL 可达性检查过"（HEAD 请求记状态码），
  不代表"这个岗位还在招" —— 不要混淆，否则会给出虚假的确定性。

★★ P7 架构变更（2026-09-19，ISSUE #59）：线索的**生产者**从这个仓库搬走了

    采集端现在是**独立部署**的 `leadhunter`（`projects/leadhunter`，命令 `lh`），
    它负责 add / probe / capture / export；本仓库只做"**消费**"：
    `jh leads import <快照.json>` 把线索导进来、`jh leads serve` 起一个只读页面。
    所以下面这些列是"**采集端带过来的事实**"，不是本仓库算出来的：

        http_status / checked_at  采集端做的可达性检查
                                  ★ **空 = 未检查，不是"失效"**（页面必须分三态）
        external_id               采集端那条线索的 id
                                  **只做溯源**，绝不当唯一键：采集端换台机器 id 就变了
        collected_at              采集端**采集到**它的时间
                                  （与本行 `created_at` 不同：后者是"导入进本库"的时间）
        name_norm                 公司名归一化后的值，**UNIQUE**
                                  —— 让"同一份快照导两次不会变成两份"成为**库层面的约束**，
                                  而不是"服务层记得先查一下"这种靠自觉的约定
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, validates

from jobhunter.models.base import BaseModel, UTCDateTime, utc_now_default

__all__ = ["Company", "company_key"]


def company_key(name: str) -> str:
    """公司名的匹配键：去掉**所有空白**后的名字（`companies.name_norm` 的值）。

    ★★ 为什么是"去空白"而不是更聪明的归一化

      这是**跨仓库的口径**：采集端（`leadhunter`）合并线索时用的就是
      `"".join(name.split())`。消费端若自己加码（比如把「有限公司」折叠掉），
      就会把生产者刻意分开的两家公司并成一条 —— 那是"错合并"，会丢线索，
      而"少合并"只是清单里多一行。代价不对称时选代价小的那边。

    ★ 为什么放在模型层而不是服务层：它是**这一列的定义**（唯一索引建在它上面），
      而 `name_norm` 必须永远等于 `company_key(name)` —— 让模型自己维护这条
      不变量，任何创建 `Company` 的代码都不可能忘（见下面的 `_sync_name_norm`）。
    """
    return "".join(str(name or "").split())


class Company(BaseModel):
    """一条官网/内推投递线索。"""

    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: 公司名去掉空白后的值（UNIQUE）—— 导入幂等的匹配键，见 `company_key()`
    name_norm: Mapped[str] = mapped_column(String(200), nullable=False)
    # 官网招聘页 URL（可达性检查的对象）
    official_url: Mapped[str | None] = mapped_column(Text)
    # 从哪个平台看到的：贴吧 / 小黑盒 / 小红书 / 知乎 / 微信公众号
    source_platform: Mapped[str | None] = mapped_column(String(40))
    # ★ 官网 / 内推 / HR直招 —— 决定"该怎么投"，页面按它分组
    source_type: Mapped[str | None] = mapped_column(String(20))
    # 原始线索文本（"在哪看到的、原话是什么"）
    evidence_text: Mapped[str | None] = mapped_column(Text)
    evidence_url: Mapped[str | None] = mapped_column(Text)
    # 只表示"URL 可达性检查过"，**不代表岗位还在招**
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    # ---- 采集端带过来的事实（P7 消费端，迁移 0005）----------------------
    #: 可达性检查的状态码；**NULL = 未检查**（不是失效）
    http_status: Mapped[int | None] = mapped_column(Integer)
    #: 那次检查的时间（UTC）
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    #: 采集端那条线索的 id —— 只做溯源，不当唯一键
    external_id: Mapped[str | None] = mapped_column(String(64))
    #: 采集端采集到它的时间（UTC）
    collected_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now_default, nullable=False
    )

    __table_args__ = (
        # 同公司合并去重（导入时按归一化公司名归并）
        Index("ix_companies_name", "name"),
        # ★ 唯一：同名（归一化后）只允许一行 —— 导入幂等的库层保障
        Index("ix_companies_name_norm", "name_norm", unique=True),
        # 页面与导出按 source_type 分组
        Index("ix_companies_source_type", "source_type"),
        # 溯源：按采集端 id 反查"这条是哪次导出进来的"
        Index("ix_companies_external_id", "external_id"),
    )

    def __repr__(self) -> str:
        return f"<Company id={self.id} name={self.name!r} type={self.source_type!r}>"

    @validates("name")
    def _sync_name_norm(self, _key: str, value: str) -> str:
        """**保证** `name_norm = company_key(name)`（赋值 `name` 时自动跟上）。

        ★ 为什么要这么做，而不是"创建时记得传 name_norm"：

          `name_norm` 上有 UNIQUE 索引，它是"同一份快照导两次不会变成两份"的
          **库层保障**。但保障的前提是"每一行都填了正确的 name_norm" ——
          如果靠调用方自觉，那么某天有人写 `Company(name="某某科技")`（比如测试、
          或者将来新加的导入路径）就会直接撞 NOT NULL；更糟的情况是有人
          自己拼一个近似的 name_norm，于是幂等悄悄失效（重复行进得来）。

          这个项目已经因为"靠记得"吃过好几次亏（#37 / #24 / #55），
          所以这里把不变量交给模型自己维护：**只要给 name 赋值，name_norm 一定对**。
        """
        self.name_norm = company_key(value)
        return value

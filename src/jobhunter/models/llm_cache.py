"""LLM 缓存模型：`llm_cache`（v3 新增）。

★★ 这张表的全部价值都压在 `cache_key` 的构成上 ★★

    cache_key = sha256( prompt_version | model | jd_hash | resume.content_hash )

四个成分各自解决一个真实 bug：

  ① `resume.content_hash`
     ★ 这是**最容易被写错、后果最严重**的一项（评审指出的原始 bug）。
     v2 的 key 只有 jd_hash。后果：我改了简历，同一个岗位的匹配分**仍然是旧值**。
     而"改了简历 → 分数应该变"恰恰是这个工具存在的核心理由。
     漏掉它，用户会得出"这工具算得不准"的结论，然后弃用。

  ② `prompt_version`
     提示词改了，旧结果绝不该复用。
     否则你调了半天提示词，看到的还是老输出 —— 会误以为"改提示词没用"。

  ③ `model`
     换模型（deepseek-chat → deepseek-reasoner）必须让缓存失效，
     否则新旧模型的输出混在一起，无法比较效果。

  ④ `jd_hash`
     JD 内容变了（HR 改了要求）→ 重新算。

★ 为什么必须建**独立表**而不是塞进 jobs 表加几列：
    · 一个岗位可能有多个 task（match / greeting / tailor / question_predict）各自的缓存；
    · 需要按任务**统计命中率与成本**（tokens_in/tokens_out/cost 三列）；
    · 缓存是纯派生数据，可以随时整体清掉而不影响业务数据 ——
      独立表让"清缓存"成为一句 DELETE，不会有牵连。

★ 为什么**不建外键**：
    缓存与业务生命周期解耦。删掉一个岗位不应该级联删缓存
    （缓存命中判断本来就走 cache_key，不依赖岗位是否存在）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from jobhunter.models.base import Base, UTCDateTime, utc_now_default

__all__ = ["LLMCache"]


class LLMCache(Base):
    """一次 LLM 调用的结构化结果 + 用量统计。

    注意这里继承 `Base` 而不是 `BaseModel`：
    **主键是 cache_key（业务算出来的摘要），不是自增 id。**
    这样"同一个 key 再写一次"天然是 UPSERT 语义，不会产生重复行。
    """

    __tablename__ = "llm_cache"

    # ★ sha256(prompt_version | model | jd_hash | resume.content_hash)
    #   16 进制 64 字符，做主键
    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)

    # match / greeting / tailor / jd_parse / question_predict / leads_parse
    # 用于按任务统计命中率与成本
    task: Mapped[str] = mapped_column(String(40), nullable=False)

    # 模型返回的结构化 JSON（原始响应，不做二次加工：
    # 将来想换解析口径时，历史缓存还能重新利用）
    response_json: Mapped[str] = mapped_column(Text, nullable=False)

    # 用量与成本：DeepSeek 很便宜，但"便宜"不等于"不用看"。
    # 有了这三列，`jh stats` 才能回答"这个月花了多少钱、哪个任务最贵"。
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    cost: Mapped[float | None] = mapped_column(Float)

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now_default, nullable=False
    )

    # 额外留一列记录实际用的模型：排查"为什么这个缓存结果看起来不对"时
    # 需要知道它是哪个模型产出的（cache_key 里虽然含 model，但那是哈希、看不出原文）
    model: Mapped[str | None] = mapped_column(String(60))

    __table_args__ = (
        # 按任务统计成本/命中率
        Index("ix_llm_cache_task", "task"),
        # 按时间做成本趋势（月度花了多少）
        Index("ix_llm_cache_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<LLMCache key={self.cache_key[:12]}… task={self.task!r}>"

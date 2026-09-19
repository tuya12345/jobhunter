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

★★ `hits` / `last_hit_at` 两列（ISSUES #25）—— 为什么"被复用了几次"必须落库

    这张表原来只记"这一行是多少钱算出来的"，没有记"这一行被复用了几次"。
    于是命中率**根本算不出来**（`jh cache stats` 要它），只能给一个"有多少行"
    —— 而"有多少行"回答不了"缓存到底有没有用"。

    两条路可选：
      ① 每行加两列，在**唯一入口** `llm/cache.py::get_cached()` 自增；
      ② 另建一张"命中流水表"，每次命中写一行。
    选了 ①，理由：
      · 命中流水表与 `jh cache clear` 的承诺自相矛盾 —— 本文件下面写着"删掉缓存
        不影响业务数据"，而清空缓存后流水表里还留着"曾经命中过"，命中率会挂在
        一个已经不存在的缓存上；
      · ① 让"命中"这件事只有一个写入点（`get_cached`），以后新增调用点不会漏计数
        ——"形参加了但没人传"在这个项目已经出现过三次（#37 / #24 / #55）。
    代价：同 key 重算（`put_cached` 覆盖写）时旧 `hits` 会归零，口径是"当前这份
    缓存被复用了多少次"，不是"这个任务历史上被复用多少次"。写在 `cache_stats_svc`
    的模块说明里，免得下一个人以为是 bug。

★ `hits` 为什么给 server_default / default=0 而不是可空：
    可空的话 "0 次命中" 与 "还没统计过" 就分不出来，而这两者对命中率的影响
    完全不同（前者算 0%、后者应当算"无数据"）。默认 0 让口径唯一。

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
    #
    # ⚠️ 它们是**这一行当初真花掉的钱**，不是"本次调用"的钱：
    #    命中缓存时本次成本记 0（ISSUE #26），要算"省下多少"就用 cost × hits。
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    cost: Mapped[float | None] = mapped_column(Float)

    #: 这行结果被复用了几次（只在 `llm/cache.py::get_cached()` 自增）
    hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: 最近一次被复用的时间（从没命中过则为 NULL，`jh cache stats` 用它回答"还在用吗"）
    last_hit_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

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
        return f"<LLMCache key={self.cache_key[:12]}… task={self.task!r} hits={self.hits}>"

"""岗位模型：`jobs` —— 字段最多、索引最多的一张表。

它同时承担三个角色，这也是它字段多的根本原因：
    1. **抓取结果**（platform / url / jd_text / salary / jd_fetch_status）
    2. **共享中间层的产物**（jd_keywords_json / direction 隐含在里面）
    3. **匹配打分结果**（rule_score / rule_detail_json / llm_score / match_score）

★★ 本表最重要的一条纪律：**去重命中时绝不覆盖人工决策字段**。

    重复遇到同一个岗位（platform_job_id 相同，或归一化 hash 相同）时：

        允许更新：last_seen_at、salary_text、jd_fetch_status、jd_text/jd_hash（内容真变了）
        ★ 绝不覆盖：status、match_score、llm_score、rule_score、rule_detail_json、
                    matched_resume_id、reason_json、jd_keywords_json

    为什么：这些字段是"我的决策"或"我花过钱算出来的结果"。一旦被去重逻辑顺手覆盖：
        · 我已经 skipped 的岗位会被悄悄改回 new，第二天又被推给我；
        · 已经算好的分数被清空 → 重复付费调用 LLM；
        · `jh match set` 的手工裁决被抹掉。
    这是评审明确点出的坑，实现（P2 的 job_svc.upsert_job）必须照此写。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from jobhunter.models.base import BaseModel, UTCDateTime, json_text, utc_now_default

__all__ = ["Job"]


class Job(BaseModel):
    """一个岗位。"""

    __tablename__ = "jobs"

    # ---- 来源标识 ----------------------------------------------------------
    # boss / 前程无忧 / manual（手动粘贴）/ official（官网）…
    platform: Mapped[str] = mapped_column(String(40), nullable=False)
    # ★ 平台内的岗位 ID（从 URL 或页面里解析）。
    #   为什么它是最可靠的去重层：标题/公司名会被 HR 改来改去，但 URL 里的 ID 稳定。
    #   手动粘贴的岗位没有它 → 允许 NULL，靠 dedup_norm_hash 兜底。
    platform_job_id: Mapped[str | None] = mapped_column(String(120))

    # ---- 基本信息 ----------------------------------------------------------
    company: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(60))
    # 保留原文，不急着解析："3-5年"、"经验不限"、"应届生" 形态太多，
    # 解析失败时至少原文还在（P3 再从原文提取 hard_requirements）
    experience: Mapped[str | None] = mapped_column(String(80))
    education: Mapped[str | None] = mapped_column(String(80))
    # ★ intern / fulltime 区分。NULL = 未识别（不要默认 False！那会把实习岗当全职）
    is_intern: Mapped[bool | None] = mapped_column(Boolean)

    # ---- 薪资（原文 + 数值化）---------------------------------------------
    # 原文如 "15-25K·14薪"：给人看
    salary_text: Mapped[str | None] = mapped_column(String(80))
    # 数值化：给机器筛。"15-25K" → 15000 / 25000（单位：元/月）
    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)

    # ---- JD 正文 -----------------------------------------------------------
    jd_text: Mapped[str | None] = mapped_column(Text)
    # ★ sha256(归一化后的 jd_text)。LLM 缓存 key 的一部分 + 判断 JD 是否变过
    jd_hash: Mapped[str | None] = mapped_column(String(64))
    # ★ ok / failed / partial —— 抓取失败必须留痕。
    #   如果抓失败还留个空 jd_text 且状态是 ok，后面匹配会基于空文本乱打分，
    #   而你完全不知道为什么分数这么低。
    jd_fetch_status: Mapped[str | None] = mapped_column(String(20), default="ok")

    url: Mapped[str | None] = mapped_column(Text)
    # ★ 来自哪个搜索词/渠道 → "哪个渠道有效"复盘的原始数据
    source_keyword: Mapped[str | None] = mapped_column(String(120))
    # ★ 归一化(公司|职位|城市) 的 hash → 去重第三层兜底（平台 ID 与 URL 都拿不到时）
    dedup_norm_hash: Mapped[str | None] = mapped_column(String(64))

    # ---- 共享中间层产物 ★★ -------------------------------------------------
    # JD 抽出的技能/要求列表（JSON 数组）。被三处复用：
    #   ① 匹配打分  ② 定制简历（tailor）  ③ 生成开场白（greeting）
    # 这是全项目"只算一次"的核心载体。
    jd_keywords_json: Mapped[str | None] = json_text()

    # ---- 匹配打分 ----------------------------------------------------------
    # 规则层得分（0 成本，先算）
    rule_score: Mapped[int | None] = mapped_column(Integer)
    # ★ 分维度明细 {"direction": 30, "hard": 24, "skill": 32, "notes": [...]}
    #   → `jh match explain <job_id>` 打印它。没有它就没法调权重（评审原话）。
    rule_detail_json: Mapped[str | None] = json_text()
    # LLM 精评分（只对 rule_score Top N 算）
    llm_score: Mapped[int | None] = mapped_column(Integer)
    # 最终分：有 llm_score 用它，否则退化为 rule_score
    match_score: Mapped[int | None] = mapped_column(Integer)
    # 推荐用哪份简历（SET NULL：删简历不该删岗位）
    matched_resume_id: Mapped[int | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL")
    )
    # LLM 的完整理由 {"strengths":[], "gaps":[], "missing_keywords":[], "reason":"…"}
    reason_json: Mapped[str | None] = json_text()

    # ---- 状态与时间 --------------------------------------------------------
    # new → scored → queued → applied / skipped（见 models/constants.py）
    status: Mapped[str] = mapped_column(String(20), default="new", nullable=False)
    found_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now_default, nullable=False)
    # ★ 每次重复遇到这个岗位时**只更新它**：证明岗位还挂着（没下架）
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now_default, nullable=False
    )

    __table_args__ = (
        # 去重第一层：按平台 ID 精确匹配（命中率最高，必须走索引）
        Index("ix_jobs_platform_job_id", "platform_job_id"),
        # 去重第三层：归一化 hash 兜底
        Index("ix_jobs_dedup_norm_hash", "dedup_norm_hash"),
        # 数据库层兜住重复：同一平台下同一个 platform_job_id 只能有一条。
        # NULL 不参与唯一约束 → 手动粘贴的岗位（platform_job_id 为 NULL）不受影响。
        UniqueConstraint("platform", "platform_job_id", name="uq_jobs_platform_platform_job_id"),
        # `jh job list --status`、`jh today` 排除已投递
        Index("ix_jobs_status", "status"),
        # `jh today` 按分降序
        Index("ix_jobs_match_score", "match_score"),
        # ★ 复合索引：支撑"同公司同天不推多条"的去重查询
        #   （校招里给同一家公司连投多个岗位是减分项）
        Index("ix_jobs_company_city", "company", "city"),
    )

    def __repr__(self) -> str:
        return (
            f"<Job id={self.id} {self.company!r} {self.title!r} "
            f"score={self.match_score} status={self.status!r}>"
        )

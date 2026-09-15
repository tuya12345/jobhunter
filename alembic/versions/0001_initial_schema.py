"""initial schema: 11 tables

Revision ID: 0001
Revises:
Create Date: 2026-09-06

v3 数据模型的初始迁移：11 张表 + 全部索引。

★ 建表顺序按**外键依赖**排列（被引用者先建）：
    resumes → jobs → resume_versions → applications → {events, interviews→questions,
    assessments, offers} → companies → llm_cache

  jobs.matched_resume_id → resumes，而 resume_versions.job_id → jobs，
  两者互相引用（循环）。SQLite 在 CREATE TABLE 时**不检查外键目标是否存在**，
  所以「先建 jobs、再建 resume_versions」即可，不需要拆成 ALTER 补约束。
  （这也限制了将来换 PostgreSQL 时需要调整，已在 docs/data_model.md 记录。）

★ downgrade 必须逆序 drop：先删有外键依赖的表，否则有外键开启时会失败。
  测试会跑 upgrade → downgrade → upgrade 三连，保证迁移可逆。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. resumes —— 简历主表
    # ------------------------------------------------------------------
    op.create_table(
        "resumes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=120), nullable=True),
        sa.Column("direction", sa.String(length=60), nullable=True),
        sa.Column("file_path", sa.Text(), nullable=True),
        # content_json：结构化解析结果（JSON 字符串）
        sa.Column("content_json", sa.Text(), nullable=True),
        # ★ content_hash：LLM 缓存 key 的一部分 → 改简历自动失效旧分
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resumes")),
    )
    op.create_index("ix_resumes_content_hash", "resumes", ["content_hash"])
    op.create_index("ix_resumes_direction", "resumes", ["direction"])

    # ------------------------------------------------------------------
    # 2. jobs —— 岗位表（字段与索引最多）
    # ------------------------------------------------------------------
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("platform", sa.String(length=40), nullable=False),
        # ★ 去重第一层
        sa.Column("platform_job_id", sa.String(length=120), nullable=True),
        sa.Column("company", sa.String(length=200), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("city", sa.String(length=60), nullable=True),
        sa.Column("experience", sa.String(length=80), nullable=True),
        sa.Column("education", sa.String(length=80), nullable=True),
        # ★ nullable：NULL 表示"未识别"，不要默认 False（否则实习岗会被当全职）
        sa.Column("is_intern", sa.Boolean(), nullable=True),
        sa.Column("salary_text", sa.String(length=80), nullable=True),
        sa.Column("salary_min", sa.Integer(), nullable=True),
        sa.Column("salary_max", sa.Integer(), nullable=True),
        sa.Column("jd_text", sa.Text(), nullable=True),
        sa.Column("jd_hash", sa.String(length=64), nullable=True),
        # ★ ok / failed / partial —— 抓取失败必须留痕
        sa.Column("jd_fetch_status", sa.String(length=20), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("source_keyword", sa.String(length=120), nullable=True),
        # ★ 去重第二层兜底
        sa.Column("dedup_norm_hash", sa.String(length=64), nullable=True),
        # ★★ 共享中间层产物：被匹配 / 定制简历 / 开场白三处复用
        sa.Column("jd_keywords_json", sa.Text(), nullable=True),
        sa.Column("rule_score", sa.Integer(), nullable=True),
        sa.Column("rule_detail_json", sa.Text(), nullable=True),
        sa.Column("llm_score", sa.Integer(), nullable=True),
        sa.Column("match_score", sa.Integer(), nullable=True),
        sa.Column("matched_resume_id", sa.Integer(), nullable=True),
        sa.Column("reason_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("found_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["matched_resume_id"],
            ["resumes.id"],
            name=op.f("fk_jobs_matched_resume_id_resumes"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        # 同一平台下 platform_job_id 唯一；NULL（手动粘贴的岗位）不参与约束
        sa.UniqueConstraint(
            "platform", "platform_job_id", name="uq_jobs_platform_platform_job_id"
        ),
    )
    op.create_index("ix_jobs_platform_job_id", "jobs", ["platform_job_id"])
    op.create_index("ix_jobs_dedup_norm_hash", "jobs", ["dedup_norm_hash"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_match_score", "jobs", ["match_score"])
    # ★ 复合索引：支撑"同公司同天不推多条"
    op.create_index("ix_jobs_company_city", "jobs", ["company", "city"])

    # ------------------------------------------------------------------
    # 3. resume_versions —— AI 定制简历版本
    # ------------------------------------------------------------------
    op.create_table(
        "resume_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("resume_id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column("suggestions_json", sa.Text(), nullable=True),
        sa.Column("approved_ids", sa.Text(), nullable=True),
        # ★ FidelityGuard 检查结果（可回溯"这版为什么被拒"）
        sa.Column("fidelity_report_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["resume_id"],
            ["resumes.id"],
            name=op.f("fk_resume_versions_resume_id_resumes"),
            ondelete="CASCADE",
        ),
        # SET NULL：岗位被清理不该丢掉"我定制过一版"的记录
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_resume_versions_job_id_jobs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resume_versions")),
    )
    op.create_index("ix_resume_versions_resume_id", "resume_versions", ["resume_id"])
    op.create_index("ix_resume_versions_job_id", "resume_versions", ["job_id"])

    # ------------------------------------------------------------------
    # 4. applications —— 投递记录（当前状态）
    # ------------------------------------------------------------------
    op.create_table(
        "applications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        # 注意：jobs 这里是默认的 RESTRICT —— 有投递记录的岗位不许被静默删除
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("resume_version_id", sa.Integer(), nullable=True),
        sa.Column("platform", sa.String(length=40), nullable=True),
        sa.Column("channel", sa.String(length=20), nullable=True),
        sa.Column("greeting_text", sa.Text(), nullable=True),
        sa.Column("greeting_variant", sa.String(length=30), nullable=True),
        # ★★ 闭环字段：话术是否真的发出去了、发前是否被改过
        sa.Column("greeting_sent", sa.Boolean(), nullable=False),
        sa.Column("greeting_edited", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("viewed_at", sa.DateTime(), nullable=True),
        sa.Column("replied_at", sa.DateTime(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_applications_job_id_jobs")
        ),
        sa.ForeignKeyConstraint(
            ["resume_version_id"],
            ["resume_versions.id"],
            name=op.f("fk_applications_resume_version_id_resume_versions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_applications")),
    )
    op.create_index("ix_applications_job_id", "applications", ["job_id"])
    op.create_index("ix_applications_status", "applications", ["status"])
    # ★ 日额度与"投后 N 天"提醒都从这里聚合（不建计数表，避免不一致）
    op.create_index("ix_applications_applied_at", "applications", ["applied_at"])

    # ------------------------------------------------------------------
    # 5. application_events —— 事件流（append-only）
    # ------------------------------------------------------------------
    op.create_table(
        "application_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("from_status", sa.String(length=20), nullable=True),
        sa.Column("to_status", sa.String(length=20), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=True),
        # ★ 业务时间（真实发生，可补记）与系统时间（写库时间）分开
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_application_events_application_id_applications"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_events")),
    )
    op.create_index(
        "ix_application_events_application_id", "application_events", ["application_id"]
    )
    op.create_index("ix_application_events_occurred_at", "application_events", ["occurred_at"])

    # ------------------------------------------------------------------
    # 6. interviews —— 面试
    # ------------------------------------------------------------------
    op.create_table(
        "interviews",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("round", sa.String(length=30), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(), nullable=True),
        sa.Column("mode", sa.String(length=20), nullable=True),
        sa.Column("meeting_link", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_interviews_application_id_applications"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_interviews")),
    )
    op.create_index("ix_interviews_application_id", "interviews", ["application_id"])
    op.create_index("ix_interviews_scheduled_at", "interviews", ["scheduled_at"])

    # ------------------------------------------------------------------
    # 7. interview_questions —— 面试问题与自评
    # ------------------------------------------------------------------
    op.create_table(
        "interview_questions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("interview_id", sa.Integer(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("my_answer", sa.Text(), nullable=True),
        sa.Column("rating", sa.String(length=10), nullable=True),
        sa.Column("category", sa.String(length=60), nullable=True),
        sa.Column("is_weak", sa.Boolean(), nullable=False),
        # ★ recorded / predicted —— 必须区分，否则 `jh weak` 结论会被自己猜的题污染
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["interview_id"],
            ["interviews.id"],
            name=op.f("fk_interview_questions_interview_id_interviews"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_interview_questions")),
    )
    op.create_index(
        "ix_interview_questions_interview_id", "interview_questions", ["interview_id"]
    )
    op.create_index("ix_interview_questions_category", "interview_questions", ["category"])
    op.create_index("ix_interview_questions_is_weak", "interview_questions", ["is_weak"])

    # ------------------------------------------------------------------
    # 8. assessments —— 笔试 / 测评 / AI面（v3 新增）
    # ------------------------------------------------------------------
    op.create_table(
        "assessments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("invited_at", sa.DateTime(), nullable=True),
        # ★★ 截止时间：错过即自动淘汰，顶部提醒的数据源
        sa.Column("deadline_at", sa.DateTime(), nullable=True),
        sa.Column("platform", sa.String(length=40), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        # 用 String：很多测评不给分数只给"通过/未通过"
        sa.Column("score", sa.String(length=40), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_assessments_application_id_applications"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assessments")),
    )
    op.create_index("ix_assessments_application_id", "assessments", ["application_id"])
    op.create_index("ix_assessments_deadline_at", "assessments", ["deadline_at"])

    # ------------------------------------------------------------------
    # 9. offers —— Offer（v3 新增）
    # ------------------------------------------------------------------
    op.create_table(
        "offers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("company", sa.String(length=200), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("salary_text", sa.String(length=80), nullable=True),
        sa.Column("salary_month", sa.Integer(), nullable=True),
        sa.Column("months", sa.Integer(), nullable=True),
        sa.Column("location", sa.String(length=60), nullable=True),
        # ★★ 签约截止日
        sa.Column("deadline_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("pros", sa.Text(), nullable=True),
        sa.Column("cons", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name=op.f("fk_offers_application_id_applications"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_offers")),
    )
    op.create_index("ix_offers_application_id", "offers", ["application_id"])
    op.create_index("ix_offers_deadline_at", "offers", ["deadline_at"])

    # ------------------------------------------------------------------
    # 10. companies —— 官网/内推线索池（P7）
    # ------------------------------------------------------------------
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("official_url", sa.Text(), nullable=True),
        sa.Column("source_platform", sa.String(length=40), nullable=True),
        # ★ 官网 / 内推 / HR直招 —— 决定"该怎么投"
        sa.Column("source_type", sa.String(length=20), nullable=True),
        sa.Column("evidence_text", sa.Text(), nullable=True),
        sa.Column("evidence_url", sa.Text(), nullable=True),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_companies")),
    )
    op.create_index("ix_companies_name", "companies", ["name"])
    op.create_index("ix_companies_source_type", "companies", ["source_type"])

    # ------------------------------------------------------------------
    # 11. llm_cache —— LLM 缓存（v3 新增，无外键）
    # ------------------------------------------------------------------
    op.create_table(
        "llm_cache",
        # ★ 主键是算出来的摘要，不是自增 id → 同 key 重写天然是 UPSERT
        sa.Column("cache_key", sa.String(length=64), nullable=False),
        sa.Column("task", sa.String(length=40), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("cost", sa.Float(), nullable=True),
        sa.Column("model", sa.String(length=60), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("cache_key", name=op.f("pk_llm_cache")),
    )
    op.create_index("ix_llm_cache_task", "llm_cache", ["task"])
    op.create_index("ix_llm_cache_created_at", "llm_cache", ["created_at"])


def downgrade() -> None:
    """逆序删除（先删有外键依赖的表）。"""
    op.drop_index("ix_llm_cache_created_at", table_name="llm_cache")
    op.drop_index("ix_llm_cache_task", table_name="llm_cache")
    op.drop_table("llm_cache")

    op.drop_index("ix_companies_source_type", table_name="companies")
    op.drop_index("ix_companies_name", table_name="companies")
    op.drop_table("companies")

    op.drop_index("ix_offers_deadline_at", table_name="offers")
    op.drop_index("ix_offers_application_id", table_name="offers")
    op.drop_table("offers")

    op.drop_index("ix_assessments_deadline_at", table_name="assessments")
    op.drop_index("ix_assessments_application_id", table_name="assessments")
    op.drop_table("assessments")

    op.drop_index("ix_interview_questions_is_weak", table_name="interview_questions")
    op.drop_index("ix_interview_questions_category", table_name="interview_questions")
    op.drop_index("ix_interview_questions_interview_id", table_name="interview_questions")
    op.drop_table("interview_questions")

    op.drop_index("ix_interviews_scheduled_at", table_name="interviews")
    op.drop_index("ix_interviews_application_id", table_name="interviews")
    op.drop_table("interviews")

    op.drop_index("ix_application_events_occurred_at", table_name="application_events")
    op.drop_index("ix_application_events_application_id", table_name="application_events")
    op.drop_table("application_events")

    op.drop_index("ix_applications_applied_at", table_name="applications")
    op.drop_index("ix_applications_status", table_name="applications")
    op.drop_index("ix_applications_job_id", table_name="applications")
    op.drop_table("applications")

    op.drop_index("ix_resume_versions_job_id", table_name="resume_versions")
    op.drop_index("ix_resume_versions_resume_id", table_name="resume_versions")
    op.drop_table("resume_versions")

    op.drop_index("ix_jobs_company_city", table_name="jobs")
    op.drop_index("ix_jobs_match_score", table_name="jobs")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_index("ix_jobs_dedup_norm_hash", table_name="jobs")
    op.drop_index("ix_jobs_platform_job_id", table_name="jobs")
    op.drop_table("jobs")

    op.drop_index("ix_resumes_direction", table_name="resumes")
    op.drop_index("ix_resumes_content_hash", table_name="resumes")
    op.drop_table("resumes")

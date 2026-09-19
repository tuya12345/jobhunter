"""P6：`interview_questions` 支持"JD 预测题"（挂岗位，而不是挂某一场面试）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 这次迁移改了什么、为什么要改                                                ║
╚══════════════════════════════════════════════════════════════════════════╝

    ① `interview_id`：NOT NULL → **可空**
    ② 新增 `job_id`（FK → `jobs.id`，`ON DELETE SET NULL`）+ 索引

背景：P0 建表时假设"面试题总是某场面试里被问到的"，所以 `interview_id` 写成 NOT NULL。
但 P6 的 `jh question predict <job_id>` 是按**岗位**预测可能被问的题
（计划书【提示词 8】第 4 条），而这个动作通常发生在**面试还没约上**的时候。
坚持 NOT NULL 只有两条路，都很糟：

    · 逼用户先 `jh interview add` —— 为了塞下一道还没被问的题，先编一个面试时间；
    · 造一条假的 `interviews` 记录来挂预测题 —— 它会出现在 `jh interview list`、
      `jh stats` 的面试场次统计、以及顶部"本周面试"提醒里。
      **为了装下一条数据而污染三个功能的输入。**

所以改成：`recorded` 填 `interview_id`，`predicted` 填 `job_id`，两者互斥（不强制约束，
因为 SQLite 的 CHECK 在 batch 迁移里很难维护；由服务层保证，并有测试守住）。

★ 为什么 `job_id` 用 `SET NULL` 而不是 `CASCADE`：
  岗位记录将来被清理时，不该连带删掉"我为它准备过哪几道题"（那是我的准备成果）。
  与 `resume_versions.job_id` 同一个取舍。

★ 为什么用 `batch_alter_table`：SQLite 不支持 `ALTER COLUMN`，
  Alembic 的 batch 模式会"建新表 → 拷数据 → 换名"。
  项目里 `env.py` 已开 `render_as_batch=True`，这里显式用 batch 上下文更清楚。
  注意：本迁移**不动已有数据**——`recorded` 行的 `job_id` 保持 NULL，这是正确的
  （它们的归属本来就是面试，不是岗位）。

★ downgrade 会把 `job_id` 列与索引删掉，并把 `interview_id` 改回 NOT NULL。
  改回 NOT NULL 之前必须**先删掉 `interview_id IS NULL` 的行**（预测题）——
  否则 downgrade 会因约束冲突失败。这是有损操作，所以在 downgrade 里明确写出来：
  回滚会丢掉预测题（它们不是"真实记录"，可以接受；真实题一条都不会动）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("interview_questions") as batch:
        batch.add_column(sa.Column("job_id", sa.Integer(), nullable=True))
        batch.alter_column("interview_id", existing_type=sa.Integer(), nullable=True)
        batch.create_foreign_key(
            "fk_interview_questions_job_id_jobs",
            "jobs",
            ["job_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_interview_questions_job_id", ["job_id"])


def downgrade() -> None:
    # ★ 先把"只挂岗位"的预测题删掉：不删就改不回 NOT NULL。
    #   这是**有损回滚**，但它只影响 predicted 行（真实题不受影响）。
    op.execute("DELETE FROM interview_questions WHERE interview_id IS NULL")
    with op.batch_alter_table("interview_questions") as batch:
        batch.drop_index("ix_interview_questions_job_id")
        batch.drop_constraint("fk_interview_questions_job_id_jobs", type_="foreignkey")
        batch.drop_column("job_id")
        batch.alter_column("interview_id", existing_type=sa.Integer(), nullable=False)

"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

# Alembic 需要的两个标识
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """升级。

    ★ SQLite 注意：改列/删列/加约束请用 batch 模式：
          with op.batch_alter_table("jobs") as batch_op:
              batch_op.add_column(sa.Column("new_col", sa.String(20)))
      env.py 里已开 render_as_batch=True，但**迁移脚本里仍要显式用 batch_alter_table**，
      否则 autogenerate 生成的 ALTER 在 SQLite 上会直接失败。
    """
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """降级（必须能让 upgrade → downgrade → upgrade 反复跑通）。"""
    ${downgrades if downgrades else "pass"}

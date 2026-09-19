"""`llm_cache` 加 `hits` / `last_hit_at` 两列（ISSUE #25）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 这次迁移改了什么、为什么要改                                                ║
╚══════════════════════════════════════════════════════════════════════════╝

    ① 新增 `hits`（INTEGER NOT NULL DEFAULT 0）—— 这行结果被复用了几次
    ② 新增 `last_hit_at`（DATETIME，可空）—— 最近一次被复用的时间
    ③ 给 `last_hit_at` 不加索引（见下）

背景：`jh cache stats` 要回答"缓存到底有没有用"，而唯一算不出来的数字就是
**命中率** —— 表里只记了"这一行是多少钱算出来的"，没记"这一行被复用了几次"。
每次命中写一张流水表也能算，但那会让 `jh cache clear` 的承诺变得自相矛盾
（详见 `models/llm_cache.py` 与 `services/cache_stats_svc.py` 的说明），
所以选择"每行两列 + 在唯一入口 `llm/cache.py::get_cached()` 自增"。

★ 口径（写进 `jh cache stats` 的输出，用户不该自己猜）：
      命中率 = Σhits / (Σhits + 行数)，每一行 = 一次真实付费调用。
  也就是说 `hits` 的语义是"**当前这份**缓存结果被复用了几次"，
  不是"这个任务历史上被复用几次"。

★ 为什么默认值是 `server_default="0"` 而不只是 ORM 的 `default=0`：
  已存在的行在 ALTER TABLE 时不会经过 ORM，拿不到 Python 侧默认值；
  给 server_default 才能保证老行被填成 0（而不是 NULL）。
  `hits` 为 NULL 会让命中率算不出来（NULL + 行数 = NULL），而那正是本迁移要修的毛病。
  注意 `llm_cache` 的主键是业务算出来的 `cache_key`，所以老行的 `hits=0` 是对的：
  迁移之前根本没有"命中计数"这件事，从 0 开始数，不假装知道历史。

★ 为什么 `last_hit_at` 可空且**不建索引**：
  可空是因为"从没被命中过"就是一个真实状态（NULL 表示没数据，而不是"0 次"——
  与 `hits=0` 配合才能区分）。索引没必要：`jh cache stats` 是按 task 分组
  全表聚合（已有 `ix_llm_cache_task`），行数量级是几百到几千，
  再加一个只有统计页面才碰的索引属于白付写入代价。

★ 为什么用 `batch_alter_table`：SQLite 不支持 `ALTER COLUMN`，
  Alembic 的 batch 模式会"建新表 → 拷数据 → 换名"（同 0002 / 0003）。
  这里只是加两列，batch 与直接 `add_column` 都能过，但**统一用 batch**
  是为了移动端/旧 SQLite 上行为一致（本项目 `env.py` 已开 `render_as_batch=True`）。

★ downgrade 把两列删掉即可：这两列是**统计口径**，不是业务数据，
  回滚时丢掉命中计数是可以接受的（缓存本身是纯派生数据）。
  注意 downgrade **不删 `llm_cache` 的行**（缓存内容本身与这两列无关）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("llm_cache") as batch:
        # server_default 让**已有行**也被填成 0（见模块 docstring）
        batch.add_column(
            sa.Column("hits", sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch.add_column(sa.Column("last_hit_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("llm_cache") as batch:
        batch.drop_column("last_hit_at")
        batch.drop_column("hits")

"""一个岗位只能有一条投递记录 —— 把"幂等"从注释里的约定变成**库约束**。

╔══════════════════════════════════════════════════════════════════════════╗
║ 这次迁移改了什么、为什么要改（只读代码审查 JH-11）                          ║
╚══════════════════════════════════════════════════════════════════════════╝

`applications` 一直有一句注释："一个岗位理论上只会有一条（投递记录）"。
维持它的只有 `apply_svc.ensure_pending_application()` 的"先查后插"——
而"先查后插"在真实世界里会漏：

    · 页面上的两个标签页几乎同时点"标记投递"；
    · 两个进程（`jh leads serve` + `jh today`）撞在一起；
    · 一个 HTTP 请求被重发。

两边都查不到 → 各插一行。**后果不是"多一条记录"这么轻**：

    · `count_applied_today()` 把同一个岗位算成投了两次 → 日额度假了；
    · `jh stats` 的渠道转化率分母跟着一起假；
    · `get_latest_application()` 只取 `id` 最大的那条，另一条从此变成幽灵。

所以这里把那句注释变成唯一索引。键**只是 `job_id`**，不是 `(company_id, job_id)`：

    投递是"针对某个岗位"的一次动作，`company_id` 只是"这次投递是从哪条线索
    记来的"这个**溯源**信息（可空 —— BOSS 上投的、`jh apply` 记的都没有线索）。
    把它算进键里，等于允许同一岗位有"线索 A 的一条 + 线索 B 的一条"，
    那正是把一次投递算成两次。线索归属撞车由服务层显式处理
    （`leads_svc.link_lead_to_application()` 不静默改归属）。

★ 为什么不是"新增一个索引"而是**重建同名索引**：`ix_applications_job_id`
  已经存在（普通索引），唯一索引同样能服务 `WHERE job_id = ?` 的查询。
  留两个索引只是白白多一份写放大，所以这里先删后建、沿用同一个名字 ——
  这样模型层 `Index("ix_applications_job_id", "job_id", unique=True)` 与库里
  的名字对得上（本项目靠"模型与迁移同名"这类判据做过好几道守卫）。

★ 为什么升级前要**检测重复并报错**，而不是"删掉多余的行"

    唯一索引在存在重复行时会直接失败，报的是 SQLite 的
    `UNIQUE constraint failed` —— 用户看不懂，也不知道该怎么办。
    而"自动挑一条留下"更糟：投递记录是用户的资产，丢掉哪一条都可能丢掉
    一次真实的投递（以及它的事件流）。所以这里先查一遍，把重复的 `job_id`
    列出来并给出两条可操作的处理路径（见 `_check_no_duplicates()`）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None

_INDEX_NAME = "ix_applications_job_id"


def upgrade() -> None:
    _check_no_duplicates()
    op.drop_index(_INDEX_NAME, table_name="applications")
    op.create_index(_INDEX_NAME, "applications", ["job_id"], unique=True)


def downgrade() -> None:
    """回到"只是普通索引"（记录本身一条都不动）。"""
    op.drop_index(_INDEX_NAME, table_name="applications")
    op.create_index(_INDEX_NAME, "applications", ["job_id"], unique=False)


def _check_no_duplicates() -> None:
    """库里已经有"同一岗位多条投递"时**明确报错**，并说清怎么处理。

    ★ 报错只列 id（`job_id` / 记录 id），**不含任何用户内容** ——
      库里的公司名、备注都可能带隐私，迁移日志不该成为新的泄露面。
    """
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT job_id, COUNT(*) AS n, MIN(id) AS first_id, MAX(id) AS last_id "
            "FROM applications GROUP BY job_id HAVING COUNT(*) > 1 "
            "ORDER BY job_id"
        )
    ).fetchall()

    if not rows:
        return

    detail = "；".join(
        f"岗位 #{row.job_id} 有 {row.n} 条（记录 id {row.first_id}…{row.last_id}）" for row in rows[:5]
    )
    more = "" if len(rows) <= 5 else f"（还有 {len(rows) - 5} 个岗位同样重复，未逐一列出）"
    raise RuntimeError(
        "迁移 0006 中止：库里存在「同一个岗位多条投递记录」，"
        "无法建立唯一索引（因为那会让'哪一条是真的'变成一个不确定的选择）。\n"
        f"  重复：{detail}{more}\n"
        "  处理办法（二选一，都在 `jh stats` / `jh apply --pending` 里能看到这些记录）：\n"
        "    · 确认是同一件事被记了两次 → 删掉多余的那几条"
        "（`sqlite3 <你的库> \"DELETE FROM applications WHERE id = <多余的id>\"`），\n"
        "    · 其实是两次不同的投递 → 说明它们本该挂在不同的岗位上，"
        "改掉其中一条的 `job_id`（先确认岗位 id 再改）。\n"
        "  为什么不让迁移替你挑：投递记录是你的资产，"
        "丢掉哪一条都可能丢掉一次真实投递和它的事件流 —— 这种事必须由人决定。"
    )

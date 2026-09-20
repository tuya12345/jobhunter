"""P7 消费端：`companies` 加导入所需的列（口径与溯源），并让"同名合并"成为**库约束**。

╔══════════════════════════════════════════════════════════════════════════╗
║ 这次迁移改了什么、为什么要改                                                ║
╚══════════════════════════════════════════════════════════════════════════╝

用户 2026-09-19 把 P7 拆成"采集端（独立部署 leadhunter）+ 消费端（本仓库）"，
本仓库从此只做三件事：`companies` 表 + `jh leads import` + `jh leads serve` 页面。
采集端会通过 JSON 快照把这几列带过来（规格 `docs/P7-消费端规格.md` §3.1）：

    ① `http_status`（INTEGER，可空）  —— 采集端做的 URL 可达性检查结果
    ② `checked_at` （DATETIME，可空） —— 那次检查是什么时候做的
    ③ `external_id`（VARCHAR(64)，可空，带索引）—— 采集端那条线索的 id，**只做溯源**
    ④ `collected_at`（DATETIME，可空）—— 采集端**采集到**这条线索的时间

★★ 第五列 `name_norm`：让"导入幂等"变成库层面的约束，而不是一句口号

    规格把"同一份 JSON 导两次 → 新增 0 条、更新 N 条"列为硬要求，匹配口径是
    **公司名归一化**。如果只在服务层"查一下有没有同名"再决定插入还是更新，
    这个不变量就依赖"所有写入口都记得先查" —— 而这个项目已经因为
    "同一份数据多个写入口、口径不一致"吃过两次亏（#49 / #57），
    也刚因为"形参加了但没人传"吃过三次（#37 / #24 / #55）。

    所以这里把归一化后的名字**存成一列**并加 **UNIQUE 索引**：
    重复导入在数据库层就插不进去第二行，"新增 0 条"不再靠代码自觉。

    匹配键是「**去掉所有空白**后的公司名」—— 与采集端（`leadhunter`）内部的合并
    口径**一字不差**。刻意**不**用本仓库的 `normalize_for_dedup()`：那是给 JD 标题
    去重用的，会把「星澜科技有限公司」归一成「星澜」而「星澜科技」保持原样 ——
    消费端若比采集端合并得更狠，就会把生产者刻意分开的两家公司并成一条
    （丢线索，还会撞这里的唯一索引）。最该避免的就是这种"错合并"。

★★ 第六列 `applications.company_id`：让"这条线索投过没"有**明确**答案

    页面上要显示"这条线索已经投过了"，而 `applications` 原本只有
    `channel`（招聘网站/官网/内推/HR直招）这一个来源线索 —— 用它反推
    "哪条线索投的"是**猜**：同渠道的另一条线索会把这里也点亮，
    于是页面显示"已投"而对不上任何真实动作（#57 就是这类"看不出真相"的毛病）。

    所以加一列可空的 `company_id`（外键 → `companies.id`，带索引）：
    只有"从线索页标记投递"产生的记录才会填它，`jh apply` 那条路径留空。
    可空是刻意的：绝大多数投递（BOSS 上投的）本来就没有对应线索。

★ 为什么 `name_norm` 的回填要**检测冲突并报错**（而不是"挑一条留下"）

    这一列要变成 NOT NULL + UNIQUE。如果库里已经存在"归一化后同名"的两行
    （理论上不该有：`companies` 此前只能由 P7 的占位命令接触，实际是空表），
    迁移**必须失败并说清楚**，而不是替用户挑一条、把另一条悄悄丢掉 ——
    "猜哪条对"正是本项目反复拒绝做的事。报错信息里给出表名与处理办法。

★ 为什么 `external_id` 用字符串而不是整数

    快照里采集端的 id 现在是整数，但它是**别的仓库**的主键：将来换实现、
    换机器、改自增策略都可能变。溯源字段只需要"能对上"，不需要参与计算，
    所以统一存字符串（导入时把整数转成 str），以后出现非数字 id 也不会撑爆。

★ downgrade：删掉这五列（含索引）。迁移前的 `companies` 只有
  name/official_url/source_platform/source_type/evidence_*/verified/note/created_at，
  这五列全是 P7 消费端带进来的新信息，删掉即可回到旧形状；
  **不删任何已有的线索行**。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None

_ADDED_COLUMNS = (
    "name_norm",
    "collected_at",
    "external_id",
    "checked_at",
    "http_status",
)


def upgrade() -> None:
    with op.batch_alter_table("companies") as batch:
        batch.add_column(sa.Column("http_status", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("checked_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("external_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("collected_at", sa.DateTime(), nullable=True))
        # 先允许为空，回填之后再收紧成 NOT NULL（SQLite 只能建表重写，故用 batch）
        batch.add_column(sa.Column("name_norm", sa.String(length=200), nullable=True))

    _backfill_name_norm()

    with op.batch_alter_table("companies") as batch:
        batch.alter_column(
            "name_norm",
            existing_type=sa.String(length=200),
            nullable=False,
        )

    op.create_index("ix_companies_external_id", "companies", ["external_id"])
    # ★ 唯一：同名（归一化后）的公司只允许一行 —— 导入幂等的库层保障
    op.create_index("ix_companies_name_norm", "companies", ["name_norm"], unique=True)

    # ★ 线索 → 投递记录的**明确**关联（页面显示"这条线索投过没"要用它）。
    #   可空：`jh apply` 那条路径（BOSS 上投的）本来就没有对应线索。
    with op.batch_alter_table("applications") as batch:
        batch.add_column(sa.Column("company_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_applications_company_id", "companies", ["company_id"], ["id"])
    op.create_index("ix_applications_company_id", "applications", ["company_id"])


def _backfill_name_norm() -> None:
    """给已有行补 `name_norm`；出现"归一化后同名"就**明确报错**（见模块 docstring）。

    ★ 规则**抄**在这里而不是 import 服务层：迁移脚本是"历史中的一个固定动作"，
      它必须永远按当时的口径跑。如果哪天 `leads_svc.company_key()` 改了规则，
      import 进来的话这条迁移在新库上的行为就跟着变 —— 那正是迁移最不该有的性质。
      （口径本身：去掉所有空白，与采集端 `leadhunter` 一致。）
    """
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, name FROM companies")).fetchall()

    seen: dict[str, int] = {}
    collisions: list[tuple[str, int, int]] = []
    for row_id, name in rows:
        normalized = "".join(str(name or "").split())
        if not normalized:
            # 公司名为空/全是空白 → 没有可用的匹配键。用 id 兜底，
            # 保证 NOT NULL 能加上，同时**不改写**用户看到的名字。
            normalized = f"（无法归一化）{row_id}"
        if normalized in seen:
            collisions.append((normalized, seen[normalized], row_id))
            continue
        seen[normalized] = row_id
        connection.execute(
            sa.text("UPDATE companies SET name_norm = :norm WHERE id = :row_id"),
            {"norm": normalized, "row_id": row_id},
        )

    if collisions:
        detail = "；".join(
            f"归一化名 {norm!r} 同时命中 id={first} 与 id={second}"
            for norm, first, second in collisions[:5]
        )
        raise RuntimeError(
            "迁移 0005 中止：companies 表里已经存在「归一化后同名」的线索，"
            "无法建立唯一索引（因为那会让'同名合并'变成一个不确定的选择）。\n"
            f"  冲突：{detail}\n"
            "  处理办法：先手工确认这几行是不是同一家公司 —— 是，就合并成一行"
            "（把有用的备注/证据并过去）；不是，就改掉其中一条的名字让它可区分。\n"
            "  为什么不让迁移替你挑：丢掉哪一条都可能丢掉一条真实线索，"
            "这种事必须由人决定。"
        )


def downgrade() -> None:
    op.drop_index("ix_applications_company_id", table_name="applications")
    with op.batch_alter_table("applications") as batch:
        batch.drop_constraint("fk_applications_company_id", type_="foreignkey")
        batch.drop_column("company_id")

    op.drop_index("ix_companies_name_norm", table_name="companies")
    op.drop_index("ix_companies_external_id", table_name="companies")
    with op.batch_alter_table("companies") as batch:
        for column in _ADDED_COLUMNS:
            batch.drop_column(column)

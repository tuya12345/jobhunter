"""`application_events.event_type` 加 CHECK 约束（ISSUE #42）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 这次迁移改了什么、为什么要改                                                ║
╚══════════════════════════════════════════════════════════════════════════╝

    `application_events.event_type` 原来是自由的 `String(40)`，没有任何约束。
    后果：拼错的事件类型（`status_chnage`）会被**静默写进库**，而时间轴/统计
    按 `event_type` 分类 → 出现"永远对不上的分类"。

    本迁移把它收紧成 `models/constants.py` 的 `EVENT_TYPES` 八取一：

        status_change / greeting_sent / greeting_edited / followup / note /
        assessment_invite / interview_scheduled / offer_received

★ 为什么用 `batch_alter_table`：SQLite 不支持 `ALTER TABLE ... ADD CONSTRAINT`，
  Alembic 的 batch 模式会"建新表 → 拷数据 → 换名"。项目 `env.py` 已开
  `render_as_batch=True`，这里显式用 batch 上下文更清楚（同 0002）。

★★ 已有非法数据怎么处理（**必须选一种并写下来**）：
  选的是 **① 先修数据，再升级**（不是"报错让用户自己清"）。

  理由：`application_events` 是 append-only 的**历史**，里面可能有真实投递记录。
  直接 raise 会让升级失败，用户被卡在一个既升不上去、也不敢删库的位置 ——
  而这条迁移的全部价值只是"以后别再写错"。

  具体修法（`upgrade()` 里逐条可读）：
    · value 在 EVENT_TYPES 里的 → 不动（绝大多数）；
    · 其余 → 统一归一成 `status_change`（历史里唯一的**生产**写入类型，
      拼错的事件类型 99% 就出在它身上），并把**原值**写进
      `payload_json.migrated_from_event_type`，历史不丢；
      `payload_json` 原本不是对象的，就用 `{"migrated_from": ...}` 包一层。
    · 归一之后仍然 `event_type` 非法的行（NULL / 空串）→ 同样归一，
      因为列本身是 NOT NULL，理论上不会出现，但迁移不能因为这个失败。

  ⚠️ 这条修法是**有损的**（原值从 `event_type` 列搬进了 payload）。
  选择它的理由：另一个选项是"报错并让用户手工改库"，那是把工具的纪律
  转嫁成用户的事故。归一 + 留痕是"可回滚、可追溯"的那一个。

★ downgrade 只删约束、**不回滚数据归一**：被归一的事件类型没有可靠的反向
  映射（payload 里的原值可以回填，但那会让"回滚迁移"变成一次数据改写，
  风险大于收益）。downgrade 的承诺是"约束消失、表结构回到 0002"，
  数据层面保持升级后的形态 —— 这一点在这里写明。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import column, table

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 与 `jobhunter.models.constants.EVENT_TYPES` **逐字一致**。
#: ★ 刻意在这里写一份字面量而不是 import 常量：迁移脚本一旦提交就是**历史**，
#:   它必须能在"常量已经改过好几版"的将来仍然跑出当时的结果。
#:   两份不一致时 `tests/unit/test_event_type_constraint.py` 会立刻变红。
EVENT_TYPES: tuple[str, ...] = (
    "status_change",
    "greeting_sent",
    "greeting_edited",
    "followup",
    "note",
    "assessment_invite",
    "interview_scheduled",
    "offer_received",
)

#: 约束名。必须显式给（SQLite 的 batch 模式要求约束有名字），
#: 且与模型 `__table_args__` 里的 `ck_application_events_event_type` 同名。
CONSTRAINT_NAME = "ck_application_events_event_type"

#: 归一的目标值（见模块 docstring 的"已有非法数据怎么处理"）
_FALLBACK_EVENT_TYPE = "status_change"


def _check_expression() -> str:
    """CHECK 的 SQL 表达式（单一来源：upgrade 与模型必须逐字一致）。"""
    values = ", ".join(f"'{value}'" for value in EVENT_TYPES)
    return f"event_type IN ({values})"


def _repair_invalid_event_types() -> int:
    """把已有的非法 event_type 归一掉并留痕，返回被改的行数。

    ★ 为什么必须在加约束**之前**做：有非法行的表加不上 CHECK，
      迁移会以一个看不懂的 SQLite 报错结束。
    """
    events = table(
        "application_events",
        column("id", sa.Integer),
        column("event_type", sa.String),
        column("payload_json", sa.Text),
    )
    valid = list(EVENT_TYPES)

    # 用 SQL 一次修完（不把可能很多的行拉进内存）：
    #   ① 非法（含 NULL/空串）→ 归一成 status_change；
    #   ② payload_json 是合法 JSON 对象 → 塞一个 migrated_from_event_type；
    #   ③ 其余（NULL / 非对象 JSON / 坏 JSON）→ 用 json_object() 包一层。
    # `json_object` / `json_valid` 是 SQLite 3.9+ 的内置函数（Python 3.11+
    # 自带的 sqlite3 远高于这个版本）。
    repaired = (
        sa.update(events)
        .where(events.c.event_type.not_in(valid))
        .values(
            event_type=_FALLBACK_EVENT_TYPE,
            payload_json=sa.case(
                (
                    sa.and_(
                        events.c.payload_json.is_not(None),
                        sa.text("json_valid(application_events.payload_json) = 1"),
                        sa.text("json_type(application_events.payload_json) = 'object'"),
                    ),
                    sa.text(
                        "json_set(application_events.payload_json,"
                        " '$.migrated_from_event_type',"
                        " coalesce(application_events.event_type, ''))"
                    ),
                ),
                else_=sa.text(
                    "json_object('migrated_from_event_type',"
                    " coalesce(application_events.event_type, ''))"
                ),
            ),
        )
    )
    result = op.get_bind().execute(repaired)
    return int(result.rowcount or 0)


def upgrade() -> None:
    # ① 先修数据（否则 CHECK 加不上，见 `_repair_invalid_event_types` 的说明）
    repaired = _repair_invalid_event_types()
    if repaired:
        # 打一行日志比"静默改数据"诚实：升级可以被审计
        print(f"[0003] 归一了 {repaired} 条非法 event_type（原值留在 payload_json 里）")

    # ② 再加约束
    with op.batch_alter_table("application_events") as batch:
        batch.create_check_constraint(CONSTRAINT_NAME, _check_expression())


def downgrade() -> None:
    """退回 0002 的结构：只删 CHECK 约束（数据归一不回滚，见模块 docstring）。"""
    with op.batch_alter_table("application_events") as batch:
        batch.drop_constraint(CONSTRAINT_NAME, type_="check")

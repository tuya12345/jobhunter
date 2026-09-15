"""SQLAlchemy 基础设施：Base / 命名约定 / UTCDateTime / json_text。

这一个文件里塞了三个"踩过坑才会提前做"的决定，每一个都有具体事故背景：

╔══════════════════════════════════════════════════════════════════════════╗
║ 决定 1：显式命名约定（naming_convention）                                  ║
╚══════════════════════════════════════════════════════════════════════════╝

数据库里如果存在**匿名约束**，SQLite 上 Alembic 想改表时会报
"Constraint must have a name"。SQLite 不支持 `ALTER TABLE ... DROP CONSTRAINT`，
必须靠 batch 模式"建新表→拷数据→换名"，而 batch 模式**要求约束有名字**。

不提前定这个约定，等到第一次改字段时就要删库重来 —— 而那时库里已经有
真实的投递记录了。所以 P0 就把命名模板写死：

    ix_<表>_<列>         索引
    uq_<表>_<列>         唯一约束
    ck_<表>_<名>         检查约束
    fk_<表>_<列>_<目标>   外键
    pk_<表>              主键

╔══════════════════════════════════════════════════════════════════════════╗
║ 决定 2：UTCDateTime（时区感知类型）                                        ║
╚══════════════════════════════════════════════════════════════════════════╝

SQLite 只有 TEXT/REAL/INTEGER，**没有带时区的时间类型**。SQLAlchemy 的
`DateTime` 绑定器更严格：给它一个 aware 的 datetime 会直接抛

    TypeError: SQLite DateTime type only accepts Python datetime and date objects as input

（这个报错完全没提"时区"两个字，第一次遇到会一脸茫然。）

所以自定义 TypeDecorator，两端各做一步转换：

    写入：aware → astimezone(UTC) → 去掉 tzinfo → 存 naive UTC
    读出：naive → 补 tzinfo=UTC → 返回 aware

语义上"全库都是 UTC"，读出即 aware，从根上避免
`can't subtract offset-naive and offset-aware datetimes` 这个经典崩溃。
本项目到处在算"投了几天"，这条不解决就是定时炸弹。

╔══════════════════════════════════════════════════════════════════════════╗
║ 决定 3：JSON 用 Text 存 + `*_json` 后缀                                   ║
╚══════════════════════════════════════════════════════════════════════════╝

SQLite 没有原生 JSON 类型。用 Text 存 JSON 字符串，并**在列名上带 `_json` 后缀**。
好处是读代码时一眼能分清：
    `jd_keywords_json` 是字符串（要 json.loads）
    `jd_text`          是真正的正文（直接用）
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, Text, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from jobhunter.utils.time import to_naive_utc, to_utc

__all__ = ["Base", "BaseModel", "UTCDateTime", "json_text", "utc_now_default"]


# ---------------------------------------------------------------------------
# 命名约定（决定 1）
# ---------------------------------------------------------------------------
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UTCDateTime(TypeDecorator[datetime]):
    """以「naive UTC」形式存储、读写都返回 aware UTC 的 datetime 类型。

    实现细节都在上面的模块 docstring 里；这里补充两个易踩点：

    · `cache_ok = True`：告诉 SQLAlchemy 这个类型可以安全地参与缓存键计算。
      不设的话每次建表 SQL 编译都会有性能告警。

    · `process_result_value` 里必须用 `to_utc()` 而不是 `replace(tzinfo=UTC)`：
      将来如果换成 PostgreSQL（原生 timestamptz，读出来就是 aware），
      `to_utc()` 能正确处理 aware 输入，而 `replace` 会**覆盖**掉真实偏移量。
      也就是说这个类型现在是"未来可换库"的。
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        """写入数据库：任何 datetime → naive UTC。"""
        if value is None:
            return None
        return to_naive_utc(value)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        """从数据库读出：naive UTC → aware UTC。"""
        if value is None:
            return None
        return to_utc(value)


def utc_now_default() -> datetime:
    """列默认值用的"当前时间"（aware UTC）。

    单独包一层是为了让 models 里的 `default=utc_now_default` 读起来像"默认值"，
    而不是一堆 `lambda: datetime.now(UTC)`。顺带统一了 don't-use-utcnow 的纪律。
    """
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Base（决定 1 + 3）
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    """所有 ORM 模型的基类。

    `metadata` 里挂上命名约定 —— 这是**全项目唯一**该定义 metadata 的地方。
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class BaseModel(Base):
    """带自增主键的抽象基类。

    `AUTOINCREMENT` 的价值（SQLite 特有）：
        默认的 `INTEGER PRIMARY KEY` 会**复用**已删除行的 id。
        本项目 `jh apply 3=applied 7=replied` 里的"序号"是 `jh today` 清单的展示序号，
        不是主键，所以复用问题不致命；但 id 复用会让"我上次说的是第 7 条"
        这种记忆彻底失效。加上 AUTOINCREMENT 后 id 单调递增、永不复用。

        代价：SQLite 会多维护一张 `sqlite_sequence` 表，写入略慢 ——
        对本项目的量级（几十到几千条）完全无感。
    """

    __abstract__ = True

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)


def json_text(nullable: bool = True, default: str | None = None) -> Mapped[str | None]:
    """构造一个"存 JSON 字符串"的 Text 列（列名请以 `_json` 结尾）。

    用法：
        jd_keywords_json: Mapped[str | None] = json_text()

    为什么用函数而不是自定义类型：
        自定义 Type 会引入"自动 json.dumps/loads"的隐式行为，
        在调试时反而不直观（打印出来是对象还是字符串要靠猜）。
        这里保持"它就是一个字符串"，序列化用 utils/io.py 的 to_json/from_json，
        显式调用、显式可见。
    """
    return mapped_column(Text, nullable=nullable, default=default)

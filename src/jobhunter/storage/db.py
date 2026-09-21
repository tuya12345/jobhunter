"""数据库连接层：engine 创建 + PRAGMA + session 管理。

╔══════════════════════════════════════════════════════════════════════════╗
║ 三个 PRAGMA —— 一个都不能少（评审采纳项 #6）                               ║
╚══════════════════════════════════════════════════════════════════════════╝

① `journal_mode = WAL`（Write-Ahead Logging）
   默认的 rollback journal 模式下，**读会阻塞写、写会阻塞读**。
   本项目后面 P8 会加 Web 界面，那时"后台在跑匹配打分"和"前端在刷看板"
   会真的并发 → 默认模式下就是 `sqlite3.OperationalError: database is locked`。
   WAL 让读写互不阻塞，顺带崩溃恢复也更安全。

② `busy_timeout = 5000`
   即使有 WAL，写与写之间仍会互斥。默认 busy_timeout 是 0：一撞锁立刻报错。
   设成 5000ms 表示"撞锁了先等 5 秒再放弃" —— 对本项目的写入频率来说，
   5 秒足够让另一个写事务完成，用户完全感知不到。

③ `foreign_keys = ON`  ★★ 最容易被漏掉、后果最隐蔽的一个
   SQLite **默认不强制外键约束**！不开的话：
       · 插一条 application 指向不存在的 job_id → 成功，脏数据进库；
       · 删一个 job → 它的 applications 变成孤儿，`jh today` 里出现幽灵条目；
       · 所有 `ondelete=` 策略全部失效。
   而且这个坑**没有任何报错**，只能靠测试守。
   → tests/unit/test_smoke.py 里有一条专门的 IntegrityError 测试盯着它。

⚠️ 关键点：PRAGMA 是**连接级**的（foreign_keys / busy_timeout），
   所以必须挂在 `connect` 事件上，而不是只对某一次连接执行。
   注意 `journal_mode` 是**数据库级持久**设置，但每连接重设一次无害且更保险。

╔══════════════════════════════════════════════════════════════════════════╗
║ 其他决定                                                                  ║
╚══════════════════════════════════════════════════════════════════════════╝

· **不用连接池**（`NullPool`）：本项目是 CLI，每个命令一个短进程，
  用完就退。池化只会在进程退出时留下没关干净的连接。
  P8 的 Web 服务如果确有需要，再单独给 Web 配一个池化 engine。

· **`expire_on_commit=False`**：commit 之后对象属性仍可读，
  避免"提交完再打印 company 触发一次隐式查询（而 session 已关）"这种经典报错。
  对 CLI 的"写库 → 立刻打印结果"流程很关键。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from jobhunter.utils.io import ensure_dir

__all__ = [
    "check_pragmas",
    "create_db_engine",
    "database_ready",
    "get_session_factory",
    "make_session_factory",
    "session_scope",
    "sqlite_version",
]

logger = logging.getLogger(__name__)

# 期望的 PRAGMA 值 —— 测试与 `jh init` 都用它来校验
EXPECTED_PRAGMAS: dict[str, str] = {
    "journal_mode": "wal",
    "foreign_keys": "1",
    "busy_timeout": "5000",
}


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _sqlite_file_path(url: str) -> Path | None:
    """从 sqlite URL 解析出文件路径（内存库返回 None）。

    为什么需要：SQLite 在**目录不存在**时报的错是
    `unable to open database file` —— 完全没提是目录的问题，
    非常难排查。所以建 engine 前先把目录建好。
    """
    if not _is_sqlite(url):
        return None
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    raw = url[len(prefix) :]
    if not raw or raw == ":memory:":
        return None
    return Path(raw)


def apply_sqlite_pragmas(dbapi_connection: object, _connection_record: object = None) -> None:
    """在每条新连接上执行 PRAGMA（挂在 Engine 的 connect 事件上）。

    单独暴露成函数是为了让测试能直接断言"engine 上的监听器存在"，
    而不必去猜内部实现。`_connection_record` 是 SQLAlchemy 事件的第二个参数，
    这里不需要它（用下划线前缀表明"故意不用"）。
    """
    # dbapi_connection 是 sqlite3.Connection，但为了兼容将来换驱动，用鸭子类型调用
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def create_db_engine(
    url: str | None = None,
    *,
    echo: bool = False,
    ensure_directory: bool = True,
) -> Engine:
    """创建 Engine 并挂上 PRAGMA。

    Args:
        url: 连接串。None → 用配置里的（`settings.database_url`）。
        echo: 是否打印 SQL（调试用，注意 SQL 里可能有真实数据）。
        ensure_directory: 建库前先确保目录存在（默认开；测试传 False 也行）。
    """
    if url is None:
        # 局部导入避免 config → storage → config 的循环依赖
        from jobhunter.config.settings import get_settings

        url = get_settings().database_url

    if ensure_directory:
        db_file = _sqlite_file_path(url)
        if db_file is not None:
            ensure_dir(db_file.parent)

    engine = create_engine(
        url,
        echo=echo,
        future=True,
        # CLI 是短进程，不需要连接池；用 NullPool 保证连接用完即关
        poolclass=NullPool,
        connect_args={"check_same_thread": False} if _is_sqlite(url) else {},
    )

    if _is_sqlite(url):

        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_connection: object, connection_record: object) -> None:
            apply_sqlite_pragmas(dbapi_connection, connection_record)

    logger.debug("数据库 engine 已创建：%s", url)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """构造 sessionmaker。

    `expire_on_commit=False` 的理由见模块 docstring：
    CLI 里"写库 → 立刻 rich 打印"是主流程，不能让 commit 之后对象不可读。
    """
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """事务边界：正常提交，异常回滚（并重新抛出）。

    用法：
        with session_scope(session_factory) as session:
            session.add(obj)
        # 出了 with 就已经提交

    为什么要有这个上下文管理器：
      · 业务代码里手写 try/commit/except/rollback/close 很容易漏一处；
      · 漏了 rollback 的后果是"这次写失败但连接还挂在事务里"，
        后续操作全部报 "cannot start a transaction within a transaction" —— 极难定位。
    """
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def check_pragmas(engine: Engine) -> dict[str, str]:
    """读取当前连接的 PRAGMA 实际值，用于 `jh init` 自检与测试断言。

    返回值示例：{"journal_mode": "wal", "foreign_keys": "1", "busy_timeout": "5000"}

    ★ 注意 `journal_mode` 要用 `fetchone()` 读结果（它是有返回值的 PRAGMA），
      而 `foreign_keys` / `busy_timeout` 只设置不返回 ——
      写错的话会拿到 None，从而产生"看起来没生效"的误判。
    """
    result: dict[str, str] = {}
    with engine.connect() as conn:
        for pragma in ("journal_mode", "foreign_keys", "busy_timeout"):
            value = conn.execute(text(f"PRAGMA {pragma}")).scalar()
            result[pragma] = str(value).lower() if value is not None else ""
    return result


def sqlite_version() -> str:
    """返回底层 SQLite 版本。排查"为什么 WAL/迁移行为不一样"时的第一手信息。"""
    return sqlite3.sqlite_version


def get_session_factory(
    url: str | None = None,
) -> tuple[Engine, sessionmaker[Session]]:
    """按当前配置创建 engine + sessionmaker（CLI 命令的标准入口）。

    ★ 为什么每次都新建而**不做全局单例缓存**：
      CLI 是短进程，一条命令跑完就退出 —— 缓存没有收益，反而会在测试里
      造成"上一个用例的库被下一个用例复用"的串味问题
      （本项目对"测试不许碰真实库"是零容忍的，见 tests/conftest.py）。

    Returns:
        (engine, session_factory)。**调用方负责在结束时 engine.dispose()**，
        否则 Windows 上已打开的 SQLite 文件会拦住删除/重命名操作。

    ★ 不做"库不存在就自动建"的事：那是 `jh init` 的职责。
      这里的调用方应该先判断库文件是否存在，给出"请先运行 jh init"的友好提示。
    """
    engine = create_db_engine(url)
    return engine, make_session_factory(engine)


def database_ready(url: str | None = None) -> bool:
    """数据库是否**可以使用**（库文件存在、业务表在、且迁移版本是最新的）。

    ╔══════════════════════════════════════════════════════════════════════╗
    ║ 这道关卡原来只问了"表在不在" —— 于是停在旧迁移上的库被放行（ISSUES #60） ║
    ╚══════════════════════════════════════════════════════════════════════╝

    实测（验证会话在用户真实库上撞到的）：库停在迁移 `0001`，而程序需要 `0005`。
    `resumes` 表在 → 关卡放行 → 第一条 SQL 就炸 `no such column: http_status`，
    用户看到的是**原始 SQLAlchemy 堆栈**（`jh leads import`）
    或页面里**一串 500**（`jh leads serve`）。

    ★ 这正是本函数 docstring 想消灭的那种体验（"no such table 对用户完全不可读"），
      只是判据只覆盖了它被写出来时的那一个方向 —— 与 #56 是同一个形态：
      **守卫只守了"登记了的都生效"，守不住"该登记的没登记"**。

    ★ 所以现在两条都查：
        · 表在不在（旧判据）；
        · 迁移版本是不是程序认识的 head（新判据）。
      版本比程序新（用户装的是旧版程序）**也算不可用** —— 那种情况跑 jh init
      会把库降级，所以必须拦住并给另一个方向的提示（见 `schema_status`）。

    ★★ `unknown` 也算**不可用**（只读代码审查 JH-08）：

      修 #60 时这里留了一句"unknown 不当成不可用"（怕把'库其实好好的、只是版本
      读不出来'变成新的拦截点）—— 结果是**反方向的 fail-open**：
      迁移脚本目录缺失/不可读、`alembic_version` 表损坏时，命令照常往下跑，
      直到真正访问到缺失的列/表才炸出一句原始 SQL 错误。那正是本函数要消灭的体验。

      所以现在改为：**确认不了就不放行**，并给一句可操作的话
      （`SchemaStatus.message()` 对 unknown 会说明"检查迁移脚本 / 跑 jh init"）。

    Raises: 不抛异常。任何读不出来的情况都返回 False（退回"请先运行 jh init"）。
    """
    from sqlalchemy import inspect as sa_inspect

    from jobhunter.storage.migrate import schema_status

    if url is None:
        from jobhunter.config.settings import get_settings

        url = get_settings().database_url

    db_file = _sqlite_file_path(url)
    if db_file is not None and not db_file.is_file():
        return False

    engine = create_db_engine(url, ensure_directory=False)
    try:
        if "resumes" not in set(sa_inspect(engine).get_table_names()):
            return False
        # ok 才可用：behind / ahead / unknown 三种都拦下（各自的提示见 message()）
        return schema_status(engine, db_url=url).is_usable
    except Exception:  # noqa: BLE001 - 库损坏/路径异常都当作"没准备好"
        return False
    finally:
        engine.dispose()


def database_problem(url: str | None = None) -> str:
    """库**为什么**不能用：返回一句中文提示（能用的库返回空串）。

    与 `database_ready()` 分开的理由：判断"能不能用"和解释"为什么不能"
    是两件事，而调用方（`require_db`）两件都要 —— 只有这样提示语才能
    区分"还没初始化"与"结构是旧的"与"程序比库旧"。
    """
    from sqlalchemy import inspect as sa_inspect

    from jobhunter.storage.migrate import schema_status

    if url is None:
        from jobhunter.config.settings import get_settings

        url = get_settings().database_url

    db_file = _sqlite_file_path(url)
    if db_file is not None and not db_file.is_file():
        return ""

    engine = create_db_engine(url, ensure_directory=False)
    try:
        if "resumes" not in set(sa_inspect(engine).get_table_names()):
            return ""
        status = schema_status(engine, db_url=url)
    except Exception:  # noqa: BLE001
        return ""
    finally:
        engine.dispose()
    return status.message()

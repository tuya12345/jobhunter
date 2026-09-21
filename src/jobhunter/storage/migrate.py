"""数据库迁移：用 Alembic 的 **Python API** 驱动 `upgrade head`。

╔══════════════════════════════════════════════════════════════════════════╗
║ 为什么用 Python API，而不是 subprocess 调 `alembic upgrade head`           ║
╚══════════════════════════════════════════════════════════════════════════╝

1. **跨平台**：subprocess 要处理 Windows 的路径反斜杠、`alembic` 是否在 PATH 里、
   中文路径引号转义……每一条都是只在别人机器上才炸的坑。
2. **错误可捕获**：`command.upgrade()` 抛的是真异常，可以用 try/except 包成
   一句中文提示；subprocess 只能拿到退出码和一堆 stderr 文本。
3. **配置同源**：DB URL 从 `settings` 单例取，和业务代码用的是**同一个**配置，
   不会出现"迁移改了这个库，程序连那个库"。

★ `render_as_batch=True`（写在 alembic/env.py 里）是 SQLite 的必需项：
    SQLite 不支持 `ALTER TABLE ... ALTER COLUMN`，Alembic 会用 batch 模式
    "建新表 → 拷数据 → 改名"来模拟。不开的话，第一次想改字段就会失败 ——
    而那时库里已经有真实投递记录了。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from alembic import command

__all__ = [
    "SchemaStatus",
    "alembic_config",
    "current_revision",
    "downgrade_base",
    "head_revision",
    "schema_status",
    "upgrade_head",
]

logger = logging.getLogger(__name__)


def alembic_config(db_url: str | None = None, *, alembic_ini: Path | None = None) -> Config:
    """构造 Alembic 配置。

    ★ 不使用 alembic.ini 里的 `sqlalchemy.url`：
      那条配置会把**本机绝对路径**写进仓库（`sqlite:///D:/Users/.../jobhunter.db`），
      既没意义又泄露目录结构。所以 URL 一律在运行时注入。
    """
    if db_url is None:
        from jobhunter.config.settings import get_settings

        db_url = get_settings().database_url

    if alembic_ini is None:
        from jobhunter.config.paths import find_project_root

        alembic_ini = find_project_root() / "alembic.ini"

    if not alembic_ini.is_file():
        raise FileNotFoundError(
            f"找不到 alembic.ini：{alembic_ini}\n"
            "请确认在项目根目录下运行，或用 JH_CONFIG 指定配置文件位置。"
        )

    cfg = Config(str(alembic_ini))
    # 让 env.py 能读到它（见 alembic/env.py 的实现）
    cfg.set_main_option("sqlalchemy.url", db_url)
    # 确保 script_location 是绝对路径 —— 否则换工作目录跑就找不到迁移脚本
    cfg.set_main_option("script_location", str(alembic_ini.parent / "alembic"))
    return cfg


def upgrade_head(db_url: str | None = None) -> None:
    """把数据库升级到最新版本（`jh init` 的第一步）。

    幂等：已经是最新版本时 Alembic 什么都不做，不会重复建表。
    """
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "head")
    logger.debug("迁移完成：head")


def downgrade_base(db_url: str | None = None) -> None:
    """把数据库降级到空（只给测试用：验证迁移可逆）。

    ★ 这个函数**绝不该**出现在任何面向用户的命令里。
      误调用等于把我的投递记录全删了。
    """
    cfg = alembic_config(db_url)
    command.downgrade(cfg, "base")


def current_revision(engine: Engine) -> str | None:
    """读当前数据库的 Alembic 版本号（`jh init` 用它打印小结、测试用它断言）。

    返回 None 表示"这个库还没被 Alembic 管过"（空库或老库）。

    ★★ 为什么把 Alembic 自己的 INFO 日志**临时静音**（ISSUES #60 的连带发现）

      `MigrationContext.configure()` 会 INFO 打印两行（「Context impl SQLiteImpl.」/
      「Will assume non-transactional DDL.」）。这在迁移时是好事（看得见发生了什么），
      但本函数现在是**每条命令都会走**的关卡（`require_db` → `database_ready`）——
      于是普通命令的输出里会平白多出两行 Alembic 日志。

      更糟的是它会把一个一直在那儿的坑引爆：项目在 setup 时把 `sys.stdout`
      **存进**了 StreamHandler，而被测试驱动时那个流已经关了 ——
      一写日志就 `ValueError: I/O operation on closed file`，
      表现为**满屏 Traceback 混在正常输出里**（`utils/logging.py` 里已另外修掉那个根因）。

      所以这里按"最小惊讶"处理：只在**这次调用**期间把它降到 WARNING，
      调完恢复 —— 不全局改日志配置（那是入口点的职责）。
    """
    with _quiet_alembic(), engine.connect() as conn:
        context = MigrationContext.configure(conn)
        return context.get_current_revision()


@contextmanager
def _quiet_alembic() -> Iterator[None]:
    """临时把 Alembic 的日志级别降到 WARNING（用完恢复原样）。"""
    target = logging.getLogger("alembic.runtime.migration")
    previous = target.level
    target.setLevel(logging.WARNING)
    try:
        yield
    finally:
        target.setLevel(previous)


def head_revision(db_url: str | None = None) -> str | None:
    """程序**认识的**最新迁移版本（从 `alembic/versions/` 读，不是写死的常量）。

    ★ 为什么必须从脚本目录读：写死一个常量，加迁移时就会忘记改它，
      而"忘记改"的表现恰好是**守卫失效**（它以为库是最新的）。
    """
    try:
        script = ScriptDirectory.from_config(alembic_config(db_url))
        return script.get_current_head()
    except Exception:  # noqa: BLE001 - 脚本目录坏了不该让"检查库"这件事崩掉
        logger.warning("读不到迁移脚本的 head 版本（alembic/versions 是否完整？）")
        return None


@dataclass(frozen=True)
class SchemaStatus:
    """库结构与程序期望的对照结果（ISSUES #60）。

    ★★ 为什么需要它：`database_ready()` 只问了一句"**表在不在**"，
      而用户的库可能停在旧迁移上（表在、列不在）—— 于是关卡放行，
      然后在第一句 SQL 上炸出 `no such column: http_status` 的原始堆栈。
      它要回答的是"**能不能用**"，而不是"**像不像一个库**"。
    """

    #: ok / behind / ahead / unknown
    state: str
    #: 库里的版本（None = 还没被 Alembic 管过）
    database: str | None = None
    #: 程序认识的版本
    head: str | None = None

    @property
    def is_usable(self) -> bool:
        return self.state == "ok"

    def message(self) -> str:
        """给用户看的中文说明（`require_db` 直接打印它）。

        ★ 四种状态的话**必须不一样** —— 尤其 ahead（程序比库旧）：
          那种情况叫用户"跑 jh init"是**错的方向**（他需要升级程序，不是升级库）。
        """
        if self.state == "behind":
            return (
                f"数据库结构是旧的（当前 {self.database}，程序需要 {self.head}）。\n"
                "  跑一下 [bold]jh init[/bold] 升级（幂等，不会动你的数据）。\n"
                "  [dim]升级只加表/加列，不改写已有内容。[/dim]"
            )
        if self.state == "ahead":
            return (
                f"数据库比程序新（库 {self.database} > 程序 {self.head}）—— "
                "你装的是**旧版本的程序**。\n"
                "  请先升级程序本身（[bold]git pull[/bold] 或 "
                "[bold]uv sync --upgrade[/bold]），不要跑 jh init：\n"
                "  那会把库降到旧结构，反而丢东西。"
            )
        if self.state == "unknown":
            # ★ unknown 也要**说清怎么办**（JH-08）：它现在会拦住命令，
            #   所以不能像原来那样返回空串让调用方去猜（那会退化成一句
            #   含糊的"请先运行 jh init"，而真正的病因可能完全不是这个）。
            detail = (
                f"（程序认识的版本：{self.head}）" if self.head else "（迁移脚本目录读不到版本号）"
            )
            return (
                "无法确认数据库结构，为安全起见先停下：迁移版本读不出来"
                f"{detail}。\n"
                "  常见原因：① `alembic/versions/` 不完整（更新程序时漏了文件）；"
                "② 库文件的 `alembic_version` 表损坏或权限不对；\n"
                "          ③ 库文件本身损坏。\n"
                "  怎么办：先在项目根跑 [bold]jh init[/bold]（幂等）—— "
                "它会重读迁移脚本；若仍报同样的错，看上面那两个原因。"
            )
        return ""


def schema_status(engine: Engine, *, db_url: str | None = None) -> SchemaStatus:
    """比较"库里的迁移版本"与"程序认识的 head"。

    ★ 返回 `unknown` 的两种情况都**不当成错误**：库读不出来（损坏/权限）、
      或脚本目录读不到 head。这时退回原来那句"请先运行 jh init"即可 ——
      修 #60 不该引入新的崩溃点。
    """
    head = head_revision(db_url)
    if head is None:
        return SchemaStatus(state="unknown")
    try:
        current = current_revision(engine)
    except Exception:  # noqa: BLE001 - 库损坏 / 表结构异常
        return SchemaStatus(state="unknown", head=head)

    if current is None:
        # 完全没有版本号：可能是空库，也可能是"表在但没被 Alembic 管过"的老库。
        # 两种情况都交给 `jh init`（它跑 upgrade head，幂等）。
        return SchemaStatus(state="behind", database=None, head=head)
    if current == head:
        return SchemaStatus(state="ok", database=current, head=head)

    # ★ 版本号是字符串，但本项目的迁移号是定宽数字（0001…0005），可以直接比大小；
    #   比不出来（比如将来换成 UUID/哈希）时保守地判成 behind —— 升级是幂等的，
    #   而"库比程序新"判错会把人引向错误的方向。
    try:
        if int(current) > int(head):
            return SchemaStatus(state="ahead", database=current, head=head)
    except ValueError:
        pass
    return SchemaStatus(state="behind", database=current, head=head)

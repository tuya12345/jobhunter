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
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine

from alembic import command

__all__ = ["alembic_config", "current_revision", "downgrade_base", "upgrade_head"]

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
    """
    with engine.connect() as conn:
        context = MigrationContext.configure(conn)
        return context.get_current_revision()

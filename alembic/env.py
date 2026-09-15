"""Alembic 运行环境。

三个关键配置，每一个都对应一个真实的坑：

① **连接串从代码注入**（不读 alembic.ini 的 sqlalchemy.url）
   避免把本机绝对路径提交进仓库；也保证迁移用的库和程序连的库是同一个。
   优先级：`-x db_url=...`（测试用） > alembic.ini 里注入的值 > settings 推导。

② **`render_as_batch=True`（★ SQLite 必需）**
   SQLite 不支持 `ALTER TABLE ... ALTER COLUMN`。Alembic 的 batch 模式会
   "建新表 → 拷数据 → 改名" 来模拟改列操作。
   不开这个，第一次想给 jobs 加个字段就会失败 —— 而那时库里已经有真实投递记录了。
   本项目 100% 用 SQLite，所以永久开启。

③ **`compare_type=True`**
   autogenerate 时比较列类型（否则把 String(80) 改成 String(200) 不会被识别）。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# 让 alembic 能 import 到 src 下的包（不依赖 pip install -e）
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from jobhunter.models import Base  # noqa: E402  必须导入汇总模块，否则 metadata 是空的

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ★ autogenerate 的依据：全项目的表都在 Base.metadata 里
target_metadata = Base.metadata


def _resolve_url() -> str:
    """决定这次迁移连哪个库。

    ★ 优先级里最要紧的是 `-x db_url=...`：
      测试要在一个临时库上跑"升级 → 降级 → 再升级"，
      绝不能碰真实库。这条通道让测试可以显式指定目标。
    """
    x_args = context.get_x_argument(as_dictionary=True)
    if "db_url" in x_args:
        return x_args["db_url"]

    configured = config.get_main_option("sqlalchemy.url", None)
    if configured:
        return configured

    # 兜底：直接从 settings 推导（正常情况走不到这里）
    from jobhunter.config.settings import get_settings

    return get_settings().database_url


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连数据库（`alembic upgrade head --sql`）。

    用途：想先 review 一遍迁移会执行什么 SQL 时。
    """
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：真正连库执行迁移（默认走这条）。"""
    # ensure the parent directory exists (SQLite 不会自动建目录)
    url = _resolve_url()
    if url.startswith("sqlite:///"):
        db_file = Path(url.removeprefix("sqlite:///"))
        if db_file.name and db_file.parent.name:
            db_file.parent.mkdir(parents=True, exist_ok=True)

    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = url

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # ★ 见模块 docstring ②：SQLite 改表必需
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

"""storage：数据库连接与迁移。

  db.py        engine 创建 + PRAGMA + session_scope() 上下文管理器  [P0]
  migrate.py   用 Alembic 的 **Python API** 跑 upgrade head          [P0]

★★ 三个 PRAGMA 一个都不能少（评审采纳项）：
    journal_mode = WAL      读写不互斥；将来 P8 的 Web 界面并发写不会 database is locked
    busy_timeout = 5000     撞锁时等 5 秒再报错，而不是立刻失败
    foreign_keys = ON       ★ SQLite **默认不开外键**！不开的话所有 FK 约束形同虚设，
                            脏数据会悄无声息地写进去。tests/unit/test_smoke.py 里有一条
                            专门的 IntegrityError 测试守住它。

为什么用 Alembic 的 Python API 而不是 subprocess 调 `alembic` 命令：
跨平台更稳（没有 PATH/编码/引号问题）、错误可以直接捕获、能和配置单例共用同一个 DB URL。
"""

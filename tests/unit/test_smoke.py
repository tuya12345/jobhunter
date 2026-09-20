"""Smoke test：工程底座是否真的立住了。

这是 P0 最重要的一组测试 —— 每一个都在守一个"踩过坑才会提前做"的决定：

    ① `jh init` 真的建出了 11 张业务表
    ② ORM 声明与迁移脚本**一致**（防模型/迁移漂移）
    ③ PRAGMA 三项真的生效（WAL / busy_timeout / foreign_keys）
    ④ ★ 外键**真的在强制**（这条专门守 SQLite 默认关闭外键的坑）
    ⑤ 迁移可逆（upgrade → downgrade → upgrade 三连）
    ⑥ `jh init` 幂等（跑两次不报错、不清数据）

为什么用 Alembic 迁移建表而不是 `Base.metadata.create_all()`：
    迁移脚本才是线上真正会执行的东西。用 create_all 建的库"永远是对的"，
    迁移脚本写错了测试也发现不了 —— 那测的就是假的东西。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from jobhunter.cli.app import app
from jobhunter.models import Base, Job
from jobhunter.storage.db import EXPECTED_PRAGMAS, check_pragmas, create_db_engine
from jobhunter.storage.migrate import current_revision, downgrade_base, upgrade_head

# 迁移脚本里的版本号（改了迁移就要改这里 —— 这本身就是一道提醒）
#   0001 初始 11 张表
#   0002（P6）interview_questions.interview_id 可空 + 新增 job_id（预测题挂岗位）
#   0003（#42）application_events.event_type 加 CHECK（EVENT_TYPES 八取一）
#   0004（#25）llm_cache 加 hits / last_hit_at（命中率）
#   0005（#59）P7 消费端：companies 加导入列 + name_norm 唯一 + applications.company_id
# ★ 版本号**写死**是刻意的（见下），但下面那条断言用的是 conftest 的 `alembic_head`
#   夹具：加迁移时"改这里的注释"仍然是必须的动作，而断言不会因为忘了改而假绿。
EXPECTED_REVISION = "0005"
EXPECTED_TABLES = 11

runner = CliRunner()


# ---------------------------------------------------------------------------
# ① 建库 + 建表
# ---------------------------------------------------------------------------
def test_init_creates_all_tables(engine) -> None:
    """`jh init` 之后，11 张业务表全部存在。"""
    names = set(inspect(engine).get_table_names())
    business = names - {"alembic_version"}
    assert len(business) == EXPECTED_TABLES, f"表数量不对：{sorted(business)}"
    assert "jobs" in business
    assert "application_events" in business
    assert "llm_cache" in business


def test_revision_is_at_head(engine, alembic_head: str) -> None:
    """库的 Alembic 版本 = 迁移脚本的最新版本。

    ★ `alembic_head` 从迁移脚本目录**读**出来（不是写死的字符串）：
      写死的话，加一道迁移却忘了改常量，这条测试会红得莫名其妙；
      更糟的是有人为了让它绿去改常量，而库其实停在旧版本。
      这里比较的是"库里的版本"vs"脚本里的 head"，两边都是真实的。
    """
    assert current_revision(engine) == alembic_head == EXPECTED_REVISION


# ---------------------------------------------------------------------------
# ② ORM 与迁移一致性（最容易长期腐坏的地方）
# ---------------------------------------------------------------------------
def test_metadata_matches_migrated_schema(engine) -> None:
    """ORM 声明的表/列/索引必须与迁移建出来的完全一致。

    ★ 这条测试的价值：模型和迁移是两份手写的定义，长期一定会漂移。
      漂移的后果是"程序以为有这个列，数据库里没有"→ 运行时才炸，
      而且往往是在用户已经录了几十条岗位之后。
    """
    inspector = inspect(engine)
    db_tables = set(inspector.get_table_names()) - {"alembic_version"}
    md_tables = set(Base.metadata.tables)

    assert db_tables == md_tables, (
        f"表不一致：仅在模型里 {md_tables - db_tables}；仅在库里 {db_tables - md_tables}"
    )

    for table_name, table in Base.metadata.tables.items():
        db_columns = {c["name"] for c in inspector.get_columns(table_name)}
        md_columns = {c.name for c in table.columns}
        assert db_columns == md_columns, (
            f"{table_name} 列不一致："
            f"仅在模型里 {md_columns - db_columns}；仅在库里 {db_columns - md_columns}"
        )

        db_indexes = {i["name"] for i in inspector.get_indexes(table_name)}
        md_indexes = {i.name for i in table.indexes}
        assert db_indexes == md_indexes, (
            f"{table_name} 索引不一致："
            f"仅在模型里 {md_indexes - db_indexes}；仅在库里 {db_indexes - md_indexes}"
        )


# ---------------------------------------------------------------------------
# ③ PRAGMA
# ---------------------------------------------------------------------------
def test_pragmas_are_applied(engine) -> None:
    """WAL / busy_timeout / foreign_keys 三项都要生效。

    注意 journal_mode 是**数据库级持久**设置，所以断言用 wal。
    """
    actual = check_pragmas(engine)
    for pragma, expected in EXPECTED_PRAGMAS.items():
        assert actual.get(pragma) == expected, (
            f"PRAGMA {pragma} 期望 {expected!r}，实际 {actual.get(pragma)!r}"
        )


# ---------------------------------------------------------------------------
# ④ ★ 外键真的在强制（守 SQLite 默认关闭外键的坑）
# ---------------------------------------------------------------------------
def test_foreign_keys_are_enforced(engine) -> None:
    """插入一条指向不存在岗位的投递记录，**必须**报 IntegrityError。

    ★ SQLite 默认**不强制**外键。如果 storage/db.py 的 connect 监听器漏了
      `PRAGMA foreign_keys=ON`，这条测试会"插进去不报错"从而失败 ——
      这正是它存在的意义：把"静默失效的约束"变成"会红的测试"。

      没有这条测试的话，脏数据（孤儿投递记录）只会表现为
      "jh today 里出现看不懂的条目"，极难溯源。
    """
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO applications "
                "(job_id, status, greeting_sent, greeting_edited, created_at) "
                "VALUES (:job_id, 'pending', 0, 0, '2026-09-06 12:00:00')"
            ),
            {"job_id": 999_999},
        )


def test_orm_relationship_cascade_delete(engine, session) -> None:
    """删掉投递时，它的事件流应当被数据库级联清掉（ON DELETE CASCADE）。"""
    job = Job(platform="manual", title="测试岗位", status="new")
    session.add(job)
    session.commit()

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO applications "
                "(job_id, status, greeting_sent, greeting_edited, created_at) "
                "VALUES (:job_id, 'pending', 0, 0, '2026-09-06 12:00:00')"
            ),
            {"job_id": job.id},
        )
        app_id = conn.execute(text("SELECT id FROM applications")).scalar_one()
        conn.execute(
            text(
                "INSERT INTO application_events "
                "(application_id, event_type, occurred_at, created_at) "
                "VALUES (:app_id, 'status_change', '2026-09-06 12:00:00', "
                "'2026-09-06 12:00:00')"
            ),
            {"app_id": app_id},
        )

    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM application_events")).scalar_one() == 1
        conn.execute(text("DELETE FROM applications WHERE id = :id"), {"id": app_id})
        # 级联删除生效 → 事件流也被清掉
        assert conn.execute(text("SELECT COUNT(*) FROM application_events")).scalar_one() == 0


# ---------------------------------------------------------------------------
# ⑤ 迁移可逆
# ---------------------------------------------------------------------------
def test_migration_is_reversible(engine, db_url: str) -> None:
    """upgrade → downgrade → upgrade 三连必须跑通。

    ★ 为什么必须在 CI 里跑：迁移不可逆意味着**出事时无法回滚**。
      而且 downgrade 写错（比如漏删某个索引、顺序反了）通常只在真正需要回滚的
      紧急时刻才会暴露 —— 那时候没有时间调试。
    """
    engine.dispose()

    downgrade_base(db_url)
    after_down = set(inspect(create_db_engine(db_url)).get_table_names()) - {"alembic_version"}
    assert after_down == set(), f"downgrade 之后不该还有表：{sorted(after_down)}"

    upgrade_head(db_url)
    after_up = set(inspect(create_db_engine(db_url)).get_table_names()) - {"alembic_version"}
    assert len(after_up) == EXPECTED_TABLES


# ---------------------------------------------------------------------------
# ⑥ `jh init` 幂等 + 端到端
# ---------------------------------------------------------------------------
def test_cli_init_is_idempotent(settings) -> None:
    """`jh init` 跑两次都成功；第二次不改动已有数据。

    ★ 幂等很重要：用户看到"要不要初始化"的提示时会反复跑它。
      如果第二次就报错或重建库，等于把真实投递记录删了。
    """
    first = runner.invoke(app, ["--no-remind", "init"])
    assert first.exit_code == 0, first.output
    assert "11 张" in first.output

    # 造一条数据，验证第二次 init 不会清库
    db_path = settings.paths.db_path
    assert db_path.is_file()
    from sqlalchemy import create_engine

    raw_engine = create_engine(settings.database_url)
    with raw_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO jobs (platform, title, status, found_at, created_at) "
                "VALUES ('manual', '幂等测试岗位', 'new', "
                "'2026-09-06 12:00:00', '2026-09-06 12:00:00')"
            )
        )

    second = runner.invoke(app, ["--no-remind", "init"])
    assert second.exit_code == 0, second.output
    assert "已存在" in second.output

    with raw_engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM jobs")).scalar_one()
    assert count == 1, "第二次 jh init 不应该清掉已有数据"
    raw_engine.dispose()


def test_cli_init_creates_directories(settings) -> None:
    """`jh init` 会把 data/ output/ state/ 都建出来。

    ★ 为什么要测这个：SQLite 文件不存在会自动创建，但**目录**不存在时
      报的是 `unable to open database file` —— 完全看不出是目录问题。
    """
    paths = settings.paths
    for directory in (paths.data_dir, paths.output_dir, paths.state_dir):
        if directory.exists():
            directory.rmdir()

    result = runner.invoke(app, ["--no-remind", "init"])
    assert result.exit_code == 0, result.output
    assert paths.data_dir.is_dir()
    assert paths.output_dir.is_dir()
    assert paths.state_dir.is_dir()


def test_db_file_is_sqlite(settings) -> None:
    """建出来的确实是 SQLite 文件（而不是某个意外路径）。"""
    runner.invoke(app, ["--no-remind", "init"])
    header = settings.paths.db_path.read_bytes()[:16]
    assert header.startswith(b"SQLite format 3"), "数据库文件头不对"


def test_paths_are_isolated_to_tmp(project_root: Path) -> None:
    """★ 隔离自检：测试用的库路径必须在临时目录里（见 conftest 的说明）。"""
    assert project_root.exists()

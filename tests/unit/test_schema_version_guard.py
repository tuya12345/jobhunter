"""库停在旧迁移上时，新命令必须给**中文可操作提示**，而不是原始堆栈/一串 500（ISSUE #60）。

★★ 这条的形态值得记下来

    验证会话在**用户真实库**上撞到：库停在迁移 `0001`（程序需要 `0005`）。
    `jh leads import` 渲染出一个 SQLAlchemy Traceback 面板；
    `jh leads serve` 服务能起、页面 200，但 `/api/leads` 是 **HTTP 500**。

    **根因不在 P7**：`database_ready()` 只问了一句"**表在不在**" ——
    库由 0001 建出来时 `resumes` 就在，于是关卡放行，然后在第一条 SQL 上炸掉。
    而 `require_db()` 的 docstring 恰好写着"这里换成一句『请先运行 jh init』"：
    **预料到了后果，判据没覆盖到它。**

    ★ 这与 #56（`DELIMITED_TAGS` 只守"登记了的都生效"）是同一个形态：
      **守卫只覆盖了它被写出来时的那一个方向。**

★ 所以下面的用例不只测"会不会拦"，还测**三种状态的提示语各不相同** ——
  "库比程序旧"与"程序比库旧"要用户做的事**正好相反**（跑 jh init vs 升级程序）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from alembic import command
from jobhunter.cli.app import app
from jobhunter.storage.db import create_db_engine, database_problem, database_ready
from jobhunter.storage.migrate import (
    alembic_config,
    head_revision,
    schema_status,
    upgrade_head,
)

runner = CliRunner()


@pytest.fixture
def old_db(db_url: str) -> str:
    """一个**停在 0001** 的库（正是用户真实库的状态）。

    ★ 用真实迁移链造出来，而不是手写建表：这样它和用户那个库是同一种东西。
    """
    upgrade_head(db_url)
    command.downgrade(alembic_config(db_url), "0001")
    return db_url


def _engine(url: str):
    return create_db_engine(url, ensure_directory=False)


# ===========================================================================
# 判据本身
# ===========================================================================
def test_schema_status_reports_behind(old_db: str) -> None:
    engine = _engine(old_db)
    try:
        status = schema_status(engine, db_url=old_db)
    finally:
        engine.dispose()

    assert status.state == "behind"
    assert status.database == "0001"
    assert status.head == head_revision()
    assert not status.is_usable
    # 提示语必须点出"当前什么版本、需要什么版本"，否则用户不知道该不该跑 jh init
    assert "0001" in status.message() and str(status.head) in status.message()
    assert "jh init" in status.message()


def test_schema_status_ok_on_a_fresh_database(engine, db_url: str) -> None:
    status = schema_status(engine, db_url=db_url)
    assert status.state == "ok" and status.is_usable
    assert status.message() == ""


def test_schema_status_ahead_when_the_database_is_newer(engine, db_url: str, monkeypatch) -> None:
    """★ 库比程序新（用户装的是旧版程序）：提示**必须**是另一个方向。

    这种情况叫用户"跑 jh init"是错的 —— 那会把库降到旧结构，反而丢东西。
    """
    from jobhunter.storage import migrate

    monkeypatch.setattr(migrate, "head_revision", lambda db_url=None: "0001")
    status = schema_status(engine, db_url=db_url)

    assert status.state == "ahead"
    assert not status.is_usable
    message = status.message()
    assert "升级程序" in message or "git pull" in message
    assert "不要跑 jh init" in message or "不要" in message


def test_schema_status_unknown_blocks_and_explains(engine, db_url: str, monkeypatch) -> None:
    """读不到 head（脚本目录坏了 / 库损坏）→ unknown，**拦住命令并说清怎么办**。

    ★ 这条原来是反过来的：修 #60 时写的是"unknown 不当成不可用"（怕引入新的拦截点）——
      那留下了一个**反方向的 fail-open**（只读代码审查 JH-08）：
      迁移脚本读不到时命令照常往下跑，直到真正访问缺失的列/表才炸原始 SQL 错误。
      现在改为"确认不了就不放行"，并且 `message()` 必须给出可操作的话。
    """
    from jobhunter.storage import migrate

    monkeypatch.setattr(migrate, "head_revision", lambda db_url=None: None)
    status = schema_status(engine, db_url=db_url)
    assert status.state == "unknown"
    assert not status.is_usable, "unknown 又被当成可用了（fail-open）"

    message = status.message()
    assert message, "unknown 必须给出中文说明（空串会退化成一句含糊的'请先运行 jh init'）"
    assert "无法确认数据库结构" in message
    assert "jh init" in message
    # ★ 不能把用户往错的方向引：unknown 的病因不是"库旧"
    assert "结构是旧的" not in message


# ===========================================================================
# 关卡：database_ready / database_problem
# ===========================================================================
def test_database_ready_is_false_on_an_old_migration(old_db: str) -> None:
    """★ 核心回归：表都在、但结构是旧的 → 必须**不可用**（改前这里是 True）。"""
    assert database_ready(old_db) is False, (
        "停在旧迁移上的库被判成可用了 —— 接下来就是 no such column 的原始堆栈"
    )
    assert "0001" in database_problem(old_db)


def test_database_ready_is_true_on_a_fresh_database(db_url: str) -> None:
    upgrade_head(db_url)
    assert database_ready(db_url) is True
    assert database_problem(db_url) == ""


def test_database_ready_is_false_when_the_file_is_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope.db"
    assert database_ready(f"sqlite:///{missing.as_posix()}") is False
    # 文件不存在时 database_problem 给空串 → 调用方退回"请先运行 jh init"
    assert database_problem(f"sqlite:///{missing.as_posix()}") == ""


def test_database_ready_is_false_when_the_head_is_unreadable(engine, monkeypatch) -> None:
    """★★ 回归（JH-08 的正题）：head 读不出来 → **不可用**，并给出"无法确认"的说明。

    ★ 这条与 `database_ready()` 里那句 `status.is_usable` 是**同一件事的两端**：
      只断言 `schema_status()` 的状态码，守不住"调用方有没有真的把它当回事"。
    """
    from jobhunter.storage import db as db_module
    from jobhunter.storage import migrate

    monkeypatch.setattr(db_module, "_sqlite_file_path", lambda _url: None)
    monkeypatch.setattr(db_module, "create_db_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(migrate, "head_revision", lambda db_url=None: None)

    # 先让库里真的有表（否则拦它的是"表不存在"那条旧判据，测不到 unknown）
    from sqlalchemy import Column, Integer, MetaData, Table

    Table("resumes", MetaData(), Column("id", Integer, primary_key=True)).create(
        engine, checkfirst=True
    )

    assert db_module.database_ready("sqlite:///:memory:") is False
    problem = db_module.database_problem("sqlite:///:memory:")
    assert "无法确认数据库结构" in problem


# ===========================================================================
# 用户会撞到的两个入口
# ===========================================================================
def test_leads_import_on_an_old_database_gives_chinese_hint_not_a_traceback(
    old_db: str, monkeypatch, tmp_path: Path
) -> None:
    """`jh leads import`：必须是中文提示 + 退出码 1，**不是** SQLAlchemy Traceback。"""
    monkeypatch.setenv("JH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JH_DB_URL", old_db)
    from jobhunter.config.settings import reset_settings

    reset_settings()

    snapshot = tmp_path / "snap.json"
    snapshot.write_text(
        '{"schema": "jobhunter.leads", "schema_version": 1, "leads": []}', encoding="utf-8"
    )

    result = runner.invoke(app, ["--no-remind", "leads", "import", str(snapshot), "--dry-run"])

    assert result.exit_code == 1, result.output
    assert "0001" in result.output, "没告诉用户库是什么版本"
    assert "jh init" in result.output, "没告诉用户该跑什么命令"
    assert "Traceback" not in result.output, "又把原始堆栈丢给用户了"
    assert "no such" not in result.output


def test_leads_serve_refuses_to_start_on_an_old_database(
    old_db: str, monkeypatch, tmp_path: Path
) -> None:
    """`jh leads serve`：**启动前**就要拦下来 —— 否则页面一起来就是一串 500。"""
    monkeypatch.setenv("JH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JH_DB_URL", old_db)
    from jobhunter.config.settings import reset_settings

    reset_settings()

    # 不让它真的起服务：只要走到 uvicorn.run 就说明关卡没拦住
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: pytest.fail("库结构不对却起了服务"))

    result = runner.invoke(app, ["--no-remind", "leads", "serve"])

    assert result.exit_code == 1
    assert "0001" in result.output and "jh init" in result.output


# ===========================================================================
# 接口层：万一服务已经在跑（库在运行期间被换/被降级）
# ===========================================================================
def test_api_returns_503_with_chinese_detail_instead_of_500(old_db: str) -> None:
    """库结构不对时，`/api/leads` 应当是 **503 + 中文说明**，不是 500 堆栈。"""
    from fastapi.testclient import TestClient

    from jobhunter.web.app import create_app

    with TestClient(create_app(db_url=old_db, dist_dir=Path("__missing__"))) as client:
        response = client.get("/api/leads")

    assert response.status_code == 503, f"应当是 503，实际 {response.status_code}"
    detail = response.json()["detail"]
    assert "0001" in detail and "jh init" in detail


def test_api_health_reports_the_schema_problem(old_db: str) -> None:
    """`/api/health` 仍要 200（它是探活接口），但**如实报出**结构问题。"""
    from fastapi.testclient import TestClient

    from jobhunter.web.app import create_app

    with TestClient(create_app(db_url=old_db, dist_dir=Path("__missing__"))) as client:
        payload = client.get("/api/health").json()

    assert payload["ok"] is True
    assert payload["schema_problem"] and "0001" in payload["schema_problem"]


# ===========================================================================
# `jh init` 仍然是那条出路（这条把"提示"与"能力"接上）
# ===========================================================================
def test_jh_init_actually_upgrades_the_old_database(
    old_db: str, monkeypatch, tmp_path: Path
) -> None:
    """提示里让用户跑 `jh init` —— 那就必须真的能修好（不然提示是空头支票）。"""
    monkeypatch.setenv("JH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JH_DB_URL", old_db)
    from jobhunter.config.settings import reset_settings

    reset_settings()

    result = runner.invoke(app, ["--no-remind", "init"])
    assert result.exit_code == 0, result.output

    assert database_ready(old_db) is True
    engine = _engine(old_db)
    try:
        with engine.connect() as conn:
            # 升级之后新列真的在（这是 #60 里用户缺的那一列）
            conn.execute(text("SELECT http_status FROM companies LIMIT 1"))
    finally:
        engine.dispose()

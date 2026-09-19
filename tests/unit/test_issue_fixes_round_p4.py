"""本轮修复（ISSUES #31 #34 #35 #37 #38 #42 #48 #49 #50）的单元测试。

★ 这些用例与 `tests/verify/**` 里验证会话的 xfail 用例**不重复**：
  验证用例守的是"缺陷不再发生"（外部契约），这里守的是**实现细节里的取舍**：
  迁移如何处理历史脏数据、排他创建的重试、幂等性、以及"生成 ≠ 发送"的边界。
  两边都在才叫修住了 —— 只有一边的话，改动很容易在另一条路径上退回去。
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text
from typer.testing import CliRunner

from alembic import command
from jobhunter.cli.app import app
from jobhunter.cli.commands.apply_cmd import _parse_pairs
from jobhunter.models import Application, ApplicationEvent, Job
from jobhunter.models.constants import APPLICATION_STATUSES, EVENT_TYPES, JOB_STATUSES
from jobhunter.services import apply_svc, export_svc, today_svc
from jobhunter.storage.db import create_db_engine
from jobhunter.storage.migrate import alembic_config, current_revision, upgrade_head
from tests.verify._helpers import insert_job
from tests.verify.test_v_p4_today import _seed

runner = CliRunner()

MIGRATION_0003 = "ck_application_events_event_type"

#: 当前 head 的 revision。0004（#25，llm_cache 加 hits/last_hit_at）之后
#: 它不再是那个断言里写死的 "0003" —— 这里只保留与 #42 有关的断言，
#: 把"head 是哪个版本"留给 `test_smoke.py::test_revision_is_at_head` 一处去守。
HEAD_REVISION = "0004"


# ===========================================================================
# #42 —— event_type 的 CHECK 约束（迁移 + 服务层两道）
# ===========================================================================
def test_migration_is_at_0003(engine) -> None:
    """#42 的约束在 head 版本上依然生效（revision 号本身由 smoke 守）。"""
    assert current_revision(engine) == HEAD_REVISION


def test_event_type_check_exists_in_db(engine) -> None:
    """库层面真的有这条 CHECK（不是只在模型里声明）。"""
    with engine.connect() as conn:
        ddl = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='application_events'")
        ).scalar_one()
    assert MIGRATION_0003 in ddl, f"表定义里没有 CHECK 约束：\n{ddl}"
    for value in EVENT_TYPES:
        assert f"'{value}'" in ddl, f"CHECK 里漏了合法取值 {value!r}"


def test_event_types_and_job_statuses_are_disjoint() -> None:
    """★ ISSUE #48 的前提：`skipped` 是**岗位**状态，不是投递状态。

    两套常量一旦混在一起，"投递状态"这个概念就会再次含糊。
    """
    assert "skipped" in JOB_STATUSES
    assert "skipped" not in EVENT_TYPES
    assert "skipped" not in APPLICATION_STATUSES


def test_record_event_rejects_typo_with_chinese_message(session, settings) -> None:
    """服务层先给一句人话（含可用取值），而不是让 SQLite 的约束报错冲到用户面前。"""
    jobs = _seed(session, settings, count=1)
    session.flush()
    application, _ = apply_svc.ensure_pending_application(session, jobs[0])
    session.flush()

    with pytest.raises(apply_svc.ApplicationError) as excinfo:
        apply_svc.record_event(session, application.id, "status_chnage")
    message = str(excinfo.value)
    assert "status_chnage" in message
    assert "status_change" in message, f"报错里没有可用取值：{message}"


def test_record_event_accepts_every_declared_type(session, settings) -> None:
    """8 个合法取值都要能写进去（别把 CHECK 写窄了）。"""
    jobs = _seed(session, settings, count=1)
    session.flush()
    application, _ = apply_svc.ensure_pending_application(session, jobs[0])
    session.flush()
    for event_type in EVENT_TYPES:
        apply_svc.record_event(session, application.id, event_type)
    session.flush()
    written = set(
        session.scalars(
            select(ApplicationEvent.event_type).where(
                ApplicationEvent.application_id == application.id
            )
        )
    )
    assert written == set(EVENT_TYPES)


# ---------------------------------------------------------------------------
# #42 的迁移：**已有非法数据**必须先被修好（否则升级会失败）
# ---------------------------------------------------------------------------
def _downgrade_to_0002(db_url: str) -> None:
    """把库降到 0002（此刻还没有 CHECK，可以塞脏数据进来）。"""
    command.downgrade(alembic_config(db_url), "0002")


def test_migration_repairs_invalid_event_types_before_adding_the_check(db_url) -> None:
    """★★ 迁移的可升级性：库里已有拼错的 event_type 时，升级必须**成功**并留痕。

    依据（本 issue 的硬要求）："若现有数据里已有非法 event_type，
    迁移必须先把它们修好或明确报错（不许让升级失败）"。
    这里选的是**修好 + 留痕**（原值进 payload_json），所以本用例断言：
      ① 升级不抛错；② 非法值被归一；③ 原值没有丢。
    """
    upgrade_head(db_url)
    _downgrade_to_0002(db_url)
    engine = create_db_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO jobs (platform, title, status, found_at, created_at) "
                    "VALUES ('manual', '迁移测试岗', 'new', "
                    "'2026-09-06 12:00:00', '2026-09-06 12:00:00')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO applications "
                    "(job_id, status, greeting_sent, greeting_edited, created_at) "
                    "VALUES (1, 'pending', 0, 0, '2026-09-06 12:00:00')"
                )
            )
            # 三条脏数据：典型拼错、坏 JSON 的 payload、NULL payload
            conn.execute(
                text(
                    "INSERT INTO application_events "
                    "(application_id, event_type, payload_json, occurred_at, created_at) "
                    "VALUES (1, 'status_chnage', '{\"source\": \"jh apply\"}', "
                    "'2026-09-06 12:00:00', '2026-09-06 12:00:00')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO application_events "
                    "(application_id, event_type, payload_json, occurred_at, created_at) "
                    "VALUES (1, 'Follow_Up', 'not json', "
                    "'2026-09-06 12:00:00', '2026-09-06 12:00:00')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO application_events "
                    "(application_id, event_type, payload_json, occurred_at, created_at) "
                    "VALUES (1, 'note ', NULL, "
                    "'2026-09-06 12:00:00', '2026-09-06 12:00:00')"
                )
            )
    finally:
        engine.dispose()

    upgrade_head(db_url)  # ← 不许抛错
    engine = create_db_engine(db_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT event_type, payload_json FROM application_events ORDER BY id")
            ).all()
        assert current_revision(engine) == HEAD_REVISION
    finally:
        engine.dispose()

    assert [row[0] for row in rows] == ["status_change"] * 3, (
        f"非法 event_type 没有被归一：{[row[0] for row in rows]}"
    )
    original = _json.loads(rows[0][1])
    assert original["source"] == "jh apply", "归一顺手把已有的 payload 字段弄丢了"
    assert original["migrated_from_event_type"] == "status_chnage", (
        f"原值没有留痕，历史不可回溯：{original}"
    )
    assert _json.loads(rows[1][1])["migrated_from_event_type"] == "Follow_Up"
    assert _json.loads(rows[2][1])["migrated_from_event_type"] == "note "


def test_migration_downgrade_removes_the_check_and_keeps_data(db_url) -> None:
    """downgrade 只删约束、不动数据（回滚不该顺手改历史）。"""
    upgrade_head(db_url)
    engine = create_db_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO jobs (platform, title, status, found_at, created_at) "
                    "VALUES ('manual', '回滚测试岗', 'new', "
                    "'2026-09-06 12:00:00', '2026-09-06 12:00:00')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO applications "
                    "(job_id, status, greeting_sent, greeting_edited, created_at) "
                    "VALUES (1, 'pending', 0, 0, '2026-09-06 12:00:00')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO application_events "
                    "(application_id, event_type, occurred_at, created_at) "
                    "VALUES (1, 'status_change', '2026-09-06 12:00:00', '2026-09-06 12:00:00')"
                )
            )
    finally:
        engine.dispose()

    _downgrade_to_0002(db_url)
    engine = create_db_engine(db_url)
    try:
        with engine.connect() as conn:
            ddl = conn.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='application_events'"
                )
            ).scalar_one()
            count = conn.execute(text("SELECT COUNT(*) FROM application_events")).scalar_one()
        assert MIGRATION_0003 not in ddl, "downgrade 之后 CHECK 还在"
        assert count == 1, "downgrade 把事件历史删了"
        assert current_revision(engine) == "0002"
    finally:
        engine.dispose()

    upgrade_head(db_url)  # 三连的最后一步：还能再升回去
    engine = create_db_engine(db_url)
    try:
        assert current_revision(engine) == HEAD_REVISION
        assert "application_events" in set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


# ===========================================================================
# #49 —— 两个入口的"该催谁"必须是同一个定义
# ===========================================================================
def test_pending_list_excludes_started_processes(session, settings) -> None:
    jobs = _seed(session, settings, count=4)
    session.flush()
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 9, 18, 4, 0, tzinfo=UTC)
    long_ago = now - timedelta(days=30)
    for job, status in zip(jobs, ("applied", "interview", "assessment", "replied"), strict=True):
        apply_svc.set_status(session, job.id, "applied", occurred_at=long_ago)
        session.flush()
        apply_svc.set_status(session, job.id, status, occurred_at=long_ago)
    session.flush()

    rows = apply_svc.list_actionable(session, now=now)
    by_job = {row.job_id: row for row in rows}
    assert jobs[0].id in by_job, "在等的投递（applied）应当留在待处理清单里"
    assert jobs[3].id in by_job, "对方已回复（replied）是'该我回话'，也该在清单里"
    assert jobs[1].id not in by_job, "已进面试的投递不该出现在待处理清单（更不该被劝放弃）"
    assert jobs[2].id not in by_job, "已进笔试的投递不该出现在待处理清单"
    assert "没动静" not in by_job[jobs[3].id].reason, (
        f"replied 的理由不能是'没动静'（与事实相反）：{by_job[jobs[3].id].reason}"
    )


def test_waiting_statuses_are_shared_with_reminders() -> None:
    """两个模块引用的是**同一个**常量对象（不是各写一份字面量）。"""
    from jobhunter.services import reminder_svc

    assert reminder_svc._WAITING_STATUSES is apply_svc.WAITING_STATUSES


# ===========================================================================
# #35 —— "过滤前达线"的口径
# ===========================================================================
def test_candidates_count_is_before_excluding_applied(session, settings) -> None:
    jobs = _seed(session, settings, count=3)
    session.flush()
    _jobs, before, _dropped = today_svc.select_candidates(session, settings=settings)

    apply_svc.set_status(session, jobs[0].id, "applied")
    session.flush()
    remaining, after, _dropped = today_svc.select_candidates(session, settings=settings)

    assert after == before, f"'过滤前达线'不该因为投了一条就变小：{before} → {after}"
    assert len(remaining) == before - 1, "清单本身当然要少一条（这才叫'过滤'）"


# ===========================================================================
# #34 —— `--copy N` 不能静默忽略筛选选项
# ===========================================================================
def test_copy_target_honours_min_score(session, settings) -> None:
    """显式给了 `--min-score` 就必须生效（空范围 → 明确报错，而不是照常复制）。"""
    _seed(session, settings, count=3, score_start=100)  # 100 / 95 / 90
    session.flush()
    today_svc.build_today_list(session, limit=3, settings=settings)
    session.commit()

    job, reason = today_svc.copy_target(session, 2, min_score=999, settings=settings)
    assert job is None
    assert "--min-score" in reason

    # 阈值卡在中间：第 2 条（95 分）仍在范围内
    job, reason = today_svc.copy_target(session, 2, min_score=95, settings=settings)
    assert job is not None and reason == ""
    # 而第 3 条（90 分）被这一次筛选排除掉了
    job, reason = today_svc.copy_target(session, 3, min_score=95, settings=settings)
    assert job is None and "--min-score" in reason


def test_copy_target_without_min_score_does_not_shrink_the_scope(session, settings) -> None:
    """不给 `--min-score` 时不能**额外**收缩范围 —— 用户没做筛选。

    ★ 断言方式：同一条序号（第 3 条）在"只给 `--limit 2`"时应当被拦，
      而"什么都不给"时必须能取到。如果实现里偷偷拿 `config.threshold` 去卡，
      第二条就会变成一个含糊的"不在当前待投清单里"（用户会一头雾水）。
    """
    jobs = _seed(session, settings, count=3, score_start=100)
    session.flush()
    today_svc.save_snapshot([job.id for job in jobs], settings)

    # `--limit 2` 是显式筛选 → 第 3 条要报"在 --limit 之外"
    job, reason = today_svc.copy_target(session, 3, limit=2, settings=settings)
    assert job is None and "--limit" in reason

    # 不给任何筛选 → 第 3 条取得到
    job, reason = today_svc.copy_target(session, 3, settings=settings)
    assert job is not None, reason


def test_copy_target_honours_limit(session, settings) -> None:
    _seed(session, settings, count=3, score_start=100)
    session.flush()
    today_svc.build_today_list(session, limit=3, settings=settings)
    session.commit()

    job, reason = today_svc.copy_target(session, 3, limit=2, settings=settings)
    assert job is None
    assert "--limit" in reason


def test_copy_target_rejects_out_of_range_index(session, settings) -> None:
    _seed(session, settings, count=1)
    session.flush()
    today_svc.build_today_list(session, limit=1, settings=settings)
    session.commit()

    job, reason = today_svc.copy_target(session, 9, settings=settings)
    assert job is None
    assert "没有第 9 条" in reason


# ===========================================================================
# #38 —— 导出"绝不覆盖"是**硬保证**（排他创建）
# ===========================================================================
def test_write_exclusive_never_overwrites(tmp_path: Path) -> None:
    """★ 真正的保证：两次导出落在**两个**文件上，第一个的内容一个字节都没变。"""
    first = export_svc._write_exclusive(tmp_path, "today", ".md", "第一份")
    second = export_svc._write_exclusive(tmp_path, "today", ".md", "第二份")

    assert first != second
    assert first.name == "today.md" and second.name == "today-2.md"
    assert first.read_text(encoding="utf-8") == "第一份", "第一个文件被覆盖了"
    assert second.read_text(encoding="utf-8") == "第二份"


def test_write_exclusive_survives_a_foreign_file_appearing(tmp_path: Path) -> None:
    """★ TOCTOU 场景：目标名**已经**被别的进程占住 → 必须换名，绝不覆盖。

    这是"时间戳 + exists()"那套概率性命名做不到的（ISSUES #38）。
    """
    (tmp_path / "today.md").write_text("别人的文件", encoding="utf-8")
    path = export_svc._write_exclusive(tmp_path, "today", ".md", "我的导出")
    assert path.name == "today-2.md"
    assert (tmp_path / "today.md").read_text(encoding="utf-8") == "别人的文件"


def test_export_markdown_twice_in_the_same_minute(session, settings) -> None:
    """同一分钟内连导两次（时间戳完全一样）也不许互相覆盖。"""
    _seed(session, settings, count=1)
    session.flush()
    result = today_svc.build_today_list(session, limit=1, settings=settings)
    output_dir = settings.paths.output_dir

    first = export_svc.export_markdown(result.items, output_dir=output_dir)
    original = first.read_text(encoding="utf-8")
    second = export_svc.export_markdown(result.items, output_dir=output_dir)

    assert first != second
    assert first.read_text(encoding="utf-8") == original
    assert all(path.read_text(encoding="utf-8") for path in (first, second)), (
        "有导出物是空的（说明内容没写进去）"
    )


def test_unique_path_is_only_a_name_hint(tmp_path: Path) -> None:
    """`unique_path` 依旧只负责给候选名（保证来自排他创建，不是它）。"""
    assert export_svc.unique_path(tmp_path, "today", ".md").name == "today.md"
    (tmp_path / "today.md").write_text("占位", encoding="utf-8")
    assert export_svc.unique_path(tmp_path, "today", ".md").name == "today-2.md"


# ===========================================================================
# #37 —— 闭环字段：生成 ≠ 发送
# ===========================================================================
def _pending_with_greeting(session, settings):  # noqa: ANN001, ANN202
    jobs = _seed(session, settings, count=1)
    session.flush()
    result = today_svc.build_today_list(session, limit=1, settings=settings)
    session.flush()
    application = session.scalars(select(Application).where(Application.job_id == jobs[0].id)).one()
    assert application.greeting_text, "测试前提：这条投递应当已经有开场白"
    assert result.items
    return jobs[0], application


def test_generating_a_greeting_does_not_set_sent(session, settings) -> None:
    """★★ 合规红线：**只是生成**话术，绝不能写 greeting_sent / greeting_edited。"""
    _job, application = _pending_with_greeting(session, settings)
    assert application.greeting_sent is False, "生成开场白就被当成'发出去了'"
    assert application.greeting_edited is False
    events = session.scalars(
        select(ApplicationEvent.event_type).where(ApplicationEvent.application_id == application.id)
    ).all()
    assert "greeting_sent" not in events
    assert "greeting_edited" not in events


def test_mark_greeting_sets_columns_and_events(session, settings) -> None:
    job, application = _pending_with_greeting(session, settings)
    apply_svc.mark_greeting(session, job.id, sent=True, edited=True)
    session.flush()

    assert application.greeting_sent is True
    assert application.greeting_edited is True
    events = session.scalars(
        select(ApplicationEvent.event_type).where(ApplicationEvent.application_id == application.id)
    ).all()
    assert events.count("greeting_sent") == 1
    assert events.count("greeting_edited") == 1


def test_mark_greeting_is_idempotent(session, settings) -> None:
    """重复声明同一个事实不该在时间轴上留下两条一模一样的痕迹。"""
    job, application = _pending_with_greeting(session, settings)
    apply_svc.mark_greeting(session, job.id, sent=True)
    session.flush()
    apply_svc.mark_greeting(session, job.id, sent=True)
    session.flush()
    events = session.scalars(
        select(ApplicationEvent.event_type).where(ApplicationEvent.application_id == application.id)
    ).all()
    assert events.count("greeting_sent") == 1


def test_set_status_applied_sets_greeting_sent_column(session, settings) -> None:
    """有开场白时标 applied → 两列里的 greeting_sent 要真的置位（原来是恒 0）。"""
    job, application = _pending_with_greeting(session, settings)
    apply_svc.set_status(session, job.id, "applied")
    session.flush()
    assert application.greeting_sent is True


def test_set_status_explicit_false_can_correct_it(session, settings) -> None:
    """只投了简历、没发开场白 → 显式传 False 纠正，且不写 greeting_sent 事件。"""
    job, application = _pending_with_greeting(session, settings)
    apply_svc.set_status(session, job.id, "applied", greeting_sent=False)
    session.flush()
    assert application.greeting_sent is False
    events = session.scalars(
        select(ApplicationEvent.event_type).where(ApplicationEvent.application_id == application.id)
    ).all()
    assert "greeting_sent" not in events


def test_applying_before_generating_backfills_the_trace(session, settings) -> None:
    """★ 先打标、后生成开场白：痕迹要补上（否则闭环统计会安静漏掉这一类）。"""
    jobs = _seed(session, settings, count=1)
    session.flush()
    apply_svc.set_status(session, jobs[0].id, "applied")
    session.flush()
    application = session.scalars(select(Application).where(Application.job_id == jobs[0].id)).one()
    assert application.greeting_text is None
    events_before = session.scalars(
        select(ApplicationEvent.event_type).where(ApplicationEvent.application_id == application.id)
    ).all()
    assert "greeting_sent" not in events_before

    today_svc.ensure_item(session, jobs[0], 1, settings=settings)
    session.flush()

    # 生成之后开场白补上了；闭环字段由人声明（这里用 mark_greeting 显式声明"发了"）
    assert application.greeting_text
    apply_svc.mark_greeting(session, jobs[0].id, sent=True)
    session.flush()
    events_after = session.scalars(
        select(ApplicationEvent.event_type).where(ApplicationEvent.application_id == application.id)
    ).all()
    assert events_after.count("greeting_sent") == 1


# ===========================================================================
# #48 —— 设计文档范例 `12=skipped`
# ===========================================================================
def test_pair_syntax_accepts_skipped_but_not_arbitrary_job_statuses() -> None:
    parsed, errors = _parse_pairs(["12=skipped"])
    assert not errors
    assert parsed == [("12", "skipped")]

    parsed, errors = _parse_pairs(["12=queued"])  # 也是岗位状态，但**不是**本 issue 的诉求
    assert errors and not parsed


def test_skip_job_marks_job_and_leaves_no_false_investment(session, settings) -> None:
    """标 skipped 只动岗位状态；没有投递记录时**不**凭空造一条。"""
    jobs = _seed(session, settings, count=1)
    session.flush()
    session.execute(text("DELETE FROM applications"))
    session.flush()

    result = apply_svc.set_status(session, jobs[0].id, "skipped")
    session.flush()
    assert result.to_status == "skipped"
    assert jobs[0].status == "skipped"
    assert session.scalars(select(Application)).all() == []
    assert session.scalars(select(ApplicationEvent)).all() == []


def test_skip_job_with_pending_application_writes_a_note_not_a_status_change(
    session,
    settings,
) -> None:
    """有 pending 投递时：置 rejected 退出待处理清单，但**不声称**一次投递状态流转。"""
    job, application = _pending_with_greeting(session, settings)
    before = session.scalars(
        select(ApplicationEvent.event_type).where(ApplicationEvent.application_id == application.id)
    ).all()
    assert "status_change" not in before

    apply_svc.set_status(session, job.id, "skipped")
    session.flush()
    events = session.scalars(
        select(ApplicationEvent.event_type).where(ApplicationEvent.application_id == application.id)
    ).all()
    assert job.status == "skipped"
    assert application.status == "rejected"
    assert "note" in events
    assert "status_change" not in events, "标 skipped 竟然写了一条投递状态流转事件"


def test_skip_job_refuses_an_already_applied_job(session, settings) -> None:
    job, _application = _pending_with_greeting(session, settings)
    apply_svc.set_status(session, job.id, "applied")
    session.flush()
    with pytest.raises(apply_svc.ApplicationError):
        apply_svc.set_status(session, job.id, "skipped")


# ===========================================================================
# #50 —— 每个 token 都要有归属
# ===========================================================================
def test_batch_mixed_with_keyword_is_rejected(session, settings) -> None:
    jobs = _seed(session, settings, count=2)
    session.commit()
    before = [job.status for job in jobs]

    result = runner.invoke(app, ["--no-remind", "apply", "1=applied", "复检公司"])
    assert result.exit_code != 0, f"混合输入被静默接受了：\n{result.output}"
    session.expire_all()
    assert [session.get(Job, job.id).status for job in jobs] == before


def test_batch_pair_conflicting_with_status_option_is_rejected(session, settings) -> None:
    _seed(session, settings, count=1)
    session.commit()
    result = runner.invoke(app, ["--no-remind", "apply", "1=applied", "--status", "rejected"])
    assert result.exit_code != 0
    assert "两遍" in result.output or "--status" in result.output


def test_pairs_with_keywords_and_status_is_still_allowed(session, settings) -> None:
    """对照（应当通过）：给了 --status 时，序号/关键词与批量对可以混写。"""
    _seed(session, settings, count=3)
    session.commit()
    result = runner.invoke(app, ["--no-remind", "apply", "1", "3=replied", "--status", "viewed"])
    assert result.exit_code == 0, f"合法的混写被拒了：\n{result.output}"


# ===========================================================================
# #39 —— 无简历分支：TodayItem 必须带 job（渲染读 item.job.city）
# ===========================================================================
def test_ensure_item_without_any_resume_still_carries_the_job(session, settings) -> None:
    """库里没有简历时也要能构造出可渲染的 TodayItem（漏传 job 会 AttributeError）。"""
    job = insert_job(session)
    session.flush()

    item = today_svc.ensure_item(session, job, 1, settings=settings)

    assert item.job is job
    assert item.job.city == "深圳"
    assert item.title == "AI Agent 开发工程师"
    assert item.greeting_issues, "没有简历时应当明说'无法生成开场白'"


def test_today_list_renders_without_any_resume(session, settings) -> None:
    """整条路径：**库里没有简历**时 `jh today` 也要能出清单并渲染（不崩）。

    ★ 这是 ISSUE #39 那条"当前不可达、但一旦触达就 AttributeError"的路径：
      有达线岗位、没有简历 → 走 `ensure_item` 的 `candidate is None` 分支 →
      渲染时读 `item.job.city`。列表非空才是真正的触达条件
      （空清单会走 `_print_empty`，压根碰不到渲染层）。
    """
    insert_job(session, match_score=88, status="scored")
    session.commit()  # ★ CLI 自己开 session：必须先 commit 才看得见（只 flush 不够）
    result = runner.invoke(app, ["--no-remind", "today", "--limit", "1"])
    assert result.exit_code == 0, f"没有简历的库把 `jh today` 弄崩了：\n{result.output}"
    assert "深圳" in result.output, (
        f"清单没有渲染出岗位（城市列来自 item.job.city）：\n{result.output}"
    )
    assert "没有简历" in result.output, "没有简历这件事应当明说，而不是静默出个空开场白"


def test_print_items_renders_item_with_a_job(session, settings) -> None:
    """★ 渲染层**真的**读 `item.job.city`（ISSUE #39 的爆炸点）。

    这条用例的价值在于：它把"漏传 job"从"将来某条路径触达才炸"变成
    **现在就会红**。用 `--check` 拿不到清单，所以直接喂一个手工构造的
    `TodayItem` 给渲染函数 —— 形状与"库里没有简历"那条分支产出的条目一致。
    """
    from jobhunter.cli.commands import today_cmd

    job = insert_job(session)
    session.flush()
    item = today_svc.TodayItem(
        index=1,
        job=job,
        score=job.match_score or 88,
        application_id=1,
        greeting_issues=["库里没有简历，无法生成开场白（先 jh resume add）"],
    )
    result = today_svc.TodayList(items=[item], candidates=1, daily_limit=20)

    today_cmd._print_items(result, settings=settings)  # 不抛 AttributeError 即通过
    assert item.job.city == "深圳"


# ===========================================================================
# #37 的 CLI 入口：`--sent` / `--edited` 只声明事实，不替用户说话
# ===========================================================================
def test_copy_option_marks_greeting_sent_only_with_the_flag(
    session,
    settings,
    monkeypatch,
) -> None:
    """`jh today --copy 1` 本身**不**置位；`--copy 1 --sent` 才置位。"""
    from jobhunter.cli.commands import today_cmd

    monkeypatch.setattr(today_cmd, "copy_to_clipboard", lambda _text: True)
    _seed(session, settings, count=1)
    session.commit()
    session.execute(text("DELETE FROM applications"))
    session.commit()

    plain = runner.invoke(app, ["--no-remind", "today", "--copy", "1"])
    assert plain.exit_code == 0, plain.output
    session.expire_all()
    application = session.scalars(select(Application)).one()
    assert application.greeting_text, "测试前提：--copy 应当生成了开场白"
    assert application.greeting_sent is False, "只是复制了一下就被当成'已发送'"

    declared = runner.invoke(app, ["--no-remind", "today", "--copy", "1", "--sent"])
    assert declared.exit_code == 0, declared.output
    session.expire_all()
    application = session.scalars(select(Application)).one()
    assert application.greeting_sent is True


def test_sent_flag_without_copy_is_rejected(session, settings) -> None:
    """`--sent` 不指明对象时必须报错，而不是静默什么都不做。"""
    _seed(session, settings, count=1)
    session.commit()
    result = runner.invoke(app, ["--no-remind", "today", "--sent"])
    assert result.exit_code == 2
    assert "--copy" in result.output


# ---------------------------------------------------------------------------
# 顺带守一条本轮改动引入的隐式契约：`_seed` 造的数据必须真的走了打分路径
# ---------------------------------------------------------------------------
def test_seed_helper_still_scores_jobs(session, settings) -> None:
    jobs = _seed(session, settings, count=2)
    session.flush()
    assert all(job.match_score is not None for job in jobs)
    assert all(job.jd_keywords_json for job in jobs)


def test_cli_applied_says_it_inferred_greeting_sent(session, settings) -> None:
    """★ 推定必须**在终端里说出来**（ISSUES #37 的收尾，主会话补）。

    服务层在"有开场白 + 标 applied"时会推定"话术已发出"。那是个**推定**，不是事实 ——
    不说出来的话，用户会在统计里看到一个自己没做过的动作（本项目最反对的静默失真）。
    这条守住三件事：① 推定确实发生；② 终端打出了提示；③ 提示里给出纠正命令。
    """
    job, application = _pending_with_greeting(session, settings)
    session.commit()

    result = runner.invoke(
        app, ["--no-remind", "apply", "--job", str(job.id), "--status", "applied"]
    )
    assert result.exit_code == 0, result.output
    assert "已推定" in result.output, f"推定没有告诉用户：\n{result.output}"
    assert "--no-sent" in result.output, "提示里要给纠正命令，否则用户没法改回来"

    session.expire_all()
    assert application.greeting_sent is True


def test_cli_no_sent_corrects_the_inference(session, settings) -> None:
    """`--no-sent` 要能把推定改回 False，**并且说出来**（否则用户不知道生效没有）。"""
    job, application = _pending_with_greeting(session, settings)
    session.commit()

    first = runner.invoke(
        app, ["--no-remind", "apply", "--job", str(job.id), "--status", "applied"]
    )
    assert first.exit_code == 0, first.output
    session.expire_all()
    assert application.greeting_sent is True

    # 状态已经是 applied（changed=False），但闭环字段要能被纠正
    second = runner.invoke(
        app, ["--no-remind", "apply", "--job", str(job.id), "--status", "applied", "--no-sent"]
    )
    assert second.exit_code == 0, second.output
    assert "改回 False" in second.output, f"纠正没有告诉用户：\n{second.output}"
    session.expire_all()
    assert application.greeting_sent is False

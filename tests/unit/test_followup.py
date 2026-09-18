"""P4.5：零手打跟进的单测（顶部提醒 / 批量打标 / 模糊匹配 / 待处理清单）。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from jobhunter.cli.app import app
from jobhunter.config.settings import get_settings
from jobhunter.models import (
    Application,
    ApplicationEvent,
    Assessment,
    Interview,
    Job,
    Offer,
    Resume,
)
from jobhunter.services import apply_svc, reminder_svc
from jobhunter.services.resume_svc import ResumeContent
from jobhunter.utils.time import now_utc

runner = CliRunner()


def make_resume() -> Resume:
    return Resume(
        name="主简历",
        direction="AI Agent",
        content_json=ResumeContent(skills=["Python", "RAG"]).model_dump_json(),
        content_hash="a" * 64,
        is_default=True,
    )


def make_job(**overrides: Any) -> Job:
    base: dict[str, Any] = {
        "platform": "manual",
        "company": "某某科技",
        "title": "AI Agent 开发",
        "city": "深圳",
        "jd_text": "要求 Python、RAG。",
        "status": "scored",
        "match_score": 80,
    }
    base.update(overrides)
    return Job(**base)


@pytest.fixture
def world(session, settings):
    """简历 + 三个岗位，其中两个已投（天数可调）。"""
    session.add(make_resume())
    jobs = [
        make_job(company="甲公司", title="AI Agent 开发", match_score=90),
        make_job(company="乙公司", title="后端开发", match_score=80),
        make_job(company="丙公司", title="前端开发", match_score=70),
    ]
    session.add_all(jobs)
    session.flush()
    return session, jobs


def apply_with_age(session, job: Job, days: int, status: str = "applied") -> Application:
    """造一条"投了 N 天"的投递记录。"""
    application, _ = apply_svc.ensure_pending_application(session, job)
    application.status = status
    application.applied_at = now_utc() - timedelta(days=days)
    session.flush()
    return application


# ===========================================================================
# ① 顶部提醒：3/7/14 天三个桶
# ===========================================================================
def test_stale_buckets_are_cumulative(world, settings):
    """4 / 9 / 20 天前的三条 → 3 天桶 3 条、7 天桶 2 条、14 天桶 1 条。

    ★ 三个桶是**累计**的（第 20 天那条同时属于三个桶）：
      用户要的是"有几条到了该处理的时间点"，而不是"恰好第 3 天的那几条"
      —— 后者要求他每天都准点打开工具，不现实。
    """
    session, jobs = world
    for job, days in zip(jobs, (4, 9, 20), strict=True):
        apply_with_age(session, job, days)

    summary = reminder_svc.collect_reminders(session, settings=settings)
    assert summary.stale_followup == [3, 2, 1]
    assert summary.has_stale and not summary.is_empty


def test_fresh_applications_do_not_remind(world, settings):
    session, jobs = world
    apply_with_age(session, jobs[0], 0)
    apply_with_age(session, jobs[1], 2)  # 还差一天
    summary = reminder_svc.collect_reminders(session, settings=settings)
    assert summary.stale_followup == [0, 0, 0]
    assert summary.is_empty, "刚投的岗位不该被提醒"


def test_terminal_statuses_are_not_reminded(world, settings):
    """进了面试/Offer/被拒的，不该再催我"跟进"。"""
    session, jobs = world
    apply_with_age(session, jobs[0], 30, status="interview")
    apply_with_age(session, jobs[1], 30, status="rejected")
    apply_with_age(session, jobs[2], 30, status="offer")
    summary = reminder_svc.collect_reminders(session, settings=settings)
    assert summary.stale_followup == [0, 0, 0]


def test_pending_applications_are_not_counted_as_stale(world, settings):
    """`pending` 表示"还没投"，它不是"投了没动静"。"""
    session, jobs = world
    apply_svc.ensure_pending_application(session, jobs[0])
    summary = reminder_svc.collect_reminders(session, settings=settings)
    assert summary.stale_followup == [0, 0, 0]


def test_interview_assessment_offer_windows(world, settings):
    session, jobs = world
    app0 = apply_with_age(session, jobs[0], 1)
    app1 = apply_with_age(session, jobs[1], 1)
    app2 = apply_with_age(session, jobs[2], 1)
    moment = now_utc()
    session.add_all(
        [
            Interview(
                application_id=app0.id, scheduled_at=moment + timedelta(days=2), status="待面"
            ),
            Interview(
                application_id=app1.id, scheduled_at=moment + timedelta(days=30), status="待面"
            ),
            Interview(
                application_id=app2.id, scheduled_at=moment + timedelta(days=1), status="已面"
            ),
            Assessment(
                application_id=app0.id,
                kind="笔试",
                deadline_at=moment + timedelta(days=1),
                status="待做",
            ),
            Assessment(
                application_id=app1.id,
                kind="测评",
                deadline_at=moment + timedelta(days=9),
                status="待做",
            ),
            Offer(application_id=app2.id, company="丙公司", deadline_at=moment + timedelta(days=1)),
        ]
    )
    session.flush()

    summary = reminder_svc.collect_reminders(session, settings=settings)
    assert len(summary.upcoming_interviews) == 1  # 30 天后 / 已面 的都不算
    assert "甲公司" in summary.upcoming_interviews[0]
    assert len(summary.assessment_deadlines) == 1  # 9 天后的不算
    assert len(summary.offer_deadlines) == 1
    assert summary.actionable_count == 3


def test_format_reminders_is_silent_when_empty():
    assert reminder_svc.format_reminders(reminder_svc.ReminderSummary()) == []


def test_format_reminders_omits_zero_buckets():
    summary = reminder_svc.ReminderSummary(stale_followup=[0, 2, 0])
    lines = reminder_svc.format_reminders(summary)
    assert "投后 7 天无动静 2 条" in lines[0]
    assert "3 天未跟进" not in lines[0], "0 条的类别不该出现（每次都读一遍确认没事 = 纯噪音）"
    assert "14 天" not in lines[0]
    assert any("jh apply --pending" in line for line in lines), "要告诉用户下一步敲什么"


def test_get_reminders_without_database_returns_empty():
    """★ 纪律 ②：库还没建（首次运行）也不能让任何命令崩。

    这条守的是"提醒没有资格阻断主命令" —— 它在每条 `jh` 命令前都会跑。
    """
    summary = reminder_svc.get_reminders()  # 不传 session，且此刻没有库
    assert summary.is_empty


# ===========================================================================
# ② 待处理清单
# ===========================================================================
def test_list_actionable_orders_by_urgency(world, settings):
    session, jobs = world
    apply_with_age(session, jobs[0], 20)
    apply_with_age(session, jobs[1], 4)
    apply_svc.ensure_pending_application(session, jobs[2])  # 还没投

    rows = apply_svc.list_actionable(session)
    assert [row.job_id for row in rows] == [jobs[0].id, jobs[1].id, jobs[2].id]
    assert "建议放弃" in rows[0].reason
    assert "跟进" in rows[1].reason
    assert "还没投" in rows[2].reason
    assert rows[2].days is None


def test_list_actionable_excludes_closed(world, settings):
    session, jobs = world
    apply_with_age(session, jobs[0], 30, status="offer")
    apply_with_age(session, jobs[1], 30, status="rejected")
    assert apply_svc.list_actionable(session) == []


# ===========================================================================
# ③ CLI：批量打标
# ===========================================================================
@pytest.fixture
def cli_world(isolated_env: Path):
    assert runner.invoke(app, ["--no-remind", "init"]).exit_code == 0
    settings = get_settings()
    from jobhunter.storage.db import get_session_factory

    engine, factory = get_session_factory(settings.database_url)
    with factory() as live:
        live.add(make_resume())
        for index in range(1, 4):
            live.add(
                make_job(
                    company=f"公司{index}",
                    title=f"岗位{index}",
                    match_score=90 - index,
                    platform_job_id=f"p{index}",
                )
            )
        live.commit()
    engine.dispose()
    return isolated_env


def _statuses() -> dict[int, str]:
    settings = get_settings()
    from jobhunter.storage.db import get_session_factory

    engine, factory = get_session_factory(settings.database_url)
    with factory() as live:
        rows = {app.job_id: app.status for app in live.scalars(select(Application))}
    engine.dispose()
    return rows


def test_cli_batch_by_job_id(cli_world, monkeypatch):
    monkeypatch.setenv("JH_LLM_PROVIDER", "mock")
    from jobhunter.config.settings import reset_settings

    reset_settings()
    runner.invoke(app, ["--no-remind", "today", "--limit", "3"])

    result = runner.invoke(app, ["--no-remind", "apply", "#1=applied", "#2=viewed"])
    assert result.exit_code == 0, result.output
    assert "批量打标完成：2 条" in result.output
    assert _statuses() == {1: "applied", 2: "viewed", 3: "pending"}


def test_cli_batch_invalid_status_changes_nothing(cli_world, monkeypatch):
    """★ 有错就整批不动 —— 一半成功一半失败会让人不知道该不该重跑。"""
    monkeypatch.setenv("JH_LLM_PROVIDER", "mock")
    from jobhunter.config.settings import reset_settings

    reset_settings()
    runner.invoke(app, ["--no-remind", "today", "--limit", "3"])
    before = _statuses()

    result = runner.invoke(app, ["--no-remind", "apply", "#1=applied", "#2=已投"])
    assert result.exit_code == 2
    assert "整批不动" in result.output
    assert _statuses() == before, "有错的那一批竟然改了一部分"


def test_cli_batch_unknown_target_changes_nothing(cli_world, monkeypatch):
    monkeypatch.setenv("JH_LLM_PROVIDER", "mock")
    from jobhunter.config.settings import reset_settings

    reset_settings()
    runner.invoke(app, ["--no-remind", "today", "--limit", "3"])
    before = _statuses()

    result = runner.invoke(app, ["--no-remind", "apply", "#999=applied", "#1=applied"])
    assert result.exit_code == 2
    assert "找不到" in result.output
    assert _statuses() == before


def test_cli_batch_by_today_index(cli_world, monkeypatch):
    """`序号=状态` 走 `jh today` 的序号（与快照一致）。"""
    monkeypatch.setenv("JH_LLM_PROVIDER", "mock")
    from jobhunter.config.settings import reset_settings

    reset_settings()
    runner.invoke(app, ["--no-remind", "today", "--limit", "3"])

    result = runner.invoke(app, ["--no-remind", "apply", "2=applied"])
    assert result.exit_code == 0, result.output
    assert _statuses()[2] == "applied"


def test_cli_batch_writes_events(cli_world, monkeypatch):
    monkeypatch.setenv("JH_LLM_PROVIDER", "mock")
    from jobhunter.config.settings import reset_settings

    reset_settings()
    runner.invoke(app, ["--no-remind", "today", "--limit", "3"])
    runner.invoke(app, ["--no-remind", "apply", "#1=applied", "#2=applied"])

    settings = get_settings()
    from jobhunter.storage.db import get_session_factory

    engine, factory = get_session_factory(settings.database_url)
    with factory() as live:
        events = list(live.scalars(select(ApplicationEvent)))
    engine.dispose()
    assert len([e for e in events if e.event_type == "status_change"]) == 2


# ===========================================================================
# ④ CLI：模糊匹配（命中多条绝不猜）
# ===========================================================================
def test_cli_keyword_multi_match_does_not_guess(cli_world, monkeypatch):
    monkeypatch.setenv("JH_LLM_PROVIDER", "mock")
    from jobhunter.config.settings import reset_settings

    reset_settings()
    runner.invoke(app, ["--no-remind", "today", "--limit", "3"])
    before = _statuses()

    result = runner.invoke(app, ["--no-remind", "apply", "公司", "--status", "applied"])
    assert result.exit_code == 1
    assert "没有替你猜" in result.output
    assert _statuses() == before, "多命中时动了数据"


def test_cli_keyword_single_match_applies(cli_world, monkeypatch):
    monkeypatch.setenv("JH_LLM_PROVIDER", "mock")
    from jobhunter.config.settings import reset_settings

    reset_settings()
    runner.invoke(app, ["--no-remind", "today", "--limit", "3"])

    result = runner.invoke(app, ["--no-remind", "apply", "公司1", "--status", "applied"])
    assert result.exit_code == 0, result.output
    assert _statuses()[1] == "applied"


def test_cli_keyword_no_match_is_actionable(cli_world):
    result = runner.invoke(app, ["--no-remind", "apply", "不存在的公司", "--status", "applied"])
    assert result.exit_code == 1
    assert "没有匹配" in result.output
    assert "jh apply --pending" in result.output


def test_cli_keyword_with_yes_applies_all(cli_world, monkeypatch):
    """`--yes` 写在位置参数之后也要生效（Click 会把选项吞进 tokens）。"""
    monkeypatch.setenv("JH_LLM_PROVIDER", "mock")
    from jobhunter.config.settings import reset_settings

    reset_settings()
    runner.invoke(app, ["--no-remind", "today", "--limit", "3"])

    result = runner.invoke(app, ["--no-remind", "apply", "公司", "--status", "viewed", "--yes"])
    assert result.exit_code == 0, result.output
    assert set(_statuses().values()) == {"viewed"}


def test_cli_keyword_invalid_status_is_actionable(cli_world):
    """非法状态要在**碰数据库之前**就报出来（拼错状态是最常见的手误）。"""
    result = runner.invoke(app, ["--no-remind", "apply", "公司", "--status", "skipped"])
    assert result.exit_code == 2
    assert "不认识" in result.output
    assert "可用" in result.output


# ===========================================================================
# ⑤ CLI：--pending
# ===========================================================================
def test_cli_pending_lists_actionable(cli_world):
    result = runner.invoke(app, ["--no-remind", "apply", "--pending"])
    assert result.exit_code == 0, result.output
    assert "待处理" in result.output


def test_cli_pending_empty_is_friendly(isolated_env: Path):
    runner.invoke(app, ["--no-remind", "init"])
    result = runner.invoke(app, ["--no-remind", "apply", "--pending"])
    assert result.exit_code == 0, result.output
    assert "没有待处理的投递" in result.output


def test_cli_timeline_still_placeholder(cli_world):
    """时间轴是 P6 的活 —— 现在必须诚实地说没做。"""
    result = runner.invoke(app, ["--no-remind", "apply", "--timeline", "1"])
    assert result.exit_code == 2
    assert "P6" in result.output

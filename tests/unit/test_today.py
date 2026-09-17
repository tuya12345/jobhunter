"""待投清单 / 投递打标 / 导出的单测（P4）。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from jobhunter.cli.app import app
from jobhunter.config.settings import get_settings
from jobhunter.models import Application, ApplicationEvent, Job, Resume
from jobhunter.services import apply_svc, export_svc, today_svc
from jobhunter.services.resume_svc import EducationItem, ProjectItem, ResumeContent
from jobhunter.utils.time import now_utc

runner = CliRunner()


def make_resume(**overrides: Any) -> Resume:
    content = ResumeContent(
        basic={"job_intention": "AI Agent"},
        education=[
            EducationItem(
                school="某某大学", major="软件工程", degree="本科", period="2023.09 - 2027.06"
            )
        ],
        skills=["Python", "LangChain", "RAG"],
        projects=[ProjectItem(name="Agent 平台", skills=["LangChain", "Python"])],
    )
    base: dict[str, Any] = {
        "name": "主简历",
        "direction": "AI Agent",
        "content_json": content.model_dump_json(),
        "content_hash": "a" * 64,
        "is_default": True,
    }
    base.update(overrides)
    return Resume(**base)


def make_job(**overrides: Any) -> Job:
    base: dict[str, Any] = {
        "platform": "manual",
        "company": "某某科技",
        "title": "AI Agent 开发",
        "city": "深圳",
        "jd_text": "要求熟悉 Python、LangChain、RAG。",
        "jd_hash": "j" * 64,
        "status": "scored",
        "match_score": 80,
        "jd_keywords_json": '["Python", "LangChain", "RAG"]',
    }
    base.update(overrides)
    return Job(**base)


@pytest.fixture
def ready(session, settings) -> Session:  # noqa: F821 - 类型只是可读性
    """一份简历 + 三个够分岗位（其中两条同公司）。"""
    session.add(make_resume())
    session.add(make_job(company="甲公司", title="AI Agent 开发", match_score=90))
    session.add(make_job(company="甲公司", title="大模型应用", match_score=85))  # 同公司
    session.add(make_job(company="乙公司", title="Python 后端", match_score=70))
    session.add(make_job(company="丙公司", title="低分岗", match_score=10))  # 不够分
    session.flush()
    return session


# ===========================================================================
# 选取
# ===========================================================================
def test_select_excludes_low_score_and_dedupes_company(ready, settings):
    jobs, total, dropped = today_svc.select_candidates(ready, settings=settings)
    assert [job.title for job in jobs] == ["AI Agent 开发", "Python 后端"]
    assert total == 3  # 够分 3 条（含被去重的那条）
    assert dropped == 1  # 甲公司只留分数最高的
    assert jobs[0].match_score >= jobs[1].match_score


def test_select_respects_limit_after_dedupe(ready, settings):
    jobs, _total, _dropped = today_svc.select_candidates(ready, settings=settings, limit=1)
    assert len(jobs) == 1
    assert jobs[0].match_score == 90


def test_select_excludes_applied_and_skipped(ready, settings):
    applied = ready.scalars(
        __import__("sqlalchemy").select(Job).where(Job.company == "乙公司")
    ).first()
    applied.status = "skipped"
    ready.flush()
    jobs, _total, _dropped = today_svc.select_candidates(ready, settings=settings)
    assert "Python 后端" not in [job.title for job in jobs]


def test_select_excludes_jobs_with_non_pending_application(ready, settings):
    job = ready.scalars(__import__("sqlalchemy").select(Job).where(Job.company == "乙公司")).first()
    ready.add(Application(job_id=job.id, status="replied"))
    ready.flush()
    jobs, _total, _dropped = today_svc.select_candidates(ready, settings=settings)
    assert "Python 后端" not in [job.title for job in jobs]


def test_min_score_overrides_threshold(ready, settings):
    jobs, _total, _dropped = today_svc.select_candidates(ready, settings=settings, min_score=86)
    assert [job.title for job in jobs] == ["AI Agent 开发"]


# ===========================================================================
# 额度
# ===========================================================================
def test_count_applied_today(ready):
    jobs = list(ready.scalars(__import__("sqlalchemy").select(Job)))
    ready.add(Application(job_id=jobs[0].id, status="applied", applied_at=now_utc()))
    ready.add(
        Application(
            job_id=jobs[1].id,
            status="applied",
            applied_at=now_utc() - timedelta(days=2),  # 前天的，不该算进今天
        )
    )
    ready.add(Application(job_id=jobs[2].id, status="pending"))  # 还没投，不算
    ready.flush()
    assert apply_svc.count_applied_today(ready) == 1


# ===========================================================================
# 投递记录与状态
# ===========================================================================
def test_ensure_pending_is_idempotent(ready):
    job = ready.scalars(__import__("sqlalchemy").select(Job)).first()
    first, created_first = apply_svc.ensure_pending_application(ready, job, greeting="你好")
    second, created_second = apply_svc.ensure_pending_application(ready, job, greeting="覆盖试试")
    assert created_first is True and created_second is False
    assert first.id == second.id
    assert second.greeting_text == "你好"  # 已有内容不被覆盖（用户可能手改过）


def test_set_status_writes_event_and_syncs_job(ready):
    job = ready.scalars(__import__("sqlalchemy").select(Job)).first()
    apply_svc.ensure_pending_application(ready, job, greeting="你好")
    result = apply_svc.set_status(ready, job.id, "applied")
    assert result.changed and result.from_status == "pending"
    assert result.to_status == "applied"
    assert job.status == "applied"
    events = list(ready.scalars(__import__("sqlalchemy").select(ApplicationEvent)))
    status_events = [event for event in events if event.event_type == "status_change"]
    assert [(event.from_status, event.to_status) for event in status_events] == [
        ("pending", "applied")
    ]
    # ★ 闭环字段：有开场白时标"已投"要顺带记一条 greeting_sent
    #   （以后统计"生成的话术到底有没有被用上"全靠它）
    assert [event.event_type for event in events if event.event_type == "greeting_sent"]
    assert ready.get(Application, result.application_id).applied_at is not None


def test_set_status_twice_does_not_duplicate_event(ready):
    """★ 重复敲同一条命令不能重复写事件（会污染时间轴，几周后分不清哪条是真的）。"""
    job = ready.scalars(__import__("sqlalchemy").select(Job)).first()
    apply_svc.ensure_pending_application(ready, job, greeting="你好")
    apply_svc.set_status(ready, job.id, "applied")
    before = len(list(ready.scalars(__import__("sqlalchemy").select(ApplicationEvent))))
    again = apply_svc.set_status(ready, job.id, "applied")
    assert again.changed is False
    events = list(ready.scalars(__import__("sqlalchemy").select(ApplicationEvent)))
    assert len(events) == before, "重复打标写进了多余的事件"
    assert len([event for event in events if event.event_type == "status_change"]) == 1
    assert len([event for event in events if event.event_type == "greeting_sent"]) == 1


def test_set_status_rejects_unknown_status(ready):
    job = ready.scalars(__import__("sqlalchemy").select(Job)).first()
    with pytest.raises(apply_svc.ApplicationError):
        apply_svc.set_status(ready, job.id, "已投")  # 中文/拼错的状态必须报错，不能静默建野状态


# ===========================================================================
# 序号快照（防"投完第 1 条之后第 3 条变了"）
# ===========================================================================
def test_snapshot_keeps_index_stable(ready, settings):
    """★ 这条守的是"最致命的误操作"：序号漂移导致标错岗位。"""
    result = today_svc.build_today_list(ready, settings=settings, provider=None)
    assert [item.job.title for item in result.items] == ["AI Agent 开发", "Python 后端"]

    # 投掉第 1 条 → 实时筛选下"第 2 条"会变成低分岗之前的另一条
    apply_svc.set_status(ready, result.items[0].job.id, "applied")
    ready.flush()

    job, from_snapshot = today_svc.resolve_index(ready, 2, settings=settings)
    assert from_snapshot is True
    assert job is not None and job.title == "Python 后端"  # 仍指向原来那一条


def test_snapshot_ignores_other_days(ready, settings):
    today_svc.save_snapshot([1, 2, 3], settings)
    assert today_svc.load_snapshot(settings) == [1, 2, 3]
    # 换一天再看：昨天的序号不能拿来用
    tomorrow = now_utc() + timedelta(days=1)
    assert today_svc.load_snapshot(settings, now=tomorrow) is None


def test_resolve_index_without_snapshot_uses_live_selection(ready, settings):
    job, from_snapshot = today_svc.resolve_index(ready, 1, settings=settings)
    assert from_snapshot is False
    assert job is not None and job.match_score == 90


# ===========================================================================
# 导出
# ===========================================================================
def test_export_creates_new_files_only(ready, settings, isolated_env):
    result = today_svc.build_today_list(ready, settings=settings, provider=None)
    output_dir = settings.paths.output_dir
    first = export_svc.export_markdown(result.items, output_dir=output_dir)
    second = export_svc.export_markdown(result.items, output_dir=output_dir)
    assert first != second, "第二次导出必须写新文件，不能覆盖"
    assert first.exists() and second.exists()
    assert "今日待投清单" in first.read_text(encoding="utf-8")


def test_export_csv_has_bom_and_header(ready, settings):
    result = today_svc.build_today_list(ready, settings=settings, provider=None)
    path = export_svc.export_csv(result.items, output_dir=settings.paths.output_dir)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "CSV 必须带 BOM，否则 Excel 打开是乱码"
    body = raw.decode("utf-8-sig")
    assert "公司" in body.splitlines()[0]
    assert "甲公司" in body


# ===========================================================================
# CLI
# ===========================================================================
@pytest.fixture
def cli_ready(isolated_env: Path):
    """建库 + 简历 + 岗位（直接写库，不走抓取）。"""
    assert runner.invoke(app, ["--no-remind", "init"]).exit_code == 0
    settings = get_settings()
    from jobhunter.storage.db import get_session_factory

    engine, factory = get_session_factory(settings.database_url)
    with factory() as live:
        live.add(make_resume())
        for index in range(1, 7):
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


def test_cli_today_limit_caps_rows(cli_ready, monkeypatch):
    monkeypatch.setenv("JH_LLM_PROVIDER", "mock")
    from jobhunter.config.settings import reset_settings

    reset_settings()
    result = runner.invoke(app, ["--no-remind", "today", "--limit", "2"])
    assert result.exit_code == 0, result.output
    assert "今日待投（2 条）" in result.output
    assert "岗位3" not in result.output


def test_cli_today_check_reports_quota(cli_ready):
    result = runner.invoke(app, ["--no-remind", "today", "--check"])
    assert result.exit_code == 0, result.output
    assert "今天已投" in result.output
    assert "0 / 上限" in result.output


def test_cli_today_empty_explains_why(isolated_env: Path):
    runner.invoke(app, ["--no-remind", "init"])
    result = runner.invoke(app, ["--no-remind", "today"])
    assert result.exit_code == 0, result.output
    assert "还没有岗位" in result.output


def test_cli_today_no_candidates_explains_why(cli_ready, settings):
    """有岗位但都没到分数线 → 提示去跑 match / 降分数线，而不是空表。"""
    from jobhunter.storage.db import get_session_factory

    engine, factory = get_session_factory(settings.database_url)
    with factory() as live:
        live.execute(text("UPDATE jobs SET match_score = 10"))
        live.commit()
    engine.dispose()
    result = runner.invoke(app, ["--no-remind", "today"])
    assert result.exit_code == 0, result.output
    assert "清单是空的" in result.output
    assert "jh match run" in result.output


def test_cli_today_export_writes_file(cli_ready, monkeypatch):
    monkeypatch.setenv("JH_LLM_PROVIDER", "mock")
    from jobhunter.config.settings import reset_settings

    reset_settings()
    result = runner.invoke(app, ["--no-remind", "today", "--limit", "2", "--export", "md"])
    assert result.exit_code == 0, result.output
    assert "已导出" in result.output
    assert list(get_settings().paths.output_dir.glob("today-*.md"))


def test_cli_today_rejects_bad_export_format(cli_ready):
    result = runner.invoke(app, ["--no-remind", "today", "--export", "pdf"])
    assert result.exit_code == 2
    assert "md 或 csv" in result.output


def test_cli_apply_by_index(cli_ready, monkeypatch):
    monkeypatch.setenv("JH_LLM_PROVIDER", "mock")
    from jobhunter.config.settings import reset_settings

    reset_settings()
    runner.invoke(app, ["--no-remind", "today", "--limit", "3"])
    result = runner.invoke(app, ["--no-remind", "apply", "2", "--status", "applied"])
    assert result.exit_code == 0, result.output
    assert "→" in result.output and "applied" in result.output

    engine, factory = __import__("jobhunter.storage.db", fromlist=["x"]).get_session_factory(
        get_settings().database_url
    )
    with factory() as live:
        rows = list(live.scalars(__import__("sqlalchemy").select(Application)))
    engine.dispose()
    # `jh today` 会为清单里每一条建 pending；打标只把第 2 条改成 applied
    statuses = [row.status for row in rows]
    assert statuses.count("applied") == 1
    assert "pending" in statuses


def test_cli_apply_by_job_id(cli_ready):
    result = runner.invoke(app, ["--no-remind", "apply", "--job", "1", "--status", "applied"])
    assert result.exit_code == 0, result.output
    assert "岗位 #1" in result.output


def test_cli_apply_rejects_unknown_status(cli_ready):
    result = runner.invoke(app, ["--no-remind", "apply", "1", "--status", "已投"])
    assert result.exit_code == 1
    assert "不认识" in result.output


def test_cli_apply_needs_status(cli_ready):
    result = runner.invoke(app, ["--no-remind", "apply", "1"])
    assert result.exit_code == 2
    assert "--status" in result.output


def test_cli_apply_batch_syntax_is_deferred(cli_ready):
    """`3=applied` 是 P4.5 的语法 —— 现在必须**诚实地说没做**，而不是假装不认识。"""
    result = runner.invoke(app, ["--no-remind", "apply", "3=applied"])
    assert result.exit_code == 2
    assert "P4.5" in result.output


def test_cli_apply_out_of_range_index(cli_ready):
    result = runner.invoke(app, ["--no-remind", "apply", "999", "--status", "applied"])
    assert result.exit_code == 1
    assert "第 999 条" in result.output


def test_cli_apply_when_no_candidates_created(ready):
    """库里一个够分岗位都没有时打标 → 直接说清没有第 N 条，而不是崩。"""
    result = runner.invoke(app, ["--no-remind", "apply", "1", "--status", "applied"])
    assert result.exit_code in (1, 2)

"""三个**用户已裁决**缺陷的定点用例：ISSUE #27 / #40 / #41。

这三条的裁决都是 A 案，各自的要点：

    #27  `jh match run` 默认跳过已投 / 已跳过（`--all` 显式覆盖），
         并把"算了多少条 / 跳过多少条"打在屏幕上（不静默少算）；
    #40  导出物加 `job_id`（序号隔天就对不上），简历名收窄成「简历 #id」
         （导出物会被丢进聊天窗口，文件名里常带真名）—— **只收窄导出物**；
    #41  `jh today --copy N` 重新生成的文本要**回写库内**（库内始终最新），
         但回写**不能**顺手把 `greeting_sent` 置位（#37：生成 ≠ 已发出）。

★ 这些用例是按"**修复前应当是红的**"来写的：
  #27 改前会把 applied/skipped 的岗位也重算（rule_score 被写上新值）；
  #40 改前导出物里既没有 job_id、又原样写着「张三-简历.pdf」；
  #41 改前 `--copy` 生成的新文本不会进库（库里留着上次的旧话术）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from jobhunter.cli.app import app
from jobhunter.cli.commands import today_cmd
from jobhunter.config.settings import get_settings, reset_settings
from jobhunter.models import Application, Job, Resume
from jobhunter.services import apply_svc, export_svc, match_svc, report_svc, today_svc
from jobhunter.services.resume_svc import EducationItem, ProjectItem, ResumeContent
from jobhunter.storage.db import get_session_factory

runner = CliRunner()

#: 故意写成"带真名"的简历文件名 —— #40 的用例要证明它**不会**出现在导出物里。
PII_RESUME_NAME = "张三-简历.pdf"

JD = "要求熟悉 Python、LangChain、RAG，做过真实项目。"


# ===========================================================================
# 造数据
# ===========================================================================
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
        "name": PII_RESUME_NAME,
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
        "jd_text": JD,
        "jd_hash": "j" * 64,
        "status": "scored",
        "match_score": 80,
        "jd_keywords_json": '["Python", "LangChain", "RAG"]',
    }
    base.update(overrides)
    return Job(**base)


def _session_factory():
    return get_session_factory(get_settings().database_url)


def _app_snapshot(job_id: int) -> dict[str, Any]:
    """把某个岗位的投递记录读成普通字典（避免 ORM 对象脱离 session 后炸掉）。"""
    engine, factory = _session_factory()
    try:
        with factory() as live:
            row = apply_svc.get_latest_application(live, job_id)
            assert row is not None, f"岗位 {job_id} 没有投递记录"
            return {
                "greeting_text": row.greeting_text,
                "greeting_variant": row.greeting_variant,
                "greeting_sent": bool(row.greeting_sent),
                "greeting_edited": bool(row.greeting_edited),
            }
    finally:
        engine.dispose()


def _job_snapshot(title: str) -> dict[str, Any]:
    engine, factory = _session_factory()
    try:
        with factory() as live:
            row = live.scalars(select(Job).where(Job.title == title)).one()
            return {
                "id": row.id,
                "rule_score": row.rule_score,
                "match_score": row.match_score,
                "status": row.status,
            }
    finally:
        engine.dispose()


@pytest.fixture
def cli_seeded(isolated_env: Path) -> Path:
    """建库 + 一份简历 + 三个岗位（待处理 / 已投 / 已跳过）。"""
    assert runner.invoke(app, ["--no-remind", "init"]).exit_code == 0
    engine, factory = _session_factory()
    with factory() as live:
        live.add(make_resume())
        live.add(make_job(title="待处理岗", status="scored", match_score=80, platform_job_id="p1"))
        live.add(make_job(title="已投岗", status="applied", match_score=90, platform_job_id="p2"))
        live.add(make_job(title="跳过岗", status="skipped", match_score=85, platform_job_id="p3"))
        live.commit()
    engine.dispose()
    return isolated_env


# ===========================================================================
# ISSUE #27 —— 默认跳过 applied / skipped，`--all` 覆盖，且必须报出跳过条数
# ===========================================================================
def test_match_run_skips_processed_jobs_by_default(cli_seeded: Path) -> None:
    """默认只算待处理的岗位，并把"算了 / 跳过"的条数**打在屏幕上**。

    ★ 修复前的症状：三个岗位全被重算（`applied` / `skipped` 的 `rule_score`
      被覆盖成新值）—— 用户为不会再投的岗位白付一次钱，而且屏幕上什么都不说。
    """
    result = runner.invoke(app, ["--no-remind", "match", "run", "--no-llm"])
    assert result.exit_code == 0, result.output

    # ① 不静默少算：算了多少、跳过多少、为什么跳过，都要能看见
    assert "本次打分" in result.output, result.output
    assert "跳过" in result.output, result.output
    assert "已投 1 条" in result.output and "已跳过 1 条" in result.output, result.output

    # ② 只有待处理的那个岗位被算了
    assert _job_snapshot("待处理岗")["rule_score"] is not None
    assert _job_snapshot("已投岗")["rule_score"] is None, "已投的岗位被重算了（ISSUE #27）"
    assert _job_snapshot("跳过岗")["rule_score"] is None, "已跳过的岗位被重算了（ISSUE #27）"


def test_match_run_all_rescores_everything_without_touching_status(cli_seeded: Path) -> None:
    """`--all`：用户说"我要重算全部"时必须真的全算，且**不改**人工裁决过的状态。

    ★ 为什么状态也要断言：重算会走 `apply_result_to_job`，它的纪律是
      "状态只前进不后退" —— 重算不能把 `skipped`（我不投）改成 `scored`。
    """
    result = runner.invoke(app, ["--no-remind", "match", "run", "--no-llm", "--all"])
    assert result.exit_code == 0, result.output
    assert "--all" in result.output

    for title in ("待处理岗", "已投岗", "跳过岗"):
        assert _job_snapshot(title)["rule_score"] is not None, f"{title} 没被 --all 算到"
    assert _job_snapshot("已投岗")["status"] == "applied"
    assert _job_snapshot("跳过岗")["status"] == "skipped"


def test_match_run_with_only_processed_jobs_says_why(cli_seeded: Path) -> None:
    """库里全是已处理的岗位时：说清"都处理过了 + 怎么重算"，而不是一张空表。"""
    runner.invoke(app, ["--no-remind", "match", "run", "--no-llm"])  # 先让待处理岗也变成有分的
    engine, factory = _session_factory()
    with factory() as live:
        live.scalars(select(Job).where(Job.title == "待处理岗")).one().status = "skipped"
        live.commit()
    engine.dispose()

    result = runner.invoke(app, ["--no-remind", "match", "run", "--no-llm"])
    assert result.exit_code == 1
    assert "都已经处理过了" in result.output, result.output
    assert "--all" in result.output, "没说怎么重算全部 —— 用户会被卡在这里"
    assert "库里还没有岗位" not in result.output, "把'都处理过了'说成了'库里没岗位'"


def test_scoring_scope_also_looks_at_application_records(session) -> None:
    """岗位状态没同步、但有**非 pending 投递记录**时，默认同样不重算。

    ★ 两道判据都要有（与 `today_svc.select_candidates` 同源）：
      `jobs.status` 与投递记录可能只写了一边（`jh apply` 会同步，但历史数据不会）。
      这时以"投递记录"这个更硬的事实为准，否则会出现
      "库里有投递记录、工具还在给它重算推荐理由"的矛盾。

    ★ 同时钉住反向语义：`queued`（我决定要投）**必须**照算 ——
      "已处理过"指的是投过/跳过，不是"我动过它"。
    """
    session.add(make_resume())
    unsynced = make_job(title="记录已投但状态没同步", status="scored", platform_job_id="u1")
    queued = make_job(title="我决定要投", status="queued", platform_job_id="q1")
    fresh = make_job(title="还没处理", status="new", platform_job_id="n1")
    session.add_all([unsynced, queued, fresh])
    session.flush()
    session.add(Application(job_id=unsynced.id, status="rejected"))
    session.flush()

    scope = match_svc.select_scoring_jobs(session)
    kept = {job.id for job in scope.jobs}
    assert unsynced.id not in kept, "有投递记录的岗位仍被重算"
    assert scope.reasons.get("applied-unsynced") == 1, "跳过原因没有区分'状态没同步'这一类"
    assert {queued.id, fresh.id} <= kept, "queued / new 的岗位必须照算"
    assert scope.skipped_count == 1 and scope.total == 3

    everything = match_svc.select_scoring_jobs(session, include_all=True)
    assert len(everything.jobs) == 3 and everything.skipped == [], "--all 没有真的覆盖全部"


# ===========================================================================
# ISSUE #40 —— 导出物要有 job_id，且不许带简历文件名
# ===========================================================================
def _fill_greetings(session) -> list[today_svc.TodayItem]:
    """出清单（provider=None → 模板兜底，不花钱），返回清单条目。"""
    result = today_svc.build_today_list(session, settings=get_settings(), provider=None)
    assert result.items, "清单是空的，后面的导出断言就没有意义了"
    return result.items


def test_export_markdown_carries_job_id_and_hides_resume_name(cli_seeded: Path) -> None:
    """Markdown 导出：每条都有岗位 id，且**只有**「简历 #id」、不出现简历文件名。

    ★ 修复前的症状：导出物里只有"当天清单的序号"（隔天就对不上），
      简历那一行原样写着 `张三-简历.pdf`（导出物常被丢进聊天窗口/云笔记）。
    """
    output_dir = get_settings().paths.output_dir
    engine, factory = _session_factory()
    try:
        with factory() as live:
            items = _fill_greetings(live)
            live.commit()
            path = export_svc.export_markdown(items, output_dir=output_dir)
            expected = [(item.job.id, item.resume_id) for item in items]
    finally:
        engine.dispose()

    text = path.read_text(encoding="utf-8")
    for job_id, resume_id in expected:
        assert f"岗位 id：**{job_id}**" in text, f"导出物里没有岗位 id={job_id}"
        assert f"简历 #{resume_id}" in text, "简历没有收窄成「简历 #id」"
    assert PII_RESUME_NAME not in text, "导出物里出现了简历文件名（含真名）"
    assert "张三" not in text, "导出物里泄露了真名"
    # ★ 序号与岗位 id 是两件事，导出物里要说清楚（否则下一个人还是会拿序号跨天用）
    assert "--job" in text, "没有告诉用户跨天该怎么用岗位 id 打标"


def test_export_csv_carries_job_id_and_hides_resume_name(cli_seeded: Path) -> None:
    """CSV 导出：表头有 `岗位id` 列、行里有真值，简历列同样只有编号。"""
    output_dir = get_settings().paths.output_dir
    engine, factory = _session_factory()
    try:
        with factory() as live:
            items = _fill_greetings(live)
            live.commit()
            path = export_svc.export_csv(items, output_dir=output_dir)
            first = items[0]
            expected_id = first.job.id
            expected_resume = first.resume_id
    finally:
        engine.dispose()

    body = path.read_bytes().decode("utf-8-sig")
    header = body.splitlines()[0]
    assert "岗位id" in header, f"CSV 表头没有岗位 id 列：{header}"
    assert f"简历 #{expected_resume}" in body
    # QUOTE_ALL：每个字段都被引号包着，所以 `"3"` 这种写法能精确命中"id 这一列"
    assert f'"{expected_id}"' in body, "CSV 行里没有岗位 id 的值"
    assert PII_RESUME_NAME not in body, "CSV 里出现了简历文件名（含真名）"


def test_report_timeline_names_the_job_id(session) -> None:
    """#40 的同类修正：周报是**导出物**，它的时间轴原来只有"公司 · 职位"。

    同一家公司投了两个岗位时，那种写法根本分不清说的是哪个 —— 与 `jh today`
    导出漏 job_id 是同一个问题（复盘时无法精确定位）。
    """
    session.add(make_resume())
    job = make_job(title="周报里的岗位", platform_job_id="r1")
    session.add(job)
    session.flush()
    apply_svc.set_status(session, job.id, "applied")
    session.flush()

    text = report_svc.render_markdown(report_svc.build_report(session))
    assert f"（job_id={job.id}）" in text, "周报时间轴没有岗位 id，复盘时对不上岗位"


# ===========================================================================
# ISSUE #41 —— `--copy N` 重新生成后必须回写库（但不置 greeting_sent）
# ===========================================================================
#: 模拟"上一次生成留下的旧话术"，用来证明 --copy 之后它会被换掉。
OLD_GREETING = "很久以前生成、库里一直留着的旧话术"


def test_replace_greeting_overwrites_but_never_touches_closure_flags(session) -> None:
    """服务层：显式覆盖文本 + 同步变体；**不碰** greeting_sent / greeting_edited。"""
    session.add(make_resume())
    job = make_job(title="回写岗", platform_job_id="w1")
    session.add(job)
    session.flush()
    application, _ = apply_svc.ensure_pending_application(
        session, job, greeting=OLD_GREETING, greeting_variant="with_project"
    )
    apply_svc.mark_greeting(session, job.id, sent=True, edited=True)
    session.flush()

    result = apply_svc.replace_greeting(
        session, job.id, greeting="这次新生成的话术", variant="without_project"
    )
    assert result is not None
    assert result.changed is True and result.previous == OLD_GREETING
    assert result.was_sent is True  # CLI 要靠它提醒"库里文本换了、但确实发过"

    row = session.get(Application, application.id)
    assert row.greeting_text == "这次新生成的话术"
    assert row.greeting_variant == "without_project", "变体没跟着同步（A/B 统计会算错组）"
    assert row.greeting_sent is True, "回写把闭环字段改了 —— 生成 ≠ 已发出（ISSUE #37）"
    assert row.greeting_edited is True

    # 文本没变时不该声称"变更了"（CLI 据此决定要不要打印那一行）
    again = apply_svc.replace_greeting(session, job.id, greeting="这次新生成的话术")
    assert again is not None and again.changed is False


def test_copy_regenerates_and_writes_back_to_db(cli_seeded: Path, monkeypatch) -> None:
    """★ #41 的核心用例：`--copy N` 生成的新文本必须落进 `applications.greeting_text`。

    ★ 修复前的症状（验证会话实测）：缓存失效时会重新调模型 → 屏幕上是一段新文本，
      而库里还是旧值 → 用户下次看到旧话术，以为"这工具记错了"。
      这条用例把"屏幕上的文本"（复制走的那一份）与"库里的文本"直接对上。
    """
    monkeypatch.setenv("JH_LLM_PROVIDER", "mock")
    reset_settings()

    engine, factory = _session_factory()
    try:
        with factory() as live:
            job = live.scalars(select(Job).where(Job.title == "待处理岗")).one()
            apply_svc.ensure_pending_application(
                live, job, greeting=OLD_GREETING, greeting_variant="with_project"
            )
            live.commit()
            job_id = job.id
    finally:
        engine.dispose()

    copied: list[str] = []

    def _fake_clipboard(text: str) -> bool:
        """替掉真实剪贴板：既不依赖系统上有没有 clip.exe，又能拿到"屏幕上的那份文本"。"""
        copied.append(text)
        return True

    monkeypatch.setattr(today_cmd, "copy_to_clipboard", _fake_clipboard)

    result = runner.invoke(app, ["--no-remind", "today", "--copy", "1"])
    assert result.exit_code == 0, result.output
    assert copied, "没走到复制那一步，用例没有测到东西"

    stored = _app_snapshot(job_id)
    assert stored["greeting_text"] != OLD_GREETING, (
        "重新生成之后库里还是旧话术 —— 屏幕（新文本）与库内（旧值）不一致（ISSUE #41）"
    )
    assert stored["greeting_text"] == copied[0], "库内文本与这次复制走的文本不是同一份"
    assert stored["greeting_variant"], "变体没写进库（A/B 统计会漏掉这一条）"
    # ★ #37 的红线：回写**不等于**"已发出"
    assert stored["greeting_sent"] is False, "回写把 greeting_sent 置位了（生成 ≠ 已发出）"

    # ★ 缓存命中（文本没变）时不该再声称"库内已更新" —— 不说不存在的事
    again = runner.invoke(app, ["--no-remind", "today", "--copy", "1"])
    assert again.exit_code == 0, again.output
    assert "库内话术已更新" not in again.output, "文本没变却报告了一次'已更新'"
    assert _app_snapshot(job_id)["greeting_text"] == copied[0]

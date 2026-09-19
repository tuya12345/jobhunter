"""笔试/测评登记 + Offer 比较的单测（P6）。

★ 这一批用例守的是两类"沉默的失败"：
    · 笔试错过截止 —— 工具没提醒，用户**根本不会发现**是自己漏了；
    · Offer 比不出来 —— 只写 "25K" 看不出 15 薪和 13 薪差 5 万。
  所以断言的重点不是"字段存进去了"，而是**提醒窗口与比较结论对不对**。

★ 数据构造口径照抄 `tests/unit/test_today.py`（make_job），
  CLI 用例照抄它的模式：`jh init` 建库 → 直接写库造数据 → `runner.invoke`。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from jobhunter.cli.app import app
from jobhunter.config.settings import get_settings
from jobhunter.models import Application, ApplicationEvent, Assessment, Job, Offer
from jobhunter.services import apply_svc, assessment_svc, offer_svc
from jobhunter.services.reminder_svc import ASSESSMENT_LOOKAHEAD_DAYS
from jobhunter.storage.db import get_session_factory
from jobhunter.utils.time import now_utc, to_local

runner = CliRunner()


# ===========================================================================
# 造数据
# ===========================================================================
def make_job(**overrides: Any) -> Job:
    """造一个岗位（字段与 test_today.make_job 一致，便于对照）。"""
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


def add_job(session, **overrides: Any) -> Job:
    """加一个岗位并 flush（拿到 id）。"""
    job = make_job(**overrides)
    session.add(job)
    session.flush()
    return job


def count_events(session, event_type: str) -> int:
    """数某一类事件有几条（"没写重复事件"这类断言全靠它）。"""
    rows = session.scalars(
        select(ApplicationEvent).where(ApplicationEvent.event_type == event_type)
    ).all()
    return len(rows)


def load_all(model: type[Any]) -> list[Any]:
    """从当前（tmp_path 里的）库读某张表的全部记录 —— CLI 用例断言用。"""
    engine, factory = get_session_factory(get_settings().database_url)
    try:
        with factory() as live:
            return list(live.scalars(select(model)))
    finally:
        engine.dispose()


def count_events_in_db(event_type: str) -> int:
    """从当前库里数某一类事件（CLI 用例用）。"""
    return sum(1 for event in load_all(ApplicationEvent) if event.event_type == event_type)


@pytest.fixture
def cli_db(isolated_env: Path) -> Path:
    """建好库 + 一个岗位（#1 = 甲公司 · AI Agent 开发）供 CLI 端到端用例使用。"""
    assert runner.invoke(app, ["--no-remind", "init"]).exit_code == 0
    engine, factory = get_session_factory(get_settings().database_url)
    with factory() as live:
        live.add(make_job(company="甲公司", title="AI Agent 开发"))
        live.commit()
    engine.dispose()
    return isolated_env


# ===========================================================================
# 服务层：校验（非法值必须在写库之前被拦住）
# ===========================================================================
def test_add_assessment_rejects_unknown_kind(session):
    """依据：契约要求非法 kind 抛 AssessmentError，且提示里列出可用值。

    ★ 为什么必须列可用值：用户打错一个字，报错却不说"能写什么"，
      他就只能去翻文档 —— 这是自用工具里最容易让人放弃的一类摩擦。
    """
    job = add_job(session)
    with pytest.raises(assessment_svc.AssessmentError) as excinfo:
        assessment_svc.add_assessment(session, job.id, kind="电话面试")
    message = str(excinfo.value)
    assert "笔试" in message
    assert "测评" in message
    assert "AI面" in message


def test_add_assessment_rejects_unknown_status(session):
    """依据：非法状态要被拒绝，而不是静默建一条看板永远看不见的野记录。"""
    job = add_job(session)
    with pytest.raises(assessment_svc.AssessmentError) as excinfo:
        assessment_svc.add_assessment(session, job.id, kind="笔试", status="做完了")
    assert "待做" in str(excinfo.value)


def test_add_assessment_unknown_job_raises(session):
    """依据：找不到岗位属于**业务失败**（多半是 id 敲错），必须报错而不是建孤儿记录。"""
    with pytest.raises(assessment_svc.AssessmentError) as excinfo:
        assessment_svc.add_assessment(session, 999, kind="笔试")
    assert "999" in str(excinfo.value)
    assert session.scalars(select(Assessment)).all() == []


def test_add_offer_rejects_non_positive_salary(session):
    """依据：年包 = 月薪 × 月数，0/负数会让比较表把明显的错误答案算成"最优"。"""
    job = add_job(session)
    with pytest.raises(offer_svc.OfferError):
        offer_svc.add_offer(session, job.id, salary_month=-1, months=12)
    with pytest.raises(offer_svc.OfferError):
        offer_svc.add_offer(session, job.id, salary_month=20000, months=0)


# ===========================================================================
# 服务层：登记（没有投递记录也要能记；只前进不回退）
# ===========================================================================
def test_add_assessment_creates_pending_application(session):
    """★ 依据：用户往往是**先收到笔试**才回头登记，不能逼他先打标。

    所以没有投递记录时自动补一条 pending，并把状态推进到 assessment；
    记录只允许有一条（幂等口径来自 apply_svc）。
    """
    job = add_job(session)
    assert session.scalars(select(Application)).all() == []

    assessment = assessment_svc.add_assessment(
        session, job.id, kind="笔试", deadline_at=now_utc() + timedelta(days=2)
    )

    applications = session.scalars(select(Application)).all()
    assert len(applications) == 1
    assert applications[0].status == "assessment"
    assert assessment.application_id == applications[0].id
    assert assessment.status == "待做"


def test_add_offer_creates_pending_application_and_copies_job(session):
    """★ 依据：offer 登记同样不该要求先打标；company/title 冗余抄一份，
    这样 `jh offer compare` 不依赖 join 的完整性（模型注释里的取舍）。"""
    job = add_job(session, company="丙公司", title="大模型应用")
    offer = offer_svc.add_offer(
        session, job.id, salary_text="20-25K", salary_month=22000, months=15, location="深圳"
    )

    assert offer.company == "丙公司"
    assert offer.title == "大模型应用"
    assert offer.status == "待定"
    applications = session.scalars(select(Application)).all()
    assert len(applications) == 1
    assert applications[0].status == "offer"


def test_add_assessment_does_not_regress_interview_status(session):
    """★ 依据：只前进不回退 —— 补记一条历史信息不该改变当前进度认知。

    投递已经是 interview 时再补登记笔试：状态必须仍是 interview，
    且**不写**新的 status_change 事件（否则时间轴上会出现一条假的"退回笔试"）。
    """
    job = add_job(session)
    apply_svc.set_status(session, job.id, "applied")
    apply_svc.set_status(session, job.id, "interview")
    session.flush()
    before = count_events(session, "status_change")
    assert before == 2

    assessment_svc.add_assessment(session, job.id, kind="测评")
    session.flush()

    application = apply_svc.get_latest_application(session, job.id)
    assert application.status == "interview"
    assert count_events(session, "status_change") == before


def test_add_offer_does_not_revive_rejected_application(session):
    """★ 依据：同一条"只前进不回退"规则 —— 被拒的投递不该因为补记 offer 而复活。"""
    job = add_job(session)
    apply_svc.set_status(session, job.id, "rejected")
    session.flush()

    offer_svc.add_offer(session, job.id, salary_month=20000, months=13)
    session.flush()

    application = apply_svc.get_latest_application(session, job.id)
    assert application.status == "rejected"
    assert offer_svc.list_offers(session)[0].offer.salary_month == 20000


def test_add_assessment_writes_invite_event(session):
    """★ 依据：事件流是"邀请信号"的落地处 —— 笔试邀请必须留下 assessment_invite，
    否则时间轴上看不出"这家公司什么时候给过我笔试"。"""
    job = add_job(session)
    assessment_svc.add_assessment(session, job.id, kind="AI面", platform="牛客")
    session.flush()

    assert count_events(session, "assessment_invite") == 1
    events = session.scalars(
        select(ApplicationEvent).where(ApplicationEvent.event_type == "assessment_invite")
    ).all()
    assert "牛客" in (events[0].payload_json or "")


def test_add_offer_writes_received_event(session):
    """★ 依据：拿到 offer 是终局信号，必须写 offer_received（时间轴 + 后续统计要用）。"""
    job = add_job(session)
    offer_svc.add_offer(session, job.id, salary_month=25000, months=15)
    session.flush()

    assert count_events(session, "offer_received") == 1


# ===========================================================================
# 服务层：列表 / 提醒窗口
# ===========================================================================
def test_upcoming_window_reuses_reminder_lookahead():
    """★ 依据：窗口天数只能有一处定义（reminder_svc.ASSESSMENT_LOOKAHEAD_DAYS）。

    提醒说 3 天、列表说 2 天，用户不会去比对两处代码，只会觉得"这工具的数字不可信"。
    """
    assert assessment_svc.UPCOMING_DAYS == ASSESSMENT_LOOKAHEAD_DAYS


def test_list_assessments_orders_deadline_none_last(session):
    """依据：列表是行动清单 —— 截止最近的排最前；没有截止时间的排最后。"""
    job = add_job(session)
    moment = now_utc()
    assessment_svc.add_assessment(
        session, job.id, kind="笔试", deadline_at=moment + timedelta(days=5)
    )
    assessment_svc.add_assessment(session, job.id, kind="测评", deadline_at=None)
    assessment_svc.add_assessment(
        session, job.id, kind="AI面", deadline_at=moment + timedelta(days=1)
    )
    session.flush()

    rows = assessment_svc.list_assessments(session, now=moment)

    assert [row.assessment.kind for row in rows] == ["AI面", "笔试", "测评"]
    assert rows[0].days_left == 1
    assert rows[-1].days_left is None
    assert rows[0].company == "某某科技"
    assert rows[0].job_id == job.id


def test_list_assessments_upcoming_window(session):
    """★ 依据（--upcoming 的口径）：只看"待做"且在 UPCOMING_DAYS 天内截止的。

    进：3 天内截止的待做、没填截止时间的待做（否则它永远不会被看见）。
    不进：5 天后截止的、已做、已过期。
    """
    job = add_job(session)
    moment = now_utc()
    soon = assessment_svc.add_assessment(
        session, job.id, kind="笔试", deadline_at=moment + timedelta(days=3)
    )
    no_deadline = assessment_svc.add_assessment(session, job.id, kind="测评")
    far = assessment_svc.add_assessment(
        session, job.id, kind="笔试", deadline_at=moment + timedelta(days=5)
    )
    done = assessment_svc.add_assessment(
        session, job.id, kind="笔试", deadline_at=moment + timedelta(days=1), status="已做"
    )
    expired = assessment_svc.add_assessment(
        session, job.id, kind="笔试", deadline_at=moment + timedelta(days=1), status="已过期"
    )
    session.flush()

    ids = {
        row.assessment.id
        for row in assessment_svc.list_assessments(session, upcoming=True, now=moment)
    }

    assert ids == {soon.id, no_deadline.id}
    assert far.id not in ids
    assert done.id not in ids
    assert expired.id not in ids


# ===========================================================================
# 服务层：年包与比较
# ===========================================================================
def test_offer_annual_package_and_is_best(session):
    """★ 依据：compare_rows 必须标出年包最大的那一个 —— 这是"多个 offer 怎么选"的核心。"""
    first = add_job(session, company="甲公司", title="Python 后端")
    second = add_job(session, company="乙公司", title="AI Agent 开发")
    small = offer_svc.add_offer(session, first.id, salary_text="18K", salary_month=18000, months=13)
    big = offer_svc.add_offer(
        session, second.id, salary_text="20-25K", salary_month=22000, months=15
    )
    session.flush()

    assert big.annual_package == 330000
    assert small.annual_package == 234000

    rows = offer_svc.compare_rows(offer_svc.list_offers(session))
    by_company = {row["offer"].company: row for row in rows}
    assert by_company["乙公司"]["is_best"] is True
    assert by_company["甲公司"]["is_best"] is False
    assert by_company["乙公司"]["annual"] == 330000
    assert by_company["乙公司"]["annual_wan"] == pytest.approx(33.0)


def test_offer_missing_months_has_no_annual(session):
    """★ 依据：缺月数（或月薪）时年包必须是 None —— 不许拿 12 个月瞎猜。

    也绝不能因此被当成"年包最低"而排除：宁可说"信息不足，比不了"。
    """
    job = add_job(session)
    offer = offer_svc.add_offer(session, job.id, salary_month=22000)
    session.flush()

    assert offer.annual_package is None
    row = offer_svc.compare_rows(offer_svc.list_offers(session))[0]
    assert row["annual"] is None
    assert row["annual_wan"] is None
    assert row["is_best"] is False


def test_compare_rows_tie_marks_none(session):
    """★ 依据（口径，写在 compare_rows 的 docstring 里）：并列最大时**一个都不标**。

    高亮的意思是"有一个严格更好的选择"；两个都标等于说"一样好"，
    而那件事用户自己看得出来。
    """
    first = add_job(session, company="甲公司")
    second = add_job(session, company="乙公司")
    offer_svc.add_offer(session, first.id, salary_month=20000, months=15)  # 30 万
    offer_svc.add_offer(session, second.id, salary_month=25000, months=12)  # 30 万
    session.flush()

    rows = offer_svc.compare_rows(offer_svc.list_offers(session))

    assert [row["annual"] for row in rows] == [300000, 300000]
    assert not any(row["is_best"] for row in rows)


def test_list_offers_orders_deadline_none_last(session):
    """依据：比较表按**签约截止**排序 —— "哪个快要签了"比"哪个钱多"更紧急。"""
    job = add_job(session)
    moment = now_utc()
    offer_svc.add_offer(session, job.id, salary_month=10000, months=12, deadline_at=None)
    offer_svc.add_offer(
        session, job.id, salary_month=20000, months=12, deadline_at=moment + timedelta(days=2)
    )
    offer_svc.add_offer(
        session, job.id, salary_month=30000, months=12, deadline_at=moment + timedelta(days=5)
    )
    session.flush()

    rows = offer_svc.list_offers(session, now=moment)

    assert [row.offer.salary_month for row in rows] == [20000, 30000, 10000]
    assert rows[0].days_left == 2
    assert rows[-1].days_left is None


# ===========================================================================
# CLI：参数错 → 退出码 2
# ===========================================================================
def test_cli_assessment_add_rejects_unknown_kind(cli_db):
    """依据：用法/参数错 = 退出码 2，且提示里必须列出可用值。"""
    result = runner.invoke(
        app, ["--no-remind", "assessment", "add", "--job", "1", "--kind", "电话面试"]
    )
    assert result.exit_code == 2, result.output
    assert "笔试" in result.output
    assert "测评" in result.output
    assert "AI面" in result.output
    assert load_all(Assessment) == []


def test_cli_assessment_add_rejects_unknown_status(cli_db):
    """依据：同上（状态维度）—— 退出码 2 且列出 待做/已做/已过期。"""
    result = runner.invoke(
        app, ["--no-remind", "assessment", "add", "--job", "1", "--status", "做完了"]
    )
    assert result.exit_code == 2, result.output
    assert "待做" in result.output
    assert "已做" in result.output
    assert "已过期" in result.output
    assert load_all(Assessment) == []


def test_cli_assessment_add_rejects_unparsable_deadline(cli_db):
    """★ 依据：猜错时间的后果是错过笔试，所以看不懂就报错（退出码 2），不猜。"""
    result = runner.invoke(
        app, ["--no-remind", "assessment", "add", "--job", "1", "--deadline", "明天下午"]
    )
    assert result.exit_code == 2, result.output
    assert "deadline" in result.output
    assert load_all(Assessment) == []


def test_cli_offer_add_rejects_unparsable_deadline(cli_db):
    """依据：offer 的签约截止同样是"沉默失败"高发处，格式错必须当场拦住。"""
    result = runner.invoke(app, ["--no-remind", "offer", "add", "--job", "1", "--deadline", "下周"])
    assert result.exit_code == 2, result.output
    assert load_all(Offer) == []


def test_cli_offer_add_rejects_unknown_job(cli_db):
    """依据：业务失败（岗位不存在）= 退出码 1，与参数错（2）区分开。"""
    result = runner.invoke(
        app,
        [
            "--no-remind",
            "offer",
            "add",
            "--job",
            "999",
            "--salary-month",
            "22000",
            "--months",
            "15",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "999" in result.output
    assert load_all(Offer) == []


# ===========================================================================
# CLI：端到端（登记 → 列表/比较）
# ===========================================================================
def test_cli_assessment_add_then_list(cli_db):
    """★ 依据（端到端）：登记后必须能在列表里看到公司名与剩余天数 ——
    "还剩几天"是唯一能让人决定"现在做还是晚上做"的信息。"""
    deadline_text = f"{to_local(now_utc() + timedelta(days=4)):%Y-%m-%d %H:%M}"
    added = runner.invoke(
        app,
        [
            "--no-remind",
            "assessment",
            "add",
            "--job",
            "1",
            "--kind",
            "笔试",
            "--deadline",
            deadline_text,
            "--platform",
            "牛客",
        ],
    )
    assert added.exit_code == 0, added.output
    assert "甲公司" in added.output

    listed = runner.invoke(app, ["--no-remind", "assessment", "list"])
    assert listed.exit_code == 0, listed.output
    assert "甲公司" in listed.output
    assert "AI Agent 开发" in listed.output
    assert "笔试" in listed.output
    assert "牛客" in listed.output
    assert "4 天" in listed.output


def test_cli_assessment_add_without_application_advances_status(cli_db):
    """★ 依据：只收到笔试、还没打标也要能登记（自动补 pending 并推进状态），
    事件流里同时留下 status_change 与 assessment_invite。"""
    result = runner.invoke(
        app, ["--no-remind", "assessment", "add", "--job", "1", "--kind", "测评"]
    )
    assert result.exit_code == 0, result.output

    applications = load_all(Application)
    assert len(applications) == 1
    assert applications[0].status == "assessment"
    event_types = {event.event_type for event in load_all(ApplicationEvent)}
    assert event_types == {"status_change", "assessment_invite"}


def test_cli_assessment_list_empty_hints_next_step(isolated_env):
    """依据：空清单必须给出"下一步敲什么"，否则用户不知道这张表怎么填。"""
    runner.invoke(app, ["--no-remind", "init"])
    result = runner.invoke(app, ["--no-remind", "assessment", "list"])
    assert result.exit_code == 0, result.output
    assert "jh assessment add --job" in result.output


def test_cli_offer_add_then_compare(cli_db):
    """★ 依据（端到端）：年包 = 月薪 × 月数 必须算出来并打印（1 位小数），
    只写 "20-25K" 是比不出 15 薪和 13 薪差别的。"""
    deadline_text = f"{to_local(now_utc() + timedelta(days=3)):%Y-%m-%d}"
    added = runner.invoke(
        app,
        [
            "--no-remind",
            "offer",
            "add",
            "--job",
            "1",
            "--salary",
            "20-25K",
            "--salary-month",
            "22000",
            "--months",
            "15",
            "--location",
            "深圳",
            "--deadline",
            deadline_text,
        ],
    )
    assert added.exit_code == 0, added.output
    assert "年包" in added.output
    assert "33.0" in added.output

    compared = runner.invoke(app, ["--no-remind", "offer", "compare"])
    assert compared.exit_code == 0, compared.output
    assert "年包（万元）" in compared.output
    assert "33.0" in compared.output
    assert "22000" in compared.output
    assert "15" in compared.output
    assert "深圳" in compared.output
    assert "还剩 3 天" in compared.output
    # 只有一个 offer 时照常打印，但要说明"现在比不出什么"
    assert "只有一个 offer，等有第二个再来比较" in compared.output

    offers = load_all(Offer)
    assert len(offers) == 1
    assert offers[0].company == "甲公司"
    assert offers[0].title == "AI Agent 开发"
    assert offers[0].annual_package == 330000
    assert load_all(Application)[0].status == "offer"
    assert count_events_in_db("offer_received") == 1


def test_cli_offer_compare_two_offers_marks_best(cli_db):
    """★ 依据：多个 offer 时必须能一眼看出年包最高的那个（整行高亮 = is_best）。"""
    engine, factory = get_session_factory(get_settings().database_url)
    with factory() as live:
        live.add(make_job(company="乙公司", title="大模型应用", platform_job_id="p2"))
        live.commit()
    engine.dispose()

    runner.invoke(
        app,
        [
            "--no-remind",
            "offer",
            "add",
            "--job",
            "1",
            "--salary-month",
            "18000",
            "--months",
            "13",
        ],
    )
    runner.invoke(
        app,
        [
            "--no-remind",
            "offer",
            "add",
            "--job",
            "2",
            "--salary-month",
            "22000",
            "--months",
            "15",
        ],
    )
    result = runner.invoke(app, ["--no-remind", "offer", "compare"])

    assert result.exit_code == 0, result.output
    assert "23.4" in result.output  # 18000 × 13
    assert "33.0" in result.output  # 22000 × 15
    assert "年包最高" in result.output
    assert "乙公司" in result.output


def test_cli_offer_compare_hints_missing_salary_month(cli_db):
    """★ 依据：算不出年包的行显示 — 并明确告诉用户补哪两个参数 ——
    否则他会以为工具算错了，而不是自己少填了。"""
    runner.invoke(app, ["--no-remind", "offer", "add", "--job", "1", "--salary", "20-25K"])
    result = runner.invoke(app, ["--no-remind", "offer", "compare"])
    assert result.exit_code == 0, result.output
    assert "—" in result.output
    assert "--salary-month" in result.output
    assert "--months" in result.output
    assert "补 --salary-month 与 --months 才能算年包" in result.output


def test_cli_offer_compare_empty_hints_add(isolated_env):
    """依据：空表要给出可直接照抄的下一条命令。"""
    runner.invoke(app, ["--no-remind", "init"])
    result = runner.invoke(app, ["--no-remind", "offer", "compare"])
    assert result.exit_code == 0, result.output
    assert "jh offer add" in result.output

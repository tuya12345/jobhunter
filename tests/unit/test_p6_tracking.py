"""P6：跟进体系的核心测试（状态机 / 事件流 / 统计口径 / 时间轴 / 薄弱点 / 周报 / 追问）。

★ 这个文件守的是 P6 里**最容易写错、而且错了没人发现**的四件事：

    ① **状态机语义**：rejected 可以从任意状态来、offer 可以从 interview 直接跳
       （小公司一面就发 offer 很常见）。写成"只能逐级前进"会静默拒绝真实场景。
    ② **事件流是 append-only**：时间轴、统计口径的可复现性全建立在这上面。
       重复敲同一条命令**不该**再写一条事件（P4 就是数事件数数出来的 bug）。
    ③ **统计口径**：按"至少走到过这一步"算，而不是"当前停在哪一步"。
       后者会系统性低估（现在被拒的投递，曾经可能走到过二面）。
    ④ **两类问题必须分开展示**：recorded（能力缺口）vs predicted（准备不足），
       混在一起 `jh weak` 的结论就是错的。

★ 真实 LLM 的部分（`question predict` / `followup`）用**假 Provider** 做确定性断言；
  真模型的抽样在 `@pytest.mark.live`（不进 CI）。

★ 「依据」= 计划书【提示词 8】与 `tests/verify/test_v_p6*.py` 会用到的功能契约。
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from jobhunter.cli.app import app
from jobhunter.llm.base import LLMProvider, LLMResponse, PromptPayload
from jobhunter.models import (
    Application,
    ApplicationEvent,
    Interview,
    InterviewQuestion,
    Job,
    Resume,
)
from jobhunter.services import (
    apply_svc,
    followup_svc,
    interview_svc,
    question_svc,
    report_svc,
    resume_svc,
    stats_svc,
    track_svc,
    weak_svc,
)
from jobhunter.utils.time import now_utc

from .test_tailor_svc import fake_content

runner = CliRunner()


# ===========================================================================
# 夹具
# ===========================================================================
def make_resume() -> Resume:
    content = fake_content()
    return Resume(
        name="主简历",
        direction="Python 后端开发",
        content_json=content.model_dump_json(),
        content_hash=resume_svc.compute_content_hash(content),
        is_default=True,
    )


def make_job(**overrides: Any) -> Job:
    base: dict[str, Any] = {
        "platform": "BOSS",
        "company": "某某科技",
        "title": "Python 后端开发实习生",
        "city": "深圳",
        "jd_text": "要求熟悉 Python、FastAPI、MySQL，了解 Redis；有 Kubernetes 经验优先。",
        "jd_hash": "p" * 64,
        "status": "scored",
        "match_score": 72,
        "jd_keywords_json": '["Python", "FastAPI", "MySQL", "Redis", "Kubernetes"]',
        # 方向回音统计读的就是它（打分时算出来的结论，不现算）
        "rule_detail_json": json.dumps({"jd_direction": "Python 开发"}, ensure_ascii=False),
    }
    base.update(overrides)
    return Job(**base)


@pytest.fixture
def ready(session, settings):
    """一份简历 + 一个岗位（都已 flush）。"""
    session.add(make_resume())
    session.add(make_job())
    session.commit()
    return session


class StubProvider(LLMProvider):
    """假 Provider：按顺序吐出预设 JSON（不联网、不花钱、完全确定）。"""

    model = "stub-model"

    def __init__(self, *payloads: str) -> None:
        self.payloads = list(payloads)
        self.calls: list[PromptPayload] = []

    def complete(self, payload: PromptPayload) -> LLMResponse:
        self.calls.append(payload)
        content = self.payloads.pop(0) if self.payloads else ""
        return LLMResponse(content=content, model=self.model)


def _apply(session, job: Job, status: str, *, days_ago: int = 0) -> Application:
    """打标（可指定"几天前投的"）。"""
    when = now_utc() - timedelta(days=days_ago)
    apply_svc.set_status(session, job.id, status, occurred_at=when)
    session.commit()
    return apply_svc.get_latest_application(session, job.id)


# ===========================================================================
# ① 状态机与事件流
# ===========================================================================
def test_rejected_can_come_from_any_status(ready) -> None:
    """依据：`models/constants.py` 状态机规则 1「rejected 可以从任意状态发生」。"""
    for index, status in enumerate(("pending", "applied", "viewed", "replied", "interview")):
        job = make_job(company=f"公司{index}", title=f"岗{index}", jd_hash=f"{index}" * 64)
        ready.add(job)
        ready.commit()
        _apply(ready, job, status)
        result = apply_svc.set_status(ready, job.id, "rejected")
        ready.commit()
        assert result.to_status == "rejected", status


def test_offer_can_jump_straight_from_interview(ready) -> None:
    """依据：状态机规则 2「offer 可以从 interview 直接跳」（小公司一面就发 offer）。"""
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied")
    apply_svc.set_status(ready, job.id, "interview")
    result = apply_svc.set_status(ready, job.id, "offer")
    ready.commit()
    assert result.from_status == "interview"
    assert result.to_status == "offer"


def test_event_stream_is_append_only_and_skips_duplicates(ready) -> None:
    """重复敲同一条命令**不该**再写事件（P4 数事件数发现的 bug）。

    依据：`apply_svc.set_status` 的 docstring「重复敲同一条命令是常见的手误，
    写一堆重复事件会污染时间轴」。
    """
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied")
    before = ready.scalars(select(ApplicationEvent)).all()
    apply_svc.set_status(ready, job.id, "applied")  # 再敲一次
    apply_svc.set_status(ready, job.id, "applied")
    ready.commit()
    after = ready.scalars(select(ApplicationEvent)).all()
    assert len(after) == len(before), "同状态重复打标不该写新事件"


def test_timeline_reads_the_event_stream_in_real_order(ready) -> None:
    """时间轴必须按 `occurred_at`（真实发生时间）排序，而不是录入顺序。

    依据：`models/application.py`「occurred_at 与 created_at 必须分开：
    补记的面试会污染时间轴」。
    """
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied", days_ago=23)
    # 补记：昨天已读、前天对方回复（顺序与录入顺序相反）
    apply_svc.set_status(ready, job.id, "viewed", occurred_at=now_utc() - timedelta(days=1))
    apply_svc.set_status(ready, job.id, "replied", occurred_at=now_utc() - timedelta(days=2))
    ready.commit()

    timeline = track_svc.build_timeline(ready, job.id)
    labels = [entry.label for entry in timeline.entries]
    assert timeline.days == 23
    assert "已投递" in labels[0]
    # 时间轴顺序 = **发生时间**顺序：replied(2 天前) 在 viewed(1 天前) 之前，
    # 尽管我是按相反顺序录入的。
    # （状态变更的标签形如"对方回复（对方已读 → 对方回复）"，所以按"箭头右边"判断）
    assert "→ 对方回复）" in labels[1]
    assert "→ 对方已读）" in labels[2]
    assert [entry.occurred_at for entry in timeline.entries] == sorted(
        entry.occurred_at for entry in timeline.entries
    )
    assert timeline.status_label == "对方回复"


def test_timeline_without_application_explains_why(ready) -> None:
    """没有投递记录时要说清"还没投"，而不是画一条空时间轴。"""
    job = ready.scalars(select(Job)).one()
    timeline = track_svc.build_timeline(ready, job.id)
    assert timeline.empty
    assert timeline.notes and "还没有投递记录" in timeline.notes[0]


def test_timeline_unknown_job_raises_lookup_error(ready) -> None:
    with pytest.raises(LookupError):
        track_svc.build_timeline(ready, 999)


# ===========================================================================
# ② 面试与问题
# ===========================================================================
def test_interview_add_advances_application_and_writes_event(ready) -> None:
    """面试登记会把投递推进到 interview，并写一条 interview_scheduled 事件。"""
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied")
    interview = interview_svc.add_interview(
        ready, job.id, round_="一面", scheduled_at=now_utc() + timedelta(days=2), mode="视频"
    )
    ready.commit()

    application = apply_svc.get_latest_application(ready, job.id)
    assert application.status == "interview"
    events = ready.scalars(select(ApplicationEvent)).all()
    assert "interview_scheduled" in {event.event_type for event in events}
    assert interview.scheduled_at is not None


def test_interview_add_does_not_drag_status_back(ready) -> None:
    """补记一场已经面完的面试，不该把已到 offer 的投递拽回来。"""
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied")
    apply_svc.set_status(ready, job.id, "interview")
    apply_svc.set_status(ready, job.id, "offer")
    ready.commit()

    interview_svc.add_interview(ready, job.id, round_="终面", status="已面")
    ready.commit()
    assert apply_svc.get_latest_application(ready, job.id).status == "offer"


def test_interview_list_upcoming_filters_by_status_and_time(ready) -> None:
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied")
    interview_svc.add_interview(
        ready, job.id, round_="一面", scheduled_at=now_utc() + timedelta(days=3)
    )
    interview_svc.add_interview(
        ready, job.id, round_="二面", scheduled_at=now_utc() - timedelta(days=3), status="已面"
    )
    ready.commit()

    upcoming = interview_svc.list_interviews(ready, upcoming=True)
    assert [row.interview.round for row in upcoming] == ["一面"]
    assert len(interview_svc.list_interviews(ready)) == 2


def test_interview_rejects_unknown_status(ready) -> None:
    job = ready.scalars(select(Job)).one()
    with pytest.raises(interview_svc.InterviewError):
        interview_svc.add_interview(ready, job.id, status="随便")


def test_question_add_validates_rating_and_owner(ready) -> None:
    """`--rating` 只有三档；既没给面试也没给岗位要报错。"""
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied")
    with pytest.raises(question_svc.QuestionError):
        question_svc.add_question(ready, question="X", job_id=job.id, rating="excellent")
    with pytest.raises(question_svc.QuestionError):
        question_svc.add_question(ready, question="X")


def test_question_add_accepts_job_only(ready) -> None:
    """面完忘了是哪一场时，只给 `--job` 也能记（迁移 0002 让 interview_id 可空）。"""
    job = ready.scalars(select(Job)).one()
    row = question_svc.add_question(
        ready, question="Redis 缓存穿透怎么处理？", job_id=job.id, category="Redis", weak=True
    )
    ready.commit()
    assert row.interview_id is None
    assert row.job_id == job.id
    assert row.source == "recorded"


def test_predict_questions_stores_with_job_and_source(ready, settings) -> None:
    """`jh question predict` 存进行 `interview_questions`，source=predicted、挂岗位。"""
    job = ready.scalars(select(Job)).one()
    provider = StubProvider(
        json.dumps(
            {
                "questions": [
                    {
                        "question": "这 3 个模块怎么划分的？",
                        "category": "项目细节",
                        "why": "验证真实性",
                    },
                    {
                        "question": "你了解过 Kubernetes 吗？",
                        "category": "Kubernetes",
                        "why": "JD 优先项",
                    },
                ]
            },
            ensure_ascii=False,
        )
    )
    result = question_svc.predict_questions(
        ready, job, count=2, provider=provider, settings=settings
    )
    ready.commit()

    assert len(result.stored) == 2
    row = ready.scalars(
        select(InterviewQuestion).where(InterviewQuestion.source == "predicted")
    ).first()
    assert row is not None
    assert row.job_id == job.id and row.interview_id is None
    assert row.category == "项目细节"


def test_predict_questions_dedupes_by_exact_text(ready, settings) -> None:
    """同一个岗位的重复题要跳过（模型重复跑会给出高度相似的题）。"""
    job = ready.scalars(select(Job)).one()
    payload = json.dumps(
        {"questions": [{"question": "同一个问题", "category": "项目细节", "why": "x"}]},
        ensure_ascii=False,
    )
    first = question_svc.predict_questions(
        ready, job, provider=StubProvider(payload), settings=settings, use_cache=False
    )
    ready.commit()
    second = question_svc.predict_questions(
        ready, job, provider=StubProvider(payload), settings=settings, use_cache=False
    )
    ready.commit()

    assert len(first.stored) == 1
    assert not second.stored and len(second.skipped) == 1


def test_predict_without_key_explains_what_still_works(ready, settings) -> None:
    job = ready.scalars(select(Job)).one()
    with pytest.raises(question_svc.QuestionError) as excinfo:
        question_svc.predict_questions(ready, job, settings=settings)
    assert "jh weak" in str(excinfo.value)


def test_predict_uses_task_scoped_cache(ready, settings) -> None:
    """缓存 key 必须带 task=question_predict（否则会与 match/greeting 撞车）。"""
    from jobhunter.models import LLMCache

    job = ready.scalars(select(Job)).one()
    payload = json.dumps(
        {"questions": [{"question": "Q", "category": "C", "why": "W"}]}, ensure_ascii=False
    )
    provider = StubProvider(payload, payload)
    question_svc.predict_questions(ready, job, provider=provider, settings=settings)
    ready.commit()
    second = question_svc.predict_questions(ready, job, provider=provider, settings=settings)
    assert len(provider.calls) == 1, "第二次该命中缓存"
    assert second.from_cache is True
    assert [row.task for row in ready.scalars(select(LLMCache))] == ["question_predict"]


# ===========================================================================
# ③ 薄弱点聚合
# ===========================================================================
def _seed_questions(session, job: Job, rows: list[tuple[str, str, bool, str]]) -> None:
    """批量塞题：`(问题, 分类, 是否弱, 来源)`。"""
    for text, category, weak, source in rows:
        session.add(
            InterviewQuestion(
                job_id=job.id,
                question=text,
                category=category,
                is_weak=weak,
                source=source,
            )
        )
    session.commit()


def test_weak_sorts_by_weak_count_desc(ready) -> None:
    """依据：`weak_cmd` docstring「排序口径是**没答上来次数**而不是平均分」。"""
    job = ready.scalars(select(Job)).one()
    _seed_questions(
        ready,
        job,
        [
            ("前端性能 1", "前端性能", True, "recorded"),
            ("前端性能 2", "前端性能", True, "recorded"),
            ("前端性能 3", "前端性能", True, "recorded"),
            ("数据库索引 1", "数据库索引", True, "recorded"),
            ("数据库索引 2", "数据库索引", True, "recorded"),
            ("Agent 编排 1", "Agent 编排", True, "recorded"),
            ("Agent 编排 2", "Agent 编排", False, "recorded"),
            ("Agent 编排 3", "Agent 编排", False, "recorded"),
        ],
    )
    report = weak_svc.build_weak_report(ready)
    assert [(row.category, row.weak_count) for row in report.rows] == [
        ("前端性能", 3),
        ("数据库索引", 2),
        ("Agent 编排", 1),
    ]
    assert "🔴" in report.rows[0].conclusion
    assert "前端性能" in report.headline


def test_weak_splits_recorded_and_predicted(ready) -> None:
    """两类题分开展示：预测题答不上来只说明没准备过，不代表能力缺口。"""
    job = ready.scalars(select(Job)).one()
    _seed_questions(
        ready,
        job,
        [
            ("真实题", "Redis", True, "recorded"),
            ("预测题", "Redis", True, "predicted"),
        ],
    )
    report = weak_svc.build_weak_report(ready)
    row = report.rows[0]
    assert (row.recorded_weak, row.recorded_total) == (1, 1)
    assert (row.predicted_weak, row.predicted_total) == (1, 1)

    only_recorded = weak_svc.build_weak_report(ready, recorded_only=True)
    assert only_recorded.rows[0].total == 1
    assert only_recorded.predicted_count == 0


def test_weak_reports_uncategorized_questions(ready) -> None:
    """没分类的题要**说出来**：分类是唯一的聚合维度，静默忽略等于数据消失。"""
    job = ready.scalars(select(Job)).one()
    _seed_questions(ready, job, [("没分类", "", True, "recorded")])
    report = weak_svc.build_weak_report(ready)
    assert report.uncategorized == 1
    assert weak_svc.UNCategorized in [row.category for row in report.rows]


def test_weak_cli_end_to_end(ready) -> None:
    job = ready.scalars(select(Job)).one()
    _seed_questions(
        ready,
        job,
        [("Q1", "Redis", True, "recorded"), ("Q2", "Redis", True, "recorded")],
    )
    result = runner.invoke(app, ["--no-remind", "weak"])
    assert result.exit_code == 0, result.output
    assert "Redis" in result.output
    assert "薄弱点" in result.output


# ===========================================================================
# ④ 统计口径
# ===========================================================================
def test_stats_counts_at_least_reached_not_current_status(ready) -> None:
    """★★ 口径测试：被拒的投递也要算进它**走到过**的每一步。

    依据：`stats_svc` docstring「按当前状态统计会系统性低估：
    一个现在是 rejected 的投递，很可能走到过二面」。
    """
    job = ready.scalars(select(Job)).one()
    other = make_job(company="乙公司", title="后端", jd_hash="q" * 64)
    ready.add(other)
    ready.commit()

    # 岗位一：走到 interview 后被拒（历史高水位 = interview）
    _apply(ready, job, "applied")
    apply_svc.set_status(ready, job.id, "interview")
    apply_svc.set_status(ready, job.id, "rejected")
    # 岗位二：只投了，没有任何动静
    _apply(ready, other, "applied")
    ready.commit()

    report = stats_svc.build_stats(ready)
    assert report.overall.total == 2
    assert report.overall.viewed == 1, "被拒的那条也应该算'至少被看过'"
    assert report.overall.replied == 1
    assert report.overall.interview == 1
    assert report.overall.rejected == 1
    assert report.overall.rate("interview") == 0.5


def test_stats_groups_by_platform_variant_and_direction(ready) -> None:
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied")
    apply_svc.set_status(ready, job.id, "replied")
    ready.commit()
    # 变体由 today_svc 写入；这里直接改字段（模拟 jh today 生成过开场白）
    application = apply_svc.get_latest_application(ready, job.id)
    application.greeting_variant = "with_project"
    ready.commit()

    report = stats_svc.build_stats(ready)
    assert [item.platform for item in report.platforms] == ["BOSS"]
    assert [item.variant for item in report.variants] == ["with_project"]
    assert [item.direction for item in report.directions] == ["Python 开发"]


def test_stats_days_window_filters_old_applications(ready) -> None:
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied", days_ago=30)
    report = stats_svc.build_stats(ready, days=7)
    assert report.overall.total == 0
    assert stats_svc.build_stats(ready).overall.total == 1


def test_stats_sample_note_warns_about_small_samples(ready) -> None:
    """★ 纪律：样本小的时候必须明说"别下结论"（不宣称统计显著性）。"""
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied")
    ready.commit()
    report = stats_svc.build_stats(ready)
    assert "样本量很小" in report.sample_note
    assert any("不宣称统计显著性" in note for note in report.notes)


def test_stats_format_rate_carries_numerator_and_denominator() -> None:
    """只有百分比看不出 `100%` 是 1/1 还是 20/20 —— 必须带分子分母。"""
    assert stats_svc.format_rate(0.5, 4) == "50.0%（2/4）"
    assert stats_svc.format_rate(None, 0) == "—"


def test_stats_cli_end_to_end(ready) -> None:
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied")
    ready.commit()
    result = runner.invoke(app, ["--no-remind", "stats"])
    assert result.exit_code == 0, result.output
    assert "转化率统计" in result.output
    assert "回复率" in result.output
    assert "开场白变体" in result.output


def test_stats_export_writes_new_file_only(ready, settings) -> None:
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied")
    ready.commit()
    first = runner.invoke(app, ["--no-remind", "stats", "--export", "md"])
    second = runner.invoke(app, ["--no-remind", "stats", "--export", "md"])
    assert first.exit_code == 0 and second.exit_code == 0, first.output
    files = sorted(path.name for path in settings.paths.output_dir.glob("stats*.md"))
    assert len(files) == 2, f"两次导出应当是两份新文件：{files}"


# ===========================================================================
# ⑤ 周报
# ===========================================================================
def test_report_renders_sections_and_writes_new_file(ready, settings) -> None:
    """周报：五节齐全、只写新文件、数字与 jh stats 同源。"""
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied", days_ago=1)
    apply_svc.set_status(ready, job.id, "replied")
    ready.commit()

    data = report_svc.build_report(ready)
    text = report_svc.render_markdown(data)
    for heading in ("这一周的数字", "这一周发生了什么", "面试与问题", "薄弱点", "下周该干什么"):
        assert heading in text
    assert "对方回复" in text, "时间轴上的事件要进周报"

    path = report_svc.export_report(data, settings=settings)
    path2 = report_svc.export_report(data, settings=settings)
    assert path != path2 and path.is_file() and path2.is_file(), "只写新文件"


def test_report_states_a_quiet_week_honestly(ready, settings) -> None:
    """空白周照实写"这一周没投"，不拿累计数据充数。"""
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied", days_ago=40)  # 40 天前投的，不属于本周
    ready.commit()
    data = report_svc.build_report(ready)
    assert data.quiet_week is True
    text = report_svc.render_markdown(data)
    assert "本周期没有投递记录" in text


def test_report_week_start_is_monday_beijing(ready) -> None:
    """周期起点 = 本周一 00:00（北京时间）—— 与"最近 7 天"不同，要和人对得上。"""
    start = report_svc.week_start()
    from jobhunter.utils.time import to_local

    local = to_local(start)
    assert local.weekday() == 0
    assert (local.hour, local.minute) == (0, 0)


def test_report_cli_end_to_end(ready, settings) -> None:
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied")
    ready.commit()
    result = runner.invoke(app, ["--no-remind", "report"])
    assert result.exit_code == 0, result.output
    assert "已生成周报" in result.output
    assert list(settings.paths.output_dir.glob("report-*.md"))


# ===========================================================================
# ⑥ 追问话术（假 Provider 的确定性断言 + 模板兜底）
# ===========================================================================
def _followup_text(**overrides: Any) -> str:
    base = {
        "text": (
            "您好，我 5 天前投递了 Python 后端开发实习生岗位。"
            "我做过订单管理系统的后端接口开发，主要用 Python + FastAPI，和岗位要求比较对口。"
            "想请问这个岗位目前还在推进吗？如果需要补充材料，随时告诉我，谢谢！"
        )
    }
    base.update(overrides)
    return json.dumps(base, ensure_ascii=False)


def test_followup_without_key_uses_template(ready, settings) -> None:
    """没配 key → 模板兜底，并把原因说出来（不静默）。"""
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied", days_ago=5)
    result = followup_svc.generate_followup(ready, job, settings=settings)
    assert result.source == "template"
    assert result.days == 5
    assert any("没有配置大模型密钥" in item for item in result.issues)
    assert "Python" in result.text, "模板必须引用真实素材"
    assert len(result.text) <= followup_svc.MAX_CHARS


def test_followup_rejects_fabricated_text(ready, settings) -> None:
    """编造类文案**一律降级模板**（闸门在追问路径上同样生效）。"""
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied", days_ago=5)
    provider = StubProvider(
        json.dumps(
            {"text": "您好，我精通 Python、Go、Kubernetes，性能提升 300%，希望尽快回复。"},
            ensure_ascii=False,
        )
    )
    result = followup_svc.generate_followup(ready, job, provider=provider, settings=settings)
    assert result.source == "template"
    assert any(("编造" in item) or ("数据" in item) or ("技能" in item) for item in result.issues)


def test_followup_rejects_banned_words_and_overlong(ready, settings) -> None:
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied", days_ago=5)
    banned = StubProvider(json.dumps({"text": "贵司的 Python 岗位还在招吗？"}, ensure_ascii=False))
    assert (
        followup_svc.generate_followup(ready, job, provider=banned, settings=settings).source
        == "template"
    )
    long_text = "您好，" + "我用 Python 做过订单管理系统。" * 12
    overlong = StubProvider(json.dumps({"text": long_text}, ensure_ascii=False))
    # ★ use_cache=False：两次调用用的是同一个假 model 名，不关缓存的话
    #   第二次会命中第一次的缓存（这本身说明缓存生效，但会让这条断言测错东西）
    result = followup_svc.generate_followup(
        ready, job, provider=overlong, settings=settings, use_cache=False
    )
    assert result.source == "template"
    assert any("太长" in item for item in result.issues)


def test_followup_accepts_a_truthful_message(ready, settings) -> None:
    """真话必须能通过（否则这条路径永远只有模板）。"""
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied", days_ago=5)
    provider = StubProvider(_followup_text())
    result = followup_svc.generate_followup(ready, job, provider=provider, settings=settings)
    assert result.source == "llm", result.issues
    assert result.text.startswith("您好")


def test_followup_cache_is_task_scoped(ready, settings) -> None:
    from jobhunter.models import LLMCache

    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied", days_ago=5)
    provider = StubProvider(_followup_text(), _followup_text())
    followup_svc.generate_followup(ready, job, provider=provider, settings=settings)
    followup_svc.generate_followup(ready, job, provider=provider, settings=settings)
    assert len(provider.calls) == 1
    assert [row.task for row in ready.scalars(select(LLMCache))] == ["followup"]


def test_followup_sent_records_event(ready) -> None:
    """`--sent` 才写 followup 事件：生成 ≠ 发送。"""
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied", days_ago=5)
    followup_svc.record_sent(ready, job.id, text="您好…", style="简短", days=5)
    ready.commit()
    events = ready.scalars(select(ApplicationEvent)).all()
    assert "followup" in {event.event_type for event in events}
    timeline = track_svc.build_timeline(ready, job.id)
    assert any("追问" in entry.label for entry in timeline.entries)


def test_followup_cli_end_to_end(ready) -> None:
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied", days_ago=5)
    result = runner.invoke(app, ["--no-remind", "followup", str(job.id)])
    assert result.exit_code == 0, result.output
    assert "追问话术" in result.output
    assert "本工具不会替你发送任何消息" in result.output


def test_followup_cli_rejects_bad_variant(ready) -> None:
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied")
    result = runner.invoke(app, ["--no-remind", "followup", str(job.id), "--variant", "随便"])
    assert result.exit_code == 2


# ===========================================================================
# ⑦ 看板 / 时间轴的 CLI
# ===========================================================================
def test_apply_timeline_cli(ready) -> None:
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied", days_ago=3)
    result = runner.invoke(app, ["--no-remind", "apply", "--timeline", str(job.id)])
    assert result.exit_code == 0, result.output
    assert "投递时间轴" in result.output
    assert "已投递" in result.output


def test_apply_timeline_unknown_job(ready) -> None:
    result = runner.invoke(app, ["--no-remind", "apply", "--timeline", "999"])
    assert result.exit_code == 1


def test_interview_and_question_cli_flow(ready) -> None:
    """验收路径的一段：约面 → 记 3 题（2 弱）→ jh weak 聚合正确。"""
    job = ready.scalars(select(Job)).one()
    _apply(ready, job, "applied")

    added = runner.invoke(
        app,
        [
            "--no-remind",
            "interview",
            "add",
            "--job",
            str(job.id),
            "--round",
            "一面",
            "--at",
            "2026-09-11 14:00",
            "--mode",
            "视频",
        ],
    )
    assert added.exit_code == 0, added.output
    interview = ready.scalars(select(Interview)).one()

    for text, rating, weak in [
        ("Redis 缓存穿透怎么处理？", "bad", True),
        ("FastAPI 依赖注入怎么用？", "ok", False),
        ("项目里最难的地方是什么？", "bad", True),
    ]:
        result = runner.invoke(
            app,
            [
                "--no-remind",
                "question",
                "add",
                "--interview",
                str(interview.id),
                "--q",
                text,
                "--rating",
                rating,
                "--category",
                "Redis",
                *(["--weak"] if weak else []),
            ],
        )
        assert result.exit_code == 0, result.output

    weak = runner.invoke(app, ["--no-remind", "weak"])
    assert weak.exit_code == 0
    assert "Redis" in weak.output and "2" in weak.output


# ===========================================================================
# ⑧ 提示词文件的版本纪律（P6 新增两个提示词，同一个规矩）
# ===========================================================================
#: P6 新增的两个提示词正文指纹（改正文必须 +1 版本号，见 `llm/prompts.py`）。
#: ★ 与 `test_tailor_svc.PROMPT_BODY_SHA256` 同一套纪律 —— 缓存 key 含版本号，
#:   "改了提示词却在吃旧结果"是最难查的一类静默失效。
PROMPT_BODY_SHA256 = {
    ("question.md", 1): "87a89fc91c3514ba68ad20e280fd4ddfcf23fc9d80c8b1a8c8258cbcea3eff4a",
    ("followup.md", 1): "ba14707a305f9144462d09d6e9d574aebf17c306de79a532cc8f8e8954371de5",
}

SEVEN_ELEMENTS = ("角色", "任务", "依据", "约束与边界", "输出格式", "示例", "思考方式")


@pytest.mark.parametrize("name", ["question.md", "followup.md"])
def test_prompt_files_have_seven_elements_and_version(name: str) -> None:
    """依据：计划书要求系统提示词放 `prompts/*.md` 且七要素齐全。"""
    from jobhunter.llm.prompts import load_prompt_file

    prompt = load_prompt_file(name)
    for element in SEVEN_ELEMENTS:
        assert element in prompt.body, f"{name} 缺少要素：{element}"
    digest = prompt.sha256
    expected = PROMPT_BODY_SHA256.get((name, prompt.version))
    assert expected is not None, (
        f"{name} 的版本 {prompt.version} 还没登记正文指纹：{digest}"
        "（把它加进 PROMPT_BODY_SHA256 再提交）"
    )
    assert digest == expected, (
        f"{name} 的正文改了但版本号没变 —— 缓存不会失效。"
        f"请把 prompt-version 改成 {prompt.version + 1} 并更新指纹"
    )


def test_prompt_placeholders_use_template_syntax() -> None:
    """占位符必须是 `$var`（Template），因为正文里有 JSON 花括号。"""
    from jobhunter.llm.prompts import load_prompt_file

    question = load_prompt_file("question.md")
    followup = load_prompt_file("followup.md")
    assert "$count" in question.body
    assert "$max_chars" in followup.body and "$banned" in followup.body

"""【P4.5 专项】零手打跟进：批量打标 / 模糊匹配 / 待处理清单 / 顶部固定提醒。

设计文档【提示词 6】的验收标准：
    「能用一条命令批量打标；顶部提醒能正确统计"3/7/14 天"；pytest 绿」
以及它点名要测的三件事：批量语法解析、模糊匹配（多命中时不误改）、状态变更写事件。

P4.5 的痛点原话：*"一天 20 个岗位要敲 20 次命令 → 会第 3 天弃用"* ——
所以这里每条都在问同一件事：**"什么都不想敲"的时候，工具还在产生价值吗？**
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from typer.testing import CliRunner

from jobhunter.cli.app import app
from jobhunter.cli.commands.apply_cmd import _parse_pairs
from jobhunter.config.settings import get_settings
from jobhunter.models import Application, Job
from jobhunter.services import apply_svc, greeting_svc, reminder_svc, resume_svc
from jobhunter.utils.time import DISPLAY_TZ
from tests.verify._helpers import build_detail, insert_job, insert_resume, score_job

runner = CliRunner()


def _seed(session, settings, *, count: int = 3, company_prefix: str = "虚构公司"):  # noqa: ANN001, ANN202
    insert_resume(session)
    jobs = []
    for index in range(count):
        job = insert_job(
            session,
            build_detail(
                company=f"{company_prefix}{index}",
                title=f"AI Agent 开发工程师 {index}",
                platform_job_id=f"p45-{company_prefix}-{index}",
                url=f"https://www.zhipin.com/job_detail/p45-{company_prefix}-{index}.html",
            ),
        )
        score_job(session, job, settings)
        jobs.append(job)
    session.flush()
    return jobs


def _status_of(session, job_id: int) -> str:  # noqa: ANN001
    session.expire_all()
    row = session.scalars(select(Application).where(Application.job_id == job_id)).first()
    return row.status if row else "(无记录)"


# ===========================================================================
# ① 批量语法解析（设计文档点名要测）
# ===========================================================================
@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (["3=applied"], [("3", "applied")]),
        (["3=applied", "7=replied"], [("3", "applied"), ("7", "replied")]),
        (["#12=rejected"], [("#12", "rejected")]),
        (["3 = applied"], [("3", "applied")]),  # 容忍空格
        (["  5=viewed  ", "#9=offer"], [("5", "viewed"), ("#9", "offer")]),  # 混写 + 首尾空白
    ],
)
def test_pair_syntax_is_parsed(tokens: list[str], expected: list[tuple[str, str]]) -> None:
    """依据：设计文档 §5.2「批量打标（一条命令搞定一整轮）：`jh apply 3=applied 7=replied`」。"""
    parsed, errors = _parse_pairs(tokens)
    assert not errors, f"合法输入被拒了：{errors}"
    assert parsed == expected


@pytest.mark.parametrize("token", ["3applied", "3=", "=applied", "3=已投", "3=banana"])
def test_pair_syntax_rejects_bad_tokens(token: str) -> None:
    """对抗：畸形 / 未知状态的批量 token。

    依据：`apply_svc.set_status` docstring「写了不认识的状态要**报错拒绝**，
    而不是静默建一个野状态（那会让看板统计出现永远对不上的分类）」。

    注：`abc=applied` 这类"格式对但定位符解析不出"的 token **不在这里判**
    （`_parse_pairs` 只管格式与状态），它由 `_resolve_target` → CLI 层的
    "找不到对应岗位" 分支处理，见 `test_batch_with_any_error_changes_nothing`。
    """
    _parsed, errors = _parse_pairs([token])
    assert errors, f"非法 token {token!r} 被接受了"


def test_design_doc_example_skipped_is_supported() -> None:
    """★★ 依据：设计文档【提示词 6】把这一行当成**零手打跟进的范例**写死的：

        jh apply 3=applied 7=replied 12=skipped

    但 `APPLICATION_STATUSES` 里没有 `skipped`（它是**岗位**状态；
    投递状态是 pending/applied/viewed/replied/assessment/interview/offer/rejected）
    → 这条范例输出 "状态 `skipped` 不认识"，退出码 2。

    失败是**安全的**（报错、零改动），但**照着文档敲的第一条命令就报错**，
    对"会用一条命令处理一整轮"这个卖点是实打实的伤害。

    期望：要么让 `N=skipped` 真的生效（顺带把岗位标 skipped —— 语义就是"这个岗我不投了"），
    要么在错误信息里直接给出替代命令（`jh job skip <id>`）。
    """
    parsed, errors = _parse_pairs(["12=skipped"])
    assert not errors, f"设计文档的范例被拒了：{errors}"
    assert parsed == [("12", "skipped")]


def test_batch_with_any_error_changes_nothing(session, engine) -> None:  # noqa: ARG001
    """★★ 依据：`_mark_batch` docstring「**全对才执行、有错就一条都不动**，
    是唯一让人放心的语义」。

    构造：一条合法 + 一条越界序号。断言 ① 退出码 2；② **合法那条也没被改**。
    半成功半失败的批量比直接报错危险得多 —— 用户不知道该不该重跑。
    """
    jobs = _seed(session, settings=get_settings(), count=2)
    session.commit()

    before = [_status_of(session, job.id) for job in jobs]
    result = runner.invoke(app, ["apply", "1=applied", "999=applied"])

    assert result.exit_code == 2, f"有错的批量应当以 2 退出：\n{result.output}"
    after = [_status_of(session, job.id) for job in jobs]
    assert after == before, f"有错时仍然改了状态：{before} → {after}"


def test_batch_unknown_status_changes_nothing(session, engine) -> None:  # noqa: ARG001
    """同上的另一个入口：状态拼错。"""
    jobs = _seed(session, settings=get_settings(), count=1)
    session.commit()

    result = runner.invoke(app, ["apply", "1=banana"])
    assert result.exit_code == 2, f"未知状态应当以 2 退出：\n{result.output}"
    assert _status_of(session, jobs[0].id) == "(无记录)", "状态拼错却建了记录"


def test_batch_marks_and_writes_events(session, engine) -> None:  # noqa: ARG001
    """依据：设计文档 §4「**所有状态变更都写 `application_events`**（可画时间轴、可归因）」。"""
    jobs = _seed(session, settings=get_settings(), count=2)
    session.commit()

    result = runner.invoke(app, ["apply", "1=applied", "2=rejected"])
    assert result.exit_code == 0, f"批量打标失败：\n{result.output}"
    assert _status_of(session, jobs[0].id) == "applied"
    assert _status_of(session, jobs[1].id) == "rejected"

    events = session.execute(
        text("SELECT COUNT(*) FROM application_events WHERE event_type = 'status_change'")
    ).scalar_one()
    assert events == 2, f"两条打标应当写 2 条事件，实际 {events}"


def test_batch_by_job_id_works(session, engine) -> None:  # noqa: ARG001
    """依据：交付声明「`#job_id=status`」—— 不依赖 `jh today` 的序号。"""
    jobs = _seed(session, settings=get_settings(), count=1)
    session.commit()

    result = runner.invoke(app, ["apply", f"#{jobs[0].id}=viewed"])
    assert result.exit_code == 0, f"按 job_id 打标失败：\n{result.output}"
    assert _status_of(session, jobs[0].id) == "viewed"


# ===========================================================================
# ② 模糊匹配（设计文档点名：多命中时不误改）
# ===========================================================================
def test_fuzzy_multi_hit_changes_nothing(session, engine) -> None:  # noqa: ARG001
    """★★ 依据：设计文档 §5.2「模糊匹配……匹配到多条时**列出来让我选**，绝不猜一个然后改错」
    + `apply_cmd.py` docstring「打错状态的代价远大于再敲一次命令」。

    构造两家名字都含"虚构"的公司 → 一次模糊匹配命中 2 条 → **零改动**。
    """
    jobs = _seed(session, settings=get_settings(), count=2, company_prefix="虚构")
    session.commit()
    for job in jobs:  # 先让它们有 pending 记录（模糊匹配只在待处理集里找）
        apply_svc.ensure_pending_application(session, job)
    session.commit()

    result = runner.invoke(app, ["apply", "虚构", "--status", "applied"])
    assert result.exit_code == 1, f"多命中应当以 1 退出（列出来让人选）：\n{result.output}"
    assert "没有替你猜" in result.output or "匹配到" in result.output
    for job in jobs:
        assert _status_of(session, job.id) == "pending", (
            f"多命中时改了状态（job #{job.id}）—— 这正是'以为标的是这家、其实标了另一家'"
        )


def test_fuzzy_multi_hit_with_yes_changes_all(session, engine) -> None:  # noqa: ARG001
    """依据：`--yes` 的语义「模糊匹配到多条时不询问，全部应用」。"""
    jobs = _seed(session, settings=get_settings(), count=2, company_prefix="虚构")
    session.commit()
    for job in jobs:
        apply_svc.ensure_pending_application(session, job)
    session.commit()

    result = runner.invoke(app, ["apply", "虚构", "--status", "applied", "--yes"])
    assert result.exit_code == 0, f"--yes 应当全改：\n{result.output}"
    for job in jobs:
        assert _status_of(session, job.id) == "applied"


def test_fuzzy_single_hit_changes_it(session, engine) -> None:  # noqa: ARG001
    """单命中时直接改（不用再确认一次，否则"零手打"就落空了）。"""
    jobs = _seed(session, settings=get_settings(), count=2, company_prefix="别家")
    unique = insert_job(
        session,
        build_detail(
            company="独此一家科技有限公司",
            title="AI Agent 工程师",
            platform_job_id="p45-unique",
            url="https://www.zhipin.com/job_detail/p45-unique.html",
        ),
    )
    apply_svc.ensure_pending_application(session, unique)
    session.commit()
    assert jobs

    result = runner.invoke(app, ["apply", "独此一家", "--status", "viewed"])
    assert result.exit_code == 0, f"单命中应当直接改：\n{result.output}"
    assert _status_of(session, unique.id) == "viewed"


def test_fuzzy_no_hit_is_reported(session, engine) -> None:  # noqa: ARG001
    """边界：关键词一个都不匹配 → 可读提示 + 非 0 退出。"""
    _seed(session, settings=get_settings(), count=1)
    session.commit()
    result = runner.invoke(app, ["apply", "根本不存在的公司", "--status", "applied"])
    assert result.exit_code == 1
    assert "没有匹配" in result.output
    assert "Traceback" not in result.output


# ===========================================================================
# ③ 待处理清单
# ===========================================================================
def test_pending_lists_actionable_items(session, engine) -> None:  # noqa: ARG001
    """依据：设计文档 §5.2.5「`jh apply --list-pending`：列出'待处理'的投递
    （供批量打标参考序号）」。"""
    jobs = _seed(session, settings=get_settings(), count=2)
    apply_svc.set_status(session, jobs[0].id, "applied")
    session.commit()

    result = runner.invoke(app, ["apply", "--pending"])
    assert result.exit_code == 0, f"--pending 失败：\n{result.output}"
    assert str(jobs[0].id) in result.output, "已投的岗位没出现在待处理清单里"


def test_pending_shows_age_in_days(session, settings) -> None:
    """依据：`PendingRow` 带 `days` 与 `reason`（"投后 N 天，该跟进一下了"）——
    那是"该不该跟进"的唯一线索，也是 `--pending` 相对普通列表的全部价值。
    """
    jobs = _seed(session, settings, count=1)
    applied_at = datetime(2026, 9, 10, 4, 0, tzinfo=UTC)  # 北京 09-10 12:00
    apply_svc.set_status(session, jobs[0].id, "applied", occurred_at=applied_at)
    session.flush()

    rows = apply_svc.list_actionable(session, now=datetime(2026, 9, 15, 4, 0, tzinfo=UTC))
    assert rows, "list_actionable 没返回任何待处理项"
    row = rows[0]
    assert row.days == 5, f"投了 5 天，days 却是 {row.days}"
    assert "5" in row.reason, f"reason 里没有天数：{row.reason!r}"
    assert row.job_id == jobs[0].id and row.status == "applied"


# ===========================================================================
# ④ 顶部固定提醒（设计文档 §5.2.3）
# ===========================================================================
def test_stale_buckets_are_cumulative(session, settings) -> None:
    """★★ 依据：`_stale_counts` 的 `age >= threshold` —— 三个桶是**累计**的：
    投了 14 天的那条，同时也算"3 天未跟进"和"7 天无动静"。

    设计方案里写的是"投后 3 天未跟进 5 条 ｜ 投后 7 天无动静 3 条 ｜ 投后 14 天建议放弃 1 条"
    —— 5 ≥ 3 ≥ 1 这种非增关系正是累计桶的形状（不是互斥分桶）。
    """
    jobs = _seed(session, settings, count=3)
    now = datetime(2026, 9, 18, 4, 0, tzinfo=UTC)  # 北京 09-18 12:00
    for job, days in zip(jobs, (3, 7, 14), strict=True):
        occurred = now - timedelta(days=days)
        apply_svc.set_status(session, job.id, "applied", occurred_at=occurred)
    session.flush()

    summary = reminder_svc.collect_reminders(session, settings=settings, now=now)
    assert summary.stale_followup == [3, 2, 1], (
        f"累计桶不对：期望 [3,2,1]（3天:3条 / 7天:2条 / 14天:1条），实际 {summary.stale_followup}"
    )


def test_stale_uses_beijing_calendar_days(session, settings) -> None:
    """★★ 依据：设计文档 §3.2 + `days_since` docstring「按**自然日**（按北京时间算）」。

    构造两条只差 1 小时（UTC）的投递，但**北京日期差一天**：
        北京 09-15 23:30（UTC 09-15 15:30）→ 到 09-18 是 3 天
        北京 09-16 00:30（UTC 09-15 16:30）→ 到 09-18 是 2 天
    → 只有第一条该进"3 天未跟进"桶。用 UTC 日算的话两条都会进。
    """
    jobs = _seed(session, settings, count=2)
    now = datetime(2026, 9, 18, 4, 0, tzinfo=UTC)
    just_before_midnight = datetime(2026, 9, 15, 15, 30, tzinfo=UTC)  # 北京 09-15 23:30
    just_after_midnight = datetime(2026, 9, 15, 16, 30, tzinfo=UTC)  # 北京 09-16 00:30
    apply_svc.set_status(session, jobs[0].id, "applied", occurred_at=just_before_midnight)
    apply_svc.set_status(session, jobs[1].id, "applied", occurred_at=just_after_midnight)
    session.flush()

    summary = reminder_svc.collect_reminders(session, settings=settings, now=now)
    assert summary.stale_followup[0] == 1, (
        f"按 UTC 日算了（北京 09-15 23:30 与 09-16 00:30 都被算成 3 天）："
        f"{summary.stale_followup}；期望只有 1 条进 3 天桶"
    )
    assert DISPLAY_TZ.key == "Asia/Shanghai"


@pytest.mark.parametrize("terminal", ["interview", "offer", "rejected", "assessment"])
def test_terminal_states_are_not_nagged(session, settings, terminal: str) -> None:
    """★★ 依据：`_WAITING_STATUSES` 的注释「已经进入面试/Offer 流程的不该再催」。

    "投后 14 天建议放弃"如果对已经拿到 Offer 的岗位也报，用户会立刻不信这个提醒。
    """
    jobs = _seed(session, settings, count=1)
    now = datetime(2026, 9, 18, 4, 0, tzinfo=UTC)
    old = now - timedelta(days=30)
    apply_svc.set_status(session, jobs[0].id, "applied", occurred_at=old)
    session.flush()
    apply_svc.set_status(session, jobs[0].id, terminal, occurred_at=old)
    session.flush()

    summary = reminder_svc.collect_reminders(session, settings=settings, now=now)
    assert summary.stale_followup == [0, 0, 0], (
        f"状态是 {terminal!r}（终态/已进流程）却还在催：{summary.stale_followup}"
    )


def test_empty_summary_prints_nothing_at_all() -> None:
    """★★ 依据：`format_reminders` docstring「只打印**非零**的部分……
    0 条的类别混在里面会让人每次都重新读一遍才能确认'今天没什么事'，那是纯噪音」
    + `is_empty` docstring「此时**不打印任何内容**」。

    "没有待办时完全静默"是这条能力能不能长期存在的前提 ——
    一个每次敲命令都刷三行"0 条"的提醒，两周内就会被用户要求关掉。
    """
    assert reminder_svc.format_reminders(reminder_svc.ReminderSummary()) == []


def test_only_nonzero_categories_are_printed() -> None:
    """同一纪律的另一面：只有 3 天桶非零时，不该出现"7 天 / 14 天"字样。"""
    summary = reminder_svc.ReminderSummary(stale_followup=[2, 0, 0])
    lines = reminder_svc.format_reminders(summary)
    joined = "\n".join(lines)
    assert "投后 3 天未跟进 2 条" in joined, f"非零类别没打印：{joined}"
    assert "7 天" not in joined and "14 天" not in joined, f"零值类别被打印了（噪音）：{joined}"


def test_reminders_are_silent_when_database_is_absent(isolated_env) -> None:  # noqa: ARG001
    """★★ 依据：`get_reminders` docstring「任何失败都退化成'空提醒'：
    库没建、表被删、文件损坏……提醒**没有资格让主命令失败**」。

    ★ 这条必须**不请求 `engine` fixture**（那样库就建好了）—— 它测的正是"库不存在"。
    """
    summary = reminder_svc.get_reminders()
    assert summary.is_empty, "库没建的时候提醒居然有内容（或抛了异常）"


def test_every_command_still_works_without_database(engine) -> None:  # noqa: ARG001
    """对照：库是好的时候，任意命令照常工作（提醒不能反过来挡住主命令）。"""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, f"jh --help 失败：\n{result.output}"
    assert "Traceback" not in result.output


# ===========================================================================
# ⑤ 字数修复调用（P4.5 顺带修的那个真缺陷）
# ===========================================================================
def _candidate(session, settings):  # noqa: ANN001, ANN202
    insert_resume(session)
    return __import__(
        "jobhunter.services.match_svc", fromlist=["match_svc"]
    ).load_resume_candidates(session, settings)[0]


def _ready(session, settings):  # noqa: ANN001, ANN202
    job = insert_job(session)
    score_job(session, job, settings)
    return job, _candidate(session, settings)


def test_length_only_failure_is_recognised() -> None:
    """依据：`is_length_only_failure` docstring「判定方式是 `reasons` 与
    '这个文本真实存在的字数问题'**集合相等** —— 而不是比数量
    （数量相等会把'禁用词 + 恰好也超字数'误判成纯字数问题）」。

    两个方向都要断言：纯字数 → True；字数 + 禁用词 → False。
    """
    settings = get_settings()
    too_long = "看到岗位要求 Python。" + "补" * 200
    length_reasons = greeting_svc._length_problems(too_long, version="full", settings=settings)
    assert length_reasons, "测试自身问题：构造的文本没有字数问题"

    assert greeting_svc.is_length_only_failure(
        length_reasons, too_long, version="full", settings=settings
    )

    mixed = [*length_reasons, "用了禁用词「贵司」"]
    assert not greeting_svc.is_length_only_failure(
        mixed, too_long, version="full", settings=settings
    ), "字数 + 禁用词被误判成'纯字数问题'（那会让模型去'改一遍'，而不是直接降级）"


def test_fabrication_failure_skips_the_repair_call() -> None:
    """★★ 依据：`is_length_only_failure` docstring「保真类失败（编造了技能/数字/学历）是
    **信任问题**，不是笔误 —— 让同一个模型再写一遍……那类失败一律**直接降级到确定性模板**」。

    验证方式：喂一个"编造 + 超字数"的文本，断言 provider **只被调用 1 次**
    （= 没有发生修复调用）。
    """
    from jobhunter.llm.mock import MockProvider
    from jobhunter.services.resume_svc import ResumeContent

    settings = get_settings()
    resume = ResumeContent(skills=["Python"], raw_text="（fixture）")
    candidate = __import__("jobhunter.services.match_svc", fromlist=["match_svc"]).ResumeCandidate(
        resume_id=1,
        name="主简历",
        direction="AI Agent",
        content=resume,
        content_hash="x" * 64,
        pool={"Python"},
        explicit={"Python"},
    )
    job = Job(
        platform="boss", title="AI Agent", city="深圳", jd_text="要求 Python", jd_hash="h" * 64
    )
    provider = MockProvider()

    fabricated_and_long = json.dumps(
        {
            "full": "我熟悉 Kubernetes，做过 300% 提升。" + "补" * 200,
            "short": "熟悉 Python，方便聊聊吗？",
        },
        ensure_ascii=False,
    )
    parsed = greeting_svc.parse_greeting(fabricated_and_long)
    result = greeting_svc._settle_versions(
        parsed,
        job=job,
        candidate=candidate,
        keywords=["Python"],
        variant="with_project",
        settings=settings,
        vocabulary=resume_svc.load_skill_vocabulary(settings),
        provider=provider,
        previous_message=__import__("jobhunter.llm.base", fromlist=["LLMMessage"]).LLMMessage(
            role="user", content="原始提示词"
        ),
    )

    assert result.source == "template", "编造的内容没被降级"
    assert len(provider.calls) == 0, (
        f"编造类失败却发起了修复调用（provider 被调用 {len(provider.calls)} 次）——"
        "让同一个模型'再写一遍'无法保证它不再编"
    )
    assert not result.repaired, f"编造类失败不该有修复版本：{result.repaired}"


def test_length_failure_triggers_a_repair_call() -> None:
    """★★ 反向：**纯字数**失败必须走修复调用（P4.5 顺带修的那个真缺陷 ——
    实测开场白几乎全部超字数被闸门拦下、整批降级成模板，钱花了拿到模板）。

    这里喂一个纯超字数的 full，断言 provider **被调用 1 次**（发生了修复）。
    """
    from jobhunter.llm.base import LLMMessage
    from jobhunter.llm.mock import MockProvider
    from jobhunter.services.resume_svc import ResumeContent

    settings = get_settings()
    resume = ResumeContent(skills=["Python"], raw_text="（fixture）")
    match_svc = __import__("jobhunter.services.match_svc", fromlist=["match_svc"])
    candidate = match_svc.ResumeCandidate(
        resume_id=1,
        name="主简历",
        direction="AI Agent",
        content=resume,
        content_hash="x" * 64,
        pool={"Python"},
        explicit={"Python"},
    )
    job = Job(
        platform="boss", title="AI Agent", city="深圳", jd_text="要求 Python", jd_hash="h" * 64
    )

    fixed = (
        "您好，我是 AI Agent 方向的应届生。看到岗位要求 Python，我在智能问答助手里实际用过，"
        "做的是检索增强问答的完整链路，也上线跑过一段时间。"
        "方便请教一下这个岗位目前最看重哪一块？"
    )
    assert settings.greeting.full_min_chars <= len(fixed) <= settings.greeting.full_max_chars, (
        f"测试自身问题：修复后的文本长度 {len(fixed)} 不在声明区间内"
    )
    # ★ 修复调用的 `task` 也是 `"greeting"`（`build_repair_payload`），不是单独的任务名 ——
    #   所以这里的 mock 响应必须挂在 `greeting` 上。本用例直接调 `_settle_versions`，
    #   因此 provider 只会被调用这一次（= 修复调用）。
    provider = MockProvider(
        responses={"greeting": json.dumps({"full": fixed, "short": fixed[:40]}, ensure_ascii=False)}
    )

    # ★ 这条文本**只能**有字数问题：含关键词、结尾是问句、无禁用词、无编造。
    #   否则闸门会额外报"结尾没有问句"，`is_length_only_failure` 就会（正确地）判 False，
    #   修复调用不会发生 —— 我第一版就是这么写的，测试打脸后才明白这条约束的意义。
    too_long = json.dumps(
        {
            "full": "看到岗位要求 Python。" + "补" * 200 + "方便聊聊吗？",
            "short": "熟悉 Python，方便聊聊吗？",
        },
        ensure_ascii=False,
    )
    parsed = greeting_svc.parse_greeting(too_long)
    result = greeting_svc._settle_versions(
        parsed,
        job=job,
        candidate=candidate,
        keywords=["Python"],
        variant="with_project",
        settings=settings,
        vocabulary=resume_svc.load_skill_vocabulary(settings),
        provider=provider,
        previous_message=LLMMessage(role="user", content="原始提示词"),
    )

    assert len(provider.calls) == 1, (
        f"纯字数失败没有发起修复调用（provider 调用 {len(provider.calls)} 次）——"
        "那就会直接降级模板，钱白花"
    )
    assert "full" in result.repaired, f"修复后的完整版没被采纳：{result.repaired}"
    assert len(result.full) <= settings.greeting.full_max_chars

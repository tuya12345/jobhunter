"""【P6 专项】完整跟进体系：时间轴 / 面试 / 问题 / 笔试 / Offer / 统计 / 周报 / .ics。

设计文档【提示词 8】的验收路径：
    「投递→批量打标→记面试→记 3 题(2 弱)→`jh weak` 聚合正确→加笔试→
      `jh offer compare`→`jh stats` 转化率正确→导出报告/ics」

这一组盯三类最容易写错的地方（也都是开发会话自己在 verify_hints 里点名的）：
  ① **统计口径**：「走到过某一步」而非「当前停在哪一步」；
  ② **状态机 / 事件流**：终态可从任意状态发生、重复打标不写重复事件；
  ③ **导出格式**：`.ics` 我只做「能解析 + 合规」的**独立**校验（开发会话已声明没有真导入日历）。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from typer.testing import CliRunner

from jobhunter.cli.app import app
from jobhunter.llm.mock import MockProvider
from jobhunter.models import InterviewQuestion, Job
from jobhunter.services import (
    apply_svc,
    cal_svc,
    followup_svc,
    offer_svc,
    question_svc,
    stats_svc,
    track_svc,
    weak_svc,
)
from jobhunter.storage.migrate import current_revision, downgrade_base, upgrade_head
from tests.verify._helpers import build_detail, insert_job, insert_resume, score_job

runner = CliRunner()
NOW = datetime(2026, 9, 19, 4, 0, tzinfo=UTC)  # 北京 2026-09-19 12:00


def _seed(session, settings, *, count: int = 2):  # noqa: ANN001, ANN202
    insert_resume(session)
    jobs = []
    for index in range(count):
        job = insert_job(
            session,
            build_detail(
                company=f"六期公司{index}",
                title=f"AI Agent 开发工程师 {index}",
                platform_job_id=f"p6-{index}",
                url=f"https://www.zhipin.com/job_detail/p6-{index}.html",
            ),
        )
        score_job(session, job, settings)
        jobs.append(job)
    session.flush()
    return jobs


def _events(session, job_id: int, event_type: str | None = None) -> int:
    from jobhunter.models import Application

    application = session.scalars(select(Application).where(Application.job_id == job_id)).first()
    if application is None:
        return 0
    sql = "SELECT COUNT(*) FROM application_events WHERE application_id = :aid"
    params: dict[str, object] = {"aid": application.id}
    if event_type:
        sql += " AND event_type = :et"
        params["et"] = event_type
    return session.execute(text(sql), params).scalar_one()


# ===========================================================================
# ① 统计口径：「走到过某一步」而不是「当前停在哪一步」
# ===========================================================================
def test_stats_count_reached_stages_not_current_stage(session, settings) -> None:
    """★★ 依据：交付声明「统计按『至少走到过这一步』（当前状态 ∪ 事件流出现过的状态），
    不是『当前停在哪一步』—— 后者会把『走到过二面、现在被拒』的投递静默抹掉」。

    构造：投 4 条 —— 一条走到 interview 后被 rejected、一条 viewed、一条 applied、一条 pending。
    断言 rejected 那条**仍然**被算进 viewed / replied / interview 的分子。
    若按"当前状态"算，它会被从三个分子里同时抹掉 → 转化率静默低估。
    """
    jobs = _seed(session, settings, count=4)
    old = NOW - timedelta(days=10)

    # ① 一路走到面试，然后被拒（这是最容易写错的样本）
    for status in ("applied", "viewed", "replied", "interview", "rejected"):
        apply_svc.set_status(session, jobs[0].id, status, occurred_at=old)
        session.flush()
    # ② 只到 viewed
    for status in ("applied", "viewed"):
        apply_svc.set_status(session, jobs[1].id, status, occurred_at=old)
        session.flush()
    # ③ 只投出去
    apply_svc.set_status(session, jobs[2].id, "applied", occurred_at=old)
    # ④ pending（还没投，不该进任何分子）
    apply_svc.ensure_pending_application(session, jobs[3])
    session.flush()

    report = stats_svc.build_stats(session, now=NOW)
    counts = report.overall
    # ★ `total` 不含 pending —— 后者是"开场白已生成、还没投"，不是一次投递。
    #   把它算进分母会低估所有转化率（我第一版就是这么期望的，测试打脸后才确认语义是对的）。
    assert counts.total == 3, f"投递总数应只算真投出去的 3 条，实际 {counts.total}"
    assert counts.viewed == 2, f"已读应为 2（含那条被拒的），实际 {counts.viewed}"
    assert counts.replied == 1, f"回复应为 1（被拒那条走到过 replied），实际 {counts.replied}"
    assert counts.interview == 1, f"约面应为 1，实际 {counts.interview}"
    assert counts.rejected == 1


def test_stats_excludes_pending_from_the_denominator(session, settings) -> None:
    """★★ 依据：`StageCounts.total` 是各转化率的**分母**。

    `pending` 的含义是"开场白已生成、就差我点投递"（`ensure_pending_application` 的注释），
    它**不是**一次投递。把它算进分母会让所有转化率系统性偏低 —— 而且完全静默。
    """
    jobs = _seed(session, settings, count=2)
    apply_svc.ensure_pending_application(session, jobs[0])
    apply_svc.ensure_pending_application(session, jobs[1])
    session.flush()

    report = stats_svc.build_stats(session, now=NOW)
    assert report.overall.total == 0, (
        f"两条都只是 pending（还没投），分母却是 {report.overall.total}"
    )


def test_stats_reports_numerator_and_denominator(session, settings) -> None:
    """依据：交付声明「每个比率带分子/分母」+【提示词 8】「**只记录对比，不宣称统计显著性**
    —— 样本太小」。没有分母的比率在样本 <10 时会被误读。
    """
    jobs = _seed(session, settings, count=2)
    apply_svc.set_status(session, jobs[0].id, "applied", occurred_at=NOW - timedelta(days=3))
    session.flush()

    report = stats_svc.build_stats(session, now=NOW)
    rendered = stats_svc.format_rate(0.5, 2)
    assert "2" in rendered, f"比率没有带分母：{rendered!r}"
    assert report.overall.total >= 1


# ===========================================================================
# ② 状态机与事件流
# ===========================================================================
@pytest.mark.parametrize("terminal", ["rejected", "offer"])
def test_terminal_status_can_be_reached_from_any_stage(session, settings, terminal: str) -> None:
    """依据：设计文档 §4 状态机「**rejected 可从任意状态发生**；offer 可从 interview 直接跳」。"""
    jobs = _seed(session, settings, count=1)
    apply_svc.set_status(session, jobs[0].id, "applied", occurred_at=NOW - timedelta(days=5))
    session.flush()
    result = apply_svc.set_status(session, jobs[0].id, terminal, occurred_at=NOW)
    session.flush()
    assert result.to_status == terminal and result.changed


def test_offer_jumps_directly_from_interview(session, settings) -> None:
    """依据：同上「offer 可从 interview 直接跳」（不必经过 assessment）。"""
    jobs = _seed(session, settings, count=1)
    for status in ("applied", "interview"):
        apply_svc.set_status(session, jobs[0].id, status, occurred_at=NOW - timedelta(days=2))
        session.flush()
    result = apply_svc.set_status(session, jobs[0].id, "offer", occurred_at=NOW)
    session.flush()
    assert result.from_status == "interview" and result.to_status == "offer"


def test_repeat_marking_writes_no_duplicate_event(session, settings) -> None:
    """★★ 依据：交付声明 verify_hints ②「重复打标**不写重复事件**（数事件条数）」。

    事件流是 append-only 的**事实记录**；重复写同一条会让时间轴出现
    "改了两次状态"的假象，也会让统计口径依赖去重。
    """
    jobs = _seed(session, settings, count=1)
    apply_svc.set_status(session, jobs[0].id, "applied", occurred_at=NOW - timedelta(days=1))
    session.flush()
    before = _events(session, jobs[0].id, "status_change")

    for _ in range(3):
        apply_svc.set_status(session, jobs[0].id, "applied", occurred_at=NOW)
        session.flush()

    after = _events(session, jobs[0].id, "status_change")
    assert after == before, f"重复打标写了重复事件：{before} → {after}"


# ===========================================================================
# ③ 时间轴：按发生时间排序，不是录入顺序
# ===========================================================================
def test_timeline_sorts_by_occurred_at_not_creation_order(session, settings) -> None:
    """★★ 依据：交付声明「时间轴按 `occurred_at`（**发生时间**）而不是 `created_at`」。

    对抗：**反向录入** —— 先记 viewed（今天），再补记更早的 applied（10 天前）。
    如果按录入顺序（created_at / 主键）排，时间轴会把"投递"显示在"已读"之后，
    整条时间线读起来是错的。
    """
    jobs = _seed(session, settings, count=1)
    apply_svc.set_status(session, jobs[0].id, "viewed", occurred_at=NOW)  # 先录晚的
    session.flush()
    apply_svc.set_status(session, jobs[0].id, "applied", occurred_at=NOW - timedelta(days=10))
    session.flush()

    timeline = track_svc.build_timeline(session, jobs[0].id, now=NOW)
    times = [entry.occurred_at for entry in timeline.entries]
    assert times == sorted(times), f"时间轴没有按发生时间排序：{times}"
    assert len(timeline.entries) >= 2, f"只画出了 {len(timeline.entries)} 条事件"


def test_timeline_raises_for_missing_job(session) -> None:
    """边界：岗位不存在 → `LookupError`（CLI 翻成退出码 1，不给堆栈）。"""
    with pytest.raises(LookupError):
        track_svc.build_timeline(session, 999999)


# ===========================================================================
# ④ 迁移 0002：可空 interview_id + job_id
# ===========================================================================
def test_migration_0002_is_reversible(db_url) -> None:
    """★ 依据：交付声明 verify_hints ④「upgrade→downgrade→**upgrade 三连**」+
    「downgrade 会丢预测题（有意），真实题不动」。

    这条验证的是迁移脚本本身（真实上线会执行的东西），不是 ORM 定义。
    """
    upgrade_head(db_url)
    head_before = current_revision(
        __import__("jobhunter.storage.db", fromlist=["x"]).create_db_engine(
            db_url, ensure_directory=False
        )
    )

    downgrade_base(db_url)
    upgrade_head(db_url)

    from jobhunter.storage.db import create_db_engine

    engine = create_db_engine(db_url, ensure_directory=False)
    try:
        assert current_revision(engine) == head_before, "三连之后没回到同一个 revision"
        columns = {
            col["name"]: col
            for col in __import__("sqlalchemy").inspect(engine).get_columns("interview_questions")
        }
        assert "job_id" in columns, "0002 没有加 job_id 列"
        assert columns["interview_id"]["nullable"] is True, (
            "interview_id 仍是 NOT NULL —— 那 `jh question predict <job_id>` 就没法存预测题"
        )
    finally:
        engine.dispose()


def _predict(session, job, *, settings):  # noqa: ANN001, ANN202
    """走真实预测路径写一批预测题（`source="predicted"`，挂在岗位上）。"""
    import json as _json

    provider = MockProvider(
        responses={
            "question_predict": _json.dumps(
                {
                    "questions": [
                        {
                            "question": "请介绍一下你做过的检索增强项目",
                            "category": "项目经历",
                            "why": "JD 要求 RAG 经验",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        }
    )
    return question_svc.predict_questions(
        session, job, count=1, settings=settings, provider=provider, use_cache=False
    )


def test_predicted_question_stores_without_an_interview(session, settings) -> None:
    """依据：迁移 0002 的理由「`jh question predict <job_id>` 拿的是岗位，
    而准备面试通常发生在面试还没约上时；坚持 NOT NULL 只能编一个假面试时间」。

    断言：预测题在 `interview_id IS NULL` 下能落库，且 `job_id` 记上了。
    """
    jobs = _seed(session, settings, count=1)
    result = _predict(session, jobs[0], settings=settings)
    session.flush()

    assert result.stored, f"预测题没有落库（skipped={result.skipped}）"
    row = result.stored[0]
    assert row.interview_id is None
    assert row.job_id == jobs[0].id
    assert row.source == "predicted"


def test_deleting_a_job_keeps_the_question_with_null_job_id(session, settings) -> None:
    """依据：`job_id` 的 `ondelete="SET NULL"` —— 删岗位不该把面试题一起删掉
    （复盘价值在题本身）。

    这条同时是对 `foreign_keys=ON` 的又一次实测（SQLite 默认不强制外键）。
    """
    jobs = _seed(session, settings, count=1)
    result = _predict(session, jobs[0], settings=settings)
    session.flush()
    question_id = result.stored[0].id

    session.delete(jobs[0])
    session.flush()
    # ★ 必须 expire：`ON DELETE SET NULL` 是**数据库**完成的，而 ORM 的身份映射里
    #   那个对象仍带着旧值 —— 不 expire 就会读到"库里已置空、内存说还有"的假象
    #   （我第一版差点把这个假象当 bug 报上去）。
    session.expire_all()

    kept = session.get(InterviewQuestion, question_id)
    assert kept is not None, "删岗位把面试题一起删了"
    assert kept.job_id is None, "job_id 没有变成 NULL（SET NULL 没生效）"


# ===========================================================================
# ⑤ `jh weak`：按"没答上来次数"聚合
# ===========================================================================
def test_weak_ranks_by_miss_count(session, settings) -> None:
    """★★ 依据：【提示词 8】「`jh weak` → 按 category 聚合、**按"没答上来次数"降序**」。

    对抗：让"前端性能"在同一知识点上错 3 次、另一个知识点错 1 次 →
    前者必须排第一（按平均分算的话，一次错分可能被一次满分稀释掉）。
    """
    jobs = _seed(session, settings, count=1)
    interview = __import__("jobhunter.services.interview_svc", fromlist=["x"]).add_interview(
        session, jobs[0].id, round_="一面", scheduled_at=NOW - timedelta(days=2)
    )
    session.flush()

    for index in range(3):
        question_svc.add_question(
            session,
            question=f"前端性能问题 {index}",
            interview_id=interview.id,
            category="前端性能",
            rating="bad",
            weak=True,
        )
    question_svc.add_question(
        session,
        question="Redis 缓存穿透怎么处理",
        interview_id=interview.id,
        category="缓存",
        rating="bad",
        weak=True,
    )
    session.flush()

    report = weak_svc.build_weak_report(session)
    assert report.rows, "weak 一条都没聚合出来"
    assert report.rows[0].category == "前端性能", (
        f"没答上来 3 次的知识点没排第一：{[row.category for row in report.rows]}"
    )
    assert report.rows[0].weak_count == 3


def test_weak_separates_recorded_from_predicted(session, settings) -> None:
    """依据：交付声明「实录/预测**分列**」—— 预测题从没被真问过，
    混在一起算"薄弱点"会把"可能会问"当成"答不上来"。
    """
    jobs = _seed(session, settings, count=1)
    _predict(session, jobs[0], settings=settings)
    interview = __import__("jobhunter.services.interview_svc", fromlist=["x"]).add_interview(
        session, jobs[0].id, round_="一面", scheduled_at=NOW - timedelta(days=2)
    )
    session.flush()
    question_svc.add_question(
        session,
        question="实录题",
        interview_id=interview.id,
        category="实录类",
        rating="bad",
        weak=True,
    )
    session.flush()

    report = weak_svc.build_weak_report(session)
    assert report.recorded_count == 1, f"实录题数不对：{report.recorded_count}"
    assert report.predicted_count == 1, f"预测题数不对：{report.predicted_count}"
    categories = {row.category for row in report.rows}
    assert "实录类" in categories


def test_weak_surfaces_uncategorized_questions(session, settings) -> None:
    """★★ 依据：交付声明「**没分类的题要被提示出来**（分类是唯一聚合维度，
    静默忽略 = 数据消失）」。

    这是"静默失真"的典型：用户记了题，但因为没填 category，它在薄弱点里
    完全不出现 —— 用户会以为"我没记过错题"。
    """
    jobs = _seed(session, settings, count=1)
    interview = __import__("jobhunter.services.interview_svc", fromlist=["x"]).add_interview(
        session, jobs[0].id, round_="一面", scheduled_at=NOW - timedelta(days=2)
    )
    session.flush()
    question_svc.add_question(
        session, question="没分类的题", interview_id=interview.id, rating="bad", weak=True
    )
    session.flush()

    report = weak_svc.build_weak_report(session)
    assert report.uncategorized >= 1, (
        "没分类的错题被静默忽略了 —— 用户会以为'我没记过错题'\n"
        f"（uncategorized={report.uncategorized}，rows={[r.category for r in report.rows]}）"
    )


# ===========================================================================
# ⑥ 追问话术：只生成不发送 + 编造降级
# ===========================================================================
def test_followup_falls_back_on_fabrication(session, settings) -> None:
    """★★ 依据：设计文档 §七.3（AI 保真由代码强制）+ 交付声明 verify_hints ⑥
    「喂『精通 Kubernetes / 提升 300%』必须降级模板并说明原因」。

    追问话术同样是要发给 HR 的 —— 保真闸门对它一样成立。
    """
    jobs = _seed(session, settings, count=1)
    apply_svc.set_status(session, jobs[0].id, "applied", occurred_at=NOW - timedelta(days=5))
    session.flush()

    provider = MockProvider(
        responses={"followup": "您好，我精通 Kubernetes，曾把接口性能提升 300%，想了解一下进度。"}
    )
    result = followup_svc.generate_followup(
        session, jobs[0], settings=settings, provider=provider, use_cache=False
    )

    assert result.source == "template", f"编造的话术被当成模型版本放行了：{result.text!r}"
    assert result.issues, "降级了却没有说明原因（静默降级）"
    assert "Kubernetes" not in result.text or "300" not in result.text, (
        f"降级后的文本里仍然带着编造内容：{result.text!r}"
    )


def test_followup_does_not_write_an_event_before_being_sent(session, settings) -> None:
    """★★ 依据：交付声明「**只生成，不发送**」+ verify_hints ⑥
    「`--sent` 之前不得写 followup 事件（生成 ≠ 发送）」。

    这是合规红线的一部分：工具可以写话术，但"发出去了"这个事实只能由人确认。
    未发送就写事件 = 工具替用户声称了一件没发生的事。
    """
    jobs = _seed(session, settings, count=1)
    apply_svc.set_status(session, jobs[0].id, "applied", occurred_at=NOW - timedelta(days=5))
    session.flush()

    followup_svc.generate_followup(session, jobs[0], settings=settings, use_cache=False)
    session.flush()

    assert _events(session, jobs[0].id, "followup") == 0, (
        "只是生成了话术就写了 followup 事件 —— 生成 ≠ 发送"
    )


# ===========================================================================
# ⑦ `.ics`：我自己做"能解析 + 合规"的独立校验
# ===========================================================================
def _ics_lines(text_content: str) -> list[str]:
    """按 RFC 5545 展开折行（续行以单个空格开头）。"""
    unfolded: list[str] = []
    for raw in text_content.split("\r\n"):
        if raw.startswith(" ") and unfolded:
            unfolded[-1] += raw[1:]
        else:
            unfolded.append(raw)
    return unfolded


def test_ics_is_rfc_shaped_and_foldable(session, settings) -> None:
    """★★ 依据：交付声明 verify_hints ⑦「`.ics` 建议真导入一次日历（我只做了文本断言）」
    + `cal_svc` docstring「每行以 CRLF 结尾，含最后一行」。

    ★ 我**不能**真导入日历软件（那需要人），但可以把"文本断言"换成
      **合规性断言** —— 这比"包含某个字符串"强得多，而且正是导入失败的真实原因：
        ① 全部行以 CRLF 结尾（含最后一行）；
        ② **每行不超过 75 个八位组**（RFC 5545 折行规则，按**字节**算；
           中文一行很容易超 —— 这正是手写 ics 最常踩的坑）；
        ③ 骨架齐全：BEGIN/END:VCALENDAR、VERSION:2.0、PRODID；
        ④ 每个 VEVENT 有 UID/DTSTAMP/DTSTART/DTEND/SUMMARY；
        ⑤ DTSTART/DTEND 是 UTC 的 `Z` 形式（否则跨时区会错时间）。
    """
    jobs = _seed(session, settings, count=1)
    interview = __import__("jobhunter.services.interview_svc", fromlist=["x"]).add_interview(
        session,
        jobs[0].id,
        round_="一面",
        scheduled_at=NOW + timedelta(days=2),
        mode="视频",
        link="https://example.com/meet/abc",
    )
    session.flush()

    export = cal_svc.export_ics(session, settings=settings, now=NOW)
    raw = export.path.read_bytes()
    content = raw.decode("utf-8")

    # ① CRLF（含最后一行）
    assert b"\n" in raw and raw.replace(b"\r\n", b"").find(b"\n") == -1, "存在不是 CRLF 的换行"
    assert content.endswith("\r\n"), "最后一行没有以 CRLF 结尾"

    # ② 折行：每行 ≤ 75 字节
    too_long = [
        (index, line) for index, line in enumerate(content.split("\r\n")) if len(line.encode()) > 75
    ]
    assert not too_long, f"有行超过 75 字节（导入日历会失败/乱码）：{too_long[:3]}"

    # ③ 骨架
    lines = _ics_lines(content)
    assert lines[0] == "BEGIN:VCALENDAR"
    assert any(line.startswith("VERSION:2.0") for line in lines)
    assert any(line.startswith("PRODID") for line in lines)
    assert "END:VCALENDAR" in lines

    # ④ / ⑤ 每个事件
    starts = [line for line in lines if line.startswith("DTSTART")]
    ends = [line for line in lines if line.startswith("DTEND")]
    assert starts and ends, f"一个事件都没导出（interview id={interview.id}）"
    for line in starts + ends:
        assert re.search(r"DT(?:START|END):\d{8}T\d{6}Z$", line), f"时间不是 UTC Z 形式：{line}"
    for required in ("UID", "DTSTAMP", "SUMMARY"):
        assert any(line.startswith(required) for line in lines), f"缺少 {required}"


def test_ics_uid_is_stable_across_exports(session, settings) -> None:
    """依据：`event_uid(kind, source_id)` 的设计意图 —— UID 稳定才能让日历
    **更新**已有事件而不是新增重复条目。
    """
    jobs = _seed(session, settings, count=1)
    __import__("jobhunter.services.interview_svc", fromlist=["x"]).add_interview(
        session, jobs[0].id, round_="一面", scheduled_at=NOW + timedelta(days=2)
    )
    session.flush()

    first = cal_svc.export_ics(session, settings=settings, now=NOW)
    second = cal_svc.export_ics(session, settings=settings, now=NOW)

    def uids(export: object) -> list[str]:
        return [cal_svc.event_uid(event.kind, event.source_id) for event in export.events]  # type: ignore[attr-defined]

    assert uids(first) == uids(second), "两次导出的 UID 不一致 —— 日历里会出现重复事件"
    assert len(set(uids(first))) == len(uids(first)), "UID 之间有重复"


def test_ics_with_no_events_is_still_a_valid_calendar(session, settings) -> None:
    """依据：`render_ics` docstring「事件为空时同样返回合法的空日历 ——
    '今天没安排'不应该是一个需要用户处理的错误」。"""
    _seed(session, settings, count=1)
    session.flush()
    export = cal_svc.export_ics(session, settings=settings, now=NOW)

    content = export.path.read_text(encoding="utf-8")
    assert "BEGIN:VCALENDAR" in content and "END:VCALENDAR" in content
    assert export.event_count == 0


def test_ics_escapes_special_characters() -> None:
    """依据：RFC 5545 的 TEXT 转义规则（`,` `;` `\\` 与换行）。
    公司名里带逗号在中文场景很常见（"某某科技（深圳）有限公司, 南山"）。
    """
    escaped = cal_svc.escape_text("A, B; C\\D\nE")
    assert "," not in escaped.replace("\\,", ""), f"逗号没转义：{escaped!r}"
    assert ";" not in escaped.replace("\\;", ""), f"分号没转义：{escaped!r}"
    assert "\n" not in escaped, f"换行没转义（会截断 ics 行）：{escaped!r}"


# ===========================================================================
# ⑧ `jh offer compare`：年包口径
# ===========================================================================
def test_offer_compare_shows_annual_package_difference(session, settings) -> None:
    """★★ 依据：交付声明 verify_hints ⑧「`jh offer compare` 的年包：15 薪 vs 13 薪必须能看出差距」。

    这是"offer 比较"最容易骗人的地方：两个 offer 月薪一样、月数不同，
    只看月薪会得出"一样好"的结论。年包 = 月薪 × 月数。
    """
    jobs = _seed(session, settings, count=2)
    offer_svc.add_offer(session, jobs[0].id, salary_month=20000, months=15, location="深圳")
    offer_svc.add_offer(session, jobs[1].id, salary_month=20000, months=13, location="深圳")
    session.flush()

    rows = offer_svc.list_offers(session, now=NOW)
    compared = offer_svc.compare_rows(rows)
    assert len(compared) == 2

    annuals = [item.get("annual") for item in compared]
    assert None not in annuals, f"年包没算出来：{compared}"
    assert sorted(annuals, reverse=True) == [300000, 260000], f"年包算错了：{annuals}"


def test_offer_compare_does_not_mark_a_tie_as_best(session, settings) -> None:
    """依据：交付声明 verify_hints ⑧「并列最大**不标最优**」——
    两个并列第一时标一个"最优"是误导。"""
    jobs = _seed(session, settings, count=2)
    offer_svc.add_offer(session, jobs[0].id, salary_month=20000, months=15)
    offer_svc.add_offer(session, jobs[1].id, salary_month=20000, months=15)
    session.flush()

    compared = offer_svc.compare_rows(offer_svc.list_offers(session, now=NOW))
    best = [item for item in compared if item.get("best")]
    assert len(best) != 1, f"并列最大却标了唯一最优：{[i.get('annual') for i in compared]}"


@pytest.mark.parametrize(
    "argv", [["stats"], ["weak"], ["offer", "compare"], ["report"], ["cal", "export"]]
)
def test_p6_commands_never_traceback(argv: list[str], engine) -> None:  # noqa: ARG001
    """对抗：P6 的只读命令在空库上不该抛堆栈（CI 里就是空库跑的）。"""
    result = runner.invoke(app, argv)
    combined = result.output + str(result.exception or "")
    assert "Traceback" not in combined, f"{' '.join(argv)} 抛了堆栈：\n{combined}"
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        pytest.fail(f"{' '.join(argv)} 抛了未处理的异常：{result.exception!r}")


def test_stats_ignores_jobs_without_applications(session, settings) -> None:
    """边界：库里有岗位但没有任何投递 → 统计不该崩，也不该把"岗位"当"投递"计数。"""
    _seed(session, settings, count=3)
    session.flush()
    report = stats_svc.build_stats(session, now=NOW)
    assert report.overall.total == 0, f"没有任何投递，total 却是 {report.overall.total}"
    assert isinstance(session.get(Job, 1), Job)

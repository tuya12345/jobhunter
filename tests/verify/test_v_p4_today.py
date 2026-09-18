"""【P4 专项】待投清单 / 投递打标 / 导出 / 序号快照 / 日额度。

P4 的承诺（设计文档 §5.1.4 + 评审采纳项 #1）：
    "按 match_score 降序、排除已投递、同公司不推多条" +
    "零手打跟进：让我什么都不想敲的时候工具依然有价值" +
    "导出只写新文件"。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from typer.testing import CliRunner

from jobhunter.cli.app import app
from jobhunter.llm.mock import MockProvider
from jobhunter.models import Application, Job
from jobhunter.services import apply_svc, export_svc, today_svc
from jobhunter.storage.db import create_db_engine, make_session_factory
from jobhunter.utils.time import DISPLAY_TZ
from tests.verify._helpers import build_detail, insert_job, insert_resume, score_job

runner = CliRunner()


def _seed(session, settings, *, count: int = 3, score_start: int = 90):  # noqa: ANN001, ANN202
    """造 `count` 个不同公司的岗位，并给它们**递减**的 match_score（便于断言排序）。"""
    insert_resume(session)
    jobs = []
    for index in range(count):
        job = insert_job(
            session,
            build_detail(
                company=f"虚构公司{index}",
                title=f"AI Agent 开发工程师 {index}",
                platform_job_id=f"seed{index}",
                url=f"https://www.zhipin.com/job_detail/seed{index}.html",
            ),
        )
        score_job(session, job, settings)
        job.match_score = score_start - index * 5  # 90 / 85 / 80 …
        jobs.append(job)
    session.flush()
    return jobs


# ===========================================================================
# ① 清单选取：排除 / 排序 / 同公司去重 / limit / min-score
# ===========================================================================
def test_selection_is_sorted_by_score_desc(session, settings) -> None:
    """依据：设计文档 §5.1.4「按 match_score 降序」。"""
    _seed(session, settings, count=4)
    jobs, total, _dropped = today_svc.select_candidates(session, settings=settings)

    scores = [job.match_score for job in jobs]
    assert scores == sorted(scores, reverse=True), f"没有按分数降序：{scores}"
    assert total == 4


def test_selection_excludes_applied_and_skipped(session, settings) -> None:
    """依据：设计文档 §5.1.4「排除已投递」+ `job.status` 的 new→…→applied/skipped 状态机。

    两条路径都要测：① 岗位 status 被改；② 有非 pending 的投递记录（状态列可能不同步）。
    """
    jobs = _seed(session, settings, count=3)
    session.flush()

    # ① 岗位状态
    jobs[0].status = "skipped"
    # ② 投递记录（岗位状态仍是 scored，模拟"两处不同步"）
    apply_svc.set_status(session, jobs[1].id, "applied")
    session.flush()

    selected, _total, _dropped = today_svc.select_candidates(session, settings=settings)
    ids = {job.id for job in selected}
    assert jobs[0].id not in ids, "已 skipped 的岗位仍被推荐"
    assert jobs[1].id not in ids, "已有投递记录的岗位仍被推荐"
    assert jobs[2].id in ids


def test_same_company_keeps_only_the_highest_score(session, settings) -> None:
    """依据：设计文档 §5.1.4「**同公司去重**：同一天不推同公司多条
    （评审指出校招里是减分项）」+ `select_candidates` 注释「保留分数最高的那一条」。"""
    insert_resume(session)
    same_company = []
    for index, score in enumerate((70, 95, 88)):
        job = insert_job(
            session,
            build_detail(
                company="同一家虚构公司",
                title=f"岗位 {index}",
                platform_job_id=f"same{index}",
                url=f"https://www.zhipin.com/job_detail/same{index}.html",
            ),
        )
        score_job(session, job, settings)
        job.match_score = score
        same_company.append(job)
    session.flush()

    selected, total, dropped = today_svc.select_candidates(session, settings=settings)
    assert total == 3, "过滤前应看到 3 条"
    assert dropped == 2, f"应挤掉 2 条同公司，实际 {dropped}"
    assert len(selected) == 1
    assert selected[0].match_score == 95, f"保留的不是最高分那条：{selected[0].match_score}"


def test_unknown_company_jobs_are_not_collapsed_together(session, settings) -> None:
    """边界：抓取失败导致 `company` 为空时，不能把一堆"无名岗位"当成同一家公司挤掉。"""
    insert_resume(session)
    for index in range(3):
        job = insert_job(
            session,
            build_detail(
                company=None,
                title=f"岗位 {index}",
                platform_job_id=f"noname{index}",
                url=f"https://www.zhipin.com/job_detail/noname{index}.html",
            ),
        )
        score_job(session, job, settings)
    session.flush()

    selected, _total, dropped = today_svc.select_candidates(session, settings=settings)
    assert dropped == 0, "公司名为空的岗位被当成同一家公司挤掉了"
    assert len(selected) == 3


def test_limit_and_min_score_are_respected(session, settings) -> None:
    """依据：设计文档 §5.1.4「`jh today --limit 20 [--min-score 60]`」。"""
    _seed(session, settings, count=5, score_start=100)
    session.flush()

    limited, _total, _dropped = today_svc.select_candidates(session, limit=2, settings=settings)
    assert len(limited) == 2, f"--limit 2 却给了 {len(limited)} 条"

    high, _total, _dropped = today_svc.select_candidates(session, min_score=95, settings=settings)
    assert all(job.match_score >= 95 for job in high), "min_score 过滤没生效"
    assert len(high) < 5


def test_zero_limit_returns_nothing_without_crashing(session, settings) -> None:
    """边界：`--limit 0`。"""
    _seed(session, settings, count=2)
    session.flush()
    limited, _total, _dropped = today_svc.select_candidates(session, limit=0, settings=settings)
    assert limited == []


# ===========================================================================
# ② 事务边界：花了钱的开场白必须落盘（P3 #3 的同类问题）
# ===========================================================================
async def test_today_greetings_are_durable_without_the_cli_commit(
    session, settings, db_url
) -> None:
    """★★ 依据：P3 刚修过的 ISSUES #3 的**同一条纪律**
    （`match_llm_svc` docstring「每完成一个就立刻写库……中途 Ctrl+C 也不会丢掉已经算出来的
    （那些是真的花了钱的）」），以及开发会话自己在 STATUS §6 列出的验证点
    「**事务边界**：`jh today` 生成开场白（花钱）后是否落盘；中断会不会丢」。

    `build_today_list` 为整张清单调 N 次 LLM，但 `ensure_item` 只 `session.flush()`，
    唯一的 `commit()` 在 `today_cmd.py:100`（**全部生成完之后**）。
    → 生成到一半中断，前面已经花掉的调用全被回滚。

    验证方式与 #3 一致：跑完服务层后**不调用 CLI 的 commit**，用新连接去读。
    """
    jobs = _seed(session, settings, count=2)
    session.commit()  # 前面的铺垫先落盘

    provider = MockProvider()
    result = today_svc.build_today_list(session, limit=2, settings=settings, provider=provider)
    assert len(result.items) == 2, "清单条数不对，测试前提不成立"

    other_engine = create_db_engine(db_url, ensure_directory=False)
    try:
        with make_session_factory(other_engine)() as other:
            rows = other.execute(text("SELECT COUNT(*) FROM applications")).scalar_one()
            greetings = other.execute(
                text("SELECT COUNT(*) FROM applications WHERE greeting_text IS NOT NULL")
            ).scalar_one()
            fresh = other.get(Job, jobs[0].id)
            assert fresh is not None
            status = fresh.status
    finally:
        other_engine.dispose()

    assert rows >= 2, "清单生成的投递记录没有落盘（只 flush 没 commit），中断即全丢"
    assert greetings >= 1, "花了钱生成的开场白没有落盘，中断即全丢"
    assert status is not None


# ===========================================================================
# ③ 序号快照：投掉一条之后序号不能漂移（开发会话自称的"最致命误操作"防护）
# ===========================================================================
def test_snapshot_keeps_index_stable_after_applying_one(session, settings) -> None:
    """★★ 依据：`resolve_index` docstring「投完第 1 条 → 清单往前挪一位 →
    '第 3 条'已经不是刚才看到的第 3 条 —— 这正是'最致命的误操作'」。

    这条**必须动态复现原场景**：先出清单 → 投掉第 1 条 → 再解析序号 3。
    """
    jobs = _seed(session, settings, count=4)
    session.flush()

    result = today_svc.build_today_list(session, limit=4, settings=settings)
    assert len(result.items) == 4
    original_third = result.items[2].job.id

    # 投掉第 1 条（走真实服务层）
    apply_svc.set_status(session, result.items[0].job.id, "applied")
    session.flush()

    resolved, from_snapshot = today_svc.resolve_index(session, 3, settings=settings)
    assert from_snapshot is True, "没有走今天的快照"
    assert resolved is not None
    assert resolved.id == original_third, (
        f"序号漂移了：清单里的第 3 条是 job {original_third}，apply 3 现在指向 {resolved.id}"
        "（这正是'我以为标的是第 3 条，实际改的是别的岗位'）"
    )
    assert jobs  # 保持变量被使用


def test_snapshot_is_ignored_on_another_day(session, settings) -> None:
    """依据：`load_snapshot` docstring「**不是今天的**一律当没有（跨天不该复用昨天的序号）」。"""
    _seed(session, settings, count=2)
    session.flush()
    today_svc.build_today_list(session, limit=2, settings=settings)

    path = today_svc.snapshot_path(settings)
    assert path.is_file(), "快照文件没写出来"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["date"] = "2000-01-01"  # 伪造一份"昨天的"
    from jobhunter.utils.io import write_json

    write_json(path, payload)

    assert today_svc.load_snapshot(settings) is None, "昨天的快照被当成今天的用了"


def test_out_of_range_index_does_not_crash(session, settings) -> None:
    """边界：快照存在但序号越界 / 快照指向的岗位已被删除。

    期望：返回 (None, True) 让 CLI 给出可读提示，**不是抛异常或指向错岗位**。
    """
    _seed(session, settings, count=2)
    session.flush()
    today_svc.build_today_list(session, limit=2, settings=settings)

    job, from_snapshot = today_svc.resolve_index(session, 99, settings=settings)
    assert job is None and from_snapshot is True

    job, from_snapshot = today_svc.resolve_index(session, 0, settings=settings)
    assert job is None

    job, from_snapshot = today_svc.resolve_index(session, -1, settings=settings)
    assert job is None, "负数序号居然解析出了一个岗位"


def test_index_falls_back_to_live_query_without_snapshot(session, settings) -> None:
    """依据：`resolve_index` docstring「快照不存在（没跑过 `jh today`）才退回实时筛选」。"""
    _seed(session, settings, count=3)
    session.flush()
    assert today_svc.load_snapshot(settings) is None, "测试前提：不该有快照"

    job, from_snapshot = today_svc.resolve_index(session, 1, settings=settings)
    assert job is not None
    assert from_snapshot is False, "没有快照却声称来自快照"


# ===========================================================================
# ④ 投递打标
# ===========================================================================
def test_illegal_status_is_rejected(session, settings) -> None:
    """依据：设计文档 §5.2「`--status` 只接受 APPLICATION_STATUSES；
    写了不认识的状态要**报错拒绝**，而不是静默建一个野状态」。"""
    jobs = _seed(session, settings, count=1)
    session.flush()
    with pytest.raises(apply_svc.ApplicationError):
        apply_svc.set_status(session, jobs[0].id, "banana")


def test_reapplying_same_status_is_idempotent(session, settings) -> None:
    """依据：`ApplyResult.changed` 字段 + STATUS §5「第三条提示'本来就是 applied，没有改动'」。

    重复打标不能重复计数、也不该写两条重复事件。
    """
    jobs = _seed(session, settings, count=1)
    session.flush()

    first = apply_svc.set_status(session, jobs[0].id, "applied")
    session.flush()
    second = apply_svc.set_status(session, jobs[0].id, "applied")
    session.flush()

    assert first.changed is True
    assert second.changed is False, "重复打标被当成了状态变更"
    assert second.from_status == second.to_status

    events = session.execute(
        text("SELECT COUNT(*) FROM application_events WHERE event_type = 'status_change'")
    ).scalar_one()
    assert events == 1, f"重复打标写了 {events} 条状态变更事件（应为 1）"


def test_status_change_writes_an_event(session, settings) -> None:
    """依据：设计文档 §4「**所有状态变更都写 `application_events`**（可画投递时间轴、可归因）」。"""
    jobs = _seed(session, settings, count=1)
    session.flush()
    apply_svc.set_status(session, jobs[0].id, "applied")
    session.flush()

    row = session.execute(
        text("SELECT from_status, to_status FROM application_events ORDER BY id DESC LIMIT 1")
    ).one()
    assert row[1] == "applied", f"事件流里没有记录这次变更：{row}"


def test_applied_at_is_set_and_is_utc_aware(session, settings) -> None:
    """依据：设计文档 §4「applications(applied_at…)」+ §3.2 时间统一 UTC aware。"""
    jobs = _seed(session, settings, count=1)
    session.flush()
    apply_svc.set_status(session, jobs[0].id, "applied")
    session.flush()
    session.expire_all()

    application = session.scalars(
        select(Application).where(Application.job_id == jobs[0].id)
    ).first()
    assert application is not None
    assert application.applied_at is not None, "标成 applied 却没记 applied_at"
    assert application.applied_at.tzinfo is not None, "applied_at 丢了时区"


# ===========================================================================
# ⑤ 日额度：按**北京时间自然日**
# ===========================================================================
def test_daily_quota_uses_beijing_calendar_day(session, settings) -> None:
    """★★ 依据：`count_applied_today` docstring「按**北京时间自然日**算……
    UTC 会比北京晚 8 小时，晚上 8 点后投的会被算到'明天'，额度就白算了」。

    构造一个**跨天边界**：北京时间今天 07:00 = UTC 昨天 23:00。
    这条必须计入"今天" —— 用 UTC 日聚合的话会漏掉它。
    """
    jobs = _seed(session, settings, count=1)
    session.flush()

    local_now = datetime(2026, 9, 15, 7, 0, tzinfo=DISPLAY_TZ)
    occurred = local_now.astimezone(UTC)  # UTC 是 2026-09-14 23:00，北京是 09-15 07:00

    apply_svc.set_status(session, jobs[0].id, "applied", occurred_at=occurred)
    session.flush()

    counted = apply_svc.count_applied_today(session, now=datetime(2026, 9, 15, 12, 0, tzinfo=UTC))
    assert counted == 1, (
        f"北京时间 09-15 07:00 投的（UTC 09-14 23:00）没有被算进'今天'：counted={counted}"
    )

    # 反向：前一整天的不该算进来
    counted_next = apply_svc.count_applied_today(
        session, now=datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    )
    assert counted_next == 0, "昨天的投递被算进了今天"


def test_daily_quota_ignores_pending(session, settings) -> None:
    """依据：额度算的是"投出去几条"，pending（还没投）不该占额度。"""
    jobs = _seed(session, settings, count=2)
    session.flush()
    apply_svc.ensure_pending_application(session, jobs[0])
    session.flush()
    assert apply_svc.count_applied_today(session) == 0, "pending 被算进了已投额度"


# ===========================================================================
# ⑥ 导出：只写新文件 + 不含隐私
# ===========================================================================
def test_export_creates_a_new_file_and_never_overwrites(session, settings, isolated_env) -> None:  # noqa: ARG001
    """★★ 依据：设计文档 §七.6 + 评审采纳项 #10「导出可能原地改简历 →
    **必须写新文件**」（同一纪律适用于所有导出物）。

    验证方式：连续导出两次，**第二次的文件必须不同名**，
    且第一次的内容**必须原封不动**（不是"被覆盖成一样的"）。
    """
    _seed(session, settings, count=2)
    session.flush()
    result = today_svc.build_today_list(session, limit=2, settings=settings)
    output_dir = settings.paths.output_dir

    first = export_svc.export_markdown(result.items, output_dir=output_dir)
    first_text = first.read_text(encoding="utf-8")
    second = export_svc.export_markdown(result.items, output_dir=output_dir)

    assert first != second, "第二次导出覆盖了第一次的文件（应该换个名字）"
    assert first.read_text(encoding="utf-8") == first_text, "第一次导出的内容被改动了"
    assert first.is_file() and second.is_file()


def test_csv_export_is_excel_friendly_and_has_no_pii(session, settings) -> None:  # noqa: ARG001
    """依据：STATUS.md P4 §4.5「CSV 带 BOM 以便 Excel 打开中文不乱码」+
    §4.5「导出不含简历原文（隐私取舍）」。"""
    insert_resume(session)
    jobs = _seed(session, settings, count=2)
    result = today_svc.build_today_list(session, limit=2, settings=settings)

    path = export_svc.export_csv(result.items, output_dir=settings.paths.output_dir)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "CSV 没有 BOM，Excel 打开中文会乱码"

    text_content = raw.decode("utf-8-sig")
    assert "虚构公司0" in text_content, "导出内容里没有岗位"
    for secret in ("13800000000", "xiaoman.lin@example.com"):
        assert secret not in text_content, f"导出物里含简历隐私：{secret}"
    assert jobs  # 保持变量被使用


def test_export_requires_items_without_crashing(session, settings) -> None:  # noqa: ARG001
    """边界：清单为空时导出（不该崩、不该产生空文件块）。"""
    result = today_svc.build_today_list(session, limit=0, settings=settings)
    path = export_svc.export_markdown(result.items, output_dir=settings.paths.output_dir)
    assert path.is_file()
    assert path.read_text(encoding="utf-8").strip(), "空清单导出出了完全空白的文件"


# ===========================================================================
# ⑦ CLI 层：参数校验与语序
# ===========================================================================
@pytest.mark.parametrize(
    "argv",
    [
        ["today", "--variant", "banana"],
        ["today", "--export", "pdf"],
        ["today", "--copy", "999"],
        ["today", "--limit", "-3"],
        ["apply", "999", "--status", "applied"],
        ["apply", "1", "--status", "banana"],
    ],
)
def test_today_and_apply_cli_never_traceback(argv: list[str], engine) -> None:  # noqa: ARG001
    """对抗：超范围参数与不存在的序号。

    期望：可读错误 + 非 0 退出，绝不出现 Python 堆栈。
    """
    result = runner.invoke(app, argv)
    combined = result.output + str(result.exception or "")
    assert "Traceback" not in combined, f"{' '.join(argv)} 抛了堆栈：\n{combined}"
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        pytest.fail(f"{' '.join(argv)} 抛了未处理的异常：{result.exception!r}")


@pytest.mark.parametrize(
    "argv",
    [
        ["apply", "1", "--status", "applied"],
        ["apply", "--status", "applied", "1"],
    ],
)
def test_apply_accepts_both_argument_orders(argv: list[str], session, settings) -> None:  # noqa: ARG001
    """依据：STATUS.md P4 §4.7「Click 的可变位置参数会吞掉它后面的选项……
    已做最小修补（**任意语序都认**）」。

    两种语序都必须能把状态改成功 —— 否则用户在自然语序下会被静默忽略。
    """
    jobs = _seed(session, settings, count=1)
    session.flush()
    today_svc.build_today_list(session, limit=1, settings=settings)
    session.commit()

    result = runner.invoke(app, argv)
    assert result.exit_code == 0, f"{' '.join(argv)} 失败：\n{result.output}"

    session.expire_all()
    application = session.scalars(
        select(Application).where(Application.job_id == jobs[0].id)
    ).first()
    assert application is not None
    assert application.status == "applied", (
        f"语序 {' '.join(argv)} 下状态没有改成功（实际 {application.status}）"
    )

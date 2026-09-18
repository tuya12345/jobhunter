"""【P4 专项 · 续】清单/快照/打标的边界与"静默失真"。

这一组来自两个方向：
  ① 验证会话自己发现的（开场白注入、误判真话）；
  ② 对 P4 服务层做独立审计后**逐条动态复现**的场景。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from typer.testing import CliRunner

from jobhunter.cli.app import app
from jobhunter.models import Application
from jobhunter.services import apply_svc, today_svc
from jobhunter.utils.io import write_text
from tests.verify._helpers import insert_job, insert_resume, score_job  # noqa: F401
from tests.verify.test_v_p4_today import _seed

runner = CliRunner()


# ===========================================================================
# ① `--copy` 的筛选选项被静默忽略
# ===========================================================================
@pytest.mark.xfail(
    reason="ISSUE #34（🟡）：--copy N 静默忽略 --min-score / --limit",
    strict=False,
)
def test_copy_option_is_not_silently_ignored(session, settings) -> None:
    """🟡 依据：`--copy N`、`--min-score`、`--limit` 是同一条命令的选项，
    用户给 `--copy 2 --min-score 999` 时的合理预期是
    "在我限定的范围里取第 2 条"（此时范围为空 → 应当报错/提示无此条）。

    实测：`_copy_nth` 收了 `min_score` 形参却**从不使用**，于是照常复制第 2 条。
    静默忽略选项比报错更糟 —— 用户以为筛选生效了，拿到的是别的岗位的话术。
    """
    _seed(session, settings, count=3, score_start=100)
    session.flush()
    today_svc.build_today_list(session, limit=3, settings=settings)
    session.commit()

    result = runner.invoke(app, ["today", "--copy", "2", "--min-score", "999"])
    assert result.exit_code != 0 or "没有" in result.output, (
        "`--min-score 999` 下清单应为空，但 `--copy 2` 照常复制了内容 ——"
        f"筛选选项被静默忽略：\n{result.output}"
    )


def test_check_option_does_not_need_a_list(session, settings) -> None:  # noqa: ARG001
    """对照（应当通过）：`--check` 只报额度、不生成任何东西（`today_cmd.py:73-76`）。"""
    _seed(session, settings, count=2)
    session.flush()
    result = runner.invoke(app, ["today", "--check"])
    assert result.exit_code == 0, f"--check 失败：\n{result.output}"


# ===========================================================================
# ② 序号快照的语义与稳健性
# ===========================================================================
def test_snapshot_is_not_shrunk_by_limit(session, settings) -> None:
    """🔴 依据：`resolve_index` docstring 承诺
    「存了快照之后，序号在**当天内**始终指向同一批岗位」。

    但 `build_today_list` 每次都用**本轮（可能被 `--limit` 截断的）结果**覆盖快照
    （`today_svc.py:253`）。后果：`today --limit 3` 之后 `jh apply 5` 找不到，
    而库里明明有 5 个达线岗位 —— 序号语义被 `--limit` 悄悄改掉了。
    """
    _seed(session, settings, count=5, score_start=100)
    session.flush()

    today_svc.build_today_list(session, limit=3, settings=settings)
    session.flush()

    snapshot = today_svc.load_snapshot(settings)
    assert snapshot is not None
    assert len(snapshot) > 3, (
        f"快照被 `--limit 3` 截断成 {len(snapshot)} 条 —— "
        "此后序号 4/5 永久不可达，而库里仍有 5 个达线岗位"
    )


def test_empty_selection_does_not_erase_todays_snapshot(session, settings) -> None:
    """🔴 同源：某次筛选为空时会把快照写成 `[]`，而 `load_snapshot` 返回 `[]`（falsy）
    → 静默退回实时筛选，此时序号含义与用户上次看到的清单**完全无关**。"""
    _seed(session, settings, count=3)
    session.flush()

    today_svc.build_today_list(session, limit=3, settings=settings)
    before = today_svc.load_snapshot(settings)
    assert before, "前提：先要有一份非空快照"

    today_svc.build_today_list(session, min_score=999, settings=settings)  # 筛不出东西
    after = today_svc.load_snapshot(settings)

    assert after == before, (
        f"一次空结果把今天的快照清掉了：{before} → {after}\n"
        "此后 `jh apply N` 会静默退回实时筛选，序号指向的岗位与用户看到的不再一致"
    )


@pytest.mark.parametrize(
    "garbage",
    ['{"date": ', "not json at all", "[]", '{"date": "x", "job_ids": "abc"}', ""],
)
def test_snapshot_file_with_garbage_does_not_crash(garbage: str, session, settings) -> None:
    """对抗：快照文件被手工改坏 / 写坏（截断、类型错、空文件）。

    期望：当作"没有快照"（退回实时筛选），**不是崩溃、也不是按半截数据乱指岗位**。
    """
    _seed(session, settings, count=2)
    session.flush()
    path = today_svc.snapshot_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, garbage)

    assert today_svc.load_snapshot(settings) is None, f"坏快照 {garbage!r} 没被拒绝"
    job, from_snapshot = today_svc.resolve_index(session, 1, settings=settings)
    assert job is not None and from_snapshot is False, "坏快照之后没有正确退回实时筛选"


# ===========================================================================
# ③ "先打标、后生成"→ 开场白永久缺席
# ===========================================================================
def test_applying_before_generating_does_not_lose_the_greeting(session, settings) -> None:
    """🟡 依据：设计文档 §4「`applications` 要有 greeting_sent / greeting_edited（闭环）」
    与 §5.1.5「开场白闭环可追踪」。

    路径：先 `jh apply N --status applied`（还没跑 `jh today`）→
    `ensure_pending_application` 建了一条 `greeting_text=None` 的记录；
    此后生成开场白时**不覆盖已有记录**（保护人工内容，设计合理），
    于是这条投递永远没有开场白，也不会写 `greeting_sent` 事件 ——
    统计"话术有没有被用上"时它会安静地消失。
    """
    jobs = _seed(session, settings, count=1)
    session.flush()

    apply_svc.set_status(session, jobs[0].id, "applied")
    session.flush()

    today_svc.ensure_item(session, jobs[0], 1, settings=settings)
    session.flush()

    application = session.scalars(
        select(Application).where(Application.job_id == jobs[0].id)
    ).first()
    assert application is not None
    assert application.greeting_text, (
        "先打标后生成时，这条投递拿不到开场白（greeting_text 仍是 None）——闭环统计会漏掉它"
    )


# ===========================================================================
# ④ "过滤前达线 N 条"的口径
# ===========================================================================
@pytest.mark.xfail(
    reason="ISSUE #35（🟡）：candidates 计数在'排除已投递'之后统计，与 UI 文案'过滤前'不符",
    strict=False,
)
def test_candidates_count_matches_its_label(session, settings) -> None:
    """🟡 依据：`TodayList.candidates` 注释「过滤前达到分数线的数量」（`today_svc.py:90`）
    与 CLI 文案「过滤前达线 N 条」。

    实测 `total` 是在"排掉已有非 pending 投递"**之后**统计的（`today_svc.py:133-134`），
    所以那个数字是"排掉已投之后"的，与字面意思不符 ——
    用户会误判"今天还有多少机会"。
    """
    _seed(session, settings, count=3)
    session.flush()

    jobs_all, total_before, _dropped = today_svc.select_candidates(session, settings=settings)
    apply_svc.set_status(session, jobs_all[0].id, "applied")
    session.flush()
    _jobs, total_after, _dropped = today_svc.select_candidates(session, settings=settings)

    assert total_after == total_before, (
        f"「过滤前达线」的数量在排除已投递之后变了：{total_before} → {total_after}\n"
        "要么改口径（在排除之前统计），要么改文案（说明已排除已投递）"
    )


# ===========================================================================
# ⑤ 加固观察项
# ===========================================================================
def test_pending_application_greeting_is_not_silently_replaced(session, settings) -> None:
    """对照（应当通过）：已有人工写入的开场白**不该**被自动生成覆盖
    （`apply_svc.py:96-99`「保护已有内容」）。"""
    jobs = _seed(session, settings, count=1)
    session.flush()
    apply_svc.ensure_pending_application(
        session,
        jobs[0],
        resume_id=None,
        greeting="人工写好的开场白",
        greeting_variant="with_project",
    )
    session.flush()

    today_svc.ensure_item(session, jobs[0], 1, settings=settings)
    session.flush()

    application = session.scalars(
        select(Application).where(Application.job_id == jobs[0].id)
    ).first()
    assert application is not None
    assert application.greeting_text == "人工写好的开场白", (
        f"人工写的开场白被自动生成覆盖了：{application.greeting_text!r}"
    )


@pytest.mark.xfail(
    reason="ISSUE #42（🟢）：event_type 无 CHECK 约束，拼错的事件类型会被静默写库",
    strict=False,
)
def test_application_events_rejects_unknown_event_type(session, settings) -> None:
    """🟢 加固观察项：`application_events.event_type` 是自由的 `String(40)`，无 CHECK 约束
    （`models/constants.py:10-11` 说留到 P6）。

    后果：拼错的事件类型会被静默写进去，而 P6 的时间轴/统计按 event_type 分类
    → 出现"永远对不上的分类"。这正是 `apply_cmd.py` docstring 自己警惕的失败方式。
    """
    jobs = _seed(session, settings, count=1)
    session.flush()
    application, _ = apply_svc.ensure_pending_application(session, jobs[0])
    session.flush()

    with pytest.raises(Exception):  # noqa: B017, PT011 - 期望数据库层拒绝未知 event_type
        session.execute(
            text(
                "INSERT INTO application_events (application_id, event_type, occurred_at,"
                " created_at) VALUES (:aid, 'status_chnage', '2026-01-01', '2026-01-01')"
            ),
            {"aid": application.id},
        )
        session.flush()
    session.rollback()

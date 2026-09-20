"""`jh cache` 与"命中缓存时本次记 0"的口径（ISSUES #25 / #26）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 这两条 issue 是同一件事的两半，所以用例也放一个文件里                         ║
╚══════════════════════════════════════════════════════════════════════════╝

    #25  `llm_cache` 没有清理入口、命中率也算不出来
         → `jh cache stats` / `jh cache clear` + `hits` / `last_hit_at` 两列
    #26  命中缓存时把**历史** tokens/cost 累加进"本次"
         → 本次记 0；另记"省下多少"；两笔账在屏幕上分开显示

★ 这里守的是**行为与口径**（外部契约由 `tests/verify/**` 守）：
    · 命中率 = Σhits / (Σhits + 行数)，且这个定义必须打印给用户看；
    · `clear --task` 只删那一类，`clear` **只删 llm_cache 表**（其它表行数不变）；
    · `--yes` 才真删；非交互终端**不删**（没人回答 ≠ 同意）；
    · 命中缓存时本次 `cost == 0`、`saved_cost > 0`（改前是"本次 cost = 历史值"）；
    · `get_cached()` 自增 hits、同一行第二次命中变成 2，未命中时一行都不写；
    · 迁移 0004 可升级 / 可回滚 / 老行默认 hits=0。

★ 关于"改前是什么样"：`test_cache_hit_does_not_inflate_this_run_cost` 用
  `LLMRunStats` 的原始行为做对照（老实现会把命中行的 cost 加进本次成本），
  所以这条用例的断言方向明确是"反转"而不是"顺便对一下"。
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from typer.testing import CliRunner

from alembic import command
from jobhunter.cli.app import app
from jobhunter.config.settings import get_settings
from jobhunter.llm import cache as llm_cache
from jobhunter.llm.mock import MockProvider
from jobhunter.models import (
    Application,
    Company,
    Interview,
    Job,
    LLMCache,
    Offer,
    Resume,
    ResumeVersion,
)
from jobhunter.services import cache_stats_svc, match_llm_svc, match_svc
from jobhunter.services.resume_svc import EducationItem, ProjectItem, ResumeContent
from jobhunter.storage.db import create_db_engine, get_session_factory
from jobhunter.storage.migrate import alembic_config, current_revision, upgrade_head
from jobhunter.utils.time import now_utc

runner = CliRunner()

JD = (
    "岗位职责：负责 AI Agent 应用开发，基于大模型 API 实现工具调用与多轮对话。\n"
    "任职要求：熟悉 Python、LangChain、RAG，了解 FastAPI。"
)

VERDICT_JSON = (
    '{"score": 88, "strengths": ["有 RAG 项目经验"], "gaps": ["缺少线上部署经验"],'
    ' "missing_keywords": ["Docker"], "recommended_resume": null, "reason": "方向一致"}'
)

#: 一次真实调用的成本（用例断言用的假价钱；模拟 DeepSeek 那种"很便宜但非零"）
CALL_COST = 0.02

#: 除 `llm_cache` 之外的业务表 —— `clear` 之后它们的行数必须一条不变
OTHER_TABLES = (
    Company.__tablename__,
    Resume.__tablename__,
    ResumeVersion.__tablename__,
    Job.__tablename__,
    Application.__tablename__,
    Interview.__tablename__,
    Offer.__tablename__,
)


# ---------------------------------------------------------------------------
# 夹具与助手
# ---------------------------------------------------------------------------
def make_content(**overrides: Any) -> ResumeContent:
    base: dict[str, Any] = {
        "basic": {"job_intention": "AI Agent"},
        "education": [
            EducationItem(
                school="某某大学", major="软件工程", degree="本科", period="2023.09 - 2027.06"
            )
        ],
        "skills": ["Python", "LangChain", "RAG"],
        "projects": [ProjectItem(name="Agent 平台", skills=["LangChain"])],
    }
    base.update(overrides)
    return ResumeContent(**base)


class CostedProvider(MockProvider):
    """MockProvider + 一个**非零**成本（默认 mock 的 cost 恒为 0，测不出"省下")。"""

    def __init__(self, responses: dict[str, str] | None = None, *, cost: float = CALL_COST) -> None:
        super().__init__(responses)
        self.cost = cost

    def complete(self, payload):  # noqa: ANN001, ANN201 - 测试替身
        response = super().complete(payload)
        response.cost = self.cost
        return response


def seed_job_with_cache_row(
    session,
    *,
    key: str,
    task: str,
    cost: float | None,
    hits: int = 0,
    content: str = VERDICT_JSON,
    model: str = "mock-model",
    tokens_in: int | None = 100,
    tokens_out: int | None = 20,
) -> LLMCache:
    """直接写一行缓存（CLI 用例用；不需要真跑一遍 LLM）。"""
    row = LLMCache(
        cache_key=key,
        task=task,
        response_json=content,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=cost,
        model=model,
        hits=hits,
        last_hit_at=None,
    )
    session.add(row)
    session.flush()
    return row


# ===========================================================================
# ① `get_cached()` —— hits 的唯一自增点（ISSUE #25 的实现核心）
# ===========================================================================
def test_get_cached_increments_hits_and_stamps_last_hit(session, settings) -> None:
    row = seed_job_with_cache_row(session, key="k" * 64, task="match", cost=CALL_COST)

    first = llm_cache.get_cached(session, "k" * 64)
    assert first is not None
    assert row.hits == 1, "第一次命中必须在唯一入口自增（否则命中率永远算不出来）"
    assert row.last_hit_at is not None
    assert first.hits == 0, "返回结构里的 hits 是**命中前**的值（本次之后才是 1）"

    second = llm_cache.get_cached(session, "k" * 64)
    assert second is not None
    assert row.hits == 2, "同一行命中两次就该记 2 次（命中率的分母正是靠它）"
    assert second.hits == 1


def test_get_cached_returns_none_without_writing_anything(session, settings) -> None:
    """未命中：一行都不写（纯读代码路径跑一万次也不该造出幽灵行）。"""
    assert llm_cache.get_cached(session, "0" * 64) is None
    assert session.scalars(select(LLMCache)).all() == []
    assert session.query(LLMCache).count() == 0


def test_hit_counter_is_not_lost_when_the_caller_rolls_back(session, settings) -> None:
    """★ 只在 `get_cached()` 里 flush、不 commit —— 回滚时计数跟着业务一起撤销。

    这是**有意**的（见 `get_cached` 的 docstring）：失败的操作谈不上"省了钱"，
    而且这里 commit 会把调用方还没写完的业务数据提前落盘（制造"缓存写了、结果没写"）。
    这条用例把那个取舍钉住：不 commit 不是漏写，而是"让调用方掌握事务边界"。
    """
    row = seed_job_with_cache_row(session, key="r" * 64, task="match", cost=CALL_COST)
    session.commit()

    llm_cache.get_cached(session, "r" * 64)
    session.flush()
    assert row.hits == 1, "测试前提：命中确实自增了"

    session.rollback()
    session.expire_all()
    assert row.hits == 0, "回滚之后计数不该留下（半途失败的操作不算命中）"


def test_get_cached_picks_up_a_row_written_earlier_in_the_same_session(session, settings) -> None:
    """同一 Session 里刚 `put_cached` 的行应当能被读到（不要绕开未提交的数据）。"""
    llm_cache.put_cached(
        session,
        "p" * 64,
        task="greeting",
        content="{}",
        model="mock-model",
        cost=CALL_COST,
    )
    hit = llm_cache.get_cached(session, "p" * 64)
    assert hit is not None
    assert session.get(LLMCache, "p" * 64).hits == 1


def test_saved_equals_the_row_cost(session, settings) -> None:
    """「省下」的定义就是那一行的 `cost`（口径与 #25 的 cache stats 同一个乘积）。"""
    seed_job_with_cache_row(session, key="s" * 64, task="match", cost=CALL_COST)
    hit = llm_cache.get_cached(session, "s" * 64)
    assert hit is not None and hit.saved == CALL_COST

    stats = cache_stats_svc.CacheHitStats()
    stats.add(hit)
    assert (stats.hits, stats.saved_cost) == (1, CALL_COST)
    assert "省下" in stats.summary


def test_saved_accumulates_within_one_batch(session, settings) -> None:
    """一次运行里命中多条 → "本次省下"是这些行成本的和（不是只记最后一条）。"""
    hit = llm_cache.CachedResponse(content="{}", model="m", cost=0.03)
    stats = cache_stats_svc.CacheHitStats()
    stats.add(hit)
    stats.add(llm_cache.CachedResponse(content="{}", model="m", cost=0.01))
    assert (stats.hits, stats.saved_cost) == (2, pytest.approx(0.04))
    assert "命中缓存 2 条" in stats.summary


def test_saved_is_none_when_the_row_has_no_cost(session, settings) -> None:
    """没记成本的行：`saved is None`（不算成"省了 0 元"，那是两回事）。"""
    seed_job_with_cache_row(session, key="n" * 64, task="match", cost=None)
    hit = llm_cache.get_cached(session, "n" * 64)
    assert hit is not None and hit.saved is None

    stats = cache_stats_svc.CacheHitStats()
    stats.add(hit)
    assert stats.saved_cost == 0.0
    assert stats.unknown_cost_hits == 1, "要说清'有几条没记成本'，否则金额看起来是准的"
    assert "没记成本" in stats.summary


# ===========================================================================
# ② ISSUE #26 —— 命中缓存时"本次花 0、另记省下"
# ===========================================================================
def _run_match_llm(session, jobs, provider) -> match_llm_svc.LLMRunStats:
    settings = get_settings()
    candidates = match_svc.load_resume_candidates(session, settings)
    import asyncio

    return asyncio.run(
        match_llm_svc.run_llm_layer(session, jobs, candidates, provider=provider, settings=settings)
    )


def _scored_job(session, *, jd_hash: str = "j" * 64) -> Job:
    resume = Resume(
        name="主简历",
        direction="AI Agent",
        content_json=make_content().model_dump_json(),
        content_hash="f" * 64,
        is_default=True,
    )
    session.add(resume)
    job = Job(
        platform="manual",
        company="某某科技",
        title="AI Agent 开发工程师",
        city="深圳",
        jd_text=JD,
        jd_hash=jd_hash,
        status="new",
    )
    session.add(job)
    session.flush()

    settings = get_settings()
    candidates = match_svc.load_resume_candidates(session, settings)
    result = match_svc.match_best_candidate(job.jd_text, candidates, job=job, settings=settings)
    assert result is not None
    match_svc.apply_result_to_job(job, result)
    session.flush()
    return job


def test_cache_hit_does_not_inflate_this_run_cost(session, settings) -> None:
    """★★#26 的核心反转：命中缓存时**本次成本记 0**，省下的钱另记。

    改之前：`stats.cost += hit.cost`（命中行的**历史**值）—— 同一份缓存命中
    10 次，这里就报 10 倍的钱，用户会以为这工具很贵，而它其实很便宜。
    所以断言必须成对：本次 cost 恒为 0（哪怕命中行记着 0.02），
    而 saved_cost 等于那一行的成本。
    """
    job = _scored_job(session)
    provider = CostedProvider({"match": VERDICT_JSON})

    first = _run_match_llm(session, [job], provider)
    assert (first.calls, first.cached) == (1, 0)
    assert first.tokens_in > 0 and first.tokens_out > 0
    assert first.cost == pytest.approx(CALL_COST), "第一次是真调用，成本要照实记"
    assert first.saved_cost == 0.0

    # 第二次：同一岗位、同一简历、同一模型 → 命中缓存
    second = _run_match_llm(session, [job], provider)
    assert (second.calls, second.cached) == (0, 1)
    assert second.cost == 0.0, "命中缓存时本次成本必须是 0（改前是历史值）"
    assert second.tokens_in == 0 and second.tokens_out == 0
    assert second.saved_cost == pytest.approx(CALL_COST), "省下的钱要与那一行的 cost 一致"
    assert second.saved_stats.hits == 1

    # 再命中一次：本次仍是 0、省下的仍是那一行的成本。
    # ★ 注意口径："省下"是**每次运行各自**记一笔（`LLMRunStats` 是一次运行的统计），
    #   跨运行的累计口径在 `jh cache stats`（`Σ cost × hits`）。
    third = _run_match_llm(session, [job], provider)
    rows = session.scalars(select(LLMCache)).all()
    assert [row.hits for row in rows] == [2], f"缓存行不该被重写：{rows}"
    assert third.cost == 0.0
    assert third.saved_cost == pytest.approx(CALL_COST), "本次仍是那一行的成本，不是翻倍"
    assert len(provider.calls) == 1, "三次运行只该真的调用一次模型"

    # 而累计口径（`cache stats`）看的是 2 次复用：省下 = 2 × 行成本
    report = cache_stats_svc.build_cache_stats(session)
    match_stat = next(item for item in report.tasks if item.task == "match")
    assert match_stat.hits == 2
    assert match_stat.saved == pytest.approx(CALL_COST * 2)


def test_hit_rate_and_saved_are_the_same_arithmetic(session, settings) -> None:
    """★ 两半共用一个口径：命中 3 次之后 `cache stats` 的省下 == 3 × 行成本。"""
    job = _scored_job(session)
    provider = CostedProvider({"match": VERDICT_JSON})
    for _ in range(4):  # 1 次真调用 + 3 次命中
        _run_match_llm(session, [job], provider)

    report = cache_stats_svc.build_cache_stats(session)
    row = next(item for item in report.tasks if item.task == "match")
    assert (row.entries, row.hits) == (1, 3)
    assert row.hit_rate == pytest.approx(0.75)  # 3 / (3 + 1)
    assert row.saved == pytest.approx(CALL_COST * 3)
    assert report.overall.saved == pytest.approx(row.saved)
    assert report.overall.attempts == 4


# ===========================================================================
# ③ `cache_stats_svc.build_cache_stats()` —— 分组与数字（口径的唯一实现）
# ===========================================================================
def test_build_cache_stats_groups_by_task_and_sums(session, settings) -> None:
    seed_job_with_cache_row(
        session, key="a" * 64, task="match", cost=0.02, hits=3, tokens_in=1000, tokens_out=200
    )
    seed_job_with_cache_row(
        session, key="b" * 64, task="match", cost=0.01, hits=1, tokens_in=500, tokens_out=100
    )
    seed_job_with_cache_row(
        session, key="c" * 64, task="greeting", cost=None, hits=0, tokens_in=None, tokens_out=None
    )
    session.flush()

    report = cache_stats_svc.build_cache_stats(session)
    by_task = {item.task: item for item in report.tasks}

    assert by_task["match"].entries == 2
    assert by_task["match"].tokens_in == 1500
    assert by_task["match"].tokens_out == 300
    assert by_task["match"].cost == pytest.approx(0.03)
    assert by_task["match"].hits == 4
    assert by_task["match"].hit_rate == pytest.approx(4 / 6)
    assert by_task["match"].saved == pytest.approx(0.02 * 3 + 0.01 * 1)

    # 成本为空的行：计入条目数，但金额与"省下"都不算它，并且要能说出有几行
    assert by_task["greeting"].entries == 1
    assert by_task["greeting"].missing_cost_rows == 1
    assert by_task["greeting"].saved == 0.0
    assert by_task["greeting"].hit_rate == 0.0  # 有行没命中 → 0%，不是 None

    # 合计 = 分组之和（两套算法迟早漂移，所以干脆只算一次再累加）
    assert report.overall.entries == 3
    assert report.overall.hits == 4
    assert report.overall.cost == pytest.approx(0.03)
    assert report.overall.saved == pytest.approx(0.07)
    assert report.overall.hit_rate == pytest.approx(4 / 7)
    assert report.overall.missing_cost_rows == 1
    assert [item.task for item in report.tasks] == ["match", "greeting"], "按成本从高到低"
    assert cache_stats_svc.HIT_RATE_NOTE in report.notes


def test_build_cache_stats_on_an_empty_table(session, settings) -> None:
    report = cache_stats_svc.build_cache_stats(session)
    assert report.is_empty
    assert report.overall.hit_rate is None, "一条都没有时是'没有数据'，不是 0%"
    assert report.tasks == []


def test_last_hit_at_tracks_the_most_recent_hit(session, settings) -> None:
    old = now_utc() - timedelta(days=3)
    seed_job_with_cache_row(session, key="d" * 64, task="match", cost=0.01, hits=2)
    row = session.get(LLMCache, "d" * 64)
    row.last_hit_at = old
    session.flush()

    report = cache_stats_svc.build_cache_stats(session)
    assert report.tasks[0].last_hit_at == old

    llm_cache.get_cached(session, "d" * 64)  # 再命中一次 → 时间戳刷新
    session.flush()
    report = cache_stats_svc.build_cache_stats(session)
    assert report.tasks[0].last_hit_at is not None
    assert report.tasks[0].last_hit_at > old, "最近命中时间必须跟着最后一次命中走"


# ===========================================================================
# ④ `jh cache stats`
# ===========================================================================
@pytest.fixture
def seeded_db(isolated_env: Path) -> Path:
    """建库 + 两行 match 缓存 + 一行 greeting 缓存（供 CLI 用例）。"""
    assert runner.invoke(app, ["--no-remind", "init"]).exit_code == 0
    engine, factory = get_session_factory(get_settings().database_url)
    with factory() as session:
        session.add(Company(name="某某科技"))
        session.add(
            Resume(
                name="主简历",
                direction="AI Agent",
                content_json=make_content().model_dump_json(),
                content_hash="f" * 64,
                is_default=True,
            )
        )
        session.add(Job(platform="manual", title="AI Agent 开发", city="深圳", jd_text=JD))
        seed_job_with_cache_row(
            session, key="a" * 64, task="match", cost=0.02, hits=3, tokens_in=1000, tokens_out=200
        )
        seed_job_with_cache_row(
            session, key="b" * 64, task="match", cost=0.01, hits=1, tokens_in=500, tokens_out=100
        )
        seed_job_with_cache_row(
            session, key="c" * 64, task="greeting", cost=0.005, hits=0, tokens_in=300, tokens_out=50
        )
        session.commit()
    engine.dispose()
    return isolated_env


def test_cli_cache_stats_shows_groups_and_numbers(seeded_db: Path) -> None:
    result = runner.invoke(app, ["--no-remind", "cache", "stats"])
    assert result.exit_code == 0, result.output
    for marker in (
        "LLM 缓存",
        "match",
        "greeting",
        "命中率",
        # match：被复用 4 次 / (4 次复用 + 2 行) = 4/6；分母带出来才看得懂
        "4/6",
        "被复用",
        "最近命中",
        "从未",  # greeting 那行从没被复用
        "省下",
    ):
        assert marker in result.output, f"`jh cache stats` 的输出里缺少：{marker}\n{result.output}"
    # 口径必须打印给用户（不许让他猜）
    assert "命中率 = 被复用次数 ÷" in result.output
    assert "省下 = 命中一次就省下那一行的 cost" in result.output
    assert "只删 llm_cache 表" in result.output


def test_cli_cache_stats_task_filter(seeded_db: Path) -> None:
    result = runner.invoke(app, ["--no-remind", "cache", "stats", "--task", "greeting"])
    assert result.exit_code == 0, result.output
    assert "greeting" in result.output
    assert "match" not in result.output, "过滤之后不该还出现别的任务"
    assert "0%（0/1）" in result.output, "一次都没命中过 → 0%，且要带分母"
    # ★ 合计必须**跟着过滤**（否则表里只有 greeting、合计却是三类的和）
    assert "合计：缓存 1 条" in result.output, f"合计没有跟着 --task 走：\n{result.output}"
    assert "以上合计只含 task=greeting" in result.output


def test_total_of_uses_the_same_arithmetic_as_the_groups(session, settings) -> None:
    """`total_of()` 是**唯一**的合计算法（CLI 过滤后也用它，避免两套数字）。"""
    seed_job_with_cache_row(session, key="aa" * 32, task="match", cost=0.02, hits=2)
    seed_job_with_cache_row(session, key="bb" * 32, task="greeting", cost=None, hits=0)
    session.flush()

    report = cache_stats_svc.build_cache_stats(session)
    manual = cache_stats_svc.total_of(report.tasks)
    assert manual == report.overall

    only_match = cache_stats_svc.total_of([report.tasks[0]])
    assert only_match.entries == report.tasks[0].entries
    assert only_match.saved == report.tasks[0].saved
    assert only_match.missing_cost_rows == report.tasks[0].missing_cost_rows


def test_cli_cache_stats_unknown_task_is_an_error(seeded_db: Path) -> None:
    """打错任务名要明确报错（空表会被误读成"这一类还没缓存过"）。"""
    result = runner.invoke(app, ["--no-remind", "cache", "stats", "--task", "macth"])
    assert result.exit_code == 1
    assert "没有 task=macth 的缓存" in result.output
    assert "match" in result.output, "报错里要列出已有的任务名"


def test_cli_cache_stats_on_empty_cache(isolated_env: Path) -> None:
    runner.invoke(app, ["--no-remind", "init"])
    result = runner.invoke(app, ["--no-remind", "cache", "stats"])
    assert result.exit_code == 0, result.output
    assert "缓存是空的" in result.output


def test_cli_cache_without_subcommand_prints_help(isolated_env: Path) -> None:
    result = runner.invoke(app, ["--no-remind", "cache"])
    assert result.exit_code in (0, 2)
    assert "stats" in result.output and "clear" in result.output


# ===========================================================================
# ⑤ `jh cache clear`
# ===========================================================================
def _row_counts() -> dict[str, int]:
    engine, factory = get_session_factory(get_settings().database_url)
    try:
        with factory() as session:
            return {
                name: int(session.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar_one())
                for name in ("llm_cache", *OTHER_TABLES)
            }
    finally:
        engine.dispose()


def test_cli_cache_clear_yes_deletes_everything_and_only_the_cache_table(
    seeded_db: Path,
) -> None:
    """★★ 交付要求里最硬的一条：`clear` **只删 llm_cache**，其它表行数一条不变。"""
    before = _row_counts()
    assert before["llm_cache"] == 3

    result = runner.invoke(app, ["--no-remind", "cache", "clear", "--yes"])
    assert result.exit_code == 0, result.output
    assert "已清掉 3 条缓存" in result.output
    assert "只删 llm_cache 表" in result.output

    after = _row_counts()
    assert after["llm_cache"] == 0
    for name in OTHER_TABLES:
        assert after[name] == before[name], (
            f"`jh cache clear` 把 {name} 动了：{before[name]} → {after[name]}"
            "（`models/llm_cache.py` 承诺过缓存是纯派生数据）"
        )


def test_cli_cache_clear_task_filter_keeps_the_rest(seeded_db: Path) -> None:
    result = runner.invoke(app, ["--no-remind", "cache", "clear", "--task", "greeting", "--yes"])
    assert result.exit_code == 0, result.output
    assert "已清掉 1 条缓存" in result.output

    engine, factory = get_session_factory(get_settings().database_url)
    try:
        with factory() as session:
            rows = session.scalars(select(LLMCache)).all()
    finally:
        engine.dispose()
    assert [row.task for row in rows] == ["match", "match"], "只该清掉 greeting 那一类"


def test_cli_cache_clear_prints_the_count_before_deleting(seeded_db: Path, monkeypatch) -> None:
    """★ 交付要求：**删之前**要打印"将要删多少条"（不是删完才报个数）。

    验证方式和用户实际看到的一样：走真实的 `typer.confirm`（它会把这句问话
    打到终端上），然后回答 n —— 于是"这句问话出现过"与"回答 n 就真的没删"
    被同一条用例钉住。
    """
    from jobhunter.cli.commands import cache_cmd

    monkeypatch.setattr(cache_cmd, "_stdin_is_interactive", lambda: True)
    result = runner.invoke(app, ["--no-remind", "cache", "clear"], input="n\n")
    assert result.exit_code == 0, result.output
    assert "将删除 3 条缓存（全部）" in result.output, f"没有在删之前报出条数：\n{result.output}"
    assert "已取消" in result.output
    assert _row_counts()["llm_cache"] == 3, "回答了 n 却还是删了"


def test_cli_cache_clear_declined_keeps_everything(seeded_db: Path, monkeypatch) -> None:
    """交互终端里回答 n → 一条都不删（走真实的 `typer.confirm`）。"""
    from jobhunter.cli.commands import cache_cmd

    monkeypatch.setattr(cache_cmd, "_stdin_is_interactive", lambda: True)
    result = runner.invoke(app, ["--no-remind", "cache", "clear"], input="n\n")
    assert result.exit_code == 0, result.output
    assert "已取消" in result.output
    assert _row_counts()["llm_cache"] == 3


def test_cli_cache_clear_confirmed_deletes(seeded_db: Path, monkeypatch) -> None:
    """交互终端里回答 y → 真删。"""
    from jobhunter.cli.commands import cache_cmd

    monkeypatch.setattr(cache_cmd, "_stdin_is_interactive", lambda: True)
    result = runner.invoke(app, ["--no-remind", "cache", "clear"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "已清掉 3 条缓存" in result.output
    assert _row_counts()["llm_cache"] == 0


def test_cli_cache_clear_refuses_without_a_tty(seeded_db: Path, monkeypatch) -> None:
    """★ 非交互（管道 / CI / 被别的脚本调用）时**不删**，并告诉用户加 --yes。

    "没人能回答这个问题"绝不能被当成"同意删除" —— 那会是一个静默发生的删除。
    """
    from jobhunter.cli.commands import cache_cmd

    monkeypatch.setattr(cache_cmd, "_stdin_is_interactive", lambda: False)
    result = runner.invoke(app, ["--no-remind", "cache", "clear"])
    assert result.exit_code == 0, result.output
    assert "--yes" in result.output
    assert "已清掉" not in result.output, "非交互时竟然真的删了"
    assert _row_counts()["llm_cache"] == 3, "非交互却把缓存删了"


def test_cli_cache_clear_aborts_cleanly_when_input_runs_out(seeded_db: Path, monkeypatch) -> None:
    """★ 问了确认却读不到输入（管道没数据 / Ctrl+C）时：

    要给一句人话 + 明确"什么都没删"，并以非 0 退出 —— 而不是一句干巴巴的 `Aborted.`
    （用户看不懂发生了什么，也不知道缓存到底删没删）。中止不等于成功，所以退出码不能是 0。
    """
    from jobhunter.cli.commands import cache_cmd

    monkeypatch.setattr(cache_cmd, "_stdin_is_interactive", lambda: True)
    result = runner.invoke(app, ["--no-remind", "cache", "clear"], input="")
    assert result.exit_code == 1, result.output
    assert "什么都没删" in result.output
    assert "--yes" in result.output
    assert _row_counts()["llm_cache"] == 3


def test_cli_cache_clear_on_empty_cache_says_nothing_happened(isolated_env: Path) -> None:
    runner.invoke(app, ["--no-remind", "init"])
    result = runner.invoke(app, ["--no-remind", "cache", "clear", "--yes"])
    assert result.exit_code == 0, result.output
    assert "没有要删的缓存" in result.output


def test_cli_cache_clear_then_stats_is_zero(seeded_db: Path) -> None:
    """清完之后命中率统计**整体归零**（缓存是派生数据，统计口径也一起丢弃）。"""
    runner.invoke(app, ["--no-remind", "cache", "clear", "--yes"])
    result = runner.invoke(app, ["--no-remind", "cache", "stats"])
    assert result.exit_code == 0, result.output
    assert "缓存是空的" in result.output


# ===========================================================================
# ⑥ 迁移 0004：可升级 / 可回滚 / 老行默认 hits=0
# ===========================================================================
def _columns(db_url: str) -> dict[str, dict]:
    import sqlalchemy as sa

    engine = create_db_engine(db_url, ensure_directory=False)
    try:
        return {col["name"]: col for col in sa.inspect(engine).get_columns("llm_cache")}
    finally:
        engine.dispose()


def test_migration_0004_is_reversible(db_url, alembic_head: str) -> None:
    """upgrade → downgrade → upgrade 三连（参考 `test_v_p6` 的写法）。"""
    upgrade_head(db_url)
    engine = create_db_engine(db_url, ensure_directory=False)
    try:
        # 到 head（可能已经不止 0004）—— 这条用例关心的是"0004 那两列"能不能回滚
        assert current_revision(engine) == alembic_head
    finally:
        engine.dispose()

    before = _columns(db_url)
    assert {"hits", "last_hit_at"} <= set(before)
    assert before["hits"]["nullable"] is False

    command.downgrade(alembic_config(db_url), "0003")
    dropped = _columns(db_url)
    assert "hits" not in dropped and "last_hit_at" not in dropped, "downgrade 没把两列删掉"

    # 回滚之后缓存行本身还在（downgrade 只动结构，不动缓存内容）
    engine = create_db_engine(db_url, ensure_directory=False)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO llm_cache (cache_key, task, response_json, created_at) "
                    "VALUES ('z', 'match', '{}', '2026-09-19 12:00:00')"
                )
            )
    finally:
        engine.dispose()

    upgrade_head(db_url)
    engine = create_db_engine(db_url, ensure_directory=False)
    try:
        assert current_revision(engine) == alembic_head
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT cache_key, hits, last_hit_at FROM llm_cache")).all()
    finally:
        engine.dispose()
    # ★ 老行必须被填成 0（而不是 NULL）：NULL 会让命中率算不出来 ——
    #   那正是这条迁移要修的毛病。server_default 就是为它准备的。
    assert [tuple(row) for row in rows] == [("z", 0, None)]


def test_migration_0004_upgrades_an_existing_0003_database_with_rows(db_url) -> None:
    """已有缓存行的老库升级：行不丢、hits 从 0 开始数（不假装知道历史）。"""
    upgrade_head(db_url)
    command.downgrade(alembic_config(db_url), "0003")

    engine = create_db_engine(db_url, ensure_directory=False)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO llm_cache (cache_key, task, response_json, cost, created_at) "
                    "VALUES ('old', 'match', '{\"score\": 60}', 0.03, '2026-09-19 12:00:00')"
                )
            )
    finally:
        engine.dispose()

    upgrade_head(db_url)  # ← 不许抛错
    engine = create_db_engine(db_url, ensure_directory=False)
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT cache_key, task, cost, hits FROM llm_cache")).one()
    finally:
        engine.dispose()
    assert tuple(row) == ("old", "match", 0.03, 0)


def test_model_and_migration_agree_on_the_new_columns(engine) -> None:
    """ORM 与迁移两份手写定义不许漂移（`test_smoke` 只比列名，这里比默认值）。"""
    import sqlalchemy as sa

    columns = {col["name"]: col for col in sa.inspect(engine).get_columns("llm_cache")}
    hits = LLMCache.__table__.columns["hits"]
    assert columns["hits"]["nullable"] is False
    assert hits.nullable is False
    assert hits.default is not None and hits.default.arg == 0
    assert hits.server_default is not None, (
        "迁移侧的 server_default 是给**已有行**兜底的，删掉它老库升级会留下 NULL"
    )


def test_llm_cache_still_has_exactly_the_declared_columns(engine) -> None:
    """加列不该顺手改别的（这条同时守住"没有多出重复的统计列"）。"""
    import sqlalchemy as sa

    names = {col["name"] for col in sa.inspect(engine).get_columns("llm_cache")}
    assert names == {
        "cache_key",
        "task",
        "response_json",
        "tokens_in",
        "tokens_out",
        "cost",
        "hits",
        "last_hit_at",
        "created_at",
        "model",
    }


# ===========================================================================
# ⑦ 结构守卫：命中计数只有一个写入点
# ===========================================================================
def test_hits_are_only_written_in_the_cache_choke_point() -> None:
    """★ `llm_cache.hits` 只许在 `llm/cache.py::get_cached()` 里自增（ISSUE #25 的关键设计）。

    五个调用点各写一遍的话，第六个必然漏 —— 这个项目已经因为同一形态
    （"形参加了但没人传"）吃过三次亏（#37 / #24 / #55）。
    所以这里不测行为，直接读源码：`src/` 里除 `llm/cache.py` 外，
    不许出现"把某个东西的 `.hits` 赋值"的写法。

    ★ 为什么不检查全局的 `Assign` 目标名（那样会把 `for hits in ...`、
      `stats.saved_stats.hits` 这类无关代码一起抓出来）：只看**属性**赋值
      （`x.hits = ...` / `x.hits += ...`），因为"命中计数"必然写在某个
      模型对象上。
    """
    import ast

    from jobhunter.config.paths import find_project_root

    src = find_project_root() / "src"
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        if path.name == "cache.py" and path.parent.name == "llm":
            continue  # 唯一的合法写入点
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets: list[ast.expr] = list(node.targets)
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            else:
                continue
            for target in targets:
                if not isinstance(target, ast.Attribute) or target.attr != "hits":
                    continue
                # `model.hits` / `row.hits` 这类写在 ORM 对象上的赋值都要报出来
                root = target.value
                root_name = root.id if isinstance(root, ast.Name) else ""
                if root_name in _ORM_HIT_OWNERS:
                    offenders.append(f"{path.relative_to(find_project_root())}:{node.lineno}")
    assert not offenders, (
        f"命中计数在这些地方被写了一次（应当只经过 llm/cache.py::get_cached()）：{offenders}"
    )


#: 属于 `LLMCache` 行的变量名（命中计数只该写在它们身上）。
#: ★ 写成白名单而不是"任何 `.hits =`"：`stats.saved_stats.hits` 这种统计字段
#:   本来就会在别处被赋值，把它算成违规会让守卫变成噪音（而噪音守卫会被关掉）。
_ORM_HIT_OWNERS = {"row", "existing", "hit", "model", "cache_row"}


def test_stats_service_does_not_reimplement_the_saved_arithmetic() -> None:
    """ "省下"的乘法只有一份：`CacheHitStats.add` 用的是 `CachedResponse.saved`。"""
    hit = llm_cache.CachedResponse(content="{}", model="m", cost=0.25)
    stats = cache_stats_svc.CacheHitStats()
    stats.add(hit)
    assert stats.saved_cost == 0.25

    # 换掉 `saved` 的实现，`add()` 的结果跟着变 —— 证明它没有另算一遍 cost
    class Renamed(llm_cache.CachedResponse):
        @property
        def saved(self) -> float | None:
            return 1.5

    other = cache_stats_svc.CacheHitStats()
    other.add(Renamed(content="{}", model="m", cost=0.25))
    assert other.saved_cost == 1.5, "`CacheHitStats` 自己算了一遍 cost（两份口径）"


def test_get_cached_returns_the_raw_response_json(session, settings) -> None:
    """缓存内容原样取出（不重新序列化）—— 顺带守住 `response_json` 的契约。"""
    payload = json.dumps({"score": 77}, ensure_ascii=False)
    seed_job_with_cache_row(session, key="q" * 64, task="match", cost=0.01, content=payload)
    hit = llm_cache.get_cached(session, "q" * 64)
    assert hit is not None and hit.content == payload


def test_count_cached_matches_the_rows(session, settings) -> None:
    seed_job_with_cache_row(session, key="x" * 64, task="match", cost=0.01)
    seed_job_with_cache_row(session, key="y" * 64, task="greeting", cost=0.01)
    session.flush()
    assert cache_stats_svc.count_cached(session) == 2
    assert cache_stats_svc.count_cached(session, task="match") == 1
    assert cache_stats_svc.count_cached(session, task="tailor") == 0
    assert cache_stats_svc.CACHE_TABLE == "llm_cache"


def test_clear_cache_returns_the_deleted_count(session, settings) -> None:
    seed_job_with_cache_row(session, key="m" * 64, task="match", cost=0.01)
    seed_job_with_cache_row(session, key="g" * 64, task="greeting", cost=0.01)
    session.flush()
    assert cache_stats_svc.clear_cache(session, task="match") == 1
    assert session.scalar(select(func.count()).select_from(LLMCache)) == 1
    assert cache_stats_svc.clear_cache(session) == 1
    assert session.scalar(select(func.count()).select_from(LLMCache)) == 0


def test_cache_table_is_still_pure_derived_data(session, settings) -> None:
    """清缓存不影响任何业务表（服务层的同一断言，CLI 层另有一条）。"""
    session.add(Company(name="某某科技"))
    session.add(
        Resume(
            name="主简历",
            direction="AI Agent",
            content_json=make_content().model_dump_json(),
            content_hash="f" * 64,
            is_default=True,
        )
    )
    session.add(Job(platform="manual", title="AI Agent 开发", jd_text=JD))
    seed_job_with_cache_row(session, key="t" * 64, task="match", cost=0.01)
    session.flush()

    counts_before = {
        name: int(session.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar_one())
        for name in OTHER_TABLES
    }
    cache_stats_svc.clear_cache(session)
    session.flush()
    counts_after = {
        name: int(session.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar_one())
        for name in OTHER_TABLES
    }
    assert counts_after == counts_before

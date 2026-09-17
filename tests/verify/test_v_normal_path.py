"""【正常路径】功能真的能用吗 —— 验证会话独立设计，不复用开发会话的断言。

覆盖：工程底座 → 岗位录入 → 去重 → 共享层匹配 → explain → gap → LLM 层与缓存 → 时间。
每条都独立运行（自建数据，不依赖执行顺序）。
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from jobhunter.adapters.manual import html_to_detail
from jobhunter.cli.app import app
from jobhunter.config.settings import get_settings
from jobhunter.llm import cache as llm_cache
from jobhunter.llm.mock import MockProvider
from jobhunter.services import job_svc, match_llm_svc, match_svc, resume_svc
from jobhunter.storage.db import check_pragmas, create_db_engine
from jobhunter.utils.io import read_text, to_json, write_text
from tests.verify._helpers import (
    build_detail,
    build_resume_content,
    insert_job,
    insert_resume,
    score_job,
)

runner = CliRunner()


# ===========================================================================
# ① 工程底座：不是"打印出来好看"，而是真的生效
# ===========================================================================
def test_pragmas_are_really_in_effect(engine) -> None:
    """依据：设计文档 §3.2「连接时执行 journal_mode=WAL / busy_timeout=5000 / foreign_keys=ON」。

    只读回配置值还不够（`foreign_keys` 是连接级的），所以下一条再验"真的拦住违约写入"。
    """
    pragmas = check_pragmas(engine)
    assert pragmas["journal_mode"] == "wal", f"WAL 没生效：{pragmas}"
    assert pragmas["foreign_keys"] == "1", f"外键没生效：{pragmas}"
    assert pragmas["busy_timeout"] == "5000", f"busy_timeout 不是 5000：{pragmas}"


def test_foreign_keys_actually_reject_orphan_row(engine) -> None:
    """依据：SQLite **默认不强制外键**，开了才有意义（db.py 自己的 docstring 就强调了这点）。

    用一条指向不存在简历的 jobs 写入验证：必须 IntegrityError。
    这条不过 = `foreign_keys=ON` 只是"看起来开了"。
    """
    with engine.connect() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO jobs (platform, jd_text, status, found_at, created_at,"
                " matched_resume_id) VALUES ('x', 'y', 'new', '2026-01-01',"
                " '2026-01-01', 999999)"
            )
        )


def test_utc_stored_and_shanghai_displayed() -> None:
    """依据：设计文档 §3.2「统一 UTC aware 存储；展示层转 Asia/Shanghai」。

    关键：UTC 的 2026-09-10 20:00 在北京是 **09-11 04:00**，日期跨天 ——
    这正是"直接切字符串会错"的场景。
    """
    from datetime import UTC, datetime

    from jobhunter.utils.time import DISPLAY_TZ, fmt_local, now_utc, to_local

    assert DISPLAY_TZ.key == "Asia/Shanghai"
    assert now_utc().tzinfo is not None, "now_utc() 必须返回 aware"

    moment = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)
    assert to_local(moment).strftime("%Y-%m-%d %H:%M") == "2026-09-11 04:00"
    assert fmt_local(moment) == "2026-09-11 04:00"


def test_log_redaction_masks_phone_and_email() -> None:
    """依据：设计文档 §3.2「日志 Filter 自动打码手机号/邮箱」。

    "打印一条含手机号的日志看输出" —— 这里就是那条。

    ★ 不依赖 pytest 的 capsys/capfd：日志 handler 绑的是**进程级** stdout，
      捕获fixture 抓它的行为随 pytest 版本而变（实测过：抓不到）。
      这里直接接管 root 上每个 StreamHandler 的 stream，读**真正会被写出去**的字符串 ——
      这样测的是脱敏管线本身，而不是捕获机制。
    """
    import io

    from jobhunter.utils.logging import RedactingFilter, setup_logging

    setup_logging("INFO")
    root = logging.getLogger()
    assert root.handlers, "setup_logging 没有安装任何 handler"
    for handler in root.handlers:
        assert any(isinstance(f, RedactingFilter) for f in handler.filters), (
            f"handler {handler!r} 上没挂脱敏 Filter —— 日志会从这里漏出去"
        )

    buffers: list[io.StringIO] = []
    for handler in root.handlers:
        if isinstance(handler, logging.StreamHandler):
            buffer = io.StringIO()
            handler.stream = buffer  # 接管输出，读"最终写出去的内容"
            buffers.append(buffer)

    phone = "13812345678"
    email = "zhangsan@qq.com"
    logger = logging.getLogger("verify.redaction")
    logger.warning("投递联系人 %s 邮箱 %s", phone, email)  # args 里带敏感信息
    logger.warning(f"拼接写法 {phone} / {email}")  # noqa: G004 - msg 里已是完整字符串

    written = "".join(buffer.getvalue() for buffer in buffers)
    assert written.strip(), "日志一条都没写出去，测试等于没测（检查 setup_logging 的 handler）"
    assert phone not in written, f"日志里出现了完整手机号：{written!r}"
    assert "zhangsan" not in written, f"日志里出现了完整邮箱用户名：{written!r}"
    assert "138****5678" in written, f"手机号没有被真正打码：{written!r}"
    assert "z***@qq.com" in written, f"邮箱没有被真正打码：{written!r}"


def test_utf8_is_enforced_on_write_and_read(isolated_env) -> None:
    """依据：设计文档 §3.2「统一 utils/io.py 强制 UTF-8」（Windows 裸 open 默认 cp936）。

    Windows 上实测一次：写中文 + emoji，磁盘字节必须是 UTF-8，读回必须一致。
    """
    target = isolated_env / "编码测试.txt"
    payload = "中文内容 🚀 全角Ａｇｅｎｔ"
    write_text(target, payload)

    raw = target.read_bytes()
    assert raw.decode("utf-8") == payload, "磁盘字节不是 UTF-8"
    assert read_text(target) == payload
    with pytest.raises(UnicodeDecodeError):
        raw.decode("gbk")


def test_write_text_does_not_leave_partial_file_on_path(isolated_env) -> None:
    """依据：io.write_text docstring「默认开启 atomic，保证写到一半崩了不会留半截文件」。"""
    target = isolated_env / "原子写.txt"
    write_text(target, "第一次内容")
    write_text(target, "第二次内容")
    assert read_text(target) == "第二次内容"
    leftovers = [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, f"原子写留下了临时文件：{leftovers}"


# ===========================================================================
# ② 岗位录入主路径：人给链接 → 抓详情 → 入库
# ===========================================================================
def test_html_fixture_parses_into_usable_detail(sample_html: str) -> None:
    """依据：设计文档 §5.1「主路径 = 人给 URL → 抓详情」+ P2 提示词。"""
    detail = html_to_detail(sample_html, "https://www.zhipin.com/job_detail/abc123.html")
    assert detail.is_usable, f"抓取结果不可用：{detail.fetch_status=} {detail.jd_text[:80]!r}"
    assert detail.company == "虚构科技有限公司"
    assert detail.title == "AI Agent 开发工程师"
    assert detail.city == "深圳"
    assert detail.platform_job_id == "abc123", "platform_job_id 应从 URL 抽出"
    assert "FastAPI" in detail.jd_text
    assert detail.salary_text, "页面上明明有 15-25K·14薪，薪资却没抓到"


def test_upsert_then_dedup_hits_platform_job_id(session, sample_html: str) -> None:
    """依据：设计文档 §5.1「去重① platform_job_id 精确匹配」。"""
    detail = html_to_detail(sample_html, "https://www.zhipin.com/job_detail/abc123.html")
    first = job_svc.upsert_job(session, detail)
    assert first.outcome == "created" and first.is_new

    second = job_svc.upsert_job(session, detail)
    assert second.job_id == first.job_id, "同一 platform_job_id 不该产生第二条"
    assert second.matched_by == "platform_job_id"
    assert job_svc.count_by_status(session).get("new") == 1


def test_job_list_cli_runs_on_empty_and_populated_db(session, engine) -> None:  # noqa: ARG001
    """依据：README 的 `jh job list`。空库与有数据都要能跑（CLI 边界）。"""
    import os

    empty = runner.invoke(app, ["job", "list"])
    assert empty.exit_code == 0, f"空库时 jh job list 失败：{empty.output}"

    insert_job(session)
    session.commit()
    populated = runner.invoke(app, ["job", "list"])
    assert populated.exit_code == 0, f"有数据时 jh job list 失败：{populated.output}"
    assert "AI Agent" in populated.output
    assert os.environ.get("JH_DATA_DIR"), "环境隔离失效"


# ===========================================================================
# ③ 共享中间层：打分、explain 同源、gap
# ===========================================================================
def test_rule_score_pipeline_end_to_end(session, settings) -> None:
    """依据：设计文档 §2.1 共享层契约（jd_keywords / direction / skill_overlap /
    rule_score / rule_detail）。"""
    resume = insert_resume(session)
    job = insert_job(session)
    result = score_job(session, job, settings)

    assert 0 <= result.rule_score <= 100
    assert result.rule_score == job.rule_score, "落库的规则分与实际算出来的不一致"
    assert result.matched_resume_id == resume.id
    assert result.jd_keywords, "AI Agent 的 JD 一个技能词都没抽到，词表/匹配有问题"
    assert "Python" in result.matched_keywords, f"简历有 Python 却没命中：{result.skill_overlap}"
    assert set(result.rule_detail) >= {"direction", "hard", "skill", "total", "version", "parts"}
    assert result.rule_detail["total"] == result.rule_score


def test_explain_lines_agree_with_stored_score(session, settings) -> None:
    """依据：STATUS.md §4「rule_detail_json 与 explain 的一致性（三个来源同源）」。

    explain 显示的数字必须等于库里存的 rule_score —— 否则用户看到的解释是假的。
    """
    insert_resume(session)
    job = insert_job(session)
    score_job(session, job, settings)

    joined = "\n".join(match_svc.explain_lines(job))
    assert f"合计：{job.rule_score} 分" in joined, f"explain 的合计与库里不一致：\n{joined}"

    summary = match_svc.rule_detail_summary(job)
    assert summary, "rule_detail_summary 没解析出三个维度"
    assert sum(summary.values()) == pytest.approx(job.rule_score, abs=1.0), (
        f"三个维度之和 {summary} 与总分 {job.rule_score} 对不上"
    )


def test_gap_aggregates_and_does_not_write_db(session, settings) -> None:
    """依据：设计文档 §5.1.3「jh gap 跨岗位聚合缺口」+ STATUS.md「不落库」。"""
    insert_resume(session)
    for idx in range(3):
        job = insert_job(
            session,
            build_detail(
                platform_job_id=f"gap{idx}",
                url=f"https://www.zhipin.com/job_detail/gap{idx}.html",
                jd_text=("要求熟悉 Python、FastAPI、Kubernetes 与 RAG，负责大模型应用的后端开发。"),
            ),
        )
        score_job(session, job, settings)
    session.commit()

    before = session.execute(text("SELECT COUNT(*) FROM jobs")).scalar_one()
    report = match_svc.analyze_gaps(session, settings=settings)
    after = session.execute(text("SELECT COUNT(*) FROM jobs")).scalar_one()

    assert before == after, "analyze_gaps 不该写库"
    rows = {row.keyword: row for row in report.rows}
    assert "Kubernetes" in rows, f"3 个 JD 都要求 Kubernetes，缺口表里却没有：{sorted(rows)}"
    assert rows["Kubernetes"].have is False
    assert rows["Kubernetes"].priority in {"high", "medium"}, (
        f"3/3 = 100% 的岗位都要求它，优先级却是 {rows['Kubernetes'].priority!r}"
    )


# ===========================================================================
# ④ 技能匹配的词边界（设计文档点名的两个反例）
# ===========================================================================
@pytest.mark.parametrize(
    ("text", "should_match", "should_not_match"),
    [
        ("熟悉 JavaScript 开发", "JavaScript", "Java"),
        ("熟悉 Java 开发", "Java", "JavaScript"),
        ("用过 Google Analytics 做埋点", None, "Go"),
        ("用 Go 写后端服务", "Go", None),
    ],
)
def test_skill_word_boundary(
    text: str, should_match: str | None, should_not_match: str | None
) -> None:
    """依据：设计文档 §5.1.2「词表 + 英文词边界 \\b，Java≠JavaScript、Go≠Google」。"""
    vocabulary = resume_svc.load_skill_vocabulary(get_settings())
    hits = [name for name, aliases in vocabulary.items() if resume_svc._match_any(text, aliases)]

    if should_match:
        assert should_match in hits, f"{text!r} 应命中 {should_match}，实际命中 {hits}"
    if should_not_match:
        assert should_not_match not in hits, (
            f"{text!r} 不该命中 {should_not_match}，实际命中 {hits}"
        )


def test_hard_requirement_is_penalty_not_elimination(session, settings) -> None:
    """依据：设计文档 §5.1.2「硬条件不满足 → 大幅降分，**不是直接淘汰**」。

    构造"经验要求过高 + 城市不符 + 方向不符 + 技能全不中"的最差岗位：
    它仍必须出现在打分结果里、分数 > 0。这是"不静默丢弃机会"的纪律。
    """
    insert_resume(session)
    job = insert_job(
        session,
        build_detail(
            title="资深芯片验证工程师",
            city="哈尔滨",
            experience="8-10年",
            platform_job_id="worst1",
            url="https://www.zhipin.com/job_detail/worst1.html",
            jd_text="负责芯片前端验证，要求 Verilog、SystemVerilog、UVM，十年以上经验。",
        ),
    )
    result = score_job(session, job, settings)

    assert result.rule_score > 0, "硬条件不满足被'淘汰'成 0 分了，违反降分不淘汰的纪律"
    assert job.status == "scored", "岗位没有进入 scored 状态，等于被静默丢弃"
    assert result.rule_score < 60, f"这么差的岗位不该到 60 分：{result.rule_score}"
    assert result.matched_resume_id is not None


# ===========================================================================
# ⑤ LLM 层 + 缓存（设计文档 §5.1.2 / 评审采纳项 #3）
# ===========================================================================
async def test_cache_hit_does_not_call_provider_twice(session, settings) -> None:
    """依据：STATUS.md §4「同岗位跑两次只调一次 LLM」。"""
    insert_resume(session)
    job = insert_job(session)
    score_job(session, job, settings)
    candidates = match_svc.load_resume_candidates(session, settings)
    provider = MockProvider()

    stats1 = await match_llm_svc.run_llm_layer(
        session, [job], candidates, provider=provider, settings=settings
    )
    assert stats1.calls == 1, f"第一次应该真的调一次，实际 {stats1.calls}"
    assert job.llm_score is not None
    assert job.match_score == job.llm_score, "有 LLM 分时 match_score 必须用它"

    stats2 = await match_llm_svc.run_llm_layer(
        session, [job], candidates, provider=provider, settings=settings
    )
    assert stats2.calls == 0, "第二次不该再发请求（应命中缓存）"
    assert stats2.cached == 1
    assert len(provider.calls) == 1


async def test_editing_resume_invalidates_cache(session, settings) -> None:
    """依据：评审采纳项 #3「改简历应换新结果」—— 缓存 key 必须含 resume.content_hash。

    这是这个工具存在的核心理由：改简历就是为了让分数变。
    命中旧缓存的话，用户会得出"改简历没用"的错误结论。
    """
    resume = insert_resume(session)
    job = insert_job(session)
    score_job(session, job, settings)
    candidates = match_svc.load_resume_candidates(session, settings)
    provider = MockProvider()

    stats1 = await match_llm_svc.run_llm_layer(
        session, [job], candidates, provider=provider, settings=settings
    )
    assert stats1.calls == 1

    changed = build_resume_content(skills=[*build_resume_content().skills, "Kubernetes"])
    resume.content_json = to_json(changed.model_dump(), indent=None)
    resume.content_hash = resume_svc.compute_content_hash(changed)
    session.flush()

    candidates2 = match_svc.load_resume_candidates(session, settings)
    assert candidates2[0].content_hash != candidates[0].content_hash, "改简历后 content_hash 没变"

    stats2 = await match_llm_svc.run_llm_layer(
        session, [job], candidates2, provider=provider, settings=settings
    )
    assert stats2.calls == 1, "改了简历却命中旧缓存 —— 用户会看到分数纹丝不动"
    assert len(provider.calls) == 2


def test_cache_key_requires_all_four_components() -> None:
    """依据：设计文档 §5.1.2「cache_key = sha256(prompt_version|model|jd_hash|content_hash)」。

    四个成分缺任一必须返回 None（= 禁用缓存），绝不能用空串顶替 ——
    那会让不同简历共用一个 key，分数错得毫无痕迹。
    """
    base = {
        "prompt_version": 1,
        "model": "deepseek-chat",
        "jd_hash": "a" * 64,
        "resume_content_hash": "b" * 64,
    }
    key = llm_cache.compute_cache_key(**base)
    assert key is not None and len(key) == 64

    assert llm_cache.compute_cache_key(**{**base, "jd_hash": None}) is None
    assert llm_cache.compute_cache_key(**{**base, "resume_content_hash": None}) is None
    assert llm_cache.compute_cache_key(**{**base, "model": ""}) is None

    for field, value in (
        ("prompt_version", 2),
        ("model", "deepseek-reasoner"),
        ("jd_hash", "c" * 64),
        ("resume_content_hash", "d" * 64),
    ):
        assert llm_cache.compute_cache_key(**{**base, field: value}) != key, f"{field} 没参与 key"


# ===========================================================================
# ⑥ 时间与哈希的基本性质
# ===========================================================================
def test_days_since_uses_local_calendar_days() -> None:
    """依据：设计文档 §3.2 + 顶部提醒的"投后 3/7/14 天"口径（按北京自然日）。"""
    from datetime import UTC, datetime

    from jobhunter.utils.time import days_since

    now = datetime(2026, 9, 11, 4, 0, tzinfo=UTC)  # 北京 09-11 12:00
    same_local_day = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)  # 北京 09-11 04:00
    assert days_since(same_local_day, now=now) == 0
    three_days_ago = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)  # 北京 09-08 20:00
    assert days_since(three_days_ago, now=now) == 3


def test_sqlite_has_no_timezone_but_orm_restores_aware(session) -> None:
    """依据：设计文档 §3.2「存 UTC aware」。

    SQLite 没有时区类型，真正的保证来自 UTCDateTime：写进去 aware，读出来必须还是 aware。
    """
    from jobhunter.models import Job
    from jobhunter.utils.time import now_utc

    job = insert_job(session)
    moment = now_utc()
    job.last_seen_at = moment
    session.commit()
    session.expire_all()

    reloaded = session.get(Job, job.id)
    assert reloaded is not None and reloaded.last_seen_at is not None
    assert reloaded.last_seen_at.tzinfo is not None, "从库里读出来的时间丢了时区"
    assert reloaded.last_seen_at == moment


def test_jd_hash_is_normalization_insensitive() -> None:
    """依据：评审采纳项 #4 + compute_jd_hash docstring
    「先归一化再哈希，否则同一份 JD 两次抓到会得到不同 hash，缓存直接失效」。"""
    a = "熟悉 Python 开发，要求本科。"
    b = "熟悉　Python　开发，\n要求本科。  "  # 全角空格 + 换行 + 尾随空白
    assert job_svc.compute_jd_hash(a) == job_svc.compute_jd_hash(b)
    assert job_svc.compute_jd_hash(None) is None
    assert job_svc.compute_jd_hash("   ") is None


def test_dedup_norm_hash_normalizes_fullwidth_and_salary() -> None:
    """依据：设计文档 §5.1「dedup_norm_hash（去空格 / 全半角统一 / 去薪资）」。"""
    a = job_svc.compute_dedup_norm_hash("虚构科技有限公司", "AI Agent 开发工程师", "深圳")
    b = job_svc.compute_dedup_norm_hash("虚构科技有限公司 ", "ＡＩ　Ａｇｅｎｔ 开发工程师", "深圳")
    assert a is not None and a == b, "全角/空格归一化没生效"

    assert job_svc.compute_dedup_norm_hash(None, "x", "深圳") is None
    assert job_svc.compute_dedup_norm_hash("x", "y", None) is None


# ===========================================================================
# ⑦ 迁移与表结构（设计文档 §3.2 / 评审采纳项 #19）
# ===========================================================================
def test_migration_head_is_reachable(engine) -> None:
    """依据：设计文档 §3.2「Alembic：jh init 自动 upgrade head」+ §4 的 11 张表。"""
    from jobhunter.storage.migrate import current_revision

    assert current_revision(engine), "迁移没跑到任何 revision"

    names = set(inspect(engine).get_table_names())
    expected = {
        "resumes",
        "resume_versions",
        "jobs",
        "applications",
        "application_events",
        "interviews",
        "interview_questions",
        "assessments",
        "offers",
        "companies",
        "llm_cache",
    }
    assert not (expected - names), f"设计文档约定的 11 张表缺了：{expected - names}"


def test_jobs_table_has_required_columns_and_indexes(engine) -> None:
    """依据：评审采纳项 #19「jobs 表缺关键字段、没索引」+ 设计文档 §4。"""
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("jobs")}
    required = {
        "platform_job_id",
        "is_intern",
        "salary_min",
        "salary_max",
        "jd_hash",
        "jd_fetch_status",
        "source_keyword",
        "dedup_norm_hash",
        "jd_keywords_json",
        "rule_detail_json",
    }
    assert not (required - columns), f"jobs 表缺字段：{required - columns}"

    indexed = {name for idx in inspector.get_indexes("jobs") for name in idx["column_names"]}
    for name in ("platform_job_id", "dedup_norm_hash", "status", "match_score"):
        assert name in indexed, f"{name} 上没有索引（评审采纳项 #19）"


def test_cli_init_is_idempotent(db_url: str) -> None:
    """依据：README「jh init（幂等，可重复执行）」。"""
    first = runner.invoke(app, ["init"])
    assert first.exit_code == 0, first.output
    second = runner.invoke(app, ["init"])
    assert second.exit_code == 0, second.output

    engine = create_db_engine(db_url, ensure_directory=False)
    try:
        assert "jobs" in set(inspect(engine).get_table_names())
        assert check_pragmas(engine)["journal_mode"] == "wal"
    finally:
        engine.dispose()

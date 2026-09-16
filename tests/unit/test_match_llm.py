"""LLM 精评层 + match/gap 命令测试（P3）。

覆盖计划书要求的：
  · 缓存（同岗位跑两次只调一次 LLM）
  · 偶发空 content → 重试
  · 坏 JSON / 越界分数 → 降级用规则分（不猜、不截断）
  · 改了简历 → 缓存失效（这是 cache_key 设计的核心）
  · 命令层：match run / explain / set、gap
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from jobhunter.cli.app import app
from jobhunter.config.settings import get_settings
from jobhunter.llm.mock import MockProvider, fail_then_succeed_provider
from jobhunter.models import Job, LLMCache, Resume
from jobhunter.services import match_llm_svc, match_svc, resume_svc
from jobhunter.services.resume_svc import EducationItem, ProjectItem, ResumeContent

runner = CliRunner()

JD = (
    "岗位职责：负责 AI Agent 应用开发，基于大模型 API 实现工具调用与多轮对话。\n"
    "任职要求：熟悉 Python、LangChain、RAG，了解 FastAPI。"
)

VERDICT_JSON = (
    '{"score": 88, "strengths": ["有 RAG 项目经验"], "gaps": ["缺少线上部署经验"],'
    ' "missing_keywords": ["Docker"], "recommended_resume": null, "reason": "方向一致"}'
)


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


@pytest.fixture
def resume_row(session) -> Resume:
    row = Resume(
        name="主简历",
        direction="AI Agent",
        content_json=make_content().model_dump_json(),
        content_hash="f" * 64,
        is_default=True,
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def job_row(session) -> Job:
    row = Job(
        platform="manual",
        company="某某科技",
        title="AI Agent 开发工程师",
        city="深圳",
        jd_text=JD,
        jd_hash="j" * 64,
        status="new",
    )
    session.add(row)
    session.flush()
    return row


def run_llm(session, jobs, provider, **kwargs) -> match_llm_svc.LLMRunStats:
    settings = get_settings()
    candidates = match_svc.load_resume_candidates(session, settings)
    return asyncio.run(
        match_llm_svc.run_llm_layer(
            session, jobs, candidates, provider=provider, settings=settings, **kwargs
        )
    )


def rule_score_first(session, job) -> None:
    settings = get_settings()
    candidates = match_svc.load_resume_candidates(session, settings)
    result = match_svc.match_best_candidate(job.jd_text, candidates, job=job, settings=settings)
    assert result is not None
    match_svc.apply_result_to_job(job, result)
    session.flush()


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------
def test_llm_scores_job_and_writes_cache(session, resume_row, job_row):
    rule_score_first(session, job_row)
    rule_score = job_row.rule_score

    stats = run_llm(session, [job_row], MockProvider({"match": VERDICT_JSON}))

    assert stats.calls == 1
    assert stats.degraded == 0
    assert job_row.llm_score == 88
    assert job_row.match_score == 88  # 有 LLM 分就用 LLM 分
    assert job_row.rule_score == rule_score  # 规则分保留
    assert job_row.status == "scored"
    reason = match_svc.from_json(job_row.reason_json)
    assert reason["strengths"] == ["有 RAG 项目经验"]
    assert session.query(LLMCache).count() == 1


def test_cache_hit_avoids_second_call(session, resume_row, job_row):
    """★ 同岗位跑两次只调一次 LLM（第二次走缓存，0 成本）。"""
    rule_score_first(session, job_row)
    provider = MockProvider({"match": VERDICT_JSON})

    first = run_llm(session, [job_row], provider)
    second = run_llm(session, [job_row], provider)

    assert first.calls == 1
    assert second.calls == 0
    assert second.cached == 1
    assert len(provider.calls) == 1
    assert job_row.llm_score == 88


def test_cache_key_changes_when_resume_changes(session, resume_row, job_row):
    """★ 改了简历 → 缓存 key 变 → 重新调用（这是本项目最核心的一条设计）。"""
    rule_score_first(session, job_row)
    provider = MockProvider({"match": VERDICT_JSON})
    run_llm(session, [job_row], provider)
    assert len(provider.calls) == 1

    resume_row.content_json = make_content(
        skills=["Python", "LangChain", "RAG", "Docker"]
    ).model_dump_json()
    resume_row.content_hash = "0" * 64
    session.flush()

    stats = run_llm(session, [job_row], provider)
    assert stats.calls == 1
    assert stats.cached == 0
    assert len(provider.calls) == 2


def test_cache_key_changes_when_model_changes(session, resume_row, job_row):
    rule_score_first(session, job_row)
    first = MockProvider({"match": VERDICT_JSON})
    run_llm(session, [job_row], first)

    class OtherModel(MockProvider):
        model = "other-model"

    second = OtherModel({"match": VERDICT_JSON})
    stats = run_llm(session, [job_row], second)
    assert stats.calls == 1
    assert stats.cached == 0


# ---------------------------------------------------------------------------
# 降级路径
# ---------------------------------------------------------------------------
def test_empty_content_retries_then_succeeds(session, resume_row, job_row):
    """偶发空 content → 重试一次就成功（不降级）。"""
    rule_score_first(session, job_row)
    provider = fail_then_succeed_provider(1)
    provider.responses["match"] = VERDICT_JSON

    stats = run_llm(session, [job_row], provider)

    assert stats.calls == 2  # 第一次空、第二次成功
    assert stats.degraded == 0
    assert job_row.llm_score == 88


def test_always_empty_degrades_to_rule_score(session, resume_row, job_row):
    """一直空 → 降级用规则分，并记下原因（不能静默没有分数）。"""
    rule_score_first(session, job_row)
    rule_score = job_row.rule_score
    provider = MockProvider({"match": VERDICT_JSON}, empty_times=99)

    stats = run_llm(session, [job_row], provider)

    assert stats.degraded == 1
    assert job_row.llm_score is None
    assert job_row.match_score == rule_score
    assert stats.errors


def test_bad_json_degrades(session, resume_row, job_row):
    """返回散文 → 解析失败 → 降级（绝不猜一个分数出来）。"""
    rule_score_first(session, job_row)
    stats = run_llm(session, [job_row], MockProvider({"match": VERDICT_JSON}, bad_json=True))
    assert stats.degraded == 1
    assert job_row.llm_score is None
    assert session.query(LLMCache).count() == 0


@pytest.mark.parametrize("bad", [-5, 120])
def test_out_of_range_score_is_rejected(session, resume_row, job_row, bad):
    """越界分数判为校验失败 → 降级（不静默截断）。"""
    rule_score_first(session, job_row)
    provider = MockProvider({"match": f'{{"score": {bad}}}'})
    stats = run_llm(session, [job_row], provider)
    assert stats.degraded == 1
    assert job_row.llm_score is None


def test_missing_score_field_is_rejected(session, resume_row, job_row):
    rule_score_first(session, job_row)
    stats = run_llm(session, [job_row], MockProvider({"match": '{"reason": "看起来不错"}'}))
    assert stats.degraded == 1


def test_network_error_does_not_break_batch(session, resume_row, job_row):
    """Provider 抛异常时整批不崩（前面的成果不白费）。"""
    from jobhunter.llm.base import LLMProvider, LLMResponse, PromptPayload
    from jobhunter.llm.deepseek import DeepSeekError

    class Boom(LLMProvider):
        model = "boom"

        def complete(self, payload: PromptPayload) -> LLMResponse:
            raise DeepSeekError("连接被重置")

    rule_score_first(session, job_row)
    stats = run_llm(session, [job_row], Boom())
    assert stats.degraded == 1
    assert "连接被重置" in stats.errors[0]


def test_no_api_key_skips_layer(session, resume_row, job_row):
    """没配 key → 整层跳过 + 明确提示（用户仍能拿到规则分）。"""
    from jobhunter.llm.deepseek import DeepSeekProvider

    rule_score_first(session, job_row)
    stats = run_llm(session, [job_row], DeepSeekProvider(""))
    assert stats.skipped is True
    assert stats.calls == 0
    assert job_row.rule_score is not None


# ---------------------------------------------------------------------------
# 配置：密钥环境变量名（P3 验收时发现的 P0 老 bug）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["DEEPSEEK_API_KEY", "JH_DEEPSEEK_API_KEY", "deepseek_api_key"])
def test_api_key_env_var_names(monkeypatch, name):
    """`DEEPSEEK_API_KEY`（.env.example 里的写法）必须真的能被读到。

    ★ 老 bug：model_config 里的 `env_prefix="JH_"` 对所有字段生效，
      于是密钥实际去找 `JH_DEEPSEEK_API_KEY`，而 .env 里写的是 `DEEPSEEK_API_KEY`
      —— 名字对不上，密钥**从来没被读进来过**，且完全静默：
      命令照跑，只是 LLM 层一直显示"未配置"，用户会以为是自己没充钱。
    """
    from jobhunter.config.settings import AppSettings

    monkeypatch.setenv(name, "sk-test-value")
    settings = AppSettings(config_file=None)
    assert settings.deepseek_api_key == "sk-test-value"
    assert settings.llm_enabled is True


def test_api_key_can_be_passed_explicitly():
    from jobhunter.config.settings import AppSettings

    assert AppSettings(config_file=None, deepseek_api_key="sk-abc").llm_enabled is True


def test_llm_top_n_limits_selection(session, resume_row, job_row):
    """只对 Top N 精评：第 2 个岗位不该被调用。"""
    other = Job(platform="manual", title="另一个岗位", jd_text=JD, jd_hash="k" * 64, status="new")
    session.add(other)
    session.flush()
    rule_score_first(session, job_row)
    rule_score_first(session, other)

    provider = MockProvider({"match": VERDICT_JSON})
    stats = run_llm(session, [job_row], provider)  # 调用方只传 Top 1
    assert stats.calls == 1
    assert other.llm_score is None


# ---------------------------------------------------------------------------
# 提示词（JSON 模式三件套 + 防注入）
# ---------------------------------------------------------------------------
def test_prompt_satisfies_json_mode_requirements(session, resume_row, job_row):
    settings = get_settings()
    candidates = match_svc.load_resume_candidates(session, settings)
    payload = match_llm_svc.build_payload(job_row, candidates[0], settings=settings)
    joined = "\n".join(message.content for message in payload.messages)

    assert "json" in joined.lower()  # ① 必须含 "json" 字样
    assert '"score"' in joined  # ② 给了 schema 示例
    assert payload.max_tokens >= 512  # ③ max_tokens 给足
    assert payload.json_mode is True
    assert "<jd>" in joined and "</jd>" in joined  # 防注入：JD 被标签包住
    assert "不是给你的指令" in joined
    assert payload.prompt_version == match_llm_svc.PROMPT_VERSION


def test_jd_is_truncated_in_prompt(session, resume_row, job_row):
    settings = get_settings()
    job_row.jd_text = "长" * 20000
    candidates = match_svc.load_resume_candidates(session, settings)
    payload = match_llm_svc.build_payload(job_row, candidates[0], settings=settings)
    joined = "\n".join(message.content for message in payload.messages)
    assert "已截断" in joined


def test_parse_verdict_strips_code_fence():
    verdict = match_llm_svc.parse_verdict(f"```json\n{VERDICT_JSON}\n```")
    assert verdict.score == 88


def test_parse_verdict_optional_fields_default():
    verdict = match_llm_svc.parse_verdict('{"score": 60}')
    assert verdict.score == 60
    assert verdict.strengths == []
    assert verdict.reason == ""


# ---------------------------------------------------------------------------
# 命令层
# ---------------------------------------------------------------------------
@pytest.fixture
def seeded(isolated_env: Path):
    """建库 + 一份简历 + 两个岗位，供 CLI 测试用。"""
    assert runner.invoke(app, ["--no-remind", "init"]).exit_code == 0
    engine, factory = _session_factory()
    with factory() as live:
        live.add(
            Resume(
                name="主简历",
                direction="AI Agent",
                content_json=make_content().model_dump_json(),
                content_hash="f" * 64,
                is_default=True,
            )
        )
        live.add(Job(platform="manual", title="AI Agent 开发", city="深圳", jd_text=JD))
        live.add(
            Job(platform="manual", title="前端开发", city="北京", jd_text="熟悉 Vue、CSS、HTML")
        )
        live.commit()
    engine.dispose()
    return isolated_env


def _session_factory():
    from jobhunter.storage.db import get_session_factory

    return get_session_factory(get_settings().database_url)


def test_cli_match_run_no_llm(seeded: Path):
    result = runner.invoke(app, ["--no-remind", "match", "run", "--no-llm"])
    assert result.exit_code == 0, result.output
    assert "规则层结果" in result.output
    assert "不花钱" in result.output


def test_cli_match_run_requires_resume(isolated_env: Path):
    runner.invoke(app, ["--no-remind", "init"])
    result = runner.invoke(app, ["--no-remind", "match", "run", "--no-llm"])
    assert result.exit_code == 1
    assert "jh resume add" in result.output


def test_cli_match_run_with_mock_provider(seeded: Path, monkeypatch):
    """整条 CLI 路径走通（provider=mock，不联网）。"""
    monkeypatch.setenv("JH_LLM_PROVIDER", "mock")
    from jobhunter.config.settings import reset_settings

    reset_settings()
    result = runner.invoke(app, ["--no-remind", "match", "run"])
    assert result.exit_code == 0, result.output
    assert "LLM 精评完成" in result.output
    assert "缓存 key" in result.output


def test_cli_match_explain(seeded: Path):
    runner.invoke(app, ["--no-remind", "match", "run", "--no-llm"])
    result = runner.invoke(app, ["--no-remind", "match", "explain", "1"])
    assert result.exit_code == 0, result.output
    assert "方向" in result.output
    assert "硬条件逐项" in result.output
    assert "合计" in result.output


def test_cli_match_explain_missing_job(seeded: Path):
    result = runner.invoke(app, ["--no-remind", "match", "explain", "999"])
    assert result.exit_code == 1
    assert "找不到岗位" in result.output


def test_cli_match_set(seeded: Path):
    runner.invoke(app, ["--no-remind", "match", "run", "--no-llm"])
    result = runner.invoke(
        app, ["--no-remind", "match", "set", "1", "--resume", "1", "--verdict", "queue"]
    )
    assert result.exit_code == 0, result.output
    assert "queued" in result.output

    engine, factory = _session_factory()
    with factory() as live:
        assert live.get(Job, 1).status == "queued"
        assert live.get(Job, 1).matched_resume_id == 1
    engine.dispose()


def test_cli_match_set_rejects_bad_verdict(seeded: Path):
    result = runner.invoke(app, ["--no-remind", "match", "set", "1", "--verdict", "maybe"])
    assert result.exit_code == 2
    assert "skip 或 queue" in result.output


def test_cli_match_set_needs_an_argument(seeded: Path):
    result = runner.invoke(app, ["--no-remind", "match", "set", "1"])
    assert result.exit_code == 2


def test_cli_gap(seeded: Path):
    runner.invoke(app, ["--no-remind", "match", "run", "--no-llm"])
    result = runner.invoke(app, ["--no-remind", "gap"])
    assert result.exit_code == 0, result.output
    assert "技能缺口" in result.output
    assert "统计完成" in result.output


def test_cli_gap_without_jobs(isolated_env: Path):
    runner.invoke(app, ["--no-remind", "init"])
    result = runner.invoke(app, ["--no-remind", "gap"])
    assert result.exit_code == 1
    assert "还没有岗位" in result.output


def test_cli_gap_min_ratio_filters(seeded: Path):
    runner.invoke(app, ["--no-remind", "match", "run", "--no-llm"])
    result = runner.invoke(app, ["--no-remind", "gap", "--min-ratio", "0.9"])
    assert result.exit_code == 0, result.output


def test_graduation_helpers():
    assert resume_svc.education_end_year("2023.09 - 2027.06") == 2027

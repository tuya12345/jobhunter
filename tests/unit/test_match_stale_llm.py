"""「改了简历，上一次的 LLM 分必须作废」（ISSUE #17）。

★ 这条守的是**用户能看到的东西**，不是内部字段：

    `match_score` 的合成规则是"有 LLM 分就用 LLM 分"。所以只要上一次的
    `llm_score` 还留在库里，规则层重算出来的新 `rule_score` 就永远显示不出来 ——
    用户看到的是"我改了简历，分数纹丝不动"。

    缓存层早就是对的（`llm/cache.py` 的 key 含 `resume.content_hash`），
    漏的是**库里已经落下的那一份**。这个文件专门盯它。

★ 判据是"这次算分用的是哪一版简历"：规则层把当次的 `resume_content_hash`
  写进 `rule_detail_json`，下一次重算时拿来对比（LLM 层要求"先跑规则层"，
  所以那条记录一定早于那次 LLM 调用）。
"""

from __future__ import annotations

from typing import Any

import pytest

from jobhunter.models import Job, Resume
from jobhunter.services import match_llm_svc, match_svc, resume_svc
from jobhunter.services.resume_svc import EducationItem, ProjectItem, ResumeContent
from jobhunter.utils.io import from_json, to_json

JD = (
    "岗位职责：负责 AI Agent 应用开发，基于大模型 API 实现工具调用与多轮对话。\n"
    "任职要求：熟悉 Python、LangChain、RAG，了解 FastAPI。"
)

VERDICT_JSON = '{"score": 93, "reason": "针对当时的简历"}'
NEW_VERDICT_JSON = '{"score": 61, "reason": "改了简历之后重算"}'


def make_content(**overrides: Any) -> ResumeContent:
    base: dict[str, Any] = {
        "basic": {"job_intention": "AI Agent", "city": "深圳"},
        "education": [
            EducationItem(
                school="某某大学", major="软件工程", degree="本科", period="2023.09 - 2027.06"
            )
        ],
        "skills": ["Python", "LangChain", "RAG"],
        "projects": [ProjectItem(name="某某 Agent 平台", skills=["LangChain"])],
    }
    base.update(overrides)
    return ResumeContent(**base)


def add_resume(session, content: ResumeContent | None = None, **overrides: Any) -> Resume:
    """入库一份简历（`content_hash` 用生产同一个函数算，口径同源）。"""
    body = content if content is not None else make_content()
    base: dict[str, Any] = {
        "name": "主简历",
        "direction": "AI Agent",
        "content_json": to_json(body.model_dump(), indent=None),
        "content_hash": resume_svc.compute_content_hash(body),
        "is_default": True,
    }
    base.update(overrides)
    row = Resume(**base)
    session.add(row)
    session.flush()
    return row


def add_job(session, **overrides: Any) -> Job:
    base: dict[str, Any] = {
        "platform": "manual",
        "company": "某某科技",
        "title": "AI Agent 开发工程师",
        "city": "深圳",
        "experience": "经验不限",
        "education": "本科",
        "is_intern": False,
        "jd_text": JD,
        "jd_hash": "j" * 64,
        "status": "new",
    }
    base.update(overrides)
    row = Job(**base)
    session.add(row)
    session.flush()
    return row


def run_rule_layer(session, job: Job, settings) -> match_svc.MatchResult:
    """跑一遍生产路径的规则层（`match_best_candidate` → `apply_result_to_job`）。"""
    candidates = match_svc.load_resume_candidates(session, settings)
    assert candidates, "库里没有简历"
    result = match_svc.match_best_candidate(job.jd_text, candidates, job=job, settings=settings)
    assert result is not None
    match_svc.apply_result_to_job(job, result)
    session.flush()
    return result


def run_llm_layer(session, job: Job, settings, verdict: str = VERDICT_JSON) -> None:
    import asyncio

    from jobhunter.llm.mock import MockProvider

    candidates = match_svc.load_resume_candidates(session, settings)
    asyncio.run(
        match_llm_svc.run_llm_layer(
            session, [job], candidates, provider=MockProvider({"match": verdict}), settings=settings
        )
    )


def edit_resume(session, resume: Resume, *, extra_skill: str = "Kubernetes") -> str:
    """改简历内容并重算 hash（模拟 `jh resume add` 覆盖同一份简历）。"""
    body = make_content(skills=[*make_content().skills, extra_skill])
    resume.content_json = to_json(body.model_dump(), indent=None)
    resume.content_hash = resume_svc.compute_content_hash(body)
    session.flush()
    return resume.content_hash


# ===========================================================================
# 规则层把"用的是哪一版简历"记下来了（后面所有判据的基础）
# ===========================================================================
def test_rule_layer_records_which_resume_version_it_used(session, settings) -> None:
    """没有这条记录就无从判断旧 LLM 分还有没有效 —— 所以它本身也要被测。"""
    resume = add_resume(session)
    job = add_job(session)
    result = run_rule_layer(session, job, settings)

    assert result.resume_content_hash == resume.content_hash
    detail = from_json(job.rule_detail_json, default={})
    assert detail["resume_content_hash"] == resume.content_hash
    assert detail["resume_name"] == "主简历"


# ===========================================================================
# #17 的核心：改了简历 → 旧 LLM 分作废
# ===========================================================================
def test_resume_change_drops_stale_llm_score(session, settings) -> None:
    """改了简历、重跑规则层 → match_score 必须回到新算出来的规则分。"""
    resume = add_resume(session)
    job = add_job(session)
    run_rule_layer(session, job, settings)
    run_llm_layer(session, job, settings)
    assert job.llm_score == 93 and job.match_score == 93

    edit_resume(session, resume)
    run_rule_layer(session, job, settings)

    assert job.llm_score is None, "旧简历算出来的 LLM 分还留在库里"
    assert job.reason_json is None, "理由同样是针对旧简历写的，必须一起清"
    assert job.match_score == job.rule_score, (
        f"改了简历之后 match_score 仍是旧 LLM 分"
        f"（match_score={job.match_score}, rule_score={job.rule_score}）"
    )


def test_resume_change_does_not_touch_rule_score_or_status(session, settings) -> None:
    """作废 LLM 分不能连累规则层的产物。"""
    resume = add_resume(session)
    job = add_job(session)
    run_rule_layer(session, job, settings)
    run_llm_layer(session, job, settings)
    job.status = "applied"
    session.flush()

    edit_resume(session, resume)
    result = run_rule_layer(session, job, settings)

    assert job.rule_score == result.rule_score
    assert from_json(job.rule_detail_json, default={})["version"] == match_svc.RULE_DETAIL_VERSION
    assert job.status == "applied", "重新打分不该把已投的状态改回去"


def test_running_again_after_resume_change_uses_a_fresh_verdict(session, settings) -> None:
    """★ 作废之后紧接着跑 LLM 层 → 拿到的是**针对新简历**的新分。

    这条回答"清了会不会白花钱"：不会。缓存 key 里含 `resume.content_hash`，
    改了简历必然是一次新调用（而没改时是命中缓存、0 成本）。
    """
    resume = add_resume(session)
    job = add_job(session)
    run_rule_layer(session, job, settings)
    run_llm_layer(session, job, settings)

    edit_resume(session, resume)
    run_rule_layer(session, job, settings)
    run_llm_layer(session, job, settings, verdict=NEW_VERDICT_JSON)

    assert job.llm_score == 61
    assert job.match_score == 61
    assert from_json(job.reason_json, default={})["reason"] == "改了简历之后重算"


# ===========================================================================
# 没变时绝不误清（重复跑一次不该烧掉已经花过钱的结果）
# ===========================================================================
def test_unchanged_resume_keeps_the_paid_llm_score(session, settings) -> None:
    """简历一个字都没改 → 重跑规则层必须保留 LLM 分与理由。"""
    add_resume(session)
    job = add_job(session)
    run_rule_layer(session, job, settings)
    run_llm_layer(session, job, settings)
    reason_before = job.reason_json

    run_rule_layer(session, job, settings)
    run_rule_layer(session, job, settings)  # 跑两遍，确认不是"只有第一次才对"

    assert job.llm_score == 93, "没改简历却把 LLM 分清了（重复跑一次就白花钱）"
    assert job.match_score == 93
    assert job.reason_json == reason_before, "没改简历却把理由清了"


def test_job_without_llm_score_is_untouched(session, settings) -> None:
    """从没跑过 LLM 的岗位：`apply_result_to_job` 不该有任何"作废"动作。"""
    add_resume(session)
    job = add_job(session)
    run_rule_layer(session, job, settings)
    assert job.llm_score is None
    assert job.reason_json is None
    assert job.match_score == job.rule_score


# ===========================================================================
# 另外两种"失效"情况
# ===========================================================================
def test_switching_matched_resume_drops_llm_score(session) -> None:
    """换了参照的简历记录（`matched_resume_id` 变了）→ 旧 LLM 分作废。

    这里直接把两次规则层结果摆在一起比对（不依赖"哪份简历分更高"），
    免得测试因为权重调整而误红。
    """
    other = add_resume(session, name="另一份简历", direction="后端")
    job = add_job(session)
    job.rule_detail_json = to_json(
        {"version": match_svc.RULE_DETAIL_VERSION, "resume_content_hash": other.content_hash}
    )
    job.matched_resume_id = other.id
    job.llm_score = 88
    job.match_score = 88
    job.reason_json = to_json({"reason": "针对另一份简历"})
    session.flush()

    result = match_svc.MatchResult(
        rule_score=70,
        rule_detail={"version": match_svc.RULE_DETAIL_VERSION, "resume_content_hash": "h" * 64},
        matched_resume_id=other.id + 999,  # 换了记录（哪怕内容 hash 恰好相同）
        resume_content_hash="h" * 64,
    )
    match_svc.apply_result_to_job(job, result)

    assert job.llm_score is None and job.reason_json is None
    assert job.match_score == 70


@pytest.mark.parametrize(
    "legacy_detail",
    [
        None,  # 从来没走过规则层
        {"version": 1, "parts": {}},  # 旧版本软件写的明细：没有简历版本字段
        {"version": 1, "resume_content_hash": ""},  # 字段在但取不到值
    ],
)
def test_unverifiable_provenance_is_treated_as_stale(session, legacy_detail) -> None:
    """★ 版本无从查证的 LLM 分：不作结论（宁可重算，也不留一个不知道来源的分）。

    留着它的代价是**静默显示一个错的分数**（用户没有任何办法发现）；
    清掉的代价几乎为零 —— `jh match run` 紧接着跑 LLM 层，
    缓存 key 没变时直接命中缓存，0 成本写回同一个分（`llm/cache.py` 的口径）。
    """
    add_resume(session)
    job = add_job(session)
    job.rule_detail_json = None if legacy_detail is None else to_json(legacy_detail)
    job.llm_score = 88
    job.match_score = 88
    job.reason_json = to_json({"reason": "不知道是针对哪一版简历算的"})
    session.flush()

    result = match_svc.MatchResult(
        rule_score=64,
        rule_detail={"version": match_svc.RULE_DETAIL_VERSION, "resume_content_hash": "n" * 64},
        matched_resume_id=None,
        resume_content_hash="n" * 64,
    )
    match_svc.apply_result_to_job(job, result)

    assert job.llm_score is None, "无从查证的 LLM 分被当成了结论"
    assert job.match_score == 64

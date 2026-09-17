"""开场白生成 + 防编造校验的单测（P4）。"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from jobhunter.config.settings import AppSettings
from jobhunter.llm.mock import MockProvider
from jobhunter.models import Job, Resume
from jobhunter.services import fidelity, greeting_svc, match_svc, resume_svc
from jobhunter.services.resume_svc import EducationItem, ProjectItem, ResumeContent


def make_content(**overrides: Any) -> ResumeContent:
    base: dict[str, Any] = {
        "basic": {"job_intention": "AI Agent"},
        "education": [
            EducationItem(
                school="某某大学", major="软件工程", degree="本科", period="2023.09 - 2027.06"
            )
        ],
        "skills": ["Python", "LangChain", "RAG"],
        "projects": [
            ProjectItem(
                name="Agent 学习平台",
                role="全栈",
                period="2025.03 - 2025.09",
                bullets=["用 LangChain 做了 RAG 问答"],
                skills=["LangChain", "RAG", "Python"],
            )
        ],
    }
    base.update(overrides)
    return ResumeContent(**base)


@pytest.fixture
def candidate(session: Session, settings: AppSettings) -> match_svc.ResumeCandidate:
    row = Resume(
        name="主简历",
        direction="AI Agent",
        content_json=make_content().model_dump_json(),
        content_hash="a" * 64,
        is_default=True,
    )
    session.add(row)
    session.flush()
    return match_svc.load_resume_candidates(session, settings)[0]


def make_job(**overrides: Any) -> Job:
    base: dict[str, Any] = {
        "platform": "manual",
        "company": "某某科技",
        "title": "AI Agent 开发工程师",
        "city": "深圳",
        "jd_text": "负责 AI Agent 开发，要求熟悉 Python、LangChain、RAG。",
        "jd_hash": "j" * 64,
        "status": "scored",
        "match_score": 80,
        "jd_keywords_json": '["Python", "LangChain", "RAG"]',
    }
    base.update(overrides)
    return Job(**base)


# ===========================================================================
# fidelity：防编造
# ===========================================================================
def test_clean_text_passes(settings):
    report = fidelity.check_text(
        "我做过 RAG 问答，用的是 LangChain。",
        resume=make_content(),
        learning_whitelist={"Kubernetes"},
    )
    assert report.passed, report.summary()
    assert "RAG" in report.mentioned_skills


def test_fabricated_skill_is_caught(settings):
    report = fidelity.check_text("我熟悉 Kubernetes 与 Docker。", resume=make_content())
    assert not report.passed
    assert {item.token for item in report.violations} == {"Kubernetes", "Docker"}
    assert all(item.kind == "skill_not_in_resume" for item in report.violations)


def test_learning_whitelist_needs_annotation(settings):
    """白名单技能可以提，但必须带"在学习"标注 —— 这正是白名单存在的意义。"""
    ok = fidelity.check_text(
        "Kubernetes 我正在学习。", resume=make_content(), learning_whitelist={"Kubernetes"}
    )
    assert ok.passed, ok.summary()
    bad = fidelity.check_text(
        "我熟悉 Kubernetes。", resume=make_content(), learning_whitelist={"Kubernetes"}
    )
    assert not bad.passed


def test_skill_outside_allowed_set_is_caught(settings):
    """开场白只能提 JD 要求的关键词（沾边），提别的等于跑题。"""
    report = fidelity.check_text(
        "我在项目里用过 Python 和 LangChain。",
        resume=make_content(),
        allowed_skills={"RAG"},
    )
    assert not report.passed
    assert {item.kind for item in report.violations} == {"skill_not_in_jd"}


@pytest.mark.parametrize(
    ("text", "should_pass"),
    [
        ("我把接口耗时降了 300%。", False),
        ("做过 1 个项目。", True),  # len(projects) == 1，是真话
        ("做过 2 个项目。", False),  # 数量对不上
        ("负责过 5 个模块。", False),  # "模块"不是映射过的名词 → 不给证据
        ("有 3 项技能。", True),  # len(skills) == 3
        ("有 3 年经验。", False),  # ★ 数字 3 恰好等于技能数，但名词是"年" → 不算证据
    ],
)
def test_number_claims(text, should_pass):
    """★ 最后那两条是本文件最要紧的断言：

    把"可推导计数"（len(skills) == 3）无条件当成证据，会让"我有 3 年经验"
    被判成真话 —— 数字对上了，含义完全不对。所以计数只在**名词匹配**时才算证据。
    """
    report = fidelity.check_text(text, resume=make_content())
    assert report.passed is should_pass, report.summary()


def test_degree_claim_is_caught():
    report = fidelity.check_text("我是硕士学历。", resume=make_content())
    assert not report.passed
    assert report.violations[0].kind == "degree_not_held"
    assert fidelity.check_text("我是本科学历。", resume=make_content()).passed


def test_education_without_degree_does_not_crash():
    """★ `EducationItem` 没有 `raw_line` 字段 —— 曾经在 match_svc 里写过它。

    这条守的是"没解析出学历的那条教育经历"这种真实边界数据：
    以前会 AttributeError（因为 `item.degree` 非空时短路才没暴露）。
    """
    content = make_content(education=[EducationItem(school="某某大学", major="软件工程")])
    assert fidelity.check_text("我做过 RAG。", resume=content).passed
    report = fidelity.check_text("我是硕士。", resume=content)
    assert not report.passed


# ===========================================================================
# greeting：四道闸门 + 模板兜底
# ===========================================================================
def test_template_always_passes_gates(settings, candidate):
    """模板是兜底，它自己必须先满足全部硬约束 —— 否则兜底本身成了漏洞。"""
    job = make_job()
    for variant in greeting_svc.VARIANTS:
        result = greeting_svc.generate_greeting(job, candidate, settings=settings, variant=variant)
        assert result.source == "template"
        assert not result.issues, result.issues
        assert (
            settings.greeting.full_min_chars <= len(result.full) <= settings.greeting.full_max_chars
        )
        assert len(result.short) <= settings.greeting.short_max_chars
        assert "Python" in result.full  # 沾边
        assert result.full.rstrip().endswith("？")


def test_template_without_keywords_still_works(settings, candidate):
    """JD 一个关键词都没有时也要能生成（不能说"无法生成"就完事）。"""
    result = greeting_svc.generate_greeting(
        make_job(jd_keywords_json="[]"), candidate, settings=settings
    )
    assert result.full and result.short
    assert not result.issues


def test_mock_greeting_is_rejected_by_gates(settings, candidate, session):
    """MockProvider 的默认话术只有 41 字且没有问句 → 必须被闸门拦下并降级。"""
    job = make_job()
    result = greeting_svc.generate_greeting(
        job, candidate, session=session, provider=MockProvider(), settings=settings
    )
    assert result.source == "template"
    assert any("字数" in issue for issue in result.issues)
    assert any("问句" in issue for issue in result.issues)


GOOD_FULL = (
    "您好，我是 AI Agent 方向的应届生，在 Agent 学习平台里用 LangChain 做过 RAG 问答，"
    "从切块、向量化到重排都自己写过一遍，也做过提示词调试。方便的话，"
    "想请教一下这个岗位目前更看重哪一块？"
)
GOOD_SHORT = "AI Agent 方向，LangChain 与 RAG 有项目经验，方便聊聊吗？"


def _good_greeting(**overrides: Any) -> str:
    """一份"四道闸门都过"的模型输出（JSON 字符串）。"""
    import json

    payload = {"full": GOOD_FULL, "short": GOOD_SHORT}
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def test_good_llm_greeting_passes(settings, candidate, session):
    job = make_job()
    result = greeting_svc.generate_greeting(
        job,
        candidate,
        session=session,
        provider=MockProvider({"greeting": _good_greeting()}),
        settings=settings,
    )
    assert result.source == "llm", result.issues
    assert not result.issues
    assert len(result.full) <= settings.greeting.full_max_chars


def test_direction_word_is_not_a_fabrication(settings, candidate, session):
    """★ 方向词（"AI Agent 方向"）不是技能声称 —— 词表会把 "Agent" 抽成技能。

    不排除它，任何开场白都会因为说了自己的方向而被判编造（误报）。
    """
    assert "Agent" not in candidate.pool
    result = greeting_svc.generate_greeting(make_job(), candidate, settings=settings)
    assert candidate.direction in result.full
    assert not result.issues


def test_banned_word_falls_back(settings, candidate, session):
    job = make_job()
    text = _good_greeting(full=_good_greeting().replace("您好", "贵司您好"))
    result = greeting_svc.generate_greeting(
        job,
        candidate,
        session=session,
        provider=MockProvider({"greeting": text}),
        settings=settings,
    )
    assert result.source == "template"
    assert any("禁用词" in issue for issue in result.issues)


def test_fabricated_skill_in_llm_output_falls_back(settings, candidate, session):
    """模型写了简历里没有的技能 → 整版降级（这是发出去会出事的那种）。"""
    job = make_job()
    bad = _good_greeting(
        full=(
            "您好，我在项目里用 Kubernetes 做过容器编排，也做过 RAG 问答，"
            "熟悉 LangChain 的链路搭建与提示词调试，能快速上手你们的业务。"
            "方便的话，想请教一下这个岗位目前更看重哪一块？"
        )
    )
    result = greeting_svc.generate_greeting(
        job, candidate, session=session, provider=MockProvider({"greeting": bad}), settings=settings
    )
    assert result.source == "template"
    assert any("Kubernetes" in issue for issue in result.issues)


def test_bad_json_falls_back(settings, candidate, session):
    job = make_job()
    result = greeting_svc.generate_greeting(
        job,
        candidate,
        session=session,
        provider=MockProvider({"greeting": "不是 json"}),
        settings=settings,
    )
    assert result.source == "template"
    assert any("解析失败" in issue for issue in result.issues)


def test_partial_fallback_keeps_the_good_version(settings, candidate, session):
    """只有一版不合格时，**另一版仍然用模型输出**（不做无谓的整体降级）。"""
    job = make_job()
    text = _good_greeting(short="太短")
    result = greeting_svc.generate_greeting(
        job,
        candidate,
        session=session,
        provider=MockProvider({"greeting": text}),
        settings=settings,
    )
    assert result.full == GOOD_FULL  # 完整版是模型的
    assert result.short != "太短"  # 精简版降级
    assert any(issue.startswith("short") for issue in result.issues)


# ===========================================================================
# 缓存
# ===========================================================================
def test_greeting_cache_avoids_second_call(settings, candidate, session):
    job = make_job()
    provider = MockProvider({"greeting": _good_greeting()})
    greeting_svc.generate_greeting(
        job, candidate, session=session, provider=provider, settings=settings
    )
    greeting_svc.generate_greeting(
        job, candidate, session=session, provider=provider, settings=settings
    )
    assert len(provider.calls) == 1


def test_greeting_cache_key_differs_from_match(session, candidate):
    """★ match 与 greeting 的 key 必须不同 —— 否则开场白会读到匹配的 JSON。"""
    from jobhunter.llm import cache as llm_cache

    common = {
        "prompt_version": 1,
        "model": "m",
        "jd_hash": "j" * 64,
        "resume_content_hash": "a" * 64,
    }
    assert llm_cache.compute_cache_key(**common, task="match") != llm_cache.compute_cache_key(
        **common, task="greeting"
    )
    # 不传 task 时与老行为一致（向后兼容，别把已有缓存全废掉）
    assert llm_cache.compute_cache_key(**common) == llm_cache.compute_cache_key(**common)


def test_greeting_only_uses_overlap_keywords(settings, candidate):
    """交集为空时不得凭空提 JD 关键词（宁可少说，不可乱说）。"""
    job = make_job(jd_keywords_json='["Kubernetes"]')  # 简历里没有
    result = greeting_svc.generate_greeting(job, candidate, settings=settings)
    assert "Kubernetes" not in result.full


def test_pick_variant_alternates_and_respects_forced():
    assert greeting_svc.pick_variant(0) != greeting_svc.pick_variant(1)
    assert greeting_svc.pick_variant(0, forced="without_project") == "without_project"
    assert greeting_svc.pick_variant(7, forced="with_project") == "with_project"


def test_vocabulary_pool_helper_is_shared(settings):
    """`resume_skill_pool` 的实现搬到了 resume_svc，match_svc 的同名函数必须转发到同一结果。"""
    content = make_content()
    assert resume_svc.resume_skill_pool(content) == match_svc.resume_skill_pool(content)
    assert resume_svc.resume_skill_pool(content)[0] >= {"Python", "LangChain", "RAG"}

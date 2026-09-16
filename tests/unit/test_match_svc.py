"""规则层匹配测试（P3）。"""

from __future__ import annotations

from typing import Any

import pytest

from jobhunter.models import Job, Resume
from jobhunter.services import match_svc, resume_svc
from jobhunter.services.resume_svc import EducationItem, ProjectItem, ResumeContent
from jobhunter.utils.io import to_json


def make_resume(**overrides: Any) -> ResumeContent:
    base: dict[str, Any] = {
        "basic": {"job_intention": "AI Agent 开发", "city": "深圳"},
        "education": [
            EducationItem(
                school="某某大学",
                major="软件工程",
                degree="本科",
                period="2023.09 - 2027.06",
            )
        ],
        "skills": ["Python", "LangChain", "RAG", "FastAPI", "MySQL", "JavaScript"],
        "projects": [
            ProjectItem(
                name="AI Agent 全栈学习平台",
                role="全栈",
                period="2025.03 - 2025.09",
                bullets=["用 LangChain + RAG 做了一个问答助手"],
                skills=["LangChain", "RAG", "Python"],
            )
        ],
    }
    base.update(overrides)
    return ResumeContent(**base)


def make_job(**overrides: Any) -> Job:
    base: dict[str, Any] = {
        "platform": "manual",
        "company": "某某科技",
        "title": "AI Agent 开发工程师",
        "city": "深圳",
        "experience": "经验不限",
        "education": "本科",
        "is_intern": False,
        "jd_text": "岗位职责：负责 AI Agent 应用开发，要求熟悉 Python、LangChain、RAG。",
    }
    base.update(overrides)
    return Job(**base)


@pytest.fixture
def profiles(settings):
    return resume_svc.load_direction_profiles(settings)


@pytest.fixture
def vocabulary(settings):
    return resume_svc.load_skill_vocabulary(settings)


def match(jd_text: str, resume: ResumeContent, *, job: Job | None = None, settings, **kw):
    return match_svc.match_jd_resume(
        jd_text,
        resume,
        resume_id=1,
        resume_name="主简历",
        resume_direction="AI Agent",
        job=job,
        settings=settings,
        **kw,
    )


# ---------------------------------------------------------------------------
# 排序：高 / 中 / 低三个假岗位
# ---------------------------------------------------------------------------
def test_ranking_high_mid_low(settings, profiles, vocabulary):
    resume = make_resume()
    high = match(
        "负责 AI Agent 开发，熟悉 Python、LangChain、RAG、FastAPI、MySQL",
        resume,
        settings=settings,
        profiles=profiles,
        vocabulary=vocabulary,
    )
    mid = match(
        "负责大模型应用开发，熟悉 Python、RAG、Docker",
        resume,
        settings=settings,
        profiles=profiles,
        vocabulary=vocabulary,
    )
    low = match(
        "负责前端页面开发，熟悉 Vue、CSS、Webpack、响应式布局",
        resume,
        settings=settings,
        profiles=profiles,
        vocabulary=vocabulary,
    )
    assert high.rule_score > mid.rule_score > low.rule_score
    assert high.direction == "AI Agent"
    assert low.direction == "前端开发"


def test_low_score_still_returned_not_eliminated(settings, profiles, vocabulary):
    """硬条件不满足 + 方向不符 → 分很低，但**仍然有分数**（不淘汰）。"""
    resume = make_resume()
    result = match(
        "前端开发，要求 5 年以上经验，熟悉 Vue、React",
        resume,
        job=make_job(experience="5年以上", title="资深前端"),
        settings=settings,
        profiles=profiles,
        vocabulary=vocabulary,
    )
    assert 0 <= result.rule_score < 60
    checks = {item["name"]: item for item in result.rule_detail["parts"]["hard"]["checks"]}
    assert checks["经验"]["ratio"] == 0.2
    assert result.rule_detail["parts"]["direction"]["ratio"] == match_svc.DIRECTION_MISMATCH_RATIO


# ---------------------------------------------------------------------------
# 技能归一化（词边界）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("jd", "must_have", "must_not_have"),
    [
        ("熟悉 JavaScript 与前端工程化", "JavaScript", "Java"),
        ("使用 Google Analytics 分析数据", None, "Go"),
        # Vue3 / TypeScript5 必须命中（版本号是数字，右边界只排除字母）
        ("熟悉 Vue3 + TypeScript5", "Vue", None),
        ("做过 RAG 检索增强", "RAG", "Go"),
    ],
)
def test_jd_keyword_word_boundary(settings, vocabulary, jd, must_have, must_not_have):
    keywords = match_svc.extract_jd_keywords(jd, settings, vocabulary=vocabulary)
    if must_have:
        assert must_have in keywords
    if must_not_have:
        assert must_not_have not in keywords


def test_version_suffix_skills_match(settings, vocabulary):
    keywords = match_svc.extract_jd_keywords("Vue3 + TypeScript5", settings, vocabulary=vocabulary)
    assert "Vue" in keywords
    assert "TypeScript" in keywords


def test_resume_pool_ignores_raw_text(settings):
    """raw_text 里的技能不算"我会"（避免把"我在学"算成会）。"""
    resume = make_resume(skills=["Python"], projects=[], raw_text="我正在学 Kubernetes")
    pool, explicit = match_svc.resume_skill_pool(resume)
    assert "Python" in pool
    assert "Kubernetes" not in pool
    assert explicit == {"Python"}


# ---------------------------------------------------------------------------
# 权重再分配
# ---------------------------------------------------------------------------
def test_skill_dimension_dropped_when_no_keywords(settings, profiles, vocabulary):
    """JD 里没有词表内技能 → 技能维度判不出来，权重让给其余维度。"""
    resume = make_resume()
    result = match(
        "我们是一家有活力的公司，欢迎加入我们。",
        resume,
        settings=settings,
        profiles=profiles,
        vocabulary=vocabulary,
    )
    skill = result.rule_detail["parts"]["skill"]
    assert skill["ratio"] is None
    assert skill["points"] == 0
    assert result.rule_detail["redistributed"] is True
    assert 0 < result.rule_score <= 100
    # 其余两维度的实际权重之和应接近 100
    effective = sum(part["effective_weight"] for part in result.rule_detail["parts"].values())
    assert abs(effective - 100) < 0.01


def test_weights_from_config_are_used(settings, config_file, profiles, vocabulary):
    config_file.write_text(
        "match_weights:\n  direction: 10\n  hard: 10\n  skill: 80\n",
        encoding="utf-8",
    )
    from jobhunter.config.settings import get_settings, reset_settings

    reset_settings()
    custom = get_settings()
    resume = make_resume()
    result = match_svc.match_jd_resume(
        "熟悉 Python、LangChain、RAG",
        resume,
        resume_id=1,
        job=make_job(),
        settings=custom,
    )
    assert result.rule_detail["weights"] == {"direction": 10, "hard": 10, "skill": 80}


def test_rule_detail_has_contract_keys(settings, profiles, vocabulary):
    result = match("熟悉 Python 与 RAG", make_resume(), settings=settings)
    detail = result.rule_detail
    for key in ("direction", "hard", "skill", "total"):
        assert isinstance(detail[key], int | float)
    assert detail["version"] == match_svc.RULE_DETAIL_VERSION


def test_short_jd_does_not_get_free_full_marks(settings):
    """★ JD 只抽出 1~2 个技能词时不该拿技能满分（P3 验收实测出的问题）。

    实测案例：一个"测试开发工程师"的 JD 只抽到 1 个词（Python），
    命中 1 个 → 覆盖率 100% → 技能拿满 40 分，排名超过了核心的 AI Agent 岗。
    现在用证据下限把这类 JD 的分母抬到 5。
    """
    short = match("熟悉 Python", make_resume(), settings=settings)
    skill = short.rule_detail["parts"]["skill"]
    assert skill["coverage"] == 1.0  # 原始覆盖率确实是 100%
    assert skill["ratio"] == pytest.approx(1 / match_svc._SKILL_EVIDENCE_FLOOR)
    assert skill["points"] < 40

    long_jd = match(
        "熟悉 Python、LangChain、RAG、FastAPI、MySQL",
        make_resume(),
        settings=settings,
    )
    long_skill = long_jd.rule_detail["parts"]["skill"]
    assert long_skill["ratio"] == 1.0  # 5 项以上就不打折
    assert long_skill["ratio"] > skill["ratio"]


# ---------------------------------------------------------------------------
# 硬条件逐项
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("experience", "expect_ratio"),
    [
        ("经验不限", 1.0),
        ("1年以内", 1.0),
        ("2年", 0.7),
        ("3-5年", 0.55),  # 取最小年数 3：3 年经验就能进的岗位不该按 5 年算
        ("5年以上", 0.2),
    ],
)
def test_experience_ladder(settings, profiles, vocabulary, experience, expect_ratio):
    job = make_job(experience=experience)
    result = match("Python", make_resume(), job=job, settings=settings)
    checks = {item["name"]: item for item in result.rule_detail["parts"]["hard"]["checks"]}
    assert checks["经验"]["ratio"] == expect_ratio


def test_job_type_conflict_is_heavy(settings, config_file, profiles, vocabulary):
    config_file.write_text("job_type: intern\n", encoding="utf-8")
    from jobhunter.config.settings import get_settings, reset_settings

    reset_settings()
    custom = get_settings()
    job = make_job(is_intern=False)
    result = match_svc.match_jd_resume("Python", make_resume(), job=job, settings=custom)
    checks = {item["name"]: item for item in result.rule_detail["parts"]["hard"]["checks"]}
    assert checks["实习/全职"]["ratio"] == match_svc.HARD_CONFLICT_RATIO


def test_city_out_of_preference_is_mild(settings, profiles, vocabulary):
    job = make_job(city="北京")
    result = match("Python", make_resume(), job=job, settings=settings)
    checks = {item["name"]: item for item in result.rule_detail["parts"]["hard"]["checks"]}
    assert checks["城市"]["ratio"] == match_svc.CITY_OTHER_RATIO


def test_graduation_year_from_period(settings):
    resume = make_resume()
    assert match_svc._graduation_year(resume) == 2027
    assert resume_svc.education_end_year("2023.09 - 2027.06") == 2027
    assert resume_svc.education_end_year("2023.09 至今") is None
    assert resume_svc.education_end_year("") is None


# ---------------------------------------------------------------------------
# explain / 落库
# ---------------------------------------------------------------------------
def test_explain_lines_cover_three_dimensions(settings, session):
    job = make_job()
    result = match("熟悉 Python、RAG、Docker", make_resume(), job=job, settings=settings)
    match_svc.apply_result_to_job(job, result)
    lines = match_svc.explain_lines(job)
    text = "\n".join(lines)
    assert "方向" in text and "硬条件" in text and "技能" in text
    assert "合计" in text
    assert "Docker" in text


def test_apply_result_sets_status_and_score(settings):
    job = make_job()
    result = match("Python", make_resume(), job=job, settings=settings)
    match_svc.apply_result_to_job(job, result)
    assert job.rule_score == result.rule_score
    assert job.match_score == result.rule_score
    assert job.status == "scored"
    assert match_svc.decode_keywords(job.jd_keywords_json)


def test_apply_result_does_not_revive_applied_job(settings):
    job = make_job(status="applied")
    match_svc.apply_result_to_job(job, match("Python", make_resume(), job=job, settings=settings))
    assert job.status == "applied"


def test_explain_lines_on_unscored_job():
    assert "jh match run" in match_svc.explain_lines(make_job())[0]


def test_explain_lines_on_old_version(settings):
    job = make_job()
    job.rule_detail_json = to_json({"version": 0, "parts": {}})
    assert "旧格式" in match_svc.explain_lines(job)[0]


# ---------------------------------------------------------------------------
# 多份简历：挑最合适的
# ---------------------------------------------------------------------------
def test_pick_best_resume(session, settings):
    agent = Resume(
        name="AI Agent 版",
        direction="AI Agent",
        content_json=make_resume().model_dump_json(),
        content_hash="a" * 64,
        is_default=True,
    )
    frontend = Resume(
        name="前端版",
        direction="前端开发",
        content_json=make_resume(
            skills=["JavaScript", "CSS", "HTML", "Vue", "React"],
            projects=[],
            basic={"job_intention": "前端开发"},
        ).model_dump_json(),
        content_hash="b" * 64,
    )
    session.add_all([agent, frontend])
    session.flush()

    candidates = match_svc.load_resume_candidates(session, settings)
    result = match_svc.match_best_candidate(
        "负责前端页面开发，熟悉 Vue、React、CSS、HTML、JavaScript",
        candidates,
        job=make_job(title="前端开发工程师"),
        settings=settings,
    )
    assert result is not None
    assert result.matched_resume_id == frontend.id
    assert result.resume_direction == "前端开发"


# ---------------------------------------------------------------------------
# jh gap 聚合
# ---------------------------------------------------------------------------
def _seed_jobs(session, jds: list[str], settings) -> None:
    vocabulary = resume_svc.load_skill_vocabulary(settings)
    for index, jd in enumerate(jds, start=1):
        job = make_job(title=f"岗位{index}", jd_text=jd)
        job.jd_keywords_json = to_json(
            match_svc.extract_jd_keywords(jd, settings, vocabulary=vocabulary)
        )
        session.add(job)
    session.flush()


def test_gap_aggregates_and_prioritizes(session, settings):
    resume = Resume(
        name="主简历",
        direction="AI Agent",
        # projects 也要清空：默认 fixture 的项目里写着 LangChain，
        # 留着会让"我没有 LangChain"这个前提不成立
        content_json=make_resume(skills=["Python", "RAG"], projects=[]).model_dump_json(),
        content_hash="c" * 64,
        is_default=True,
    )
    session.add(resume)
    _seed_jobs(
        session,
        [
            "熟悉 Python、RAG、LangChain",
            "熟悉 Python、LangChain、Docker",
            "熟悉 LangChain、Docker",
            "熟悉 LangChain",
        ],
        settings,
    )

    report = match_svc.analyze_gaps(session, settings=settings)
    assert report.analyzed_jobs == 4
    by_name = {row.keyword: row for row in report.rows}

    assert by_name["LangChain"].job_count == 4
    assert by_name["LangChain"].ratio == 1.0
    assert by_name["LangChain"].have is False
    assert by_name["LangChain"].priority == "high"

    assert by_name["Python"].have is True
    assert by_name["Python"].priority == ""

    # 排序：缺口在前，且按比例降序
    priorities = [row.priority for row in report.rows]
    assert priorities == sorted(
        priorities, key=lambda p: {"high": 0, "medium": 1, "low": 2, "": 3}[p]
    )


def test_gap_without_keywords_falls_back_to_jd_text(session, settings):
    session.add(
        Resume(
            name="主简历",
            content_json=make_resume(skills=["Python"]).model_dump_json(),
            content_hash="d" * 64,
            is_default=True,
        )
    )
    session.add(make_job(jd_text="熟悉 Python 与 LangChain"))  # 没跑过 match
    session.flush()

    report = match_svc.analyze_gaps(session, settings=settings)
    assert report.analyzed_jobs == 1
    assert {row.keyword for row in report.rows} >= {"Python", "LangChain"}


def test_gap_marks_only_mentioned_skills(session, settings):
    """只在项目描述里出现、技能栏没列 → only_mentioned=True（零成本提分项）。"""
    session.add(
        Resume(
            name="主简历",
            content_json=make_resume(skills=["Python"]).model_dump_json(),
            content_hash="e" * 64,
            is_default=True,
        )
    )
    _seed_jobs(session, ["熟悉 LangChain"], settings)
    report = match_svc.analyze_gaps(session, settings=settings)
    row = next(item for item in report.rows if item.keyword == "LangChain")
    assert row.have is True
    assert row.only_mentioned is True


# ---------------------------------------------------------------------------
# 方向判定
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("title", "expect"),
    [
        ("AI Agent 开发工程师", "AI Agent"),
        ("前端开发工程师", "前端开发"),
        ("Python 后端开发", "Python 开发"),
        ("产品经理", ""),
    ],
)
def test_detect_direction(settings, profiles, title, expect):
    label, _hits = match_svc.detect_direction(title, settings, profiles=profiles)
    assert label == expect

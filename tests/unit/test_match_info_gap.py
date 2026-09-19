"""ISSUE #9：**没有信息量的 JD 不许拿到分数线以上**。

★ 这条修的是"整份 JD 的信息量"，不是某个维度（验证会话在用例里点明了这一层）：

    权重再分配规则本来是对的 —— 某一维度判不出来就把它的权重让给其余维度
    （总比"凭空少 30 分"或"编造一半匹配"强）。但当**整份 JD 什么信息都没有**时，
    这条规则会得出一个荒唐结论：技能判不出来（让掉 40）、硬条件判不出来（让掉 30）、
    方向词也没抽到（给中性分）→ 只剩中性分，可它恰好落在分数线之上，
    于是"什么都没写的岗位"排进了待投清单。

  这和修过的"JD 写得越少技能分越高"是同一类问题的更大尺度复发：
  不是某个维度，而是**整份 JD 的信息量**。

★ 但**不能一刀切**：真实 JD 必须照常打分，否则就是把功能修坏。
  下面第三条用例专门守这一点。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from jobhunter.models import Job, Resume
from jobhunter.services import match_svc
from jobhunter.services.resume_svc import compute_content_hash

from .test_tailor_svc import fake_content


def _seed(session: Session) -> tuple[Resume, Job]:
    content = fake_content()
    resume = Resume(
        name="主简历",
        direction="Python 后端开发",
        content_json=content.model_dump_json(),
        content_hash=compute_content_hash(content),
        is_default=True,
    )
    job = Job(
        platform="manual",
        company="某某科技",
        title="Python 后端开发实习生",
        city="深圳",
        jd_hash="u" * 64,
        status="new",
    )
    session.add_all([resume, job])
    session.commit()
    return resume, job


def _score(session: Session, settings, job: Job, **overrides: Any) -> match_svc.MatchResult:
    for key, value in overrides.items():
        setattr(job, key, value)
    session.flush()
    candidates = match_svc.load_resume_candidates(session, settings)
    result = match_svc.match_best_candidate(job.jd_text, candidates, job=job, settings=settings)
    assert result is not None
    return result


def test_empty_jd_does_not_reach_the_threshold(session, settings) -> None:
    """依据：ISSUE #9 的验证用例 —— 一个**连正文都没有**的岗位不该达到分数线。"""
    _resume, job = _seed(session)
    result = _score(session, settings, job, jd_text="", title="", company="")

    assert result.rule_score < settings.threshold, (
        f"空 JD 拿到了 {result.rule_score} 分（分数线 {settings.threshold}）—— 它会进待投清单"
    )
    joined = " ".join(str(note) for note in result.rule_detail.get("notes", []))
    assert "信息" in joined, f"分维度说明里要讲清「为什么这个分不能信」：{joined}"


def test_jd_with_text_but_nothing_extractable_also_scores_low(session, settings) -> None:
    """有正文但**抽不出任何东西**（技能/方向/硬条件全无）同样算"信息不足"。

    ★ 这条是我加的（验证会话只覆盖了空 JD）：真实世界里更常见的是
      "我们是一家很好的公司，欢迎加入"这类没有任何可判断信息的 JD。
    """
    _resume, job = _seed(session)
    result = _score(
        session,
        settings,
        job,
        jd_text="我们是一家很好的公司，欢迎加入我们，一起做有意思的事情。",
        title="",
        company="",
    )
    assert result.rule_score < settings.threshold


def test_a_real_jd_still_scores_normally(session, settings) -> None:
    """★ 反向守卫：真实 JD 必须照常打分（防止"一刀切把所有岗位打 0"）。"""
    _resume, job = _seed(session)
    result = _score(
        session,
        settings,
        job,
        jd_text="要求熟悉 Python、FastAPI、MySQL，了解 Redis 缓存，有后端项目经验优先。",
        jd_keywords_json='["Python", "FastAPI", "MySQL", "Redis"]',
    )
    assert result.rule_score > 0, "真实 JD 被判成'信息不足'了 —— 修过头了"
    assert result.matched_keywords, "真实 JD 应当抽到技能"


def test_information_gap_is_flagged_in_rule_detail(session, settings) -> None:
    """信息不足这件事要**落进 rule_detail**，让 `jh match explain` 与 `jh today` 都能显示出来。

    （只说一句"0 分"用户会以为是 bug；说"JD 里没有任何可判断的信息"才是可行动的。）
    """
    _resume, job = _seed(session)
    result = _score(session, settings, job, jd_text="", title="", company="")
    detail = result.rule_detail
    assert detail.get("insufficient_info") is True, f"缺少 insufficient_info 标记：{detail.keys()}"


@pytest.mark.parametrize("field", ["jd_text", "title"])
def test_gap_clears_once_any_information_appears(session, settings, field: str) -> None:
    """只要补上一点信息（哪怕只是个标题），就不该再判"信息不足"。"""
    _resume, job = _seed(session)
    overrides: dict[str, Any] = {"jd_text": "", "title": "", "company": ""}
    overrides[field] = "Python 后端开发"
    result = _score(session, settings, job, **overrides)
    assert result.rule_detail.get("insufficient_info") is not True

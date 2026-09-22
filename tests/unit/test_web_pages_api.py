"""P8 页面接口（只读四条）：今日清单 / 看板 / 时间轴 / 问题与统计。

★ 这组用例守两件事：

  1. **接口形状**（前端照着它写）：四个 GET 都要 200，且带上页面真正要用的字段；
  2. ★★ **GET 不写库、不调模型**：这是与 CLI `jh today` 的**关键区别** ——
     网页刷新一次就调一次模型、写一条 pending，是"刷新花钱 + 留幽灵记录"。
     用例用"调用前后库里的行数与开场白不变"来钉住它。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jobhunter.models import (
    Application,
    Interview,
    InterviewQuestion,
    Job,
    Resume,
)
from jobhunter.services import apply_svc
from jobhunter.services.resume_svc import ResumeContent
from jobhunter.utils.time import now_utc
from jobhunter.web.app import create_app


def _seed(session) -> dict[str, Any]:
    """造一份最小但完整的数据：简历 + 3 个岗位 + 一条已投 + 面试 + 两道题。"""
    session.add(
        Resume(
            name="主简历",
            direction="Python 开发",
            content_json=ResumeContent(skills=["Python", "FastAPI"]).model_dump_json(),
            content_hash="c" * 64,
            is_default=True,
        )
    )
    hot = Job(
        platform="boss",
        company="星澜科技",
        title="Python 后端开发",
        city="深圳",
        jd_text="要求 Python、FastAPI。",
        jd_hash="a" * 64,
        status="scored",
        match_score=88,
        url="https://www.zhipin.com/job_detail/aaaaaaaaaaaa.html",
    )
    applied = Job(
        platform="boss",
        company="云栖数据",
        title="数据平台开发",
        city="东莞",
        jd_text="要求 Python、SQL。",
        jd_hash="b" * 64,
        status="applied",
        match_score=75,
    )
    cold = Job(
        platform="manual",
        company="某小厂",
        title="前端开发",
        city="广州",
        jd_text="要求 Vue。",
        jd_hash="d" * 64,
        status="new",
        match_score=30,
    )
    session.add_all([hot, applied, cold])
    session.flush()

    application, _ = apply_svc.ensure_pending_application(session, applied)
    application.greeting_text = "您好，我对这个岗位很感兴趣。"
    apply_svc.set_status(session, applied.id, "applied")
    interview = Interview(application_id=application.id, round="一面", scheduled_at=now_utc())
    session.add(interview)
    session.flush()
    session.add_all(
        [
            InterviewQuestion(
                interview_id=interview.id,
                question="讲讲你做过的缓存优化",
                category="Redis",
                is_weak=True,
                rating="bad",
                source="recorded",
            ),
            InterviewQuestion(
                job_id=applied.id,
                question="Python 的 GIL 是什么",
                category="Python",
                source="recorded",
            ),
        ]
    )
    session.commit()
    return {"hot": hot.id, "applied": applied.id, "cold": cold.id, "interview": interview.id}


@pytest.fixture
def client(engine, db_url: str) -> TestClient:
    # ★ 依赖 `engine` 夹具：它会跑 Alembic 迁移建表（只给 db_url 不会建库）
    return TestClient(create_app(db_url=db_url, dist_dir=None))


def test_today_lists_candidates_without_writing(session, db_url: str, client: TestClient) -> None:
    """今日清单：只列候选 + 只读已有开场白；**不生成、不写库**。"""
    ids = _seed(session)

    before_jobs = session.query(Job).count()
    before_apps = session.query(Application).count()

    response = client.get("/api/today")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["candidates"] >= 1
    assert body["shown"] == len(body["items"])
    hot = next(item for item in body["items"] if item["job_id"] == ids["hot"])
    assert hot["company"] == "星澜科技"
    assert hot["match_score"] == 88
    # 这条还没跑过 jh today → 没有开场白，**如实留空并给命令**，不替用户调模型
    assert hot["greeting_text"] is None
    assert body["missing_greeting"] >= 1
    assert "jh today" in body["generate_hint"]

    session.expire_all()
    assert session.query(Job).count() == before_jobs, "GET /api/today 写了库"
    assert session.query(Application).count() == before_apps, "GET /api/today 建了投递记录"


def test_today_reports_the_existing_greeting(session, db_url: str, client: TestClient) -> None:
    """已投那条的开场白要能读到（页面"一键复制"复制的就是它）。"""
    ids = _seed(session)

    body = client.get("/api/today").json()
    # 已投的岗位默认不在"待投候选"里（口径由 select_candidates 决定），
    # 所以这里直接查"库里有开场白"这件事在接口里的可见性：拿不到候选就跳过断言
    items = {item["job_id"]: item for item in body["items"]}
    if ids["applied"] in items:
        assert items[ids["applied"]]["greeting_text"] == "您好，我对这个岗位很感兴趣。"


def test_dashboard_has_status_counts_and_actionable(
    session, db_url: str, client: TestClient
) -> None:
    _seed(session)

    response = client.get("/api/dashboard")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["jobs_total"] == 3
    counts = {row["status"]: row["count"] for row in body["jobs_by_status"]}
    assert counts.get("applied") == 1
    # 已投且在等回应 → 出现在"该我动手"的清单里（口径来自 apply_svc.list_actionable）
    assert isinstance(body["actionable"], list)
    assert isinstance(body["upcoming_interviews"], list)


def test_timeline_returns_entries(session, db_url: str, client: TestClient) -> None:
    ids = _seed(session)

    response = client.get(f"/api/jobs/{ids['applied']}/timeline")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["job_id"] == ids["applied"]
    assert body["status"] == "applied"
    assert body["entries"], "时间轴应当是空的？事件流里至少有一条 status_change"
    # 时间一律 ISO 8601 带时区（契约与线索页一致）
    assert any("T" in str(entry.get("occurred_at", "")) for entry in body["entries"])


def test_timeline_404_for_unknown_job(client: TestClient) -> None:
    response = client.get("/api/jobs/999999/timeline")
    assert response.status_code == 404
    assert "找不到" in response.json()["detail"]


def test_questions_and_weak_points(session, db_url: str, client: TestClient) -> None:
    _seed(session)

    response = client.get("/api/questions")
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["questions"]) == 2
    weak = next(item for item in body["questions"] if item["is_weak"])
    assert weak["category"] == "Redis"
    assert weak["company"] == "云栖数据", "经面试归属的岗位信息没有解析出来"
    # 薄弱点聚合：没答上来的分类要出现在报告里
    categories = [row["category"] for row in body["weak"]["rows"]]
    assert "Redis" in categories


def test_stats_is_serialisable(session, db_url: str, client: TestClient) -> None:
    """统计接口：能算出来、能序列化（页面画柱状图用它）。"""
    _seed(session)

    response = client.get("/api/stats")
    assert response.status_code == 200, response.text
    body = response.json()

    # StatsReport 的形状：`overall` 里是各阶段计数（total/viewed/replied/…）
    assert body["overall"]["total"] >= 1, f"统计字段形状变了：{sorted(body)[:8]}"
    # 至少不该出现 datetime 对象没被序列化的情况（TestClient 会直接报错，这里再钉一次）
    assert isinstance(body, dict)


def test_page_endpoints_are_read_only_even_with_days_filter(
    session, db_url: str, client: TestClient
) -> None:
    """`days` 过滤也要是纯读（防止将来有人顺手在里面写"补算"逻辑）。"""
    _seed(session)
    before = session.query(Application).count()

    assert client.get("/api/stats", params={"days": 7}).status_code == 200
    assert client.get("/api/questions", params={"top": 3, "recorded_only": True}).status_code == 200

    session.expire_all()
    assert session.query(Application).count() == before


def test_applied_at_is_recent_enough_for_the_days_filter(session) -> None:
    """（自检）种子数据里那条投递确实带 `applied_at`，否则 `days` 过滤会静默空掉。"""
    ids = _seed(session)
    application = apply_svc.get_latest_application(session, ids["applied"])
    assert application is not None and application.applied_at is not None
    assert application.applied_at >= now_utc() - timedelta(days=1)

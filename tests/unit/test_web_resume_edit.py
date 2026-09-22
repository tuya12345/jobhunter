"""P8：在网页上**改简历**（`GET /api/resumes*` + `POST /api/resumes/{id}/copies`）。

★ 这组用例守的核心是一条红线：**编辑 = 新增一份，原始导入的那份一个字节都不动**。
  与"导出只写新文件"同源 —— 用户手滑改错的代价不该是"重新导入 + 重新解析"。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from jobhunter.models import Resume
from jobhunter.services import resume_svc
from jobhunter.services.resume_svc import ResumeContent
from jobhunter.web.app import create_app


def _make_resume(session, *, name: str = "主简历", default: bool = True) -> Resume:
    content = ResumeContent(
        basic={"name": "示例同学", "summary": "三年后端"},
        skills=["Python", "FastAPI"],
        projects=[{"name": "订单系统", "role": "后端", "bullets": ["写了 12 个接口"]}],
    )
    row = Resume(
        name=name,
        direction="Python 开发",
        content_json=content.model_dump_json(),
        content_hash=resume_svc.compute_content_hash(content),
        is_default=default,
    )
    session.add(row)
    session.commit()
    return row


def _client(db_url: str) -> TestClient:
    return TestClient(create_app(db_url=db_url, dist_dir=None))


def test_get_resume_returns_structured_content(session, engine, db_url: str) -> None:
    row = _make_resume(session)

    response = _client(db_url).get(f"/api/resumes/{row.id}")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["name"] == "主简历"
    assert body["is_default"] is True
    assert body["content"]["skills"] == ["Python", "FastAPI"]
    assert body["content"]["projects"][0]["name"] == "订单系统"


def test_get_unknown_resume_is_404(engine, db_url: str) -> None:
    response = _client(db_url).get("/api/resumes/999999")
    assert response.status_code == 404
    assert "找不到简历" in response.json()["detail"]


def test_list_resumes(session, engine, db_url: str) -> None:
    _make_resume(session, name="主简历")
    _make_resume(session, name="前端版", default=False)

    body = _client(db_url).get("/api/resumes").json()
    assert [row["name"] for row in body["resumes"]] == ["主简历", "前端版"]


def test_edit_saves_a_new_copy_and_keeps_the_original(session, engine, db_url: str) -> None:
    """★ 核心：改完是**新的一份**，原始那份（内容、名字、id）原样保留。"""
    original = _make_resume(session)
    original_json = original.content_json
    content = ResumeContent.model_validate(
        {
            "basic": {"name": "示例同学", "summary": "三年后端"},
            "skills": ["Python", "FastAPI", "Redis"],  # ← 加了一个技能
            "projects": [{"name": "订单系统", "role": "后端", "bullets": ["写了 12 个接口"]}],
        }
    )

    response = _client(db_url).post(
        f"/api/resumes/{original.id}/copies", json={"content": content.model_dump()}
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["created"] is True
    assert body["resume_id"] != original.id
    assert "网页编辑" in body["name"]
    assert body["is_default"] is True
    assert "jh match run" in body["note"], "必须提醒：改了简历 → 旧的精评分要重算"

    session.expire_all()
    keeper = session.get(Resume, original.id)
    assert keeper is not None
    assert keeper.content_json == original_json, "原始简历被改动了"
    assert keeper.is_default is False, "默认简历没有切到新版"
    assert session.query(Resume).count() == 2


def test_edit_with_identical_content_is_idempotent(session, engine, db_url: str) -> None:
    """点两次"保存"不该在库里堆两份一模一样的简历。"""
    original = _make_resume(session)
    content = ResumeContent.model_validate({"basic": {}, "skills": ["Python"], "projects": []})

    client = _client(db_url)
    first = client.post(
        f"/api/resumes/{original.id}/copies", json={"content": content.model_dump()}
    )
    assert first.json()["created"] is True
    new_id = first.json()["resume_id"]

    second = client.post(f"/api/resumes/{new_id}/copies", json={"content": content.model_dump()})
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["resume_id"] == new_id
    assert "没有新建记录" in second.json()["note"]

    session.expire_all()
    assert session.query(Resume).count() == 2, "幂等被打破：多存了一份"


def test_edit_rejects_broken_content_with_chinese_detail(session, engine, db_url: str) -> None:
    """结构不对 → 422 + **中文说明**（不是 pydantic 的英文堆栈）。"""
    original = _make_resume(session)

    response = _client(db_url).post(
        f"/api/resumes/{original.id}/copies",
        json={"content": {"skills": "应该是数组，不是字符串"}},
    )
    assert response.status_code == 422
    assert "不符合结构" in response.json()["detail"]


def test_edit_404_for_unknown_source(engine, db_url: str) -> None:
    response = _client(db_url).post("/api/resumes/999999/copies", json={"content": {}})
    assert response.status_code == 404


def test_edited_copy_can_be_kept_as_non_default(session, engine, db_url: str) -> None:
    """`make_default=False`：存下来但不动默认简历（想做"另一个方向的版本"时用）。"""
    original = _make_resume(session)
    content = ResumeContent.model_validate({"skills": ["Go"]})

    body = (
        _client(db_url)
        .post(
            f"/api/resumes/{original.id}/copies",
            json={"content": content.model_dump(), "make_default": False, "name": "Go 方向"},
        )
        .json()
    )

    assert body["is_default"] is False
    session.expire_all()
    assert session.get(Resume, original.id).is_default is True
    assert session.get(Resume, body["resume_id"]).name == "Go 方向"

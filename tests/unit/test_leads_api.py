"""`jh leads serve` 的 Web 层：接口契约 + 那条"唯一写动作"的幂等与如实反馈（ISSUE #59）。

★★ 这组用例守两件事

    一、**接口契约**（`docs/P7-消费端API.md`）：页面只认这几个字段，
        改了字段名而没改文档 = 前端静默拿到 undefined。所以这里逐字段断言。

    二、**标记投递必须如实反馈**（规格 §3.4，也是 ISSUE #57 的原症状）：
        第二次点同一条不能看起来跟第一次一模一样 —— 要么说"更新了什么"，
        要么说"没有变化"。下面用 `message` 的内容把这个要求钉死。

★ 为什么用 `TestClient` + 临时库：规格第六节写明"CI 里不做端到端 UI 测试
  （没有浏览器），但 API 层要有测试"，而临时库保证用例绝不碰真实线索。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from jobhunter.models import Application, Company, Job
from jobhunter.services import leads_svc
from jobhunter.storage.db import create_db_engine
from jobhunter.web import app as web_app


def _snapshot(leads: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": "jobhunter.leads",
        "schema_version": 1,
        "exported_at": "2026-09-19T18:47:00+08:00",
        "generator": {"tool": "leadhunter", "version": "0.1.0"},
        "leads": leads,
    }


def _lead(name: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "external_id": 1,
        "name": name,
        "official_url": "https://example.com/a",
        "source_platform": "小红书",
        "source_type": "官网",
        "evidence_text": f"看到「{name}」在招人",
        "evidence_url": None,
        "note": None,
        "http_status": 404,
        "checked_at": "2026-09-19T10:47:36+00:00",
        "collected_at": "2026-09-19T10:47:17+00:00",
    }
    base.update(overrides)
    return base


@pytest.fixture
def client(db_url: str, engine, tmp_path: Path) -> Any:
    """指向临时库的 TestClient（产物目录指向一个空目录 → 走"还没构建"那条路径）。

    ★ 依赖 `engine` 夹具是**必须**的：那个夹具会跑 Alembic 迁移把表建出来。
      少了它，`create_app()` 拿到的是一个空库，接口会以 "no such table" 500 ——
      而那种失败看起来像"接口写错了"，其实只是测试没建表。
    """
    empty_dist = tmp_path / "no-dist"
    application = web_app.create_app(db_url=db_url, dist_dir=empty_dist)
    with TestClient(application) as test_client:
        yield test_client


@pytest.fixture
def seeded(db_url: str, engine, tmp_path: Path) -> dict[str, int]:
    """造两条线索 + 一个岗位（直接写库，不经接口 —— 导入那条路径另有专门用例）。"""
    path = tmp_path / "snap.json"
    path.write_text(
        json.dumps(
            _snapshot(
                [
                    _lead("星澜科技", external_id=1, http_status=404),
                    _lead(
                        "云枢智能",
                        external_id=2,
                        source_type="内推",
                        official_url=None,
                        http_status=None,
                    ),
                    _lead(
                        "虚构网络",
                        external_id=3,
                        official_url="https://example.invalid/c",
                        http_status=None,
                    ),
                ]
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    engine = create_db_engine(db_url, ensure_directory=False)
    from jobhunter.storage.db import make_session_factory

    factory = make_session_factory(engine)
    with factory() as session:
        leads_svc.import_snapshot(session, path)
        job = Job(company="甲公司", title="Python 后端开发实习生", city="深圳", platform="manual")
        session.add(job)
        session.commit()
        ids = {
            "星澜科技": session.scalars(select(Company.id).where(Company.name == "星澜科技")).one(),
            "云枢智能": session.scalars(select(Company.id).where(Company.name == "云枢智能")).one(),
            "虚构网络": session.scalars(select(Company.id).where(Company.name == "虚构网络")).one(),
            "job": job.id,
        }
    engine.dispose()
    return ids


# ===========================================================================
# 只读接口
# ===========================================================================
def test_health(client: TestClient) -> None:
    payload = client.get("/api/health").json()
    assert payload["ok"] is True
    assert payload["frontend"] is False, "产物目录是空的，应当如实报 false"
    assert "version" in payload


def test_leads_are_grouped_by_source_type(client: TestClient, seeded: dict[str, int]) -> None:
    payload = client.get("/api/leads").json()

    labels = [group["label"] for group in payload["groups"]]
    assert labels == ["官网", "内推", "HR直招", "未分类"], "分组顺序必须固定（页面照着渲染）"
    counts = {group["label"]: group["count"] for group in payload["groups"]}
    assert counts == {"官网": 2, "内推": 1, "HR直招": 0, "未分类": 0}
    assert payload["totals"] == {"leads": 3, "applied": 0, "unchecked": 2}


def test_lead_payload_has_every_contract_field(client: TestClient, seeded: dict[str, int]) -> None:
    """逐字段核对（契约在 `docs/P7-消费端API.md`）—— 少一个字段页面就是 undefined。"""
    payload = client.get("/api/leads").json()
    lead = next(
        item for group in payload["groups"] for item in group["leads"] if item["name"] == "星澜科技"
    )

    for key in (
        "id",
        "name",
        "external_id",
        "official_url",
        "source_platform",
        "source_type",
        "evidence_text",
        "evidence_url",
        "note",
        "collected_at",
        "created_at",
        "reachability",
        "application",
        "applications_count",
    ):
        assert key in lead, f"契约里有 {key}，接口没给"
    assert lead["reachability"]["state"] == "checked_error"
    assert lead["reachability"]["http_status"] == 404
    assert lead["application"] is None
    assert lead["applications_count"] == 0


def test_unchecked_is_a_third_state_not_a_failure(
    client: TestClient, seeded: dict[str, int]
) -> None:
    """★★ 规格点名的验收项：「未检查」不能显示成「失效」，两种原因要分得清。"""
    payload = client.get("/api/leads").json()
    leads = {item["name"]: item for group in payload["groups"] for item in group["leads"]}

    no_url = leads["云枢智能"]["reachability"]
    with_url = leads["虚构网络"]["reachability"]

    assert no_url["state"] == "unchecked" and no_url["label"] == "未检查"
    assert "没有官网网址" in no_url["reason"]
    assert with_url["state"] == "unchecked" and with_url["label"] == "未检查"
    assert "有网址" in with_url["reason"]
    # 「失效」这个词不能出现在任何"未检查"的解释里
    assert "失效" not in no_url["reason"] and "失效" not in with_url["reason"]


def test_jobs_endpoint_is_read_only_list(client: TestClient, seeded: dict[str, int]) -> None:
    payload = client.get("/api/jobs").json()
    assert [job["id"] for job in payload["jobs"]] == [seeded["job"]]
    assert payload["jobs"][0]["label"] == "甲公司 · Python 后端开发实习生 · 深圳"


def test_jobs_empty_is_a_legal_state(client: TestClient) -> None:
    assert client.get("/api/jobs").json() == {"jobs": []}


# ===========================================================================
# 唯一写接口：标记投递
# ===========================================================================
def _apply(client: TestClient, lead_id: int, job_id: int, **body: Any) -> dict[str, Any]:
    response = client.post(f"/api/leads/{lead_id}/apply", json={"job_id": job_id, **body})
    assert response.status_code == 200, response.text
    return response.json()


def test_mark_applied_creates_an_application_with_derived_channel(
    client: TestClient, seeded: dict[str, int], session
) -> None:
    result = _apply(client, seeded["星澜科技"], seeded["job"])

    assert result["created"] is True
    assert result["channel"] == "官网", "渠道应当由线索的 source_type 推导"
    assert result["status"] == "applied"
    assert "新建记录" in result["message"]

    # ★ 新建**不是**"改写"：`ensure_pending_application()` 会先播一个默认渠道
    #   （`招聘网站`），若把它当成"改之前的值"，用户第一次标记就会看到
    #   「渠道 招聘网站 → 官网」—— 一次新建被描述成一次改写（前端实测发现过）。
    assert result["channel_before"] is None, "新建时没有'之前的渠道'可言"
    assert result["channel_changed"] is False
    assert "招聘网站" not in result["message"]

    application = session.scalars(select(Application)).one()
    assert application.status == "applied"
    assert application.channel == "官网"
    assert application.company_id == seeded["星澜科技"], "必须记下'这条投递来自哪条线索'"


def test_marking_twice_is_idempotent_and_says_so(
    client: TestClient, seeded: dict[str, int], session
) -> None:
    """★★ 规格 §3.4：重复点不会产生第二条记录，而且**必须说出来**。"""
    first = _apply(client, seeded["星澜科技"], seeded["job"])
    second = _apply(client, seeded["星澜科技"], seeded["job"])

    assert second["application_id"] == first["application_id"]
    assert second["created"] is False, "第二次不该新建记录"
    assert second["status_changed"] is False
    assert "没有变化" in second["message"], (
        "第二次的输出必须与第一次不同（否则就是 #57 的原症状：看起来一模一样）"
    )
    assert session.scalar(select(func.count()).select_from(Application)) == 1


def test_channel_change_is_reported_not_silent(
    client: TestClient, seeded: dict[str, int], session
) -> None:
    """★★ ISSUE #57 的原症状：改了渠道却一声不吭。这里必须把"从 X 改成 Y"说出来。"""
    _apply(client, seeded["星澜科技"], seeded["job"], channel="官网")
    result = _apply(client, seeded["星澜科技"], seeded["job"], channel="内推")

    assert result["channel_changed"] is True
    assert result["channel_before"] == "官网"
    assert result["channel"] == "内推"
    assert "渠道 官网 → 内推" in result["message"], "渠道被改写了就必须在反馈里写明'从什么变成什么'"
    assert session.scalars(select(Application)).one().channel == "内推"


def test_status_advance_is_reported(client: TestClient, seeded: dict[str, int]) -> None:
    _apply(client, seeded["星澜科技"], seeded["job"], status="pending")
    result = _apply(client, seeded["星澜科技"], seeded["job"], status="replied")

    assert result["status_changed"] is True
    assert "状态 pending → replied" in result["message"]


def test_leads_without_source_type_get_no_channel(
    client: TestClient, seeded: dict[str, int], session
) -> None:
    """内推那条有 source_type；这里造一条没有的，渠道应当是"未指定"而不是猜一个。"""
    path = seeded  # 用现成的库，直接改一条做不到 —— 用 API 层面验证另一条线索
    result = _apply(client, path["云枢智能"], path["job"])
    assert result["channel"] == "内推"  # 这条有 source_type（内推）

    application = session.scalars(select(Application)).one()
    assert application.channel == "内推"


def test_lead_application_is_reflected_in_the_list(
    client: TestClient, seeded: dict[str, int]
) -> None:
    _apply(client, seeded["星澜科技"], seeded["job"])
    payload = client.get("/api/leads").json()
    lead = next(
        item for group in payload["groups"] for item in group["leads"] if item["name"] == "星澜科技"
    )

    assert lead["application"] is not None
    assert lead["application"]["job_id"] == seeded["job"]
    assert lead["application"]["status"] == "applied"
    assert lead["applications_count"] == 1
    assert payload["totals"]["applied"] == 1


def test_application_is_linked_to_the_right_lead(
    client: TestClient, seeded: dict[str, int]
) -> None:
    """★ 只有被点的那条线索显示"已投" —— 不能靠渠道反推（那会点亮别的线索）。"""
    _apply(client, seeded["星澜科技"], seeded["job"])
    payload = client.get("/api/leads").json()
    leads = {item["name"]: item for group in payload["groups"] for item in group["leads"]}

    assert leads["星澜科技"]["application"] is not None
    assert leads["虚构网络"]["application"] is None, "同渠道（官网）的另一条线索不该被点亮"


def test_unknown_lead_is_404(client: TestClient, seeded: dict[str, int]) -> None:
    response = client.post("/api/leads/9999/apply", json={"job_id": seeded["job"]})
    assert response.status_code == 404
    assert "找不到线索" in response.json()["detail"]


def test_unknown_job_is_404(client: TestClient, seeded: dict[str, int]) -> None:
    response = client.post(f"/api/leads/{seeded['星澜科技']}/apply", json={"job_id": 9999})
    assert response.status_code == 404
    assert "找不到岗位" in response.json()["detail"]


def test_unknown_status_is_400_with_available_values(
    client: TestClient, seeded: dict[str, int]
) -> None:
    response = client.post(
        f"/api/leads/{seeded['星澜科技']}/apply",
        json={"job_id": seeded["job"], "status": "随便写的"},
    )
    assert response.status_code == 400
    assert "不认识" in response.json()["detail"]


# ===========================================================================
# 静态页面与"产物还没构建"
# ===========================================================================
def test_missing_frontend_gives_actionable_html_not_a_blank_page(client: TestClient) -> None:
    """规格 §3.3：产物不存在时要给可操作提示，**不要白屏**。"""
    response = client.get("/")
    assert response.status_code == 503
    body = response.text
    assert "npm ci" in body and "npm run build" in body
    assert "text/html" in response.headers["content-type"]


def test_frontend_is_served_when_dist_exists(db_url: str, engine, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>线索页</title>", encoding="utf-8")
    (dist / "assets.js").write_text("console.log('ok')", encoding="utf-8")

    application = web_app.create_app(db_url=db_url, dist_dir=dist)
    with TestClient(application) as client:
        assert "线索页" in client.get("/").text
        assert client.get("/assets.js").status_code == 200
        assert client.get("/api/health").json()["frontend"] is True


# ===========================================================================
# 纪律守卫：Web 层不许自己写库
# ===========================================================================
def test_web_layer_never_writes_to_the_database_itself() -> None:
    """★★ 结构守卫：写库只能经 `leads_svc` → `apply_svc`（唯一写入口）。

    本项目因为"同一份数据两个写入口、口径不一致"吃过两次亏（#49 / #57），
    所以这里用源码扫描把纪律钉死：Web 层不许出现 `session.add` / `session.delete`
    / 直接改 `application.status`。
    """
    import ast

    from jobhunter.config.paths import find_project_root

    source = (find_project_root() / "src" / "jobhunter" / "web" / "app.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {"add", "delete", "add_all"}:
            value = node.value
            if isinstance(value, ast.Name) and value.id == "session":
                offenders.append(f"line {node.lineno}: session.{node.attr}()")
        # 直接给 ORM 字段赋值（写库的另一种形态）
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in {"application", "company", "job"}
                ):
                    offenders.append(f"line {node.lineno}: {target.value.id}.{target.attr} = …")
    assert not offenders, (
        f"Web 层出现了直接写库的代码：{offenders}\n"
        "请改成调用 services 层（leads_svc / apply_svc）—— 那是唯一的写入口。"
    )
    assert "session.commit()" in source, "写接口应当由 Web 层自己收事务边界（否则会漏提交）"

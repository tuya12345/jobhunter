"""`jh leads serve` 的 Web 层：FastAPI 应用 + 只读页面（P7 消费端，ISSUE #59）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 这一层只做"搬运"：把 services 的结果变成 JSON，把页面的点击变成一次服务调用 ║
╚══════════════════════════════════════════════════════════════════════════╝

★ 为什么业务逻辑一行都不写在这里

    · 导入：`services/leads_svc.import_snapshot()`（幂等 + dry-run + 中文报错）
    · 标记投递：`services/leads_svc.link_lead_to_application()`
      → 它内部走 `apply_svc.set_status()`（状态机、事件、闭环字段的唯一入口）

    本项目因为"同一份数据两个写入口、口径不一致"吃过两次亏（#49 / #57），
    所以 Web 层**不允许**碰 ORM 的写操作 —— 它只能调用那两个服务函数。
    这条纪律有测试守着（`tests/unit/test_leads_api.py` 里断言 API 模块不直接
    操作 `session.add/commit`、不改 `application.status`）。

★ 边界（写下来免得被当成遗漏）

    · **不做鉴权**：只监听回环地址（默认 127.0.0.1），见 `docs/COMPLIANCE.md`。
    · **不做增删改线索**：线索的唯一来源是 `jh leads import`。
    · **不做批量/自动投递**：唯一的写接口是"用户点一下 → 记一次投递"。
    · 不做 P8 的另外三个页面。

★ 接口契约见 `docs/P7-消费端API.md`（与前端 `web/` 共用的那一份）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobhunter import __version__
from jobhunter.config.settings import get_settings
from jobhunter.models import Application, Company, Job
from jobhunter.services import (
    apply_svc,
    interview_svc,
    leads_svc,
    question_svc,
    stats_svc,
    today_svc,
    track_svc,
    weak_svc,
)
from jobhunter.storage.db import create_db_engine, database_problem, make_session_factory
from jobhunter.utils.logging import get_logger
from jobhunter.web import serialize

logger = get_logger(__name__)

__all__ = ["create_app", "frontend_dist_dir", "missing_frontend_html"]

#: 前端构建产物目录（`web/dist`，不进仓库；页面由 Vite 构建）
_WEB_DIR_NAME = "web"
_DIST_DIR_NAME = "dist"

#: 产物缺失时给用户的提示（终端与页面**用同一句**，避免两处说法不一致）
BUILD_HINT = "前端还没构建：在 web/ 目录跑 `npm ci && npm run build`"


def frontend_dist_dir() -> Path:
    """`web/dist` 的绝对路径（项目根下）。"""
    from jobhunter.config.paths import find_project_root

    return find_project_root() / _WEB_DIR_NAME / _DIST_DIR_NAME


def missing_frontend_html() -> str:
    """产物不存在时返回的 HTML。

    ★ 为什么给一张**可读的页面**而不是 500/JSON：用户是在浏览器里打开它的，
      白屏或一坨 JSON 只会让人以为"服务坏了"；这句话直接告诉他跑哪条命令。
    """
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>JobHunter 线索页</title>
<style>
  body {{ font-family: system-ui, "Segoe UI", "Microsoft YaHei", sans-serif;
         margin: 0; padding: 3rem 1.5rem; background: #f7f7f8; color: #1f2328; }}
  main {{ max-width: 46rem; margin: 0 auto; background: #fff; border-radius: 12px;
          padding: 2rem 2.25rem; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  h1 {{ font-size: 1.25rem; margin: 0 0 .75rem; }}
  code {{ background: #f2f3f5; padding: .15rem .4rem; border-radius: 6px; }}
  pre {{ background: #f2f3f5; padding: .9rem 1rem; border-radius: 8px; overflow-x: auto; }}
  p {{ line-height: 1.7; }}
</style></head>
<body><main>
  <h1>页面还没构建</h1>
  <p>{BUILD_HINT}</p>
  <pre>cd web
npm ci
npm run build</pre>
  <p>构建完刷新本页即可。后端接口已经在跑：<code>/api/health</code>、<code>/api/leads</code>。</p>
  <p>（这个提示出现在这里，是因为 <code>web/dist/index.html</code> 不存在 ——
     构建产物不进仓库，属于正常状态。）</p>
</main></body></html>"""


class ApplyRequest(BaseModel):
    """`POST /api/leads/{id}/apply` 的请求体。"""

    job_id: int = Field(description="投到的岗位 id（applications.job_id 是 NOT NULL）")
    status: str = "applied"
    channel: str | None = None


def create_app(*, db_url: str | None = None, dist_dir: Path | None = None) -> FastAPI:
    """建应用。

    Args:
        db_url: 覆盖数据库连接（测试用临时库；默认按配置解析）
        dist_dir: 覆盖前端产物目录（测试用）

    为什么把它做成工厂函数而不是模块级单例：
      · 测试要能指向临时库（`TestClient(create_app(db_url=...))`），
        否则 API 测试会去碰真实库 —— 这个项目已经因为"临时脚本写进真实库"吃过一次亏；
      · 单例还会让"改配置后不生效"这类问题变得难查。
    """
    app = FastAPI(
        title="JobHunter 线索页",
        version=__version__,
        description="P7 消费端：看导入的线索 + 标记投递（本地工具，只监听回环地址）",
    )

    engine = create_db_engine(db_url) if db_url else create_db_engine()
    session_factory = make_session_factory(engine)

    # ★★ 库结构不对时，接口要**说人话**，不是一串 500（ISSUES #60）
    #
    #   验证会话实测：库停在迁移 0001 时 `jh leads serve` 能起来、页面也返回 200，
    #   但 `/api/leads` 是 **HTTP 500**（原始 SQLAlchemy 堆栈）。
    #   CLI 那条路现在会被 `require_db` 拦住（服务不再启动）；但"服务已起来、
    #   库随后被动过"仍可能发生 —— 所以这里再兜一层：把"结构不对"翻译成
    #   **503 + 中文说明**，而不是把堆栈丢给页面。
    schema_problem = database_problem(db_url)
    if schema_problem:
        logger.warning("线索页启动时发现库结构问题：%s", schema_problem.splitlines()[0])

    def get_session() -> Any:
        session: Session = session_factory()
        try:
            yield session
        finally:
            session.close()

    def _require_schema() -> None:
        """接口入口的闸门：结构不对 → 503 + 可操作提示（不是 500 堆栈）。"""
        if schema_problem:
            raise HTTPException(status_code=503, detail=schema_problem)

    # ------------------------------------------------------------------
    # 只读接口
    # ------------------------------------------------------------------
    @app.get("/api/health")
    def health(session: Session = Depends(get_session)) -> dict[str, Any]:
        dist = dist_dir or frontend_dist_dir()
        return {
            "ok": True,
            "version": __version__,
            # 结构有问题时也如实报出来（页面据此显示提示，而不是等接口 503）
            "schema_problem": schema_problem.splitlines()[0] if schema_problem else None,
            "leads": int(session.scalar(select(func.count()).select_from(Company)) or 0)
            if not schema_problem
            else 0,
            "jobs": int(session.scalar(select(func.count()).select_from(Job)) or 0)
            if not schema_problem
            else 0,
            "frontend": (dist / "index.html").is_file(),
        }

    @app.get("/api/leads")
    def list_leads(session: Session = Depends(get_session)) -> dict[str, Any]:
        """按 `source_type` 分组返回线索（三态可达性见 `leads_svc.reachability_of`）。"""
        _require_schema()
        groups: list[dict[str, Any]] = []
        totals = {"leads": 0, "applied": 0, "unchecked": 0}
        for source_type, rows in leads_svc.grouped_leads(session):
            payload = [_lead_payload(session, row) for row in rows]
            totals["leads"] += len(payload)
            totals["applied"] += sum(1 for item in payload if item["application"])
            totals["unchecked"] += sum(
                1 for item in payload if item["reachability"]["state"] == "unchecked"
            )
            groups.append(
                {
                    "source_type": source_type,
                    "label": source_type or "未分类",
                    "count": len(payload),
                    "leads": payload,
                }
            )
        return {
            "generated_at": leads_svc.iso_utc(leads_svc.exported_now()),
            "totals": totals,
            "groups": groups,
        }

    @app.get("/api/jobs")
    def list_jobs(session: Session = Depends(get_session)) -> dict[str, Any]:
        """标记投递时要选的岗位（**只读列表**，不是看板页面 —— 看板是 P8）。"""
        _require_schema()
        rows = session.scalars(select(Job).order_by(Job.id.desc())).all()
        return {
            "jobs": [
                {
                    "id": job.id,
                    "company": job.company,
                    "title": job.title,
                    "city": job.city,
                    "status": job.status,
                    "label": " · ".join(part for part in (job.company, job.title, job.city) if part)
                    or f"岗位 #{job.id}",
                }
                for job in rows
            ]
        }

    # P8 的四个只读页面接口：抽到 `_register_page_routes`（create_app 已经够长，
    # 而且那四条与线索页的读写接口是**两组不同的关注点**）。
    _register_page_routes(app, get_session=get_session, require_schema=_require_schema)

    # ------------------------------------------------------------------
    # 唯一的写接口
    # ------------------------------------------------------------------
    @app.post("/api/leads/{lead_id}/apply")
    def apply_lead(
        lead_id: int,
        request: ApplyRequest,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """把一条线索记成某岗位的一次投递（幂等 + 如实反馈，见 `leads_svc`）。"""
        _require_schema()
        lead = session.get(Company, lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail=f"找不到线索 id={lead_id}")
        try:
            result = leads_svc.link_lead_to_application(
                session,
                request.job_id,
                lead,
                channel=request.channel,
                status=request.status,
                source="jh leads serve",
            )
            session.commit()
        except apply_svc.ApplicationError as exc:
            session.rollback()
            raise HTTPException(status_code=_error_status(str(exc)), detail=str(exc)) from exc
        return {
            "lead_id": result.lead_id,
            "lead_name": result.lead_name,
            "application_id": result.application_id,
            "job_id": result.job_id,
            "created": result.created,
            "channel": result.channel,
            "channel_before": result.channel_before,
            "channel_changed": result.channel_changed,
            "status": result.status,
            "status_before": result.status_before,
            "status_changed": result.status_changed,
            "owned_by_other_lead": result.owned_by_other_lead,
            "message": result.message,
        }

    # ------------------------------------------------------------------
    # 静态页面
    # ------------------------------------------------------------------
    _mount_frontend(app, dist_dir or frontend_dist_dir())
    return app


def _mount_frontend(app: FastAPI, target: Path) -> None:
    """把前端产物挂到 `/`；产物不存在时给一张**可操作**的说明页。

    ★ 抽成函数的理由不只是"create_app 太长"：这段是"页面从哪来"的**唯一**描述，
      与接口逻辑混在一起时最容易在改动中被碰坏（而它的失败形态是"白屏"）。
    """
    if (target / "index.html").is_file():
        # html=True 让 `/` 直接吃 index.html；SPA 的其它路由由 fallback 处理
        app.mount("/", StaticFiles(directory=str(target), html=True), name="web")
        return

    logger.warning("前端构建产物不存在：%s（%s）", target, BUILD_HINT)

    @app.get("/", response_class=HTMLResponse)
    def _missing_frontend() -> HTMLResponse:
        return HTMLResponse(missing_frontend_html(), status_code=503)


def _error_status(message: str) -> int:
    """把服务层的中文错误映射成 HTTP 状态码（只有"找不到"是 404，其余是 400）。"""
    return 404 if "找不到" in message else 400


def _lead_payload(session: Session, row: Company) -> dict[str, Any]:
    """一条线索的 JSON（字段名与 `docs/P7-消费端API.md` 一一对应）。"""
    reach = leads_svc.reachability_of(row)
    application = _lead_application(session, row)
    return {
        "id": row.id,
        "name": row.name,
        "external_id": row.external_id,
        "official_url": row.official_url,
        "source_platform": row.source_platform,
        "source_type": row.source_type,
        "evidence_text": row.evidence_text,
        "evidence_url": row.evidence_url,
        "note": row.note,
        "collected_at": leads_svc.iso_utc(row.collected_at),
        "created_at": leads_svc.iso_utc(row.created_at),
        "reachability": {
            "state": reach.state,
            "label": reach.label,
            "http_status": reach.http_status,
            "checked_at": leads_svc.iso_utc(reach.checked_at),
            "reason": reach.reason,
        },
        "application": application,
        "applications_count": _lead_application_count(session, row),
    }


def _lead_application(session: Session, row: Company) -> dict[str, Any] | None:
    """这条线索"已经产生的投递记录"（页面用它显示"已投"）。

    ★ 关联口径：`applications.company_id == 线索.id`（迁移 0005 加的那一列），
      只有"从线索页标记投递"产生的记录才会填它。**不按渠道反推** ——
      渠道只有四五个取值，反推会把"同渠道的另一条线索"也算成已投。
    """
    if row.id is None:
        return None
    application = session.scalars(
        select(Application)
        .where(Application.company_id == row.id)
        .order_by(Application.id.desc())
        .limit(1)
    ).first()
    if application is None:
        return None
    job = session.get(Job, application.job_id)
    return {
        "application_id": application.id,
        "job_id": application.job_id,
        "job_label": " · ".join(part for part in (job.company, job.title, job.city) if part)
        if job
        else f"岗位 #{application.job_id}",
        "channel": application.channel,
        "status": application.status,
        "applied_at": leads_svc.iso_utc(application.applied_at),
    }


def _lead_application_count(session: Session, row: Company) -> int:
    """这条线索一共产生过几条投递记录（页面可以显示"投过 N 次"）。"""
    if row.id is None:
        return 0
    return int(
        session.scalar(
            select(func.count()).select_from(Application).where(Application.company_id == row.id)
        )
        or 0
    )


def _register_page_routes(app: FastAPI, *, get_session: Any, require_schema: Any) -> None:
    """注册 P8 的四个**只读**页面接口（今日清单 / 看板 / 时间轴 / 问题与统计）。

    ★★ 只读是刻意的（理由见每条接口的 docstring）：CLI 的 `jh today` 会生成开场白并写
      pending 记录，而"打开网页 = 调模型 + 写库"会让刷新变成花钱、还留下幽灵记录。
      页面这条路只读库里已有的东西，没有就如实说"去命令行生成"。
    """

    # ------------------------------------------------------------------
    # P8 页面接口（今日清单 / 看板 / 面试与薄弱点 / 统计）
    # ------------------------------------------------------------------
    #
    # ★★ 四条都是**只读**（GET 不写库、不调模型）——这是刻意的：
    #
    #   CLI 的 `jh today` 会**生成开场白**并写 pending 投递记录（那是"一条命令干完一轮"的
    #   产品设计）。网页如果照抄那条路径，就会出现"打开页面 = 调模型 = 写库"，
    #   刷新三次花三次钱、还留下三条 pending。所以页面这条路：
    #     · 开场白**只读库里已有的**（`applications.greeting_text`）；
    #     · 库里没有就如实说"还没生成，去命令行跑 jh today"，并给出命令。
    #   要生成就在 CLI 里生成 —— 页面的职责是"看和复制"，不是"替我花钱"。
    @app.get("/api/today")
    def today(session: Session = Depends(get_session)) -> dict[str, Any]:
        """今日待投清单（只读版本：不生成开场白、不写库）。"""
        require_schema()
        settings = get_settings()
        jobs, candidates, dropped = today_svc.select_candidates(session, settings=settings)
        items: list[dict[str, Any]] = []
        missing_greeting = 0
        for job in jobs:
            application = apply_svc.get_latest_application(session, job.id)
            greeting = (application.greeting_text if application else None) or None
            if greeting is None:
                missing_greeting += 1
            items.append(
                {
                    "job_id": job.id,
                    "company": job.company or "",
                    "title": job.title or "",
                    "city": job.city,
                    "salary_text": job.salary_text,
                    "match_score": job.match_score,
                    "url": job.url,
                    "greeting_text": greeting,
                    "greeting_variant": (application.greeting_variant if application else None),
                    "application_id": application.id if application else None,
                }
            )
        return {
            "candidates": candidates,
            "shown": len(items),
            "dropped": dropped,
            "missing_greeting": missing_greeting,
            "generate_hint": (
                f"有 {missing_greeting} 条还没生成开场白 —— 在命令行跑 `jh today` 生成"
                "（网页只读，不替你调模型/写库）"
                if missing_greeting
                else None
            ),
            "items": items,
        }

    @app.get("/api/dashboard")
    def dashboard(session: Session = Depends(get_session)) -> dict[str, Any]:
        """求职看板：岗位状态分布 + 该我动手的投递 + 接下来的面试。"""
        require_schema()
        status_rows = session.execute(
            select(Job.status, func.count()).group_by(Job.status).order_by(Job.status)
        ).all()
        actionable = apply_svc.list_actionable(session)
        upcoming = interview_svc.list_interviews(session, upcoming=True)
        return {
            "jobs_by_status": [
                {"status": status, "count": int(count)} for status, count in status_rows
            ],
            "jobs_total": int(session.scalar(select(func.count()).select_from(Job)) or 0),
            "actionable": [
                {
                    "job_id": row.job_id,
                    "application_id": row.application_id,
                    "company": row.company,
                    "title": row.title,
                    "status": row.status,
                    "days": row.days,
                    "reason": row.reason,
                }
                for row in actionable
            ],
            "upcoming_interviews": serialize.as_payload(upcoming),
        }

    @app.get("/api/jobs/{job_id}/timeline")
    def job_timeline(job_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
        """一个岗位的投递时间轴（读 append-only 事件流）。"""
        require_schema()
        try:
            timeline = track_svc.build_timeline(session, job_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return serialize.as_payload(timeline)

    @app.get("/api/questions")
    def questions(
        session: Session = Depends(get_session),
        top: int = 10,
        recorded_only: bool = False,
    ) -> dict[str, Any]:
        """面试问题清单 + 薄弱点聚合（`top` 控制薄弱点返回几个知识点）。"""
        require_schema()
        rows = question_svc.list_questions(session)
        weak = weak_svc.build_weak_report(session, top=top, recorded_only=recorded_only)
        return {
            "questions": [
                {
                    "id": row.question.id,
                    "question": row.question.question,
                    "answer": row.question.my_answer,
                    "rating": row.question.rating,
                    "category": row.question.category,
                    "is_weak": row.question.is_weak,
                    "source": row.question.source,
                    "company": row.company,
                    "title": row.title,
                    "round": row.round_,
                    "interview_id": row.interview_id,
                    "job_id": row.job_id,
                }
                for row in rows
            ],
            "weak": serialize.as_payload(weak),
        }

    @app.get("/api/stats")
    def stats(session: Session = Depends(get_session), days: int | None = None) -> dict[str, Any]:
        """转化率统计（页面画柱状图用）。`days` 只看最近 N 天投出去的。"""
        require_schema()
        return serialize.as_payload(stats_svc.build_stats(session, days=days))

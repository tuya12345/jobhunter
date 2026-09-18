"""待投清单的选取逻辑（`jh today` 的"选谁"这一半）。

★ 与"生成开场白"分开的原因：选谁是**纯查询 + 纯规则**（好测、不花钱），
  生成开场白要调模型。分开之后 `--copy N`、`--export`、`--check`
  都能复用同一套序号，不必各自重写一遍筛选。

★★ 序号稳定性（最重要的一条）
    `jh today` 打印的序号会被 `jh apply 3 --status applied`、`jh today --copy 3` 引用。
    序号一旦漂移，就会出现"我以为标的是第 3 条，实际改的是别的岗位"——
    这是本工具**最致命**的一类误操作。
    所以：筛选与排序必须**完全确定**（match_score desc, id asc），
    且下列三个命令共用 `select_candidates()`，绝不各写一套。

★ 筛选规则（逐条都有理由）
    ① `match_score >= min_score`：低于分数线的不推（config.threshold 的语义）
    ② 排除 `jobs.status in (applied, skipped)`：投过的、我主动跳过的
    ③ 排除"已经有非 pending 投递记录"的岗位：状态列没同步时也不会重复推
    ④ **同公司只留分数最高的那一条**：校招里给同一家公司连投多个岗位是减分项
    ⑤ `limit` 截断在最后 —— 先去重再截断，否则会出现"20 条里 8 条是同一家公司"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunter.config.settings import AppSettings, get_settings
from jobhunter.models import Application, Job
from jobhunter.services import apply_svc, match_svc
from jobhunter.utils.io import read_json, write_json
from jobhunter.utils.logging import get_logger
from jobhunter.utils.time import now_utc, to_local

logger = get_logger(__name__)

__all__ = [
    "TodayItem",
    "TodayList",
    "build_today_list",
    "ensure_item",
    "load_snapshot",
    "resolve_index",
    "save_snapshot",
    "select_candidates",
]

#: 这些岗位状态不再进待投清单
_EXCLUDED_JOB_STATUSES = ("applied", "skipped")


@dataclass
class TodayItem:
    """清单里的一条（CLI 直接渲染它）。"""

    index: int
    job: Job
    score: int
    resume_id: int | None = None
    resume_name: str = ""
    application_id: int | None = None
    greeting_full: str = ""
    greeting_short: str = ""
    greeting_variant: str = ""
    greeting_source: str = ""
    #: 开场白降级到模板的原因（有值就要显示给用户看）
    greeting_issues: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.job.title or "（未写职位）"

    @property
    def company(self) -> str:
        return self.job.company or "—"

    @property
    def url(self) -> str:
        return self.job.url or ""


@dataclass
class TodayList:
    """一次清单的完整结果。"""

    items: list[TodayItem] = field(default_factory=list)
    #: 过滤前有多少岗位达到分数线（用来说明"为什么只有这几条"）
    candidates: int = 0
    #: 因"同公司去重"被挤掉的条数
    dropped_same_company: int = 0
    applied_today: int = 0
    daily_limit: int = 0

    @property
    def remaining_quota(self) -> int:
        return max(0, self.daily_limit - self.applied_today)


def select_candidates(
    session: Session,
    *,
    min_score: int | None = None,
    limit: int | None = None,
    settings: AppSettings | None = None,
) -> tuple[list[Job], int, int]:
    """选出今天的候选岗位。

    Returns:
        (岗位列表, 过滤前达到分数线的数量, 因同公司被挤掉的数量)
    """
    settings = settings or get_settings()
    threshold = settings.threshold if min_score is None else min_score

    statement = (
        select(Job)
        .where(
            Job.match_score.is_not(None),
            Job.match_score >= threshold,
            Job.status.not_in(_EXCLUDED_JOB_STATUSES),
        )
        # ★ 排序必须完全确定：分数降序 + id 升序（同分时不会每次顺序不同）
        .order_by(Job.match_score.desc(), Job.id.asc())
    )
    rows = list(session.scalars(statement))

    # 已经有"真的投出去"的记录 → 不再推（状态列与投递记录可能不同步，两道都要查）
    applied_job_ids = set(
        session.scalars(select(Application.job_id).where(Application.status != "pending"))
    )
    rows = [job for job in rows if job.id not in applied_job_ids]
    total = len(rows)

    # 同公司去重：保留分数最高的那一条（列表已按分数排序，第一次出现即最高）
    seen: set[str] = set()
    deduped: list[Job] = []
    for job in rows:
        key = (job.company or "").strip()
        if key and key in seen:
            continue
        seen.add(key)
        deduped.append(job)

    dropped = len(rows) - len(deduped)
    if limit is not None and limit >= 0:
        deduped = deduped[:limit]
    return deduped, total, dropped


def ensure_item(
    session: Session,
    job: Job,
    index: int,
    *,
    settings: AppSettings,
    provider: object | None = None,
    variant: str | None = None,
) -> TodayItem:
    """给**单个**岗位生成开场白 + 写 pending 投递记录，返回清单里的一条。

    ★ 为什么单独抽出来：`jh today --copy 3` 只需要第 3 条，
      按整张清单生成会白花 19 次调用（有 key 时是真花钱）。
    """
    from jobhunter.services import greeting_svc

    candidates = match_svc.load_resume_candidates(session, settings)
    by_id = {candidate.resume_id: candidate for candidate in candidates}
    candidate = by_id.get(job.matched_resume_id) or (candidates[0] if candidates else None)
    chosen_variant = greeting_svc.pick_variant(index - 1, forced=variant)

    if candidate is None:
        application, _ = apply_svc.ensure_pending_application(session, job)
        session.commit()  # 与下面同理：写了一条记录就落盘
        return TodayItem(
            index=index,
            job=job,
            score=job.match_score or 0,
            application_id=application.id,
            greeting_issues=["库里没有简历，无法生成开场白（先 jh resume add）"],
        )

    greeting = greeting_svc.generate_greeting(
        job,
        candidate,
        session=session,
        provider=provider,
        settings=settings,
        variant=chosen_variant,
    )
    application, _created = apply_svc.ensure_pending_application(
        session,
        job,
        resume_id=candidate.resume_id,
        greeting=greeting.full,
        greeting_variant=greeting.variant,
    )
    # ★ 立刻 commit，而不是只 flush（ISSUES #32，P3 #3 的同类复发）：
    #   这一条里可能有**真花了钱**的 LLM 调用（开场白）与缓存写入。
    #   只 flush 的话事务一直开着，`jh today` 生成到第 7 条时 Ctrl+C，
    #   前 6 条全部回滚 —— 钱花了、结果没了，重跑还要再花一次。
    #   与 `match_llm_svc._apply()` 是同一条纪律：**每完成一条就落盘**。
    session.commit()
    return TodayItem(
        index=index,
        job=job,
        score=job.match_score or 0,
        resume_id=candidate.resume_id,
        resume_name=candidate.name,
        application_id=application.id,
        greeting_full=greeting.full,
        greeting_short=greeting.short,
        greeting_variant=greeting.variant,
        greeting_source=greeting.source,
        greeting_issues=greeting.issues,
    )


def build_today_list(
    session: Session,
    *,
    min_score: int | None = None,
    limit: int | None = None,
    settings: AppSettings | None = None,
    provider: object | None = None,
    variant: str | None = None,
    generate: bool = True,
) -> TodayList:
    """选出清单，并为每条**生成开场白 + 写 pending 投递记录**（幂等）。

    Args:
        generate: False 时只选不生成（`--check` 的预览路径用）。
        provider: 不传就只走模板兜底（没配 key 也能出清单）。
    """
    settings = settings or get_settings()
    jobs, total, dropped = select_candidates(
        session, min_score=min_score, limit=limit, settings=settings
    )
    result = TodayList(
        candidates=total,
        dropped_same_company=dropped,
        applied_today=apply_svc.count_applied_today(session),
        daily_limit=settings.daily_limit,
    )
    if not generate:
        result.items = [
            TodayItem(index=i, job=job, score=job.match_score or 0)
            for i, job in enumerate(jobs, start=1)
        ]
        return result

    for index, job in enumerate(jobs, start=1):
        result.items.append(
            ensure_item(session, job, index, settings=settings, provider=provider, variant=variant)
        )

    # ★★ 快照 = 今天的**完整达线清单**（在 `limit` **之前**取），而且空结果不覆盖。
    #    为什么不能在 `limit` 之后取（ISSUES #33，验证会话实测）：
    #      `jh today --limit 3` 跑一次，快照就只剩 3 条 —— 序号 4/5 当天永久不可达，
    #      而库里明明还有达线岗位；`jh apply 5` 会直接说"没有第 5 条"。
    #      序号语义被一个**显示选项**悄悄改掉了。
    #    为什么空结果不覆盖：
    #      某次筛选为空（比如 `--min-score 999`）会把快照写成 `[]`；
    #      而 `load_snapshot()` 对空列表返回 falsy → `resolve_index` 静默退回实时筛选，
    #      此时序号指向的岗位与用户上次看到的清单**毫无关系** —— 正是最危险的情形。
    #    语义一旦定成"今天的完整清单"，`--limit` 就只是"只看前 N 条"，
    #    与 `resolve_index` docstring 的承诺（当天内始终指向同一批岗位）一致。
    full_jobs, _total_all, _dropped_all = select_candidates(
        session, min_score=min_score, limit=None, settings=settings
    )
    if full_jobs:
        save_snapshot([job.id for job in full_jobs], settings)
    return result


# ===========================================================================
# 序号快照（防止"投完第 1 条之后，第 3 条变成了别的岗位"）
# ===========================================================================
def snapshot_path(settings: AppSettings) -> Path:
    """`state/today-YYYY-MM-DD.json`（按**北京时间**的日期）。"""
    today = to_local(now_utc()).strftime("%Y-%m-%d")
    return settings.paths.state_dir / f"today-{today}.json"


def save_snapshot(
    job_ids: list[int], settings: AppSettings, *, now: datetime | None = None
) -> Path | None:
    """把清单的岗位顺序写进 state/（供 `--copy N` 与 `jh apply N` 复用）。

    ★ **空列表不写**（ISSUES #33）：一次筛选为空就把当天的序号基准擦掉，
      会让 `load_snapshot` 返回 falsy 而静默退回实时筛选 —— 序号含义与用户
      上次看到的清单完全不同，这比"没有快照"危险得多。
      调用方本来就该传"今天的完整达线清单"，空的时候保持原快照不动。
    """
    if not job_ids:
        return None
    path = snapshot_path(settings)
    payload = {
        "date": to_local(now or now_utc()).strftime("%Y-%m-%d"),
        "created_at": (now or now_utc()).isoformat(),
        "job_ids": job_ids,
    }
    return write_json(path, payload)


def load_snapshot(settings: AppSettings, *, now: datetime | None = None) -> list[int] | None:
    """读今天的快照；**不是今天的**一律当没有（跨天不该复用昨天的序号）。"""
    path = snapshot_path(settings)
    data = read_json(path, default=None)
    if not isinstance(data, dict):
        return None
    today = to_local(now or now_utc()).strftime("%Y-%m-%d")
    if data.get("date") != today:
        return None
    job_ids = data.get("job_ids")
    if not isinstance(job_ids, list):
        return None
    return [int(value) for value in job_ids if isinstance(value, int)]


def resolve_index(
    session: Session,
    index: int,
    *,
    settings: AppSettings,
) -> tuple[Job | None, bool]:
    """把清单序号解析成岗位。

    Returns:
        (岗位或 None, 是否来自今天的快照)

    ★★ 为什么要快照（P4 手敲时发现的真问题）：
      `jh today` 打出 20 条 → 投完第 1 条 → 再敲 `jh apply 3`。
      此时第 1 条已经变成 applied 被排除，**整个清单往前挪了一位**，
      "第 3 条"已经不是你刚才看到的第 3 条了 —— 这正是模块 docstring 里
      说的"最致命的误操作"。
      存了快照之后，序号在**当天内**始终指向同一批岗位；
      快照不存在（没跑过 `jh today`）才退回实时筛选。

    ★ 序号的定义（ISSUES #33 之后定死，别再含糊）：
      快照存的是"**今天的完整达线清单**"（同公司去重之后、`--limit` 截断**之前**）。
      所以 `jh today --limit 3` 只影响**显示和生成**的条数，
      不影响序号含义 —— `jh apply 5` 照样能找到第 5 个达线岗位。
    """
    job_ids = load_snapshot(settings)
    if job_ids:
        if 1 <= index <= len(job_ids):
            job = session.get(Job, job_ids[index - 1])
            if job is not None:
                return job, True
        return None, True
    jobs, _total, _dropped = select_candidates(session, settings=settings)
    if 1 <= index <= len(jobs):
        return jobs[index - 1], False
    return None, False

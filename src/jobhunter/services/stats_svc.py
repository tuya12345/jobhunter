"""转化率统计（`jh stats`）—— 用数据复盘自己的投递，而不是凭感觉。

╔══════════════════════════════════════════════════════════════════════════╗
║ 口径必须写死在代码里（否则数字无法复现，也就无法复盘）                       ║
╚══════════════════════════════════════════════════════════════════════════╝

    投递数 = 有 `applied_at` 的投递（`pending` 不算 —— 还没投出去）
    已读率 = "至少走到过 viewed" / 投递数
    回复率 = "至少走到过 replied" / 投递数
    约面率 = "至少走到过 assessment" / 投递数     ← 笔试/测评也算"对方要我了"
    Offer 率 = "至少走到过 offer" / 投递数

★★ "至少走到过" ≠ "当前停在这一步"（这是本模块最容易被写错的地方）

    按**当前状态**统计会系统性低估：一个现在是 `rejected` 的投递，
    很可能走到过二面 —— 按当前状态算，它连"回复"都不算。
    所以判据是 `当前状态在阈值之后` **或** `事件流里出现过阈值之后的状态`。
    这也正是 `application_events` 是 append-only 的原因（`models/application.py`）。

★★ 另一条纪律：**只记录对比，不宣称统计显著性**（评审明确采纳）

    一个秋招可能就 100 次投递，分到 2 个开场白变体各 50 次，
    回复事件可能只有个位数。任何"变体 A 显著优于 B"的结论都是噪声。
    所以这一层只输出数字与样本量，**不生成"建议使用变体 A"这类伪结论**；
    样本量太小时在结论里直接说"样本太小，别下结论"。

★ 方向回音：按 JD 方向（`jobs.rule_detail_json` 里 match 算出来的 `jd_direction`）
  分组，回答"我投哪个方向更容易有回音"。没打过分的岗位归到「（未打分）」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunter.models import Application, ApplicationEvent, Job
from jobhunter.models.constants import APPLICATION_STATUSES
from jobhunter.utils.io import from_json
from jobhunter.utils.logging import get_logger
from jobhunter.utils.time import now_utc

logger = get_logger(__name__)

__all__ = [
    "MIN_SAMPLE_FOR_COMPARISON",
    "DirectionStat",
    "PlatformStat",
    "StatsReport",
    "VariantStat",
    "build_stats",
    "format_rate",
]

#: 低于这个样本量时，两组之间的差异基本是噪声（结论里会明说）
MIN_SAMPLE_FOR_COMPARISON = 10

#: 状态在状态机里的顺序（"至少走到过"就是"下标 ≥ 阈值"）
_STAGE_ORDER: tuple[str, ...] = tuple(APPLICATION_STATUSES)
#: 各转化率的阈值状态（口径见模块 docstring）
THRESHOLDS: dict[str, str] = {
    "viewed": "viewed",
    "replied": "replied",
    "interview": "assessment",
    "offer": "offer",
}
#: 中文标签（渲染层直接用，别在 CLI 里再写一份）
RATE_LABELS: dict[str, str] = {
    "viewed": "已读率",
    "replied": "回复率",
    "interview": "约面率",
    "offer": "Offer 率",
}


def format_rate(value: float | None, total: int) -> str:
    """把比率写成 `12.5%（3/24）`；样本为 0 时写 `—`。

    ★ 一定要带分子/分母：只有百分比的话，`100%` 到底是"1/1"还是"20/20"
      完全看不出来，而这决定了它是不是噪声。
    """
    if not total or value is None:
        return "—"
    return f"{value * 100:.1f}%（{round(value * total)}/{total}）"


def _reached(status: str, threshold: str) -> bool:
    """`status` 是否已经走到（或走过）`threshold` 这一步。"""
    if status not in _STAGE_ORDER or threshold not in _STAGE_ORDER:
        return False
    return _STAGE_ORDER.index(status) >= _STAGE_ORDER.index(threshold)


@dataclass
class StageCounts:
    """一组投递在各阶段"至少走到过"的条数。"""

    total: int = 0
    viewed: int = 0
    replied: int = 0
    interview: int = 0
    offer: int = 0
    rejected: int = 0

    def rate(self, key: str) -> float | None:
        if not self.total:
            return None
        return getattr(self, key) / self.total

    def as_dict(self) -> dict[str, float | None]:
        return {key: self.rate(key) for key in THRESHOLDS}


@dataclass
class PlatformStat:
    """一个投递渠道/平台的转化情况。"""

    platform: str
    counts: StageCounts


@dataclass
class VariantStat:
    """一个开场白变体的转化情况（**只记录，不下结论**）。"""

    variant: str
    counts: StageCounts

    @property
    def small_sample(self) -> bool:
        return self.counts.total < MIN_SAMPLE_FOR_COMPARISON


@dataclass
class DirectionStat:
    """一个岗位方向的回音情况。"""

    direction: str
    counts: StageCounts


@dataclass
class StatsReport:
    """`jh stats` 的完整结果。"""

    days: int | None = None
    overall: StageCounts = field(default_factory=StageCounts)
    platforms: list[PlatformStat] = field(default_factory=list)
    variants: list[VariantStat] = field(default_factory=list)
    directions: list[DirectionStat] = field(default_factory=list)
    #: 面试场次 / 记录题数 / 薄弱题数（复盘时有用的三个附注数字）
    interviews: int = 0
    questions: int = 0
    weak_questions: int = 0
    #: 口径与样本量提示（CLI 原样打印，免得用户自己猜）
    notes: list[str] = field(default_factory=list)

    @property
    def sample_note(self) -> str:
        if self.overall.total == 0:
            return "还没有投递记录"
        if self.overall.total < MIN_SAMPLE_FOR_COMPARISON:
            return (
                f"样本量很小（{self.overall.total} 条）——"
                f"分组之间的差异基本是噪声，别据此下结论（≥{MIN_SAMPLE_FOR_COMPARISON} 条再看）"
            )
        return f"样本量 {self.overall.total} 条"


def _stage_counts(applications: list[Application], events: dict[int, set[str]]) -> StageCounts:
    """按"至少走到过"统计一组投递。

    Args:
        events: `application_id → 事件流里出现过的 to_status 集合`。
    """
    counts = StageCounts()
    for application in applications:
        counts.total += 1
        reached = set(events.get(application.id or 0, set()))
        reached.add(application.status)
        if application.status == "rejected":
            counts.rejected += 1
        for key, threshold in THRESHOLDS.items():
            if any(_reached(status, threshold) for status in reached):
                setattr(counts, key, getattr(counts, key) + 1)
    return counts


def _events_by_application(session: Session) -> dict[int, set[str]]:
    """把事件流折叠成 `application_id → 出现过的状态集合`（只关心状态变迁）。"""
    out: dict[int, set[str]] = {}
    rows = session.execute(
        select(ApplicationEvent.application_id, ApplicationEvent.to_status).where(
            ApplicationEvent.to_status.is_not(None)
        )
    ).all()
    for application_id, to_status in rows:
        if to_status:
            out.setdefault(application_id, set()).add(to_status)
    return out


def _direction_of(job: Job | None) -> str:
    """岗位方向：优先用打分时算出来的 `jd_direction`，否则「（未打分）」。

    ★ 为什么不现算：那会让"统计结果"与"我当时看到的匹配结果"不一致 ——
      复盘时最忌讳的就是"同一件事两套数字"。
    """
    if job is None:
        return "（岗位已删除）"
    detail = from_json(job.rule_detail_json, default=None)
    if isinstance(detail, dict):
        label = str(detail.get("jd_direction") or "").strip()
        if label:
            return label
    return "（未打分）"


def build_stats(
    session: Session,
    *,
    days: int | None = None,
    now: datetime | None = None,
) -> StatsReport:
    """算出转化率统计。

    Args:
        days: 只统计最近 N 天内**投出去**的（按 `applied_at`）。None = 全部。
    """
    moment = now or now_utc()
    since = moment - timedelta(days=days) if days else None

    rows = session.execute(select(Application, Job).join(Job, Application.job_id == Job.id)).all()
    events = _events_by_application(session)

    applications: list[Application] = []
    pairs: list[tuple[Application, Job]] = []
    for application, job in rows:
        if application.applied_at is None:
            continue  # 还没投出去的不进统计（口径见模块 docstring）
        if since is not None and application.applied_at < since:
            continue
        applications.append(application)
        pairs.append((application, job))

    report = StatsReport(days=days)
    report.overall = _stage_counts(applications, events)

    by_platform: dict[str, list[Application]] = {}
    by_variant: dict[str, list[Application]] = {}
    by_direction: dict[str, list[Application]] = {}
    for application, job in pairs:
        by_platform.setdefault(application.platform or "（未知）", []).append(application)
        by_variant.setdefault(application.greeting_variant or "（没用开场白）", []).append(
            application
        )
        by_direction.setdefault(_direction_of(job), []).append(application)

    report.platforms = [
        PlatformStat(platform=name, counts=_stage_counts(items, events))
        for name, items in sorted(by_platform.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]
    report.variants = [
        VariantStat(variant=name, counts=_stage_counts(items, events))
        for name, items in sorted(by_variant.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]
    report.directions = [
        DirectionStat(direction=name, counts=_stage_counts(items, events))
        for name, items in sorted(by_direction.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]

    from jobhunter.models import Interview, InterviewQuestion

    report.interviews = len(
        session.scalars(
            select(Interview.id).join(Application, Interview.application_id == Application.id)
        ).all()
    )
    questions = list(session.scalars(select(InterviewQuestion)))
    report.questions = len(questions)
    report.weak_questions = sum(1 for item in questions if item.is_weak)
    report.notes = [
        "口径：按「至少走到过这一步」统计（不是当前停在哪一步）——"
        "否则被拒的投递会连带把它的历史高水位一起抹掉",
        "回复率含笔试/测评邀请；约面率 = 走到 assessment 及之后",
        f"面试 {report.interviews} 场 ｜ 记录问题 {report.questions} 条"
        f"（其中没答上来 {report.weak_questions} 条）",
        "★ 只记录对比，不宣称统计显著性：样本小的时候，两个变体/方向之间的差异说明不了任何事",
    ]
    if not report.overall.total:
        report.notes.append(
            "还没有投递记录 —— 先 jh today 出清单、投完 jh apply 打标，这里才有数字"
        )
    return report

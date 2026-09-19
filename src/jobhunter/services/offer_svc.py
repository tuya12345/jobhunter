"""Offer：`offers`（P6）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 这个模块的核心不是"存下 offer"，而是**把年包算出来横向比**                 ║
╚══════════════════════════════════════════════════════════════════════════╝

只写 "25K" 完全看不出 15 薪和 13 薪的差距（一年差 5 万）。
所以除了 `salary_text`（原文，给人看）之外，还要求用户补
`salary_month` + `months` 两个**数值**字段 —— 年包由 `Offer.annual_package` 推导。

★ 为什么数值字段允许留空而不是强制填：
  offer 常常是先口头通知、隔天才发正式邮件，HR 说的"总包"口径也五花八门。
  强制必填只会逼用户当场编一个数字 —— 那比留空更糟。
  留空的代价由 `compare_rows` 明确呈现（显示 `—` 并提示要补什么）。

★ 状态推进（`offer`）与笔试登记共用同一套"只前进不回退"规则，见
  `_advance_to_offer`。这里不 import `assessment_svc` 的私有函数：
  两个模块各自持有一份**很小**的规则，比跨服务引用私有函数更好读；
  真正的共享部分（`apply_svc.set_status` 的事件口径）已经共享了。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunter.models import Application, ApplicationEvent, Job, Offer
from jobhunter.models.constants import APPLICATION_STATUSES
from jobhunter.services import apply_svc
from jobhunter.utils.io import to_json
from jobhunter.utils.logging import get_logger
from jobhunter.utils.time import days_between, now_utc

logger = get_logger(__name__)

__all__ = [
    "OfferError",
    "OfferRow",
    "add_offer",
    "compare_rows",
    "list_offers",
]

#: 登记 offer 时**不允许**被回退到的状态（从常量表推导，不另抄一份名单）
_BEYOND_OFFER: frozenset[str] = frozenset(
    APPLICATION_STATUSES[APPLICATION_STATUSES.index("offer") + 1 :]
)


class OfferError(ValueError):
    """Offer 登记失败（找不到岗位、薪资数字不合理等）。"""


@dataclass
class OfferRow:
    """一个 offer + 它所属岗位的 id + 距签约截止的天数。"""

    offer: Offer
    job_id: int
    #: 距签约截止还有几个自然日（负数=已过期，None=没填截止日）
    days_left: int | None

    @property
    def label(self) -> str:
        """`公司 · 职位`（用 offer 上冗余的那一份，脱离投递记录也能显示）。"""
        return f"{self.offer.company or '—'} · {self.offer.title or '（未写职位）'}"


# ===========================================================================
# 登记
# ===========================================================================
def add_offer(
    session: Session,
    job_id: int,
    *,
    salary_text: str | None = None,
    salary_month: int | None = None,
    months: int | None = None,
    location: str | None = None,
    deadline_at: datetime | None = None,
    pros: str | None = None,
    cons: str | None = None,
    note: str | None = None,
) -> Offer:
    """登记一个 offer，并把投递状态推进到 `offer`。

    Args:
        deadline_at: 签约截止时间，UTC（调用方用 `parse_local_input` 转好再传）。
        salary_month / months: 月薪（元）与发几个月 —— 只有两个都有才算得出年包。

    Raises:
        OfferError: 岗位不存在，或月薪/月数给了非正数。

    ★ 为什么要校验"非正数"（契约里没要求的额外校验）：
      年包 = 月薪 × 月数，一旦有一个是 0 或负数，比较表会把它算成最小值、
      甚至排到"最优"位置。这类数字几乎一定是手滑（少按一个 0 或多写一个负号），
      当场报错比让它静静躺在库里污染比较结果好得多。
    """
    _require_positive(salary_month, "--salary-month（月薪）")
    _require_positive(months, "--months（发几个月）")

    application = _application_for(session, job_id)
    _advance_to_offer(session, job_id, source="jh offer add")

    job = session.get(Job, job_id)
    offer = Offer(
        application_id=application.id,
        # ★ 冗余抄一份 company/title：offer 到手后往往要脱离原投递记录做横向比较
        #   （甚至投递记录被清理），compare 不该依赖 join 的完整性。
        company=job.company if job else None,
        title=job.title if job else None,
        salary_text=salary_text,
        salary_month=salary_month,
        months=months,
        location=location,
        deadline_at=deadline_at,
        status="待定",
        pros=pros,
        cons=cons,
        note=note,
    )
    session.add(offer)
    session.add(
        ApplicationEvent(
            application_id=application.id,
            event_type="offer_received",
            payload_json=to_json(
                {
                    "company": offer.company,
                    "salary_text": salary_text,
                    "salary_month": salary_month,
                    "months": months,
                    "deadline_at": deadline_at.isoformat() if deadline_at else None,
                    "source": f"jh offer add --job {job_id}",
                }
            ),
            occurred_at=now_utc(),
        )
    )
    session.flush()
    return offer


def _require_positive(value: int | None, label: str) -> None:
    """数值字段给了就必须是正数（None = 用户没填，允许）。"""
    if value is not None and value < 1:
        raise OfferError(f"{label} 必须是正数，收到 {value}（这类数字几乎一定是手滑）")


def _application_for(session: Session, job_id: int) -> Application:
    """取这个岗位的投递记录；没有就现建一条 pending。

    ★ 与 `assessment_svc._application_for` 是**刻意**的两份：
      错误类型不同（OfferError / AssessmentError），而且各自要讲清楚自己的场景。
      真正的共享部分（`ensure_pending_application` 的幂等口径）已经在 apply_svc 里。
    """
    job = session.get(Job, job_id)
    if job is None:
        raise OfferError(f"找不到岗位 id={job_id}")
    application = apply_svc.get_latest_application(session, job.id)
    if application is None:
        application, _created = apply_svc.ensure_pending_application(session, job)
    return application


def _advance_to_offer(session: Session, job_id: int, *, source: str) -> None:
    """把投递状态推进到 `offer`，但**只前进不回退**（已 rejected 的不"复活"）。

    ★ 复用一个已有的历史包袱会让人很难受：被拒的岗位通常是"流程已结束"的
      记录，补记一条 offer 信息（哪怕只是归档用）不该把它改回进行中，
      否则看板上的"在推进"数量会凭空多出来。
    """
    application = apply_svc.get_latest_application(session, job_id)
    if application is not None and application.status in _BEYOND_OFFER:
        logger.debug(
            "投递 #%s 当前是 %s（已到终点），登记 offer 不回退状态",
            application.id,
            application.status,
        )
        return
    apply_svc.set_status(session, job_id, "offer", source=source)


# ===========================================================================
# 列表与比较
# ===========================================================================
def list_offers(session: Session, *, now: datetime | None = None) -> list[OfferRow]:
    """列出所有 offer，按签约截止时间升序（没填截止日的排最后）。

    ★ 为什么按截止时间排而不是按年包排：这是一张**决策待办**表 ——
      "哪个快要签了"比"哪个钱多"更紧急，钱多的排前面会让人先看最不着急的那条。
      年包的排序/高亮由 `compare_rows` 的 `is_best` 负责（两件事分开做）。
    """
    moment = now or now_utc()
    rows = session.execute(
        select(Offer, Application.job_id)
        .join(Application, Offer.application_id == Application.id)
        .order_by(Offer.deadline_at.is_(None), Offer.deadline_at)
    ).all()
    return [
        OfferRow(offer=offer, job_id=job_id, days_left=days_between(moment, offer.deadline_at))
        for offer, job_id in rows
    ]


def compare_rows(rows: Sequence[OfferRow]) -> list[dict[str, Any]]:
    """把 offer 列表整理成可直接渲染的比较数据（**纯函数**，不碰数据库）。

    Returns:
        与输入等长的列表，每项：
            {"offer": Offer, "annual": int|None, "annual_wan": float|None,
             "days_left": int|None, "is_best": bool}

    ★ `is_best` 的口径（写死在这里，别处不许再判一次）：
      **年包最大且唯一**的行才是 `True`。并列最大时**一个都不标** ——
      高亮的意义是"有一个严格更好的选择"，两个都标等于告诉用户
      "这两个一样好"，而那是他自己也能看出来的，标了反而稀释了这个标记。

    ★ 算不出年包的行永远不是 `is_best`：宁可说"信息不足，比不了"，
      也不能把"没填月数"当成"年包最低"（那会让人直接排除掉一个可能更好的 offer）。

    ★ `annual_wan` 只做单位换算、**保留完整精度**：把"显示成 1 位小数"
      留给 fmt 层（CLI），服务层返回的数值不该被显示需求裁剪 ——
      否则以后想按年包排序/求差时会发现数据已经被四舍五入过了。
    """
    annuals = [row.offer.annual_package for row in rows]
    computable = [annual for annual in annuals if annual is not None]
    best = max(computable) if computable else None
    best_count = sum(1 for annual in annuals if annual == best) if best is not None else 0

    return [
        {
            "offer": row.offer,
            "annual": annual,
            "annual_wan": None if annual is None else annual / 10000,
            "days_left": row.days_left,
            "is_best": best_count == 1 and annual == best,
        }
        for row, annual in zip(rows, annuals, strict=True)
    ]

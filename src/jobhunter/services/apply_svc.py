"""投递记录：`applications`（当前状态）+ `application_events`（append-only 事件流）。

★ 本模块只做三件事，都很小 —— 因为"投递状态"是**用户的资产**，
  逻辑越少越不容易写坏：

    ① 准备一条 pending 投递（`jh today` 生成开场白时写）
    ② 改状态（`jh apply <n> --status applied`），**每次都追加一条事件**
    ③ 数"今天投了几条"（**实时聚合**，不建计数表）

★ 为什么不建计数表：计数器和真实记录对不上是最难查的一类不一致
  （`models/application.py` 里写明了这条取舍）。日额度直接从
  `applications.applied_at` 聚合，永远与明细一致。

★ 事件流只允许 INSERT：UPDATE/DELETE 会破坏"历史不可篡改"，
  而统计口径的可复现性完全建立在这个前提上。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from jobhunter.models import Application, ApplicationEvent, Job
from jobhunter.models.constants import APPLICATION_STATUSES, EVENT_TYPES
from jobhunter.utils.io import to_json
from jobhunter.utils.logging import get_logger
from jobhunter.utils.time import days_since, now_utc, to_local

logger = get_logger(__name__)

__all__ = [
    "ApplicationError",
    "ApplyResult",
    "GreetingWriteback",
    "PendingRow",
    "SKIP_TOKEN",
    "WAITING_STATUSES",
    "count_applied_today",
    "ensure_pending_application",
    "get_latest_application",
    "list_actionable",
    "mark_greeting",
    "record_event",
    "replace_greeting",
    "set_status",
    "skip_job",
]


class ApplicationError(ValueError):
    """投递状态操作失败（非法状态、找不到记录等）。"""


#: 状态 → 该状态对应的时间戳列名（打标时顺手补上，看板与统计都要用）
_STATUS_TIMESTAMP: dict[str, str] = {
    "applied": "applied_at",
    "viewed": "viewed_at",
    "replied": "replied_at",
}
#: 打标后同步到 `jobs.status` 的状态（job 的状态机只有 new/scored/queued/applied/skipped）
_JOB_STATUS_SYNC = {"applied": "applied", "rejected": "applied"}

#: ★ ISSUE #48：`skipped` **不是投递状态**，它是**岗位**状态（`JOB_STATUSES` 里的
#:  "我主动跳过，不想投"）。设计文档【提示词 6】却把 `jh apply 3=applied 12=skipped`
#:  当成零手打范例写死 —— 照文档敲得退出码 2，对"一条命令处理一整轮"这个卖点
#:  是实打实的伤害。所以这里显式接受它：语义 = "这个岗我不投了"。
#:  · 有投递记录 → 记一条 `note` 事件 + 把 applications.status 置 rejected，
#:    否则 `jh apply --pending` 会一直催一条我已经决定不投的岗；
#:    ⚠️ **不写 status_change 事件**（那会声称一次没发生过的投递状态流转，时间轴说谎）
#:  · 没有投递记录（还没生成开场白）→ 只动 jobs.status，并明确说出来
SKIP_TOKEN = "skipped"


@dataclass
class ApplyResult:
    """一次状态变更的结果（供 CLI 打印）。"""

    application_id: int
    job_id: int
    from_status: str
    to_status: str
    changed: bool
    #: ★ ISSUES #37 的收尾：这次打标**推定**了"开场白已发出"（`greeting_sent=True`）。
    #:   推定必须被调用方**说出来**给用户看 —— 不说就是一个自己没做过的动作
    #:   出现在统计里（本项目最反对的静默失真）。CLI 会据此打一行提示 + 纠正命令。
    greeting_inferred: bool = False
    #: 这次把之前的推定**纠正**回了 False（`--no-sent`）。CLI 也要说一句 ——
    #: 不然用户只看到"本来就是 applied，没有改动"，不知道纠正生效了没有。
    greeting_corrected: bool = False


def get_latest_application(session: Session, job_id: int) -> Application | None:
    """取某个岗位最近的一条投递记录（一个岗位理论上只会有一条）。"""
    return session.scalars(
        select(Application).where(Application.job_id == job_id).order_by(Application.id.desc())
    ).first()


def record_event(
    session: Session,
    application_id: int,
    event_type: str,
    *,
    from_status: str | None = None,
    to_status: str | None = None,
    payload: dict[str, object] | None = None,
    occurred_at: datetime | None = None,
) -> ApplicationEvent:
    """往事件流里追加一条**经过校验**的事件（唯一入口）。

    ★ 服务层也要校验（ISSUE #42），不能只靠数据库的 CHECK：
      数据库报的是 `IntegrityError: CHECK constraint failed`，对用户来说
      那是一句"工具坏了"；这里报的是
      「事件类型 'status_chnage' 不认识。可用：status_change、greeting_sent、…」。
      两道都要有 —— 数据库那道保证"任何路径都写不进脏数据"（包括将来的新代码
      和手工 SQL），这一道保证**用户看到的是人话**。

    ★ 字段名用 `payload`（dict）而不是 `payload_json`（str）：
      调用方不该自己 json.dumps（口径统一在 `utils/io.to_json`）。
    """
    if event_type not in EVENT_TYPES:
        raise ApplicationError(f"事件类型 {event_type!r} 不认识。可用：{'、'.join(EVENT_TYPES)}")
    event = ApplicationEvent(
        application_id=application_id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        payload_json=to_json(payload) if payload is not None else None,
        occurred_at=occurred_at or now_utc(),
    )
    session.add(event)
    return event


def ensure_pending_application(
    session: Session,
    job: Job,
    *,
    resume_id: int | None = None,
    greeting: str | None = None,
    greeting_variant: str | None = None,
) -> tuple[Application, bool]:
    """确保这个岗位有一条 pending 投递记录（幂等）。

    Returns:
        (记录, 是否新建)

    ★ 为什么幂等：`jh today` 会被反复执行（每次想看清单就跑一次）。
      不幂等的话，跑 3 次就有 3 条 pending，额度统计与"待处理列表"全部失真。
      判定口径：**同一个岗位只允许存在一条非终态的投递记录**。

    ★★ 两道防线（只读代码审查 JH-11），缺一不可：

      ① 库层：`applications.job_id` 上的 **UNIQUE 索引**（迁移 0006）——
         两个标签页 / 两个进程 / 重发的请求同时走到这里时，只有一个能插进去；
      ② 服务层：唯一冲突**不是报错给用户**，而是回到"已有这条"的语义上
         （重新读出来复用它）—— 用户的意图是"确保有这么一条"，
         而冲突恰恰说明它已经存在了，此时报错才是错的。

      ⚠️ 为什么用 `begin_nested()`（SAVEPOINT）：冲突只该回滚**这一次 INSERT**，
      不能把调用方事务里已经写好的别的东西一起回滚（例如 `jh today` 刚写好的
      开场白）。普通 `session.rollback()` 会连那些一起丢掉 —— 那才是更贵的 bug。
    """
    existing = get_latest_application(session, job.id)
    if existing is not None:
        _fill_greeting(existing, greeting=greeting, greeting_variant=greeting_variant)
        session.flush()
        return existing, False

    application = Application(
        job_id=job.id,
        platform=job.platform,
        channel="招聘网站",
        greeting_text=greeting,
        greeting_variant=greeting_variant,
        status="pending",
    )
    try:
        with session.begin_nested():
            session.add(application)
            session.flush()
    except IntegrityError:
        # 竞态：另一个写入口在我们"查过之后、插入之前"建好了同岗位的记录。
        # 这不是用户要处理的错误 —— 重新读出来给它，语义与幂等路径完全一致。
        raced = get_latest_application(session, job.id)
        if raced is None:  # pragma: no cover - 唯一冲突必然对应一行存在
            raise
        logger.info(
            "岗位 #%s 的投递记录已被另一个写入口建好，复用它（唯一索引挡住的那次插入）", job.id
        )
        _fill_greeting(raced, greeting=greeting, greeting_variant=greeting_variant)
        session.flush()
        return raced, False
    return application, True


def _fill_greeting(
    application: Application,
    *,
    greeting: str | None,
    greeting_variant: str | None,
) -> None:
    """只补空字段，绝不覆盖已有开场白（用户可能已经手改过）。"""
    if greeting and not application.greeting_text:
        application.greeting_text = greeting
    if greeting_variant and not application.greeting_variant:
        application.greeting_variant = greeting_variant


@dataclass
class GreetingWriteback:
    """一次"把刚生成的话术写回库"的结果（供 CLI 如实说出来）。"""

    application_id: int
    #: 库里的文本是否**真的**被改了（文本一样就不写，避免无意义的 UPDATE）
    changed: bool
    #: 覆盖前库里的旧文本（原来是空的就是 None）
    previous: str | None = None
    #: ★ 覆盖前这条投递是否已被标记为「已发出」（ISSUE #37 的闭环字段）。
    #:   回写**不碰**它，但 CLI 要能提醒：库里文本换了新的，而"发过"这个事实不变。
    was_sent: bool = False


def replace_greeting(
    session: Session,
    job_id: int,
    *,
    greeting: str,
    variant: str | None = None,
) -> GreetingWriteback | None:
    """把**刚生成**的开场白写回库，让"屏幕上看到的"与"库里存着的"始终一致（ISSUE #41）。

    ★ 为什么需要这个入口（验证会话实测的症状）：

        `jh today --copy N` 在**缓存失效**时会重新调模型 → 屏幕上是一段**新文本**，
        而 `ensure_pending_application` 只补空字段（那是为了保护用户手写的内容），
        于是库里还是旧值 —— 用户下次在别处看到旧话术，会以为"这工具记错了"。

      ★ 与 `ensure_pending_application` 的方向**正好相反**，别混用：
        · 那个是"只补空"（清单路径 `jh today` 用它，保护人工内容）；
        · 这个是**显式覆盖**，只由 `jh today --copy N` 调用 ——
          那个场景下屏幕上已经是一段新文本，库内必须跟上（用户裁决 #41-A：
          库内始终最新）。

    ★★ 绝不碰 `greeting_sent` / `greeting_edited`（ISSUE #37 的合规红线）：
      "生成"不等于"已发出"。回写文本**不能**顺手把闭环字段置位；
      已经置位的值也原样保留（那是"我确实发过话术"这个事实，
      不是"这一段文本发过"）。CLI 会在 `was_sent=True` 时额外提醒一句。

    Returns:
        `None` 表示这个岗位还没有投递记录（没跑过 `jh today`）—— 此时没有可写的对象。
    """
    application = get_latest_application(session, job_id)
    if application is None:
        return None

    previous = application.greeting_text
    changed = previous != greeting
    if changed:
        application.greeting_text = greeting
    # 变体跟着一起同步：话术与变体是同一份账，
    # 只换文本不换变体，A/B 统计会把这段文本算到另一个变体头上。
    if variant and application.greeting_variant != variant:
        application.greeting_variant = variant
    session.flush()
    return GreetingWriteback(
        application_id=application.id,
        changed=changed,
        previous=previous,
        was_sent=bool(application.greeting_sent),
    )


def mark_greeting(
    session: Session,
    job_id: int,
    *,
    sent: bool | None = None,
    edited: bool | None = None,
) -> Application | None:
    """只改开场白的闭环字段（不动投递状态）—— `jh today --copy N --sent/--edited` 用。

    ★★ 为什么必须有一个"只置位、不改状态"的入口（ISSUE #37）：
      `greeting_sent` / `greeting_edited` 两列在原来的代码里**没有任何调用方传参**，
      于是恒为 0 —— "这个工具生成的话术到底有没有被用上"永远查不出来。

    ★★ 别把"生成"当成"发送"（合规红线）：
      生成只是产出一段文本，工具**绝不代用户声称发出去了** ——
      这两个字段只能由人的**真实动作**置位：
        · `jh today --copy N --sent`   ：我把它复制去粘贴了
        · `jh today --copy N --edited` ：我复制之后改过再发
        · `jh apply N --status applied --sent/--edited` ：随打标一起声明
      没给 `--sent` 时永远是"未发送"（工具不替用户说话）。

    ★ 对应的 `greeting_sent` / `greeting_edited` **事件**也在这里补：
      统计口径（时间轴 + `applications` 两列）必须同时有，只写一边会得到
      "列说发过、时间轴说没有"的矛盾数据。
    """
    application = get_latest_application(session, job_id)
    if application is None or not application.greeting_text:
        # 还没生成过开场白（`jh today` 没跑过）→ 没有可标记的对象，如实返回 None
        return None

    # ★ 置位与写事件的口径只有一份：`_settle_greeting_closure`
    #   （`set_status` 与这里共用，免得两处口径分叉）。
    if sent is True:
        application.greeting_sent = True
    if edited is True:
        application.greeting_edited = True
    _settle_greeting_closure(session, application, when=now_utc(), sent=sent, edited=edited)
    session.flush()
    return application


def _has_event(session: Session, application_id: int, event_type: str) -> bool:
    """这条投递是否已经有某类事件（避免重复写同一条痕迹）。"""
    count = session.scalar(
        select(func.count())
        .select_from(ApplicationEvent)
        .where(
            ApplicationEvent.application_id == application_id,
            ApplicationEvent.event_type == event_type,
        )
    )
    return bool(count)


def _settle_greeting_closure(
    session: Session,
    application: Application,
    *,
    when: datetime,
    sent: bool | None,
    edited: bool | None,
) -> None:
    """把开场白闭环字段（`greeting_sent` / `greeting_edited`）与事件流对齐。

    ★ 两道口径都要有（ISSUE #37）：
      · **列**（`applications.greeting_sent`）—— 看板/统计直接读它；
      · **事件**（`application_events`）—— 时间轴要能回答"什么时候发的"。
      只写一边会得到"列说发过、时间轴说没有"的矛盾数据。

    ★ 幂等：已有同类事件就不重复写。重复敲同一条命令不该在时间轴上留下两条
      一模一样的"话术已发"（几天后根本分不清哪条是真的 —— P4 验收时数出来的坑）。
    """
    if sent is False and application.greeting_sent:
        application.greeting_sent = False

    # ★ "补写丢失的痕迹"：先 `jh apply N --status applied` 再生成开场白时，
    #   之前那次 set_status 没有 greeting_text 可发，于是没有任何 greeting_sent 事件；
    #   等开场白后来补上（`ensure_pending_application` 补字段），痕迹要在这里落地，
    #   否则"话术有没有被用上"的统计会安静地漏掉这一类。
    if application.greeting_sent and not _has_event(session, application.id, "greeting_sent"):
        record_event(
            session,
            application.id,
            "greeting_sent",
            payload={"variant": application.greeting_variant},
            occurred_at=when,
        )

    if edited is True and application.greeting_text:
        # "发之前改过"：只在有开场白可改时才有意义
        application.greeting_edited = True
        if not _has_event(session, application.id, "greeting_edited"):
            record_event(
                session,
                application.id,
                "greeting_edited",
                payload={"variant": application.greeting_variant},
                occurred_at=when,
            )


def set_status(
    session: Session,
    job_id: int,
    status: str,
    *,
    occurred_at: datetime | None = None,
    source: str = "jh apply",
    note: str | None = None,
    greeting_sent: bool | None = None,
    greeting_edited: bool | None = None,
) -> ApplyResult:
    """把某个岗位的投递记录改成新状态，并**追加一条事件**。

    Args:
        status: 必须是 `APPLICATION_STATUSES` 里的值 —— 写了不认识的状态要报错，
                而不是静默建一个野状态（那会让看板统计出现永远对不上的分类）。
                唯一的例外是 `"skipped"`：它是**岗位**状态，见 `skip_job`
                （设计文档的范例 `jh apply 12=skipped` 要能跑通，ISSUE #48）。
        occurred_at: 业务时间（真实发生时间）。默认现在；补记时传历史时间。
        greeting_sent / greeting_edited: 闭环字段 —— 这个工具生成的话术到底有没有被用上。
            不传时：标成 `applied` 且有开场白 → 视为已发出（人点过投递才会到这一步）；
            显式传 `False` 可以纠正（例如只投了简历、没发开场白）。

    ★ 为什么 `from_status == to_status` 也算一次操作但不写事件：
      重复敲同一条命令是常见的手误，写一堆重复事件会污染时间轴；
      但返回值里标明 `changed=False`，让 CLI 能说清"本来就已是这个状态"。
    """
    if status == SKIP_TOKEN:
        return skip_job(session, job_id, source=source, occurred_at=occurred_at)
    if status not in APPLICATION_STATUSES:
        raise ApplicationError(f"状态 {status!r} 不认识。可用：{'、'.join(APPLICATION_STATUSES)}")

    application = get_latest_application(session, job_id)
    if application is None:
        # 还没生成过开场白就直接标"已投"是合法操作（用户可能在线下投过了）
        job = session.get(Job, job_id)
        if job is None:
            raise ApplicationError(f"找不到岗位 id={job_id}")
        application, _created = ensure_pending_application(session, job)

    from_status = application.status
    changed = from_status != status
    # ★ 纠正的判定必须在**改字段之前**取快照（踩过一次：下面 330 行会先把
    #   `application.greeting_sent` 赋成传进来的 False，再判断"有没有纠正"就永远是 False）。
    was_greeting_sent = bool(application.greeting_sent)
    application.status = status
    when = occurred_at or now_utc()

    column = _STATUS_TIMESTAMP.get(status)
    if column and getattr(application, column) is None:
        setattr(application, column, when)
    if note:
        application.note = note
    if greeting_sent is not None:
        application.greeting_sent = greeting_sent
    if greeting_edited is not None:
        application.greeting_edited = greeting_edited

    if changed:
        record_event(
            session,
            application.id,
            "status_change",
            from_status=from_status,
            to_status=status,
            payload={"source": source},
            occurred_at=when,
        )
    # ★ 闭环字段与它的事件（ISSUES #37）全部收在 `_settle_greeting_closure` 里：
    #   `set_status` 的决策点已经够多了（PLR0912），这段单独抽出来才读得清。
    greeting_inferred = False
    # 纠正："之前推定过已发，这次明确说没发" → 要把列改回 False 并让 CLI 说出来。
    # 用**改字段之前**的快照 `was_greeting_sent` 判断（见上面的说明）。
    greeting_corrected = greeting_sent is False and was_greeting_sent
    if greeting_sent is None and changed and status == "applied" and application.greeting_text:
        # 走到 applied 只可能是人点过投递按钮，而这个工具**只生成、不发送** ——
        # 所以"标成已投"时推定话术确实发出去了（显式 `--no-sent` 可以纠正）。
        # ⚠️ 这是**推定**，必须回传给 CLI 让它打一行提示：不说出来，
        #    用户就会在统计里看到一个自己没做过的动作（静默失真）。
        application.greeting_sent = True
        greeting_inferred = True
    _settle_greeting_closure(
        session,
        application,
        when=when,
        sent=greeting_sent,
        edited=greeting_edited,
    )

    job = session.get(Job, job_id)
    if job is not None and status in _JOB_STATUS_SYNC:
        # 岗位状态前进（不回退）：已 skipped 的岗位被标 applied 时也以 applied 为准，
        # 因为"真的投了"是更硬的事实。
        job.status = _JOB_STATUS_SYNC[status]

    session.flush()
    return ApplyResult(
        application_id=application.id,
        job_id=job_id,
        from_status=from_status,
        to_status=status,
        changed=changed,
        greeting_inferred=greeting_inferred,
        greeting_corrected=bool(greeting_corrected),
    )


def skip_job(
    session: Session,
    job_id: int,
    *,
    source: str = "jh apply",
    occurred_at: datetime | None = None,
) -> ApplyResult:
    """把**岗位**标成 skipped（"这个岗我不投了"）—— 设计文档范例 `12=skipped` 的落地。

    ★ 为什么需要它（ISSUE #48）：
      设计文档【提示词 6】把 `jh apply 3=applied 7=replied 12=skipped` 当范例写死，
      但 `skipped` 是**岗位**状态（`JOB_STATUSES`），不是投递状态 ——
      照文档敲会得到"状态 `skipped` 不认识" + 退出码 2。
      失败是安全的，但**照着文档敲的第一条命令就报错**，伤害的正是
      "一条命令处理一整轮"这个卖点。

    ★ 语义（写清楚，免得日后含糊）：
      · `jobs.status = "skipped"` —— 这是"我不投"的**权威**记录；
      · 如果已经有投递记录 → 同时把 `applications.status` 置 `rejected`
        （它不该再出现在 `jh apply --pending` 里被催），并追加一条 `note` 事件；
        ⚠️ **不写 status_change 事件**：那会声称一次"投递状态流转"，
        而实际上什么都没投过 —— 时间轴不许说谎。
      · 没有投递记录（还没生成开场白）→ 只动 `jobs.status`。
        返回的 `application_id` 用 `0` 占位（CLI 靠 `changed` 判断要不要报成功）。

    ★ 已 `applied` 的岗位不在这里改：`set_status` 的注释里写了"岗位状态前进不回退"，
      但那是针对 `applied` 的。**skip 一个已经投出去的岗位**是不可能的操作
      （投都投了，"不投"没有意义）→ 明确报错，而不是悄悄改一个假的岗位状态。
    """
    job = session.get(Job, job_id)
    if job is None:
        raise ApplicationError(f"找不到岗位 id={job_id}")

    application = get_latest_application(session, job_id)
    if application is not None and application.status != "pending":
        raise ApplicationError(
            f"岗位 #{job_id} 已经有过投递动作（当前状态 {application.status} 不是 pending），"
            "不能再标 skipped —— 已投出去的岗位请改用 "
            f"`jh apply #{job_id} --status rejected` 表达「对方拒了 / 我不跟了」。"
        )

    from_status = job.status
    changed = from_status != SKIP_TOKEN
    job.status = SKIP_TOKEN
    if application is not None:
        # 有 pending 记录：置 rejected 让它退出待处理清单 + 留一条 note（不是 status_change）
        application.status = "rejected"
        record_event(
            session,
            application.id,
            "note",
            payload={"source": source, "job_status": SKIP_TOKEN},
            occurred_at=occurred_at or now_utc(),
        )
    session.flush()
    return ApplyResult(
        application_id=application.id if application is not None else 0,
        job_id=job_id,
        from_status=from_status,
        to_status=SKIP_TOKEN,
        changed=changed,
    )


def count_applied_today(session: Session, *, now: datetime | None = None) -> int:
    """今天已经投了几条（按**北京时间自然日**算）。

    ★ 为什么按北京自然日而不是 UTC：用户说"今天投了 5 条"指的是他的今天。
      UTC 会比北京晚 8 小时，晚上 8 点后投的会被算到"明天"，额度就白算了。
    """
    local_now = to_local(now or now_utc())
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    start_utc = local_start.astimezone(local_now.tzinfo).astimezone(now_utc().tzinfo)
    end_utc = local_end.astimezone(local_now.tzinfo).astimezone(now_utc().tzinfo)

    total = session.scalar(
        select(func.count())
        .select_from(Application)
        .where(
            Application.applied_at.is_not(None),
            Application.applied_at >= start_utc,
            Application.applied_at < end_utc,
        )
    )
    return int(total or 0)


# ===========================================================================
# 待处理清单（`jh apply --pending`）
# ===========================================================================
@dataclass
class PendingRow:
    """一条"需要我动手"的投递。"""

    job_id: int
    application_id: int | None
    company: str
    title: str
    status: str
    #: 投出去几天了（没投则为 None）
    days: int | None
    #: 为什么它出现在这里（人话）
    reason: str

    @property
    def label(self) -> str:
        return f"{self.company or '—'} · {self.title or '（未写职位）'}"


#: ★★ "该催谁"的**唯一口径**（ISSUE #49）。
#: 顶部提醒（`reminder_svc._stale_counts`）与待处理清单（`list_actionable`）
#: **必须**用同一套状态，否则同一份数据在两个入口给出相反建议 ——
#: 用户刚约上面试，待处理清单却说"投后 30 天没动静，建议放弃或换个渠道"，
#: 这会直接摧毁他对这个工具的信任（两个入口互相打脸）。
#:
#: 判定依据：`applied` / `viewed` 是"**在等对方回应**"，才需要催；
#:   · `pending`      → 还没投，该催（理由不同："还没投"）
#:   · `replied`      → 对方回过了，轮到我回话，该催（但理由不是"没动静"）
#:   · `assessment`   → 流程已经启动（笔试/测评在手），催"放弃"是错的
#:   · `interview`    → 同上，刚约上面试
#:   · `offer` / `rejected` → 终态，不该出现
#: `reminder_svc._WAITING_STATUSES` 与之对应的子集是 `("applied", "viewed")`，
#: `tests/unit/test_pending_consistency.py` 会把两者放在一起断言（再分叉就变红）。
WAITING_STATUSES: tuple[str, ...] = ("applied", "viewed")
#: 清单里还要额外包含这些（理由与"没动静"不同，但同样是"需要我动手"）
_ACTIONABLE_EXTRA_STATUSES: tuple[str, ...] = ("pending", "replied")
#: 清单能出现的全部状态
_ACTIONABLE_STATUSES: tuple[str, ...] = ("pending", *WAITING_STATUSES, *_ACTIONABLE_EXTRA_STATUSES)


def list_actionable(session: Session, *, now: datetime | None = None) -> list[PendingRow]:
    """列出"需要我动手"的投递：还没投的 + 投了以后在等对方回应的。

    ★ 为什么把"还没投的"也算进来：`jh today` 生成了开场白就写了一条 `pending`，
      它代表"简历已经准备好、就差我点投递"。这正是最该被提醒的一类。

    ★★ 为什么 `assessment` / `interview` **不在这里**（ISSUE #49）：
      它们表示"流程已经启动"，理由句子却只有"投后 N 天没动静，建议放弃"，
      于是刚约上面试的岗位被建议"放弃"。同一份数据在顶部提醒与待处理清单
      两个入口给出相反建议，比少提醒一条危险得多。
      口径统一在 `_ACTIONABLE_STATUSES` / `WAITING_STATUSES`（见常量注释），
      元测试 `test_pending_and_reminders_agree_on_who_to_nag` 会守住它。

    ★ 排序：先按"紧急程度"（超过 14 天 > 7 天 > 3 天 > 还没投），
      再按投出时间从早到晚 —— 最该处理的排最前面，因为这是**行动清单**。
    """
    moment = now or now_utc()
    rows = session.execute(
        select(Application, Job)
        .join(Job, Application.job_id == Job.id)
        .where(Application.status.in_(_ACTIONABLE_STATUSES))
        .order_by(Application.applied_at.is_(None).desc(), Application.applied_at)
    ).all()

    out: list[PendingRow] = []
    for application, job in rows:
        days = days_since(application.applied_at, now=moment) if application.applied_at else None
        if days is None:
            reason = "还没投（开场白已生成）"
        elif application.status not in WAITING_STATUSES:
            # replied：对方回过了，轮到我 —— 理由**不能**说"没动静"（那与事实相反）
            reason = f"对方已回复 {days} 天，该我回话了"
        elif days >= 14:
            reason = f"投后 {days} 天没动静，建议放弃或换个渠道"
        elif days >= 7:
            reason = f"投后 {days} 天没动静"
        elif days >= 3:
            reason = f"投后 {days} 天，该跟进一下了"
        else:
            reason = f"投后 {days} 天"
        out.append(
            PendingRow(
                job_id=job.id,
                application_id=application.id,
                company=job.company or "",
                title=job.title or "",
                status=application.status,
                days=days,
                reason=reason,
            )
        )
    # 紧急度排序：None（没投）排最后，其余按天数降序
    out.sort(key=lambda row: (row.days is None, -(row.days or 0), row.job_id))
    return out

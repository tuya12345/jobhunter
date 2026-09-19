"""日历导出：把面试 / 笔试截止 / Offer 签约截止导出成 `.ics`（P6）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 为什么是"导一份 .ics"，而不是做一个提醒系统                                 ║
╚══════════════════════════════════════════════════════════════════════════╝

常驻进程 + 系统通知意味着要处理开机自启、跨平台通知权限、后台资源占用……
而这台工具的使用频率是"每天跑几条命令"。导一份 `.ics` 丢进手机日历，
成本几乎为零、可靠性反而更高（手机日历的提醒永远不会因为你忘了敲命令而消失）。

★ 数据来源只读 `interviews` / `assessments` / `offers` 三张表，
  时间窗口与 join 风格**照抄 `reminder_svc._upcoming_*`** —— 顶部提醒说
  "9-11 笔试截止"，日历里就必须能看到同一条；两处口径不一致是这类工具最伤口碑的 bug。

★ 只导"还没发生/还没结束"的事：
    · 面试：`status == "待面"` 且有 `scheduled_at`
    · 笔试/测评：`status == "待做"` 且有 `deadline_at`
    · Offer：`status == "待定"` 且有 `deadline_at`
  已经面完/做完/拒掉的再出现在日历里，只会让人每天多划掉几条无效事件。

╔══════════════════════════════════════════════════════════════════════════╗
║ `.ics` 里四个"能生成文件但导入就出错"的地方（本模块的主要复杂度都在这）      ║
╚══════════════════════════════════════════════════════════════════════════╝

① **CRLF 行尾**（RFC 5545 §3.1 要求 CRLF）。`\n` 结尾的文件在 Windows 自带日历/
   Google 日历上可能被当成一行超长内容而解析失败。含**最后一行**也要有 CRLF。

② **75 字节折行**（§3.1）：物理行不超过 75 个**八位组**。坑在于"字节 ≠ 字符"：
   一个汉字 3 字节、emoji 4 字节。按字符折（75 字符）会写出最长 225 字节的行；
   按字节硬切会把一个汉字切成两半 —— 那是**不可逆的乱码**。
   所以 `_fold_line` 按字符累加字节数，在字符边界上折。

③ **UID 必须稳定且唯一**（§3.8.4.7）。日历客户端靠 UID 判断"这是不是同一个事件"。
   UID 里如果带时间戳/随机数，同一个面试每导出一次就多一条重复事件 ——
   用户导入三次就有三条一模一样的"一面"，会直接弃用这个功能。
   所以 UID 只由 `(kind, source_id)` 决定：`jobhunter-{kind}-{source_id}@local`。

④ **时间一律用 UTC 的 `YYYYMMDDTHHMMSSZ`**，不用"本地时间 + TZID"。
   本机是 Asia/Shanghai，但手机日历/Google 日历的时区库行为各不相同，
   `TZID=Asia/Shanghai` 在缺少 tzdata 的客户端上会被忽略甚至报错；
   而 `Z` 形式是绝对的、任何客户端都不可能理解错 —— 展示成本地时间由客户端自己做。

★ 事件时长的取舍（见 `_interview_events` / `_deadline_events` 的注释）：
  面试是**一段占用**（1 小时），笔试/Offer 截止只是**一个时间点**（30 分钟提醒）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunter.config.settings import AppSettings, get_settings
from jobhunter.models import Application, Assessment, Interview, Job, Offer
from jobhunter.utils.io import ensure_dir, write_text
from jobhunter.utils.time import now_utc, to_utc

__all__ = [
    "DEFAULT_DAYS",
    "DEFAULT_FILENAME",
    "DEFAULT_PROD_ID",
    "MAX_LINE_BYTES",
    "CalEvent",
    "CalExport",
    "build_events",
    "escape_text",
    "event_uid",
    "export_ics",
    "render_ics",
]

#: `jh cal export --days` 的默认窗口（天）。两个月足够覆盖"现在能看到的安排"，
#:  又不至于把半年后排期的面试当成确定事件写进日历。
DEFAULT_DAYS = 60
#: 默认文件名。`--out` 不传时写 `settings.paths.output_dir / 这个`。
DEFAULT_FILENAME = "jobhunter.ics"
#: 日历的 PRODID（标识"这个 .ics 是谁生成的"，出问题时便于回溯）
DEFAULT_PROD_ID = "-//JobHunter//求职猎手//CN"
#: UID 的域名部分。用 `@local` 而不是真实域名：这是本机生成的私有事件，
#:  不冒用任何真实域名，也不会和别的日历产品撞 UID。
UID_DOMAIN = "local"
#: RFC 5545 §3.1：物理行不超过 75 个八位组（octets = UTF-8 字节）
MAX_LINE_BYTES = 75

#: 面试时长：1 小时。面试是"一段时间被占用"，日历上留出可见的块才不会撞车。
INTERVIEW_MINUTES = 60
#: 截止提醒时长：30 分钟。**这不是"要花 30 分钟做的事"**，只是让截止时间点在
#:  日历上有个可见的最小跨度（零长度事件在部分客户端里不显示，或者要用手势才能看到）。
DEADLINE_MINUTES = 30

#: 面试"形式"为视频时不写 location：视频面试没有物理地点，
#:  有些记录习惯会把会议链接粘在 location 字段里，那应该出现在 URL 和描述里。
VIDEO_MODE = "视频"


@dataclass
class CalEvent:
    """一条待写入日历的事件（与 ORM 解耦的纯数据，便于直接断言）。

    ★ 与设计契约的一处偏离（有意）：契约的字段列表里没有 `url`，
      但又要求"有 meeting_link 时把它作为 `URL` 属性输出"。
      为了不把结构化数据降级成描述文本、再回头用字符串解析把链接抠出来
      （那是最容易出错的一种做法），这里补了一个**带默认值**的 `url` 字段。
      它是最后一个字段，所以按位置构造 7 个参数的老写法完全不受影响。
    """

    kind: str  # interview / assessment / offer
    source_id: int  # 对应表的主键（UID 稳定性的全部来源）
    title: str
    start: datetime  # UTC aware
    end: datetime  # UTC aware
    description: str = ""
    location: str = ""
    url: str = ""

    @property
    def uid(self) -> str:
        """日历里这个事件的唯一标识（只由 kind + 主键决定，与时间无关）。"""
        return event_uid(self.kind, self.source_id)


@dataclass
class CalExport:
    """一次导出的结果（路径 + 条数 + 是否覆盖了同名文件 + 事件本身）。"""

    path: Path
    event_count: int
    overwritten: bool
    events: list[CalEvent]


# ===========================================================================
# 文本处理
# ===========================================================================
def event_uid(kind: str, source_id: int) -> str:
    """构造稳定的 UID：`jobhunter-{kind}-{source_id}@local`。

    ★ 绝不把时间写进 UID：面试改期（`scheduled_at` 变了）应当**更新**日历里那条事件，
      而不是新增一条。UID 只认"哪张表的哪一行"，改期在日历客户端上表现为该事件被更新，
      这正是我们想要的语义。
    """
    return f"jobhunter-{kind}-{source_id}@{UID_DOMAIN}"


def escape_text(value: str) -> str:
    """按 RFC 5545 §3.3.11 转义 TEXT 值。

    顺序很重要：**必须先转反斜杠**。否则后面 `,` → `\\,` 生成的那个反斜杠
    会被再转一次，变成 `\\\\,`，导进日历就会看到多余的反斜杠。
    换行必须先处理 `\\r\\n` 再处理 `\\n`：Windows 上从文本框粘进来的备注常常是 CRLF，
    两处都替换成 `\\n`（ICS 里没有"换行"这种字符，只有转义序列 `\\n` ——
    这也是为什么 .ics 里不能出现真实的裸换行）。
    """
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold_line(line: str) -> str:
    """把一行折成不超过 75 字节的物理行（续行以**一个空格**开头）。

    ★ 为什么不能按字符折：中文一个字 3 字节，75 个汉字就是 225 字节 ——
      生成的文件看起来完全正常，导进日历却可能被客户端截断或拒绝。
    ★ 为什么不能按字节硬切：会在多字节字符中间断开，解码直接坏掉（不可逆）。
      所以这里**逐字符累加它自己的 UTF-8 字节数**，只在字符边界上换行。

    续行那个前导空格**算进 75 字节**里（它是这一行的内容，不是免费的），
    因此第一行的预算是 75 字节，后续每行是 74 字节内容 + 1 字节空格。

    返回的字符串内部用 `\\r\\n ` 连接；调用方负责在最后追加行尾 CRLF。
    """
    if len(line.encode("utf-8")) <= MAX_LINE_BYTES:
        return line

    chunks: list[str] = []
    current: list[str] = []
    used = 0
    limit = MAX_LINE_BYTES
    for char in line:
        size = len(char.encode("utf-8"))
        if used + size > limit:
            chunks.append("".join(current))
            current = []
            used = 0
            limit = MAX_LINE_BYTES - 1  # 续行要带一个前导空格
        current.append(char)
        used += size
    chunks.append("".join(current))
    return "\r\n ".join(chunks)


def _utc_stamp(moment: datetime) -> str:
    """UTC 时间的 ICS 表示：`20260911T060000Z`（§3.3.5 DATE-TIME 的 UTC 形式）。"""
    return to_utc(moment).strftime("%Y%m%dT%H%M%SZ")


def _join_parts(parts: Iterable[str | None], *, sep: str = " · ") -> str:
    """把非空字段用 ` · ` 连起来。

    ★ 缺字段就**省略**，绝不让 `None` 或空串出现在标题里
      （`面试：某某科技 · None（None）` 这种标题会让人以为工具坏了）。
    """
    return sep.join(cleaned for part in parts if (cleaned := (part or "").strip()))


# ===========================================================================
# 采集
# ===========================================================================
def _interview_events(session: Session, *, now: datetime, horizon: datetime) -> list[CalEvent]:
    """窗口内还没面的面试。

    时长固定 1 小时：日历里面试是"这段时间我没空"，有跨度才能和别的安排比对。
    没有结束时间的面试只会显示成一条细线，容易看漏。
    """
    rows = session.execute(
        select(
            Interview.id,
            Interview.scheduled_at,
            Interview.round,
            Interview.mode,
            Interview.meeting_link,
            Interview.location,
            Interview.note,
            Job.company,
            Job.title,
        )
        .join(Application, Interview.application_id == Application.id)
        .join(Job, Application.job_id == Job.id)
        .where(
            Interview.status == "待面",
            Interview.scheduled_at.is_not(None),
            Interview.scheduled_at >= now,
            Interview.scheduled_at <= horizon,
        )
        .order_by(Interview.scheduled_at)
    ).all()

    events: list[CalEvent] = []
    for (
        interview_id,
        scheduled_at,
        round_name,
        mode,
        meeting_link,
        location,
        note,
        company,
        job_title,
    ) in rows:
        start = to_utc(scheduled_at)
        who = _join_parts((company, job_title))
        title = f"面试：{who}" if who else "面试"
        if round_name:
            title = f"{title}（{round_name}）"

        # 描述里放"去打这场面试需要知道的一切"：轮次 / 形式 / 链接 / 备注
        lines = []
        if round_name:
            lines.append(f"轮次：{round_name}")
        if mode:
            lines.append(f"形式：{mode}")
        if meeting_link:
            lines.append(f"会议链接：{meeting_link}")
        if note:
            lines.append(f"备注：{note}")

        events.append(
            CalEvent(
                kind="interview",
                source_id=interview_id,
                title=title,
                start=start,
                end=start + timedelta(minutes=INTERVIEW_MINUTES),
                description="\n".join(lines),
                # 视频面试没有物理地点（location 字段里常被粘进会议链接，
                # 那是 URL 该管的事）→ 留空，避免日历里出现一个点不开的"地点"
                location="" if mode == VIDEO_MODE else (location or ""),
                url=meeting_link or "",
            )
        )
    return events


def _assessment_events(session: Session, *, now: datetime, horizon: datetime) -> list[CalEvent]:
    """窗口内还没做的笔试 / 测评 / AI 面。

    ★ 时长只有 30 分钟，因为这是**截止提醒**而不是"一段要占用的时间"：
      笔试实际要做 60-120 分钟，但那个时间由我自己挑（截止前任意时刻都行）。
      如果按 90 分钟写进日历，就会在某个我根本没打算做题的时刻显示"忙"，
      反而会挡住真正要安排的事。所以这里只标记"这个点之前必须做完"。
    """
    rows = session.execute(
        select(
            Assessment.id,
            Assessment.deadline_at,
            Assessment.kind,
            Assessment.platform,
            Assessment.note,
            Job.company,
            Job.title,
        )
        .join(Application, Assessment.application_id == Application.id)
        .join(Job, Application.job_id == Job.id)
        .where(
            Assessment.status == "待做",
            Assessment.deadline_at.is_not(None),
            Assessment.deadline_at >= now,
            Assessment.deadline_at <= horizon,
        )
        .order_by(Assessment.deadline_at)
    ).all()

    events: list[CalEvent] = []
    for assessment_id, deadline_at, kind, platform, note, company, job_title in rows:
        # 公司名缺失时退到岗位名（与 reminder_svc 的 `company or title or kind` 同一口径），
        # 再缺就退到类型本身 —— 标题永远可读，不会出现"笔试截止：None"
        who = _join_parts((company or job_title, kind))
        title = f"笔试截止：{who or '笔试/测评'}"

        lines = []
        if kind:
            lines.append(f"类型：{kind}")
        if platform:
            lines.append(f"平台：{platform}")
        if note:
            lines.append(f"备注：{note}")

        start = to_utc(deadline_at)
        events.append(
            CalEvent(
                kind="assessment",
                source_id=assessment_id,
                title=title,
                start=start,
                end=start + timedelta(minutes=DEADLINE_MINUTES),
                description="\n".join(lines),
            )
        )
    return events


def _offer_events(session: Session, *, now: datetime, horizon: datetime) -> list[CalEvent]:
    """窗口内还没定的 Offer 签约截止。

    ★ 用 **outerjoin**（reminder_svc 的 offer 查询干脆不 join）：
      `offers` 冗余存了 company/title，正是为了"投递记录被清理后仍能比较"。
      这里 join 只是为了补全缺失的字段，**不能因为关联不到岗位就把这条 offer 丢掉** ——
      丢掉的后果是错过签约截止，这正是本功能要防的"沉默失败"。
    """
    rows = session.execute(
        select(
            Offer.id,
            Offer.deadline_at,
            Offer.company,
            Offer.title,
            Offer.salary_text,
            Offer.note,
            Job.company,
            Job.title,
        )
        .outerjoin(Application, Offer.application_id == Application.id)
        .outerjoin(Job, Application.job_id == Job.id)
        .where(
            Offer.status == "待定",
            Offer.deadline_at.is_not(None),
            Offer.deadline_at >= now,
            Offer.deadline_at <= horizon,
        )
        .order_by(Offer.deadline_at)
    ).all()

    events: list[CalEvent] = []
    for (
        offer_id,
        deadline_at,
        offer_company,
        offer_title,
        salary_text,
        note,
        job_company,
        job_title,
    ) in rows:
        company = (offer_company or job_company or "").strip()
        title = f"Offer 签约截止：{company}" if company else "Offer 签约截止"

        lines = []
        role = _join_parts((offer_title, job_title))
        if role:
            lines.append(f"职位：{role}")
        if salary_text:
            lines.append(f"薪资：{salary_text}")
        if note:
            lines.append(f"备注：{note}")

        start = to_utc(deadline_at)
        events.append(
            CalEvent(
                kind="offer",
                source_id=offer_id,
                title=title,
                start=start,
                end=start + timedelta(minutes=DEADLINE_MINUTES),
                description="\n".join(lines),
            )
        )
    return events


def build_events(
    session: Session, *, days: int = DEFAULT_DAYS, now: datetime | None = None
) -> list[CalEvent]:
    """采集未来 `days` 天内的事件，按开始时间升序返回。

    Args:
        days: 窗口长度（天）。0 表示"只看此刻"，用于"我这会儿有事吗"。
        now:  测试注入的"现在"（UTC）。不传则取 `now_utc()`。
    """
    moment = to_utc(now) if now is not None else now_utc()
    horizon = moment + timedelta(days=days)

    events = [
        *_interview_events(session, now=moment, horizon=horizon),
        *_assessment_events(session, now=moment, horizon=horizon),
        *_offer_events(session, now=moment, horizon=horizon),
    ]
    # 一律按开始时间升序。同一时刻再按 (kind, source_id) 兜底排序：
    # 数据库不保证同值行的返回顺序，不加这一层的话"同一份数据渲染两次"
    # 可能得到字节不同的文件 —— 而日历客户端会把它当成内容变化。
    events.sort(key=lambda event: (event.start, event.kind, event.source_id))
    return events


# ===========================================================================
# 渲染
# ===========================================================================
def _vevent_lines(event: CalEvent, *, stamp: str) -> list[str]:
    """把一条事件铺成 VEVENT 的逻辑行（折行由 `render_ics` 统一做）。"""
    lines = [
        "BEGIN:VEVENT",
        f"UID:{event.uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{_utc_stamp(event.start)}",
        f"DTEND:{_utc_stamp(event.end)}",
        f"SUMMARY:{escape_text(event.title)}",
    ]
    if event.description:
        lines.append(f"DESCRIPTION:{escape_text(event.description)}")
    if event.location:
        lines.append(f"LOCATION:{escape_text(event.location)}")
    if event.url:
        # ★ URL 是 URI 值类型，不是 TEXT：按 RFC 它**不做** TEXT 转义
        #   （把链接里的 `,` 写成 `\,` 反而会让客户端打不开这个链接）。
        lines.append(f"URL:{event.url}")
    lines.append("END:VEVENT")
    return lines


def render_ics(
    events: Iterable[CalEvent], *, now: datetime | None = None, prod_id: str = DEFAULT_PROD_ID
) -> str:
    """渲染成完整的 `.ics` 文本（**每行以 CRLF 结尾，含最后一行**）。

    `DTSTAMP` 用 `now`：它是"这个文件是什么时候生成的"，不是事件时间。
    事件时间（DTSTART/DTEND）一律 UTC `Z` 形式，见模块 docstring ④。

    事件为空时同样返回合法的空日历（只有 VCALENDAR 骨架，没有 VEVENT）——
    "今天没安排"不应该是一个需要用户处理的错误。
    """
    stamp = _utc_stamp(to_utc(now) if now is not None else now_utc())
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{prod_id}",
        "CALSCALE:GREGORIAN",
    ]
    for event in events:
        lines.extend(_vevent_lines(event, stamp=stamp))
    lines.append("END:VCALENDAR")

    # 先折行再加 CRLF：折行产生的续行分隔符本身也是 CRLF + 一个空格
    return "".join(f"{_fold_line(line)}\r\n" for line in lines)


# ===========================================================================
# 导出
# ===========================================================================
def export_ics(
    session: Session,
    *,
    out: str | Path | None = None,
    days: int = DEFAULT_DAYS,
    settings: AppSettings | None = None,
    now: datetime | None = None,
) -> CalExport:
    """导出 `.ics` 到磁盘，返回路径与事件清单。

    ★★ **这是"导出快照"语义：同名文件直接覆盖**（`overwritten=True`）。

        简历/清单导出**必须**只写新文件（见 `export_svc.unique_path` 与
        `utils/io.write_text` 的隐私红线）：那些是"一次性的产物"，
        覆盖掉就等于销毁了我可能需要回头查的东西。

        **日历是这条纪律的唯一例外**，理由是它被设计成**幂等快照**：
        同一个 `jobhunter.ics` 每次导出都是"当前全部待办的完整重放"。
        如果像简历那样生成 `jobhunter-2.ics` / `jobhunter-3.ics`，
        用户每次导入日历都会多出一整套重复事件（UID 相同也救不了 ——
        用户导入的是**另一个文件**，客户端只会问"要合并吗"，
        而人总是点"合并"），几百条重复事件之后这个功能就废了。
        固定文件名还带来一个好处：可以把它挂进云盘/脚本，重复执行无副作用。

    ★ 落盘用 `utils/io.write_text`：它写死 `encoding="utf-8"` 且 `newline=""`。
        - **没有 BOM**（`write_text` 用的是 `utf-8` 而不是 `utf-8-sig`）。这一点对本
          文件是硬要求：`.ics` 规范里 BOM 是非法的，很多客户端会直接解析失败。
          换句话说这里可以直接用它、不必绕开；但如果哪天有人把 `write_text` 改成
          带 BOM，`.ics` 就必须改走 `Path.write_text(..., encoding="utf-8", newline="")`。
        - `newline=""` 是**必须**的：我们拼好的 `\\r\\n` 会被原样写出。少了它，
          Windows 会把 `\\r\\n` 再翻译一次变成 `\\r\\r\\n`（每个事件都多一个空行）。
    """
    settings = settings or get_settings()
    events = build_events(session, days=days, now=now)
    content = render_ics(events, now=now)

    if out is None or (isinstance(out, str) and not out.strip()):
        path = settings.paths.output_dir / DEFAULT_FILENAME
    else:
        path = Path(out)

    ensure_dir(path.parent)
    overwritten = path.exists()
    write_text(path, content)
    return CalExport(path=path, event_count=len(events), overwritten=overwritten, events=events)

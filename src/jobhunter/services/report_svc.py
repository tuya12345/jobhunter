"""求职周报（`jh report`）—— 把这一周发生的事写成一页 Markdown（P6）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 为什么值得做（计划书原话）                                                  ║
╚══════════════════════════════════════════════════════════════════════════╝

    *"这份周报是给自己看的复盘，也是**面试时可以讲的东西**"* ——
    "我用数据复盘过自己的投递转化率"比"我很努力在投简历"有说服力得多。

★ 三条边界（都是这个功能最容易做歪的地方）：

    ① **只写新文件**：`output/report-<日期>.md`，绝不覆盖已有报告
       （与简历/清单导出同一条纪律；日历是唯一例外，因为它是幂等快照）。
    ② **不编造空白**：这一周什么都没发生，就照实写"本周没有投递/回复"，
       而不是拿"累计数据"充数 —— 那会让人以为这一周很忙。
    ③ **不宣称统计显著性**：数字与样本量照实打印，结论留给看的人
       （`stats_svc` 里写死了这条纪律，这里沿用同一批数字，不另算一套）。

★ 内容全部来自库里已有的东西（`stats_svc` / `weak_svc` / `track_svc` / 事件流），
  这一层**只做汇总与排版**，不引入任何新口径 —— 否则周报里的数字会和
  `jh stats` 对不上，而"同一件事两套数字"是复盘时最忌讳的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunter.config.settings import AppSettings, get_settings
from jobhunter.models import Application, ApplicationEvent, Interview, InterviewQuestion, Job
from jobhunter.services import apply_svc, export_svc, stats_svc, track_svc, weak_svc
from jobhunter.utils.logging import get_logger
from jobhunter.utils.time import fmt_date_local, fmt_datetime_local, now_utc, to_local

logger = get_logger(__name__)

__all__ = [
    "ReportData",
    "ReportEvent",
    "build_report",
    "export_report",
    "render_markdown",
    "week_start",
]


def week_start(now: datetime | None = None) -> datetime:
    """本周一 00:00（**北京时间**，返回 UTC）。

    ★ 为什么用"自然周"而不是"最近 7 天"：周报要能和人脑里的"这周"对上
      （周一开例会、周五交周报）。滚动 7 天会让"上周做了什么"每次问都不一样。
    """
    local = to_local(now or now_utc())
    monday = local.date() - timedelta(days=local.weekday())
    start_local = datetime.combine(monday, time.min, tzinfo=local.tzinfo)
    return start_local.astimezone(now_utc().tzinfo)


@dataclass
class ReportEvent:
    """周报时间轴上的一条事件。"""

    day: str
    time_text: str
    label: str
    company: str = ""
    title: str = ""


@dataclass
class ReportData:
    """周报的完整数据。"""

    since: datetime
    until: datetime
    stats: stats_svc.StatsReport
    weak: weak_svc.WeakReport
    events: list[ReportEvent] = field(default_factory=list)
    #: 本周期内安排的面试（含未来）
    interviews: list[tuple[str, str]] = field(default_factory=list)
    #: 本周期记录的面试题（题目 + 来源）
    questions: list[tuple[str, str]] = field(default_factory=list)
    #: 待处理的投递（"下周该干什么"）
    pending: list[apply_svc.PendingRow] = field(default_factory=list)
    title: str = "求职周报"

    @property
    def period_text(self) -> str:
        return f"{fmt_date_local(self.since)} ~ {fmt_date_local(self.until)}"

    @property
    def quiet_week(self) -> bool:
        """这一周没有任何事件、没有新投递、没有问题记录（用于"照实写空白"）。"""
        return not self.events and not self.questions and not self.interviews


def _period_events(session: Session, *, since: datetime, until: datetime) -> list[ReportEvent]:
    """取周期内的事件流（带岗位信息），按时间升序。"""
    rows = session.execute(
        select(ApplicationEvent, Job)
        .join(Application, ApplicationEvent.application_id == Application.id)
        .join(Job, Application.job_id == Job.id)
        .where(ApplicationEvent.occurred_at >= since, ApplicationEvent.occurred_at <= until)
        .order_by(ApplicationEvent.occurred_at, ApplicationEvent.id)
    ).all()
    out: list[ReportEvent] = []
    for event, job in rows:
        local = to_local(event.occurred_at)
        out.append(
            ReportEvent(
                day=fmt_date_local(event.occurred_at),
                time_text=f"{local:%H:%M}",
                label=track_svc.entry_label(event),
                company=job.company or "",
                title=job.title or "",
            )
        )
    return out


def build_report(
    session: Session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    now: datetime | None = None,
) -> ReportData:
    """汇总一个周期的求职数据（默认：本周一到今天）。"""
    moment = now or now_utc()
    start = since or week_start(moment)
    end = until or moment

    days = max(1, (to_local(end).date() - to_local(start).date()).days + 1)
    report = ReportData(
        since=start,
        until=end,
        # ★ 统计口径与 `jh stats --days` 完全一致：同一个函数、同一批数字
        stats=stats_svc.build_stats(session, days=days, now=moment),
        weak=weak_svc.build_weak_report(session, top=5),
        events=_period_events(session, since=start, until=end),
        title=f"求职周报（{fmt_date_local(start)} ~ {fmt_date_local(end)}）",
    )

    interviews = session.execute(
        select(Interview, Job)
        .join(Application, Interview.application_id == Application.id)
        .join(Job, Application.job_id == Job.id)
        .where(Interview.scheduled_at.is_not(None))
        .order_by(Interview.scheduled_at)
    ).all()
    for interview, job in interviews:
        when = interview.scheduled_at
        if when is None or not (start <= when <= end + timedelta(days=7)):
            continue
        label = f"{fmt_datetime_local(when)} {interview.round or '面试'}"
        report.interviews.append((label, f"{job.company or ''} · {job.title or ''}".strip(" ·")))

    for question in session.scalars(
        select(InterviewQuestion)
        .where(InterviewQuestion.created_at >= start, InterviewQuestion.created_at <= end)
        .order_by(InterviewQuestion.id)
    ):
        source = "预测" if question.source == "predicted" else "实录"
        report.questions.append((question.question, source))

    report.pending = apply_svc.list_actionable(session, now=moment)[:10]
    return report


# ===========================================================================
# 渲染
# ===========================================================================
def _section_numbers(data: ReportData) -> list[str]:
    """第一节：这一周的数字（口径与 `jh stats` 完全同一批）。"""
    overall = data.stats.overall
    lines = ["## 一、这一周的数字", ""]
    if not overall.total:
        # ★ 照实写空白：拿"累计数据"充数会让人以为这一周很忙
        lines += ["本周期没有投递记录（**照实写**：这一周没投，或者还没打标）。", ""]
    else:
        lines += ["| 指标 | 数值 |", "|---|---|", f"| 投递 | {overall.total} |"]
        lines += [
            f"| {label} | {stats_svc.format_rate(overall.rate(key), overall.total)} |"
            for key, label in stats_svc.RATE_LABELS.items()
        ]
        lines += [f"| 被拒 | {overall.rejected} |", ""]
    lines += [
        "> 口径：按「至少走到过这一步」统计，不是当前停在哪一步；",
        "> 只记录对比，不宣称统计显著性（样本小的时候差异说明不了什么）。",
        "",
    ]
    return lines


def _section_events(data: ReportData) -> list[str]:
    """第二节：这一周发生了什么（按天分组的事件流）。"""
    lines = ["## 二、这一周发生了什么", ""]
    if not data.events:
        return [*lines, "这一周没有任何状态变化（没有投递/已读/回复/约面/笔试）。", ""]
    current_day = ""
    for event in data.events:
        if event.day != current_day:
            current_day = event.day
            lines += [f"**{event.day}**", ""]
        who = f"{event.company} · {event.title}".strip(" ·")
        lines.append(f"- `{event.time_text}` {event.label}　{who}")
    return [*lines, ""]


def _section_interviews(data: ReportData) -> list[str]:
    """第三节：面试安排与本周期记录/预测的问题。"""
    lines = ["## 三、面试与问题", ""]
    if not data.interviews and not data.questions:
        return [*lines, "本周期没有面试安排，也没有记录新问题。", ""]
    if data.interviews:
        lines += ["**面试安排**", ""]
        lines += [f"- {label}　{who}" for label, who in data.interviews]
        lines.append("")
    if data.questions:
        lines += [f"**本周期记录/预测的问题（{len(data.questions)} 条）**", ""]
        lines += [f"- [{source}] {text}" for text, source in data.questions[:15]]
        lines.append("")
    return lines


def _section_weak(data: ReportData) -> list[str]:
    """第四节：薄弱点（**累计**口径，必须写明，否则会被当成"这一周的"）。"""
    lines = ["## 四、薄弱点（累计，不只看这一周）", ""]
    if not data.weak.rows:
        return [
            *lines,
            "还没有面试题记录（`jh question add` 记真实被问的，"
            "`jh question predict` 猜可能问的）。",
            "",
        ]
    lines += ["| 知识点 | 没答上来 | 被问次数 | 结论 |", "|---|---|---|---|"]
    lines += [
        f"| {row.category} | {row.weak_count} | {row.total} | {row.conclusion} |"
        for row in data.weak.rows
    ]
    lines += ["", f"**{data.weak.headline}**", ""]
    return lines


def _section_next(data: ReportData) -> list[str]:
    """第五节：下周该干什么（可直接当待办清单用）。"""
    lines = ["## 五、下周该干什么", ""]
    if not data.pending:
        return [*lines, "没有待处理的投递 —— 要么都处理完了，要么还没开始投。", ""]
    lines += [
        f"- [ ] `{row.label}`　{row.reason}　（job_id={row.job_id}）" for row in data.pending[:8]
    ]
    lines += [
        "",
        "打标示例：`jh apply 3=applied 7=replied`　｜　跟进话术：`jh followup <job_id>`",
        "",
    ]
    return lines


def render_markdown(data: ReportData) -> str:
    """把周报渲染成 Markdown（这一层不做任何新计算，只排版）。

    ★ 按"节"拆成小函数：每一节都能单独读、单独测，
      而且"这一节为什么这么写"（比如薄弱点是累计口径）能就近写在注释里。
    """
    lines: list[str] = [
        f"# {data.title}",
        "",
        f"周期：{data.period_text}　｜　{data.stats.sample_note}",
        "",
    ]
    lines += _section_numbers(data)
    lines += _section_events(data)
    lines += _section_interviews(data)
    lines += _section_weak(data)
    lines += _section_next(data)
    lines += ["---", "", "> 由 `jh report` 生成。数字口径见 `jh stats`，薄弱点口径见 `jh weak`。"]
    return "\n".join(lines).rstrip() + "\n"


def export_report(
    data: ReportData,
    *,
    out_dir: Path | None = None,
    settings: AppSettings | None = None,
    now: datetime | None = None,
) -> Path:
    """把周报写到 `output/report-<日期>.md`（**只写新文件**）。

    ★ 与简历/清单导出同一条纪律：绝不覆盖已有报告。
      想改内容就重新生成一份（文件名带日期，天然不会撞车；
      同一天生成多次会得到 `-2`/`-3` 后缀，而不是把上一份盖掉）。
    """
    from jobhunter.utils.io import write_text

    settings = settings or get_settings()
    directory = Path(out_dir) if out_dir is not None else settings.paths.output_dir
    stamp = to_local(now or now_utc()).strftime("%Y%m%d")
    path = export_svc.unique_path(directory, f"report-{stamp}", ".md")
    return write_text(path, render_markdown(data))

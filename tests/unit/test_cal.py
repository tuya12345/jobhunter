"""`jh cal export` 日历导出的单测（P6）。

测试重点**不是**"能不能生成一个 .ics 文件"，而是那几个"看起来正常、
导进手机日历才发现不对"的地方：

    · CRLF 行尾（含最后一行）—— 用 `\\n` 结尾的文件在部分客户端上解析失败；
    · 75 **字节**折行 —— 中文一个字 3 字节，按字符折会写出 225 字节的行；
    · UID 稳定唯一 —— 否则每次导入都多一整套重复事件；
    · UTC 的 `Z` 形式 —— 用本地时间 + TZID 会在缺 tzdata 的客户端上出错；
    · 覆盖同名文件 —— 生成 jobhunter-2.ics 会让用户的日历里出现两份重复事件。

CLI 用例走真实路径（`jh init` 建库 → 插数据 → `jh cal export`），
因为"服务层对了但命令没接上"是这类功能最常见的失手方式。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from jobhunter.cli.app import app
from jobhunter.config.settings import get_settings
from jobhunter.models import Application, Assessment, Interview, Job, Offer
from jobhunter.services import cal_svc
from jobhunter.utils.time import now_utc

runner = CliRunner()

#: 固定的"现在"：窗口断言与真实日历无关，测试不会因为跑在哪一天而变红
NOW = datetime(2026, 9, 10, 0, 0, tzinfo=UTC)
#: 基准面试时间 = 库里存进去的那个 UTC 时刻（UTC 形式断言就认它）
INTERVIEW_AT = NOW + timedelta(days=1)
MEETING_LINK = "https://meeting.example.com/abc-123"


# ===========================================================================
# 造数据（参考 tests/unit/test_today.py 的 make_job）
# ===========================================================================
def make_job(**overrides: Any) -> Job:
    """只填 NOT NULL 字段的岗位。"""
    base: dict[str, Any] = {
        "platform": "manual",
        "company": "某某科技",
        "title": "Python 后端开发实习生",
        "city": "深圳",
        "jd_hash": "j" * 64,
        "status": "scored",
        "match_score": 80,
    }
    base.update(overrides)
    return Job(**base)


def add_application(session: Session, **job_overrides: Any) -> Application:
    """建岗位 + 投递记录并 flush，返回 application。

    ★ `interviews` / `assessments` / `offers` 三张表都挂 `application_id`，
      所以三类事件都必须先有投递记录 —— 这也是本文件要自己造数据的原因。
    """
    job = make_job(**job_overrides)
    session.add(job)
    session.flush()
    application = Application(job_id=job.id, status="applied")
    session.add(application)
    session.flush()
    return application


def read_ics(path: Path) -> str:
    """按**字节**读回 .ics。

    ★ 不能用 `Path.read_text()`：它默认做通用换行翻译（`\\r\\n` → `\\n`），
      读回来就看不到 CRLF 了 —— 而 CRLF 恰恰是本文件要守住的东西
      （踩过一次：用 read_text 读回的内容里一个 `UID:` 都匹配不到，
      因为折行的 `\\r\\n ` 也被翻译成了 `\\n `）。
    """
    return path.read_bytes().decode("utf-8")


def unfold(text: str) -> str:
    """解开 RFC 5545 的折行（续行 = CRLF + 一个空格）。

    日历客户端解析时做的就是这一步；用它把"折过的行"还原成逻辑行再断言，
    就不用关心某一行的字节数是否刚好越过了 75。
    """
    return text.replace("\r\n ", "")


def summary_lines(text: str) -> list[str]:
    """取出解折后的所有 SUMMARY 行（顺序即文件里的顺序）。"""
    return [line for line in unfold(text).split("\r\n") if line.startswith("SUMMARY:")]


def uid_set(text: str) -> set[str]:
    """取出所有 UID（解折后），用来断言"稳定且唯一"。"""
    return {line for line in unfold(text).split("\r\n") if line.startswith("UID:")}


# ===========================================================================
# fixture：一份"有代表性"的数据
# ===========================================================================
@pytest.fixture
def seeded(session: Session) -> dict[str, Any]:
    """窗口内 3 条（面试 / 笔试 / Offer）+ 8 条**必须被过滤掉**的。"""
    alpha = add_application(session, company="某某科技", title="Python 后端开发实习生")
    beta = add_application(session, company="某某网络", title="前端开发实习生")
    gamma = add_application(session, company="某某集团", title="数据分析实习生")

    interview = Interview(
        application_id=alpha.id,
        round="一面",
        scheduled_at=INTERVIEW_AT,
        mode="视频",
        meeting_link=MEETING_LINK,
        status="待面",
        note="提前 10 分钟进会议室",
    )
    assessment = Assessment(
        application_id=beta.id,
        kind="笔试",
        deadline_at=NOW + timedelta(days=3),
        platform="牛客",
        status="待做",
    )
    offer = Offer(
        application_id=gamma.id,
        company="某某集团",
        title="数据分析实习生",
        salary_text="8-10K·14薪",
        deadline_at=NOW + timedelta(days=5),
        status="待定",
    )

    # ---- 以下都不该进日历：窗口外 / 状态不对 / 没有时间 ----
    far_interview = Interview(
        application_id=alpha.id,
        round="二面",
        scheduled_at=NOW + timedelta(days=90),
        status="待面",
    )
    past_interview = Interview(
        application_id=alpha.id,
        round="三面",
        scheduled_at=NOW - timedelta(days=1),
        status="待面",
    )
    done_interview = Interview(
        application_id=alpha.id,
        round="HR面",
        scheduled_at=NOW + timedelta(days=2),
        status="已面",
    )
    unscheduled_interview = Interview(application_id=alpha.id, round="终面", status="待面")
    done_assessment = Assessment(
        application_id=beta.id,
        kind="测评",
        deadline_at=NOW + timedelta(days=2),
        status="已做",
    )
    expired_assessment = Assessment(
        application_id=beta.id,
        kind="AI面",
        deadline_at=NOW - timedelta(days=1),
        status="已过期",
    )
    accepted_offer = Offer(
        application_id=gamma.id,
        company="某某集团",
        deadline_at=NOW + timedelta(days=1),
        status="已接受",
    )
    far_offer = Offer(
        application_id=gamma.id,
        company="某某集团",
        deadline_at=NOW + timedelta(days=120),
        status="待定",
    )

    session.add_all(
        [
            interview,
            assessment,
            offer,
            far_interview,
            past_interview,
            done_interview,
            unscheduled_interview,
            done_assessment,
            expired_assessment,
            accepted_offer,
            far_offer,
        ]
    )
    session.flush()

    return {
        "interview": interview.id,
        "assessment": assessment.id,
        "offer": offer.id,
        "excluded": [
            ("interview", far_interview.id),
            ("interview", past_interview.id),
            ("interview", done_interview.id),
            ("interview", unscheduled_interview.id),
            ("assessment", done_assessment.id),
            ("assessment", expired_assessment.id),
            ("offer", accepted_offer.id),
            ("offer", far_offer.id),
        ],
    }


def render_all(session: Session, *, days: int = 60, now: datetime = NOW) -> str:
    """采集 + 渲染（大多数断言只关心最终文本）。"""
    return cal_svc.render_ics(cal_svc.build_events(session, days=days, now=now), now=now)


# ===========================================================================
# 1. 窗口与状态过滤
# ===========================================================================
def test_build_events_keeps_only_pending_in_window(seeded, session):
    events = cal_svc.build_events(session, days=60, now=NOW)
    assert {(event.kind, event.source_id) for event in events} == {
        ("interview", seeded["interview"]),
        ("assessment", seeded["assessment"]),
        ("offer", seeded["offer"]),
    }


def test_build_events_drops_out_of_window_and_other_statuses(seeded, session):
    """窗口外 / 已面已做已接受 / 没有时间的，一条都不进。"""
    kept = {
        (event.kind, event.source_id) for event in cal_svc.build_events(session, days=60, now=NOW)
    }
    for pair in seeded["excluded"]:
        assert pair not in kept, f"{pair} 不该出现在日历里"


def test_window_days_shrinks_the_horizon(seeded, session):
    """窗口是 `[now, now+days]`：--days 4 只留面试(+1d)与笔试(+3d)，Offer(+5d) 出局。"""
    assert [event.kind for event in cal_svc.build_events(session, days=4, now=NOW)] == [
        "interview",
        "assessment",
    ]
    assert cal_svc.build_events(session, days=0, now=NOW) == []


# ===========================================================================
# 2. 三类事件的标题与排序
# ===========================================================================
def test_titles_carry_company_and_are_sorted_ascending(seeded, session):
    events = cal_svc.build_events(session, days=60, now=NOW)
    assert [event.title for event in events] == [
        "面试：某某科技 · Python 后端开发实习生（一面）",
        "笔试截止：某某网络 · 笔试",
        "Offer 签约截止：某某集团",
    ]
    assert [event.start for event in events] == sorted(event.start for event in events)
    # 缺字段就省略，标题里绝不能出现 None
    assert all("None" not in event.title for event in events)


def test_missing_fields_are_omitted_from_titles(session):
    """公司/轮次都没填时标题仍然可读（不会出现 `面试：None（None）`）。"""
    application = add_application(session, company="", title="")
    session.add(
        Interview(
            application_id=application.id,
            scheduled_at=NOW + timedelta(days=1),
            status="待面",
        )
    )
    session.flush()
    events = cal_svc.build_events(session, days=60, now=NOW)
    assert len(events) == 1
    assert events[0].title == "面试"
    assert "None" not in events[0].title


# ===========================================================================
# 3. 事件细节：时长 / 地点 / 链接
# ===========================================================================
def test_interview_event_is_one_hour_with_link(seeded, session):
    event = cal_svc.build_events(session, days=60, now=NOW)[0]
    assert event.end - event.start == timedelta(hours=1)
    assert event.url == MEETING_LINK
    assert f"会议链接：{MEETING_LINK}" in event.description
    assert "轮次：一面" in event.description
    assert "形式：视频" in event.description
    assert "备注：提前 10 分钟进会议室" in event.description
    # 视频面试没有物理地点
    assert event.location == ""


def test_onsite_interview_keeps_location(session):
    application = add_application(session, company="某某实业", title="后端开发")
    session.add(
        Interview(
            application_id=application.id,
            round="终面",
            scheduled_at=NOW + timedelta(days=2),
            mode="现场",
            location="深圳市南山区科技园 1 栋",
            status="待面",
        )
    )
    session.flush()
    event = cal_svc.build_events(session, days=60, now=NOW)[0]
    assert event.location == "深圳市南山区科技园 1 栋"
    assert "LOCATION:深圳市南山区科技园 1 栋" in unfold(render_all(session))


def test_deadline_events_are_30_minute_reminders(seeded, session):
    """笔试/Offer 截止是"时间点提醒"，不是一段被占用的日程。"""
    by_kind = {event.kind: event for event in cal_svc.build_events(session, days=60, now=NOW)}
    for kind in ("assessment", "offer"):
        assert by_kind[kind].end - by_kind[kind].start == timedelta(minutes=30)
    assert "某某网络" in by_kind["assessment"].title
    assert "牛客" in by_kind["assessment"].description
    assert by_kind["offer"].end - by_kind["offer"].start == timedelta(minutes=30)


# ===========================================================================
# 4. UID：稳定且唯一
# ===========================================================================
def test_uid_is_stable_and_unique(seeded, session):
    first = render_all(session)
    # 换一个"现在"再渲染一次（仍是同一批窗口内事件）：
    # UID 与时间无关，两次必须完全相同 —— 否则日历里会出现重复事件
    second = render_all(session, now=NOW + timedelta(hours=1))
    assert uid_set(first) == uid_set(second)
    assert uid_set(first) == {
        f"UID:jobhunter-interview-{seeded['interview']}@local",
        f"UID:jobhunter-assessment-{seeded['assessment']}@local",
        f"UID:jobhunter-offer-{seeded['offer']}@local",
    }
    assert len(uid_set(first)) == 3  # 不同 kind / 不同 id 不重复


def test_uid_format_helper():
    assert cal_svc.event_uid("interview", 7) == "jobhunter-interview-7@local"


# ===========================================================================
# 5. CRLF
# ===========================================================================
def test_every_line_ends_with_crlf_including_the_last(seeded, session):
    text = render_all(session)
    assert text.startswith("BEGIN:VCALENDAR\r\n")
    assert text.endswith("END:VCALENDAR\r\n")
    # 没有裸 LF（把 CRLF 都吃掉之后不该还剩 \n）
    assert "\n" not in text.replace("\r\n", "")
    assert text.count("\r\n") == len(text.split("\r\n")) - 1


def test_empty_calendar_also_ends_with_crlf():
    text = cal_svc.render_ics([], now=NOW)
    assert text.endswith("END:VCALENDAR\r\n")
    assert "\n" not in text.replace("\r\n", "")


# ===========================================================================
# 6. 折行：75 **字节**，在字符边界上折
# ===========================================================================
def test_fold_line_only_folds_when_over_75_bytes():
    assert cal_svc._fold_line("A" * 75) == "A" * 75
    assert cal_svc._fold_line("A" * 76) == "A" * 75 + "\r\n " + "A"


def test_fold_line_counts_bytes_not_characters():
    """75 个汉字 = 225 字节：必须折行，且不能在汉字中间断开。"""
    line = "SUMMARY:" + "中文标题" * 25
    folded = cal_svc._fold_line(line)
    physical = folded.split("\r\n")
    assert len(physical) > 1
    assert all(len(part.encode("utf-8")) <= cal_svc.MAX_LINE_BYTES for part in physical)
    assert all(part.startswith(" ") for part in physical[1:])
    assert all(part.strip() for part in physical)
    # 解开折行后**逐字**等于原文（中文一个字都没少、没被切半）
    assert unfold(folded) == line
    assert len(unfold(folded).encode("utf-8")) == len(line.encode("utf-8"))


def test_long_chinese_summary_is_folded_inside_the_ics(session):
    long_company = "某某科技股份有限公司" * 6
    title = f"面试：{long_company} · Python 后端开发实习生（一面）"
    assert len(title.encode("utf-8")) > cal_svc.MAX_LINE_BYTES  # 前提：确实超长

    application = add_application(session, company=long_company, title="Python 后端开发实习生")
    session.add(
        Interview(
            application_id=application.id,
            round="一面",
            scheduled_at=NOW + timedelta(days=1),
            status="待面",
        )
    )
    session.flush()

    text = render_all(session)
    assert all(len(line.encode("utf-8")) <= cal_svc.MAX_LINE_BYTES for line in text.split("\r\n"))
    assert summary_lines(text) == [f"SUMMARY:{title}"]
    # 折行产生的续行只可能来自折行（每个物理行都以空格开头的那部分）
    assert any(line.startswith(" ") for line in text.split("\r\n"))


# ===========================================================================
# 7. 转义
# ===========================================================================
def test_special_characters_are_escaped(session):
    application = add_application(session, company="甲乙,科技;\\公司", title="后端")
    session.add(
        Interview(
            application_id=application.id,
            round="一面",
            scheduled_at=NOW + timedelta(days=1),
            mode="电话",
            status="待面",
            note="第一行\n第二行",
        )
    )
    session.flush()

    text = unfold(render_all(session))
    # `\` → `\\`（先转，否则会把自己生成的转义符再转一次）、`,` → `\,`、`;` → `\;`
    assert r"SUMMARY:面试：甲乙\,科技\;\\公司 · 后端（一面）" in text
    # 备注里的真实换行 → 转义序列 `\n`（.ics 里不能出现裸换行）
    assert r"备注：第一行\n第二行" in text
    assert "\n" not in text.replace("\r\n", "")


def test_url_value_is_not_text_escaped(session):
    """URL 是 URI 值类型：转义链接里的逗号会让客户端打不开链接。"""
    link = "https://example.com/room?id=1,2"
    application = add_application(session, company="某某", title="开发")
    session.add(
        Interview(
            application_id=application.id,
            scheduled_at=NOW + timedelta(days=1),
            meeting_link=link,
            status="待面",
        )
    )
    session.flush()
    text = unfold(render_all(session))
    assert f"URL:{link}" in text
    assert r"URL:https://example.com/room?id=1\,2" not in text


# ===========================================================================
# 8. 空日历
# ===========================================================================
def test_empty_events_render_a_valid_empty_calendar():
    text = cal_svc.render_ics([], now=NOW)
    assert "BEGIN:VCALENDAR" in text
    assert "VERSION:2.0" in text
    assert "CALSCALE:GREGORIAN" in text
    assert "PRODID:-//JobHunter//求职猎手//CN" in text
    assert "VEVENT" not in text
    assert text.index("END:VCALENDAR") > text.index("BEGIN:VCALENDAR")


def test_render_ics_writes_dtstamp_from_now(seeded, session):
    text = unfold(render_all(session))
    assert "DTSTAMP:20260910T000000Z" in text
    assert text.count("DTSTAMP:") == 3  # 每个 VEVENT 一个


# ===========================================================================
# 9. DTSTART/DTEND 用 UTC 的 Z 形式
# ===========================================================================
def test_dtstart_dtend_are_utc_z_form(seeded, session):
    text = unfold(render_all(session))
    # INTERVIEW_AT = 2026-09-11 00:00 UTC，时长 1 小时
    assert "DTSTART:20260911T000000Z" in text
    assert "DTEND:20260911T010000Z" in text
    # 笔试 +3 天、Offer +5 天（30 分钟提醒）
    assert "DTSTART:20260913T000000Z" in text
    assert "DTEND:20260913T003000Z" in text
    assert "DTSTART:20260915T000000Z" in text
    # 绝不出现本地时间 + TZID 的写法
    assert "TZID" not in text
    # 库里存的时刻确实等于断言的 UTC 时刻（expire 后强制从库里重读一次）
    session.expire_all()
    stored = session.get(Interview, seeded["interview"]).scheduled_at
    assert stored == INTERVIEW_AT
    assert stored.tzinfo is not None


# ===========================================================================
# 10. export_ics：写盘 / 覆盖 / --out
# ===========================================================================
def test_export_ics_writes_utf8_file_without_bom(seeded, session, settings):
    export = cal_svc.export_ics(session, days=60, settings=settings, now=NOW)
    assert export.path == settings.paths.output_dir / "jobhunter.ics"
    assert export.event_count == 3
    assert export.overwritten is False

    raw = export.path.read_bytes()
    # .ics 里 BOM 是非法的（很多客户端会解析失败）
    assert not raw.startswith(b"\xef\xbb\xbf")
    # Windows 上如果换行被再翻译一次，会变成 \r\r\n（每个事件后多一个空行）
    assert b"\r\r\n" not in raw
    text = raw.decode("utf-8")
    assert text.startswith("BEGIN:VCALENDAR\r\n")
    assert text.count("BEGIN:VEVENT") == 3


def test_export_ics_overwrites_the_same_snapshot(seeded, session, settings):
    """第二次导出覆盖同一路径，目录里永远只有一个 .ics（否则日历里会重复）。"""
    first = cal_svc.export_ics(session, days=60, settings=settings, now=NOW)
    second = cal_svc.export_ics(session, days=60, settings=settings, now=NOW + timedelta(hours=5))

    assert first.path == second.path
    assert second.overwritten is True
    assert first.overwritten is False
    assert len(list(settings.paths.output_dir.glob("*.ics"))) == 1
    # 覆盖后 UID 集合不变 → 手机日历按 UID 更新而不是新增
    first_uids = uid_set(read_ics(first.path))
    assert first_uids == uid_set(read_ics(second.path))
    assert len(first_uids) == 3


def test_export_ics_honours_out_and_creates_parent_dir(seeded, session, settings, tmp_path):
    target = tmp_path / "nested" / "my-calendar.ics"
    export = cal_svc.export_ics(session, out=target, days=60, settings=settings, now=NOW)
    assert export.path == target
    assert target.is_file()
    assert "BEGIN:VCALENDAR" in read_ics(target)


def test_export_ics_with_no_events_still_writes_a_calendar(session, settings):
    """空结果照样导出：否则上一次的旧文件会被手机日历当成"最新安排"。"""
    export = cal_svc.export_ics(session, days=60, settings=settings, now=NOW)
    assert export.event_count == 0
    text = read_ics(export.path)
    assert text.startswith("BEGIN:VCALENDAR")
    assert "END:VCALENDAR" in text
    assert "VEVENT" not in text


# ===========================================================================
# 11. CLI 端到端
# ===========================================================================
@pytest.fixture
def cli_ready(isolated_env: Path) -> Path:
    """走真实路径建库（`jh init`），再直接写库造三类事件。

    时间用**真实 now** 往前推：CLI 内部用的是 `now_utc()`，
    写死 2026 年的时间点在某些机器上会落到窗口外。
    """
    assert runner.invoke(app, ["--no-remind", "init"]).exit_code == 0
    settings = get_settings()

    from jobhunter.storage.db import get_session_factory

    engine, factory = get_session_factory(settings.database_url)
    base = now_utc()
    try:
        with factory() as live:
            alpha = add_application(live, company="某某科技", title="Python 后端开发实习生")
            live.add(
                Interview(
                    application_id=alpha.id,
                    round="一面",
                    scheduled_at=base + timedelta(days=1),
                    mode="视频",
                    meeting_link=MEETING_LINK,
                    status="待面",
                )
            )
            beta = add_application(live, company="某某网络", title="前端开发实习生")
            live.add(
                Assessment(
                    application_id=beta.id,
                    kind="笔试",
                    deadline_at=base + timedelta(days=3),
                    status="待做",
                )
            )
            gamma = add_application(live, company="某某集团", title="数据分析实习生")
            live.add(
                Offer(
                    application_id=gamma.id,
                    company="某某集团",
                    deadline_at=base + timedelta(days=5),
                    status="待定",
                )
            )
            live.commit()
    finally:
        engine.dispose()
    return isolated_env


def test_cli_cal_export_end_to_end(cli_ready):
    result = runner.invoke(app, ["--no-remind", "cal", "export"])
    assert result.exit_code == 0, result.output
    assert "已导出 3 个事件" in result.output
    assert "面试：某某科技 · Python 后端开发实习生（一面）" in result.output
    assert "笔试截止：某某网络 · 笔试" in result.output

    path = get_settings().paths.output_dir / "jobhunter.ics"
    text = read_ics(path)
    assert "BEGIN:VCALENDAR" in text
    assert text.count("BEGIN:VEVENT") == 3
    assert text.endswith("END:VCALENDAR\r\n")


def test_cli_cal_export_days_zero_has_no_events_but_succeeds(cli_ready):
    result = runner.invoke(app, ["--no-remind", "cal", "export", "--days", "0"])
    assert result.exit_code == 0, result.output
    assert "已导出 0 个事件" in result.output
    assert "未来 0 天没有面试/笔试/Offer 截止" in result.output

    # 照样导出（内容是一个合法空日历）
    text = read_ics(get_settings().paths.output_dir / "jobhunter.ics")
    assert "BEGIN:VCALENDAR" in text
    assert "VEVENT" not in text


def test_cli_cal_export_custom_out(cli_ready, tmp_path):
    target = tmp_path / "我的日历.ics"
    result = runner.invoke(app, ["--no-remind", "cal", "export", "--out", str(target)])
    assert result.exit_code == 0, result.output
    assert target.is_file()
    assert str(target) in result.output


def test_cli_cal_export_rejects_negative_days(cli_ready):
    result = runner.invoke(app, ["--no-remind", "cal", "export", "--days=-1"])
    assert result.exit_code == 2
    assert "--days" in result.output


def test_cli_cal_export_needs_database(isolated_env: Path):
    """库还没建 → 明确提示先 `jh init`，退出码 1（与 require_db 的约定一致）。"""
    result = runner.invoke(app, ["--no-remind", "cal", "export"])
    assert result.exit_code == 1
    assert "jh init" in result.output

"""时间测试：守住"UTC 存储 + 北京时间展示"这条全项目约定。

★ 为什么时间需要专门一组测试

    本项目的核心功能里有大量"过了几天"的计算：
        投后 3 天未跟进 / 7 天无动静 / 14 天建议放弃 / 笔试还有几天截止。
    这些都建立在"全库统一 UTC aware"的前提上。一旦某处混进 naive datetime：
        · `aware - naive` 直接抛 TypeError（还算好，能立刻发现）；
        · 更糟的是**悄悄差 8 小时**：北京时间 09-11 07:00 的笔试截止，
          会被算成还剩一天，实际只剩几小时 → 错过笔试。
    所以这组测试覆盖三类边界：
        ① 类型（naive / aware）
        ② 时区换算（UTC ↔ Asia/Shanghai，含跨天）
        ③ 自然日计算（按北京时间算，而不是满 24 小时）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobhunter.utils.time import (
    DISPLAY_TZ,
    add_days,
    days_between,
    days_since,
    fmt_date_local,
    fmt_datetime_local,
    fmt_local,
    now_utc,
    parse_local_input,
    to_local,
    to_naive_utc,
    to_utc,
)


# ---------------------------------------------------------------------------
# ① 基本类型
# ---------------------------------------------------------------------------
def test_now_utc_is_timezone_aware() -> None:
    """`now_utc()` 必须是 aware 的（这正是不用已弃用的 utcnow() 的原因）。"""
    now = now_utc()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_to_utc_treats_naive_as_utc() -> None:
    """naive 输入按 UTC 解释（数据库里读出来的就是这个形态）。"""
    naive = datetime(2026, 9, 6, 12, 0, 0)
    result = to_utc(naive)
    assert result.tzinfo is not None
    assert result.hour == 12  # 没有被平移
    assert result.utcoffset() == timedelta(0)


def test_to_utc_converts_aware_input() -> None:
    """aware 输入换算到 UTC（同一时刻，只是换了表示）。"""
    beijing = datetime(2026, 9, 6, 20, 0, 0, tzinfo=DISPLAY_TZ)
    result = to_utc(beijing)
    assert result.hour == 12  # 20:00 北京 = 12:00 UTC
    assert result.utcoffset() == timedelta(0)


def test_to_naive_utc_is_naive() -> None:
    """存储用的 naive UTC 必须真的没有 tzinfo。

    ★ 原因：SQLAlchemy 的 SQLite DATETIME 绑定器**拒绝**带时区的 datetime，
      报错是 "SQLite DateTime type only accepts Python datetime and date
      objects as input" —— 完全没提"时区"，第一次遇到会很懵。
    """
    aware = datetime(2026, 9, 6, 20, 0, 0, tzinfo=DISPLAY_TZ)
    naive = to_naive_utc(aware)
    assert naive.tzinfo is None
    assert naive.hour == 12


def test_aware_roundtrip() -> None:
    """aware → naive(存储) → aware 的往返不丢信息。"""
    original = now_utc()
    stored = to_naive_utc(original)
    restored = to_utc(stored)
    assert restored == original.replace(microsecond=original.microsecond)


# ---------------------------------------------------------------------------
# ② 时区换算（含跨天）
# ---------------------------------------------------------------------------
def test_to_local_is_beijing() -> None:
    """UTC 12:00 → 北京时间 20:00（+08:00）。"""
    utc_dt = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)
    local = to_local(utc_dt)
    assert local.hour == 20
    assert local.utcoffset() == timedelta(hours=8)


def test_date_rolls_over_in_local_time() -> None:
    """★ 最关键的一条：UTC 的 09-10 20:00 在北京是 **09-11** 04:00。

    这是"直接切字符串算日期"必然出错的场景。
    投递记录如果按 UTC 日期分组，晚上投的岗位会被算到前一天，
    "今天已投 X 条"的额度统计就会错。
    """
    utc_dt = datetime(2026, 9, 10, 20, 0, 0, tzinfo=UTC)
    assert to_local(utc_dt).day == 11
    assert fmt_date_local(utc_dt) == "2026-09-11"


def test_fmt_local_formats() -> None:
    """格式化辅助函数。"""
    utc_dt = datetime(2026, 9, 11, 6, 0, 0, tzinfo=UTC)  # 北京 14:00
    assert fmt_local(utc_dt) == "2026-09-11 14:00"
    assert fmt_datetime_local(utc_dt) == "2026-09-11 14:00"
    assert fmt_date_local(utc_dt) == "2026-09-11"
    assert fmt_local(utc_dt, "%H:%M") == "14:00"


def test_fmt_local_handles_none() -> None:
    """None 打印成 —（表格里空着比印 None 好看）。"""
    assert fmt_local(None) == "—"
    assert fmt_datetime_local(None) == "—"
    assert fmt_date_local(None) == "—"


# ---------------------------------------------------------------------------
# ③ 自然日计算（提醒功能的地基）
# ---------------------------------------------------------------------------
def test_days_since_uses_calendar_days() -> None:
    """★ 按**自然日**算，而不是满 24 小时。

    用户说"投了 3 天没动静"指的是日历上过了 3 天。
    北京时间 09-06 23:00 投的，北京时间 09-09 08:00 看时应该显示 3 天
    （虽然只过了 57 小时）。
    """
    applied = datetime(2026, 9, 6, 23, 0, 0, tzinfo=DISPLAY_TZ)
    now = datetime(2026, 9, 9, 8, 0, 0, tzinfo=DISPLAY_TZ)
    assert days_since(applied, now=now) == 3


def test_days_since_returns_none_for_none() -> None:
    """没投递时间就不该编出一个数字。"""
    assert days_since(None) is None


def test_days_since_uses_now_utc_by_default() -> None:
    """不传 now 时用当前时间。"""
    three_days_ago = now_utc() - timedelta(days=3)
    assert days_since(three_days_ago) == 3


def test_days_between() -> None:
    """两个时间相差几个自然日（笔试/offer 截止倒计时）。"""
    start = datetime(2026, 9, 6, 9, 0, 0, tzinfo=DISPLAY_TZ)
    end = datetime(2026, 9, 11, 23, 59, 0, tzinfo=DISPLAY_TZ)
    assert days_between(start, end) == 5
    assert days_between(None, end) is None
    assert days_between(start, None) is None


def test_add_days_keeps_awareness() -> None:
    """加天数之后仍然是 aware（提醒阈值计算会连续调用）。"""
    base = now_utc()
    result = add_days(base, 7)
    assert result.tzinfo is not None
    assert days_between(base, result) == 7


# ---------------------------------------------------------------------------
# ④ 用户输入解析（面试时间、笔试截止）
# ---------------------------------------------------------------------------
def test_parse_full_datetime_as_beijing() -> None:
    """★ 用户输入的 "2026-09-11 14:00" 是**北京时间**，存进去要变 06:00 UTC。

    如果把它当 UTC 存，面试提醒会整体差 8 小时 —— 这是这类工具最致命的 bug
    （用户会因此错过面试）。
    """
    parsed = parse_local_input("2026-09-11 14:00")
    assert parsed.tzinfo is not None
    assert parsed == datetime(2026, 9, 11, 6, 0, 0, tzinfo=UTC)
    assert to_local(parsed).hour == 14


def test_parse_date_only_is_midnight_beijing() -> None:
    """只给日期 → 当天 00:00（北京时间）。"""
    parsed = parse_local_input("2026-09-11")
    assert to_local(parsed) == datetime(2026, 9, 11, 0, 0, 0, tzinfo=DISPLAY_TZ)


def test_parse_iso_with_t_separator() -> None:
    """ISO 的 T 分隔写法也要认。"""
    assert parse_local_input("2026-09-11T14:00") == parse_local_input("2026-09-11 14:00")


def test_parse_month_day_uses_current_year() -> None:
    """省略年份（"09-11 14:00"）按**当年**补。"""
    parsed = parse_local_input("09-11 14:00")
    local = to_local(parsed)
    assert (local.month, local.day, local.hour) == (9, 11, 14)
    assert local.year == to_local(now_utc()).year


def test_parse_time_only_is_today() -> None:
    """只给时刻（"14:00"）→ 今天 14:00（北京时间）。"""
    parsed = parse_local_input("14:00")
    local = to_local(parsed)
    today_local = to_local(now_utc()).date()
    assert local.date() == today_local
    assert local.hour == 14


def test_parse_rejects_garbage() -> None:
    """看不懂的输入必须报错，不能猜。

    ★ 为什么宁可报错也不猜：猜错时间的后果是"错过面试/笔试"，
      比让用户重新输入一次严重得多。
    """
    for garbage in ("下周三下午", "明天", "abc", "13-45", "25:00"):
        with pytest.raises(ValueError, match="看不懂这个时间"):
            parse_local_input(garbage)


def test_parse_rejects_empty() -> None:
    """空输入报错。"""
    with pytest.raises(ValueError, match="不能为空"):
        parse_local_input("   ")

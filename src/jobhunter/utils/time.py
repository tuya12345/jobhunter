"""时间工具：统一 **UTC aware 存储 + Asia/Shanghai 展示**。

╔══════════════════════════════════════════════════════════════════════════╗
║ 为什么要专门写一个模块，而不是到处 datetime.now()？                        ║
╚══════════════════════════════════════════════════════════════════════════╝

1. **`datetime.utcnow()` 已被弃用（Python 3.12+）**，而且它返回的是 *naive*（无时区）
   对象。名字里写着 "utc"，但对象本身不知道自己是 UTC —— 这就是 bug 的温床。

2. **naive 与 aware 相减会直接抛 TypeError**。本项目到处都在算"投了几天了"
   （3 天未跟进 / 7 天无动静 / 14 天建议放弃 / 笔试还有几天截止）。
   如果库里混进 naive 值，这些统计会在某个时刻突然崩掉，而且很难复现。

3. **夏令时 / 跨时区**：只存 UTC、展示时才转本地时区，是唯一不会出错的做法。
   展示时区固定 `Asia/Shanghai`（用 zoneinfo，不依赖第三方 tzdata 包）。

4. **SQLite 没有时区类型**（只有 TEXT/REAL/INTEGER）。所以"时区感知"这件事
   必须由代码在写入/读出时补齐 —— 见 models/base.py 的 UTCDateTime。

约定（全项目统一，不要破例）：
    · 入库、比较、计算   → 一律 UTC aware
    · 打印给人看        → to_local() / fmt_local()
    · 用户输入的时间     → parse_local_input() 解析后**立刻**转 UTC 再入库
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# 展示时区：求职场景固定为北京时间。集中在这里定义，避免各处硬编码字符串。
DISPLAY_TZ = ZoneInfo("Asia/Shanghai")

__all__ = [
    "DISPLAY_TZ",
    "add_days",
    "days_between",
    "days_since",
    "fmt_date_local",
    "fmt_datetime_local",
    "fmt_local",
    "now_utc",
    "parse_local_input",
    "to_local",
    "to_naive_utc",
    "to_utc",
]


# ---------------------------------------------------------------------------
# 获取 / 归一化
# ---------------------------------------------------------------------------
def now_utc() -> datetime:
    """当前时间，**UTC 且带时区信息**（aware）。

    这是全项目唯一允许的"取当前时间"入口。
    不要用 `datetime.utcnow()`（已弃用、naive），也不要用 `datetime.now()`（本地时区、naive）。

    例：
        >>> dt = now_utc()
        >>> dt.tzinfo is not None
        True
    """
    return datetime.now(UTC)


def to_utc(dt: datetime) -> datetime:
    """把任意 datetime 归一化成 UTC aware。

    · 已经是 aware 的 → 换算到 UTC（同一时刻，只是换了表示）；
    · naive 的         → **假定它本来就是 UTC** 并补上时区。

    为什么 naive 按 UTC 处理：数据库里只存 UTC，从 SQLite 读出来的一定是 naive 的 UTC 值，
    所以"补 UTC"是正确的默认。真正的本地时间必须先经过 parse_local_input()。
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_naive_utc(dt: datetime) -> datetime:
    """转成 naive 的 UTC，**仅供存储层使用**。

    为什么存储层需要 naive：SQLAlchemy 的 SQLite DATETIME 绑定器明确拒绝带时区的
    datetime（会抛 "SQLite DateTime type only accepts Python datetime and date
    objects as input"）。所以"去掉 tzinfo"这一步必须在写入前做掉，
    而读出时由 UTCDateTime 再补回来 —— 语义没有丢，只是过了一道"序列化"。
    """
    return to_utc(dt).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# 展示（转北京时间）
# ---------------------------------------------------------------------------
def to_local(dt: datetime, tz: ZoneInfo | None = None) -> datetime:
    """把 UTC（或任意 aware）时间转成展示时区（默认 Asia/Shanghai）的 aware 时间。"""
    return to_utc(dt).astimezone(tz or DISPLAY_TZ)


def fmt_local(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """格式化为北京时间字符串；None → `"—"`（表格里空着比印个 None 好看）。

    ★ 这是唯一应该出现在终端输出里的时间格式化函数。
      直接把 UTC 时间打到屏幕上会让人误判"面试是几点"。
    """
    if dt is None:
        return "—"
    return to_local(dt).strftime(fmt)


def fmt_datetime_local(dt: datetime | None) -> str:
    """常用完整格式：`2026-09-11 14:00`（北京时间）。"""
    return fmt_local(dt, "%Y-%m-%d %H:%M")


def fmt_date_local(dt: datetime | date | None) -> str:
    """只要日期部分：`2026-09-11`（北京时间）。

    ★ 注意：UTC 的 2026-09-10 20:00 在北京时间是 2026-09-11 04:00，
      **日期会跨天**。所以"投递日期"这类展示必须走时区转换，不能直接切字符串。
    """
    if dt is None:
        return "—"
    if isinstance(dt, datetime):
        return to_local(dt).strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 计算（提醒功能的地基）
# ---------------------------------------------------------------------------
def days_since(dt: datetime | None, *, now: datetime | None = None) -> int | None:
    """从 dt 到现在过了几个**自然日**（按北京时间算），负数表示未来。

    用途：顶部提醒里的"投后 3 天未跟进 / 7 天无动静 / 14 天建议放弃"。

    为什么按"自然日"而不是"满 24 小时"：
      用户说"投了 3 天没动静"，指的是日历上过了 3 天（哪怕只过了 71 小时）。
      按自然日更符合直觉。先在本地时区取 date() 再相减，跨时区/跨天都不会错。
    """
    if dt is None:
        return None
    reference = to_utc(now) if now is not None else now_utc()
    return (to_local(reference).date() - to_local(dt).date()).days


def days_between(
    start: datetime | None,
    end: datetime | None,
    *,
    display_tz: bool = True,
) -> int | None:
    """两个时间之间相差几个自然日（end - start）；任一为 None 则返回 None。

    用途："笔试还有几天截止""offer 签约截止日还剩几天"。
    """
    if start is None or end is None:
        return None
    if display_tz:
        return (to_local(end).date() - to_local(start).date()).days
    return (to_utc(end) - to_utc(start)).days


def parse_local_input(text: str, *, tz: ZoneInfo | None = None) -> datetime:
    """解析用户手输的时间（按**本地时区**理解）并返回 UTC aware。

    支持的写法（够用就好，不引入 dateutil 的模糊推断以免猜错）：
        "2026-09-11"          → 当天 00:00
        "2026-09-11 14:00"    → 指定时刻
        "2026-09-11T14:00"    → ISO 形式也认
        "09-11 14:00"         → 省略年份，按**展示时区的今年**补
        "14:00"               → 今天 14:00

    为什么要这个函数：用户输入的一定是"北京时间"，
    如果直接当 UTC 存进去，面试提醒会整体差 8 小时。
    """
    target_tz = tz or DISPLAY_TZ
    raw = text.strip()
    if not raw:
        raise ValueError("时间不能为空")

    # 有完整年份的写法，直接 strptime
    candidates = (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
        "%Y-%m-%d",
    )
    for fmt in candidates:
        try:
            local_dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return local_dt.replace(tzinfo=target_tz).astimezone(UTC)

    # ★ 省略年份的写法（"09-11 14:00" / "9-11" / "14:00"）**不用 strptime**。
    #   原因：Python 3.13 起，对"没有年份"的格式串 strptime 会发
    #   DeprecationWarning（"Parsing dates involving a day of month without a year
    #   specified is ambiguous"），并在 3.15 改成抛异常或换默认年份。
    #   与其依赖一个即将改变的行为，不如自己按"展示时区的今年"显式补年份 ——
    #   语义也更清楚：用户写 "09-11" 指的就是今年的 9 月 11 日。
    today_local = now_utc().astimezone(target_tz)

    # 只要时刻："14:00" → 今天这个点
    clock = _parse_clock(raw)
    if clock is not None:
        hour, minute = clock
        local_dt = datetime.combine(today_local.date(), time(hour, minute))
        return local_dt.replace(tzinfo=target_tz).astimezone(UTC)

    # "09-11 14:00" 或 "09-11"
    parts = raw.replace("T", " ").replace("/", "-").split()
    if len(parts) in {1, 2}:
        month_day = parts[0].split("-")
        if len(month_day) == 2:
            try:
                month, day = int(month_day[0]), int(month_day[1])
            except ValueError:
                month = day = 0
            if 1 <= month <= 12 and 1 <= day <= 31:
                hour = minute = 0
                if len(parts) == 2:
                    parsed_clock = _parse_clock(parts[1])
                    if parsed_clock is None:
                        raise ValueError(_unreadable(text))
                    hour, minute = parsed_clock
                local_dt = datetime(today_local.year, month, day, hour, minute, tzinfo=target_tz)
                return local_dt.astimezone(UTC)

    raise ValueError(_unreadable(text))


def _parse_clock(text: str) -> tuple[int, int] | None:
    """解析 "14:00" / "14:00:30" 形式，返回 (hour, minute)；不是时刻则返回 None。"""
    parts = text.split(":")
    if len(parts) not in {2, 3}:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def _unreadable(text: str) -> str:
    """统一的"看不懂"报错文案。

    ★ 宁可报错也不猜：猜错时间的后果是错过面试/笔试，比让用户重输一次严重得多。
    """
    return (
        f"看不懂这个时间：{text!r}。可以写成 2026-09-11、2026-09-11 14:00、09-11 14:00 或 14:00。"
    )


def add_days(dt: datetime, days: int) -> datetime:
    """在 UTC 时间上加/减天数（提醒阈值计算用，保持 aware）。"""
    return to_utc(dt) + timedelta(days=days)

"""把 services 返回的 dataclass 变成"页面能直接吃的 JSON"（P8）。

★ 为什么单独一个模块、而不是在 app.py 里 `asdict()` 了事：

  · services 返回的是**领域对象**（`TodayList` / `Timeline` / `StatsReport` / `WeakReport`），
    里面有 `datetime`、`Path`、嵌套 dataclass —— 直接 `jsonable_encoder` 会得到
    "本地时区的裸字符串"，而本项目的接口契约是**一律 ISO 8601 带时区**（见 leads API 的说明）；
  · 转换口径只有一份，页面才不会在四个接口里看到四种时间格式。

★ 这是**只读**转换：不改领域对象、不落库、不调模型。
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from jobhunter.utils.time import to_utc

__all__ = ["as_payload", "iso"]


def iso(value: datetime | None) -> str | None:
    """时间 → ISO 8601 带时区（库里存的是 naive UTC，这里补上 +00:00）。"""
    if value is None:
        return None
    return to_utc(value).isoformat()


def _convert(value: Any) -> Any:
    """递归把 dataclass / datetime / Path 变成 JSON 友好的形状。

    ★ `datetime` 走 `iso()`（UTC + 偏移量），`Path` 走 `str()` ——
      两者都是"库里/磁盘上的东西"，页面只需要能显示、能比较，不需要可写回。
    """
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _convert(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_convert(item) for item in value]
    return value


def as_payload(value: Any) -> Any:
    """dataclass（可嵌套）→ dict；其它类型按 `_convert` 处理。

    ★ 用 `asdict()` 拿字段（不改原对象），再统一过一遍 `_convert`。
    """
    if is_dataclass(value) and not isinstance(value, type):
        return _convert(asdict(value))
    return _convert(value)

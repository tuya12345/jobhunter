"""导出待投清单（Markdown / CSV，写到 `output/`）。

★ 隐私红线：**只写新文件，绝不覆盖任何已有文件**（`utils/io.write_text` 的 docstring
  里写了这条纪律，这里负责兑现它）。做法是每次导出都生成一个**带时间戳的唯一文件名**，
  并且仍然检查一次"目标已存在就换名" —— 万一同一分钟里跑两次也不会互相覆盖。

★ 为什么导出内容里**不含简历原文**：导出的 Markdown/CSV 常常会被丢进聊天窗口、
  云笔记或直接发给别人看。里面只要有公司、职位、分数、链接和开场白就够用了；
  把简历正文塞进导出文件，等于给自己挖一个泄露渠道。
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from jobhunter.services.today_svc import TodayItem
from jobhunter.utils.io import ensure_dir, write_text
from jobhunter.utils.logging import get_logger
from jobhunter.utils.time import fmt_date_local, now_utc, to_local

logger = get_logger(__name__)

__all__ = ["export_csv", "export_markdown", "unique_path"]

#: CSV 表头（顺序固定，方便在 Excel 里做透视）
_CSV_FIELDS = (
    "序号",
    "公司",
    "职位",
    "城市",
    "匹配分",
    "用哪份简历",
    "变体",
    "链接",
    "完整版开场白",
    "精简版开场白",
)


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """在目录里找一个不存在的文件名（`stem-2.md`、`stem-3.md`…）。"""
    ensure_dir(directory)
    candidate = directory / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


def _stamp(now: datetime | None = None) -> str:
    """文件名里的时间戳（北京时间，精确到分钟）。"""
    local = to_local(now or now_utc())
    return local.strftime("%Y%m%d-%H%M")


def export_markdown(
    items: Iterable[TodayItem], *, output_dir: Path, now: datetime | None = None
) -> Path:
    """把清单写成 Markdown，返回文件路径。"""
    items = list(items)
    local_now = to_local(now or now_utc())
    lines = [
        f"# 今日待投清单（{fmt_date_local(local_now)}）",
        "",
        f"共 {len(items)} 条。开场白可直接复制；投完用 `jh apply <序号> --status applied` 打标。",
        "",
    ]
    for item in items:
        lines.append(f"## {item.index}. {item.company} · {item.title}")
        lines.append("")
        lines.append(f"- 匹配分：**{item.score}**")
        if item.job.city:
            lines.append(f"- 城市：{item.job.city}")
        if item.resume_name:
            lines.append(f"- 用哪份简历：{item.resume_name}")
        if item.greeting_variant:
            lines.append(f"- 开场白变体：{item.greeting_variant}")
        if item.url:
            lines.append(f"- 链接：{item.url}")
        lines.append("")
        lines.append(f"**完整版**（{len(item.greeting_full)} 字）")
        lines.append("")
        lines.append(f"> {item.greeting_full}")
        lines.append("")
        if item.greeting_short:
            lines.append(f"**精简版**（{len(item.greeting_short)} 字）")
            lines.append("")
            lines.append(f"> {item.greeting_short}")
            lines.append("")
        if item.greeting_issues:
            lines.append("⚠️ 开场白降级说明：" + "；".join(item.greeting_issues))
            lines.append("")
    path = unique_path(output_dir, f"today-{_stamp(now)}", ".md")
    return write_text(path, "\n".join(lines))


def export_csv(
    items: Iterable[TodayItem], *, output_dir: Path, now: datetime | None = None
) -> Path:
    """把清单写成 CSV（Excel 能直接打开），返回文件路径。

    ★ `newline=""` 必须显式给：不写的话 Windows 上每行之间会多一个空行
      （csv 模块自己写 `\r\n`，文本模式再翻译一次）。
    """
    items = list(items)
    buffer = io.StringIO()
    # QUOTE_ALL：开场白里有逗号、引号、换行，全引起来最省事也最不容易被 Excel 吃错
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL)
    writer.writerow(_CSV_FIELDS)
    for item in items:
        writer.writerow(
            [
                item.index,
                item.company,
                item.title,
                item.job.city or "",
                item.score,
                item.resume_name,
                item.greeting_variant,
                item.url,
                item.greeting_full,
                item.greeting_short,
            ]
        )
    path = unique_path(output_dir, f"today-{_stamp(now)}", ".csv")
    # ★ CSV 要带 BOM，否则 Excel 打开中文是乱码（这是 Windows 上的经典坑：
    #   文件本身是 UTF-8 没错，但 Excel 默认按本地编码猜）。
    return write_text(path, "\ufeff" + buffer.getvalue())

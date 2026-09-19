"""导出待投清单（Markdown / CSV，写到 `output/`）。

★★ 隐私红线：**只写新文件，绝不覆盖任何已有文件**。

  这条纪律不能靠"文件名带时间戳 + `exists()` 检查"来兑现 —— 那只是**概率性**的
  命名（`export_svc.py` 原实现，ISSUES #38）：两次导出之间、甚至"检查完到写下去"
  之间只要有人/别的进程创建了同名文件，底层 `utils/io.write_text` 的 `os.replace`
  就会**无条件覆盖**它。跨进程（两个终端、一个定时任务）时尤其明显。

  所以真正的保证是**排他创建**（`O_CREAT | O_EXCL`）：
  `_write_exclusive()` 直接以"不存在才创建"的方式打开文件，
  已存在就**换一个名字重试** —— 名字的生成（`unique_path`）只负责给候选，
  "到底能不能写"由操作系统这一原子操作裁决。Windows 上同样成立
  （`open(path, "x")` 就是 `CREATE_NEW`）。

★ 为什么导出内容里**不含简历原文**：导出的 Markdown/CSV 常常会被丢进聊天窗口、
  云笔记或直接发给别人看。里面只要有公司、职位、分数、链接和开场白就够用了；
  把简历正文塞进导出文件，等于给自己挖一个泄露渠道。
"""

from __future__ import annotations

import csv
import errno
import io
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from jobhunter.services.today_svc import TodayItem
from jobhunter.utils.io import ENCODING, ensure_dir
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

#: 排他创建的重试上限。到上限说明"这一分钟里已经有 100 个同名导出物"，
#: 那已经不正常了 —— 明确报错比无限循环好。
_MAX_EXCLUSIVE_ATTEMPTS = 100


def _candidate_names(stem: str, suffix: str) -> Iterable[str]:
    """候选文件名：`stem.md`、`stem-2.md`、`stem-3.md`…（无限序列）。"""
    yield f"{stem}{suffix}"
    counter = 2
    while True:
        yield f"{stem}-{counter}{suffix}"
        counter += 1


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """在目录里找一个**当前看起来**不存在的文件名（`stem-2.md`、`stem-3.md`…）。

    ★ 这个函数只是**给候选名**，它给不出任何保证（`exists()` 与真正的写入之间
      永远有窗口）。真正的"绝不覆盖"由 `_write_exclusive()` 的排他创建兑现 ——
      见模块 docstring 的 ISSUES #38 说明。
    """
    ensure_dir(directory)
    candidate = directory / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


def _write_exclusive(directory: Path, stem: str, suffix: str, content: str) -> Path:
    """**排他创建**一个新文件并写入内容，返回真正的路径。

    ★★ 这是"导出绝不覆盖"的硬保证（ISSUES #38）：

        · `open(path, "x")` → 底层是 `O_CREAT | O_EXCL`（Windows 上是 `CREATE_NEW`），
          "检查是否存在"和"创建"是**同一个原子操作**，没有 TOCTOU 窗口；
        · 已存在（`FileExistsError`）就换下一个候选名重试 ——
          候选名来自 `unique_path()` 给出的那一支，保证第一次尝试与旧行为一致
          （同一个 `stem`，有冲突才 -2、-3…）；
        · 文件由**独占的 fd** 直接承载，不走 `write_text` 的"写临时文件 + os.replace"
          （那个 replace 会无条件覆盖，正是本 issue 的病灶）。

    ★ 为什么不用 `os.replace` 的原子性来替代：原子替换保证的是"内容不会写一半"，
      不保证"不覆盖别人的文件"。这里要的是后者，两者不能互换。
    """
    ensure_dir(directory)
    for attempt, name in enumerate(_candidate_names(stem, suffix), start=1):
        if attempt > _MAX_EXCLUSIVE_ATTEMPTS:  # pragma: no cover - 极端病态情况
            raise FileExistsError(
                f"{directory} 里已有 {_MAX_EXCLUSIVE_ATTEMPTS} 个 {stem}{suffix} 系列文件，"
                "不再继续尝试（先清理 output/ 再导出）"
            )
        path = directory / name
        try:
            # ★ newline=""：CSV 自己要写 \r\n，文本模式再翻译一次就会多空行
            with open(path, "x", encoding=ENCODING, newline="") as handle:  # noqa: PTH123
                handle.write(content)
        except FileExistsError:
            # ★ Windows 上"目标已存在"也可能报 EACCES（不是 EEXIST）；
            #   Python 会把这两种都映射成 FileExistsError，所以这里能一并兜住。
            logger.debug("导出目标已存在，换名重试：%s", path)
            continue
        except OSError as exc:  # pragma: no cover - 磁盘满/权限等
            if exc.errno in (errno.EEXIST, errno.EACCES):
                continue
            raise
        return path
    raise AssertionError("unreachable")  # pragma: no cover - 循环里必 return 或 raise


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
    # ★★ 排他创建（不是 `write_text`）：见 `_write_exclusive` 的说明。
    #    返回的路径是**真正写成的那个**（重试过就是 -2/-3…）。
    return _write_exclusive(output_dir, f"today-{_stamp(now)}", ".md", "\n".join(lines))


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
    # ★ CSV 要带 BOM，否则 Excel 打开中文是乱码（这是 Windows 上的经典坑：
    #   文件本身是 UTF-8 没错，但 Excel 默认按本地编码猜）。
    # ★★ 命名与落盘都由 `_write_exclusive` 负责：``unique_path`` 那套
    #    "时间戳 + exists()" 只是命名习惯，**保证**来自排他创建（ISSUES #38）。
    return _write_exclusive(
        output_dir, f"today-{_stamp(now)}", ".csv", "\ufeff" + buffer.getvalue()
    )

"""IO 工具：全项目**唯一**允许直接读写文本文件的地方，且强制 UTF-8。

╔══════════════════════════════════════════════════════════════════════════╗
║ 为什么必须封这一层（评审采纳项 #8）                                        ║
╚══════════════════════════════════════════════════════════════════════════╝

Windows 上 `open("简历.txt")` 用的是**系统区域编码**（中文系统是 cp936/GBK），
不是 UTF-8。后果：

    · 读用户导出的 UTF-8 文本 → `UnicodeDecodeError: 'gbk' codec can't decode...`
    · 写中文导出文件         → 文件在别人机器上打开是乱码
    · 更坏的情况：**写入时静默丢字**（GBK 编码不了 emoji，某些路径下抛错、某些路径下变 '?'）

而且这个坑"我这儿能跑"特别具有欺骗性 —— 只要你的项目路径/内容里没有中文就永远不触发。

三层防护：
    1. 本模块：所有读写都写死 `encoding="utf-8"`（调用方**无法**忘记）；
    2. lint：ruff 的 `PLW1514`（裸 `open()` 必须显式 encoding）。
       ★ **它是 preview 规则** —— 只在 `pyproject.toml` 的 `select` 里列出来
       **不会生效**（ruff 只给一句 "has no effect because preview is not enabled" 的提示）。
       所以它由一条**单独的 CI 步骤** `ruff check --preview --select PLW1514` 守着
       （见 `.github/workflows/ci.yml` 与 `pyproject.toml` 里那段说明：不开全局 preview
       是因为会一次性引入 9 条无关规则、65 处误报）。
       `tests/unit/test_encoding_lint_guard.py` 同时用 AST 自查一遍 `src/`，
       免得"这条 CI 步骤被谁删掉"之后又变成"注释说有效、实际无效"。
    3. CI 跑 windows-latest：真在中文 Windows 上验证。

另外两个细节：
    · `newline=""`（写）与 `newline=None`（读）：避免 Windows 把 `\\n` 悄悄变成 `\\r\\n`，
      让导出文件在 Git 里产生"整文件都改了"的假 diff；
    · 原子写（先写同目录临时文件再 replace）：写导出文件时中途崩掉，
      不会留下一个半截的、看起来正常的文件。

★ 第四层（CI 实测补上的）：**标准输出也要强制 UTF-8**，见 `force_utf8_output()`。
  GitHub Actions 的 windows-latest 控制台是 **cp1252**，
  `jh --help` 里的 `→` 和 emoji 会直接让 rich 抛 UnicodeEncodeError 而**整个命令失败**。
  这不是"显示不好看"，是程序崩了 —— 所以必须在**任何输出发生之前**改掉 stdout/stderr 的编码。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

__all__ = [
    "ENCODING",
    "append_text",
    "ensure_dir",
    "force_utf8_output",
    "from_json",
    "read_json",
    "read_text",
    "to_json",
    "write_json",
    "write_text",
]

# 全项目统一的编码常量。不要在任何地方另写 "utf-8" 字面量，统一引用这里。
ENCODING = "utf-8"


def force_utf8_output() -> None:
    """把 stdout / stderr 强制切成 UTF-8（**幂等**，可重复调用）。

    为什么必须做，而不是"用 emoji 要小心一点"：
        Python 的 stdio 编码取自运行环境。
          · Windows 控制台：默认 cp936（中文）/ cp1252（英文）
          · GitHub Actions windows runner：cp1252
          · Linux/macOS：通常本来就是 UTF-8
        一旦是 cp1252，本项目 CLI 里的 `→`、`·`、`✓`、emoji 全都会抛
        `UnicodeEncodeError: 'charmap' codec can't encode characters`，
        **直接让命令以非 0 退出**。也就是说在英文 Windows 上，`jh --help` 都是崩的。

    为什么要在**模块导入时**调用（见 jobhunter/__init__.py）：
        rich 的 `Console` 在构造时就会探测并**缓存**底层终端的编码，
        而各命令模块里的 `console = Console()` 是模块级对象（import 时就创建）。
        如果在 `main()` 里才改编码，那些 Console 已经拿着 cp1252 了，改了也没用。

    为什么用 `errors="replace"` 而不是默认的 `strict`：
        这是**最后一道保险**。即使某个环境连 UTF-8 都写不出去
        （比如被重定向到一个用别的编码打开的管道），
        也只会把个别字符显示成 `?`，而不是让整个命令崩掉。
        —— "输出有个别乱码"远好于"工具直接不能用"。

    为什么用 `getattr` / try-except 而不是直接调：
        `sys.stdout` 在嵌入式环境、被 pytest 捕获时、或换成自定义流时
        可能没有 `reconfigure()`。这里静默跳过，绝不让"修编码"本身成为新的崩溃点。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding=ENCODING, errors="replace")
        except (ValueError, OSError, AttributeError):
            # 流已关闭 / 不支持重配置 → 算了，保持原样
            continue


# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------
def ensure_dir(path: str | os.PathLike[str]) -> Path:
    """确保目录存在（已存在不报错），返回 Path。

    为什么到处都要用它：SQLite 在**文件不存在**时会自动建库，
    但如果 `data/` 目录本身不存在，报错信息是一句很含糊的
    `unable to open database file` —— 极难排查。所以建库前先 ensure_dir。
    """
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# 文本
# ---------------------------------------------------------------------------
def read_text(path: str | os.PathLike[str], *, default: str | None = None) -> str:
    """读文本文件（强制 UTF-8）。

    Args:
        default: 文件不存在时返回它而不是抛错。
                 用途：读可选的用户线索文件、读还没生成的 state 文件。
                 传 None（默认）表示"文件必须存在"。
    """
    p = Path(path)
    try:
        # newline=None：把 \r\n 统一成 \n，读进来就好处理
        return p.read_text(encoding=ENCODING)
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def write_text(
    path: str | os.PathLike[str],
    content: str,
    *,
    atomic: bool = True,
) -> Path:
    """写文本文件（强制 UTF-8），返回 Path。

    ★ 本项目的隐私红线之一：**导出简历只写新文件，绝不覆盖原件**。
      这个函数本身不做保护（它只是个工具），保护写在 services 层：
      导出前先 assert 目标路径 != 源路径，导出后校验源文件 hash 没变。
      但这里默认开启 `atomic=True`，保证"写到一半崩了不会留半截文件"。

    Args:
        atomic: True → 先写同目录临时文件再 os.replace（同分区内是原子操作）；
                False → 直接写（仅在写临时文件本身时用）。
    """
    p = Path(path)
    ensure_dir(p.parent)

    if not atomic:
        with open(p, "w", encoding=ENCODING, newline="") as fh:
            fh.write(content)
        return p

    # 临时文件必须和目标在**同一个目录**，否则 os.replace 可能跨分区而失败
    fd, tmp_name = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=ENCODING, newline="") as fh:
            fh.write(content)
        os.replace(tmp_path, p)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return p


def append_text(path: str | os.PathLike[str], content: str) -> Path:
    """追加文本（强制 UTF-8）。用于日志型文件（如 leads 原始抓屏记录）。"""
    p = Path(path)
    ensure_dir(p.parent)
    with open(p, "a", encoding=ENCODING, newline="") as fh:
        fh.write(content)
    return p


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------
def to_json(data: Any, *, indent: int | None = 2) -> str:
    """对象 → JSON 字符串。

    固定参数的理由：
        ensure_ascii=False —— 中文直接可读（存库和导出给人看的都是中文，
                              转成 \\uXXXX 虽然正确但完全没法肉眼排查）
        indent=2           —— 导出文件、落库的 *_json 字段都便于 diff
        sort_keys=False    —— 保持字段顺序（尤其是 LLM 返回的字段顺序有意义时）
    """
    return json.dumps(data, ensure_ascii=False, indent=indent, sort_keys=False, default=str)


def from_json(text: str | None, *, default: Any = None) -> Any:
    """JSON 字符串 → 对象。

    容忍三种"脏"输入，因为它们在本项目里都会真实出现：
        · None / ""          → 返回 default（数据库里未填充的字段就是 None）
        · 空白字符串         → 返回 default
        · 坏 JSON            → 返回 default（手工改库、旧版本残留格式）
    这是**读**路径，宽容比严格更有用；严格校验放在 service 层写 pydantic 模型时做。
    """
    if text is None:
        return default
    stripped = text.strip()
    if not stripped:
        return default
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return default


def read_json(path: str | os.PathLike[str], *, default: Any = None) -> Any:
    """读 JSON 文件（强制 UTF-8）；不存在或损坏时返回 default。

    注意这里给 read_text 传 default="" 而不是 None：
    对"文件不存在"和"文件是空的"两种情况的处理完全一样（都返回 default），
    所以不需要在这里区分。
    """
    text = read_text(path, default="")
    return from_json(text, default=default)


def write_json(
    path: str | os.PathLike[str],
    data: Any,
    *,
    indent: int | None = 2,
    atomic: bool = True,
) -> Path:
    """写 JSON 文件（强制 UTF-8 + 原子写）。

    ★ 用途示例：`jh today` 导出待投清单到 output/、`jh leads export` 导出官网清单。
      导出一律**写新文件**，不覆盖用户已有的东西。
    """
    return write_text(path, to_json(data, indent=indent) + "\n", atomic=atomic)

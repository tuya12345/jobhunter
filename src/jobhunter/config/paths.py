"""路径解析：把"文件放哪里"这件事收成一个地方。

╔══════════════════════════════════════════════════════════════════════════╗
║ 为什么不让业务代码自己拼路径                                               ║
╚══════════════════════════════════════════════════════════════════════════╝

如果 services 里到处写 `Path("data/jobhunter.db")`，会有两个后果：

1. **依赖当前工作目录**：在 `D:/a/b` 下运行和在项目根下运行，用的库不是同一个 ——
   用户会以为"我的数据丢了"。
2. **测试无法隔离**：单测一旦忘了改路径，就会写到真实库上。
   （本项目对这条零容忍：测试洗掉真实投递记录是不可接受的意外。）

所以本模块提供两个入口：
    · 默认布局：`<项目根>/data`、`/output`、`/state`（项目根 = pyproject.toml 所在目录）
    · 测试注入：直接给 Settings 传 `data_dir` 等字段（见 config/settings.py），
      或调用 `default_paths(root=tmp_path)`。

目录用途：
    data/      SQLite 库文件、抓屏产物（data/leads/）—— 真实数据，gitignore
    output/    导出的 Markdown / CSV / docx —— 产物，gitignore
    state/     运行时状态（上次运行时间、临时进度）—— gitignore
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from jobhunter.utils.io import ensure_dir

__all__ = ["PathSet", "default_paths", "find_project_root"]


@dataclass(frozen=True)
class PathSet:
    """一组解析好的路径。frozen=True 表示创建后不可变 —— 避免运行中被偷偷改掉。"""

    root: Path  # 项目根目录
    data_dir: Path  # 数据库与运行时数据
    output_dir: Path  # 导出产物
    state_dir: Path  # 运行时状态

    @property
    def db_path(self) -> Path:
        """SQLite 库文件路径。"""
        return self.data_dir / "jobhunter.db"

    @property
    def sqlite_url(self) -> str:
        """SQLAlchemy 用的 SQLite URL。

        注意用 `as_posix()`：Windows 上 `sqlite:///D:\\a\\b.db` 里的反斜杠会被
        URL 解析器吃掉，必须转成 `D:/a/b.db` 这种正斜杠形式才可靠。
        """
        return f"sqlite:///{self.db_path.as_posix()}"

    @property
    def leads_dir(self) -> Path:
        """P7 抓屏产物目录（一次抓屏一个子目录）。"""
        return self.data_dir / "leads"

    def ensure_all(self) -> PathSet:
        """确保三个目录都存在，返回自身（便于链式调用）。

        为什么建库前必须调它：SQLite 文件不存在会自动创建，
        但**目录**不存在时只报一句含糊的 `unable to open database file`。
        """
        for directory in (self.data_dir, self.output_dir, self.state_dir):
            ensure_dir(directory)
        return self


@lru_cache(maxsize=1)
def find_project_root(start: Path | None = None) -> Path:
    """向上查找项目根目录（含 pyproject.toml 的那一层）。

    为什么需要：`jh` 装成命令行工具后，用户可能在任何目录下执行它。
    用"文件位置"而不是"当前工作目录"来定位项目根，行为才稳定。
    兜底策略（找不到时）：返回当前文件上溯 4 层（src/jobhunter/config/paths.py），
    再退一步用 cwd —— 保证任何情况下都能返回一个可用的目录，不会让 CLI 直接崩。
    """
    current = Path(start or __file__).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate

    # 开发态兜底：src/jobhunter/config/paths.py → 上溯 4 层 = 项目根
    fallback = Path(__file__).resolve().parents[3]
    if (fallback / "pyproject.toml").is_file():
        return fallback
    return Path.cwd()


def default_paths(root: Path | None = None) -> PathSet:
    """按默认布局构造 PathSet（不创建目录，只算路径）。

    环境变量覆盖（给"把库放到加密盘/其他盘"这类需求留口子）：
        JH_DATA_DIR / JH_OUTPUT_DIR / JH_STATE_DIR
    """
    base = Path(root) if root is not None else find_project_root()
    data_dir = os.environ.get("JH_DATA_DIR")
    output_dir = os.environ.get("JH_OUTPUT_DIR")
    state_dir = os.environ.get("JH_STATE_DIR")
    return PathSet(
        root=base,
        data_dir=Path(data_dir) if data_dir else base / "data",
        output_dir=Path(output_dir) if output_dir else base / "output",
        state_dir=Path(state_dir) if state_dir else base / "state",
    )

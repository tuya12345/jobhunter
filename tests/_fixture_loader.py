"""共享测试小工具：加载 `tests/fixtures/` 下的脚本。

★ 为什么需要这个（而不是直接 `from make_sample_resume import …`）

    `tests/fixtures/` 里放的是**脚本**而不是包（没有 `__init__.py`），
    因为 `make_sample_resume.py` 要被当作命令行工具直接跑：
        python tests/fixtures/make_sample_resume.py

    一旦把 fixtures 变成包，脚本里的相对路径与 `__main__` 语义都会变，
    给"生成 fixture"这件简单事增加无谓的复杂度。

    所以测试这边用 importlib 按文件路径加载 —— 两种情况都能用：
    包内测试与被直接执行的脚本。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

__all__ = ["FIXTURES_DIR", "load_fixture_module"]

#: 本文件位于 tests/ 下，所以 fixtures 目录是同级的 tests/fixtures
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_fixture_module(name: str) -> ModuleType:
    """按文件路径加载 `tests/fixtures/<name>.py` 并缓存到 sys.modules。

    Args:
        name: 不含 `.py` 的文件名，如 `make_sample_resume`。
    """
    if name in sys.modules:
        return sys.modules[name]

    path = FIXTURES_DIR / f"{name}.py"
    if not path.is_file():
        raise FileNotFoundError(f"找不到 fixture 脚本：{path}")

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - 理论上不会发生
        raise ImportError(f"无法加载 fixture 脚本：{path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

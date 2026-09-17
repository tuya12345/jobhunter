"""`tests/verify/` 的局部 fixture：只做"路径/环境"层面的准备，不做业务打桩。

隔离本身复用项目根 `tests/conftest.py` 的 `isolated_env`（autouse），
这里不重复实现 —— 但**不依赖**它提供的业务 fixture，保证验证测试可独立解释。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def sample_html() -> str:
    """一段"像真的"岗位详情页 HTML（内容虚构）。"""
    from tests.verify._helpers import SAMPLE_HTML

    return SAMPLE_HTML

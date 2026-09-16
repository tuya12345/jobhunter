"""Provider 工厂：按配置挑一个 `LLMProvider`（P3 新增）。

★ 为什么单独一个文件，而不是塞进 `llm/__init__.py`：

    如果把工厂写在 `llm/__init__.py` 里，它就要 import `llm.deepseek`；
    而 `llm.deepseek` 又要 import `llm.base` —— 后者会触发
    `llm/__init__.py` 的执行。于是出现**循环导入**：
    先 import `llm.deepseek` 的人会拿到一个"只执行了一半"的包。

    拆成两个模块后依赖方向是干净的：
        factory → deepseek → base
        factory → mock     → base

★ 这一层的存在意义：**业务代码永远不 import 具体 Provider**。
    `match_llm_svc` 只写 `provider = get_provider(settings)`，
    于是测试切到 mock、将来接别的模型，都不用碰业务代码。
"""

from __future__ import annotations

from jobhunter.config.settings import AppSettings
from jobhunter.llm.base import LLMProvider
from jobhunter.llm.deepseek import DeepSeekProvider
from jobhunter.llm.mock import MockProvider
from jobhunter.utils.logging import get_logger

logger = get_logger(__name__)

__all__ = ["get_provider"]


def get_provider(settings: AppSettings) -> LLMProvider:
    """按 `settings.llm_provider` 返回 Provider。

    · `deepseek`（默认）：真的联网。**没有 key 也返回它** ——
      "没配 key"要在调用处被明确处理成"降级到规则分 + 提示"，
      而不是在这里偷偷换成 mock（那样用户会看到一堆假分数，还以为是真的）。
    · `mock`：假 Provider，不发网络请求（测试与 `jh demo` 用）。
    """
    if settings.llm_provider == "mock":
        logger.debug("使用 MockProvider（llm_provider=mock）")
        return MockProvider()
    return DeepSeekProvider(
        settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
    )

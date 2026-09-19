"""DeepSeek Provider（P3 实现，走 OpenAI 兼容协议）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 为什么用 httpx 手写请求，而不是装 `openai` SDK                              ║
╚══════════════════════════════════════════════════════════════════════════╝

DeepSeek 的接口就是 OpenAI 的 `/chat/completions`，请求/响应结构非常稳定。
这里只用到两个字段（messages / response_format）与三个响应字段
（choices[0].message.content / usage），为它引入一整个 SDK 的取舍是：

    · 好处（手写）：零额外依赖；出错时看到的是**原始响应体**，
      排查"为什么返回空 content"比透过 SDK 的异常包装清楚得多；
      `pyproject.toml` 里 `[llm]` 那组 extras 就可以一直不装。
    · 代价（手写）：将来要用 function calling / 流式工具调用，
      得自己补齐（到 P5 真需要时再换 SDK 不迟，Provider 层就是为此存在的）。

★★ 三个必须写死的细节（DeepSeek JSON 模式的"三件套"，写错就静默退化）

    ① **提示词里必须出现 "json" 字样**（`response_format={"type":"json_object"}`
       要求提示词里含这个词，否则 API 直接报错或返回散文）；
    ② 提示词里给出 **schema 示例**（模型照着抄字段名，比空口描述可靠得多）；
    ③ `max_tokens` **给足**（截断的 JSON 无法解析，比重试一次还贵）。

    前两条在 `services/match_llm_svc.py` 的提示词模板里保证，第三条在这里
    通过 `payload.max_tokens` 传下去。**三者缺一，症状都是"偶发解析失败"** ——
    这也是为什么 P0 就先把 `MockProvider` 的"空 content / 坏 JSON"开关做好了。

★ 重试策略的分工（别在两处都重试）：
    这一层**只重试"网络层"的失败**（超时、连接被重置、429/5xx）——
    它们重试一次基本就好了，且与提示词无关。
    "返回了空 content / 不是 JSON"属于**业务层**失败，由 `match_llm_svc` 决定
    重试还是降级到规则分。混在一起重试会放大 token 消耗且难定位。
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx

from jobhunter.llm.base import LLMProvider, LLMResponse, PromptPayload
from jobhunter.utils.logging import get_logger

logger = get_logger(__name__)

__all__ = ["DeepSeekError", "DeepSeekProvider"]


class DeepSeekError(RuntimeError):
    """调用 DeepSeek 失败（网络层，重试后仍失败）。"""


#: 价格表（元 / 百万 tokens），用于估算成本。
#: 来源：DeepSeek 官方定价（deepseek-chat）。**这是"估算"不是"账单"** ——
#: 官方调价、缓存命中折扣、夜间折扣都不会体现在这里。
#: 写成一处的意义：将来调价只改这里，且所有历史成本的口径一致。
_PRICE_PER_MILLION: dict[str, tuple[float, float]] = {
    # 模型名: (输入价, 输出价)
    "deepseek-chat": (2.0, 8.0),
    "deepseek-reasoner": (4.0, 16.0),
}

#: 网络层重试次数（不含首次）。只针对超时/连接错误/429/5xx。
_NETWORK_RETRIES = 2
#: 重试的基础退避（秒）。指数退避：0.8s → 1.6s。
_RETRY_BACKOFF = 0.8


def estimate_cost(model: str, tokens_in: int | None, tokens_out: int | None) -> float | None:
    """按价格表估算成本（元）。价格表里没有的模型返回 None（**不猜**）。"""
    price = _PRICE_PER_MILLION.get(model)
    if price is None or tokens_in is None or tokens_out is None:
        return None
    in_price, out_price = price
    return round(tokens_in / 1_000_000 * in_price + tokens_out / 1_000_000 * out_price, 6)


class DeepSeekProvider(LLMProvider):
    """DeepSeek 的 `/chat/completions` 封装（同步 + 异步）。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        """
        Args:
            transport: 仅测试用 —— 塞一个 `httpx.MockTransport` 就能在不联网的
                       情况下测完整的请求/响应/重试路径。生产环境传 None。
        """
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._transport = transport

    # ------------------------------------------------------------------
    def is_configured(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------------
    def complete(self, payload: PromptPayload) -> LLMResponse:
        """同步调用（`jh tailor brief` 这类单条交互）。"""
        if not self.api_key:
            raise DeepSeekError("没有配置 deepseek_api_key（在项目根的 .env 里填）")
        body = self._build_body(payload)
        last_error: Exception | None = None
        for attempt in range(_NETWORK_RETRIES + 1):
            try:
                with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
                    response = client.post(self._endpoint(), json=body, headers=self._headers())
                return self._parse_response(response)
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if not self._is_retryable(exc) or attempt == _NETWORK_RETRIES:
                    break
                delay = _RETRY_BACKOFF * (2**attempt)
                logger.warning("DeepSeek 调用失败（%s），%.1fs 后重试", exc, delay)
                time.sleep(delay)
        raise DeepSeekError(f"DeepSeek 调用失败：{last_error}") from last_error

    async def acomplete(self, payload: PromptPayload) -> LLMResponse:
        """异步调用（P3 的批量精评走这条，配合 Semaphore 并发）。"""
        if not self.api_key:
            raise DeepSeekError("没有配置 deepseek_api_key（在项目根的 .env 里填）")
        body = self._build_body(payload)
        last_error: Exception | None = None
        for attempt in range(_NETWORK_RETRIES + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout, transport=self._transport
                ) as client:
                    response = await client.post(
                        self._endpoint(), json=body, headers=self._headers()
                    )
                return self._parse_response(response)
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if not self._is_retryable(exc) or attempt == _NETWORK_RETRIES:
                    break
                delay = _RETRY_BACKOFF * (2**attempt)
                logger.warning("DeepSeek 调用失败（%s），%.1fs 后重试", exc, delay)
                await asyncio.sleep(delay)
        raise DeepSeekError(f"DeepSeek 调用失败：{last_error}") from last_error

    # ------------------------------------------------------------------
    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _build_body(self, payload: PromptPayload) -> dict[str, Any]:
        """把 `PromptPayload` 转成请求体。

        ★ `max_tokens` 直接透传，**不在这一层兜底调大**：
          它是提示词设计的一部分（JSON 模式必须给足），
          在这一层偷偷改大，会让"提示词写错了"这类问题更难发现。
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in payload.messages],
            "temperature": payload.temperature,
            "max_tokens": payload.max_tokens,
        }
        if payload.json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """判断异常值不值得重试。

        ★ 4xx（除 429）**不重试**：401 是密钥错、400 是提示词不合规，
          重试一百次也是同样的结果，只会浪费时间并掩盖真正的错误信息。
        """
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            return status == 429 or status >= 500
        return isinstance(exc, httpx.TransportError)

    def _parse_response(self, response: httpx.Response) -> LLMResponse:
        """解析响应体。

        ★ 为什么把原始响应放进 `LLMResponse.raw`：
          "为什么这次返回空 content" 是本项目最需要排查的一类问题
          （空 content 会导致降级到规则分）。保留原始响应才能事后看清
          到底是 `finish_reason=length`（被截断）还是内容过滤。
          ⚠️ 但**不要整体打进日志** —— raw 里含 JD 全文，日志只打长度与状态。

        ★★ 200 + 非 JSON 必须变成**可读的错误**（ISSUES #10）

          真实常见：网关/代理返回 200，响应体却是一页 HTML
          （"502 Bad Gateway"、Cloudflare 的错误页、公司网络的拦截页…）。
          原来的写法直接 `response.json()`，于是冲出去的是
          `JSONDecodeError: Expecting value: line 1 column 1 (char 0)` ——
          它既没说"哪个状态码"、也没说"响应体长什么样"，
          用户（和三个月后的我）只能瞎猜是密钥错、网络错还是模型错。

          这里转成 `DeepSeekError` 并带上：状态码 + **截断并脱敏**的响应体开头。
          为什么截断：错误页动辄几十 KB，全文塞进异常会淹没真正有用的信息。
          为什么脱敏：万一某个代理把请求头回显进错误页，`sk-...` 就会顺着
          异常消息进日志/终端 —— 密钥泄露的代价远大于多留几个字符的排查价值。
        """
        if response.status_code >= 400:
            # 让 httpx 抛，交由上层统一处理（4xx 不重试的判定在 _is_retryable）
            raise httpx.HTTPStatusError(
                f"HTTP {response.status_code}: {response.text[:300]}",
                request=response.request,
                response=response,
            )

        try:
            data = response.json()
        except ValueError as exc:  # json.JSONDecodeError 是 ValueError 的子类
            raise DeepSeekError(
                f"DeepSeek 返回了 HTTP {response.status_code}，但响应体不是 JSON"
                f"（多半是网关/代理插进来的错误页，而不是模型的回答）。\n"
                f"响应体开头：{_body_preview(response)}"
            ) from exc

        if not isinstance(data, dict):
            # 合法 JSON 但不是对象（比如网关返回了 `[]`）：同样是"响应体不对"，
            # 直接 .get 会抛裸 AttributeError —— 那又是一句没法排查的错误。
            raise DeepSeekError(
                f"DeepSeek 返回了 HTTP {response.status_code}，响应体是合法 JSON，"
                f"但顶层不是对象而是 {type(data).__name__}。\n"
                f"响应体开头：{_body_preview(response)}"
            )

        choices = data.get("choices") or []
        content = ""
        finish_reason = None
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content") or ""
            finish_reason = choices[0].get("finish_reason")

        usage = data.get("usage") or {}
        tokens_in = usage.get("prompt_tokens")
        tokens_out = usage.get("completion_tokens")

        if not content.strip():
            # 只记长度与原因，绝不打印正文（正文里可能有 JD 与简历片段）
            logger.warning(
                "DeepSeek 返回空 content（finish_reason=%s，tokens_out=%s）",
                finish_reason,
                tokens_out,
            )

        return LLMResponse(
            content=content,
            model=data.get("model") or self.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=estimate_cost(self.model, tokens_in, tokens_out),
            raw=data,
        )


#: 响应体预览的长度上限（字符）。
_BODY_PREVIEW_LIMIT = 200


def _body_preview(response: httpx.Response, *, limit: int = _BODY_PREVIEW_LIMIT) -> str:
    """把响应体压成一行**可安全展示**的预览：压空白 → 脱敏 → 截断。

    ★ 三个动作都必要：
      · 压空白：HTML 错误页大量换行缩进，不压掉的话 200 字符里看不到几个字；
      · 脱敏：`sk-` 开头的密钥一旦被回显，不能让它进异常消息（进而进日志）；
      · 截断：留个标记，免得读者误以为"响应体就这么长"。
    """
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - charset 声明损坏时连 text 都可能抛
        return "（响应体无法解码）"
    text = re.sub(r"\s+", " ", text or "").strip()
    # 脱敏：任何形如密钥的串一律打码（宁可多打码，也不要漏一个）
    text = re.sub(r"sk-[A-Za-z0-9_\-]{3,}", "sk-****", text)
    if not text:
        return "（空响应体）"
    if len(text) > limit:
        return text[:limit] + f"…（已截断，共 {len(text)} 字符）"
    return text

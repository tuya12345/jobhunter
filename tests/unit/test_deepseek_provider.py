"""DeepSeek Provider 的**响应解析**测试：网络通了、但响应体不对时，用户看到什么。

★ 为什么单独给这一层写测试（而不是只在 `tests/verify` 里覆盖）

    `match_llm_svc` 的重试/降级逻辑很完整，唯独"响应体根本不是 JSON"这一路
    原来会漏出裸的 `JSONDecodeError`。而它恰恰是最常见的一类：
    公司网络/代理/网关插进来的错误页会带 **200** 状态码返回 HTML，
    密钥、配额、模型名全都正常 —— 只看状态码完全看不出问题在哪。

    所以这里断言的不是"抛了异常"，而是**异常消息里有没有排查所需的两个东西**：
    状态码与（截断、脱敏后的）响应体开头。

★ 全部走 `httpx.MockTransport`，不联网（本项目红线：测试绝不发出站请求）。
"""

from __future__ import annotations

import httpx
import pytest

from jobhunter.llm.base import LLMMessage, PromptPayload
from jobhunter.llm.deepseek import DeepSeekError, DeepSeekProvider


def _payload() -> PromptPayload:
    return PromptPayload(
        prompt_version=1,
        task="match",
        messages=[LLMMessage(role="user", content="请输出 json")],
    )


def _provider(handler) -> DeepSeekProvider:  # noqa: ANN001 - 测试内部的桩
    return DeepSeekProvider("sk-unit-test", transport=httpx.MockTransport(handler))


def test_http_200_html_body_raises_readable_error() -> None:
    """200 + HTML（网关错误页）→ `DeepSeekError`，消息里能看出"不是 JSON"。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>502 Bad Gateway</html>", request=request)

    with pytest.raises(DeepSeekError) as excinfo:
        _provider(handler).complete(_payload())

    message = str(excinfo.value)
    assert "json" in message.lower(), f"没告诉用户「响应不是 JSON」：{message}"
    assert "200" in message, f"没带状态码：{message}"
    assert "502 Bad Gateway" in message, f"没带响应体开头，排查只能靠猜：{message}"


def test_http_200_non_dict_json_is_rejected_readably() -> None:
    """合法 JSON 但顶层不是对象（`[]`）也不能抛裸 `AttributeError`。

    这一路的失效方式更隐蔽：`json.loads` 成功了，`.get()` 才炸 ——
    报出来的是 Python 内部错误，用户完全看不出"响应体形状不对"。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[], request=request)

    with pytest.raises(DeepSeekError) as excinfo:
        _provider(handler).complete(_payload())
    assert "list" in str(excinfo.value)


def test_error_message_does_not_echo_api_key() -> None:
    """错误页万一回显了请求头，密钥也不能顺着异常消息进日志/终端。

    ★ 这不是"多虑"：`sk-` 打码是**最后一道**防线，
      它挡住的正是"某个中间代理把请求原样回显"这种真实事故。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html>auth failed for Bearer sk-abcdef1234567890</html>",
            request=request,
        )

    with pytest.raises(DeepSeekError) as excinfo:
        _provider(handler).complete(_payload())
    assert "sk-abcdef1234567890" not in str(excinfo.value), "密钥被写进了异常消息"
    assert "sk-****" in str(excinfo.value), "打码了，但看不出「这里本来是个密钥」"


def test_long_error_body_is_truncated() -> None:
    """错误页动辄几十 KB —— 异常消息必须截断，否则真正有用的信息被淹没。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>" + "x" * 5000 + "</html>", request=request)

    with pytest.raises(DeepSeekError) as excinfo:
        _provider(handler).complete(_payload())
    message = str(excinfo.value)
    assert "已截断" in message
    assert len(message) < 1000, f"异常消息太长（{len(message)} 字符），没法在终端里读"


def test_valid_response_still_parses() -> None:
    """正常情况下解析路径不能被上面的改动影响（含 usage 与成本估算）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "deepseek-chat",
                "choices": [{"message": {"content": '{"score": 88}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
            request=request,
        )

    response = _provider(handler).complete(_payload())
    assert response.content == '{"score": 88}'
    assert response.tokens_in == 100 and response.tokens_out == 20
    assert response.cost is not None

"""假 LLM Provider：测试与 `jh demo` 用（P0 就实现）。

★ 为什么 P0 就要有它（而不是等 P3 再说）：
    没有假 Provider，"测试匹配打分"就必须真的联网调 DeepSeek。后果有两个，
    而且都很致命：
        1. **CI 会随机红**（网络抖动、余额不足、模型限流）→ 大家最后一定会
           把这些测试删掉或加 skip → 等于没测；
        2. 每次跑测试都花钱、变慢，本地就不愿意跑了。

    有了它，P3 的匹配逻辑可以用**完全确定性**的输入输出做断言：
        · 同样输入 → 同样输出，断言可以写死；
        · 可以精确构造"返回空 content""返回坏 JSON""返回越界分数"等
          异常场景，验证上层的重试与降级逻辑 —— 这些用真模型几乎无法稳定复现。

★ 设计：注册一个"任务 → 响应"的映射；没配的就返回一个可预测的默认值。
"""

from __future__ import annotations

import json
import re
from typing import Any

from jobhunter.llm.base import LLMProvider, LLMResponse, PromptPayload

__all__ = ["MockProvider", "fail_then_succeed_provider", "jittered_provider"]


class MockProvider(LLMProvider):
    """按 task 返回预设响应的假 Provider。

    用法：
        provider = MockProvider(responses={"match": '{"score": 88, ...}'})
        resp = provider.complete(payload)      # 不发网络请求

    还支持三种"故意使坏"的开关，专门用来测上层的健壮性：
        empty_times：前 N 次返回空 content（测重试）
        bad_json：永远返回无法解析的文本（测降级）
        jitter：给 score 加微小随机（**不要用在断言里**，只用于 demo 观感）
    """

    model = "mock-model"

    def __init__(
        self,
        responses: dict[str, str] | None = None,
        *,
        empty_times: int = 0,
        bad_json: bool = False,
        jitter: bool = False,
        calls: list[PromptPayload] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.empty_times = empty_times
        self.bad_json = bad_json
        self.jitter = jitter
        # 记录调用历史 —— 测试可以断言"同岗位跑两次只调了一次 LLM"（缓存测试）
        self.calls: list[PromptPayload] = calls if calls is not None else []
        self._empty_served = 0

    def complete(self, payload: PromptPayload) -> LLMResponse:
        self.calls.append(payload)

        # ① 模拟"偶发空 content"：这是 DeepSeek JSON 模式下真实存在的行为
        if self._empty_served < self.empty_times:
            self._empty_served += 1
            return LLMResponse(content="", model=self.model, tokens_in=10, tokens_out=0, cost=0.0)

        # ② 模拟"返回了非 JSON 的散文"（提示词漏写 "json" 时的典型后果）
        if self.bad_json:
            return LLMResponse(
                content="这个岗位看起来挺合适的，我觉得你可以投一下试试。",
                model=self.model,
                tokens_in=10,
                tokens_out=20,
                cost=0.0,
            )

        content = self.responses.get(payload.task) or self._default_for(payload)

        # ③ 可选的轻微抖动：仅用于 demo，让多次运行看起来"像真的"
        if self.jitter:
            content = self._apply_jitter(content)

        return LLMResponse(
            content=content,
            model=self.model,
            # 假数据也给出可预测的用量，便于测成本统计
            tokens_in=sum(len(m.content) for m in payload.messages) // 2,
            tokens_out=len(content) // 2,
            cost=0.0,
        )

    # ------------------------------------------------------------------
    def _default_for(self, payload: PromptPayload) -> str:
        """没预设响应时，按任务返回一个结构合法的默认值。

        ★ 关键：默认值必须**符合 schema**（字段名与真实实现一致）。
          否则测试会在"字段拼错"这种假问题上失败，掩盖真实 bug。
        """
        defaults: dict[str, dict[str, Any]] = {
            "match": {
                "score": 70,
                "strengths": ["Python 基础扎实"],
                "gaps": ["缺少大规模分布式经验"],
                "missing_keywords": ["Kubernetes"],
                "recommended_resume": None,
                "reason": "（mock）技能有交集，方向基本一致",
            },
            "jd_parse": {
                "jd_keywords": ["Python", "FastAPI", "MySQL"],
                "direction": "后端",
                "hard_requirements": {
                    "experience": "3-5年",
                    "education": "本科",
                    "city": "深圳",
                    "is_intern": False,
                },
            },
            "greeting": {
                "full": "（mock）看到岗位要求 Python 与 FastAPI，我做过类似的后端项目…",
                "short": "（mock）Python 后端方向，有 FastAPI 项目经验，方便聊两句吗？",
            },
            "tailor_brief": {
                "top_projects": ["项目A", "项目B", "项目C"],
                "top_skills": ["Python", "FastAPI"],
                "avoid": ["不必强调前端经历"],
            },
            "question_predict": {
                "questions": ["（mock）讲讲你做过的接口性能优化", "（mock）为什么选这个方向"],
            },
            "leads_parse": {
                "leads": [
                    {
                        "name": "（mock）某某科技",
                        "official_url": "https://example.com/careers",
                        "source_type": "官网",
                    }
                ]
            },
        }
        return json.dumps(defaults.get(payload.task, {"ok": True}), ensure_ascii=False)

    def _apply_jitter(self, content: str) -> str:
        """给 JSON 里的 score 加 ±3 的抖动（只用于 demo 观感）。"""

        def _bump(match: re.Match[str]) -> str:
            value = int(match.group(1))
            # 用固定步长而不是随机数：保证"看起来有变化"但测试仍可预测
            bumped = value + (3 if value % 2 == 0 else -3)
            return f'"score": {max(0, min(100, bumped))}'

        return re.sub(r'"score":\s*(\d+)', _bump, content)


def fail_then_succeed_provider(succeed_after: int = 1) -> MockProvider:
    """前 N 次返回空 content，之后返回正常结果。

    专门用于测"偶发空 content → 重试"这条路径。
    """
    return MockProvider(empty_times=succeed_after)


def jittered_provider(responses: dict[str, str] | None = None) -> MockProvider:
    """带抖动的假 Provider（demo 用，不要用于断言）。"""
    return MockProvider(responses, jitter=True)

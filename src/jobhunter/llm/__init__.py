"""llm：大模型接入层。

只有一件事：**把提示词送出去，把结构化结果拿回来**。它不知道"岗位""简历"是什么，
所以换模型、加 Provider 都不会污染业务代码。

  base.py       LLMProvider 抽象 + LLMResponse（含 tokens/cost 统计）  [P0 定义契约]
  deepseek.py   DeepSeek 实现（走 OpenAI 兼容协议）                    [P3]
  mock.py       假 Provider：测试用，不发网络请求                     [P0]
  cache.py      llm_cache 读写 + cache_key 计算                       [P3]

★★ cache_key 的设计（评审指出的原始 bug，务必不要改回去）：
      sha256(prompt_version | model | jd_hash | resume.content_hash)
   · 含 resume.content_hash → **改了简历不会拿到旧分数**（这是本工具最核心的价值）；
   · 含 prompt_version     → 改了提示词，旧结果不复用（否则你会以为"改提示词没用"）；
   · 含 model              → 换模型必须失效。

★ 结构化输出的"三件套"（DeepSeek JSON 模式的硬性要求，P3 实现时写死）：
   ① 提示词里必须出现 "json" 字样；② 给出 schema 示例；③ max_tokens 给足。
  并且要处理**偶发空 content**（重试 + 降级到规则分）。
"""

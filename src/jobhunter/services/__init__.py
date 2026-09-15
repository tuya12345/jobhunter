"""services：业务逻辑层。

为什么要有这一层（而不是把逻辑写在 CLI 里）：
  · CLI 只负责"把用户输入翻译成函数调用"，将来 P8 的 FastAPI 可以**原样复用**这些 service；
  · 业务逻辑可被单测直接调用，不用去解析终端输出；
  · 依赖方向单向：cli → services → (adapters | llm | storage | models)，反向依赖一律禁止。

星号 = 项目核心：
    ★ match_svc.py  共享中间层（JD 解析 + 简历匹配），产物被三处复用
    ★ fidelity.py   FidelityGuard（P5）—— 用代码而不是提示词保证"不编造"

各文件落地阶段见 docs/architecture.md §3 的目录树标注。
"""

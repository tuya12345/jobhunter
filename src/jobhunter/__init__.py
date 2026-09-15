"""JobHunter · 求职猎手。

一个「本地优先」的开源求职辅助工具：抓岗位详情 → 两级匹配打分 → 按 JD 定制开场白
→ 跟踪投递/笔试/面试/Offer 全流程。

这个包的分层（详见 docs/architecture.md）：

    cli/        Typer 命令层，只做参数解析与终端渲染，不含业务逻辑
    services/   业务层，纯逻辑、可被 CLI 与将来的 Web 复用
    adapters/   外部世界（网页/App），统一 JobSource 契约，主实现是 manual.py
    llm/        大模型 Provider 抽象 + DeepSeek 实现 + 缓存
    storage/    engine / session / PRAGMA / Alembic 调用
    models/     11 张表的 ORM 定义
    config/     pydantic-settings 配置（config.yaml + .env）
    utils/      io（强制 UTF-8）/ time（UTC）/ logging（脱敏）等无业务依赖的工具

三条不可逾越的红线（写进 README，也用代码守住）：
    1. 不做全自动投递 —— 脚本止于"出清单"，投递按钮由人点；
    2. 不做自动搜索翻页 —— 主路径是"人给链接 → 工具抓详情"；
    3. AI 改简历不得编造 —— 由代码级 FidelityGuard 强制校验。
"""

from __future__ import annotations

__version__ = "0.1.0"

# 当前阶段标记：CLI 占位命令会用它生成"该功能在 PX 阶段实现"的提示。
CURRENT_PHASE = "P0"

__all__ = ["CURRENT_PHASE", "__version__"]

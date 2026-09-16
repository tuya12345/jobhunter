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

# ★ 必须在**任何** rich/typer 的 Console 被构造之前执行，所以放在包导入时最前面：
#   rich 构造 Console 时会探测并缓存终端编码，而各命令模块的 `console = Console()`
#   是模块级对象（import 时就建好了）。等到 main() 里再改编码已经太晚。
#   背景：GitHub Actions 的 windows-latest 控制台是 cp1252，
#   `jh --help` 里的 → 和 emoji 会让 rich 抛 UnicodeEncodeError，命令直接崩。
from jobhunter.utils.io import force_utf8_output as _force_utf8_output

_force_utf8_output()

__version__ = "0.1.0"

# 当前阶段标记：CLI 占位命令会用它生成"该功能在 PX 阶段实现"的提示。
# ★ 每完成一个阶段都要记得改这里 —— 它是**用户唯一能一眼看到的阶段标记**
#   （`jh --version` 与每个占位命令的面板都会打印它）。
#   停更在这里 = 对用户说假话："明明能导简历了，工具还说自己在 P0"。
CURRENT_PHASE = "P3"

__all__ = ["CURRENT_PHASE", "__version__"]

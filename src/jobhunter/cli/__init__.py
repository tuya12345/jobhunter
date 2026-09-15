"""cli：Typer 命令层。

硬约束（docs/architecture.md §2）：
  · 只做「参数解析 + 调用 service + rich 渲染」，**禁止**在这里写业务逻辑；
  · 命令之间不互相调用，公共逻辑提取到 services/ 或 common.py。

文件职责：
    app.py          组装 Typer app + 全局 callback（顶部"待处理提醒"）
    common.py       终端输出辅助（表格/面板/JSON）与占位命令的统一提示
    commands/*.py   每个顶层命令一个文件；commands/__init__.py 是集中注册表

为什么命令要"一个文件一个命令"：本项目当学习材料用。
一个命令一个文件时，将来想改 `jh today` 只需打开 today_cmd.py，
不会被 19 个命令挤在一个 app.py 里绕晕。
"""

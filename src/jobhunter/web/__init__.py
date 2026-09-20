"""`jobhunter.web` —— P7 消费端的 Web 层（FastAPI + 静态页面）。

    app.py   `jh leads serve` 用的 FastAPI 应用（工厂函数 `create_app()`）

★ 为什么单独一层、而不是塞进 `cli/commands/`：

    命令层负责"解析参数、打印结果"，业务层负责"规则与写库"。Web 层和 CLI 是
    **同一层的两个入口**（都只是调用 `services/`），所以它是独立的一层；
    把它写进命令文件会让"页面做了什么"和"命令做了什么"混在一起，
    而这正是 #49 / #57 那类"两个入口口径不一致"的温床。

★ 依赖是**可选的**：`fastapi` / `uvicorn` 放在 `[web]` extra 里（见 pyproject），
  所以 `import jobhunter.web.app` **不能**出现在模块顶层被无条件导入的地方 ——
  CLI 里是在 `jh leads serve` 命令函数内部 import 的，没装 extra 时给中文提示。
  （理由同 `[docx]` / `[emulator]`：主依赖保持干净，用不到的人不必装一个 Web 框架。）
"""

from __future__ import annotations

__all__: list[str] = []

"""`python -m jobhunter` 入口。

为什么除了 `jh` 脚本还要留这个：
    · `jh` 依赖 console_scripts 安装进 PATH，某些环境（CI、受限的 Windows 终端、
      临时用 `uv run --no-project` 跑）可能没有；
    · `python -m jobhunter` 只要有 src 在 sys.path 里就能跑，是最可靠的兜底入口。

两者最终都调用同一个 `main()`，行为完全一致（不存在"两个入口行为不同"的问题）。
"""

from __future__ import annotations

from jobhunter.cli.app import main

if __name__ == "__main__":
    main()

"""剪贴板（`jh today --copy N` 用）。

★ 为什么不引入 pyperclip 之类的依赖：
  三个平台各一条命令就够了（Windows `clip` / macOS `pbcopy` / Linux `xclip`），
  而多一个依赖就多一个"在别人的机器上装不上"的可能。
  真正的取舍是**剪贴板这件事本身不可测**：与其假装它能测，
  不如把"执行命令"这一步做成**可注入**的（`runner` 参数），
  测试注入一个假 runner 验证"传进去的文本对不对"，不去碰真实剪贴板。

★ 隐私：往剪贴板写的是**要发给 HR 的话术**，不含简历原文与手机号。
  但这个函数**绝不允许把文本写进日志** —— 日志会被复制粘贴到别处。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable

from jobhunter.utils.logging import get_logger

logger = get_logger(__name__)

__all__ = ["copy_to_clipboard", "clipboard_command"]


def clipboard_command(platform: str | None = None) -> list[str] | None:
    """返回当前平台的剪贴板命令；找不到可用命令时返回 None。

    Linux 上 `xclip` 与 `xsel` 都可能缺（服务器/最小安装），
    所以用 `shutil.which` 实际探测，而不是"猜它在"。
    """
    platform = platform or sys.platform
    if platform.startswith("win"):
        return ["clip"]
    if platform == "darwin":
        return ["pbcopy"]
    for candidate in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        if shutil.which(candidate[0]):
            return candidate
    return None


def copy_to_clipboard(
    text: str,
    *,
    runner: Callable[[list[str], str], int] | None = None,
    platform: str | None = None,
) -> bool:
    """把文本写进系统剪贴板。成功返回 True。

    Args:
        runner: 注入点 —— `(command, text) -> returncode`。
                测试用它验证"命令与文本对不对"，不真去改用户的剪贴板。
    """
    if not text.strip():
        return False

    command = clipboard_command(platform)
    if command is None:
        logger.warning("这个系统上找不到可用的剪贴板命令（试过 clip / pbcopy / xclip / xsel）")
        return False

    if runner is None:

        def runner(cmd: list[str], payload: str) -> int:  # type: ignore[misc]
            # clip / pbcopy / xclip 都是从 stdin 读，所以用 input= 喂进去
            try:
                completed = subprocess.run(
                    cmd,
                    input=payload.encode("utf-8"),
                    check=False,
                    capture_output=True,
                )
                return completed.returncode
            except OSError as exc:  # pragma: no cover - 命令存在但执行失败
                logger.warning("剪贴板命令执行失败：%s", exc)
                return 1

    try:
        return runner(command, text) == 0
    except Exception as exc:  # noqa: BLE001 - 剪贴板失败不该让整个命令崩掉
        logger.warning("复制到剪贴板失败：%s", exc)
        return False

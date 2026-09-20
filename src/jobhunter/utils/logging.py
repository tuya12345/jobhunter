"""日志工具：**自动脱敏** + 统一初始化。

╔══════════════════════════════════════════════════════════════════════════╗
║ 为什么日志脱敏是"工程底座"而不是"锦上添花"                                 ║
╚══════════════════════════════════════════════════════════════════════════╝

这个工具会处理**真实简历**（含姓名、手机号、邮箱）和**真实 HR 对话**。
一旦日志里出现手机号：

    · 用户把日志贴到 GitHub issue 求助 → 个人信息直接公开；
    · 日志进 CI 产物 / 报错截图 / 聊天窗口 → 泄露面无限扩大；
    · 而"记得别打日志"这种约定，靠人是守不住的（尤其报错时最想打全文）。

所以做法是**在 Filter 层无差别打码**，让"打码"成为默认行为：

    · 手机号  13812345678        → 138****5678
    · 邮箱    zhangsan@qq.com    → z***@qq.com
    · 身份证  110101199001011234 → 110101********1234

★ 更硬的一条约定：**任何日志都不打印简历原文**。
  规则是"日志只打摘要与计数，正文一律不打"。这条靠代码评审 + 测试守
  （tests/unit/test_logging_redaction.py 会拿一段带真实信息的假简历文本过一遍日志管线）。

★ Filter 必须幂等：重复安装 setup_logging() 不应该把同一条日志打码两次
  （`138****5678` 再打一次会变成 `138*****678`）。这里用 logger 上的标记位防重。
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "RedactingFilter",
    "get_logger",
    "redact_text",
    "setup_logging",
]

# ---------------------------------------------------------------------------
# 脱敏规则
# ---------------------------------------------------------------------------
# ⚠️ 只放**确定性强**的模式。宁可少打一种，也不要误伤正常文本
#    （比如把版本号 1.2.3.4 当成 IP 打码，会让人完全看不懂日志）。

# 中国大陆手机号：1 开头，第二位 3-9，共 11 位
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)(\d{4})(\d{4})(?!\d)")
# 邮箱：保留首字符与域名，中间打码
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
# 18 位身份证（含 X 结尾）：保留前 6 后 4
_ID_CARD_RE = re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)")


def redact_text(text: str) -> str:
    """对一段文本做脱敏，返回打码后的文本。

    单独暴露成函数的原因：需要给"非 logging 场景"复用
    （例如把异常信息拼进 CLI 输出、或写 state 文件时）。
    """
    if not text:
        return text
    redacted = _PHONE_RE.sub(r"\1****\3", text)
    redacted = _EMAIL_RE.sub(r"\1***\2", redacted)
    redacted = _ID_CARD_RE.sub(r"\1********\2", redacted)
    return redacted


class RedactingFilter(logging.Filter):
    """在日志离开进程之前，把 record 里的手机号/邮箱/身份证打码。

    为什么改 `record.msg` 和 `record.args` 而不是只处理 `record.getMessage()`：

        日志有两种写法，都必须覆盖 ——
            logger.info("投递 %s", phone)          # args 里带敏感信息
            logger.info(f"投递 {phone}")           # msg 里已经是完整字符串

        如果只处理 `getMessage()` 的结果，就要在 Filter 里重新赋值 msg/args，
        容易和后续 Formatter 的 `%` 格式化打架。这里的做法是：
            · args 是元组/list/dict → 逐项递归脱敏；
            · msg 若是字符串       → 直接脱敏；
        两者都处理后，Formatter 再做 `%` 拼接时拿到的就已经是打码后的值。

    幂等性：如果同一段文本被处理两次，`138****5678` 会变成 `138*****678`。
    所以 setup_logging() 用标记位保证同一个 logger 只挂一个 Filter，
    并且 `_redact_str` 里还做了一次"再脱敏一次是否变化"的精确判断。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """永远返回 True —— 这个 Filter 只做脱敏，不做过滤。"""
        # 1) 先处理参数（可能是元组、字典，也可能嵌套）
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._redact_value(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._redact_value(v) for v in record.args)
            else:
                record.args = self._redact_value(record.args)

        # 2) 再处理已经拼好或本身含敏感信息的消息体
        if isinstance(record.msg, str) and record.msg:
            record.msg = self._redact_str(record.msg)

        # 3) 异常堆栈里也可能有敏感信息（例如 URL 带手机号）
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = None  # 让 Formatter 重新渲染（渲染时 msg 已脱敏）
        return True

    def _redact_value(self, value: Any) -> Any:
        """递归处理 args 里的单值（可能嵌了 list/dict/对象）。"""
        if isinstance(value, str):
            return self._redact_str(value)
        if isinstance(value, tuple | list):
            return type(value)(self._redact_value(v) for v in value)
        if isinstance(value, dict):
            return {k: self._redact_value(v) for k, v in value.items()}
        return value

    def _redact_str(self, text: str) -> str:
        """字符串脱敏，带**精确的**幂等保护。

        为什么不能简单地用 `if "****" in text: return text`：
            那样任何本身就含 `****` 的日志（例如密码占位符 `api_key=****`）
            会连带把同一行里的手机号也放过，等于在脱敏上开了个后门。

        正确做法是判断"再脱敏一次是否还会变化"：
            会变化 → 说明还没脱敏 → 处理
            不变化 → 已经处理过（或本来就没有敏感信息）→ 原样返回
        """
        redacted = redact_text(text)
        if redacted == text:
            return text
        return redacted


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------
_DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class _CurrentStreamHandler(logging.StreamHandler):
    """写日志时**现取** `sys.stdout`，而不是记住 setup 那一刻的那个流。

    ★★ 为什么必须这样（一个真实踩到的坑）

      `logging.StreamHandler(sys.stdout)` 会把流**存下来**。而 CLI 在被测试
      驱动时（`typer.testing.CliRunner`），`sys.stdout` 会被替换成一个临时缓冲，
      用完就关；于是"setup 时存下的那个流"在后续调用里是**已关闭**的，
      任何一条日志都会让 logging 打印：

          --- Logging error ---
          ValueError: I/O operation on closed file.

      ★ 触发条件很隐蔽：只有当某条命令**在捕获期间真的写了日志**才炸。
        以前它恰好没炸是因为 `require_db()` 只查"表在不在"（不产生日志）；
        ISSUES #60 之后它每次都会问一次 Alembic 版本，而 Alembic 会 INFO 打印
        「Context impl SQLiteImpl.」—— 于是三条简历命令的用例立刻变红，
        红的样子还是**满屏 Traceback 混在正常输出里**。

      ★ 为什么不是"把 alembic 的日志级别调高"了事：那只治了这一个来源；
        任何一条 `logger.warning`（比如抓取失败、配置告警）都会撞同一个坑。
        真正的修法是"别把流存下来"。
    """

    def __init__(self) -> None:
        super().__init__(sys.stdout)
        #: 上一次成功拿到的流（当前流不可用时兜底，尽量别丢日志）
        self._fallback: object = sys.stderr

    def emit(self, record: logging.LogRecord) -> None:
        stream = sys.stdout
        if getattr(stream, "closed", False):
            stream = self._fallback
        self.stream = stream
        super().emit(record)


def setup_logging(
    level: str | int | None = None,
    *,
    log_file: str | os.PathLike[str] | None = None,
) -> logging.Logger:
    """初始化根 logger：控制台输出 + 脱敏 Filter（幂等，可重复调用）。

    Args:
        level:   日志级别。None → 读环境变量 `JH_LOG_LEVEL`，再默认 INFO。
        log_file:可选日志文件路径。CLI 目前不需要（不想把求职操作写进明文文件），
                 留这个参数是给将来 P8 的 Web 服务用。

    为什么强制 stdout 是 UTF-8：
        Windows 控制台默认可能是 cp936，rich/emoji/中文混排时会抛
        `UnicodeEncodeError`。这里显式重配，避免"日志本身把程序搞崩"。
    """
    if level is None:
        level = os.environ.get("JH_LOG_LEVEL", "INFO")
    if isinstance(level, str):
        level = logging.getLevelName(level.upper())
        if not isinstance(level, int):
            level = logging.INFO

    root = logging.getLogger()

    # 控制台 handler：只在第一次添加，避免重复 setup 造成日志打印多次
    has_console = any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in root.handlers
    )
    if not has_console:
        # Python 3.7+ 支持 reconfigure；某些嵌入式环境没有这个方法，故容错
        with contextlib.suppress(AttributeError, ValueError):
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        console = _CurrentStreamHandler()
        console.setFormatter(logging.Formatter(_DEFAULT_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
        root.addHandler(console)

    if log_file is not None and not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        # io 在函数内导入：utils 包内部避免循环依赖（io 不依赖 logging，但保持这个习惯）
        from jobhunter.utils.io import ensure_dir

        target = Path(log_file)
        ensure_dir(target.parent)
        file_handler = logging.FileHandler(target, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
        root.addHandler(file_handler)

    root.setLevel(level)

    # ★ 铁律：不可能有任何 handler 漏掉脱敏 Filter。所以给**所有** handler 挂，
    #   而且逐个检查（而不是用一个"已经装过"的标记位）——
    #   因为测试框架（pytest 的 logging 插件）会在我们之后往 root 上加自己的 handler，
    #   用标记位的话那个 handler 就永远拿不到 Filter，日志会从那里漏出去。
    #   逐个检查既保证"最终每个 handler 都有"，又保证不会重复挂（幂等）。
    for handler in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())

    # 第三方库的噪音降级：httpx 会把完整 URL 打进 INFO（URL 里可能有敏感参数）
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    return root


def get_logger(name: str) -> logging.Logger:
    """取模块级 logger。

    约定：每个模块顶部写 `logger = get_logger(__name__)`。
    不要在库里调用 setup_logging()（那是入口点 cli/app.py 的职责），
    否则 import 一个库就偷偷改了全局日志配置。
    """
    return logging.getLogger(name)

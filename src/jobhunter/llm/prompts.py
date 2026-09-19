"""提示词文件（`prompts/*.md`）的加载与渲染 —— **只实现一次**。

╔══════════════════════════════════════════════════════════════════════════╗
║ 为什么提示词要放文件、而且版本号写在文件里                                  ║
╚══════════════════════════════════════════════════════════════════════════╝

    · 提示词是**要反复改**的东西（P4/P5 每一轮真实联调都在改它）。
      写在 Python 字符串里，改一次就要在 diff 里滚过一屏代码；
    · `llm_cache` 的 key 含 `prompt_version`，**改了正文不 +1 就等于没改**
      （缓存一直命中旧结果，现象是"我改了提示词但输出没变"）。
      把版本号放在文件第一行（`<!-- prompt-version: 3 -->`），
      再配一条"正文指纹"测试，就能把这个口头纪律变成一次红灯。

★ 渲染用 `string.Template`（`$var`）而不是 `str.format`（`{var}`）：
  提示词里必然有 JSON 示例（`{...}`），format 会把它们当占位符 ——
  要么 KeyError，要么满屏 `{{`。`$` 与 JSON 不冲突。

★ 文件位置：显式路径 > `settings.paths.root/prompts`（config.yaml 所在目录）>
  项目根（`find_project_root()`）。第三条是必需的：测试把 config 指向 tmp 目录，
  以及用户把 config.yaml 指到别处（备份/多份配置）时，提示词仍然在项目里。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from string import Template

from jobhunter.config.settings import AppSettings, get_settings
from jobhunter.utils.io import read_text

__all__ = [
    "PROMPT_DIR",
    "PromptFile",
    "PromptFileError",
    "body_sha256",
    "load_prompt_file",
    "prompt_path",
    "render",
]

#: 提示词目录（相对项目根）
PROMPT_DIR = Path("prompts")

#: 版本声明：`<!-- prompt-version: 3 -->`（第一行，必须存在）
_VERSION_RE = re.compile(r"<!--\s*prompt-version:\s*(?P<version>\d+)\s*-->")


class PromptFileError(RuntimeError):
    """提示词文件缺失/没有版本号。

    ★ 宁可明确报错，也不内置一份兜底提示词：那样文件丢了程序照样跑，
      但跑的是**旧提示词**，而且没人会发现（P5 的取舍，这里沿用）。
    """


@dataclass(frozen=True)
class PromptFile:
    """加载好的提示词。"""

    #: 文件名（不含目录），如 `tailor.md`
    name: str
    #: 文件里声明的版本号（进缓存 key）
    version: int
    #: 去掉版本注释后的正文
    body: str
    path: Path

    @property
    def sha256(self) -> str:
        """正文指纹（测试用它守住"改了正文必须 +1"）。"""
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()


def prompt_path(
    name: str, settings: AppSettings | None = None, *, path: Path | str | None = None
) -> Path:
    """定位提示词文件（三条候选，见模块 docstring）。"""
    if path is not None:
        return Path(path)
    settings = settings or get_settings()
    from jobhunter.config.paths import find_project_root

    candidates = (
        settings.paths.root / PROMPT_DIR / name,
        find_project_root() / PROMPT_DIR / name,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def load_prompt_file(
    name: str, settings: AppSettings | None = None, *, path: Path | str | None = None
) -> PromptFile:
    """读一个提示词文件并取出它声明的版本号。

    Raises:
        PromptFileError: 文件不存在，或没有 `<!-- prompt-version: N -->`。
    """
    target = prompt_path(name, settings, path=path)
    if not target.is_file():
        raise PromptFileError(
            f"找不到提示词文件：{target}\n"
            f"  · 正常情况下它应该在项目根的 prompts/{name}\n"
            f"  · 如果是把项目搬走了，用 --config 指定新的 config.yaml 位置即可"
        )
    raw = read_text(target)
    match = _VERSION_RE.search(raw)
    if match is None:
        raise PromptFileError(
            f"提示词文件里没有版本号：{target}\n  请在文件开头加一行：<!-- prompt-version: 1 -->"
        )
    body = _VERSION_RE.sub("", raw, count=1).strip()
    return PromptFile(name=name, version=int(match.group("version")), body=body, path=target)


def render(body: str, **values: object) -> str:
    """把正文里的 `$变量` 填成本次运行的实际参数（缺的变量原样保留）。"""
    return Template(body).safe_substitute(**values)


def body_sha256(prompt: PromptFile) -> str:
    """提示词正文的 sha256（测试用；单独暴露是为了测试读起来更直白）。"""
    return prompt.sha256

"""编码防线自查（ISSUE #7）：不许再出现"注释说有效、实际无效"的防护。

★ 背景（这条测试存在的原因）

    `src/jobhunter/utils/io.py` 的注释一直声称有"三层防护"，其中第二层是
    "ruff 规则 `PLW1514`（在 pyproject 里开启）"。实际上 `PLW1514` 是 ruff 的
    **preview** 规则 —— 不显式 `--preview` 时，把它写进 `select` 也**一句都不会报**。
    也就是说这层防护从来没生效过，而注释让人以为它在守。

    修法（见 `pyproject.toml` 的 `[tool.ruff.lint]` 上方那段说明）：
    不开全局 preview（会引入 9 条无关规则、实测 65 处误报），
    改为 CI 里单跑一条 `ruff check --preview --select PLW1514 src/ tests/`，
    并由本文件把这个约定**钉住**。

★ 这里做两件事（互为冗余，缺一不可）

    ① 用 AST **自己**扫一遍 `src/`：任何 `open()` / `os.fdopen()` /
       `Path.read_text|write_text|open()` 调用都必须带 `encoding=`。
       不依赖 ruff 的版本与配置 —— 换句话说，"关掉 ruff 也依然有人守"。
    ② 断言 CI 里那条 `--preview --select PLW1514` 步骤还在。
       因为它才是**全仓**（含 tests/）的那道防线，而它是唯一一道：
       删掉它不会有任何别的测试变红 —— 除非本文件盯着它。

★ 为什么只扫 `src/`（不扫全仓）

    `tests/` 下还有别的会话负责的文件（`tests/verify/**`）。把别人的文件纳入
    这条测试的失败面，会让"我这边红了但改不了"变成常态。全仓交给 CI 那条 ruff 命令。
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
CI_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"

#: 这些名字的调用一旦漏了 `encoding=`，在中文 Windows 上就会用 cp936 读写
#: （读 UTF-8 中文 → UnicodeDecodeError；写中文 → 别人机器上乱码）。
_ENCODING_SENSITIVE_METHODS = {"open", "read_text", "write_text", "fdopen"}


def _iter_src_files() -> list[Path]:
    return sorted(path for path in SRC_ROOT.rglob("*.py") if path.is_file())


def _utf8_wrappers(tree: ast.AST) -> tuple[set[str], set[str]]:
    """找出"本项目自己的 UTF-8 包装器"在本文件里叫什么名字。

    ★ 为什么必须把它们排除掉：`jobhunter.utils.io.read_text/write_text` 内部已经写死
      `encoding="utf-8"`（这正是第一层防护）。`io.py:230` 调用自己的 `write_text(path, …)`
      时当然不会再传一次 encoding —— 那不是裸 open，把它算成违规就变成"扫描器在说谎"。
    """
    funcs: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("utils.io"):
            for alias in node.names:
                funcs.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith("utils.io"):
                    modules.add(alias.asname or alias.name.split(".")[0])
    return funcs, modules


def _missing_encoding_calls(tree: ast.AST) -> list[int]:
    """返回"缺 encoding= 的文本 IO 调用"所在行号。"""
    wrappers, wrapper_modules = _utf8_wrappers(tree)
    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            # 内建 open()；本项目的 read_text/write_text 包装器则要排除
            if func.id != "open" or func.id in wrappers:
                continue
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
            if name not in _ENCODING_SENSITIVE_METHODS:
                continue
            # `io.read_text(...)`（模块别名）：同样是本项目的包装器
            if isinstance(func.value, ast.Name) and func.value.id in wrapper_modules:
                continue
        else:
            continue
        if any(keyword.arg == "encoding" for keyword in node.keywords):
            continue
        # 位置参数里也可能塞了 encoding（open(p, "w", -1, "utf-8")）——
        # 本项目一处都不这么写；真出现就报出来，让人显式写关键字。
        offenders.append(node.lineno)
    return offenders


def test_src_never_opens_text_without_explicit_encoding() -> None:
    """★ 独立于 ruff 的那道防线：`src/` 里不许有"没说编码"的文本 IO。"""
    files = _iter_src_files()
    assert len(files) > 20, f"只扫到 {len(files)} 个文件 —— 路径写错了？扫描会变成假绿"

    offenders: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line in _missing_encoding_calls(tree):
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{line}")
    assert not offenders, (
        "这些地方打开文本却没有显式 encoding（中文 Windows 上会用 cp936，"
        f"读写中文直接炸或静默乱码）：{offenders}"
    )


def test_guard_test_itself_catches_a_bare_open() -> None:
    """元测试：上面那个扫描器必须真的能抓到裸 `open()`（否则它只是个永远绿的摆设）。"""
    sample = ast.parse('with open("x.txt") as fh:\n    fh.read()\n')
    assert _missing_encoding_calls(sample) == [1]

    ok = ast.parse('with open("x.txt", encoding="utf-8") as fh:\n    fh.read()\n')
    assert _missing_encoding_calls(ok) == []

    pathlib_call = ast.parse('Path("x").read_text()\n')
    assert _missing_encoding_calls(pathlib_call) == [1]

    # 反例：本项目自己的包装器不该被误报（它内部已经写死 utf-8）
    wrapper = ast.parse(
        "from jobhunter.utils.io import read_text, write_text\n"
        "write_text('a.txt', '内容')\n"
        "read_text('a.txt')\n"
    )
    assert _missing_encoding_calls(wrapper) == []


def test_ci_still_runs_the_preview_rule_that_guards_encoding() -> None:
    """CI 里那条 `--preview --select PLW1514` 必须还在（它是全仓唯一的那道防线）。"""
    assert CI_WORKFLOW.is_file(), f"CI 工作流不见了：{CI_WORKFLOW}"
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "PLW1514" in text, "CI 里没有跑 PLW1514 —— io.py 的注释会再次变成'说有效、实际无效'"
    assert "--preview" in text, (
        "CI 里跑了 PLW1514 却没带 --preview：preview 规则不显式开启时**一句都不会报**，"
        "看起来有这条步骤，实际等于没有（这正是 ISSUE #7 的形态）"
    )


def test_pyproject_does_not_pretend_to_enable_the_preview_rule() -> None:
    """★ 防复发：配置里不许留一个"看起来开着 PLW1514"的假象（ISSUE #7 的形态）。

    三件事一起查（都是结构性的，不靠读注释的语气）：

        ① `preview` 没被打开 —— 打开了就会一次性引入 9 条无关规则（65 处误报）；
        ② `select` 里**不出现** `PLW1514` —— 它在那里等于没有（preview 未开时不报），
           留着只会让下一个读配置的人以为"这层防护已经在守"；
        ③ 真话写在注释里：明确指出它由 `--preview --select PLW1514` 那条 CI 命令守。
    """
    text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lint = tomllib.loads(text)["tool"]["ruff"]["lint"]

    assert not lint.get("preview"), (
        "开了全局 preview：会引入 9 条与本项目无关的 preview 规则（实测 65 处），"
        "而且 preview 规则不稳定 → CI 会为与改动无关的原因变红"
    )
    assert "PLW1514" not in lint["select"], (
        "PLW1514 被写进了 select，但它是 preview 规则 —— 这样写**一句都不会报**，"
        "只会让人以为防护已经开着（这正是 ISSUE #7）"
    )
    assert "--preview --select PLW1514" in text, (
        "pyproject 里必须写清 PLW1514 真正由哪条命令守（否则下一个人又要重新踩一遍 #7）"
    )


def test_io_module_docstring_qualified_the_preview_rule() -> None:
    """`io.py` 那三层防护的说明必须点明 PLW1514 是 preview 规则、由 CI 单独守。"""
    text = (PROJECT_ROOT / "src" / "jobhunter" / "utils" / "io.py").read_text(encoding="utf-8")
    assert "PLW1514" in text and "preview" in text
    assert "--preview --select PLW1514" in text, "只说'由 CI 守'不够，要给出具体那条命令"

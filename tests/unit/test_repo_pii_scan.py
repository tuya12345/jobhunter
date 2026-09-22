"""全仓库隐私扫描（**受 git 跟踪的文件里不许出现真实身份标识**）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 为什么这个文件必须存在（只读代码审查 JH-01）                                ║
╚══════════════════════════════════════════════════════════════════════════╝

CI 里那条「隐私扫描」步骤原来指向 `tests/verify/test_v_p9.py` —— 那是**验证会话**
的文件，而且当时**没有被 git 跟踪**（`git status` 里是 `??`，`git ls-files` 无输出）。

后果很具体：从当前分支做一次干净 checkout，CI 里那一步会因为
"测试路径不存在"而**非零退出** —— 也就是说远端 CI 在红灯，而本地看着是绿的。
更糟的一种可能是有人"顺手把这一步删掉"：那隐私扫描就再也没有自动化保证了。

所以这里放一份**受跟踪**的实现（本文件在 `tests/unit/` 下，属于开发会话的边界）：

    · 扫描逻辑与验证会话那份**同口径**：样本从环境变量 `JH_PII_SAMPLES`
      或本机 `state/pii-samples.txt`（已 gitignore）取；
    · 没配样本时**跳过而不是通过**（`-rs` 会把它列出来，"没扫"永远看得见）；
    · 命中只报**位置**（文件 + 样本序号），不回显命中内容 —— 内容本身就是隐私；
    · 带一条**元测试**证明扫描器真的会响（一个从不触发的隐私扫描比没有更糟：
      它给出的结论是"扫过了，安全"）。

★ 为什么不 import 验证会话那份：那会让 CI 又依赖一个不在 `tests/unit/` 的文件。
  两份实现的口径由**各自的元测试**守住；真要合并，应该由验证会话把它的文件提交进
  仓库之后再合并（那时这条可以删）。
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest
import yaml

from jobhunter.config.paths import find_project_root

#: 文本里不该出现的二进制/大文件后缀（读进来也没有意义，还会拖慢扫描）
_SKIP_SUFFIXES = frozenset(
    {".db", ".db-wal", ".db-shm", ".pyc", ".docx", ".pdf", ".png", ".jpg", ".jpeg", ".ico", ".zip"}
)


def _load_pii_samples() -> list[str]:
    """取"已知真实标识"的检测样本 —— **从仓库外取，绝不写在本文件里**。

    ★ 检测器不能携带它要检测的秘密（ISSUES #54 的教训：把真名/手机号硬编码成
      断言样本，"查隐私"的那行代码本身就成了仓库里的一份隐私副本）。

    取法（按优先级）：
      ① 环境变量 `JH_PII_SAMPLES`：逗号分隔；
      ② 仓库内**已被 gitignore** 的本地文件 `state/pii-samples.txt`：一行一个
         （`#` 开头的行是注释）。
    两者都没有时返回空列表 → 调用方**跳过**（不是通过）。
    """
    raw = os.environ.get("JH_PII_SAMPLES", "")
    samples = [item.strip() for item in raw.split(",") if item.strip()]
    if samples:
        return samples

    local = find_project_root() / "state" / "pii-samples.txt"
    if local.is_file():
        return [
            line.strip()
            for line in local.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    return []


def _scan_paths_for_samples(
    root: Path, relatives: Iterable[str], samples: Sequence[str]
) -> list[str]:
    """纯函数：在给定的（相对）文件列表里找样本串。抽出来是为了能被元测试直接验证。

    ★ 返回的是 `文件：命中样本[序号]`，**不含命中内容**。
    """
    offenders: list[str] = []
    for relative in relatives:
        path = root / relative
        if path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        offenders.extend(
            f"{relative}：命中样本[{index}]"
            for index, sample in enumerate(samples)
            if sample in text
        )
    return offenders


def _git_tracked_files(root: Path) -> list[str]:
    """受 git 跟踪的文件列表（**只有这些才算"仓库里有隐私"**）。

    ★ 为什么不扫整个工作树：`.env`、`state/pii-samples.txt`、真实简历这些
      本来就是"本机文件 + 已 gitignore" —— 它们**应该**含真实信息，
      把它们算成泄露是误报（验证会话第一版扫工作树，就是这么误报的）。
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True,
            capture_output=True,
            # ★ 必须显式 utf-8：`git ls-files` 会输出**中文文件名**
            #   （`vendor/shotocr/docs/调用手册.md` 这类），而 Windows 上默认按 GBK 解码 →
            #   解码线程抛 UnicodeDecodeError → `stdout` 变成 None →
            #   两个用例都报 `AttributeError: 'NoneType' has no attribute 'split'`（实测踩过）。
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - 环境没有 git
        pytest.skip("环境里没有可用的 git（或这里不是 git 仓库）→ 无法列出受跟踪文件")
    return [name for name in (completed.stdout or "").split("\0") if name]


def test_working_tree_has_no_real_identifiers() -> None:
    """★★ 受跟踪的文件里不许出现"已知真实标识"样本。

    依据：`docs/COMPLIANCE.md` 的声明「工作区 0 命中真实标识」+ 【提示词 11】
    第 3 条「隐私检查（重点）：全仓库扫描有无硬编码密钥、真实手机号/邮箱/姓名」。
    """
    samples = _load_pii_samples()
    if not samples:
        pytest.skip(
            "未配置真实标识样本（跳过而非通过）："
            "设 JH_PII_SAMPLES='值1,值2' 或写 state/pii-samples.txt（该路径已 gitignore）"
        )

    root = find_project_root()
    offenders = _scan_paths_for_samples(root, _git_tracked_files(root), samples)
    assert not offenders, "受跟踪的文件里仍有真实身份标识（只报位置，不回显内容）：\n" + "\n".join(
        offenders
    )


def test_pii_scanner_is_actually_capable_of_firing(tmp_path: Path) -> None:
    """★★ **元测试**：证明扫描器真的会响，不是一个恒绿的空壳。

    双向断言（本项目已经出现过三次"防线看起来在、实际测不到"）：
      ① 文件里**有**样本 → 必须命中；
      ② 文件里**没有**样本 → 必须零命中（防止它变成"见谁报谁"的误报机器）。
    """
    synthetic = "合成样本甲"
    planted = tmp_path / "planted.txt"
    planted.write_text(f"这一行含 {synthetic} 这个词\n", encoding="utf-8")
    clean = tmp_path / "clean.txt"
    clean.write_text("这一行什么都不含\n", encoding="utf-8")

    hits = _scan_paths_for_samples(tmp_path, ["planted.txt"], [synthetic])
    assert hits, "扫描器漏掉了明确植入的样本 —— 它是死的，隐私检查等于没做"
    # ★ 命中信息里**不能**出现命中内容本身
    assert synthetic not in hits[0], "命中信息回显了隐私内容（应该只报位置与序号）"

    misses = _scan_paths_for_samples(tmp_path, ["clean.txt"], [synthetic])
    assert not misses, f"扫描器对干净文件也报命中（误报机器）：{misses}"

    # 二进制/大文件后缀要被跳过（否则一次 .db 扫描能读出大量文本，噪音与耗时都失控）
    binary = tmp_path / "sample.db"
    binary.write_text(f"里面也有 {synthetic}\n", encoding="utf-8")
    assert _scan_paths_for_samples(tmp_path, ["sample.db"], [synthetic]) == []


def test_ci_step_references_a_tracked_privacy_test() -> None:
    """★★ **结构性守卫**（JH-01 的正题）：CI 真正**执行的**测试文件必须受 git 跟踪。

    ★ 为什么要有这条：这次的缺陷不是"隐私扫描写错了"，而是"CI 指向了一个
      干净 checkout 里不存在的文件" —— 前者靠用例内容守，后者只能靠
      "把 CI 里出现的测试路径拿去问 git"来守。
      （本项目反复学到的：守卫只覆盖一个方向等于没守。）

    ★ 为什么解析 YAML 的 `run:`（而不是全文正则）：注释里提到某个路径
      （比如"这里原来指向 X"）不是依赖关系，只有真正会被执行的命令才算 ——
      把注释也算进来的守卫会逼着人不敢写解释，那是本末倒置。
    """
    root = find_project_root()
    workflow = root / ".github" / "workflows" / "ci.yml"
    if not workflow.is_file():  # pragma: no cover - 打包分发时没有 CI 文件
        pytest.skip("这个 checkout 里没有 .github/workflows/ci.yml")

    parsed = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    commands = [
        step.get("run") or ""
        for job in (parsed.get("jobs") or {}).values()
        for step in (job.get("steps") or [])
    ]
    referenced = set(re.findall(r"tests/[A-Za-z0-9_./\-]+\.py", "\n".join(commands)))
    assert referenced, "CI 里没有引用任何 tests/ 路径 —— 这条守卫会变成空壳（请检查解析）"

    tracked = set(_git_tracked_files(root))
    missing = sorted(path for path in referenced if path not in tracked)
    assert not missing, (
        "CI 引用了**未被 git 跟踪**的测试文件（干净 checkout 里会直接失败）：\n  "
        + "\n  ".join(missing)
        + "\n请先把这些文件提交进仓库，或把 CI 改成只引用受跟踪的文件。"
    )

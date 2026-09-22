"""截图 OCR：把"一张岗位截图"变成文字，交给现有的文本解析路径（2026-09-21 接入）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 这个模块存在的理由（#67 优先级 6）                                         ║
╚══════════════════════════════════════════════════════════════════════════╝

`jh job add` 原有四条路：给链接抓 / `--html` 本地网页 / `--manual` 粘贴 / `--file` 批量。
它们都要求**文字已经在那里**。而真实工作流里最常见的起点其实是**一张截图**：
BOSS 的页面会自关、会要登录、会插水印，截图反而是最稳的一份"证据"。

所以这里接上 shotocr（`vendor/shotocr`，本地离线、不联网、不上传图片）：
    `jh job add --image 截图.png --url "<原链接>"` → OCR → 走 `parse_detail_from_text`。

★★ 三条硬约束（决定了这个模块的形状）

  1. **不做硬依赖**：shotocr 的依赖约 250 MB（RapidOCR + opencv，其中 opencv 113 MB）。
     所以这里是**子进程调用**，jobhunter 的 `pyproject.toml` 里**没有**它 ——
     `uv sync` 不装 shotocr 时，其余功能照常可用，只是 `--image` 会给出可操作提示。
  2. **不猜**：找不到 shotocr 就是找不到，给出"三条怎么装/怎么配"的路，绝不去
     `pip install` 一个东西、也绝不静默降级成"没识别出内容"。
  3. **原文不改**：OCR 出来的文本原样交给解析层（`parse_detail_from_text`），
     这里只负责"跑命令 + 取文本"，不做任何清洗 —— 清洗的口径只有解析层那一份
     （两处各清一次，迟早不一致）。

★ 调用契约（只认 shotocr 自己的 CLI）：`--file <图片> --txt <临时文件> --json <临时文件> --quiet`。
  用 `--txt` 取文本而不是读 stdout：stdout 里混着摘要/提示，而**文本文件只有一个用途**。
  `--json` 顺带拿平均置信度 —— 太低时 CLI 会提醒"这段可能认错了字"（如实，而不是假装准）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from jobhunter.config.settings import AppSettings, get_settings
from jobhunter.utils.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "OcrError",
    "OcrResult",
    "OcrUnavailableError",
    "find_shotocr",
    "read_image",
]

#: 项目根下的默认位置（`git ls-files vendor` 里就有它，随仓库一起走）
_VENDOR_RELATIVE = Path("vendor") / "shotocr"
#: venv 里 python 的位置（Windows / POSIX 各一份）
_VENV_PYTHONS = (
    Path(".venv") / "Scripts" / "python.exe",
    Path(".venv") / "bin" / "python",
)
#: 低于这个平均置信度就提醒用户"这段可能认错了字"（不阻断，只是如实说）
_LOW_CONFIDENCE = 0.75


class OcrUnavailableError(RuntimeError):
    """找不到可用的 shotocr —— 消息一律是**中文可操作提示**。"""


class OcrError(RuntimeError):
    """OCR 跑起来了但没拿到文本（进程失败 / 超时 / 图片读不了）。"""


@dataclass(frozen=True)
class OcrResult:
    """一次识别的结果。"""

    text: str
    #: 用了哪个 shotocr（诊断用，例如 `vendor/shotocr`）
    source: str
    #: 平均置信度（拿不到就是 None —— 不编一个数出来）
    mean_score: float | None = None
    #: 识别引擎（rapid / paddle）
    engine: str | None = None
    #: 原始 JSON（`--json` 的内容，拿不到就是 None）
    raw: dict[str, object] | None = None

    @property
    def low_confidence(self) -> bool:
        """置信度偏低吗（拿不到置信度时**不算**低 —— 不猜）。"""
        return self.mean_score is not None and self.mean_score < _LOW_CONFIDENCE


def _vendored_root(settings: AppSettings) -> Path:
    """项目根下的 `vendor/shotocr`（用 `find_project_root` 而不是 cwd）。"""
    from jobhunter.config.paths import find_project_root

    configured = settings.ocr.root
    if configured is not None:
        return Path(configured)
    return find_project_root() / _VENDOR_RELATIVE


def find_shotocr(settings: AppSettings | None = None) -> tuple[list[str], Path | None] | None:
    """找一个可用的 shotocr 调用方式，返回 `(命令前缀, 工作目录)`。

    查找顺序（**先近后远**，每一步都有理由）：

      ① `ocr.root` 或 `vendor/shotocr` 里的 venv python + `src`：
         这是本仓库自带的那份，最可控，也是默认；
      ② `PATH` 上的 `shotocr`（用户自己装了入口脚本，例如 `shotocr.cmd`）；
      ③ 当前解释器 `python -m shotocr`（用户把 shotocr 装进了 jobhunter 的环境 ——
         不推荐（会让依赖变重），但既然装了就没理由不用）。

    返回 `None` = 三个都没找到（调用方给"怎么装"的提示，**不自己装**）。
    """
    settings = settings or get_settings()

    root = _vendored_root(settings)
    for relative in _VENV_PYTHONS:
        python = root / relative
        if not python.is_file():
            continue
        src = root / "src"
        if not (src / "shotocr").is_dir():
            continue
        return [str(python), "-m", "shotocr"], root

    found = shutil.which("shotocr")
    if found:
        return [found], None

    # 最后一条：用户自己装进了 jobhunter 的环境
    try:
        probe = subprocess.run(  # noqa: S603 - 解释器来自 sys.executable，无 shell
            [sys.executable, "-c", "import shotocr"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - 解释器异常
        probe = None
    if probe is not None and probe.returncode == 0:
        return [sys.executable, "-m", "shotocr"], None

    return None


def _unavailable_message(settings: AppSettings) -> str:
    root = _vendored_root(settings)
    return (
        "找不到 shotocr（截图 OCR 工具），`--image` 用不了。\n"
        f"  期望位置：{root}（含 src/shotocr 与 .venv）\n"
        "  怎么装（三选一）：\n"
        "    · 用仓库自带那份：把 shotocr 放到上面的期望位置，然后\n"
        f'        uv venv "{root / ".venv"}"\n'
        f'        uv pip install --python "{root / _VENV_PYTHONS[0]}" '
        f'-r "{root / "requirements.txt"}"\n'
        "    · 或者把它装在别处，然后在 config.yaml 里指路径：\n"
        "        ocr:\n"
        "          root: D:/tools/shotocr\n"
        "    · 或者不用 OCR：`jh job add --manual` 手动粘贴、`--html` 用另存的网页。"
    )


def read_image(
    image: str | Path,
    *,
    settings: AppSettings | None = None,
    command: list[str] | None = None,
) -> OcrResult:
    """识别一张图片里的文字（**子进程调 shotocr，不做硬依赖**）。

    Args:
        image: 图片路径（支持中文路径 —— shotocr 自己处理，我们只传字符串）。
        command: 显式指定调用命令（测试注入用）；不传就自动找。

    Raises:
        OcrUnavailableError: 找不到 shotocr。
        OcrError: 图片不存在 / 进程失败 / 超时 / 没拿到文本。
    """
    settings = settings or get_settings()
    target = Path(image)
    if not target.is_file():
        raise OcrError(
            f"找不到图片：{target}\n"
            "  · `--image` 要的是**图片文件**（png/jpg/jpeg/bmp/webp），不是网页或目录"
        )

    if command is None:
        found = find_shotocr(settings)
        if found is None:
            raise OcrUnavailableError(_unavailable_message(settings))
        prefix, workdir = found
    else:
        prefix, workdir = command, None

    with tempfile.TemporaryDirectory(prefix="jh-ocr-") as tmp:
        txt_path = Path(tmp) / "text.txt"
        json_path = Path(tmp) / "result.json"
        # ★★ 传给子进程的图片路径**必须绝对化**（2026-09-22 修的真实缺陷）
        #
        #   下面 `subprocess.run(..., cwd=workdir)` 会把子进程的工作目录换成 shotocr
        #   的根目录（vendored 布局下 `find_shotocr` 返回的 `workdir` 非 None）。
        #   而原来的写法把**用户原样给的字符串**交给 `--file` —— 相对路径于是被
        #   shotocr 按**新的 cwd** 解析，指向不存在的地方：
        #
        #       $ jh job add --image 截图.png          # 文件就在当前目录
        #       OCR 失败（退出码 2）：截图.png
        #       shotocr 说：[错误] 图片不存在：截图.png   ← 用户会以为工具坏了
        #
        #   问题的本体是**两个基准**：存在性检查用的是父进程的 cwd（所以"文件在"），
        #   子进程用的是 `workdir`（所以"图片不存在"）。绝对化之后只剩一个基准。
        #   ★ 文档教的写法正是相对路径（`job_cmd.py` 的 help、`docs/install.md` §10），
        #     所以这曾经是"照文档抄一遍就失败"。
        #   ★ 不改 `cwd` 而只加绝对路径：`cwd` 是给 `python -m shotocr` 用的
        #     （shotocr 自己可能有依赖 cwd 的相对路径），动它风险更大。
        image_arg = str(target.resolve())
        argv = [
            *prefix,
            "--file",
            image_arg,
            "--txt",
            str(txt_path),
            "--json",
            str(json_path),
            # ★ `--quiet` 只压掉 stdout 的正文（我们要的是文件），
            #   报错仍然走 stderr —— 失败时才有话可说。
            "--quiet",
        ]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        if workdir is not None:
            # vendor 那份是 src 布局：`python -m shotocr` 需要能找到包
            env["PYTHONPATH"] = os.pathsep.join(
                [str(workdir / "src"), env.get("PYTHONPATH", "")]
            ).rstrip(os.pathsep)

        try:
            completed = subprocess.run(  # noqa: S603 - 命令来自探测/配置，无 shell
                argv,
                cwd=str(workdir) if workdir is not None else None,
                env=env,
                capture_output=True,
                timeout=settings.ocr.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OcrError(
                f"OCR 超时（超过 {settings.ocr.timeout_seconds} 秒）：{target}\n"
                "  · 第一次跑要加载模型，慢是正常的；可以在 config.yaml 里调大\n"
                "    `ocr.timeout_seconds`，或换一张小一点的截图"
            ) from exc
        except OSError as exc:  # pragma: no cover - 命令不可执行
            raise OcrError(f"启动 OCR 失败：{exc}") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or b"").decode("utf-8", "replace").strip()
            raise OcrError(
                f"OCR 失败（退出码 {completed.returncode}）：{target}\n"
                + (f"  shotocr 说：{detail[-800:]}\n" if detail else "")
                + "  · 确认这是一张能看清文字的图片；失败重跑一次也可能是模型加载问题"
            )

        text = ""
        if txt_path.is_file():
            text = txt_path.read_text(encoding="utf-8").strip()
        if not text:
            raise OcrError(
                f"OCR 跑完了，但没识别出任何文字：{target}\n"
                "  · 图片可能是空白的 / 太模糊 / 尺寸异常\n"
                "  · 想确认识别效果：`--image` 之前先单独跑一次 shotocr 看它的输出"
            )

        payload: dict[str, object] | None = None
        if json_path.is_file():
            try:
                loaded = json.loads(json_path.read_text(encoding="utf-8"))
                payload = loaded if isinstance(loaded, dict) else None
            except (OSError, ValueError):  # pragma: no cover - json 坏了不该让识别白跑
                payload = None

    mean_score = _float_or_none((payload or {}).get("mean_score"))
    engine = (payload or {}).get("engine")
    return OcrResult(
        text=text,
        source=str(workdir or prefix[0]),
        mean_score=mean_score,
        engine=engine if isinstance(engine, str) else None,
        raw=payload,
    )


def _float_or_none(value: object) -> float | None:
    """把 JSON 里的数字取成 float（不是数字就 None —— 不编）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None

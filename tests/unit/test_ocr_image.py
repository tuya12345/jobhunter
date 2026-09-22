"""`jh job add --image`（截图 OCR，2026-09-21 接入 shotocr —— #67 优先级 6）。

★ 这组用例守三件事（每一件都是"不做就会变成静默失真"的）：

  1. **不做硬依赖**：shotocr 的依赖约 250 MB（RapidOCR + opencv），
     它**不许**进 `pyproject.toml`、源码里也不许 `import shotocr` ——
     否则 `uv sync` 会把一个大多数人用不到的重量级依赖装给所有人。
     调用方式是**子进程**（`adapters/ocr.py`）。
  2. **找不到就说清楚**：给出"装 / 配路径 / 换路子"三条出路，而不是一句
     "识别失败"（那会让人以为图有问题，其实是工具没装）。
  3. **识别结果如实标注**：来源是 `ocr`（不是 `manual`）、置信度偏低要提醒 ——
     OCR 会认错字，而认错的字会一路进 match / tailor。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobhunter.adapters import ocr as ocr_module
from jobhunter.cli.app import app

runner = CliRunner()

#: 假 shotocr：只实现我们真正调用的那个协议（`--file/--txt/--json/--quiet`）。
#: ★ 用**真的子进程**而不是 monkeypatch `subprocess.run`：
#:   这样"参数怎么拼、编码怎么传、退出码怎么读"都真的跑过一遍。
_FAKE_SHOTOCR = """
import argparse, json, pathlib, sys

parser = argparse.ArgumentParser()
parser.add_argument("--file")
parser.add_argument("--txt")
parser.add_argument("--json")
parser.add_argument("--quiet", action="store_true")
args = parser.parse_args()

if args.file == "trigger-failure":
    sys.exit(3)

# ★★ 真实 shotocr **会先检查图片存不存在**再识别（`[错误] 图片不存在`，退出码 2）。
#   这里必须照样模拟 —— 否则"相对路径被换过的 cwd 带偏"这类缺陷
#   在假实现上永远看不出来（假实现不读图，传什么都能"成功"）。
#   见 `test_relative_image_path_survives_a_changed_workdir`。
if not pathlib.Path(args.file).is_file():
    print(f"[错误] 图片不存在：{args.file}", file=sys.stderr)
    sys.exit(2)

text = "职位描述\\n公司：星澜科技\\n职位：Python 后端开发\\n深圳 15-25K\\n负责后端接口开发。"
pathlib.Path(args.txt).write_text(text, encoding="utf-8")
pathlib.Path(args.json).write_text(
    json.dumps({"mean_score": 0.93, "engine": "rapid"}, ensure_ascii=False), encoding="utf-8"
)
"""


@pytest.fixture
def fake_shotocr(tmp_path: Path) -> list[str]:
    script = tmp_path / "fake_shotocr.py"
    script.write_text(_FAKE_SHOTOCR, encoding="utf-8")
    return [sys.executable, str(script)]


@pytest.fixture
def image(tmp_path: Path) -> Path:
    target = tmp_path / "shot.png"
    # 内容无所谓（假 shotocr 不看图），但**文件必须真的存在** —— `read_image`
    # 的第一道检查就是"图片在不在"。
    target.write_bytes(b"\x89PNG\r\n\x1a\n(fake image)")
    return target


# ===========================================================================
# 查找：先近后远，找不到要说清怎么装
# ===========================================================================
def test_finds_the_vendored_layout(tmp_path: Path, settings) -> None:
    """vendored 布局（`<root>/src/shotocr` + `<root>/.venv`）要能被认出来。"""
    root = tmp_path / "shotocr"
    (root / "src" / "shotocr").mkdir(parents=True)
    python = root / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")

    patched = settings.model_copy(update={"ocr": settings.ocr.model_copy(update={"root": root})})
    found = ocr_module.find_shotocr(patched)

    assert found is not None
    command, workdir = found
    assert command == [str(python), "-m", "shotocr"]
    assert workdir == root


def test_returns_none_when_nothing_is_installed(tmp_path: Path, settings, monkeypatch) -> None:
    patched = settings.model_copy(
        update={"ocr": settings.ocr.model_copy(update={"root": tmp_path / "nowhere"})}
    )
    monkeypatch.setattr(ocr_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(ocr_module.sys, "executable", str(tmp_path / "no-python"))

    assert ocr_module.find_shotocr(patched) is None


def test_unavailable_message_gives_three_ways_out(tmp_path: Path, settings) -> None:
    """★ 找不到 shotocr 时，消息里必须有**三条可操作出路**（不是"识别失败"）。"""
    patched = settings.model_copy(
        update={"ocr": settings.ocr.model_copy(update={"root": tmp_path / "nowhere"})}
    )
    image = tmp_path / "x.png"
    image.write_bytes(b"fake")  # 文件要存在：否则先报的是"找不到图片"

    with pytest.raises(ocr_module.OcrUnavailableError) as excinfo:
        ocr_module.read_image(image, settings=patched)

    message = str(excinfo.value)
    assert "找不到 shotocr" in message
    assert "uv venv" in message  # ① 怎么装
    assert "ocr:" in message and "root:" in message  # ② 怎么配路径
    assert "--manual" in message  # ③ 不用 OCR 的替代路径


# ===========================================================================
# 识别：文本、置信度、失败都如实返回
# ===========================================================================
def test_read_image_returns_text_and_score(image: Path, settings, fake_shotocr) -> None:
    result = ocr_module.read_image(image, settings=settings, command=fake_shotocr)

    assert "星澜科技" in result.text
    assert result.mean_score == pytest.approx(0.93)
    assert result.engine == "rapid"
    assert result.low_confidence is False
    assert result.source  # 用了哪个 shotocr（诊断用）


def test_relative_image_path_survives_a_changed_workdir(
    tmp_path: Path, settings, fake_shotocr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★★ 相对图片路径 + shotocr **有工作目录** → 仍然必须找得到图片。

    ★ 这条守的是一个**真实缺陷**（2026-09-22 复核发现，用户明确授权修复）：

      `read_image` 会把子进程的 `cwd` 换成 shotocr 的根目录（vendored 布局下
      `find_shotocr` 返回的 `workdir` 非 None），但传给 `--file` 的却是**原样的
      字符串** —— 相对路径于是被 shotocr 按**新的 cwd** 解析，指向不存在的地方。

      症状：`jh job add --image 截图.png` 报「图片不存在：截图.png」，
      而那个文件就在用户眼前。**文档教的正是这种写法**（`job_cmd.py` 的 help 与
      `docs/install.md` §10 都是 `--image 截图.png`）。

    ★ 为什么原来那些用例抓不到：它们要么传 `command=`（→ `workdir=None`，不换 cwd），
      要么 monkeypatch `find_shotocr` 返回 `(cmd, None)`；而 `image` fixture 是
      `tmp_path`（**绝对**路径）。**"非 None 的 workdir" 与 "绝对路径" 两条
      恰好一起把触发条件抹掉了。** 所以这条用例必须同时钉住这两点：
      显式给一个非 None 的 `workdir`，并显式 `chdir` 之后用**相对**路径。
    """
    root = tmp_path / "shotocr-root"
    root.mkdir()
    # ★ 只替换 `find_shotocr`（让它返回非 None 的 workdir），
    #   但照旧用**真的子进程**跑假 shotocr —— 这样 cwd 是真的被换过。
    monkeypatch.setattr(ocr_module, "find_shotocr", lambda _settings=None: (fake_shotocr, root))

    user_cwd = tmp_path / "user-cwd"
    user_cwd.mkdir()
    monkeypatch.chdir(user_cwd)
    # 中文文件名一起验掉（Windows 上的编码坑本项目踩过多次）
    relative = "截图.png"
    (user_cwd / relative).write_bytes(b"\x89PNG\r\n\x1a\n(fake image)")

    result = ocr_module.read_image(relative, settings=settings)

    assert "星澜科技" in result.text, "相对路径在 cwd 被换过之后没被正确解析"


def test_cli_accepts_a_relative_image_path(
    tmp_path: Path, settings, fake_shotocr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 用户视角的那一条：`jh job add --image 截图.png`（**相对**路径）必须能用。

    `read_image` 的纯函数用例守机制，这条守"CLI 真的把用户的相对路径走通了" ——
    因为文档教给用户的就是这个写法，而它曾经必然失败。
    """
    root = tmp_path / "shotocr-root"
    root.mkdir()
    monkeypatch.setattr(ocr_module, "find_shotocr", lambda _settings=None: (fake_shotocr, root))

    user_cwd = tmp_path / "user-cwd"
    user_cwd.mkdir()
    monkeypatch.chdir(user_cwd)
    (user_cwd / "截图.png").write_bytes(b"\x89PNG\r\n\x1a\n(fake image)")

    result = runner.invoke(app, ["--no-remind", "job", "add", "--image", "截图.png", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "星澜科技" in result.output, "相对路径的截图没有被识别/解析出来"


def test_low_confidence_is_flagged(tmp_path: Path, settings) -> None:
    script = tmp_path / "low.py"
    script.write_text(
        "import argparse, json, pathlib\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--file'); p.add_argument('--txt'); p.add_argument('--json')\n"
        "p.add_argument('--quiet', action='store_true')\n"
        "a = p.parse_args()\n"
        "pathlib.Path(a.txt).write_text('识别得不太行', encoding='utf-8')\n"
        "pathlib.Path(a.json).write_text(json.dumps({'mean_score': 0.42}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    target = tmp_path / "blur.png"
    target.write_bytes(b"x")

    result = ocr_module.read_image(target, settings=settings, command=[sys.executable, str(script)])

    assert result.low_confidence is True, "置信度 0.42 必须被标成偏低"


def test_missing_image_is_a_clear_error(tmp_path: Path, settings) -> None:
    with pytest.raises(ocr_module.OcrError) as excinfo:
        ocr_module.read_image(tmp_path / "nope.png", settings=settings)

    assert "找不到图片" in str(excinfo.value)


def test_failing_command_reports_exit_code(tmp_path: Path, settings) -> None:
    target = tmp_path / "trigger-failure"
    target.write_bytes(b"x")
    script = tmp_path / "fail.py"
    script.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")

    with pytest.raises(ocr_module.OcrError) as excinfo:
        ocr_module.read_image(target, settings=settings, command=[sys.executable, str(script)])

    assert "退出码 3" in str(excinfo.value)


def test_empty_result_is_an_error_not_an_empty_job(tmp_path: Path, settings) -> None:
    """★ 识别不出文字时**不许**当成"一条空 JD"入库（那是假成功）。"""
    script = tmp_path / "empty.py"
    script.write_text(
        "import argparse, pathlib\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--file'); p.add_argument('--txt'); p.add_argument('--json')\n"
        "p.add_argument('--quiet', action='store_true')\n"
        "a = p.parse_args()\n"
        "pathlib.Path(a.txt).write_text('   ', encoding='utf-8')\n",
        encoding="utf-8",
    )
    target = tmp_path / "blank.png"
    target.write_bytes(b"x")

    with pytest.raises(ocr_module.OcrError) as excinfo:
        ocr_module.read_image(target, settings=settings, command=[sys.executable, str(script)])

    assert "没识别出任何文字" in str(excinfo.value)


# ===========================================================================
# CLI：`jh job add --image`
# ===========================================================================
def test_cli_parses_an_image(
    image: Path, settings, fake_shotocr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--image` 走**同一条**文本解析路径：公司/职位都应当被解析出来。"""
    monkeypatch.setattr(ocr_module, "find_shotocr", lambda _settings=None: (fake_shotocr, None))

    result = runner.invoke(
        app,
        ["--no-remind", "job", "add", "--image", str(image), "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "星澜科技" in result.output, "识别出的公司名没有被解析出来"
    assert "Python 后端开发" in result.output
    assert "没有写入数据库" in result.output


def test_cli_says_which_engine_and_score(
    image: Path, settings, fake_shotocr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """页面上要能看见"识别了多少字、什么引擎、多可信"（而不是只有一张表）。"""
    monkeypatch.setattr(ocr_module, "find_shotocr", lambda _settings=None: (fake_shotocr, None))

    result = runner.invoke(app, ["--no-remind", "job", "add", "--image", str(image), "--dry-run"])

    assert "识别到" in result.output
    assert "rapid" in result.output
    assert "0.93" in result.output


def test_cli_rejects_image_with_manual(image: Path) -> None:
    """★ `--image` 与另外三种文本来源互斥（不许"挑一份听"，见 #61 那条纪律）。"""
    result = runner.invoke(
        app,
        ["--no-remind", "job", "add", "--image", str(image), "--text", "某段文本"],
    )

    assert result.exit_code == 1
    assert "不能与" in result.output


def test_cli_reports_missing_shotocr(
    image: Path, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没装 shotocr 时：**中文可操作提示 + 非 0 退出**，不许静默什么都不做。"""
    monkeypatch.setattr(ocr_module, "find_shotocr", lambda _settings=None: None)
    monkeypatch.setattr(ocr_module.shutil, "which", lambda name: None)

    result = runner.invoke(app, ["--no-remind", "job", "add", "--image", str(image), "--dry-run"])

    assert result.exit_code != 0
    assert "找不到 shotocr" in result.output


# ===========================================================================
# 结构性守卫：不做硬依赖
# ===========================================================================
def test_cli_marks_the_source_as_ocr(
    tmp_path: Path, settings, fake_shotocr, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★★ OCR 出来的 JD 必须在 `raw` 里标成 `ocr`，人粘贴的标成 `manual`。

    ★ 为什么值得一条（2026-09-22 复核发现的**守卫缺口**）：
      `raw["source"]` 是排查"这条 JD 为什么怪"的**第一条线索** ——
      OCR 可能把字符认错，人粘贴的不会。`ocr.py` 与 `job_cmd.py` 的注释
      都写明"来源要如实标注"，但**此前没有任何用例断言它**：
      将来重构 `_collect_details` 时静默退回 `manual`，不会有任何测试变红。

    ★ 为什么**两边都断言**（ocr / manual）：只断言 OCR 那一侧的话，
      "永远写 ocr"这种改法照样绿；两个方向都钉住才是活的守卫。
    """
    from jobhunter.cli.commands import job_cmd

    monkeypatch.setattr(ocr_module, "find_shotocr", lambda _settings=None: (fake_shotocr, None))
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n(fake image)")

    details, failures = job_cmd._collect_details(
        urls=None,
        file=None,
        manual=False,
        text=None,
        html_file=None,
        url=None,
        platform=None,
        image=image,
        settings=settings,
    )

    assert failures == []
    assert len(details) == 1
    assert details[0].raw["source"] == "ocr", f"OCR 的来源没标成 ocr：{details[0].raw}"
    assert details[0].raw["ocr"]["source"], "没有留下「用了哪个 shotocr」的诊断信息"

    pasted, pasted_failures = job_cmd._collect_details(
        urls=None,
        file=None,
        manual=False,
        text="职位描述\n负责 Python 后端开发。",
        html_file=None,
        url=None,
        platform=None,
        settings=settings,
    )

    assert pasted_failures == []
    assert pasted[0].raw["source"] == "manual", f"粘贴文本被标成了 {pasted[0].raw['source']}"
    assert "ocr" not in pasted[0].raw, "粘贴路径不该带上 ocr 的诊断键"


def test_shotocr_is_not_a_hard_dependency() -> None:
    """★★ `uv sync` 不装 shotocr 也必须能跑 jobhunter（#67 §四的验收项）。

    两道判据（缺一个都守不住"它其实已经变成硬依赖了"）：
      ① `pyproject.toml` 的**依赖列表**里没有 shotocr / rapidocr / opencv；
      ② `src/` 里没有 `import shotocr`（只能走子进程，见 `adapters/ocr.py`）。

    ★ 判据①必须**解析 TOML 只看依赖表**，不能拿整个文件做子串匹配 ——
      否则注释里提一句 `shotocr`（解释"为什么把它排除出 ruff"）就会误报。
      这个坑本项目记过一次（#56：按词面匹配的守卫会把"说明不做某事"当成"做了某事"）。
    """
    import tomllib

    from jobhunter.config.paths import find_project_root

    root = find_project_root()
    with (root / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)

    declared: list[str] = list(pyproject.get("project", {}).get("dependencies", []))
    for extra in (pyproject.get("project", {}).get("optional-dependencies", {}) or {}).values():
        declared.extend(extra)
    for group in (pyproject.get("dependency-groups", {}) or {}).values():
        declared.extend(item for item in group if isinstance(item, str))

    joined = " ".join(declared).lower()
    for forbidden in ("shotocr", "rapidocr", "opencv", "paddleocr"):
        assert forbidden not in joined, f"依赖表里出现了 {forbidden} —— 它不该是硬依赖"

    offenders: list[str] = []
    for path in (root / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import shotocr", "from shotocr")):
                offenders.append(f"{path.relative_to(root)}: {stripped}")
    assert not offenders, "源码里直接 import 了 shotocr（必须走子进程）：\n" + "\n".join(offenders)

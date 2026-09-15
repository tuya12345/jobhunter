"""`jh resume` 命令层测试（P1）。

★ 为什么 CLI 层也要测（而不是只测 service）

    service 测试证明"解析逻辑对"，但用户碰的是 CLI。命令层最容易坏在三处：
        ① **库没初始化**时抛 `no such table: resumes` —— 用户完全不知道怎么办；
        ② 参数默认值（不传路径时用 `.env` 里的默认简历）没接上；
        ③ `add` 的幂等语义没在命令层体现（重复导入却提示"已导入"）。

    这三条都在这里用真实的临时库 + 真实 docx 跑一遍。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobhunter.cli.app import app
from jobhunter.cli.common import EXIT_NOT_IMPLEMENTED
from tests._fixture_loader import load_fixture_module

pytest.importorskip("docx", reason="需要 [docx] extra：uv sync --extra docx")

runner = CliRunner()


@pytest.fixture
def resume_docx(tmp_path: Path) -> Path:
    """临时生成一份虚构简历（不碰 tests/fixtures 里的文件）。"""
    build_sample_resume = load_fixture_module("make_sample_resume").build_sample_resume

    target = tmp_path / "我的简历.docx"
    build_sample_resume(target)
    return target


@pytest.fixture
def initialized(isolated_env: Path) -> Path:
    """先跑 `jh init` 建库（`resume` 命令的前置条件）。"""
    result = runner.invoke(app, ["--no-remind", "init"])
    assert result.exit_code == 0, result.output
    return isolated_env


# ---------------------------------------------------------------------------
# 未初始化时的提示
# ---------------------------------------------------------------------------
def test_list_without_database_hints_to_run_init() -> None:
    """没建库时给"请先运行 jh init"，而不是 `no such table`。"""
    result = runner.invoke(app, ["--no-remind", "resume", "list"])
    assert result.exit_code == 1
    assert "jh init" in result.output
    assert "no such table" not in result.output


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------
def test_add_imports_resume(initialized: Path, resume_docx: Path) -> None:
    """`jh resume add <path>` 能导入并报告解析到的内容量。"""
    result = runner.invoke(app, ["--no-remind", "resume", "add", str(resume_docx)])
    assert result.exit_code == 0, result.output
    assert "已导入" in result.output
    assert "技能" in result.output
    # 第一份自动成为默认简历
    assert "默认简历" in result.output


def test_add_uses_default_resume_path(
    initialized: Path, resume_docx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 不传路径时用 `.env` 里的 `JH_DEFAULT_RESUME_PATH`。

    这是给"每天都用同一份简历"的真实工作流准备的：不该强迫用户每次输路径。
    """
    monkeypatch.setenv("JH_DEFAULT_RESUME_PATH", str(resume_docx))
    from jobhunter.config.settings import reset_settings

    reset_settings()

    result = runner.invoke(app, ["--no-remind", "resume", "add"])
    assert result.exit_code == 0, result.output
    assert "已导入" in result.output


def test_add_without_path_and_without_default_explains(
    initialized: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既没传路径、也没配默认值时，要说清两种用法（而不是抛异常）。"""
    monkeypatch.delenv("JH_DEFAULT_RESUME_PATH", raising=False)
    from jobhunter.config.settings import reset_settings

    reset_settings()

    result = runner.invoke(app, ["--no-remind", "resume", "add"])
    assert result.exit_code == EXIT_NOT_IMPLEMENTED
    assert "JH_DEFAULT_RESUME_PATH" in result.output


def test_add_is_idempotent(initialized: Path, resume_docx: Path) -> None:
    """★ 同一份内容重复导入：不新建记录，提示"库里已有"。"""
    first = runner.invoke(app, ["--no-remind", "resume", "add", str(resume_docx)])
    assert first.exit_code == 0
    second = runner.invoke(app, ["--no-remind", "resume", "add", str(resume_docx)])
    assert second.exit_code == 0
    assert "已有" in second.output

    listing = runner.invoke(app, ["--no-remind", "resume", "list"])
    # 表里只有一条记录：数一下 id=1 出现的次数（表头里没有数字）
    assert listing.output.count("★") == 1


def test_add_reports_parse_warnings(initialized: Path, tmp_path: Path) -> None:
    """解析不完整时给出**可操作**的提示（而不是假装成功）。"""
    target = tmp_path / "只有姓名.txt"
    target.write_text("林小满\n13800000000\n", encoding="utf-8")
    result = runner.invoke(app, ["--no-remind", "resume", "add", str(target)])
    assert result.exit_code == 0, result.output
    # 没有项目、没有技能 → 应该提醒用户检查小标题
    assert "项目" in result.output


def test_add_rejects_unsupported_format(initialized: Path, tmp_path: Path) -> None:
    """`.doc` 这类格式要给出"另存为 docx"的建议。"""
    target = tmp_path / "老简历.doc"
    target.write_bytes(b"\xd0\xcf\x11\xe0")
    result = runner.invoke(app, ["--no-remind", "resume", "add", str(target)])
    assert result.exit_code == 1
    assert "docx" in result.output


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------
def test_list_is_empty_before_import(initialized: Path) -> None:
    """还没导入时给出下一步动作，而不是一片空白。"""
    result = runner.invoke(app, ["--no-remind", "resume", "list"])
    assert result.exit_code == 0
    assert "还没有导入任何简历" in result.output
    assert "jh resume add" in result.output


def test_list_shows_imported_resume(initialized: Path, resume_docx: Path) -> None:
    """导入后能列出：方向、项目数、技能数、默认标记。"""
    runner.invoke(app, ["--no-remind", "resume", "add", str(resume_docx)])
    result = runner.invoke(app, ["--no-remind", "resume", "list"])
    assert result.exit_code == 0
    assert "★" in result.output  # 默认简历标记
    assert "简历列表" in result.output


# ---------------------------------------------------------------------------
# split
# ---------------------------------------------------------------------------
def test_split_gives_suggestions(initialized: Path, resume_docx: Path) -> None:
    """`jh resume split` 要给出多方向建议与缺口。"""
    runner.invoke(app, ["--no-remind", "resume", "add", str(resume_docx)])
    result = runner.invoke(app, ["--no-remind", "resume", "split", "1"])
    assert result.exit_code == 0, result.output
    assert "该前置的项目" in result.output
    # 至少给出两个方向（用户的真实需求就是"一份拆两个方向"）
    assert result.output.count("该前置的项目") >= 2


def test_split_defaults_to_default_resume(initialized: Path, resume_docx: Path) -> None:
    """不传 id 时用默认简历（并明确告诉用户用了哪一份）。"""
    runner.invoke(app, ["--no-remind", "resume", "add", str(resume_docx)])
    result = runner.invoke(app, ["--no-remind", "resume", "split"])
    assert result.exit_code == 0, result.output
    assert "未指定简历" in result.output


def test_split_with_explicit_direction(initialized: Path, resume_docx: Path) -> None:
    """`--direction` 可以指定要拆的方向（可重复）。"""
    runner.invoke(app, ["--no-remind", "resume", "add", str(resume_docx)])
    result = runner.invoke(
        app,
        ["--no-remind", "resume", "split", "1", "-d", "AI Agent", "-d", "前端开发"],
    )
    assert result.exit_code == 0, result.output
    assert "AI Agent" in result.output
    assert "前端开发" in result.output


def test_split_save_writes_markdown(initialized: Path, resume_docx: Path) -> None:
    """★ `--save` 把建议落成 Markdown（这份文件是要交给 WorkBuddy 当指令的）。

    所以它必须：① 真的写出来；② 是**新文件**（不覆盖任何东西）；③ 内容可读。
    """
    runner.invoke(app, ["--no-remind", "resume", "add", str(resume_docx)])
    result = runner.invoke(app, ["--no-remind", "resume", "split", "1", "--save"])
    assert result.exit_code == 0, result.output
    assert "建议已写入" in result.output

    from jobhunter.config.settings import get_settings

    output_dir = get_settings().paths.output_dir
    written = sorted(output_dir.glob("resume-split-*.md"))
    assert written, f"output 目录里没有生成建议文件：{list(output_dir.iterdir())}"

    text = written[0].read_text(encoding="utf-8")
    assert "拆分方向建议" in text
    assert "该前置的项目" in text
    # 必须带上"不许编造"的约束，因为这份文件会直接当改写指令用
    assert "不得新增" in text or "不允许新增" in text


def test_split_does_not_touch_the_resume_file(initialized: Path, resume_docx: Path) -> None:
    """★ 产品纪律：`split` 绝不改动简历文件（P1 只给建议）。

    用哈希前后比对来守 —— 这也是 P5 导出简历时"原文件 hash 不变"那条测试的雏形。
    """
    import hashlib

    before = hashlib.sha256(resume_docx.read_bytes()).hexdigest()
    runner.invoke(app, ["--no-remind", "resume", "add", str(resume_docx)])
    runner.invoke(app, ["--no-remind", "resume", "split", "1", "--save"])
    after = hashlib.sha256(resume_docx.read_bytes()).hexdigest()
    assert before == after, "split 改了简历文件！"


def test_split_unknown_id_gives_actionable_error(initialized: Path) -> None:
    """不存在的 id 要提示去看 `jh resume list`。"""
    result = runner.invoke(app, ["--no-remind", "resume", "split", "999"])
    assert result.exit_code == 1
    assert "jh resume list" in result.output

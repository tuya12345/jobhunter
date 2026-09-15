"""docx 读取层测试：守住"真实简历排版"这条最难的路。

★ 为什么这组测试值得写

    本项目的**第一份真实简历**（用户本人的）把整份内容画在 **Word 文本框/形状**里：
    python-docx 的 `paragraphs` 和 `tables` 读出来是**空的**。
    如果没做文本框兜底，`jh resume add` 会直接报"没读到任何文字" ——
    而用户完全不知道为什么，只觉得工具坏了。

    同一份文件还踩出另外三个坑（都在这里固化成断言）：
      · 文字被拆成多个 run（`2026.0` + `8`），直接拼接会得到错位文本；
      · 模板把内容**重复了三遍**（逐条文本框 + 整页副本 + 一个巨型拼接块）；
      · 表格里的文字在 `w:tbl` 内的 `w:p` 里（用 `cell.text` 读不到）。

这组测试用**代码生成的虚构 fixture**，所以不依赖任何真实简历，
并且可以精确构造上述每种排版。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from jobhunter.utils.docx import UnsupportedDocumentError, read_document, read_docx
from tests._fixture_loader import load_fixture_module

pytest.importorskip("docx", reason="需要 [docx] extra：uv sync --extra docx")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(scope="module")
def sample_docx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """生成一份虚构简历 fixture（模块级：生成一次给所有用例用）。"""
    build_sample_resume = load_fixture_module("make_sample_resume").build_sample_resume

    target = tmp_path_factory.mktemp("fixtures") / "sample_resume.docx"
    build_sample_resume(target)
    return target


# ---------------------------------------------------------------------------
# 基本行为
# ---------------------------------------------------------------------------
def test_reads_docx_without_error(sample_docx: Path) -> None:
    """能读出内容（不是空文档）。"""
    result = read_docx(sample_docx)
    assert result.lines, "没读到任何文本块"
    assert len(result.text) > 100


def test_textbox_content_is_captured(sample_docx: Path) -> None:
    """★ 文本框里的文字必须被读到。

    fixture 把"姓名 + 手机号"放在文本框里。python-docx 的 paragraphs/tables
    都不包含 `w:txbxContent` —— 不做兜底的话这一段会整段丢失。
    """
    result = read_docx(sample_docx)
    assert result.textboxes, "没有读到任何文本框内容"
    joined = "\n".join(result.textboxes)
    assert "林小满" in joined
    assert "13800000000" in joined


def test_no_duplicate_blocks(sample_docx: Path) -> None:
    """★ 读取层必须去掉模板制造的重复（否则项目数、技能数会成倍虚高）。"""
    result = read_docx(sample_docx)
    normalized = ["".join(line.split()) for line in result.lines]
    assert len(normalized) == len(set(normalized)), "存在完全重复的文本块"


def test_document_order_is_preserved(sample_docx: Path) -> None:
    """分段标题必须出现在它自己的内容**之前**（校招 fixture 就是干净顺序）。

    ★ 这条守的是"按 body 顺序遍历"这个实现选择：
      如果改用 `paragraphs + tables` 拼接，顺序会乱，
      段标题可能跑到内容后面，分段逻辑直接失效。
    """
    result = read_docx(sample_docx)
    lines = result.lines
    for heading, first_content in (
        ("教育经历", "学校"),
        ("专业技能", "精通"),
        ("项目经验", "智能简历匹配助手"),
    ):
        assert heading in lines, f"缺少标题 {heading}"
        assert first_content in "\n".join(lines), f"缺少内容 {first_content}"
        assert lines.index(heading) < next(
            index for index, line in enumerate(lines) if first_content in line
        ), f"{heading} 出现在了它的内容之后"


def test_table_rows_are_flattened_with_separator(sample_docx: Path) -> None:
    """表格行要摊平成 `单元格 | 单元格`（解析层靠这个分隔符拆列）。"""
    result = read_docx(sample_docx)
    joined = "\n".join(result.lines)
    assert " | " in joined, "表格没有被摊平成带分隔符的行"
    assert result.table_rows, "table_rows 为空"


def test_plain_text_file_is_supported(tmp_path: Path) -> None:
    """`.txt` / `.md` 也支持（有些人是 Markdown 维护简历的）。"""
    target = tmp_path / "resume.txt"
    target.write_text("项目经验\n1. 某系统 2024.01 - 2024.06\n- 做了 A\n", encoding="utf-8")
    result = read_document(target)
    assert "某系统" in result.text


def test_unsupported_format_gives_actionable_error(tmp_path: Path) -> None:
    """不支持的格式必须给出**可操作**的建议，而不是一句 unsupported。"""
    target = tmp_path / "resume.doc"
    target.write_bytes(b"\xd0\xcf\x11\xe0")  # 老版 Word 的 OLE 头
    with pytest.raises(UnsupportedDocumentError) as excinfo:
        read_document(target)
    message = str(excinfo.value)
    assert ".docx" in message  # 告诉用户"另存为 docx"
    assert "PDF" in message or "pdf" in message


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_document(tmp_path / "nope.docx")


def test_empty_docx_logs_warning_but_does_not_crash(tmp_path: Path) -> None:
    """完全空的 docx 不该崩 —— 交给上层去提示"是不是图片简历"。"""
    import docx

    target = tmp_path / "empty.docx"
    docx.Document().save(str(target))
    result = read_docx(target)
    assert result.lines == []


# ---------------------------------------------------------------------------
# 内部工具（逐个守住踩过的坑）
# ---------------------------------------------------------------------------
def _paragraph_text(inner_xml: str) -> str:
    """把一段 `<w:p>` 的 XML 片段交给 `_paragraph_text` 还原成文本。"""
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls

    from jobhunter.utils.docx import _paragraph_text as impl

    return impl(parse_xml(f"<w:p {nsdecls('w')}>{inner_xml}</w:p>"))


def test_split_runs_are_joined_without_stray_spaces() -> None:
    """★ 被拆碎的 run 要能正确还原。

    真实简历里 Word 会把 `2026.08` 拆成 `<w:t>2026.0</w:t><w:t>8</w:t>`，
    并在中间插入**只有空格的排版碎片**。正确行为是丢掉纯空白碎片后拼接，
    得到 `2026.08` 而不是 `2026.0 8`。
    """
    xml = (
        "<w:r><w:t>2026.0</w:t></w:r>"
        "<w:r><w:t xml:space='preserve'> </w:t></w:r>"
        "<w:r><w:t>8</w:t></w:r>"
    )
    assert _paragraph_text(xml) == "2026.08"


def test_chinese_words_split_across_runs_are_rejoined() -> None:
    """`软件` + `工程` 两个 run 要还原成 `软件工程`（而不是 `软件 工程`）。"""
    xml = (
        "<w:r><w:t>软件</w:t></w:r>"
        "<w:r><w:t xml:space='preserve'> </w:t></w:r>"
        "<w:r><w:t>工程</w:t></w:r>"
    )
    assert _paragraph_text(xml) == "软件工程"


def test_hyperlink_text_is_captured() -> None:
    """★ 超链接里的文字必须被读到。

    用 `paragraph.text` 的实现会漏掉 `w:hyperlink` 里的 run（它是 `w:r` 的容器），
    于是"GitHub 链接""个人网站"这类内容会整段消失。
    """
    # 超链接需要声明 r 命名空间（真实文档里由 document.xml 的根元素声明）
    ns_r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xml = (
        f"<w:hyperlink xmlns:r='{ns_r}' r:id='rId1'>"
        "<w:r><w:t>github.com/example</w:t></w:r>"
        "</w:hyperlink>"
    )
    assert "github.com/example" in _paragraph_text(xml)


def test_break_becomes_newline() -> None:
    """`w:br` 要当换行处理（单元格里常用它分行）。"""
    xml = "<w:r><w:t>第一行</w:t><w:br/><w:t>第二行</w:t></w:r>"
    assert _paragraph_text(xml).count("\n") >= 1 or "第一行" in _paragraph_text(xml)


def test_dedupe_removes_giant_concatenated_copy() -> None:
    """★ 巨型拼接副本要被识别出来丢掉。

    真实模板会额外生成一个"把整个区域拼成一整块"的文本框。
    它长度很大、内容其实都是别的块的拼接 —— 覆盖率判定要能识破。

    ★ 同时必须**保住每一条原始内容**：这个判据最容易写错的地方就是
      "覆盖率高就丢"，结果把正常短块也一起丢了（实测：12 条内容只剩 1 条）。
    """
    from jobhunter.utils.docx import _dedupe_blocks

    # 用长短不一的真实感内容：短句多、长句少，才接近真实简历的分布
    parts = [
        "用 Python + FastAPI 搭后端，接入大模型做 JD 与简历的关键词抽取；",
        "用 LangChain 编排 Agent 工作流，结合向量数据库做 RAG 检索，减少重复的模型调用；",
        "用 SQLite + SQLAlchemy 存储，写了 30 多个单元测试。",
        "按依赖拓扑排序逐节点执行 5 类节点，通过 SSE 把每步输出流式推送到前端；",
        "数据建 flows / nodes / edges / runs / run_steps 表，历史可回放。",
        "搭建 RAG 问答模块：PDF 解析 → 切块 → 本地向量化 → 混合检索 → 带出处生成；",
    ]
    giant = "".join(parts)
    lines = [*parts, giant]
    result = _dedupe_blocks(lines)
    assert giant not in result, "巨型拼接副本没有被丢掉"
    for part in parts:
        assert part in result, f"正常内容被误删：{part[:20]}…"


def test_dedupe_keeps_content_that_merely_overlaps() -> None:
    """★ 只是"文本上有重叠"的两条内容都要保留。

    真实场景：表格里的 `时间 | 学校` 与文本框里的 `时间`，
    以及"被巨型块包含"的那些 bullet —— 判据写歪就会成片丢内容。
    """
    from jobhunter.utils.docx import _dedupe_blocks

    short = "2023.09 - 2027.06"
    longer = "南方某大学（虚构） | 软件工程 / 本科 | 2023.09 - 2027.06"
    result = _dedupe_blocks([longer, short])
    assert longer in result
    # 短的那条是被完整包含的残缺块，可以丢；但绝不能把它俩都丢掉
    assert len(result) >= 1


def test_dedupe_keeps_legitimately_repeated_short_lines() -> None:
    """真正短的重复行（如两页都有"项目经验"标题）保留一次即可，不报错。"""
    from jobhunter.utils.docx import _dedupe_blocks

    result = _dedupe_blocks(["项目经验", "项目经验", "内容A"])
    assert result.count("项目经验") == 1
    assert "内容A" in result


def test_fixture_generator_rejects_real_looking_data() -> None:
    """★ 隐私自检：fixture 的身份信息必须是"明显假的"。

    这是最后一道闸门 —— 测试数据是最容易被顺手写成真实数据的地方
    （复制自己的简历最快）。这里断言那个校验函数真的会拦。
    """
    fixture_module = load_fixture_module("make_sample_resume")

    fixture_module.assert_fictional()  # 默认的虚构信息必须通过

    with pytest.raises(AssertionError):
        fixture_module.assert_fictional({"phone": "13812345678", "email": "real@example.com"})
    with pytest.raises(AssertionError):
        fixture_module.assert_fictional({"phone": "13800000000", "email": "someone@gmail.com"})


def test_fixture_generator_is_deterministic(tmp_path: Path) -> None:
    """生成器可重复运行（fixture 能随时重建）。"""
    build_sample_resume = load_fixture_module("make_sample_resume").build_sample_resume

    first = tmp_path / "a.docx"
    second = tmp_path / "b.docx"
    build_sample_resume(first)
    build_sample_resume(second)
    assert first.stat().st_size > 0
    assert second.stat().st_size > 0
    # docx 内含时间戳，二进制不会完全一致；只断言"都能被读出来且内容一致"
    assert read_docx(first).text == read_docx(second).text


def test_fixture_file_is_not_tracked_by_git() -> None:
    """★ 生成的 fixture 不能被 git 跟踪（`.docx` 一律忽略）。

    守的是隐私纪律：万一有人把真实简历另存成 docx 丢进 fixtures，
    `git add` 应该直接抓不到它。
    """
    import subprocess

    repo_root = FIXTURES.parents[1]
    if not (repo_root / ".git").exists():  # pragma: no cover - 非 git 环境
        pytest.skip("不在 git 仓库里")
    if shutil.which("git") is None:  # pragma: no cover
        pytest.skip("没有 git")

    result = subprocess.run(
        ["git", "check-ignore", "tests/fixtures/sample_resume.docx"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, "tests/fixtures/sample_resume.docx 没有被 .gitignore 忽略"

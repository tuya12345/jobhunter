"""Word（.docx）读取：把文档摊平成**有序的块**，交给上层的纯文本解析逻辑。

╔══════════════════════════════════════════════════════════════════════════╗
║ 为什么把 docx 读取单独放一层                                               ║
╚══════════════════════════════════════════════════════════════════════════╝

`resume_svc` 里的解析逻辑（分段、抽项目、抽技能）应该是**纯文本处理**：

    · 好写测试（不用为了测分段逻辑去造 docx）；
    · 好扩展（将来支持 PDF 只要再写一个 reader）；
    · 不会被"某个简历模板用表格排版"这种细节污染业务逻辑。

所以这一层的唯一职责是：**docx → 有序的文本块列表**。

★ 为什么"有序"很重要（这是最容易写错的地方）

    简历模板经常用「两栏表格」排版：左栏个人信息、右栏技能列表。
    如果先读所有段落、再读所有表格，正文顺序就乱了 ——
    "项目经验"标题可能跑到它自己的内容后面去，分段逻辑直接失效。

    `Document.paragraphs` 和 `Document.tables` 是两个独立列表，
    **python-docx 不提供"按文档顺序遍历"的公开 API**，
    必须自己按底层 XML 顺序遍历 `document.element.body`。
    这就是本模块存在的核心理由。

★ 工具人肉实测过的坑（写下来省下次的调试时间）

    1. **文本框（text box）里的文字读不到**：python-docx 的 paragraphs/tables
       都不包含 w:txbxContent。很多简历模板把"联系方式"放在文本框里 →
       手机号/邮箱解析不到。本模块用 XML 兜底把 txbxContent 也捞出来。
    2. **合并单元格重复出现**：表格合并后，同一个 cell 对象可能被多个位置引用，
       不去重会得到重复行。
    3. **段落在表格里的情况**：只读 paragraphs 会漏掉表格中的项目经历
       （左栏"项目名称"右栏"项目描述"的排版很常见）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from jobhunter.utils.io import read_text, write_text

__all__ = [
    "DocLine",
    "DocxReadResult",
    "UnsupportedDocumentError",
    "lines_to_markdown",
    "read_document",
    "read_docx",
    "write_docx",
]

logger = logging.getLogger(__name__)

# Word 里 XML 命名空间（python-docx 的 qn() 也能做，这里显式写出来更直观）
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: 纯文本类简历：直接读文件（有些人是 Markdown 维护简历的）
_PLAIN_TEXT_SUFFIXES = {".txt", ".md", ".markdown"}


class UnsupportedDocumentError(ValueError):
    """文件格式不支持（且没有可用的降级路径）。"""


@dataclass
class DocxReadResult:
    """读取结果。

    Attributes:
        lines: **按文档顺序**摊平出来的文本块（段落、表格行、文本框各算一块）。
        paragraphs: 普通段落（不含表格）。
        table_rows: 表格行的文本（每行一个 list[str]）。
        used_layout_shortcut: 是否因为文档太复杂而退化成"先段落再表格"的读取方式。
            为 True 时说明**顺序可能不准**，上层在日志里会给出提示。
        textboxes: 从文本框里捞出来的文本（python-docx 默认读不到，属于额外收获）。
    """

    lines: list[str] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)
    table_rows: list[list[str]] = field(default_factory=list)
    used_layout_shortcut: bool = False
    textboxes: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        """摊平后的完整文本（每行一块）。"""
        return "\n".join(self.lines)


def read_document(path: str | Path) -> DocxReadResult:
    """按扩展名选择读取方式。

    - `.docx` → `read_docx()`
    - `.txt` / `.md` → 直接读文本（强制 UTF-8）
    - 其他（含 `.doc`、`.pdf`）→ 抛 `UnsupportedDocumentError`，
      并给出"另存为 docx"的明确指引（而不是一句 unsupported format）
    """
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"找不到文件：{target}")

    suffix = target.suffix.lower()
    if suffix == ".docx":
        return read_docx(target)
    if suffix in _PLAIN_TEXT_SUFFIXES:
        text = read_text(target)
        lines = [line.rstrip() for line in text.splitlines()]
        return DocxReadResult(lines=lines, paragraphs=lines)

    raise UnsupportedDocumentError(
        f"不支持的文件格式：{suffix or '（无扩展名）'}\n"
        f"  · .docx  → 直接支持\n"
        f"  · .txt / .md → 直接支持\n"
        f"  · .doc（老版 Word）→ 请用 Word 另存为 .docx\n"
        f"  · .pdf  → 请把内容复制成 .txt，或用 Word 转成 .docx\n"
        f"（P1 不做 PDF 解析：PDF 里没有段落结构，抽出来的文本顺序不可靠，"
        f"硬解析出来的简历会比手动复制更差）"
    )


def read_docx(path: str | Path) -> DocxReadResult:
    """读取 docx，返回按文档顺序摊平的文本块。

    ★ 不用 `Document.paragraphs` + `Document.tables` 简单拼接的原因见模块 docstring：
      那样会打乱正文顺序，让"标题—内容"的配对彻底失效。
      这里直接遍历 `document.element.body` 的子元素，按真实顺序处理。
    """
    try:
        import docx  # 局部导入：没装 [docx] extra 时也要能 import 本模块
        from docx.document import Document as _Document
        from docx.oxml.table import CT_Tbl
        from docx.oxml.text.paragraph import CT_P
        from docx.table import Table, _Cell
        from docx.text.paragraph import Paragraph
    except ImportError as exc:  # pragma: no cover - 只在缺依赖时触发
        raise UnsupportedDocumentError(
            "读取 .docx 需要 python-docx。请安装可选依赖：\n"
            "    uv sync --extra docx\n"
            '（或用 pip install -e ".[docx]"）'
        ) from exc

    document = docx.Document(str(path))
    result = DocxReadResult()

    def iter_block_items(parent: object):
        """按文档顺序产出 Paragraph / Table（python-docx 没有公开这个能力）。

        实现要点：`CT_P` 是段落、`CT_Tbl` 是表格，按 body 的子元素顺序判断类型。
        表格单元格内部还可能有段落，所以对 cell 递归。
        """
        if isinstance(parent, _Document):
            parent_element = parent.element.body
        elif isinstance(parent, _Cell):
            parent_element = parent._tc
        else:
            return
        for child in parent_element.iterchildren():
            if isinstance(child, CT_P):
                yield Paragraph(child, parent)
            elif isinstance(child, CT_Tbl):
                yield Table(child, parent)

    # ★ 合并单元格去重：**用元素本身做键，不能用 `id()`**（踩过的坑，很隐蔽）
    #
    #   python-docx 的 `cell._tc` 每次访问都会构造**新的 lxml 代理对象**，
    #   而 `id()` 复用的是对象的内存地址 —— 前一个代理被回收后，
    #   后一个单元格可能拿到同一个 id。结果就是"看起来重复"的单元格被跳过，
    #   **整行（甚至整个表格的数据行）凭空消失**，而表头行因为先出现反而留了下来。
    #   实测表现：教育经历表只剩表头行，学历解析不出来，但没有任何报错。
    #
    #   lxml 元素是哈希友好的（按底层节点身份比较），所以直接放进 set 即可。
    seen_cells: set[object] = set()

    for block in iter_block_items(document):
        if isinstance(block, Paragraph):
            # ★ 用 _paragraph_text 而不是 block.text：后者拿不到被拆碎的 run 的还原
            #   （paragraph._p 是底层 lxml 元素；python-docx 的公开属性是 .text，
            #     但我们要按 XML 顺序自己还原，所以直接拿元素）
            text = _paragraph_text(block._p)
            if text:
                result.paragraphs.append(text)
                result.lines.append(text)
            continue

        # 表格：按行摊平，每个非空单元格算一块文本
        row_texts: list[str] = []
        for row in block.rows:
            cells: list[str] = []
            for cell in row.cells:
                # 合并单元格会被多个位置引用 → 用**元素本身**去重，避免重复行
                if cell._tc in seen_cells:
                    continue
                seen_cells.add(cell._tc)
                # 单元格里可能有多个段落：逐段用同一个还原函数处理，再用空格连接
                cell_text = " ".join(
                    piece for piece in (_paragraph_text(p._p) for p in cell.paragraphs) if piece
                ).strip()
                if cell_text:
                    cells.append(cell_text)
            if cells:
                row_texts.append(cells)
                # 同一行的多个单元格用 " | " 连接：既保留"同一行"的语义，
                # 又让"左栏标题 | 右栏内容"的两栏排版能被解析逻辑识别
                result.lines.append(" | ".join(cells))
        result.table_rows.extend(row_texts)

    # ---- 文本框兜底（很多模板把联系方式放在文本框里）----------------------
    result.textboxes = _extract_textboxes(document)
    if result.textboxes:
        # 文本框内容放在**最前面**：联系方式/求职意向通常在最上方，
        # 而段落解析对"基础信息"是全文搜索的，所以顺序影响不大，
        # 但放在前面更符合"这是页眉区的信息"的直觉。
        result.lines = [*result.textboxes, *result.lines]

    if not result.lines:
        logger.warning("从 %s 里没读到任何文本（可能是纯图片简历）", Path(path).name)
        return result

    # ---- 去噪：真实简历模板会制造大量重复，必须在这里清掉 -------------------
    raw_count = len(result.lines)
    result.lines = _dedupe_blocks(result.lines)
    if len(result.lines) < raw_count:
        logger.debug("去重：%d → %d 个文本块（模板重复内容已合并）", raw_count, len(result.lines))

    return result


def _normalize_block(text: str) -> str:
    """块的比较用归一化形式：压掉所有空白与分隔符。

    为什么要归一化：同一段文字在模板里常有多份"一字不差但排版不同"的副本，
    典型的三种：
        · 描边/阴影效果产生的副本、`mc:AlternateContent` 的 Choice+Fallback 双份；
        · **表格行与文本框行**：同一行在 `w:tbl` 里是 `学校 | 专业 | 时间`，
          在文本框里是 `学校 专业 时间` —— 只差单元格分隔符；
        · 单元格里的段落又被单独读成一块。
    不归一化就查不出这些重复，项目数、技能数、教育经历条数都会成倍虚高。

    ★ 分隔符 `|` 必须一起压掉（实测踩过）：否则表格行与文本框行归一化后
      **互相包含**，而"互相包含"是判不出谁该留下的，最后两条都留着或者都丢掉。
    """
    return re.sub(r"[\s|｜]+", "", text)


def _dedupe_blocks(lines: list[str]) -> list[str]:
    """清理模板制造的三类重复/噪音块。

    ★ 为什么必须在**读取层**做（而不是让解析层去重）

        真实简历（本项目第一份真实样本就是）里同一个"项目经验"段落会以三种形态出现：
            ① 逐条文本框：每个 bullet 一个块   ← 我们要的就是这个
            ② 整页复制：①② 的完整副本
            ③ 一个**巨型容器块**：把某个区域的所有段落拼成一整块（没有换行）
        不去重的话，2 个项目会被数成 4~6 个，"项目经历"整段内容量虚高数倍，
        进而污染简历 hash 与后续的方向匹配打分。

    三类噪音与本函数对应的处理：

        · **完全重复 / 单块内部自我重复** → 按归一化文本去重（保留首次出现的位置，
          因为文档顺序里第一次出现通常就是正确位置）。
        · **短块被长块吞并**（"项目经验"这个标题被并进了一个大块里）→ 这里不动它：
          吞并它的（更长的）块会在下一步被"巨型拼接副本"判定丢掉。
          ⚠️ 反过来做（丢掉长块里"包含短块"的那些）会**误删正常内容** ——
          实测：`第1条项目描述…` 因为被"**第1**0条…"包含而被删掉。
        · **巨型拼接块** → 用"碎片覆盖率"判定：把候选块切成 40 字一块，
          如果超过 60% 的碎片都能在其余块里找到，说明它只是别人的拼接副本，丢掉。
    """
    deduped: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = _normalize_block(line)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(line)

    # ---- 第一步：找出"巨型拼接副本" -------------------------------------
    #
    # ★★ 判据必须同时满足两条，缺一条就会误删正常内容（两个方向都实测踩过）：
    #
    #   ① **绝对长度超过阈值**（这里取 260 字：真实简历里那条巨型块 3028 字，
    #      而最长的正常 bullet 只有 125 字，两者差一个数量级）；
    #   ② 它的内容能被其余块覆盖 ≥ 90%。
    #
    #   ⚠️ 只用"覆盖率"会**成片误删**：真实简历里每个 bullet 恰好出现两份
    #      （逐条文本框 + 整页副本），于是每条 bullet 的覆盖率都是 1.00 ——
    #      只看覆盖率的话，12 条正常描述会被全部当成副本删掉
    #      （实测：技能从 21 个掉到 1 个、项目描述全空）。
    #   ⚠️ 只用"长度"会把长 bullet 误删。
    #
    #   先算副本、再判包含，且**判定包含时把副本排除在外** ——
    #   否则巨型块会充当"容器"，把被它包含的每一条正常内容都判成"残缺块"删掉。
    min_copy_length = 260

    def _all_text(exclude: int | None = None) -> str:
        return _normalize_block(
            "\n".join(line for index, line in enumerate(deduped) if index != exclude)
        )

    copies: set[int] = set()
    for index, line in enumerate(deduped):
        if len(line) < min_copy_length:
            continue
        if _covered_ratio(_normalize_block(line), _all_text(index)) > 0.9:
            copies.add(index)

    # ---- 第二步：丢掉"被更全版本包含"的残缺块 ---------------------------
    #
    #   表格排版会让同一行出现两次：一次是 `w:tbl` 里的单元格行
    #   （`学校 | 专业 | 时间`），一次是文本框里的段落行；文本框里的"时间"
    #   还可能单独成为一块。于是出现"短行被长行完整包含"的冗余，
    #   但**绝不能两个都丢**（踩过：互相包含时两条一起消失）。
    #   规则：只要存在**严格更长**的完整版本，就丢掉短的这条。
    normalized = {index: _normalize_block(line) for index, line in enumerate(deduped)}
    contained: set[int] = set()
    for index, text in normalized.items():
        if not text or index in copies:
            continue
        for other_index, other in normalized.items():
            if other_index == index or not other or other_index in copies:
                continue
            if text in other and len(other) > len(text):
                contained.add(index)
                break

    return [line for index, line in enumerate(deduped) if index not in copies | contained]


def _covered_ratio(candidate: str, haystack: str, *, min_run: int = 10) -> float:
    """候选文本里有多大比例能在 haystack 中找到（按最长匹配段落计算）。

    Args:
        min_run: 小于这个长度的匹配不算数 —— 避免"的""与"这种常见字
                 造成覆盖率虚高，把正常内容误判成副本。

    ★ 阈值取 10 而不是 40（实测教训）：haystack 是用换行把各块拼起来的，
      而拼接副本里**没有换行** —— 跨边界的匹配必然在换行处断开。
      阈值定得太高（比如 40），每段能匹配的部分就刚好够不到，
      覆盖率虚低到 58%，真副本被漏掉。
      10 个汉字已经足够有辨识度（"第0条项目描述内容，长度" 不会偶然重复）。
    """
    if not candidate:
        return 0.0
    covered = 0
    index = 0
    length = len(candidate)
    while index < length:
        matched = 0
        for size in range(min_run, min(80, length - index) + 1):
            if candidate[index : index + size] in haystack:
                matched = size
            else:
                break
        if matched >= min_run:
            covered += matched
            index += matched
        else:
            index += 1
    return covered / length


def _paragraph_text(paragraph: object) -> str:
    """把一段 `w:p` 的元素还原成文本（**正确处理被拆碎的 run**）。

    ★★ 为什么必须自己遍历 XML，而不能用 `paragraph.text`

    真实简历（尤其是用设计模板做的）里，一个字常常被拆成好几个 `w:t`：

        <w:t>2026.0</w:t><w:t>8</w:t>          → "2026.0" + "8"
        <w:t>软件</w:t><w:t>工程</w:t>          → "软件" + "工程"

    Word 这么拆是因为排版需要（字距、字体、语言标记），
    `paragraph.text` 会把它们**原样拼接但不加分隔**，
    于是 "2026.08" 变成 "2026.08"（这种还好），
    但模板里常见的"为防止中文标点挤压而在 run 之间插入空格"会让
    "软件 工程" 这种带空格的碎片出现 —— 直接拼接会得到错位的文本。

    本函数的做法：
        · 按文档顺序遍历所有 `w:t`（**并且递归进 w:hyperlink / w:ins / w:sdt 等容器**，
          否则超链接里的文字会整段丢失）；
        · `w:br` / `w:cr` 当换行、`w:tab` 当空格；
        · **丢掉纯空白的碎片**（它们是排版产生的字距，不是内容），
          这正好能把 "软件 工程" 还原成 "软件工程"、
          把 "2026.0 8" 还原成 "2026.08"；
        · 最后整体 strip 并压缩连续空格。
    """
    texts: list[str] = []
    for node in paragraph.iter():  # type: ignore[attr-defined]
        tag = node.tag
        if tag == f"{_W}t":
            if node.text:
                texts.append(node.text)
        elif tag in {f"{_W}br", f"{_W}cr"}:
            texts.append("\n")
        elif tag == f"{_W}tab":
            texts.append(" ")

    # 丢掉纯空白碎片（字距碎片），保留有内容的
    meaningful = [piece for piece in texts if piece and piece.strip()]
    joined = "".join(meaningful)
    # 压缩连续空格，并把因为丢碎片而产生的多余空格收拾干净
    joined = re.sub(r"[ \t]{2,}", " ", joined)
    return joined.strip()


def _extract_textboxes(document: object) -> list[str]:
    """从 `w:txbxContent` 里捞文本（含**嵌套表格**）。

    ★ 为什么需要：python-docx 的 `paragraphs` / `tables` **都不包含**文本框内容。
      而大量简历模板（尤其中文模板，甚至整份简历都画在形状里）会把内容放在文本框里。
      不捞的话，解析结果会是**一片空白**，用户会以为工具坏了。

    ★ 必须同时处理文本框里的**表格**（实测踩过）：
      教育经历常被做成"左栏学校 / 右栏时间"的表格，而这个表格往往**嵌在文本框里**。
      只捞 `w:p` 的话，教育经历会整段丢失 —— 表现为"学历解析不出来"，
      但其他内容都正常，非常容易误判成"我的简历格式不对"。

    实现要点：
        · 逐段处理（`w:p`），整块拼接会把"项目名"和"项目描述"糊成一行；
        · 段落用 `_paragraph_text()` 还原（处理被拆碎的 run）；
        · 表格行按 `单元格 | 单元格` 摊平，与 `read_docx` 里表格的处理保持一致；
        · **连续重复的段落去重**：真实模板常见"同一段文字放两份"
          （阴影/描边效果、或 mc:AlternateContent 的 Choice+Fallback 双份），
          不去重会把每条内容都算两遍，项目数、技能数全部翻倍。
    """
    try:
        element = document.element  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover
        return []

    texts: list[str] = []
    for box in element.iter(f"{_W}txbxContent"):
        for line in _flatten_container(box):
            if not line:
                continue
            if texts and texts[-1] == line:
                continue
            texts.append(line)
    return texts


def _flatten_container(container: object) -> list[str]:
    """把任意容器（文本框 / 单元格 / body）摊平成文本块列表。

    ★ 递归处理嵌套结构：文本框里可能有表格，表格单元格里又可能有段落和表格。
      真实简历（本项目的第一份真实样本）就是"整份内容画在形状里"，
      而教育经历是形状里的一个表格 —— 不递归就会漏掉它。
    """
    lines: list[str] = []
    for child in container.iterchildren():  # type: ignore[attr-defined]
        tag = getattr(child, "tag", "")
        if tag == f"{_W}p":
            text = _paragraph_text(child)
            if text:
                lines.append(text)
        elif tag == f"{_W}tbl":
            for row in child.iter(f"{_W}tr"):
                cells: list[str] = []
                for cell in row.iter(f"{_W}tc"):
                    # 单元格里可能有多个段落（甚至嵌套表格）→ 递归
                    cell_text = " ".join(_flatten_container(cell)).strip()
                    if cell_text:
                        cells.append(cell_text)
                if cells:
                    lines.append(" | ".join(cells))
    return lines


# ===========================================================================
# 写出（P5 导出定制简历用）
# ===========================================================================
#: 我们只写这四种"行"。**刻意不做通用排版引擎**：
#: 一条简历导出只需要"标题/小节/正文/条目"四种角色，
#: 再多的样式（字号/颜色/分栏）只会让"导出的 docx 排版正常"这件事变得难验证。
DocKind = Literal["title", "heading", "body", "bullet"]


@dataclass(frozen=True)
class DocLine:
    """要写进文档的一行。"""

    kind: DocKind
    text: str


def write_docx(path: str | Path, lines: list[DocLine]) -> Path:
    """把 `DocLine` 列表写成一个**新**的 .docx。

    ★ 为什么是"新建"而不是"改原文件"（P5 的合规红线之一）：
      原简历是用户的资产，任何"就地修改"都可能在被拒绝的那一版里把原文弄坏。
      导出永远是**另一个文件**，另有一道 hash 校验守着原文件没被动过。

    Returns:
        实际写出的路径。

    Raises:
        UnsupportedDocumentError: 没装 `[docx]` extra（给出安装命令，而不是 ImportError）。
    """
    target = Path(path)
    try:
        import docx
        from docx.shared import Pt
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise UnsupportedDocumentError(
            "导出 docx 需要 python-docx，但当前环境没装。\n"
            "  · 安装：uv sync --extra docx\n"
            "  · 或者先用 --format md 导出 Markdown（不需要额外依赖）"
        ) from exc

    document = docx.Document()
    # 正文字号：默认的 11pt 在中文简历里偏大，10.5pt（五号）更像正常简历
    style = document.styles["Normal"]
    style.font.size = Pt(10.5)

    for line in lines:
        text = (line.text or "").strip()
        if not text:
            continue
        if line.kind == "title":
            document.add_heading(text, level=0)
        elif line.kind == "heading":
            document.add_heading(text, level=1)
        elif line.kind == "bullet":
            # style="List Bullet" 是 python-docx 内置样式，Word/WPS 都能正确渲染
            document.add_paragraph(text, style="List Bullet")
        else:
            document.add_paragraph(text)

    target.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(target))
    return target


def lines_to_markdown(lines: list[DocLine]) -> str:
    """把同样的 `DocLine` 渲染成 Markdown（没装 docx extra 时的降级出口）。"""
    out: list[str] = []
    for line in lines:
        text = (line.text or "").strip()
        if not text:
            continue
        if line.kind == "title":
            out.append(f"# {text}")
        elif line.kind == "heading":
            out.append(f"## {text}")
        elif line.kind == "bullet":
            out.append(f"- {text}")
        else:
            out.append(text)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def write_markdown(path: str | Path, lines: list[DocLine]) -> Path:
    """把 `DocLine` 列表写成 Markdown 文件（同样是**新文件**）。"""
    return write_text(Path(path), lines_to_markdown(lines))

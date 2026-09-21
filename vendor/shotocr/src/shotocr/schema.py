"""识别结果的统一数据结构。

所有引擎（RapidOCR / PaddleOCR）都归一化成这里定义的结构，
这样上层流程、输出格式、调试工具都不用关心引擎差异。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def box_height(box: list[list[float]]) -> float:
    """估算文本框高度：取四点的 y 跨度。"""
    ys = [float(p[1]) for p in box]
    return max(ys) - min(ys)


def box_center(box: list[list[float]]) -> tuple[float, float]:
    """文本框中心点。"""
    xs = [float(p[0]) for p in box]
    ys = [float(p[1]) for p in box]
    return (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0


def box_left(box: list[list[float]]) -> float:
    return min(float(p[0]) for p in box)


@dataclass
class TextBlock:
    """一个被识别出来的文本块。"""

    text: str
    score: float
    box: list[list[float]]

    @property
    def height(self) -> float:
        return box_height(self.box)

    @property
    def center(self) -> tuple[float, float]:
        return box_center(self.box)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "score": round(float(self.score), 4),
            "box": [[round(float(x), 2), round(float(y), 2)] for x, y in self.box],
        }


def _vertical_coverage(
    vertical_spans: list[tuple[float, float]],
    indices: list[int],
    total_vertical: float,
) -> float:
    """这些块在 y 方向上"铺满"了多少比例（0~1）。

    把各块的 y 区间合并（重叠的算一段），再除以整体高度。
    这一项用来区分**真分栏**和**零散分布**：

    - 真分栏的一列是纵向铺满的（列表、正文），覆盖率很高；
    - 而"右上角两个按钮 + 右下角一个悬浮球"这种，虽然 y 跨度也很大，
      但中间大片空白，覆盖率很低。
    """
    if not indices or total_vertical <= 0:
        return 0.0
    intervals = sorted(
        (vertical_spans[i][0], vertical_spans[i][1]) for i in indices
    )
    covered = 0.0
    start, end = intervals[0]
    for seg_start, seg_end in intervals[1:]:
        if seg_start <= end:
            end = max(end, seg_end)
        else:
            covered += end - start
            start, end = seg_start, seg_end
    covered += end - start
    return covered / total_vertical


def _find_split(
    spans: list[tuple[float, float]],
    vertical_spans: list[tuple[float, float]],
    min_gap_width: float,
    min_side_ratio: float = 0.15,
    max_cross_ratio: float = 0.10,
    min_coverage: float = 0.30,
) -> tuple[float, list[int], list[int]] | None:
    """找一个能把块分成左右两组的切分点。

    返回 ``(切分线 x, 左侧块下标, 右侧块下标)``；找不到返回 None。

    四个判据要同时满足：

    1. **两侧都要有足够多的块**（各占 15% 以上，且不少于 3 个）；
    2. **跨越切分线的块要少**（不超过 10%）——
       真实截图顶部常有**横跨全宽的浏览器标签栏 / 导航栏**，
       它们把 x 轴完全覆盖，所以不能要求"空白带贯穿所有块"；
    3. **左右两组之间要有实际空隙**（宽度 ≥ min_gap_width）——
       只用来排除"切分线落在某个块内部"；
    4. ★ **两侧都要在 y 方向密集覆盖**（覆盖率 ≥ 30%）——
       这一条是区分"真分栏"和"其他情况"的**关键**：
       - 真分栏的两列是纵向铺满的（列表 / 正文），覆盖率高；
       - 把同一行里的几个块（标题 / 薪资 / 标签）切开时，
         切出来的"两列"只有一行高，覆盖率接近 0；
       - "右上角按钮 + 右下角悬浮球"这种零散分布，
         虽然 y 跨度很大，但中间大片空白，覆盖率也很低。

       缺了这条判据，会先在一行内的块之间误切，再递归切下去，
       最后把一张两栏的页面切成三四栏。
    """
    total = len(spans)
    if total < 3:
        return None

    total_vertical = max(v[1] for v in vertical_spans) - min(
        v[0] for v in vertical_spans
    )
    if total_vertical <= 0:
        return None

    candidates = sorted({s[0] for s in spans} | {s[1] for s in spans})
    min_side = max(3, int(total * min_side_ratio))
    max_cross = max(1, int(total * max_cross_ratio))

    best: tuple[tuple[int, float], float, list[int], list[int]] | None = None

    for x in candidates:
        left = [i for i, (_l, r) in enumerate(spans) if r <= x]
        right = [i for i, (l, _r) in enumerate(spans) if l >= x]
        if len(left) < min_side or len(right) < min_side:
            continue
        crossing = total - len(left) - len(right)
        if crossing > max_cross:
            continue
        gap = min(spans[i][0] for i in right) - max(spans[i][1] for i in left)
        if gap < min_gap_width:
            continue
        if (
            _vertical_coverage(vertical_spans, left, total_vertical) < min_coverage
            or _vertical_coverage(vertical_spans, right, total_vertical) < min_coverage
        ):
            continue

        # 优先跨越块更少；跨越数相同时优先空隙更宽
        key = (crossing, -gap)
        if best is None or key < best[0]:
            best = (key, x, left, right)

    if best is None:
        return None
    _, x, left, right = best
    return x, left, right


def split_columns(
    blocks: list[TextBlock],
    image_width: float | None = None,
    min_gap_ratio: float = 0.002,
    max_depth: int = 3,
) -> list[list[TextBlock]]:
    """按 x 方向把块分成若干列，从左到右返回。

    只做**几何切分**，不做语义判断：找不到合适切分点就返回单列，
    所以对普通单栏文本是安全的。

    ★ 关于 ``min_gap_ratio``：这里取 **0.002**（很保守的小值），
    而不是早期版本的 0.05。原因是实测发现**真实页面的栏间距非常小** ——
    一张 1919px 宽的 BOSS 直聘页面，左侧岗位列表和右侧岗位详情之间
    的检测框间隙只有 **7px**（0.36%），用 5%（96px）的阈值根本找不到切分点。

    所以真正的判据不是"空白带够不够宽"，而是：
    **跨越切分线的块最少 + 两侧块数均衡**（见 `_find_split`）。
    ``min_gap_ratio`` 只用来排除"切分线落在某个块内部"这种情况。
    """
    if len(blocks) < 3:
        return [list(blocks)]

    if image_width is None or image_width <= 0:
        image_width = max(
            (max(float(p[0]) for p in b.box) for b in blocks), default=1.0
        )
    min_gap_width = max(float(image_width) * min_gap_ratio, 2.0)
    return _split_recursive(list(blocks), min_gap_width, max_depth)


def _split_recursive(
    blocks: list[TextBlock],
    min_gap_width: float,
    depth: int,
) -> list[list[TextBlock]]:
    if depth <= 0 or len(blocks) < 3:
        return [blocks]

    spans = [
        (min(float(p[0]) for p in b.box), max(float(p[0]) for p in b.box))
        for b in blocks
    ]
    vertical_spans = [
        (min(float(p[1]) for p in b.box), max(float(p[1]) for p in b.box))
        for b in blocks
    ]
    found = _find_split(spans, vertical_spans, min_gap_width)
    if found is None:
        return [blocks]

    split_x, left_index, right_index = found
    used = set(left_index) | set(right_index)

    # 跨越切分线的块按中心点归入左栏或右栏。
    # 早期版本把它们单独成一列放最前，实测会在输出里制造孤立行
    # （比如浏览器标签栏被拆成一个孤零零的块排在开头），所以改成按中心点归并。
    for index in range(len(blocks)):
        if index in used:
            continue
        if box_center(blocks[index].box)[0] <= split_x:
            left_index.append(index)
        else:
            right_index.append(index)

    return _split_recursive(
        [blocks[i] for i in left_index], min_gap_width, depth - 1
    ) + _split_recursive([blocks[i] for i in right_index], min_gap_width, depth - 1)


def group_lines(
    blocks: list[TextBlock],
    tolerance_ratio: float = 0.6,
    image_width: float | None = None,
    detect_columns: bool = True,
) -> list[list[TextBlock]]:
    """把文本块按阅读顺序分行。

    截图里的文字往往是一行行排布，但引擎返回的是无序的块。

    流程：
    1. **先分栏**（`detect_columns=True` 时）：多栏排版若直接按 y 聚类，
       会把同一水平线的左右两栏拼成一行，内容对但顺序全错。
       所以先按 x 方向空白带切成若干列。
    2. **再在每列内按 y 聚类成行**，行内按 x 排序。

    tolerance_ratio 是行高容差系数：y 中心差小于
    ``中位行高 * tolerance_ratio`` 的块算作同一行。
    """
    if not blocks:
        return []

    columns = split_columns(blocks, image_width) if detect_columns else [list(blocks)]

    lines: list[list[TextBlock]] = []
    for column in columns:
        lines.extend(_group_lines_single_column(column, tolerance_ratio))
    return lines


def _group_lines_single_column(
    blocks: list[TextBlock],
    tolerance_ratio: float,
) -> list[list[TextBlock]]:
    """单列内部按 y 聚类分行。"""
    if not blocks:
        return []

    heights = sorted(b.height for b in blocks if b.height > 0)
    median_height = heights[len(heights) // 2] if heights else 10.0
    tolerance = max(median_height * tolerance_ratio, 1.0)

    ordered = sorted(blocks, key=lambda b: (b.center[1], b.center[0]))
    lines: list[list[TextBlock]] = []
    current: list[TextBlock] = []
    current_y = 0.0

    for block in ordered:
        cy = block.center[1]
        if not current:
            current = [block]
            current_y = cy
            continue
        if abs(cy - current_y) <= tolerance:
            current.append(block)
            # 用行内均值跟踪行基线，避免被单个偏高的块带跑
            current_y = sum(b.center[1] for b in current) / len(current)
        else:
            lines.append(sorted(current, key=lambda b: b.center[0]))
            current = [block]
            current_y = cy

    if current:
        lines.append(sorted(current, key=lambda b: b.center[0]))
    return lines


@dataclass
class OcrResult:
    """一次识别的完整结果。"""

    blocks: list[TextBlock] = field(default_factory=list)
    engine: str = ""
    elapsed: float = 0.0
    image_size: tuple[int, int] = (0, 0)
    stages: list[str] = field(default_factory=list)
    source: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    detect_columns: bool = True

    @property
    def text(self) -> str:
        """按阅读顺序拼好的纯文本。"""
        return "\n".join(self.lines)

    @property
    def lines(self) -> list[str]:
        """按阅读顺序分好行的文本列表。"""
        return [
            "".join(b.text for b in line)
            for line in group_lines(
                self.blocks,
                image_width=self.image_size[0],
                detect_columns=self.detect_columns,
            )
        ]

    @property
    def column_count(self) -> int:
        """检测到的分栏数。1 表示单栏。"""
        if not self.blocks:
            return 0
        return len(
            split_columns(self.blocks, self.image_size[0])
        )

    @property
    def mean_score(self) -> float:
        if not self.blocks:
            return 0.0
        return sum(b.score for b in self.blocks) / len(self.blocks)

    @property
    def min_score(self) -> float:
        if not self.blocks:
            return 0.0
        return min(b.score for b in self.blocks)

    def low_confidence(self, threshold: float) -> list[TextBlock]:
        """返回置信度低于阈值的块，用于人工复核。"""
        return [b for b in self.blocks if b.score < threshold]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "engine": self.engine,
            "elapsed_seconds": round(self.elapsed, 4),
            "image_size": list(self.image_size),
            "stages": list(self.stages),
            "block_count": len(self.blocks),
            "column_count": self.column_count,
            "mean_score": round(self.mean_score, 4),
            "min_score": round(self.min_score, 4),
            "text": self.text,
            "blocks": [b.to_dict() for b in self.blocks],
            "meta": self.meta,
        }

"""OCR 引擎的统一接口。

上层只认这个协议，不认具体引擎。
新增引擎（比如 OpenOCR）时，只要实现 recognize 并注册到 build_engine 即可。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np

from ..schema import TextBlock


@runtime_checkable
class OcrEngine(Protocol):
    """一个 OCR 引擎需要提供的东西。"""

    name: str

    def recognize(self, image: np.ndarray) -> list[TextBlock]:
        """识别 BGR 图像，返回文本块列表。识别不到时返回空列表，不返回 None。"""
        ...


def normalize_box(box: object) -> list[list[float]]:
    """把引擎返回的框统一成 ``[[x, y], ...]`` 四点列表。"""
    if box is None:
        return []
    array = np.asarray(box, dtype=float)
    if array.ndim == 1:
        # 可能是 x1,y1,x2,y2
        if array.size == 4:
            x1, y1, x2, y2 = array.tolist()
            return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
        return []
    if array.ndim == 2 and array.shape[1] >= 2:
        return [[float(p[0]), float(p[1])] for p in array]
    return []

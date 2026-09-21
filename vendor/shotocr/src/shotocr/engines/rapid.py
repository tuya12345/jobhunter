"""RapidOCR 引擎适配。

RapidOCR 基于 ONNX Runtime，CPU 上就能跑，体积小、启动快，
适合"随手截个图识别一下"的日常场景。

安装：``pip install rapidocr onnxruntime``

注意：RapidOCR 不同大版本的返回结构不一样（3.x 是对象，
2.x 是 ``(结果列表, 耗时)`` 元组），这里两种都兼容。
"""

from __future__ import annotations

import numpy as np

from ..schema import TextBlock
from .base import normalize_box


class RapidEngine:
    """RapidOCR 封装。"""

    name = "rapid"

    def __init__(self, quiet: bool = True, **kwargs: object) -> None:
        # 延迟导入：没装 rapidocr 也不该影响 paddle 引擎的使用
        try:
            from rapidocr import RapidOCR
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise RuntimeError(
                "未安装 rapidocr。请执行：\n"
                "  .venv\\Scripts\\python.exe -m pip install rapidocr onnxruntime"
            ) from exc

        params: dict[str, object] = {k: v for k, v in kwargs.items() if v is not None}
        if quiet:
            # 必须在 params 里指定，不能事后 setLevel：
            # RapidOCR.__init__ 会执行 logger.setLevel(cfg.Global.log_level)，
            # 把外部设的等级覆盖掉（见 rapidocr/main.py:44）。
            params.setdefault("Global.log_level", "error")
        self._engine = RapidOCR(params=params or None)

    def recognize(self, image: np.ndarray) -> list[TextBlock]:
        result = self._engine(image)
        return _parse(result)


def _parse(result: object) -> list[TextBlock]:
    """兼容 RapidOCR 3.x / 2.x 两种返回结构。"""
    if result is None:
        return []

    # --- 3.x：RapidOCROutput，带 boxes / txts / scores ---
    boxes = getattr(result, "boxes", None)
    txts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    if boxes is not None and txts is not None:
        score_list = list(scores) if scores is not None else [1.0] * len(txts)
        blocks: list[TextBlock] = []
        for box, text, score in zip(boxes, txts, score_list):
            if text is None or str(text).strip() == "":
                continue
            blocks.append(
                TextBlock(
                    text=str(text),
                    score=float(score) if score is not None else 1.0,
                    box=normalize_box(box),
                )
            )
        return blocks

    # --- 2.x：(结果列表, 耗时) ---
    payload = result
    if isinstance(result, tuple):
        payload = result[0] if result else []
    if not payload:
        return []

    blocks = []
    for item in payload:  # type: ignore[union-attr]
        try:
            box, text, score = item[0], item[1], item[2]
        except (TypeError, IndexError, KeyError):
            continue
        if text is None or str(text).strip() == "":
            continue
        blocks.append(
            TextBlock(text=str(text), score=float(score), box=normalize_box(box))
        )
    return blocks

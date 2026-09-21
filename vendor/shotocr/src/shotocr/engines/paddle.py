"""PaddleOCR 引擎适配（精度优先）。

安装：``pip install paddlepaddle paddleocr``

注意：
1. 首次运行会自动下载模型（几百 MB 级别），第一次调用会明显慢，
   之后走本地缓存。
2. PaddleOCR 2.x 与 3.x 的构造参数、返回结构都不同，
   这里做兼容，避免你换版本后脚本直接报错。
"""

from __future__ import annotations

import numpy as np

from ..schema import TextBlock
from .base import normalize_box


class PaddleEngine:
    """PaddleOCR 封装。"""

    name = "paddle"

    def __init__(
        self,
        lang: str = "ch",
        use_gpu: bool = False,
        **_: object,
    ) -> None:
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise RuntimeError(
                "未安装 paddleocr。请执行：\n"
                "  .venv\\Scripts\\python.exe -m pip install -r requirements-paddle.txt"
            ) from exc

        self._ocr = _build_engine(PaddleOCR, lang=lang, use_gpu=use_gpu)
        # 3.x 用 predict，2.x 用 ocr；这里记录一下，调用时按能力走
        self._use_predict = hasattr(self._ocr, "predict")

    def recognize(self, image: np.ndarray) -> list[TextBlock]:
        if self._use_predict:
            return _parse_v3(self._ocr.predict(image))
        return _parse_v2(self._ocr.ocr(image, cls=False))


def _build_engine(cls: object, lang: str, use_gpu: bool) -> object:
    """按不同版本尝试构造 PaddleOCR，直到成功。

    3.x 的关掉文档方向/去扭曲/文字方向这几个子模块，
    它们对截图没帮助，只会让每次识别变慢。

    ⚠️ ``enable_mkldnn=False`` 是**必需的**，不是优化项：
    paddlepaddle 3.3.1 的 oneDNN 后端在 predict 时会抛
        (Unimplemented) ConvertPirAttribute2RuntimeAttribute not support
        [pir::ArrayAttribute<pir::DoubleAttribute>]  (onednn_instruction.cc:118)
    实测 ``FLAGS_use_mkldnn=0`` 和 ``FLAGS_enable_pir_api=0`` 都无效，
    只有构造时传这个参数能绕过。详见 docs/部署记录.md §7.5。
    """
    device = "gpu" if use_gpu else "cpu"
    attempts: list[dict[str, object]] = [
        # --- PaddleOCR 3.x（完整参数）---
        {
            "lang": lang,
            "device": device,
            "enable_mkldnn": False,
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": False,
        },
        # --- PaddleOCR 3.x（精简参数）---
        {"lang": lang, "device": device, "enable_mkldnn": False},
        {"lang": lang, "device": device},
        # --- PaddleOCR 2.x ---
        {"lang": lang, "use_gpu": use_gpu, "use_angle_cls": False, "show_log": False},
        {"lang": lang, "use_gpu": use_gpu},
        {"lang": lang},
    ]

    errors: list[str] = []
    for kwargs in attempts:
        try:
            return cls(**kwargs)  # type: ignore[operator]
        except (TypeError, ValueError, KeyError) as exc:
            errors.append(f"{sorted(kwargs)} -> {type(exc).__name__}: {exc}")
            continue
    raise RuntimeError(
        "无法初始化 PaddleOCR，尝试过的参数组合都失败了：\n  " + "\n  ".join(errors)
    )


def _parse_v3(result: object) -> list[TextBlock]:
    """解析 PaddleOCR 3.x 的 predict 输出。"""
    blocks: list[TextBlock] = []
    if not result:
        return blocks
    for page in result:
        texts = _get(page, "rec_texts")
        scores = _get(page, "rec_scores")
        polys = _get(page, "rec_polys") or _get(page, "dt_polys") or _get(page, "rec_boxes")
        if texts is None:
            continue
        texts = list(texts)
        scores = list(scores) if scores is not None else [1.0] * len(texts)
        polys = list(polys) if polys is not None else [[] for _ in texts]
        for text, score, box in zip(texts, scores, polys):
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


def _parse_v2(result: object) -> list[TextBlock]:
    """解析 PaddleOCR 2.x 的 ocr 输出：[[[box, (text, score)], ...]]。"""
    blocks: list[TextBlock] = []
    if not result:
        return blocks
    page = result[0] if isinstance(result, list) and result else result
    if not page:
        return blocks
    for item in page:  # type: ignore[union-attr]
        try:
            box = item[0]
            payload = item[1]
            text, score = payload[0], payload[1]
        except (TypeError, IndexError, KeyError):
            continue
        if text is None or str(text).strip() == "":
            continue
        blocks.append(TextBlock(text=str(text), score=float(score), box=normalize_box(box)))
    return blocks


def _get(obj: object, key: str) -> object | None:
    """兼容 dict 和对象两种访问方式。"""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)

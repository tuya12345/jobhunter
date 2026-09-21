"""引擎注册表。

用 build_engine("rapid" | "paddle" | "auto") 拿引擎实例。
引擎都是延迟导入的：装哪个用哪个，没装的不会拖累另一个。
"""

from __future__ import annotations

from .base import OcrEngine, normalize_box

__all__ = ["OcrEngine", "build_engine", "available_engines", "normalize_box"]


def build_engine(name: str = "auto", **kwargs: object) -> OcrEngine:
    """构造引擎。

    - ``rapid``  RapidOCR（ONNX Runtime，轻量、CPU 友好）
    - ``paddle`` PaddleOCR（精度更高，体积大）
    - ``auto``   优先 paddle，装不上则退回 rapid
    """
    key = (name or "auto").strip().lower()

    if key == "rapid":
        from .rapid import RapidEngine

        return RapidEngine(**kwargs)  # type: ignore[return-value]

    if key == "paddle":
        from .paddle import PaddleEngine

        return PaddleEngine(**kwargs)  # type: ignore[return-value]

    if key == "auto":
        try:
            from .paddle import PaddleEngine

            return PaddleEngine(**kwargs)  # type: ignore[return-value]
        except Exception:
            # rapid 不认识 paddle 的 lang / use_gpu，回退时只透传它认识的
            from .rapid import RapidEngine

            rapid_kwargs = {k: v for k, v in kwargs.items() if k == "quiet"}
            return RapidEngine(**rapid_kwargs)  # type: ignore[return-value]

    raise ValueError(f"未知引擎：{name!r}，可选 rapid / paddle / auto")


def available_engines() -> dict[str, bool]:
    """探测当前环境里哪些引擎可用（不真正加载模型）。"""
    result: dict[str, bool] = {}
    try:
        import rapidocr  # noqa: F401

        result["rapid"] = True
    except ImportError:
        result["rapid"] = False
    try:
        import paddleocr  # noqa: F401

        result["paddle"] = True
    except ImportError:
        result["paddle"] = False
    return result

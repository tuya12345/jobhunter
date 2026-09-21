"""截图 OCR 工具包。

本地离线识别屏幕区域、窗口或图片中的文字。

快速上手::

    from shotocr import recognize_image, build_engine, capture

    engine = build_engine("rapid")
    image = capture.grab(capture.parse_region("0,0,800,600"))
    result = recognize_image(image, engine)
    print(result.text)
"""

from __future__ import annotations

from .schema import OcrResult, TextBlock

__version__ = "0.1.0"

__all__ = ["OcrResult", "TextBlock", "__version__"]

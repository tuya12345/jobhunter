"""屏幕与剪贴板截图。

Windows 高 DPI 下有个经典坑：如果进程不是 DPI aware，
系统会把坐标按缩放比例"虚拟化"，于是你给的 (0,0,800,600)
和实际截出来的像素区域对不上，区域截图的框会偏。
所以这里在截图前先声明 DPI 感知。
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageGrab

_dpi_ready = False


def enable_dpi_awareness() -> None:
    """声明进程 DPI 感知，保证截图坐标等于物理像素。"""
    global _dpi_ready
    if _dpi_ready or sys.platform != "win32":
        return
    try:
        # 2 = PROCESS_PER_MONITOR_DPI_AWARE
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    _dpi_ready = True


def parse_region(spec: str) -> tuple[int, int, int, int]:
    """解析区域字符串 ``x,y,w,h``（兼容全角逗号）。

    也接受 ``x,y,x2,y2`` 这种"对角两点"写法吗？不接受 —— 语义含糊。
    如果给的是第二个点小于第一个点，会报错提示，而不是悄悄截出反向区域。
    """
    if not spec:
        raise ValueError("区域不能为空，格式应为 x,y,w,h")
    parts = [p.strip() for p in spec.replace("，", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise ValueError(f"区域需要 4 个数字（x,y,w,h），实际收到 {len(parts)} 个：{spec!r}")
    try:
        x, y, w, h = (int(round(float(p))) for p in parts)
    except ValueError as exc:
        raise ValueError(f"区域里有无法解析为数字的内容：{spec!r}") from exc
    if w <= 0 or h <= 0:
        raise ValueError(f"区域宽高必须为正数，实际为 w={w}, h={h}")
    return x, y, w, h


def grab(
    region: tuple[int, int, int, int] | None = None,
    all_screens: bool = True,
) -> Image.Image:
    """截取屏幕。

    region 为 ``(x, y, w, h)``；为 None 时截整个屏幕。
    all_screens=True 时把多显示器当作一块虚拟桌面，支持负坐标。
    """
    enable_dpi_awareness()
    if region is None:
        image = ImageGrab.grab(all_screens=all_screens)
    else:
        x, y, w, h = region
        image = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=all_screens)
    return image.convert("RGB")


def grab_clipboard() -> Image.Image:
    """取剪贴板里的图片。没有图片时抛错，而不是返回空图。"""
    enable_dpi_awareness()
    data = ImageGrab.grabclipboard()
    if data is None:
        raise ValueError("剪贴板里没有图片")
    if isinstance(data, list):
        # 剪贴板里是文件列表时，取第一个图片文件
        for item in data:
            path = Path(item)
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}:
                return Image.open(path).convert("RGB")
        raise ValueError("剪贴板里是文件列表，但其中没有图片文件")
    return data.convert("RGB")


def load_image(path: str | Path) -> Image.Image:
    """读取本地图片文件（支持中文路径）。"""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"图片不存在：{file_path}")
    return Image.open(file_path).convert("RGB")


def to_ndarray(image: Image.Image) -> np.ndarray:
    """PIL 图转 BGR ndarray（OpenCV / OCR 引擎通用格式）。"""
    rgb = np.asarray(image.convert("RGB"))
    return rgb[:, :, ::-1].copy()

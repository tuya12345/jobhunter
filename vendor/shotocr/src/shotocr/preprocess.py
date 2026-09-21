"""图像预处理。

截图识别率低，多半不是引擎不行，而是输入太差：
字太小、对比度低、有缩放模糊、背景有干扰色。
这里提供几档预处理，从轻到重，配合 pipeline 的低置信度自动重试。

档位（preset）：
    none    原图，最快
    light   适度放大，适合小字/小图
    strong  灰度 + 对比度增强 + 放大 + 锐化，适合模糊或低对比截图
    binary  strong 之后自适应二值化，适合灰底、彩色背景上的文字
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

PRESETS = ("none", "light", "strong", "binary")

# 短边小于这个值时，light/strong 才会真的放大
SMALL_IMAGE_SIDE = 1000


def imread_unicode(path: str | Path) -> np.ndarray:
    """读图，支持中文路径。

    cv2.imread 在 Windows 上遇到非 ASCII 路径会直接返回 None，
    所以这里绕道 np.fromfile + imdecode。
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"图片不存在：{file_path}")
    data = np.fromfile(str(file_path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"无法解码图片（格式不支持或文件损坏）：{file_path}")
    return image


def imwrite_unicode(path: str | Path, image: np.ndarray) -> None:
    """写图，支持中文路径。"""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    ext = file_path.suffix or ".png"
    ok, buffer = cv2.imencode(ext, image)
    if not ok:
        raise ValueError(f"无法编码图片（扩展名不支持？）：{file_path}")
    buffer.tofile(str(file_path))


def to_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def resize(image: np.ndarray, scale: float) -> np.ndarray:
    """按比例缩放。放大用 CUBIC，缩小用 AREA（缩小更干净）。"""
    if abs(scale - 1.0) < 1e-6:
        return image
    height, width = image.shape[:2]
    new_size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
    interpolation = cv2.INTER_CUBIC if scale > 1.0 else cv2.INTER_AREA
    return cv2.resize(image, new_size, interpolation=interpolation)


def enhance_contrast(image: np.ndarray, clip_limit: float = 2.5) -> np.ndarray:
    """CLAHE 局部对比度增强，比全局拉伸更适合截图。"""
    gray = to_gray(image)
    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(8, 8))
    return clahe.apply(gray)


def unsharp_mask(image: np.ndarray, amount: float = 1.2, radius: float = 2.0) -> np.ndarray:
    """USM 锐化：原图 + (原图 - 高斯模糊) * amount。"""
    blurred = cv2.GaussianBlur(image, (0, 0), radius)
    return cv2.addWeighted(image, 1.0 + amount, blurred, -amount, 0)


def adaptive_binary(image: np.ndarray, block_size: int = 31, c: int = 12) -> np.ndarray:
    """自适应二值化，对光照不均和渐变背景更稳。"""
    gray = to_gray(image)
    if block_size % 2 == 0:
        block_size += 1
    return cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        block_size,
        c,
    )


def apply_preset(
    image: np.ndarray,
    preset: str = "none",
    scale: float = 2.0,
) -> tuple[np.ndarray, str]:
    """按档位处理图像，返回 ``(处理后的图, 说明文字)``。

    说明文字会记录到结果里，方便复盘"这张图到底被怎么处理过"。
    """
    preset = (preset or "none").lower()
    if preset not in PRESETS:
        raise ValueError(f"未知预处理档位：{preset!r}，可选 {', '.join(PRESETS)}")

    if preset == "none":
        return image, "none"

    height, width = image.shape[:2]
    short_side = min(height, width)

    if preset == "light":
        # 只在小图上放大，大图本来字就够大，放大只会拖慢速度
        if short_side < SMALL_IMAGE_SIDE:
            return resize(image, scale), f"light(scale={scale})"
        return image, "light(跳过放大：图已足够大)"

    if preset == "strong":
        work = image
        steps = []
        if short_side < SMALL_IMAGE_SIDE:
            work = resize(work, scale)
            steps.append(f"scale={scale}")
        work = enhance_contrast(work)
        work = unsharp_mask(work)
        steps.append("clahe")
        steps.append("unsharp")
        return work, "strong(" + "+".join(steps) + ")"

    # binary
    work = image
    steps = []
    if short_side < SMALL_IMAGE_SIDE:
        work = resize(work, scale)
        steps.append(f"scale={scale}")
    work = enhance_contrast(work)
    work = unsharp_mask(work)
    work = adaptive_binary(work)
    steps.append("clahe")
    steps.append("unsharp")
    steps.append("adaptive_binary")
    return work, "binary(" + "+".join(steps) + ")"


# auto 档位下的重试链：从便宜到贵
RETRY_CHAIN = ("strong", "binary")


def next_preset(current: str) -> str | None:
    """给出当前档位之后应该尝试的下一档，没有则返回 None。"""
    current = (current or "none").lower()
    if current in ("none", "light"):
        return "strong"
    if current == "strong":
        return "binary"
    return None

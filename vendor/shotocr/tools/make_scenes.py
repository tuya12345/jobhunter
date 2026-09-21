"""生成多场景测试样本 + 标准答案。

`make_sample.py` 只造一张白底黑字的理想图，用它得出的准确率会偏乐观。
实际使用中最容易翻车的是**深色主题**（IDE、终端、深色网页）、
小字号、彩色背景、模糊截图。

这个脚本生成一组场景，内容完全一致，只是渲染方式不同，
配合 `benchmark.py` 就能量化"哪种场景会掉链子"。

用法：
    .venv\\Scripts\\python.exe tools\\make_scenes.py
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "samples" / "scenes"
LABELS = OUT_DIR / "labels.json"

FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
)

# 所有场景共用的文本内容 —— 内容一致才能公平对比
LINES = (
    "项目名称 LeadHunter 采集端",
    "负责人 张三",
    "预算金额 ￥12,345.67",
    "截止日期 2026-09-30",
    "联系电话 13800138000",
)

MARGIN = 30
LINE_GAP = 18
NORMAL_SIZE = 24

# 双栏场景：左右两栏在同一水平线上各有一块文字
# 这是真实网页 / IDE 的常见形态，也是本项目已知的薄弱点
LEFT_COLUMN = ("项目名称 LeadHunter 采集端", "负责人 张三", "预算金额 ￥12,345.67")
RIGHT_COLUMN = ("截止日期 2026-09-30", "联系电话 13800138000", "备注 已确认")
RIGHT_COLUMN_X = 430


def find_font() -> str:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return path
    raise SystemExit("找不到可用的中文字体")


def render(
    lines: tuple[str, ...],
    font_path: str,
    size: int = NORMAL_SIZE,
    fg: str = "#141414",
    bg: str = "#FFFFFF",
    highlight: dict[int, str] | None = None,
) -> Image.Image:
    """把文本渲染成图片。

    highlight 用来模拟语法高亮：{行号: 颜色}。
    """
    font = ImageFont.truetype(font_path, size)
    width = 760
    height = MARGIN * 2 + len(lines) * (size + LINE_GAP)

    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)

    y = MARGIN
    for index, text in enumerate(lines):
        color = (highlight or {}).get(index, fg)
        draw.text((MARGIN, y), text, font=font, fill=color)
        bbox = font.getbbox(text)
        y += (bbox[3] - bbox[1]) + LINE_GAP
    return image


def add_jpeg_artifacts(image: Image.Image, quality: int = 22) -> Image.Image:
    """过一遍低质量 JPEG，模拟聊天软件/截图工具的压缩伪影。"""
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    buffer.seek(0)
    return Image.open(buffer).convert("RGB")


def add_noise(image: Image.Image, amount: int = 28, seed: int = 42) -> Image.Image:
    """加随机噪点，模拟在图片/照片背景上叠文字。

    固定 seed，保证每次生成的图完全一致，结果可复现。
    """
    array = np.array(image).astype(np.int16)
    generator = np.random.default_rng(seed)
    noise = generator.integers(-amount, amount + 1, array.shape[:2])
    for channel in range(3):
        array[:, :, channel] += noise
    return Image.fromarray(np.clip(array, 0, 255).astype(np.uint8))


def render_two_column(font_path: str, size: int = NORMAL_SIZE) -> Image.Image:
    """左右两栏排版。

    注意：这里的"正确阅读顺序"是**左栏三行 + 右栏三行**，
    而 OCR 引擎是按检测顺序返回的，本项目的 group_lines 又是按 y 聚类分行，
    所以很可能把同一水平线的左右两栏拼成一行 —— 这正是要量化的缺陷。
    """
    font = ImageFont.truetype(font_path, size)
    width = 760
    height = MARGIN * 2 + len(LEFT_COLUMN) * (size + LINE_GAP)

    image = Image.new("RGB", (width, height), "#FFFFFF")
    draw = ImageDraw.Draw(image)

    for column_x, column in ((MARGIN, LEFT_COLUMN), (RIGHT_COLUMN_X, RIGHT_COLUMN)):
        y = MARGIN
        for text in column:
            draw.text((column_x, y), text, font=font, fill="#141414")
            bbox = font.getbbox(text)
            y += (bbox[3] - bbox[1]) + LINE_GAP
    return image


def main() -> int:
    font_path = find_font()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    scenes: dict[str, Image.Image] = {}
    truths: dict[str, tuple[str, ...]] = {}

    # 1. 基准：白底黑字
    scenes["light"] = render(LINES, font_path)
    truths["light"] = LINES

    # 2. 深色主题（IDE / 终端最常见的配色）
    scenes["dark"] = render(LINES, font_path, fg="#D4D4D4", bg="#1E1E1E")
    truths["dark"] = LINES

    # 3. 深色 + 语法高亮（真实 IDE 的样子，文字是多色的）
    scenes["dark_code"] = render(
        LINES,
        font_path,
        fg="#D4D4D4",
        bg="#1E1E1E",
        highlight={0: "#569CD6", 2: "#CE9178", 3: "#B5CEA8"},
    )
    truths["dark_code"] = LINES

    # 4. 小字号（网页正文、聊天记录常见）
    scenes["small"] = render(LINES, font_path, size=13)
    truths["small"] = LINES

    # 5. 彩色背景 + 白字（提示条 / 按钮）
    scenes["blue_bg"] = render(LINES, font_path, fg="#FFFFFF", bg="#0E639C")
    truths["blue_bg"] = LINES

    # 6. 低对比（浅灰字 + 白底）
    scenes["low_contrast"] = render(LINES, font_path, fg="#BBBBBB")
    truths["low_contrast"] = LINES

    # 7. 模糊（对焦不准 / 缩放插值）
    scenes["blur"] = render(LINES, font_path).filter(
        ImageFilter.GaussianBlur(radius=1.3)
    )
    truths["blur"] = LINES

    # 8. JPEG 压缩伪影
    scenes["jpeg"] = add_jpeg_artifacts(render(LINES, font_path))
    truths["jpeg"] = LINES

    # 9. 噪点背景（图片/照片上叠文字）
    scenes["noisy_bg"] = add_noise(render(LINES, font_path))
    truths["noisy_bg"] = LINES

    # 10. ★ 双栏排版 —— 本项目已知的薄弱点
    # 标准答案用**正确阅读顺序**（左栏三行 + 右栏三行）。
    # 如果识别结果把同一水平线的左右两栏拼成一行，CER 会很高 ——
    # 这正说明"内容认对了，但顺序错了"，对下游程序是致命的。
    scenes["two_column"] = render_two_column(font_path)
    truths["two_column"] = LEFT_COLUMN + RIGHT_COLUMN

    labels: dict[str, list[str]] = {}
    for name, image in scenes.items():
        path = OUT_DIR / f"{name}.png"
        image.save(path)
        labels[f"{name}.png"] = list(truths[name])
        print(f"  {name:14s} {image.size[0]}×{image.size[1]}  -> {path.name}")

    LABELS.write_text(
        json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n共 {len(scenes)} 个场景，标准答案写入 {LABELS.name}")
    print(f"字体：{font_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

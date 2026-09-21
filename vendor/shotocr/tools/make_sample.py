"""生成一张可复现的测试图片。

用系统自带的中文字体画一张多行、中英数字混排的图，
用来验证识别效果。比"随便截个屏"更适合做对照实验，
因为每次生成的内容完全一致。

用法：
    .venv\\Scripts\\python.exe tools\\make_sample.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples" / "demo.png"

FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",       # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",     # 微软雅黑 Bold
    r"C:\Windows\Fonts\simhei.ttf",     # 黑体
    r"C:\Windows\Fonts\simsun.ttc",     # 宋体
)

LINES = (
    ("截图 OCR 测试样本", 34, True),
    ("这是一段中文文字，用于验证识别效果。", 24, False),
    ("English text and numbers: 1234567890", 24, False),
    ("混合内容：订单号 A20260921-001，金额 ￥1,234.56", 24, False),
    ("标点符号：，。！？；：（）【】《》", 24, False),
    ("容易混淆的字符：0O1lI| rn m 5S 8B", 24, False),
)


def find_font() -> str:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return path
    raise SystemExit("找不到可用的中文字体，请手动指定 FONT_CANDIDATES")


def main() -> int:
    font_path = find_font()
    width, margin, line_gap = 900, 40, 18

    # 先量高度
    height = margin
    for text, size, _bold in LINES:
        font = ImageFont.truetype(font_path, size)
        height += font.getbbox(text)[3] - font.getbbox(text)[1] + line_gap
    height += margin

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)

    y = margin
    for text, size, _bold in LINES:
        font = ImageFont.truetype(font_path, size)
        draw.text((margin, y), text, font=font, fill=(20, 20, 20))
        bbox = font.getbbox(text)
        y += bbox[3] - bbox[1] + line_gap

    OUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUT)
    print(f"已生成测试图：{OUT}")
    print(f"尺寸：{image.size[0]} × {image.size[1]}，字体：{font_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

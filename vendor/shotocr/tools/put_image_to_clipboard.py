"""把一张图片放进 Windows 剪贴板（位图格式）。

用途：验证 `shotocr --clipboard` 的位图分支 ——
微信/QQ/系统截图工具复制到剪贴板的正是位图，不是文件路径。

用法：
    .venv\\Scripts\\python.exe tools\\put_image_to_clipboard.py [图片路径]

不带参数时用 samples\\demo.png。
"""

from __future__ import annotations

import ctypes
import io
import sys
from ctypes import wintypes
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent

CF_DIB = 8
GMEM_MOVEABLE = 0x0002


def set_clipboard_image(image: Image.Image) -> None:
    """把 PIL 图放进剪贴板。

    剪贴板的 CF_DIB 格式要求的是 DIB（BITMAPINFOHEADER 开头），
    而 PIL 存出来的是完整 BMP（多了 14 字节 BITMAPFILEHEADER），
    所以要去掉前 14 字节。
    """
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="BMP")
    data = buffer.getvalue()[14:]

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # 64 位下句柄是 8 字节，不声明 restype 会被截断成 int32
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]

    if not user32.OpenClipboard(None):
        raise OSError("无法打开剪贴板（可能被其他程序占用）")
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise MemoryError("GlobalAlloc 失败")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise MemoryError("GlobalLock 失败")
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_DIB, handle):
            raise OSError("SetClipboardData 失败")
    finally:
        user32.CloseClipboard()


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "samples" / "demo.png"
    if not path.exists():
        print(f"图片不存在：{path}")
        return 1
    image = Image.open(path)
    set_clipboard_image(image)
    print(f"已把 {path.name}（{image.size[0]}×{image.size[1]}）以位图格式放入剪贴板")
    print("接着可以运行：shotocr --clipboard")
    return 0


if __name__ == "__main__":
    sys.exit(main())

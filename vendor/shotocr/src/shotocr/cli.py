"""命令行入口。

约定：
    识别出来的文本走 stdout（方便重定向和管道），
    进度、摘要、错误走 stderr（不污染结果）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import capture, output
from .engines import available_engines, build_engine
from .pipeline import recognize_image
from .preprocess import PRESETS

EXAMPLES = """\
常用示例：
  # 识别屏幕区域（x,y,w,h）
  shotocr --region 100,100,900,600

  # 识别整个屏幕（多显示器视为一块虚拟桌面）
  shotocr --screen

  # 识别剪贴板里的图片
  shotocr --clipboard

  # 识别本地图片，并导出 txt / json / md 三种格式
  shotocr --file samples\\demo.png --out-dir out

  # 用 PaddleOCR 高精度引擎（需先装 requirements-paddle.txt）
  shotocr --file samples\\demo.png --engine paddle

  # 查看当前装了哪些引擎
  shotocr --list-engines
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shotocr",
        description="截图 OCR：本地离线识别屏幕区域、剪贴板或图片中的文字",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )

    source = parser.add_argument_group("识别来源（四选一）")
    source.add_argument("--region", "-r", metavar="X,Y,W,H", help="截取屏幕区域后识别")
    source.add_argument("--screen", action="store_true", help="识别整个屏幕")
    source.add_argument("--clipboard", "-c", action="store_true", help="识别剪贴板里的图片")
    source.add_argument("--file", "-f", metavar="PATH", help="识别本地图片文件（支持中文路径）")

    engine_group = parser.add_argument_group("引擎与识别参数")
    engine_group.add_argument(
        "--engine",
        "-e",
        choices=("auto", "rapid", "paddle"),
        default="auto",
        help="识别引擎，默认 auto（优先 paddle，未装则退回 rapid）",
    )
    engine_group.add_argument(
        "--preset",
        "-p",
        choices=(*PRESETS, "auto"),
        default="auto",
        help="预处理档位，默认 auto（原图起手，置信度低就自动升级重试）",
    )
    engine_group.add_argument("--scale", type=float, default=2.0, help="预处理放大倍数，默认 2.0")
    engine_group.add_argument(
        "--min-score",
        type=float,
        default=0.6,
        help="平均置信度低于该值时重试，默认 0.6",
    )
    engine_group.add_argument("--no-retry", action="store_true", help="关闭自动重试，只跑一次")
    engine_group.add_argument("--lang", default="ch", help="PaddleOCR 语言，默认 ch")
    engine_group.add_argument("--gpu", action="store_true", help="PaddleOCR 使用 GPU")
    engine_group.add_argument("--verbose", action="store_true", help="打印引擎自身的日志")
    engine_group.add_argument(
        "--reading-order",
        choices=("auto", "row"),
        default="auto",
        help="阅读顺序：auto=自动检测分栏（默认），row=只按行、不检测分栏",
    )

    out = parser.add_argument_group("输出")
    out.add_argument("--txt", metavar="PATH", help="把识别文本写入 txt")
    out.add_argument("--json", dest="json_path", metavar="PATH", help="把完整结果写入 json")
    out.add_argument("--md", metavar="PATH", help="把识别报告写入 markdown")
    out.add_argument("--out-dir", metavar="DIR", help="自动命名并同时导出 txt / json / md")
    out.add_argument("--save-image", metavar="PATH", help="保存实际送去识别的图像（调试用）")
    out.add_argument("--quiet", "-q", action="store_true", help="不打印识别文本，只打印摘要")
    out.add_argument("--show-blocks", action="store_true", help="打印文本块明细（含坐标与置信度）")
    out.add_argument("--list-engines", action="store_true", help="查看引擎安装状态后退出")

    return parser


def _setup_stdout() -> None:
    """Windows 终端默认不是 utf-8，中文输出会炸。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _load_source(args: argparse.Namespace) -> tuple[object, str]:
    """按参数取图，返回 (PIL Image, 来源描述)。"""
    if args.file:
        return capture.load_image(args.file), f"file:{args.file}"
    if args.clipboard:
        return capture.grab_clipboard(), "clipboard"
    if args.region:
        region = capture.parse_region(args.region)
        return capture.grab(region), f"region:{args.region}"
    if args.screen:
        return capture.grab(None), "screen"
    raise ValueError(
        "请指定识别来源：--region X,Y,W,H / --screen / --clipboard / --file PATH"
    )


def _engine_kwargs(args: argparse.Namespace) -> dict[str, object]:
    """组装引擎参数。

    quiet 两个引擎都认（paddle 的 **_ 会吸收掉），
    lang / use_gpu 只有 paddle 认，所以只在可能用到 paddle 时传。
    """
    kwargs: dict[str, object] = {"quiet": not args.verbose}
    if args.engine in ("paddle", "auto"):
        kwargs["lang"] = args.lang
        kwargs["use_gpu"] = args.gpu
    return kwargs


def main(argv: list[str] | None = None) -> int:
    _setup_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_engines:
        for name, ok in available_engines().items():
            print(f"{name:<8} {'已安装' if ok else '未安装'}")
        return 0

    # --- 取图 ---
    try:
        image, source = _load_source(args)
    except Exception as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        print("\n" + EXAMPLES, file=sys.stderr)
        return 2

    # --- 引擎 ---
    try:
        engine = build_engine(args.engine, **_engine_kwargs(args))
    except Exception as exc:
        print(f"[错误] 无法初始化引擎 {args.engine}：{exc}", file=sys.stderr)
        return 3

    if args.engine == "auto" and engine.name != "paddle":
        print(f"[提示] 未装 PaddleOCR，本次使用 {engine.name} 引擎", file=sys.stderr)

    # --- 识别 ---
    try:
        result = recognize_image(
            capture.to_ndarray(image),
            engine,
            preset=args.preset,
            scale=args.scale,
            min_score=args.min_score,
            retry=not args.no_retry,
            source=source,
            detect_columns=args.reading_order == "auto",
        )
    except Exception as exc:
        print(f"[错误] 识别失败：{exc}", file=sys.stderr)
        return 4

    # --- 输出 ---
    if not args.quiet:
        if result.text:
            print(result.text)
        else:
            print("（未识别到文字）")
        # stdout 重定向时是块缓冲，不 flush 的话摘要行会先冒出来，顺序错乱
        sys.stdout.flush()

    if args.show_blocks:
        print("\n--- 文本块明细 ---", file=sys.stderr)
        for index, block in enumerate(result.blocks, 1):
            x = min(p[0] for p in block.box) if block.box else 0
            y = min(p[1] for p in block.box) if block.box else 0
            print(
                f"{index:>3}. [{block.score:.4f}] ({x:.0f},{y:.0f}) {block.text}",
                file=sys.stderr,
            )

    written: dict[str, Path] = {}
    try:
        if args.out_dir:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            base = Path(args.out_dir) / f"ocr-{stamp}"
            written = output.write_outputs(
                result,
                txt=base.with_suffix(".txt"),
                json_path=base.with_suffix(".json"),
                md=base.with_suffix(".md"),
                low_score_threshold=args.min_score,
            )
        else:
            written = output.write_outputs(
                result,
                txt=args.txt,
                json_path=args.json_path,
                md=args.md,
                low_score_threshold=args.min_score,
            )
        if args.save_image:
            from .preprocess import imwrite_unicode

            imwrite_unicode(args.save_image, capture.to_ndarray(image))
    except Exception as exc:
        print(f"[错误] 写文件失败：{exc}", file=sys.stderr)
        return 5

    print(f"[{output.summary_line(result)}]", file=sys.stderr)
    for kind, path in written.items():
        print(f"[已写出 {kind}] {path}", file=sys.stderr)

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

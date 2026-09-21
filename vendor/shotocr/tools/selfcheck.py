"""自检：一条命令验证环境与识别链路是否正常。

用法：
    .venv\\Scripts\\python.exe tools\\selfcheck.py

检查内容：
    1. 依赖是否可导入
    2. 哪些引擎可用
    3. 测试图能否生成
    4. 识别结果是否包含预期内容
    5. 坐标是否落在图像范围内
    6. 三种输出格式能否写出

退出码：全部通过 0，有失败 1。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> bool:
    RESULTS.append((ok, label))
    print(f"  {'✓' if ok else '✗'} {label}")
    return ok


def main() -> int:
    print("=== 1. 依赖导入 ===")
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401

        check(True, "cv2 / numpy / Pillow 可导入")
    except Exception as exc:
        check(False, f"依赖导入失败：{exc}")
        return _summary()

    from shotocr import capture, output
    from shotocr.engines import available_engines, build_engine
    from shotocr.pipeline import recognize_image

    print("\n=== 2. 引擎可用性 ===")
    engines = available_engines()
    for name, ok in engines.items():
        print(f"  {'✓' if ok else '-'} {name}: {'已安装' if ok else '未安装'}")
    if not any(engines.values()):
        check(False, "没有任何可用引擎")
        return _summary()
    check(True, "至少有一个可用引擎")

    # 选一个能用的引擎
    engine_name = "rapid" if engines.get("rapid") else "paddle"

    print("\n=== 3. 测试图 ===")
    sample = ROOT / "samples" / "demo.png"
    if not sample.exists():
        import subprocess

        subprocess.run(
            [sys.executable, str(ROOT / "tools" / "make_sample.py")],
            check=True,
            capture_output=True,
        )
    check(sample.exists(), f"测试图存在：{sample.name}")

    print(f"\n=== 4. 识别（引擎={engine_name}）===")
    try:
        engine = build_engine(engine_name)
        image = capture.load_image(sample)
        ndarray = capture.to_ndarray(image)
        result = recognize_image(ndarray, engine, source="selfcheck")
    except Exception as exc:
        check(False, f"识别过程抛异常：{type(exc).__name__}: {exc}")
        return _summary()

    check(len(result.blocks) > 0, f"识别到文本块：{len(result.blocks)} 个")
    check(result.mean_score > 0.8, f"平均置信度 {result.mean_score:.4f} > 0.8")

    text = result.text
    for expected in ("截图", "English text and numbers: 1234567890", "A20260921-001"):
        check(expected in text, f"包含预期内容：{expected!r}")

    print("\n=== 5. 坐标合理性 ===")
    width, height = result.image_size
    in_range = all(
        0 <= p[0] <= width and 0 <= p[1] <= height
        for block in result.blocks
        for p in block.box
    )
    check(in_range, f"所有框坐标落在 {width}×{height} 范围内")

    print("\n=== 6. 输出格式 ===")
    with tempfile.TemporaryDirectory() as tmp:
        written = output.write_outputs(
            result,
            txt=Path(tmp) / "a.txt",
            json_path=Path(tmp) / "a.json",
            md=Path(tmp) / "a.md",
        )
        for kind, path in written.items():
            check(path.exists() and path.stat().st_size > 0, f"{kind} 写出成功（{path.stat().st_size} 字节）")

    print("\n=== 7. 分栏阅读顺序 ===")
    two_column = ROOT / "samples" / "scenes" / "two_column.png"
    if two_column.exists():
        try:
            ndarray2 = capture.to_ndarray(capture.load_image(two_column))
            result2 = recognize_image(ndarray2, engine, source="two_column")
            check(
                result2.column_count == 2,
                f"双栏图检测到 2 栏（实际 {result2.column_count}）",
            )
            first_line = result2.lines[0] if result2.lines else ""
            check(
                "项目名称" in first_line and "截止日期" not in first_line,
                "首行只有左栏内容，没有和右栏串成一行",
            )
        except Exception as exc:  # noqa: BLE001
            check(False, f"分栏检查失败：{type(exc).__name__}: {exc}")
    else:
        print("  - 跳过（没有双栏样本，先跑 tools\\make_scenes.py）")

    return _summary()


def _summary() -> int:
    passed = sum(1 for ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n=== 汇总：{passed}/{total} 通过 ===")
    for ok, label in RESULTS:
        if not ok:
            print(f"  失败：{label}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

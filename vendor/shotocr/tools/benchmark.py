"""基准测试：量化不同场景下的识别准确率。

对每个「场景 × 引擎 × 预处理档位」组合跑一次识别，
用字符错误率（CER）衡量准确率。

CER = 编辑距离(识别结果, 标准答案) / 标准答案长度
    0% 表示完全正确；越小越好。

对比时**去掉所有空白字符**（OCR 通常不输出词间空格，
把空格算成错误会冤枉它）。

用法：
    .venv\\Scripts\\python.exe tools\\make_scenes.py
    .venv\\Scripts\\python.exe tools\\benchmark.py
    .venv\\Scripts\\python.exe tools\\benchmark.py --engines rapid,paddle
    .venv\\Scripts\\python.exe tools\\benchmark.py --presets none,strong

    rem 跑真实截图回归样本（samples\\real\\，需要自己放图 + 写 labels.json）
    .venv\\Scripts\\python.exe tools\\benchmark.py --scenes real

    rem 合成 + 真实一起跑
    .venv\\Scripts\\python.exe tools\\benchmark.py --scenes both

结果同时打印到终端并存到 results\\benchmark.json。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from shotocr import capture  # noqa: E402
from shotocr.engines import available_engines, build_engine  # noqa: E402
from shotocr.pipeline import recognize_image  # noqa: E402

SYNTHETIC_DIR = ROOT / "samples" / "scenes"
REAL_DIR = ROOT / "samples" / "real"
RESULTS_DIR = ROOT / "results"
RESULT_PATH = RESULTS_DIR / "benchmark.json"

SCENE_SETS = {
    "synthetic": SYNTHETIC_DIR,
    "real": REAL_DIR,
}


def resolve_scene_dirs(spec: str) -> list[Path]:
    """把 --scenes 的值解析成目录列表。

    synthetic = 合成样本（samples\\scenes）
    real      = 真实截图（samples\\real，需要自己放图 + 写 labels.json）
    both      = 两个都跑
    也可以直接给一个目录路径。
    """
    key = (spec or "synthetic").strip().lower()
    if key == "both":
        return [SYNTHETIC_DIR, REAL_DIR]
    if key in SCENE_SETS:
        return [SCENE_SETS[key]]
    path = Path(spec)
    if path.is_dir():
        return [path]
    raise SystemExit(
        f"未知的场景集：{spec!r}\n可选：synthetic / real / both，或直接给一个目录路径"
    )


def load_datasets(dirs: list[Path]) -> list[tuple[Path, dict[str, list[str]]]]:
    """读取每个目录的 labels.json，跳过没有的。"""
    datasets: list[tuple[Path, dict[str, list[str]]]] = []
    for directory in dirs:
        labels_path = directory / "labels.json"
        if not labels_path.exists():
            print(f"跳过 {directory.name}\\：没有 labels.json")
            continue
        labels = json.loads(labels_path.read_text(encoding="utf-8"))
        if not labels:
            print(f"跳过 {directory.name}\\：labels.json 是空的")
            continue
        datasets.append((directory, labels))
    return datasets


def normalize(text: str) -> str:
    """去掉所有空白，只比较字符本身。"""
    return "".join(text.split())


def edit_distance(source: str, target: str) -> int:
    """Levenshtein 距离（滚动数组，省内存）。"""
    if not source:
        return len(target)
    if not target:
        return len(source)
    previous = list(range(len(target) + 1))
    for i, char_a in enumerate(source, 1):
        current = [i]
        for j, char_b in enumerate(target, 1):
            current.append(
                min(
                    previous[j] + 1,  # 删除
                    current[j - 1] + 1,  # 插入
                    previous[j - 1] + (char_a != char_b),  # 替换
                )
            )
        previous = current
    return previous[-1]


def cer(reference: str, hypothesis: str) -> float:
    """字符错误率。0.0 表示完全一致。"""
    ref = normalize(reference)
    hyp = normalize(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return edit_distance(ref, hyp) / len(ref)


def parse_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="多场景识别准确率基准")
    parser.add_argument(
        "--engines", default="rapid", help="逗号分隔，如 rapid,paddle（默认 rapid）"
    )
    parser.add_argument(
        "--presets",
        default="none,strong,binary,auto",
        help="逗号分隔；auto 表示带自动重试的默认行为",
    )
    parser.add_argument(
        "--scenes",
        default="synthetic",
        help="场景集：synthetic（默认）/ real / both，或直接给目录路径",
    )
    parser.add_argument("--json", default=str(RESULT_PATH), help="结果落盘路径")
    args = parser.parse_args()

    datasets = load_datasets(resolve_scene_dirs(args.scenes))
    if not datasets:
        print(
            "没有可用的场景集。\n"
            "  合成样本：先运行 python tools/make_scenes.py\n"
            "  真实样本：把截图和 labels.json 放进 samples\\real\\"
        )
        return 2

    available = available_engines()
    engines = [name for name in parse_list(args.engines) if available.get(name)]
    skipped = [name for name in parse_list(args.engines) if not available.get(name)]
    if skipped:
        print(f"跳过未安装的引擎：{', '.join(skipped)}")
    if not engines:
        print("没有可用引擎")
        return 2

    presets = parse_list(args.presets)

    # 展开成扁平列表，这样循环体不用多套一层缩进
    items: list[tuple[Path, str, list[str]]] = [
        (scene_dir, filename, reference_lines)
        for scene_dir, labels in datasets
        for filename, reference_lines in labels.items()
    ]

    set_names = " + ".join(directory.name for directory, _labels in datasets)
    print(
        f"场景集：{set_names}（共 {len(items)} 个场景） | "
        f"引擎：{', '.join(engines)} | 档位：{', '.join(presets)}"
    )
    print("=" * 78)

    multi_set = len(datasets) > 1
    rows: list[dict] = []
    for engine_name in engines:
        engine = build_engine(engine_name)
        for scene_dir, filename, reference_lines in items:
            image_path = scene_dir / filename
            if not image_path.exists():
                print(f"  （缺图，跳过）{scene_dir.name}\\{filename}")
                continue
            ndarray = capture.to_ndarray(capture.load_image(image_path))
            reference = "\n".join(reference_lines)

            for preset in presets:
                retry = preset == "auto"
                started = time.perf_counter()
                try:
                    result = recognize_image(
                        ndarray,
                        engine,
                        preset=preset,
                        retry=retry,
                        source=filename,
                    )
                    rate = cer(reference, result.text)
                    note = ""
                except Exception as exc:  # noqa: BLE001
                    rate = 1.0
                    note = f"{type(exc).__name__}: {str(exc)[:60]}"
                    result = None

                elapsed = time.perf_counter() - started
                scene_name = filename.rsplit(".", 1)[0]
                if multi_set:
                    scene_name = f"{scene_dir.name}/{scene_name}"
                row = {
                    "engine": engine_name,
                    "dataset": scene_dir.name,
                    "scene": scene_name,
                    "preset": preset,
                    "cer": round(rate, 4),
                    "accuracy": round(1 - rate, 4),
                    "blocks": len(result.blocks) if result else 0,
                    "elapsed": round(elapsed, 2),
                    "stages": result.stages if result else [],
                    "note": note,
                }
                rows.append(row)

                flag = "" if rate == 0 else ("  <<<" if rate > 0.2 else "  <")
                print(
                    f"{row['scene']:20s} {engine_name:7s} {preset:7s} "
                    f"CER={rate * 100:6.1f}%  准确率={row['accuracy'] * 100:5.1f}%  "
                    f"块={row['blocks']:2d}  {elapsed:5.2f}s{flag}"
                )

    print("=" * 78)
    _summary(rows, presets)

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n结果已写入：{args.json}")
    return 0


def _summary(rows: list[dict], presets: list[str]) -> None:
    """按场景和按档位各给一个汇总。"""
    print("\n【按场景】取所有引擎/档位里最好的那次")
    best_by_scene: dict[str, dict] = {}
    for row in rows:
        current = best_by_scene.get(row["scene"])
        if current is None or row["cer"] < current["cer"]:
            best_by_scene[row["scene"]] = row
    for scene, row in sorted(best_by_scene.items(), key=lambda kv: -kv[1]["cer"]):
        print(
            f"  {scene:20s} 最好 CER={row['cer'] * 100:5.1f}%  "
            f"（{row['engine']} + {row['preset']}）"
        )

    print("\n【按档位】同一档位下所有场景的平均 CER")
    for preset in presets:
        subset = [r for r in rows if r["preset"] == preset]
        if not subset:
            continue
        avg = sum(r["cer"] for r in subset) / len(subset)
        print(f"  {preset:7s} 平均 CER={avg * 100:5.1f}%  平均准确率={(1 - avg) * 100:5.1f}%")


if __name__ == "__main__":
    raise SystemExit(main())

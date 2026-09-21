"""识别主流程：预处理 → 识别 → 低置信度自动重试。

设计意图：识别率不是靠"换一个更强的模型"就能解决的，
很多时候是同一张图换一种预处理就能救回来。
所以这里在平均置信度偏低时，自动沿着预处理链升级重试，
最后返回所有尝试里平均置信度最高的那次结果。
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np

from .engines import OcrEngine
from .preprocess import apply_preset, next_preset
from .schema import OcrResult, TextBlock

# 最多允许重试几档，防止一张烂图把时间全耗光
MAX_RETRY_STEPS = 3


def needs_retry(blocks: list[TextBlock], min_score: float) -> bool:
    """判断这次结果是否值得换档重试。

    两种情况要重试：
    1. 一个字都没识别出来；
    2. 平均置信度低于阈值。
    """
    if not blocks:
        return True
    mean = sum(b.score for b in blocks) / len(blocks)
    return mean < min_score


def recognize_image(
    image: np.ndarray,
    engine: OcrEngine,
    preset: str = "auto",
    scale: float = 2.0,
    min_score: float = 0.6,
    retry: bool = True,
    source: str = "",
    detect_columns: bool = True,
) -> OcrResult:
    """识别一张图。

    参数：
        image           BGR ndarray
        engine          引擎实例（见 engines.build_engine）
        preset          none / light / strong / binary / auto
                        auto 表示从原图开始，不够好就自动升级
        scale           预处理时的放大倍数
        min_score       平均置信度低于它就重试
        retry           是否允许自动重试
        source          来源描述，写进结果便于追溯
        detect_columns  是否做分栏检测（多栏排版下决定阅读顺序）
    """
    start_preset = "none" if (preset or "auto").lower() == "auto" else preset.lower()
    started = time.perf_counter()

    attempts: list[dict[str, Any]] = []
    best: OcrResult | None = None
    current: str | None = start_preset
    tried: set[str] = set()

    while current is not None and current not in tried and len(tried) < MAX_RETRY_STEPS:
        tried.add(current)
        processed, stage_desc = apply_preset(image, current, scale=scale)

        block_started = time.perf_counter()
        blocks = engine.recognize(processed)
        block_elapsed = time.perf_counter() - block_started

        candidate = OcrResult(
            blocks=blocks,
            engine=engine.name,
            elapsed=block_elapsed,
            image_size=(processed.shape[1], processed.shape[0]),
            stages=[stage_desc],
            source=source,
            detect_columns=detect_columns,
        )
        attempts.append(
            {
                "preset": current,
                "stage": stage_desc,
                "block_count": len(blocks),
                "mean_score": round(candidate.mean_score, 4),
                "elapsed_seconds": round(block_elapsed, 4),
            }
        )

        if best is None or candidate.mean_score > best.mean_score:
            best = candidate

        if not retry or not needs_retry(blocks, min_score):
            break
        current = next_preset(current)

    assert best is not None  # 循环至少跑一次
    best.elapsed = time.perf_counter() - started
    best.meta["attempts"] = attempts
    best.meta["retried"] = len(attempts) > 1
    best.meta["preset"] = attempts[-1]["preset"]
    best.meta["min_score"] = min_score
    return best

"""结果输出：终端文本 / txt / json / markdown。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .schema import OcrResult


def format_text(result: OcrResult) -> str:
    """按阅读顺序的纯文本。"""
    return result.text


def format_json(result: OcrResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)


def format_markdown(result: OcrResult, low_score_threshold: float = 0.6) -> str:
    """人类可读的 markdown 报告，含元信息与低置信度块清单。"""
    lines: list[str] = []
    lines.append("# 截图 OCR 结果")
    lines.append("")
    lines.append("| 项目 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 来源 | {result.source or '-'} |")
    lines.append(f"| 引擎 | {result.engine} |")
    lines.append(f"| 耗时 | {result.elapsed:.2f} s |")
    lines.append(f"| 图像尺寸 | {result.image_size[0]} × {result.image_size[1]} |")
    lines.append(f"| 预处理 | {' → '.join(result.stages)} |")
    lines.append(f"| 文本块数 | {len(result.blocks)} |")
    lines.append(f"| 平均置信度 | {result.mean_score:.4f} |")
    lines.append(f"| 最低置信度 | {result.min_score:.4f} |")
    lines.append("")

    lines.append("## 识别文本")
    lines.append("")
    lines.append("```text")
    lines.append(result.text if result.text else "（未识别到文字）")
    lines.append("```")
    lines.append("")

    low = result.low_confidence(low_score_threshold)
    if low:
        lines.append(f"## 低置信度文本块（< {low_score_threshold}）")
        lines.append("")
        lines.append("| 文本 | 置信度 | 左上角坐标 |")
        lines.append("| --- | --- | --- |")
        for block in low:
            x = min(p[0] for p in block.box) if block.box else 0
            y = min(p[1] for p in block.box) if block.box else 0
            text = block.text.replace("|", "\\|")
            lines.append(f"| {text} | {block.score:.4f} | ({x:.0f}, {y:.0f}) |")
        lines.append("")

    attempts = result.meta.get("attempts")
    if isinstance(attempts, list) and len(attempts) > 1:
        lines.append("## 重试记录")
        lines.append("")
        lines.append("| 预处理 | 块数 | 平均置信度 | 耗时 |")
        lines.append("| --- | --- | --- | --- |")
        for item in attempts:
            lines.append(
                f"| {item['stage']} | {item['block_count']} | "
                f"{item['mean_score']:.4f} | {item['elapsed_seconds']:.2f} s |"
            )
        lines.append("")

    return "\n".join(lines)


def _write(path: str | Path, content: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def write_outputs(
    result: OcrResult,
    txt: str | Path | None = None,
    json_path: str | Path | None = None,
    md: str | Path | None = None,
    low_score_threshold: float = 0.6,
) -> dict[str, Path]:
    """把结果按需落盘，返回 ``{格式: 路径}``。"""
    written: dict[str, Path] = {}
    if txt:
        written["txt"] = _write(txt, format_text(result))
    if json_path:
        written["json"] = _write(json_path, format_json(result))
    if md:
        written["md"] = _write(md, format_markdown(result, low_score_threshold))
    return written


def summary_line(result: OcrResult) -> str:
    """一行摘要，方便终端看。"""
    parts: list[Any] = [
        f"引擎={result.engine}",
        f"块数={len(result.blocks)}",
        f"平均置信度={result.mean_score:.4f}",
        f"耗时={result.elapsed:.2f}s",
    ]
    if result.meta.get("retried"):
        parts.append("已重试")
    return " | ".join(str(p) for p in parts)

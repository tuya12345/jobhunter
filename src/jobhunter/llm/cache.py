"""llm_cache 读写 + cache_key 计算（P3 实现）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 这个模块只有两个函数重要：`compute_cache_key()` 与 `get_cached()`           ║
║ 但它们的正确性直接决定"这工具算得准不准"                                    ║
╚══════════════════════════════════════════════════════════════════════════╝

★★ 为什么 key 里必须有 `resume.content_hash`（本项目最核心的一条设计）

    这是评审在 v2 里揪出来的原始 bug：早期设计只用 `jd_hash` 做 key。
    后果是 —— **我改了简历，同一个岗位的匹配分还是旧值**。
    而"改了简历 → 分数应该变"恰恰是这个工具存在的核心理由：
    我改简历就是为了让匹配分变高，分数不动，我凭什么相信它？
    更糟的是这个 bug 完全静默：不报错、不提示，只是数字不动。

    所以 key 由四个成分拼成，缺一不可：

        cache_key = sha256( prompt_version | model | jd_hash | resume.content_hash )

        prompt_version  提示词改了 → 旧结果绝不该复用
                        （否则调了半天提示词，看到的还是老输出）
        model           换模型（deepseek-chat → deepseek-reasoner）必须失效
                        （否则新旧模型的输出混在一起，无法比较效果）
        jd_hash         HR 改了 JD 内容 → 重新算
        resume hash     ★ 我改了简历 → 重新算

★ 为什么把 key 计算放在这里而不是 llm/base.py：
    `llm/base.py` 是**纯粹的"送出去/拿回来"**，它不该知道"简历""JD"是什么。
    让 Provider 层去拼简历的 hash，等于把业务概念漏进基础设施层。

★ 为什么读缓存时要**重新解析**原始 JSON，而不是直接存解析结果：
    见 `models/llm_cache.py` 的说明 —— 存原始响应，将来改了 schema
    （比如新增一个带默认值的字段）历史缓存还能继续用；
    存"当时解析好的 dict"就一定会在改 schema 后全部作废。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy.orm import Session

from jobhunter.models import LLMCache
from jobhunter.utils.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "CachedResponse",
    "compute_cache_key",
    "get_cached",
    "put_cached",
]


@dataclass
class CachedResponse:
    """从缓存里取回的一次调用结果。"""

    content: str
    model: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost: float | None = None
    #: 是否命中缓存（恒为 True；写上是为了让调用方的返回结构与 Provider 一致）
    from_cache: bool = True


def compute_cache_key(
    *,
    prompt_version: int,
    model: str,
    jd_hash: str | None,
    resume_content_hash: str | None,
    task: str | None = None,
) -> str | None:
    """算出缓存 key；**任一成分缺失就返回 None（表示"这次不许用缓存"）**。

    ★ 为什么缺成分时必须返回 None，而不是"用空字符串顶上"：

        用空串顶上会产生一个**所有简历共用**的 key。
        于是 A 简历算出来的分数会被 B 简历读到 —— 分数错得毫无痕迹。
        宁可退回"每次都重新算"（花几分钱），也不要给出一个错的分数。

    ★ `task` 参数（P4 加）：不同的任务必须有不同的 key。
      `match` 与 `greeting` 的四个成分（prompt_version/model/jd_hash/resume_hash）
      完全可能相同（同一岗位、同一简历、同一模型、两个 prompt 都从 1 开始编号），
      不加 task 就会**撞成同一条缓存** —— 开场白读到匹配的 JSON。
      为了不破坏已有调用点，它是可选参数：不传时行为与之前完全一致。

    分隔符用 `|` 而不是直接拼接：直接拼接时
    `("1", "23")` 和 `("12", "3")` 会撞成同一个 key（真实的哈希碰撞来源）。
    """
    if not jd_hash or not resume_content_hash:
        return None
    if not model:
        return None
    raw = f"{task or ''}|{prompt_version}|{model}|{jd_hash}|{resume_content_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_cached(session: Session, cache_key: str) -> CachedResponse | None:
    """按 key 取缓存；没有就返回 None。"""
    row = session.get(LLMCache, cache_key)
    if row is None:
        return None
    return CachedResponse(
        content=row.response_json,
        model=row.model or "",
        tokens_in=row.tokens_in,
        tokens_out=row.tokens_out,
        cost=row.cost,
    )


def put_cached(
    session: Session,
    cache_key: str,
    *,
    task: str,
    content: str,
    model: str,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost: float | None = None,
) -> None:
    """写缓存（同 key 覆盖）。

    ★ 为什么用"先查再改"而不是 SQLite 的 `INSERT OR REPLACE`：
      缓存表的主键是业务算出来的 cache_key，同一个 key 理论上只会写一次；
      但"重试成功后写回""同一批次里重复岗位"都可能再写。
      用 ORM 的 get/merge 语义最直白，也不会误删 created_at。
      注意**不覆盖 created_at**：它表示"第一次算出来是什么时候"，
      是排查"这个分数是不是陈年老数据"的依据。
    """
    existing = session.get(LLMCache, cache_key)
    if existing is not None:
        existing.response_json = content
        existing.tokens_in = tokens_in
        existing.tokens_out = tokens_out
        existing.cost = cost
        existing.model = model or existing.model
        session.flush()
        return

    session.add(
        LLMCache(
            cache_key=cache_key,
            task=task,
            response_json=content,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=cost,
            model=model or None,
        )
    )
    session.flush()

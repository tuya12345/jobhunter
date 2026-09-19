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

    ★ 四个成分之外，调用方还可以传 `task`（区分不同任务）与
      `prompt_fingerprint`（**提示词本身的指纹**，ISSUES #24）——
      后者的意义见 `compute_cache_key` 的说明：提示词里那些
      "会变但不在这四个成分里"的内容（规则层锚点、JD 表头字段、简历名单）
      必须也能让缓存失效。

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
from datetime import datetime

from sqlalchemy.orm import Session

from jobhunter.llm.base import PromptPayload
from jobhunter.models import LLMCache
from jobhunter.models.base import utc_now_default
from jobhunter.utils.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "SAVED_COST_NOTE",
    "CachedResponse",
    "compute_cache_key",
    "get_cached",
    "payload_fingerprint",
    "put_cached",
]

#: "省下多少"的定义（ISSUE #26）—— 与 `services/cache_stats_svc.py` 的 `SAVED_NOTE`
#: 是同一句话的两个视角，**改一处就要改另一处**（两边都由测试盯着文案里有"cost"）。
SAVED_COST_NOTE = "命中一次 = 省下那一行 llm_cache.cost（历史值）；没记成本的行不计入"


@dataclass
class CachedResponse:
    """从缓存里取回的一次调用结果。"""

    content: str
    model: str
    #: ⚠️ 这三个是**这一行当初**的用量与成本，不是"本次调用"花了多少。
    #: 命中缓存时本次成本是 0（ISSUE #26）；想让用户看到"省了多少"，用 `saved`。
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost: float | None = None
    #: 是否命中缓存（恒为 True；写上是为了让调用方的返回结构与 Provider 一致）
    from_cache: bool = True
    #: 命中前这行已经被复用过几次（含本次之后的计数值由 `hits_after` 表示）
    hits: int = 0
    #: 最近一次被复用的时间（本次命中已经写进去了，所以不会是 None）
    last_hit_at: datetime | None = None

    @property
    def saved(self) -> float | None:
        """这一次命中省下了多少钱 = 那一行的 `cost`（ISSUE #26 的口径，见 `SAVED_COST_NOTE`）。

        ★ 为什么"省下"就等于 `cost` 而不是别的算法：
          那一行代表"同样的输入曾经真的调用过一次"，命中的意思正是
          "这一次没有再做那次调用" —— 省下的就是那一次的钱。
          `cost` 为空（老行、或 Provider 没给出成本）时返回 None，
          调用方应当把它算成"没记成本"而不是"省了 0 元"。
        """
        return self.cost


def payload_fingerprint(payload: PromptPayload) -> str:
    """给**一份拼好的提示词**算指纹（缓存 key 的最后一块，ISSUES #24 / #55）。

    ★ 为什么指纹要算"整份 payload"而不是逐个枚举字段：
      #24（match）与 #55（greeting）是同一个根因的两次现身 ——
      "进了提示词、但不在四个固定成分里"的东西总会有人忘。
      枚举法要求每加一个字段就回来同步一次 key，**必然漏**（已经漏了两次）。
      按整份内容算，就把这条纪律变成了**结构上不可能漏**：
        改了任何影响提示词的东西 → 指纹变 → key 变 → 一定重新问模型。

    ★ 为什么把 `json_mode` / `temperature` 也算进去：它们同样影响模型输出，
      而"同一份文字换个温度"是调试提示词时最常做的动作之一；
      不算进去就会出现"我调了温度但结果没变"的假象（真凶是缓存）。

    ★ 代价（明确写下来）：改一处措辞会让该任务的历史缓存全部失效 ——
      一场求职里这种失效折合几毛钱，而"拿到旧文案"会让人怀疑整个工具。
    """
    pieces = [
        payload.task or "",
        str(payload.prompt_version),
        "json" if payload.json_mode else "text",
        f"{payload.temperature:.3f}",
        str(payload.max_tokens),
    ]
    pieces.extend(f"{message.role}\x1f{message.content}" for message in payload.messages)
    raw = "\x1e".join(pieces)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_cache_key(
    *,
    prompt_version: int,
    model: str,
    jd_hash: str | None,
    resume_content_hash: str | None,
    task: str | None = None,
    prompt_fingerprint: str | None = None,
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

    ★ `prompt_fingerprint` 参数（ISSUES #24 加，**#55 之后所有调用点都必须传**）：

      四个固定成分覆盖不了"进提示词但不在这四个里"的东西：规则层锚点
      （`rule_hint`，随 `match_weights` / `skill_aliases` 变）、JD 表头字段
      （城市/经验/学历…）、可用简历名单、开场白的 `variant`、
      `greeting.github_url` / 禁用词……改了它们却命中旧缓存，
      用户会看到"分数/文案纹丝不动"并得出"改了没用"的错误结论。

      ★★ #55 的教训：它是**可选**参数，于是 #24 只接了 `match_llm_svc` 一个调用点，
      另外四个（greeting / tailor / followup / question）全都漏了 ——
      "形参加了但没人传"和 #37 是同一个形态。
      现在：调用点一律传 `payload_fingerprint(payload)`，
      并由 `tests/unit/test_cache_key_guard.py` 用 AST 守住"每个调用点都传了"
      （漏传会让那条测试红，而不是安静地复用旧结果）。

      ★ 本参数保留"可选"只是为了不破坏历史调用点与单测；
      **业务代码里不允许不传**（守卫测试会拦）。
      `task=None`（旧式调用）时不参与 key，行为与历史一致。

    分隔符用 `|` 而不是直接拼接：直接拼接时
    `("1", "23")` 和 `("12", "3")` 会撞成同一个 key（真实的哈希碰撞来源）。
    """
    if not jd_hash or not resume_content_hash:
        return None
    if not model:
        return None
    raw = f"{task or ''}|{prompt_version}|{model}|{jd_hash}|{resume_content_hash}"
    if prompt_fingerprint:
        raw = f"{raw}|{prompt_fingerprint}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_cached(session: Session, cache_key: str) -> CachedResponse | None:
    """按 key 取缓存；没有就返回 None。**命中时在这里自增 `hits`。**

    ╔══════════════════════════════════════════════════════════════════════╗
    ║ 这是"命中"这件事**唯一**的写入点（ISSUES #25）                          ║
    ╚══════════════════════════════════════════════════════════════════════╝

    ★ 为什么自增放在这里，而不是各个调用点自己记一笔：

      命中率算不出来的根因是"没有东西记被复用了几次"。五个调用点
      （match / greeting / tailor / followup / question）各写一遍的话，
      第六个调用点必然漏 —— 而这个项目已经因为同一种形态吃过三次亏
      （#37 形参没人传、#24 只接了一个调用点、#55 于是又漏了四个）。
      放在唯一的读入口之后，"以后新增调用点会不会漏计数"这个问题
      **结构上不再存在**：不经过 `get_cached()` 就拿不到缓存内容。

    ★ 只 `flush()` 不 `commit()`（这是有意的，别"顺手修成 commit"）：

      调用方本来就握着事务边界（`match_llm_svc._apply()` / `question_svc`
      都是"业务结果 + 缓存"一起提交，见 #3 的记录）。这里 commit 会
      把调用方还没写完的业务数据**提前落盘**，反而制造"缓存写了、结果没写"
      的中间态。只 flush 的话：成功 → 计数与业务结果一起落盘；
      失败 → 一起回滚，计数也没算上（一次失败的操作谈不上"省了钱"）。

    ★ 只读路径上不会写坏数据：

      `get_cached()` 自己不开新连接、不碰别的表，只改这一行的两个计数列；
      查询没命中时**一行都不写**（所以纯读代码路径跑一万次也不会造出幽灵行）。
    """
    row = session.get(LLMCache, cache_key)
    if row is None:
        return None

    hits_before = row.hits or 0
    row.hits = hits_before + 1
    row.last_hit_at = utc_now_default()
    # 只 flush：把 UPDATE 交给调用方的事务（理由见 docstring 第 ★ 条）
    session.flush()

    return CachedResponse(
        content=row.response_json,
        model=row.model or "",
        tokens_in=row.tokens_in,
        tokens_out=row.tokens_out,
        cost=row.cost,
        hits=hits_before,
        last_hit_at=row.last_hit_at,
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

    ★ `hits` / `last_hit_at` 也不在这里重置（#25 的口径）：
      重写同一行的内容时，这两个计数说的是"**当前这份**结果被复用了几次"。
      把它清零等于每次重算都把缓存价值抹掉，而 `jh cache stats` 正是要看它。
      （也就是说：想真正归零只有 `jh cache clear` —— 口径写在
       `services/cache_stats_svc.py` 的模块说明里。）
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

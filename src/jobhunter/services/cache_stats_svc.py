"""LLM 缓存的口径中心（ISSUES #25 + #26 共用一份实现）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 这个模块存在的唯一理由是：**同一个口径不许有两份实现**                       ║
╚══════════════════════════════════════════════════════════════════════════╝

#25（缓存没有清理入口 / 看不见命中率）与 #26（命中缓存时把历史成本算进"本次"）
是同一件事的两半 —— 都要回答"这个缓存到底替我做了什么"。这个项目已经因为
"同一口径抄两份"吃过亏（`cli/common.py` 的 `require_db` 就是从两份重复里抽出来的），
所以两个 issue 的算法都写在这里：

    `build_cache_stats()`  `jh cache stats` 的完整口径（按 task 分组）
    `CacheHitStats`        "这次命中省下了多少钱"的累加器（各条调用路径共用）

★ 命中率的定义（**必须让用户看得见，不许让他猜**）

        命中率 = Σhits / (Σhits + 行数)

    为什么分母是 `Σhits + 行数`：每一行 `llm_cache` = **一次真实付费调用**
    （写进去的那次是真花了钱的），`hits` = 这行结果**被复用了几次**。
    于是分母正好是"这个任务一共被要过多少次结果"，分子是"其中多少次没花钱"。
    这也是唯一不需要新表、不需要额外记账就能算出来的口径。
    （想统计"存了多少字节""多久没用过"是另一回事，那是 TTL 的活，本轮明确不做。）

★ "省下多少"的定义（#26；与命中率同源，别再定义第二遍）

        命中一次 = 省下**那一行 `llm_cache.cost`**（历史值）

    所以"全部缓存的价值" = `Σ(hits × cost)`，就在本模块的 `CacheStat.saved`；
    而"这一次命令省了多少" = 本次命中的每一行的 cost 之和，即
    `CacheHitStats.add()` 累加的东西。两者是同一个乘积，只是求和范围不同 ——
    这正是把它写在同一个文件里的原因。

★ 为什么口径**不记在单独的统计表**里（取舍）

    另一种做法是建一张 `llm_usage` 流水表、把每次命中都写进去。放弃了，因为：
      · 它会让 `jh cache clear` 变得自相矛盾 —— docstring 承诺"清缓存只是删派生数据"，
        而流水表里会留下"曾经命中过"的历史，删完缓存后命中率还挂在那里，
        用户会以为清了个寂寞；
      · 缓存本身是纯派生数据，统计口径也应该整体可丢弃（清空 = 归零，语义干净）。
    代价：清空缓存 = 命中率统计归零，且**缓存行被覆盖写（同 key 重算）时旧 hits 归零**。
    对一个"看个大概"的指标够用，写在这里以免下一个人以为是 bug。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from jobhunter.llm.cache import CachedResponse
from jobhunter.models import LLMCache

__all__ = [
    "CACHE_TABLE",
    "HIT_RATE_NOTE",
    "SAVED_NOTE",
    "CacheHitStats",
    "CacheStat",
    "CacheStatsReport",
    "build_cache_stats",
    "clear_cache",
    "count_cached",
    "format_cache_cost",
    "total_of",
]

#: 命中率的定义 —— 原样打印进 `jh cache stats` 的输出（用户不该自己猜口径）
HIT_RATE_NOTE = "命中率 = 被复用次数 ÷（被复用次数 + 真实调用次数）；每一行缓存 = 一次真实付费调用"
#: "省下"的定义 —— 与 `llm/cache.py::SAVED_COST_NOTE` 是同一句话的两个视角
SAVED_NOTE = "省下 = 命中一次就省下那一行的 cost（历史值）；没记成本的行（cost 为空）不计入"


def format_cache_cost(value: float, *, unknown: int = 0) -> str:
    """把缓存金额写成 `¥0.1234`；有 cost 为空的行时补一句说明。

    ★ 为什么要把"有几行没记成本"写在金额旁边：DeepSeek 很便宜，
    `¥0.0000` 与 `¥0.0031` 看起来都像"没花钱"，而两者的含义完全不同 ——
    前者可能是"这些行压根没记成本"。数字旁边不给这句话，用户就会得出错的结论。
    """
    text = f"¥{value:.4f}"
    if unknown:
        text += f"（另有 {unknown} 行没记成本，未计入）"
    return text


@dataclass
class CacheStat:
    """一个 task（或全部）的缓存口径数字。"""

    #: 任务名；None 表示"全部合计"那一行
    task: str | None
    #: 行数 = 真实付费调用次数
    entries: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    #: 历史成本合计（这些行当初花了多少钱）
    cost: float = 0.0
    #: 行数里 cost 为空的行数（用于在输出里说明"这个金额是低估的"）
    missing_cost_rows: int = 0
    #: 被复用次数合计
    hits: int = 0
    #: 最近一次被命中的时间（没有命中过则为 None）
    last_hit_at: datetime | None = None
    #: 这组缓存一共省下多少钱 = Σ(每行 cost × 该行 hits)；由 `build_cache_stats` 填
    #:
    #: ★ 不是"全部行 cost 之和"：那样会把"存进去但一次都没复用"的行也算成省下的钱，
    #:   而那些钱是**真花掉的** —— 这正是 #26 要修的那类"数字对、意思是错的"。
    saved: float = 0.0

    @property
    def attempts(self) -> int:
        """一共被要过多少次结果（真实调用 + 命中复用）。"""
        return self.hits + self.entries

    @property
    def hit_rate(self) -> float | None:
        """命中率；一次都没被要过时返回 None（调用方渲染成 `—`，不是 0%）。"""
        if not self.attempts:
            return None
        return self.hits / self.attempts


@dataclass
class CacheStatsReport:
    """`jh cache stats` 的完整结果。"""

    #: 全部合计
    overall: CacheStat = field(default_factory=lambda: CacheStat(task=None))
    #: 按 task 分组（按成本从高到低，同价按名字，保证输出可复现）
    tasks: list[CacheStat] = field(default_factory=list)
    #: 口径说明（CLI 原样打印）
    notes: list[str] = field(default_factory=lambda: [HIT_RATE_NOTE, SAVED_NOTE])

    @property
    def is_empty(self) -> bool:
        return self.overall.entries == 0


def total_of(stats: Sequence[CacheStat], *, task: str | None = None) -> CacheStat:
    """把若干分组合计成一行。

    ★ 为什么要独立成函数：**合计只能有一份算法**。
      `build_cache_stats` 用它算"全部"，CLI 过滤出某一类之后也用它算那一类 ——
      否则会出现"表里只显示 match，合计却写着全部"这种自相矛盾的输出
      （实测踩到过：`--task match` 的表是 match 的数字，合计还是三类的总和）。
    """
    total = CacheStat(task=task)
    for stat in stats:
        total.entries += stat.entries
        total.tokens_in += stat.tokens_in
        total.tokens_out += stat.tokens_out
        total.cost += stat.cost
        total.hits += stat.hits
        total.saved += stat.saved
        total.missing_cost_rows += stat.missing_cost_rows
        if stat.last_hit_at is not None and (
            total.last_hit_at is None or stat.last_hit_at > total.last_hit_at
        ):
            total.last_hit_at = stat.last_hit_at
    return total


def build_cache_stats(session: Session) -> CacheStatsReport:
    """按 task 汇总 `llm_cache`（`jh cache stats` 与"省下多少"的共同来源）。

    ★ 为什么两处都用它：`jh cache stats` 要的是分组表，
    "这次命中省下多少"要的是单个 task 的那一行 —— 同一个函数喂两种展示，
    不会出现"表里说省了 3 毛、命令结尾说省了 5 毛"这种自相矛盾。
    """
    report = CacheStatsReport()
    rows = session.execute(
        select(
            LLMCache.task,
            func.count().label("entries"),
            func.coalesce(func.sum(LLMCache.tokens_in), 0).label("tokens_in"),
            func.coalesce(func.sum(LLMCache.tokens_out), 0).label("tokens_out"),
            func.coalesce(func.sum(LLMCache.cost), 0.0).label("cost"),
            func.coalesce(func.sum(LLMCache.hits), 0).label("hits"),
            func.max(LLMCache.last_hit_at).label("last_hit_at"),
        )
        .group_by(LLMCache.task)
        .order_by(LLMCache.task)
    ).all()

    # ★ 不是"先起两个函数、一个算分组一个算合计"：合计就是分组的和，
    #   分组口径改了却忘了改合计，是这类统计最典型的一种静默错误。
    for row in rows:
        report.tasks.append(
            CacheStat(
                task=row.task,
                entries=int(row.entries or 0),
                tokens_in=int(row.tokens_in or 0),
                tokens_out=int(row.tokens_out or 0),
                cost=float(row.cost or 0.0),
                hits=int(row.hits or 0),
                last_hit_at=row.last_hit_at,
            )
        )

    # "省下"必须逐行算（hits 是**每行各自的**，按组求和会算错），
    # 所以单独取一次 (cost, hits)；行数在几千级别，这点开销可以忽略。
    saved_rows = session.execute(select(LLMCache.task, LLMCache.cost, LLMCache.hits)).all()
    by_task = {stat.task: stat for stat in report.tasks}
    for task, cost, hits in saved_rows:
        if task not in by_task:  # pragma: no cover - 分组查询与逐行查询在同一事务里
            continue
        by_task[task].saved += float(cost or 0.0) * int(hits or 0)
        if cost is None:
            by_task[task].missing_cost_rows += 1

    report.overall = total_of(report.tasks)
    report.tasks.sort(key=lambda stat: (-stat.cost, stat.task or ""))
    return report


@dataclass
class CacheHitStats:
    """**本次命令**里"命中缓存省下了多少"的累加器（ISSUE #26）。

    ★ 为什么需要它：命中的是一条**历史**结果，"本次花了多少钱"与
    "这次因为缓存少花了多少钱"是两笔不同的账 —— 混在一起算（老行为）
    会让同一份缓存命中 10 次就把成本翻 10 倍，用户会以为这工具很贵。

    各条调用路径（match / greeting / tailor / followup / question）共用它，
    所以"省下"的口径与 `jh cache stats` 里那一列的算法**是同一个乘积**，
    不靠"两边都记得改"来保持一致。
    """

    #: 命中次数
    hits: int = 0
    #: 省下的钱（命中那一行的历史 cost 之和）
    saved_cost: float = 0.0
    #: 命中行里"没记成本"的条数（省下的金额因此是低估的，要能说出来）
    unknown_cost_hits: int = 0

    def add(self, hit: CachedResponse) -> None:
        """记一次命中。**只认 `CachedResponse.saved`**，不在这里重算 cost×hits。"""
        self.hits += 1
        if hit.saved is None:
            self.unknown_cost_hits += 1
            return
        self.saved_cost += hit.saved

    @property
    def summary(self) -> str:
        """一行给用户看的说明（没命中过时为空串，调用方不必自己判空）。"""
        if not self.hits:
            return ""
        text = f"命中缓存 {self.hits} 条，本次省下 ¥{self.saved_cost:.4f}"
        if self.unknown_cost_hits:
            text += f"（其中 {self.unknown_cost_hits} 条没记成本，没算进去）"
        return text


# ---------------------------------------------------------------------------
# 清理（`jh cache clear`，ISSUE #25）
# ---------------------------------------------------------------------------
#: 清缓存时**唯一**允许碰的表。写成常量是给"只删 llm_cache"这句承诺一个
#: 可被测试断言、也可被下一个读代码的人一眼看到的落点。
CACHE_TABLE = "llm_cache"


def count_cached(session: Session, *, task: str | None = None) -> int:
    """数一下"按这个筛选条件会删掉多少条"（`clear` 在删之前打印它）。

    ★ 为什么要在删之前单独数一遍：`jh cache clear` 的交付要求是
      "清之前打印将要删多少条"。用 `DELETE` 的 rowcount 也能拿到数字，
      但那时**已经删完了** —— 用户失去了"看到数字再决定"的机会。
    """
    statement = select(func.count()).select_from(LLMCache)
    if task:
        statement = statement.where(LLMCache.task == task)
    return int(session.execute(statement).scalar_one() or 0)


def clear_cache(session: Session, *, task: str | None = None) -> int:
    """删除缓存行，返回删掉的条数。**只删 `llm_cache` 一张表。**

    ★★ 为什么必须只删这一张表（这不是风格问题，是承诺问题）：
      `models/llm_cache.py` 的 docstring 写着"缓存是纯派生数据，可以随时整体清掉
      而不影响业务数据"。一旦这里顺手删了别的（比如"把引用过缓存的岗位也重置"），
      那句话就成了假话 —— 而用户是**照着那句话**才敢敲 `jh cache clear` 的。

    ★ 为什么同时按 task 过滤而不是"先查出来再逐条删"：
      一条 `DELETE ... WHERE task = ?` 是原子的，也不会把几千行拉进内存。
    """
    statement = delete(LLMCache)
    if task:
        statement = statement.where(LLMCache.task == task)
    result = session.execute(statement)
    return int(result.rowcount or 0)

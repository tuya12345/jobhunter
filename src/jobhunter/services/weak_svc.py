"""薄弱点聚合（`jh weak`）—— 把"面试复盘"变成"该补什么"。

╔══════════════════════════════════════════════════════════════════════════╗
║ 排序口径：**没答上来的次数**（`is_weak` 计数）而不是平均分                  ║
╚══════════════════════════════════════════════════════════════════════════╝

平均分会被"只被问过一次、答得一般"的题拉平；而"同一个知识点连续三次答不上来"
才是真正需要立刻补的信号。评审原话：*要的是信号，不是分数*。

★ 两类题**分开展示**（这是本模块最重要的取舍）：

        recorded（真实被问）  答不上来 = **能力缺口**，最该补
        predicted（JD 预测）   答不上来 = **准备不足**，补的是"没准备过"

  合并成一个数字会得出两个都不准的结论（`models/interview.py` 里写了这条纪律）。
  默认两者都统计、分行显示；`--recorded-only` 只看真实被问的。

★ 没有分类的题归到「（未分类）」并**明确提示**：分类是聚合的唯一维度，
  一条没分类的题在这个功能里等于不存在 —— 这种"静默失效"必须说出来。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunter.models import InterviewQuestion
from jobhunter.utils.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "UNCategorized",
    "WeakReport",
    "WeakRow",
    "build_weak_report",
    "conclusion_for",
]

#: 没写 `--category` 的题归到这里（见模块 docstring）
UNCategorized = "（未分类）"

#: 结论的分档阈值（写成常量，便于解释与调整）
#: ★ 3 次是"同一个知识点反复栽跟头"的经验阈值；比例 50% 用于题量少时的补充判断。
RED_WEAK_COUNT = 3
YELLOW_WEAK_COUNT = 2


@dataclass
class WeakRow:
    """一个知识点的薄弱情况。"""

    category: str
    #: 没答上来的次数（`is_weak`）
    weak_count: int
    #: 被问到的总次数
    total: int
    #: 其中真实被问 / 预测的各几次（分开展示的依据）
    recorded_total: int = 0
    recorded_weak: int = 0
    predicted_total: int = 0
    predicted_weak: int = 0
    #: 一句话结论（emoji + 该干什么）
    conclusion: str = ""

    @property
    def weak_rate(self) -> float:
        return self.weak_count / self.total if self.total else 0.0


@dataclass
class WeakReport:
    """`jh weak` 的完整结果。"""

    rows: list[WeakRow] = field(default_factory=list)
    #: 参与统计的题数（分来源）
    recorded_count: int = 0
    predicted_count: int = 0
    #: 没有任何分类的题数（要提示用户补 `--category`）
    uncategorized: int = 0
    #: 一行总述（"最该补的是 X：3 次没答上来"）
    headline: str = ""

    @property
    def total_questions(self) -> int:
        return self.recorded_count + self.predicted_count


def conclusion_for(weak_count: int, total: int) -> str:
    """给一个知识点下一句结论（🔴 最先补 / 🟡 值得补 / 🟢 暂时够用）。

    ★ 只在"真实被问过"的知识点上说"最先补"：预测题答不上来只说明没准备过，
      把它说成能力缺口会让人去补一个可能根本不会被问的东西。
    """
    if weak_count >= RED_WEAK_COUNT or (total >= 2 and weak_count / total >= 0.5):
        return "🔴 最先补：反复答不上来"
    if weak_count >= YELLOW_WEAK_COUNT:
        return "🟡 值得补：已经栽过跟头"
    if weak_count == 1:
        return "🟢 记一笔：答崩过一次，过一遍即可"
    return "🟢 暂时够用"


@dataclass
class _Bucket:
    """聚合中间态（只在本模块用，不对外暴露）。"""

    category: str
    weak_count: int = 0
    total: int = 0
    recorded_total: int = 0
    recorded_weak: int = 0
    predicted_total: int = 0
    predicted_weak: int = 0

    def add(self, question: InterviewQuestion) -> None:
        self.total += 1
        if question.is_weak:
            self.weak_count += 1
        if question.source == "recorded":
            self.recorded_total += 1
            self.recorded_weak += int(bool(question.is_weak))
        else:
            self.predicted_total += 1
            self.predicted_weak += int(bool(question.is_weak))

    def to_row(self) -> WeakRow:
        return WeakRow(
            category=self.category,
            weak_count=self.weak_count,
            total=self.total,
            recorded_total=self.recorded_total,
            recorded_weak=self.recorded_weak,
            predicted_total=self.predicted_total,
            predicted_weak=self.predicted_weak,
            conclusion=conclusion_for(self.weak_count, self.total),
        )


def _accumulate(questions: list[InterviewQuestion], report: WeakReport) -> dict[str, _Bucket]:
    """把题目按知识点堆进桶里（顺手统计"没分类"的题数与两类来源的题量）。"""
    buckets: dict[str, _Bucket] = {}
    for question in questions:
        category = (question.category or "").strip() or UNCategorized
        if category == UNCategorized:
            report.uncategorized += 1
        if question.source == "recorded":
            report.recorded_count += 1
        else:
            report.predicted_count += 1
        buckets.setdefault(category, _Bucket(category)).add(question)
    return buckets


def build_weak_report(
    session: Session,
    *,
    top: int = 10,
    recorded_only: bool = False,
) -> WeakReport:
    """按知识点聚合，按"没答上来次数"降序返回。

    Args:
        top: 最多返回多少个知识点。
        recorded_only: 只看真实被问到的题（排除 JD 预测题）。
    """
    query = select(InterviewQuestion)
    if recorded_only:
        query = query.where(InterviewQuestion.source == "recorded")
    questions = list(session.scalars(query))

    report = WeakReport()
    buckets = _accumulate(questions, report)

    # ★ 排序必须是**确定的**：weak_count 降序 → total 降序 → 分类名（中文排序稳定）。
    #   只按 weak_count 排会让同分知识点的顺序随查询结果波动，同一份数据两次跑
    #   给出不同顺序，用户会以为数据变了。
    ordered = sorted(
        buckets.values(), key=lambda item: (-item.weak_count, -item.total, item.category)
    )
    report.rows = [bucket.to_row() for bucket in ordered[: max(0, top)]]

    with_weakness = [row for row in report.rows if row.weak_count]
    if with_weakness:
        first = with_weakness[0]
        report.headline = (
            f"最该补的是「{first.category}」：{first.total} 次里 {first.weak_count} 次没答上来"
            f"（真实被问 {first.recorded_weak}/{first.recorded_total}）"
        )
    elif report.total_questions:
        report.headline = "目前没有标记为「没答上来」的题 —— 记题时用 --weak 标一下，这里才有信号"
    else:
        report.headline = "还没有任何面试题记录（jh question add / jh question predict）"
    return report

"""状态与枚举常量（单一事实来源）。

为什么不用 Python 的 `enum.StrEnum`：
    · 存进 SQLite 时，StrEnum 也能落成字符串，但 SQLAlchemy 会想用原生 ENUM 类型，
      而 SQLite 的 ENUM 实际是 VARCHAR + CHECK 约束 → 以后想加一个状态就要改表；
    · 本项目是自用工具，状态集合会随实际求职流程微调（比如突然多一个"体检"环节），
      用裸字符串 + 集中常量表最灵活。

代价是没有类型检查保护。所以：
    · 所有写入状态的地方都必须引用这里的常量，不要写字面量；
    · 非法状态用数据库层的 CHECK 约束兜一部分（见各模型的 `__table_args__`，P6 补全）。

★ 状态机的两条重要规则（评审采纳）：
    1. **rejected 可以从任意状态发生** —— 海投里的"感谢信"随时会来；
    2. **offer 可以从 interview 直接跳** —— 小公司一面就发 offer 很常见。
  所以状态流转的校验必须是"白名单允许"，而不是"只能按顺序走一步"。
"""

from __future__ import annotations

__all__ = [
    "APPLICATION_STATUSES",
    "ASSESSMENT_KINDS",
    "ASSESSMENT_STATUSES",
    "CHANNELS",
    "EVENT_TYPES",
    "INTERVIEW_STATUSES",
    "JOB_STATUSES",
    "OFFER_STATUSES",
    "QUESTION_RATINGS",
    "QUESTION_SOURCES",
    "TERMINAL_APPLICATION_STATUSES",
]

# ---------------------------------------------------------------------------
# 岗位状态
# ---------------------------------------------------------------------------
# new      刚入库，还没打分
# scored   已经算过分（规则层或 LLM 层）
# queued   我决定要投，进了待投队列
# applied  已投
# skipped  我主动跳过（不想投 / 不合适）
JOB_STATUSES: tuple[str, ...] = ("new", "scored", "queued", "applied", "skipped")


# ---------------------------------------------------------------------------
# 投递状态机
# ---------------------------------------------------------------------------
# pending    已生成开场白，但还没投出去（等你点投递按钮）
# applied    已投递
# viewed     对方已读
# replied    对方回复了
# assessment 收到笔试/测评/AI面邀请
# interview  约面
# offer      拿到 offer
# rejected   被拒（可能从任意状态发生）
APPLICATION_STATUSES: tuple[str, ...] = (
    "pending",
    "applied",
    "viewed",
    "replied",
    "assessment",
    "interview",
    "offer",
    "rejected",
)

# 终态：进了这两个状态之后，跟进提醒就不该再骚扰我
TERMINAL_APPLICATION_STATUSES: tuple[str, ...] = ("offer", "rejected")


# ---------------------------------------------------------------------------
# 事件流类型（append-only）
# ---------------------------------------------------------------------------
# status_change      状态流转（最主要的类型）
# greeting_sent      开场白真的发出去了（闭环：greeting_sent=True）
# greeting_edited    发之前我改过（衡量生成质量的信号）
# followup           我主动追问了一次
# note               随手记一笔
# assessment_invite  收到笔试邀请
# interview_scheduled 约了面试
# offer_received     收到 offer
EVENT_TYPES: tuple[str, ...] = (
    "status_change",
    "greeting_sent",
    "greeting_edited",
    "followup",
    "note",
    "assessment_invite",
    "interview_scheduled",
    "offer_received",
)


# ---------------------------------------------------------------------------
# 面试场次状态
# ---------------------------------------------------------------------------
# 待面   约好了，还没面（顶部"本周面试"提醒只认这一种）
# 已面   面完了，等结果（`jh question add` 通常在这之后记题）
# 出结果 有结论了（进/不进都可以是"出结果"，具体结论看投递状态）
INTERVIEW_STATUSES: tuple[str, ...] = ("待面", "已面", "出结果")


# ---------------------------------------------------------------------------
# 投递渠道（转化率统计的口径来源）
# ---------------------------------------------------------------------------
# 招聘网站    BOSS/拉勾/智联等平台的站内投递
# 官网       公司官网招聘页直接投
# 内推        有人帮忙递简历
# HR直招      HR 直接联系我
CHANNELS: tuple[str, ...] = ("招聘网站", "官网", "内推", "HR直招")


# ---------------------------------------------------------------------------
# 笔试 / 测评
# ---------------------------------------------------------------------------
# 海投必遇的三种：在线笔试、性格/能力测评、AI 面试
ASSESSMENT_KINDS: tuple[str, ...] = ("笔试", "测评", "AI面")
ASSESSMENT_STATUSES: tuple[str, ...] = ("待做", "已做", "已过期")


# ---------------------------------------------------------------------------
# Offer
# ---------------------------------------------------------------------------
OFFER_STATUSES: tuple[str, ...] = ("待定", "已接受", "已拒绝")


# ---------------------------------------------------------------------------
# 面试问题
# ---------------------------------------------------------------------------
# ★ source 必须区分"我真实被问的"和"我拿 JD 猜的"：
#   否则 `jh weak` 的薄弱点结论会被自己猜的题污染，完全失真。
QUESTION_SOURCES: tuple[str, ...] = ("recorded", "predicted")
# 自评只分 3 档：样本量小的时候 5 档全是噪声（评审采纳）
QUESTION_RATINGS: tuple[str, ...] = ("bad", "ok", "good")

"""共享中间层：JD ↔ 简历 的**规则层**匹配（P3，本项目核心）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 为什么叫"共享中间层"而不是"匹配模块"                                        ║
╚══════════════════════════════════════════════════════════════════════════╝

它算出来的东西**不止匹配用**：

    jd_keywords      → ① 匹配打分  ② P5 定制简历（按 JD 关键词改写）  ③ P4 开场白
    direction        → 匹配 + 简历拆分 + 待投清单分组
    rule_detail      → `jh match explain`（调权重靠它）
    技能归一化        → `jh gap` 判断"这个技能我到底有没有"

所以这些结果**只算一次就落库**（`jobs.jd_keywords_json` / `rule_detail_json`），
后面所有阶段都读它，不重复解析。这是全项目"最贵的一次计算"。

═══════════════════════════════════════════════════════════════════════════
打分公式（0 成本的规则层；LLM 层只对 Top N 精评）
═══════════════════════════════════════════════════════════════════════════

三个维度，权重放 config（默认 30 / 30 / 40）：

    ① 方向  30%   JD 的方向 vs 简历的方向（方向错了，分再高也白投）
    ② 硬条件 30%   经验年限 / 学历 / 城市 / 实习还是全职
    ③ 技能  40%   JD 抽到的技能里，我简历里有几个

    rule_score = 100 × Σ(ratio_i × weight_i) / Σ(weight_i)      （i 只取"判得出来"的维度）

★★ 两条最容易写错、也最重要的规则

    **规则一：硬条件不满足是「大幅降分」而不是「直接淘汰」。**

        项目计划书的原话。原因是真实的：应届生投"3 年以下"的岗位很常见，
        很多 JD 写"3-5 年"其实也收应届。如果直接淘汰，用户会看到
        "这个岗位根本没出现在清单里"，而他永远不会知道是被规则干掉的 ——
        这是最糟的失败方式（静默丢弃机会）。
        所以：不满足 → ratio 压到 0.2~0.4，分数难看但仍排在清单里，
        人自己一眼扫过去就能决定要不要投。

    **规则二：判不出来的维度，把它的权重「让给」其他维度，而不是给 0 分。**

        例：JD 是一段很短的文本，一个技能词都没抽到 → 技能维度无法判断。
        · 若给 0 分：这个岗位总分会莫名其妙地低 40 分，用户完全不知道为什么；
        · 若按中性 0.5 给分：等于凭空编了一个"技能匹配一半"的结论；
        · 正确做法（本实现）：把它从分母里拿掉，权重按比例分给其余维度。
          这样"我确实能判断的两件事"决定了分数，且 `rule_detail` 里
          写明 `redistributed: true`，`jh match explain` 会把它显示出来。

★ 技能归一化：**复用 P1 的成果，绝不重写一套**
  （`resume_svc.load_skill_vocabulary` / `_alias_pattern` / `_match_any`）
  Java ≠ JavaScript、Go ≠ Google、中文别名的边界问题，P1 已经踩完了。

★ 本模块**不碰数据库之外的任何外部世界**：不联网、不调 LLM。
  LLM 精评在 `services/match_llm_svc.py`，它复用这里算好的 rule_score 排序。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunter.adapters.parse_text import (
    extract_city,
    extract_education,
    extract_experience,
    looks_like_intern,
    normalize_text,
)
from jobhunter.config.settings import AppSettings, get_settings
from jobhunter.models import Job, Resume
from jobhunter.services import resume_svc
from jobhunter.services.resume_svc import DirectionProfile, ResumeContent
from jobhunter.utils.io import from_json, to_json
from jobhunter.utils.logging import get_logger
from jobhunter.utils.time import now_utc, to_local

logger = get_logger(__name__)

__all__ = [
    "HardRequirements",
    "MatchResult",
    "ResumeCandidate",
    "analyze_gaps",
    "apply_llm_to_job",
    "apply_result_to_job",
    "decode_keywords",
    "detect_direction",
    "explain_lines",
    "extract_jd_keywords",
    "load_resume_candidates",
    "match_best_candidate",
    "match_jd_resume",
    "resume_skill_pool",
    "rule_detail_summary",
    "stored_skill_overlap",
]

#: rule_detail 的结构版本号。**改了结构就 +1** ——
#: 老库里存的明细要能识别出来，否则 explain 会 KeyError。
RULE_DETAIL_VERSION = 1

#: 判不出来时的处理：ratio=None（从分母里拿掉），不用中性分糊弄。
UNKNOWN = None

# ---- 各维度的 ratio 取值（每一个都是"有理由的具体数字"，不是随手拍的）-----
#: 硬条件不满足时的得分率。
#: 取 0.35 而不是 0：应届生投"3 年经验"的岗位仍然常见，
#: 0 分会让这类岗位直接沉底，等于变相淘汰（违反"降分不淘汰"的纪律）。
HARD_MISS_RATIO = 0.35
#: 硬条件严重冲突（如 config 只要实习、JD 是全职）时的得分率。
#: 比普通不满足更狠，因为这属于"路线不对"，不是"资历差一点"。
HARD_CONFLICT_RATIO = 0.2
#: 方向判不出来（JD 和简历都没给出方向信号）→ 中性偏好一点。
#: 取 0.6 而不是 0.5：无法判断时不该惩罚岗位（它可能是个好岗位，只是词表没覆盖）。
DIRECTION_UNKNOWN_RATIO = 0.6
#: 方向不同、但简历覆盖了对方方向一半以上的核心技能 → 可以跨投。
DIRECTION_CROSS_RATIO = 0.55
#: 方向不同且技能也对不上 → 基本白投，但不给 0（万一是我们判断错了）。
DIRECTION_MISMATCH_RATIO = 0.15
#: 城市不在偏好列表里 → 降分但保留（异地机会值不值得投由人判断）。
CITY_OTHER_RATIO = 0.4

#: 技能维度的"证据下限"：JD 抽出的关键词少于这个数时，按这个数当分母。
#: 详见 `score_skill` 的 docstring（P3 验收时实测出的"JD 写得越少分越高"问题）。
_SKILL_EVIDENCE_FLOOR = 5

#: 学历等级从低到高（用于比较"我够不够"）。
#: 注意 `_DEGREE_ORDER`（parse_text 里的）是从高到低的，这里刻意反过来写一遍 ——
#: 比较大小需要"越小越低"的语义，直接复用那个顺序会让 `>=` 的含义变得难读。
_DEGREE_RANK: dict[str, int] = {
    "高中": 1,
    "中专": 2,
    "专科": 3,
    "大专": 3,
    "学士": 4,
    "本科": 4,
    "研究生": 5,
    "硕士": 5,
    "博士": 6,
}


# ===========================================================================
# 数据结构
# ===========================================================================
@dataclass
class HardRequirements:
    """JD 里的硬条件（原文保留，不做过度解析）。

    ★ 为什么四项都存**原文**（`"3-5年"` 而不是 `3`）：
      见 `parse_text.extract_experience` 的说明 —— `经验不限`、`应届生`、
      `3年以上` 的语义差别很大，硬压成数字会丢信息。
      判别逻辑放在 `_check_experience()` 里做，那里可以写清楚每一档的理由。
    """

    experience: str | None = None
    education: str | None = None
    city: str | None = None
    #: True 实习 / False 全职 / None 判不出来（**绝不当成 False**）
    is_intern: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "experience": self.experience,
            "education": self.education,
            "city": self.city,
            "is_intern": self.is_intern,
        }

    @classmethod
    def from_job(cls, job: Job | None, jd_text: str | None) -> HardRequirements:
        """从岗位记录（+ JD 正文兜底）里取硬条件。

        为什么还需要 `jd_text` 兜底：`jobs.experience` 是 P2 抓取时抽的，
        抓取时可能没抽到（页面结构变了 / 手动粘贴的文本没写字段行），
        但 JD 正文里往往写着"3 年以上经验"。这里补抽一次，成本几乎为零。
        """
        text = jd_text or ""
        job_exp = getattr(job, "experience", None) if job is not None else None
        job_edu = getattr(job, "education", None) if job is not None else None
        job_city = getattr(job, "city", None) if job is not None else None
        job_intern = getattr(job, "is_intern", None) if job is not None else None

        # ★ 只从**正文前部**补抽经验/学历：JD 后半段常有
        #   "有 3 年以上经验者优先"这类与硬门槛无关的话，
        #   整段扫会把"加分项"误当成"硬要求"。
        head = normalize_text(text)[:800]
        return cls(
            experience=job_exp or extract_experience(head),
            education=job_edu or extract_education(head),
            city=job_city or extract_city(head),
            is_intern=job_intern if job_intern is not None else looks_like_intern(text),
        )


@dataclass
class DimensionScore:
    """一个维度的得分明细（`jh match explain` 直接渲染它）。"""

    name: str
    #: 0~1 的得分率；**None = 这个维度判不出来**（见模块 docstring 的规则二）
    ratio: float | None
    #: config 里配的原始权重
    weight: int
    #: 权重再分配后**实际**用的权重（判不出来的维度会被拿掉）
    effective_weight: float = 0.0
    #: 折算后的分数（0~effective_weight）
    points: float = 0.0
    #: 一句话解释"为什么是这个分"（给人看的，会进 rule_detail）
    note: str = ""
    #: 该维度的附加信息（如技能维度的 matched/missing 列表）
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def judged(self) -> bool:
        return self.ratio is not None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ratio": None if self.ratio is None else round(self.ratio, 4),
            "weight": self.weight,
            "effective_weight": round(self.effective_weight, 2),
            "points": round(self.points, 2),
            "note": self.note,
        }
        data.update(self.extra)
        return data


@dataclass
class MatchResult:
    """`match_jd_resume()` 的返回值（= 计划书里约定的共享层契约）。"""

    jd_keywords: list[str] = field(default_factory=list)
    direction: str = ""
    hard_requirements: HardRequirements = field(default_factory=HardRequirements)
    #: {"matched": [...], "missing": [...]}
    skill_overlap: dict[str, list[str]] = field(default_factory=dict)
    rule_score: int = 0
    #: ★ 分维度明细：`{"direction": 30.0, "hard": 21.0, "skill": 24.0, "total": 75, ...}`
    #:   顶层三个数字是"折算后的分数"，`parts` 里是每个维度的完整明细。
    rule_detail: dict[str, Any] = field(default_factory=dict)
    matched_resume_id: int | None = None
    #: 简历的名字与方向（只为了打印好看，不参与打分）
    resume_name: str = ""
    resume_direction: str = ""
    #: 命中的维度列表（判得出来的维度名）
    judged_dimensions: list[str] = field(default_factory=list)

    @property
    def missing_keywords(self) -> list[str]:
        """技能维度的缺口（`jh gap` 与 explain 都用它）。"""
        return list(self.skill_overlap.get("missing") or [])

    @property
    def matched_keywords(self) -> list[str]:
        return list(self.skill_overlap.get("matched") or [])

    def to_dict(self) -> dict[str, Any]:
        return {
            "jd_keywords": self.jd_keywords,
            "direction": self.direction,
            "hard_requirements": self.hard_requirements.to_dict(),
            "skill_overlap": self.skill_overlap,
            "rule_score": self.rule_score,
            "rule_detail": self.rule_detail,
            "matched_resume_id": self.matched_resume_id,
        }


@dataclass
class ResumeCandidate:
    """一份"准备好被打分"的简历（解析后的内容 + 预计算的技能集）。

    ★ 为什么要单独一个结构、而不是每次现算：
      `jh match run` 要给 30~100 个岗位打分，如果每个岗位都把简历重新
      解析一遍、把 200 个技能别名正则重新编译一遍，会白白慢好几倍。
      这里把"与岗位无关"的部分（技能全集、方向）算一次，反复使用。
    """

    resume_id: int | None
    name: str
    direction: str
    content: ResumeContent
    #: `resumes.content_hash`。**LLM 缓存 key 的必要成分**（改了简历必须重算），
    #: 所以它必须跟着候选人一起传，而不是每次再去查库。
    content_hash: str = ""
    #: 简历里出现过的全部技能（含只在项目描述里提到的）
    pool: set[str] = field(default_factory=set)
    #: 只在"专业技能"栏主动列出的技能（可信度更高，explain 会区分展示）
    explicit: set[str] = field(default_factory=set)
    #: 最晚一段教育经历的结束年份（判"是不是应届生"用；None = 判不出来）
    graduation_year: int | None = None
    #: 最高学历（本科 / 硕士…；None = 判不出来）
    highest_degree: str = ""


# ===========================================================================
# ① JD 关键词抽取（词表优先，LLM 抽取默认关）
# ===========================================================================
def extract_jd_keywords(
    jd_text: str | None,
    settings: AppSettings | None = None,
    *,
    vocabulary: dict[str, list[str]] | None = None,
) -> list[str]:
    """从 JD 正文里抽出规范化后的技能关键词。

    ★ 为什么"词表优先、LLM 抽取默认关"（计划书原话，这里说清取舍）：

        词表法 的代价是**漏**：JD 里写的新框架（比如某个刚出的 Agent 框架）
               词表没有 → 抽不到 → 技能维度少算一项。
        LLM 法 的代价是**贵 + 不稳定**：每个岗位多一次调用，
               同样的 JD 两次跑可能抽出不同的词，分数就跟着飘。
        对一个"要反复跑、要对分数有信心"的工具，
        **确定性 > 召回率**。漏掉的词会在 `jh gap` 里表现为"这个词没被统计"，
        用户可以往 `config.yaml` 的 `skill_aliases` 里加一行补上 —— 这是可控的。

    顺序：按**词表顺序**输出（不是按 JD 里出现的先后）——
    这样同一份 JD 重复抽取结果完全一致，rule_score 才稳定、缓存才有意义。
    """
    settings = settings or get_settings()
    if not jd_text or not jd_text.strip():
        return []
    vocabulary = vocabulary or resume_svc.load_skill_vocabulary(settings)
    return [name for name, aliases in vocabulary.items() if resume_svc._match_any(jd_text, aliases)]


# ===========================================================================
# ② 方向判定
# ===========================================================================
def detect_direction(
    text: str | None,
    settings: AppSettings | None = None,
    *,
    profiles: dict[str, DirectionProfile] | None = None,
) -> tuple[str, dict[str, int]]:
    """判断一段文字（JD 或简历）属于哪个方向。

    Returns:
        (方向 label；判不出来是空串, 各方向命中数)

    ★ 打分口径（为什么标题词和正文词权重不同）：
        · `title_hints`（"AI""前端""后端"这类**标题词**）命中一次算 **3 分**
          —— 岗位标题是"这个岗位要什么人"最直接的声明；
        · `aliases`（"智能体""大模型应用"）命中一次算 **1 分**
          —— 出现在正文里的方向词噪音大（"与前端同学协作"也会命中"前端"）。
      平局时按画像在 yaml 里的顺序取第一个（顺序稳定 = 结果可复现）。

    ★ 为什么返回命中数而不是只返回结论：
      `jh match explain` 要能回答"你凭什么说这是 AI Agent 方向"。
      只看结论没法排查"方向判错了"这类问题。
    """
    settings = settings or get_settings()
    profiles = profiles if profiles is not None else resume_svc.load_direction_profiles(settings)
    if not text or not text.strip() or not profiles:
        return "", {}

    hits: dict[str, int] = {}
    for key, profile in profiles.items():
        score = 0
        for hint in profile.title_hints:
            if hint and resume_svc._match_any(text, [hint]):
                score += 3
        for alias in profile.aliases:
            if alias and resume_svc._match_any(text, [alias]):
                score += 1
        if score:
            hits[profile.label or key] = score

    if not hits:
        return "", {}
    # max 在同分时返回**第一个**遇到的 —— dict 保持 yaml 里的顺序，结果可复现
    best_label = max(hits, key=lambda label: hits[label])
    return best_label, hits


# ===========================================================================
# ③ 简历侧：技能全集、学历、毕业年份
# ===========================================================================
def resume_skill_pool(content: ResumeContent) -> tuple[set[str], set[str]]:
    """取出简历的技能全集。

    Returns:
        (pool, explicit)
        pool     —— 出现在任何地方（技能栏 / 项目描述 / 实习描述）的技能
        explicit —— 只在"专业技能"栏主动列出的技能

    ★ 为什么**不**把 `raw_text` 也算进来：
      `raw_text` 是整份简历的原文兜底，包含"我在学 LangChain"这种句子。
      把它算进技能池，等于**声称自己会**一个还在学的技能 ——
      匹配分虚高，而这恰恰是这个工具最不该犯的错（骗自己）。
      结构化字段（skills / projects[].skills）是 P1 解析出来的"我确实写过"的部分。

    ★ 为什么 `pool` 要包含 `explicit`：
      P1 的解析会把"项目描述里提到的技能"填进 `project.skills`，
      但技能栏里写了、项目里没用的技能只出现在 `content.skills`。
      两者取并集才是"我会什么"的完整答案。
    """
    explicit = set(content.skills)
    pool = set(explicit)
    for item in content.projects:
        pool.update(item.skills)
    for item in content.internships:
        pool.update(item.skills)
    return pool, explicit


def _highest_degree(content: ResumeContent) -> str:
    """简历里的最高学历（取学历等级最高的那一段）。判不出来返回空串。"""
    best = ""
    best_rank = 0
    for item in content.education:
        degree = item.degree or _extract_degree_from_text(item.raw_line or "")
        rank = _DEGREE_RANK.get(degree, 0)
        if rank > best_rank:
            best, best_rank = degree, rank
    return best


def _extract_degree_from_text(text: str) -> str:
    """从一行文字里抠学历（复用 P2 的抽取规则，避免第二套口径）。"""
    return extract_education(text) or ""


def _graduation_year(content: ResumeContent) -> int | None:
    """最晚一段教育经历的结束年份（用来判断"我是不是应届生"）。

    ★ 为什么敢用"结束年份"来判应届：
      应届生（2027 届）的简历上，教育经历的结束时间必然是**未来**的年份
      （2027.06）。已经毕业的人写的是过去的年份。
      这个推断不完美（延毕、gap 会打脸），但它只用于**给硬条件降分**，
      不会淘汰任何岗位 —— 判断错的代价很小，所以值得用。
    """
    years: list[int] = []
    for item in content.education:
        end = resume_svc.education_end_year(item.period)
        if end is not None:
            years.append(end)
    return max(years) if years else None


def _is_fresh_graduate(candidate: ResumeCandidate) -> bool | None:
    """是不是应届生。None = 判不出来（**绝不当成 False**）。

    ★ 为什么按**展示时区**（北京时间）取"今年"：
      跨年那几个小时里，UTC 还在去年 —— 对"2027 届算不算应届"这种判断，
      时区差一年会得出完全相反的结论。全项目的"今天"都以北京时间为准
      （见 utils/time.py 的 DISPLAY_TZ）。
    """
    if candidate.graduation_year is None:
        return None
    current_year = to_local(now_utc()).year
    return candidate.graduation_year >= current_year


# ===========================================================================
# ④ 三个维度各自的打分
# ===========================================================================
def _core_coverage(candidate: ResumeCandidate, profile: DirectionProfile | None) -> float:
    """简历对某个方向**核心技能**的覆盖率（0~1）。判不出来返回 0。"""
    if profile is None or not profile.core_skills:
        return 0.0
    hit = sum(1 for skill in profile.core_skills if skill in candidate.pool)
    return hit / len(profile.core_skills)


def score_direction(
    jd_direction: str,
    candidate: ResumeCandidate,
    settings: AppSettings,
    *,
    profiles: dict[str, DirectionProfile],
) -> DimensionScore:
    """维度①：岗位方向（默认权重 30）。

    判定表（每一档都有理由，不要随手改数字）：

        JD 方向判不出来         → 0.6  中性偏好：不该因为词表没覆盖而惩罚岗位
        与简历方向一致          → 1.0  最理想
        方向不同但技能对得上     → 0.55 可跨投（例：简历是 AI Agent，
                                      但投"大模型应用"岗，核心技能高度重叠）
        方向不同且技能也对不上   → 0.15 基本白投，但**不淘汰**
        简历方向为空            → 退化为"核心技能覆盖率"决定：
                                 0.3 + 0.7×覆盖率（范围 0.3~1.0）
    """
    weight = settings.match_weights.direction
    profile = _profile_by_label(profiles, jd_direction)

    if not jd_direction:
        return DimensionScore(
            name="direction",
            ratio=DIRECTION_UNKNOWN_RATIO,
            weight=weight,
            note="JD 里没有出现任何方向词（词表没覆盖到），这一维度只能给中性分",
        )

    resume_direction = candidate.direction or ""
    coverage = _core_coverage(candidate, profile)

    if not resume_direction:
        ratio = 0.3 + 0.7 * coverage
        note = f"简历没写方向，按它在「{jd_direction}」核心技能上的覆盖率判定：{coverage:.0%}"
    elif resume_direction == jd_direction:
        ratio = 1.0
        note = f"JD 与简历方向一致（{jd_direction}）"
    elif coverage >= 0.5:
        ratio = DIRECTION_CROSS_RATIO
        note = (
            f"JD 是「{jd_direction}」、简历是「{resume_direction}」，"
            f"但简历覆盖了该方向 {coverage:.0%} 的核心技能 → 可以跨投"
        )
    else:
        ratio = DIRECTION_MISMATCH_RATIO
        note = (
            f"JD 是「{jd_direction}」、简历是「{resume_direction}」，"
            f"且核心技能只覆盖 {coverage:.0%} → 投了大概率白投"
        )

    return DimensionScore(
        name="direction",
        ratio=ratio,
        weight=weight,
        note=note,
        extra={
            "jd_direction": jd_direction,
            "resume_direction": resume_direction,
            "core_coverage": round(coverage, 3),
        },
    )


def _profile_by_label(profiles: dict[str, DirectionProfile], label: str) -> DirectionProfile | None:
    """按 label 找画像（找不到就按 key 找，再找不到返回 None）。"""
    if not label:
        return None
    for key, profile in profiles.items():
        if label in (profile.label, key):
            return profile
    return None


def score_hard(
    requirements: HardRequirements,
    candidate: ResumeCandidate,
    settings: AppSettings,
) -> DimensionScore:
    """维度②：硬条件（默认权重 30）。

    四个子项**等权平均**，判不出来的子项不计入分母。

    ★ 为什么等权而不是"经验权重更高"：
      这四个是"要么满足要么不满足"的门槛，没有理由说哪个更重要；
      人为给不同权重只会让 explain 里多出一堆解释不清的数字。
      真正的调节手段是"不满足时压到多低"（HARD_MISS_RATIO），
      那个数字在 explain 里会直接显示出来。

    ★ 实习/全职冲突单独处理得更狠（0.2）：
      config 里写 `job_type: intern` 表示"我只要实习"，
      这时一个全职岗不是"资历差一点"，而是**路线不对**。
    """
    weight = settings.match_weights.hard
    checks: list[tuple[str, float | None, str]] = [
        _check_experience(requirements.experience, candidate),
        _check_education(requirements.education, candidate),
        _check_city(requirements.city, settings),
        _check_job_type(requirements.is_intern, settings),
    ]
    judged = [(name, ratio, note) for name, ratio, note in checks if ratio is not None]
    if not judged:
        return DimensionScore(
            name="hard",
            ratio=UNKNOWN,
            weight=weight,
            note="JD 没写经验/学历/城市/实习与否，硬条件这一维度判不出来",
            extra={"checks": [{"name": n, "ratio": None, "note": note} for n, _, note in checks]},
        )

    ratio = sum(item[1] for item in judged) / len(judged)  # type: ignore[misc]
    missed = [name for name, value, _ in judged if value is not None and value < 0.6]
    if missed:
        note = "不满足：" + "、".join(missed) + "（降分但不淘汰，自己看一眼再决定）"
    else:
        note = "经验/学历/城市/实习四项都满足"

    return DimensionScore(
        name="hard",
        ratio=ratio,
        weight=weight,
        note=note,
        extra={
            "checks": [
                {"name": name, "ratio": None if value is None else round(value, 3), "note": text}
                for name, value, text in checks
            ],
            "missed": missed,
        },
    )


#: 经验门槛的"阶梯表"：`(年限上限, 得分率, 一句理由)`，从上往下第一个满足的生效。
#: ★ 为什么用表而不是一串 if/elif：这张表就是**判断标准本身**，
#:   平铺出来才能一眼看全"3 年和 5 年分别怎么算"，改的时候也不容易漏改一档。
#:   针对的是"2027 届应届生"这个真实场景（计划书里的目标用户）。
_EXPERIENCE_LADDER: tuple[tuple[int, float, str], ...] = (
    (1, 1.0, "1 年以内，实习经历够用"),
    (2, 0.7, "差得不多，可以试"),
    (3, 0.55, "应届偏紧，但很多公司会放宽"),
    (4, HARD_MISS_RATIO, "应届基本够不着（降分保留，不淘汰）"),
)
#: 超出阶梯表最后一档（4 年以上）时的兜底。
_EXPERIENCE_FAR: tuple[float, str] = (0.2, "差距较大（降分保留，自己判断）")


def _check_experience(
    required: str | None, candidate: ResumeCandidate
) -> tuple[str, float | None, str]:
    """经验年限：JD 要求 vs 我是不是应届生。

    ★ 判不出来（JD 没写经验，或简历里读不出毕业年份）→ None，**不计入分母**。
    """
    if not required:
        return ("经验", None, "JD 没写经验要求")
    text = required.replace(" ", "")

    if _is_fresh_graduate(candidate) is None:
        return ("经验", None, "简历里读不出毕业年份，无法判断是否应届")

    if any(word in text for word in ("不限", "无经验", "无需经验", "应届", "在校")):
        return ("经验", 1.0, f"JD 写「{required}」，明确要应届/不限经验")

    years = _years_in(text)
    if years is None:
        return ("经验", None, f"JD 写「{required}」，但读不出具体年限")

    for ceiling, ratio, why in _EXPERIENCE_LADDER:
        if years <= ceiling:
            return ("经验", ratio, f"JD 要求「{required}」，{why}")
    ratio, why = _EXPERIENCE_FAR
    return ("经验", ratio, f"JD 要求「{required}」，{why}")


def _years_in(text: str) -> int | None:
    """从 `3-5年` / `3年以上` / `5年` 里取**最小**的年数作为门槛。

    ★ 为什么取最小（一开始写成最大，被测试打脸）：
      "3-5年" 的岗位通常 3 年经验的人就能进，取 5 会把门槛凭空抬高一档；
      而 "5年以上" 只有一个数字，取最小就是 5 —— 两者自然区分开了。
    ★ 区间要单独先匹配：`(\\d+)\\s*年` 在 "3-5年" 上只会匹配到 "5年"，
      于是 3 被漏掉、门槛被算成 5（这就是上面那个 bug 的成因）。
    """
    span = re.search(r"(\d+)\s*[-~—–至到]\s*(\d+)\s*年", text)
    if span:
        return int(span.group(1))
    found = [int(value) for value in re.findall(r"(\d+)\s*年", text)]
    return min(found) if found else None


def _check_education(
    required: str | None, candidate: ResumeCandidate
) -> tuple[str, float | None, str]:
    """学历：JD 要求 vs 简历最高学历。"""
    if not required:
        return ("学历", None, "JD 没写学历要求")
    if not candidate.highest_degree:
        return ("学历", None, "简历里读不出学历")

    need_rank = _degree_rank(required)
    have_rank = _degree_rank(candidate.highest_degree)
    if need_rank == 0 or have_rank == 0:
        mine = candidate.highest_degree
        return ("学历", None, f"学历写法认不出来（JD「{required}」/简历「{mine}」）")
    if have_rank >= need_rank:
        return ("学历", 1.0, f"JD 要「{required}」，我是{candidate.highest_degree}，满足")
    return (
        "学历",
        HARD_MISS_RATIO,
        f"JD 要「{required}」，我是{candidate.highest_degree}，差一档（降分保留）",
    )


def _degree_rank(text: str) -> int:
    """取一段文字里学历的最高等级（认不出来返回 0）。"""
    best = 0
    for word, rank in _DEGREE_RANK.items():
        if word in text:
            best = max(best, rank)
    return best


def _check_city(city: str | None, settings: AppSettings) -> tuple[str, float | None, str]:
    """城市：JD 城市 vs config 的 `cities` 偏好列表。

    ★ 这一项和"经验/学历"的性质不同：它**不是**能力门槛，而是偏好。
      所以不满足时只降到 0.4（异地机会完全值得看一眼：可能愿意搬，也可能是远程）。
      `cities` 没配 → 判不出来（None），不计入分母。
    """
    if not city:
        return ("城市", None, "JD 没写城市")
    if not settings.cities:
        return ("城市", None, "config 里没配 cities，无法判断城市偏好")
    if city in settings.cities:
        return ("城市", 1.0, f"{city} 在你的意向城市里")
    return ("城市", CITY_OTHER_RATIO, f"{city} 不在意向城市（{'/'.join(settings.cities)}）里")


def _check_job_type(is_intern: bool | None, settings: AppSettings) -> tuple[str, float | None, str]:
    """实习还是全职：JD 的 `is_intern` vs config 的 `job_type`。"""
    if is_intern is None:
        return ("实习/全职", None, "从 JD 里看不出是实习还是全职")
    if settings.job_type == "both":
        return ("实习/全职", 1.0, "实习和全职你都要（job_type=both）")

    want_intern = settings.job_type == "intern"
    if want_intern == is_intern:
        kind = "实习" if is_intern else "全职"
        return ("实习/全职", 1.0, f"这是{kind}岗，符合 job_type={settings.job_type}")

    kind = "实习" if is_intern else "全职"
    return (
        "实习/全职",
        HARD_CONFLICT_RATIO,
        f"这是{kind}岗，但 config 里 job_type={settings.job_type}（路线不符，重降分）",
    )


def score_skill(
    jd_keywords: list[str],
    candidate: ResumeCandidate,
    settings: AppSettings,
    *,
    profiles: dict[str, DirectionProfile],
    jd_direction: str,
) -> DimensionScore:
    """维度③：技能交集（默认权重 40）。

    公式：`ratio = 命中的 JD 关键词数 / max(JD 关键词总数, 证据下限)`

    ★ 为什么是"覆盖率"而不是"命中数"：
      命中 5 个技能，在一个只要求 5 个技能的 JD 里是完美匹配，
      在一个要求 15 个技能的 JD 里只能算三分之一。
      用命中数会让"要求写得多的岗位"天然占便宜，这是错的。

    ★ 为什么要"证据下限"（`_SKILL_EVIDENCE_FLOOR`）—— 这是 P3 验收时实测出来的：

        20 个岗位跑完，一个"测试开发工程师"排到了第 3 名（86 分），
        比核心的"AI Agent 开发工程师"（80 分）还高。查下来原因是：
        那个 JD 只抽出 **1 个**技能词（Python），命中 1 个 → 覆盖率 100% → 技能拿满分 40。
        也就是说 **JD 写得越少越容易拿高分**，这显然是错的。
        真相是：只抽出 1 个词，说明我们对这个岗位的了解非常有限，
        这时候给满分是"过度自信"。用 5 作为分母下限，
        等于说"少于 5 项要求的岗位，我按 5 项来算覆盖"。

    ★ 为什么不做"核心技能加权"（本可以实现，但**故意不做**）：
      加权之后，`jh match explain` 里就再也说不清"为什么是 62 分" ——
      用户看到一个数字，却无法用一句话验证它。对一个需要**反复调参**的工具，
      "公式能口算"比"公式更准"重要。核心技能的命中情况仍然会展示出来，
      只是不参与算分。
    """
    weight = settings.match_weights.skill
    profile = _profile_by_label(profiles, jd_direction)
    core = set(profile.core_skills) if profile else set()

    if not jd_keywords:
        return DimensionScore(
            name="skill",
            ratio=UNKNOWN,
            weight=weight,
            note="JD 里一个词表内的技能都没抽到（JD 太短或词表没覆盖），这一维度判不出来",
            extra={"matched": [], "missing": [], "core_matched": [], "core_missing": []},
        )

    matched = [word for word in jd_keywords if word in candidate.pool]
    missing = [word for word in jd_keywords if word not in candidate.pool]
    coverage = len(matched) / len(jd_keywords)
    # 证据不足时压低：分母取 max(关键词数, 下限)
    denominator = max(len(jd_keywords), _SKILL_EVIDENCE_FLOOR)
    ratio = len(matched) / denominator

    core_matched = [word for word in matched if word in core]
    core_missing = [word for word in missing if word in core]
    note = f"JD 要求 {len(jd_keywords)} 项技能，简历命中 {len(matched)} 项"
    if denominator != len(jd_keywords):
        note += (
            f"；但这个 JD 只抽出 {len(jd_keywords)} 项技能，说明我们对它的了解很有限，"
            f"按 {denominator} 项计（避免「要求写得少」的岗位白拿满分）"
        )
    if core_missing:
        note += f"；其中核心技能缺：{'、'.join(core_missing[:5])}"

    return DimensionScore(
        name="skill",
        ratio=ratio,
        weight=weight,
        note=note,
        extra={
            "matched": matched,
            "missing": missing,
            "coverage": round(coverage, 4),
            "core_matched": core_matched,
            "core_missing": core_missing,
        },
    )


# ===========================================================================
# ⑤ 汇总：加权 + 权重再分配
# ===========================================================================
def _combine(dimensions: list[DimensionScore]) -> tuple[int, bool]:
    """把三个维度合成 rule_score，并处理"判不出来的维度"。

    ★★ 权重再分配（模块 docstring 的规则二，这里是最容易写错的地方）

        假设技能维度判不出来（ratio=None），权重是 30/30/40：

            错误做法 A：ratio 当 0 算 → 总分凭空少 40 分，没人知道为什么
            错误做法 B：ratio 当 0.5 算 → 编造了一个"技能匹配一半"的结论
            正确做法  ：把它从分母里拿掉
                        总分 = 100 × (0.83×30 + 0.7×30) / (30 + 30)

        注意分母是**参与打分的权重之和**，不是固定的 100。
        返回的 bool 表示"是否发生过再分配"，会被写进 rule_detail 供 explain 显示。

    ★ 三个维度全都判不出来 → 50 分（中性）+ 明确写在 note 里。
      这不是"随便给一个数"，而是告诉用户"这个岗位我什么都不知道"，
      清单里它排中间，一眼就能看出是"信息不足"而不是"很差"。
    """
    judged = [item for item in dimensions if item.judged]
    if not judged:
        for item in dimensions:
            item.effective_weight = 0.0
            item.points = 0.0
        return 50, True

    total_weight = sum(item.weight for item in judged)
    redistributed = len(judged) != len(dimensions)
    for item in dimensions:
        if not item.judged:
            item.effective_weight = 0.0
            item.points = 0.0
            continue
        item.effective_weight = item.weight * 100 / total_weight
        item.points = item.ratio * item.effective_weight  # type: ignore[operator]

    score = sum(item.points for item in judged)
    # round 后夹到 0~100：浮点误差理论上不会越界，但分数是要落库的，
    # 夹一下比事后解释"为什么库里有个 100.00000001"便宜得多。
    return max(0, min(100, round(score))), redistributed


# ===========================================================================
# ⑥ 对外主函数（计划书约定的共享层契约）
# ===========================================================================
def match_jd_resume(
    jd_text: str | None,
    resume: ResumeContent,
    *,
    resume_id: int | None = None,
    resume_name: str = "",
    resume_direction: str = "",
    job: Job | None = None,
    settings: AppSettings | None = None,
    profiles: dict[str, DirectionProfile] | None = None,
    vocabulary: dict[str, list[str]] | None = None,
) -> MatchResult:
    """**共享层的入口**：给定 JD 与简历，算出规则层匹配结果。

    这个函数是纯函数（不碰数据库、不联网）：给了什么就算什么。
    落库由 `apply_result_to_job()` 负责 —— 分开的好处是测试极好写，
    而且 P5（定制简历）将来可以直接调它做"改完简历分数会不会变高"的预演。
    """
    settings = settings or get_settings()
    profiles = profiles if profiles is not None else resume_svc.load_direction_profiles(settings)
    vocabulary = vocabulary or resume_svc.load_skill_vocabulary(settings)

    candidate = ResumeCandidate(
        resume_id=resume_id,
        name=resume_name,
        direction=resume_direction,
        content=resume,
    )
    pool, explicit = resume_skill_pool(resume)
    candidate.pool, candidate.explicit = pool, explicit
    candidate.highest_degree = _highest_degree(resume)
    candidate.graduation_year = _graduation_year(resume)

    return _evaluate(
        jd_text,
        candidate,
        job=job,
        settings=settings,
        profiles=profiles,
        vocabulary=vocabulary,
    )


def _evaluate(
    jd_text: str | None,
    candidate: ResumeCandidate,
    *,
    job: Job | None,
    settings: AppSettings,
    profiles: dict[str, DirectionProfile],
    vocabulary: dict[str, list[str]],
) -> MatchResult:
    """真正干活的那个（`match_jd_resume` 与批量路径共用）。"""
    text = jd_text or ""
    jd_keywords = extract_jd_keywords(text, settings, vocabulary=vocabulary)

    # 方向判定用"标题 + 正文"：标题里的方向词权重更高（见 detect_direction）
    title = getattr(job, "title", None) if job is not None else None
    jd_direction, _hits = detect_direction(f"{title or ''}\n{text}", settings, profiles=profiles)

    requirements = HardRequirements.from_job(job, text)
    dimensions = [
        score_direction(jd_direction, candidate, settings, profiles=profiles),
        score_hard(requirements, candidate, settings),
        score_skill(
            jd_keywords,
            candidate,
            settings,
            profiles=profiles,
            jd_direction=jd_direction,
        ),
    ]
    rule_score, redistributed = _combine(dimensions)

    skill_dim = next(item for item in dimensions if item.name == "skill")
    matched = list(skill_dim.extra.get("matched") or [])
    missing = list(skill_dim.extra.get("missing") or [])

    detail: dict[str, Any] = {
        # ---- 计划书约定的顶层三个数字（折算后的分数）----
        "direction": next(item.points for item in dimensions if item.name == "direction"),
        "hard": next(item.points for item in dimensions if item.name == "hard"),
        "skill": skill_dim.points,
        "total": rule_score,
        # ---- 明细 ----
        "version": RULE_DETAIL_VERSION,
        "parts": {item.name: item.to_dict() for item in dimensions},
        "weights": settings.match_weights.model_dump(),
        "redistributed": redistributed,
        "jd_direction": jd_direction,
        "resume_direction": candidate.direction,
        "resume_name": candidate.name,
        "notes": _notes(dimensions, redistributed),
    }

    return MatchResult(
        jd_keywords=jd_keywords,
        direction=jd_direction,
        hard_requirements=requirements,
        skill_overlap={"matched": matched, "missing": missing},
        rule_score=rule_score,
        rule_detail=detail,
        matched_resume_id=candidate.resume_id,
        resume_name=candidate.name,
        resume_direction=candidate.direction,
        judged_dimensions=[item.name for item in dimensions if item.judged],
    )


def _notes(dimensions: list[DimensionScore], redistributed: bool) -> list[str]:
    """给 explain 用的一行行说明（**人话**，不是字段名）。"""
    notes = [f"{item.name}：{item.note}" for item in dimensions]
    if redistributed:
        dropped = [item.name for item in dimensions if not item.judged]
        notes.append(
            f"⚠️ 有维度判不出来（{'、'.join(dropped)}），它的权重已按比例分给其余维度 —— "
            "所以这里的分数不能和别的岗位的分维度分数直接横向比"
        )
    return notes


# ===========================================================================
# ⑦ 批量：把库里所有简历装成候选人，给岗位挑最合适的一份
# ===========================================================================
def load_resume_candidates(
    session: Session, settings: AppSettings | None = None
) -> list[ResumeCandidate]:
    """把库里所有简历解析成候选人（每份只解析一次，反复用）。

    ★ 为什么"同一份简历解析一次"很重要：`jh match run` 可能要给上百个岗位打分。
      每份简历的解析 + 技能全集计算是毫秒级，但乘以岗位数就是秒级；
      更要紧的是它让整批打分**建立在同一份快照上** ——
      如果一边跑一边重新读库，可能出现"前 10 个岗位用旧简历、后 10 个用新简历"
      这种无法复现的结果。
    """
    settings = settings or get_settings()
    resumes = list(session.scalars(select(Resume).order_by(Resume.id)))
    candidates: list[ResumeCandidate] = []
    for row in resumes:
        content = resume_svc.content_from_json(row.content_json)
        pool, explicit = resume_skill_pool(content)
        candidates.append(
            ResumeCandidate(
                resume_id=row.id,
                name=row.name or f"简历#{row.id}",
                direction=row.direction or "",
                content=content,
                content_hash=row.content_hash or "",
                pool=pool,
                explicit=explicit,
                graduation_year=_graduation_year(content),
                highest_degree=_highest_degree(content),
            )
        )
    return candidates


def match_best_candidate(
    jd_text: str | None,
    candidates: list[ResumeCandidate],
    *,
    job: Job | None = None,
    settings: AppSettings | None = None,
    profiles: dict[str, DirectionProfile] | None = None,
    vocabulary: dict[str, list[str]] | None = None,
) -> MatchResult | None:
    """在多份简历里挑出对该 JD 匹配最好的那一份。

    ★ 为什么要"挑最好"而不是"用默认简历"：
      用户会为不同方向准备不同的简历（P1 的 `resume split` 就是干这个的）。
      一个岗位该配哪份简历，是**算出来的结论**，不该让人每次手动指定。
      返回的 `matched_resume_id` 会落库，`jh match explain` 里能看到
      "这个岗位推荐用哪份简历"。

    ★ 平局怎么办：保留**先出现**的那份（按 resume id 升序），
      保证同样输入永远得到同样结论（可复现比"更聪明"重要）。
    """
    settings = settings or get_settings()
    profiles = profiles if profiles is not None else resume_svc.load_direction_profiles(settings)
    vocabulary = vocabulary or resume_svc.load_skill_vocabulary(settings)

    best: MatchResult | None = None
    for candidate in candidates:
        result = _evaluate(
            jd_text,
            candidate,
            job=job,
            settings=settings,
            profiles=profiles,
            vocabulary=vocabulary,
        )
        if best is None or result.rule_score > best.rule_score:
            best = result
    return best


# ===========================================================================
# ⑧ 落库
# ===========================================================================
def apply_result_to_job(job: Job, result: MatchResult) -> None:
    """把规则层结果写进岗位记录。

    ★ 写了哪些、为什么：
        jd_keywords_json  共享中间层产物（P4/P5 都要读，只算一次）
        rule_score        规则分
        rule_detail_json  分维度明细（explain 靠它，**不写就没法调权重**）
        matched_resume_id 推荐用哪份简历
        match_score       最终分 = 有 LLM 分就用 LLM 分，否则用规则分
        status            new → scored（**只前进不后退**：
                          已 applied/skipped 的岗位重新打分不该把状态改回去）
    """
    job.jd_keywords_json = to_json(result.jd_keywords)
    job.rule_score = result.rule_score
    job.rule_detail_json = to_json(result.rule_detail)
    job.matched_resume_id = result.matched_resume_id
    job.match_score = job.llm_score if job.llm_score is not None else result.rule_score
    # 只前进不后退：已 applied/skipped 的岗位重新打分不该把状态改回去。
    # 注意 None 也要当成 new —— 刚 new 出来的对象在 flush 前 status 是 None
    # （SQLAlchemy 的 default 只在 INSERT 时生效）。
    if job.status in (None, "new"):
        job.status = "scored"


def apply_llm_to_job(job: Job, *, llm_score: int, reason: dict[str, Any]) -> None:
    """把 LLM 精评结果写进岗位记录。

    ★ `match_score` 的合成规则只有一条：**有 LLM 分就用 LLM 分**。
      为什么不平均（比如 rule 40% + llm 60%）：
      LLM 是"看了完整 JD 与完整简历之后"的判断，规则层只是没有理解能力的
      关键词覆盖率。两者不是同一层信息，平均会把 LLM 的判断稀释掉。
      规则分仍然保留在 `rule_score` 里 —— 它是 LLM 挂掉时的降级值，
      也是调权重时的参照物。
    """
    job.llm_score = llm_score
    job.match_score = llm_score
    job.reason_json = to_json(reason)
    if job.status in (None, "new"):
        job.status = "scored"


# ===========================================================================
# ⑨ explain：把 rule_detail 变成人能看懂的一行行字
# ===========================================================================
def explain_lines(job: Job) -> list[str]:
    """把 `jobs.rule_detail_json` 渲染成多行文本（`jh match explain` 用）。

    ★ 为什么不在这里直接 print：CLI 层负责渲染（见 docs/architecture.md §2）。
      返回 list[str] 让 CLI 决定颜色与排版，测试也能直接断言文本。
    """
    detail = from_json(job.rule_detail_json, default=None)
    if not isinstance(detail, dict):
        return ["这个岗位还没有分维度明细，先跑：jh match run"]

    if detail.get("version") != RULE_DETAIL_VERSION:
        # ★ 宁可提示"明细格式是旧的"，也不要硬渲染出一个错的解释
        return [
            f"分维度明细是旧格式（v{detail.get('version')}），当前是 "
            f"v{RULE_DETAIL_VERSION}。重新跑 `jh match run` 即可。"
        ]

    lines: list[str] = []
    parts = detail.get("parts") or {}
    labels = {"direction": "方向", "hard": "硬条件", "skill": "技能"}
    for key in ("direction", "hard", "skill"):
        part = parts.get(key) or {}
        ratio = part.get("ratio")
        ratio_text = "判不出来" if ratio is None else f"{ratio:.0%}"
        lines.append(
            f"{labels[key]}：{part.get('points', 0):.1f} / {part.get('effective_weight', 0):.0f} 分"
            f"（得分率 {ratio_text}）—— {part.get('note', '')}"
        )
        if key == "skill":
            matched = part.get("matched") or []
            missing = part.get("missing") or []
            if matched:
                lines.append(f"    ✅ 命中：{'、'.join(matched)}")
            if missing:
                lines.append(f"    ❌ 缺口：{'、'.join(missing)}")
    if detail.get("redistributed"):
        lines.append("⚠️ 有权重被再分配（有维度判不出来），详见上面的说明")
    lines.append(f"合计：{detail.get('total')} 分（权重配置：{detail.get('weights')}）")
    if detail.get("resume_name"):
        direction = detail.get("resume_direction") or "未标方向"
        lines.append(f"参照简历：{detail.get('resume_name')}（{direction}）")
    return lines


def rule_detail_summary(job: Job) -> dict[str, float]:
    """只取三个维度的分数（给表格用，避免每行都解析一大坨 JSON）。"""
    detail = from_json(job.rule_detail_json, default=None)
    if not isinstance(detail, dict):
        return {}
    parts = detail.get("parts") or {}
    out: dict[str, float] = {}
    for key in ("direction", "hard", "skill"):
        value = (parts.get(key) or {}).get("points")
        if isinstance(value, int | float):
            out[key] = float(value)
    return out


def stored_skill_overlap(job: Job) -> dict[str, list[str]]:
    """从已落库的 `rule_detail_json` 里取技能命中/缺口。

    ★ 为什么不重新抽一遍 JD 关键词：
      抽词结果就在 `rule_detail_json` 里（`parts.skill`），重抽既浪费，
      又可能与 explain 显示的内容不一致（比如 JD 刚被重新抓取过）。
      LLM 提示词里的"锚点"必须和用户看到的数字**同源**。
    """
    detail = from_json(job.rule_detail_json, default=None)
    if not isinstance(detail, dict):
        return {"matched": [], "missing": []}
    skill = (detail.get("parts") or {}).get("skill") or {}
    return {
        "matched": [str(item) for item in (skill.get("matched") or [])],
        "missing": [str(item) for item in (skill.get("missing") or [])],
    }


# ===========================================================================
# ⑩ jh gap：跨岗位聚合"我该补什么"
# ===========================================================================
@dataclass
class GapRow:
    """一个技能在所有 JD 里的出现情况。"""

    keyword: str
    #: 出现在多少个岗位的 JD 里
    job_count: int
    #: 出现比例（job_count / 参与统计的岗位数）
    ratio: float
    #: 我的简历里有没有（经过技能归一化判断）
    have: bool
    #: 简历里只在项目描述里提过（没在技能栏主动列出来）
    only_mentioned: bool = False
    #: 优先级：high / medium / low / ""（已有 = 没有缺口）
    priority: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "keyword": self.keyword,
            "job_count": self.job_count,
            "ratio": round(self.ratio, 4),
            "have": self.have,
            "only_mentioned": self.only_mentioned,
            "priority": self.priority,
        }


@dataclass
class GapReport:
    """`jh gap` 的完整结果。"""

    total_jobs: int = 0
    #: 参与统计的岗位数（有 JD 关键词的那些）
    analyzed_jobs: int = 0
    #: 有 JD 但一个关键词都没抽到的岗位数（提示用户"可能 JD 太短"）
    empty_jobs: int = 0
    resume_id: int | None = None
    resume_name: str = ""
    rows: list[GapRow] = field(default_factory=list)

    @property
    def missing_rows(self) -> list[GapRow]:
        return [row for row in self.rows if not row.have]


#: 缺口优先级阈值。
#: ★ 为什么是 50% / 25%：这是给"投 20 个岗位"量级的人用的 ——
#:   50% 意味着 10 个岗位都要求它，补它一定值；25% 是"值得顺手学"。
#:   样本量太小（岗位 < 5）时比例本身就不稳，所以 CLI 会提示样本量。
GAP_HIGH_RATIO = 0.5
GAP_MEDIUM_RATIO = 0.25


def _gap_priority(ratio: float, have: bool) -> str:
    if have:
        return ""
    if ratio >= GAP_HIGH_RATIO:
        return "high"
    if ratio >= GAP_MEDIUM_RATIO:
        return "medium"
    return "low"


def analyze_gaps(
    session: Session,
    *,
    resume_id: int | None = None,
    settings: AppSettings | None = None,
    only_scored: bool = False,
) -> GapReport:
    """跨所有岗位聚合技能缺口 —— "简历不匹配"痛点的**真正答案**。

    ★ 它和"逐岗告诉你缺什么"的区别（产品视角评审的原话）：
        逐岗输出 → 100 条零散的"缺 LangChain、缺 Docker"，不知道先补哪个；
        jh gap   → "LangChain 出现在 78% 的 JD 里而你没有" → 直接告诉你补什么。

    ★ 数据源优先级：
        ① `jobs.jd_keywords_json`（match 跑过就有，0 成本）；
        ② 没跑过 match 的岗位 → **临时**用 jd_text 现抽（不落库）。
      为什么临时抽而不是"跳过没打分的岗位"：用户很可能先想看看自己缺什么，
      再去跑匹配。跳过会得到一份"看起来正常但其实少统计了一半岗位"的报告 ——
      这种静默失真正是这个项目最想避免的东西。

    ★ `only_scored=True` 时只看跑过 match 的岗位（`jh gap --scored`），
      用途是"排除掉那些还没解析 JD 的岗位"做对比。
    """
    settings = settings or get_settings()
    vocabulary = resume_svc.load_skill_vocabulary(settings)

    # ---- 简历侧：我要知道"我到底有没有这个技能" ----
    resume_row = _pick_resume_row(session, resume_id, settings)
    pool: set[str] = set()
    explicit: set[str] = set()
    resume_name = ""
    if resume_row is not None:
        content = resume_svc.content_from_json(resume_row.content_json)
        pool, explicit = resume_skill_pool(content)
        resume_name = resume_row.name or f"简历#{resume_row.id}"

    # ---- 岗位侧：统计每个关键词出现在多少个 JD 里 ----
    counts: dict[str, int] = {}
    analyzed = 0
    empty = 0
    total = 0
    for job in session.scalars(select(Job).order_by(Job.id)):
        total += 1
        keywords = from_json(job.jd_keywords_json, default=None)
        if not isinstance(keywords, list) or not keywords:
            if only_scored:
                continue
            keywords = extract_jd_keywords(job.jd_text, settings, vocabulary=vocabulary)
            if not keywords:
                empty += 1
                continue
        analyzed += 1
        for word in keywords:
            if isinstance(word, str) and word:
                counts[word] = counts.get(word, 0) + 1

    rows: list[GapRow] = []
    for word, count in counts.items():
        ratio = count / analyzed if analyzed else 0.0
        have = word in pool
        rows.append(
            GapRow(
                keyword=word,
                job_count=count,
                ratio=ratio,
                have=have,
                # 只在项目描述里提过、技能栏没列 → "其实会，但简历没主动声称"，
                # 这是最容易忽略的一类改进点（explain 会单独提示）
                only_mentioned=bool(have and word not in explicit),
                priority=_gap_priority(ratio, have),
            )
        )

    # 排序：先按"缺口优先级"（没有的排前面），再按出现比例降序，最后按名字。
    # ★ 加"最后按名字"是为了**结果稳定**：比例相同的两个词不能每次顺序都不一样。
    order = {"high": 0, "medium": 1, "low": 2, "": 3}
    rows.sort(key=lambda row: (order.get(row.priority, 9), -row.ratio, row.keyword))

    return GapReport(
        total_jobs=total,
        analyzed_jobs=analyzed,
        empty_jobs=empty,
        resume_id=resume_row.id if resume_row is not None else None,
        resume_name=resume_name,
        rows=rows,
    )


def _pick_resume_row(
    session: Session, resume_id: int | None, settings: AppSettings
) -> Resume | None:
    """挑一份简历：显式指定 > 默认简历 > id 最小的那份。"""
    if resume_id is not None:
        return session.get(Resume, resume_id)
    row = session.scalars(select(Resume).where(Resume.is_default.is_(True))).first()
    if row is not None:
        return row
    return session.scalars(select(Resume).order_by(Resume.id)).first()


def decode_keywords(raw: str | None) -> list[str]:
    """读 `jobs.jd_keywords_json`（给 CLI/测试用的小工具，顺带容忍脏数据）。"""
    data = from_json(raw, default=None)
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if isinstance(item, str) and item]

"""防编造校验（FidelityGuard 的核心，P4 用于开场白，P5 复用于简历改写）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 为什么"防编造"必须是**代码强制**，不能靠提示词祈祷                          ║
╚══════════════════════════════════════════════════════════════════════════╝

提示词里写"只能引用简历里真实存在的东西"，模型**大部分时候**会听；
但剩下那一小部分时候，它会顺手写出"熟悉 Kubernetes""做过 300% 的性能提升"
——而这些是**要发给 HR 的**。一旦发出去，代价不是"分数不准"，是"人品问题"。

所以：生成归模型，**判定归代码**。这里是判定那一半。

★ 三条判定规则（每条都对应一种真实会发生的编造）

    ① 技能：文案里提到的技能必须真的在简历里
       （用与匹配同一套词表 + 同一套词边界，避免"Java 命中 JavaScript"式的假阳性）
    ② 数字：`300%` / `10个模块` / `3年` 这类**可核对的量化声称**必须能在简历里找到出处
    ③ 学历：说了"硕士"而简历是本科 → 拦

★ 刻意**不做**的事（写下来免得日后被当成漏洞）：

    · 不做实体识别（"我在腾讯实习过"里的公司名不校验）——
      那需要一份公司库，而且误报率高；P4/P5 的文案模板里根本不出现公司名。
    · 不做语义蕴含判断（"主导了架构设计" vs 简历写"参与架构设计"）——
      那是另一个量级的问题，硬做只会做出一个天天误报、最后被关掉的检查。

★ 允许一个例外：`learning_whitelist`（"我在学"白名单）里的技能可以提，
  但**文案里必须出现"在学习"这类标注** —— 这正是配置项存在的意义
  （"刚做了 demo 但还不够熟"完全不许提反而失真）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobhunter.services import resume_svc
from jobhunter.services.resume_svc import ResumeContent

__all__ = ["FidelityReport", "Violation", "check_text"]

#: 量化声称的单位。**刻意只收这些**：
#: `%`/倍/个/人/万/k —— 都是"成就数字"；而"一周内到岗""3 天内回复"里的
#: 一/天不是成就声称，收进来只会制造误报（误报多了这个检查就会被关掉）。
_NUMBER_RE = re.compile(
    r"(?<![\d.])(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>%|％|倍|个|人|万|[kK])",
)
#: 年限声称单独一条："3 年经验"、"两年"（中文数字不收，只收阿拉伯数字，避免误报）
_YEARS_RE = re.compile(r"(?<![\d.])(?P<num>\d+(?:\.\d+)?)\s*年")

#: 学习标注：白名单技能必须带这些词之一才允许出现。
_LEARNING_MARKERS = ("在学习", "正在学", "学习中", "还在学", "刚入门", "初步了解")


@dataclass
class Violation:
    """一条编造嫌疑。"""

    #: skill_not_in_resume / skill_not_in_jd / number_not_in_resume / degree_not_held
    kind: str
    token: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - 只为日志/报错好看
        return f"[{self.kind}] {self.token}：{self.detail}"


@dataclass
class FidelityReport:
    """一次校验的完整结果。"""

    violations: list[Violation] = field(default_factory=list)
    #: 文案里出现的技能（规范化名）—— 排查"为什么判它编造"时看这个
    mentioned_skills: list[str] = field(default_factory=list)
    #: 文案里出现的量化声称原文
    mentioned_numbers: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.violations

    def summary(self) -> str:
        if self.passed:
            return "无编造嫌疑"
        return "；".join(str(item) for item in self.violations)


def _resume_numbers(resume: ResumeContent) -> set[str]:
    """简历正文里**出现过**的数字（不含推导值，推导见 `_count_evidence`）。"""
    numbers: set[str] = set(re.findall(r"\d+(?:\.\d+)?", resume.raw_text or ""))
    numbers.update(re.findall(r"\d+(?:\.\d+)?", " ".join(resume.skills)))
    return numbers


def _count_evidence(resume: ResumeContent, tail: str) -> set[str]:
    """ "可推导的计数"：只有当数字后面跟着**对应的名词**时才算证据。

    ★ 为什么不能把所有计数一股脑塞进证据池（写第一版时踩到的）：
      `len(skills) == 3` 是事实，但如果把 "3" 无条件当成证据，
      文案写"我有 3 年经验"就会被判成真话 —— 数字对上了，含义完全不对。
      所以按**名词**匹配：说"个项目"就去比项目数，说"项技能"才比技能数。

    ★ 为什么需要它：文案写"做过 1 个项目"时，简历里未必有"1"这个字符，
      但它是事实。不加这一步会把真话判成编造（误报比漏报更糟：
      误报多了，这个检查就会被人关掉）。
    """
    evidence: set[str] = set()
    windows: tuple[tuple[tuple[str, ...], int], ...] = (
        (("项目", "作品"), len(resume.projects)),
        (("技能", "技术栈"), len(resume.skills)),
        (("实习", "实习经历"), len(resume.internships)),
        (("段经历", "段教育"), len(resume.education)),
    )
    for nouns, count in windows:
        if any(noun in tail for noun in nouns):
            evidence.add(str(count))
    return evidence


def _normalize_number(value: str) -> set[str]:
    """一个数字的等价写法：`100.0` 与 `100` 视为同一个（模型爱写小数点）。"""
    forms = {value}
    try:
        if float(value).is_integer():
            forms.add(str(int(float(value))))
    except ValueError:  # pragma: no cover - 正则保证不会走到
        pass
    return forms


def _resume_degrees(resume: ResumeContent) -> set[str]:
    """简历里真正拿到的学历词（用于拦截"凭空升学历"）。

    ★ 只读 `degree` 与"学校+专业"：`EducationItem` **没有** `raw_line`
      （踩过：写 `item.raw_line` 会 AttributeError）。
    """
    words: set[str] = set()
    for item in resume.education:
        if item.degree:
            words.add(item.degree)
        words.update(
            re.findall(
                r"博士|硕士|研究生|本科|学士|大专|专科|中专|高中",
                f"{item.school}{item.major}{item.degree}",
            )
        )
    return words


def check_text(
    text: str,
    *,
    resume: ResumeContent,
    vocabulary: dict[str, list[str]] | None = None,
    allowed_skills: set[str] | None = None,
    learning_whitelist: set[str] | None = None,
    extra_evidence: set[str] | None = None,
) -> FidelityReport:
    """校验一段文案有没有编造简历里不存在的东西。

    Args:
        allowed_skills: 额外限制"只允许提这些技能"（开场白传
                        `JD 关键词 ∩ 简历技能`）。None = 不限制。
        learning_whitelist: 允许提、但必须带"在学习"标注的技能。
        extra_evidence: 额外算作"简历里有"的技能。**用途只有一个**：
                        求职方向词（"AI Agent 方向"）不是技能声称，
                        但词表会把里面的 "Agent" 抽成一个技能 → 会误报。
                        调用方把方向词里抽出的技能传进来，误报就消失了。

    Returns:
        `FidelityReport`；`passed` 为 False 时调用方**必须**改文案或降级，
        绝不可以把这份文案发出去。
    """
    vocabulary = vocabulary if vocabulary is not None else resume_svc.load_skill_vocabulary()
    learning_whitelist = learning_whitelist or set()
    extra_evidence = extra_evidence or set()
    text = text or ""

    report = FidelityReport()
    pool, _explicit = resume_svc.resume_skill_pool(resume)
    pool = pool | extra_evidence
    mentioned = resume_svc.extract_skills(text, vocabulary)
    report.mentioned_skills = mentioned

    annotated = any(marker in text for marker in _LEARNING_MARKERS)
    for skill in mentioned:
        # ★ `extra_evidence` 里的技能**两道检查都豁免**：它们不是"技能声称"
        #   （方向词"AI Agent 方向"里的 Agent 只是方向的写法）。
        #   只豁免第一道是不够的 —— 第二道会接着报"JD 没要求"，照样误报。
        if skill in extra_evidence:
            continue
        # ★★ 动作/通用词（`部署`/`性能优化`/`单元测试`…）不按"技能声称"判（ISSUES #30）：
        #   简历的技能栏永远不会写这些动词，但"我用 Docker 部署过项目"是**完全自然的真话**。
        #   按技能判它 → 真话被标成谎话 → 整条开场白降级成模板
        #   （实测发生过；用户视角是"我明明部署过，工具却说我编造"）。
        #   ⚠️ 豁免**只限这一类动词/通用实践**：技术栈名字仍然严格
        #   （Kubernetes 没写在简历里就是编造），数字与学历也照旧。
        if skill in resume_svc.CAPABILITY_TERMS:
            continue
        if skill not in pool:
            if skill in learning_whitelist and annotated:
                continue  # 白名单 + 有"在学习"标注 → 允许
            report.violations.append(
                Violation(
                    kind="skill_not_in_resume",
                    token=skill,
                    detail="简历里没有这个技能，属于编造",
                )
            )
        if allowed_skills is not None and skill not in allowed_skills and skill in pool:
            report.violations.append(
                Violation(
                    kind="skill_not_in_jd",
                    token=skill,
                    detail="这个技能虽然简历里有，但 JD 没要求 —— 开场白应当贴 JD",
                )
            )

    found = _resume_numbers(resume)
    for match in list(_NUMBER_RE.finditer(text)) + list(_YEARS_RE.finditer(text)):
        raw = match.group(0)
        report.mentioned_numbers.append(raw)
        tail = text[match.end() : match.end() + 6]
        evidence = found | _count_evidence(resume, tail)
        if not (_normalize_number(match.group("num")) & evidence):
            report.violations.append(
                Violation(
                    kind="number_not_in_resume",
                    token=raw,
                    detail="简历里找不到这个数字的出处",
                )
            )

    held = _resume_degrees(resume)
    for word in ("博士", "硕士", "研究生", "本科", "学士", "大专", "专科"):
        if word in text and word not in held:
            report.violations.append(
                Violation(
                    kind="degree_not_held",
                    token=word,
                    detail=f"简历里的学历是 {'/'.join(sorted(held)) or '（没写）'}，不能声称{word}",
                )
            )
    return report

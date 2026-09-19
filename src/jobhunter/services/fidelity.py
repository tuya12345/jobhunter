"""防编造校验（FidelityGuard，P4 用于开场白，P5 用于整篇简历改写）。

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

╔══════════════════════════════════════════════════════════════════════════╗
║ P5：整篇改写用的 `FidelityGuard`（同一套判定，加三道"改写才有"的检查）        ║
╚══════════════════════════════════════════════════════════════════════════╝

开场白是**从零写一句新话**，只要"提到的技能/数字在简历里有据"就够了。
简历改写不一样：它拿到一段**原文**，产出一段**改写**。多出来的风险是
"看起来只是把话说了漂亮一点，其实偷偷加了事实"。所以除了技能/数字/学历，
还要补三道：

    ③ 时间守恒：年份/月份/年限不许新增或修改（"2026.03"不能变成"2026 年 3 月-至今"）
    ① 命名实体：公司/学校这类**专有名词**必须在原简历里出现过
       （技术名走技能词表那道，合并成一条口径，避免同一个词被报两次）
    ⑤ 长度上限：改写不是扩写（默认不超过原文 150%）

★ `FidelityGuard.check(original, generated)` 的两个参数**各有分工**（这是本模块
  最容易搞混的一点，写下来）：

    · `original` —— 被改写的那段文本。它决定**长度上限**的基准。
    · `resume`   —— 整份简历（可选）。给了它，事实核查的"证据池"就是**整份简历**
      而不是那一段。这是必须的：一条项目 bullet 的改写可以引用简历别处写过的事实
      （比如技能栏里的 Python），只拿那一行当证据会**误报**。
      误报的代价比漏报高 —— 误报多了，这个检查就会被人关掉。

★ 刻意**不做**的事（P5 版本）：

    · 不做真正的 NER：公司名用"专有名词后缀"（有限公司/集团/大学/学院…）+ 逐字
      回退匹配来判，抓不到"在字节跳动实习"这种没有后缀的写法。
      取舍理由：宁可漏一个，也不要因为"把真经历判成编造"而让人不信任这个工具。
    · 不校验 `match_analysis` 里的分析性文字（strengths/gaps 是**给我看的分析**，
      不写进导出的简历文件），只校验真正会写进简历的 `suggestions[].revised`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobhunter.config.settings import AppSettings, get_settings
from jobhunter.services import resume_svc
from jobhunter.services.resume_svc import ResumeContent

__all__ = ["FidelityGuard", "FidelityReport", "Violation", "check_text"]

#: 量化声称的单位。**刻意只收这些**：
#: `%`/倍/个/人/万/k —— 都是"成就数字"；而"一周内到岗""3 天内回复"里的
#: 一/天不是成就声称，收进来只会制造误报（误报多了这个检查就会被关掉）。
_NUMBER_RE = re.compile(
    r"(?<![\d.])(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>%|％|倍|个|人|万|[kK])",
)
#: 年限声称单独一条："3 年经验"、"两年"（中文数字不收，只收阿拉伯数字，避免误报）
_YEARS_RE = re.compile(r"(?<![\d.])(?P<num>\d+(?:\.\d+)?)\s*年")

# ---------------------------------------------------------------------------
# P5 用的补充正则（只在 `FidelityGuard` 里用，不改变 `check_text` 的既有口径）
# ---------------------------------------------------------------------------
#: 改写场景要**把所有阿拉伯数字**都看一遍。开场白只关心"成就数字"（有单位的），
#: 因为一句话里出现孤立数字的概率极低；而简历改写是逐句对照原文，
#: 任何多出来的数字都是可疑的 —— 包括"10 个模块"里的 10、"300%"里的 300、
#: 以及 "2024 年" 里的 2024（后者由时间守恒先接管）。
_ANY_NUMBER_RE = re.compile(r"(?<![\d.])(?P<num>\d+(?:\.\d+)?)")
#: 数字 + 单位（单位可缺）。**单位必须一起比**，否则会漏掉最典型的一种夸大：
#:   简历写"接口从 300ms 降到 80ms"，改写写"性能提升 300%" ——
#:   只比数字的话 300 在简历里"存在"，这条就溜过去了。
#:   而"300%"这个整体在简历里根本不存在 → 拦得住。
_NUMBER_WITH_UNIT_RE = re.compile(
    r"(?<![\d.])(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>%|％|倍|个|人|万|[kK])?"
)
#: 单位归一：全角百分号与大小写 k 是同一个意思
_UNIT_FOLD = {"％": "%", "K": "k"}

#: 日期：`2024` / `2024年` / `2024年3月` / `2024.03` / `2024-3` / `2024/03`。
#: ★ 年份限定 19xx/20xx：不这么写的话，"招聘 30 人"里的 30 会被当成日期，
#:   而"2024"这种四位数字在简历语境里几乎只可能是年份。
_DATE_RE = re.compile(
    r"(?<!\d)(?P<y>(?:19|20)\d{2})"
    r"(?:\s*[年./\-]\s*(?P<m>0?[1-9]|1[0-2])\s*月?)?"
    r"(?!\d)"
)
#: 只有月份的写法："3 月"（不含"3 个月" —— `个` 会打断匹配，这是刻意的：
#: "3 个月"是时长声称，由数字守恒管，不是日期）
_MONTH_RE = re.compile(r"(?<![\d.])(?P<m>0?[1-9]|1[0-2])\s*月")

#: 机构/学校名：靠"专有名词后缀"识别。取 `[\u4e00-\u9fff]` 与拉丁字母数字混合，
#: 因为"XX 大学"、"XX 学院"里也可能夹着英文（如"XX AI 学院"）。
_ORG_RE = re.compile(
    r"(?P<prefix>[\u4e00-\u9fffA-Za-z0-9·]{2,18}?)"
    r"(?P<suffix>股份有限公司|有限责任公司|有限公司|集团|公司|大学|学院|学校|研究院|研究所|实验室)"
)
#: 出现在机构名前导的"不是名字"的字（"**在**腾讯科技""**参与**某某科技"）。
#: ★ 用途说明（否则会以为这是玄学）：正则不知道公司名从哪个字开始，
#:   于是把前导的动词/介词一起抓进 prefix。裁掉它们有两个好处：
#:     ① 能多生成几个"全称形状"候选（"参与腾讯科技" → "腾讯科技有限公司"）；
#:     ② 全是噪音的匹配（"年在某公司"）能被识别成"这不是一个机构名"而跳过 ——
#:        否则一句正常的中文会被报成"编造了机构名"，这是纯误报。
_LEADING_NOISE_CHARS = "的了在是与和及于从到把被为会能够没我你他她它参与对将给年月日第"

#: 机构名候选核的**最小长度**。
#: ★ 为什么是 3 而不是 2（这是本检查能不能生效的关键，实测踩到）：
#:   2 字核会生出"科技有限公司"这种**通用**候选 —— 只要简历里出现过任何一家
#:   "…科技有限公司"，所有公司名都会被判成"存在过"，检查当场失效
#:   （实测：编造的"腾讯科技有限公司"因此溜过了闸门）。
#:   代价是两字的公司简称（"腾讯"）必须靠更长的形状命中（"腾讯科技"），
#:   而简历/改写里几乎总会出现更长的形状。
_MIN_ENTITY_CORE = 3

#: 生成文本里"像专有名词"的拉丁词：首字母大写的 token（Kubernetes / ByteDance / Google）。
_ENTITY_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(?P<tok>[A-Z][A-Za-z0-9+#._\-]{1,30})")
#: 证据池一侧的拉丁词：大小写都要（简历里写的是 `python` 也要算它在）。
_POOL_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(?P<tok>[A-Za-z][A-Za-z0-9+#._\-]{1,30})")


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
    #: **不是违规、但用户该知道**的事（P5 加的）：例如"Kubernetes 在学习中（白名单）"。
    #: 与 violations 分开的理由：把"允许但要注意"混进违规里，用户就会去改本来
    #: 合规的文案；反过来把它丢掉，用户又不知道自己写了白名单技能。
    notes: list[str] = field(default_factory=list)
    #: 长度比（生成 / 原文），None = 没算（`check_text` 不做长度检查）
    length_ratio: float | None = None

    @property
    def passed(self) -> bool:
        return not self.violations

    def summary(self) -> str:
        if self.passed:
            return "无编造嫌疑"
        return "；".join(str(item) for item in self.violations)

    def as_dict(self) -> dict[str, object]:
        """落库用的形状（`resume_versions.fidelity_report_json`）。

        ★ 只存**判定结果**，不存原文 —— 这张表里已经有简历片段了，
          再塞一份原文副本只会让隐私面变大（见 README 的隐私边界）。
        """
        return {
            "passed": self.passed,
            "violations": [
                {"kind": item.kind, "token": item.token, "detail": item.detail}
                for item in self.violations
            ],
            "notes": list(self.notes),
            "mentioned_skills": list(self.mentioned_skills),
            "mentioned_numbers": list(self.mentioned_numbers),
            "length_ratio": None if self.length_ratio is None else round(self.length_ratio, 4),
        }


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


def _skill_violations(
    text: str,
    *,
    pool: set[str],
    vocabulary: dict[str, list[str]],
    extra_evidence: set[str],
    learning_whitelist: set[str],
    allowed_skills: set[str] | None,
    notes: list[str],
) -> tuple[list[Violation], list[str]]:
    """技能层面的判定（`check_text` 与 `FidelityGuard` 共用**同一套口径**）。

    ★ 为什么必须共用（而不是两处各写一份）：P4 结尾修过的 ISSUES #30 就是
      "动作词被当成技能声称" —— 如果开场白豁免了 `部署`、简历改写又判它编造，
      同一个词在两条路径上给出相反结论，用户会彻底不信任这个检查。
      所以豁免逻辑（CAPABILITY_TERMS / extra_evidence / 白名单）只能有一份。
    """
    mentioned = resume_svc.extract_skills(text, vocabulary)
    annotated = any(marker in text for marker in _LEARNING_MARKERS)
    violations: list[Violation] = []
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
                notes.append(f"{skill}：白名单技能且已标注「在学习」，允许出现")
                continue  # 白名单 + 有"在学习"标注 → 允许
            violations.append(
                Violation(
                    kind="skill_not_in_resume",
                    token=skill,
                    detail="简历里没有这个技能，属于编造",
                )
            )
        if allowed_skills is not None and skill not in allowed_skills and skill in pool:
            violations.append(
                Violation(
                    kind="skill_not_in_jd",
                    token=skill,
                    detail="这个技能虽然简历里有，但 JD 没要求 —— 开场白应当贴 JD",
                )
            )
    return violations, mentioned


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
    violations, mentioned = _skill_violations(
        text,
        pool=pool,
        vocabulary=vocabulary,
        extra_evidence=extra_evidence,
        learning_whitelist=learning_whitelist,
        allowed_skills=allowed_skills,
        notes=report.notes,
    )
    report.violations.extend(violations)
    report.mentioned_skills = mentioned

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


# ===========================================================================
# P5：整篇改写用的 FidelityGuard
# ===========================================================================
#: 中文简历里常见的**写法泛称**，不是实体。含义：
#: "API 接口""REST 风格""UI 层" 这类词即便简历原文没逐字写过，
#: 用它们描述一件真事也不构成编造 —— 把它们当实体报违规纯属噪音。
#: ★ 这份名单**刻意很短**：技术栈名字（SQL/Redis/Docker…）不在这里，
#:   因为那些是"我会不会"的声称，必须严格（漏了就在这里补一行，并想清楚为什么）。
_GENERIC_ACRONYMS = frozenset(
    {
        "API",
        "REST",
        "UI",
        "UX",
        "IDE",
        "OS",
        "PDF",
        "HTTP",
        "HTTPS",
        "JSON",
        "CSV",
        "URL",
        "CPU",
        "GPU",
        "RAM",
        "SDK",
        "CLI",
        "GUI",
        "CDN",
    }
)


def _entity_key(token: str) -> str:
    """拉丁词的等价键：大小写、点、连字符、加号都不算差异。

    `Node.js` / `nodejs` / `NODE.JS` 是同一个东西；`C++` / `c++` 也是。
    不做这一步，"简历写 Node.js、模型写 Nodejs"会被判成编造（纯误报）。
    """
    return re.sub(r"[^a-z0-9]", "", token.lower())


def _entity_candidates(prefix: str, suffix: str) -> list[str]:
    """把 `_ORG_RE` 切出的前缀还原成"可能的机构名"候选（用于**放宽**判定）。

    ★ 为什么要切：正则无法知道"我在腾讯科技"里哪几个字才是公司名，
      所以它会把前导的词一起吃进来（"我在"）。候选从左边逐字裁掉（"我在腾讯科技"
      → "腾讯科技"），再从右边裁到 2 字（"腾讯科技" → "腾讯"），
      **任一候选在简历里出现过就算实体存在**。
    ★ 为什么允许裁到 2 字：中国公司名最短就是 2 字（"腾讯""华为"），
      裁不够会把真公司判成编造。
    ★ 为什么同时给出 `core + suffix` 和裸 `core` 两种形状：
      简历里通常写全称（"腾讯科技有限公司"），但也会写简称（"腾讯科技"）。
    ★ 核长下限 3 见 `_MIN_ENTITY_CORE`（2 字核会让这条检查失效）。
    """
    out: list[str] = []
    for index in range(len(prefix)):
        core = prefix[index:]
        if len(core) < 2:  # noqa: PLR2004 - 名字至少两个字（"腾讯""北大"）
            break
        out.append(core + suffix)  # 全称形状
        if len(core) >= _MIN_ENTITY_CORE:
            # 简称形状：2 字核（"科技"）不许裸着算候选，否则任何一家
            # "…科技有限公司"都会让它通过（见 `_MIN_ENTITY_CORE`）
            out.append(core)
    return out


def _pool_orgs(text: str) -> tuple[str, ...]:
    """证据池里出现过的机构/学校**全名**（去重、保持出现顺序）。"""
    return tuple(dict.fromkeys(match.group(0) for match in _ORG_RE.finditer(text or "")))


def _org_is_known(candidates: list[str], pool_orgs: tuple[str, ...]) -> bool:
    """候选里有没有"某个已知机构名的左半部分"（= 简称写法）。

    ★ 为什么不直接判断"候选是不是证据池的子串"（第一版就是这么写的，漏了）：
      那么"科技有限公司"这种**通用**候选会命中"某某科技有限公司"，
      于是编造的"腾讯科技有限公司"被判成真实存在。
      改成"必须是某个已知机构名的前缀"之后，
      "腾讯科技"（编造）无法成为"某某科技有限公司"的前缀 → 拦得住；
      而真简称"某某科技" / "虚拟" 能对上全名 → 不误报。
    """
    return any(org.startswith(candidate) for candidate in candidates for org in pool_orgs)


def _strip_leading_noise(prefix: str) -> str:
    """去掉机构名前导的动词/介词（"参与腾讯科技" → "腾讯科技"）。"""
    index = 0
    while index < len(prefix) and prefix[index] in _LEADING_NOISE_CHARS:
        index += 1
    return prefix[index:]


def _pool_text(resume: ResumeContent | None, original: str) -> str:
    """事实核查的"证据池"：整份简历（优先）或那段原文的全文。"""
    if resume is None:
        return original or ""
    parts = [
        resume.raw_text or "",
        " ".join(resume.skills),
        " ".join(item.name for item in resume.projects),
        " ".join(item.company for item in resume.internships),
        " ".join(f"{item.school}{item.major}{item.degree}" for item in resume.education),
        " ".join(bullet for item in resume.projects for bullet in item.bullets),
        " ".join(bullet for item in resume.internships for bullet in item.bullets),
    ]
    return "\n".join(part for part in parts if part)


def _numbers_in(text: str) -> set[str]:
    """文本里出现过的全部数字（含各种等价写法）。"""
    numbers: set[str] = set()
    for match in _ANY_NUMBER_RE.finditer(text or ""):
        numbers |= _normalize_number(match.group("num"))
    return numbers


def _num_forms(value: str, unit: str) -> set[str]:
    """`数字 + 单位` 的等价写法：`100.0%` 与 `100%` 是同一个。"""
    return {f"{form}{unit}" for form in _normalize_number(value)}


def _number_evidence(text: str) -> tuple[set[str], set[str]]:
    """证据池里的数字：`(裸数字集合, 数字+单位集合)`。"""
    plain = _numbers_in(text)
    pairs: set[str] = set()
    for match in _NUMBER_WITH_UNIT_RE.finditer(text or ""):
        unit = match.group("unit")
        if not unit:
            continue
        unit = _UNIT_FOLD.get(unit, unit)
        pairs |= _num_forms(match.group("num"), unit)
    return plain, pairs


def _date_keys(text: str) -> tuple[set[str], set[str]]:
    """文本里的日期键：`({年, 年-月}, {月份})`。

    把 `2026.03` / `2026年3月` / `2026/3` 归一成同一个键 `ym2026-03`，
    这样"格式不同但月份相同"不会被误判成修改了时间（模型爱改格式）。
    """
    full: set[str] = set()
    months: set[str] = set()
    for match in _DATE_RE.finditer(text or ""):
        full.add(f"y{match.group('y')}")
        month = match.group("m")
        if month:
            full.add(f"ym{match.group('y')}-{int(month):02d}")
            months.add(str(int(month)))
    for match in _MONTH_RE.finditer(text or ""):
        months.add(str(int(match.group("m"))))
    return full, months


def _date_spans(text: str) -> list[tuple[int, int]]:
    """日期的字符区间（数字守恒要跳过它们，否则同一个 2024 会被报两次）。"""
    return [(match.start(), match.end()) for match in _DATE_RE.finditer(text or "")]


class FidelityGuard:
    """原简历 → 改写文本 的保真闸门（**代码强制**，不依赖模型）。

    用法（三处强制调用：生成 / approve / export）：:

        report = FidelityGuard.check(original_line, revised_line, resume=content)
        if not report.passed:
            ...  # 拒绝写入，并把 report.summary() 显示给用户

    五项确定性检查：

    ============  ==========================================================
    kind           拦什么
    ============  ==========================================================
    entity_not_in_resume   公司/学校名（专有名词）不在简历里
    skill_not_in_resume    技能词表命中但简历里没有（含"技术栈名"这一类实体）
    number_not_in_resume   凭空多出来的数字（10 个模块 / 300%）
    date_not_in_resume     凭空多出来或改过的年份/月份
    too_long               改写后超过原文的 150%（改写不是扩写）
    ============  ==========================================================

    ★ 这里是**类方法**而不是实例方法：判定不依赖任何可变状态，
      调用点写 `FidelityGuard.check(a, b)` 就够了（契约里就是这么写的）。
      配置（长度比例等）通过 `settings` / `max_ratio` 参数传进来。
    """

    #: 检查项清单。★ 测试会拿它做"五项都没有被漏掉"的断言 ——
    #: 删掉一项检查必须同时改这里，否则测试会失败（防止静默弱化闸门）。
    CHECKS = (
        "entity_not_in_resume",
        "skill_not_in_resume",
        "number_not_in_resume",
        "date_not_in_resume",
        "too_long",
    )

    @classmethod
    def check(
        cls,
        original: str,
        generated: str,
        *,
        resume: ResumeContent | None = None,
        vocabulary: dict[str, list[str]] | None = None,
        learning_whitelist: set[str] | None = None,
        extra_evidence: set[str] | None = None,
        settings: AppSettings | None = None,
        max_ratio: float | None = None,
    ) -> FidelityReport:
        """校验 `generated` 相对 `original` 有没有编造。

        Args:
            original: 被改写的原文（决定长度上限的基准）。
            generated: 改写后的文本。
            resume: 整份简历；给了它就用它当事实来源（推荐，见模块 docstring）。
            extra_evidence: 额外算作"简历里有的"技能（方向词用）。
            max_ratio: 长度上限比例；默认取 `settings.tailor.max_length_ratio`（1.5）。

        Returns:
            `FidelityReport`。`passed=False` 时调用方**必须**拒绝写入。
        """
        settings = settings or get_settings()
        vocabulary = (
            vocabulary if vocabulary is not None else resume_svc.load_skill_vocabulary(settings)
        )
        learning_whitelist = learning_whitelist or set()
        extra_evidence = extra_evidence or set()
        original = original or ""
        generated = generated or ""
        ratio = float(max_ratio if max_ratio is not None else settings.tailor.max_length_ratio)

        report = FidelityReport()
        pool_text = _pool_text(resume, original)

        # ---- ① 命名实体白名单（技术名交给 ④，避免同一个词报两次）----------
        cls._check_entities(
            generated,
            pool_text=pool_text,
            vocabulary=vocabulary,
            learning_whitelist=learning_whitelist,
            extra_evidence=extra_evidence,
            report=report,
        )

        # ---- ④ 技能词表校验（与 `check_text` 同一套口径）-------------------
        if resume is not None:
            pool, _explicit = resume_svc.resume_skill_pool(resume)
        else:
            pool = set(resume_svc.extract_skills(pool_text, vocabulary))
        violations, mentioned = _skill_violations(
            generated,
            pool=pool | extra_evidence,
            vocabulary=vocabulary,
            extra_evidence=extra_evidence,
            learning_whitelist=learning_whitelist,
            allowed_skills=None,  # 改写不要求"贴 JD"，只要求"别编"
            notes=report.notes,
        )
        report.violations.extend(violations)
        report.mentioned_skills = mentioned

        # ---- ③ 时间守恒（先做日期，数字守恒再跳过这些区间）-----------------
        date_spans = _date_spans(generated)
        pool_dates, pool_months = _date_keys(pool_text)
        cls._check_dates(
            generated,
            pool_dates=pool_dates,
            pool_months=pool_months,
            report=report,
        )

        # ---- ② 数字守恒 ----------------------------------------------------
        evidence, unit_pairs = _number_evidence(pool_text)
        spans = date_spans  # `2024 年` 里的 2024 已经由时间守恒管了，跳过
        for match in _NUMBER_WITH_UNIT_RE.finditer(generated):
            if any(start <= match.start() < end for start, end in spans):
                continue
            raw = match.group(0)
            report.mentioned_numbers.append(raw.strip())
            tail = generated[match.end() : match.end() + 6]
            derivable = _count_evidence(resume, tail) if resume is not None else set()
            unit = _UNIT_FOLD.get(match.group("unit") or "", match.group("unit") or "")
            if unit:
                # 有单位 → 要求"数字 + 单位"这个整体在简历里出现过（见 `_NUMBER_WITH_UNIT_RE`
                # 的说明：不然"从 300ms 降到 80ms"会被改写成"提升 300%"）
                ok = bool(_num_forms(match.group("num"), unit) & unit_pairs)
            else:
                ok = bool(_normalize_number(match.group("num")) & evidence)
            # 可推导的计数（"1 个项目"）算真话：文案里的 1 未必在简历里逐字出现过
            ok = ok or bool(_normalize_number(match.group("num")) & derivable)
            if not ok:
                report.violations.append(
                    Violation(
                        kind="number_not_in_resume",
                        token=raw.strip(),
                        detail="原简历里找不到这个数字（含单位）的出处 —— 不许凭空新增数字",
                    )
                )

        # ---- ⑤ 长度上限 ----------------------------------------------------
        # ★ 为什么有一个绝对余量下限（`length_floor_extra`）：
        #   原句只有 4 个字时，150% 是 6 个字 —— 任何通顺的改写都会超，
        #   这条检查就会变成"短句一律不许改"，那是纯误报。
        floor = len(original) + int(settings.tailor.length_floor_extra)
        limit = max(int(len(original) * ratio), floor)
        report.length_ratio = (len(generated) / len(original)) if original else None
        if len(generated) > limit:
            report.violations.append(
                Violation(
                    kind="too_long",
                    token=f"{len(generated)} 字",
                    detail=(
                        f"改写后 {len(generated)} 字，超过原文 {len(original)} 字的 "
                        f"{int(ratio * 100)}%（上限 {limit} 字）—— 改写不是扩写"
                    ),
                )
            )
        return report

    # ------------------------------------------------------------------
    # 分项检查（拆开是为了每一段都能被单独读懂、单独测）
    # ------------------------------------------------------------------
    @staticmethod
    def _check_entities(
        generated: str,
        *,
        pool_text: str,
        vocabulary: dict[str, list[str]],
        learning_whitelist: set[str],
        extra_evidence: set[str],
        report: FidelityReport,
    ) -> None:
        """① 命名实体白名单：机构/学校名 + 首字母大写的拉丁专有名词。"""
        annotated = any(marker in generated for marker in _LEARNING_MARKERS)
        pool_orgs = _pool_orgs(pool_text)

        for match in _ORG_RE.finditer(generated):
            prefix, suffix = match.group("prefix"), match.group("suffix")
            stripped = _strip_leading_noise(prefix)
            # 全噪音的前缀（"年在某"）说明这里根本不是机构名 → 跳过。
            # ★ 跳过是**刻意的宽松**：宁可漏掉一个没有后缀的真实体，
            #   也不要把一句正常的中文报成编造（误报会让整个检查被人关掉）。
            if len(stripped) < 2:  # noqa: PLR2004 - 名字至少两个字
                continue
            # 候选从"原前缀"和"裁掉噪音后的前缀"两边都生成（只做加法，不改判定）
            candidates = _entity_candidates(prefix, suffix) + _entity_candidates(stripped, suffix)
            if _org_is_known(candidates, pool_orgs):
                continue
            if stripped in learning_whitelist and annotated:
                report.notes.append(f"{stripped}：白名单且已标注「在学习」，允许出现")
                continue
            report.violations.append(
                Violation(
                    kind="entity_not_in_resume",
                    token=match.group(0),
                    detail=f"原简历里没有「{stripped}{suffix}」这个机构/学校名",
                )
            )

        # 技术名走 ④（词表命中会更具体地报 skill_not_in_resume），
        # 这里只管"词表不认识的拉丁专有名词"（ByteDance / Google / Kubernetes 之外的）。
        known_skills = {_entity_key(name) for name in vocabulary}
        for name in extra_evidence:
            known_skills.add(_entity_key(name))
        pool_keys = {
            _entity_key(match.group("tok")) for match in _POOL_TOKEN_RE.finditer(pool_text)
        }
        for match in _ENTITY_TOKEN_RE.finditer(generated):
            token = match.group("tok")
            key = _entity_key(token)
            if not key or key in pool_keys or key in known_skills:
                continue
            if token.upper() in _GENERIC_ACRONYMS:
                continue
            if token in learning_whitelist and annotated:
                report.notes.append(f"{token}：白名单且已标注「在学习」，允许出现")
                continue
            report.violations.append(
                Violation(
                    kind="entity_not_in_resume",
                    token=token,
                    detail="原简历里没有出现过这个专有名词（公司/技术/产品名）",
                )
            )

    @staticmethod
    def _check_dates(
        generated: str,
        *,
        pool_dates: set[str],
        pool_months: set[str],
        report: FidelityReport,
    ) -> None:
        """③ 时间守恒：年份/月份不许新增或修改。"""
        for match in _DATE_RE.finditer(generated):
            year = match.group("y")
            month = match.group("m")
            if f"y{year}" not in pool_dates:
                report.violations.append(
                    Violation(
                        kind="date_not_in_resume",
                        token=match.group(0),
                        detail=f"原简历里没有 {year} 年这个时间",
                    )
                )
                continue
            if month and f"ym{year}-{int(month):02d}" not in pool_dates:
                report.violations.append(
                    Violation(
                        kind="date_not_in_resume",
                        token=match.group(0),
                        detail=(
                            f"原简历里有 {year} 年，但没有 {year} 年 {int(month)} 月 —— "
                            "月份不许新增（时间精度也是事实）"
                        ),
                    )
                )
        # 只有月份的写法（"3 月"）：`_DATE_RE` 已经把 "2026年3月" 里的月份管了，
        # 这里跳过与日期区间重叠的部分，否则同一个月份会被报两次
        # （用户看到两条一模一样的理由，只会觉得这工具在凑数）。
        spans = _date_spans(generated)
        for match in _MONTH_RE.finditer(generated):
            if any(start <= match.start() < end for start, end in spans):
                continue
            month = str(int(match.group("m")))
            if month not in pool_months:
                report.violations.append(
                    Violation(
                        kind="date_not_in_resume",
                        token=match.group(0),
                        detail=f"原简历里没有 {month} 月这个时间",
                    )
                )

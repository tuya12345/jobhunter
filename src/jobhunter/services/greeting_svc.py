"""开场白生成（P4 的核心：解决"以前写的跟岗位根本不沾边"）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 设计要点：**模型只负责"写得好听"，代码负责"不许越界"**                     ║
╚══════════════════════════════════════════════════════════════════════════╝

一次生成产出两个版本（BOSS 的开场白输入框有字数限制）：

    完整版 80-150 字（贴在能贴的地方）　＋　精简版 ≤60 字（贴不进去时用）

★ 四道**代码闸门**（每道都对应一个真实会翻车的点）

    ① 字数：完整版必须落在 `[full_min_chars, full_max_chars]`，精简版 ≤ `short_max_chars`
    ② 禁用词：`greeting_banned_words`（"贵司""期待""三生有幸"…）
       —— 套话是群发的标志，HR 一眼就看得出
    ③ 保真：过 `fidelity.check_text()` —— 只能提简历里**真实存在**的东西，
       且只提 `JD 关键词 ∩ 简历技能`（开场白的意义就是"沾边"）
    ④ 沾边：必须出现至少一个交集关键词（交集非空时）

    **任何一道不过 → 该版本降级到确定性模板**（`_template_greeting`）。
    宁可给一句"不惊艳但每个字都真"的话，也不能把编造的内容发出去。
    降级原因记在 `Greeting.issues` 里，`jh today` 会显示出来 ——
    失败要看得见，不能静默。

★ 变体 `with_project` / `without_project`（A/B 只测这一个变量）
    带项目名 vs 不带。「带项目名」通常更具体，但也可能显得啰嗦 ——
    这正是要 A/B 的原因。默认按清单序号**交替**分配，`--variant` 可强制一种。

★ 缓存：key 与匹配共用一套公式（含 `resume.content_hash`），
  但 `task="greeting"` 参与 key —— 否则同一岗位的 match 与 greeting 会撞成同一条缓存。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from jobhunter.config.settings import AppSettings, get_settings
from jobhunter.llm import cache as llm_cache
from jobhunter.llm.base import LLMMessage, LLMProvider, PromptPayload
from jobhunter.llm.deepseek import DeepSeekError
from jobhunter.llm.safety import wrap_delimited
from jobhunter.models import Job
from jobhunter.services import fidelity, match_svc, resume_svc
from jobhunter.services.match_svc import ResumeCandidate
from jobhunter.utils.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "GREETING_PROMPT_VERSION",
    "Greeting",
    "LLMGreeting",
    "build_payload",
    "generate_greeting",
    "parse_greeting",
    "pick_variant",
]

#: 提示词版本号：改了下面的 SYSTEM_PROMPT 或 build_payload 的任何字，**必须 +1**。
#: v2（2026-09-18）：真实 key 联调发现模型**稳定超字数**（完整版 195 字 / 精简版 112 字，
#:   上限分别是 150 / 60）→ 几乎每条都降级成模板（钱花了、拿到模板）。
#:   改法：把"字数"写成**可数的字符预算 + 目标区间 + 自检要求**，并加一次"只针对字数"的修复调用。
#: v3（2026-09-18 二次联调）：精简版仍稳定超（实测 69/66 字 vs 上限 60）→
#:   在提示词里写明"它要填进有字数上限的输入框，一句话 + 一个问题就够"，
#:   并给出更窄的目标区间（30-55），而不是只说一句"不超过 60"。
GREETING_PROMPT_VERSION = 3

#: 送进提示词的 JD 关键词上限（太多会让模型抓不住重点，也推高 token）
_MAX_KEYWORDS = 8
#: 送进提示词的简历摘要上限
_MAX_RESUME_CHARS = 1600

VARIANTS = ("with_project", "without_project")

# ★ JSON 模式三件套：① 提示词含 "json" 字样（就在下面这段里）
#   ② 给 schema 示例 ③ max_tokens 给足（两个版本各一百多字，800 足够）。
#
# ★ 字数为什么要写成"目标区间 + 自检"（不是一句"80-150 字"就够）：
#   实测模型把"80-150 字"理解成"差不多这个量级"，写出 195 字（超 30%）。
#   给它一个**更窄的目标区间**（100-130）并让它**自己数一遍**，命中率高得多；
#   真正的硬约束仍由代码闸门兜（超了就走一次修复调用，再不行才用模板）。
SYSTEM_PROMPT = """你是求职开场白助手，帮求职者写发给 HR 的第一条消息。

硬性要求（违反任何一条这条消息就作废）：
1. 只能使用我提供的「可用素材」里的技能与项目，**绝对不许**添加素材之外的技能、
   数字或学历（例如素材里没有 Kubernetes，就不能写"熟悉 Kubernetes"）。
2. **字数（按字符数，含标点；一个汉字算 1 个字符）**：
   - 完整版：**必须落在 {full_min}-{full_max} 个字符之间**，目标写到
     {full_target_low}-{full_target_high} 之间；
   - 精简版：**不超过 {short_max} 个字符**，目标 {short_target_low}-{short_target_high}。
     ⚠️ 精简版是要填进**有字数上限的输入框**的，必须真的短：
     **一句话自报方向 + 一个具体问题**就够，不要罗列技能清单。
     实测提醒：把"技能+项目+问题"都塞进去一定会超过 {short_max} 字。
   - 写完**自己数一遍**：超上限就删掉最不重要的一句；不到下限就补一句素材里真实的项目细节。
   - 宁可比目标区间短一点，也不要贴着上限写。
3. 不许用这些套话：{banned}
4. 语气像一个正常的年轻人说话，不要"尊敬的 HR""百忙之中"这类公文腔。
5. 结尾用一个**具体的、轻量的问题**引导对方回复（不要"期待您的回复"）。
6. 严格按下面的 json 结构输出，不要输出解释性文字、不要用 markdown 代码块。

json 输出结构（字段名必须完全一致）：
{{"full": "完整版开场白", "short": "精简版开场白"}}
"""

#: 修复调用的提示词（**只针对字数问题**，见 `build_repair_payload`）
REPAIR_PROMPT = """你上一次的输出**字数不合格**，需要重写。
仍然遵守原来的全部硬性要求（素材不许扩、禁用词、结尾提问）。

不合格的地方：
{problems}

请重新输出 json（字段名不变），这一次**先想好每一句多少字，再落笔**：
- 完整版：{full_min}-{full_max} 个字符，目标 {full_target_low}-{full_target_high}
- 精简版：不超过 {short_max} 个字符，目标 {short_target_low}-{short_target_high}
  （一句话自报方向 + 一个具体问题，别罗列技能；实测"技能+项目+问题"一定超）

上一版内容（可以删减、改写，但不许引入新的技能/数字/学历）：
<previous>
{previous}
</previous>
"""


class LLMGreeting(BaseModel):
    """模型输出的两个版本。"""

    full: str = ""
    short: str = ""


@dataclass
class Greeting:
    """一次开场白生成的结果（两个版本 + 溯源信息）。"""

    full: str = ""
    short: str = ""
    variant: str = "with_project"
    #: llm / template —— 降级成模板时不要慌，那是**保护**生效了
    source: str = "template"
    #: 走模板的原因（空的表示模型版本一次通过）
    issues: list[str] = field(default_factory=list)
    #: 这次用到的交集关键词（排查"为什么沾边/不沾边"看它）
    keywords: list[str] = field(default_factory=list)
    #: 哪几版是**经过一次"字数修复"调用**才合格的（真实联调加的：模型爱超字数）
    repaired: list[str] = field(default_factory=list)

    def text_for(self, version: str = "full") -> str:
        return self.short if version == "short" else self.full


def _length_problems(text: str, *, version: str, settings: AppSettings) -> list[str]:
    """字数问题的**唯一判定点**（闸门与修复调用都用它，避免两套口径）。

    ★ 为什么必须单一来源：修复调用要判断"这次失败是不是**只**由字数造成"。
      如果闸门用一套措辞、这里用另一套，就会出现
      "明明只超了字数、却被判成不是纯字数问题"（或反过来）——
      我第一版就是两套措辞 + 比数量，测试立刻打脸。
    """
    greeting = settings.greeting
    if version == "full":
        if not greeting.full_min_chars <= len(text) <= greeting.full_max_chars:
            return [
                f"完整版字数 {len(text)} 不在 {greeting.full_min_chars}-"
                f"{greeting.full_max_chars}（按字符数）"
            ]
        return []
    if len(text) > greeting.short_max_chars:
        return [f"精简版字数 {len(text)} 超过 {greeting.short_max_chars}（按字符数）"]
    return []


def is_length_only_failure(
    reasons: list[str], text: str, *, version: str, settings: AppSettings
) -> bool:
    """判断一次失败是否**只**由字数造成（只有这种才允许让模型自己修）。

    ★ 为什么别的失败不许"让模型改一遍就好"：
      保真类失败（编造了技能 / 数字 / 学历）是**信任问题**，不是笔误 ——
      让同一个模型再写一遍，它是"照着改"还是"换个说法继续编"，我们无法保证。
      所以那类失败一律**直接降级到确定性模板**。
      字数问题不一样：它可数、可验证，且改短不会引入新事实。

    ★ 判定方式：`reasons` 与"这个文本真实存在的字数问题"**集合相等** ——
      而不是比数量（数量相等会把"禁用词 + 恰好也超字数"误判成纯字数问题）。
    """
    length_reasons = _length_problems(text, version=version, settings=settings)
    if not length_reasons:
        return False
    return set(reasons) == set(length_reasons)


def pick_variant(index: int, *, forced: str | None = None) -> str:
    """挑本次用哪个变体：显式指定优先，否则按序号**交替**（A/B 都要跑得到）。"""
    if forced in VARIANTS:
        return forced
    return VARIANTS[index % len(VARIANTS)]


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n…（已截断）"


def _length_hints(settings: AppSettings) -> dict[str, int]:
    """提示词里的字数参数（目标区间比硬上限窄，留出模型"估不准"的余量）。"""
    greeting = settings.greeting
    return {
        "full_min": greeting.full_min_chars,
        "full_max": greeting.full_max_chars,
        # 目标区间取硬区间的中间偏上：太靠下限容易被砍到不够
        "full_target_low": greeting.full_min_chars + 20,
        "full_target_high": max(greeting.full_min_chars + 20, greeting.full_max_chars - 20),
        "short_max": greeting.short_max_chars,
        "short_target_low": max(15, greeting.short_max_chars // 2),
        "short_target_high": greeting.short_max_chars - 5,
    }


def build_payload(
    job: Job,
    candidate: ResumeCandidate,
    *,
    keywords: list[str],
    variant: str,
    settings: AppSettings,
) -> PromptPayload:
    """构造开场白生成的输入。

    ★ 素材里只给 `JD 关键词 ∩ 简历技能` —— 这就是"不许编造"的实现方式：
      模型拿不到素材之外的东西，就没有机会编。
      （再叠加 `fidelity.check_text()` 做第二道，两道都要有：
        模型可能从 "JD 里"看到"某个技能就顺手写出来。）
    """
    banned = "、".join(settings.greeting_banned_words) or "（无）"
    resume_text = _clip(_resume_digest(candidate), _MAX_RESUME_CHARS)
    project_hint = (
        "请带上一个具体的项目名（素材里的项目）。"
        if variant == "with_project"
        else "不要提项目名字，只讲做过什么。"
    )
    github = settings.greeting.github_url.strip()
    github_hint = f"\n我的 GitHub（可以附在结尾）：{github}" if github else ""

    # ★ 用 `wrap_delimited()` 拼标签，**不要手拼 f"<jd>…</jd>"**：
    #   `job.title` / `job.city` 是从招聘页面抓来的不可信文本，
    #   里面写一个 `</jd>` 就能把标签提前闭合（ISSUES #29，与 #2 / #8 同一个洞）。
    #   中和逻辑在 `wrap_delimited()` 内部完成，所以这里不可能忘。
    jd_body = (
        f"岗位：{job.title or '（未写职位）'} ｜ 城市：{job.city or '未写'}\n"
        f"JD 要求的关键词：{'、'.join(keywords) or '（没抽到）'}"
    )
    user_content = (
        f"{wrap_delimited('jd', jd_body)}\n\n"
        f"{wrap_delimited('resume', resume_text)}\n\n"
        f"可用素材（只能从这里挑）：技能 {'、'.join(keywords) or '（无）'}\n"
        f"{project_hint}{github_hint}\n"
        f"请输出 json。"
    )
    return PromptPayload(
        prompt_version=GREETING_PROMPT_VERSION,
        task="greeting",
        messages=[
            LLMMessage(
                role="system",
                content=SYSTEM_PROMPT.format(banned=banned, **_length_hints(settings)),
            ),
            LLMMessage(role="user", content=user_content),
        ],
        json_mode=True,
        max_tokens=800,
        # 话术类任务用高温（llm/base.py 的约定）：同样输入要"像人写的"，
        # 而打分类任务才需要低温求稳定。
        temperature=0.7,
    )


def build_repair_payload(
    previous: LLMMessage,
    problems: list[str],
    *,
    settings: AppSettings,
) -> PromptPayload:
    """构造"只修字数"的第二次调用。

    ★ 为什么值得多花一次调用（真实联调实测）：
      模型对"80-150 字"的遵守率很低（实测写出 195 字的完整版、112 字的精简版），
      于是**几乎每条都降级成模板** —— 用户花了钱却拿到模板话术，
      而这个功能的价值恰恰在"像人写的"。字数问题可数、可验证、改短不会引入新事实，
      是**最适合让模型自己修**的一类失败。
    ★ 只修字数：保真类失败绝不走这条路（见 `is_length_only_failure`）。
    """
    previous_message = LLMMessage(role="user", content=previous.content)
    repair_body = REPAIR_PROMPT.format(
        problems="\n".join(f"- {item}" for item in problems),
        previous=previous_message.content[-1200:],
        **_length_hints(settings),
    )
    return PromptPayload(
        prompt_version=GREETING_PROMPT_VERSION,
        task="greeting",
        messages=[
            LLMMessage(
                role="system",
                content=SYSTEM_PROMPT.format(banned="（同上一版要求）", **_length_hints(settings)),
            ),
            LLMMessage(role="user", content=repair_body),
        ],
        json_mode=True,
        max_tokens=800,
        temperature=0.3,  # 修复要"照着改"而不是重新发挥 → 用低温
    )


def _resume_digest(candidate: ResumeCandidate) -> str:
    """简历摘要（技能 + 项目要点）。**只给真话素材**。"""
    content = candidate.content
    lines = [f"方向：{candidate.direction or '未写'}"]
    if content.skills:
        lines.append("技能：" + "、".join(content.skills))
    for item in content.projects[:3]:
        lines.append(f"项目：{item.name}" + (f"（{item.role}）" if item.role else ""))
        for bullet in item.bullets[:2]:
            lines.append(f"  · {bullet}")
    if candidate.highest_degree:
        lines.append(f"学历：{candidate.highest_degree}")
    return "\n".join(lines)


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_greeting(content: str) -> LLMGreeting:
    """解析模型输出；任何不合规都抛 `ValueError`（由上层降级）。"""
    text = _FENCE_RE.sub("", (content or "").strip()).strip()
    if not text:
        raise ValueError("模型返回了空内容")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"JSON 顶层不是对象，而是 {type(data).__name__}")
    try:
        return LLMGreeting.model_validate(data)
    except ValidationError as exc:  # pragma: no cover - 两个字段都是 str，很少走到
        raise ValueError(f"字段不符合 schema：{exc.errors()[:3]}") from exc


# ===========================================================================
# 四道闸门
# ===========================================================================
def _gate_reasons(
    text: str,
    *,
    version: str,
    keywords: list[str],
    candidate: ResumeCandidate,
    settings: AppSettings,
    vocabulary: dict[str, list[str]],
    allowed_extra: set[str] | None = None,
) -> list[str]:
    """返回这条文案违反的硬约束（空列表 = 通过）。

    Args:
        allowed_extra: 闸门 ③（"只提 JD 关键词 ∩ 简历技能"）的**额外**允许项。
            用途：调用方已经确认"这些词这个 JD 真的要"（例如 JD 正文里抽到的
            交集词、或动作类通用词 `CAPABILITY_TERMS`），但受 `_MAX_KEYWORDS`
            截断没能进提示词列表 —— 不许因为截断就把真话判成"不贴 JD"。
            ⚠️ 默认 None = 只允许 `keywords`（保持闸门 ③ 的严格性）。
    """
    # ★ 字数这一道**复用** `_length_problems()`，不在这里另写一遍：
    #   两套措辞会让 `is_length_only_failure()` 判错（我第一版就是这么错的）。
    reasons: list[str] = _length_problems(text, version=version, settings=settings)

    for word in settings.greeting_banned_words:
        if word and word in text:
            reasons.append(f"用了禁用词「{word}」")

    if keywords and not any(word in text for word in keywords):
        reasons.append("一个 JD 关键词都没提到（不沾边）")

    if not text.rstrip().endswith(("？", "?")):
        reasons.append("结尾没有用问句引导回复")

    # ★ 方向词不是技能声称：开场白必然要说"我是 AI Agent 方向的应届生"，
    #   而词表会把里面的 "Agent" 抽成一个技能 → 不排除就会误报成编造。
    direction_tokens = set(resume_svc.extract_skills(candidate.direction or "", vocabulary))
    # ★★ ISSUE #31：`allowed_skills` 必须是 **JD 关键词**，不能带上 `candidate.pool`。
    #   原实现传 `set(keywords) | set(candidate.pool)`，而 `pool ⊇ keywords`，
    #   于是 `fidelity._skill_violations` 里的
    #       `skill not in allowed_skills and skill in pool`
    #   **永远为假** —— `skill_not_in_jd` 成了死代码，闸门 ③ 只兑现了
    #   "不许提简历外的技能"这一半。
    #   为什么这一半重要：用户的原痛点就是"写的话跟岗位根本不沾边"。
    #   简历里有 Vue/Redis，但这个 JD 只要 Python/FastAPI —— 开场白里堆一堆
    #   JD 没要求的技能，就是又一次"不沾边"。
    #   ⚠️ 别再用 `| set(candidate.pool)` 把它"修回去"：
    #   `tests/verify/test_v_p4_greeting.py::test_greeting_skill_not_in_jd_is_flagged`
    #   会立刻变红；而"真话不被误报"由同一文件的
    #   `test_fidelity_does_not_flag_a_truthful_greeting` 守住。
    #
    #   ★ 唯一要豁免的一类是 `resume_svc.CAPABILITY_TERMS`（动作/通用实践词：
    #   部署 / 性能优化 / 单元测试 …）。它们**不是技术栈名字**，不该被"JD 要不要求"
    #   这道尺子量 —— 否则"我用 Docker 把服务部署到服务器上"会因为「部署」不在 JD 里
    #   被判成"不贴 JD"，真话再次被降级成模板（这正是 P4 修过的 ISSUES #30 的同类
    #   误报，只是换了一道闸门冒出来）。技术栈名字（Vue/Redis/Git…）仍然严格。
    capability_terms = set(candidate.pool) & set(resume_svc.CAPABILITY_TERMS)
    allowed_skills = set(keywords) | capability_terms | set(allowed_extra or ())
    report = fidelity.check_text(
        text,
        resume=candidate.content,
        vocabulary=vocabulary,
        # ★ 一个关键词都没抽到时 `keywords` 是空的 —— 此时"只提 JD 关键词"无从谈起，
        #   传 `None` 表示"不限制"，而不是传空集合（空集合 = 任何技能都不许提，
        #   那会让"JD 没关键词"的岗位永远只能出模板）。但 `allowed_extra` 仍然生效。
        allowed_skills=allowed_skills if (keywords or allowed_extra) else None,
        learning_whitelist=set(settings.learning_whitelist),
        extra_evidence=direction_tokens,
    )
    reasons.extend(report.summary() for _ in [0] if not report.passed)
    return reasons


# ===========================================================================
# 模板兜底（保真、保字数、保沾边 —— 一定过闸门）
# ===========================================================================
def _template_greeting(
    job: Job,
    candidate: ResumeCandidate,
    *,
    keywords: list[str],
    variant: str,
    settings: AppSettings,
    gate_keywords: set[str] | None = None,
) -> Greeting:
    """确定性模板：**只用真话拼**，字数可控，一定过四道闸门。

    它是"模型写砸了"的兜底，所以它自己必须先满足全部硬约束 ——
    否则兜底本身就成了漏洞。`tests/unit/test_greeting_svc.py` 对它做了断言。

    Args:
        gate_keywords: 闸门 ③ 的额外允许项（见 `_gate_reasons`）。
            模板**自己**拼出来的词必须都在闸门的允许集合里，否则兜底就成了
            "一段永远过不了闸门的文本"（实测踩到：`jd_keywords_json` 里的词
            比 JD 正文多时，模板会被自己的闸门判成"不贴 JD"）。
    """
    greeting = settings.greeting
    direction = candidate.direction or "开发"
    project = next((item.name for item in candidate.content.projects if item.name), "")
    project_keywords = [
        word for word in keywords if any(word in item.skills for item in candidate.content.projects)
    ][:2]

    head = f"您好，我是{direction}方向的应届生。"
    tail = "方便的话，想请教一下这个岗位目前最看重哪一块？"

    def compose(keys: list[str], extra: str = "") -> str:
        if not keys:
            body = f"看到岗位描述里的要求，{extra or '我做过类似的项目。'}"
        elif variant == "with_project" and project:
            body = f"看到岗位要求{'、'.join(keys)}，我在{project}里实际用过{extra or '这些技术。'}"
        else:
            body = f"看到岗位要求{'、'.join(keys)}，{extra or '我在项目里都实际用过。'}"
        return head + body + tail

    full = compose(keywords[:3])
    if variant == "with_project" and project_keywords:
        full = compose(keywords[:3], "、".join(project_keywords) + "。")
        if project and project not in full:
            full = compose(keywords[:3], "、".join(project_keywords) + "。")

    # 太短 → 追加一句真话（有 GitHub 就附 GitHub，否则补一句通用的）
    github = greeting.github_url.strip()
    if len(full) < greeting.full_min_chars and github:
        full = full[: -len(tail)] + f"我的 GitHub 上有相关代码：{github}。" + tail
    if len(full) < greeting.full_min_chars:
        full = full[: -len(tail)] + "我目前在学校做的项目就是这类工程实践，上手很快。" + tail
    # 还是超长说明关键词太长 → 减少关键词个数重拼
    shrink = 3
    while len(full) > greeting.full_max_chars and shrink > 0:
        shrink -= 1
        full = compose(keywords[:shrink]) if shrink else compose([])
        if len(full) < greeting.full_min_chars:
            full = full[: -len(tail)] + "我做过相关项目，可以尽快上手。" + tail

    short_keys = [word for word in keywords if len(word) <= 8][:2]
    short = f"{direction}方向，{'、'.join(short_keys)} 有项目经验，方便聊聊吗？"
    if len(short) > greeting.short_max_chars:
        short = f"{direction}方向，有相关项目经验，方便聊聊吗？"
    if len(short) > greeting.short_max_chars:  # pragma: no cover - 兜到最简
        short = "有相关项目经验，方便聊聊吗？"

    issues: list[str] = []
    for version, text in (("full", full), ("short", short)):
        reasons = _gate_reasons(
            text,
            version=version,
            keywords=keywords,
            candidate=candidate,
            settings=settings,
            vocabulary=resume_svc.load_skill_vocabulary(settings),
            allowed_extra=gate_keywords,
        )
        issues.extend(f"模板-{version}：{reason}" for reason in reasons)
    return Greeting(
        full=full, short=short, variant=variant, source="template", issues=issues, keywords=keywords
    )


# ===========================================================================
# 对外主函数
# ===========================================================================
def generate_greeting(
    job: Job,
    candidate: ResumeCandidate,
    *,
    session: Session | None = None,
    provider: LLMProvider | None = None,
    settings: AppSettings | None = None,
    variant: str = "with_project",
    use_cache: bool = True,
) -> Greeting:
    """为一个岗位生成开场白（两个版本）。

    Args:
        session: 传了才写缓存；测试可以不传。
        provider: 不传或"未配置"→ 直接走模板（不报错：没配 key 也该能用工具）。
        variant: `with_project` / `without_project`。

    ★ 流程：查缓存 → 调模型 → 过四道闸门 → 不过则降级模板 → 写缓存。
      缓存里存的是**模型原始输出**（好让日后改判定口径时还能复用）。
    """
    settings = settings or get_settings()
    vocabulary = resume_svc.load_skill_vocabulary(settings)

    # 交集：JD 要什么 ∩ 我有什么 —— 开场白只能从这里取材
    jd_keywords = match_svc.decode_keywords(job.jd_keywords_json)
    if not jd_keywords:
        jd_keywords = match_svc.extract_jd_keywords(job.jd_text, settings, vocabulary=vocabulary)
    keywords = [word for word in jd_keywords if word in candidate.pool][:_MAX_KEYWORDS]

    # ★★ ISSUE #31 的另一半：`keywords` 既是"提示词的取材范围"，也是闸门 ③ 的尺子。
    #   两者必须是**同一个集合**，否则会出现"模板/提示词允许提，闸门却判它不贴 JD"
    #   的自相矛盾（实测：岗位的 `jd_keywords_json` 里多出一个正文已不存在的词，
    #   模板照它拼出来 → 闸门把模板自己判成违规 → 一句真话被降级成模板）。
    #   所以：
    #     · 提示词/模板用 `keywords`（受 `_MAX_KEYWORDS` 截断）；
    #     · 闸门用 `gate_keywords` = 提示词集合 **∪ 当前 JD 正文里真实出现的交集词**，
    #       再 ∪ `CAPABILITY_TERMS`（动作词，见 `_gate_reasons` 里的说明）。
    #   ★ 这样既不会误报真话，也没有把 #31 放宽回去：
    #     简历里有、但 JD 正文**和**已抽关键词里都没有的技术栈（Vue/Redis/Git…）
    #     照样会被判"JD 没要求"。
    jd_text_keywords = set(
        match_svc.extract_jd_keywords(job.jd_text, settings, vocabulary=vocabulary)
    ) & set(candidate.pool)

    if provider is None or not provider.is_configured():
        return _template_greeting(
            job,
            candidate,
            keywords=keywords,
            variant=variant,
            settings=settings,
            gate_keywords=jd_text_keywords,
        )

    # ★★ 先把提示词拼出来，再用**它自己的指纹**算缓存 key（ISSUES #55）。
    #   为什么必须这样：`variant`（with_project / without_project）、职位名、城市、
    #   `settings.greeting.github_url`、禁用词、关键词集合 —— 这些**都会进提示词**，
    #   但四个固定成分（prompt_version/model/jd_hash/resume_hash）一个都覆盖不到。
    #   实测后果：切了 A/B 版本，模型被要求写"别提项目名"，返回的却是上一版那段
    #   （`source` 还是 `llm`）—— 用户以为刚生成，其实一个字都没重算。
    payload = build_payload(
        job,
        candidate,
        keywords=keywords,
        variant=variant,
        settings=settings,
    )
    cache_key = llm_cache.compute_cache_key(
        prompt_version=GREETING_PROMPT_VERSION,
        model=provider.model,
        jd_hash=job.jd_hash,
        resume_content_hash=candidate.content_hash,
        task="greeting",
        prompt_fingerprint=llm_cache.payload_fingerprint(payload),
    )
    content = ""
    content_from_cache = False
    if session is not None and use_cache and cache_key:
        hit = llm_cache.get_cached(session, cache_key)
        if hit is not None:
            content = hit.content
            content_from_cache = True

    if not content:
        try:
            response = provider.complete(payload)
        except DeepSeekError as exc:
            logger.warning("开场白生成失败，降级模板：%s", exc)
            fallback = _template_greeting(
                job, candidate, keywords=keywords, variant=variant, settings=settings
            )
            fallback.issues.append(f"LLM 调用失败：{exc}")
            return fallback
        content = response.content
        if session is not None and cache_key and not response.is_empty:
            llm_cache.put_cached(
                session,
                cache_key,
                task="greeting",
                content=content,
                model=response.model or provider.model,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
                cost=response.cost,
            )

    try:
        parsed = parse_greeting(content)
    except ValueError as exc:
        fallback = _template_greeting(
            job,
            candidate,
            keywords=keywords,
            variant=variant,
            settings=settings,
            gate_keywords=jd_text_keywords,
        )
        fallback.issues.append(f"模型输出解析失败：{exc}")
        return fallback

    return _settle_versions(
        parsed,
        job=job,
        candidate=candidate,
        keywords=keywords,
        variant=variant,
        settings=settings,
        vocabulary=vocabulary,
        provider=provider,
        gate_keywords=jd_text_keywords,
        # 命中缓存时**没有本次的 payload**（#55 之后 payload 总是会拼出来，
        # 但那次调用并没有真的发生）→ 不做修复调用（省一次钱；缓存里的文本
        # 上次已经过闸门，要么合格要么就是当时判定的结果）
        previous_message=None if content_from_cache else payload.messages[-1],
    )


def _settle_versions(
    parsed: LLMGreeting,
    *,
    job: Job,
    candidate: ResumeCandidate,
    keywords: list[str],
    variant: str,
    settings: AppSettings,
    vocabulary: dict[str, list[str]],
    provider: LLMProvider | None,
    previous_message: LLMMessage | None,
    gate_keywords: set[str] | None = None,
) -> Greeting:
    """把模型输出"落定"成最终两个版本：过闸门 → 字数可修则修 → 仍不过则用模板。

    ★ 为什么单独抽出来：这段有 4 个决策点（哪版不合格、是否纯字数问题、
       修复后是否合格、是否降级），藏在 `generate_greeting` 里既难读也难测。
       抽出来后可以单独喂"模型输出"进来看结论（见 test_greeting_svc.py）。
    """
    template = _template_greeting(
        job,
        candidate,
        keywords=keywords,
        variant=variant,
        settings=settings,
        gate_keywords=gate_keywords,
    )
    final = {"full": parsed.full.strip(), "short": parsed.short.strip()}
    failures: dict[str, list[str]] = {}

    # ★ 这个内部函数**不要叫 `check`**：验证会话的合规红线测试会 AST 扫描所有 Call 节点，
    #   而 `check` 是 Playwright 勾选框的写操作方法名（在禁用名单里）。
    #   叫 `violations_of` 既准确（它返回"违反的约束"）又不会撞上那道守卫 ——
    #   撞上时应当改自己的命名，而不是去放宽守卫（P4.5 实测踩到）。
    def violations_of(text: str, version: str) -> list[str]:
        return _gate_reasons(
            text,
            version=version,
            keywords=keywords,
            candidate=candidate,
            settings=settings,
            vocabulary=vocabulary,
            allowed_extra=gate_keywords,
        )

    for version in ("full", "short"):
        reasons = violations_of(final[version], version)
        if reasons:
            failures[version] = reasons

    repaired: list[str] = []
    if failures and provider is not None and provider.is_configured() and previous_message:
        fixable = [
            version
            for version, reasons in failures.items()
            if is_length_only_failure(reasons, final[version], version=version, settings=settings)
        ]
        if fixable:
            problems = [item for version in fixable for item in failures[version]]
            try:
                repair_response = provider.complete(
                    build_repair_payload(previous_message, problems, settings=settings)
                )
                repaired_parsed = parse_greeting(repair_response.content)
            except (DeepSeekError, ValueError) as exc:
                logger.warning("字数修复调用失败，直接降级模板：%s", exc)
            else:
                for version in fixable:
                    fixed = (
                        repaired_parsed.full if version == "full" else repaired_parsed.short
                    ).strip()
                    if fixed and not violations_of(fixed, version):
                        final[version] = fixed
                        repaired.append(version)
                        del failures[version]

    issues: list[str] = []
    for version, reasons in failures.items():
        issues.extend(f"{version}：{reason}" for reason in reasons)
        final[version] = template.full if version == "full" else template.short

    return Greeting(
        full=final["full"],
        short=final["short"],
        variant=variant,
        source="template" if issues else "llm",
        issues=issues + [item for item in template.issues if issues],
        keywords=keywords,
        repaired=repaired,
    )

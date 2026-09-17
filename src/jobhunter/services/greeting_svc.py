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
GREETING_PROMPT_VERSION = 1

#: 送进提示词的 JD 关键词上限（太多会让模型抓不住重点，也推高 token）
_MAX_KEYWORDS = 8
#: 送进提示词的简历摘要上限
_MAX_RESUME_CHARS = 1600

VARIANTS = ("with_project", "without_project")

# ★ JSON 模式三件套：① 提示词含 "json" 字样（就在下面这段里）
#   ② 给 schema 示例 ③ max_tokens 给足（两个版本各一百多字，800 足够）。
SYSTEM_PROMPT = """你是求职开场白助手，帮求职者写发给 HR 的第一条消息。

硬性要求（违反任何一条这条消息就作废）：
1. 只能使用我提供的「可用素材」里的技能与项目，**绝对不许**添加素材之外的技能、
   数字或学历（例如素材里没有 Kubernetes，就不能写"熟悉 Kubernetes"）。
2. 完整版 80-150 字，精简版不超过 60 字（按字符数算，含标点）。
3. 不许用这些套话：{banned}
4. 语气像一个正常的年轻人说话，不要"尊敬的 HR""百忙之中"这类公文腔。
5. 结尾用一个**具体的、轻量的问题**引导对方回复（不要"期待您的回复"）。
6. 严格按下面的 json 结构输出，不要输出解释性文字、不要用 markdown 代码块。

json 输出结构（字段名必须完全一致）：
{{"full": "完整版开场白", "short": "精简版开场白"}}
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

    def text_for(self, version: str = "full") -> str:
        return self.short if version == "short" else self.full


def pick_variant(index: int, *, forced: str | None = None) -> str:
    """挑本次用哪个变体：显式指定优先，否则按序号**交替**（A/B 都要跑得到）。"""
    if forced in VARIANTS:
        return forced
    return VARIANTS[index % len(VARIANTS)]


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n…（已截断）"


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
        模型可能从 JD 里"看到"某个技能就顺手写出来。）
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

    user_content = (
        f"<jd>\n岗位：{job.title or '（未写职位）'} ｜ 城市：{job.city or '未写'}\n"
        f"JD 要求的关键词：{'、'.join(keywords) or '（没抽到）'}\n</jd>\n\n"
        f"<resume>\n{resume_text}\n</resume>\n\n"
        f"可用素材（只能从这里挑）：技能 {'、'.join(keywords) or '（无）'}\n"
        f"{project_hint}{github_hint}\n"
        f"请输出 json。"
    )
    return PromptPayload(
        prompt_version=GREETING_PROMPT_VERSION,
        task="greeting",
        messages=[
            LLMMessage(role="system", content=SYSTEM_PROMPT.format(banned=banned)),
            LLMMessage(role="user", content=user_content),
        ],
        json_mode=True,
        max_tokens=800,
        # 话术类任务用高温（llm/base.py 的约定）：同样输入要"像人写的"，
        # 而打分类任务才需要低温求稳定。
        temperature=0.7,
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
) -> list[str]:
    """返回这条文案违反的硬约束（空列表 = 通过）。"""
    reasons: list[str] = []
    greeting = settings.greeting

    if version == "full":
        if not greeting.full_min_chars <= len(text) <= greeting.full_max_chars:
            reasons.append(
                f"完整版字数 {len(text)} 不在 {greeting.full_min_chars}-{greeting.full_max_chars}"
            )
    elif len(text) > greeting.short_max_chars:
        reasons.append(f"精简版字数 {len(text)} 超过 {greeting.short_max_chars}")

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
    report = fidelity.check_text(
        text,
        resume=candidate.content,
        vocabulary=vocabulary,
        allowed_skills=set(keywords) | set(candidate.pool),
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
) -> Greeting:
    """确定性模板：**只用真话拼**，字数可控，一定过四道闸门。

    它是"模型写砸了"的兜底，所以它自己必须先满足全部硬约束 ——
    否则兜底本身就成了漏洞。`tests/unit/test_greeting_svc.py` 对它做了断言。
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

    if provider is None or not provider.is_configured():
        return _template_greeting(
            job, candidate, keywords=keywords, variant=variant, settings=settings
        )

    cache_key = llm_cache.compute_cache_key(
        prompt_version=GREETING_PROMPT_VERSION,
        model=provider.model,
        jd_hash=job.jd_hash,
        resume_content_hash=candidate.content_hash,
        task="greeting",
    )
    content = ""
    if session is not None and use_cache and cache_key:
        hit = llm_cache.get_cached(session, cache_key)
        if hit is not None:
            content = hit.content

    if not content:
        payload = build_payload(
            job, candidate, keywords=keywords, variant=variant, settings=settings
        )
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
            job, candidate, keywords=keywords, variant=variant, settings=settings
        )
        fallback.issues.append(f"模型输出解析失败：{exc}")
        return fallback

    # ★ 逐版本判定：某一版不合格就**只降级那一版**（另一版还能用）。
    template = _template_greeting(
        job, candidate, keywords=keywords, variant=variant, settings=settings
    )
    issues: list[str] = []
    final = {"full": parsed.full.strip(), "short": parsed.short.strip()}
    for version in ("full", "short"):
        reasons = _gate_reasons(
            final[version],
            version=version,
            keywords=keywords,
            candidate=candidate,
            settings=settings,
            vocabulary=vocabulary,
        )
        if reasons:
            issues.extend(f"{version}：{reason}" for reason in reasons)
            final[version] = template.full if version == "full" else template.short

    return Greeting(
        full=final["full"],
        short=final["short"],
        variant=variant,
        source="template" if issues else "llm",
        issues=issues + [item for item in template.issues if issues],
        keywords=keywords,
    )

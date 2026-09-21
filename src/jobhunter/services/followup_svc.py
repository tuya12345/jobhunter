"""跟进追问话术（`jh followup <job_id>`）—— P6。

╔══════════════════════════════════════════════════════════════════════════╗
║ 合规红线：本工具**不发送任何东西**                                          ║
╚══════════════════════════════════════════════════════════════════════════╝

它只把话术写好（连同"投了多少天""岗位是什么""我哪条经历最贴"），
发不发、什么时候发、要不要改，全由我决定。这与"不做自动投递"是同一条线。

★ 与开场白（P4）的关系：

    开场白：投递**之前**发给 HR 的第一条消息（`greeting_svc`）
    追问：  投递**之后** N 天没动静时的第二条消息（本模块）

  两者共用同一套"防编造"闸门（`fidelity.FidelityGuard`）与同一套素材口径
  （`JD 关键词 ∩ 简历技能`）—— 第二句话同样不能编，而它更容易编：
  人一着急就会"补"上自己没做过的东西。

★ 为什么必须有确定性模板兜底（与 `jh tailor analyze` 的取舍相反）：
  追问消息短、结构固定、事实来源明确，写一份**只用真话拼**的模板完全可行；
  而"字段级简历改写"没有诚实的模板，所以那边是直接报错。
  没配 key / 模型不过闸门时，这里降级成模板，而不是让用户白跑一趟。

★ 降级必须说出来（`issues`）：静默降级会让人以为"模型就这水平"，
  其实是闸门在保护他 —— 这与 P4 开场白的处理一致。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from jobhunter.config.settings import AppSettings, get_settings
from jobhunter.llm import cache as llm_cache
from jobhunter.llm.base import LLMMessage, LLMProvider, PromptPayload
from jobhunter.llm.deepseek import DeepSeekError
from jobhunter.llm.prompts import PromptFile, PromptFileError, load_prompt_file, render
from jobhunter.llm.safety import wrap_delimited
from jobhunter.models import Application, ApplicationEvent, Job
from jobhunter.services import apply_svc, fidelity, match_svc, resume_svc
from jobhunter.services.match_svc import ResumeCandidate
from jobhunter.utils.io import to_json
from jobhunter.utils.logging import get_logger
from jobhunter.utils.time import days_since, fmt_date_local, now_utc

logger = get_logger(__name__)

__all__ = [
    "FOLLOWUP_STYLES",
    "Followup",
    "FollowupError",
    "build_payload",
    "generate_followup",
    "load_followup_prompt",
    "parse_followup",
    "record_sent",
]

#: 提示词文件（版本号写在文件里）
PROMPT_NAME = "followup.md"
TASK = "followup"

#: 两档语气（计划书：`--variant 话术风格：简短 / 正式`）
FOLLOWUP_STYLES: tuple[str, ...] = ("简短", "正式")
#: 长度约束（追问消息不该长：对方在手机上扫一眼就要能回）
MAX_CHARS = 160
TARGET_LOW = 80
TARGET_HIGH = 140

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class FollowupError(ValueError):
    """追问话术生成失败（没投递记录、模型不可用等）。"""


@dataclass
class Followup:
    """一条追问话术及其溯源信息。"""

    text: str = ""
    style: str = "简短"
    #: llm / template —— 降级成模板时不要慌，那是**保护**生效了
    source: str = "template"
    #: 投出去多少天（None = 库里没有投递时间）
    days: int | None = None
    #: ★ `auto` = 从 `applications.applied_at` 算出来的；`user` = 用户用 `--days` 指定的
    #:   （JH-06：人工给的天数必须与"库里算出来的"区分开，CLI 会据此打印一句提醒）
    days_source: str = "auto"
    #: 走模板的原因（空 = 模型一次通过）
    issues: list[str] = field(default_factory=list)
    #: 这次用到的素材（排查"为什么提了这条经历"）
    material: list[str] = field(default_factory=list)
    job_id: int = 0


def load_followup_prompt(settings: AppSettings | None = None) -> PromptFile:
    """读 `prompts/followup.md`。"""
    try:
        return load_prompt_file(PROMPT_NAME, settings)
    except PromptFileError as exc:
        raise FollowupError(str(exc)) from exc


def _context(
    session: Session, job: Job, *, settings: AppSettings
) -> tuple[Application | None, ResumeCandidate, list[str], list[str], int | None]:
    """取这次追问需要的事实：投递记录 / 简历 / 交集素材 / 缺的技能 / 投了几天。"""
    application = apply_svc.get_latest_application(session, job.id)
    candidates = match_svc.load_resume_candidates(session, settings)
    if not candidates:
        raise FollowupError("库里还没有简历 —— 先 jh resume add（追问话术要引用真实经历）")
    candidate = candidates[0]

    result = match_svc.match_jd_resume(
        job.jd_text,
        candidate.content,
        resume_id=candidate.resume_id,
        resume_name=candidate.name,
        resume_direction=candidate.direction,
        job=job,
        settings=settings,
    )
    vocabulary = resume_svc.load_skill_vocabulary(settings)
    keywords = list(result.jd_keywords) or match_svc.extract_jd_keywords(
        job.jd_text, settings, vocabulary=vocabulary
    )
    material = [word for word in keywords if word in candidate.pool][:8]
    days = days_since(application.applied_at) if application and application.applied_at else None
    return application, candidate, material, list(result.missing_keywords), days


def build_payload(
    job: Job,
    candidate: ResumeCandidate,
    *,
    days: int | None,
    style: str,
    material: list[str],
    prompt: PromptFile,
    settings: AppSettings,
) -> PromptPayload:
    """构造追问话术的输入（素材只给"真话"，与开场白同一套口径）。"""
    banned = "、".join(settings.greeting_banned_words) or "（无）"
    jd_body = (
        f"岗位：{job.title or '（未写职位）'} ｜ 公司：{job.company or '未写'}\n"
        f"JD 要求的关键词：{'、'.join(material) or '（没抽到）'}"
    )
    facts = (
        f"投出去：{days if days is not None else '（库里没有投递时间）'} 天\n"
        f"当前状态：{job.status}\n"
        f"我确定会的（只能从这里挑）：{'、'.join(material) or '（无）'}"
    )
    user_content = (
        f"{wrap_delimited('jd', jd_body)}\n\n"
        f"{wrap_delimited('resume', _digest(candidate))}\n\n"
        f"{wrap_delimited('facts', facts)}\n\n"
        f"请输出 json（text 字段），风格：{style}。"
    )
    return PromptPayload(
        prompt_version=prompt.version,
        task=TASK,
        messages=[
            LLMMessage(
                role="system",
                content=render(
                    prompt.body,
                    style=style,
                    banned=banned,
                    max_chars=MAX_CHARS,
                    target_low=TARGET_LOW,
                    target_high=TARGET_HIGH,
                ),
            ),
            LLMMessage(role="user", content=user_content),
        ],
        json_mode=True,
        max_tokens=600,
        temperature=0.6,
    )


def _digest(candidate: ResumeCandidate) -> str:
    """简历要点（与开场白同源：技能 + 前几个项目的名与要点，只给真话）。"""
    content = candidate.content
    lines = [f"方向：{candidate.direction or '未写'}"]
    if content.skills:
        lines.append("技能：" + "、".join(content.skills))
    for item in content.projects[:3]:
        head = f"项目：{item.name}" + (f"（{item.role}）" if item.role else "")
        lines.append(head)
        lines.extend(f"  · {bullet}" for bullet in item.bullets[:2])
    for item in content.internships[:2]:
        lines.append(f"实习：{item.company} {item.role}".strip())
    return "\n".join(lines)


def parse_followup(content: str) -> str:
    """解析 `{"text": "..."}`；任何不合规抛 `ValueError`（报错不带原文，防 PII）。"""
    text = _FENCE_RE.sub("", (content or "").strip()).strip()
    if not text:
        raise ValueError("模型返回了空内容")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"不是合法 JSON（{exc.msg}，位置 {exc.pos}）") from exc
    if not isinstance(data, dict):
        raise ValueError(f"JSON 顶层不是对象，而是 {type(data).__name__}")
    value = str(data.get("text") or "").strip()
    if not value:
        raise ValueError("text 字段是空的")
    return value


def _violations(
    text: str,
    *,
    candidate: ResumeCandidate,
    material: list[str],
    settings: AppSettings,
    vocabulary: dict[str, list[str]],
) -> list[str]:
    """这条话术违反的硬约束（空列表 = 通过）。

    ★ 判定复用 P4/P5 的 `fidelity.check_text` —— 三处（开场白/追问/简历改写）
      必须是同一套口径，否则同一个词会在不同功能里得到相反结论。
    """
    reasons: list[str] = []
    if len(text) > MAX_CHARS:
        reasons.append(f"太长（{len(text)} 字 > {MAX_CHARS}）")
    for word in settings.greeting_banned_words:
        if word and word in text:
            reasons.append(f"用了禁用词「{word}」")
    # 追问的"沾边"要求比开场白松：只要提到一个真会的东西即可（这条消息的核心是"问一下进度"）
    if material and not any(word in text for word in material):
        reasons.append("一个 JD 关键词都没提到（容易被当成群发）")
    direction_tokens = set(resume_svc.extract_skills(candidate.direction or "", vocabulary))
    report = fidelity.check_text(
        text,
        resume=candidate.content,
        vocabulary=vocabulary,
        allowed_skills=None,  # 追问不强制贴 JD，只要求别说谎
        learning_whitelist=set(settings.learning_whitelist),
        extra_evidence=direction_tokens,
    )
    if not report.passed:
        reasons.append(report.summary())
    return reasons


def _template(
    job: Job,
    *,
    days: int | None,
    material: list[str],
    style: str,
) -> Followup:
    """确定性模板：**只用真话拼**，一定过闸门。

    它是"模型写砸了 / 没配 key"的兜底，所以它自己必须先满足全部硬约束 ——
    否则兜底本身就成了漏洞（P4 开场白模板同一条纪律）。
    """
    company = job.company or "贵司"
    title = job.title or "这个岗位"
    pick = "、".join(material[:2])
    when = f"{days} 天前" if days is not None else "前几天"

    if style == "正式":
        text = (
            f"您好，我{when}投递了{company}的{title}岗位。"
            f"我在项目里用过{pick}，和岗位要求比较对口。"
            f"想请问这个岗位目前还在推进吗？如果需要补充材料或安排面试，我随时可以配合，谢谢！"
        )
    else:
        text = (
            f"您好，我{when}投了{title}。我做过和{pick}相关的项目，和岗位要求比较对口。"
            f"想请问这个岗位现在还在推进吗？需要我补什么材料的话随时说，谢谢！"
        )
    if len(text) > MAX_CHARS:  # pragma: no cover - 极端长岗位名才会走到
        text = f"您好，我{when}投了{title}，想请问这个岗位还在推进吗？谢谢！"
    issues: list[str] = []
    if len(text) > MAX_CHARS:  # pragma: no cover
        issues.append(f"模板也太长（{len(text)} 字）")
    return Followup(
        text=text,
        style=style,
        source="template",
        days=days,
        material=material,
        issues=issues,
        job_id=job.id,
    )


def generate_followup(
    session: Session,
    job: Job,
    *,
    days: int | None = None,
    style: str | None = None,
    settings: AppSettings | None = None,
    provider: LLMProvider | None = None,
    use_cache: bool = True,
) -> Followup:
    """生成一条追问话术（**只生成，不发送**）。

    Args:
        days: 覆盖"投了几天"（默认按 `applied_at` 自动算）。
        style: 简短 / 正式（默认 简短）。

    ★ 三类失败一律降级成模板，并把原因写进 `issues`：
      没配 key、调用失败、过不了闸门。**绝不把没闸门的话术直接给用户**
      —— 它是要发给 HR 的。
    """
    settings = settings or get_settings()
    vocabulary = resume_svc.load_skill_vocabulary(settings)
    application, candidate, material, _missing, auto_days = _context(
        session, job, settings=settings
    )

    # ★★ "投了几天"是**事实**，不知道就不许编（只读代码审查 JH-06）。
    #
    #   原来这里 `days or auto_days`（None 就往下走），模板里 `days is None`
    #   会渲染成「我**前几天**投了…」—— 于是"没有投递记录 / 没有 applied_at"
    #   这种**不知道**的情况会生成一句具体的时间声明，用户复制粘贴就发出去了。
    #   而这个工具的全部价值建立在"只说真话"上（FidelityGuard 那一整套都是为此）。
    #
    #   现在的口径：
    #     · 有 `applied_at` → 自动算（原来的行为）；
    #     · 显式传 `days`   → 用它，但**标明这是用户给的**（`days_source="user"`），
    #       CLI 会打印一句提醒 —— 人工补写的时间不该看起来像从库里算出来的；
    #     · 两者都没有     → 报错，并给出两条可操作路径（去打标 / 显式给天数）。
    if days is not None:
        real_days = days
        days_source = "user"
    elif auto_days is not None:
        real_days = auto_days
        days_source = "auto"
    else:
        raise FollowupError(
            f"岗位 #{job.id} 没有「已投递」时间，算不出投了几天 —— 追问话术里那句"
            "「我 N 天前投了」不能凭空编。\n"
            f"  · 确实投过了：先打标 `jh apply {job.id} --status applied`"
            "（之后这条命令就能自动算天数）；\n"
            f"  · 只是想自己指定天数：`jh followup {job.id} --days 7`"
            "（那样文案里的天数是你给的，不是库里算出来的）。"
        )
    chosen_style = style if style in FOLLOWUP_STYLES else FOLLOWUP_STYLES[0]

    if provider is None or not provider.is_configured():
        result = _template(job, days=real_days, material=material, style=chosen_style)
        result.days_source = days_source
        result.issues.append("没有配置大模型密钥 → 用确定性模板")
        return result

    prompt = load_followup_prompt(settings)
    # ★★ 先把提示词拼出来，再用**它自己的指纹**算 key（ISSUES #24 / #55）：
    #   投了几天、风格、职位名、素材集合都会进提示词，而四个固定成分覆盖不到它们。
    payload = build_payload(
        job,
        candidate,
        days=real_days,
        style=chosen_style,
        material=material,
        prompt=prompt,
        settings=settings,
    )
    cache_key = llm_cache.compute_cache_key(
        prompt_version=prompt.version,
        model=provider.model,
        jd_hash=job.jd_hash,
        resume_content_hash=candidate.content_hash,
        task=TASK,
        prompt_fingerprint=llm_cache.payload_fingerprint(payload),
    )
    # ★ 指纹已经把"投了几天"与风格算进去了 —— 所以不再需要 #55 之前那种
    #   "只在两者都是默认值时才敢用缓存"的保守限制（那是权宜之计，白白多花钱）。
    cacheable = bool(cache_key)
    content = ""
    if use_cache and cacheable:
        hit = llm_cache.get_cached(session, cache_key)
        if hit is not None:
            content = hit.content
            # ★★ 命中计数在这里就落盘（只读代码审查 JH-12）。
            #
            #   `get_cached()` 刻意只 `flush()`（理由写在它自己的 docstring 里：
            #   调用方握着事务边界）。但**这条生成路径**的调用方（CLI）拿到结果后
            #   只打印、不提交 —— 于是 Session 关闭时那次 `UPDATE hits` 被回滚，
            #   `jh cache stats` 长期低估命中次数与省下的钱（一条"记账不存在"的失真）。
            #
            #   为什么在**这里**提交是安全的：此刻本函数还没有写过任何业务数据
            #   （只读了简历与缓存），所以这次提交只可能提交"命中计数"这一笔。
            #   反过来说：缓存真被命中了，这次省钱就是事实 —— 即使后面模型/闸门
            #   出问题降级成模板，那次命中也不该被抹掉。
            session.commit()

    # ★ 降级路径只有这一个出口：模板 + 原因 + **天数的来源**。
    #   （`days_source` 必须跟着结果一起走，否则 CLI 会把人工指定的天数
    #    当成本库算出来的 —— 那正是 JH-06 要消灭的那类失真。）
    def _fallback(reasons: list[str] | None = None) -> Followup:
        result = _template(job, days=real_days, material=material, style=chosen_style)
        result.days_source = days_source
        result.issues.extend(reasons or [])
        return result

    if not content:
        try:
            response = provider.complete(payload)
        except DeepSeekError as exc:
            return _fallback([f"调用模型失败：{exc}"])
        content = response.content
        if cacheable and content.strip():
            llm_cache.put_cached(
                session,
                cache_key,
                task=TASK,
                content=content,
                model=provider.model,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
                cost=response.cost,
            )
            session.commit()

    try:
        text = parse_followup(content)
    except ValueError as exc:
        return _fallback([f"模型输出解析失败：{exc}"])

    reasons = _violations(
        text,
        candidate=candidate,
        material=material,
        settings=settings,
        vocabulary=vocabulary,
    )
    if reasons:
        return _fallback(reasons)

    return Followup(
        text=text,
        style=chosen_style,
        source="llm",
        days=real_days,
        days_source=days_source,
        material=material,
        job_id=job.id,
    )


def record_sent(
    session: Session, job_id: int, *, text: str, style: str = "", days: int | None = None
) -> ApplicationEvent:
    """记一条"我追问了"的事件（`--sent` 时由 CLI 调用）。

    ★ 为什么把话术正文也写进 payload：事件流是**我自己的流水账**，
      回头能看出"我当时是怎么问的、对方怎么回的" —— 这是复盘的材料。
      它只存在本地库里，不出现在任何导出/日志里。
    """
    application = apply_svc.get_latest_application(session, job_id)
    if application is None:
        raise FollowupError(
            f"岗位 #{job_id} 还没有投递记录 —— 先 jh apply --job {job_id} --status applied"
        )
    event = ApplicationEvent(
        application_id=application.id,
        event_type="followup",
        payload_json=to_json({"style": style, "days": days, "text": text}),
        occurred_at=now_utc(),
    )
    session.add(event)
    session.flush()
    return event


def describe_days(days: int | None) -> str:
    """人话描述"投了多久"（CLI 与模板共用）。"""
    if days is None:
        return "库里没有投递时间（先 jh apply 打标）"
    if days <= 0:
        return "今天刚投"
    return f"投出去 {days} 天"


def applied_text(application: Application | None) -> str:
    """投递时间的中文写法（没投递记录时给空串）。"""
    if application is None or application.applied_at is None:
        return ""
    return fmt_date_local(application.applied_at)


def to_dict(result: Followup) -> dict[str, Any]:
    """给导出/测试用的形状。"""
    return {
        "job_id": result.job_id,
        "text": result.text,
        "style": result.style,
        "source": result.source,
        "days": result.days,
        "issues": list(result.issues),
        "material": list(result.material),
    }

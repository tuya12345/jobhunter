"""面试问题：记录（recorded）+ 按 JD 预测（predicted）—— P6 的核心。

╔══════════════════════════════════════════════════════════════════════════╗
║ 这张表为什么能"把记录工具变成准备工具"                                      ║
╚══════════════════════════════════════════════════════════════════════════╝

只记"我被问了什么"，价值发生在**面试之后**；而预测"我可能被问什么"，
价值发生在**面试之前** —— 那才是真正能改变结果的时刻。

    记录路径（`jh question add`）  → source="recorded"，挂在**某场面试**上
    预测路径（`jh question predict`）→ source="predicted"，挂在**某个岗位**上
                                     （迁移 0002 为此把 interview_id 改成可空、加了 job_id）

★ 两条路径必须严格区分（`models/interview.py` 与 `constants.py` 都写了这条）：
  `jh weak` 的结论是"我最该补哪个知识点"。把"我自己猜的题"和"真实被问的题"
  混在一起统计，结论会完全失真 —— 要么把"自己吓自己"当成能力缺口，
  要么漏掉真正答不上来的弱项。所以 `weak_svc` 默认**分开展示**。

★ 为什么预测题**不**过 FidelityGuard（P5 那套"防编造"闸门）：
  那个闸门判的是"这句话有没有声称简历里没有的事实"。而预测题是**提问**：
  "你了解过 Kubernetes 吗"恰恰是简历里没有 Kubernetes 时最该准备的一题。
  按编造拦掉它，等于把最有价值的一类准备题删掉。
  （真正会写进简历/发给 HR 的文案才需要闸门 —— 见 `tailor_svc` / `followup_svc`。）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunter.config.settings import AppSettings, get_settings
from jobhunter.llm import cache as llm_cache
from jobhunter.llm.base import LLMMessage, LLMProvider, PromptPayload
from jobhunter.llm.deepseek import DeepSeekError
from jobhunter.llm.prompts import PromptFile, PromptFileError, load_prompt_file, render
from jobhunter.llm.safety import wrap_delimited
from jobhunter.models import Interview, InterviewQuestion, Job
from jobhunter.models.constants import QUESTION_RATINGS
from jobhunter.services import match_svc
from jobhunter.services.match_svc import ResumeCandidate
from jobhunter.utils.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "PREDICT_PROMPT_NAME",
    "PredictedQuestion",
    "PredictResult",
    "QuestionError",
    "QuestionRow",
    "add_question",
    "list_questions",
    "parse_prediction",
    "predict_questions",
    "predict_payload",
]

#: 预测提示词文件（版本号写在文件里，见 `llm/prompts.py`）
PREDICT_PROMPT_NAME = "question.md"
#: 送进提示词的简历要点上限
_MAX_RESUME_CHARS = 3000
_MAX_JD_CHARS = 3000
#: 缓存任务名（与 match / greeting / tailor 区分开，避免 key 撞车）
TASK = "question_predict"

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class QuestionError(ValueError):
    """问题记录/预测失败（面试不存在、分类非法、模型输出不可用等）。"""


class PredictedQuestion(BaseModel):
    """模型预测出来的一题。"""

    question: str = ""
    category: str = ""
    why: str = ""


class PredictedList(BaseModel):
    """模型输出的顶层结构。"""

    questions: list[PredictedQuestion] = Field(default_factory=list)


@dataclass
class QuestionRow:
    """一行问题（CLI 渲染用）。"""

    question: InterviewQuestion
    interview_id: int | None
    job_id: int | None
    company: str = ""
    title: str = ""
    round_: str | None = None

    @property
    def source_label(self) -> str:
        return "预测" if self.question.source == "predicted" else "实录"


@dataclass
class PredictResult:
    """一次预测的结果。"""

    questions: list[PredictedQuestion] = field(default_factory=list)
    #: 真正写进库的行
    stored: list[InterviewQuestion] = field(default_factory=list)
    #: 因为"这个岗位已经有一模一样的题"而跳过的
    skipped: list[str] = field(default_factory=list)
    prompt_version: int = 0
    from_cache: bool = False


# ===========================================================================
# ① 记录（recorded）
# ===========================================================================
def add_question(
    session: Session,
    *,
    question: str,
    interview_id: int | None = None,
    job_id: int | None = None,
    answer: str | None = None,
    rating: str | None = None,
    category: str | None = None,
    weak: bool = False,
    note: str | None = None,
) -> InterviewQuestion:
    """记一道**真实被问到**的题。

    Args:
        interview_id / job_id: 二选一。
            · 有面试记录 → 填 `interview_id`（归属最准确，`jh weak` 能追到是哪一场）；
            · 面完忘了是哪一场、只记得岗位 → 填 `job_id`（`interview_id` 现在是可空的，
              正是为了这种真实情况）。
        rating: `bad` / `ok` / `good`（3 档，样本小时 5 档全是噪声）。

    Raises:
        QuestionError: 既没给面试也没给岗位、面试/岗位不存在、rating 不认识。
    """
    text = (question or "").strip()
    if not text:
        raise QuestionError("问题不能是空的")
    if interview_id is None and job_id is None:
        raise QuestionError("要么给 --interview <面试ID>，要么给 --job <岗位ID>")
    if rating is not None and rating not in QUESTION_RATINGS:
        raise QuestionError(f"自评 {rating!r} 不认识。可用：{'、'.join(QUESTION_RATINGS)}")

    if interview_id is not None:
        interview = session.get(Interview, interview_id)
        if interview is None:
            raise QuestionError(f"找不到面试 id={interview_id}。用 jh interview list 看有哪些。")
    if job_id is not None and session.get(Job, job_id) is None:
        raise QuestionError(f"找不到岗位 id={job_id}")

    row = InterviewQuestion(
        interview_id=interview_id,
        job_id=job_id,
        question=text,
        my_answer=answer,
        rating=rating,
        category=(category or "").strip() or None,
        is_weak=bool(weak),
        source="recorded",
        note=note,
    )
    session.add(row)
    session.flush()
    return row


# ===========================================================================
# ② 预测（predicted）
# ===========================================================================
def _digest(candidate: ResumeCandidate) -> str:
    """简历要点（比开场白用的摘要更全：面试题要顺着项目细节问）。

    ★ 为什么不复用 `greeting_svc._resume_digest`：那个摘要是为"写一句话"裁剪过的
      （只取 3 个项目、每个 2 条要点，还要压到 1600 字）。面试预测需要**更多细节**
      才问得出有信息量的问题，两者的取舍不同，刻意分开。
    """
    content = candidate.content
    lines = [f"方向：{candidate.direction or '未写'}"]
    if content.skills:
        lines.append("技能栏：" + "、".join(content.skills))
    for item in content.projects[:5]:
        head = f"项目：{item.name}"
        if item.role:
            head += f"（{item.role}）"
        if item.period:
            head += f" {item.period}"
        lines.append(head)
        lines.extend(f"  · {bullet}" for bullet in item.bullets[:4])
        if item.skills:
            lines.append("  用到的技术：" + "、".join(item.skills))
    for item in content.internships[:3]:
        lines.append(f"实习：{item.company} {item.role} {item.period}".strip())
        lines.extend(f"  · {bullet}" for bullet in item.bullets[:3])
    if candidate.highest_degree:
        lines.append(f"学历：{candidate.highest_degree}")
    return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n…（已截断）"


def load_predict_prompt(settings: AppSettings | None = None) -> PromptFile:
    """读 `prompts/question.md`（版本号在文件里）。"""
    try:
        return load_prompt_file(PREDICT_PROMPT_NAME, settings)
    except PromptFileError as exc:
        raise QuestionError(str(exc)) from exc


def predict_payload(
    job: Job,
    candidate: ResumeCandidate,
    *,
    matched: list[str],
    missing: list[str],
    count: int,
    prompt: PromptFile,
) -> PromptPayload:
    """构造"按 JD 预测面试题"的输入。

    ★ JD 用 `wrap_delimited` 包起来（不可信输入，防标签提前闭合，ISSUES #2/#8/#29）。
    """
    jd_body = (
        f"岗位：{job.title or '（未写职位）'} ｜ 公司：{job.company or '未写'}"
        f" ｜ 城市：{job.city or '未写'}\n"
        f"JD 正文：\n{_clip(job.jd_text or '（没抓到正文）', _MAX_JD_CHARS)}"
    )
    facts = (
        f"我命中了的技能：{'、'.join(matched) or '（无）'}\n"
        f"我缺的技能（可以问「了解过吗」，但不能问「你在项目里怎么用的」）："
        f"{'、'.join(missing) or '（无）'}"
    )
    user_content = (
        f"{wrap_delimited('jd', jd_body)}\n\n"
        f"{wrap_delimited('resume', _clip(_digest(candidate), _MAX_RESUME_CHARS))}\n\n"
        f"{wrap_delimited('facts', facts)}\n\n"
        f"请按系统提示词的要求输出 json（questions 数组，正好 {count} 条）。"
    )
    return PromptPayload(
        prompt_version=prompt.version,
        task=TASK,
        messages=[
            LLMMessage(role="system", content=render(prompt.body, count=count)),
            LLMMessage(role="user", content=user_content),
        ],
        json_mode=True,
        # 5 道题 × （问题 + 分类 + 理由）≈ 600 token，给 1200 留足余量
        max_tokens=1200,
        # 预测题要"像真人问的"，但也要稳定 → 中温
        temperature=0.5,
    )


def parse_prediction(content: str) -> list[PredictedQuestion]:
    """解析模型输出；不合规抛 `ValueError`。

    ★ 报错不带模型原文（可能含简历细节，见 P5 的经验）：只说清楚哪里不合法。
    """
    text = _FENCE_RE.sub("", (content or "").strip()).strip()
    if not text:
        raise ValueError("模型返回了空内容")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"不是合法 JSON（{exc.msg}，位置 {exc.pos}）") from exc
    if isinstance(data, list):  # 模型偶尔直接给数组
        data = {"questions": data}
    if not isinstance(data, dict):
        raise ValueError(f"JSON 顶层不是对象，而是 {type(data).__name__}")
    try:
        parsed = PredictedList.model_validate(data)
    except ValidationError as exc:
        fields = "、".join(".".join(str(p) for p in item["loc"]) for item in exc.errors()[:3])
        raise ValueError(f"字段不符合 schema（问题字段：{fields}）") from exc
    out = [item for item in parsed.questions if item.question.strip()]
    if not out:
        raise ValueError("questions 是空的（模型没给出任何问题）")
    return out


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def _pick_candidate(
    session: Session, *, resume_id: int | None, settings: AppSettings
) -> ResumeCandidate:
    """挑一份简历（显式指定优先，否则用第一份）。"""
    candidates = match_svc.load_resume_candidates(session, settings)
    if not candidates:
        raise QuestionError("库里还没有简历 —— 先 jh resume add，预测题需要知道你会什么")
    if resume_id is None:
        return candidates[0]
    for item in candidates:
        if item.resume_id == resume_id:
            return item
    raise QuestionError(f"找不到简历 #{resume_id}")


def _model_content(
    session: Session,
    job: Job,
    candidate: ResumeCandidate,
    *,
    matched: list[str],
    missing: list[str],
    count: int,
    prompt: PromptFile,
    provider: LLMProvider,
    use_cache: bool,
) -> tuple[str, bool]:
    """取模型输出（命中缓存就直接用），返回 `(content, from_cache)`。

    ★ 抽出来的理由不只是"分支太多"：缓存与真调用这两条路径的**写入语义不同**
      （缓存要 commit 才算真的花了钱不白花），单独放一处才不会漏。
    """
    cache_key = llm_cache.compute_cache_key(
        prompt_version=prompt.version,
        model=provider.model,
        jd_hash=job.jd_hash,
        resume_content_hash=candidate.content_hash,
        task=TASK,
    )
    if use_cache and cache_key:
        hit = llm_cache.get_cached(session, cache_key)
        if hit is not None:
            return hit.content, True

    payload = predict_payload(
        job,
        candidate,
        matched=matched,
        missing=missing,
        count=count,
        prompt=prompt,
    )
    try:
        response = provider.complete(payload)
    except DeepSeekError as exc:
        raise QuestionError(f"调用模型失败：{exc}") from exc
    if cache_key and response.content.strip():
        llm_cache.put_cached(
            session,
            cache_key,
            task=TASK,
            content=response.content,
            model=provider.model,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost=response.cost,
        )
        # ★ 花钱拿到的结果必须立刻落库：不 commit 的话，后面任何异常
        #   （包括"用户只是看看、命令报错退出"）都会让这次调用白花钱。
        session.commit()
    return response.content, False


def predict_questions(
    session: Session,
    job: Job,
    *,
    count: int = 5,
    resume_id: int | None = None,
    settings: AppSettings | None = None,
    provider: LLMProvider | None = None,
    use_cache: bool = True,
) -> PredictResult:
    """按 JD 预测面试题并**写进库**（`source="predicted"`，挂在岗位下）。

    ★ 去重：同一个岗位已经有一模一样的题就跳过（模型重复跑会给出高度相似的题，
      库里堆三份"你负责哪部分"对准备毫无帮助）。
      口径是"归一化后完全相同"——**不做模糊相似判断**：那会引入一个
      "到底算不算重复"的主观阈值，而它错了会让用户丢掉该看的题。
    """
    settings = settings or get_settings()
    prompt = load_predict_prompt(settings)
    candidate = _pick_candidate(session, resume_id=resume_id, settings=settings)

    result = match_svc.match_jd_resume(
        job.jd_text,
        candidate.content,
        resume_id=candidate.resume_id,
        resume_name=candidate.name,
        resume_direction=candidate.direction,
        job=job,
        settings=settings,
    )

    if provider is None or not provider.is_configured():
        raise QuestionError(
            "没有配置大模型密钥（DEEPSEEK_API_KEY），无法预测面试题。\n"
            "  · 想看已经记过的题：jh weak / jh interview list\n"
            "  · 要用预测：把 key 写进项目根的 .env"
        )

    content, from_cache = _model_content(
        session,
        job,
        candidate,
        matched=result.matched_keywords,
        missing=result.missing_keywords,
        count=count,
        prompt=prompt,
        provider=provider,
        use_cache=use_cache,
    )

    try:
        predicted = parse_prediction(content)
    except ValueError as exc:
        raise QuestionError(f"模型输出解析失败：{exc}") from exc

    existing = {
        _norm(row.question)
        for row in session.scalars(
            select(InterviewQuestion).where(InterviewQuestion.job_id == job.id)
        )
    }
    out = PredictResult(questions=predicted, prompt_version=prompt.version, from_cache=from_cache)
    for item in predicted:
        text = item.question.strip()
        if _norm(text) in existing:
            out.skipped.append(text)
            continue
        row = InterviewQuestion(
            interview_id=None,
            job_id=job.id,
            question=text,
            category=(item.category or "").strip() or None,
            source="predicted",
            is_weak=False,
            note=(item.why or "").strip() or None,
        )
        session.add(row)
        existing.add(_norm(text))
        out.stored.append(row)
    session.flush()
    logger.debug(
        "预测面试题：job=%s 得到 %s 条，新入库 %s 条", job.id, len(predicted), len(out.stored)
    )
    return out


# ===========================================================================
# ③ 查询
# ===========================================================================
def list_questions(
    session: Session,
    *,
    interview_id: int | None = None,
    job_id: int | None = None,
    source: str | None = None,
) -> list[QuestionRow]:
    """列出问题（可按面试 / 岗位 / 来源过滤），新→旧。"""
    query = (
        select(InterviewQuestion, Interview, Job)
        .outerjoin(Interview, InterviewQuestion.interview_id == Interview.id)
        .outerjoin(Job, InterviewQuestion.job_id == Job.id)
        .order_by(InterviewQuestion.id.desc())
    )
    if interview_id is not None:
        query = query.where(InterviewQuestion.interview_id == interview_id)
    if job_id is not None:
        query = query.where(InterviewQuestion.job_id == job_id)
    if source is not None:
        query = query.where(InterviewQuestion.source == source)

    rows: list[QuestionRow] = []
    for question, interview, job in session.execute(query).all():
        rows.append(
            QuestionRow(
                question=question,
                interview_id=question.interview_id,
                job_id=question.job_id,
                company=(job.company if job else "") or "",
                title=(job.title if job else "") or "",
                round_=interview.round if interview else None,
            )
        )
    return rows


def job_for_question(session: Session, question: InterviewQuestion) -> Job | None:
    """取一道题最终归属的岗位（`interview → application → job` 或直接 `job_id`）。"""
    if question.job_id is not None:
        return session.get(Job, question.job_id)
    if question.interview_id is None:
        return None
    interview = session.get(Interview, question.interview_id)
    if interview is None:
        return None
    from jobhunter.models import Application

    application = session.get(Application, interview.application_id)
    if application is None:
        return None
    return session.get(Job, application.job_id)


def to_json_row(row: InterviewQuestion) -> dict[str, Any]:
    """问题的可打印形状（`jh report` 与将来的导出共用）。"""
    return {
        "id": row.id,
        "question": row.question,
        "answer": row.my_answer,
        "rating": row.rating,
        "category": row.category,
        "is_weak": row.is_weak,
        "source": row.source,
    }

"""`jh tailor` 的服务层（P5）：brief（讲什么）→ analyze（改什么）→ approve → export。

╔══════════════════════════════════════════════════════════════════════════╗
║ 这一层只做一件事：**把"模型想改什么"和"我允许改什么"分开**                    ║
╚══════════════════════════════════════════════════════════════════════════╝

    模型负责说"这句话可以改得更好"，
    代码负责判断"这个改法有没有编造"（`fidelity.FidelityGuard`），
    我负责最终点头（`approve`）。

★ 为什么一定是三步而不是"一键生成新简历"：
  改简历是**发给 HR 的**。让模型直接产出一份改好的简历，
  意味着"编造"只要骗过一次检查就发出去了。逐条审核把风险切成了一条条
  可以单独否掉的小块，而且每条都留着"为什么这么改"（reason）。

★ FidelityGuard 在这里被调用**三次**（计划书的硬要求，缺一不可）：

    ① analyze 生成后：逐条校验，违规的建议**直接丢掉**（不写库），
       并记下丢掉的原因 —— 否则模型编一句"熟悉 Kubernetes"就会静默进库，
       等我点到 approve 时已经默认它是真的了。
    ② approve 批准时：**再校验一遍**。理由：判定口径会变严（改了配置/改了词表），
       而库里的建议是历史数据；"上次检查过"不能当成"现在也合格"。
    ③ export 导出时：校验"最终写进文件的那份文本"（逐条 + 整篇两道）。
       这是最后一道：前面的检查再全，也保不住"替换逻辑本身把文本搞乱"。

★ 顺带一件工程上的事：`analyze` 的缓存 key 用的是**统一入口**
  `llm_cache.compute_cache_key(task="tailor")`，与 match / greeting 同一套
  （含 `resume.content_hash`）→ 改了简历必然重算，不会拿到旧建议。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunter.config.settings import AppSettings, get_settings
from jobhunter.llm import cache as llm_cache
from jobhunter.llm.base import LLMMessage, LLMProvider, PromptPayload
from jobhunter.llm.deepseek import DeepSeekError
from jobhunter.llm.prompts import (
    PromptFile,
    PromptFileError,
    load_prompt_file,
    prompt_path,
)
from jobhunter.llm.safety import wrap_delimited
from jobhunter.models import Job, Resume, ResumeVersion
from jobhunter.services import match_svc, resume_svc
from jobhunter.services.fidelity import FidelityGuard, FidelityReport
from jobhunter.services.match_svc import ResumeCandidate
from jobhunter.utils.docx import DocLine, write_docx, write_markdown
from jobhunter.utils.io import ensure_dir
from jobhunter.utils.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "Brief",
    "BriefPoint",
    "ExportResult",
    "MatchAnalysis",
    "Suggestion",
    "TailorAnalysis",
    "TailorError",
    "TailorFidelityError",
    "TailorPrompt",
    "TailorResult",
    "analyze",
    "analyze_and_save",
    "approve",
    "build_brief",
    "build_payload",
    "export_version",
    "list_versions",
    "load_prompt",
    "load_version",
    "parse_analysis",
    "render_system_prompt",
    "suggestions_of",
]

#: LLM 缓存里的任务名（与 match / greeting 区分开，避免 key 撞上）
TASK = "tailor"

#: 提示词文件名（相对项目根的 prompts/）。★ 计划书要求它放在那里，
#: 而不是散在 Python 里：提示词是要反复改的东西，改一次就得能看出"改了哪一版"。
PROMPT_NAME = "tailor.md"

#: 送进提示词的简历正文上限。一份两页简历大约 2000-3000 字，
#: 6000 足够放下，又能挡住"简历里塞了一篇小说"这种极端输入把 token 打爆。
_MAX_RESUME_CHARS = 6000
_MAX_JD_CHARS = 3000

#: 提示词文件里声明的版本号：`<!-- prompt-version: 3 -->`（解析在 `llm/prompts.py`）
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
#: 文本归一：比较"原文是否逐字存在"时忽略所有空白（Word 里的换行/空格不可靠）
_WS_RE = re.compile(r"\s+")

#: `sections` 的内部 key → 导出文档里显示的中文小节名
SECTION_TITLES: dict[str, str] = {
    "contact": "个人信息",
    "intention": "求职意向",
    "education": "教育经历",
    "skills": "专业技能",
    "projects": "项目经历",
    "internships": "实习经历",
    "campus": "校园经历",
    "awards": "荣誉奖项",
    "certificates": "证书",
    "summary": "自我评价",
}
#: 不单独占一节的小节（"header" 是简历顶部的姓名/联系方式，已经作为标题写了）
_SKIP_SECTIONS = frozenset({"header"})


class TailorError(RuntimeError):
    """定制简历的业务错误（CLI 把它显示成人话，不打印 traceback）。"""


class TailorFidelityError(TailorError):
    """被 FidelityGuard 拦下（编造嫌疑）。

    ★ 单独一个类型：CLI 要把它渲染成"**拒绝写入** + 具体哪一句为什么"，
      而不是普通的"出错了"——用户需要知道是**哪一条建议**有问题。
    """

    def __init__(self, message: str, *, violations: list[str] | None = None) -> None:
        super().__init__(message)
        self.violations = violations or []


class TailorPromptError(TailorError):
    """提示词文件缺失/损坏（宁可明确报错，也不要偷偷用一份内置的旧提示词）。"""


# ===========================================================================
# 提示词加载（prompts/tailor.md，七要素齐全）
# ===========================================================================
#: ★ P6 起，加载逻辑抽到 `llm/prompts.py`（question / followup 也要用同一套纪律）。
#: 这里只保留"文件名 + 把共享异常翻译成本模块异常"这一层，免得三处各写一套。
PROMPT_NAME = "tailor.md"

#: 兼容别名：`TailorPrompt` 现在就是共享层的 `PromptFile`
TailorPrompt = PromptFile


def _prompt_path(settings: AppSettings, path: Path | str | None = None) -> Path:
    """定位 `prompts/tailor.md`（转调 `llm/prompts.prompt_path`）。

    ★ 这个私有包装**刻意保留**：验证会话的独立用例
      （`tests/verify/test_v_p5.py::test_prompt_fingerprint_guard_is_armed`）
      通过它拿到"真实提示词文件"，再自己算摘要来验证指纹守卫真的上膛了。
      他们不该被迫在测试里硬编码一个相对路径 —— 那是把实现细节复制一份。
    """
    return prompt_path(PROMPT_NAME, settings, path=path)


def load_prompt(
    settings: AppSettings | None = None, *, path: Path | str | None = None
) -> PromptFile:
    """读 `prompts/tailor.md`（版本号写在文件第一行）。

    ★ 版本号为什么写在**文件里**而不是 Python 常量：缓存 key 里必须含版本号，
      而"改了文件忘了改常量"是必然会发生的。写进文件后，
      `tests/unit/test_tailor_svc.py` 的"正文指纹"测试会在
      "正文变了但版本没 +1"时直接失败 —— 把口头纪律变成一次红灯。
    ★ 为什么把 `PromptFileError` 翻译成 `TailorPromptError`：
      调用方只认识"定制简历的提示词错误"，不该被迫 import 两个异常类型。
    """
    try:
        return load_prompt_file(PROMPT_NAME, settings, path=path)
    except PromptFileError as exc:
        raise TailorPromptError(str(exc)) from exc


def render_system_prompt(prompt: TailorPrompt, settings: AppSettings) -> str:
    """把提示词正文里的 `$变量` 填成本次运行的实际约束。

    ★ 用 `string.Template`（`$var`）而不是 `str.format`（`{var}`）：
      提示词里必然有 JSON 结构示例（`{...}`），用 format 会把它们当成占位符 ——
      要么报 KeyError，要么要写一堆 `{{`，改一次提示词就要数一遍括号。
    """
    tailor = settings.tailor
    return Template(prompt.body).safe_substitute(
        max_suggestions=tailor.max_suggestions,
        max_ratio_percent=int(tailor.max_length_ratio * 100),
    )


# ===========================================================================
# 数据结构
# ===========================================================================
class Suggestion(BaseModel):
    """一条字段级建议（模型的输出形状，与提示词里的 schema 一一对应）。"""

    section: str = ""
    original: str = ""
    revised: str = ""
    reason: str = ""


class MatchAnalysis(BaseModel):
    """匹配分析（模型对"我 vs 这个 JD"的判断）。"""

    score: int = 0
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    missing_keywords: list[str] = Field(default_factory=list)


class TailorAnalysis(BaseModel):
    """`jh tailor analyze` 的模型输出。"""

    match_analysis: MatchAnalysis = Field(default_factory=MatchAnalysis)
    suggestions: list[Suggestion] = Field(default_factory=list)


@dataclass
class DroppedSuggestion:
    """被闸门拦下、**没有**写进库的建议。"""

    suggestion: Suggestion
    reasons: list[str]


@dataclass
class TailorResult:
    """一次 analyze 的结果（含每条建议的去留）。"""

    job_id: int
    resume_id: int | None
    resume_name: str
    prompt_version: int
    analysis: TailorAnalysis
    #: 通过闸门、可以逐条审核的建议（顺序即 approve 时用的编号）
    kept: list[Suggestion] = field(default_factory=list)
    #: 被拦下的建议 + 原因（要给用户看，绝不能静默丢）
    dropped: list[DroppedSuggestion] = field(default_factory=list)
    #: 合并后的闸门报告（落 `resume_versions.fidelity_report_json`）
    report: FidelityReport = field(default_factory=FidelityReport)
    #: 命中缓存（没有花钱）
    from_cache: bool = False
    #: 写库后的版本 id（没写库时为 None）
    version_id: int | None = None

    @property
    def refused(self) -> bool:
        """是否"整批被拒"（有建议被拦，且没有一条留下来）。

        ★ 这种情况**不写库**：写一条"0 条建议"的版本记录只会让人以为跑成功了。
        """
        return bool(self.dropped) and not self.kept


# ===========================================================================
# ① brief：不花钱的"面试讲什么"
# ===========================================================================
@dataclass
class BriefPoint:
    """一个"重点讲"的项目。"""

    index: int
    name: str
    role: str
    hits: list[str] = field(default_factory=list)
    detail: str = ""
    line: str = ""


@dataclass
class Brief:
    """`jh tailor brief` 的结果（全部是**确定性**计算，不调模型）。"""

    job_id: int
    title: str
    company: str
    score: int
    score_source: str
    resume_id: int | None
    resume_name: str
    direction: str
    points: list[BriefPoint] = field(default_factory=list)
    highlight: list[str] = field(default_factory=list)
    highlight_note: str = ""
    avoid: list[str] = field(default_factory=list)
    #: 讲法自检（brief 自己也不会教我编造）
    report: FidelityReport = field(default_factory=FidelityReport)
    notes: list[str] = field(default_factory=list)


def _pick_candidate(
    session: Session,
    job: Job,
    *,
    resume_id: int | None,
    settings: AppSettings,
) -> ResumeCandidate:
    """选一份简历：显式指定优先，否则用"对这个 JD 匹配最好"的那份。"""
    candidates = match_svc.load_resume_candidates(session, settings)
    if not candidates:
        raise TailorError("库里还没有简历。先跑：jh resume add <简历文件>")

    if resume_id is not None:
        for candidate in candidates:
            if candidate.resume_id == resume_id:
                return candidate
        raise TailorError(f"找不到简历 #{resume_id}。用 jh resume list 看有哪些。")

    best = match_svc.match_best_candidate(job.jd_text, candidates, job=job, settings=settings)
    if best is not None and best.matched_resume_id is not None:
        for candidate in candidates:
            if candidate.resume_id == best.matched_resume_id:
                return candidate
    return candidates[0]


def _keyword_hits(text: str, keywords: list[str], vocabulary: dict[str, list[str]]) -> list[str]:
    """`text` 里出现了哪些关键词（走词表的别名与词边界，避免 Java 命中 JavaScript）。"""
    return [word for word in keywords if resume_svc._match_any(text, vocabulary.get(word, [word]))]


def build_brief(
    session: Session,
    job: Job,
    *,
    resume_id: int | None = None,
    settings: AppSettings | None = None,
) -> Brief:
    """算出"重点讲哪几个项目、突出哪几个技能、要避开什么"（**不调 LLM**）。

    ★ 为什么它是 P5 的第一步（计划书的降级起点）：
      面试时"我该讲什么"的价值，比"简历改一个字"高得多，而它**完全确定性** ——
      不需要 API key、不花钱、可复现。先把这一半交付掉，
      完整版（字段级建议）才有资格谈"值不值这一次调用"。
    """
    settings = settings or get_settings()
    vocabulary = resume_svc.load_skill_vocabulary(settings)
    candidate = _pick_candidate(session, job, resume_id=resume_id, settings=settings)
    content = candidate.content

    result = match_svc.match_jd_resume(
        job.jd_text,
        content,
        resume_id=candidate.resume_id,
        resume_name=candidate.name,
        resume_direction=candidate.direction,
        job=job,
        settings=settings,
        vocabulary=vocabulary,
    )
    keywords = list(result.jd_keywords)
    matched = result.matched_keywords
    missing = result.missing_keywords

    brief = Brief(
        job_id=job.id,
        title=job.title or "（未写职位）",
        company=job.company or "（未写公司）",
        score=_stored_score(job, result.rule_score),
        score_source=_score_source(job),
        resume_id=candidate.resume_id,
        resume_name=candidate.name,
        direction=candidate.direction or "（未写方向）",
    )

    # ---- 重点讲哪几个项目：按"与 JD 关键词的命中数"排序（平局保持简历顺序）----
    scored: list[tuple[int, int, BriefPoint]] = []
    for index, project in enumerate(content.projects):
        text = " ".join([project.name, project.role, " ".join(project.skills), *project.bullets])
        hits = _keyword_hits(text, matched or keywords, vocabulary)
        detail = next(
            (bullet for bullet in project.bullets if _keyword_hits(bullet, hits, vocabulary)),
            project.bullets[0] if project.bullets else "",
        )
        point = BriefPoint(
            index=index + 1,
            name=project.name or f"项目{index + 1}",
            role=project.role,
            hits=hits,
            detail=detail,
            line=_talk_line(project.name, project.role, hits),
        )
        # 排序键：命中多的优先；都没命中时保持简历里的顺序（可复现比"更聪明"重要）
        scored.append((-len(hits), index, point))
    scored.sort(key=lambda item: (item[0], item[1]))
    brief.points = [point for _neg, _idx, point in scored[: settings.tailor.brief_projects]]

    # ---- 突出哪 2 个技能：优先"技能栏里主动列出来的"（可信度更高）----------
    brief.highlight = _pick_highlight(matched or keywords, candidate, settings)
    if brief.highlight:
        explicit_hits = [word for word in brief.highlight if word in candidate.explicit]
        brief.highlight_note = (
            "这 2 个是 JD 要、你技能栏里也明确写了 的"
            if len(explicit_hits) == len(brief.highlight)
            else "其中有的只在项目描述里提过 —— 建议在技能栏里补一句（是真话，不算编造）"
        )
    else:
        brief.highlight_note = (
            "JD 的关键词里没有一条能在简历里找到对应，先别硬凑（见下面的「要避开」）"
        )

    # ---- 要避开什么 -------------------------------------------------------
    brief.avoid = _avoid_list(
        missing=missing,
        keywords=keywords,
        candidate=candidate,
        result=result,
        settings=settings,
    )
    brief.notes = _brief_notes(job, result, brief)

    # ---- 讲法自检：brief 给我的每句"怎么讲"都必须过 FidelityGuard ----------
    direction_tokens = set(resume_svc.extract_skills(candidate.direction or "", vocabulary))
    for point in brief.points:
        report = FidelityGuard.check(
            point.line,
            point.line,
            resume=content,
            vocabulary=vocabulary,
            learning_whitelist=set(settings.learning_whitelist),
            extra_evidence=direction_tokens,
            settings=settings,
        )
        if report.passed:
            continue
        # 讲法本身有问题 → 去掉这句话（宁可少讲一句，也不能教用户编造）
        point.line = ""
        brief.report.violations.extend(report.violations)
    return brief


def _stored_score(job: Job, rule_score: int) -> int:
    """库里已有 LLM 分就用它，否则用刚算出来的规则分。"""
    llm_score = getattr(job, "llm_score", None)
    return int(llm_score) if isinstance(llm_score, int) else int(rule_score)


def _score_source(job: Job) -> str:
    return "LLM 精评" if isinstance(getattr(job, "llm_score", None), int) else "规则分"


def _talk_line(name: str, role: str, hits: list[str]) -> str:
    """一句话"怎么讲"（**只用简历里真实存在的技能与项目名**）。"""
    head = f"《{name}》" if name else "简历里的项目"
    if role:
        head += f"（{role}）"
    if hits:
        return f"{head}：重点讲「{'、'.join(hits[:3])}」这块你是怎么做的、踩过什么坑。"
    return f"{head}：讲清你负责的那部分 + 一个具体的取舍（JD 没直接点名，就别硬扯）。"


def _pick_highlight(
    matched: list[str], candidate: ResumeCandidate, settings: AppSettings
) -> list[str]:
    """挑"突出哪几个技能"：优先技能栏主动列出的，其次项目里提过的。"""
    limit = settings.tailor.brief_skills
    explicit = [word for word in matched if word in candidate.explicit]
    others = [word for word in matched if word not in candidate.explicit]
    return (explicit + others)[:limit]


def _avoid_list(
    *,
    missing: list[str],
    keywords: list[str],
    candidate: ResumeCandidate,
    result: match_svc.MatchResult,
    settings: AppSettings,
) -> list[str]:
    """算出"要避开什么"（= 别吹哪一块）。

    ★ 这一节比"重点讲什么"更重要：面试里 90% 的翻车不是"没讲好优势"，
      而是"讲了一个自己不会的东西，被追问两句就露了"。
      所以这里列的每一条都对应一个**会被追问**的点。
    """
    avoid: list[str] = []
    if missing:
        avoid.append(
            f"JD 要求但简历里没有（绝不能说「会」，最多说「正在学」）：{'、'.join(missing[:8])}"
        )

    learning = [word for word in keywords if word in set(settings.learning_whitelist)]
    if learning:
        avoid.append(f"白名单技能（可以说「在学习中」，不能说「熟练」）：{'、'.join(learning)}")

    # 硬条件不匹配（经验年限 / 学历 / 城市）—— 从规则层的 note 里取，
    # 不在这里重新判一遍（两套口径必然漂移）
    parts = (result.rule_detail or {}).get("parts") or {}
    hard = parts.get("hard") if isinstance(parts, dict) else None
    if isinstance(hard, dict):
        ratio = hard.get("ratio")
        note = str(hard.get("note") or "").strip()
        if note and (ratio is None or float(ratio) < 1.0):
            avoid.append(f"硬条件别硬凑：{note}")

    if candidate.graduation_year is None and candidate.highest_degree == "":
        avoid.append("简历里没有能判定毕业年份/学历的信息 —— 面试前自己先算清楚，别临场编")
    return avoid


def _brief_notes(job: Job, result: match_svc.MatchResult, brief: Brief) -> list[str]:
    notes: list[str] = []
    if not result.jd_keywords:
        notes.append("这个岗位没抽到 JD 关键词 —— 先跑 jh match run（或把 JD 正文补全）")
    if brief.points and not brief.points[0].hits:
        notes.append(
            "没有一个项目和 JD 关键词直接对上：重点讲「你负责哪部分 + 为什么这么做」，别硬扯技术栈"
        )
    if job.url:
        notes.append(f"岗位链接：{job.url}")
    return notes


# ===========================================================================
# ② analyze：字段级建议（调模型 + 过闸门）
# ===========================================================================
def build_payload(
    job: Job,
    candidate: ResumeCandidate,
    *,
    keywords: list[str],
    matched: list[str],
    missing: list[str],
    prompt: TailorPrompt,
    settings: AppSettings,
) -> PromptPayload:
    """构造 `jh tailor analyze` 的输入。

    ★ 素材与"事实"全给，但**明确告诉模型哪些是它没有的**（missing）：
      这样它不会为了"对齐 JD"去写一个简历里根本不存在的技能 ——
      提示词里写了"只能进 gaps"，这里再把 missing 单独列出来降低它犯规的机会。
      （真正兜底的仍是 `FidelityGuard`：提示词是劝，闸门是拦。）
    """
    resume_text = _clip(content_text(candidate), _MAX_RESUME_CHARS)
    jd_text = _clip(job.jd_text or "", _MAX_JD_CHARS)

    # ★ 用 `wrap_delimited()` 拼标签，不要手拼 `<jd>…</jd>`：
    #   JD 是从招聘页面抓来的**不可信文本**，写一个 `</jd>` 就能提前闭合标签
    #   （ISSUES #2/#8/#29 是同一个洞，这里靠统一的入口一次性堵住）。
    jd_body = (
        f"岗位：{job.title or '（未写职位）'} ｜ 公司：{job.company or '未写'}"
        f" ｜ 城市：{job.city or '未写'}\n"
        f"JD 正文：\n{jd_text or '（没抓到正文，只有关键词）'}"
    )
    facts = (
        f"JD 关键词：{'、'.join(keywords) or '（没抽到）'}\n"
        f"简历里已有的（matched）：{'、'.join(matched) or '（无）'}\n"
        f"简历里没有的（missing，绝不能写进 revised）：{'、'.join(missing) or '（无）'}\n"
        f"求职方向：{candidate.direction or '（未写）'}"
    )
    user_content = (
        f"{wrap_delimited('jd', jd_body)}\n\n"
        f"{wrap_delimited('resume', resume_text)}\n\n"
        f"{wrap_delimited('facts', facts)}\n\n"
        f"请按系统提示词的要求输出 json（match_analysis + suggestions）。"
    )
    return PromptPayload(
        prompt_version=prompt.version,
        task=TASK,
        messages=[
            LLMMessage(role="system", content=render_system_prompt(prompt, settings)),
            LLMMessage(role="user", content=user_content),
        ],
        json_mode=True,
        # 建议条数最多 12 条 + 分析，2000 token 足够；给足是为了别被截断成半截 JSON
        max_tokens=2000,
        # 改写建议要"像人写的"，但又要贴 JD 措辞 → 中温（0.7 会开始自由发挥）
        temperature=0.4,
    )


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n…（已截断）"


def content_text(candidate: ResumeCandidate) -> str:
    """送给模型的简历正文：优先原始文本（`original` 要逐字对得上），其次结构化拼装。"""
    raw = (candidate.content.raw_text or "").strip()
    if raw:
        return raw
    return _compose_from_structure(candidate.content)


def parse_analysis(content: str) -> TailorAnalysis:
    """解析模型输出；任何不合规都抛 `ValueError`（由上层转成 TailorError）。

    ★ 报错信息里**不带模型原文**：那段文本里可能含简历 PII，
      而这类报错很容易被贴进 issue/聊天里（踩过一次，见 ISSUES #44）。
      只报"哪里不合法"，需要看原文时用户自己加 --debug。
    """
    text = _FENCE_RE.sub("", (content or "").strip()).strip()
    if not text:
        raise ValueError("模型返回了空内容")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"不是合法 JSON（{exc.msg}，位置 {exc.pos}）。"
            "常见原因：模型输出了 markdown 说明文字或半截 JSON"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(f"JSON 顶层不是对象，而是 {type(data).__name__}")
    try:
        return TailorAnalysis.model_validate(data)
    except ValidationError as exc:
        fields = "、".join(".".join(str(part) for part in item["loc"]) for item in exc.errors()[:3])
        raise ValueError(f"字段不符合 schema（问题字段：{fields}）") from exc


def analyze(
    session: Session,
    job: Job,
    *,
    resume_id: int | None = None,
    settings: AppSettings | None = None,
    provider: LLMProvider | None = None,
    use_cache: bool = True,
) -> TailorResult:
    """生成字段级建议并**逐条过闸门**（不写库；写库见 `analyze_and_save`）。

    Raises:
        TailorError: 没配 key / 调用失败 / 模型输出不合法。
            这里**刻意不降级**（与开场白不同）：开场白有确定性模板可用，
            而"字段级改写建议"没有诚实的模板 —— 硬造一份只会是假建议。
            所以直接告诉用户"这次没成，先用 jh tailor brief"。
    """
    settings = settings or get_settings()
    vocabulary = resume_svc.load_skill_vocabulary(settings)
    prompt = load_prompt(settings)
    candidate = _pick_candidate(session, job, resume_id=resume_id, settings=settings)

    result = match_svc.match_jd_resume(
        job.jd_text,
        candidate.content,
        resume_id=candidate.resume_id,
        resume_name=candidate.name,
        resume_direction=candidate.direction,
        job=job,
        settings=settings,
        vocabulary=vocabulary,
    )

    if provider is None or not provider.is_configured():
        raise TailorError(
            "没有配置大模型密钥（DEEPSEEK_API_KEY），无法生成改写建议。\n"
            "  · 现在就能用的：jh tailor brief <job_id>（不花钱、纯规则）\n"
            "  · 要用完整版：把 key 写进项目根的 .env"
        )

    # ★ 先构造 payload，再算 key（ISSUES #55）。
    #   关键词/matched/missing 都会进提示词，但不在 jd_hash 里 —— 只按 jd_hash 建 key
    #   会出现"提示词变了、key 没变"，于是把上一份建议原样喂回去。
    #   整份提示词取指纹后，key 与输入在结构上不可能不一致。
    payload = build_payload(
        job,
        candidate,
        keywords=list(result.jd_keywords),
        matched=result.matched_keywords,
        missing=result.missing_keywords,
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
    content = ""
    from_cache = False
    if use_cache and cache_key:
        hit = llm_cache.get_cached(session, cache_key)
        if hit is not None:
            content = hit.content
            from_cache = True

    if not content:
        content = _complete_with_retry(provider, payload, settings)
        if cache_key and content:
            llm_cache.put_cached(
                session,
                cache_key,
                task=TASK,
                content=content,
                model=provider.model,
            )

    try:
        analysis = parse_analysis(content)
    except ValueError as exc:
        raise TailorError(f"模型输出解析失败：{exc}") from exc

    kept, dropped, report = _screen(
        analysis,
        candidate=candidate,
        settings=settings,
        vocabulary=vocabulary,
    )
    return TailorResult(
        job_id=job.id,
        resume_id=candidate.resume_id,
        resume_name=candidate.name,
        prompt_version=prompt.version,
        analysis=analysis,
        kept=kept,
        dropped=dropped,
        report=report,
        from_cache=from_cache,
    )


def _complete_with_retry(
    provider: LLMProvider, payload: PromptPayload, settings: AppSettings
) -> str:
    """调模型；**空 content 才重试**（DeepSeek 偶发空返回）。

    ★ 为什么只重试空返回、不重试"解析失败"：解析失败通常是输出格式问题，
      同一个提示词再来一次大概率还是错的（实测：重试只会多花钱）。
    """
    last_error: str = ""
    for attempt in range(1 + settings.llm_max_retries):
        try:
            response = provider.complete(payload)
        except DeepSeekError as exc:
            raise TailorError(f"调用模型失败：{exc}") from exc
        if response.content.strip():
            return response.content
        last_error = "模型返回了空内容"
        logger.warning("tailor 第 %d 次调用返回空内容", attempt + 1)
    raise TailorError(f"调用模型失败：{last_error}（已重试 {settings.llm_max_retries} 次）")


def _norm(text: str) -> str:
    """归一化：压掉所有空白。用于"这句话在简历里到底有没有"的定位。"""
    return _WS_RE.sub("", text or "")


def _locate(original: str, resume_text: str) -> bool:
    """建议的 `original` 能不能在简历里**逐字**找到（允许空白差异）。"""
    needle = _norm(original)
    return bool(needle) and needle in _norm(resume_text)


def _screen(
    analysis: TailorAnalysis,
    *,
    candidate: ResumeCandidate,
    settings: AppSettings,
    vocabulary: dict[str, list[str]],
) -> tuple[list[Suggestion], list[DroppedSuggestion], FidelityReport]:
    """闸门：逐条校验建议，返回 `(留下的, 丢掉的, 合并报告)`。

    ★ 三条丢弃理由（每条都在给用户看的文案里说清楚）：
      ① `original` 不在简历里 → 这条建议**没法定位**（不是编造，但也没法用）
      ② revised 是空的 / 和 original 一样 → 空操作，只会浪费我的审核时间
      ③ 没过 `FidelityGuard` → 有编造嫌疑（**最要紧的一条**）
    """
    content = candidate.content
    resume_text = content.raw_text or content_text(candidate)
    direction_tokens = set(resume_svc.extract_skills(candidate.direction or "", vocabulary))
    report = FidelityReport()
    kept: list[Suggestion] = []
    dropped: list[DroppedSuggestion] = []

    for suggestion in analysis.suggestions:
        reasons: list[str] = []
        if not suggestion.revised.strip():
            dropped.append(DroppedSuggestion(suggestion, ["revised 是空的"]))
            continue
        if _norm(suggestion.revised) == _norm(suggestion.original):
            dropped.append(DroppedSuggestion(suggestion, ["改完和原文一模一样（空操作）"]))
            continue
        if not _locate(suggestion.original, resume_text):
            dropped.append(
                DroppedSuggestion(
                    suggestion, ["original 在简历里找不到（模型转述了原文，没法定位）"]
                )
            )
            continue

        item_report = FidelityGuard.check(
            suggestion.original,
            suggestion.revised,
            resume=content,
            vocabulary=vocabulary,
            learning_whitelist=set(settings.learning_whitelist),
            extra_evidence=direction_tokens,
            settings=settings,
        )
        report.notes.extend(item_report.notes)
        if not item_report.passed:
            reasons.extend(str(item) for item in item_report.violations)
            report.violations.extend(item_report.violations)
            dropped.append(DroppedSuggestion(suggestion, reasons))
            continue
        kept.append(suggestion)

    report.mentioned_skills = sorted(
        {
            skill
            for suggestion in kept
            for skill in resume_svc.extract_skills(suggestion.revised, vocabulary)
        }
    )
    return kept, dropped, report


# ===========================================================================
# 落库 / 读库（resume_versions）
# ===========================================================================
def analyze_and_save(
    session: Session,
    job: Job,
    *,
    resume_id: int | None = None,
    settings: AppSettings | None = None,
    provider: LLMProvider | None = None,
    use_cache: bool = True,
) -> TailorResult:
    """`analyze()` + 写库。**整批被拒时不写库**（见 `TailorResult.refused`）。"""
    settings = settings or get_settings()
    result = analyze(
        session,
        job,
        resume_id=resume_id,
        settings=settings,
        provider=provider,
        use_cache=use_cache,
    )
    if result.refused or not result.kept:
        return result
    version = ResumeVersion(
        resume_id=result.resume_id,
        job_id=job.id,
        suggestions_json=json.dumps(
            {
                "match_analysis": result.analysis.match_analysis.model_dump(),
                "suggestions": [
                    {"id": index + 1, **suggestion.model_dump()}
                    for index, suggestion in enumerate(result.kept)
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        fidelity_report_json=json.dumps(
            {"prompt_version": result.prompt_version, **_report_payload(result)},
            ensure_ascii=False,
            indent=2,
        ),
    )
    session.add(version)
    session.commit()
    result.version_id = version.id
    return result


def _report_payload(result: TailorResult) -> dict[str, Any]:
    """`fidelity_report_json` 的内容：判定结果 + 被丢掉的建议及原因。"""
    payload = result.report.as_dict()
    payload["dropped"] = [
        {
            "section": item.suggestion.section,
            "original": item.suggestion.original,
            "reasons": item.reasons,
        }
        for item in result.dropped
    ]
    return payload


def load_version(session: Session, version_id: int) -> ResumeVersion:
    """按 id 取版本；不存在时抛 `TailorError`（CLI 显示人话）。"""
    version = session.get(ResumeVersion, version_id)
    if version is None:
        raise TailorError(f"找不到定制版本 #{version_id}。用 jh tailor list 看有哪些。")
    return version


def _payload_of(version: ResumeVersion) -> dict[str, Any]:
    """把 `suggestions_json` 解析成**顶层的对象**，坏了就给一句人话。

    ★ 这里守的是"库里的数据被手工改过"这条路径（ISSUES #51）：
      第一版只 guard 了 `JSONDecodeError`（"不是合法 JSON"），于是
      **合法 JSON 但结构不对**时会甩出裸的 Python 错误 ——
            f"实际是 {type(data).__name__}。"
            "手工改过 resume_versions.suggestions_json 的话，"
            "请对照 `jh tailor analyze` 写入的结构。"
    """
    try:
        data = json.loads(version.suggestions_json or "{}")
    except json.JSONDecodeError as exc:
        raise TailorError(
            f"版本 #{version.id} 的建议数据损坏（不是合法 JSON：{exc.msg}，位置 {exc.pos}）"
        ) from None
    if not isinstance(data, dict):
        raise TailorError(
            f"版本 #{version.id} 的建议数据格式不对：顶层应当是对象（{{...}}），"
            f"实际是 {type(data).__name__}。"
            "如果你手工改过 resume_versions.suggestions_json，"
            "请对照 `jh tailor analyze` 写入的结构。"
        )
    items = data.get("suggestions")
    if items is not None and not isinstance(items, list):
        raise TailorError(
            f"版本 #{version.id} 的建议数据格式不对：`suggestions` 应当是数组，"
            f"实际是 {type(items).__name__}"
        )
    return data


def suggestions_of(version: ResumeVersion) -> list[dict[str, Any]]:
    """取版本里的建议列表（原样返回 dict，附带 `id`）。

    Raises:
        TailorError: 库里的数据不是合法 JSON、或结构不对（见 `_payload_of`）。
    """
    data = _payload_of(version)
    return [item for item in (data.get("suggestions") or []) if isinstance(item, dict)]


def analysis_of(version: ResumeVersion) -> MatchAnalysis:
    """取版本里的匹配分析（缺了就返回默认值；结构坏了抛 `TailorError`）。"""
    data = _payload_of(version)
    raw = data.get("match_analysis") or {}
    try:
        return MatchAnalysis.model_validate(raw)
    except ValidationError:  # pragma: no cover
        return MatchAnalysis()


def approved_ids(version: ResumeVersion) -> list[int]:
    """已批准的编号（升序）。`approved_ids` 为空 ≠ 没批准过任何一条。"""
    try:
        data = json.loads(version.approved_ids or "[]")
    except json.JSONDecodeError:  # pragma: no cover
        return []
    if not isinstance(data, list):
        return []
    return sorted(
        int(item) for item in data if isinstance(item, (int, float, str)) and str(item).isdigit()
    )


def fidelity_report_of(version: ResumeVersion) -> dict[str, Any]:
    """读回落库的闸门报告（`jh tailor diff` 会显示它）。"""
    try:
        data = json.loads(version.fidelity_report_json or "{}")
    except json.JSONDecodeError:  # pragma: no cover
        return {}
    return data if isinstance(data, dict) else {}


@dataclass
class VersionRow:
    """`jh tailor list` 的一行。"""

    id: int
    job_id: int | None
    resume_id: int
    created_at: Any
    total: int
    approved: int
    file_path: str | None


def list_versions(session: Session, *, job_id: int | None = None) -> list[VersionRow]:
    """列出定制版本（可按岗位过滤），新的在前。"""
    query = select(ResumeVersion).order_by(ResumeVersion.id.desc())
    if job_id is not None:
        query = query.where(ResumeVersion.job_id == job_id)
    rows: list[VersionRow] = []
    for version in session.scalars(query):
        rows.append(
            VersionRow(
                id=version.id,
                job_id=version.job_id,
                resume_id=version.resume_id,
                created_at=version.created_at,
                total=len(suggestions_of(version)),
                approved=len(approved_ids(version)),
                file_path=version.file_path,
            )
        )
    return rows


# ===========================================================================
# ③ approve：逐条批准（**再校验一遍**）
# ===========================================================================
def approve(
    session: Session,
    version: ResumeVersion,
    *,
    ids: list[int] | None = None,
    all_: bool = False,
    settings: AppSettings | None = None,
) -> list[int]:
    """批准若干条建议，写 `approved_ids`，返回批准后的完整编号列表。

    ★ 为什么 approve 还要再跑一次 `FidelityGuard`（不是多此一举）：
      库里那条建议是**上一次**生成的，而判定口径会变严：我可能刚往词表里
      加了技能、或者刚把 length_ratio 调紧。批准意味着"这句话可以进简历"，
      所以必须用**当前**的口径重新判一次。
    ★ 有违规时**整批不写**（不是"跳过违规的那条"）：批准是一个动作，
      半成功最难排查 —— 用户会以为都批了。
    """
    settings = settings or get_settings()
    vocabulary = resume_svc.load_skill_vocabulary(settings)
    items = suggestions_of(version)
    if not items:
        raise TailorError(f"版本 #{version.id} 里没有建议可批准")

    valid = {int(item["id"]) for item in items if "id" in item}
    if all_:
        target = sorted(valid)
    elif ids:
        unknown = [item for item in ids if item not in valid]
        if unknown:
            raise TailorError(
                f"版本 #{version.id} 里没有这些编号：{unknown}（可用编号：{sorted(valid)}）"
            )
        target = sorted(set(ids))
    else:
        raise TailorError("要批准哪些？给编号（如 jh tailor approve 1 2）或 --all")

    resume_row = session.get(Resume, version.resume_id)
    content = resume_svc.content_from_json(resume_row.content_json if resume_row else None)
    direction_tokens = set(
        resume_svc.extract_skills((resume_row.direction if resume_row else "") or "", vocabulary)
    )

    violations: list[str] = []
    for item in items:
        number = int(item.get("id") or 0)
        if number not in target:
            continue
        report = FidelityGuard.check(
            str(item.get("original") or ""),
            str(item.get("revised") or ""),
            resume=content,
            vocabulary=vocabulary,
            learning_whitelist=set(settings.learning_whitelist),
            extra_evidence=direction_tokens,
            settings=settings,
        )
        if not report.passed:
            violations.append(f"#{number}：{report.summary()}")

    if violations:
        raise TailorFidelityError(
            "这一版里有通不过保真检查的建议，已拒绝写入。请改文案或删掉这条建议：",
            violations=violations,
        )

    merged = sorted(set(approved_ids(version)) | set(target))
    version.approved_ids = json.dumps(merged, ensure_ascii=False)
    session.commit()
    return merged


# ===========================================================================
# ④ export：导出**新文件**（并验原文件 hash 不变）
# ===========================================================================
@dataclass
class ExportResult:
    """一次导出的结果。"""

    path: Path
    fmt: str
    applied: list[dict[str, Any]] = field(default_factory=list)
    unapplied: list[tuple[dict[str, Any], str]] = field(default_factory=list)
    #: 最终写进文件的文本（`--show` 时打印；也是"整篇闸门"的被检对象）
    text: str = ""
    report: FidelityReport = field(default_factory=FidelityReport)
    original_path: Path | None = None
    original_hash_before: str = ""
    original_hash_after: str = ""

    @property
    def original_unchanged(self) -> bool:
        if self.original_path is None:
            return True
        return self.original_hash_before == self.original_hash_after


def _hash_file(path: Path) -> str:
    """文件 sha256（读不到就返回空串：调用方按"没校验"处理并提示）。"""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:  # pragma: no cover - 文件被删/被占用
        return ""


def _compose_from_structure(content: resume_svc.ResumeContent) -> list[DocLine]:
    """没有 `sections` 时用结构化字段拼一份（老数据兜底）。"""
    lines: list[DocLine] = []
    basic = content.basic or {}
    lines.append(DocLine("title", str(basic.get("name") or "简历")))
    contact = " ｜ ".join(
        str(basic.get(key) or "") for key in ("phone", "email", "city", "job_intention")
    ).strip(" ｜")
    if contact:
        lines.append(DocLine("body", contact))
    if content.education:
        lines.append(DocLine("heading", SECTION_TITLES["education"]))
        for item in content.education:
            lines.append(
                DocLine("bullet", f"{item.school} {item.major} {item.degree} {item.period}".strip())
            )
    if content.skills:
        lines.append(DocLine("heading", SECTION_TITLES["skills"]))
        lines.append(DocLine("body", "、".join(content.skills)))
    if content.projects:
        lines.append(DocLine("heading", SECTION_TITLES["projects"]))
        for item in content.projects:
            lines.append(DocLine("body", f"{item.name} {item.role} {item.period}".strip()))
            lines.extend(DocLine("bullet", bullet) for bullet in item.bullets)
    if content.internships:
        lines.append(DocLine("heading", SECTION_TITLES["internships"]))
        for item in content.internships:
            lines.append(DocLine("body", f"{item.company} {item.role} {item.period}".strip()))
            lines.extend(DocLine("bullet", bullet) for bullet in item.bullets)
    return lines


def build_document(
    content: resume_svc.ResumeContent,
    revisions: dict[str, str],
) -> tuple[list[DocLine], list[tuple[str, str]]]:
    """按原文结构拼出导出文档，并把已批准建议的替换应用上去。

    Args:
        revisions: `{归一化后的原文: 改写}`。

    Returns:
        `(文档行, 没能应用上的替换)`；后者每项是 `(归一化原文, 改写)`。

    ★ 为什么按 `sections`（解析出的分段原文）拼，而不是按结构化字段重建：
      重建会**静默丢掉**没被建模的内容（荣誉奖项/自我评价/证书），
      用户拿到一份"少了半页"的简历去投递，是这个功能最不能犯的错。
      按分段原文拼，等于"原文原样搬过去，只替换我批准的那几句"。
    """
    sections = content.sections or {}
    if not sections:
        return _compose_from_structure(content), []

    lines: list[DocLine] = []
    basic = content.basic or {}
    name = str(basic.get("name") or "").strip()
    lines.append(DocLine("title", name or "简历"))
    contact = " ｜ ".join(
        str(basic.get(key) or "").strip() for key in ("phone", "email", "city", "job_intention")
    ).strip(" ｜")
    if contact:
        lines.append(DocLine("body", contact))

    used: set[str] = set()
    for key, raw_lines in sections.items():
        if key in _SKIP_SECTIONS:
            continue
        title = SECTION_TITLES.get(key, key)
        body = [str(item).strip() for item in raw_lines if str(item).strip()]
        if not body:
            continue
        lines.append(DocLine("heading", title))
        for item in body:
            revised = revisions.get(_norm(item))
            if revised is not None:
                used.add(_norm(item))
                lines.append(DocLine("bullet", revised))
            else:
                lines.append(DocLine("body", item))

    unapplied = [
        (original, revised) for original, revised in revisions.items() if original not in used
    ]
    return lines, unapplied


def _gate_items(
    chosen: list[dict[str, Any]],
    *,
    content: resume_svc.ResumeContent,
    vocabulary: dict[str, list[str]],
    direction_tokens: set[str],
    settings: AppSettings,
) -> FidelityReport:
    """逐条闸门（**第三次调用 FidelityGuard**）：有任何一条不过就抛异常。

    ★ 为什么"有一条不过就整批拒绝写文件"：导出动作是原子的 ——
      半套建议写进简历（有的改了有的没改）比不导出更糟：
      用户会以为文件就是自己批准的那版。
    """
    report = FidelityReport()
    violations: list[str] = []
    for item in chosen:
        number = int(item.get("id") or 0)
        item_report = FidelityGuard.check(
            str(item.get("original") or ""),
            str(item.get("revised") or ""),
            resume=content,
            vocabulary=vocabulary,
            learning_whitelist=set(settings.learning_whitelist),
            extra_evidence=direction_tokens,
            settings=settings,
        )
        report.notes.extend(item_report.notes)
        if not item_report.passed:
            report.violations.extend(item_report.violations)
            violations.append(f"#{number}：{item_report.summary()}")
    if violations:
        raise TailorFidelityError("导出前保真检查没通过（已拒绝写文件）：", violations=violations)
    return report


def _resolve_export_path(
    out: Path | str | None, *, fmt: str, version: ResumeVersion, settings: AppSettings
) -> Path:
    """决定写到哪个**新**文件：显式 --out 优先，否则在 output/ 里取一个不重名的。"""
    suffix = ".docx" if fmt == "docx" else ".md"
    if out is None:
        from jobhunter.services import export_svc

        stem = f"resume-job{version.job_id or 0}-v{version.id}"
        return export_svc.unique_path(settings.paths.output_dir, stem, suffix)

    target = Path(out)
    if target.exists():
        raise TailorError(
            f"目标文件已存在，不会覆盖：{target}\n"
            f"  · 换个名字，或删掉它再导出（导出**永远**只写新文件）"
        )
    ensure_dir(target.parent)
    return target


def export_version(
    session: Session,
    version: ResumeVersion,
    *,
    settings: AppSettings | None = None,
    out: Path | str | None = None,
    fmt: str = "docx",
) -> ExportResult:
    """导出已批准的建议（**写新文件**），并校验原简历文件没被动过。

    Raises:
        TailorError: 没批准任何建议 / 没有可写的内容 / 目标文件已存在。
        TailorFidelityError: 导出前闸门不通过（逐条 + 整篇两道）。
    """
    settings = settings or get_settings()
    if fmt not in ("docx", "md"):
        raise TailorError(f"不支持的导出格式：{fmt}（只支持 docx / md）")
    vocabulary = resume_svc.load_skill_vocabulary(settings)

    approved = set(approved_ids(version))
    if not approved:
        raise TailorError(
            f"版本 #{version.id} 还没有批准任何建议。\n"
            f"  · 先看：jh tailor diff {version.id}\n"
            f"  · 再批：jh tailor approve {version.id} --all"
        )
    chosen = [item for item in suggestions_of(version) if int(item.get("id") or 0) in approved]
    if not chosen:
        raise TailorError(f"版本 #{version.id} 的已批准编号在建议列表里找不到（数据不一致）")

    resume_row = session.get(Resume, version.resume_id)
    if resume_row is None:
        raise TailorError(f"版本 #{version.id} 关联的简历已不存在")
    content = resume_svc.content_from_json(resume_row.content_json)
    direction_tokens = set(resume_svc.extract_skills(resume_row.direction or "", vocabulary))

    report = _gate_items(
        chosen,
        content=content,
        vocabulary=vocabulary,
        direction_tokens=direction_tokens,
        settings=settings,
    )

    revisions = {
        _norm(str(item.get("original") or "")): str(item.get("revised") or "") for item in chosen
    }
    lines, unapplied = build_document(content, revisions)

    # ---- 闸门（第三次调用）：整篇 -------------------------------------------------
    text = "\n".join(line.text for line in lines if line.text.strip())
    whole = FidelityGuard.check(
        content.raw_text or text,
        text,
        resume=content,
        vocabulary=vocabulary,
        learning_whitelist=set(settings.learning_whitelist),
        extra_evidence=direction_tokens,
        settings=settings,
    )
    report.notes.extend(whole.notes)
    if not whole.passed:
        report.violations.extend(whole.violations)
        raise TailorFidelityError(
            "导出前的整篇保真检查没通过（已拒绝写文件）：",
            violations=[str(item) for item in whole.violations],
        )

    # ---- 写**新**文件 -------------------------------------------------------------
    target = _resolve_export_path(out, fmt=fmt, version=version, settings=settings)
    original_path = Path(resume_row.file_path) if resume_row.file_path else None
    before = _hash_file(original_path) if original_path else ""
    if fmt == "docx":
        write_docx(target, lines)
    else:
        write_markdown(target, lines)
    after = _hash_file(original_path) if original_path else ""

    # ★ 自己校验自己：导出绝不允许动到原文件。这条断言的意义是"如果哪天
    #   有人把实现改成就地修改，测试会立刻红"，而不是"防用户手滑"。
    if original_path is not None and before and before != after:
        raise TailorError(
            f"原简历文件在导出过程中被改动了（{original_path}）—— 这是严重 bug，已中止。"
        )

    result = ExportResult(
        path=target,
        fmt=fmt,
        applied=chosen,
        unapplied=[
            ({"original": original, "revised": revised}, "原文在导出文档里没找到对应行")
            for original, revised in unapplied
        ],
        text=text,
        report=report,
        original_path=original_path,
        original_hash_before=before,
        original_hash_after=after,
    )
    version.file_path = str(target)
    session.commit()
    return result

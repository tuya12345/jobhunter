"""`tests/verify/` 的造数据助手（纯函数，无 fixture、无打桩）。

放在独立模块而不是 conftest 里：验证测试要能 `from tests.verify._helpers import ...`
稳定引用，不依赖 pytest 的 conftest 注入机制。
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy.orm import Session

from jobhunter.adapters.base import JobDetail
from jobhunter.models import Job, Resume
from jobhunter.services import resume_svc
from jobhunter.services.resume_svc import (
    EducationItem,
    InternshipItem,
    ProjectItem,
    ResumeContent,
)
from jobhunter.utils.io import to_json

#: 一份"通用型"假简历的技能集：AI Agent 方向 + 一批常见技能。
#: 全部虚构；手机号用 fixture 专用占位 13800000000。
RESUME_SKILLS: list[str] = [
    "Python",
    "FastAPI",
    "MySQL",
    "Redis",
    "Docker",
    "Git",
    "RAG",
    "LLM",
    "JavaScript",
    "Vue",
]


def build_resume_content(
    *,
    skills: list[str] | None = None,
    direction_text: str = "AI Agent 应用开发 / Python 后端开发",
) -> ResumeContent:
    """造一份结构化简历内容（内容全部虚构）。"""
    return ResumeContent(
        basic={
            "name": "林小满",
            "phone": "13800000000",
            "email": "xiaoman.lin@example.com",
            "city": "深圳",
            "job_intention": direction_text,
        },
        education=[
            EducationItem(
                school="虚构大学",
                major="软件工程",
                degree="本科",
                period="2023.09-2027.06",
            )
        ],
        skills=list(RESUME_SKILLS if skills is None else skills),
        projects=[
            ProjectItem(
                name="智能问答助手",
                role="独立开发",
                period="2026.03-2026.08",
                bullets=[
                    "用 Python + FastAPI 实现检索增强问答，接入 DeepSeek。",
                    "用 Redis 做结果缓存，响应时间下降一半。",
                ],
                skills=["Python", "FastAPI", "RAG", "Redis"],
            )
        ],
        internships=[
            InternshipItem(
                company="虚构科技有限公司",
                role="后端实习生",
                period="2026.06-2026.09",
                bullets=["参与订单服务的接口开发与联调。"],
                skills=["Python", "MySQL"],
            )
        ],
        raw_text="（fixture）虚构简历原文，仅用于测试。",
    )


def insert_resume(
    session: Session,
    content: ResumeContent | None = None,
    *,
    name: str = "主简历",
    direction: str = "AI Agent",
    is_default: bool = True,
) -> Resume:
    """把一份简历写进测试库并返回 ORM 对象（content_hash 用库里的函数算，口径同源）。"""
    content = content if content is not None else build_resume_content()
    resume = Resume(
        name=name,
        direction=direction,
        file_path="tests/verify/fixture.docx",
        content_json=to_json(content.model_dump(), indent=None),
        content_hash=resume_svc.compute_content_hash(content),
        is_default=is_default,
    )
    session.add(resume)
    session.flush()
    return resume


def build_detail(
    *,
    company: str | None = "虚构科技有限公司",
    title: str | None = "AI Agent 开发工程师",
    city: str | None = "深圳",
    jd_text: str | None = None,
    url: str = "https://www.zhipin.com/job_detail/abc123.html",
    platform: str = "boss",
    platform_job_id: str | None = "abc123",
    fetch_status: str = "ok",
    experience: str | None = "1-3年",
    education: str | None = "本科",
    is_intern: bool | None = False,
    salary_text: str | None = "15-25K",
    salary_min: int | None = 15000,
    salary_max: int | None = 25000,
) -> JobDetail:
    """造一个抓取结果（`JobDetail`）。"""
    return JobDetail(
        platform=platform,
        jd_text=(
            jd_text
            if jd_text is not None
            else (
                "岗位职责：负责 AI Agent 应用的开发，熟悉 Python、FastAPI、RAG 与 LLM。"
                "要求本科及以上学历，1-3 年经验，工作地点深圳。"
            )
        ),
        platform_job_id=platform_job_id,
        company=company,
        title=title,
        city=city,
        experience=experience,
        education=education,
        is_intern=is_intern,
        salary_text=salary_text,
        salary_min=salary_min,
        salary_max=salary_max,
        url=url,
        fetch_status=fetch_status,
        raw={},
    )


def insert_job(session: Session, detail: JobDetail | None = None, **overrides: object) -> Job:
    """把岗位直接写进测试库（不走 upsert，方便精确控制字段）。"""
    detail = detail if detail is not None else build_detail()
    job = Job(
        platform=detail.platform,
        platform_job_id=detail.platform_job_id,
        company=detail.company,
        title=detail.title,
        city=detail.city,
        experience=detail.experience,
        education=detail.education,
        is_intern=detail.is_intern,
        salary_text=detail.salary_text,
        salary_min=detail.salary_min,
        salary_max=detail.salary_max,
        jd_text=detail.jd_text,
        jd_hash=hashlib.sha256((detail.jd_text or "").encode("utf-8")).hexdigest(),
        jd_fetch_status=detail.fetch_status,
        url=detail.url,
        **overrides,  # type: ignore[arg-type]
    )
    session.add(job)
    session.flush()
    return job


def score_job(session: Session, job: Job, settings):  # noqa: ANN001, ANN201
    """跑一遍规则层并落库（复现生产路径 `match_best_candidate` → `apply_result_to_job`）。"""
    from jobhunter.services import match_svc

    candidates = match_svc.load_resume_candidates(session, settings)
    assert candidates, "测试库里的简历没被装成候选人"
    result = match_svc.match_best_candidate(job.jd_text, candidates, job=job, settings=settings)
    assert result is not None, "match_best_candidate 返回 None（库里没简历？）"
    match_svc.apply_result_to_job(job, result)
    session.flush()
    return result


#: 岗位详情页里的 JD 正文（拆成多行拼接，避免超长行 —— ruff E501）
JD_BODY = (
    "岗位职责：负责 AI Agent 应用的架构设计与开发；"
    "熟悉 Python、FastAPI、RAG、LLM 相关技术栈；"
    "参与检索增强生成链路的优化。"
    "任职要求：本科及以上学历，计算机相关专业；"
    "1-3 年开发经验，优秀应届生亦可；有 Docker、Git 使用经验。"
)

#: 一段"像真的"岗位详情页 HTML（含 JSON-LD，覆盖解析器主路径）。
#: 内容虚构，公司名带"虚构"，不指向任何真实企业。
SAMPLE_HTML = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>AI Agent 开发工程师 - 虚构科技有限公司 - 深圳</title>
  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "title": "AI Agent 开发工程师",
    "hiringOrganization": {{"@type": "Organization", "name": "虚构科技有限公司"}},
    "jobLocation": {{"@type": "Place",
      "address": {{"@type": "PostalAddress", "addressLocality": "深圳",
                  "addressRegion": "广东"}}}},
    "experienceRequirements": "1-3年",
    "educationRequirements": "本科",
    "description": "{JD_BODY}"
  }}
  </script>
</head>
<body>
  <div class="job-detail">
    <h1 class="job-name">AI Agent 开发工程师</h1>
    <div class="job-salary">15-25K·14薪</div>
    <div class="job-area">深圳</div>
    <div class="job-detail-section">
      <div class="job-sec-text">{JD_BODY}</div>
    </div>
  </div>
</body>
</html>
"""


# ===========================================================================
# AST 层面的静态取证助手
# ---------------------------------------------------------------------------
# ★ 为什么必须用 AST 而不是"逐行 grep"：
#   本项目**在文档字符串里大量声明"我不做 XX"**（这本身是好事），
#   逐行 grep 会把那些声明当成违规命中 —— 我第一版扫描就是这么误报的。
#   AST 能精确区分"文档字符串/注释"与"真正的代码"。
# ===========================================================================
def python_sources(root: Path) -> Iterator[Path]:
    """遍历项目源码（排除虚拟环境与缓存）。"""
    for path in root.rglob("*.py"):
        parts = set(path.parts)
        if ".venv" in parts or "__pycache__" in parts:
            continue
        yield path


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """收集"文档字符串"节点（AST 不区分字符串用途，需要自己标出来）。"""
    ids: set[int] = set()
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if isinstance(node, holders):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def parse_module(path: Path) -> tuple[ast.Module, set[int]]:
    """解析一个 .py，返回 (AST, 文档字符串节点 id 集合)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return tree, _docstring_constant_ids(tree)


def code_strings(path: Path) -> Iterator[tuple[int, str]]:
    """产出**非文档字符串**的字符串字面量（真正的"代码里的字符串"）。"""
    tree, doc_ids = parse_module(path)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in doc_ids
        ):
            yield node.lineno, node.value


def call_names(path: Path) -> Iterator[tuple[int, str]]:
    """产出所有函数调用的"点号全名"（如 `page.click` / `open` / `Path.read_text`）。"""
    tree, _ = parse_module(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node.lineno, ast.unparse(node.func)


def call_keywords(path: Path) -> Iterator[tuple[int, str, set[str]]]:
    """产出 (行号, 被调用名, 关键字参数名集合)。"""
    tree, _ = parse_module(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node.lineno, ast.unparse(node.func), {kw.arg for kw in node.keywords if kw.arg}


def call_details(path: Path) -> Iterator[tuple[int, str, int, set[str]]]:
    """产出 (行号, 被调用名, 位置参数个数, 关键字参数名集合)。

    为什么需要位置参数个数：`datetime.now(UTC)` 是**对的**（aware），
    `datetime.now()` 才是 naive 的本地时间 —— 只看名字分不出来。
    """
    tree, _ = parse_module(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield (
                node.lineno,
                ast.unparse(node.func),
                len(node.args),
                {kw.arg for kw in node.keywords if kw.arg},
            )

"""models：11 张表的 ORM 定义（SQLAlchemy 2.0 风格）。

    base.py         DeclarativeBase + 命名约定 + UTCDateTime + json_text  [P0 全项目复用]
    resume.py       resumes / resume_versions
    job.py          jobs           ← 字段最多、索引最多的一张，去重与匹配都围绕它
    application.py  applications / application_events（append-only 事件流）
    interview.py    interviews / interview_questions
    assessment.py   assessments    ← 笔试/测评（海投必遇，v2 漏掉的环节）
    offer.py        offers         ← 终点站（v2 的 8 张表里居然没有 Offer）
    company.py      companies      ← 官网/内推线索池
    llm_cache.py    llm_cache      ← 缓存 key 是这个项目最容易被写错的地方

设计理由、索引用途、外键删除策略详见 docs/data_model.md。
本模块 __init__ 负责**汇总导入**：Alembic 的 env.py 只 import Base，
就能拿到完整 metadata（否则 autogenerate 会以为表丢了）。
"""

from __future__ import annotations

from jobhunter.models.application import Application, ApplicationEvent
from jobhunter.models.assessment import Assessment
from jobhunter.models.base import Base, UTCDateTime, json_text
from jobhunter.models.company import Company
from jobhunter.models.constants import (
    APPLICATION_STATUSES,
    ASSESSMENT_KINDS,
    EVENT_TYPES,
    JOB_STATUSES,
    QUESTION_RATINGS,
    QUESTION_SOURCES,
)
from jobhunter.models.interview import Interview, InterviewQuestion
from jobhunter.models.job import Job
from jobhunter.models.llm_cache import LLMCache
from jobhunter.models.offer import Offer
from jobhunter.models.resume import Resume, ResumeVersion

__all__ = [
    "APPLICATION_STATUSES",
    "ASSESSMENT_KINDS",
    "EVENT_TYPES",
    "JOB_STATUSES",
    "QUESTION_RATINGS",
    "QUESTION_SOURCES",
    "Application",
    "ApplicationEvent",
    "Assessment",
    "Base",
    "Company",
    "Interview",
    "InterviewQuestion",
    "Job",
    "LLMCache",
    "Offer",
    "Resume",
    "ResumeVersion",
    "UTCDateTime",
    "json_text",
]

"""简历模型：`resumes`（简历主表）+ `resume_versions`（AI 定制版本）。

设计要点：
    · **只记路径不复制文件**：原 docx 留在用户自己的目录里。
      复制进 data/ 的坏处是产生"哪个才是最新版"的困惑，且容易误提交。
    · **content_hash 是 LLM 缓存 key 的组成部分**（见 llm/cache.py）。
      没有它就会出现"我改了简历，同一岗位却仍然返回旧匹配分"——
      而"改了简历分数应该变"正是这个工具最核心的价值。
    · resume_versions 存 **fidelity_report_json**：FidelityGuard 的检查结果。
      这样"某一版为什么被拒"是可回溯的，而不是只留一句"生成失败"。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from jobhunter.models.base import BaseModel, UTCDateTime, json_text, utc_now_default

__all__ = ["Resume", "ResumeVersion"]


class Resume(BaseModel):
    """一份简历（原始 docx 的结构化投影）。

    `content_json` 的结构（P1 由 resume_svc 解析产出）：
        {
          "basic": {"name":…, "phone":…, "email":…, "city":…, "job_intention":…},
          "education": [{"school":…, "major":…, "degree":…, "period":…}],
          "skills": ["Python", "Vue", …],
          "projects": [{"name":…, "role":…, "period":…, "bullets":[…]}],
          "internships": [{"company":…, "role":…, "period":…, "bullets":[…]}],
          "raw_text": "……"          # 原文兜底：规则解析漏掉的段落还能被 LLM 兜住
        }

    ★ 注意：content_json 含真实姓名/手机号/邮箱 → **绝不进日志、绝不进导出以外的文件**。
    """

    __tablename__ = "resumes"

    # 简历名，如"主简历"、"AI Agent 版"
    name: Mapped[str | None] = mapped_column(String(120))
    # 求职方向：AI Agent / 前端 / 后端…（`jh resume split` 帮我拆出来）
    direction: Mapped[str | None] = mapped_column(String(60))
    # 原始文件路径（docx/pdf）。只记路径，不复制文件。
    file_path: Mapped[str | None] = mapped_column(Text)
    # 结构化解析结果（JSON 字符串，中文可读）
    content_json: Mapped[str | None] = json_text()
    # ★ sha256(规范化后的简历内容) —— LLM 缓存 key 的一部分
    content_hash: Mapped[str | None] = mapped_column(String(64))
    # 是否默认简历：按方向挑不到匹配简历时用它
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now_default, nullable=False
    )

    versions: Mapped[list[ResumeVersion]] = relationship(
        back_populates="resume",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        # 缓存命中与"同一份简历重复导入"检测都要按 hash 查
        Index("ix_resumes_content_hash", "content_hash"),
        # `jh resume list --direction` 与匹配时按方向挑简历
        Index("ix_resumes_direction", "direction"),
    )

    def __repr__(self) -> str:
        return f"<Resume id={self.id} name={self.name!r} direction={self.direction!r}>"


class ResumeVersion(BaseModel):
    """为某个岗位定制的简历版本（P5）。

    流程：生成建议 → 我逐条审核（approved_ids）→ 导出新 docx（file_path）。
    **永远不覆盖原简历文件** —— 导出目标是新文件，且导出后校验原文件 hash 未变。
    """

    __tablename__ = "resume_versions"

    resume_id: Mapped[int] = mapped_column(
        ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False
    )
    # ★ SET NULL 而不是 CASCADE：岗位记录将来被清理时，
    #   不该连带删掉"我为它定制过一版简历"的记录（那是我的工作成果）。
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    # 导出的**新** docx 路径
    file_path: Mapped[str | None] = mapped_column(Text)
    # {"match_analysis": {...}, "suggestions": [{"section","original","revised","reason"}]}
    suggestions_json: Mapped[str | None] = json_text()
    # 我批准过的建议 id 列表 —— 人工审核留痕，"这版为什么要这么改"可回溯
    approved_ids: Mapped[str | None] = json_text()
    # ★ FidelityGuard 检查结果：{"passed": bool, "violations": [...]}
    fidelity_report_json: Mapped[str | None] = json_text()
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now_default, nullable=False
    )

    resume: Mapped[Resume] = relationship(back_populates="versions")

    __table_args__ = (
        Index("ix_resume_versions_resume_id", "resume_id"),
        Index("ix_resume_versions_job_id", "job_id"),
    )

    def __repr__(self) -> str:
        return f"<ResumeVersion id={self.id} resume_id={self.resume_id} job_id={self.job_id}>"

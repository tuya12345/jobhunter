"""`jh tailor` 的 CLI 行为测试（P5）。

这里只测"用户在终端里看到什么 / 退出码是多少"，服务层逻辑在 `test_tailor_svc.py`。
重点守三件事：

    ① `--all` 写在编号后面也要生效（Click 的可变位置参数会吞掉后面的选项，
       P4.5 在 `jh apply` 上踩过同样的坑 —— 这条测试就是那个坑的看门人）
    ② 没配 key / 没批准 / 没版本时**说人话 + 非 0 退出**，不假装成功
    ③ 帮助里六个子命令都在（漏注册会被 `jh --help` 悄悄吞掉）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from jobhunter.cli.app import app
from jobhunter.models import Job, Resume, ResumeVersion
from jobhunter.services.resume_svc import compute_content_hash

from .test_tailor_svc import (  # noqa: F401 - 复用同一份假简历，避免两份 fixture 漂移
    BULLET_INTERN,
    BULLET_PYTHON,
    SKILLS_LINE,
    fake_content,
)

runner = CliRunner()


def _seed(session) -> None:
    content = fake_content()
    session.add(
        Resume(
            name="主简历",
            direction="Python 后端开发",
            content_json=content.model_dump_json(),
            content_hash=compute_content_hash(content),
            is_default=True,
        )
    )
    session.add(
        Job(
            platform="manual",
            company="某某科技",
            title="Python 后端开发实习生",
            city="深圳",
            jd_text="要求熟悉 Python、FastAPI、MySQL，了解 Redis；有 Kubernetes 经验优先。",
            jd_hash="j" * 64,
            status="scored",
            match_score=72,
            jd_keywords_json='["Python", "FastAPI", "MySQL", "Redis", "Kubernetes"]',
        )
    )
    session.commit()


@pytest.fixture
def seeded(session) -> Any:
    _seed(session)
    return session


def _craft_version(session, suggestions: list[dict[str, Any]]) -> ResumeVersion:
    resume = session.scalars(select(Resume)).one()
    job = session.scalars(select(Job)).one()
    version = ResumeVersion(
        resume_id=resume.id,
        job_id=job.id,
        suggestions_json=json.dumps(
            {
                "match_analysis": {
                    "score": 72,
                    "strengths": ["Python"],
                    "gaps": ["Kubernetes"],
                    "missing_keywords": ["Kubernetes"],
                },
                "suggestions": [{"id": i + 1, **item} for i, item in enumerate(suggestions)],
            },
            ensure_ascii=False,
        ),
    )
    session.add(version)
    session.commit()
    return version


CLEAN_SUGGESTIONS = [
    {
        "section": "项目经历",
        "original": BULLET_PYTHON,
        "revised": "用 Python + FastAPI 完成了 3 个业务模块的接口开发",
        "reason": "与 JD 用词对齐",
    },
    {
        "section": "专业技能",
        "original": SKILLS_LINE,
        "revised": "Python、FastAPI、MySQL、Redis、Docker、Git（后端为主）",
        "reason": "突出后端技能",
    },
]


def test_help_lists_all_subcommands() -> None:
    result = runner.invoke(app, ["tailor", "--help"])
    assert result.exit_code == 0
    for name in ("brief", "analyze", "list", "diff", "approve", "export"):
        assert name in result.output, f"帮助里没有 {name}"


def test_brief_prints_three_sections(seeded) -> None:
    """`jh tailor brief` 是**不花钱**的那一半：没有 key 也必须能跑。"""
    result = runner.invoke(app, ["tailor", "brief", "1"])
    assert result.exit_code == 0, result.output
    assert "重点讲这几个项目" in result.output
    assert "突出这几个技能" in result.output
    assert "要避开什么" in result.output
    assert "订单管理系统" in result.output
    assert "Kubernetes" in result.output, "JD 要而简历没有的技能必须出现在「要避开」里"


def test_analyze_without_key_tells_the_truth(seeded) -> None:
    """没配 key → 退出码 1 + 指路 brief，**不许**造一份假建议出来。"""
    result = runner.invoke(app, ["tailor", "analyze", "1"])
    assert result.exit_code == 1
    assert "jh tailor brief" in result.output
    assert "DEEPSEEK_API_KEY" in result.output


def test_approve_accepts_all_option_after_ids(seeded) -> None:
    """★ Click 的可变位置参数会吞掉后面的 `--all` —— 这条守住那个坑。

    `jh tailor approve 1 --all` 必须等价于 `jh tailor approve --all 1`。
    如果哪天有人删掉 `_pull_all_out_of_tokens`，这里会红。
    """
    version = _craft_version(seeded, CLEAN_SUGGESTIONS)

    result = runner.invoke(app, ["tailor", "approve", str(version.id), "1", "--all"])

    assert result.exit_code == 0, result.output
    assert "已批准 2 条" in result.output
    seeded.refresh(version)
    assert json.loads(version.approved_ids or "[]") == [1, 2]


def test_approve_refuses_fabricated_suggestion_with_reason(seeded) -> None:
    """approve 前的**再校验**：违规时非 0 退出，并把"哪一句为什么"打出来。"""
    version = _craft_version(
        seeded,
        [
            {
                "section": "专业技能",
                "original": SKILLS_LINE,
                "revised": "Python、FastAPI、Kubernetes",
                "reason": "贴 JD",
            }
        ],
    )
    result = runner.invoke(app, ["tailor", "approve", str(version.id), "--all"])
    assert result.exit_code == 1
    assert "拒绝写入" in result.output
    assert "Kubernetes" in result.output
    seeded.refresh(version)
    assert version.approved_ids is None


def test_approve_rejects_non_numeric_id(seeded) -> None:
    version = _craft_version(seeded, CLEAN_SUGGESTIONS)
    result = runner.invoke(app, ["tailor", "approve", str(version.id), "abc"])
    assert result.exit_code == 2
    assert "不是编号" in result.output


def test_diff_shows_before_and_after(seeded) -> None:
    version = _craft_version(seeded, CLEAN_SUGGESTIONS)
    result = runner.invoke(app, ["tailor", "diff", str(version.id)])
    assert result.exit_code == 0, result.output
    assert "- 原：" in result.output
    assert "+ 改：" in result.output
    assert "与 JD 用词对齐" in result.output
    assert "待审" in result.output


def test_export_without_any_version_tells_next_step(seeded) -> None:
    result = runner.invoke(app, ["tailor", "export", "1"])
    assert result.exit_code == 1
    assert "还没有定制版本" in result.output


def test_export_without_approval_tells_next_step(seeded) -> None:
    _craft_version(seeded, CLEAN_SUGGESTIONS)
    result = runner.invoke(app, ["tailor", "export", "1"])
    assert result.exit_code == 1
    assert "没有已批准的建议" in result.output
    assert "jh tailor approve" in result.output


def test_export_writes_new_docx_and_reports_hash(seeded, tmp_path: Path) -> None:
    """端到端（CLI 视角）：批准 → 导出 → 打印"原文件未改动"。"""
    from jobhunter.utils.docx import DocLine, write_docx

    original = write_docx(
        tmp_path / "原简历.docx",
        [DocLine("title", "林小满"), DocLine("body", BULLET_PYTHON)],
    )
    resume = seeded.scalars(select(Resume)).one()
    resume.file_path = str(original)
    seeded.commit()
    version = _craft_version(seeded, CLEAN_SUGGESTIONS)

    assert runner.invoke(app, ["tailor", "approve", str(version.id), "--all"]).exit_code == 0
    result = runner.invoke(app, ["tailor", "export", "1", "--format", "md"])

    assert result.exit_code == 0, result.output
    assert "已导出" in result.output
    assert "未改动 ✓" in result.output
    assert "应用了 2 条已批准建议" in result.output
    assert list(seeded.scalars(select(ResumeVersion)).all())[0].file_path is not None

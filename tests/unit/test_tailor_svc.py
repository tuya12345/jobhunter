"""P5：AI 定制简历 + **FidelityGuard**（确定性测试）。

★★ 这个文件存在的理由（评审明确指出的坑，务必守住）：

    这里**没有一条**"调用真实 LLM，然后断言它的输出里没有 XX"的测试。
    那种测试测的是**模型顺从性**：这次运气好绿了，下次模型心情不好就红，
    而红的原因和代码质量无关 → 最后一定会被人加上 skip 或者直接删掉。
    真实 LLM 的抽样检查放在 `@pytest.mark.live`（nightly 跑，不阻塞 CI）。

    取而代之的是**恶意 fixture**：我手写一段"模型编造出来的"文本
    （含 `Kubernetes` / `300%` / `10个模块` / `2024年`），
    断言它被 `FidelityGuard` 拦住、且拦下的理由正确。
    这些断言 100% 确定，永远不会有 flaky。

测试分四组：
    ① FidelityGuard 的五项检查（含"真话不许误报"）
    ② 生成环节：模型的恶意输出被丢掉、不写库
    ③ approve / export 两处**再校验**（改严口径后旧建议也必须拦得住）
    ④ 导出只写新文件 + 原文件 hash 不变 + 提示词文件的七要素与版本纪律
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunter.llm.base import LLMProvider, LLMResponse, PromptPayload
from jobhunter.models import Job, Resume, ResumeVersion
from jobhunter.services import tailor_svc
from jobhunter.services.fidelity import FidelityGuard
from jobhunter.services.resume_svc import (
    EducationItem,
    InternshipItem,
    ProjectItem,
    ResumeContent,
    compute_content_hash,
)
from jobhunter.utils.docx import DocLine, read_docx, write_docx

# ===========================================================================
# 恶意 fixture 用的"真简历"（全部虚构数据，不含任何真实 PII）
# ===========================================================================
#: 这份简历里**故意没有**：Kubernetes / 300 / 10（个模块）/ 2024 年 /
#: 腾讯科技有限公司 —— 恶意 fixture 就照这些空档来编。
RAW_TEXT = "\n".join(
    [
        "林小满  13800000000  lxm@example.com  求职意向：Python 后端开发",
        "教育经历",
        "虚拟大学 | 计算机科学与技术 | 本科 | 2023.09-2027.06",
        "专业技能",
        "Python、FastAPI、MySQL、Redis、Docker、Git",
        "项目经历",
        "订单管理系统 | 后端负责人 | 2025.03-2025.06",
        "用 Python + FastAPI 写了 3 个模块的接口",
        "用 Redis 做缓存，接口平均耗时从 500ms 降到 80ms",
        "实习经历",
        "某某科技有限公司 | 后端实习生 | 2025.07-2025.09",
        "参与订单服务重构",
    ]
)

BULLET_PYTHON = "用 Python + FastAPI 写了 3 个模块的接口"
BULLET_REDIS = "用 Redis 做缓存，接口平均耗时从 500ms 降到 80ms"
BULLET_INTERN = "参与订单服务重构"
SKILLS_LINE = "Python、FastAPI、MySQL、Redis、Docker、Git"


def fake_content() -> ResumeContent:
    """构造一份可复现的假简历（与上面 RAW_TEXT 一致）。"""
    return ResumeContent(
        basic={
            "name": "林小满",
            "phone": "13800000000",
            "email": "lxm@example.com",
            "job_intention": "Python 后端开发",
        },
        education=[
            EducationItem(
                school="虚拟大学",
                major="计算机科学与技术",
                degree="本科",
                period="2023.09-2027.06",
            )
        ],
        skills=["Python", "FastAPI", "MySQL", "Redis", "Docker", "Git"],
        projects=[
            ProjectItem(
                name="订单管理系统",
                role="后端负责人",
                period="2025.03-2025.06",
                bullets=[BULLET_PYTHON, BULLET_REDIS],
                skills=["Python", "FastAPI", "Redis"],
            )
        ],
        internships=[
            InternshipItem(
                company="某某科技有限公司",
                role="后端实习生",
                period="2025.07-2025.09",
                bullets=[BULLET_INTERN],
                skills=["Python"],
            )
        ],
        raw_text=RAW_TEXT,
        sections={
            "header": ["林小满  13800000000  lxm@example.com  求职意向：Python 后端开发"],
            "education": ["虚拟大学 | 计算机科学与技术 | 本科 | 2023.09-2027.06"],
            "skills": ["Python、FastAPI、MySQL、Redis、Docker、Git"],
            "projects": [
                "订单管理系统 | 后端负责人 | 2025.03-2025.06",
                BULLET_PYTHON,
                BULLET_REDIS,
            ],
            "internships": ["某某科技有限公司 | 后端实习生 | 2025.07-2025.09", BULLET_INTERN],
        },
    )


def make_resume(**overrides: Any) -> Resume:
    content = fake_content()
    base: dict[str, Any] = {
        "name": "主简历",
        "direction": "Python 后端开发",
        "content_json": content.model_dump_json(),
        "content_hash": compute_content_hash(content),
        "is_default": True,
    }
    base.update(overrides)
    return Resume(**base)


def make_job(**overrides: Any) -> Job:
    base: dict[str, Any] = {
        "platform": "manual",
        "company": "某某科技",
        "title": "Python 后端开发实习生",
        "city": "深圳",
        "jd_text": "要求熟悉 Python、FastAPI、MySQL，了解 Redis；有 Kubernetes 经验优先。",
        "jd_hash": "j" * 64,
        "status": "scored",
        "match_score": 72,
        "jd_keywords_json": '["Python", "FastAPI", "MySQL", "Redis", "Kubernetes"]',
    }
    base.update(overrides)
    return Job(**base)


@pytest.fixture
def ready(session: Session, settings) -> Session:
    """一份简历 + 一个岗位（都已 flush，id 可用）。"""
    session.add(make_resume())
    session.add(make_job())
    session.commit()
    return session


class StubProvider(LLMProvider):
    """假 Provider：按顺序吐出预先写好的 JSON（**不联网、不花钱、完全确定**）。"""

    model = "stub-model"

    def __init__(self, *payloads: str) -> None:
        self.payloads = list(payloads)
        self.calls: list[PromptPayload] = []

    def complete(self, payload: PromptPayload) -> LLMResponse:
        self.calls.append(payload)
        content = self.payloads.pop(0) if self.payloads else ""
        return LLMResponse(content=content, model=self.model)


def analysis_json(*suggestions: dict[str, str], score: int = 70) -> str:
    """拼一份模型输出（形状与提示词里的 schema 一致）。"""
    return json.dumps(
        {
            "match_analysis": {
                "score": score,
                "strengths": ["有 Python 后端项目经验"],
                "gaps": ["缺 Kubernetes"],
                "missing_keywords": ["Kubernetes"],
            },
            "suggestions": list(suggestions),
        },
        ensure_ascii=False,
    )


def guard(original: str, generated: str, **kwargs: Any):
    """用假简历跑一次闸门（`resume=` 默认给整份简历，与真实调用点一致）。"""
    kwargs.setdefault("resume", fake_content())
    return FidelityGuard.check(original, generated, **kwargs)


# ===========================================================================
# ① FidelityGuard：五项检查
# ===========================================================================
MALICIOUS_CASES = [
    # (原文, 模型的"改写", 期望被拦下的 kind, 说明)
    (
        BULLET_PYTHON,
        "用 Python + FastAPI + Kubernetes 写了 3 个模块的接口",
        "skill_not_in_resume",
        "简历里没有 Kubernetes",
    ),
    (BULLET_REDIS, "用 Redis 做缓存，接口性能提升 300%", "number_not_in_resume", "简历里没有 300%"),
    (
        BULLET_PYTHON,
        "用 Python 写了 10 个模块的接口",
        "number_not_in_resume",
        "3 个模块被夸成 10 个",
    ),
    (
        BULLET_PYTHON,
        "2024 年起用 Python 写了 3 个模块的接口",
        "date_not_in_resume",
        "简历里没有 2024 年",
    ),
    (
        BULLET_INTERN,
        "参与腾讯科技有限公司的订单服务重构",
        "entity_not_in_resume",
        "简历里没有这家公司",
    ),
    (
        BULLET_INTERN,
        "参与订单服务重构（ByteDance 实习期间）",
        "entity_not_in_resume",
        "简历里没有 ByteDance",
    ),
    (
        BULLET_PYTHON,
        "用 Python + FastAPI 写了 3 个模块的接口，"
        "负责整体架构设计与跨团队协作，推动了部门的技术升级和流程改造",
        "too_long",
        "改写不能是扩写",
    ),
]


@pytest.mark.parametrize(
    ("original", "generated", "kind", "why"),
    MALICIOUS_CASES,
    ids=[case[3] for case in MALICIOUS_CASES],
)
def test_guard_blocks_fabrications(original: str, generated: str, kind: str, why: str) -> None:
    """依据：设计文档 §七.3「AI 改简历必须保真 → FidelityGuard **代码强制**」，
    以及提示词 7 的"恶意 fixture 断言全部被拦"（Kubernetes/300%/10个模块/2024年）。"""
    report = guard(original, generated)
    kinds = [item.kind for item in report.violations]
    assert not report.passed, f"没拦住：{why}\n{generated}"
    assert kind in kinds, f"拦下了，但理由不对（{why}）：{kinds}"


def test_guard_check_list_is_complete() -> None:
    """五项检查**一项都不能少**（删检查必须同时改 `CHECKS`，测试会拦下）。

    依据：提示词 7 列出的五条确定性检查项。
    """
    assert set(FidelityGuard.CHECKS) == {
        "entity_not_in_resume",
        "skill_not_in_resume",
        "number_not_in_resume",
        "date_not_in_resume",
        "too_long",
    }


TRUTHFUL_CASES = [
    (
        "用 Python + FastAPI 写了 3 个模块的接口",
        "用 Python + FastAPI 完成了 3 个业务模块的接口开发",
        "措辞调整",
    ),
    (
        BULLET_PYTHON,
        "用 Python + FastAPI 写了 3 个模块的接口，并对外提供 REST 接口",
        "REST 属于写法泛称，不该报编造",
    ),
    (BULLET_REDIS, "用 Redis 做缓存，把接口平均耗时从 500ms 压到 80ms", "数字与单位都在原文里"),
    (BULLET_INTERN, "参与某某科技有限公司的订单服务重构", "真公司名"),
    (BULLET_INTERN, "在虚拟大学期间参与订单服务重构", "真学校名"),
    (
        BULLET_PYTHON,
        "用 Python + FastAPI 写了 3 个模块的接口，并用 Docker 部署上线",
        "部署属于通用实践词（ISSUES #30 的边界要延续到 P5）",
    ),
    (BULLET_PYTHON, "负责订单管理系统 3 个模块的 Python 接口开发", "重排 + 对齐措辞"),
]


@pytest.mark.parametrize(
    ("original", "generated", "why"), TRUTHFUL_CASES, ids=[case[2] for case in TRUTHFUL_CASES]
)
def test_guard_does_not_flag_truthful_rewrites(original: str, generated: str, why: str) -> None:
    """依据：`fidelity` 模块 docstring「误报比漏报更糟：误报多了这个检查就会被人关掉」。"""
    report = guard(original, generated)
    assert report.passed, f"把真话判成编造了（{why}）：{report.summary()}"


def test_guard_still_strict_about_tech_stack() -> None:
    """豁免只限"动作/通用实践词"，技术栈名字仍然严格（P4 ISUSES #30 修法的边界）。

    如果哪天有人把豁免放宽成"所有技能都不判"，这条测试会立刻红。
    """
    ok = guard("用 Python 写了接口", "用 Python 写了接口，并用 Docker 部署上线")
    assert ok.passed, ok.summary()
    bad = guard("用 Python 写了接口", "用 Python 和 Kubernetes 写了接口")
    assert [item.kind for item in bad.violations] == ["skill_not_in_resume"]


def test_guard_learning_whitelist_needs_marker() -> None:
    """白名单技能可以提，但**必须**带"在学习"标注（提示词 7 第 1 条的括号部分）。"""
    with_marker = guard(
        "熟悉 Python",
        "熟悉 Python，正在学习 LangChain",
        learning_whitelist={"LangChain"},
    )
    assert with_marker.passed, with_marker.summary()
    assert any("LangChain" in note for note in with_marker.notes)

    without_marker = guard(
        "熟悉 Python", "熟悉 Python、LangChain", learning_whitelist={"LangChain"}
    )
    assert not without_marker.passed
    assert [item.kind for item in without_marker.violations] == ["skill_not_in_resume"]


def test_guard_length_limit_is_configurable() -> None:
    """长度上限默认 150%，且**可配置**（配置项 tailor.max_length_ratio）。"""
    original = "用 Python 写了接口" * 40  # 360 字（够长，绝对余量不会盖过比例）
    generated = original + "补充一句真实的细节" * 6  # 414 字 ≈ 1.15 倍
    assert guard(original, generated).passed
    strict = guard(original, generated, max_ratio=1.1)
    assert [item.kind for item in strict.violations] == ["too_long"]


def test_guard_short_sentence_gets_absolute_margin() -> None:
    """短句要有绝对余量，否则"150%"会退化成"短句一律不许改"（纯误报）。"""
    report = guard("参与重构", "参与订单服务模块的重构工作")
    assert report.passed, report.summary()


def test_guard_report_is_json_serialisable() -> None:
    """报告要能落 `resume_versions.fidelity_report_json`（且**不含原文**，避免隐私面变大）。"""
    report = guard(BULLET_PYTHON, "用 Python + Kubernetes 写了 3 个模块的接口")
    payload = report.as_dict()
    assert payload["passed"] is False
    assert isinstance(payload["violations"], list)
    text = json.dumps(payload, ensure_ascii=False)
    assert BULLET_PYTHON not in text, "报告里不该复述原文"
    assert "Kubernetes" in text, "但违规的 token 要留着，否则没法排查"


# ===========================================================================
# ② 生成环节：恶意输出被丢掉、不写库
# ===========================================================================
def test_analyze_drops_malicious_suggestions(ready: Session, settings) -> None:
    """依据：提示词 7「在【生成】【approve】【export】三处强制调用；违规拒绝写入」。

    一条好建议 + 两条编造 → 只有好的那条能进库，被拦下的原因要留在报告里。
    """
    job = ready.scalars(select(Job)).one()
    provider = StubProvider(
        analysis_json(
            {
                "section": "项目经历",
                "original": BULLET_PYTHON,
                "revised": "用 Python + FastAPI 完成了 3 个业务模块的接口开发",
                "reason": "与 JD 用词对齐",
            },
            {
                "section": "专业技能",
                "original": SKILLS_LINE,
                "revised": "Python、FastAPI、MySQL、Redis、Docker、Git、Kubernetes",
                "reason": "贴 JD",
            },
            {
                "section": "项目经历",
                "original": BULLET_REDIS,
                "revised": "用 Redis 做缓存，接口性能提升 300%",
                "reason": "量化成果",
            },
        )
    )

    result = tailor_svc.analyze_and_save(ready, job, provider=provider, settings=settings)

    assert len(result.kept) == 1
    assert "FastAPI" in result.kept[0].revised
    assert len(result.dropped) == 2
    assert result.version_id is not None

    version = ready.get(ResumeVersion, result.version_id)
    # ★ 只看 suggestions：`match_analysis` 里出现"缺 Kubernetes"是**对的**
    #   （那是给我看的分析，不写进简历）；要守的是"编造不能进建议列表"。
    stored = json.dumps(
        json.loads(version.suggestions_json or "{}")["suggestions"], ensure_ascii=False
    )
    assert "Kubernetes" not in stored, "编造的建议**绝不能**进库（否则 approve 时就成了默认真话）"
    assert "300%" not in stored
    report = json.loads(version.fidelity_report_json or "{}")
    assert report["passed"] is False
    assert len(report["dropped"]) == 2
    assert any("Kubernetes" in str(item) for item in report["violations"])


def test_analyze_refuses_to_write_when_everything_is_fabricated(ready: Session, settings) -> None:
    """整批被拦时不写库、退出码语义由 CLI 处理（这里断言"库里没有新版本"）。"""
    job = ready.scalars(select(Job)).one()
    provider = StubProvider(
        analysis_json(
            {
                "section": "专业技能",
                "original": "熟悉 Python",
                "revised": "熟悉 Python、Kubernetes",
                "reason": "贴 JD",
            },
            {
                "section": "项目经历",
                "original": BULLET_PYTHON,
                "revised": "2024 年用 Python 写了 10 个模块的接口",
                "reason": "量化",
            },
        )
    )

    result = tailor_svc.analyze_and_save(ready, job, provider=provider, settings=settings)

    assert result.refused is True
    assert result.version_id is None
    assert ready.scalars(select(ResumeVersion)).all() == []


def test_analyze_drops_unlocatable_and_noop_suggestions(ready: Session, settings) -> None:
    """两种"不是编造但没法用"的建议也要丢掉并说明原因（不能静默）。

    ① `original` 在简历里找不到 → 导出时没法定位替换点
    ② 改完和原文一样 → 空操作，只会浪费审核时间
    """
    job = ready.scalars(select(Job)).one()
    provider = StubProvider(
        analysis_json(
            {
                "section": "项目经历",
                "original": "模型自己转述的一句话",
                "revised": "换一种说法的转述",
                "reason": "措辞",
            },
            {
                "section": "项目经历",
                "original": BULLET_PYTHON,
                "revised": BULLET_PYTHON,
                "reason": "没改",
            },
            {
                "section": "项目经历",
                "original": BULLET_INTERN,
                "revised": "参与订单服务的重构与联调",
                "reason": "措辞",
            },
        )
    )

    result = tailor_svc.analyze_and_save(ready, job, provider=provider, settings=settings)

    assert len(result.kept) == 1
    reasons = " ".join(" ".join(item.reasons) for item in result.dropped)
    assert "找不到" in reasons
    assert "一模一样" in reasons


def test_analyze_without_key_fails_loudly(ready: Session, settings) -> None:
    """没配 key 时**不许假装成功**，也不许造一份假建议（与开场白的模板降级不同）。

    理由：字段级改写没有"诚实的模板"，硬造只会是假建议 —— 所以直接报错并指路。
    """
    job = ready.scalars(select(Job)).one()
    with pytest.raises(tailor_svc.TailorError) as excinfo:
        tailor_svc.analyze_and_save(ready, job, settings=settings)
    assert "jh tailor brief" in str(excinfo.value)
    assert ready.scalars(select(ResumeVersion)).all() == []


def test_analyze_cache_is_task_scoped(ready: Session, settings) -> None:
    """缓存走统一入口且 **task=tailor** —— 否则会和 match / greeting 撞 key。"""
    job = ready.scalars(select(Job)).one()
    good = {
        "section": "项目经历",
        "original": BULLET_PYTHON,
        "revised": "用 Python + FastAPI 完成了 3 个业务模块的接口开发",
        "reason": "对齐 JD 用词",
    }
    provider = StubProvider(analysis_json(good), analysis_json(good))

    first = tailor_svc.analyze_and_save(ready, job, provider=provider, settings=settings)
    second = tailor_svc.analyze_and_save(ready, job, provider=provider, settings=settings)

    assert len(provider.calls) == 1, "第二次应当命中缓存（不再花钱）"
    assert second.from_cache is True
    assert first.version_id != second.version_id  # 每次 analyze 都是一版，可回溯

    from jobhunter.models import LLMCache

    rows = ready.scalars(select(LLMCache)).all()
    assert [row.task for row in rows] == ["tailor"]


# ===========================================================================
# ③ approve / export 两处再校验
# ===========================================================================
def _craft_version(
    session: Session,
    *,
    suggestions: list[dict[str, Any]],
    approved: list[int] | None = None,
) -> ResumeVersion:
    """直接往库里塞一条"历史版本"（模拟"当时合格、后来口径变严"的数据）。"""
    resume = session.scalars(select(Resume)).one()
    job = session.scalars(select(Job)).one()
    version = ResumeVersion(
        resume_id=resume.id,
        job_id=job.id,
        suggestions_json=json.dumps(
            {
                "match_analysis": {
                    "score": 70,
                    "strengths": [],
                    "gaps": [],
                    "missing_keywords": [],
                },
                "suggestions": [
                    {"id": index + 1, **item} for index, item in enumerate(suggestions)
                ],
            },
            ensure_ascii=False,
        ),
        approved_ids=None if approved is None else json.dumps(approved),
    )
    session.add(version)
    session.commit()
    return version


def test_approve_rechecks_and_refuses(ready: Session, settings) -> None:
    """依据：提示词 7「在【生成】【approve】【export】三处强制调用」。

    库里存的是**上一次**生成的建议，而判定口径会变严 ——
    所以 approve 必须用当前口径重判一次，否则"上次检查过"就成了永久通行证。
    """
    version = _craft_version(
        ready,
        suggestions=[
            {
                "section": "专业技能",
                "original": "熟悉 Python",
                "revised": "熟悉 Python、Kubernetes",
                "reason": "贴 JD",
            },
        ],
    )
    with pytest.raises(tailor_svc.TailorFidelityError) as excinfo:
        tailor_svc.approve(ready, version, all_=True, settings=settings)
    assert any("Kubernetes" in item for item in excinfo.value.violations)
    ready.refresh(version)
    assert version.approved_ids is None, "有违规时整批都不许写"


def test_approve_writes_ids_for_clean_suggestions(ready: Session, settings) -> None:
    version = _craft_version(
        ready,
        suggestions=[
            {
                "section": "项目经历",
                "original": BULLET_PYTHON,
                "revised": "用 Python + FastAPI 完成了 3 个业务模块的接口开发",
                "reason": "对齐",
            },
            {
                "section": "项目经历",
                "original": BULLET_INTERN,
                "revised": "参与订单服务的重构与联调",
                "reason": "措辞",
            },
        ],
    )
    merged = tailor_svc.approve(ready, version, ids=[2], settings=settings)
    assert merged == [2]
    merged = tailor_svc.approve(ready, version, all_=True, settings=settings)
    assert merged == [1, 2]


def test_approve_rejects_unknown_id(ready: Session, settings) -> None:
    version = _craft_version(
        ready,
        suggestions=[
            {"section": "项目经历", "original": BULLET_PYTHON, "revised": "改一句", "reason": "x"}
        ],
    )
    with pytest.raises(tailor_svc.TailorError) as excinfo:
        tailor_svc.approve(ready, version, ids=[9], settings=settings)
    assert "没有这些编号" in str(excinfo.value)


def test_export_writes_new_file_and_leaves_original_untouched(
    ready: Session, settings, tmp_path: Path
) -> None:
    """依据：提示词 7「导出**写新文件**，并测"原简历文件 hash 不变"」。"""
    original_dir = tmp_path / "resume"
    original_dir.mkdir()
    original_path = write_docx(
        original_dir / "我的简历.docx",
        [DocLine("title", "林小满"), DocLine("body", BULLET_PYTHON)],
    )
    before = hashlib.sha256(original_path.read_bytes()).hexdigest()
    resume = ready.scalars(select(Resume)).one()
    resume.file_path = str(original_path)
    ready.commit()

    version = _craft_version(
        ready,
        suggestions=[
            {
                "section": "项目经历",
                "original": BULLET_PYTHON,
                "revised": "用 Python + FastAPI 完成了 3 个业务模块的接口开发",
                "reason": "对齐",
            },
            {
                "section": "项目经历",
                "original": BULLET_REDIS,
                "revised": "用 Redis 做缓存，把接口平均耗时从 500ms 压到 80ms",
                "reason": "量化",
            },
        ],
        approved=[1, 2],
    )

    result = tailor_svc.export_version(ready, version, settings=settings)

    assert result.path.is_file()
    assert result.path != original_path
    assert hashlib.sha256(original_path.read_bytes()).hexdigest() == before
    assert result.original_unchanged is True
    assert result.original_hash_before == before
    assert len(result.applied) == 2

    text = read_docx(result.path).text
    assert "订单管理系统" in text, "导出必须保留原文的项目名"
    assert "完成了 3 个业务模块的接口开发" in text, "批准的改写要真的写进文件"
    assert "压到 80ms" in text
    assert "专业技能" in text, "分节标题要保留（排版正常的最低要求）"
    ready.refresh(version)
    assert version.file_path == str(result.path)


def test_export_refuses_when_fidelity_fails(ready: Session, settings) -> None:
    """export 是最后一道：即使 approved_ids 被人为写脏，也不许把编造写进文件。"""
    version = _craft_version(
        ready,
        suggestions=[
            {
                "section": "专业技能",
                "original": "熟悉 Python",
                "revised": "熟悉 Python、Kubernetes、Go",
                "reason": "贴 JD",
            },
        ],
        approved=[1],
    )
    with pytest.raises(tailor_svc.TailorFidelityError):
        tailor_svc.export_version(ready, version, settings=settings)
    assert list(settings.paths.output_dir.glob("*")) == [], "被拒时不许留下半个文件"


def test_export_requires_approval_first(ready: Session, settings) -> None:
    version = _craft_version(
        ready,
        suggestions=[
            {"section": "项目经历", "original": BULLET_PYTHON, "revised": "改一句", "reason": "x"}
        ],
    )
    with pytest.raises(tailor_svc.TailorError) as excinfo:
        tailor_svc.export_version(ready, version, settings=settings)
    assert "还没有批准" in str(excinfo.value)


def test_export_never_overwrites_an_existing_file(ready: Session, settings, tmp_path: Path) -> None:
    """导出**永远**只写新文件：--out 指到已存在的文件时必须拒绝。"""
    version = _craft_version(
        ready,
        suggestions=[
            {
                "section": "项目经历",
                "original": BULLET_PYTHON,
                "revised": "用 Python + FastAPI 完成了 3 个业务模块的接口开发",
                "reason": "对齐",
            },
        ],
        approved=[1],
    )
    existing = tmp_path / "已存在.docx"
    existing.write_text("别动我", encoding="utf-8")
    with pytest.raises(tailor_svc.TailorError) as excinfo:
        tailor_svc.export_version(ready, version, settings=settings, out=existing)
    assert "不会覆盖" in str(excinfo.value)
    assert existing.read_text(encoding="utf-8") == "别动我"


def test_export_markdown_fallback_without_docx_dependency(ready: Session, settings) -> None:
    """没有 docx extra 时也能导出（--format md）：这条出口必须一直可用。"""
    version = _craft_version(
        ready,
        suggestions=[
            {
                "section": "项目经历",
                "original": BULLET_INTERN,
                "revised": "参与订单服务的重构与联调",
                "reason": "措辞",
            },
        ],
        approved=[1],
    )
    result = tailor_svc.export_version(ready, version, settings=settings, fmt="md")
    assert result.path.suffix == ".md"
    content = result.path.read_text(encoding="utf-8")
    assert "参与订单服务的重构与联调" in content


# ===========================================================================
# ④ brief：不花钱的那一半
# ===========================================================================
def test_brief_is_deterministic_and_covers_three_questions(ready: Session, settings) -> None:
    """依据：提示词 7「先做 `jh tailor brief`：重点讲哪 3 个项目、突出哪 2 个技能、要避开什么」。"""
    job = ready.scalars(select(Job)).one()
    brief = tailor_svc.build_brief(ready, job, settings=settings)

    assert 1 <= len(brief.points) <= settings.tailor.brief_projects
    assert len(brief.highlight) <= settings.tailor.brief_skills
    assert brief.avoid, "JD 里有简历没有的技能（Kubernetes），必须给出「要避开」的提醒"
    assert brief.points[0].name == "订单管理系统", "与 JD 对得上的项目要排第一"
    assert "Python" in brief.points[0].hits

    # 讲法自检：brief 教我的每一句都必须是简历里有的
    for point in brief.points:
        if point.line:
            assert guard(point.line, point.line).passed, point.line


def test_brief_talk_line_never_mentions_missing_skills(ready: Session, settings) -> None:
    """brief 的"怎么讲"只能出现简历里真的有的技能（否则等于教人编造）。"""
    job = ready.scalars(select(Job)).one()
    brief = tailor_svc.build_brief(ready, job, settings=settings)
    for point in brief.points:
        assert "Kubernetes" not in point.line


def test_brief_avoids_missing_skills_and_hard_gaps(ready: Session, settings) -> None:
    job = ready.scalars(select(Job)).one()
    job.experience = "3-5年"
    ready.commit()
    brief = tailor_svc.build_brief(ready, job, settings=settings)
    joined = " ".join(brief.avoid)
    assert "Kubernetes" in joined
    assert "硬条件" in joined, "岗位要 3-5 年经验，brief 必须提醒别硬凑"


# ===========================================================================
# ⑤ 提示词文件：七要素 + 版本号纪律
# ===========================================================================
#: `prompts/tailor.md` 去掉版本注释后的正文指纹。
#: ★ 改了正文就必须**同时**做两件事：① 把文件里的 `prompt-version` +1；
#:   ② 把这里的摘要换成新值。忘了 ① 会导致缓存不失效（改了提示词却在吃旧结果），
#:   这条测试就是为了让那个错误变成一次红灯，而不是一个查不出来的怪现象。
PROMPT_BODY_SHA256 = {
    # v1：首版（七要素 + 三条硬约束 + 两个反例）
    1: "753692153d2b46ea9d0e972ac183f51bee557d9c60454e09139b1799a9719bc2",
    # v2：真实联调发现模型会把「参与」升级成「负责」（程度词升级）→ 加"程度守恒"规则
    2: "960626b9b4a7a60bf7b187aab25d69d64f5129f357e35b6be6cb1b89286e6548",
}

SEVEN_ELEMENTS = ("角色", "任务", "依据", "约束与边界", "输出格式", "示例", "思考方式")


def test_prompt_file_has_seven_elements() -> None:
    """依据：提示词 7「系统提示词放 prompts/tailor.md（七要素齐全）」。"""
    prompt = tailor_svc.load_prompt()
    for element in SEVEN_ELEMENTS:
        assert element in prompt.body, f"提示词缺少要素：{element}"
    assert "{max_suggestions}" not in prompt.body, "占位符应当用 $var（Template），不是 {var}"


def test_prompt_version_matches_body_fingerprint() -> None:
    """提示词正文变了 → 版本号必须 +1（否则缓存不失效，改了提示词等于没改）。"""
    prompt = tailor_svc.load_prompt()
    digest = hashlib.sha256(prompt.body.encode("utf-8")).hexdigest()
    expected = PROMPT_BODY_SHA256.get(prompt.version)
    assert expected is not None, (
        f"提示词版本 {prompt.version} 还没有登记正文指纹。"
        f"当前正文 sha256 = {digest}（把它加进 PROMPT_BODY_SHA256 再提交）"
    )
    assert digest == expected, (
        "prompts/tailor.md 的正文改了，但版本号没变。\n"
        f"  当前正文 sha256 = {digest}\n"
        f"  登记值（v{prompt.version}） = {expected}\n"
        f"  正确做法：把文件里的 <!-- prompt-version: {prompt.version} --> 改成 "
        f"{prompt.version + 1}，再把新摘要登记到 PROMPT_BODY_SHA256。"
    )


def test_prompt_render_fills_limits(settings) -> None:
    """提示词里的长度/条数约束要来自配置（改配置就该改提示词文案）。"""
    prompt = tailor_svc.load_prompt(settings)
    rendered = tailor_svc.render_system_prompt(prompt, settings)
    assert f"{int(settings.tailor.max_length_ratio * 100)}%" in rendered
    assert str(settings.tailor.max_suggestions) in rendered
    assert "$max_suggestions" not in rendered


def test_missing_prompt_file_raises_a_clear_error(tmp_path: Path) -> None:
    """提示词丢了要**明确报错**，而不是偷偷用一份内置的旧提示词。"""
    with pytest.raises(tailor_svc.TailorPromptError) as excinfo:
        tailor_svc.load_prompt(path=tmp_path / "nope.md")
    assert "找不到提示词文件" in str(excinfo.value)

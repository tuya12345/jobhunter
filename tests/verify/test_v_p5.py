"""【P5 专项】AI 定制简历 + FidelityGuard（代码强制防编造，三处调用）。

设计文档【提示词 7】的验收标准：
    「tailor brief 能给出"重点讲什么"；完整版能出建议；**恶意 fixture 测试全绿**；
      导出的 docx 排版正常且原文件未被改动」
以及它写死的红线：
    「**防编造是代码强制**（不是靠提示词祈祷）；在【生成】【审核】【导出】三处强制调用；
      **不要写"调用真实 LLM 然后断言输出里没有 XX"这种不稳定测试**」。

这一组就是去证伪这几句话 —— 尤其是"三处强制"到底是不是真的三处。
"""

from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy.orm import Session

from jobhunter.config.settings import get_settings
from jobhunter.models import Job, Resume, ResumeVersion
from jobhunter.services import resume_svc, tailor_svc
from jobhunter.services.fidelity import FidelityGuard
from jobhunter.services.resume_svc import EducationItem, ProjectItem, ResumeContent
from tests._fixture_loader import load_fixture_module
from tests.verify._helpers import build_detail, insert_job, insert_resume, score_job

#: 一份"原简历"（用于改写保真判定的事实来源）
ORIGINAL_RESUME = ResumeContent(
    basic={"name": "林小满", "city": "深圳"},
    education=[
        EducationItem(school="虚构大学", major="软件工程", degree="本科", period="2023.09-2027.06")
    ],
    skills=["Python", "FastAPI", "Docker", "RAG", "LLM", "Redis"],
    projects=[
        ProjectItem(
            name="智能问答助手",
            role="独立开发",
            period="2026.03-2026.08",
            bullets=[
                "用 Python 与 FastAPI 实现检索增强问答，接口响应时间 300ms。",
                "用 Redis 做结果缓存，命中率提升到 60%。",
                "在虚构科技有限公司实习期间参与订单服务开发。",
            ],
            skills=["Python", "FastAPI", "RAG", "Redis"],
        )
    ],
    internships=[],
    raw_text=(
        "林小满 深圳 虚构大学 软件工程 本科 2023.09-2027.06 "
        "Python FastAPI Docker RAG LLM Redis "
        "智能问答助手 用 Python 与 FastAPI 实现检索增强问答，接口响应时间 300ms。 "
        "用 Redis 做结果缓存，命中率提升到 60%。 "
        "在虚构科技有限公司实习期间参与订单服务开发。"
    ),
)


def _guard(original: str, generated: str, *, resume: ResumeContent | None = None):  # noqa: ANN202
    settings = get_settings()
    return FidelityGuard.check(
        original,
        generated,
        resume=resume if resume is not None else ORIGINAL_RESUME,
        vocabulary=resume_svc.load_skill_vocabulary(settings),
        learning_whitelist=set(settings.learning_whitelist),
        settings=settings,
    )


# ===========================================================================
# ① 恶意 fixture 全拦（设计文档点名的四项 + 我自己补的四类）
# ===========================================================================
#: `(原句, 改写句, 期望的 violation kind)`
MALICIOUS = [
    # —— 设计文档【提示词 7】点名的四项 ——
    (
        "用 Python 与 FastAPI 实现检索增强问答。",
        "用 Python 与 FastAPI 实现检索增强问答，熟悉 Kubernetes。",
        "skill_not_in_resume",
    ),
    (
        "用 Redis 做结果缓存，命中率提升到 60%。",
        "用 Redis 做结果缓存，命中率提升 300%。",
        "number_not_in_resume",
    ),
    (
        "用 Python 与 FastAPI 实现检索增强问答。",
        "负责 10个模块 的架构设计与开发。",
        "number_not_in_resume",
    ),
    (
        "用 Redis 做结果缓存，命中率提升到 60%。",
        "2024年 起负责缓存优化，命中率提升到 60%。",
        "date_not_in_resume",
    ),
    # —— 我自己补的四类（验证会话独立设计）——
    # ① 数字连同单位守恒：把 300ms 改小
    ("接口响应时间 300ms。", "接口响应时间 5ms。", "number_not_in_resume"),
    # ② 时间守恒：把毕业年份改晚一年
    (
        "虚构大学 软件工程 本科 2023.09-2027.06",
        "虚构大学 软件工程 本科 2023.09-2028.06",
        "date_not_in_resume",
    ),
    # ③ 单位换掉：300ms → 300%（数字一样，含义完全不同）
    ("接口响应时间 300ms。", "接口响应时间缩短 300%。", "number_not_in_resume"),
    # ④ 编造公司名（实体白名单）
    (
        "在虚构科技有限公司实习期间参与订单服务开发。",
        "在某某网络有限公司实习期间参与订单服务开发。",
        "entity_not_in_resume",
    ),
    # ⑤ 长度上限：改写不是扩写
    (
        "用 Redis 做结果缓存。",
        "用 Redis 做结果缓存。" + "并且深入参与了整体架构的演进与优化工作。" * 6,
        "too_long",
    ),
]


@pytest.mark.parametrize(("original", "generated", "kind"), MALICIOUS)
def test_guard_blocks_malicious_rewrites(original: str, generated: str, kind: str) -> None:
    """★★ 依据：设计文档 §七.3 + 【提示词 7】的
    「喂**恶意 fixture**，断言全部被 FidelityGuard 拦截」。

    ★ 这是**确定性**断言（不调真实 LLM），所以能长期留在 CI 里 ——
      正是评审否掉"调真实 LLM 再断言输出"那条路的原因。
    """
    report = _guard(original, generated)
    assert not report.passed, f"没拦住：{original!r} → {generated!r}（报告说无编造嫌疑）"
    kinds = {item.kind for item in report.violations}
    assert kind in kinds, f"拦住了，但理由不是 {kind}，而是 {kinds}"


def test_every_declared_check_can_actually_fire() -> None:
    """★★ 元测试：`FidelityGuard.CHECKS` 里声明的**每一项**都必须有用例能触发它。

    为什么需要：五项检查里只要有一项被"顺手删掉"或写成了死代码，
    上面那组参数化用例仍然会全绿（它只断言"至少有一项拦住了"）。
    这条把"声明"和"真的会响"绑在一起 ——
    删检查必须同时改 `CHECKS`，否则这里立刻红。
    """
    triggered: set[str] = set()
    for original, generated, _expected in MALICIOUS:
        triggered |= {item.kind for item in _guard(original, generated).violations}

    missing = set(FidelityGuard.CHECKS) - triggered
    assert not missing, (
        f"这些检查项声明了却没有任何用例能触发（可能是死代码）：{missing}\n已触发：{triggered}"
    )


def test_guard_accepts_a_truthful_rewrite() -> None:
    """★★ 真话不许误报（这是 P4 #30 的边界在 P5 是否延续的关键）。

    依据：`fidelity` docstring「误报多了这个检查就会被关掉」。
    下面每一条都是**基于原简历的真实事实**做的措辞调整：
      · 把"用 Python 与 FastAPI 实现……"改成"使用 Python、FastAPI 实现……"（同义词替换）
      · 保留 300ms 与 60% 这两个原文就有的数字
      · 提到"部署"这类通用实践词（P4 #30 的教训）
      · 提到 `REST 接口` 这类写法泛称（不是某家公司的专有名词）
    """
    cases = [
        (
            "用 Python 与 FastAPI 实现检索增强问答，接口响应时间 300ms。",
            "使用 Python、FastAPI 实现了检索增强问答，接口响应时间 300ms。",
        ),
        (
            "用 Redis 做结果缓存，命中率提升到 60%。",
            "用 Redis 做结果缓存，命中率提升到 60%，并部署到了测试环境。",
        ),
        (
            "用 Python 与 FastAPI 实现检索增强问答。",
            "用 Python 与 FastAPI 实现检索增强问答，对外提供 REST 接口。",
        ),
    ]
    for original, generated in cases:
        report = _guard(original, generated)
        assert report.passed, f"真话被误判成编造：{generated!r}\n  → {report.summary()}"


# ===========================================================================
# ② 三处强制调用：生成 / approve / export
# ===========================================================================
def _fabricated_version(session: Session, resume: Resume, job: Job) -> ResumeVersion:
    """造一条**已经写进库**的编造建议（并在 approved_ids 里也算已批准）。

    模拟"判定口径变严之前批准过的东西"——这正是 approve/export 必须再校验的理由。
    """
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
                    {
                        "id": 1,
                        "section": "projects",
                        "original": "用 Python 与 FastAPI 实现检索增强问答。",
                        "revised": (
                            "用 Python、FastAPI 与 Kubernetes 实现检索增强问答，性能提升 300%。"
                        ),
                        "reason": "对齐 JD 关键词",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        approved_ids=json.dumps([1]),
        fidelity_report_json=json.dumps({"passed": True}, ensure_ascii=False),
    )
    session.add(version)
    session.flush()
    return version


@pytest.mark.parametrize(
    "payload",
    [
        '[{"id": 1}]',
        '"just a string"',
        "42",
        "null",
        '{"suggestions": "not a list"}',
    ],
)
def test_wrong_shaped_suggestion_payload_gives_a_readable_error(session, payload: str) -> None:
    """🟡 依据：`suggestions_of` 的 `except json.JSONDecodeError` 注释
    「**只有手工改库才会遇到**」—— 说明作者本就打算让"库里的数据坏了"走**可读错误**
    （`TailorError` → CLI 打一句人话）。

    但那个 guard **只覆盖"不是合法 JSON"**：合法 JSON 但**结构不对**
    （数组 / 字符串 / 数字 / null，或 `suggestions` 不是列表）时，
    `data.get(...)` 会抛裸 `AttributeError: 'list' object has no attribute 'get'`
    （`tailor_svc.py:917`；`analysis_of:927` 同样）。

    这是"**手工改库**"这条路径上的真实缺口：用户/脚本写进去一个 JSON 数组是很自然的形态，
    而 `AttributeError` 冲出来时既看不懂、也不知道该改哪。
    """
    resume = insert_resume(session)
    job = insert_job(session)
    version = ResumeVersion(
        resume_id=resume.id, job_id=job.id, suggestions_json=payload, approved_ids="[]"
    )
    session.add(version)
    session.flush()

    with pytest.raises(tailor_svc.TailorError):
        tailor_svc.suggestions_of(version)


def test_approve_rejects_fabricated_suggestion(session, settings) -> None:
    """★★ 依据：`approve` docstring「批准意味着"这句话可以进简历"，
    所以必须用**当前**的口径重新判一次」+「有违规时**整批不写**」。

    对抗：库里已经躺着一条编造建议（approved_ids 也写了）→ 再批准必须被拒。
    """
    resume = insert_resume(session)
    job = insert_job(session)
    version = _fabricated_version(session, resume, job)

    with pytest.raises(tailor_svc.TailorFidelityError) as excinfo:
        tailor_svc.approve(session, version, ids=[1], settings=settings)

    # ★ 违规明细在 `.violations`（CLI 会逐条渲染）——只看 `str(exc)` 会漏掉真正的理由
    detail = " ".join(getattr(excinfo.value, "violations", []))
    assert "Kubernetes" in detail or "300" in detail, (
        f"拒了但没说清哪里编造：{detail or str(excinfo.value)}"
    )


def test_export_rejects_fabricated_suggestion_and_writes_no_file(
    session, settings, isolated_env
) -> None:  # noqa: ARG001
    """★★ 依据：`export_version` 的「逐条 + 整篇两道」+ 设计文档
    「导出**只写新文件**」+【提示词 7】「导出的 docx 排版正常且原文件未被改动」。

    对抗：库里那条编造建议已批准 → 导出必须被拒，且 **output 目录里一个文件都不许多**。
    """
    resume = insert_resume(session)
    job = insert_job(session)
    version = _fabricated_version(session, resume, job)

    out_dir = settings.paths.output_dir
    before = {path.name for path in out_dir.iterdir()} if out_dir.exists() else set()

    with pytest.raises(tailor_svc.TailorError):
        tailor_svc.export_version(session, version, settings=settings)

    after = {path.name for path in out_dir.iterdir()} if out_dir.exists() else set()
    assert after == before, f"导出被拒了却仍然产生了文件：{sorted(after - before)}"


# ===========================================================================
# ③ 导出：原文件 hash 不变 + 不覆盖已有文件
# ===========================================================================
def _real_resume(session: Session, tmp_path) -> Resume:  # noqa: ANN001
    """用 fixture 生成一份**真的 docx** 并导入（导出要读它的 file_path 算 hash）。"""
    sample = load_fixture_module("make_sample_resume")
    path = tmp_path / "我的简历.docx"
    sample.build_sample_resume(path)
    result = resume_svc.add_resume(session, path, make_default=True)
    row = session.get(Resume, result.resume_id)
    assert row is not None
    return row


def _truthful_version(session: Session, resume: Resume, job: Job) -> ResumeVersion:
    """造一条**真实**的建议：拿简历里真实存在的 bullet 做同义改写。"""
    content = resume_svc.content_from_json(resume.content_json)
    bullet = next(
        (b for item in content.projects for b in item.bullets if len(b) >= 12),
        "",
    )
    assert bullet, "测试自身问题：假简历里没找到可用的 bullet"
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
                    {
                        "id": 1,
                        "section": "projects",
                        "original": bullet,
                        "revised": bullet + "。",
                        "reason": "语气更完整",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        approved_ids=json.dumps([1]),
        fidelity_report_json=json.dumps({"passed": True}, ensure_ascii=False),
    )
    session.add(version)
    session.flush()
    return version


def test_export_does_not_touch_the_original_file(session, settings, tmp_path) -> None:
    """★★ 依据：设计文档 §七.6「导出可能原地改简历 → **必须写新文件** + 测『原文件 hash 不变』」
    （评审采纳项 #10，被列为红线之一）。

    这条必须用**真 docx** 走一遍完整路径：导入 → 造版本 → 导出 → 对比 sha256。
    """
    resume = _real_resume(session, tmp_path)
    assert resume.file_path
    original = __import__("pathlib").Path(resume.file_path)
    before = hashlib.sha256(original.read_bytes()).hexdigest()

    job = insert_job(session)
    version = _truthful_version(session, resume, job)
    result = tailor_svc.export_version(session, version, settings=settings)

    after = hashlib.sha256(original.read_bytes()).hexdigest()
    assert after == before, f"导出把原简历改了！\n{before}\n{after}"
    assert result.path != original, "导出写到了原文件路径上"
    assert result.path.is_file(), "导出没有产生新文件"
    assert result.original_hash_before == result.original_hash_after == before, (
        "导出结果里记录的 hash 前后不一致"
    )


def test_export_refuses_to_overwrite_an_existing_file(session, settings, tmp_path) -> None:
    """★ 依据：交付声明「`--out` 已存在必须拒绝」+ 设计文档「只写新文件」的纪律。

    不覆盖是**硬保证**（与 🟡 #38 的`jh today`导出不同 —— 那里只是自动改名）。
    """
    resume = _real_resume(session, tmp_path)
    job = insert_job(session)
    version = _truthful_version(session, resume, job)

    target = settings.paths.output_dir / "已存在.docx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"already here")
    before = target.read_bytes()

    with pytest.raises(tailor_svc.TailorError):
        tailor_svc.export_version(session, version, settings=settings, out=target)

    assert target.read_bytes() == before, "拒绝覆盖之后文件内容变了"


# ===========================================================================
# ④ brief：零成本路径 + 沾边
# ===========================================================================
def test_brief_is_zero_cost_and_on_topic(session, settings) -> None:
    """★★ 依据：设计文档【提示词 7】「**第一步（先做这个，轻量）**`jh tailor brief <job_id>`
    → 只输出'重点讲哪 3 个项目、突出哪 2 个技能、要避开什么'，不做改写」
    + 交付声明「`brief` **不调模型**」。

    "零成本"怎么证伪：`build_brief` 的签名里**没有 provider 参数**
    （对比 `analyze`/`analyze_and_save` 都有），所以它在结构上不可能调用模型；
    再叠加"没配 key 也必须出结论"的行为断言。
    """
    import inspect

    params = inspect.signature(tailor_svc.build_brief).parameters
    assert "provider" not in params, (
        f"brief 居然接受 provider（那它就可能调模型，不再是零成本路径）：{list(params)}"
    )

    insert_resume(session)
    job = insert_job(
        session,
        build_detail(jd_text="岗位要求：熟悉 Python、FastAPI 与 Kubernetes，负责后端开发。"),
    )
    score_job(session, job, settings)
    session.flush()

    brief = tailor_svc.build_brief(session, job, settings=settings)
    assert brief.points, "brief 一个'重点讲的项目'都没给出来"
    assert len(brief.points) <= settings.tailor.brief_projects
    assert brief.highlight, "brief 没有给出'突出哪几个技能'"
    assert len(brief.highlight) <= settings.tailor.brief_skills

    # 沾边：brief 里出现的技能必须来自 JD ∩ 简历，不能凭空来
    pool = resume_svc.resume_skill_pool(
        resume_svc.content_from_json(
            session.get(Resume, brief.resume_id).content_json  # type: ignore[union-attr]
        )
    )[0]
    for token in brief.highlight:
        assert token in pool, f"brief 突出了简历里没有的技能：{token}"
    assert "Kubernetes" not in brief.highlight, "brief 突出了简历里没有的技能（Kubernetes）"


# ===========================================================================
# ⑤ 提示词纪律：改了正文不改版本号 → 必须能被发现
# ===========================================================================
def test_prompt_fingerprint_guard_is_armed(tmp_path) -> None:
    """★★ 依据：设计文档 + 评审采纳项 #3 —— 「改提示词必须 +1 版本号，
    否则缓存不失效是**静默失效**」。开发会话的机制是：
    `tests/unit/test_tailor_svc.py::PROMPT_BODY_SHA256`（版本 → 正文摘要），
    正文变了而版本没变时那条测试会红。

    **我不复用他们的测试当证据（那是自证）**，而是自己验两件事：
      ① 我**自己算**真实文件的正文 sha256，看它是否等于他们登记的值
         —— 这证明"守卫真的上膛了"（登记值没写错/没漂移）；
      ② 把正文改一个字后，版本号不变、摘要必变
         —— 这证明"正文一改，那条守卫一定会响"。
    """
    real = tailor_svc._prompt_path(get_settings())
    prompt = tailor_svc.load_prompt(get_settings())

    # ① 自己算摘要，与登记值对比（守卫上膛）
    from tests.unit.test_tailor_svc import PROMPT_BODY_SHA256

    my_digest = hashlib.sha256(prompt.body.encode("utf-8")).hexdigest()
    registered = PROMPT_BODY_SHA256.get(prompt.version)
    assert registered is not None, (
        f"提示词版本 v{prompt.version} 没有登记正文摘要 —— 指纹守卫形同虚设\n"
        f"（当前正文 sha256 = {my_digest}）"
    )
    assert my_digest == registered, (
        "登记的正文摘要与真实文件不一致 —— 守卫已经失效（正文改了但摘要是旧的）：\n"
        f"  我算的 = {my_digest}\n  登记的 = {registered}"
    )

    # ② 正文一改，摘要必变（守卫会响）
    body = real.read_text(encoding="utf-8")
    tampered = body.replace("七要素", "七要素（改过）", 1)
    if tampered == body:
        tampered = body + "\n<!-- 偷偷加一句 -->\n"
    fake = tmp_path / "tailor.md"
    fake.write_text(tampered, encoding="utf-8")
    other = tailor_svc.load_prompt(get_settings(), path=fake)

    assert other.version == prompt.version, "测试自身问题：版本号应当没变（这正是要防的场景）"
    assert hashlib.sha256(other.body.encode("utf-8")).hexdigest() != my_digest, (
        "改了提示词正文，摘要却没变 —— 那指纹守卫根本发现不了"
    )
    assert other.body != prompt.body, "改了正文却没生效？"


def test_prompt_keeps_the_declared_version_consistent_with_its_body() -> None:
    """正向：真实提示词文件的版本号与正文**当前是自洽的**，且提示词满足 JSON 模式三件套。

    依据：交付声明「提示词在 prompts/tailor.md 七要素」+ `llm/base.py` 的
    「① 提示词里必须出现 "json" 字样（否则 JSON 模式不生效，会返回散文）」。
    """
    prompt = tailor_svc.load_prompt(get_settings())
    assert prompt.version >= 1
    assert prompt.body.strip(), "提示词正文是空的"
    lowered = prompt.body.lower()
    for marker in ("json", "suggestion"):
        assert marker in lowered, f"提示词里缺少 {marker!r}（JSON 模式三件套之一）"

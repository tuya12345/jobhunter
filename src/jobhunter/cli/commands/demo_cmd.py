"""`jh demo` —— 假数据全流程演示（P9）。

    jh demo            # 用临时库跑一遍完整流程，退出即销毁
    jh demo --keep     # 保留临时库与产物，便于自己翻着看

★★ 三条纪律（每一条都对应一类真实会发生的坏结果）

    ① **绝不碰真库**。demo 一旦写进 `data/jobhunter.db`，真实投递记录就被假数据混进来了，
       "统计转化率"这类功能立刻失去意义 —— 而且很难发现是哪来的。
       做法：把 `JH_DATA_DIR/OUTPUT_DIR/STATE_DIR` 指到临时目录，**并断言**
       解析出来的库路径确实变了、且落在临时目录里（不是靠"我记得设了环境变量"）。
    ② **绝不联网、绝不花钱**。用 `MockProvider`（它就是为这个场景写的）：
       别人 clone 下来直接 `jh demo` 就能看到全部能力，不需要 Key、不产生费用。
       演示里的开场白/预测题是**假 Provider 的预设响应**，输出里会明确标出来。
    ③ **输出本身是自证**：每一步都打印"做了什么 + 关键数字"，观众看得懂，
       我自己也能一眼看出哪一步坏了（这也是开源项目的门面）。

★ 为什么不 shell 出去一条条跑 `jh` 命令：那需要两套配置解析，而环境变量的隔离只能靠
  "传参给子进程" —— P5 正是这么踩过一次（父进程仍解到真库，往真实库里插了两行假数据）。
  在进程内直接调 service 层，隔离点只有一个，反而更安全。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.panel import Panel

from jobhunter.cli.common import console
from jobhunter.config.settings import AppSettings, get_settings, reset_settings
from jobhunter.llm.mock import MockProvider
from jobhunter.models import Application, Job, Resume
from jobhunter.services import (
    apply_svc,
    assessment_svc,
    followup_svc,
    interview_svc,
    match_svc,
    offer_svc,
    question_svc,
    report_svc,
    resume_svc,
    stats_svc,
    tailor_svc,
    today_svc,
    track_svc,
    weak_svc,
)
from jobhunter.services.resume_svc import EducationItem, InternshipItem, ProjectItem, ResumeContent
from jobhunter.storage.db import create_db_engine, make_session_factory
from jobhunter.storage.migrate import upgrade_head
from jobhunter.utils.time import add_days, now_utc

app = typer.Typer(
    help="演示：用假数据 + 独立库跑通全流程（绝不污染真库，P9）",
    # ★ 单命令应用用 callback（原因见 gap_cmd.py 的说明）
    invoke_without_command=True,
    no_args_is_help=False,
)

# ===========================================================================
# 假数据（**全部虚构**：手机号是占位段、邮箱用 example.com、公司名是"甲乙丙…"）
# ===========================================================================
DEMO_RESUME_LINES = [
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

#: 6 个假岗位：覆盖"很匹配 / 一般 / 不匹配 / 实习 / 全职 / 前端 / 其他语言"几种情况，
#: 这样 `jh match run`、`jh gap`、`jh today`、`jh stats` 都能看到有意义的差异。
DEMO_JOBS: list[dict[str, Any]] = [
    {
        "company": "甲公司",
        "title": "Python 后端开发实习生",
        "city": "深圳",
        "is_intern": True,
        "jd_text": (
            "岗位职责：负责后端接口开发与维护，参与订单系统迭代。\n"
            "任职要求：熟悉 Python、FastAPI、MySQL；了解 Redis 缓存；有 Kubernetes 经验优先。"
        ),
        "keywords": ["Python", "FastAPI", "MySQL", "Redis", "Kubernetes"],
        "direction": "Python 开发",
    },
    {
        "company": "乙公司",
        "title": "后端开发工程师（校招）",
        "city": "深圳",
        "is_intern": False,
        "jd_text": "要求熟悉 Python/Go、MySQL、Redis，了解 Docker；有分布式经验优先。",
        "keywords": ["Python", "Go", "MySQL", "Redis", "Docker"],
        "direction": "Python 开发",
    },
    {
        "company": "丙公司",
        "title": "大模型应用开发实习生",
        "city": "广州",
        "is_intern": True,
        "jd_text": "参与大模型应用开发：RAG 检索增强、LangChain 编排、向量数据库；熟悉 Python。",
        "keywords": ["Python", "RAG", "LangChain", "向量数据库"],
        "direction": "AI Agent",
    },
    {
        "company": "丁公司",
        "title": "前端开发实习生",
        "city": "深圳",
        "is_intern": True,
        "jd_text": "负责管理后台前端开发：Vue3、TypeScript、Element Plus；了解 Node.js。",
        "keywords": ["Vue", "TypeScript", "Node.js", "前端工程化"],
        "direction": "前端",
    },
    {
        "company": "戊公司",
        "title": "Java 后端开发（3-5 年）",
        "city": "东莞",
        "is_intern": False,
        "jd_text": "3-5 年经验，精通 Java、Spring Boot、Kubernetes、微服务架构。",
        "keywords": ["Java", "Spring Boot", "Kubernetes", "微服务"],
        "direction": "Java 开发",
    },
    {
        "company": "己公司",
        "title": "测试开发实习生",
        "city": "深圳",
        "is_intern": True,
        "jd_text": "负责接口自动化测试：Python、pytest、单元测试；了解 CI/CD。",
        "keywords": ["Python", "pytest", "单元测试", "CI/CD"],
        "direction": "测试开发",
    },
]

#: 假 Provider 的预设响应：**仍然要过真实闸门**，所以文案是照着约束写的
#: （开场白 80-150 字、含 JD 关键词、结尾问句；预测题是对象数组；追问 ≤160 字）。
_MOCK_RESPONSES: dict[str, str] = {
    "greeting": (
        '{"full": "您好，我是 Python 后端方向的应届生。看到岗位要求 Python 与 FastAPI，'
        "我用 Python + FastAPI 写过订单管理系统的 3 个模块接口，并用 Redis 把接口平均耗时"
        '从 500ms 降到 80ms。想请教一下这个岗位目前最看重哪一块？", '
        '"short": "Python 后端方向，FastAPI + Redis 都有项目经验，方便聊聊吗？"}'
    ),
    "question_predict": (
        '{"questions": ['
        '{"question": "你说用 Redis 把接口耗时从 500ms 降到 80ms，缓存穿透和雪崩当时怎么处理的？",'
        ' "category": "Redis 缓存", "why": "顺着简历里的性能数字追问原理"},'
        '{"question": "订单管理系统那 3 个模块是怎么划分的？接口职责边界怎么定的？",'
        ' "category": "项目细节", "why": "从最有信息量的一句开始验证真实性"},'
        '{"question": "JD 提到 Kubernetes，你了解过吗？如果让你学，你会怎么入手？",'
        ' "category": "Kubernetes", "why": "缺的技能用「了解过吗」试探学习能力"}]}'
    ),
    "followup": (
        '{"text": "您好，我几天前投递了 Python 后端开发实习生岗位。我做过订单管理系统的'
        "后端接口开发，主要用 Python + FastAPI，也用 Redis 做过缓存优化。想请问这个岗位"
        '目前还在推进吗？需要补充材料的话随时告诉我，谢谢！"}'
    ),
}

#: 演示要还原的环境变量（`_restore` 会清掉它们，免得同进程后续命令继续用演示库）
_ENV_KEYS = ("JH_DATA_DIR", "JH_OUTPUT_DIR", "JH_STATE_DIR", "JH_LLM_PROVIDER")


def _build_content() -> ResumeContent:
    """构造演示用的假简历（结构完整，便于展示解析/匹配/定制）。"""
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
                bullets=[
                    "用 Python + FastAPI 写了 3 个模块的接口",
                    "用 Redis 做缓存，接口平均耗时从 500ms 降到 80ms",
                ],
                skills=["Python", "FastAPI", "Redis"],
            )
        ],
        internships=[
            InternshipItem(
                company="某某科技有限公司",
                role="后端实习生",
                period="2025.07-2025.09",
                bullets=["参与订单服务重构"],
                skills=["Python"],
            )
        ],
        raw_text="\n".join(DEMO_RESUME_LINES),
        sections={
            "header": [DEMO_RESUME_LINES[0]],
            "education": [DEMO_RESUME_LINES[2]],
            "skills": [DEMO_RESUME_LINES[4]],
            "projects": [DEMO_RESUME_LINES[6], DEMO_RESUME_LINES[7], DEMO_RESUME_LINES[8]],
            "internships": [DEMO_RESUME_LINES[10], DEMO_RESUME_LINES[11]],
        },
    )


@app.callback(invoke_without_command=True)
def demo(
    keep: Annotated[bool, typer.Option("--keep", help="保留演示库与产物（默认用完即删）")] = False,
) -> None:
    """跑一遍完整流程演示（P9）。"""
    # ★ 取"当前配置的库"之前先重建配置单例：调用方（尤其测试）可能刚设过
    #   JH_DATA_DIR 却还没清缓存，那样取到的是旧值，会让后面的隔离断言误判。
    reset_settings()
    real_url = get_settings().database_url
    scratch = Path(tempfile.mkdtemp(prefix="jh-demo-"))
    for name in ("data", "output", "state"):
        (scratch / name).mkdir()

    console.print()
    console.print(
        Panel(
            "全部数据都是**假的**（虚构简历 + 6 个虚构岗位），库建在临时目录，退出即销毁。\n"
            "演示**不联网、不花钱**：模型部分用假 Provider，"
            "但生成的话术仍然要过真实的保真闸门。",
            title="🎬 JobHunter 演示",
            border_style="cyan",
        )
    )

    try:
        settings = _isolate(scratch, real_url=real_url)
        provider = MockProvider(_MOCK_RESPONSES, jitter=True)
        _seed(settings)
        _step_match(settings)
        _step_today(settings, provider)
        _step_track(settings)
        _step_prepare(settings, provider)
        _step_review(settings)
        _step_export(settings, provider)
    finally:
        _restore()
        if keep:
            console.print()
            console.print(f"[dim]演示数据保留在：{scratch}[/dim]")
        else:
            shutil.rmtree(scratch, ignore_errors=True)
            # ★ 把"删掉的到底是哪个目录"打出来：既让用户确认没碰别的东西，
            #   也让测试可以**针对这个路径**断言"它确实没了" ——
            #   而不是去扫系统临时目录里有没有 `jh-demo-*` 残留
            #   （那种全局扫描在多个进程同时跑 demo 时会互相误伤，实测踩到过）。
            console.print(f"[dim]演示临时目录已清理：{scratch}[/dim]")


# ===========================================================================
# 隔离（本模块最重要的十几行）
# ===========================================================================
def _isolate(scratch: Path, *, real_url: str) -> AppSettings:
    """把数据目录指到临时目录，并**断言**真的切过去了。

    ★ 为什么必须断言而不是"设完环境变量就放心"：
      P5 真发生过一次「脚本只给子进程传了 env，父进程仍解到真库，于是往真实库里
      插了两行假数据」。这类事故的共同点是：**看起来一切正常**。
      所以这里用两道断言兜住（真库 URL 必须变了 + 临时目录必须出现在 URL 里）。
    """
    # ★ 先记下用户可能已经设过的值，退出时**原样放回去**（不是简单 pop）：
    #   如果用户平时就靠 `JH_DATA_DIR` 把库放在加密盘，pop 会把他的配置弄丢，
    #   下一次 get_settings() 解到默认路径 —— 实测就是这条让 `jh demo` 结束时
    #   报"还原失败：配置单例还指着演示库"（install.md 的第 11 条 FAQ）。
    _SNAPSHOT.original = {name: os.environ.get(name) for name in _ENV_KEYS}
    _SNAPSHOT.scratch = scratch
    os.environ["JH_DATA_DIR"] = str(scratch / "data")
    os.environ["JH_OUTPUT_DIR"] = str(scratch / "output")
    os.environ["JH_STATE_DIR"] = str(scratch / "state")
    os.environ["JH_LLM_PROVIDER"] = "mock"
    reset_settings()

    settings = get_settings()
    if settings.database_url == real_url or scratch.as_posix() not in settings.database_url:
        # ★ 隔离失败时**先把环境变量还原再抛**：否则进程会停在"指向临时目录"的状态，
        #   同进程里后续命令（以及测试里的下一条用例）会继续用那个库 ——
        #   实测踩过：一条"隔离失败"的用例把 JH_LLM_PROVIDER=mock 留在了环境里，
        #   导致后面"没配 key"的用例拿到假 Provider 而失败。
        _restore()
        raise RuntimeError(
            f"演示库的路径不对：{settings.database_url}\n"
            f"（真实库是 {real_url}）—— 继续跑会把假数据写进真实投递记录，已中止。"
        )
    settings.paths.ensure_all()
    upgrade_head(settings.database_url)
    console.print(f"① 建库：{settings.database_url}")
    console.print("   [dim]（临时目录，退出即销毁；真实库一个字节都没动）[/dim]")
    return settings


@dataclass
class _Snapshot:
    """进入 demo 前的环境现场（原值 + 临时目录），`_restore` 用它收尾。

    ★ 用模块级容器而不是给 `_isolate`/`_restore` 都加参数：两个函数会被 CLI 与测试
      分别调用，"原值"必须跨这两个调用存在。用一个类装起来也顺便让"忘了收尾"这件事
      变得可见（`original` 空字典就意味着没进过 `_isolate`）。
    """

    original: dict[str, str | None] = field(default_factory=dict)
    scratch: Path | None = None


#: 当前这次 demo 的环境现场（没在跑 demo 时是空的）
_SNAPSHOT = _Snapshot()


def _restore() -> None:
    """还原环境变量与配置单例（否则同进程里后续命令会继续用演示库）。

    ★ 还原成**用户原来的值**（可能是"没设"，也可能是他自己设的路径），
      而不是一律删掉 —— 见 `_isolate` 里的说明。

    ★★ 这里**只断言一件事：不再指着演示库**（P9 验收 #53 的教训）。
      第一版断言的是"还原后的库 URL 必须等于进 demo 前那个 URL" —— 那个比较
      **假定了环境在 demo 期间不会变**，于是"调用方在进 demo 前刚改过环境变量、
      但配置单例还是旧的"这种情况下会误报：
        进 demo 时 real_url 解到 A（旧缓存）→ demo 期间换到临时目录 →
        还原时环境变量已指向 B（调用方刚设的）→ A ≠ B → 抛异常。
      而这时"用户的环境配置"其实**已经正确还原**了。
      正确的断言是**目标属性**：`get_settings()` 解析出来的库不能再落在演示临时目录里。
    """
    scratch = _SNAPSHOT.scratch
    for name in _ENV_KEYS:
        original = _SNAPSHOT.original.get(name)
        if original is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = original
    _SNAPSHOT.original = {}
    _SNAPSHOT.scratch = None
    reset_settings()

    url = get_settings().database_url
    # ★ 只比对**演示库自己的目录**（`<scratch>/data`）而不是整个 scratch 前缀：
    #   前缀匹配会在"临时目录恰好是数据目录的祖先"时误报（测试里就踩到过：
    #   scratch 与测试自己的 data 目录同在一个 tmp_path 下）。
    demo_marker = (scratch / "data").as_posix() if scratch is not None else None
    if demo_marker and demo_marker in url:  # pragma: no cover - 只可能是实现写错
        raise RuntimeError(
            f"还原失败：配置单例还指着演示库（{url}）\n"
            "这是 demo 的隔离逻辑写错了，请把上面这条 URL 发我。"
        )


def _session(settings: AppSettings):
    engine = create_db_engine(settings.database_url, ensure_directory=False)
    return engine, make_session_factory(engine)


# ===========================================================================
# 各步骤（每一步都打印"做了什么 + 关键数字"）
# ===========================================================================
def _seed(settings: AppSettings) -> None:
    """灌入假简历 + 6 个假岗位。"""
    engine, factory = _session(settings)
    try:
        with factory() as session:
            content = _build_content()
            session.add(
                Resume(
                    name="主简历（演示）",
                    direction="Python 后端开发",
                    content_json=content.model_dump_json(),
                    content_hash=resume_svc.compute_content_hash(content),
                    is_default=True,
                )
            )
            for index, item in enumerate(DEMO_JOBS, start=1):
                session.add(
                    Job(
                        platform="manual",
                        company=str(item["company"]),
                        title=str(item["title"]),
                        city=str(item["city"]),
                        is_intern=bool(item["is_intern"]),
                        jd_text=str(item["jd_text"]),
                        jd_hash=f"demo{index}".ljust(64, "0"),
                        url=f"https://example.com/job/{index}",
                        status="new",
                        jd_keywords_json=json.dumps(item["keywords"], ensure_ascii=False),
                        # 打分时才会写这一列；演示里先塞好，"方向回音"统计才有数据
                        rule_detail_json=json.dumps(
                            {"jd_direction": item["direction"]}, ensure_ascii=False
                        ),
                    )
                )
            session.commit()
    finally:
        engine.dispose()
    console.print(f"② 录入：1 份假简历 + {len(DEMO_JOBS)} 个假岗位")
    for item in DEMO_JOBS:
        console.print(f"   [dim]· {item['company']} · {item['title']}[/dim]")


def _step_match(settings: AppSettings) -> None:
    """跑规则层匹配（**纯本地计算**，不联网），并把分数写回岗位。"""
    engine, factory = _session(settings)
    try:
        with factory() as session:
            candidates = match_svc.load_resume_candidates(session, settings)
            rows: list[tuple[Job, Any]] = []
            for job in session.query(Job).order_by(Job.id):
                result = match_svc.match_best_candidate(
                    job.jd_text, candidates, job=job, settings=settings
                )
                if result is None:
                    continue
                match_svc.apply_result_to_job(job, result)
                rows.append((job, result))
            session.commit()

            console.print()
            console.print("③ 两级匹配（规则层，本地计算，不花钱）")
            for job, result in sorted(rows, key=lambda pair: -pair[1].rule_score):
                console.print(
                    f"   {result.rule_score:>3} 分　{job.company} · {job.title}"
                    f"　[dim]({result.direction or '方向未定'})[/dim]"
                )
            if rows:
                best_job, best = max(rows, key=lambda pair: pair[1].rule_score)
                console.print(
                    f"   [dim]最高分：{best_job.company} · {best_job.title}（{best.rule_score} 分）"
                    f" —— 想看为什么：jh match explain {best_job.id}[/dim]"
                )
    finally:
        engine.dispose()


def _step_today(settings: AppSettings, provider: MockProvider) -> None:
    """出待投清单 + 生成开场白（假 Provider，但仍然过四道闸门）。"""
    engine, factory = _session(settings)
    try:
        with factory() as session:
            result = today_svc.build_today_list(
                session, limit=3, settings=settings, provider=provider
            )
            session.commit()
            console.print()
            console.print(f"④ 今日待投清单（取分数最高的 {len(result.items)} 条）")
            for item in result.items:
                tag = "" if item.greeting_source == "llm" else "（模板兜底）"
                console.print(
                    f"   #{item.index}　{item.score} 分　{item.company} · {item.title}"
                    f"　[dim]{len(item.greeting_full)} 字开场白{tag}[/dim]"
                )
                console.print(f"      [cyan]{item.greeting_full}[/cyan]")
                if item.greeting_short:
                    console.print(f"      [dim]精简版：{item.greeting_short}[/dim]")
                if item.greeting_issues:
                    console.print(f"      [yellow]降级原因：{item.greeting_issues[0]}[/yellow]")
    finally:
        engine.dispose()


def _step_track(settings: AppSettings) -> None:
    """打标跟进：一路到 offer / 约面后被拒 / 只投了（覆盖状态机的两条特殊规则）。"""
    engine, factory = _session(settings)
    try:
        with factory() as session:
            jobs = list(session.query(Job).order_by(Job.id))
            plan = (
                ("applied", "viewed", "replied", "interview", "offer"),
                ("applied", "replied", "interview", "rejected"),
                ("applied", "viewed", "replied"),
                ("applied", "viewed"),
                ("applied",),
                ("applied",),
            )
            for job, statuses in zip(jobs, plan, strict=False):
                for status in statuses:
                    apply_svc.set_status(session, job.id, status)
            session.commit()

            console.print()
            console.print("⑤ 打标跟进（每次状态变更都会写入 append-only 事件流）")
            for job in jobs:
                application = apply_svc.get_latest_application(session, job.id)
                status = application.status if isinstance(application, Application) else "—"
                console.print(f"   {job.company} · {job.title} → [bold]{status}[/bold]")

            timeline = track_svc.build_timeline(session, jobs[0].id)
            console.print(f"   [dim]时间轴示例（{jobs[0].company}）：[/dim]")
            for entry in timeline.entries:
                console.print(f"      [dim]{entry.local_text}　{entry.label}[/dim]")
    finally:
        engine.dispose()


def _step_prepare(settings: AppSettings, provider: MockProvider) -> None:
    """面试准备：约面 → 记 3 道真题（2 道答崩）→ 按 JD 预测题 → 录笔试 → 录 Offer。"""
    engine, factory = _session(settings)
    try:
        with factory() as session:
            target = session.query(Job).order_by(Job.id).first()
            assert target is not None
            interview = interview_svc.add_interview(
                session,
                target.id,
                round_="一面",
                scheduled_at=add_days(now_utc(), 2),
                mode="视频",
                link="https://example.com/meeting/demo",
            )
            for text, rating, category, weak in (
                ("Redis 缓存穿透和雪崩怎么处理？", "bad", "Redis 缓存", True),
                ("FastAPI 的依赖注入你是怎么用的？", "ok", "FastAPI", False),
                ("项目里最难的地方是什么？", "bad", "项目细节", True),
            ):
                question_svc.add_question(
                    session,
                    question=text,
                    interview_id=interview.id,
                    rating=rating,
                    category=category,
                    weak=weak,
                )
            prediction = question_svc.predict_questions(
                session, target, count=3, provider=provider, settings=settings
            )
            assessment_svc.add_assessment(
                session,
                target.id,
                kind="笔试",
                deadline_at=add_days(now_utc(), 2),
                platform="牛客",
            )
            offer = offer_svc.add_offer(
                session,
                target.id,
                salary_text="22-25K",
                salary_month=22000,
                months=15,
                location="深圳",
                deadline_at=add_days(now_utc(), 9),
                pros="技术栈对口",
                cons="通勤 1 小时",
            )
            session.commit()

            console.print()
            console.print("⑥ 面试准备与 Offer")
            console.print(f"   约面：{target.company} · 一面（2 天后）")
            console.print("   记题：3 道（其中 2 道标记为没答上来）")
            console.print(
                f"   按 JD 预测题：{len(prediction.stored)} 道"
                "　[dim]（假 Provider 的预设响应，仍然按 schema 校验后入库）[/dim]"
            )
            for row in prediction.stored:
                console.print(f"      [dim]· [{row.category}] {row.question}[/dim]")
            console.print("   笔试：牛客 · 2 天后截止")
            annual = offer.annual_package
            yearly = f"{annual / 10000:.1f} 万" if annual else "—"
            console.print(f"   Offer：22-25K × 15 个月 = 年包 {yearly}")
    finally:
        engine.dispose()


def _step_review(settings: AppSettings) -> None:
    """复盘：薄弱点 / 转化率 / 定制简历 brief（全是确定性计算）。"""
    engine, factory = _session(settings)
    try:
        with factory() as session:
            weak = weak_svc.build_weak_report(session)
            console.print()
            console.print("⑦ 薄弱点（按「没答上来次数」排序，实录与预测分列）")
            for row in weak.rows:
                console.print(
                    f"   {row.category}　没答上来 {row.weak_count}/{row.total}"
                    f"　[dim]{row.conclusion}[/dim]"
                )
            console.print(f"   [bold]{weak.headline}[/bold]")

            stats = stats_svc.build_stats(session)
            console.print()
            console.print(f"⑧ 转化率（{stats.sample_note}）　[dim]口径：至少走到过这一步[/dim]")
            rates = "　".join(
                f"{stats_svc.RATE_LABELS[key]} "
                f"{stats_svc.format_rate(stats.overall.rate(key), stats.overall.total)}"
                for key in stats_svc.RATE_LABELS
            )
            console.print(f"   投递 {stats.overall.total}　{rates}")
            for direction in stats.directions:
                reply_rate = stats_svc.format_rate(
                    direction.counts.rate("replied"), direction.counts.total
                )
                console.print(
                    f"   [dim]方向「{direction.direction}」："
                    f"投 {direction.counts.total}　回复 {reply_rate}[/dim]"
                )

            job = session.query(Job).order_by(Job.id).first()
            if job is not None:
                brief = tailor_svc.build_brief(session, job, settings=settings)
                console.print()
                console.print(f"⑨ 定制简历 brief（{job.company} · {job.title}，不调模型）")
                for point in brief.points:
                    if point.line:
                        console.print(f"   重点讲：{point.line}")
                console.print(f"   突出技能：{'、'.join(brief.highlight) or '（无）'}")
                for item in brief.avoid[:2]:
                    console.print(f"   [yellow]要避开：{item}[/yellow]")
    finally:
        engine.dispose()


def _step_export(settings: AppSettings, provider: MockProvider) -> None:
    """导出：追问话术 + 周报 Markdown（写进临时 output/，**不会动你的 output/**）。"""
    engine, factory = _session(settings)
    try:
        with factory() as session:
            job = session.query(Job).order_by(Job.id).first()
            assert job is not None
            followup = followup_svc.generate_followup(
                session, job, settings=settings, provider=provider, use_cache=False
            )
            console.print()
            console.print("⑩ 追问话术（只生成，不发送）")
            console.print(f"   [cyan]{followup.text}[/cyan]")
            source = "模型" if followup.source == "llm" else "模板"
            degrade = f"　｜　降级原因：{followup.issues[0]}" if followup.issues else ""
            console.print(f"   [dim]{len(followup.text)} 字　｜　来源：{source}{degrade}[/dim]")

            data = report_svc.build_report(session)
            path = report_svc.export_report(data, settings=settings)
            produced = sorted(p.name for p in settings.paths.output_dir.glob("*"))
            console.print()
            console.print("⑪ 周报与产物")
            console.print(f"   已写出：{path}")
            console.print(f"   临时 output/ 里现在有：{produced}")
    finally:
        engine.dispose()

    console.print()
    console.print(
        Panel(
            "演示结束。真实数据目录（data / output / state）与真实数据库\n"
            "**没有被写入任何东西**。\n\n"
            "自己动手：[green]jh init[/green] → [green]jh resume add[/green] → "
            "[green]jh job add <链接>[/green] → [green]jh match run[/green] → "
            "[green]jh today[/green]",
            title="✅ 演示完成",
            border_style="green",
        )
    )

"""`jh match` —— 两级匹配打分（P3：本项目核心，已实现）。

    jh match run [--top 30] [--threshold 60] [--no-llm] [--all]
        ① 规则层（0 成本）：给**待处理**的岗位打 rule_score，并把
           jd_keywords / rule_detail / matched_resume_id 一起落库；
           ★ 默认**跳过已投 / 已跳过的岗位**（ISSUE #27：给不会再投的岗位
           重算一遍是纯浪费），`--all` 显式要求重算全部；
           这次算了多少条、跳过了多少条会打在屏幕上（不静默少算）。
        ② LLM 层：只对规则分 Top N 走大模型精评（并发 + 边算边写库 + 成本统计）。

    jh match explain <job_id>
        ★ 打印各维度得分明细。**调权重靠它** ——
          计划书评审的原话："否则调权重无法进行"。
          看不到"方向给了 30、硬条件给了 21、技能给了 24"，
          改 config 里的权重就只能是瞎猜。

    jh match set <job_id> --resume <id> --verdict skip|queue
        人工裁决：我的话比模型大。`skip` 直接排除出待投清单。

三维度与默认权重（config.yaml 可调）：
    岗位方向 30% ｜ 经验年限/是否应届 30% ｜ 技能交集 40%
    ★ 硬条件不满足是「大幅降分」而非「直接淘汰」—— 应届生常能投 3 年以下的岗。

★ 本命令**不做任何写库之外的动作**：不发消息、不投递、不碰招聘网站。
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer
from rich.panel import Panel
from rich.table import Table
from sqlalchemy.orm import Session

from jobhunter.cli.common import console, err_console, require_db, success, warn
from jobhunter.config.settings import AppSettings, get_settings
from jobhunter.llm.factory import get_provider
from jobhunter.models import Job, Resume
from jobhunter.services import job_svc, match_llm_svc, match_svc, resume_svc
from jobhunter.utils.io import from_json

app = typer.Typer(name="match", help="匹配打分：跑分 / 明细解释 / 人工裁决（P3）")


@app.command("run")
def match_run(
    top: Annotated[
        int | None,
        typer.Option("--top", help="LLM 精评取规则分前 N 条（默认取 config 的 llm_top_n）"),
    ] = None,
    threshold: Annotated[
        int | None, typer.Option("--threshold", help="分数线（默认取 config，仅用于小结）")
    ] = None,
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="只跑规则层（不花任何钱，秒出）")
    ] = False,
    all_jobs: Annotated[
        bool,
        typer.Option(
            "--all",
            help="连已投 / 已跳过的岗位也重算（默认跳过它们：重算纯属浪费钱）",
        ),
    ] = False,
) -> None:
    """跑两级匹配打分：规则层全量 → LLM 层只精评 Top N。"""
    engine, factory = require_db()
    settings = get_settings()
    top_n = settings.llm_top_n if top is None else top
    line = settings.threshold if threshold is None else threshold

    try:
        with factory() as session:
            candidates = match_svc.load_resume_candidates(session, settings)
            if not candidates:
                _no_resume_hint()
                raise typer.Exit(code=1)

            # ★★ ISSUE #27：默认只算「还没处理过」的岗位 —— 已经投出去 / 我主动跳过的
            #    再花钱重算一遍是纯浪费，还会让 `jh stats` 的成本看起来更高。
            #    `--all` 是显式出口（"我想重算全部"是用户对自己数据的正当要求）。
            #    取数口径与"跳过几条"的分档说明都在 `match_svc.select_scoring_jobs`，
            #    CLI 不自己写一套筛选（否则两个入口的口径迟早分叉）。
            scope = _resolve_scope(session, include_all=all_jobs)

            # ---- ① 规则层：全量打分 -------------------------------------
            scored, no_jd = _run_rule_layer(session, scope.jobs, candidates, settings)
            session.commit()

            # ★ 算了多少条、跳过多少条 —— **必须打出来**（不静默少算）。
            #   注意两种「跳过」不能混成一个数字：这里是"已处理过所以没算"，
            #   低于分数线是排名表下面那句的另一件事（口径见 ISSUE #27 裁决）。
            _print_scope(scope, include_all=all_jobs)

            if no_jd:
                warn(f"有 {len(no_jd)} 个岗位没有 JD 正文，规则层只能按已有字段打分：")
                for item in no_jd[:5]:
                    console.print(f"  [dim]· id={item.id} {item.title or '（无标题）'}[/dim]")

            console.print()
            _print_ranking(scored, line, title="规则层结果")

            # ---- ② LLM 层：只精评 Top N ---------------------------------
            if no_llm:
                console.print()
                console.print("[dim]--no-llm：跳过 LLM 精评（本次不花钱）[/dim]")
                _print_next_steps()
                return

            ordered = sorted(scored, key=lambda item: (-(item.rule_score or 0), item.id))
            selected = ordered[: max(0, top_n)]
            if not selected:
                console.print()
                warn(f"llm_top_n={top_n}，没有岗位需要精评")
                _print_next_steps()
                return

            provider = get_provider(settings)
            console.print()
            console.print(
                f"[bold]LLM 精评[/bold]（规则分前 {len(selected)} 条"
                f"，模型 {provider.model}，并发 {settings.llm_concurrency}）"
            )
            stats = asyncio.run(
                match_llm_svc.run_llm_layer(
                    session,
                    selected,
                    candidates,
                    provider=provider,
                    settings=settings,
                    on_progress=_progress_printer(len(selected)),
                )
            )
            session.commit()

            if stats.skipped:
                console.print()
                warn(stats.errors[0] if stats.errors else "LLM 层已跳过")
                console.print(
                    "  [dim]规则分已经算好并落库，`jh match explain <id>` 照样能看到明细。"
                    "要开精评：在项目根的 .env 里填 DEEPSEEK_API_KEY[/dim]"
                )
            else:
                _print_llm_summary(stats, line)
            _print_next_steps()
    finally:
        engine.dispose()


def _resolve_scope(session: Session, *, include_all: bool) -> match_svc.ScoringScope:
    """取本次要打分的岗位；一条都算不了时**说清原因**并退出（ISSUE #27）。

    ★ 为什么单独抽出来：`match_run` 本身的决策点已经够多（top/threshold/no-llm/
      LLM 降级…），这段"选不出来"的三种结局塞进去只会让主流程更难读，
      也会撞上 ruff 的语句数上限（PLR0915）。
    """
    scope = match_svc.select_scoring_jobs(session, include_all=include_all)
    if scope.jobs:
        return scope
    if scope.skipped:
        # 库里有岗位、但全都被处理过 —— 这**不是**"库里没岗位"，
        # 报错时必须说清是哪种，否则用户会去 `jh job add` 一个已经存在的岗位。
        console.print(
            f"[yellow]库里 {scope.total} 个岗位都已经处理过了"
            f"（{scope.reason_text()}），默认不再重算。[/yellow]"
        )
        console.print(
            "  [dim]要连它们一起重算：[green]jh match run --all[/green]"
            "（重算会重新花 LLM 的钱）[/dim]"
        )
        raise typer.Exit(code=1)
    console.print("[yellow]库里还没有岗位。[/yellow]先录一个：")
    console.print("  [green]jh job add <岗位链接>[/green]")
    raise typer.Exit(code=1)


def _run_rule_layer(
    session: Session,
    jobs: list[Job],
    candidates: list[match_svc.ResumeCandidate],
    settings: AppSettings,
) -> tuple[list[Job], list[Job]]:
    """给**传进来的这些**岗位算规则分并落库，返回 (打过分的岗位, 没有 JD 的岗位)。

    ★ 注意"全量"的含义变了（ISSUE #27）：以前传的就是库里全部岗位，
      现在是 `select_scoring_jobs()` 筛过的那一批（默认不含已投/已跳过）。
      筛选口径只在那一个地方定义，这里不做二次判断。
    """
    scored: list[Job] = []
    no_jd: list[Job] = []
    # 词表与方向画像**只加载一次**：几十个岗位各自重复加载会让命令明显变慢，
    # 而且重复加载还有"读到文件改动前后两个版本"的风险（结果不可复现）。
    vocabulary = resume_svc.load_skill_vocabulary(settings)
    profiles = resume_svc.load_direction_profiles(settings)
    for job in jobs:
        if not job.jd_text:
            no_jd.append(job)
        result = match_svc.match_best_candidate(
            job.jd_text,
            candidates,
            job=job,
            settings=settings,
            profiles=profiles,
            vocabulary=vocabulary,
        )
        if result is None:
            continue
        match_svc.apply_result_to_job(job, result)
        scored.append(job)
    session.flush()
    return scored, no_jd


def _print_scope(scope: match_svc.ScoringScope, *, include_all: bool) -> None:
    """把「这次算了多少条 / 跳过多少条」写清楚（ISSUE #27 的硬要求）。

    ★ 为什么值得单独一行：原来的行为是**静默地**给所有岗位重算 ——
      用户看到的只有一张表，既不知道算了多少条，也不知道有 8 条已投的岗位
      刚刚又花了一次钱。现在少算了就必须说明白为什么少。

    ★ 为什么跳过原因要分档（已投 / 已跳过 / 有投递记录但状态没同步）：
      "跳过 3 条"这种数字没法自解释，用户会怀疑是不是漏算了。
      分档之后他能一眼对上自己做过的事。

    ★ 与「低于分数线」的关系：那句在排名表下面（`_print_ranking`），
      两者是**不同**的过滤，所以这里绝不把它们加在一起说。
    """
    console.print()
    if include_all:
        console.print(
            f"  [dim]--all：本次重算全部 [bold]{len(scope.jobs)}[/bold] 个岗位"
            "（含已投 / 已跳过 —— 分数会被覆盖成这次算的）[/dim]"
        )
        return

    line = f"  [dim]本次打分 [bold]{len(scope.jobs)}[/bold] 条"
    if scope.skipped:
        line += (
            f"，跳过 [bold]{scope.skipped_count}[/bold] 条已处理过的（{scope.reason_text()}）"
            "—— 跳过 ≠ 没分：它们的分数还是上次算的那个[/dim]"
        )
        line += "\n  [dim]↳ 要连它们一起重算：[green]jh match run --all[/green][/dim]"
    else:
        line += "（库里没有已投 / 已跳过的岗位，全都算了）[/dim]"
    console.print(line)


def _print_ranking(jobs: list[Job], threshold: int, *, title: str) -> None:
    """按分数打印排名表（含三个维度的分数，一眼看出分是从哪来的）。"""
    ordered = sorted(jobs, key=lambda item: (-(item.match_score or 0), item.id))
    table = Table(title=f"{title}（共 {len(ordered)} 条）", header_style="bold cyan")
    table.add_column("id", justify="right")
    table.add_column("分", justify="right")
    table.add_column("规则分", justify="right")
    table.add_column("方向", justify="right")
    table.add_column("硬条件", justify="right")
    table.add_column("技能", justify="right")
    table.add_column("公司", overflow="fold")
    table.add_column("职位", overflow="fold")
    table.add_column("城市")

    over = 0
    for job in ordered:
        parts = match_svc.rule_detail_summary(job)
        score = job.match_score or 0
        if score >= threshold:
            over += 1
        style = "bold green" if score >= threshold else "dim"
        table.add_row(
            str(job.id),
            f"[{style}]{score}[/{style}]",
            str(job.rule_score if job.rule_score is not None else "—"),
            f"{parts.get('direction', 0):.0f}",
            f"{parts.get('hard', 0):.0f}",
            f"{parts.get('skill', 0):.0f}",
            job.company or "—",
            job.title or "—",
            job.city or "—",
        )
    console.print(table)
    console.print(
        f"  [dim]分数线 {threshold}：有 [bold]{over}[/bold] 条达标（`jh today` 会按它筛，P4）[/dim]"
    )


def _progress_printer(total: int) -> object:
    """返回一个"每完成一条打印一个点"的回调（不刷新整屏，日志友好）。"""

    def _on_progress(done: int, _total: int, outcome: match_llm_svc._Outcome) -> None:
        mark = "·" if outcome.verdict else "✗"
        color = "dim" if outcome.from_cache else ""
        text = f"[{color}]{mark}[/{color}]" if color else mark
        console.print(text, end="")
        if done >= total:
            console.print()

    return _on_progress


def _print_llm_summary(stats: match_llm_svc.LLMRunStats, threshold: int) -> None:
    """打印 LLM 层的用量与成本（**每次都打**：花了钱就要让用户看到）。

    ★★ ISSUE #26：**"本次花了"与"命中缓存省下"必须分开显示**

      老行为把命中缓存那一行（历史值）的 cost 加进"本次成本"，
      于是同一份缓存命中 10 次，这里就报 10 倍的钱 —— 用户会以为
      这工具很贵，而实际上它很便宜。那种错**不报错、数字看着也合理**，
      所以修法不只是改累加，还要让两笔账在屏幕上各占一行：
      这次真的花了多少、以及因为缓存少花了多少（省下的口径与
      `jh cache stats` 是同一个乘积，见 `services/cache_stats_svc.py`）。
    """
    console.print()
    pieces = [f"调用 {stats.calls} 次"]
    if stats.cached:
        pieces.append(f"[cyan]命中缓存 {stats.cached} 条（0 成本）[/cyan]")
    if stats.degraded:
        pieces.append(f"[yellow]降级 {stats.degraded} 条（用规则分）[/yellow]")
    if stats.tokens_in or stats.tokens_out:
        pieces.append(f"tokens {stats.tokens_in}→{stats.tokens_out}")
    if stats.cost:
        pieces.append(f"本次约 ¥{stats.cost:.4f}")
    success("LLM 精评完成：" + "　".join(pieces))

    saved = stats.saved_stats
    if saved.hits:
        line = (
            f"  命中缓存省下约 ¥{saved.saved_cost:.4f}（{saved.hits} 条结果直接复用，没有重复付费）"
        )
        if saved.unknown_cost_hits:
            line += f"　[dim]（其中 {saved.unknown_cost_hits} 条没记成本，未计入）[/dim]"
        console.print(line)
        console.print("  [dim]「本次花了」与「省下」是两笔账，别相加；细节见 jh cache stats[/dim]")

    if stats.errors:
        console.print("  [dim]降级原因（前几条）：[/dim]")
        for item in stats.errors[:3]:
            console.print(f"    [dim]· {item}[/dim]")
    console.print(
        "  [dim]★ 缓存 key 含「简历内容 hash」，所以你**改了简历再跑，分会自动重算**[/dim]"
    )


def _print_next_steps() -> None:
    console.print()
    console.print(
        "下一步：[green]jh match explain <id>[/green] 看某个岗位的分维度明细"
        "　｜　[green]jh gap[/green] 看我该补什么技能"
    )


def _no_resume_hint() -> None:
    err_console.print(
        Panel(
            "匹配需要一份简历，但库里一份都没有。\n\n"
            "  [green]jh resume add <你的简历.docx>[/green]\n\n"
            "[dim]没有简历就没法算「我缺什么」，这是匹配的前提。[/dim]",
            title="缺少简历",
            border_style="yellow",
        )
    )


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------
@app.command("explain")
def match_explain(
    job_id: Annotated[int, typer.Argument(help="岗位 ID")],
) -> None:
    """打印某个岗位的分维度得分明细（**调权重靠它**）。"""
    engine, factory = require_db()
    try:
        with factory() as session:
            job = job_svc.get_job(session, job_id)
            if job is None:
                err_console.print(f"[red]找不到岗位 id={job_id}[/red]")
                raise typer.Exit(code=1)
            _print_explain(job, session)
    finally:
        engine.dispose()


def _print_explain(job: Job, session: Session) -> None:
    """渲染一个岗位的完整打分解释。"""
    _print_explain_header(job)

    # ---- 分维度明细：直接来自落库的 rule_detail_json（与打分时同源）----
    console.print()
    for line in match_svc.explain_lines(job):
        if line.startswith("    "):
            console.print(f"[dim]{line.strip()}[/dim]")
        else:
            console.print(line)

    _print_hard_checks(job)
    _print_llm_reason(job)

    # ---- 推荐简历 ----
    if job.matched_resume_id is not None:
        row = session.get(Resume, job.matched_resume_id)
        if row is not None:
            console.print(f"  [dim]推荐简历：{row.name}（id={row.id}）[/dim]")


def _print_explain_header(job: Job) -> None:
    """表头：这是哪个岗位、现在是什么状态、三个分数分别是多少。"""
    console.print()
    console.print(
        f"[bold]#{job.id}　{job.company or '（未写公司）'}　{job.title or '（未写职位）'}[/bold]"
    )

    def _num(value: int | None) -> str:
        return "—" if value is None else str(value)

    console.print(
        f"  [dim]城市 {job.city or '—'}｜经验 {job.experience or '—'}"
        f"｜学历 {job.education or '—'}｜状态 {job.status}｜"
        f"匹配分 {_num(job.match_score)}"
        f"（规则 {_num(job.rule_score)}｜LLM {_num(job.llm_score)}）[/dim]"
    )


def _print_hard_checks(job: Job) -> None:
    """硬条件四个子项逐条列出（"为什么硬条件只给了 18 分"就看这里）。"""
    detail = from_json(job.rule_detail_json, default=None)
    checks = (((detail or {}).get("parts") or {}).get("hard") or {}).get("checks") or []
    if not checks:
        return
    console.print()
    table = Table(title="硬条件逐项", header_style="bold cyan")
    table.add_column("项目")
    table.add_column("判定", justify="center")
    table.add_column("说明", overflow="fold")
    for item in checks:
        ratio = item.get("ratio")
        mark = "—" if ratio is None else ("✅" if ratio >= 0.6 else "⚠️")
        table.add_row(str(item.get("name")), mark, str(item.get("note")))
    console.print(table)


def _print_llm_reason(job: Job) -> None:
    """LLM 的完整理由（优势 / 差距 / 缺的关键词 / 一句话结论）。"""
    reason = from_json(job.reason_json, default=None)
    if not (isinstance(reason, dict) and reason):
        console.print()
        console.print("  [dim]还没有 LLM 精评（跑 `jh match run` 或没配 Key）[/dim]")
        return

    console.print()
    console.print("[bold]LLM 精评理由[/bold]")
    fields = (
        ("strengths", "[green]优势：[/green]"),
        ("gaps", "[yellow]差距：[/yellow]"),
        ("missing_keywords", "[red]缺的关键词：[/red]"),
    )
    for key, label in fields:
        values = reason.get(key)
        if values:
            console.print("  " + label + "；".join(map(str, values)))
    if reason.get("recommended_resume"):
        console.print(f"  [dim]模型推荐简历：{reason['recommended_resume']}[/dim]")
    if reason.get("reason"):
        console.print(f"  [dim]{reason['reason']}[/dim]")


# ---------------------------------------------------------------------------
# set
# ---------------------------------------------------------------------------
@app.command("set")
def match_set(
    job_id: Annotated[int, typer.Argument(help="岗位 ID")],
    resume: Annotated[int | None, typer.Option("--resume", "-r", help="指定用哪份简历")] = None,
    verdict: Annotated[
        str | None, typer.Option("--verdict", help="人工裁决：skip（不投）/ queue（要投）")
    ] = None,
) -> None:
    """人工覆盖模型判断（我的话比模型大）。"""
    if resume is None and verdict is None:
        err_console.print("[red]至少要给一个参数：--resume 或 --verdict[/red]")
        raise typer.Exit(code=2)

    engine, factory = require_db()
    try:
        with factory() as session:
            job = job_svc.get_job(session, job_id)
            if job is None:
                err_console.print(f"[red]找不到岗位 id={job_id}[/red]")
                raise typer.Exit(code=1)

            if resume is not None:
                row = session.get(Resume, resume)
                if row is None:
                    err_console.print(f"[red]找不到简历 id={resume}[/red]")
                    raise typer.Exit(code=1)
                job.matched_resume_id = row.id
                console.print(f"  简历 → [bold]{row.name}[/bold]（id={row.id}）")

            if verdict is not None:
                if verdict not in ("skip", "queue"):
                    err_console.print("[red]--verdict 只能是 skip 或 queue[/red]")
                    raise typer.Exit(code=2)
                job.status = "skipped" if verdict == "skip" else "queued"
                console.print(f"  裁决 → [bold]{job.status}[/bold]")

            session.commit()
    finally:
        engine.dispose()

    success(f"岗位 #{job_id} 已更新（人工裁决不会被后续抓取覆盖）")

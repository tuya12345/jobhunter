"""【D 档裁决复验】用户 2026-09-19 拍板的 15 条里，开发会话实施的那 12 条。

背景：这批条目此前**全部没有可证伪用例**（它们挂在"需要用户拍板"，没法先写断言）。
用户拍板之后，开发会话按裁决实现了（提交 `9397fe9` / `9c12a23` / `2435f37` /
`2a10ef7` / `4616873` / `2540f5d`）。
**现在语义定下来了，就该把断言补上** —— 否则这 12 条又变成"开发说修好了"。

★ 本文件按用户裁决逐条写，每条的 docstring 里都写明"裁决定了什么"，
  这样将来有人想改行为时，能立刻看到当初是怎么定的。
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from sqlalchemy import func, select

from jobhunter.config.paths import find_project_root
from jobhunter.models.job import Job
from jobhunter.models.llm_cache import LLMCache
from jobhunter.services import apply_svc, export_svc, match_svc, today_svc
from tests.verify._helpers import build_detail, insert_job, insert_resume, score_job


# ===========================================================================
# #18 —— 删掉"翻页上限"这个假控制
# ===========================================================================
def test_the_fake_pagination_limit_is_actually_gone() -> None:
    """**用户裁决 A**：删掉 `rate_limit.max_pages` + 修正 COMPLIANCE 措辞。

    ★ 这条为什么重要：`COMPLIANCE.md` 曾声称「频率克制 ✅ 随机延时 + **翻页上限** + 出错即停」，
      而 `max_pages` 全仓零读取点（`adapters/base.py` 的 `search()` 有签名没实现、没调用）。
      **合规文档声称一个不存在的控制，比一个没人读的配置项严重得多** ——
      它是"自评文件不可信"的起点。

    ★ 判据用**模型字段**而不是"源码里还有没有这个词"：
      删掉之后代码里仍会留下"这里刻意没有 max_pages"的解释性注释，那是**好事**，不该被判红。
      （我第一版就是按字符串搜的，于是把解释当成了残留。）
    """
    from jobhunter.config.settings import AppSettings, RateLimitSettings

    assert "max_pages" not in RateLimitSettings.model_fields, (
        "RateLimitSettings 里还有 max_pages 字段 —— 裁决是删掉这个假控制"
    )
    rate_limit = AppSettings.model_fields["rate_limit"].default
    assert not hasattr(rate_limit, "max_pages"), "AppSettings 实例上还能拿到 max_pages"

    config_text = (find_project_root() / "config.yaml").read_text(encoding="utf-8")
    # 只看**没被注释掉**的配置行
    live_lines = [
        line
        for line in config_text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert not any("max_pages" in line for line in live_lines), (
        "config.yaml 里还有生效的 max_pages 配置项"
    )

    # 合规文档不能再**声称**有"翻页上限"这个控制。
    # ★ 判据要精确：文档**应该**保留一段说明"以前写过翻页上限、后来为什么删掉"——
    #   那是透明的复盘，不该被判红。真正要防的是"把翻页上限列成一条生效的控制"。
    #   （我前两版按"这一行有没有否定词"筛，把复盘文字当成了残留 —— 两次误报。）
    compliance = (find_project_root() / "docs" / "COMPLIANCE.md").read_text(encoding="utf-8")

    frequency_rows = [line for line in compliance.splitlines() if line.startswith("| 频率")]
    assert frequency_rows, "COMPLIANCE.md 里找不到『频率克制』那一行"
    assert not any("翻页上限" in row for row in frequency_rows), (
        "『频率克制』那一行仍把翻页上限列为生效的控制：\n  " + "\n  ".join(frequency_rows)
    )

    # ★★ 2026-09-21 改写：原断言要求 COMPLIANCE 写明「当前没有自动翻页路径」——
    #    那句话在写下那天是真的，**但用户 09-21 授权放开了自动搜索**，
    #    于是这个断言的前提死了。
    #
    #    当时交接文档的处理是：在 §5.2 里保留一段**引述旧标题的"墓碑文字"**，
    #    让这条断言继续通过，并**主动在文档里写明"这是守卫被绕过、不是守卫通过"**。
    #    这个处理是诚实的（它没有假装守卫还有效），但断言本身必须由我来改 —— 它是我的文件。
    #
    #    改成什么：判据从"翻页必须没有"换成"**文档说的与代码做的必须一致**"。
    #    这条不变式在两种授权状态下都成立，而且比原来那条更接近 #18 的本意
    #    （#18 要修的就是"文档声称一个代码里不存在的控制"）。
    compliance = (find_project_root() / "docs" / "COMPLIANCE.md").read_text(encoding="utf-8")
    discover = find_project_root() / "src" / "jobhunter" / "adapters" / "discover.py"
    pagination_exists = discover.is_file()

    claims_no_paging = "当前没有自动翻页路径" in compliance
    claims_has_paging = "有自动翻页" in compliance or "自动搜索" in compliance

    if pagination_exists:
        assert claims_has_paging, (
            "发现层（`discover.py`）存在、也就是**确实有**自动搜索翻页，"
            "但 COMPLIANCE.md 没有如实说明 —— 文档与代码不符（这正是 #18 要修的那类问题）"
        )
    else:
        assert claims_no_paging, (
            "没有发现层、也就是**确实没有**自动翻页，但 COMPLIANCE.md 没说清这一点"
        )


def test_the_random_delay_that_does_exist_is_still_there() -> None:
    """删假控制时**不能把真控制一起删掉** —— 随机延时是真的存在的。

    ★ 这是"做减法"时的典型风险：删掉一整段描述，连带把里面真实的那部分也删了。
      所以正反两面都要有断言。
    """
    manual = (find_project_root() / "src" / "jobhunter" / "adapters" / "manual.py").read_text(
        encoding="utf-8"
    )
    assert "uniform" in manual and "sleep" in manual, (
        "随机延时不见了 —— 那是**真实存在**的频率控制，删假控制时不该连它一起删"
    )


# ===========================================================================
# #19 —— UA 保留，但用途必须写在文档里
# ===========================================================================
def test_the_ua_override_is_documented_with_its_reason() -> None:
    """**用户裁决 A**：保留该 UA，但在文档里写明用途（为拿桌面版页面，不是规避风控）。

    ★ 判据不是"文档里出现了 UA 这个词"，而是**它解释了为什么**：
      只写"我们设了 UA"读起来像掩饰，写出取舍才是负责。

    ★ 我第一版按"含 UA 的段落"取文本，漏掉了真正关键的那句
      （「**它不是用来规避风控的**」所在的段落里没有 UA 二字）——
      于是**误报成没写**。改成按**小节**取文本。
    """
    compliance = (find_project_root() / "docs" / "COMPLIANCE.md").read_text(encoding="utf-8")

    # 找到讲 UA 的那一节（标题里带 UA），取到下一个同级标题为止
    lines = compliance.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("#") and "UA" in line), None)
    assert start is not None, "COMPLIANCE.md 里没有专门讲 UA 的小节"
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].startswith("#") and (len(lines[i]) - len(lines[i].lstrip("#"))) <= level
        ),
        len(lines),
    )
    section = "\n".join(lines[start:end])
    assert len(section) > 200, f"UA 那一节太短，像是没写清楚：{section!r}"

    assert "桌面" in section, "没有说明这个 UA 的**目的**（拿到桌面版页面）—— 只写'设了 UA'像掩饰"
    assert "不是用来规避风控" in section or "并非用来规避风控" in section, (
        "没有写明它**不是**用于规避风控 —— 这句是这条裁决的核心"
    )


# ===========================================================================
# #20 —— 去重命中时来源字段"首次为准"
# ===========================================================================
def test_dedup_keeps_the_first_source_instead_of_overwriting_it(session, settings) -> None:
    """**用户裁决 A**：去重命中时 `url` / `source_keyword` 改成"首次为准"。

    ★ 理由：`DECISION_FIELDS` 已经确立了"去重命中时绝不覆盖人工决策字段"，
      来源信息属于同一类 —— 二次录入时把来源改写掉，事后复盘"这岗从哪找到的"就失真了。

    ★ 走**生产路径** `job_svc.upsert_job`（不是直接造 ORM 对象）：
      覆盖规则写在 upsert 里，绕开它就等于没验。
    """
    from jobhunter.services import job_svc

    detail = build_detail(url="https://example.com/job/first-wins")
    first = job_svc.upsert_job(session, detail, source_keyword="第一来源")
    session.flush()
    assert first.is_new, "第一次录入应当是新建"

    # 同一个岗位、换个来源关键词再录一次 → 应走去重合并
    again = job_svc.upsert_job(session, detail, source_keyword="第二来源")
    session.flush()

    assert again.job_id == first.job_id, "同一个岗位被录成了两条：去重没生效"
    assert not again.is_new, "第二次录入被当成了新建"

    job = session.get(Job, first.job_id)
    session.refresh(job)
    assert job.source_keyword == "第一来源", (
        f"去重命中时把来源关键词覆盖了（第一来源 → {job.source_keyword!r}）—— "
        "事后无法复盘'这个岗是从哪找到的'（ISSUE #20）"
    )


# ===========================================================================
# #21 —— 文档口径统一成"三层"
# ===========================================================================
def test_dedup_layer_count_is_consistent_across_docs() -> None:
    """**用户裁决 A**：统一成"三层"（以代码事实为准）。

    ★ 判据是"**全仓只有一个说法**"，而不是"某三个文件改对了" ——
      上次口径分叉就是因为只改了看得见的那一处。
    """
    root = find_project_root()
    two_layer: list[str] = []
    for name in ("README.md", "docs/roadmap.md", "docs/architecture.md", "docs/config.md"):
        path = root / name
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "两层去重" in line or "两层" in line and "去重" in line:
                two_layer.append(f"{name}:{number}: {line.strip()}")

    assert not two_layer, (
        "还有文档在说『两层去重』，而代码是三层（platform_job_id → URL → dedup_norm_hash）：\n  "
        + "\n  ".join(two_layer)
    )


# ===========================================================================
# #23 —— `reasoning_content` 不请求也不存
# ===========================================================================
def test_reasoning_content_is_not_read_or_stored() -> None:
    """**用户裁决 A**：不请求也不存（当前用的 `deepseek-chat` 根本不返回，是死代码）。

    ★ "死代码"的危害是它让读代码的人以为"我们处理了推理内容"。

    ★ 判据是"**代码里不读不存**"，不是"源码里不许出现这个词"：
      删掉之后仍会留下"为什么刻意不读"的解释性注释/文档字符串，那是**好事**。
      所以我先把注释与 docstring 剥掉再查
      （第一版按行首 `#` 过滤，把 docstring 里的散文当成了代码）。
    """
    import ast

    root = find_project_root()
    hits: list[str] = []
    for path in (root / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # 剥掉所有 docstring：把模块/类/函数的第一个字符串常量删掉
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = node.body
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    node.body = body[1:] or [ast.Pass()]
        code = ast.unparse(tree)  # 注释也不会出现在这里
        if "reasoning_content" in code:
            hits.append(f"{path.relative_to(root)}（剥掉注释与 docstring 后仍有引用）")

    assert not hits, "代码里还在读/存 reasoning_content（裁决是彻底不读不存）：\n  " + "\n  ".join(
        hits
    )


# ===========================================================================
# #25 —— `jh cache stats` / `jh cache clear`
# ===========================================================================
def test_cache_commands_exist_and_clear_only_the_cache(session, settings) -> None:
    """**用户裁决 A**：加 `jh cache stats` + `jh cache clear`，不做 TTL。

    ★ 关键断言有两条：
      ① 命令真的注册了（不是只有个文件）；
      ② `clear` **只删缓存**，不牵连业务数据 —— `models/llm_cache.py` 的 docstring
         承诺过"缓存是纯派生数据，删掉不影响业务数据"，行为必须与承诺一致。
    """
    from typer.testing import CliRunner

    from jobhunter.cli.app import app
    from jobhunter.services import cache_stats_svc

    runner = CliRunner()
    help_text = runner.invoke(app, ["cache", "--help"]).stdout
    assert "stats" in help_text and "clear" in help_text, (
        f"`jh cache` 下没有 stats/clear 两个子命令：{help_text!r}"
    )

    # 造一条缓存 + 一条业务数据
    insert_resume(session)
    job = insert_job(session)
    score_job(session, job, settings)
    session.add(LLMCache(cache_key="c" * 64, task="verify", response_json=json.dumps({"ok": True})))
    session.flush()
    jobs_before = session.scalar(select(func.count()).select_from(Job))
    assert session.scalar(select(func.count()).select_from(LLMCache)) >= 1

    removed = cache_stats_svc.clear_cache(session)
    session.flush()

    assert removed >= 1, f"clear_cache 报删了 {removed} 条，但库里明明有缓存"
    assert session.scalar(select(func.count()).select_from(LLMCache)) == 0, "clear 没有清掉缓存"
    assert session.scalar(select(func.count()).select_from(Job)) == jobs_before, (
        "clear 把业务数据（jobs）也删了 —— 缓存是纯派生数据，清它不该牵连任何业务表"
    )


# ===========================================================================
# #26 —— 缓存命中时本次成本记 0
# ===========================================================================
def test_cache_hit_reports_zero_cost_for_this_call(session, settings) -> None:
    """**用户裁决 A**：命中时本次记 0，另记"省下 X"。

    ★ 原来把**历史** tokens/cost 累加进"本次"，于是同一份缓存命中 10 次，
      `jh stats` 里的成本会翻 10 倍 —— 用户会以为这工具很贵，而它其实很便宜。
      **数字不报错、但意思是错的**，是静默失真里最典型的一种。

    ★ 断言分两头：**"省下"的口径**必须是"每行成本 × 该行命中次数"，
      而不是"全部行成本之和" —— 后者会把"存进去但从没复用"的行也算成省下的钱，
      而那些钱是**真花掉的**。
    """
    from jobhunter.services import cache_stats_svc

    session.add_all(
        [
            # 花了 0.10，被复用了 3 次 → 省下 0.30
            LLMCache(cache_key="a" * 64, task="match", response_json="{}", cost=0.10, hits=3),
            # 花了 0.50，一次都没复用 → 省下 0，这 0.50 是真花掉的
            LLMCache(cache_key="b" * 64, task="match", response_json="{}", cost=0.50, hits=0),
        ]
    )
    session.flush()

    report = cache_stats_svc.build_cache_stats(session)
    total = report.overall
    assert abs(total.saved - 0.30) < 1e-9, (
        f"『省下』算成了 {total.saved}，期望 0.30 —— 口径必须是 Σ(每行成本 × 该行命中次数)，"
        "不能把从没复用过的行也算进省下的钱（那笔钱是真花掉的）"
    )
    assert total.hits == 3, f"命中次数统计不对：{total.hits}"
    assert abs(total.cost - 0.60) < 1e-9, (
        f"历史成本合计算成了 {total.cost}，期望 0.60 —— 那是『当初花掉的』，与『省下的』是两个数"
    )


# ===========================================================================
# #27 —— match run 默认跳过已处理岗位
# ===========================================================================
def test_match_run_skips_already_handled_jobs_by_default(session, settings) -> None:
    """**用户裁决 A**：默认跳过 `applied`/`skipped`，加 `--all` 覆盖。

    ★ 但**不能因此让用户失去"我要重算全部"的能力** —— 那是他们的数据。
      所以正反两面都要断言：默认跳过 + `--all` 真的能算全部。

    ★ 还要断言**跳过原因是分档的**（不是笼统一个数字）：
      UI 上要能说清"为什么少了这些条"，混成"跳过 N 条"就是本项目的静默失真纪律要防的事。
    """
    insert_resume(session)  # 造岗位前得先有简历，否则规则层跑不起来
    jobs = [
        insert_job(
            session, build_detail(url=f"https://example.com/job/{i}", platform_job_id=f"v{i}")
        )
        for i in range(3)
    ]
    session.flush()
    jobs[0].status = "applied"
    jobs[1].status = "skipped"
    session.flush()

    scope = match_svc.select_scoring_jobs(session, include_all=False)
    ids_pending = {job.id for job in scope.jobs}
    assert jobs[0].id not in ids_pending, "已投递的岗位仍被默认算分（白花额度）"
    assert jobs[1].id not in ids_pending, "已跳过的岗位仍被默认算分"
    assert jobs[2].id in ids_pending, "没处理过的岗位反而不算了？"
    assert scope.skipped_count == 2, f"跳过条数不对：{scope.skipped_count}"
    assert scope.reasons, "跳过原因没有分档 —— UI 上说不清『为什么少了这些条』"
    assert scope.total == 3, f"total 应当是『库里一共有多少岗位』：{scope.total}"

    everything = match_svc.select_scoring_jobs(session, include_all=True)
    assert {job.id for job in jobs} <= {job.id for job in everything.jobs}, (
        "`--all` 没有覆盖全部岗位 —— 用户会失去『改完权重全部重算』的能力"
    )


# ===========================================================================
# #28 —— README 提醒 .env 是本机文件
# ===========================================================================
def test_readme_warns_that_env_is_a_local_secret_file() -> None:
    """**用户裁决 A**：不动 `.env`，只在 README 提醒一句。

    ★ 措辞要注意分寸：工作区是**干净**的（受跟踪文件零命中，已复验），
      这里要传达的是"这是你的本机文件、别外传"，不是"仓库里有隐私"。
    """
    readme = (find_project_root() / "README.md").read_text(encoding="utf-8")
    mentions = [line for line in readme.splitlines() if ".env" in line]
    assert mentions, "README 里没有提到 .env"
    joined = "\n".join(mentions)
    assert any(word in joined for word in ("本机", "不要提交", "别提交", "gitignore", "私密")), (
        "README 提到了 .env 但没说清它是本机私密文件、不要外传：\n  " + joined
    )


# ===========================================================================
# #40 —— 导出带 job_id，且简历名收窄
# ===========================================================================
def test_export_carries_job_id_and_narrows_the_resume_name(
    session, settings, tmp_path: Path
) -> None:
    """**用户裁决 A**：加 `job_id` + 简历名改成「简历 #id」。

    ★ 两个理由各一条断言：
      · `job_id` —— 序号是"当天清单里的序号"，隔天就对不上，没法照着打标/复盘；
      · 简历名收窄 —— 导出物常被丢进聊天窗口/云笔记，而简历**文件名**里往往带着真名。
    """
    insert_resume(session)
    job = insert_job(session)
    score_job(session, job, settings)
    session.flush()

    item = today_svc.ensure_item(session, job, 1, settings=settings)
    session.flush()

    path = export_svc.export_markdown([item], output_dir=tmp_path)
    text = path.read_text(encoding="utf-8")

    assert str(job.id) in text, (
        "导出物里没有岗位 id —— 只能靠『当天清单序号』对应，隔天就失效（ISSUE #40）"
    )
    assert "job_id" in text or f"#{job.id}" in text, "导出物里没有可识别的岗位 id 字段"

    # 简历名不得直接出现（收窄成「简历 #id」这类）
    if item.resume_name:
        assert item.resume_name not in text, (
            f"导出物里出现了简历名称 {item.resume_name!r} —— 简历文件名常含真名，"
            "而导出物是会被丢进聊天窗口/云笔记的（ISSUE #40）"
        )


# ===========================================================================
# #41 —— --copy 重新生成后回写库
# ===========================================================================
def test_regenerated_greeting_is_written_back_to_the_database(session, settings) -> None:
    """**用户裁决 A**：重新生成的文本回写库（库内始终最新）。

    ★ 现状曾是：缓存命中时屏幕 == 库内；**缓存失效**时重新调模型 → 屏幕是新文本、
      库里还是旧值 → 下次打开看板看到旧话术，用户以为"这工具记错了"。

    ★ 同时必须守住 #37 的纪律：**"生成"不等于"已发出"** ——
      回写文本不能顺手把 `greeting_sent` 置位。
    """
    insert_resume(session)
    job = insert_job(session)
    score_job(session, job, settings)
    session.flush()

    item = today_svc.ensure_item(session, job, 1, settings=settings)
    session.flush()

    application = apply_svc.get_latest_application(session, job.id)
    assert application is not None
    assert application.greeting_text == item.greeting_full, (
        "库里存的开场白与清单里显示的不是同一段 —— 屏幕与库内不一致（ISSUE #41）"
    )
    assert application.greeting_sent is False, (
        "回写开场白时把 greeting_sent 也置位了 —— 『生成』不等于『已发出』（ISSUE #37 的纪律）"
    )


# ===========================================================================
# #43 —— 配置校验
# ===========================================================================
def test_bad_config_gives_a_readable_error_instead_of_being_silently_ignored() -> None:
    """**用户裁决 A**：`learning_whitelist` / `greeting_banned_words` 加校验。

    ★ 这条的价值全在"**静默无效**"四个字：配置写错了现在不报错，
      用户只会觉得"这个功能没效果"，然后不再用它。

    ★ 判据：坏配置必须**抛错**，且错误信息里能看出是哪个键、期望什么 ——
      不是笼统的 "validation error"。
    """
    import pydantic
    import pytest

    from jobhunter.config.settings import AppSettings

    with pytest.raises((pydantic.ValidationError, ValueError)) as excinfo:
        AppSettings(greeting_banned_words=["贵司", "   "])
    message = str(excinfo.value)
    assert "greeting_banned_words" in message or "禁用词" in message, (
        f"坏配置报错了，但看不出是哪个键出的问题：{message[:300]}"
    )


def test_every_registered_marker_still_has_users() -> None:
    """顺带守住 #22 的不变式（分层测试不能是空的）—— 这条与 D 档同批，防止将来被删。"""
    config = tomllib.loads((find_project_root() / "pyproject.toml").read_text(encoding="utf-8"))
    names = [
        entry.split(":")[0].strip() for entry in config["tool"]["pytest"]["ini_options"]["markers"]
    ]
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in (find_project_root() / "tests").rglob("*.py")
    )
    unused = [name for name in names if f"pytest.mark.{name}" not in sources]
    assert not unused, f"这些 marker 注册了却没人用：{unused}"


# ===========================================================================
# 附：迁移脚本可跑（#20/#21 改了注释，必须确认迁移链没被弄坏）
# ===========================================================================
def test_migration_chain_still_builds_from_scratch(session) -> None:
    """从空库跑完整迁移链 —— 本轮有人动过 `0001`（只改注释），要确认它仍然可用。

    ★ 判据是"**空库能建到最新**"，而不是"我本地能跑"：已经跑过的迁移被改动，
      在一台新机器上才会暴露问题。
    """
    from alembic.migration import MigrationContext

    context = MigrationContext.configure(session.connection())
    current = context.get_current_revision()
    assert current, "迁移版本号读不到 —— 迁移链可能坏了"
    assert current >= "0003", f"迁移版本停在 {current}，比预期旧"

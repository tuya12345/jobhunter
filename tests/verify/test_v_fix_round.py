"""【复验轮】开发会话声称修好、但**我当时没有独立用例**的那几条。

背景：2026-09-19 开发会话清了 B 档 10 条 + C 档 11 条 + 未标注 4 条。
其中 20 条有我为它们预先写好的可证伪用例（修复后全部转为 XPASS，那些已经是有力的证据）。
**但有 6 条没有**：#7 / #22 / #24 / #37 / #38 / #39。

★ 为什么必须补：没有独立用例时，"已修复"就只是开发会话的自述 ——
  而那正是"独立验证"这个角色存在的全部理由（ISSUES.md 文末的"验证覆盖缺口"一节）。
  这个文件补的就是那个缺口（#24 顺出来的 #55 另见 test_v_p4_greeting.py）。
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from jobhunter.services import apply_svc, export_svc, today_svc
from tests.verify._helpers import insert_job, insert_resume, score_job


# ===========================================================================
# #39 —— "库里没有简历"分支漏传 job（潜在 AttributeError）
# ===========================================================================
def test_today_item_without_any_resume_still_carries_its_job(session, settings) -> None:
    """★★ ISSUE #39：`ensure_item` 在"库里没有简历"分支构造 `TodayItem` 时漏传 `job`，
    而渲染要读 `item.job.city`（`today_cmd.py`）→ 该路径一旦被触达即 `AttributeError`。

    ★ 当时我把它记为"当前不可达"，所以没写用例 —— 但**不可达只是当下**：
      这正是"潜在崩溃点"，一旦有别的改动让它可达，用户看到的就是一个 Python 堆栈。
      现在补上，把"不可达"变成"可达也不崩"。

    这条路径的触达条件就是"库里没有简历"（`load_resume_candidates` 返回空）。
    """
    job = insert_job(session)
    # ★ 不能调 score_job()：它自己要先装简历才能算分（那正是本条要避开的）。
    #   直接给一个达线的规则分即可 —— 本条盯的是"数据构造有没有带上 job"。
    job.match_score = 80
    job.jd_keywords_json = json.dumps(["Python", "FastAPI"], ensure_ascii=False)
    session.flush()

    item = today_svc.ensure_item(session, job, 1, settings=settings)

    assert item.job is job, (
        "没有简历的分支构造 TodayItem 时没带上 job —— 渲染读 item.job.city 会 AttributeError"
    )
    # 渲染侧真正的读法（today_cmd 就是这么读的）：不能抛
    assert item.job.city is not None
    # 该分支还要给出可操作的理由，而不是静默空开场白
    assert any("简历" in issue for issue in item.greeting_issues), (
        f"没有简历时应当告诉用户去 jh resume add，实际 issues={item.greeting_issues}"
    )


# ===========================================================================
# #38 —— 导出"绝不覆盖"必须是硬保证，不是概率性命名
# ===========================================================================
def test_export_never_touches_a_file_that_already_exists(tmp_path: Path) -> None:
    """★★ ISSUE #38：原实现是"时间戳命名 + `exists()` 检查"，而底层 `write_text`
    是 `os.replace`（**无条件覆盖**）—— 检查与写入之间存在 TOCTOU 窗口。

    ★ 确定性证伪方式（不靠并发去撞窗口，那种用例会时灵时不灵）：
      **在写入者会挑中的那个名字上预先放一个"毒文件"**，然后要求
      "毒文件一个字节都没被动过，新内容写到下一个候选名里"。
      原实现在 `unique_path` 之后仍然可能落到同一个名字上并被 `os.replace` 覆盖；
      新实现用 `open(path, "x")`（`O_CREAT|O_EXCL`）独占创建，撞上就换名。
    """
    fixed_now = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    stamp = export_svc._stamp(fixed_now)
    poison = tmp_path / f"today-{stamp}.md"
    poison.write_text("我不能被覆盖", encoding="utf-8")

    written = export_svc.export_markdown([], output_dir=tmp_path, now=fixed_now)

    assert poison.read_text(encoding="utf-8") == "我不能被覆盖", (
        "已存在的文件被覆盖了 —— 这正是 ISSUE #38 要防的事（导出是隐私红线：只写新文件）"
    )
    assert written != poison, "写到了已存在的那个文件名上"
    assert written.is_file(), f"没有产出新文件：{written}"


def test_concurrent_exports_all_survive(tmp_path: Path) -> None:
    """同一个时间戳下**并发**导出：每一份都必须落成独立文件，一份都不能丢。

    ★ 与上一条的分工：上一条是确定性的"契约"验证，这一条是并发下的**压力证据**。
      原实现（`unique_path` + `os.replace`）在 16 个线程抢同一分钟内必然互相覆盖，
      结果会是"文件数 < 线程数"。
    """
    fixed_now = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
    workers = 16
    barrier = threading.Barrier(workers)
    results: list[Path] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _one(index: int) -> None:
        try:
            barrier.wait(timeout=10)  # 尽量让 16 个线程真的同时冲进去
            path = export_svc.export_markdown([], output_dir=tmp_path, now=fixed_now)
            with lock:
                results.append(path)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_one, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, f"并发导出抛异常：{errors}"
    assert len(results) == workers, f"应产出 {workers} 份，实际 {len(results)}"
    assert len(set(results)) == workers, (
        f"有 {workers - len(set(results))} 份导出被写到了同一个文件上（互相覆盖）"
    )
    on_disk = sorted(path.name for path in tmp_path.glob("today-*.md"))
    assert len(on_disk) == workers, f"磁盘上只有 {len(on_disk)} 个文件，期望 {workers}：{on_disk}"


# ===========================================================================
# #37 —— greeting_sent / greeting_edited 不能恒为 0
# ===========================================================================
def test_greeting_sent_is_set_by_a_real_action(session, settings) -> None:
    """★★ ISSUE #37：`set_status` 的 `greeting_sent` / `greeting_edited` 两个形参
    **没有任何调用方传参** → 两列恒为 0，任何直接读列的统计都会得出"从未用过话术"。

    ★ 修好之后，"真的投出去了"这个动作应当把列置位；而且不只是列 ——
      事件流也要补上（两处口径一致，否则看板与统计又会各说各话）。
    """
    insert_resume(session)
    job = insert_job(session)
    score_job(session, job, settings)
    application, _ = apply_svc.ensure_pending_application(
        session, job, greeting="您好，看到岗位要求 Python，方便聊聊吗？", greeting_variant="short"
    )
    session.commit()
    assert application.greeting_sent is False, "还没投之前不该是已发出"

    apply_svc.set_status(session, job.id, "applied", source="verify")
    session.commit()
    session.refresh(application)

    assert application.greeting_sent is True, (
        "标成 applied 且有开场白，greeting_sent 列仍为 False —— "
        "任何读这一列的统计都会得出'从未用过话术'（ISSUE #37 原症状）"
    )
    from sqlalchemy import select

    from jobhunter.models.application import ApplicationEvent

    kinds = list(
        session.scalars(
            select(ApplicationEvent.event_type).where(
                ApplicationEvent.application_id == application.id
            )
        )
    )
    assert "greeting_sent" in kinds, (
        f"列置位了但事件流里没有 greeting_sent —— 看板与统计会各说各话。实际事件：{kinds}"
    )


def test_greeting_sent_can_be_corrected_back_to_false(session, settings) -> None:
    """`--no-sent` 那条路（开发会话为 #37 补的纠正入口）必须真的能把它改回来。

    ★ 这一条盯的是"能不能纠正"，因为**推定**（标 applied 就当成发过了）
      本身是一个猜测。猜错了却改不回来，这个字段就不可信 —— 不可信的字段
      比没有这个字段更糟（用户会照着它做判断）。
    """
    insert_resume(session)
    job = insert_job(session)
    score_job(session, job, settings)
    application, _ = apply_svc.ensure_pending_application(
        session, job, greeting="您好，看到岗位要求 Python，方便聊聊吗？", greeting_variant="short"
    )
    apply_svc.set_status(session, job.id, "applied", source="verify")
    session.commit()
    session.refresh(application)
    assert application.greeting_sent is True

    apply_svc.set_status(session, job.id, "applied", source="verify", greeting_sent=False)
    session.commit()
    session.refresh(application)

    assert application.greeting_sent is False, "显式传 greeting_sent=False 没能纠正推定值"


# ===========================================================================
# #24 —— 缓存 key 必须覆盖 rule_hint / JD 表头字段（match 那一半）
# ===========================================================================
def test_match_cache_key_changes_when_the_prompt_content_changes() -> None:
    """★★ ISSUE #24：`compute_cache_key` 的四个固定成分覆盖不了"进提示词但不在里面"的东西
    （规则层锚点 `rule_hint`、JD 表头字段…）→ 改了权重却命中旧缓存，
    用户会看到"分数纹丝不动"，从而得出"调权重没用"的错误结论。

    ★ 判据：提示词内容一变，key 就必须变。我没有断言"模型被调用了几次"
      （那会被重试/降级干扰），而是直接比 key —— key 是 `llm_cache` 的主键，
      "不同输入 → 不同 key"就是它的全部契约。
    """
    from jobhunter.llm import cache as llm_cache
    from jobhunter.llm.base import LLMMessage, PromptPayload
    from jobhunter.services import match_llm_svc

    def _fingerprint(hint: str) -> str:
        payload = PromptPayload(
            prompt_version=1,
            task="match",
            messages=[
                LLMMessage(role="system", content="你是匹配评审。"),
                LLMMessage(role="user", content=hint),
            ],
        )
        return match_llm_svc._prompt_fingerprint(payload)

    base = {
        "prompt_version": 1,
        "model": "mock-model",
        "jd_hash": "a" * 64,
        "resume_content_hash": "b" * 64,
        "task": "match",
    }
    first = llm_cache.compute_cache_key(**base, prompt_fingerprint=_fingerprint("方向：AI Agent"))
    second = llm_cache.compute_cache_key(**base, prompt_fingerprint=_fingerprint("方向：后端开发"))

    assert first and second
    assert first != second, (
        "改了 rule_hint（提示词内容）而缓存 key 不变 —— "
        "调完权重再跑匹配会直接复用旧分（ISSUE #24 的原症状）"
    )


def test_every_cache_key_call_site_passes_a_prompt_fingerprint() -> None:
    """`prompt_fingerprint` 是**可选**参数 —— 加了但没接线就等于没修。

    ★ 这个形态本项目出现过三次：#37（形参无调用方传参）、#24（加了参数只在 match 接线）、
      #55（greeting 那条路径上同一个洞还在）。
      所以判据不能是"函数支持指纹"，必须是 **每一个调用点都传了**。

    ★★ 这条用例的来历值得记：它原本是**故意断言 greeting 还没接线**的绊线
      （"修好了却没人回来改这里"必须被处理，而不是让断言悄悄失去意义）。
      本轮开发会话修好 #55 之后，它**如期变红**，现在翻成了"必须全部接线"。
      也就是说：**这条用例自己就是"先有断言、后有修复"这套机制的示范。**

    ★ 扫描前必须**剥掉注释与 docstring**：`tailor_svc.py` 的模块文档里有一句
      「`llm_cache.compute_cache_key(task="tailor")`」是**说明**，不是调用点。
      （我第一版按原文正则扫，把它当成了漏接线的调用 —— 又是"判据太粗"这一类误报。）
    """
    import ast
    import re

    from jobhunter.config.paths import find_project_root

    services = find_project_root() / "src" / "jobhunter" / "services"
    call = re.compile(r"compute_cache_key\(")
    fingerprint = re.compile(r"prompt_fingerprint\s*=")

    missing: list[str] = []
    total = 0
    for path in sorted(services.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
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
        text = ast.unparse(tree)

        for match in call.finditer(text):
            depth = 0
            index = match.end() - 1
            while index < len(text):
                if text[index] == "(":
                    depth += 1
                elif text[index] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                index += 1
            total += 1
            arguments = text[match.end() : index]
            if not fingerprint.search(arguments):
                missing.append(path.name)

    assert total >= 5, f"只找到 {total} 个 compute_cache_key 调用点，是不是被重构掉了？"
    assert not missing, (
        "这些调用点没有传 `prompt_fingerprint` —— 提示词变了它们照样命中旧缓存"
        "（#24 / #55 的原症状）：\n  " + "\n  ".join(missing)
    )


# ===========================================================================
# #7 —— 注释声称的 ruff PLW1514 防护必须真的会响
# ===========================================================================
def test_the_declared_bare_open_guard_actually_fires(tmp_path: Path) -> None:
    """★★ ISSUE #7：`utils/io.py` 的注释一直声称"第二层防护 = ruff PLW1514"，
    而 PLW1514 是 **preview** 规则 —— 不开 `--preview` 时写进 `select` 也一句都不报。

    ★ 这一条**不读注释、不读 pyproject 的说明**，而是真去跑一次：
      在一个临时文件里写一个裸 `open()`，用**CI 里那条一模一样的命令**去检查它。
      · 默认规则集**不该**报（这正是原缺陷：注释说有防护、其实没有）；
      · `--preview --select PLW1514` **必须**报 —— 否则所谓的防护又是空的。
      两边都断言，是为了防止"把命令删了/规则改了"之后这里静默变成一条恒绿用例。
    """
    import subprocess
    import sys

    probe = tmp_path / "probe_bare_open.py"
    probe.write_text(
        "from pathlib import Path\n\n\n"
        "def read(path: Path) -> str:\n"
        "    with open(path) as handle:\n"
        "        return handle.read()\n",
        encoding="utf-8",
    )

    def _run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            [sys.executable, "-m", "ruff", "check", *args, str(probe)],
            capture_output=True,
            text=True,
            check=False,
        )

    default = _run()
    assert default.returncode == 0, (
        "默认规则集居然报了裸 open() —— 那说明 PLW1514 已经被转正，"
        "这段说明与 CI 里那条单独步骤都该更新了（见 pyproject.toml 的注释）"
    )

    guarded = _run("--preview", "--select", "PLW1514")
    # ★ 不能断言输出里有 "PLW1514" —— ruff 报的是规则**名**（`unspecified-encoding`），
    #   不是代码。我第一版就是这么写错的，然后误判成"守卫没响"。
    output = guarded.stdout + guarded.stderr
    assert guarded.returncode != 0 and "unspecified-encoding" in output, (
        "CI 里那条 `ruff check --preview --select PLW1514` 没有抓住裸 open() —— "
        "所谓'第二层防护'又是空的（ISSUE #7 的原症状）\n"
        f"stdout={guarded.stdout!r}\nstderr={guarded.stderr!r}"
    )


def test_the_ci_workflow_keeps_that_guard_step() -> None:
    """守卫只被 CI 里**一条单独的步骤**守着，所以要把"那条步骤还在"也钉住。

    ★ 为什么值得单独一条：`pyproject.toml` 里明明写着"不能开全局 preview"，
      于是 PLW1514 的唯一执行点就是 CI 那一行。谁顺手删了它，
      代码里不会有任何变化 —— 只有这条用例会红。
    """
    from jobhunter.config.paths import find_project_root

    ci = find_project_root() / ".github" / "workflows" / "ci.yml"
    assert ci.is_file(), "CI 工作流不见了"
    text = ci.read_text(encoding="utf-8")
    assert "--preview" in text and "PLW1514" in text, (
        "CI 里那条 `ruff check --preview --select PLW1514` 步骤没了 —— "
        "`utils/io.py` 声称的第二层防护会重新变成一句空话（ISSUE #7）"
    )


# ===========================================================================
# #22 —— 注册了却没人用的 marker（"分层测试"实际没有发生）
# ===========================================================================
def test_every_registered_marker_is_actually_used_by_some_test() -> None:
    """★★ ISSUE #22：`live` / `emulator` 两个 marker 注册了但**零使用**，
    也没有 nightly workflow —— 也就是说"分层测试"这件事实际没有发生。

    ★ 不变式（而不是"检查某个文件在不在"）：**每一个注册的 marker，
      都必须至少有一个用例真的在用它**。注册了没人用 = 那条分层是空的，
      而这种空，从 `pyproject.toml` 上一眼看不出来。
    """
    import tomllib

    from jobhunter.config.paths import find_project_root

    root = find_project_root()
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    markers = config["tool"]["pytest"]["ini_options"]["markers"]
    names = [entry.split(":")[0].strip() for entry in markers]
    assert names, "pyproject 里没有注册任何 marker"

    sources = "\n".join(path.read_text(encoding="utf-8") for path in (root / "tests").rglob("*.py"))
    unused = [
        name
        for name in names
        if f"pytest.mark.{name}" not in sources
        and f"pytestmark = pytest.mark.{name}" not in sources
    ]
    assert not unused, (
        f"这些 marker 注册了但没有任何用例使用：{unused} —— 分层测试是空的（ISSUE #22 的原症状）"
    )


def test_the_nightly_workflow_runs_both_layers_without_failing_the_build() -> None:
    """依据：【提示词 11】的 CI 部分与 ISSUE #22 的修法。

    分层测试要有意义，需要三件事同时成立：
      ① 有一个**定时**跑的 workflow（否则 live 永远不跑）；
      ② 两个 marker 分组都被跑到；
      ③ 它**不阻塞**主构建（真模型抖动不该把仓库变红 —— 那会让人开始忽略红灯）。
    """
    from jobhunter.config.paths import find_project_root

    nightly = find_project_root() / ".github" / "workflows" / "nightly.yml"
    assert nightly.is_file(), "没有 nightly workflow —— live/emulator 这两层永远不会被跑到"
    text = nightly.read_text(encoding="utf-8")

    assert "cron:" in text, "nightly 没有定时触发"
    assert "-m live" in text, "nightly 没有跑 live 层"
    assert "-m emulator" in text, "nightly 没有跑 emulator 层"
    assert "continue-on-error: true" in text, (
        "nightly 失败会阻塞构建 —— 真模型抖动会把仓库变红，几次之后大家就开始忽略红灯了"
    )
    assert "-rs" in text, (
        "没带 -rs：skip 的原因不会出现在摘要里，于是'没配密钥所以整层跳过'这件事会静默发生"
    )

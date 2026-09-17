"""【异常路径】网络失败 / 文件损坏 / 数据库锁 / 编码错误 / LLM 返回空。

每条都断言"用户看到的是什么"，而不只是"没抛异常"——
这个项目自己反复强调的纪律是：**不允许静默失真**。
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from jobhunter.cli.app import app
from jobhunter.llm.base import LLMMessage, PromptPayload
from jobhunter.llm.deepseek import DeepSeekError, DeepSeekProvider
from jobhunter.llm.mock import MockProvider
from jobhunter.models import Job, Resume
from jobhunter.services import match_llm_svc, match_svc, resume_svc
from jobhunter.storage.db import create_db_engine, make_session_factory
from jobhunter.utils.io import write_text
from tests.verify._helpers import (
    build_detail,
    build_resume_content,
    insert_job,
    insert_resume,
    score_job,
)

runner = CliRunner()


def _payload() -> PromptPayload:
    return PromptPayload(
        prompt_version=1,
        task="match",
        messages=[LLMMessage(role="user", content="请输出 json")],
    )


# ===========================================================================
# ① 网络层：失败必须变成可理解的错误，且不该重试不该重试的
# ===========================================================================
def test_connect_error_becomes_deepseek_error_after_retries() -> None:
    """依据：deepseek.py docstring「这一层只重试网络层失败」。

    断言：最终抛 `DeepSeekError`（而不是把 httpx 的异常漏给上层），
    且确实重试了 1+2 次（`_NETWORK_RETRIES = 2`）。
    """
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectError("连接被拒绝", request=request)

    provider = DeepSeekProvider("sk-verify-test", transport=httpx.MockTransport(handler))
    with pytest.raises(DeepSeekError):
        provider.complete(_payload())
    assert len(attempts) == 3, f"网络错误的重试次数不符：{len(attempts)}"


def test_401_is_not_retried() -> None:
    """依据：`_is_retryable` 注释「4xx（除 429）不重试：401 是密钥错，重试一百次也一样」。"""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(401, json={"error": "invalid api key"}, request=request)

    provider = DeepSeekProvider("sk-verify-bad", transport=httpx.MockTransport(handler))
    with pytest.raises(DeepSeekError):
        provider.complete(_payload())
    assert len(attempts) == 1, f"401 不该重试，却请求了 {len(attempts)} 次"


@pytest.mark.xfail(
    reason="ISSUE #10（🟡）：200 + 非 JSON 响应体时抛裸 JSONDecodeError，无法指导排查",
    strict=False,
)
def test_http_200_with_non_json_body_does_not_leak_raw_exception() -> None:
    """边界：网关返回 200 + HTML（真实常见）。

    期望：错误信息里能看出"响应不是 JSON"，而不是一句裸的 JSONDecodeError
    把用户扔在"为什么会这样"里（属于可诊断性问题）。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>502 Bad Gateway</html>", request=request)

    provider = DeepSeekProvider("sk-verify-test", transport=httpx.MockTransport(handler))
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 - 这里就是要看"抛了什么"
        provider.complete(_payload())
    message = str(excinfo.value)
    assert "json" in message.lower() or "JSON" in message, (
        f"200 但响应体不是 JSON 时，报错没告诉用户原因：{type(excinfo.value).__name__}: {message}"
    )


# ===========================================================================
# ② LLM 输出异常：空 content / 坏 JSON / 越界分数
# ===========================================================================
async def test_empty_content_retries_then_degrades_keeping_rule_score(session, settings) -> None:
    """依据：设计文档 §5.1.2 + STATUS.md「空 content → 重试 1 次 → 仍失败降级用规则分」。

    断言三件事：① 真的重试了；② 降级后**规则分还在**；③ 没有写入任何 LLM 脏数据。
    """
    insert_resume(session)
    job = insert_job(session)
    result = score_job(session, job, settings)
    candidates = match_svc.load_resume_candidates(session, settings)

    provider = MockProvider(empty_times=99)  # 永远返回空
    stats = await match_llm_svc.run_llm_layer(
        session, [job], candidates, provider=provider, settings=settings
    )

    assert stats.degraded == 1, f"没有降级：{stats}"
    retries = settings.llm_max_retries
    assert stats.calls == retries + 1, (
        f"空 content 的重试次数不符：calls={stats.calls}, llm_max_retries={retries}"
    )
    assert job.llm_score is None, "降级了却写进了 LLM 分（脏数据）"
    assert job.match_score == result.rule_score, "降级后 match_score 应回落到规则分"
    assert job.reason_json is None, "降级了却写了 reason"
    assert stats.errors, "降级了却没有记录原因"


@pytest.mark.xfail(
    reason="ISSUE #17（🟡）：改简历后重跑规则层，上一次的 llm_score 仍被当成 match_score",
    strict=False,
)
async def test_stale_llm_score_is_dropped_when_resume_changes(session, settings) -> None:
    """★★ 依据：评审采纳项 #3「LLM 缓存 key 错（改简历后仍返回旧分）」
    —— 这条修的是**缓存**层；但**库里已经落下的 llm_score** 走的是另一条路：

        `apply_result_to_job` 合成 match_score 的规则是
        「有 llm_score 就用 llm_score，否则用 rule_score」（match_svc.py:1073）。

    于是：改了简历 → 重跑 `jh match run` → 规则分确实变了，
    但 `match_score` 仍然显示**上一次针对旧简历**算出来的 LLM 分。
    用户看到"我改了简历，分数纹丝不动"——正是评审点名的那个症状，
    只是发生在库里的历史值上，而不是缓存命中。

    期望：规则层重算时，若 `matched_resume_id` / 简历 hash 已变化，
    那次 LLM 分就必须视为失效（清空 llm_score / reason_json）。
    """
    from jobhunter.utils.io import to_json as dump_json

    resume = insert_resume(session)
    job = insert_job(session)
    score_job(session, job, settings)
    candidates = match_svc.load_resume_candidates(session, settings)
    provider = MockProvider(responses={"match": '{"score": 93, "reason": "针对旧简历"}'})
    await match_llm_svc.run_llm_layer(
        session, [job], candidates, provider=provider, settings=settings
    )
    assert job.llm_score == 93 and job.match_score == 93

    # 改简历：加一个技能（content_hash 会变）
    changed = build_resume_content(skills=[*build_resume_content().skills, "Kubernetes"])
    resume.content_json = dump_json(changed.model_dump(), indent=None)
    resume.content_hash = resume_svc.compute_content_hash(changed)
    session.flush()

    # 重跑规则层（生产路径）
    score_job(session, job, settings)

    assert job.match_score == job.rule_score, (
        f"改了简历并重跑规则层之后，match_score 仍是上一次的 LLM 分"
        f"（match_score={job.match_score}, rule_score={job.rule_score}, "
        f"llm_score={job.llm_score}）—— 用户会看到'改了简历分数不动'"
    )


async def test_bad_json_is_rejected_not_guessed(session, settings) -> None:
    """依据：模块 docstring 第 ② 条「校验不过就当失败，绝不'尽力猜一个分数出来'」。"""
    insert_resume(session)
    job = insert_job(session)
    score_job(session, job, settings)
    candidates = match_svc.load_resume_candidates(session, settings)

    provider = MockProvider(bad_json=True)
    stats = await match_llm_svc.run_llm_layer(
        session, [job], candidates, provider=provider, settings=settings
    )
    assert stats.degraded == 1
    assert job.llm_score is None, "散文里没有分数，却被猜了一个出来"


@pytest.mark.parametrize("value", [150, -20, "abc"])
def test_out_of_range_or_wrong_type_score_is_rejected(value: object) -> None:
    """依据：模块 docstring 第 ③ 条「分数越界 → schema 上 ge=0, le=100，越界即校验失败」。

    ★ 注意：边界必须"拒绝"，不能"截断到 100"——截断会抹掉"模型开始乱答"的信号。
    """
    with pytest.raises(match_llm_svc.VerdictParseError):
        match_llm_svc.parse_verdict(f'{{"score": {value!r}}}'.replace("'", '"'))


@pytest.mark.xfail(
    reason="ISSUE #11（🟡）：pydantic 非严格模式把 true 强转成 1 分（静默失真）",
    strict=False,
)
def test_boolean_score_is_rejected_not_coerced() -> None:
    """依据：同上 + 「绝不静默截断/静默转换」。

    `true` 是"模型答错了"，不是"1 分"。把它强转成 1 分属于静默失真：
    用户会在清单里看到一个 1 分的岗位，而根本原因是模型乱答。
    """
    with pytest.raises(match_llm_svc.VerdictParseError):
        match_llm_svc.parse_verdict('{"score": true}')


def test_missing_score_is_rejected() -> None:
    """依据：`LLMMatchVerdict` docstring「score 是唯一必须拿到的东西（没有它就只能降级）」。"""
    with pytest.raises(match_llm_svc.VerdictParseError):
        match_llm_svc.parse_verdict('{"strengths": ["a"]}')


def test_markdown_fence_is_tolerated_but_garbage_is_not() -> None:
    """依据：`parse_verdict` docstring「先剥一层 markdown 代码块；剥完还解析不了就必须失败」。"""
    verdict = match_llm_svc.parse_verdict('```json\n{"score": 77}\n```')
    assert verdict.score == 77
    with pytest.raises(match_llm_svc.VerdictParseError):
        match_llm_svc.parse_verdict("这个岗位挺合适的")


async def test_one_job_failing_does_not_abort_the_batch(session, settings) -> None:
    """依据：`run_llm_layer` docstring「单个岗位失败不中断整批，
    否则前面已经花掉的调用就白费了，而用户看到的是'什么都没发生'」。"""

    class ExplodingProvider(MockProvider):
        def complete(self, payload: PromptPayload):  # type: ignore[override]
            raise RuntimeError("模拟 provider 内部炸了")

    insert_resume(session)
    jobs = [
        insert_job(
            session,
            build_detail(
                platform_job_id=f"batch{idx}",
                url=f"https://www.zhipin.com/job_detail/batch{idx}.html",
            ),
        )
        for idx in range(3)
    ]
    for job in jobs:
        score_job(session, job, settings)
    candidates = match_svc.load_resume_candidates(session, settings)

    stats = await match_llm_svc.run_llm_layer(
        session, jobs, candidates, provider=ExplodingProvider(), settings=settings
    )
    assert stats.degraded == 3, "有岗位失败就中断了整批"
    assert all(job.llm_score is None for job in jobs)
    assert all(job.rule_score is not None for job in jobs), "规则分被连累丢了"


async def test_llm_results_are_durable_without_the_cli_commit(session, settings, db_url) -> None:
    """依据：模块 docstring「每完成一个就**立刻写库**（不是攒到最后一起写）——
    中途 Ctrl+C 也不会丢掉已经算出来的（那些是真的花了钱的）」。

    验证方式：跑完 LLM 层后**不调用 CLI 的 commit**，用一个全新连接去读。
    如果只有 flush、没有 commit，新连接什么都看不到 —— 那句承诺就没兑现。
    """
    insert_resume(session)
    job = insert_job(session)
    score_job(session, job, settings)
    candidates = match_svc.load_resume_candidates(session, settings)
    session.commit()  # 规则层的结果先落盘，模拟"上一阶段已完成"

    provider = MockProvider()
    stats = await match_llm_svc.run_llm_layer(
        session, [job], candidates, provider=provider, settings=settings
    )
    assert stats.calls == 1

    other_engine = create_db_engine(db_url, ensure_directory=False)
    try:
        with make_session_factory(other_engine)() as other:
            cache_rows = other.execute(text("SELECT COUNT(*) FROM llm_cache")).scalar_one()
            fresh_job = other.get(Job, job.id)
            assert fresh_job is not None
            llm_score = fresh_job.llm_score
    finally:
        other_engine.dispose()

    assert cache_rows >= 1, "花了钱的 LLM 结果没有落盘（只 flush 没 commit），中断即全丢"
    assert llm_score is not None, "岗位上的 LLM 分没有落盘，中断即全丢"


# ===========================================================================
# ③ 数据库并发（设计文档 §3.2 的 WAL 到底买到了什么）
# ===========================================================================
def test_readers_are_not_blocked_by_an_open_write_transaction(session, db_url) -> None:
    """依据：db.py docstring「默认模式下读会阻塞写、写会阻塞读……
    WAL 让读写互不阻塞，P8 的 Web 界面才不会被锁住」。

    实测：一个连接持有未提交的写事务时，另一个连接必须仍能读。
    """
    insert_job(session)  # 未提交的写事务
    session.flush()

    other_engine = create_db_engine(db_url, ensure_directory=False)
    try:
        with make_session_factory(other_engine)() as other:
            count = other.execute(text("SELECT COUNT(*) FROM jobs")).scalar_one()
            assert count == 0, "读到的是未提交的数据（隔离级别不对）"
    finally:
        other_engine.dispose()
    session.rollback()


# ===========================================================================
# ④ 文件损坏 / 编码错误：用户看到的提示能不能指导下一步
# ===========================================================================
@pytest.mark.xfail(
    reason="ISSUE #12（🟡）：非 UTF-8 简历只报裸 UnicodeDecodeError，不给中文的'另存为 UTF-8'指引",
    strict=False,
)
def test_gbk_resume_file_gives_actionable_error(isolated_env, engine) -> None:  # noqa: ARG001
    """依据：`ResumeParseError` 的存在意义（resume_svc.py 的"空文档"分支写了详细原因与建议）
    + 设计文档 §3.2「Windows 上裸 open() 默认 cp936」这条踩坑记录。

    一个 GBK 编码的 .txt 简历是中文 Windows 上极常见的输入。
    期望：给出**中文的、可操作**的提示（"这不是 UTF-8，请另存为 UTF-8"）。

    ★ 断言故意要求中文关键词：裸的 `UnicodeDecodeError: 'utf-8' codec ...` 里
      恰好含有 "utf-8" 字样，如果只断言 "utf-8" 就会被它蒙混通过（我第一版就踩了这个坑）。
    """
    target = isolated_env / "gbk-resume.txt"  # 文件名刻意中性（同 docx 用例的理由）
    target.write_bytes("姓名：林小满\n技能：Python".encode("gbk"))

    result = runner.invoke(app, ["resume", "add", str(target)])
    assert result.exit_code != 0, "GBK 文件居然导入成功了（说明解码被静默替换）"
    output = result.output
    assert "Traceback" not in output, f"把原始堆栈抛给了用户：\n{output}"
    assert "编码" in output, f"报错没有用中文指出真正的原因（编码）：\n{output}"


@pytest.mark.xfail(
    reason="ISSUE #12（🟡）：损坏 docx 只报 PackageNotFoundError，看不出'文件坏了'",
    strict=False,
)
def test_corrupted_docx_gives_actionable_error(isolated_env, engine) -> None:  # noqa: ARG001
    """依据：同 P1 提示词「文件损坏」这类异常必须转成友好错误（docx 解析失败是预期内的）。

    ★ 断言只认中文的"文件坏了"类字眼，且**文件名里不能含这些字** ——
      否则裸报错 `Package not found at '损坏的简历.docx'` 会因为文件名而蒙混过关
      （我第一版就把文件名写成"损坏的简历.docx"，结果误判成通过）。
    """
    target = isolated_env / "broken.docx"  # 文件名刻意中性，避免污染断言
    target.write_bytes(b"\x00\x01\x02 not a real docx at all \xff\xfe")

    result = runner.invoke(app, ["resume", "add", str(target)])
    assert result.exit_code != 0, "损坏的 docx 居然导入成功了"
    output = result.output
    assert "Traceback" not in output, f"把原始堆栈抛给了用户：\n{output}"
    assert any(word in output for word in ("损坏", "无法打开", "不是有效", "打不开")), (
        f"报错没有告诉用户文件本身有问题：\n{output}"
    )


@pytest.mark.xfail(
    reason="ISSUE #15（🟡）：纯空白 .txt 被当成'成功导入的空简历'，还会自动变成默认简历",
    strict=False,
)
def test_empty_text_resume_raises_parse_error_with_reason(isolated_env, engine) -> None:  # noqa: ARG001
    """依据：`parse_resume_file` 的 `ResumeParseError` 分支
    「一个文本块都没读到 → 报错并给出原因与建议」。

    实测：纯空白的 .txt 会**成功导入**成一份 0 技能/0 项目/无姓名的简历，
    而且因为"库里第一份自动成为默认简历"，它还会变成后续所有匹配的参照物 ——
    比直接报错糟得多（用户不会察觉，分数却是基于一份空简历算的）。
    """
    target = isolated_env / "空的.txt"
    write_text(target, "   \n\n   ")

    result = runner.invoke(app, ["resume", "add", str(target)])
    assert result.exit_code == 1, f"空简历应友好失败，实际：\n{result.output}"
    assert "解析失败" in result.output or "没能" in result.output
    assert "Traceback" not in result.output


def test_missing_resume_file_is_reported_not_crashed(isolated_env, engine) -> None:  # noqa: ARG001
    """边界：路径不存在。"""
    result = runner.invoke(app, ["resume", "add", str(isolated_env / "根本不存在.docx")])
    assert result.exit_code == 1
    assert "找不到文件" in result.output
    assert "Traceback" not in result.output


def test_unsupported_suffix_is_reported(isolated_env, engine) -> None:  # noqa: ARG001
    """依据：`UnsupportedDocumentError` 的触发条件（.pdf/.doc 不在支持列表）。"""
    target = isolated_env / "简历.pdf"
    target.write_bytes(b"%PDF-1.4 fake")

    result = runner.invoke(app, ["resume", "add", str(target)])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "不支持" in result.output or "格式" in result.output


def test_resume_import_is_utf8_safe_for_bom_files(isolated_env, session, settings) -> None:
    """边界：Windows 记事本默认存 UTF-8 **带 BOM**。

    期望：BOM 不该污染首行解析（它是不可见字符，`isspace()` 为 False，
    `strip()` 不会去掉它 —— 这类"看不见的字符"是最典型的静默解析失败源）。
    """
    target = isolated_env / "带BOM.txt"
    content = (
        "姓名：林小满\n"
        "手机：13800000000\n"
        "技能：Python、Docker\n"
        "项目：智能问答助手\n"
        "用 Python 实现了检索问答。\n"
    )
    target.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))

    result = resume_svc.add_resume(session, target, make_default=True, settings=settings)
    row = session.get(Resume, result.resume_id)
    assert row is not None
    parsed = resume_svc.content_from_json(row.content_json)
    assert parsed.basic.get("name") == "林小满", f"BOM 破坏了姓名解析：{parsed.basic}"
    assert "Python" in parsed.skills, f"BOM 破坏了技能解析：{parsed.skills}"

"""P2 的两条修复：登录墙不许当 JD 入库（#62）、`--html` 的链接与平台（#61）。

★★ 这两条是同一条用户旅程上的两个坑

    用户想录 BOSS 的岗位 → 自动抓取撞上**登录墙**（#62：工具把登录页正文当 JD 存了，
    还打印「✓ 入库完成」）→ 于是转 `--html` 另存为网页这条路 → **链接丢了、平台被静默忽略**（#61）。
    所以两组用例放在一起：修完一条而另一条还在，那条路照样走不通。

★ #62 是 🔴：它的危害不是"没抓到"，而是**报成功、存的却是垃圾** ——
  登录页文本会被 `jh match run` 打分、被 `jh today` 生成开场白、被 `jh tailor` 定制简历，
  而用户从输出里看不出任何异常。所以下面最关键的断言是两条：
      ① 命中登录墙时 `fetch_status` 必须是 `failed`、`jd_text` 必须**清空**；
      ② CLI 必须**一条都不入库**（去数库里的行数），并给出两条不用绕过登录的路。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from jobhunter.adapters.base import JobDetail
from jobhunter.adapters.manual import ManualAdapter, html_to_detail, read_html_file
from jobhunter.adapters.parse_html import detect_access_wall
from jobhunter.cli.app import app
from jobhunter.models import Job
from jobhunter.storage.db import create_db_engine, make_session_factory

runner = CliRunner()

#: 实测里抓到的 BOSS 登录页的样子（标题 + 正文特征词，且**没有任何岗位结构**）
LOGIN_HTML = """<!DOCTYPE html>
<html><head><title>【BOSS直聘注册登录】boss直聘在线注册登录-BOSS直聘</title></head>
<body>
  <div class="footer">客户服务热线: 400 065 5799</div>
  <div>找工作 BOSS直聘直接谈</div>
  <div>APP扫码登录</div>
  <div>验证码登录/注册</div>
  <div>首次验证通过即注册BOSS直聘账号</div>
  <div>发送验证码</div>
  <div>微信登录/注册</div>
</body></html>
"""

#: 一张**真的**岗位页（有岗位结构；正文里故意出现"登录"字样，用来验证不误伤）
JOB_HTML = """<!DOCTYPE html>
<html><head><title>AI Agent 开发工程师_虚构科技_15-25K_深圳_招聘</title></head>
<body>
  <h1>AI Agent 开发工程师</h1>
  <div class="company-info"><span class="name">虚构科技有限公司</span></div>
  <div class="job-sec-text">
    <p>岗位职责：负责 Agent 平台的编排与工具调用开发；</p>
    <p>任职要求：熟悉 Python、FastAPI、LLM 应用开发；登录后可直接与 HR 沟通。</p>
  </div>
</body></html>
"""

#: 既没有墙特征、也没有岗位结构的普通页面（不判墙 —— 它只是"没抓到正文"）
PLAIN_HTML = """<!DOCTYPE html>
<html><head><title>某公司招聘页</title></head>
<body><p>欢迎投递简历，我们会尽快联系你。</p></body></html>
"""

BOSS_URL = "https://www.zhipin.com/job_detail/c6181f2d6ee9b1150nF939y-F1NX.html"


def _job_count(db_path: Path) -> int:
    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        with make_session_factory(engine)() as session:
            return int(session.scalar(select(func.count()).select_from(Job)) or 0)
    finally:
        engine.dispose()


@pytest.fixture
def initialized(isolated_env: Path) -> Path:
    """先 `jh init` 建库，返回库文件路径（用来数行数）。"""
    result = runner.invoke(app, ["--no-remind", "init"])
    assert result.exit_code == 0, result.output
    return isolated_env / "data" / "jobhunter.db"


# ===========================================================================
# #62 登录墙识别
# ===========================================================================
def test_login_page_is_detected_as_a_wall() -> None:
    note = detect_access_wall(LOGIN_HTML, platform="boss")
    assert note is not None, "登录页没有被识别出来 —— 它会被当成 JD 入库"
    assert "登录" in note
    # 说明必须是**可操作**的：给出两条不绕过登录的路
    assert "--html" in note and "--manual" in note
    # 还得说清"为什么判定是墙"，否则用户不知道怎么自查
    assert "岗位结构" in note


def test_real_job_page_is_not_flagged_even_if_it_mentions_login() -> None:
    """★ 反向守卫：正文里出现「登录后可直接沟通」**不能**被判成墙。

    单看特征词一定会误伤（真实 JD 里出现"登录"太正常了），
    所以判据是组合的：有墙特征 **且** 没有任何岗位结构。
    """
    assert detect_access_wall(JOB_HTML, platform="official") is None


def test_wall_detection_requires_both_signals() -> None:
    """元测试：把"组合判据"钉死 —— 单看任何一条都会误判。"""
    # 只有墙特征、没有岗位结构 → 判墙
    assert detect_access_wall(LOGIN_HTML) is not None
    # 只有"没有岗位结构"、没有任何墙特征 → **不**判墙（那只是普通/改版页面）
    assert detect_access_wall(PLAIN_HTML) is None
    # 墙特征 + 岗位结构（真实页里出现"验证码"这种词）→ 不判墙
    mixed = JOB_HTML.replace("登录后可直接与 HR 沟通", "需要短信验证码登录后沟通")
    assert detect_access_wall(mixed) is None


def test_wall_text_only_in_rendered_content_is_detected() -> None:
    """登录页的正文往往是**客户端渲染**的：原始 HTML 里可能一个字都没有。"""
    assert (
        detect_access_wall("<html><body></body></html>", rendered_text="APP扫码登录 验证码登录")
        is not None
    )


def test_parse_marks_the_wall_as_failed_and_clears_the_jd() -> None:
    """★★ 核心回归：登录页**不许**留下可被下游使用的正文。"""
    detail = html_to_detail(LOGIN_HTML, BOSS_URL, platform="boss")

    assert detail.fetch_status == "failed", "登录页必须是 failed（partial 会被照常入库）"
    assert detail.jd_text == "", "登录页文本必须被清空 —— 否则会被拿去打分/写开场白"
    assert detail.fetch_note and "登录" in detail.fetch_note


def test_fetch_detail_marks_the_wall_after_rendering(monkeypatch: pytest.MonkeyPatch) -> None:
    """走 `fetch_detail` 那条真实路径（渲染后判定，比只看 HTML 多一路证据）。"""
    monkeypatch.setattr("jobhunter.adapters.manual.is_playwright_available", lambda: True)
    monkeypatch.setattr(ManualAdapter, "_polite_delay", lambda self: None)
    monkeypatch.setattr(
        ManualAdapter,
        "_render_page",
        lambda self, url: (LOGIN_HTML, "APP扫码登录 验证码登录/注册", None),
    )

    detail = ManualAdapter(platform="boss").fetch_detail(BOSS_URL)

    assert detail is not None
    assert detail.fetch_status == "failed"
    assert detail.jd_text == ""
    assert detail.fetch_note and "--html" in detail.fetch_note
    assert detail.raw.get("access_wall") is True, "raw 里要留痕，便于事后排查"


def test_cli_stores_nothing_and_explains_when_it_hits_a_wall(
    initialized: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★★ CLI 级验收：**去数库里的行数**，并检查给出的两条路。

    这就是 #62 的原症状：改前它会入库一条 `jd_text=登录页文本` 的记录、
    打印「✓ 入库完成：新增 1」，退出码 0。
    """
    monkeypatch.setattr("jobhunter.adapters.manual.is_playwright_available", lambda: True)
    monkeypatch.setattr(ManualAdapter, "_polite_delay", lambda self: None)
    monkeypatch.setattr(
        ManualAdapter,
        "_render_page",
        lambda self, url: (LOGIN_HTML, "", "【BOSS直聘注册登录】"),
    )

    result = runner.invoke(app, ["--no-remind", "job", "add", BOSS_URL])

    assert result.exit_code != 0, "一条都没入库时必须是失败退出码"
    assert _job_count(initialized) == 0, "登录页被入库了（这正是 #62）"
    assert "登录" in result.output
    assert "--html" in result.output and "--manual" in result.output
    assert "入库完成" not in result.output, "什么都没入库，不许打印成功"


def test_saved_login_page_through_html_flag_is_also_refused(
    initialized: Path, tmp_path: Path
) -> None:
    """`--html` 另存下来的页面也走同一道闸门（用户很可能存到的就是登录页）。"""
    page = tmp_path / "saved_login.html"
    page.write_text(LOGIN_HTML, encoding="utf-8")

    result = runner.invoke(app, ["--no-remind", "job", "add", "--html", str(page)])

    assert _job_count(initialized) == 0
    assert "登录" in result.output
    assert result.exit_code != 0


# ===========================================================================
# #61 `--html` 的链接与平台
# ===========================================================================
def test_read_html_file_keeps_the_original_url_and_platform(tmp_path: Path) -> None:
    page = tmp_path / "saved.html"
    page.write_text(JOB_HTML, encoding="utf-8")

    detail = read_html_file(page, url=BOSS_URL, platform="boss")

    assert detail.url == BOSS_URL, "原始链接丢了 —— 列表里点不回去，第一层去重也用不上"
    assert detail.platform == "boss", "--platform 被静默忽略了"
    assert detail.platform_job_id == "c6181f2d6ee9b1150nF939y-F1NX", (
        "有了平台与链接，才能抽出平台岗位 ID（第一层去重靠它）"
    )


def test_read_html_file_without_url_still_falls_back_to_file_url(tmp_path: Path) -> None:
    """不传 `--url` 时：链接记成本地文件路径，平台记成 `manual`（**不猜 official**）。"""
    page = tmp_path / "saved.html"
    page.write_text(JOB_HTML, encoding="utf-8")

    detail = read_html_file(page)

    assert detail.url is not None and detail.url.startswith("file:///")
    assert detail.title  # 解析能力不受影响
    # ★ 本地文件没有域名 → 不该落进 `detect_platform()` 的兜底 `official`：
    #   那会把一份从 BOSS 存下来的网页记成"公司官网"，还会进渠道统计。
    assert detail.platform == "manual", "本地文件该记成 manual，而不是猜成 official"


def test_explicit_platform_beats_the_local_default(tmp_path: Path) -> None:
    """给了 `--platform` 就以它为准（用户在命令行上知道的比工具多）。"""
    page = tmp_path / "saved.html"
    page.write_text(JOB_HTML, encoding="utf-8")

    assert read_html_file(page, platform="boss").platform == "boss"


def test_cli_html_with_url_and_platform_writes_the_real_link(
    initialized: Path, tmp_path: Path
) -> None:
    """CLI 级：#61 的两个缺口一起修好之后，库里应当有真链接、真平台、真岗位 ID。"""
    page = tmp_path / "saved.html"
    page.write_text(JOB_HTML, encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "--no-remind",
            "job",
            "add",
            "--html",
            str(page),
            "--url",
            BOSS_URL,
            "--platform",
            "boss",
        ],
    )
    assert result.exit_code == 0, result.output

    engine = create_db_engine(f"sqlite:///{initialized}")
    try:
        with make_session_factory(engine)() as session:
            job = session.scalars(select(Job)).one()
            assert job.url == BOSS_URL
            assert job.platform == "boss"
            assert job.platform_job_id == "c6181f2d6ee9b1150nF939y-F1NX"
    finally:
        engine.dispose()


def test_cli_manual_text_also_accepts_url_and_platform(initialized: Path) -> None:
    """`--manual` / `--text` 同样能补链接与平台（用户从 App 里复制 JD 的常见用法）。"""
    result = runner.invoke(
        app,
        [
            "--no-remind",
            "job",
            "add",
            "--text",
            "岗位职责：负责 Python 后端开发。任职要求：熟悉 FastAPI 与 MySQL。",
            "--url",
            BOSS_URL,
            "--platform",
            "boss",
        ],
    )
    assert result.exit_code == 0, result.output

    engine = create_db_engine(f"sqlite:///{initialized}")
    try:
        with make_session_factory(engine)() as session:
            job = session.scalars(select(Job)).one()
            assert job.url == BOSS_URL
            assert job.platform == "boss"
    finally:
        engine.dispose()


def test_platform_is_honored_on_the_link_path_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ `--platform` 对**链接抓取**也必须生效（改前它只对手动文本生效）。

    `--help` 写的是"强制指定平台"，所以三条路都该认它 ——
    否则就是又一次"静默忽略一个显式给出的选项"（#34 / #50 那一类）。
    """
    monkeypatch.setattr("jobhunter.adapters.manual.is_playwright_available", lambda: True)
    monkeypatch.setattr(ManualAdapter, "_polite_delay", lambda self: None)
    monkeypatch.setattr(
        ManualAdapter,
        "_render_page",
        lambda self, url: (JOB_HTML, None, None),
    )

    forced = ManualAdapter(platform="boss").fetch_detail("https://example.com/job/1")
    auto = ManualAdapter().fetch_detail("https://example.com/job/1")

    assert forced is not None and forced.platform == "boss", "--platform 在链接路径上没生效"
    assert auto is not None and auto.platform == "official", "不指定时仍应按域名判断"


def test_url_option_refuses_contradictory_usage(initialized: Path, tmp_path: Path) -> None:
    """★ 矛盾输入当场报错，**不静默挑一个**（#61 的教训正是"静默忽略"）。"""
    page = tmp_path / "saved.html"
    page.write_text(JOB_HTML, encoding="utf-8")

    both = runner.invoke(app, ["--no-remind", "job", "add", BOSS_URL, "--url", BOSS_URL])
    assert both.exit_code == 1
    assert "不能同时用" in both.output

    alone = runner.invoke(app, ["--no-remind", "job", "add", "--url", BOSS_URL])
    assert alone.exit_code == 1
    assert "需要配合一种输入方式" in alone.output


def test_job_show_displays_a_local_file_readably(initialized: Path, tmp_path: Path) -> None:
    """本地页面记成 `file:///…` 时，详情页要标出「本地保存的页面」而不是甩一个路径。"""
    page = tmp_path / "saved.html"
    page.write_text(JOB_HTML, encoding="utf-8")
    runner.invoke(app, ["--no-remind", "job", "add", "--html", str(page)])

    result = runner.invoke(app, ["--no-remind", "job", "show", "1"])
    assert result.exit_code == 0, result.output
    assert "本地保存的页面" in result.output


def test_failed_detail_is_reported_with_its_note() -> None:
    """`_collect_details` 必须把适配器给的 `fetch_note` 原样带到失败列表里。"""
    from jobhunter.cli.commands.job_cmd import _collect_details

    note = "BOSS直聘的岗位详情需要登录才能看" + "\n" + "  jh job add --manual"
    detail = JobDetail(platform="boss", jd_text="", fetch_status="failed", fetch_note=note)

    class _Adapter:
        def __init__(self, *args: object, **kwargs: object) -> None: ...
        def fetch_detail(self, url: str) -> JobDetail:
            return detail

    import jobhunter.adapters.manual as manual_module

    original = manual_module.ManualAdapter
    manual_module.ManualAdapter = _Adapter  # type: ignore[assignment,misc]
    try:
        details, failures = _collect_details(
            urls=["https://example.com/job/1"],
            file=None,
            manual=False,
            text=None,
            html_file=None,
            url=None,
            platform=None,
            settings=None,
        )
    finally:
        manual_module.ManualAdapter = original  # type: ignore[misc]

    assert details == []
    assert len(failures) == 1
    assert "需要登录才能看" in failures[0]

"""`jh job` 命令层测试（P2）。

★ 为什么这里**不测网络抓取路径**

    `jh job add <url>` 的 Playwright 那一段需要真实网络（打不开的站点、登录墙、
    随时改版）—— 把它写进 CI 等于给 CI 装了个随机红灯，最后一定会被人删掉或加 skip。
    所以本文件测的是**不需要网络**的那三条路径：

        · `--text` / `--manual`  手动粘贴（网络不通也能用 —— 这是降级路径的存在意义）
        · `--html`               从本地保存的网页解析（离线重放）
        · `--file`               批量文件格式解析（含注释与空行的容错）

    真实抓取由你本地手动验收（见 docs/roadmap.md 的 P2 验收记录）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobhunter.cli.app import app

runner = CliRunner()

SAMPLE_JD = """公司：某某网络科技有限公司
职位：AI Agent 开发工程师
薪资：15-25K·14薪
城市：深圳

岗位职责：
1. 负责 AI Agent 应用的架构设计与开发；
2. 基于大模型 API 实现工具调用与多轮对话；
3. 参与 RAG 检索链路的优化（切块、向量化、重排）。

任职要求：
1. 熟悉 Python，了解 FastAPI 等 Web 框架；
2. 了解 LangChain / Function Calling 者优先。
"""

SAMPLE_HTML = """<!DOCTYPE html>
<html><head><title>Python 后端开发工程师_某某科技_20-30K_广州_招聘</title></head>
<body>
  <h1>Python 后端开发工程师</h1>
  <div class="company-info"><span class="name">某某科技有限公司</span></div>
  <div class="job-sec-text">
    <p>岗位职责：负责核心业务系统的后端接口开发与维护；</p>
    <p>任职要求：熟悉 Python、FastAPI、MySQL，有良好的工程习惯；</p>
    <p>加分项：了解 Docker 与 CI/CD 流程，有开源项目经验者优先。</p>
  </div>
</body></html>
"""


@pytest.fixture
def initialized(isolated_env: Path) -> Path:
    """先 `jh init` 建库。"""
    result = runner.invoke(app, ["--no-remind", "init"])
    assert result.exit_code == 0, result.output
    return isolated_env


# ---------------------------------------------------------------------------
# 未初始化
# ---------------------------------------------------------------------------
def test_add_without_database_hints_init(isolated_env: Path) -> None:
    """没建库时提示 `jh init`，而不是抛数据库错误。"""
    result = runner.invoke(app, ["--no-remind", "job", "add", "--text", SAMPLE_JD])
    assert result.exit_code == 1
    assert "jh init" in result.output
    assert "no such table" not in result.output


def test_add_without_any_input_explains_usage(initialized: Path) -> None:
    """什么都不给时，要列出四种用法（而不是静默失败）。"""
    result = runner.invoke(app, ["--no-remind", "job", "add"])
    assert result.exit_code == 1
    assert "--manual" in result.output
    assert "--file" in result.output
    assert "--html" in result.output


# ---------------------------------------------------------------------------
# --text / --manual（手动粘贴，网络不通也能用）
# ---------------------------------------------------------------------------
def test_add_from_text(initialized: Path) -> None:
    """`--text` 直接给一段 JD → 解析入库。"""
    result = runner.invoke(app, ["--no-remind", "job", "add", "--text", SAMPLE_JD])
    assert result.exit_code == 0, result.output
    assert "新增 1" in result.output

    listing = runner.invoke(app, ["--no-remind", "job", "list"])
    assert "某某网络科技有限公司" in listing.output
    assert "AI Agent 开发工程师" in listing.output


def test_add_from_stdin(initialized: Path) -> None:
    """`--manual` 从标准输入读（真实用法是粘贴后按 Ctrl+Z）。

    ★ 这条路径是"网络不通时的兜底" —— 必须能用，否则抓取一失败整个工具就废了。
    """
    result = runner.invoke(app, ["--no-remind", "job", "add", "--manual"], input=SAMPLE_JD)
    assert result.exit_code == 0, result.output
    assert "新增 1" in result.output


def test_add_empty_stdin_is_rejected(initialized: Path) -> None:
    """粘贴内容为空时明确报错，而不是静默入库一条空岗位。"""
    result = runner.invoke(app, ["--no-remind", "job", "add", "--manual"], input="   \n")
    assert "空" in result.output or result.exit_code != 0


def test_dry_run_does_not_write(initialized: Path) -> None:
    """`--dry-run` 只解析不写库（频率克制的一部分：先看看会入库什么）。"""
    result = runner.invoke(app, ["--no-remind", "job", "add", "--text", SAMPLE_JD, "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "没有写入数据库" in result.output

    listing = runner.invoke(app, ["--no-remind", "job", "list"])
    assert "还没有录入任何岗位" in listing.output


# ---------------------------------------------------------------------------
# --html（本地网页，离线重放）
# ---------------------------------------------------------------------------
def test_add_from_local_html(initialized: Path, tmp_path: Path) -> None:
    """从本地保存的网页解析（不想联网 / 抓取被拦时的兜底）。"""
    page = tmp_path / "saved.html"
    page.write_text(SAMPLE_HTML, encoding="utf-8")

    result = runner.invoke(app, ["--no-remind", "job", "add", "--html", str(page)])
    assert result.exit_code == 0, result.output
    assert "新增 1" in result.output

    listing = runner.invoke(app, ["--no-remind", "job", "list"])
    assert "Python 后端开发工程师" in listing.output
    assert "广州" in listing.output


def test_add_html_missing_file(initialized: Path, tmp_path: Path) -> None:
    """本地 HTML 不存在时给出明确失败（而不是崩栈）。"""
    result = runner.invoke(
        app, ["--no-remind", "job", "add", "--html", str(tmp_path / "nope.html")]
    )
    assert "不存在" in result.output


# ---------------------------------------------------------------------------
# --file（批量：一行一个 URL）—— 只测文件格式解析，不触网
# ---------------------------------------------------------------------------
def test_batch_file_parsing_tolerates_comments(initialized: Path, tmp_path: Path) -> None:
    """批量文件要容忍注释与空行，并且**失败要跳过而不是中断**。

    ★ 这里给的是不可达域名：真实抓取会失败（网络失败/离线），
      而这恰好验证了"失败跳过并记录、不整体中断"这条容错要求。
      失败信息里必须提示可以改用 --manual（降级路径）。
    """
    jobs_file = tmp_path / "jobs.txt"
    jobs_file.write_text(
        "# 这是注释，应该被忽略\n\nhttps://invalid.invalid/job/1\nhttps://invalid.invalid/job/2\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["--no-remind", "job", "add", "--file", str(jobs_file)])
    # 全部失败 → 退出码非 0，但必须**给出可操作的建议**而不是崩掉
    assert result.exit_code == 1, result.output
    assert "手动粘贴" in result.output or "--manual" in result.output


def test_batch_file_missing(initialized: Path, tmp_path: Path) -> None:
    """批量文件不存在时明确报错。"""
    result = runner.invoke(app, ["--no-remind", "job", "add", "--file", str(tmp_path / "nope.txt")])
    assert result.exit_code == 1
    assert "找不到文件" in result.output


# ---------------------------------------------------------------------------
# list / show / skip
# ---------------------------------------------------------------------------
def test_list_empty(initialized: Path) -> None:
    """空库时给出下一步动作。"""
    result = runner.invoke(app, ["--no-remind", "job", "list"])
    assert result.exit_code == 0
    assert "还没有录入任何岗位" in result.output
    assert "jh job add" in result.output


def test_show_job_details(initialized: Path) -> None:
    """`jh job show` 打印字段；加 `--jd` 才打印正文。"""
    runner.invoke(app, ["--no-remind", "job", "add", "--text", SAMPLE_JD])

    without_jd = runner.invoke(app, ["--no-remind", "job", "show", "1"])
    assert without_jd.exit_code == 0, without_jd.output
    assert "AI Agent 开发工程师" in without_jd.output
    assert "岗位职责" not in without_jd.output  # 默认不打印正文（避免刷屏）
    assert "--jd" in without_jd.output

    with_jd = runner.invoke(app, ["--no-remind", "job", "show", "1", "--jd"])
    assert "岗位职责" in with_jd.output


def test_show_unknown_id(initialized: Path) -> None:
    """不存在的 id 要提示去看列表。"""
    result = runner.invoke(app, ["--no-remind", "job", "show", "999"])
    assert result.exit_code == 1
    assert "jh job list" in result.output


def test_skip_job(initialized: Path) -> None:
    """`jh job skip` 把岗位标为不投，并且状态过滤能生效。"""
    runner.invoke(app, ["--no-remind", "job", "add", "--text", SAMPLE_JD])
    result = runner.invoke(app, ["--no-remind", "job", "skip", "1", "--reason", "测试"])
    assert result.exit_code == 0, result.output
    assert "已跳过" in result.output

    skipped = runner.invoke(app, ["--no-remind", "job", "list", "--status", "skipped"])
    assert "AI Agent 开发工程师" in skipped.output

    fresh = runner.invoke(app, ["--no-remind", "job", "list", "--status", "new"])
    assert "还没有录入任何岗位" in fresh.output or "AI Agent 开发工程师" not in fresh.output


def test_source_keyword_is_saved(initialized: Path) -> None:
    """`--keyword` 记来源渠道（将来 `jh stats` 复盘渠道效果靠它）。"""
    runner.invoke(
        app,
        ["--no-remind", "job", "add", "--text", SAMPLE_JD, "--keyword", "AI Agent 深圳"],
    )
    result = runner.invoke(app, ["--no-remind", "job", "show", "1"])
    assert "AI Agent 深圳" in result.output


def test_repeat_add_is_idempotent(initialized: Path) -> None:
    """★ 重复录入同一个岗位不该产生重复记录（P2 验收要求之一）。"""
    first = runner.invoke(app, ["--no-remind", "job", "add", "--text", SAMPLE_JD])
    assert "新增 1" in first.output

    second = runner.invoke(app, ["--no-remind", "job", "add", "--text", SAMPLE_JD])
    assert second.exit_code == 0, second.output
    assert "新增" not in second.output  # 不该再新增

    listing = runner.invoke(app, ["--no-remind", "job", "list"])
    assert listing.output.count("AI Agent 开发工程师") == 1

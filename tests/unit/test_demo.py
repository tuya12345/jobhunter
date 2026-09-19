"""`jh demo` 的测试（P9）—— 重点只有一件事：**它绝不许碰真库**。

★ 为什么把"隔离"当成唯一重点：
  demo 会把几十条假数据写进它用的库。一旦它用的是真实库，
  用户的真实投递记录就被假数据混进来了 —— 而且**看起来一切正常**
  （没有报错、命令成功、表里确实有数据），用户可能几周后才发现转化率数字离谱。
  P5 真的发生过一次同类事故（脚本只给子进程传了隔离环境变量，父进程仍解到真库）。
  所以这里用三条互相独立的断言把它钉死：
    ① 演示跑完之后，**配置里那个库文件根本不存在**（demo 从没写过它）；
    ② demo 期间**没有任何真实 Provider 被调用**（monkeypatch 成会炸的实现）；
    ③ 跑完之后配置被还原（否则同进程里后续命令会继续用演示库）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobhunter.cli.app import app
from jobhunter.cli.commands import demo_cmd
from jobhunter.config.settings import get_settings
from jobhunter.llm.deepseek import DeepSeekProvider

runner = CliRunner()


def test_demo_runs_the_whole_flow(settings) -> None:
    """依据：计划书 P9 第 3 条「`jh demo` 用内置假数据 + 独立/内存库跑通全流程」。"""
    result = runner.invoke(app, ["--no-remind", "demo"])
    assert result.exit_code == 0, result.output
    for marker in (
        "JobHunter 演示",
        "两级匹配",
        "今日待投清单",
        "打标跟进",
        "面试准备与 Offer",
        "薄弱点",
        "转化率",
        "定制简历 brief",
        "追问话术",
        "周报与产物",
        "演示完成",
    ):
        assert marker in result.output, f"演示里缺少环节：{marker}"


def test_demo_never_touches_the_configured_database(settings) -> None:
    """★★ 核心断言：演示跑完之后，配置里那个库**根本不存在**。

    ★ 为什么用"文件不存在"而不是"内容没变"：
      前者能同时挡住"新建了一个假库"和"往现有库里写了数据"两种情况，
      而且不依赖任何时间戳/哈希的可疑比较。
    """
    db_path = settings.paths.db_path
    assert not db_path.exists(), "测试前提：这个库还没被建过"

    result = runner.invoke(app, ["--no-remind", "demo"])
    assert result.exit_code == 0, result.output
    assert not db_path.exists(), (
        f"jh demo 把数据写进了配置里的库：{db_path}\n"
        "（demo 必须用临时库 —— 否则假数据会混进真实投递记录）"
    )
    # 连 WAL/SHM 都不该出现
    assert not list(db_path.parent.glob("*.db-wal")) if db_path.parent.exists() else True


def test_demo_uses_no_real_llm(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """演示**不许联网**：真 Provider 的任何一次调用都会让这条测试炸掉。

    依据：`settings.llm_provider` 的注释「`jh demo` 要用假数据跑通全流程，不能真的花钱」。
    """

    def _boom(self, payload):  # noqa: ANN001, ANN202 - 测试替身
        raise AssertionError("jh demo 不该调用真实 LLM（演示必须离线、不花钱）")

    monkeypatch.setattr(DeepSeekProvider, "complete", _boom)
    monkeypatch.setattr(DeepSeekProvider, "is_configured", lambda self: True)
    # 故意把 key 设上：即使配了 key，demo 也必须走 mock
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-demo-must-not-use-me")

    result = runner.invoke(app, ["--no-remind", "demo"])
    assert result.exit_code == 0, result.output
    assert "演示完成" in result.output


def test_demo_restores_settings_afterwards(settings) -> None:
    """跑完之后配置必须还原（否则同进程里下一条命令会继续用演示库）。"""
    before = get_settings().database_url
    result = runner.invoke(app, ["--no-remind", "demo"])
    assert result.exit_code == 0, result.output
    assert get_settings().database_url == before


def test_demo_keep_leaves_files_for_inspection(settings, tmp_path: Path) -> None:
    """`--keep` 保留演示库与产物（给"我想翻着看"用的），且路径会打印出来。"""
    result = runner.invoke(app, ["--no-remind", "demo", "--keep"])
    assert result.exit_code == 0, result.output
    assert "演示数据保留在：" in result.output

    kept = Path(result.output.split("演示数据保留在：")[1].splitlines()[0].strip())
    try:
        assert kept.is_dir()
        assert (kept / "data" / "jobhunter.db").is_file(), "保留模式下应当有演示库文件"
        assert list((kept / "output").glob("report-*.md")), "保留模式下应当有周报产物"
    finally:
        import shutil

        shutil.rmtree(kept, ignore_errors=True)


def test_demo_is_not_a_placeholder_anymore(settings) -> None:
    """确认 `jh demo` 真的实现了（占位命令会以退出码 2 + "尚未实现"结束）。"""
    result = runner.invoke(app, ["--no-remind", "demo"])
    assert result.exit_code == 0, result.output
    assert "尚未实现" not in result.output


def test_demo_keeps_user_preset_data_dirs(
    settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """★ 用户预设过 `JH_DATA_DIR` 时也要能正常收尾（install.md 的第 11 条 FAQ）。

    背景：`_restore` 最初是"一律 pop 掉演示期间设的环境变量"。如果用户平时就用
    `JH_DATA_DIR` 把库放在加密盘/别的盘，pop 会把他的配置弄丢，于是"还原后"的库路径
    与真实库不同 → demo 结束时报 `RuntimeError: 还原失败：配置单例还指着演示库`。
    正确做法：**记下原值、原样放回去**。
    """
    custom = tmp_path / "custom-data"
    custom.mkdir()
    monkeypatch.setenv("JH_DATA_DIR", str(custom))
    from jobhunter.config.settings import reset_settings

    reset_settings()
    before = get_settings().database_url
    assert custom.as_posix() in before, "测试前提：预设的 JH_DATA_DIR 生效了"

    result = runner.invoke(app, ["--no-remind", "demo"])
    assert result.exit_code == 0, result.output

    import os

    assert os.environ.get("JH_DATA_DIR") == str(custom), "用户的预设值必须被放回去"
    assert get_settings().database_url == before


def test_demo_prints_the_scratch_path_it_removed(settings) -> None:
    """默认（不带 `--keep`）要把**删掉的那个临时目录**打出来，并且真的删掉。

    ★ 为什么不写成"扫系统临时目录里有没有 `jh-demo-*` 残留"：
      那种全局扫描在**多个进程同时跑 demo**（比如并行测试、或我一边跑测试一边
      手敲 `jh demo`）时会互相误伤 —— 实测踩到过一次假失败。
      打印路径 + 针对该路径断言，既准确又能让用户确认"没碰别的东西"。
    """
    result = runner.invoke(app, ["--no-remind", "demo"])
    assert result.exit_code == 0, result.output
    marker = "演示临时目录已清理："
    assert marker in result.output, "默认模式应当打印它删掉的临时目录路径"
    scratch = Path(result.output.split(marker)[1].splitlines()[0].strip())
    assert not scratch.exists(), f"说了已清理，但目录还在：{scratch}"


def test_demo_aborts_if_isolation_fails(
    settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """★ 隔离失效时必须**中止**，而不是"继续跑但写进真库"。

    做法：把 `get_settings` 换成"永远返回真实配置"的实现，模拟"环境变量没生效"
    （P5 那次事故的形态）。此时 `_isolate` 的两道断言必须拦住它。

    ★ 顺带守住"中止时要把环境变量还原"：否则这个进程会停在"指向临时目录"的状态，
      后面的用例会跟着遭殃（实测：一条这样的用例把 `JH_LLM_PROVIDER=mock` 留在了环境里，
      于是另一条"没配 key 就该报错"的用例拿到了假 Provider、悄悄变成了另一种行为）。
    """
    import os

    real = get_settings()
    monkeypatch.setattr(demo_cmd, "get_settings", lambda: real)
    monkeypatch.setattr(demo_cmd, "reset_settings", lambda: None)
    # 记下"进入 demo 之前"每个变量的值：中止后必须**逐个回到这个状态**
    # （conftest 会预设 JH_DATA_DIR/OUTPUT/STATE，而 JH_LLM_PROVIDER 本来就没设）
    before = {name: os.environ.get(name) for name in demo_cmd._ENV_KEYS}
    # ★ scratch 用子目录：`tmp_path` 本身是测试数据目录的父目录，
    #   直接拿它当 scratch 会让"还原后还在演示库吗"这类前缀判断误报
    scratch = tmp_path / "scratch"

    with pytest.raises(RuntimeError) as excinfo:
        demo_cmd._isolate(scratch, real_url=real.database_url)
    assert "演示库的路径不对" in str(excinfo.value)
    after = {name: os.environ.get(name) for name in demo_cmd._ENV_KEYS}
    assert after == before, f"中止后环境变量没回到原状：{after} != {before}"


def test_demo_survives_a_data_dir_set_after_settings_were_cached(
    settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """★ #53 的真实场景：**先**让配置单例缓存住，**再**改 `JH_DATA_DIR`。

    验证会话的用例就是这样：它的 `settings`/`engine` 夹具在 `monkeypatch.setenv`
    之前就把配置解出来了（夹具在测试体之前建立），于是 demo 开头读到的"真实库"
    是**旧缓存**的值，而收尾时环境变量已经指向新目录。

    第一版 `_restore` 断言的是"还原后的 URL == 进 demo 前那个 URL" —— 这种比较
    假定了环境在 demo 期间不会变，于是会误报（面板都打出来了、退出码却是 1）。
    正确的断言是"不再指着演示临时库"。
    """
    _ = get_settings()  # 先把单例缓存住（模拟夹具的行为）
    custom = tmp_path / "加密盘"
    custom.mkdir()
    monkeypatch.setenv("JH_DATA_DIR", str(custom))

    result = runner.invoke(app, ["--no-remind", "demo"])
    assert result.exit_code == 0, f"打印了成功面板却返回非 0：\n{result.output[-1500:]}"
    assert "演示完成" in result.output
    from jobhunter.config.settings import reset_settings

    reset_settings()
    assert get_settings().database_url.startswith(f"sqlite:///{custom.as_posix()}")

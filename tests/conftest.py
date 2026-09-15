"""pytest 全局配置。

★★ 这个文件最重要的职责不是"提供 fixture"，而是**保证测试绝不碰真实数据库**。

    data/jobhunter.db 里是我真实的投递记录 —— 被测试洗掉是不可接受的意外，
    而且一旦某个测试学会了"反正会用真库"，后续所有人都会默认这么写。

所以这里做了三层隔离：
    ① 每个测试的 `JH_DATA_DIR` / `JH_OUTPUT_DIR` / `JH_STATE_DIR` 都指向 tmp_path；
    ② `JH_CONFIG` 指向一个**最小临时 config.yaml**（不读项目里那份真实偏好）；
    ③ `project_root` fixture 断言解出来的库路径在 tmp 里 —— 这条是"最后一道闸门"，
       任何人将来改动路径逻辑导致测试指向真实库时，测试会立刻失败而不是静默写坏数据。

另外所有缓存单例（配置、项目根目录）都会在每个测试前后清空，
避免"上一个测试的配置泄漏到下一个测试"这种最难查的测试间污染。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from jobhunter.config.paths import find_project_root
from jobhunter.config.settings import AppSettings, get_settings, reset_settings
from jobhunter.storage.db import create_db_engine, make_session_factory
from jobhunter.storage.migrate import upgrade_head

# 每个测试用的最小配置：故意**不**读项目里的 config.yaml，
# 但字段与真实配置同构（否则测试会在"配置形状"这种假问题上失败）
MINIMAL_CONFIG = """\
keywords: ["AI Agent", "Python 开发"]
cities: ["深圳"]
job_type: both
threshold: 60
daily_limit: 20
llm_top_n: 30
match_weights:
  direction: 30
  hard: 30
  skill: 40
skill_aliases:
  LLM: ["大模型", "LLM"]
learning_whitelist: ["LangChain"]
greeting_banned_words: ["贵司", "期待"]
show_reminders: true
"""


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """把配置与所有数据目录重定向到 tmp_path（**自动应用**到每个测试）。

    为什么用 autouse：隔离应该是**默认行为**，而不是"每个测试作者都要记得加"。
    """
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    state_dir = tmp_path / "state"
    for directory in (data_dir, output_dir, state_dir):
        directory.mkdir(parents=True, exist_ok=True)

    config_file = tmp_path / "config.yaml"
    config_file.write_text(MINIMAL_CONFIG, encoding="utf-8")

    monkeypatch.setenv("JH_DATA_DIR", str(data_dir))
    monkeypatch.setenv("JH_OUTPUT_DIR", str(output_dir))
    monkeypatch.setenv("JH_STATE_DIR", str(state_dir))
    monkeypatch.setenv("JH_CONFIG", str(config_file))
    # 清掉密钥环境变量：避免本机 .env / 环境变量影响"未配置 LLM"的判断
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    # ★ 让配置**完全忽略仓库根目录的 `.env`**（踩过的坑）
    #
    #   `AppSettings.model_config` 里写了 `env_file=".env"`，而 pytest 的工作目录
    #   就是仓库根 —— 于是开发机上的个人 `.env`（含默认简历路径、真实密钥）
    #   会被测试读到。后果有两个，都很糟：
    #     · "没配默认简历"这类用例在本机失败、在 CI 上通过（CI 没有 .env）；
    #     · 测试实际接触了用户的个人配置，违背"测试只碰 tmp"的隔离原则。
    #   把它指向一个不存在的文件，测试就只依赖显式设置的环境变量。
    monkeypatch.setattr(
        AppSettings, "model_config",
        {**AppSettings.model_config, "env_file": str(tmp_path / "nonexistent.env")},
    )

    # 清缓存：配置单例 + 项目根目录缓存（find_project_root 是 lru_cache）
    reset_settings()
    find_project_root.cache_clear()

    yield tmp_path

    # 测试结束后再清一次，防止污染后续测试
    reset_settings()
    find_project_root.cache_clear()


@pytest.fixture
def config_file(isolated_env: Path) -> Path:
    """本次测试用的（临时）config.yaml 路径。"""
    return isolated_env / "config.yaml"


@pytest.fixture
def settings():
    """已隔离的配置对象。"""
    return get_settings()


@pytest.fixture
def db_url(settings) -> str:
    """本次测试用的 SQLite URL（指向 tmp_path）。"""
    return settings.database_url


@pytest.fixture
def engine(db_url: str) -> Iterator[Engine]:
    """已建好全部表的 engine（跑过 Alembic 迁移）。

    ★ 用 Alembic 迁移建表，而不是 `Base.metadata.create_all()`：
      这样测试验证的是**迁移脚本**这一条真实路径。
      如果只用 create_all，迁移脚本写错了测试也发现不了 ——
      而迁移脚本才是线上真正会执行的东西。
    """
    upgrade_head(db_url)
    eng = create_db_engine(db_url, ensure_directory=False)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    """绑定到测试库的 sessionmaker。"""
    return make_session_factory(engine)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """一个可直接用的 Session（用完关闭，不自动提交 —— 让测试自己控制事务）。"""
    db_session = session_factory()
    try:
        yield db_session
    finally:
        db_session.close()


@pytest.fixture
def project_root(settings) -> Path:
    """★ 最后一道闸门：断言测试用的库路径确实在临时目录里。

    任何"路径逻辑被改坏、测试开始指向真实 data/jobhunter.db"的情况，
    都会在这里立刻炸掉，而不是静默写坏真实投递记录。
    """
    paths = settings.paths
    db_path = paths.db_path
    assert "pytest" in str(db_path).lower() or "tmp" in str(db_path).lower(), (
        f"测试用的数据库路径看起来不在临时目录里：{db_path}\n"
        "这非常危险（可能写到真实投递记录上）。请检查 conftest 的隔离设置。"
    )
    assert os.environ.get("JH_DATA_DIR"), "JH_DATA_DIR 未被隔离，测试可能写到真实目录"
    return paths.root

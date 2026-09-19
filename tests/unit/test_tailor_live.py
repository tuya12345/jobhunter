"""P5 真实 LLM 抽样（`@pytest.mark.live`，**默认不跑**，只在 nightly / 手动跑）。

    pytest -m live tests/unit/test_tailor_live.py

★ 为什么这个文件必须存在、又必须**默认被排除**：

    CI 里不能有"调真模型再断言输出内容"的测试 —— 那测的是模型顺从性，
    模型换一次版本就可能红，而红的原因跟代码质量无关，
    最后一定会被加 skip 或被删掉（评审明确指出的坑）。
    但"真模型这条路到底走不走得通"也确实需要有人问一次 ——
    所以它放在这里：**只在 nightly 跑**，且断言宽松到"模型措辞变了也不会红"。

★ 断言只回答三个问题（与内容无关）：

    ① 调用能成功、JSON 能解析（提示词的 JSON 模式三件套有没有写对）
    ② 活着留下来的建议**再跑一次闸门仍然合格**（闸门不是只在 mock 下才有效）
    ③ 被拦下的每一条都**带着原因**（拦了却说不清理由，等于没拦）

★ 数据隔离：用的是 `test_tailor_svc.py` 里那份**全虚构**的假简历 + 假岗位，
  临时库（conftest 已把路径指向 tmp）。**绝不碰用户的真实简历与真实投递记录。**
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import select

from jobhunter.config.settings import AppSettings
from jobhunter.llm.deepseek import DeepSeekProvider
from jobhunter.models import Job, Resume
from jobhunter.services import tailor_svc
from jobhunter.services.resume_svc import compute_content_hash

from .test_tailor_svc import fake_content

pytestmark = pytest.mark.live


def _real_key() -> str:
    """取真实 key：环境变量 → 仓库根的 `.env`。

    ★ 为什么还要读 `.env` 文件：`tests/conftest.py` 会**故意**把 `.env` 指到
      不存在的文件、并清掉 `DEEPSEEK_API_KEY` 环境变量（防止测试碰到个人配置）。
      那是正确的隔离，但 live 测试恰恰需要真 key —— 所以它自己显式去读，
      读不到就 skip（而不是失败）。
    """
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("DEEPSEEK_API_KEY="):
                return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


@pytest.fixture
def live_settings(settings: AppSettings) -> AppSettings:
    key = _real_key()
    if not key:
        pytest.skip("没有真实 key（.env / 环境变量里都没有 DEEPSEEK_API_KEY）→ 跳过 live 抽样")
    return settings.model_copy(update={"deepseek_api_key": key})


@pytest.fixture
def live_env(session, live_settings: AppSettings):
    """一份全虚构的简历 + 岗位（与单测同一份 fixture，避免两套数据漂移）。"""
    content = fake_content()
    session.add(
        Resume(
            name="主简历",
            direction="Python 后端开发",
            content_json=content.model_dump_json(),
            content_hash=compute_content_hash(content),
            is_default=True,
        )
    )
    session.add(
        Job(
            platform="manual",
            company="某某科技",
            title="Python 后端开发实习生",
            city="深圳",
            jd_text=(
                "岗位职责：负责后端接口开发与维护。\n"
                "任职要求：熟悉 Python、FastAPI、MySQL；了解 Redis 缓存；"
                "有 Kubernetes 使用经验者优先。"
            ),
            jd_hash="k" * 64,
            status="scored",
            match_score=72,
            jd_keywords_json='["Python", "FastAPI", "MySQL", "Redis", "Kubernetes"]',
        )
    )
    session.commit()
    return session


def test_live_analyze_produces_suggestions_that_survive_the_guard(
    live_env, live_settings: AppSettings
) -> None:
    """真模型跑一次：能解析 + 留下来的建议再校验仍然合格 + 拦下的都带原因。"""
    job = live_env.scalars(select(Job)).one()
    provider = DeepSeekProvider(
        live_settings.deepseek_api_key,
        base_url=live_settings.deepseek_base_url,
        model=live_settings.deepseek_model,
    )

    result = tailor_svc.analyze(
        live_env, job, provider=provider, settings=live_settings, use_cache=False
    )

    # ① 解析成功（走到这里就说明 JSON 模式三件套与 schema 是对的）
    assert isinstance(result.analysis.match_analysis.score, int)
    assert 0 <= result.analysis.match_analysis.score <= 100

    # ② 留下来的建议必须真的过闸门（不是"我们相信它过了"）
    for suggestion in result.kept:
        report = tailor_svc.FidelityGuard.check(
            suggestion.original,
            suggestion.revised,
            resume=fake_content(),
            settings=live_settings,
        )
        assert report.passed, f"留下来的建议居然不过闸门：{report.summary()}"

    # ③ 被拦下的必须有原因（"拦了但不说为什么"等于没拦）
    for dropped in result.dropped:
        assert dropped.reasons, "被拦下却没有原因"

    # 打印一份抽样结论（nightly 日志里能直接看到"模型这次编了多少"）
    print(
        f"\n[live] 建议 {len(result.kept) + len(result.dropped)} 条 → "
        f"通过 {len(result.kept)} / 被闸门拦下 {len(result.dropped)}"
    )
    for dropped in result.dropped[:5]:
        print(f"  · {dropped.suggestion.section}: {'；'.join(dropped.reasons[:2])}")

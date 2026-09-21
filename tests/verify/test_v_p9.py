"""【P9 专项】开源收尾：`jh demo` 的隔离性 + 文档/合规自检的可复跑性。

设计文档【提示词 11】的验收标准与红线：
    「**demo 模式**：`jh demo` 用内置假数据 + **独立/内存库**跑通全流程（**绝不污染真库**）」
    「最后：合规与隐私自检 → 输出 COMPLIANCE.md」

这一组盯的就是那句"绝不污染真库"——它是**可证伪**的，所以必须真去证伪：
不是看它说用了临时库，而是**跑完之后去量真库有没有被动过**。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobhunter.cli.app import app
from jobhunter.config.paths import find_project_root

runner = CliRunner()


def _fingerprint(path: Path) -> str | None:
    """文件的 (大小, sha256)；不存在返回 None。"""
    if not path.is_file():
        return None
    data = path.read_bytes()
    return f"{len(data)}:{hashlib.sha256(data).hexdigest()}"


# ===========================================================================
# ① 绝不污染真库（本阶段最重要的一条）
# ===========================================================================
def test_demo_does_not_touch_the_configured_database(engine, settings, db_url) -> None:  # noqa: ARG001
    """★★ 依据：【提示词 11】「demo 用内置假数据 + **独立/内存库**跑通全流程（**绝不污染真库**）」
    + `demo_cmd` docstring「把 JH_DATA_DIR/OUTPUT_DIR/STATE_DIR 指到临时目录，**并断言**」。

    ★ 证伪方式：先把"真库"建出来并记下**逻辑状态**，跑完 `jh demo` 再量一次
      —— "它说用了临时库"不算证据，真库的**内容**没变才算。

    ⚠️ 为什么量逻辑状态而不是文件 sha256（我第一版就错在这）：SQLite 开了 WAL，
       fixture 自己写进去的东西会在后续连接时 **checkpoint** 进主库文件，
      于是"同尺寸不同 hash"完全可能只是 checkpoint —— 字节级指纹**分不清**
      "被写了演示数据"与"日志合并回主库"。所以这里改成问数据库：行数与公司名。
    """
    from sqlalchemy import text as sa_text

    from jobhunter.storage.db import create_db_engine, make_session_factory

    real_db = settings.paths.db_path
    assert real_db.is_file(), "前提：测试用的库应当已由 engine fixture 建好"

    def snapshot() -> dict[str, object]:
        eng = create_db_engine(db_url, ensure_directory=False)
        try:
            with make_session_factory(eng)() as fresh:
                return {
                    "resumes": fresh.execute(sa_text("SELECT COUNT(*) FROM resumes")).scalar_one(),
                    "jobs": fresh.execute(sa_text("SELECT COUNT(*) FROM jobs")).scalar_one(),
                    "companies": sorted(
                        row[0] or ""
                        for row in fresh.execute(sa_text("SELECT company FROM jobs")).all()
                    ),
                }
        finally:
            eng.dispose()

    before = snapshot()
    result = runner.invoke(app, ["demo"])
    assert result.exit_code == 0, f"jh demo 失败：\n{result.output}"
    after = snapshot()

    assert after == before, (
        "`jh demo` 改了配置里那个库的**内容**：\n"
        f"  跑之前：{before}\n  跑之后：{after}\n"
        "（'绝不污染真库'是这条能力的红线，不是口号）"
    )
    # 演示用的是"甲公司/乙公司"这类假数据；真库里出现它们就是污染
    assert not any("甲" in name or "乙" in name for name in after["companies"]), (  # type: ignore[union-attr]
        f"真库里出现了演示数据：{after['companies']}"
    )


def test_demo_restores_a_preset_data_dir(monkeypatch, tmp_path, engine) -> None:  # noqa: ARG001
    """★★ 依据：交付声明「`jh demo` 在用户预设过 `JH_DATA_DIR` 时会崩（`_restore` 一律 pop
    把用户配置弄丢）→ 已修」。

    对抗：**预设** JH_DATA_DIR 指向一个自定义目录，跑完 demo 后它**必须还是那个值**。
    把用户的配置 pop 掉比"忘了恢复"更糟：用户平时可能就靠这个变量把库放在加密盘/别的盘。
    """
    custom = tmp_path / "用户的加密盘"
    custom.mkdir()
    monkeypatch.setenv("JH_DATA_DIR", str(custom))
    monkeypatch.setenv("JH_LLM_PROVIDER", "mock")

    result = runner.invoke(app, ["demo"])
    assert result.exit_code == 0, f"预设 JH_DATA_DIR 后 demo 崩了：\n{result.output}"

    assert os.environ.get("JH_DATA_DIR") == str(custom), (
        "demo 跑完把用户预设的 JH_DATA_DIR 弄丢了："
        f"{os.environ.get('JH_DATA_DIR')!r}（应为 {str(custom)!r}）"
    )
    assert os.environ.get("JH_LLM_PROVIDER") == "mock", "demo 把用户预设的 provider 弄丢了"


def test_demo_never_reaches_the_network(monkeypatch, engine) -> None:  # noqa: ARG001
    """★★ 依据：【提示词 11】「demo 用内置假数据 + **独立/内存库**跑通全流程（**绝不污染真库**）」

    证伪方式：给一个**看起来配好了**的真 key，但把 base_url 指到一个**必然连不上**的地址
    （`127.0.0.1:9`，discard 端口）。如果 demo 真的去联网，它就会超时/报错；
    它能顺利跑完，就说明**根本没发请求**。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-" + "0" * 32)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("JH_LLM_PROVIDER", raising=False)

    result = runner.invoke(app, ["demo"])
    assert result.exit_code == 0, "配了真 key 之后 demo 去联网了（或崩了）：\n" + result.output


def test_demo_output_is_self_evidencing() -> None:
    """依据：交付声明「③ **输出即自证**（每步打印做了什么+关键数字，
    README 贴真实终端输出而不是假截图）」。

    11 步全流程都应当出现在输出里 —— 否则 README 里的"演示"就没有可对照的来源。
    """
    result = runner.invoke(app, ["demo"])
    assert result.exit_code == 0, result.output
    text = result.output
    # 全流程的关键节点（设计文档【提示词 11】：录入→打分→清单→开场白→打标→
    # 面试准备→复盘→导出）
    for marker in ("匹配", "开场白", "打标", "周报"):
        assert marker in text, f"演示输出里缺少环节：{marker}\n{text[:800]}"


def test_demo_keep_preserves_artifacts_and_second_run_does_not_touch_them(
    isolated_env,  # noqa: ARG001
) -> None:
    """★ 依据：交付声明 verify_hints ②「`jh demo --keep`：保留目录里应有 `data/jobhunter.db`
    与 `output/report-*.md`；**再跑一次不带 `--keep` 的 demo 不应影响它**」。

    第二条断言才是重点：`--keep` 的意义是"留着我回头翻"，
    如果下一次普通 demo 会把它清掉，那个选项就是假的。
    """
    tmp_root = Path(tempfile.gettempdir())
    before = {path.name for path in tmp_root.glob("jh-demo-*")}

    first = runner.invoke(app, ["demo", "--keep"])
    assert first.exit_code == 0, f"demo --keep 失败：\n{first.output}"

    created = {path.name for path in tmp_root.glob("jh-demo-*")} - before
    assert created, "`--keep` 之后没有留下任何演示目录"
    kept = tmp_root / sorted(created)[0]

    db_files = list(kept.rglob("*.db"))
    assert db_files, f"保留目录里没有演示库：{sorted(p.name for p in kept.rglob('*'))[:10]}"
    fp_before = _fingerprint(db_files[0])

    second = runner.invoke(app, ["demo"])  # 不带 --keep
    assert second.exit_code == 0, f"第二次 demo 失败：\n{second.output}"

    assert kept.is_dir(), "不带 --keep 的 demo 把上一次保留的目录删了"
    assert _fingerprint(db_files[0]) == fp_before, "第二次 demo 改了上一次保留的演示库"


def test_demo_removes_its_scratch_directory_by_default() -> None:
    """依据：`demo_cmd` 的 `--keep` help「保留演示库与产物（**默认用完即删**）」。"""
    tmp_root = Path(tempfile.gettempdir())
    before = {path.name for path in tmp_root.glob("jh-demo-*")}

    result = runner.invoke(app, ["demo"])
    assert result.exit_code == 0, result.output

    leftover = {path.name for path in tmp_root.glob("jh-demo-*")} - before
    assert not leftover, f"默认（不带 --keep）却留下了临时目录：{sorted(leftover)}"


# ===========================================================================
# ② 合规与隐私自检的可复跑性
# ===========================================================================
def _load_pii_samples() -> list[str]:
    """取"已知真实标识"的检测样本 —— **从仓库外取，绝不写在本文件里**。

    ★ 为什么必须这样（这是我自己的一个已登记缺陷，见 ISSUES #54）：
      这个函数的第一版把真名/学校/手机号**硬编码在断言里**当检测样本 ——
      于是"查隐私"的那行代码本身成了仓库里的一份隐私副本，
      和 #44（台账里复述 PII 当证据）是**同一个坑的第三次现身**。
      检测器不能携带它要检测的秘密。

    取法（按优先级）：
      ① 环境变量 `JH_PII_SAMPLES`：逗号分隔；
      ② 仓库内**已被 gitignore** 的本地文件 `state/pii-samples.txt`：一行一个。
    两者都没有时**跳过**（而不是假装通过）—— 跳过会在 `-rs` 里显式列出来。
    """
    raw = os.environ.get("JH_PII_SAMPLES", "")
    samples = [item.strip() for item in raw.split(",") if item.strip()]
    if samples:
        return samples

    local = find_project_root() / "state" / "pii-samples.txt"
    if local.is_file():
        return [
            line.strip()
            for line in local.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    return []


def _scan_paths_for_samples(
    root: Path, relatives: Iterable[str], samples: Sequence[str]
) -> list[str]:
    """纯函数：在给定文件里找样本串。抽出来是为了能被**元测试**直接验证它是活的。"""
    offenders: list[str] = []
    for relative in relatives:
        path = root / relative
        if path.suffix.lower() in {".db", ".db-wal", ".db-shm", ".pyc", ".docx", ".pdf", ".png"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        offenders.extend(
            f"{relative}：命中样本[{index}]" for index, s in enumerate(samples) if s in text
        )
    return offenders


def test_working_tree_has_no_real_identifiers() -> None:
    """★★ 依据：【提示词 11】第 3 条「**隐私检查（重点）**：全仓库扫描有无硬编码密钥、
    真实手机号/邮箱/姓名」+ COMPLIANCE.md 的声明「工作区 0 命中真实标识」。

    ★ 我用**自己配置的**已知值回查（不从生产脚本里抄结论）——
      ISSUES #47 记录过：早期版本的真名/学校在 `resume_svc.py` 与台账里出现过，
      所以这几个串是"知道曾经存在过"的检测样本。

    ★ 样本从环境变量/本地 gitignored 文件取，**不写在本文件里**（见 `_load_pii_samples` 的说明）。
      没配置样本时**跳过**，并在跳过原因里写清怎么配 —— 不静默变绿。
    """
    samples = _load_pii_samples()
    if not samples:
        pytest.skip(
            "未配置真实标识样本（跳过而非通过）："
            "设 JH_PII_SAMPLES='值1,值2' 或写 state/pii-samples.txt（该路径已 gitignore）"
        )

    root = find_project_root()
    # ★ 只扫**受 git 跟踪**的文件：`.env` 是用户本机的密钥文件，
    #   已 gitignore 且未跟踪 —— 它里面有真名不属于"仓库泄露"（我第一版把它算进来，误报）。
    offenders = _scan_paths_for_samples(root, _git_tracked_files(root), samples)
    assert not offenders, "受跟踪的文件里仍有真实身份标识：\n" + "\n".join(offenders)
    # ★ 报的是**位置**而不是命中内容：命中内容本身就是隐私，不该被打进 CI 日志。
    #   上面 f-string 只写 sample 的**序号**，不写值 —— 这是有意的。


def test_pii_scanner_is_actually_capable_of_firing(tmp_path: Path) -> None:
    """★★ **元测试**：证明上面那个扫描器**真的会响**，不是一个恒绿的空壳。

    ★ 为什么必须有这一条：这个套件里已经出现过三次"防线看起来在、实际测不到"的情况
      （#8 的 `if` 包裹、我自己的两处合规守卫误报/空过）。一个从不触发的隐私扫描
      比没有扫描更糟 —— 它给的是"扫过了，安全"的虚假结论。

    双向断言：
      ① 文件里**有**样本 → 必须命中（用合成样本，不含任何真实标识）；
      ② 文件里**没有**样本 → 必须零命中（防止它变成"见谁报谁"的误报机器）。
    """
    synthetic = "合成样本甲"
    planted = tmp_path / "planted.txt"
    planted.write_text(f"这一行含 {synthetic} 这个词\n", encoding="utf-8")
    clean = tmp_path / "clean.txt"
    clean.write_text("这一行什么都不含\n", encoding="utf-8")

    hits = _scan_paths_for_samples(tmp_path, ["planted.txt"], [synthetic])
    assert hits, "扫描器漏掉了明确植入的样本 —— 它是死的，隐私检查等于没做"

    misses = _scan_paths_for_samples(tmp_path, ["clean.txt"], [synthetic])
    assert not misses, f"扫描器对干净文件也报命中（误报机器）：{misses}"

    # 跳过语义也要真的是"跳过"而不是"通过"：没配样本时不得静默变绿
    assert _load_pii_samples() == [] or all(_load_pii_samples()), (
        "_load_pii_samples 要么返回非空样本，要么返回空列表（由用例 skip），不应含空串"
    )


def _git_tracked_files(root: Path) -> list[str]:
    import subprocess

    result = subprocess.run(  # noqa: S603
        ["git", "ls-files"],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def test_compliance_doc_exists_and_covers_the_required_checks() -> None:
    """★★ 依据：【提示词 11】明确列出合规自检要覆盖 6 件事：
    ① 所有"自动化操作"点逐条判断 ② 所有"必须人工"的动作 ③ 隐私检查 ④ 频率控制
    ⑤ 依赖与迁移检查 ⑥ README 合规声明。

    `docs/COMPLIANCE.md` 是本阶段的**交付物**（P0 起就在计划书里），
    所以它必须在，且六项都要有痕迹。
    """
    doc = find_project_root() / "docs" / "COMPLIANCE.md"
    assert doc.is_file(), "P9 的交付物 docs/COMPLIANCE.md 不存在"
    text = doc.read_text(encoding="utf-8")

    markers = {
        "自动化": ("自动", "投递"),
        "必须人工": ("人工",),
        "隐私": ("隐私",),
        "频率": ("频率", "延时"),
        "迁移/依赖": ("迁移", "Alembic"),
        "合规声明": ("合规",),
    }
    missing = [label for label, keys in markers.items() if not any(key in text for key in keys)]
    assert not missing, f"COMPLIANCE.md 缺少这些自检项：{missing}"
    # 已登记的、必须在公开前处理的事（#47）应当被写明
    assert "filter-repo" in text or "历史" in text, (
        "COMPLIANCE.md 没有提到 git 历史里仍有真实标识这件事（ISSUES #47 的公开前必办项）"
    )


def test_license_has_the_compliance_clause() -> None:
    """依据：【提示词 11】「LICENSE（建议 MIT + 一句"**不鼓励用于违反平台协议的行为**"）」。"""
    license_file = find_project_root() / "LICENSE"
    assert license_file.is_file()
    text = license_file.read_text(encoding="utf-8")
    assert "MIT" in text, "LICENSE 不是 MIT"
    # ★ 只认中文会误判：实际写法是英文 "do not encourage ... violates those terms"
    lowered = text.lower()
    assert (
        "平台协议" in text
        or "不鼓励" in text
        or "not encourage" in lowered
        or "violates" in lowered
    ), "LICENSE 里缺少『不鼓励用于违反平台协议的行为』那句"


@pytest.mark.parametrize(
    "relative",
    [
        "docs/COMPLIANCE.md",
        "docs/install.md",
        "docs/config.md",
        "docs/architecture.md",
        "docs/data_model.md",
        "docs/roadmap.md",
    ],
)
def test_declared_docs_exist_and_are_not_stubs(relative: str) -> None:
    """依据：交付声明列出的文档 + 【提示词 11】「docs/：安装教程、architecture.md、配置说明」。

    不只看存在：**空壳文档**（几百字节）等于没写 —— 计划书要求 install 教程
    「我没装过，写详细些」。
    """
    path = find_project_root() / relative
    assert path.is_file(), f"缺失交付文档：{relative}"
    text = path.read_text(encoding="utf-8")
    assert len(text) > 1500, f"{relative} 只有 {len(text)} 字符，像是空壳"

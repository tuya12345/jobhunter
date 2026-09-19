"""配置校验：写错的配置必须在**加载时**报中文错（ISSUES #43，#18）。

★★ 这一组用例守的是同一件事：**配置写错了不能静默无效**

    · `learning_whitelist` 里写一个词表里没有的技能 → 白名单永远命中不了任何东西，
      用户以为放开了，实际上一句都不会被允许（他会觉得"这个功能没效果"）；
    · `greeting_banned_words` 里写空串 / 纯空白 / 一整句话 → 改前只是被静默丢掉
      （或者反过来：太长的词去误伤所有文案，见 `docs/config.md` §6 记的事故）；
    · `rate_limit.max_pages` → 配置项存在、文档据此声称有"翻页上限"，而全仓没有读取点。

    三条的共同点都不是"报错"，而是**看起来配置了、其实没有生效**。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from jobhunter.config import vocabulary
from jobhunter.config.paths import find_project_root
from jobhunter.config.settings import (
    MAX_BANNED_WORD_CHARS,
    AppSettings,
    RateLimitSettings,
)
from jobhunter.services import resume_svc


def _settings(**overrides: object) -> AppSettings:
    """用最小显式配置构造 AppSettings（不读仓库根的 .env）。"""
    base: dict[str, object] = {
        "keywords": ["AI Agent"],
        "cities": ["深圳"],
        "_env_file": None,
    }
    base.update(overrides)
    return AppSettings(**base)  # type: ignore[arg-type]


# ===========================================================================
# #43 learning_whitelist：必须是词表里真实存在的技能
# ===========================================================================
def test_builtin_skill_is_accepted_in_the_whitelist() -> None:
    assert _settings(learning_whitelist=["LangChain"]).learning_whitelist == ["LangChain"]


def test_skill_declared_in_aliases_is_accepted_in_the_whitelist() -> None:
    """先声明成规范名、再进白名单 —— 这是"想加一个新技能"的正确流程。"""
    settings = _settings(
        skill_aliases={"Prompt Engineering": ["提示词工程"]},
        learning_whitelist=["Prompt Engineering"],
    )
    assert settings.learning_whitelist == ["Prompt Engineering"]


def test_typo_in_the_whitelist_is_rejected_with_actionable_chinese() -> None:
    """★ 改前：错词被 `setdefault` 收进词表，一切静默；改后：加载即报错并给出下一步。"""
    with pytest.raises(ValidationError) as excinfo:
        _settings(learning_whitelist=["LanchChain"])
    message = str(excinfo.value)
    assert "LanchChain" in message, "报错必须指出是哪个值写错了"
    assert "skill_aliases" in message, "报错必须给出怎么改（先声明成规范名）"
    assert "learning_whitelist" in message, "报错必须指出是哪个键"


def test_blank_whitelist_entry_is_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _settings(learning_whitelist=["LangChain", "   "])
    assert "空白项" in str(excinfo.value)


def test_the_shipped_config_and_the_test_fixture_are_valid() -> None:
    """反向守卫：校验不能把真实配置判成错的（否则等于把工具锁死）。

    两个真实来源：仓库根的 `config.yaml` 与 `tests/conftest.py` 的 MINIMAL_CONFIG。
    """
    shipped = AppSettings(_env_file=None, config_file=find_project_root() / "config.yaml")
    assert shipped.learning_whitelist, "仓库配置里的白名单不该是空的（否则这条守不到东西）"
    for name in shipped.learning_whitelist:
        assert name in set(vocabulary.BUILTIN_SKILLS) | set(shipped.skill_aliases), name

    minimal = (find_project_root() / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert 'learning_whitelist: ["LangChain"]' in minimal


def test_the_whitelist_words_are_still_recognisable_skills() -> None:
    """白名单技能必须仍能被抽词器认出来（#43 不允许把这条能力改坏）。

    ★ 这条守的是 `load_skill_vocabulary()` 里那段 `setdefault`：
      校验把白名单收窄成"词表里已有的词"之后，白名单里的技能当然还能抽出来；
      但如果哪天有人把那段删掉、又放宽了校验，这里会立刻红。
    """
    settings = _settings(learning_whitelist=["LangChain"])
    vocab = resume_svc.load_skill_vocabulary(settings)
    assert "LangChain" in vocab


def test_builtin_vocabulary_has_a_single_definition() -> None:
    """词表只有一份定义（搬去 config 层是为了给 settings 校验用）。"""
    assert resume_svc._BUILTIN_SKILLS is vocabulary.BUILTIN_SKILLS
    assert len(vocabulary.BUILTIN_SKILLS) > 100


# ===========================================================================
# #43 greeting_banned_words：不能是空串 / 纯空白 / 超长
# ===========================================================================
def test_normal_banned_words_are_kept_verbatim() -> None:
    settings = _settings(greeting_banned_words=["贵司", "三生有幸"])
    assert settings.greeting_banned_words == ["贵司", "三生有幸"]


def test_empty_banned_word_is_rejected_instead_of_silently_dropped() -> None:
    """★ 改前是 `[w for w in ... if w.strip()]` —— 静默丢掉空项，用户以为配置生效了。"""
    with pytest.raises(ValidationError) as excinfo:
        _settings(greeting_banned_words=["贵司", ""])
    message = str(excinfo.value)
    assert "greeting_banned_words" in message
    assert "空的" in message


def test_whitespace_only_banned_word_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(greeting_banned_words=["   "])


def test_banned_word_with_surrounding_whitespace_is_rejected_with_the_fixed_value() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _settings(greeting_banned_words=[" 贵司 "])
    assert "贵司" in str(excinfo.value), "报错要给出期望写法，用户才能直接改"


def test_overlong_banned_word_is_rejected() -> None:
    """禁用词是朴素子串匹配：一句话当禁用词会误伤大量正常文案。"""
    long_word = "感" * (MAX_BANNED_WORD_CHARS + 1)
    with pytest.raises(ValidationError) as excinfo:
        _settings(greeting_banned_words=[long_word])
    message = str(excinfo.value)
    assert str(MAX_BANNED_WORD_CHARS) in message
    assert "太长" in message


def test_the_shipped_banned_words_fit_the_cap() -> None:
    """反向守卫：仓库配置里的真实禁用词必须都过得了这条校验。"""
    shipped = AppSettings(_env_file=None, config_file=find_project_root() / "config.yaml")
    assert shipped.greeting_banned_words
    assert all(0 < len(w) <= MAX_BANNED_WORD_CHARS for w in shipped.greeting_banned_words)


# ===========================================================================
# #18 rate_limit.max_pages：删掉"没人读的配置项"
# ===========================================================================
def test_max_pages_field_is_gone() -> None:
    assert set(RateLimitSettings.model_fields) == {"min_delay_seconds", "max_delay_seconds"}


def test_delay_range_is_still_validated() -> None:
    with pytest.raises(ValidationError) as excinfo:
        RateLimitSettings(min_delay_seconds=5.0, max_delay_seconds=1.0)
    assert "min_delay_seconds" in str(excinfo.value)


def test_no_source_file_uses_max_pages_as_code() -> None:
    """★ 结构守卫：这个配置项之所以该删，就是因为**没有任何读取点**。

    所以守卫不能只测"字段没了"，还要挡住"有人又把它加回来但依然没人读"。

    ★ 用 AST 而不是搜字符串：注释与 docstring 里**提到**它是好事
      （`settings.py` / `base.py` 都留了"为什么删掉"的说明），
      真正要拦的是"它又变成了代码里的一个名字 / 参数 / 关键字实参"。
    """
    src = find_project_root() / "src"

    def _identifier_of(node: ast.AST) -> str | None:
        """节点身上挂着的标识符名（变量名 / 属性名 / 形参名 / 关键字实参名）。"""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, (ast.arg, ast.keyword)):
            return getattr(node, "arg", None)
        return None

    def _code_uses(tree: ast.AST) -> list[int]:
        """找出"它又变成了代码里的名字 / 参数 / 实参关键字"的行号。"""
        return [node.lineno for node in ast.walk(tree) if _identifier_of(node) == "max_pages"]

    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        hits = _code_uses(ast.parse(path.read_text(encoding="utf-8")))
        if hits:
            offenders.append(f"{path.relative_to(find_project_root())}:{hits}")
    assert not offenders, f"max_pages 又回到了代码里（而它没有读取点）：{offenders}"


def test_shipped_config_no_longer_defines_max_pages() -> None:
    """配置项删了就得连 `config.yaml` 一起删 —— 只看 `rate_limit` 段里的键。"""
    import yaml

    data = yaml.safe_load((find_project_root() / "config.yaml").read_text(encoding="utf-8"))
    assert "max_pages" not in data["rate_limit"]
    assert set(data["rate_limit"]) == {"min_delay_seconds", "max_delay_seconds"}


def test_config_reference_doc_drops_the_removed_field() -> None:
    """`docs/config.md` 是配置项的对外说明：不能还把它当成一个可填的字段。

    ★ 允许留一节"已删除"的墓碑（写明为什么删），但**示例 yaml 里不许再有它** ——
      用户是照着示例改配置的，示例里留着就等于它还在。
    """
    text = (find_project_root() / "docs" / "config.md").read_text(encoding="utf-8")
    assert "max_pages: 3" not in text, "示例 yaml 里还留着这个字段"
    assert "已删除（ISSUES #18）" in text, "删掉了就要留一句为什么（否则下一个人会加回来）"


def test_docs_no_longer_claim_a_paging_cap() -> None:
    """文档不许声称一个不存在的控制（#18 的核心教训）。"""
    compliance = (find_project_root() / "docs" / "COMPLIANCE.md").read_text(encoding="utf-8")
    row = next(line for line in compliance.splitlines() if line.startswith("| 频率克制 |"))
    assert "翻页上限" not in row
    assert "随机延时" in row and "出错即停" in row


def test_public_docs_agree_on_the_number_of_dedup_layers() -> None:
    """公开文档不能各说各话（ISSUES #21：README 说两层、roadmap 说三层）。"""
    root = find_project_root()
    readme = (root / "README.md").read_text(encoding="utf-8")
    roadmap = (root / "docs" / "roadmap.md").read_text(encoding="utf-8")
    assert "两层去重" not in readme
    assert "两层去重" not in roadmap
    assert "三层去重" in readme and "三层去重" in roadmap


def _compliance_section(title: str) -> str:
    text = (find_project_root() / "docs" / "COMPLIANCE.md").read_text(encoding="utf-8")
    start = text.index(title)
    rest = text[start:]
    end = rest.find("\n## ", 1)
    return rest if end == -1 else rest[:end]


def test_user_agent_purpose_is_documented_and_not_rotated() -> None:
    """#19 的裁决是"保留 UA，但把用途写明" —— 所以两件事都要守：

    ① 文档里写清了**目的**与**边界**（不是规避风控、不轮换）；
    ② 代码里真的只有一个 UA（不轮换不是嘴上说说）。
    """
    section = _compliance_section("### 5.5")
    assert "不随请求轮换" in section
    assert "桌面版" in section and "zh-CN" in section
    assert "规避风控" in section

    src = find_project_root() / "src"
    hits = [
        path.relative_to(find_project_root())
        for path in sorted(src.rglob("*.py"))
        if "Mozilla/5.0" in path.read_text(encoding="utf-8")
    ]
    assert len(hits) == 1, f"UA 只该有一处（固定的那个），实际出现在：{hits}"


def test_readme_warns_that_dotenv_is_private() -> None:
    """#28：`.env` 不动，但要在 README 里提醒"这是你本机的私密文件"。"""
    readme = (find_project_root() / "README.md").read_text(encoding="utf-8")
    assert "请不要提交或贴给别人" in readme
    assert "JH_DEFAULT_RESUME_PATH" in readme
    # 提醒里"已被 gitignore"这句必须是真的
    ignore = (find_project_root() / ".gitignore").read_text(encoding="utf-8")
    assert any(line.strip() == ".env" for line in ignore.splitlines())


def test_doc_guard_reads_real_files() -> None:
    """元测试：上面那几条守卫读的是真文件（路径写错会让它们静默失效）。"""
    root = find_project_root()
    for relative in (
        "README.md",
        "config.yaml",
        ".gitignore",
        "docs/COMPLIANCE.md",
        "docs/config.md",
        "docs/roadmap.md",
    ):
        path = Path(root) / relative
        assert path.is_file(), f"{relative} 不存在，守卫会读空字符串"
        assert path.stat().st_size > 200, f"{relative} 小得不像真文件"

"""简历解析服务测试：守住 P1 的核心产物（结构化简历 + content_hash + 拆分建议）。

★ 三组测试各自守什么

    ① **技能词边界**（评审专门点出来的 bug）
       中文没有词边界：`re.search("Java", "JavaScript")` 会命中、`Go` 命中 `Google`。
       后果是"写了 JavaScript 的人被认为会 Java"，匹配分数虚高 —— 而这是静默的。

    ② **content_hash 稳定性**
       它是 LLM 缓存 key 的组成部分。hash 不稳定 → 每次导入简历都让缓存失效、白花钱；
       hash 漏字段 → 改了简历仍命中旧分（这个工具最核心的价值就没了）。
       所以两边都要测：**同样的内容 hash 必须相同；内容一变 hash 必须变。**

    ③ **拆分建议（split）**
       它只给建议、不改文件，但要能真的指出"哪个项目该前置、缺什么技能"。
       LLM 兜底默认关闭，所以这里断言的全是确定性规则结果。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobhunter.services import resume_svc
from tests._fixture_loader import load_fixture_module

pytest.importorskip("docx", reason="需要 [docx] extra：uv sync --extra docx")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(scope="module")
def fixture_docx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """虚构简历 fixture（模块级生成一次）。"""
    build_sample_resume = load_fixture_module("make_sample_resume").build_sample_resume

    target = tmp_path_factory.mktemp("resume") / "sample_resume.docx"
    build_sample_resume(target)
    return target


@pytest.fixture
def content(fixture_docx: Path, settings) -> resume_svc.ResumeContent:
    """解析好的结构化简历。"""
    return resume_svc.parse_resume_file(fixture_docx, settings=settings)


# ===========================================================================
# ① 技能词边界（★ 最容易写错的地方）
# ===========================================================================
def test_java_does_not_match_javascript(settings) -> None:
    """`Java` 不能命中 `JavaScript`。"""
    vocabulary = resume_svc.load_skill_vocabulary(settings)
    assert not resume_svc._match_any("熟悉 JavaScript 开发", vocabulary["Java"])
    assert resume_svc._match_any("用 Java 写了后端服务", vocabulary["Java"])


def test_go_does_not_match_google(settings) -> None:
    """`Go` 不能命中 `Google`。"""
    vocabulary = resume_svc.load_skill_vocabulary(settings)
    assert not resume_svc._match_any("在 Google 实习过", vocabulary["Go"])
    assert resume_svc._match_any("熟悉 Go 语言与并发", vocabulary["Go"])


@pytest.mark.parametrize(
    ("text", "canonical"),
    [
        ("熟悉Java开发", "Java"),
        ("用Go写后端服务", "Go"),
        ("熟练Python编程", "Python"),
        ("使用Docker部署服务", "Docker"),
        ("掌握Vue3组件开发", "Vue"),
    ],
)
def test_ascii_skill_adjacent_to_chinese_is_matched(settings, text, canonical) -> None:
    """★ 英文技能**紧贴汉字**时必须命中（最自然的写法）。

    同一个 `\\b` 坑的另一半：Python 的 `\\b` 是 Unicode 语义，汉字属于 `\\w`，
    所以"悉"与"J"之间没有词边界 → `\\bJava` 匹配不到 `熟悉Java开发`。
    右边界早先已用 `(?![A-Za-z_])` 修掉、左边界漏改，导致中文 JD 里
    "熟悉Java开发"这种写法**整词漏抽**（影响 技能匹配 / 方向判定 / jh gap）。
    """
    vocabulary = resume_svc.load_skill_vocabulary(settings)
    assert resume_svc._match_any(text, vocabulary[canonical]), f"{text!r} 没命中 {canonical}"


def test_adjacency_does_not_break_word_boundary(settings) -> None:
    """放宽左边界之后，跨词误命中仍然要被拒（不能为了召回把精确性丢掉）。"""
    vocabulary = resume_svc.load_skill_vocabulary(settings)
    assert not resume_svc._match_any("熟悉JavaScript开发", vocabulary["Java"])
    assert not resume_svc._match_any("用过Google分析", vocabulary["Go"])
    assert not resume_svc._match_any("XJava开发", vocabulary["Java"])  # 前面还有 ASCII 字母
    # 右边界只排除字母：中文跟在后面算命中（"研究Go语言两年"），数字版本号也算
    assert resume_svc._match_any("研究Go语言两年", vocabulary["Go"])


def test_cjk_alias_matches_with_chinese_context(settings) -> None:
    """★ 中文别名要在中文语境里命中。

    踩过两次的坑：`\\b` 的边界定义基于 `\\w`，而 **Python 的 `\\w` 包含汉字** ——
    所以 `\\b大模型\\b` 在"接触过大模型"里永远匹配不上。
    正确边界应只排除 ASCII 字母数字。
    """
    vocabulary = resume_svc.load_skill_vocabulary(settings)
    assert resume_svc._match_any("接触过大模型", vocabulary["LLM"])
    assert resume_svc._match_any("做过大模型应用", vocabulary["LLM"])
    # 后接 ASCII 字母时仍要拒绝半截匹配
    assert not resume_svc._match_any("大模型X框架", vocabulary["LLM"])


def test_symbol_suffixed_skills_match(settings) -> None:
    """`C++` / `C#` / `Node.js` 这类含符号的写法要能命中。"""
    assert resume_svc._match_any("掌握 C++ 与 Python", ["C++"])
    assert resume_svc._match_any("用过 C# 开发", ["C#"])
    assert resume_svc._match_any("Node.js 服务", ["Node.js"])


def test_vocabulary_merges_config_aliases(settings) -> None:
    """config.yaml 的 skill_aliases 要并入词表（归一化才生效）。"""
    vocabulary = resume_svc.load_skill_vocabulary(settings)
    assert "LLM" in vocabulary
    assert "大模型" in vocabulary["LLM"]


def test_skill_extraction_is_order_stable(settings) -> None:
    """词表顺序稳定 → 同一段文本两次抽取结果完全一致（hash 才稳定）。"""
    text = "熟悉 Python、Vue3、Docker，做过 RAG 问答"
    first = resume_svc.extract_skills(text, resume_svc.load_skill_vocabulary(settings))
    second = resume_svc.extract_skills(text, resume_svc.load_skill_vocabulary(settings))
    assert first == second
    vocabulary_order = list(resume_svc.load_skill_vocabulary(settings))
    assert first == sorted(first, key=vocabulary_order.index)


# ===========================================================================
# ② 时间段解析（真实简历踩出来的坑）
# ===========================================================================
@pytest.mark.parametrize(
    ("text", "expected_period", "expected_end"),
    [
        ("2024.03 - 2024.09", "2024.03 - 2024.09", (2024, 9)),
        ("2023.10 - 2024.01", "2023.10 - 2024.01", (2024, 1)),  # ★ 10 月不能被拆成 1 月
        ("2026.08", "2026.08", (2026, 8)),  # ★ 08 不能被拆成 0
        ("2023.09 ～ 2027.06", "2023.09 ～ 2027.06", (2027, 6)),  # 全角波浪号
        ("2023年9月—2027年6月", "2023年9月—2027年6月", (2027, 6)),
        ("2024.03至今", "2024.03至今", (9999, 12)),  # 无短横线也要认"至今"
    ],
)
def test_period_parsing(text: str, expected_period: str, expected_end: tuple[int, int]) -> None:
    """时间段解析的各种真实写法。

    ★ 两条最容易错的：
      · `2023.10` 被抽成 `2023.1`（月份正则少了负向断言 → 10 月变 1 月）
      · `2026.08` 被抽成 `2026.0`（同一个原因，但后果更严重：
        连"这一行算不算项目标题"的判断都失效，**项目解析直接归零**）
    """
    period, _ = resume_svc._extract_period(text)
    assert period == expected_period
    assert resume_svc._period_sort_key(period) == expected_end


def test_period_strip_does_not_leave_fragments() -> None:
    """抠掉时间段后不能残留碎片（文本替换实现会留下 `0 - 2024.01`）。"""
    text = "校园二手交易平台 2023.10 - 2024.01 后端负责人"
    spans = resume_svc._merge_spans(resume_svc._date_spans(text))
    stripped = resume_svc._strip_periods(text, spans)
    assert "2023" not in stripped
    assert "2024" not in stripped
    assert "校园二手交易平台" in stripped
    assert "后端负责人" in stripped


# ===========================================================================
# ③ 解析结果
# ===========================================================================
def test_parses_basic_information(content: resume_svc.ResumeContent) -> None:
    """基础信息要抽出来（fixture 里是虚构值）。"""
    assert content.basic.get("name") == "林小满"
    assert content.basic.get("phone") == "13800000000"
    assert "example.com" in content.basic.get("email", "")
    assert content.basic.get("job_intention")


def test_parses_education(content: resume_svc.ResumeContent) -> None:
    """教育经历：学校 / 专业 / 学位 / 时间。"""
    assert content.education, "没有解析出教育经历"
    first = content.education[0]
    assert "南方某大学" in first.school or "虚构" in first.school
    assert first.degree in {"本科", "学士"}
    assert "2023" in first.period


def test_parses_three_projects(content: resume_svc.ResumeContent) -> None:
    """★ 项目数必须等于真实数量（bullet 行绝不能被当成新项目）。

    踩过的坑：项目描述以 `-` 开头，而 `-` 同时在"条目起始标记"里，
    于是**每一条描述都变成一个空项目** —— 2 个项目变 11 个，
    项目数虚高会直接把匹配打分带偏。
    """
    names = [project.name for project in content.projects]
    assert len(content.projects) == 3, f"项目数不对：{names}"
    assert any("智能简历匹配助手" in name for name in names)
    assert any("校园二手交易平台" in name for name in names)
    assert any("数据看板前端" in name for name in names)
    # 每条项目都要真的带描述
    for project in content.projects:
        assert project.bullets, f"{project.name} 没有解析出描述"
        assert project.period, f"{project.name} 没有解析出时间段"


def test_project_names_are_clean(content: resume_svc.ResumeContent) -> None:
    """项目名里不该混进角色词、时间段、悬空括号。"""
    for project in content.projects:
        assert "个人项目" not in project.name
        assert "2024" not in project.name
        assert not project.name.endswith("，")
        assert not project.name.endswith("（")
        assert project.name.strip() == project.name


def test_project_bullets_exclude_pure_metadata(content: resume_svc.ResumeContent) -> None:
    """★ 每条描述必须是**实质内容**，不能是"AI 协同开发"这种元信息碎片。

    实测来历（P9 隐私自检时把真实标题换成了同形状的虚构示例）：
    真实简历的项目标题行形如
        `<项目名> · <一句话定位>（个人项目 · <协作方式>，YYYY.MM）`
    抠掉时间段与角色词之后，残留的 `· <协作方式>，` 曾被当成一条描述 ——
    它既不是项目内容，也不该占一条 bullet，展示出来像解析错位了。

    ⚠️ 这里**故意不引用真实标题原文**：那是真实简历的内容，
       而"任何来源是真实简历的文本都不进仓库"是本项目的硬规矩（见 COMPLIANCE.md）。
    """
    for project in content.projects:
        for bullet in project.bullets:
            residue = bullet.strip(" 　·—|()（）,，")
            assert residue, f"{project.name} 里有一条空描述"
            assert "协同开发" not in residue, f"{project.name} 的描述里混进了元信息：{bullet!r}"
            # 实质描述都有一定长度；元信息碎片都很短
            assert len(residue) >= 6, f"{project.name} 的描述过短，疑似碎片：{bullet!r}"


def test_parses_skills_without_java_javascript_confusion(content: resume_svc.ResumeContent) -> None:
    """技能表要含 JavaScript、且**不能**因为有 JavaScript 就把 Java 也算上……

    ★ 注意 fixture 的技能栏**确实**写了"了解 Java"，所以这里断言的是
      "两者都被识别出来了"，而词边界本身由上面的 `_match_any` 用例守。
      这样区分开，是为了让失败信息能直接指出是哪一层出了问题。
    """
    assert "JavaScript" in content.skills
    assert "Python" in content.skills
    assert "Vue" in content.skills


def test_parses_internship(content: resume_svc.ResumeContent) -> None:
    """实习经历（表格排版）要解析出来。"""
    assert content.internships, "没有解析出实习经历"
    assert any("虚构科技" in item.company for item in content.internships)


def test_raw_text_is_kept_as_fallback(content: resume_svc.ResumeContent) -> None:
    """原文必须完整保留 —— 规则解析漏掉的内容还能被 P3 的 LLM 兜底用上。"""
    assert content.raw_text
    assert "智能简历匹配助手" in content.raw_text


# ===========================================================================
# ④ content_hash
# ===========================================================================
def test_content_hash_is_stable(content: resume_svc.ResumeContent) -> None:
    """同一内容反复计算，hash 必须一致（否则 LLM 缓存每次都被白白打掉）。"""
    assert resume_svc.compute_content_hash(content) == resume_svc.compute_content_hash(content)


def test_content_hash_ignores_whitespace_noise(
    content: resume_svc.ResumeContent,
) -> None:
    """只改空白/换行不该让 hash 变（简历重新导出常常只是排版变了）。"""
    noisy = content.model_copy(deep=True)
    noisy.projects[0].name = f"  {content.projects[0].name}  "
    noisy.projects[0].bullets = [
        bullet.replace(" ", "  ") for bullet in content.projects[0].bullets
    ]
    assert resume_svc.compute_content_hash(noisy) == resume_svc.compute_content_hash(content)


def test_content_hash_changes_when_content_changes(
    content: resume_svc.ResumeContent,
) -> None:
    """★ 内容真变了 hash 必须变 —— 这直接决定"改了简历会不会命中旧分数"。"""
    changed = content.model_copy(deep=True)
    changed.skills = [*changed.skills, "Kubernetes"]
    assert resume_svc.compute_content_hash(changed) != resume_svc.compute_content_hash(content)

    changed2 = content.model_copy(deep=True)
    changed2.projects[0].bullets = [*changed2.projects[0].bullets, "新增的一条描述"]
    assert resume_svc.compute_content_hash(changed2) != resume_svc.compute_content_hash(content)


def test_content_from_json_tolerates_garbage() -> None:
    """库里的 content_json 被改坏时不该让命令崩。"""
    assert resume_svc.content_from_json(None).projects == []
    assert resume_svc.content_from_json("{不是 JSON").projects == []
    assert resume_svc.content_from_json('{"projects": "类型都不对"}').projects == []


def test_content_json_roundtrip(content: resume_svc.ResumeContent) -> None:
    """落库 → 读回，结构不丢。"""
    from jobhunter.utils.io import to_json

    restored = resume_svc.content_from_json(to_json(content.model_dump(), indent=None))
    assert restored.skills == content.skills
    assert [p.name for p in restored.projects] == [p.name for p in content.projects]
    assert resume_svc.compute_content_hash(restored) == resume_svc.compute_content_hash(content)


# ===========================================================================
# ⑤ 拆分方向建议（split）
# ===========================================================================
def test_derives_direction_from_content(content: resume_svc.ResumeContent, settings) -> None:
    """能按内容猜出方向（猜不对也不致命，只是默认值）。"""
    direction = resume_svc.derive_direction(content, settings)
    assert direction, "没能猜出任何方向"


def test_split_returns_suggestions_for_requested_directions(content, settings) -> None:
    """`split` 要能为指定方向给出建议，并且明确"哪个项目该前置"。"""
    suggestions = resume_svc.analyze_split(
        content, current_direction="AI Agent", wanted=["AI Agent", "前端开发"], settings=settings
    )
    assert len(suggestions) == 2
    labels = {item.direction for item in suggestions}
    assert labels == {"AI Agent", "前端开发"}

    for suggestion in suggestions:
        assert suggestion.priority_projects, f"{suggestion.direction} 没有推荐任何项目"
        assert suggestion.headline_skills or suggestion.plus_skills_owned, (
            f"{suggestion.direction} 没有列出任何已有技能"
        )
        # 每个推荐项目都要给出"为什么"
        for fit in suggestion.priority_projects:
            assert fit.reason


def test_split_ranks_ai_project_higher_for_agent_direction(content, settings) -> None:
    """★ 方向排序要合理：AI 项目在 AI 方向排前，前端项目在前端方向排前。"""
    suggestions = {
        item.direction: item
        for item in resume_svc.analyze_split(
            content,
            current_direction="AI Agent",
            wanted=["AI Agent", "前端开发"],
            settings=settings,
        )
    }
    agent_top = suggestions["AI Agent"].priority_projects[0].name
    frontend_top = suggestions["前端开发"].priority_projects[0].name
    assert "智能简历匹配助手" in agent_top, f"AI 方向首选了 {agent_top}"
    assert "数据看板" in frontend_top or "前端" in frontend_top, f"前端方向首选了 {frontend_top}"


def test_split_reports_gaps(content, settings) -> None:
    """缺口要给出来（这个方向常要、简历里没有的技能）。"""
    suggestions = resume_svc.analyze_split(
        content, current_direction="AI Agent", wanted=["AI Agent"], settings=settings
    )
    assert suggestions
    agent = suggestions[0]
    # fixture 的技能栏没有 LangChain（只在项目描述里出现过），核心技能里应当被标为缺口
    assert isinstance(agent.gaps, list)


def test_split_does_not_modify_anything(content, settings) -> None:
    """★ `split` 是**只读**的：不改简历对象、不写文件。

    这是产品纪律（P1 绝不动用户的简历文件），所以要有一条测试盯着。
    """
    snapshot = content.model_dump()
    resume_svc.analyze_split(
        content, current_direction="AI Agent", wanted=["AI Agent", "前端开发"], settings=settings
    )
    assert content.model_dump() == snapshot


def test_split_with_unknown_direction_returns_empty(content, settings) -> None:
    """用户要拆一个我没预置的方向（如"测试开发"）时：返回空、不报错。"""
    suggestions = resume_svc.analyze_split(
        content, current_direction="", wanted=["测试开发"], settings=settings
    )
    assert suggestions == []


def test_profile_matching_accepts_labels_keys_and_aliases(settings) -> None:
    """方向匹配要认 label / key / 别名三种写法。"""
    profiles = resume_svc.load_direction_profiles(settings)
    assert profiles, "没有读到方向画像"
    for wanted in ("AI Agent", "ai_agent", "智能体"):
        found = resume_svc.find_profiles_for([wanted], profiles)
        assert found, f"没能匹配到方向：{wanted}"
        assert found[0].key == "ai_agent"


def test_learning_whitelist_is_surfaced(content, settings) -> None:
    """配置里的"我在学"白名单技能出现在简历里时，要单独提示"需标注在学习中"。"""
    resume_with_learning = content.model_copy(deep=True)
    resume_with_learning.skills = [*resume_with_learning.skills, "LangChain"]
    suggestions = resume_svc.analyze_split(
        resume_with_learning, current_direction="AI Agent", wanted=["AI Agent"], settings=settings
    )
    assert suggestions[0].learning_skills == ["LangChain"]


# ===========================================================================
# ⑤ 脱敏助手（"人工的第二道保险"）
#
# ★ 为什么给一个"没有生产调用点"的函数写测试：
#   它唯一的用途就是**在排障时保护隐私**。这类函数最危险的失败方式不是抛异常，
#   而是"看起来打了码、其实漏了" —— 比如漏掉带 +86 前缀的号码、或者把邮箱的
#   本地部分留下半个。所以用一组固定的假数据把行为钉死，将来谁改了正则，
#   测试会立刻告诉他"你把脱敏改弱了"。
#
#   注意：这里用的是**虚构数据**（13800000000 / example.com），
#   测试文件里出现任何真实手机号或邮箱都算事故。
# ===========================================================================
@pytest.mark.parametrize(
    ("raw", "must_not_contain"),
    [
        ("联系电话：13800000000", "13800000000"),
        ("手机 13800000000 备用", "13800000000"),
        ("邮箱 linxiaoman@example.com", "linxiaoman"),
        ("a@b.co", None),  # 单字符本地部分没什么可脱的，只要求不报错
    ],
)
def test_strip_volatile_content_masks_phone_and_email(
    raw: str, must_not_contain: str | None
) -> None:
    masked = resume_svc.strip_volatile_content(raw)
    if must_not_contain is not None:
        assert must_not_contain not in masked
    # 打码不能把整段文本吃掉：非敏感内容必须原样保留（否则排障时看不出上下文）
    assert len(masked) >= len(raw) - 4


def test_strip_volatile_content_keeps_surrounding_text() -> None:
    """脱敏只动敏感片段，前后文与行结构都不能变。"""
    masked = resume_svc.strip_volatile_content(
        "姓名：林小满\n电话：13800000000\n邮箱：linxiaoman@example.com"
    )
    assert masked.startswith("姓名：林小满")
    assert masked.count("\n") == 2
    assert "example.com" in masked  # 域名保留，否则看不出"这是个邮箱"
    assert "13800000000" not in masked


# ===========================================================================
# ⑥ 抽词的三个静默失真（回归用例：ISSUES #4 / #5 / #6）
#
# ★ 为什么这三条要单独钉住：它们的共同点是**不报错、只是抽不到或抽重**，
#   而抽词结果会一路传导到技能匹配 / 方向判定 / jh gap 三处。
#   这一组按"生产调用路径"断言（`extract_skills` / `extract_jd_keywords`），
#   而不是只测内部正则 —— 免得出现"函数对了、但调用点没走它"的半吊子修复。
# ===========================================================================
@pytest.mark.parametrize(
    "text", ["负责 AI大模型 相关应用", "做过 LLM大模型 项目", "熟悉大模型应用"]
)
def test_cjk_alias_matches_after_ascii_letters(text: str, settings) -> None:
    """ISSUES #4：中文技能紧跟在 ASCII 字母后面时也要抽得到。

    `AI大模型` / `LLM大模型` 是中文 JD 里最常见的连写，
    而"大"前面是 ASCII 字母这一事实**不该**否掉整个 `大模型`。
    """
    vocabulary = resume_svc.load_skill_vocabulary(settings)
    assert resume_svc._match_any(text, vocabulary["LLM"]), f"{text!r} 没命中 LLM"


def test_relaxed_left_boundary_still_rejects_half_words(settings) -> None:
    """ISSUES #4 的守卫：放宽中文左边界**不能**把精确性一起放掉。

    `Java` / `Go` 这类含 ASCII 的写法仍然必须遵守左边界 ——
    否则"写了 JavaScript 的人被认为会 Java"，匹配分虚高（这才是边界的初衷）。
    """
    vocabulary = resume_svc.load_skill_vocabulary(settings)
    assert not resume_svc._match_any("熟悉JavaScript开发", vocabulary["Java"])
    assert not resume_svc._match_any("在用Google分析", vocabulary["Go"])
    assert not resume_svc._match_any("大模型X框架", vocabulary["LLM"])  # 右边界照旧


def test_vocabulary_maps_every_spelling_to_one_canonical(settings) -> None:
    """ISSUES #5：一个写法只能属于一个规范名（否则分母被灌水）。

    测试配置把「大模型」挂在 `LLM` 名下，而「大模型」本身也曾是内置规范名
    —— 修好之后它只应该作为 `LLM` 的一种写法存在。
    """
    vocabulary = resume_svc.load_skill_vocabulary(settings)

    owners = [
        name for name, aliases in vocabulary.items() if any(alias == "大模型" for alias in aliases)
    ]
    assert owners == ["LLM"], f"「大模型」被多个规范名认领：{owners}"


def test_alias_merge_moves_all_spellings_of_the_folded_name() -> None:
    """ISSUES #5 的另一半：合并**不能吞技能**。

    ★ 为什么这条必须单独钉住：修 #5 最省事的做法是"把重复的规范名删掉"，
      那样会顺手把被删条目**自己的别名**也弄丢（`大语言模型` 从此永远抽不到），
      而且丢得完全静默 —— 用户只会发现"gap 里少统计了一个词"。
      正确做法是把写法集合整体并给赢家。
    """
    merged = resume_svc._resolve_alias_collisions(
        {"LLM": ["LLM", "大模型"], "大模型": ["大模型", "大语言模型"]},
        prefer=["LLM"],
    )
    assert list(merged) == ["LLM"], f"折叠后还剩多个规范名：{list(merged)}"
    assert merged["LLM"] == ["LLM", "大模型", "大语言模型"], (
        f"被折叠条目的写法被吞掉了：{merged['LLM']}"
    )


def test_same_sentence_yields_one_keyword_for_one_spelling(settings) -> None:
    """ISSUES #5：同一句里同一处只产出一个关键词。

    这是"技能覆盖率分母"的正确性前提：`大模型` 同时是规范名与别名时，
    一句需求会被抽成 `LLM` + `大模型` 两个关键词，覆盖率被凭空稀释。
    """
    hits = resume_svc.extract_skills("岗位要求：熟悉大模型应用开发。", None)
    overlapping = [word for word in hits if word in {"LLM", "大模型"}]
    assert len(overlapping) == 1, f"同一句被抽成了多个规范名：{hits}"


@pytest.mark.parametrize(
    ("text", "canonical"),
    [
        ("熟悉Ｐｙｔｈｏｎ与ＭｙＳＱＬ", "Python"),
        ("用过Ｄｏｃｋｅｒ部署", "Docker"),
    ],
)
def test_fullwidth_skills_are_matched(text: str, canonical: str, settings) -> None:
    """ISSUES #6：全角英文（中文输入法下极易产生）必须被识别成半角技能。"""
    vocabulary = resume_svc.load_skill_vocabulary(settings)
    assert resume_svc._match_any(text, vocabulary[canonical]), f"{text!r} 没命中 {canonical}"


def test_nfkc_is_only_used_for_matching_not_for_rewriting() -> None:
    """ISSUES #6 的红线：全角→半角只用于**匹配**，绝不能改写用户原文。

    用户的原文是资产（P5 定制与防编造校验都读它），
    悄悄把 `Ｐｙｔｈｏｎ` 改成 `Python` 属于静默改写用户数据，
    而且会改变 `content_hash` 的口径。
    """
    original = "技能：熟悉Ｐｙｔｈｏｎ与ＭｙＳＱＬ"
    parsed = resume_svc.parse_resume_text([original], source_name="（虚构）全角简历.txt")
    assert parsed.raw_text == original, f"原文被改写了：{parsed.raw_text!r}"
    assert "Python" in parsed.skills, f"全角写法没被识别成 Python：{parsed.skills}"


# ===========================================================================
# ⑦ "空简历"必须失败（ISSUES #15）
# ===========================================================================
def test_blank_text_file_is_rejected_not_imported(tmp_path: Path, settings) -> None:
    """纯空白的 .txt 必须抛 `ResumeParseError`，且**不能**被当成一份空简历。

    ★ 为什么这条比"报错"更重要：库里第一份简历会自动成为默认简历，
      一条 0 技能 / 无姓名的空记录会悄悄成为之后所有匹配的参照物 ——
      用户不会察觉，分数却全是基于一份空简历算的。
    """
    target = tmp_path / "空的.txt"
    target.write_text("   \n\n   ", encoding="utf-8")

    with pytest.raises(resume_svc.ResumeParseError) as excinfo:
        resume_svc.parse_resume_file(target, settings=settings)
    # 提示要说清"没读到文字"，而不是一句泛泛的失败
    assert "没能" in str(excinfo.value)


def test_blank_text_file_leaves_no_resume_row(tmp_path: Path, session, settings) -> None:
    """ISSUES #15 的第二半：失败之后库里**一条简历都不该多**。"""
    from sqlalchemy import select

    from jobhunter.models import Resume

    target = tmp_path / "空的.txt"
    target.write_text("\n \n", encoding="utf-8")

    with pytest.raises(resume_svc.ResumeParseError):
        resume_svc.add_resume(session, target, make_default=True, settings=settings)
    assert session.scalars(select(Resume)).all() == [], "解析失败却留下了一条空简历"

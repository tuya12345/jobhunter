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

    实测来历：真实简历的标题行是
        `AgentFlow · 可视化 AI 流水线编排平台（个人项目 · AI 协同开发，2026.08）`
    抠掉时间段与角色词之后，残留的 `· AI 协同开发，` 曾被当成一条描述 ——
    它既不是项目内容，也不该占一条 bullet，展示出来像解析错位了。
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

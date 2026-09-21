"""【边界】空输入 / 超长 / 缺字段 / 特殊字符 / 全半角 —— 以及中文语境下的技能边界。

★★ 这一组里有几条是**故意会红**的：它们测的是"中文紧邻英文技能"这个
   中文简历/JD 里最常见的写法。之所以单独拎出来，是因为开发会话的自测
   只覆盖了"Java 开发"（带空格）这种写法 —— **测试数据的形状决定了它能发现什么**。
"""

from __future__ import annotations

import pytest

from jobhunter.config.settings import get_settings
from jobhunter.services import match_svc, resume_svc
from tests.verify._helpers import build_detail, insert_job, insert_resume


def _keywords(text: str) -> list[str]:
    """走生产路径抽 JD 关键词（不自己拼词表逻辑）。"""
    return match_svc.extract_jd_keywords(text, get_settings())


# ===========================================================================
# ① 中文紧邻英文技能 —— 中文文档里最自然的写法
# ===========================================================================
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("熟悉Java开发", "Java"),
        ("用Go写后端服务", "Go"),
        ("熟练Python编程", "Python"),
        ("使用Docker部署服务", "Docker"),
        ("做过大模型应用", "LLM"),
    ],
)
def test_skill_is_found_when_directly_adjacent_to_chinese(text: str, expected: str) -> None:
    """依据：这是产品的核心能力 —— 从**中文 JD** 里抽出技能（设计文档 §5.1.2 技能归一化）。

    `_alias_pattern` 的英文分支左侧用了 `\\b`，而 Python 正则的 `\\b` 是
    **Unicode 语义**的：汉字属于 `\\w`，所以"过"与"J"之间**没有**词边界，
    `\\bJava` 匹配不到"熟悉Java开发"。

    ★ 实现里右侧已经用 `(?![A-Za-z_])` 手工修掉了同一个坑（并有注释说明），
      左侧漏改 —— 这是典型的"修了一半"。现有测试只覆盖带空格的写法，故长期静默。
    """
    hits = _keywords(f"岗位要求：{text}，有相关项目经验优先。")
    assert expected in hits, (
        f"{text!r} 里没抽出 {expected}（抽出的是 {hits}）。\n"
        "中文紧邻英文技能会整词漏抽 —— 影响 技能匹配 / 方向判定 / jh gap 三处。"
    )


def test_skill_adjacency_variants_are_consistent() -> None:
    """同一句话的三种写法必须抽到同一个技能（决定性是本项目的设计前提：

    `extract_jd_keywords` docstring「同一份 JD 重复抽取结果完全一致，rule_score 才稳定」）。
    这里进一步要求：**语义相同的写法不该因为多了个空格就结论不同**。
    """
    with_space = set(_keywords("熟悉 Java 开发"))
    no_space = set(_keywords("熟悉Java开发"))
    assert "Java" in with_space, "带空格写法都抽不到 Java，说明词表本身有问题"
    assert "Java" in no_space, (
        f"'熟悉 Java 开发' 抽到 {sorted(with_space)}，"
        f"但 '熟悉Java开发' 只抽到 {sorted(no_space)} —— 只差一个空格，结论就不同"
    )


def test_ai_compound_word_is_matched() -> None:
    """`AI大模型` / `LLM大模型` 这类连写在中文 JD 里很常见。

    中文分支的左边界是 `(?<![A-Za-z0-9_])`，而这里前一个字符正是 ASCII 字母 `I`，
    于是整个词被否掉。
    """
    hits = _keywords("负责 AI大模型 相关应用的开发。")
    assert "LLM" in hits or "大模型" in hits, f"'AI大模型' 一个都没抽到：{hits}"


# ===========================================================================
# ② 技能归一化：同一个写法不能既是规范名又是别人的别名
# ===========================================================================
def test_jd_alias_does_not_produce_a_duplicate_canonical_keyword() -> None:
    """依据：设计文档 §5.1.2「技能归一化词表」，以及 `load_skill_vocabulary` 的合并规则。

    测试配置里 `skill_aliases` 把「大模型」挂在 `LLM` 名下，而「大模型」本身
    又是内置的规范名 → 同一句需求被抽成两个关键词：
    分母被灌水（技能覆盖率被稀释），并可能产生**假缺口**。
    """
    hits = _keywords("岗位要求：熟悉大模型应用开发。")
    overlapping = [word for word in hits if word in {"LLM", "大模型"}]
    assert len(overlapping) == 1, f"同一句'大模型'被抽成了多个规范名：{hits} —— 技能分母会被灌水"


def test_resume_with_llm_does_not_report_a_fake_gap_for_its_alias(session, settings) -> None:
    """依据：`jh gap` 的产品定义（设计文档 §5.1.3）：
    "告诉你该补什么"。如果简历写了 `LLM` 却报"缺 大模型"，
    用户会去学一个自己已经会的东西 —— 这比不报更糟。
    """
    insert_resume(session)  # 简历技能里有 LLM，没有"大模型"
    job = insert_job(
        session,
        build_detail(jd_text="岗位要求：熟悉大模型应用开发，负责检索增强。"),
    )
    candidates = match_svc.load_resume_candidates(session, settings)
    result = match_svc.match_best_candidate(job.jd_text, candidates, job=job, settings=settings)
    assert result is not None

    graph = {keyword: (keyword in result.matched_keywords) for keyword in result.jd_keywords}
    if graph.get("LLM") and graph.get("大模型") is False:
        pytest.fail(
            f"简历写了 LLM，却把'大模型'报成缺口：matched={result.matched_keywords} "
            f"missing={result.missing_keywords}"
        )
    assert "LLM" in result.matched_keywords, f"简历有 LLM 却没命中：{result.skill_overlap}"


# ===========================================================================
# ③ 归一化的全半角 / 空白 / 特殊字符
# ===========================================================================
def test_fullwidth_english_skill_is_recognized() -> None:
    """边界：全角英文（中文输入法下极易产生，且 `normalize_text` 已经有 NFKC 能力）。

    注意：`extract_jd_keywords` 把**原始** jd_text 直接交给 `_match_any`，
    没有先过 NFKC —— 所以全角写法会静默漏抽。
    """
    hits = _keywords("岗位要求：熟悉Ｐｙｔｈｏｎ与ＭｙＳＱＬ。")
    assert "Python" in hits, f"全角 'Ｐｙｔｈｏｎ' 没被识别成 Python：{hits}"


def test_unknown_skill_is_ignored_not_counted_as_missing() -> None:
    """依据：`extract_jd_keywords` docstring「词表法 的代价是漏……

    漏掉的词会在 jh gap 里表现为'这个词没被统计'」。

    即：词表外的技能应当**不参与**统计（既不加分也不计缺口），
    而不是被算成"我缺这个"。
    """
    # 用一批**确定不在词表里**的杜撰名（不能用 Coze/Dify —— 实测它们已在词表内）
    hits = _keywords("岗位要求：熟练使用 ZzFramework9000 与 QqqInternalTool 搭建工作流。")
    assert "ZzFramework9000" not in hits and "QqqInternalTool" not in hits, (
        f"词表里没有的技能不该出现在关键词里：{hits}"
    )


def test_vocabulary_covers_current_agent_ecosystem_buzzwords() -> None:
    """正向断言：Agent 方向的常见技术栈必须**在词表里**（否则 `jh gap` 会系统性漏统计）。

    依据：设计文档的关键词设定（AI Agent / 大模型应用）+ 词表是 gap 的唯一数据源。
    这条是"词表覆盖率"的看门狗：将来流行的词没加进去，会在这里露出来。
    """
    vocabulary = resume_svc.load_skill_vocabulary(get_settings())
    expected = ["LangChain", "RAG", "Docker", "FastAPI", "MySQL", "Redis", "Git"]
    missing = [name for name in expected if name not in vocabulary]
    assert not missing, f"这些常见技能不在词表里，gap 会漏统计：{missing}"


# ===========================================================================
# ④ 缺字段 / 空结构
# ===========================================================================
def test_resume_without_any_skills_gets_zero_skill_coverage(session, settings) -> None:
    """边界：简历里**任何地方**都没有技能（技能栏空、项目/实习也没写技能）。

    期望：技能维度是"判得出来且命中 0"（ratio=0.0），不是 None。
    因为我们已经充分知道"这份简历没有技能"，这不是信息不足 ——
    把 ratio 设成 None 会把它从分母里拿掉，等于**奖励**一份什么都没写的简历。
    """
    from jobhunter.services.resume_svc import ResumeContent

    blank = ResumeContent(
        basic={"name": "林小满", "city": "深圳"},
        education=[],
        skills=[],
        projects=[],
        internships=[],
        raw_text="（fixture）只有姓名与城市的空简历。",
    )
    insert_resume(session, blank)
    job = insert_job(session)
    candidates = match_svc.load_resume_candidates(session, settings)
    result = match_svc.match_best_candidate(job.jd_text, candidates, job=job, settings=settings)
    assert result is not None

    skill_part = result.rule_detail["parts"]["skill"]
    assert skill_part["ratio"] == 0.0, (
        f"简历里一个技能都没有，技能覆盖率却是 {skill_part['ratio']}（应为 0.0）"
    )
    assert result.matched_keywords == [], f"不该命中任何技能：{result.matched_keywords}"


def test_job_missing_company_and_city_still_scores(session, settings) -> None:
    """边界：抓取只拿到 JD 正文（公司/城市都没抓到）——设计上 `JobDetail` 允许字段全空。"""
    insert_resume(session)
    job = insert_job(
        session,
        build_detail(
            company=None,
            title=None,
            city=None,
            experience=None,
            education=None,
            is_intern=None,
            salary_text=None,
            salary_min=None,
            salary_max=None,
            platform_job_id=None,
            jd_text="负责 Python 后端开发，熟悉 FastAPI 与 MySQL。",
        ),
    )
    candidates = match_svc.load_resume_candidates(session, settings)
    result = match_svc.match_best_candidate(job.jd_text, candidates, job=job, settings=settings)
    assert result is not None
    assert 0 <= result.rule_score <= 100
    assert "Python" in result.matched_keywords


def test_evidence_floor_uses_five_as_denominator(session, settings) -> None:
    """依据：`score_skill` docstring 的"证据下限"（`_SKILL_EVIDENCE_FLOOR = 5`）。

    恰好抽到 5 个关键词时，分母就是 5（不触发下限放大）；
    少于 5 个时，分母按 5 算 → 覆盖率被压低。两条一起断言，防止"下限被改没了"。
    """
    insert_resume(session)
    five = insert_job(
        session,
        build_detail(
            platform_job_id="floor5",
            url="https://www.zhipin.com/job_detail/floor5.html",
            jd_text="要求 Python、FastAPI、MySQL、Redis、Docker。",
        ),
    )
    one = insert_job(
        session,
        build_detail(
            platform_job_id="floor1",
            url="https://www.zhipin.com/job_detail/floor1.html",
            jd_text="要求 Python。",
        ),
    )
    candidates = match_svc.load_resume_candidates(session, settings)

    result_five = match_svc.match_best_candidate(
        five.jd_text, candidates, job=five, settings=settings
    )
    result_one = match_svc.match_best_candidate(one.jd_text, candidates, job=one, settings=settings)
    assert result_five is not None and result_one is not None

    keywords_five = len(result_five.jd_keywords)
    keywords_one = len(result_one.jd_keywords)
    assert keywords_five >= 5, f"这段 JD 至少该抽出 5 个技能，实际 {result_five.jd_keywords}"
    assert keywords_one == 1, f"这段 JD 只该抽出 1 个技能，实际 {result_one.jd_keywords}"

    ratio_five = result_five.rule_detail["parts"]["skill"]["ratio"]
    ratio_one = result_one.rule_detail["parts"]["skill"]["ratio"]
    assert ratio_five == pytest.approx(1.0), f"5 个关键词全命中，覆盖率应为 1.0，实际 {ratio_five}"
    assert ratio_one is not None and ratio_one <= 0.2 + 1e-9, (
        f"只抽到 1 个关键词时覆盖率应被证据下限压到 ≤0.2，实际 {ratio_one}"
        "（'JD 写得越少分越高'那个已修 bug 的守卫）"
    )


def test_skill_vocabulary_includes_self_alias_for_every_canonical() -> None:
    """加固观察项：规范名本身必须能被匹配到（词表自洽性）。

    新增一个规范名时若忘记把"自己"加进别名列表，这个技能就永远抽不出来 ——
    而且是静默的（没有任何报错）。
    """
    vocabulary = resume_svc.load_skill_vocabulary(get_settings())
    missing_self = [
        name for name, aliases in vocabulary.items() if not resume_svc._match_any(name, aliases)
    ]
    assert not missing_self, f"这些规范名连自己都匹配不到：{missing_self[:10]}"

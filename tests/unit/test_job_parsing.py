"""从 HTML 抽岗位详情（**纯函数**，全部用 mock HTML 测试）。

★ 为什么 P2 的测试完全不需要浏览器

    真实招聘站点在 CI 里要么打不开、要么要登录、要么明天就改版 ——
    把测试建立在"能访问 zhipin.com"之上，等于给 CI 装了个随机红灯。
    所以这里的分工是：

        抓取（要浏览器/网络）→ adapters/manual.py     ← 只有你本地手动跑时才执行
        解析（纯函数）        → adapters/parse_html.py ← 本文件测它，确定性的

    mock HTML 还有一个真实价值：**离线重放**。解析规则改了之后，
    拿之前存下来的 HTML 重跑就能验证效果，不必再去访问对方站点。

★ 这组测试守的两条"必须有"的行为

    ① **JD 正文取最长的候选块** —— 招聘页到处都在重复"职位描述"，
       按 class 猜很容易挑到推荐位的短文本；
    ② **抓不到就如实标注 `fetch_status`** —— 标错会让 P3 基于空文本打分，
       而用户完全不知道为什么分数低。
"""

from __future__ import annotations

import pytest

from jobhunter.adapters.parse_html import (
    detect_platform,
    extract_platform_job_id,
    parse_detail_from_html,
    parse_detail_from_text,
)
from jobhunter.adapters.parse_text import (
    KNOWN_CITIES,
    extract_city,
    extract_education,
    extract_experience,
    extract_salary,
    looks_like_intern,
    normalize_for_dedup,
    normalize_text,
    strip_html,
)


# ===========================================================================
# 文本解析（薪资 / 城市 / 经验 / 学历 / 实习）
# ===========================================================================
@pytest.mark.parametrize(
    ("text", "expected_text", "expected_min", "expected_max"),
    [
        ("15-25K", "15-25K", 15000, 25000),
        ("15-25K·14薪", "15-25K·14薪", 15000, 25000),
        ("20K-40K", "20K-40K", 20000, 40000),
        ("1.5-2.5万", "1.5-2.5万", 15000, 25000),
        ("8千-1.2万", "8千-1.2万", 8000, 12000),
        ("6000-8000元/月", "6000-8000", 6000, 8000),
        ("薪资：25K", "25K", 25000, 25000),
    ],
)
def test_extract_salary_variants(
    text: str, expected_text: str, expected_min: int, expected_max: int
) -> None:
    """各种真实薪资写法的数值化（元/月）。"""
    display, low, high = extract_salary(text)
    assert display == expected_text
    assert (low, high) == (expected_min, expected_max)


@pytest.mark.parametrize("text", ["薪资面议", "面议", "3-5年经验", "本科及以上", "", "我们很缺人"])
def test_extract_salary_returns_nothing_when_absent(text: str) -> None:
    """★ 认不出就留空，绝不猜。

    "3-5年经验" 和薪资长得很像（都是数字区间）—— 必须靠"薪资上下文词"
    （薪/K/万/元…）把它们区分开，否则工作经验会被当成月薪写进库，
    而 P4 的"按钱筛"就会筛出荒谬结果。
    """
    display, low, high = extract_salary(text)
    assert (display, low, high) == (None, None, None)


def test_extract_city_from_various_layouts() -> None:
    """城市抽取要能吃下各种真实排版。"""
    assert extract_city("深圳·南山区") == "深圳"
    assert extract_city("工作地点：广东东莞") == "东莞"
    assert extract_city("北京 海淀区") == "北京"
    assert extract_city("广州市天河区") == "广州"
    assert extract_city("Remote / 远程") is None


def test_known_cities_are_real_cities() -> None:
    """城市表的自检：不能出现"东"这种会误伤的短名，也不能有重复。"""
    assert len(KNOWN_CITIES) == len(set(KNOWN_CITIES))
    assert all(len(city) >= 2 for city in KNOWN_CITIES)


def test_extract_experience_variants() -> None:
    """经验要求保留原文（不压成数字，语义差别太大）。"""
    assert extract_experience("3-5年经验") == "3-5年"
    assert extract_experience("5年以上") == "5年以上"
    assert extract_experience("经验不限") == "经验不限"
    assert extract_experience("应届生优先") == "应届生"
    assert extract_experience("我们公司很好") is None


def test_extract_education_variants() -> None:
    """学历要求。"""
    assert extract_education("本科及以上") == "本科及以上"
    assert extract_education("学历：大专") == "大专"
    assert extract_education("不限学历") is None


def test_looks_like_intern_is_three_valued() -> None:
    """★ 实习判断必须是**三态**（True / False / None），不能只有两态。

    `None` 表示"信号不足" —— 如果把 None 当 False，
    实习岗会被标成全职，用户按"全职"筛的时候就漏掉了实习机会。
    """
    assert looks_like_intern("招聘实习生") is True
    assert looks_like_intern("兼职日结") is False
    # "有实习经历者优先" 说的是**要求**，不是岗位性质 → 判不出来
    assert looks_like_intern("有实习经历者优先") is None


def test_normalize_text_fullwidth_to_halfwidth() -> None:
    """全角转半角：否则每条正则都要写两遍（迟早漏一个）。"""
    assert normalize_text("１５－２５Ｋ") == "15-25K"
    assert normalize_text("（深圳）") == "(深圳)"
    assert normalize_text("a\u3000b\tc") == "a b c"


# ===========================================================================
# 去重归一化
# ===========================================================================
def test_normalize_for_dedup_ignores_noise() -> None:
    """★ 归一化要能穿透 HR 常改的那几种噪音。

    同一个岗位，HR 改标题不改岗位 —— 下面这些写法必须归一到同一个字符串，
    否则去重第二层形同虚设（每次重抓都新增一条）。
    """
    base = normalize_for_dedup("某某科技（深圳）有限公司", "Python开发工程师", "深圳")
    for noisy_title in (
        "急招Python开发工程师",
        "高薪诚聘 Python开发工程师",
        "Python开发工程师（15-25K）",
        "python 开发工程师",
        "Python开发工程师（急聘）",
    ):
        assert normalize_for_dedup("某某科技有限公司", noisy_title, "深圳") == base, (
            f"{noisy_title!r} 没有归一到同一个岗位"
        )


def test_normalize_for_dedup_keeps_intern_distinction() -> None:
    """★ 实习/全职**不能**被归一化掉 —— 它们是两个不同的岗位。"""
    full_time = normalize_for_dedup("某公司", "Python开发工程师", "深圳")
    intern = normalize_for_dedup("某公司", "Python开发工程师（实习）", "深圳")
    assert full_time != intern


def test_normalize_for_dedup_distinguishes_city_and_company() -> None:
    """不同城市 / 不同公司不能被归一成同一个。"""
    shenzhen = normalize_for_dedup("某公司", "Python开发工程师", "深圳")
    beijing = normalize_for_dedup("某公司", "Python开发工程师", "北京")
    other = normalize_for_dedup("另一家公司", "Python开发工程师", "深圳")
    assert len({shenzhen, beijing, other}) == 3


def test_strip_html_keeps_paragraph_structure() -> None:
    """HTML→文本要保住段落（否则 JD 会粘成一大坨，P3 抽不到结构化要求）。"""
    html = "<div><p>第一段</p><p>第二段</p><ul><li>要求A</li><li>要求B</li></ul></div>"
    text = strip_html(html)
    assert "第一段" in text and "第二段" in text
    assert text.count("\n") >= 3
    assert "<p>" not in text


def test_strip_html_drops_script_and_style() -> None:
    """脚本/样式里的文字不是内容（常见坑：抽出来的 JD 里混进一大段 JS）。"""
    html = "<style>.a{color:red}</style><script>var x='忽略我'</script><p>正文</p>"
    text = strip_html(html)
    assert "正文" in text
    assert "忽略我" not in text
    assert "color:red" not in text


# ===========================================================================
# 平台识别与岗位 ID
# ===========================================================================
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.zhipin.com/job_detail/abc123.html", "boss"),
        ("https://www.lagou.com/jobs/1234567.html", "lagou"),
        ("https://jobs.51job.com/shenzhen/123456789.html", "51job"),
        ("https://www.liepin.com/job/1945.shtml", "liepin"),
        ("https://www.zhaopin.com/jobs/CC123.htm", "zhilian"),
        ("https://careers.example.com/apply/42", "official"),
    ],
)
def test_detect_platform(url: str, expected: str) -> None:
    assert detect_platform(url) == expected


def test_extract_platform_job_id() -> None:
    """★ 从 URL 抠岗位 ID（去重第一层）。

    为什么优先从 URL 抠：稳定、离线可得（批量导入还没抓页面就能算出来）。
    """
    assert extract_platform_job_id("https://www.zhipin.com/job_detail/abc123.html") == "abc123"
    assert extract_platform_job_id("https://www.lagou.com/jobs/9876543.html") == "9876543"
    assert extract_platform_job_id("https://www.zhipin.com/job_detail/abc.html?lid=1&x=2") == "abc"
    # 抠不到就返回 None —— **绝不拿 URL 本身当 ID**
    # （URL 带会话参数，同一岗位两次访问可能不同 → 会产生假重复/假新增）
    assert extract_platform_job_id("https://careers.example.com/apply") is None


# ===========================================================================
# HTML → JobDetail
# ===========================================================================
_BOSS_LIKE_HTML = """<!DOCTYPE html>
<html><head>
  <title>AI Agent 开发工程师_某某科技_15-25K_深圳_招聘-BOSS直聘</title>
  <style>.noise{color:#333}</style>
  <script>var sessionToken='不应该出现的内容';</script>
</head>
<body>
  <nav><a href="/">首页</a><span class="job-sec-text">短导航文本</span></nav>
  <div class="job-banner">
    <h1 class="name">AI Agent 开发工程师</h1>
    <span class="salary">15-25K·14薪</span>
    <p class="info-primary">深圳·南山区 3-5年 本科</p>
  </div>
  <div class="company-info"><span class="name">某某科技有限公司</span></div>
  <div class="job-sec">
    <h3>职位描述</h3>
    <div class="job-sec-text">
      <p>岗位职责：</p>
      <p>1. 负责 AI Agent 应用的架构设计与开发；</p>
      <p>2. 基于大模型 API 实现工具调用与多轮对话；</p>
      <p>3. 参与 RAG 检索链路的优化（切块、向量化、重排）。</p>
      <p>任职要求：</p>
      <p>1. 熟悉 Python，了解 FastAPI 等 Web 框架；</p>
      <p>2. 了解 LangChain / Function Calling 者优先；</p>
      <p>3. 有 RAG 或向量数据库实践经验者优先。</p>
      <p>我们提供：六险一金、弹性工作、年度体检、团建旅游、零食下午茶。</p>
    </div>
  </div>
  <div class="job-recommend">
    <div class="job-sec-text">相关推荐：Java开发工程师 20-30K</div>
  </div>
  <footer><p>BOSS直聘 版权所有</p></footer>
</body></html>
"""


def test_parse_detail_from_html_picks_longest_jd_block() -> None:
    """★ JD 正文必须取**最长**的候选块，而不是第一个命中的。

    页面上有三处 `job-sec-text`：导航（短）、正文（长）、相关推荐（中）。
    取第一个会得到"短导航文本"，取正文才算对。
    """
    detail = parse_detail_from_html(
        _BOSS_LIKE_HTML, "https://www.zhipin.com/job_detail/abc123.html"
    )

    assert detail.fetch_status == "ok"
    assert "岗位职责" in detail.jd_text
    assert "RAG 检索链路" in detail.jd_text
    assert "短导航文本" not in detail.jd_text
    assert "相关推荐" not in detail.jd_text
    # 脚本内容不能混进正文
    assert "sessionToken" not in detail.jd_text


def test_parse_detail_from_html_extracts_structured_fields() -> None:
    """结构化字段抽取：公司/职位/薪资/城市/经验/学历/平台 ID。"""
    detail = parse_detail_from_html(
        _BOSS_LIKE_HTML, "https://www.zhipin.com/job_detail/abc123.html"
    )
    assert detail.platform == "boss"
    assert detail.platform_job_id == "abc123"
    assert detail.title == "AI Agent 开发工程师"
    assert detail.company == "某某科技有限公司"
    assert detail.salary_text == "15-25K·14薪"
    assert (detail.salary_min, detail.salary_max) == (15000, 25000)
    assert detail.city == "深圳"
    assert detail.experience == "3-5年"
    assert detail.education == "本科"
    assert detail.is_intern is None  # 没提实习 → 判不出来（不是 False）


def test_parse_detail_from_html_handles_nested_same_tags() -> None:
    """★ 嵌套的同名标签不能被截断。

    真实页面里 `div.job-sec-text` 里还套着 div 是常态。
    用简单的非贪婪匹配会在第一个内层 `</div>` 处截断 —— JD 正文只剩一小半，
    而没有任何报错（这种"静默丢内容"最难发现）。
    """
    html = """
    <html><body>
      <div class="job-sec-text">
        <div class="inner"><p>第一层嵌套的内容，需要足够长才能通过长度门槛，</p>
          <div class="deeper"><p>第二层嵌套的内容也要在，验证配对逻辑处理了多层。</p></div>
        </div>
        <p>结尾这句必须在，否则说明被内层闭合标签截断了。</p>
      </div>
    </body></html>
    """
    detail = parse_detail_from_html(html, "https://www.zhipin.com/job_detail/x.html")
    assert "第一层嵌套" in detail.jd_text
    assert "第二层嵌套" in detail.jd_text
    assert "结尾这句必须在" in detail.jd_text


def test_parse_detail_from_html_falls_back_to_whole_page() -> None:
    """认不出 JD 容器时退回整页文本，并如实标 `partial`。"""
    body = "内容" * 60
    html = f"<html><head><title>某岗位_某公司</title></head><body><p>{body}</p></body></html>"
    detail = parse_detail_from_html(html, "https://careers.example.com/apply/1")
    assert detail.platform == "official"
    assert detail.fetch_status == "partial"
    assert len(detail.jd_text) > 60


def test_parse_detail_from_html_marks_failed_on_empty_page() -> None:
    """★ 空页面必须标 `failed`。

    标错（比如标成 ok）的后果：P3 会基于空文本打分，
    用户看到一堆低分岗位却完全不知道是抓取失败导致的。
    """
    detail = parse_detail_from_html("<html><body></body></html>", "https://x.com/job/1")
    assert detail.fetch_status == "failed"
    assert detail.jd_text == ""


# ===========================================================================
# 纯文本 → JobDetail（--manual 手动粘贴）
# ===========================================================================
def test_parse_detail_from_text_with_labels() -> None:
    """手动粘贴时，用户手写的 `公司：` / `职位：` 行要被认出来。"""
    text = (
        "公司：某某网络科技\n"
        "职位：Python 后端开发工程师\n"
        "薪资：18-25K\n"
        "城市：广州\n"
        "岗位职责：负责后端接口开发，熟悉 FastAPI 与 MySQL。\n"
    )
    detail = parse_detail_from_text(text)
    assert detail.platform == "manual"
    assert detail.company == "某某网络科技"
    assert detail.title == "Python 后端开发工程师"
    assert detail.city == "广州"
    assert detail.salary_text == "18-25K"
    assert detail.fetch_status == "ok"


def test_parse_detail_from_text_without_labels() -> None:
    """没有标签时：第一行短文本当岗位名，其余当正文。"""
    text = "AI 应用开发工程师\n\n岗位职责：\n1. 做大模型应用；\n2. 写 Python 服务。\n"
    detail = parse_detail_from_text(text)
    assert detail.title == "AI 应用开发工程师"
    assert "岗位职责" in detail.jd_text
    assert detail.company is None  # 认不出就留空，不猜


def test_parse_detail_from_text_long_first_line_gets_no_title() -> None:
    """★ 第一行很长时**不**拿它当岗位名。

    用户直接粘贴整段 JD 时，第一行往往是正文的一部分。
    硬当标题会产生一个"看起来像岗位名其实是一句话"的字段，
    比留空更糟（P4 生成开场白时会引用它）。
    """
    long_first = "岗位职责：负责公司核心业务系统的后端开发与维护，参与架构设计和技术选型等工作"
    detail = parse_detail_from_text(f"{long_first}\n还有更多内容。")
    assert detail.title is None

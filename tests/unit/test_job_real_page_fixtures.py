"""P2 的"真实结构"fixtures：公司名/字段抽取 + 登录墙（ISSUES #63 / #64）。

★★ 这组用例回答的是 #63 提的那个问题

    #63：**"800 条测试全绿"只证明代码在手写 fixture 上是对的** ——
    而手写的 `SAMPLE_HTML` 是"干净 JSON-LD + 一整套旧版 class"的理想输入，
    真实站点（BOSS / 腾讯 / 拉勾 / 智联 / 猎聘）**一个都不长这样**。

    所以这里换成 **`tests/fixtures/` 下的"真实结构"fixture**：
    结构（class 名与嵌套）**逐字抄自真实页面**，内容全部虚构。

★★ 为什么不直接把真实页面放进仓库（我核对后**故意没做**）

    · 用户保存的那份 BOSS 岗位页（449 KB）：**64 个 11 位数字**（页面 JSON 里的第三方电话）
      与 `退出登录` / `token` / `username` 等登录态痕迹；
    · 验证会话抓的 BOSS 登录页（10 KB）：也含 1 个 11 位数字与 `token` 字样。

    它们进仓库，CI 那道隐私扫描就该红了。所以：**结构照抄、内容虚构**，
    并加一条守卫（本文件最后那两个用例）拦住"以后有人直接把真实页面塞进来"。

★ #64 的实证：真实 BOSS 页面上
    `class="name"` 是**职位名**、`company-name` 全在**相似职位推荐位**里，
    真正的公司名在 `sider-company` 的 `<a title="…">` 上 —— 下面的 fixture 就是照这个形状做的。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jobhunter.adapters.manual import read_html_file
from jobhunter.adapters.parse_html import detect_access_wall
from jobhunter.config.paths import find_project_root

FIXTURES = Path(find_project_root()) / "tests" / "fixtures"
BOSS_JOB_PAGE = FIXTURES / "boss_job_page.html"
BOSS_LOGIN_WALL = FIXTURES / "boss_login_wall.html"


@pytest.fixture
def boss_detail():
    """按"另存为网页"的真实用法解析：`--html` + 原始链接 + 平台。"""
    return read_html_file(
        BOSS_JOB_PAGE,
        url="https://www.zhipin.com/job_detail/c6181f2d6ee9b1150nF939y-F1NX.html",
        platform="boss",
    )


# ===========================================================================
# #64 公司名不许抽成职位名
# ===========================================================================
def test_company_is_not_the_job_title(boss_detail) -> None:
    """★★ 核心回归：真实结构下公司名必须来自 `sider-company`，**不是**职位名。

    ★ 这条守的就是 #64 的原症状：公司名与职位名抽成了同一个值，
      表格里两列一模一样、`fetch_status` 还是 `ok` —— 用户不盯着看发现不了，
      而公司名是**去重键**（`name_norm` UNIQUE），错了两家公司的岗位会被并到一起。
    """
    assert boss_detail.company == "虚构科技"
    assert boss_detail.title == "后端开发实习生（AI 平台方向）"
    assert boss_detail.company != boss_detail.title, "公司名又被抽成职位名了（#64）"


def test_recommendation_cards_do_not_leak_into_fields(boss_detail) -> None:
    """★ 相似职位推荐位里的公司/职位**不许**污染当前岗位的字段。

    真实 BOSS 页面上 `company-name` 只出现在推荐位里（`xTool` / `北斗院` …），
    而 `class="name"` 的第一个命中也在推荐卡片上 —— 这正是 #64 的成因。
    """
    assert boss_detail.company not in {"别家网络", "另一家公司"}
    assert boss_detail.title not in {"前端开发实习生", "算法实习生"}
    assert "别家" not in (boss_detail.company or "")
    assert "别家" not in (boss_detail.title or "")


def test_structured_fields_come_from_the_main_region(boss_detail) -> None:
    """城市 / 薪资 / 经验 / 学历都在主区域里（改前 `html[:4000]` 全是 `<head>`）。

    ★ 薪资是**日薪**（实习岗常见）：数值列保持 None 是**对的**（日薪≠月薪，不能硬换），
      但字符串列要如实留下原文 —— 否则用户看到"薪资 —"而网页上明明写着。
    """
    assert boss_detail.city == "深圳"
    assert boss_detail.salary_text == "120-200元/天"
    assert (boss_detail.salary_min, boss_detail.salary_max) == (None, None)
    assert boss_detail.education == "本科"
    assert boss_detail.fetch_status == "ok"
    assert "AI 平台的后端接口开发" in boss_detail.jd_text


def test_job_title_layout_with_chinese_quotes_and_suffix() -> None:
    """`「岗位名招聘」_公司招聘-BOSS直聘` 这种 title 也要能分段（#64 的附带 bug）。

    ★ 改前 `_is_site_name("智玩店科技招聘")` 因为"**包含**"了通用词"招聘"而返回 True，
      于是整段被当站点名丢掉 → 分段结果 `[]` → 公司名与城市一起丢。
    """
    from jobhunter.adapters.parse_html import _split_page_title

    parts = _split_page_title("「后端开发实习生（AI 平台方向）招聘」_虚构科技招聘-BOSS直聘")
    assert parts == ["后端开发实习生（AI 平台方向）", "虚构科技"]


# ===========================================================================
# #62 的 fixture 版：登录墙（真实判据统计，页面匿名）
# ===========================================================================
def test_login_wall_fixture_is_refused() -> None:
    """真实登录页的文案 + 没有任何岗位结构 → 必须判墙、必须不入库。"""
    note = detect_access_wall(BOSS_LOGIN_WALL.read_text(encoding="utf-8"), platform="boss")
    assert note is not None
    assert "登录" in note

    detail = read_html_file(BOSS_LOGIN_WALL, url="https://www.zhipin.com/job_detail/x.html")
    assert detail.fetch_status == "failed"
    assert detail.jd_text == ""


def test_the_two_fixtures_are_actually_different_shapes() -> None:
    """★ 元测试：两份 fixture 必须真的"一个像岗位页、一个像墙"。

    防的是"fixture 写歪了"这种假绿：如果岗位页 fixture 里也没有岗位结构，
    那 `test_company_is_not_the_job_title` 通过也说明不了任何事。
    """
    job_html = BOSS_JOB_PAGE.read_text(encoding="utf-8")
    wall_html = BOSS_LOGIN_WALL.read_text(encoding="utf-8")

    for marker in ("info-primary", "sider-company", "company-info", "job-sec"):
        assert marker in job_html, f"岗位页 fixture 少了真实结构标志：{marker}"
    for marker in ("job-sec", "job-title", "data-test"):
        assert marker not in wall_html, f"登录墙 fixture 里不该有岗位结构：{marker}"


# ===========================================================================
# 守卫：真实页面（含隐私）不许直接进仓库
# ===========================================================================
_PII_PATTERNS = (
    (re.compile(r"1[3-9]\d{9}"), "手机号"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "邮箱"),
    (re.compile(r"退出登录"), "登录态痕迹"),
)


def test_fixtures_carry_no_personal_data() -> None:
    """★★ 守卫：`tests/fixtures/` 下的 HTML 不许含电话/邮箱/登录态痕迹。

    ★ 为什么必须有它（ISSUES #63 的"真实 fixture"建议撞上的现实）：
      验证会话建议把用户保存的真实 BOSS 页面收进 fixtures —— 我核对后发现那份页面
      含 **64 个 11 位数字**与 `退出登录` / `token` 字样，登录页那份也含 1 个数字。
      它们进仓库，CI 的隐私扫描就该红了。

      所以规矩是：**结构可以抄真实的，内容必须虚构**。
      这条用例就是那条规矩的守卫 —— 以后谁直接 `cp` 一份真实页面进来，这里会红。
    """
    offenders: list[str] = []
    for path in sorted(FIXTURES.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for pattern, label in _PII_PATTERNS:
            hits = pattern.findall(text)
            if hits:
                offenders.append(f"{path.name} 含{label}：{hits[:2]}")
    assert not offenders, "\n".join(
        ["fixtures 里有疑似真实个人信息（真实页面不能进仓库）：", *offenders]
    )


def test_pii_guard_would_catch_a_real_page() -> None:
    """元测试：上面那条守卫真的会响（否则它只是个摆设）。"""
    sample = "客户服务热线: 13800138000 退出登录"
    assert any(pattern.search(sample) for pattern, _ in _PII_PATTERNS)

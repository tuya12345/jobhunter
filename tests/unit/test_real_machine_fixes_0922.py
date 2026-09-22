"""2026-09-22 真机验收抓出来的三条缺陷的回归用例（修复模式）。

来源：`notes/2026-09-22-JobHunter-67复核报告.md` §5.3 —— 真抓 5 条岗位后逐条看库里的字段，
发现三件事（都不是猜的，有真实数据）：

  ① **JD 开头混进站点操作条**：5/5 条 `jd_text` 前两行是 `微信扫码分享` / `举报`；
  ② **水印残留**：1/5 条正文里还留着 token 表之外的注入形态（`kanzhun`、被切开的噪声词）；
  ③ **公司名 = 职位名**：1/5 条（与 ISSUES #64 同类，这次命中在**主区域**）。

★ 每个用例都尽量用**真实形态**（注入的空格、被切开的词、真实的 title 写法），
  但**内容全部虚构** —— 与 `tests/fixtures/boss_job_page.html` 同一条纪律。
"""

from __future__ import annotations

import pytest

from jobhunter.adapters.manual import (
    _clean_rendered_text,
    _pick_header_title_line,
)
from jobhunter.adapters.parse_html import is_chrome_line, parse_detail_from_html


# ===========================================================================
# ① 站点操作条（真机 5/5）
# ===========================================================================
@pytest.mark.parametrize(
    "line",
    [
        "微信扫码分享",
        "微 信 扫码分享",  # 站点把词切开（真实形态）
        "举报",
        "微信扫 直聘 码分享举报",  # 两个词被粘成一行 + 中间插了水印词
        " 分享 ",
        "收藏",
        "立即沟通",
    ],
)
def test_chrome_lines_are_recognised(line: str) -> None:
    assert is_chrome_line(line) is True


@pytest.mark.parametrize(
    "line",
    [
        "岗位职责：负责后端开发",  # 正文
        "我们鼓励分享技术方案",  # 正文里出现"分享"但不是整行操作条
        "投递简历后请留意邮箱",  # 含"投递"，但整行不是操作条
        "1、负责数据平台的开发与维护，熟悉 Python 与 SQL，有良好的编码习惯与文档意识",
    ],
)
def test_real_content_is_not_treated_as_chrome(line: str) -> None:
    """反向守卫：**包含**这些词不算操作条，只有"整行就是操作条"才算。

    ★ 本项目在 #56 吃过同类亏：按词面"包含"匹配的判据会把正常内容误删。
    """
    assert is_chrome_line(line) is False


def test_chrome_lines_do_not_enter_the_jd_head() -> None:
    """★ 真机 #1 的形态：正文起点之前有操作条 + 推荐位卡片标题。"""
    raw = "\n".join(
        [
            "python中级开发工程师",  # 推荐位卡片
            "15-30K·14薪",
            "某大型知名计算机软件公司",
            "东莞",
            "python开发工程师（六险一金 导师带教）",
            "微 信 扫码分享",  # ← 站点操作条（被切开）
            "举报",  # ← 站点操作条
            "职位描述",
            "岗位职责",
            "1、负责后台服务的设计与开发。",
        ]
    )
    cleaned = _clean_rendered_text(raw)

    assert "扫码分享" not in cleaned, f"操作条混进了 JD：{cleaned[:60]!r}"
    assert "举报" not in cleaned
    assert cleaned.startswith("职位描述"), f"JD 应当从正文起点开始，实际：{cleaned[:40]!r}"


def test_title_line_is_not_taken_from_a_recommendation_card() -> None:
    """★ 头部全是推荐位卡片时，**不许**把某张卡片的岗位名当成这一条的标题。"""
    header = [
        "python中级开发工程师",
        "15-30K·14薪",
        "某大型知名计算机软件公司",
        "东莞",
        "python开发工程师（六险一金 导师带教）",
        "AI Agent 开发工程师",
        "芯智未来",
        "东莞",
    ]
    assert _pick_header_title_line(header) is None


def test_short_header_still_yields_the_title() -> None:
    """反向守卫：正常的"信息头"（字段行 + 岗位名）仍然要能挑出标题。"""
    header = ["算法工程师｜LLM / AI Agent 方向", "工作地点：北京市", "部门：平台组"]
    assert _pick_header_title_line(header) == "算法工程师｜LLM / AI Agent 方向"


# ===========================================================================
# ③ 公司名 ≠ 职位名（真机 1/5）
# ===========================================================================
#: 真机页面 title 的写法：`「岗位名招聘」_公司招聘-BOSS直聘`
_REAL_PAGE_TITLE = "「Python 后端开发工程师招聘」_示例科技（深圳）有限公司招聘-BOSS直聘"


def _html_with_a_fake_company_candidate() -> str:
    """构造真机那个坑：主区域里的 `company-info` 第一行**是岗位名**。

    ★ 内容全部虚构，只有**结构**照抄真实页面（`parse_html` 的注释里记着这个形态）。
    """
    return f"""<html><head><title>{_REAL_PAGE_TITLE}</title></head><body>
      <div class="job-primary">
        <h1 class="name">Python 后端开发工程师</h1>
        <div class="company-info">
          <div class="name">Python 后端开发工程师</div>
          <div class="tag">未融资 · 20-99人</div>
        </div>
        <div class="job-sec-text">
          <p>职位描述</p><p>负责后端接口开发，熟悉 Python 与 MySQL。</p>
        </div>
      </div>
      <div class="similar-jobs"><div class="company-name">别家公司</div></div>
    </body></html>"""


def test_company_is_not_the_job_title() -> None:
    """★ 真机 #1：公司名被存成了岗位名。

    正确行为：**跳过这个假候选**，改用页面 title 里的公司段（真机实测那里有正确公司名）。
    """
    detail = parse_detail_from_html(_html_with_a_fake_company_candidate(), "https://x.test/job/1")

    assert detail.company != detail.title, "公司名又被存成了职位名"
    assert detail.company == "示例科技（深圳）有限公司", f"公司名取错了：{detail.company!r}"


def test_company_stays_empty_when_nothing_credible_is_found() -> None:
    """★ 宁可留空：页面只给出"公司名 == 岗位名"时，公司字段必须是 `None`（不是岗位名）。

    ★ 为什么这条重要：`companies.name_norm` 是去重键，公司字段装进岗位名之后，
      去重、渠道聚合、开场白里的"贵司"会一起错，而且**看起来完全正常**。
    """
    html = """<html><head><title>Python 后端开发工程师-BOSS直聘</title></head><body>
      <div class="job-primary">
        <div class="company-info"><div class="name">Python 后端开发工程师</div></div>
      </div>
    </body></html>"""
    detail = parse_detail_from_html(html, "https://x.test/job/2")

    assert detail.company is None, f"没有可信来源时应当留空，实际：{detail.company!r}"


def test_real_company_still_wins_over_the_title() -> None:
    """反向守卫：页面**有**真正的公司名时不许被这道新判据挡掉。"""
    html = """<html><head><title>岗位名招聘_某公司招聘-BOSS直聘</title></head><body>
      <div class="job-primary">
        <div class="sider-company"><a title="真正的公司名">真正的公司名</a></div>
        <div class="company-info"><div class="name">岗位名</div></div>
      </div>
    </body></html>"""
    detail = parse_detail_from_html(html, "https://x.test/job/3")

    assert detail.company == "真正的公司名"


# ===========================================================================
# ①(b) HTML 路径同样要丢操作条（2026-09-22 真机复验发现：第一次只修了渲染路径）
# ===========================================================================
def test_html_path_also_drops_chrome_lines() -> None:
    """★ 真机复验：修完渲染路径后库里**仍有** `微信扫码分享` / `举报` ——
    因为那几条 JD 走的是 **HTML 路径**（`parse_detail_from_html`）。
    两条路径必须共用同一个判据（`parse_html.is_chrome_line`），否则修一半。
    """
    html = """<html><body>
      <div class="job-sec-text">
        <div>微信扫码分享</div>
        <div>举报</div>
        <p>职位描述</p>
        <p>负责后端接口开发，熟悉 Python。</p>
      </div>
    </body></html>"""
    detail = parse_detail_from_html(html, "https://x.test/job/4")

    assert "扫码分享" not in detail.jd_text, f"HTML 路径把操作条带进了 JD：{detail.jd_text[:60]!r}"
    assert "举报" not in detail.jd_text
    assert "负责后端接口开发" in detail.jd_text

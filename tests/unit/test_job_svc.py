"""岗位入库与**两层去重**测试（P2 的核心）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 这组测试里最重要的一条：**去重命中时绝不覆盖人工决策字段**                 ║
╚══════════════════════════════════════════════════════════════════════════╝

    `test_dedup_never_touches_decision_fields` 用一个"暴力"的做法来守它：

        ① 先入库一个岗位，然后**手工把每个决策字段改成不可能由代码推导出来的值**
           （status="applied"、match_score=88、rule_score=77、reason_json=…）；
        ② 再用同一岗位（内容变了）走一遍 upsert；
        ③ 断言那些决策字段**一个都没变**，而抓取派生字段确实刷新了。

    为什么值得这么写：去重时"顺手 UPDATE 全部字段"是最自然的写法，也是破坏性最强的 ——
    已经 skip 掉的岗位会悄悄变回 new、算好的分数被清空（重复花钱调 LLM）、
    人工裁决被抹掉，而且**全程不报错**。用"不可能的值"来断言，任何人都没法糊弄过去。

★ 另外守两条边界

    · 第二层去重（归一化 hash）**缺字段时不参与** —— 宁可漏合并，也不错合并；
    · 抓取失败（空值）**不覆盖**已有值 —— 否则一次网络抖动就把之前抓到的 JD 清空了。
"""

from __future__ import annotations

from typing import Any

import pytest

from jobhunter.adapters.base import JobDetail
from jobhunter.models import Job
from jobhunter.services import job_svc


def make_detail(**overrides: Any) -> JobDetail:
    """构造一个"抓取成功"的岗位详情（字段可覆盖）。

    ★ URL 默认跟随 `platform_job_id` 变化，而不是写死一个常量。

      为什么较真（踩过的坑）：URL 现在是去重的一层（公司自建招聘站靠它兜底），
      如果所有测试岗位共用一个 URL，它们会被判成同一个岗位 ——
      而现实里**每个岗位的 URL 必然不同**。测试助手必须反映这一点，
      否则测出来的是"助手写错了"，不是产品行为。
    """
    base: dict[str, Any] = {
        "platform": "boss",
        "platform_job_id": "job-001",
        "company": "某某科技有限公司",
        "title": "AI Agent 开发工程师",
        "city": "深圳",
        "experience": "3-5年",
        "education": "本科",
        "is_intern": False,
        "salary_text": "15-25K",
        "salary_min": 15000,
        "salary_max": 25000,
        "jd_text": "岗位职责：负责 AI Agent 应用的架构设计与开发。\n" * 5,
        "fetch_status": "ok",
    }
    base.update(overrides)
    if "url" not in overrides:
        # 同一个 platform_job_id → 同一个 URL；不同 ID → 不同 URL（贴合现实）
        identifier = base.get("platform_job_id") or base.get("title") or "job"
        base["url"] = f"https://www.zhipin.com/job_detail/{identifier}.html"
    return JobDetail(**base)


# ===========================================================================
# 新入库
# ===========================================================================
def test_insert_new_job(session) -> None:
    """第一次遇到 → 新建，状态是 new，found_at / last_seen_at 都有值。"""
    result = job_svc.upsert_job(session, make_detail())
    assert result.outcome == "created"
    assert result.is_new

    job = job_svc.get_job(session, result.job_id)
    assert job is not None
    assert job.status == "new"
    assert job.company == "某某科技有限公司"
    assert (job.salary_min, job.salary_max) == (15000, 25000)
    assert job.is_intern is False
    assert job.jd_hash  # JD hash 必须算出来（LLM 缓存 key 要用）
    assert job.dedup_norm_hash  # 归一化 hash 也要算出来（第二层去重要用）
    assert job.found_at is not None
    assert job.last_seen_at is not None


def test_jd_hash_is_stable_for_same_text(session) -> None:
    """同一份 JD 两次算出的 hash 必须一致（否则 LLM 缓存每次都被打掉）。"""
    first = job_svc.compute_jd_hash("岗位职责：做大模型应用。\n任职要求：熟悉 Python。")
    second = job_svc.compute_jd_hash("岗位职责：做大模型应用。\n任职要求：熟悉 Python。")
    assert first == second

    # 只改空白不该让 hash 变（页面重排很常见）
    noisy = job_svc.compute_jd_hash("岗位职责：做大模型应用。\n\n\n任职要求：熟悉 Python。  ")
    assert noisy == first

    # 内容真变了要变
    assert job_svc.compute_jd_hash("岗位职责：完全不同的内容。") != first
    assert job_svc.compute_jd_hash("") is None


# ===========================================================================
# 去重第一层：platform_job_id
# ===========================================================================
def test_layer1_dedup_by_platform_job_id(session) -> None:
    """★ 同一个平台 + 同一个 platform_job_id → 命中（最可靠的一层）。"""
    first = job_svc.upsert_job(session, make_detail())
    second = job_svc.upsert_job(session, make_detail())

    assert second.outcome == "unchanged"
    assert second.job_id == first.job_id
    assert second.matched_by == "platform_job_id"
    assert session.query(Job).count() == 1


def test_url_dedup_catches_company_self_hosted_sites(session) -> None:
    """★ 公司自建招聘站必须靠 **URL** 去重（实测踩出来的缺陷）。

    实测来历：美团这类公司自建招聘页**根本不写公司名** ——
    于是"归一化 hash（公司+职位+城市）"因为缺公司而直接放弃匹配，
    结果是**同一个链接加三次，库里出现三条一模一样的记录**。

    修法是加一层"URL 完全相同即同一岗位"：同一个链接第二次打开还是同一个岗位，
    这比模糊匹配可靠、又比平台 ID 宽容（很多站点把岗位 ID 藏在 JS 里，URL 里取不到）。
    """
    url = "https://zhaopin.example.com/web/position/detail?jobUnionId=4592562550"
    # 模拟"页面没写公司名"的真实情况
    manual = make_detail(
        platform="official",
        platform_job_id=None,
        company=None,
        url=url,
        title="算法工程师｜LLM / AI Agent 方向",
    )

    first = job_svc.upsert_job(session, manual)
    second = job_svc.upsert_job(session, manual)
    third = job_svc.upsert_job(session, manual)

    assert first.outcome == "created"
    assert second.matched_by == "url"
    assert third.matched_by == "url"
    assert {second.job_id, third.job_id} == {first.job_id}
    assert session.query(Job).count() == 1, "同一个 URL 产生了重复记录"


def test_layer1_wins_over_layer2(session) -> None:
    """★ 能用强证据（平台 ID）就不要用弱证据（归一化 hash）。

    这条守的是"两层顺序"：如果先按归一化 hash 匹配，
    公司在同一天挂两个相似岗位时会被错误合并成一个。
    """
    first = job_svc.upsert_job(session, make_detail())
    # 同一个公司/职位/城市，但**平台岗位 ID 不同** → 是两个岗位
    second = job_svc.upsert_job(
        session, make_detail(platform_job_id="job-002", title="Java 后端开发工程师")
    )
    assert second.outcome == "created"
    assert second.job_id != first.job_id
    assert session.query(Job).count() == 2


def test_same_title_different_platform_id_is_two_jobs(session) -> None:
    """★★ 标题几乎一样、但**平台岗位 ID 不同** → 必须是两个岗位（否决条件）。

    这是实测踩出来的真实缺陷（也最容易写错）：
    同一家公司在同一城市挂两个相似标题的岗位（"AI Agent 开发工程师" 与
    "AI Agent 开发工程师（急招）"），平台 ID 是 job-001 / job-002。

    如果第二层去重（标题归一化哈希）把它们合并，会发生两件坏事：
        ① 库里只剩一条，用户以为只有一个岗位；
        ② "刷新"逻辑会把那条的 `platform_job_id` 从 job-001 改成 job-002 ——
           记录换了个身份，而用户如果已经投过 job-001，
           投递记录会挂到 job-002 上，完全对不上账。

    所以规则是：**两个互不相同的平台 ID 之间，不做模糊合并**。
    """
    first = job_svc.upsert_job(session, make_detail(platform_job_id="job-001"))
    second = job_svc.upsert_job(
        session,
        make_detail(platform_job_id="job-002", title="AI Agent 开发工程师（急招）"),
    )

    assert second.outcome == "created"
    assert second.job_id != first.job_id
    assert session.query(Job).count() == 2

    # 两条记录各自的平台 ID 都不能被对方顶掉
    ids = {job.platform_job_id for job in session.query(Job).all()}
    assert ids == {"job-001", "job-002"}


def test_different_platform_same_job_id_is_not_duplicate(session) -> None:
    """不同平台的同名 ID 不算同一个岗位（平台 ID 只在平台内唯一）。"""
    first = job_svc.upsert_job(session, make_detail())
    second = job_svc.upsert_job(
        session,
        make_detail(
            platform="lagou",
            platform_job_id="L-9001",
            url="https://www.lagou.com/jobs/9001.html",
        ),
    )
    assert second.outcome == "created"
    assert second.job_id != first.job_id
    assert session.query(Job).count() == 2


# ===========================================================================
# 去重第二层：dedup_norm_hash
# ===========================================================================
def test_layer2_dedup_by_norm_hash(session) -> None:
    """★ 没有平台 ID 时（手动粘贴的场景），靠归一化 hash 兜底去重。

    下面两次输入模拟"同一岗位从不同渠道看到、标题被 HR 改过"：
    第一次带"急招"和薪资，第二次没有 —— 归一化之后应该认为是同一个岗位。

    ★ 结果是 `refreshed` 而不是 `unchanged`：归一化只用于**判断是不是同一个岗位**，
      判断完之后标题字段仍然会被规范化（`急招…（15-25K）` → `AI Agent 开发工程师`）。
      这是有意的 —— 库里存干净的标题，将来生成开场白时不会把"急招"抄进去。
    """
    first = job_svc.upsert_job(
        session,
        make_detail(
            platform="manual",
            platform_job_id=None,
            title="急招AI Agent 开发工程师（15-25K）",
            url=None,
        ),
    )
    second = job_svc.upsert_job(
        session,
        make_detail(
            platform="manual",
            platform_job_id=None,
            title="AI Agent 开发工程师",
            url=None,
        ),
    )
    assert second.matched_by == "dedup_norm_hash"
    assert second.job_id == first.job_id
    assert session.query(Job).count() == 1

    job = job_svc.get_job(session, first.job_id)
    assert job is not None
    assert job.title == "AI Agent 开发工程师", "标题没有被规范化"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("company", None),
        ("title", None),
        ("city", None),
    ],
)
def test_layer2_skips_when_any_key_field_missing(field: str, value: None) -> None:
    """★ 公司/职位/城市**缺任何一个**时，第二层去重必须放弃。

    取舍是刻意的：宁可漏合并（清单里多一条，用户手动 skip 一下），
    也不要错合并（两个不同岗位的记录被并成一个 —— 用户会看到
    "我没投过这个岗位却显示已投"这种无法解释的状态）。
    """
    company = "某某科技有限公司" if field != "company" else value
    title = "AI Agent 开发工程师" if field != "title" else value
    city = "深圳" if field != "city" else value

    assert job_svc.compute_dedup_norm_hash(company, title, city) is None


def test_norm_hash_is_order_independent_of_noise(session) -> None:
    """归一化 hash 对"噪音差异"不敏感，对"实质差异"敏感。"""
    base = job_svc.compute_dedup_norm_hash("某某科技有限公司", "Python 开发工程师", "深圳")
    noisy = job_svc.compute_dedup_norm_hash("某某科技有限公司", "Python开发工程师（急聘）", "深圳")
    assert base == noisy

    assert base != job_svc.compute_dedup_norm_hash("某某科技有限公司", "Python 开发工程师", "北京")
    assert base != job_svc.compute_dedup_norm_hash("另一家公司", "Python 开发工程师", "深圳")
    assert base != job_svc.compute_dedup_norm_hash(
        "某某科技有限公司", "Python 开发工程师（实习）", "深圳"
    )


# ===========================================================================
# ★★ 最关键：去重绝不覆盖人工决策字段
# ===========================================================================
def test_dedup_never_touches_decision_fields(session) -> None:
    """★★ 去重命中时，人工决策 / 已花成本算出来的字段**一个都不许变**。

    做法：
        ① 入库一个岗位；
        ② 手工把决策字段设成"不可能是代码推导出来的值"；
        ③ 用同一岗位（JD 内容变了、薪资也变了）再 upsert 一次；
        ④ 断言：决策字段原封不动，抓取派生字段确实被刷新。
    """
    first = job_svc.upsert_job(session, make_detail())
    job = job_svc.get_job(session, first.job_id)
    assert job is not None

    # ② 人为写入"不可能由代码产生"的决策值
    job.status = "applied"
    job.match_score = 88
    job.llm_score = 91
    job.rule_score = 77
    job.rule_detail_json = '{"direction": 30, "hard": 24, "skill": 23}'
    job.reason_json = '{"strengths": ["人工写入的标记"]}'
    job.matched_resume_id = None
    job.jd_keywords_json = '["人工写入的关键词"]'
    session.flush()

    decision_snapshot = {name: getattr(job, name) for name in job_svc.DECISION_FIELDS}

    # ③ 同一岗位再次抓到（JD 变了 → 属于"刷新"路径）
    second = job_svc.upsert_job(
        session,
        make_detail(
            jd_text="岗位职责：内容更新了。\n任职要求：增加了新的要求。\n" * 5,
            salary_text="20-30K",
            salary_min=20000,
            salary_max=30000,
            experience="5-10年",
        ),
    )
    assert second.matched_by == "platform_job_id"
    assert second.outcome == "refreshed"

    # ④ 决策字段必须原封不动
    refreshed = job_svc.get_job(session, first.job_id)
    assert refreshed is not None
    for name, expected in decision_snapshot.items():
        assert getattr(refreshed, name) == expected, (
            f"决策字段 {name} 被去重逻辑覆盖了：{expected!r} → {getattr(refreshed, name)!r}"
        )

    # 抓取派生字段确实刷新了（否则"刷新"这件事本身就没生效）
    assert refreshed.salary_min == 20000
    assert refreshed.experience == "5-10年"
    assert refreshed.jd_hash != job_svc.compute_jd_hash(make_detail().jd_text)
    assert refreshed.last_seen_at is not None

    # 返回的更新清单里**不允许**出现决策字段
    for name in second.updated_fields:
        assert name not in job_svc.DECISION_FIELDS


def test_decision_fields_and_refreshable_fields_do_not_overlap() -> None:
    """★ 两个字段清单不能有交集。

    这条是"元测试"：即使将来有人往 `REFRESHABLE_FIELDS` 里加了
    `status` 这类字段，这里会立刻失败 —— 比等到线上把用户的 skip 决定冲掉要好。
    """
    overlap = set(job_svc.REFRESHABLE_FIELDS) & set(job_svc.DECISION_FIELDS)
    assert not overlap, f"字段同时出现在两份清单里：{sorted(overlap)}"


def test_every_refreshable_field_is_mappable_from_detail() -> None:
    """★ 每个可刷新字段都要能真的从 `JobDetail` 取到值。

    踩过的坑：循环里用 `getattr(detail, name, None)` 取新值，而
    **列名与 `JobDetail` 的字段名并不总是一样** ——
    列叫 `jd_fetch_status`，dataclass 上的字段叫 `fetch_status`。
    于是 `getattr` 永远返回 None，抓取状态**一次都没被刷新过**，
    而表面上一切正常（没有报错、没有异常，只是这个字段永远停在第一次的值）。

    这条测试的做法：构造一个"所有字段都非空"的 detail，
    断言每个可刷新字段都能取到非空新值 —— 名字对不上的字段会立刻暴露。
    """
    detail = make_detail(fetch_status="partial")
    # 需要单独计算的字段（不是直接取自 detail 的属性）
    computed = {"dedup_norm_hash", "jd_hash", "source_keyword"}
    # 字段名映射（列名 → JobDetail 上的字段名）
    aliases = {"jd_fetch_status": "fetch_status"}

    for name in job_svc.REFRESHABLE_FIELDS:
        if name in computed:
            continue
        attribute = aliases.get(name, name)
        value = getattr(detail, attribute, None)
        assert value not in (None, ""), (
            f"可刷新字段 {name!r} 无法从 JobDetail 取到值"
            f"（请检查字段名映射，别名表里目前有 {sorted(aliases)}）"
        )


def test_dedup_does_not_clear_fields_on_fetch_failure(session) -> None:
    """★ 抓取失败（空值）不能把之前抓到的字段清空。

    真实场景：第一次抓到了完整 JD，第二次网络抖动只抓到标题 ——
    如果空值也写进去，之前的 JD 就没了，P3 会突然算不出分数。
    """
    first = job_svc.upsert_job(session, make_detail())
    assert first.outcome == "created"

    job_svc.upsert_job(
        session,
        make_detail(
            jd_text="",
            salary_text=None,
            salary_min=None,
            salary_max=None,
            experience=None,
            education=None,
            city=None,
            fetch_status="failed",
        ),
    )

    job = job_svc.get_job(session, first.job_id)
    assert job is not None
    assert job.jd_text, "JD 正文被空值清空了"
    assert job.salary_min == 15000, "薪资被空值清空了"
    assert job.city == "深圳", "城市被空值清空了"
    # 但抓取状态要如实反映最后一次的结果（这是我们自己派生出来的，不算人工决策）
    assert job.jd_fetch_status == "failed"


def test_second_layer_hit_backfills_platform_job_id(session) -> None:
    """第二层命中的记录如果这次抓到了平台 ID，要补上。

    补上之后，将来就走更可靠的第一层了（少一次模糊匹配的风险）。
    """
    first = job_svc.upsert_job(
        session, make_detail(platform="manual", platform_job_id=None, url=None)
    )
    second = job_svc.upsert_job(
        session,
        make_detail(platform="manual", platform_job_id="job-999"),
    )
    assert second.job_id == first.job_id
    job = job_svc.get_job(session, first.job_id)
    assert job is not None
    assert job.platform_job_id == "job-999"


# ===========================================================================
# 列表 / 跳过
# ===========================================================================
def test_list_jobs_filters(session) -> None:
    """按状态与平台过滤（`jh job list --status/--platform` 的后端）。"""
    job_svc.upsert_job(session, make_detail())
    job_svc.upsert_job(
        session,
        make_detail(platform="lagou", platform_job_id="L1", title="前端开发工程师"),
    )
    skipped = job_svc.upsert_job(
        session, make_detail(platform_job_id="job-003", title="数据开发工程师")
    )
    job_svc.mark_skipped(session, skipped.job_id)

    assert len(job_svc.list_jobs(session)) == 3
    assert len(job_svc.list_jobs(session, platform="boss")) == 2
    assert len(job_svc.list_jobs(session, status="skipped")) == 1
    assert len(job_svc.list_jobs(session, limit=2)) == 2


def test_mark_skipped_is_not_undone_by_refetch(session) -> None:
    """★ 端到端的核心场景：我 skip 掉的岗位，再抓一次不能变回 new。

    这是评审点出来的真实风险：如果去重时顺手覆盖 status，
    用户会看到"我明明排除过的岗位又出现在清单里"。
    """
    first = job_svc.upsert_job(session, make_detail())
    job_svc.mark_skipped(session, first.job_id, reason="测试")
    session.flush()

    job_svc.upsert_job(session, make_detail(jd_text="更新过的 JD 内容。\n" * 10))
    session.flush()

    job = job_svc.get_job(session, first.job_id)
    assert job is not None
    assert job.status == "skipped"


def test_count_by_status(session) -> None:
    """状态计数（CLI 汇总行用）。"""
    job_svc.upsert_job(session, make_detail())
    skipped = job_svc.upsert_job(session, make_detail(platform_job_id="j2"))
    job_svc.mark_skipped(session, skipped.job_id)
    session.flush()

    counts = job_svc.count_by_status(session)
    assert counts.get("new") == 1
    assert counts.get("skipped") == 1


def test_source_keyword_is_recorded(session) -> None:
    """★ 记录来源搜索词 —— 将来 `jh stats` 回答"哪个渠道有效"就靠它。"""
    result = job_svc.upsert_job(session, make_detail(), source_keyword="AI Agent 深圳")
    job = job_svc.get_job(session, result.job_id)
    assert job is not None
    assert job.source_keyword == "AI Agent 深圳"

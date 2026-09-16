"""岗位业务：入库 + **两层去重** + 列表（P2）。

╔══════════════════════════════════════════════════════════════════════════╗
║ ★★ 本模块最重要的一条纪律：去重命中时**绝不覆盖人工决策字段**             ║
╚══════════════════════════════════════════════════════════════════════════╝

我把 `jobs` 表的字段分成三类，只有第一类允许被"再次抓取"覆盖：

    ┌─ ① 抓取派生字段（每次抓到都可以更新）
    │    jd_text / jd_hash / jd_fetch_status / salary_* / experience / education
    │    / is_intern / city / company / title / url / last_seen_at
    │
    ├─ ② 人工决策字段（**绝不覆盖**）
    │    status            我已经把它标成 skipped / applied 了
    │    matched_resume_id 我手工指定过用哪份简历
    │    match_score / llm_score / rule_score / rule_detail_json / reason_json
    │                     已经花过成本（含 LLM 调用费）算出来的结果
    │    jd_keywords_json  共享中间层的产物，P3/P4/P5 都复用它
    │
    └─ ③ 我批注的字段
         note（将来会有）

★ 为什么这条纪律值得单独立一块注释

    去重命中时"顺手把新抓到的值 UPDATE 进去"是**最自然的写法**，
    也是最有破坏性的：我已经 `jh job skip` 掉的岗位会被悄悄改回 new、
    第二天又出现在待投清单里；已经算好的分数被清空 → 重复付费调用 LLM；
    `jh match set` 的人工裁决被抹掉。而且这些都不会报错，只会让人觉得
    "这工具怎么老是推我已经排除的岗位"。

    这个坑在技术评审里被明确点出来过，所以实现里用**显式白名单**的方式写：
    只更新列在 `_REFRESHABLE_FIELDS` 里的字段，其余一律不动。

★ 为什么要两层去重

    · 第一层 `platform_job_id`（平台内 ID）：最可靠，但**只有从 URL 抓到才有**；
      手动粘贴的岗位没有它。
    · 第二层 `dedup_norm_hash`（公司+职位+城市 归一化）：兜底层。
      它更宽松，所以规则要**保守**（见 `compute_dedup_norm_hash` 的说明）。

    两层都不命中 → 新记录。任何一层命中 → 走"只刷新抓取派生字段"的路径。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobhunter.adapters.base import JobDetail
from jobhunter.adapters.parse_text import normalize_for_dedup
from jobhunter.models import Job
from jobhunter.utils.time import now_utc

logger = logging.getLogger(__name__)

__all__ = [
    "DECISION_FIELDS",
    "REFRESHABLE_FIELDS",
    "UpsertOutcome",
    "UpsertResult",
    "compute_dedup_norm_hash",
    "compute_jd_hash",
    "find_duplicate",
    "get_job",
    "list_jobs",
    "mark_skipped",
    "upsert_job",
]

# ---------------------------------------------------------------------------
# 字段白名单（本模块的核心约定）
# ---------------------------------------------------------------------------
#: ① 抓取派生字段：重复遇到同一岗位时**允许刷新**
REFRESHABLE_FIELDS: tuple[str, ...] = (
    "company",
    "title",
    "city",
    "experience",
    "education",
    "is_intern",
    "salary_text",
    "salary_min",
    "salary_max",
    "jd_text",
    "jd_hash",
    "jd_fetch_status",
    "url",
    "source_keyword",
    "platform_job_id",
    "dedup_norm_hash",
)

#: ② 人工决策 / 已花成本算出来的字段：**任何情况下都不被去重逻辑覆盖**
#: （这份清单是"声明式"的：即使将来有人往 upsert 里加了字段，
#:   只要它在这里，测试 `test_dedup_never_touches_decision_fields` 就会拦住。）
DECISION_FIELDS: tuple[str, ...] = (
    "status",
    "match_score",
    "llm_score",
    "rule_score",
    "rule_detail_json",
    "reason_json",
    "matched_resume_id",
    "jd_keywords_json",
    "found_at",
)


@dataclass
class UpsertResult:
    """一次入库的结果（给 CLI 展示用）。"""

    #: created（新岗位）/ refreshed（重复遇到，已刷新抓取字段）/ unchanged（重复且内容没变）
    outcome: UpsertOutcome
    job_id: int
    #: 命中的是哪一层去重（便于排查"为什么这两个被认为是同一个岗位"）
    matched_by: str | None = None
    #: 实际被更新的字段名（**永不包含 DECISION_FIELDS**）
    updated_fields: list[str] = field(default_factory=list)
    #: 内容（JD 正文）是否发生变化 —— 变了才需要让 LLM 缓存失效
    jd_changed: bool = False

    @property
    def is_new(self) -> bool:
        return self.outcome == "created"


#: 取值：created / refreshed / unchanged
UpsertOutcome = str


# ---------------------------------------------------------------------------
# 哈希
# ---------------------------------------------------------------------------
def compute_jd_hash(jd_text: str | None) -> str | None:
    """JD 正文的 hash（LLM 缓存 key 的组成部分）。

    ★ 先归一化再哈希：招聘网站正文里常有大量空白/全半角差异，
      不归一化的话"同一份 JD 两次抓到"会得到不同的 hash，
      缓存直接失效（每次都要重新花钱调 LLM）。
    """
    if not jd_text or not jd_text.strip():
        return None
    normalized = normalize_for_dedup(jd_text)
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def compute_dedup_norm_hash(
    company: str | None,
    title: str | None,
    city: str | None,
) -> str | None:
    """去重第二层的归一化 hash（公司 + 职位 + 城市）。

    ★★ 规则的取舍：**宁可漏合并，也不要错合并**

        错合并（把两个不同岗位当成一个）的后果很严重：其中一个岗位的
        `status` 会被另一个的记录吃掉、分数混在一起，用户会看到
        "我明明没投过这个岗位，它却显示已投"。

        漏合并（同一个岗位留下两条）的后果轻得多：清单里出现两条相似的，
        用户 `jh job skip` 掉一条就行。

    所以：
        · **三个字段缺任何一个就返回 None**（不做"有什么拼什么"的部分匹配）——
          "公司+职位"相同但城市未知时合并，很容易把北京和深圳的两个同名岗位并成一个；
        · 归一化时去掉薪资、"急招/高薪"等修饰词与所有标点（见 `normalize_for_dedup`）；
        · 刻意**不删**"实习/校招"：它们改变岗位性质，实习和全职是两个岗位。
    """
    if not company or not title or not city:
        return None
    normalized = normalize_for_dedup(company, title, city)
    if not normalized or normalized.count("|") != 2:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 去重查找
# ---------------------------------------------------------------------------
def find_duplicate(session: Session, detail: JobDetail) -> tuple[Job | None, str | None]:
    """按三层顺序查找是否已存在同一个岗位。

    Returns:
        `(命中的 Job 或 None, 命中的层名)`

    ★ 三层判定 + 一个**否决条件**：

        ① 平台 + platform_job_id 精确命中   → 同一个岗位（最可靠）
        ② **URL 完全相同**                   → 同一个岗位
        ③ 归一化 hash（公司 + 职位 + 城市）   → 同一个岗位（兜底）
        ④ 否决：两个"都非空且不同"的平台 ID 之间，不做模糊合并

    ②（URL）为什么必须单独加一层（实测踩出来的缺陷）：

        公司自建招聘站（美团这类）的页面**根本不写公司名** ——
        于是 ③ 需要的"公司 + 职位 + 城市"三件套缺一件，直接放弃匹配。
        实测后果：**同一个链接加三次，库里出现三条一模一样的记录**。

        而"URL 一模一样"是极强的证据：同一个链接第二次打开还是同一个岗位。
        它比 ③ 可靠、又比 ① 宽容（很多站点把岗位 ID 藏在 JS 里，URL 里取不到）。

    ④ 为什么必要（另一个实测缺陷）：同一家公司同城挂两个相似标题的岗位
        （`AI Agent 开发工程师` 与 `…（急招）`），平台 ID 是 job-001 / job-002。
        没有 ④ 时 ③ 会命中，接着"刷新"逻辑会把那条的 `platform_job_id`
        **从 job-001 改成 job-002** —— 记录换了身份，而用户已投递的那条会挂错岗位。
    """
    if detail.platform_job_id:
        found = session.scalars(
            select(Job).where(
                Job.platform == detail.platform,
                Job.platform_job_id == detail.platform_job_id,
            )
        ).first()
        if found is not None:
            return found, "platform_job_id"

    if detail.url:
        found = session.scalars(select(Job).where(Job.url == detail.url)).first()
        if found is not None:
            return found, "url"

    norm_hash = compute_dedup_norm_hash(detail.company, detail.title, detail.city)
    if norm_hash:
        found = session.scalars(select(Job).where(Job.dedup_norm_hash == norm_hash)).first()
        if found is not None:
            # ---- 否决条件：两个不同的平台 ID 之间不做模糊合并 --------------
            existing_id = found.platform_job_id
            incoming_id = detail.platform_job_id
            if existing_id and incoming_id and existing_id != incoming_id:
                logger.info(
                    "归一化 hash 相同但平台岗位 ID 不同（库内 %s vs 新抓到 %s），"
                    "判定为两个不同岗位，不合并",
                    existing_id,
                    incoming_id,
                )
                return None, None
            return found, "dedup_norm_hash"

    return None, None


# ---------------------------------------------------------------------------
# 入库
# ---------------------------------------------------------------------------
def upsert_job(
    session: Session,
    detail: JobDetail,
    *,
    source_keyword: str | None = None,
) -> UpsertResult:
    """把一个 `JobDetail` 写进库（命中重复则只刷新抓取派生字段）。

    ★ `source_keyword` 记的是"这个岗位是从哪个搜索词/渠道来的"
      → 将来 `jh stats` 回答"哪个渠道有效"就靠它。
    """
    existing, matched_by = find_duplicate(session, detail)
    jd_hash = compute_jd_hash(detail.jd_text)
    norm_hash = compute_dedup_norm_hash(detail.company, detail.title, detail.city)

    if existing is None:
        job = Job(
            platform=detail.platform,
            platform_job_id=detail.platform_job_id,
            company=detail.company,
            title=detail.title,
            city=detail.city,
            experience=detail.experience,
            education=detail.education,
            is_intern=detail.is_intern,
            salary_text=detail.salary_text,
            salary_min=detail.salary_min,
            salary_max=detail.salary_max,
            jd_text=detail.jd_text,
            jd_hash=jd_hash,
            jd_fetch_status=detail.fetch_status,
            url=detail.url,
            source_keyword=source_keyword,
            dedup_norm_hash=norm_hash,
            status="new",
            found_at=now_utc(),
            last_seen_at=now_utc(),
        )
        session.add(job)
        session.flush()
        logger.info(
            "新岗位入库：id=%s %s / %s（%s）",
            job.id,
            detail.company or "未知公司",
            detail.title or "未知岗位",
            detail.city or "未知城市",
        )
        return UpsertResult(outcome="created", job_id=job.id)

    # ---- 命中已有记录：只刷新抓取派生字段 ---------------------------------
    updated: list[str] = []
    jd_changed = False

    # ★ 更新前先落地"看起来像新值的占位"检查：绝不触碰 DECISION_FIELDS
    for name in REFRESHABLE_FIELDS:
        if name in DECISION_FIELDS:  # pragma: no cover - 防御性断言，测试里也有
            raise AssertionError(
                f"编程错误：{name} 同时出现在可刷新与决策字段里 —— 决策字段绝不能被去重覆盖"
            )
        if name == "platform_job_id":
            new_value = detail.platform_job_id
        elif name == "dedup_norm_hash":
            new_value = norm_hash
        elif name == "jd_hash":
            new_value = jd_hash
        elif name == "source_keyword":
            new_value = source_keyword
        elif name == "jd_fetch_status":
            # ★ 字段名映射：列名是 `jd_fetch_status`，而 JobDetail 上的字段叫 `fetch_status`。
            #   不加这一条的话 `getattr(detail, "jd_fetch_status", None)` 永远是 None，
            #   抓取状态**一次都不会被刷新**（实测踩过：抓失败的岗位仍然显示 ok，
            #   于是 P3 会基于空文本打分而用户完全不知道原因）。
            new_value = detail.fetch_status
        else:
            new_value = getattr(detail, name, None)

        # JD 正文与抓取状态要一起看：
        #   · 只有内容真的变了才覆盖正文（否则 jd_hash 会跟着抖动，P3 的 LLM 缓存白白失效）；
        #   · **但抓取状态必须如实反映最后一次抓取的结果** —— 它是我们自己派生出来的
        #     元信息，不是人工决策。抓失败却仍显示 ok 的话，P3 会基于空文本打分，
        #     而用户完全不知道为什么分数很低。
        if name == "jd_text":
            jd_changed = bool(new_value) and new_value != existing.jd_text
            if not jd_changed:
                continue
        elif name == "jd_hash":
            if not jd_changed:
                continue
        elif name == "jd_fetch_status":
            if new_value in (None, "") or existing.jd_fetch_status == new_value:
                continue
            existing.jd_fetch_status = new_value
            updated.append(name)
            continue

        # ★ platform_job_id 特殊处理：**只在库里那条没有 ID 时补上**。
        #   不能无条件覆盖 —— 万一两层判定出现意外（比如归一化 hash 碰撞），
        #   覆盖 ID 会让记录"换了个身份"，而它的投递记录还挂在原来那个岗位上。
        if name == "platform_job_id":
            if existing.platform_job_id:
                continue
            if not new_value:
                continue
            setattr(existing, name, new_value)
            updated.append(name)
            continue

        # 空值不覆盖已有值：抓取失败时不该把之前抓到的字段清空
        if new_value in (None, ""):
            continue
        if getattr(existing, name, None) == new_value:
            continue

        setattr(existing, name, new_value)
        updated.append(name)

    existing.last_seen_at = now_utc()

    session.flush()
    logger.info(
        "岗位已存在（命中 %s）：id=%s，刷新 %d 个抓取字段，决策字段保持不变",
        matched_by,
        existing.id,
        len(updated),
    )
    return UpsertResult(
        outcome="refreshed" if updated else "unchanged",
        job_id=existing.id,
        matched_by=matched_by,
        updated_fields=updated,
        jd_changed=jd_changed,
    )


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def get_job(session: Session, job_id: int) -> Job | None:
    """按 id 取岗位。"""
    return session.get(Job, job_id)


def list_jobs(
    session: Session,
    *,
    status: str | None = None,
    platform: str | None = None,
    limit: int | None = None,
    order_by: str = "recent",
) -> list[Job]:
    """列出岗位。

    Args:
        status: 按状态过滤（new / scored / queued / applied / skipped）
        platform: 按平台过滤（boss / manual / official …）
        order_by: `recent`（最近入库在前）/ `score`（匹配分高在前）

    ★ 默认按 `found_at` 倒序：用户刚导入完最想看的就是"刚才那批"。
      按分数排序是 `jh today` 的事（P4），这里不做越权的事。
    """
    statement = select(Job)
    if status:
        statement = statement.where(Job.status == status)
    if platform:
        statement = statement.where(Job.platform == platform)

    if order_by == "score":
        statement = statement.order_by(Job.match_score.desc().nullslast(), Job.id.desc())
    else:
        statement = statement.order_by(Job.found_at.desc(), Job.id.desc())

    if limit:
        statement = statement.limit(limit)
    return list(session.scalars(statement))


def count_by_status(session: Session) -> dict[str, int]:
    """统计各状态的岗位数（CLI 表头与将来的 `jh today` 都要用）。"""
    rows = session.execute(select(Job.status, func.count()).group_by(Job.status)).all()
    return {status: count for status, count in rows}


def mark_skipped(session: Session, job_id: int, *, reason: str | None = None) -> Job | None:
    """把岗位标为"我主动跳过"。

    ★ 这是**人工决策**，所以要防止后续抓取把它改回去（`status` 在
      `DECISION_FIELDS` 里，去重逻辑不会碰它）。
    """
    job = get_job(session, job_id)
    if job is None:
        return None
    job.status = "skipped"
    if reason:
        # 现有表结构里 jobs 没有 note 字段，先记进日志（P4.5 的事件流会承接）
        logger.info("跳过的原因（id=%s）：%s", job_id, reason)
    session.flush()
    return job


# ───────────────────────────────────────────────────────────────────────────
# 关于"把 JobDetail 转成摘要 dict"：这里**故意没有**这样一个函数
#
# P2 一开始写过一个 `detail_to_summary(detail) -> dict`，设想给"批量导入逐个报告"
# 和 `--dry-run` 共用。实际实现时两处都没用它：真正要打印的地方是
# `cli/commands/job_cmd.py::_print_details()`，它直接用 rich.Table 排 8 列
# （公司/职位/城市/薪资/实习/JD 字数/抓取状态），比"先转成 dict 再猜列顺序"直接得多。
#
# 于是它就成了第二个定义"岗位摘要长什么样"的地方 —— 与 CLI 里的表格**必然漂移**：
# 改了表头却忘了改 dict（或反过来），没人会发现。已删除。
#
# 结论同上一条：**别为了"将来两处共用"提前造中间层**，
# 等两个调用点真的都出现了、且形状确实一致时再抽。
# ───────────────────────────────────────────────────────────────────────────

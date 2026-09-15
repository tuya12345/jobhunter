"""岗位来源适配器契约（P0 定义接口，P2 实现）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 为什么 P0 就把接口定死                                                     ║
╚══════════════════════════════════════════════════════════════════════════╝

**接口是设计成果，不是实现细节。** 先把它钉下来，后面 P2 只是"往契约里填实现"：

    · services 层可以对着 `JobSource` 编程，不必关心是 Playwright 抓的、
      还是我手动粘贴的、还是（将来）某个 API 返回的；
    · 测试可以写一个假 adapter（直接返回构造好的 `JobDetail`），
      不用真的开浏览器 —— 这也是 CI 能保持秒级的原因。

★ 主路径是 `manual.py`（人给 URL → 抓详情），不是自动搜索翻页。
  BOSS 有 `__zp_stoken__` 签名 + 字体反爬，自动搜索大概率中期撞墙，
  而它是 MVP 的唯一入口、没有 fallback —— 所以主路径必须换。

★ 合规硬约束（写在这里，因为这里是唯一能拿到"外部访问能力"的地方）：
    1. 适配器**只做读**：抓详情、解析页面。**绝不**点击投递按钮、
       绝不点赞/私信/关注 —— 不做任何写操作。
    2. 频率克制：请求之间必须有随机延时（config 的 rate_limit），
       遵守翻页上限，出错即停；不绕过验证码/风控/签名。
    3. 失败必须能降级：抓不到就返回 `None` 并让上层提示"手动粘贴 JD"，
       绝不让整条链路断掉（网络不通也要能用 —— 这是 `--manual` 存在的理由）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

__all__ = ["JobDetail", "JobSource", "SearchResult"]


@dataclass
class JobDetail:
    """一个岗位的抓取结果（**适配器的唯一输出契约**）。

    设计取舍：
    · 字段全部可选（除 platform/jd_text）——网页结构千变万化，
      抓不到某个字段是常态，不该因此让整次抓取失败；
    · `jd_text` 是核心产物：宁可只要正文、其它字段全空，也不要"全字段但正文空"；
    · `raw` 保留原始数据（HTML 片段/接口 JSON），便于将来改解析规则时**离线重放**，
      不用重新抓一遍（既省时间也减少对目标站点的请求）。
    """

    # 平台标识：boss / manual / official …
    platform: str
    # JD 正文（核心产物）
    jd_text: str
    # ★ 平台内岗位 ID —— 去重最可靠的一层。抓不到留 None，靠 dedup hash 兜底。
    platform_job_id: str | None = None
    company: str | None = None
    title: str | None = None
    city: str | None = None
    experience: str | None = None
    education: str | None = None
    # None 表示"未识别"，**不要默认 False** —— 否则实习岗会被当成全职
    is_intern: bool | None = None
    salary_text: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    url: str | None = None
    # ok / failed / partial —— 抓取失败必须留痕（否则后面会基于空 JD 乱打分）
    fetch_status: str = "ok"
    # 原始数据（HTML/JSON），用于离线调试与解析规则迭代
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_usable(self) -> bool:
        """是否可用于后续匹配（至少要有关键词可抽的正文）。"""
        return bool(self.jd_text and self.jd_text.strip()) and self.fetch_status != "failed"

    def missing_fields(self) -> list[str]:
        """列出没抓到的字段（用于在 CLI 里友好提示"这几点没抓到"）。"""
        candidates = (
            "platform_job_id",
            "company",
            "title",
            "city",
            "experience",
            "education",
            "salary_text",
        )
        return [name for name in candidates if getattr(self, name) in (None, "")]


@dataclass
class SearchResult:
    """搜索结果条目（只有 `search()` 会返回）。

    ★ 注意：`search()` 属于**可选能力**，且本项目的主路径不使用它。
      保留这个类型是为了让"自动搜索 spike 通过"之后有地方放实现，
      而不是说搜索能力已经是产品的一部分（见 docs/roadmap.md 的 P2）。
    """

    url: str
    title: str | None = None
    company: str | None = None
    city: str | None = None
    salary_text: str | None = None
    platform_job_id: str | None = None


class JobSource(ABC):
    """岗位来源适配器的统一接口。

    实现者只需要保证两件事：
        1. `fetch_detail(url)` 返回 JobDetail 或 None（不抛异常给上层）；
        2. `platform` 属性标识来源（用于 jobs.platform 与去重第一层）。
    """

    #: 平台标识，子类必须覆盖（如 "manual" / "boss"）
    platform: str = "unknown"

    @abstractmethod
    def fetch_detail(self, url: str) -> JobDetail | None:
        """抓取单个岗位详情。

        Returns:
            JobDetail：成功（哪怕只抓到部分字段，也要把 fetch_status 标成 partial）
            None：彻底失败（网络不通/页面结构完全变了）
                 → 上层据此提示用户改用 `jh job add --manual` 手动粘贴

        ★ 实现约定：**不要向上抛异常**。抓取失败是预期内的正常情况，
          用返回值表达比用异常更清楚，也逼着实现者处理降级路径。
        """
        raise NotImplementedError

    def search(
        self, keyword: str, city: str | None = None, *, max_pages: int = 1
    ) -> list[SearchResult]:
        """搜索岗位（**可选能力，默认不支持**）。

        默认抛 NotImplementedError —— 这是一个**故意的**设计：
            · 主路径是"人给链接"，搜索本来就不是必需的；
            · BOSS 的自动搜索必须先过 spike 验证，没通过就不该有实现；
            · 一个明确报错的"未支持"，比一个偷偷返回空列表的假实现好得多
              （后者会让人误以为"搜了但没结果"）。
        """
        raise NotImplementedError(
            f"{type(self).__name__} 不支持搜索。"
            "本项目主路径是 `jh job add <url>`（人给链接）；"
            "自动搜索需要先通过 spikes/boss_search_spike.py 验证。"
        )

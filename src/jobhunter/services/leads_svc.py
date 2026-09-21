"""线索的**消费端**业务：导入 JSON 快照 + 把线索记成一次投递（P7，ISSUE #59）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 这个模块只做两件事，但两件都有"静默做错"的历史包袱                          ║
╚══════════════════════════════════════════════════════════════════════════╝

一、`import_snapshot()` —— 把采集端导出的 JSON 快照导进 `companies`

    用户 2026-09-19 把 P7 拆成了采集端（独立部署 `leadhunter`，命令 `lh`）与
    消费端（本仓库）。两边**不共用数据库**，唯一的接口就是那份 JSON 快照
    （契约见 `docs/P7-消费端API.md` 与 `docs/P7-消费端规格.md` §4）。

    ★★ 幂等是硬要求：同一份文件导两次必须是「新增 0 条、更新 N 条」。
       实现上**不靠**"服务层记得先查一下"，而是：

         · 匹配键存成 `companies.name_norm`，并加 **UNIQUE 索引**（迁移 0005）
           ——重复插入在数据库层就失败；
         · 匹配口径见 `company_key()`：**去掉所有空白**，与采集端内部的合并口径
           一字不差（刻意不用本仓库的 `normalize_for_dedup`，理由写在那函数上）。

       为什么把它变成库约束：这个项目因为"同一份数据多个写入口、口径不一致"
       吃过两次亏（#49 / #57），因为"形参加了但没人传"吃过三次（#37 / #24 / #55）。
       "记得先查"属于同一类靠自觉的约定，迟早会漏。

    ★ `--dry-run` **一个字都不写库**：`import_snapshot()` 先把"要做什么"算成一份
      计划（`ImportPlanRow` 列表），再决定是执行还是只报出来。这样 dry-run 与真跑走的是
      **同一段判定逻辑**，不会出现"dry-run 说没事、真跑写坏了"（那种 bug 最贵）。

    ★ 坏输入一律中文可操作提示（不是裸 `JSONDecodeError` / `KeyError` /
      pydantic 的英文堆栈）。要覆盖的坏输入：文件不存在 / 不是 JSON /
      不是 UTF-8 / `schema` 不认识 / `schema_version` 不认识 / `leads` 不是数组 /
      字段类型不对 / 空文件。验收会真的喂这几种。

二、`link_lead_to_application()` —— 页面点"标记投递"时唯一的写入口

    `applications.job_id` 是 NOT NULL，所以"把一条线索记成一次投递"必须指明
    **投到哪个岗位**（与采集端 `lh leads apply --job <id>` 同一语义）。
    状态机与事件仍然交给 `apply_svc.set_status()` —— **绝不另写一套写库逻辑**
    （#49/#57 的教训：同一份数据两个写入口，口径必然分叉）。

    ★ 幂等 + 如实反馈（ISSUE #57 的原症状）：第二次点同一条时，返回值里带着
      `created=False` 与"渠道从 X 变成 Y"，页面**必须说出来**，
      不能看起来和第一次一模一样。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobhunter.models import Company, Job

# ★ 公司名的匹配键定义在**模型层**（它就是 `name_norm` 这一列的含义），
#   这里 import 过来用，避免「同一个口径两处实现」：
from jobhunter.models.company import company_key
from jobhunter.services import apply_svc
from jobhunter.utils.time import now_utc, to_naive_utc, to_utc

__all__ = [
    "SNAPSHOT_SCHEMA",
    "SUPPORTED_SCHEMA_VERSIONS",
    "ImportPlanRow",
    "ImportReport",
    "LeadLinkResult",
    "LeadSnapshotError",
    "Reachability",
    "company_key",
    "grouped_leads",
    "import_snapshot",
    "iso_utc",
    "link_lead_to_application",
    "parse_snapshot",
    "reachability_of",
]

#: 快照的 `schema` 字段唯一合法值
SNAPSHOT_SCHEMA = "jobhunter.leads"
#: 认识的 `schema_version`。★ 不认识就报错，**不"尽力而为地猜"**
#:   （猜错的后果是字段错位入库，比直接失败糟得多）
SUPPORTED_SCHEMA_VERSIONS: tuple[int, ...] = (1,)

#: `source_type` 的合法取值 —— 与 `models/constants.py::CHANNELS` 同源，
#: 因为线索的 source_type 最终会变成投递记录的 `channel`（渠道转化率的口径）
LEAD_SOURCE_TYPES: tuple[str, ...] = ("官网", "内推", "HR直招")

#: 页面/接口上的分组顺序（`None` 归到"未分类"）
SOURCE_TYPE_ORDER: tuple[str | None, ...] = (*LEAD_SOURCE_TYPES, None)


class LeadSnapshotError(ValueError):
    """快照读不了 / 不合契约 —— 消息一律是**中文可操作提示**。"""


# ---------------------------------------------------------------------------
# 快照的 schema（采集端 ↔ 消费端 的唯一接口）
# ---------------------------------------------------------------------------
class _SnapshotLead(BaseModel):
    """快照里的一条线索。

    ★ 每一条 extra="forbid" 是刻意的：采集端若加了字段而我们不认识，
      宁可报错让两边对齐契约，也不要"默默忽略一个字段"——
      那正是"契约漂移"的开始（规格里也写了：schema 有问题就提出来，别单方面改）。
    """

    model_config = ConfigDict(extra="forbid")

    external_id: int | str | None = None
    name: str = Field(min_length=1)
    official_url: str | None = None
    source_platform: str | None = None
    source_type: str | None = None
    evidence_text: str | None = None
    evidence_url: str | None = None
    note: str | None = None
    http_status: int | None = Field(default=None, ge=0, le=599)
    checked_at: datetime | None = None
    collected_at: datetime | None = None

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("公司名不能是空白")
        return value.strip()

    @field_validator("source_type")
    @classmethod
    def _known_source_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in LEAD_SOURCE_TYPES:
            raise ValueError(
                f"source_type 只能是 {'/'.join(LEAD_SOURCE_TYPES)} 或 null，实际是 {value!r}"
            )
        return value

    @field_validator("official_url", "evidence_url")
    @classmethod
    def _only_http_urls(cls, value: str | None) -> str | None:
        """★ 只接受 `http` / `https`（只读代码审查 JH-10）。

        为什么必须在这一层拦（而不是"页面上小心点"）：

          · 这两个字段会**原样变成页面上的 `href`**（`web/src/components/LeadCard.tsx`）。
            `file:///C:/secret.txt`、`javascript:…`、`data:text/html,…` 都不是"官网链接"，
            却都能被浏览器当成可点的东西 —— 快照是**外部输入**（采集端那个仓库导出的
            JSON），让它决定页面能打开什么协议，是毫无必要的信任；
          · 字段语义本来就写着"官网地址 / 原始出处"，收下一个 `file:` 值意味着
            这个契约从第一天起就是假的。

        ★ 报错要给出**正确写法**：写错的九成是采集端把"本地另存的文件路径"
          当成了官方地址（那是 `evidence_text` 该干的事）。所以消息里直接说清。
        """
        if value is None:
            return None
        text = value.strip()
        if not text:
            return None
        scheme = urlsplit(text).scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError(
                f"不是 http/https 链接：{text!r}（协议 {scheme or '（没有协议）'}）。\n"
                "  期望：完整的网址，如 https://example.com/careers/123。\n"
                "  本地文件路径、file:// 与 data: 都不接受 —— 这不是'官网地址'，"
                "而且它会被渲染成页面上可点的链接。"
            )
        return text

    @field_validator("checked_at", "collected_at")
    @classmethod
    def _must_have_timezone(cls, value: datetime | None) -> datetime | None:
        """★ 契约要求"时间一律 ISO 8601 带时区"——naive 时间**报错而不是猜**。

        猜时区是最糟的一种"友好"：猜错 8 小时，页面上"什么时候检查的"就永远是错的，
        而且没人会发现。所以这里直接拒绝，并告诉用户正确写法。
        """
        if value is None:
            return None
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(
                f"时间 {value.isoformat()!r} 没有时区：契约要求 ISO 8601 带时区"
                "（如 2026-09-19T18:47:00+08:00 或 …+00:00）"
            )
        return value


class _SnapshotGenerator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str | None = None
    version: str | None = None


class LeadSnapshot(BaseModel):
    """一份完整的快照。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["jobhunter.leads"] = Field(alias="schema")
    schema_version: int
    exported_at: datetime | None = None
    generator: _SnapshotGenerator | None = None
    leads: list[_SnapshotLead]


def parse_snapshot(path: str | Path) -> LeadSnapshot:
    """读文件 → 校验 → 返回快照模型；任何问题都转成中文可操作提示。

    Raises:
        LeadSnapshotError: 文件不存在 / 不是 UTF-8 / 不是 JSON / 契约不符。
    """
    target = Path(path)
    if not target.exists():
        nearby = sorted(p.name for p in target.parent.glob("*.json"))[:5]
        raise LeadSnapshotError(
            f"找不到快照文件：{target}\n"
            "  · 采集端导出后是一个 .json 文件（`lh leads export --format json`）\n"
            f"  · 路径写错了？这个目录下的 json 文件有：{'、'.join(nearby) or '（一个都没有）'}"
        )
    if target.is_dir():
        raise LeadSnapshotError(f"{target} 是一个目录，不是文件 —— 请指向导出的那个 .json")

    try:
        raw = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise LeadSnapshotError(
            f"快照不是 UTF-8 编码（{target}）：{exc}\n"
            "  · 采集端导出的一定是 UTF-8；如果是手工编辑过，请另存为 UTF-8\n"
            "  · 也可能是文件被 Excel/记事本改成了 GBK"
        ) from exc
    except OSError as exc:
        raise LeadSnapshotError(f"读不了这个文件：{target}（{exc}）") from exc

    if not raw.strip():
        raise LeadSnapshotError(f"快照是空文件：{target}（导出时是不是失败了？）")

    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        head = raw.lstrip()[:40].replace("\n", " ")
        raise LeadSnapshotError(
            f"不是合法 JSON：{target}（第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}）\n"
            f"  文件开头是这样：{head!r}\n"
            "  · 快照应当是 `{'schema': 'jobhunter.leads', …}` 这样的对象\n"
            "  · 如果是 HTML/登录页，说明导出那一步没有真的导出"
        ) from exc

    return _validate_snapshot(data, target)


def _validate_snapshot(data: Any, target: Path) -> LeadSnapshot:
    """把"结构对不对"的检查集中在这里，报错要指出**哪里不对、怎么改**。"""
    if not isinstance(data, dict):
        raise LeadSnapshotError(
            f"快照顶层应该是一个对象（{{'schema': …, 'leads': […]}}），"
            f"实际是 {type(data).__name__}：{target}"
        )

    schema = data.get("schema")
    if schema != SNAPSHOT_SCHEMA:
        raise LeadSnapshotError(
            f"这不是本工具认识的快照：schema={schema!r}，期望 {SNAPSHOT_SCHEMA!r}\n"
            "  · 是不是把别的 JSON（比如岗位列表）当成线索快照导进来了？"
        )

    version = data.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        supported = "、".join(str(v) for v in SUPPORTED_SCHEMA_VERSIONS)
        raise LeadSnapshotError(
            f"快照版本不认识：schema_version={version!r}，本版本支持 {supported}\n"
            "  · 采集端可能比本仓库新 —— 请一起升级，或让采集端用旧版本导出\n"
            "  · 刻意**不做**「尽力而为地猜」：字段错位入库比直接失败糟得多"
        )

    leads = data.get("leads")
    if not isinstance(leads, list):
        raise LeadSnapshotError(
            f"`leads` 必须是数组（空数组是合法的，表示这次没采到），实际是 {type(leads).__name__}"
        )

    try:
        return LeadSnapshot.model_validate(data)
    except ValidationError as exc:
        raise LeadSnapshotError(_format_validation_error(exc, leads)) from exc


def _format_validation_error(exc: ValidationError, leads: Any) -> str:
    """把 pydantic 的校验错误翻译成"第几条线索的哪个字段不对"。"""
    lines = ["快照里有字段不符合契约："]
    for error in exc.errors()[:6]:
        location = " → ".join(str(part) for part in error["loc"])
        message = error["msg"]
        hint = ""
        if location.startswith("leads → ") and isinstance(leads, list):
            parts = error["loc"]
            # loc 形如 ('leads', 0, 'source_type')
            if len(parts) >= 2 and isinstance(parts[1], int):
                index = parts[1]
                name = ""
                if index < len(leads) and isinstance(leads[index], dict):
                    name = str(leads[index].get("name") or "（没写公司名）")
                hint = f"（第 {index + 1} 条线索「{name}」）"
        lines.append(f"  · {location}{hint}：{message}")
    lines.append(
        "  契约见 docs/P7-消费端API.md；如果是采集端多/少字段，请把差异写进交付声明，"
        "不要单方面改字段名。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 可达性：**三态**（"未检查" ≠ "失效"，这是规格点名的验收项）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Reachability:
    """一条线索的官网可达性（页面直接照着渲染）。"""

    state: Literal["checked_ok", "checked_error", "unchecked"]
    label: str
    http_status: int | None
    checked_at: datetime | None
    reason: str


def reachability_of(company: Company) -> Reachability:
    """把 `http_status` / `official_url` 翻译成三态 + 一句人话解释。

    ★★ 为什么必须有第三态：采集端的口径是「`http_status` 为空 = **未检查**」，
      而"没检查"有两种完全不同的原因：

        · 这条线索**压根没有官网网址**（内推/HR直招常见）→ 没什么可检查的；
        · **有网址，但当时没检查**（网络不通、或没跑检查那一步）→ 值得回头再试。

      把它们和"检查过、404"合并成一个"失效"，用户就会去放弃一条其实还能投的线索。
      所以 `reason` 必须把这两种原因说清楚，页面也照原样显示。
    """
    status = company.http_status
    if status is None:
        if company.official_url:
            reason = "有网址，但当时没检查（网络不通或没跑检查）"
        else:
            reason = "没有官网网址，无法检查"
        return Reachability(
            state="unchecked",
            label="未检查",
            http_status=None,
            checked_at=company.checked_at,
            reason=reason,
        )
    if 200 <= status < 400:
        return Reachability(
            state="checked_ok",
            label=f"可达（HTTP {status}）",
            http_status=status,
            checked_at=company.checked_at,
            reason=f"检查过：HTTP {status}",
        )
    return Reachability(
        state="checked_error",
        label=f"HTTP {status}",
        http_status=status,
        checked_at=company.checked_at,
        reason=f"检查过：HTTP {status}（入口可能已失效）",
    )


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------
@dataclass
class ImportPlanRow:
    """ "这条线索会被怎么处理"—— dry-run 就是把这批计划打印出来。"""

    name: str
    name_norm: str
    action: Literal["add", "update"]
    company_id: int | None = None
    #: 会真的改动哪些字段（更新时才有意义；空表示"内容没变，只是又导了一次"）
    changed_fields: tuple[str, ...] = ()
    #: 同一份文件里重复出现的公司（后面的覆盖前面的）
    duplicate_in_file: bool = False


@dataclass
class ImportReport:
    """导入结果（**如实报数**，不静默覆盖）。"""

    total_in_file: int
    added: int
    updated: int
    #: 更新里"内容真的有变化"的条数（`updated - unchanged` 就是没变化的）
    changed: int
    #: 文件里重复出现的公司数（已按"后面的为准"合并）
    duplicates_in_file: int
    dry_run: bool
    rows: list[ImportPlanRow] = field(default_factory=list)

    @property
    def unchanged(self) -> int:
        return self.updated - self.changed

    def summary(self) -> str:
        head = "（dry-run：什么都没写库）" if self.dry_run else ""
        parts = [f"新增 {self.added} 条", f"更新 {self.updated} 条"]
        if self.updated:
            parts[-1] += f"（其中 {self.changed} 条内容有变化）"
        if self.duplicates_in_file:
            parts.append(f"合并同文件重复 {self.duplicates_in_file} 条")
        return f"{head}共 {self.total_in_file} 条 → " + "、".join(parts)


#: 快照字段 → `companies` 列。★ 只列**采集端带过来的事实**，
#: 本仓库自己的列（`verified`、`created_at`）另行处理，避免"导入顺手改别的列"。
_SNAPSHOT_TO_COLUMN: dict[str, str] = {
    "name": "name",
    "official_url": "official_url",
    "source_platform": "source_platform",
    "source_type": "source_type",
    "evidence_text": "evidence_text",
    "evidence_url": "evidence_url",
    "note": "note",
    "http_status": "http_status",
    "checked_at": "checked_at",
    "collected_at": "collected_at",
    "external_id": "external_id",
}


def _snapshot_value(lead: _SnapshotLead, key: str) -> Any:
    """取快照里某个字段的"入库值"（时间统一转成 naive UTC）。

    ★ 时区：契约是"ISO 8601 带时区"，库里一律**naive UTC**（全项目一致）。
      这里只做 UTC 归一，**不做本地化转换**（规格 §4 明确要求）——
      把北京时间转成北京时间再存，等于凭空多一次换算，只会引入错误。
    """
    value = getattr(lead, key)
    if isinstance(value, datetime):
        return to_naive_utc(to_utc(value))
    if key == "external_id" and value is not None:
        return str(value)
    return value


def _row_value(row: Company, column: str) -> Any:
    """从**已有行**上取值，用来和快照比较。

    ★★ 这里有个必须归一化的坑：`UTCDateTime` 在**读**的时候会给时间贴上 UTC 时区，
      而 `_snapshot_value()` 给出的是 **naive** UTC。直接比就是"aware != naive"——
      结果是**每次重新导入都报"有变化"、每次都真的去 UPDATE 那两列**
      （幂等看着还在，但"更新 N 条（其中 M 条无变化）"这句实话就变成了假话）。
      所以比较前统一成 naive UTC。
    """
    value = getattr(row, column)
    if isinstance(value, datetime):
        return to_naive_utc(value)
    return value


def import_snapshot(
    session: Session,
    path: str | Path,
    *,
    dry_run: bool = False,
) -> ImportReport:
    """导入一份快照。

    ★ 幂等：同一份文件导两次 → 「新增 0 条、更新 N 条」，库里行数不变。
      匹配键是 `name_norm`（见 `company_key()`，唯一索引在迁移 0005 里建好）。

    ★ `dry_run=True` 时**一个字都不写库**：只读现有行、算出计划就返回。
      与真跑共用同一段判定逻辑，所以不会出现"dry-run 说没事、真跑写坏了"。

    Raises:
        LeadSnapshotError: 文件或契约有问题（中文可操作提示）。
    """
    snapshot = parse_snapshot(path)

    # ---- 第一步：把同一份文件里的重复公司合并（后面的为准）---------------
    merged: dict[str, _SnapshotLead] = {}
    duplicates = 0
    order: list[str] = []
    for lead in snapshot.leads:
        key = company_key(lead.name)
        if key in merged:
            duplicates += 1
            merged[key] = lead
            continue
        merged[key] = lead
        order.append(key)

    # ---- 第二步：和库里的现状比对，算出计划 ------------------------------
    existing: dict[str, Company] = {}
    if order:
        rows = session.scalars(select(Company).where(Company.name_norm.in_(list(order)))).all()
        existing = {row.name_norm: row for row in rows}

    plan: list[ImportPlanRow] = []
    for key in order:
        lead = merged[key]
        row = existing.get(key)
        if row is None:
            plan.append(ImportPlanRow(name=lead.name, name_norm=key, action="add"))
            continue
        changed = _changed_fields(row, lead)
        plan.append(
            ImportPlanRow(
                name=lead.name,
                name_norm=key,
                action="update",
                company_id=row.id,
                changed_fields=changed,
            )
        )

    report = ImportReport(
        total_in_file=len(snapshot.leads),
        added=sum(1 for item in plan if item.action == "add"),
        updated=sum(1 for item in plan if item.action == "update"),
        changed=sum(1 for item in plan if item.action == "update" and item.changed_fields),
        duplicates_in_file=duplicates,
        dry_run=dry_run,
        rows=plan,
    )

    if dry_run:
        return report

    # ---- 第三步：执行（只有真跑才走到这里）--------------------------------
    for item in plan:
        if item.action == "update":
            _apply_snapshot(existing[item.name_norm], merged[item.name_norm])
        else:
            session.add(_new_company(merged[item.name_norm], item.name_norm))
    session.flush()
    return report


def _apply_snapshot(row: Company, lead: _SnapshotLead) -> None:
    """把快照的值写进已有行。

    ★ 规则（写下来免得以后有人"顺手改成只补空"）：

      · 快照里**出现**的字段 → 以快照为准，null 就是 null
        （这样"同一份文件导两次"的结果完全一致，幂等才是真的幂等）；
      · 快照里**没出现**的字段 → 不动本地的值（`model_fields_set` 判断），
        这样采集端将来"少导一个字段"不会把本地信息抹掉。

      本仓库自己维护的列（`created_at` / `verified`）不在这里改：
      `created_at` 记的是"这条线索什么时候进的本库"，`verified` 由 http_status 推导。
    """
    fields_set = lead.model_fields_set
    for key, column in _SNAPSHOT_TO_COLUMN.items():
        if key not in fields_set:
            continue
        value = _snapshot_value(lead, key)
        if key == "name":
            continue  # 名字是匹配键，不改写（归一化后相同、写法不同时保留首次的写法）
        if getattr(row, column) != value:
            setattr(row, column, value)
    row.verified = row.http_status is not None


def _new_company(lead: _SnapshotLead, name_norm: str) -> Company:
    """按快照建一行新线索（`created_at` 由模型默认值给：那是"进本库"的时间）。"""
    values = {column: _snapshot_value(lead, key) for key, column in _SNAPSHOT_TO_COLUMN.items()}
    return Company(
        name_norm=name_norm,
        verified=values.get("http_status") is not None,
        **values,
    )


def _changed_fields(row: Company, lead: _SnapshotLead) -> tuple[str, ...]:
    """列出这次导入**真的会改**的字段（用于"更新 N 条（其中 M 条有变化）"这句实话）。"""
    changed: list[str] = []
    for key, column in _SNAPSHOT_TO_COLUMN.items():
        if key == "name" or key not in lead.model_fields_set:
            continue
        if _row_value(row, column) != _snapshot_value(lead, key):
            changed.append(column)
    return tuple(changed)


# ---------------------------------------------------------------------------
# 页面要的数据（分组 + 三态）：只读，供 API 层直接返回
# ---------------------------------------------------------------------------
def grouped_leads(session: Session) -> list[tuple[str | None, list[Company]]]:
    """按 `source_type` 分组返回线索（顺序固定：官网 / 内推 / HR直招 / 未分类）。

    ★ 空分组也保留：页面上"内推 0 条"本身就是信息（一眼看出这类线索还没采到）。
    """
    rows = session.scalars(select(Company).order_by(Company.source_type, Company.id)).all()
    buckets: dict[str | None, list[Company]] = {key: [] for key in SOURCE_TYPE_ORDER}
    for row in rows:
        key = row.source_type if row.source_type in LEAD_SOURCE_TYPES else None
        buckets[key].append(row)
    return [(key, buckets[key]) for key in SOURCE_TYPE_ORDER]


# ---------------------------------------------------------------------------
# 标记投递：页面唯一的写动作
# ---------------------------------------------------------------------------
@dataclass
class LeadLinkResult:
    """一次"把线索记成投递"的结果 —— 每个字段都是为了**如实说出来**。"""

    lead_id: int
    lead_name: str
    application_id: int
    job_id: int
    #: True = 这次新建了一条投递记录；False = 复用了已有那条（幂等）
    created: bool
    channel: str | None
    channel_before: str | None
    channel_changed: bool
    status: str
    status_before: str
    status_changed: bool
    #: ★ 这个岗位的投递记录**已经属于另一条线索**，所以这次没有改它的归属（JH-02）。
    #:   页面必须把这件事说出来 —— 否则用户会以为"点了标记，这条线索就记上了"。
    owned_by_other_lead: bool = False
    #: 一句人话，页面原样显示（区分"新建 / 更新了啥 / 什么都没变"）
    message: str = ""


def link_lead_to_application(
    session: Session,
    job_id: int,
    lead: Company,
    *,
    channel: str | None = None,
    status: str = "applied",
    source: str = "jh leads serve",
) -> LeadLinkResult:
    """把一条线索记成"某个岗位的一次投递"。

    ★ 为什么必须指明岗位（而不是"给线索打个已投标记"）：

      `applications.job_id` 是 NOT NULL，而且这个工具的全部复盘价值都建立在
      "投递是**针对某个岗位**的一次动作"上（`jh stats` 按渠道/岗位算转化率）。
      给线索单独存一个"已投"布尔值，就会出现两套口径：看板说没投、线索页说投了。
      所以这里与采集端 `lh leads apply --job <id>` 保持同一语义。

    ★ 状态机交给 `apply_svc.set_status()`：它会追加 `application_events`、
      同步岗位状态、处理闭环字段 —— 这些都是"看板可复盘"的前提。
      本函数**只负责**"找到/建出那条投递记录 + 记渠道"。

    ★ 幂等 + 如实反馈：重复点同一条不会多出一行；返回值里带
      `created` / `channel_before` / `status_before`，让页面能说清
      "这次更新了什么"（ISSUE #57 就是"改了渠道却一声不吭"）。

    Raises:
        apply_svc.ApplicationError: 岗位不存在 / 状态不认识。
    """
    job = session.get(Job, job_id)
    if job is None:
        raise apply_svc.ApplicationError(f"找不到岗位 id={job_id}：请先用 `jh job add <链接>` 录入")

    desired_channel = channel or lead.source_type
    application, created = apply_svc.ensure_pending_application(session, job)

    # ★★ 记下"这条投递是从哪条线索来的"（迁移 0005 的 applications.company_id）。
    #   页面靠它显示"这条线索投过没" —— 用渠道反推是猜，会点错线索（见模型注释）。
    #
    #   ⚠️⚠️ 但**绝不改写已经属于另一条线索的归属**（只读代码审查 JH-02）。
    #
    #   为什么：`applications.job_id` 上的唯一索引决定"一个岗位只有一条投递记录"
    #   （JH-11），所以"线索 A 投过岗位 J 之后，再从线索 B 标记同一个 J"
    #   复用的一定是 A 那条。原来这里无条件 `application.company_id = lead.id`，
    #   于是 A 的页面从"已投"变成"未投"、B 变成"已投" —— 一次**静默的归属转移**，
    #   历史被改写，而两个页面各自都显得很正常（最难查的那类失真）。
    #
    #   现在的口径：
    #     · 这条投递本来没有线索（`company_id is None`，`jh apply` 那条路径建的）
    #       → 认领它（这正是本列存在的意义）；
    #     · 已经属于**这条**线索 → 幂等，什么都不用改；
    #     · 已经属于**别的**线索 → 保持原样，并在返回值里如实说出来
    #       （`owned_by_other_lead=True` + 一句人话），由人来决定要不要改归属。
    owned_by_other_lead = False
    if lead.id is not None:
        if application.company_id is None:
            application.company_id = lead.id
        elif application.company_id != lead.id:
            owned_by_other_lead = True

    # ★★ `channel_before` 只在"**已有**记录被改写"时才算一次变化。
    #
    #   为什么专门处理 `created`：`ensure_pending_application()` 建新行时会先播一个
    #   默认渠道（`招聘网站`，那是给"从 BOSS 这类平台投的"准备的）。如果把它当成
    #   "改之前的渠道"，用户第一次标记就会看到「渠道 招聘网站 → 官网」——
    #   一次**新建**被描述成一次**改写**，而"招聘网站"根本不是他做过的选择。
    #   那正是 #57 那一类失真（描述与事实不符），只是方向相反。
    channel_before: str | None = None
    channel_changed = False
    if created:
        # 新记录：直接把渠道写成想要的值（没有"之前"可言）
        if desired_channel:
            application.channel = desired_channel
    else:
        channel_before = application.channel
        # ★ 只有"明确指定了渠道"才改写；推导出来是 None 时**不动**已有值
        #   （把"线索没写投递方式"理解成"把渠道清空"会丢掉真实信息）
        if desired_channel and channel_before != desired_channel:
            application.channel = desired_channel
            channel_changed = True
    session.flush()

    result = apply_svc.set_status(session, job_id, status, source=source)
    message = _link_message(
        lead=lead,
        application_id=application.id,
        created=created,
        channel=application.channel,
        channel_before=channel_before,
        channel_changed=channel_changed,
        status=result.to_status,
        status_before=result.from_status,
        status_changed=result.changed,
        owned_by_other_lead=owned_by_other_lead,
    )
    return LeadLinkResult(
        lead_id=lead.id or 0,
        lead_name=lead.name,
        application_id=application.id,
        job_id=job_id,
        created=created,
        channel=application.channel,
        channel_before=channel_before,
        channel_changed=channel_changed,
        status=result.to_status,
        status_before=result.from_status,
        status_changed=result.changed,
        owned_by_other_lead=owned_by_other_lead,
        message=message,
    )


def _link_message(
    *,
    lead: Company,
    application_id: int,
    created: bool,
    channel: str | None,
    channel_before: str | None,
    channel_changed: bool,
    status: str,
    status_before: str,
    status_changed: bool,
    owned_by_other_lead: bool = False,
) -> str:
    """拼一句**如实**的结果说明（新建 / 更新了什么 / 什么都没变）。"""
    if created:
        return (
            f"已记入投递看板：新建记录 #{application_id}"
            f"（线索「{lead.name}」，渠道 {channel or '未指定'}，状态 {status}）"
        )
    changes: list[str] = []
    if channel_changed:
        changes.append(f"渠道 {channel_before or '未指定'} → {channel or '未指定'}")
    if status_changed:
        changes.append(f"状态 {status_before} → {status}")
    if owned_by_other_lead:
        # ★ 归属没改 → **必须说出来**（JH-02）。含糊过去的话，用户会以为这条线索
        #   已经和这次投递挂上了，而页面（按 company_id 反查）会继续显示"未投"。
        tail = ("；".join(changes) + "；") if changes else ""
        return (
            f"记录 #{application_id} 已经属于**另一条线索**，这次的归属没有改动（{tail}"
            f"这条线索仍显示未投）。\n"
            "  为什么：一个岗位只有一条投递记录（投递是针对岗位的一次动作，不是每条线索一份）。\n"
            "  如果你要的就是「这次投递是从这条线索来的」，请先确认那条线索是不是选错了 —— "
            "归属只能由人来改，工具不替你转移历史。"
        )
    if not changes:
        return (
            f"没有变化：记录 #{application_id} 已经是"
            f"（渠道 {channel or '未指定'}，状态 {status}）—— 重复点不会多记一次"
        )
    return f"更新了已有记录 #{application_id}：" + "；".join(changes)


def iso_utc(value: datetime | None) -> str | None:
    """给 API 层用的时间串：**UTC + 偏移量**（`2026-09-19T10:47:00+00:00`）。

    ★ 为什么接口给 UTC 而不是北京时间：契约里的时间一律 ISO 8601 带时区，
      页面用 `new Date(...)` 自己按用户机器转本地 —— 服务端做本地化等于替用户
      假设了他在哪个时区（而那正是"时间差 8 小时"这类 bug 的来源）。
    """
    if value is None:
        return None
    return to_utc(value).isoformat()


def exported_now() -> datetime:
    """`generated_at` 用（aware UTC，便于前端直接 `new Date()`）。"""
    return now_utc()

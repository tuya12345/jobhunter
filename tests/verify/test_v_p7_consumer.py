"""【P7 消费端复验】用验证会话自己的样例跑真实导入 —— 不采信交付方的自测。

★ 样本用的是 `docs/leads-snapshot-sample.json`（我从采集端真实库导出、双方共同的契约参照物），
  **不是**开发会话自己造的测试数据。这样"契约有没有落地"才验得出来。

★ 每条断言都写明"用户会看到什么"，而不是"函数返回了什么"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from jobhunter.config.paths import find_project_root
from jobhunter.models.company import Company
from jobhunter.services import leads_svc

SAMPLE = find_project_root() / "docs" / "leads-snapshot-sample.json"


# ===========================================================================
# ① 契约：能不能吃下"不是自己造的"那份样例
# ===========================================================================
def test_the_contract_sample_parses() -> None:
    """契约的参照物必须能被消费端直接吃下 —— 这是两个仓库之间唯一的接口。"""
    snapshot = leads_svc.parse_snapshot(SAMPLE)
    assert snapshot.schema_ == "jobhunter.leads"
    assert snapshot.schema_version == 1
    assert len(snapshot.leads) == 4, f"样例里有 4 条线索，解析出 {len(snapshot.leads)} 条"

    names = [lead.name for lead in snapshot.leads]
    assert names == ["星澜科技", "云枢智能", "明澈数据", "虚构网络"], names


def test_the_sample_is_not_modified_by_us() -> None:
    """★ 样例是**契约的参照物**，双方都不该改它（改了就当没验）。

    这条防的是"为了让实现通过而把样例改小/改简单" —— 那是自己给自己出题。
    """
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
    assert payload["schema"] == "jobhunter.leads"
    assert payload["schema_version"] == 1
    assert len(payload["leads"]) == 4, (
        "样例被改过了 —— 它是契约参照物，4 条线索是刻意留的（含两种『未检查』的成因）"
    )
    first = payload["leads"][0]
    assert first["http_status"] == 404 and first["checked_at"], "第 1 条的『检查过』用例没了"
    assert payload["leads"][1]["official_url"] is None, "第 2 条的『没有网址』用例没了"
    assert payload["leads"][3]["official_url"] and payload["leads"][3]["http_status"] is None, (
        "第 4 条的『有网址但没检查成功』用例没了 —— 它与第 2/3 条的成因不同，必须保留"
    )


# ===========================================================================
# ② 导入：幂等 / 零写盘 / 如实报数
# ===========================================================================
def _counts(session) -> tuple[int, int]:
    companies = session.scalar(select(func.count()).select_from(Company))
    return companies or 0, len(session.new) + len(session.dirty)


def test_import_is_idempotent(session, settings) -> None:  # noqa: ARG001
    """★★ 同一份文件导两次必须是「新增 0 条、更新 N 条」，库里的行数不变。

    ★ 判据是**库里行数**，不是输出的文案 —— 文案可以写得好看，行数不会骗人。
    """
    first = leads_svc.import_snapshot(session, SAMPLE)
    session.commit()
    rows_after_first = session.scalar(select(func.count()).select_from(Company))
    assert rows_after_first == 4, f"第一次导入应有 4 行，实际 {rows_after_first}"
    assert first.added == 4, f"第一次应当是新增 4 条，实际 {first.added}"

    second = leads_svc.import_snapshot(session, SAMPLE)
    session.commit()
    rows_after_second = session.scalar(select(func.count()).select_from(Company))

    assert rows_after_second == 4, (
        f"导入两次之后库里有 {rows_after_second} 行 —— 幂等没做到（同一份快照被当成了两批）"
    )
    assert second.added == 0, f"第二次导入报告新增了 {second.added} 条 —— 应当全部识别为『已存在』"


def test_dry_run_writes_nothing(session, settings) -> None:  # noqa: ARG001
    """`--dry-run` 一个字都不写库 —— **去数行数**，不看输出文案。"""
    report = leads_svc.import_snapshot(session, SAMPLE, dry_run=True)
    session.flush()

    rows = session.scalar(select(func.count()).select_from(Company))
    assert rows == 0, f"演练模式往库里写了 {rows} 行"
    assert report.added == 4, "演练模式应当**照样报数**（告诉用户『会新增 4 条』），只是不落库"


def test_import_creates_every_field_from_the_snapshot(session, settings) -> None:  # noqa: ARG001
    """契约里的每个字段都要真的进库 —— 少一个字段就是"采集端的努力白费了"。"""
    leads_svc.import_snapshot(session, SAMPLE)
    session.commit()

    row = session.scalar(select(Company).where(Company.name == "星澜科技"))
    assert row is not None
    assert row.official_url == "https://example.com/xinglan"
    assert row.source_platform == "小红书"
    assert row.source_type == "官网"
    assert row.evidence_text and "刷小红书" in row.evidence_text, "原始那句话没存下来"
    assert row.http_status == 404
    assert row.checked_at is not None, "checked_at 没进库"


def test_checked_at_is_stored_as_utc(session, settings) -> None:  # noqa: ARG001
    """★★ 时区：契约里是 `+00:00`，进库必须仍是那一刻的 UTC，**不许偏 8 小时**。

    ★ 判据是"与契约里的时刻**同一瞬间**"，而不是"字符串长得像" ——
      只要时刻对，存 naive 还是 aware 是实现自由（本项目惯例是 naive UTC）。
    """
    from datetime import UTC, datetime

    leads_svc.import_snapshot(session, SAMPLE)
    session.commit()

    row = session.scalar(select(Company).where(Company.name == "星澜科技"))
    stored = row.checked_at
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=UTC)

    expected = datetime.fromisoformat("2026-09-19T10:47:36+00:00")
    delta = abs((stored - expected).total_seconds())
    assert delta < 1, (
        f"checked_at 与契约里的时刻差了 {delta} 秒（偏 8 小时就是 28800）—— "
        f"期望 {expected.isoformat()}，库里是 {row.checked_at!r}"
    )


# ===========================================================================
# ③ 可达性三态：「未检查」不能显示成「失效」
# ===========================================================================
def test_unchecked_is_not_reported_as_broken(session, settings) -> None:  # noqa: ARG001
    """★★ 规格里点名的关键一处，而且**样例里两种"未检查"的成因都留了**：

    · `#2 云枢智能` / `#3 明澈数据`：**没有网址** → 没检查
    · `#4 虚构网络`：**有网址**但当时网络不通 → 没检查

    两者都**不能**显示成"失效"。三态是：
    `checked_ok` / `checked_error`（唯一"可能失效"的一态）/ `unchecked`。
    """
    leads_svc.import_snapshot(session, SAMPLE)
    session.commit()

    def state(name: str) -> str:
        row = session.scalar(select(Company).where(Company.name == name))
        return leads_svc.reachability_of(row).state

    assert state("星澜科技") == "checked_error", "有状态码 404 的应当落进『可能失效』那一态"
    assert state("云枢智能") == "unchecked", "没有网址 → 未检查"
    assert state("明澈数据") == "unchecked", "没有网址 → 未检查"
    assert state("虚构网络") == "unchecked", (
        "**有网址但当时没检查成功**也是『未检查』—— 不能因为『有网址』就判成失效"
    )


def test_reachability_explains_why_it_is_unchecked(session, settings) -> None:  # noqa: ARG001
    """「未检查」的**两种成因**要说得出区别 —— 否则用户不知道要不要自己去点一下链接。"""
    leads_svc.import_snapshot(session, SAMPLE)
    session.commit()

    no_url = leads_svc.reachability_of(
        session.scalar(select(Company).where(Company.name == "云枢智能"))
    )
    net_fail = leads_svc.reachability_of(
        session.scalar(select(Company).where(Company.name == "虚构网络"))
    )

    assert no_url.reason != net_fail.reason, (
        f"两种『未检查』给出的说明一模一样（都是 {no_url.reason!r}）—— "
        "用户在第一种情况下『补个网址就行』，第二种『过会儿重试可能就好了』，这两句得不一样"
    )


# ===========================================================================
# ④ 坏输入：必须是中文可读错误，不能裸 JSONDecodeError
# ===========================================================================
@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("not_json.json", "这不是 JSON"),
        ("empty.json", ""),
        ("array_root.json", '[{"name": "甲"}]'),
        ("bad_version.json", '{"schema": "jobhunter.leads", "schema_version": 99, "leads": []}'),
        ("leads_not_list.json", '{"schema": "jobhunter.leads", "schema_version": 1, "leads": "x"}'),
        ("wrong_type.json", '{"schema": "jobhunter.leads", "schema_version": 1, "leads": 3}'),
        (
            "bad_field_type.json",
            json.dumps(
                {
                    "schema": "jobhunter.leads",
                    "schema_version": 1,
                    "leads": [{"name": "甲", "http_status": "不是数字"}],
                },
                ensure_ascii=False,
            ),
        ),
    ],
)
def test_bad_snapshot_gives_a_readable_error(tmp_path: Path, name: str, content: str) -> None:
    """依据：规格第三节「坏输入要给中文可操作提示，不要裸 `JSONDecodeError` / `KeyError`」。

    ★ 这条的价值在用户视角：他手上只有一份文件，"导不进去"时最需要知道的是
      **是文件坏了、还是版本不对、还是某个字段写错了** —— 三种的处理方式完全不同。
    """
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")

    with pytest.raises(leads_svc.LeadSnapshotError) as excinfo:
        leads_svc.parse_snapshot(path)

    message = str(excinfo.value)
    assert message.strip(), "报了个空错误"
    assert "Traceback" not in message
    # 必须是"看得懂的中文"，而不是 Python 异常名
    assert any("\u4e00" <= ch <= "\u9fff" for ch in message), (
        f"错误信息里没有一个汉字（用户看不懂）：{message[:200]!r}"
    )
    assert "JSONDecodeError" not in message and "KeyError" not in message, (
        f"把 Python 内部的异常名抛给用户了：{message[:200]!r}"
    )


def test_missing_file_gives_a_readable_error(tmp_path: Path) -> None:
    with pytest.raises(leads_svc.LeadSnapshotError) as excinfo:
        leads_svc.parse_snapshot(tmp_path / "根本没有这个文件.json")
    assert any("\u4e00" <= ch <= "\u9fff" for ch in str(excinfo.value))


def test_empty_leads_array_is_legal(tmp_path: Path) -> None:
    """空数组是**合法**的（表示"这次没采到"），不是错误。"""
    path = tmp_path / "empty_leads.json"
    path.write_text(
        json.dumps(
            {
                "schema": "jobhunter.leads",
                "schema_version": 1,
                "exported_at": "2026-09-20T00:00:00+00:00",
                "generator": {"tool": "leadhunter", "version": "0.1.0"},
                "leads": [],
            }
        ),
        encoding="utf-8",
    )
    snapshot = leads_svc.parse_snapshot(path)
    assert snapshot.leads == []


# ===========================================================================
# ⑤ 迁移：从空库能建到最新
# ===========================================================================
def test_companies_table_has_the_import_columns(session) -> None:
    """`companies` 必须真的有契约要求的列（走 alembic 建的库，不是 create_all）。"""
    columns = {
        row[1]
        for row in session.execute(
            __import__("sqlalchemy").text("PRAGMA table_info(companies)")
        ).all()
    }
    for required in ("http_status", "checked_at"):
        assert required in columns, f"companies 表缺列 {required}，实际有：{sorted(columns)}"

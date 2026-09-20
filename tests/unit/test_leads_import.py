"""`jh leads import`：幂等 / dry-run / 坏输入 / 三态可达性（P7 消费端，ISSUE #59）。

★★ 这组用例守的是"导入这件事**不能静默出错**"

    规格（`docs/P7-消费端规格.md` §3.2、§6）把这几条列为验收项，而且验收方式是
    **去数库里的行数**，不看输出文案。所以下面每一条都直接查库/查返回值：

      · 同一份快照导两次 → 第二次「新增 0 条、更新 N 条」，**行数不变**；
      · `--dry-run` → **一行都不写**（连 UPDATE 都没发）；
      · 5 种坏输入 → 中文可操作提示，不是裸 `JSONDecodeError` / pydantic 英文堆栈；
      · 「未检查」≠「失效」 → `http_status` 为空时**必须是第三态**，
        而且"没有网址"与"有网址但没检查"两种原因要分得清。

★ 为什么测试自己造快照（而不是读采集端那份 `docs/leads-snapshot-sample.json`）：
  那份样例是**验证会话的文件**（当前未入库），测试依赖它就会在 CI 里炸。
  所以我用同样形状的自造数据（公司名全部虚构），并在一条用例里覆盖样例点名的
  四种线索形态（404 / 没网址 / 有网址但没检查）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from jobhunter.config.paths import find_project_root
from jobhunter.models import Company
from jobhunter.services import leads_svc


def _snapshot(leads: Any, **overrides: Any) -> dict[str, Any]:
    """造一份快照。

    ★ `leads` 允许传**任意类型**（不只是 list）：坏输入用例要能造出
      `leads: {"name": …}` 这种形状 —— 那正是要拒绝的东西之一。
    """
    payload: dict[str, Any] = {
        "schema": "jobhunter.leads",
        "schema_version": 1,
        "exported_at": "2026-09-19T18:47:00+08:00",
        "generator": {"tool": "leadhunter", "version": "0.1.0"},
        "leads": leads,
    }
    payload.update(overrides)
    return payload


def _lead(name: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "external_id": 1,
        "name": name,
        "official_url": "https://example.com/a",
        "source_platform": "小红书",
        "source_type": "官网",
        "evidence_text": f"刷小红书看到「{name}」在招人",
        "evidence_url": None,
        "note": None,
        "http_status": 404,
        "checked_at": "2026-09-19T10:47:36+00:00",
        "collected_at": "2026-09-19T10:47:17+00:00",
    }
    base.update(overrides)
    return base


def _write(tmp_path: Path, payload: dict[str, Any], name: str = "snapshot.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _count(session) -> int:
    return int(session.scalar(select(func.count()).select_from(Company)) or 0)


# ===========================================================================
# 幂等 —— 规格里排第一的硬要求
# ===========================================================================
def test_import_is_idempotent_by_normalized_company_name(session, tmp_path: Path) -> None:
    """同一份文件导两次：第二次「新增 0 条、更新 N 条」，**行数不变**。"""
    path = _write(
        tmp_path,
        _snapshot([_lead("星澜科技"), _lead("云枢智能", source_type="内推", http_status=None)]),
    )

    first = leads_svc.import_snapshot(session, path)
    session.commit()
    assert (first.added, first.updated) == (2, 0)
    assert _count(session) == 2

    second = leads_svc.import_snapshot(session, path)
    session.commit()
    assert (second.added, second.updated) == (0, 2), (
        "同一份文件第二次导入必须报「新增 0 条」—— 这正是验收会数的那个数字"
    )
    assert second.changed == 0, "内容一个字都没变，不该报'有变化'"
    assert _count(session) == 2, "导入两次变成了两份（幂等失效）"


def test_same_company_written_differently_merges(session, tmp_path: Path) -> None:
    """公司名只差**空白** → 合并成一行（与采集端的合并口径一致，见 `company_key`）。"""
    first = _write(tmp_path, _snapshot([_lead("云枢智能")]), "a.json")
    second = _write(tmp_path, _snapshot([_lead("云枢 智能", note="多个空格")]), "b.json")

    leads_svc.import_snapshot(session, first)
    session.commit()
    report = leads_svc.import_snapshot(session, second)
    session.commit()

    assert report.added == 0 and report.updated == 1
    assert _count(session) == 1, "同一家公司（只差空白）被写成了两行"


def test_company_with_suffix_is_not_merged_on_purpose(session, tmp_path: Path) -> None:
    """★ 「星澜科技」与「星澜科技有限公司」**故意不合并** —— 与采集端口径一致。

    ★ 为什么把这条"不合并"也写成用例：它是**有意的取舍**，不是漏掉的边界。
      消费端若自己加码（比如用 `normalize_for_dedup` 把"有限公司"折掉），
      就会把生产者刻意分开的两家公司并成一条 —— 那属于"错合并"，会丢线索。
      真要让两边都折叠后缀，得先改**采集端**的口径（生产者先改，消费端跟随）。
    """
    first = _write(tmp_path, _snapshot([_lead("星澜科技")]), "a.json")
    second = _write(tmp_path, _snapshot([_lead("星澜科技有限公司")]), "b.json")

    leads_svc.import_snapshot(session, first)
    session.commit()
    report = leads_svc.import_snapshot(session, second)
    session.commit()

    assert report.added == 1, "带后缀的名字应当是新的一条（与采集端一致）"
    assert _count(session) == 2


def test_duplicates_inside_one_file_are_merged_and_reported(session, tmp_path: Path) -> None:
    """同一份文件里出现两次同公司：以**后面那条**为准，并且如实报出来。"""
    path = _write(
        tmp_path,
        _snapshot([_lead("星澜科技", note="旧"), _lead("星澜科技", note="新")]),
    )

    report = leads_svc.import_snapshot(session, path)
    session.commit()

    assert report.added == 1
    assert report.duplicates_in_file == 1, "同文件重复必须报出来，不能悄悄合并"
    row = session.scalars(select(Company)).one()
    assert row.note == "新"


def test_update_actually_refreshes_fields(session, tmp_path: Path) -> None:
    """第二次导入带来新信息时，字段要真的被更新（不只是报个数字）。"""
    first = _write(tmp_path, _snapshot([_lead("星澜科技", note=None)]), "a.json")
    second = _write(
        tmp_path,
        _snapshot([_lead("星澜科技", note="AI Agent 方向", http_status=200)]),
        "b.json",
    )

    leads_svc.import_snapshot(session, first)
    session.commit()
    report = leads_svc.import_snapshot(session, second)
    session.commit()

    row = session.scalars(select(Company)).one()
    assert row.note == "AI Agent 方向"
    assert row.http_status == 200
    assert row.verified is True, "http_status 有值 = 检查过（verified 由它推导）"
    assert "note" in report.rows[0].changed_fields
    assert report.changed == 1


def test_absent_field_keeps_local_value_but_explicit_null_clears_it(
    session, tmp_path: Path
) -> None:
    """★ 边界：快照里**没出现**的字段不动；出现了但是 null 就按 null 写。

    理由（写在 `leads_svc._apply_snapshot` 的 docstring 里）：契约规定导出会带上
    全部字段，所以 null = "采集端确实没有这个值"；而"字段整个缺失"说明对方换了实现，
    这时抹掉本地信息是错的。
    """
    full = _write(tmp_path, _snapshot([_lead("星澜科技", note="有备注")]), "a.json")
    leads_svc.import_snapshot(session, full)
    session.commit()

    without_note = _lead("星澜科技")
    without_note.pop("note")
    partial = _write(tmp_path, _snapshot([without_note]), "b.json")
    leads_svc.import_snapshot(session, partial)
    session.commit()
    assert session.scalars(select(Company)).one().note == "有备注", "字段缺失不该抹掉本地值"

    explicit_null = _write(tmp_path, _snapshot([_lead("星澜科技", note=None)]), "c.json")
    leads_svc.import_snapshot(session, explicit_null)
    session.commit()
    assert session.scalars(select(Company)).one().note is None, "显式 null 应当清掉"


# ===========================================================================
# dry-run —— 验收方式是"去数库里行数"
# ===========================================================================
def test_dry_run_writes_nothing(session, tmp_path: Path) -> None:
    path = _write(tmp_path, _snapshot([_lead("星澜科技"), _lead("云枢智能")]))

    report = leads_svc.import_snapshot(session, path, dry_run=True)
    session.commit()  # 就算调用方手滑 commit，也不该有东西落库

    assert report.dry_run is True
    assert report.added == 2, "dry-run 仍要如实报数（否则用户不知道会发生什么）"
    assert _count(session) == 0, "dry-run 写了库"


def test_dry_run_does_not_touch_existing_rows(session, tmp_path: Path) -> None:
    """已存在的行也不许被 dry-run 改（不只是"不新增"）。"""
    path = _write(tmp_path, _snapshot([_lead("星澜科技", note="旧")]))
    leads_svc.import_snapshot(session, path)
    session.commit()

    changed = _write(tmp_path, _snapshot([_lead("星澜科技", note="新")]), "b.json")
    report = leads_svc.import_snapshot(session, changed, dry_run=True)
    session.commit()

    assert report.rows[0].changed_fields == ("note",), "dry-run 要能预告会改哪个字段"
    assert session.scalars(select(Company)).one().note == "旧"


def test_cli_dry_run_writes_nothing(session, tmp_path: Path, db_url: str, monkeypatch) -> None:
    """走 CLI 那条路径再验一遍（用户真正敲的是命令，不是服务函数）。"""
    from typer.testing import CliRunner

    from jobhunter.cli.app import app

    path = _write(tmp_path, _snapshot([_lead("星澜科技")]))
    result = CliRunner().invoke(app, ["leads", "import", str(path), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    assert _count(session) == 0


# ===========================================================================
# 坏输入 —— 5 种，全部要中文可操作提示
# ===========================================================================
def test_missing_file_gives_actionable_chinese(session, tmp_path: Path) -> None:
    with pytest.raises(leads_svc.LeadSnapshotError) as excinfo:
        leads_svc.import_snapshot(session, tmp_path / "nope.json")
    message = str(excinfo.value)
    assert "找不到快照文件" in message
    assert "lh leads export" in message


def test_not_json_gives_actionable_chinese(session, tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("<html>登录页</html>", encoding="utf-8")
    with pytest.raises(leads_svc.LeadSnapshotError) as excinfo:
        leads_svc.import_snapshot(session, path)
    message = str(excinfo.value)
    assert "不是合法 JSON" in message
    assert "第 1 行" in message, "要指出错在哪一行，否则用户只能猜"


def test_not_utf8_gives_actionable_chinese(session, tmp_path: Path) -> None:
    path = tmp_path / "gbk.json"
    path.write_bytes('{"schema": "jobhunter.leads", "note": "中文"}'.encode("gbk"))
    with pytest.raises(leads_svc.LeadSnapshotError) as excinfo:
        leads_svc.import_snapshot(session, path)
    assert "UTF-8" in str(excinfo.value)


def test_empty_file_gives_actionable_chinese(session, tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    path.write_text("   \n", encoding="utf-8")
    with pytest.raises(leads_svc.LeadSnapshotError) as excinfo:
        leads_svc.import_snapshot(session, path)
    assert "空文件" in str(excinfo.value)


def test_unknown_schema_version_is_refused_not_guessed(session, tmp_path: Path) -> None:
    path = _write(tmp_path, _snapshot([_lead("星澜科技")], schema_version=99))
    with pytest.raises(leads_svc.LeadSnapshotError) as excinfo:
        leads_svc.import_snapshot(session, path)
    message = str(excinfo.value)
    assert "schema_version=99" in message
    assert "不认识" in message


def test_wrong_schema_name_is_refused(session, tmp_path: Path) -> None:
    path = _write(tmp_path, _snapshot([], schema="something.else"))
    with pytest.raises(leads_svc.LeadSnapshotError) as excinfo:
        leads_svc.import_snapshot(session, path)
    assert "jobhunter.leads" in str(excinfo.value)


def test_leads_must_be_a_list(session, tmp_path: Path) -> None:
    payload = _snapshot([])
    payload["leads"] = {"name": "星澜科技"}  # 故意造坏：`leads` 是对象而不是数组
    path = _write(tmp_path, payload)
    with pytest.raises(leads_svc.LeadSnapshotError) as excinfo:
        leads_svc.import_snapshot(session, path)
    assert "`leads` 必须是数组" in str(excinfo.value)


def test_wrong_field_type_points_at_the_offending_lead(session, tmp_path: Path) -> None:
    path = _write(tmp_path, _snapshot([_lead("星澜科技", http_status="不是数字")]))
    with pytest.raises(leads_svc.LeadSnapshotError) as excinfo:
        leads_svc.import_snapshot(session, path)
    message = str(excinfo.value)
    assert "第 1 条线索「星澜科技」" in message, "报错必须指出是哪一条，而不是一个下标"
    assert "http_status" in message


def test_unknown_source_type_is_refused(session, tmp_path: Path) -> None:
    path = _write(tmp_path, _snapshot([_lead("星澜科技", source_type="猎头")]))
    with pytest.raises(leads_svc.LeadSnapshotError) as excinfo:
        leads_svc.import_snapshot(session, path)
    assert "source_type" in str(excinfo.value)


def test_naive_datetime_is_refused_instead_of_guessed(session, tmp_path: Path) -> None:
    """★ 契约要求"ISO 8601 带时区"；猜时区会猜错 8 小时而且没人发现。"""
    path = _write(tmp_path, _snapshot([_lead("星澜科技", checked_at="2026-09-19T10:47:36")]))
    with pytest.raises(leads_svc.LeadSnapshotError) as excinfo:
        leads_svc.import_snapshot(session, path)
    assert "时区" in str(excinfo.value)


def test_extra_unknown_field_is_refused(session, tmp_path: Path) -> None:
    """采集端加了字段而我们不认识 → 报错让两边对齐契约，**不静默忽略**。"""
    path = _write(tmp_path, _snapshot([_lead("星澜科技", salary="20k")]))
    with pytest.raises(leads_svc.LeadSnapshotError) as excinfo:
        leads_svc.import_snapshot(session, path)
    assert "salary" in str(excinfo.value)


def test_cli_import_failure_exits_nonzero_with_chinese(session, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from jobhunter.cli.app import app

    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    result = CliRunner().invoke(app, ["leads", "import", str(bad)])

    assert result.exit_code == 1, result.output
    assert "导入失败" in result.output
    assert "JSONDecodeError" not in result.output, "不许把 Python 异常名丢给用户"


# ===========================================================================
# 三态可达性 —— "未检查" ≠ "失效"（规格点名的验收项）
# ===========================================================================
def test_reachability_has_three_states(session, tmp_path: Path) -> None:
    """三种形态各造一条：404 / 没网址 / 有网址但没检查。

    ★ 这正是采集端样例里故意留的三个测试点（#1 有 404、#2/#3 没网址、
      #4 有网址但没检查），这里用自造数据覆盖同一条口径。
    """
    path = _write(
        tmp_path,
        _snapshot(
            [
                _lead("甲", official_url="https://example.com/a", http_status=404),
                _lead("乙", official_url=None, http_status=None, source_type="内推"),
                _lead("丙", official_url="https://example.invalid/c", http_status=None),
            ]
        ),
    )
    leads_svc.import_snapshot(session, path)
    session.commit()

    rows = {row.name: row for row in session.scalars(select(Company)).all()}
    not_checked_no_url = leads_svc.reachability_of(rows["乙"])
    not_checked_with_url = leads_svc.reachability_of(rows["丙"])

    assert leads_svc.reachability_of(rows["甲"]).state == "checked_error"
    assert "404" in leads_svc.reachability_of(rows["甲"]).label

    assert not_checked_no_url.state == "unchecked"
    assert not_checked_no_url.label == "未检查"
    assert not_checked_with_url.state == "unchecked"
    assert "没有官网网址" in not_checked_no_url.reason
    assert "有网址" in not_checked_with_url.reason, (
        "两种'没检查'的原因必须分得清：一条是没网址、一条是没跑检查"
    )


def test_reachability_ok_state(session, tmp_path: Path) -> None:
    path = _write(tmp_path, _snapshot([_lead("甲", http_status=200)]))
    leads_svc.import_snapshot(session, path)
    session.commit()
    reach = leads_svc.reachability_of(session.scalars(select(Company)).one())
    assert reach.state == "checked_ok"
    assert "200" in reach.label


def test_import_stores_utc_without_localizing(session, tmp_path: Path) -> None:
    """时间一律 UTC 入库；**不做本地化转换**（+08:00 → UTC 只做一次换算）。"""
    path = _write(
        tmp_path,
        _snapshot([_lead("甲", checked_at="2026-09-19T18:47:36+08:00")]),
    )
    leads_svc.import_snapshot(session, path)
    session.commit()

    row = session.scalars(select(Company)).one()
    assert row.checked_at is not None
    # `UTCDateTime` 读出来是 **aware UTC**（存储层是 naive）；断言"这一刻"对不对：
    # 北京时间 18:47:36 = UTC 10:47:36 —— 只做了一次换算，没有多做本地化。
    assert row.checked_at.hour == 10
    assert (
        row.checked_at.utcoffset() is not None and row.checked_at.utcoffset().total_seconds() == 0
    )


def test_external_id_is_stored_for_traceability_not_as_a_key(session, tmp_path: Path) -> None:
    """`external_id` 只做溯源：换台机器 id 变了也不该产生第二行（匹配靠 name_norm）。"""
    first = _write(tmp_path, _snapshot([_lead("星澜科技", external_id=1)]), "a.json")
    second = _write(tmp_path, _snapshot([_lead("星澜科技", external_id=999)]), "b.json")

    leads_svc.import_snapshot(session, first)
    session.commit()
    leads_svc.import_snapshot(session, second)
    session.commit()

    assert _count(session) == 1, "external_id 被当成了唯一键（换机器就会重复导入）"
    assert session.scalars(select(Company)).one().external_id == "999", "溯源字段应当被刷新"


def test_empty_leads_array_is_legal(session, tmp_path: Path) -> None:
    """空数组 = "这次没采到"，是合法快照（不该报错、也不该写任何行）。"""
    path = _write(tmp_path, _snapshot([]))
    report = leads_svc.import_snapshot(session, path)
    session.commit()
    assert report.total_in_file == 0
    assert _count(session) == 0


def test_model_maintains_name_norm_automatically(session) -> None:
    """★ 结构保证：给 `name` 赋值就自动得到正确的 `name_norm`。

    ★ 为什么这条重要：`name_norm` 上的 UNIQUE 索引是导入幂等的**库层保障**，
      而保障的前提是"每一行都填了正确的 name_norm"。靠调用方自觉的话，
      某天有人写 `Company(name="某某科技")` 就会撞 NOT NULL；
      更糟的是有人自己拼一个近似的值 —— 幂等会**悄悄**失效（重复行进得来）。
      本项目因为"靠记得"吃过好几次亏（#37 / #24 / #55），所以把不变量交给模型维护。
    """
    row = Company(name="云枢 智能")
    session.add(row)
    session.flush()
    assert row.name_norm == "云枢智能", "name_norm 没有跟着 name 自动算出来"

    row.name = "云枢智能（深圳）"
    session.flush()
    assert row.name_norm == "云枢智能（深圳）", "改名后 name_norm 没跟上"


def test_unique_index_backs_the_idempotency(session, tmp_path: Path) -> None:
    """★ 幂等不只是"服务层记得查"：库层有唯一索引兜底（迁移 0005）。"""
    from sqlalchemy.exc import IntegrityError

    leads_svc.import_snapshot(session, _write(tmp_path, _snapshot([_lead("星澜科技")])))
    session.commit()

    # 绕过服务层，直接插一行同名（归一化后）的记录 —— 必须被数据库拒绝
    session.add(Company(name="星澜科技有限公司", name_norm="星澜科技", verified=False))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# ===========================================================================
# P7 架构变更留下的"死东西"不该复活（与 #18 同一种形态）
# ===========================================================================
def test_no_screencapture_dir_left_in_this_repo() -> None:
    """★ 抓屏产物目录跟着采集端走了 —— 本仓库不该再有它的**活引用**。

    ★ 为什么专门写一条：`data/leads/` 与 `PathSet.leads_dir` 在采集端搬走之后
      **没有任何使用者**了。留着一个"将来可能用"的空壳，会让读代码的人以为
      本仓库还有抓屏功能 —— 与 #18 删掉 `rate_limit.max_pages` 是同一个理由：
      **没人读的东西会变成假承诺**。

    ★ 判据是"有没有**活引用**"，不是"出现过这几个字"：`paths.py` 里留了一句
      "这里曾经有 data/leads，删掉是刻意的"（墓碑），那是好事 —— 所以这里用 AST
      跳过 docstring，只看**代码里**还有没有这个目录/属性。
    """
    import ast

    from jobhunter.config.paths import PathSet

    assert not hasattr(PathSet, "leads_dir"), "抓屏产物目录的壳又回来了（它没有使用者）"

    def _is_docstring(node: ast.Constant, parents: dict[ast.AST, ast.AST]) -> bool:
        parent = parents.get(node)
        if not isinstance(parent, ast.Expr):
            return False
        holder = parents.get(parent)
        if not isinstance(holder, ast.Module | ast.ClassDef | ast.FunctionDef):
            return False
        return bool(holder.body) and holder.body[0] is parent

    src = find_project_root() / "src"
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and not _is_docstring(node, parents)
                and "data/leads" in node.value
            ):
                offenders.append(f"{path.relative_to(find_project_root())}:{node.lineno}")
            if (isinstance(node, ast.Name) and node.id == "leads_dir") or (
                isinstance(node, ast.Attribute) and node.attr == "leads_dir"
            ):
                offenders.append(f"{path.relative_to(find_project_root())}:{node.lineno}")
    assert not offenders, f"这些地方还在用抓屏产物目录（它已经跟着采集端走了）：{offenders}"


def test_help_text_escapes_rich_markup_for_the_extra_name() -> None:
    """★ 提示语里的 `[web]` 必须对 rich 转义（否则印出来是 `pip install -e "."`）。

    ★ 这条是子代理实测发现的：rich 把方括号当**样式标签**吃掉，于是"装一下"
      那行给出的是一个**装不上任何东西**的命令。提示本身是错的，而它只在
      "依赖没装"这条出错路径上出现 —— 正是用户最需要它正确的时候。
    """
    source = (
        find_project_root() / "src" / "jobhunter" / "cli" / "commands" / "leads_cmd.py"
    ).read_text(encoding="utf-8")
    assert "escape(" in source, "应当用 rich.markup.escape 处理 [web]"
    assert 'pip install -e ".[web]"' in source, "被转义的字符串本身要写全"
    assert 'uv sync --extra web（或 pip install -e ".[web]"）' not in source, (
        "这行没走 escape，rich 会把 [web] 吃掉"
    )

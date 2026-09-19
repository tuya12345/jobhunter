"""`jh job add --file` 的三道闸门（ISSUE #13 / #14）。

    · ① scheme 白名单：只收 http / https
    · ② 读取阶段按 URL 归一化去重
    · ③ 条数上限（`URL_FILE_MAX_ITEMS`）

★ 为什么这三条值得单独的测试文件

    `--file` 是唯一"用户没法一眼看全"的入口（清单可能有几百行）。
    它静默少抓几条，用户往往第二天才发现清单里少了岗位，而那时已经不知道是哪一步丢的。
    所以这里既测"挡住了什么"，也测**"挡住之后有没有说出来"** ——
    一个不吭声的上限比没有上限更糟（用户会以为"我给的链接全抓了"）。

★ 与 `tests/verify/test_v_adversarial.py` 的分工

    验证会话那三条盯的是"函数返回值里不许出现重复 / 非 http(s) / 超过 200 条"。
    这里补的是它没覆盖的部分：**报出来了没有**、归一化的口径、上限的具体数值与顺序、
    以及"上限是覆盖在去重之后"这类容易写反的细节。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobhunter.cli.commands import job_cmd
from jobhunter.cli.commands.job_cmd import URL_FILE_MAX_ITEMS, _read_url_file, _url_reject_reason
from jobhunter.services import job_svc

GOOD = "https://www.zhipin.com/job_detail/abc123.html"


def write_listing(tmp_path: Path, *lines: str, name: str = "清单.txt") -> Path:
    target = tmp_path / name
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


# ===========================================================================
# ① scheme 白名单
# ===========================================================================
@pytest.mark.parametrize(
    ("raw", "keyword"),
    [
        ("file:///C:/Windows/win.ini", "http/https"),
        ("javascript:alert(1)", "http/https"),
        ("ftp://example.com/job/1", "http/https"),
        ("data:text/html,<h1>x</h1>", "http/https"),
        ("not-a-url-at-all", "http/https"),
        ("//example.com/job/1", "http/https"),  # 省略 scheme 的写法同样不接受
        ("https://", "没有域名"),
    ],
)
def test_non_http_entries_are_rejected_with_a_reason(raw: str, keyword: str) -> None:
    """非 http(s) 的输入必须被拒，而且理由要能说清是哪一类问题。"""
    reason = _url_reject_reason(raw)
    assert reason is not None, f"{raw} 居然通过了白名单"
    assert keyword in reason


@pytest.mark.parametrize(
    "raw",
    [
        "http://127.0.0.1:8080/admin",
        "http://localhost/admin",
        "http://192.168.1.1/router",
        "http://10.0.0.5/job",
        "http://172.16.3.9/job",
        "http://169.254.169.254/latest/meta-data",  # 云元数据地址：最该挡的那类
        "http://[::1]/admin",
        "http://nas.local/job",
    ],
)
def test_local_and_intranet_hosts_are_rejected(raw: str) -> None:
    """★ 内网 / 本机地址一律不抓。

    依据：设计文档 §七.2「不做绕过」。一个只抓公开招聘页的工具，
    没有任何理由去读本机文件、或替用户探测内网（`169.254.169.254` 更是云上最敏感的地址）。
    """
    reason = _url_reject_reason(raw)
    assert reason is not None, f"{raw} 居然进了抓取队列"
    assert "内网" in reason


def test_public_urls_and_case_insensitive_scheme_pass() -> None:
    """正常链接要放行（白名单不能严到把主路径也挡了）；scheme 大小写不敏感。"""
    assert _url_reject_reason(GOOD) is None
    assert _url_reject_reason("HTTP://Example.com/job/1") is None
    assert _url_reject_reason("https://example.com") is None


def test_file_skips_and_reports_unusable_lines(tmp_path: Path, capsys) -> None:
    """整份清单里混了坏行 → 只留能抓的，且把跳过条数与原因打出来。"""
    target = write_listing(
        tmp_path,
        "# 这是注释",
        "",
        GOOD,
        "file:///C:/Windows/win.ini",
        "http://127.0.0.1:8080/admin",
    )
    urls = _read_url_file(target)
    assert urls == [GOOD]

    output = capsys.readouterr().out
    assert "2 条被跳过" in output
    assert "第 4 行" in output and "第 5 行" in output, "没有指出是哪几行出的问题"


# ===========================================================================
# ② 读取阶段去重（复用 job_svc 的归一化）
# ===========================================================================
def test_duplicates_are_merged_by_normalized_url(tmp_path: Path, capsys) -> None:
    """同一岗位的链接只差大小写 / 末尾斜杠 / 空白 → 只抓一次，并报出条数。"""
    target = write_listing(
        tmp_path,
        "HTTPS://WWW.Zhipin.com/job_detail/abc123.html",
        "https://www.zhipin.com/job_detail/abc123.html/",
        "   https://www.zhipin.com/job_detail/abc123.html   ",
    )
    urls = _read_url_file(target)
    assert urls == ["HTTPS://WWW.Zhipin.com/job_detail/abc123.html"], (
        "去重后应当只留第一次出现的那一行"
    )

    output = capsys.readouterr().out
    assert "跳过 2 条重复链接" in output
    assert "与第 1 行是同一个链接" in output, "要说清是跟哪一行重复，否则用户没法去改清单"


def test_full_width_url_is_rejected_rather_than_rewritten(tmp_path: Path, capsys) -> None:
    """★ 全角链接**拒绝并说明**，而不是替用户改写成半角再去抓。

    `ｈｔｔｐｓ：／／…` 这种多半是复制过程中被"中文化"了。可以把它 NFKC 成半角，
    但那等于工具**悄悄改写了用户的输入**（抓回来的链接与用户给的不是同一个字符串）。
    这里的口径是：说不清就说不清 —— 报出"只接受 http/https（这里是 ｈｔｔｐｓ）"，
    让用户自己回去复制一次。
    """
    target = write_listing(tmp_path, "ｈｔｔｐｓ：／／www.zhipin.com/job_detail/abc123.html")
    assert _read_url_file(target) == []
    output = capsys.readouterr().out
    assert "被跳过" in output and "ｈｔｔｐｓ" in output, "要把它解析到的 scheme 原样告诉用户"


def test_different_jobs_are_not_merged(tmp_path: Path) -> None:
    """不同岗位（哪怕只差一个字符）绝不能被合并 —— 合并 = 用户再也看不到那条岗位。"""
    target = write_listing(
        tmp_path,
        "https://example.com/job/1",
        "https://example.com/job/2",
        "https://example.com/detail?jobId=1",
        "https://example.com/detail?jobId=2",
    )
    assert len(_read_url_file(target)) == 4


def test_bom_prefixed_listing_still_works(tmp_path: Path) -> None:
    """★ 带 UTF-8 BOM 的清单也要正常（Windows 上 PowerShell / 记事本默认写 BOM）。

    不处理的话：第一行变成 `\\ufeff# 注释`（注释判断失效）或 `\\ufeffhttps://…`
    （被当成"没有 scheme"的非法链接丢掉）—— 用户会莫名其妙少抓第一条。
    """
    target = tmp_path / "带BOM的清单.txt"
    target.write_text(
        "\ufeff# 我的清单\n" + GOOD + "\nhttps://example.com/job/2\n", encoding="utf-8"
    )
    assert _read_url_file(target) == [GOOD, "https://example.com/job/2"]


def test_query_parameters_are_kept_on_purpose(tmp_path: Path) -> None:
    """★ 刻意**不**抹掉查询参数（这是去重口径的取舍，不是漏写）。

    跟踪参数（`?utm_source=…`）确实会造成重复，但岗位 ID 也经常就写在查询里。
    删掉查询参数会把两个不同岗位合并成一个 —— 按项目一贯口径：
    **宁可漏合并，也不要错合并**（多抓一次页面 vs 用户永远看不到那条岗位）。
    """
    assert job_svc.url_dedup_key("https://x.com/detail?id=1") != job_svc.url_dedup_key(
        "https://x.com/detail?id=2"
    )


def test_repeated_import_reports_zero_duplicates_for_clean_listing(tmp_path: Path, capsys) -> None:
    """干净清单不该出现"跳过重复"的噪音（否则用户会对真警告麻木）。"""
    urls = _read_url_file(write_listing(tmp_path, GOOD, "https://example.com/job/9"))
    assert len(urls) == 2
    assert "重复" not in capsys.readouterr().out


# ===========================================================================
# ③ 条数上限
# ===========================================================================
def test_cap_keeps_the_first_n_and_never_truncates_silently(tmp_path: Path, capsys) -> None:
    """★ 上限必须"说清只处理前 N 条、其余没读"，绝不能静默截断。

    静默截断的后果：用户给了 50 条链接，工具抓了 20 条、报告"入库完成"，
    他会以为另外 30 条只是"没抓到好岗位"。
    """
    lines = [f"https://example.com/job/{index}" for index in range(URL_FILE_MAX_ITEMS + 7)]
    urls = _read_url_file(write_listing(tmp_path, *lines))

    assert len(urls) == URL_FILE_MAX_ITEMS
    assert urls == lines[:URL_FILE_MAX_ITEMS], "留下的必须是最前面那几条（顺序不能乱）"

    output = capsys.readouterr().out
    assert f"只处理前 {URL_FILE_MAX_ITEMS} 条" in output
    assert "7 条" in output and "没有抓取" in output
    assert "频率克制" in output, "要说明上限的来由，否则用户只会觉得工具坏了"


def test_cap_counts_unique_urls_only(tmp_path: Path) -> None:
    """★ 上限要覆盖在**去重之后**：重复行不该占掉名额。

    写反了会很难查：50 行清单里只要有 30 行是重复的，用户就只能抓到 20 条里的一部分，
    而错误提示还会说"超出上限"—— 但清单里其实只有 20 个真实岗位。
    """
    lines = [f"https://example.com/job/{index}" for index in range(URL_FILE_MAX_ITEMS)]
    lines += lines  # 整份复制一遍：去重后 20 条，一条都不该被上限砍掉
    urls = _read_url_file(write_listing(tmp_path, *lines))
    assert len(urls) == URL_FILE_MAX_ITEMS


def test_cap_is_a_module_constant_with_documented_tradeoff() -> None:
    """上限是常量（不是 config.yaml 项）—— 这条固定它的数值与量级。

    它是**防手滑的护栏**：抓取之间有 2~5 秒随机延时，几千行会连发几小时。
    让用户可配置等于把这个护栏变成"看起来能关掉的东西"（见常量处的注释）。
    量级取"一天真会投的数量"（与 config.yaml 的 `daily_limit: 20` 同档）。
    """
    assert URL_FILE_MAX_ITEMS == 20
    assert 5 <= URL_FILE_MAX_ITEMS <= 50, "上限要落在'一天真会投的量'这个量级"
    # 这个数字必须真的被读取阶段用上（不是写着好看的常量）
    assert job_cmd.URL_FILE_MAX_ITEMS == URL_FILE_MAX_ITEMS

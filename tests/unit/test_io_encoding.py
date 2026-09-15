"""IO 编码测试：守住 Windows 上最容易炸的那条路径。

★ 为什么这条测试价值很高

    Windows 上 `Path.read_text()` 不传 encoding 时用**系统区域编码**（中文系统 cp936）。
    后果：
        · 读用户导出的 UTF-8 文本 → UnicodeDecodeError；
        · 写含中文/emoji 的导出文件 → 别的机器上打开是乱码，
          某些情况下甚至**静默丢字**。
    这个坑最恶劣的地方是"我这儿能跑"：只要你的路径和内容都不含非 ASCII 就永远不触发，
    直到用户在中文 Windows 上跑。

    三层防护里，这一层（测试）负责防止有人把 `encoding="utf-8"` 从 utils/io.py 里删掉；
    另外两层是 ruff 的 PLW1514 规则和 CI 的 windows-latest。

同时验证两个容易被忽略的细节：
    · 换行符处理（避免 Windows 把 \\n 写成 \\r\\n 造成 Git 里"整文件都改了"的假 diff）
    · 原子写（写到一半崩掉不会留半截文件）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobhunter.utils.io import (
    ENCODING,
    append_text,
    ensure_dir,
    from_json,
    read_json,
    read_text,
    to_json,
    write_json,
    write_text,
)

# 覆盖各种"Windows 上会出事"的字符：
#   中文、全角标点、emoji（GBK 编码不了）、零宽字符、日文、特殊符号
TRICKY_TEXT = "简历：张三 · 求职方向「AI Agent」🚀 日本語テスト ✅ \u200b 完成"


# ---------------------------------------------------------------------------
# 文本读写
# ---------------------------------------------------------------------------
def test_roundtrip_tricky_text(tmp_path: Path) -> None:
    """中文 / emoji / 全角标点的往返读写必须完全一致。"""
    target = tmp_path / "导出" / "简历笔记.md"
    write_text(target, TRICKY_TEXT)
    assert read_text(target) == TRICKY_TEXT


def test_file_is_actually_utf8_bytes(tmp_path: Path) -> None:
    """★ 直接检查落盘字节：证明写的确实是 UTF-8，而不是"读回来碰巧一样"。

    两种编码都验证一遍（内容相同不代表编码相同）：
        ① 用 utf-8 解码 → 必须等于原文
        ② 用 **gbk 解码 → 必须失败**（如果内容是 GBK 编码的，这里就会成功）
    """
    target = tmp_path / "note.txt"
    write_text(target, "中文内容 🚀")
    raw = target.read_bytes()

    assert raw.decode("utf-8") == "中文内容 🚀"
    with pytest.raises(UnicodeDecodeError):
        raw.decode("gbk")


def test_no_crlf_pollution(tmp_path: Path) -> None:
    """写文件时 `\\n` 不能被偷偷换成 `\\r\\n`。

    ★ 为什么较真：如果换行符被平台改写，导出的 Markdown/CSV 在 Git 里会显示
      "整个文件都被改了"，完全没法 review 也看不出真实改动。
    """
    target = tmp_path / "multi.md"
    write_text(target, "第一行\n第二行\n第三行\n")
    # 以二进制读，绕开读路径的换行归一化
    assert target.read_bytes() == "第一行\n第二行\n第三行\n".encode()


def test_reading_back_normalizes_crlf(tmp_path: Path) -> None:
    """读回来时统一成 `\\n`（这样业务代码不用处理两种换行）。"""
    target = tmp_path / "win.txt"
    target.write_bytes(b"line1\r\nline2\r\n")
    assert read_text(target) == "line1\nline2\n"


def test_read_text_default_when_missing(tmp_path: Path) -> None:
    """给了 default 就不该抛错（读可选文件时的常见需求）。"""
    missing = tmp_path / "nope.txt"
    assert read_text(missing, default="") == ""
    assert read_text(missing, default="兜底") == "兜底"


def test_read_text_raises_without_default(tmp_path: Path) -> None:
    """没给 default 时必须抛错 —— 静默返回空字符串会掩盖真实问题。"""
    with pytest.raises(FileNotFoundError):
        read_text(tmp_path / "nope.txt")


def test_encoding_constant_is_utf8() -> None:
    """编码常量必须是 utf-8（改它就是改全项目的约定，必须显式可见）。"""
    assert ENCODING == "utf-8"


def test_append_text(tmp_path: Path) -> None:
    """追加写（P7 抓屏原始记录会用）。"""
    target = tmp_path / "log.txt"
    append_text(target, "第一条 🚀\n")
    append_text(target, "第二条 ✅\n")
    assert read_text(target) == "第一条 🚀\n第二条 ✅\n"


def test_ensure_dir_is_idempotent(tmp_path: Path) -> None:
    """重复建目录不报错。"""
    target = tmp_path / "a" / "b" / "c"
    ensure_dir(target)
    ensure_dir(target)
    assert target.is_dir()


# ---------------------------------------------------------------------------
# 原子写
# ---------------------------------------------------------------------------
def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    """原子写结束后不该留下临时文件。"""
    target = tmp_path / "out.md"
    write_text(target, "内容")
    leftovers = sorted(p.name for p in tmp_path.glob("*.tmp"))
    assert leftovers == [], f"残留临时文件：{leftovers}"


def test_atomic_write_does_not_corrupt_existing_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 写失败时，原文件必须保持完好（这正是"原子写"的意义）。

    模拟方式：让 os.replace 抛错（磁盘满 / 权限问题都会走到这一步），
    然后断言原文件内容没变。
    """
    target = tmp_path / "important.md"
    write_text(target, "原始内容，不能丢")

    import os as os_module

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr(os_module, "replace", boom)

    with pytest.raises(OSError):
        write_text(target, "新内容")

    assert read_text(target) == "原始内容，不能丢"
    leftovers = sorted(p.name for p in tmp_path.glob("*.tmp"))
    assert leftovers == [], f"临时文件没被清掉：{leftovers}"


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------
def test_json_roundtrip_keeps_chinese_readable(tmp_path: Path) -> None:
    """★ JSON 里中文必须保持可读（`ensure_ascii=False`）。

    为什么较真：存进数据库的 `*_json` 字段是给人排查问题用的。
    如果全是 `\\u5927\\u6a21\\u578b` 这种转义，等于没法用肉眼检查，
    只能写脚本解 —— 那还不如不存。
    """
    target = tmp_path / "data.json"
    payload = {"技能": ["Python", "大模型"], "备注": "面试通过 ✅"}
    write_json(target, payload)

    raw = target.read_bytes()
    assert "大模型".encode() in raw, "中文被转义了，落库/导出后不可读"
    assert "\\u5927" not in raw.decode()

    assert read_json(target) == payload


def test_to_json_signature_and_output() -> None:
    """`to_json` 的固定参数：中文可读、缩进 2、保持字段顺序。"""
    text = to_json({"b": 1, "a": "中文"})
    assert "中文" in text
    assert "\n  " in text  # 缩进
    assert text.index('"b"') < text.index('"a"'), "字段顺序应保持（不排序）"


def test_from_json_tolerates_dirty_input() -> None:
    """★ 读路径要宽容：三种"脏"输入都要能返回 default 而不是抛异常。

    这三种在本项目里都会真实出现：
        · NULL      —— 数据库里未填充的字段
        · 空白串    —— 手工改库、旧版本残留
        · 坏 JSON   —— 手工改库改坏了
    严格的校验放在 service 层（用 pydantic），读原始值这一步不该炸。
    """
    assert from_json(None, default={}) == {}
    assert from_json("", default={}) == {}
    assert from_json("   ", default={}) == {}
    assert from_json("{不是合法 JSON", default={}) == {}
    assert from_json(json.dumps({"a": 1})) == {"a": 1}


def test_read_json_missing_file_returns_default(tmp_path: Path) -> None:
    """文件不存在时返回 default（不做异常处理的话，很多命令首次运行就会崩）。"""
    assert read_json(tmp_path / "missing.json", default={"k": "v"}) == {"k": "v"}


def test_json_write_has_trailing_newline(tmp_path: Path) -> None:
    """文件末尾留一个换行（POSIX 惯例，也是 git diff 不会显示"\\ No newline"的原因）。"""
    target = tmp_path / "x.json"
    write_json(target, {"a": 1})
    assert target.read_bytes().endswith(b"\n")

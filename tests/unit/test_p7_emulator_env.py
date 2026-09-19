"""P7 模拟器环境抽样（`@pytest.mark.emulator`，**默认不跑**）。

    uv run --extra emulator pytest -m emulator -rs -v

★ 为什么这个文件只做"环境抽样"，而不是 P7 的功能测试

    P7（App 线索）**还没实现**：`jh leads probe/capture` 现在明确返回"未实现"
    （见 `src/jobhunter/cli/commands/leads_cmd.py`）。给一个不存在的功能编测试，
    测的只是"我脑补出来的接口"，那种用例唯一的产出是让人以为覆盖过了。

    所以这里只回答一个**真问题**：`docs/install.md` §9 写的那套环境
    （`uv sync --extra emulator` → `python -m uiautomator2 init` → 能看到设备）
    在**这台机器上**到底通不通。它是 nightly 的 `emulator` job 唯一的内容 ——
    那个 job 存在的意义就是把"环境到底有没有准备好"摆进日志，
    而不是等到 P7 动手那天才发现 ATX 装不上、adb 看不到设备。

★ 为什么默认排除、为什么每个测试都能 skip

    · 没有安卓设备（CI runner、普通开发机）→ **skip**，不是 fail。
      "这台机器没接模拟器"不是代码缺陷。
    · 显式设了 `ANDROID_SERIAL` 却连不上 → **fail**。
      那是"你说了有设备，但环境是坏的"，正是这个抽样要抓的东西。
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.emulator


def test_uiautomator2_extra_is_installed() -> None:
    """`[emulator]` extra 装上了没（`uv sync --extra emulator`）。

    ★ 为什么单独一条：装不上/装错版本是 P7 最常见的阻塞（上游版本频繁变动，
      项目为此把版本钉成 `==3.2.2`）。这条失败信息会直接告诉你去跑哪条命令。
    """
    module = pytest.importorskip(
        "uiautomator2",
        reason="没装 [emulator] extra：uv sync --extra emulator（版本被钉死，别用 pip 随手装）",
    )
    assert getattr(module, "__version__", ""), "uiautomator2 装上了但读不到版本号"


def test_declared_device_is_reachable() -> None:
    """声明了 `ANDROID_SERIAL` 就真的去连一次（连不上=这台机器的环境是坏的）。

    序列号从哪来：`adb devices` 里的那一列（模拟器通常是 `emulator-5554`）。
    与 `docs/install.md` §9 的 `jh leads probe --serial emulator-5554` 用同一个约定。
    """
    uiautomator2 = pytest.importorskip("uiautomator2", reason="没装 [emulator] extra")

    serial = os.environ.get("ANDROID_SERIAL", "").strip()
    if not serial:
        pytest.skip(
            "没有 ANDROID_SERIAL（这台机器没接模拟器/真机）→ 跳过设备抽样；"
            "要跑它：先 adb devices 拿到序列号，再导出 ANDROID_SERIAL=<序列号>"
        )

    device = uiautomator2.connect(serial)
    info = device.info or {}
    assert info.get("serial") in (serial, None) or serial in str(info), (
        f"连上了但不是我们要的那台设备：期望 {serial}，实际 {info.get('serial')!r}"
    )
    print(f"\n[emulator] {serial} 可连接（display={info.get('display')}）")

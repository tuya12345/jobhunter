"""日志脱敏测试：守住"隐私不外泄"这条红线。

★ 为什么脱敏必须由测试守，而不能靠"记得别打"

    这个工具会处理真实简历（姓名/手机号/邮箱）和真实 HR 对话。
    一旦日志里出现手机号：
        · 用户把日志贴到 issue 求助 → 个人信息直接公开；
        · 日志进 CI 产物 / 报错截图 / 聊天窗口 → 泄露面无限扩大。
    而"报错时最想打全文"是人的本能 —— 靠自觉一定守不住，所以靠 Filter + 测试。

这组测试覆盖两个层次：
    ① `redact_text()` 纯函数：各类敏感信息的打码规则
    ② 真实 logging 管线（Filter + Handler）：确保**任何**输出路径都被脱敏，
       包括 `logger.info("...%s", phone)` 这种把敏感信息放在 args 里的写法
       —— 这是最容易漏的一种。
"""

from __future__ import annotations

import logging

import pytest

from jobhunter.utils.logging import RedactingFilter, redact_text, setup_logging

PHONE = "13812345678"
EMAIL = "zhangsan@example.com"
ID_CARD = "110101199001011234"


# ---------------------------------------------------------------------------
# ① 纯函数
# ---------------------------------------------------------------------------
def test_phone_is_masked() -> None:
    """手机号保留前 3 后 4，中间打码。"""
    result = redact_text(f"我的手机号是{PHONE}，请查收")
    assert PHONE not in result
    assert "138****5678" in result


def test_email_is_masked() -> None:
    """邮箱保留首字母与域名。"""
    result = redact_text(f"联系邮箱：{EMAIL}")
    assert EMAIL not in result
    assert "@example.com" in result  # 域名保留：排查问题需要知道是哪个邮箱服务商
    assert "z***@" in result


def test_id_card_is_masked() -> None:
    """身份证保留前 6 后 4。"""
    result = redact_text(f"身份证 {ID_CARD}")
    assert ID_CARD not in result
    assert "110101********1234" in result


def test_multiple_secrets_in_one_line_are_all_masked() -> None:
    """同一行里多个敏感信息都要被打码（不能只处理第一个）。"""
    text = f"{PHONE} / {EMAIL} / {ID_CARD}"
    result = redact_text(text)
    assert PHONE not in result
    assert EMAIL not in result
    assert ID_CARD not in result


def test_normal_numbers_are_not_mangled() -> None:
    """正常文本不能被误伤 —— 误伤会让人完全看不懂日志。

    特意选了容易误判的样本：短工号、带短横的座机、版本号、薪资区间。
    注意：18 位**连写数字**会被当成身份证打码，这是刻意的取舍
    （宁可略微过度打码，也不能漏掉真实身份证号），所以这里不把它列为"不该动"。
    """
    samples = [
        "工号 12345",
        "座机 0755-12345678",
        "版本 1.2.3",
        "薪资 15000-25000",
        "python3.13",
    ]
    for sample in samples:
        assert redact_text(sample) == sample, f"被误伤：{sample!r}"


def test_redaction_is_idempotent() -> None:
    """重复脱敏不能越打越短（`138****5678` 再打一次不该变成 `138*****678`）。"""
    once = redact_text(f"电话 {PHONE}")
    twice = redact_text(once)
    assert once == twice


def test_empty_and_none_like_input() -> None:
    """空字符串不炸。"""
    assert redact_text("") == ""


# ---------------------------------------------------------------------------
# ② 真实 logging 管线
# ---------------------------------------------------------------------------
@pytest.fixture
def captured_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """装好脱敏 Filter 之后捕获日志。"""
    setup_logging("DEBUG")
    caplog.set_level(logging.DEBUG)
    return caplog


def test_logging_pipeline_redacts_message(captured_logs: pytest.LogCaptureFixture) -> None:
    """拼在消息里的手机号必须被打码。"""
    logger = logging.getLogger("jobhunter.test")
    logger.info("开始投递，联系人电话 %s", PHONE)
    text = captured_logs.text
    assert PHONE not in text
    assert "138****5678" in text


def test_logging_pipeline_redacts_fstring_message(captured_logs: pytest.LogCaptureFixture) -> None:
    """f-string 写法（敏感信息已经在 msg 里）同样要被打码。"""
    logger = logging.getLogger("jobhunter.test")
    logger.warning(f"抓取失败，回退到手动模式；联系方式 {EMAIL}")
    assert EMAIL not in captured_logs.text


def test_logging_pipeline_redacts_extra_args(captured_logs: pytest.LogCaptureFixture) -> None:
    """多个位置参数（含 dict 参数）都要处理。"""
    logger = logging.getLogger("jobhunter.test")

    # 元组参数
    logger.info("候选人 %s，电话 %s", "张三", PHONE)
    # 字典参数
    logger.info("邮箱=%(email)s", {"email": EMAIL})

    text = captured_logs.text
    assert PHONE not in text
    assert EMAIL not in text


def test_no_resume_body_is_logged(captured_logs: pytest.LogCaptureFixture) -> None:
    """★ 铁律：**任何日志都不打印简历原文**。

    这条测试的做法是"审查我们自己的日志调用约定"：
    真正的简历正文只允许用 `logger.debug("… %d 字", len(text))` 这种**摘要**形式输出，
    绝不允许把正文本身作为参数传进来。

    这里用一个"看起来像简历正文"的字符串，走一遍**摘要式**日志调用，
    断言正文没有出现在输出里 —— 固化"日志只打摘要"这个约定。
    """
    resume_body = "姓名：张三（虚构）｜手机：13812345678｜项目：某后台管理系统，QPS 提升 300%"
    logger = logging.getLogger("jobhunter.test")

    # ✅ 正确写法：只打摘要与长度
    logger.info("简历解析完成：%d 字，手机号已脱敏存储", len(resume_body))

    text = captured_logs.text
    assert "QPS 提升" not in text, "简历正文内容泄漏进日志了"
    assert "某后台管理系统" not in text, "简历正文内容泄漏进日志了"


def test_filter_is_installed_on_all_handlers() -> None:
    """★ 所有 handler 都必须挂上脱敏 Filter。

    只给 console 挂而漏了 file handler 是经典错误 ——
    而 file handler 恰恰是最容易被打包/上传的那份日志。
    """
    setup_logging("INFO")
    root = logging.getLogger()
    assert root.handlers, "root logger 应该有 handler"
    for handler in root.handlers:
        assert any(isinstance(f, RedactingFilter) for f in handler.filters), (
            f"handler {handler} 上没有脱敏 Filter"
        )


def test_setup_logging_is_idempotent() -> None:
    """重复 setup_logging 不能把 Filter 叠两层（会导致越打越短）。"""
    setup_logging("INFO")
    setup_logging("INFO")
    root = logging.getLogger()
    for handler in root.handlers:
        count = sum(1 for f in handler.filters if isinstance(f, RedactingFilter))
        assert count == 1, f"handler {handler} 上挂了 {count} 个脱敏 Filter"

    # 连续两次脱敏的结果应保持一致
    assert redact_text(f"电话 {PHONE}") == "电话 138****5678"

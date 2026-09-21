"""提示词的**分隔标签约定**与失效化（单点，P4 修复 #29 时建立）。

╔══════════════════════════════════════════════════════════════════════════╗
║ 为什么要有这个模块：分隔标签是**跨模块的约定**，但保证它的函数曾经是       ║
║ 某一个模块的私有实现 —— 于是同一个洞被修了三次、漏了两次                   ║
╚══════════════════════════════════════════════════════════════════════════╝

我们靠 `<jd>…</jd>` 把"资料"和"指令"分开（见 `llm/base.py` 与 `match_llm_svc`
的说明）。这个防护成立的**唯一前提**是：

    **标签内的内容无法提前把标签闭合。**

只要有人手拼 `f"<jd>{用户可控文本}</jd>"`，这个前提就没了：
文本里写一个 `</jd>` 就能把标签提前闭合，后面的内容全部落到标签外面。

真实踩过的三个实例（都在 ISSUES 里有记录）：

    #2  `jd_text` 没有失效化                        → 已修
    #8  表头五字段（公司/职位/城市/经验/学历）没有   → 已修（第一版只挪进标签、没中和，返工）
    #29 `greeting_svc` 是**另一份 build_payload**，整条路径没中和 → 本模块的由来

所以结论不是"再补一次"，而是：

    · 失效化逻辑收进**这里**（谁都能用，不再是某个模块的私有函数）；
    · 并且提供 `wrap_delimited()` —— **想拼标签就用它**，中和在内部完成。
      这样"忘记中和"在结构上不可能发生（除非有人绕过它去手拼字符串）。

★★ 第四次现身（ISSUES #56）：**名单不完整**，于是"单点"自己成了假防护

    前三次修的都是"谁用了标签"（补一个调用点、补一个字段），第四次是
    **`DELIMITED_TAGS` 名单本身漏登记**：`facts` 被 3 处调用点用了，
    却没有登记 —— `neutralize_delimiters` 不认识它，正文里写一个 `</facts>`
    照样能提前闭合。看上去在用统一入口，实际上什么都没防住。

    教训：**"大家都走同一个入口"只解决了"忘调用"，没解决"入口内部名单不全"。**
    所以现在 `wrap_delimited()` 额外保证一条与名单无关的性质：

        **它自己拼出来的那个标签，一定能被它自己中和掉**（不依赖是否已登记）。

    名单的作用因此被收窄成"跨标签防护"（比如 JD 正文里写 `</resume>` 想跳出
    后面那个块），而"我这一块的闭合标签"由 `wrap_delimited` 无条件兜住。
    未登记的标签仍会记一条 warning，因为这通常意味着有人加了新标签却忘了登记 ——
    但**即使忘了，也不会再出现假防护**。

★ 失效化的做法：把可疑片段的尖括号换成全角 `＜＞`。
  · 保留可读性（人还能看出这里原本写过 `<jd>`）；
  · 语义上不再是标签；
  · **不用零宽字符** —— 不可见字符会让日志与排障变得难以理解。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from jobhunter.utils.logging import get_logger

logger = get_logger(__name__)

__all__ = ["DELIMITED_TAGS", "neutralize_delimiters", "wrap_delimited"]

#: 会被我们当作"分隔标签"的词。用户文本里出现它们时必须先失效掉。
#:
#: ★ 加新标签时**必须同时**在这里登记。有两条守卫盯着：
#:   · `tests/unit/test_llm_safety.py`（本仓库）从源码反查 `wrap_delimited(标签, …)`
#:     的实参，漏登记会红；
#:   · 独立验证会话也有一条同方向的用例（就是它抓到 #56 的）。
#:   ★ 写文档时注意：这两条守卫都是**按源码文本扫**的，所以在注释/docstring 里
#:     举例不要写成"带引号的调用样子"，否则例子本身会被当成真实调用点。
#: `facts` 是 ISSUES #56 补上的 —— 它被漏了整整三个阶段，
#: 因为当时的守卫只遍历"已登记的标签"（守得住名单对不对，守不住东西在不在名单里）。
#: `previous` 是只读代码审查 JH-09 补上的 —— `greeting_svc` 的"只修字数"那次调用
#: 一直手拼着这个标签（上一版输出里可能有岗位标题/JD 片段），同样的洞第五次现身。
DELIMITED_TAGS: tuple[str, ...] = ("jd", "resume", "system", "facts", "previous")

#: 合法的标签名（拼标签时先校验，避免出现 `<a b>` 这种能被解析器玩出花样的写法）。
_TAG_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def _delimiter_re(tags: Iterable[str]) -> re.Pattern[str]:
    """构造"匹配这些标签的任意开/闭写法"的正则（大小写不敏感，容忍标签内空白）。

    故意**宽一点**：漏掉一种写法就等于留一条通道，而误伤的代价只是把一对尖括号变全角。
    """
    return re.compile(
        r"<\s*/?\s*(?:" + "|".join(re.escape(tag) for tag in tags) + r")\s*/?\s*>",
        re.IGNORECASE,
    )


_DELIMITER_RE = _delimiter_re(DELIMITED_TAGS)


def _widen(match: re.Match[str]) -> str:
    """把命中的标签写成全角形式（`<` → `＜`，`>` → `＞`）。"""
    return match.group(0).replace("<", "＜").replace(">", "＞")


def neutralize_delimiters(text: str | None) -> str:
    """把文本里可能**冒充分隔标签**的片段改成无法闭合的形式。

    刻意**不**删除这些片段 —— 删掉会让文本与原文不一致，
    而排障时希望任何改动都看得见。

    ★ 幂等：已经变成全角的片段不会再被匹配到（所以重复调用不会越改越花）。
    """
    if not text:
        return text or ""
    return _DELIMITER_RE.sub(_widen, text)


def wrap_delimited(tag: str, body: str | None) -> str:
    """把一段文本包进 `<tag>…</tag>`，**并先失效化内部的分隔符**。

    ★ 这是拼标签的**唯一推荐入口**：只要用它，就不可能忘记中和。
      手拼 `f"<{tag}>{body}</{tag}>"` 是这一系列缺陷的共同来源。

    ★★ 与名单无关的保证（ISSUES #56）：**这个标签自己**的闭合写法一定会被中和，
      哪怕它还没登记进 `DELIMITED_TAGS`。理由是"名单可能漏"这件事已经被现实证明了
      四次（#2 / #8 / #29 / #56），所以安全性不该建立在"名单是全的"这个假设上。

    Args:
        tag: 标签名（小写字母开头）。**建议同时登记进 `DELIMITED_TAGS`** ——
             不登记时本块仍然安全，但别的块里出现的 `</tag>` 不会被中和（会记 warning）。

    Raises:
        ValueError: 标签名不合法（如空串或含空格）—— 这种标签拼出来本身就可疑。
    """
    if not _TAG_NAME_RE.match(tag or ""):
        raise ValueError(
            f"标签名不合法：{tag!r}。只允许小写字母开头的 [a-z0-9_-]*，例如 jd / resume / facts。"
        )
    safe = neutralize_delimiters(body)
    if tag not in DELIMITED_TAGS:
        # 名单里没有它 → 至少把**它自己**的闭合/开启写法中和掉，绝不产出假防护
        safe = _delimiter_re((tag,)).sub(_widen, safe)
        logger.warning(
            "分隔标签 %r 没有登记进 DELIMITED_TAGS：本块仍安全（自己那个标签已单独中和），"
            "但其它块里出现的 </%s> 不会被中和 —— 请把它补进 llm/safety.py 的名单。",
            tag,
            tag,
        )
    return f"<{tag}>\n{safe}\n</{tag}>"

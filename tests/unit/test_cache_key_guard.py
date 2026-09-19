"""结构守卫：**每一个** `compute_cache_key()` 调用点都必须传提示词指纹（ISSUES #55）。

★★ 这个测试守的是什么

    `compute_cache_key(prompt_fingerprint=...)` 是个**可选**参数，
    于是 #24 只给 match 接上了，另外四个调用点（greeting / tailor /
    followup / question）安静地漏了 —— 结果是"提示词变了、key 没变"，
    用户看到旧文案/旧分数，还以为自己改的东西没用。

    "形参加了但没人传"在这个项目已经是**第三次**出现（#37 的 reminder 参数、
    #24、#55）。靠"下次记得传"是没用的，所以这里不测行为，而是直接读源码：
    只要 `src/` 里出现一个不传指纹的调用点，这条测试就红。

★ 为什么用 AST 而不是正则：
    正则数不清多行调用与嵌套括号，也分不清"调用"和"注释里提到的调用"。
    AST 拿到的就是 Python 真正解析出来的调用节点。
"""

from __future__ import annotations

import ast
from pathlib import Path

from jobhunter.config.paths import find_project_root

#: 被守卫的函数名（`llm/cache.py` 里的那个）
_GUARDED = "compute_cache_key"
#: 必须出现的参数名
_REQUIRED_KWARG = "prompt_fingerprint"


def _iter_calls(source: str) -> list[ast.Call]:
    """遍历 AST，找出所有"按名字调用 `compute_cache_key`"的节点。"""
    tree = ast.parse(source)
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # 支持 `compute_cache_key(...)` 与 `llm_cache.compute_cache_key(...)`
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == _GUARDED:
            calls.append(node)
    return calls


def _passed_kwargs(call: ast.Call) -> set[str]:
    return {kw.arg for kw in call.keywords if kw.arg}


def _guarded_call_sites() -> list[tuple[Path, ast.Call]]:
    """扫 `src/` 下的业务代码，返回所有被守卫的调用点。"""
    src = find_project_root() / "src"
    sites: list[tuple[Path, ast.Call]] = []
    for path in sorted(src.rglob("*.py")):
        # 定义本身（llm/cache.py）不算调用点
        if path.name == "cache.py" and path.parent.name == "llm":
            continue
        for call in _iter_calls(path.read_text(encoding="utf-8")):
            sites.append((path, call))
    return sites


# ===========================================================================
# 1. 主守卫：业务代码里每个调用点都传了指纹
# ===========================================================================
def test_every_cache_key_call_site_passes_the_prompt_fingerprint() -> None:
    sites = _guarded_call_sites()
    assert sites, (
        f"一个 `{_GUARDED}(` 调用点都没扫到 —— 要么调用点被删光了，"
        "要么这个守卫的扫描逻辑坏了（守卫失效比漏传更危险）"
    )
    offenders = [
        f"{path.relative_to(find_project_root())}:{call.lineno}"
        for path, call in sites
        if _REQUIRED_KWARG not in _passed_kwargs(call)
    ]
    assert not offenders, (
        "以下调用点没有传 "
        f"`{_REQUIRED_KWARG}=`（ISSUES #55：提示词变了但缓存 key 不变，"
        "会把上一份文案/分数原样复用）：\n  " + "\n  ".join(offenders)
    )


def test_the_guard_covers_the_known_five_call_sites() -> None:
    """五个调用点一个都不能少 —— 少一个说明有人把指纹参数连同调用行一起删了。

    ★ 数量写死是为了"删掉调用点"这件事必须被人看到：
      如果将来真的合并/删除某个任务，改这个数字即可（改动本身就是一次复核）。
    """
    names = {path.name for path, _ in _guarded_call_sites()}
    assert names == {
        "followup_svc.py",
        "greeting_svc.py",
        "match_llm_svc.py",
        "question_svc.py",
        "tailor_svc.py",
    }, f"缓存 key 调用点集合变了：{sorted(names)}"


# ===========================================================================
# 2. 元测试：守卫本身真的会响（否则上面两条可能永远绿）
# ===========================================================================
def test_the_guard_actually_fires_on_a_planted_violation(tmp_path: Path) -> None:
    """★ 一个从来不红的守卫等于没有守卫。

    这里把"漏传指纹的调用点"真的写进一个临时文件再用同一套逻辑扫，
    确认它能被抓出来 —— 保证主守卫不是"恒真断言"。
    """
    planted = tmp_path / "planted.py"
    planted.write_text(
        "from jobhunter.llm import cache as llm_cache\n"
        "\n"
        "def f():\n"
        "    return llm_cache.compute_cache_key(\n"
        "        prompt_version=1,\n"
        "        model='m',\n"
        "        jd_hash='a',\n"
        "        resume_content_hash='b',\n"
        "        task='greeting',\n"
        "    )\n",
        encoding="utf-8",
    )
    calls = _iter_calls(planted.read_text(encoding="utf-8"))
    assert len(calls) == 1
    assert _REQUIRED_KWARG not in _passed_kwargs(calls[0]), (
        "元测试的样本没构造对：它本来就该是「漏传」的样子"
    )


def test_the_guard_ignores_mentions_in_comments_and_strings() -> None:
    """注释/文档里提到 `compute_cache_key(` 不该被当成调用点（否则守卫会误报）。"""
    source = (
        "# 调用 compute_cache_key( 时必须传指纹（这里是注释）\n"
        "DOC = 'compute_cache_key( 也不该算调用'\n"
        "def f():\n"
        "    return compute_cache_key(\n"
        "        prompt_version=1,\n"
        "        model='m',\n"
        "        jd_hash='a',\n"
        "        resume_content_hash='b',\n"
        "        prompt_fingerprint='x',\n"
        "    )\n"
    )
    calls = _iter_calls(source)
    assert len(calls) == 1
    assert _REQUIRED_KWARG in _passed_kwargs(calls[0])


def test_the_guard_sees_through_a_star_kwargs_only_call() -> None:
    """`**kwargs` 转发**不算**"传了指纹" —— 那正是"看着有线、其实没接线"。

    ★ 这是有意的严格：本项目已经吃过一次"形参加上了但没人传"的亏，
      如果放开 `**kwargs` 就等于给这个洞留了后门。
    """
    source = "def f(**kw):\n    return compute_cache_key(**kw)\n"
    calls = _iter_calls(source)
    assert len(calls) == 1
    assert _REQUIRED_KWARG not in _passed_kwargs(calls[0])


# ===========================================================================
# 3. 算法只有一份：match 的同名封装必须委托给 cache.payload_fingerprint
# ===========================================================================
def test_match_wrapper_delegates_to_the_shared_implementation() -> None:
    """`match_llm_svc._prompt_fingerprint` 只是薄封装，算法不得二次实现。

    ★ 两份实现 = 迟早不一致（改了其中一份，"同一份提示词"在两条任务上
      算出两个指纹，缓存行为随任务漂移，且没有任何报错）。
    """
    from jobhunter.llm import cache as llm_cache
    from jobhunter.llm.base import LLMMessage, PromptPayload
    from jobhunter.services import match_llm_svc

    payload = PromptPayload(
        prompt_version=1,
        task="match",
        messages=[LLMMessage(role="user", content="同一份提示词")],
    )
    assert match_llm_svc._prompt_fingerprint(payload) == llm_cache.payload_fingerprint(payload)


def test_fingerprint_is_stable_and_sensitive() -> None:
    """指纹的两个基本性质：同样输入同值（跨进程稳定）、差一个字就变。"""
    from jobhunter.llm import cache as llm_cache
    from jobhunter.llm.base import LLMMessage, PromptPayload

    def _payload(**overrides: object) -> PromptPayload:
        base: dict[str, object] = {
            "prompt_version": 1,
            "task": "greeting",
            "messages": [LLMMessage(role="user", content="你好")],
        }
        base.update(overrides)
        return PromptPayload(**base)  # type: ignore[arg-type]

    first = llm_cache.payload_fingerprint(_payload())
    assert first == llm_cache.payload_fingerprint(_payload()), "同样输入必须同指纹"
    assert len(first) == 64 and first == first.lower()

    # 文案 / 温度 / json 模式 / 任务名 —— 任何一个变都必须改指纹
    assert first != llm_cache.payload_fingerprint(
        _payload(messages=[LLMMessage(role="user", content="你好！")])
    )
    assert first != llm_cache.payload_fingerprint(_payload(temperature=0.9))
    assert first != llm_cache.payload_fingerprint(_payload(json_mode=False))
    assert first != llm_cache.payload_fingerprint(_payload(task="tailor"))
    assert first != llm_cache.payload_fingerprint(_payload(prompt_version=2))

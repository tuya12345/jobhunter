# ISSUES —— 验证问题台账

> **协作规则**
> - 验证会话产出 `docs/REVIEW-P3.md`（完整报告），本文件是**问题清单 + 开发角色的处理台账**。
> - 每条问题一行：来源、等级、状态、处理结论（**改了哪个文件、怎么改的、有没有测试守住**）。
> - 🔴 直接修，修完在本文件关闭；🟡 / 🟢 先与用户确认再定。
> - **测试由验证会话负责**：本台账里不出现"我改了测试让它过"这种处理方式。
> - 🟡/🟢 条目在 `tests/verify/` 里有对应的 `xfail` 用例（断言未被放宽）：
>   修好后它们会变成 **XPASS**（`pytest -ra` 会列出来），届时把 `xfail` 标记删掉即可。

等级约定：🔴 阻断 / 必须修　🟡 建议修　🟢 观察项

---

## 未关闭

| 编号 | 来源 | 阶段 | 等级 | 问题 | 状态 |
|---|---|---|---|---|---|
| #4 | 验证 | P3 | 🟡 | `AI大模型` / `LLM大模型` 这类连写抽不到（中文分支左边界 `(?<![A-Za-z0-9_])` 被前一个 ASCII 字母否掉，`resume_svc.py:400`） | 待用户确认 |
| #5 | 验证 | P3 | 🟡 | `大模型` 既是内置规范名（`resume_svc.py:260`）又是 `LLM` 的别名（`config.yaml:52`）→ 一句"大模型"抽成 `['LLM','大模型']` 两个关键词：技能分母被灌水 + **假缺口**（简历写了 LLM 却报"缺 大模型"） | 待用户确认 |
| #6 | 验证 | P3 | 🟡 | 抽词前不做 NFKC → 全角 `Ｐｙｔｈｏｎ` 静默漏抽（`match_svc.py:330` 直接把原始 `jd_text` 交给 `_match_any`；`parse_text.normalize_text` 已有 NFKC 能力但没被用在这条路上） | 待用户确认 |
| #7 | 验证 | P3 | 🟡 | `io.py:18` 声称的"第二层防护 ruff `PLW1514`"**没有生效**：该规则是 preview，`pyproject.toml` 无 `preview = true`、`ci.yml:53` 无 `--preview`；实测 ruff 输出 `warning: Selection 'PLW1514' has no effect because preview is not enabled.` | 待用户确认 |
| #8 | 验证 | P3 | 🟡 | 表头五字段 `company/title/city/experience/education`（`match_llm_svc.py:195-198`）与 `rule_hint`（`:202`）**裸拼在 `<jd>` 标签之外** → 把指令写进公司名即可绕过标签防护 | **已随 #2 顺手修**（见下），等用户确认后关闭 |
| #9 | 验证 | P3 | 🟡 | **无信息量的 JD 拿到 80 分**（分数线 60）：`title/company/jd_text` 全空时，技能维度判不出来（权重 40 被让掉）+ 方向 0.6 中性 + 硬条件 1.0 → 80 分。与已修的"JD 写得越少技能分越高"是同类问题的更大尺度复发（`match_svc.py:833-852`） | 待用户确认 |
| #10 | 验证 | P3 | 🟡 | HTTP 200 但响应体不是 JSON（网关返回 HTML）时抛**裸 `JSONDecodeError: Expecting value: line 1 column 1`**，不告诉用户"响应不是 JSON"（`deepseek.py:208`） | 待用户确认 |
| #11 | 验证 | P3 | 🟡 | pydantic 非严格模式：`{"score": true}` 被当成 **1 分**接受（`match_llm_svc.py:107` 无 `StrictInt`/`strict=True`）→ 与模块 docstring「分数越界即校验失败、**不做静默截断**、绝不猜」矛盾 | 待用户确认 |
| #12 | 验证 | P1 | 🟡 | 非 UTF-8（GBK）简历与损坏 docx 只报**原始 Python 异常**（`UnicodeDecodeError` / `PackageNotFoundError`），没有中文可操作提示（`resume_cmd.py:117-120`）；对比"空文档"分支有详细中文说明，口径不一致 | 待用户确认 |
| #13 | 验证 | P2 | 🟡 | `jh job add --file` 读取阶段**不去重**（`job_cmd.py:376-387`）→ 同一 URL 各抓一次，白白多一次对外请求 + 多等一个 2–5s 随机延时（与"频率克制"精神相悖） | 待用户确认 |
| #14 | 验证 | P2 | 🟡 | `--file` **无 scheme 白名单、无条数上限**（`job_cmd.py:337-351`）→ `file:///C:/Windows/win.ini`、`http://127.0.0.1` 会原样进抓取队列；一次可喂 5000 行（每行 2–5s → 连续对外请求数小时） | 待用户确认 |
| #15 | 验证 | P1 | 🟡 | 纯空白 `.txt` 被当成**"成功导入的空简历"**（`docx.py:103` 的 `splitlines()`+`rstrip` 让 `lines` 非空，`resume_svc.py:1980` 的 `if not result.lines` 判不到）→ 库里多一份 0 技能/无姓名的简历，而且**因"第一份自动成为默认"变成后续所有匹配的参照物** | 待用户确认 |
| #16 | 验证 | P0 | 🟡 | CI 的 `uv sync --locked`（`ci.yml:50`）**不装 optional extras**（实测 dry-run 会 uninstall `python-docx==1.2.0`、`playwright==1.62.0`），而 3 个测试文件顶部用 `pytest.importorskip("docx")` → **73 条简历解析测试在 CI 上被静默跳过**。即"CI 在 windows 上真验证简历解析"没有发生 | 待用户确认（一行可修） |
| #17 | 验证 | P3 | 🟡 | 改了简历重跑规则层后，上一次的 `llm_score` 仍被当成 `match_score`（`match_svc.py:1073` 的合成规则 `llm_score if not None else rule_score`）→ 用户看到"改了简历分数纹丝不动"，正是评审采纳项 #3 点名的症状（发生在库里的历史值而非缓存命中） | 待用户确认 |
| #18 | 验证 | P0 | 🟢 | `rate_limit.max_pages`（默认 3，`config.yaml:120` 也写了）**全仓库无任何读取点**（只有定义 + `base.py:132` 形参）→ 配置像约束、实为装饰。当前无搜索功能故无暴露 | 待定 |
| #19 | 验证 | P2 | 🟢 | 硬编码伪造桌面 Chrome UA（`manual.py:225-229` 写死 `Chrome/124.0.0.0`），客观上隐藏了默认 UA 里的 HeadlessChrome 标识。无 `navigator.webdriver` 抹除、无 stealth、无 Cookie 复用，**不构成风控规避**，但建议在 README/COMPLIANCE 里如实写明 | 待定 |
| #20 | 验证 | P2 | 🟢 | 去重白名单里 `url` 会被**无条件覆盖**（`job_svc.py:91` + `:365`），而 `platform_job_id` 有"只在库内为空时补"的保护（`:350-357`）；`source_keyword` 也会被后续 upsert 改写 → 影响渠道复盘统计 | 待定 |
| #21 | 验证 | P2 | 🟢 | 文档口径矛盾：`README.md:191,224` 写"**三**层去重"，`docs/roadmap.md:44,368` 与 `docs/architecture.md:368` 写"**两**层"（architecture 4.6 节又列了 ①②③+否决） | 待定 |
| #22 | 验证 | P0 | 🟢 | 承诺"真实 LLM 抽样检查放 `@pytest.mark.live`、只在 nightly 跑"，但 `tests/` 内 **marker 零使用**（只有 `tests/__init__.py:6-7` 的注释），仓库也**没有 nightly workflow**（只有 `ci.yml`）。当前无风险（因为根本没有 live 测试），但承诺未落地 | 待定 |
| #23 | 验证 | P3 | 🟢 | `reasoning_content` 从不读取（`deepseek.py:212-215` 只取 `message.content`）→ 若换成 `deepseek-reasoner` 且输出被截断只回了推理内容，会判空、白付 token 后降级 | 待定 |
| #24 | 验证 | P3 | 🟢 | 缓存 key 只覆盖 `prompt_version/model/jd_hash/resume.content_hash`（`cache.py:70-93`），但 prompt 里还有 `rule_hint`（随 `match_weights`/`skill_aliases` 变）与表头字段 → **改了权重会静默复用旧分**（与"改 prompt 要 bump PROMPT_VERSION"的纪律同源，但权重不在版本号里） | 待定 |
| #25 | 验证 | P3 | 🟢 | `llm_cache` 无 TTL、全仓库无清理/统计入口（`models/llm_cache.py`、`cache.py`）；`jh stats` 是 P6 占位 → 缓存会无限增长且成本无汇总出口 | 待定 |
| #26 | 验证 | P3 | 🟢 | 缓存命中时把**历史** tokens/cost 累加进"本次"统计（`match_llm_svc.py:424-426`）→ 结尾打印的金额 ≠ 本次实际支出（口径问题，不是算错） | 待定 |
| #27 | 验证 | P3 | 🟢 | `jh match run` 对所有岗位打分，**包含已 `applied`/`skipped` 的**（`STATUS.md` §3.5 已自述）→ 已投递岗位的分数会随简历变化而变动，可能干扰"投递时的分数"回溯 | 待定 |
| #28 | 验证 | P0 | 🟢 | `.env` 里含**真实姓名与简历路径**（`JH_DEFAULT_RESUME_PATH`）。已被 `.gitignore` 覆盖且 `git ls-files --error-unmatch .env` 失败（确认未跟踪）→ 不构成泄露，仅提醒：`.env` 不要分享/贴图；日志脱敏也不覆盖姓名与**文件名**（`resume_svc.py:1065`、`docx.py:212`） | 待定 |

> 🟡 条目共 14 条（#4–#17），按用户指示**先不动**，等确认后再逐条定。
> 其中 **#16 是一行的事**（`ci.yml` 加 `--extra docx`），且它让"CI 在 Windows 上真验证简历解析"
> 从"没有发生"变成"发生" —— 已就绪，等一句话即可改。

---

## 已关闭

| 编号 | 来源 | 阶段 | 等级 | 摘要 | 处理 |
|---|---|---|---|---|---|
| #1 | 验证 | P3 | 🔴 | 中文紧邻英文技能整词漏抽（`\b` 是 Unicode 语义） | ✅ 见下 |
| #2 | 验证 | P3 | 🔴 | JD 自带 `</jd>` 可提前闭合标签 | ✅ 见下 |
| #3 | 验证 | P3 | 🔴 | LLM 层只 flush 不 commit → 花了钱的结果会丢 | ✅ 见下 |

### #1 中文紧邻英文技能整词漏抽 —— 已修

**改了什么**：`src/jobhunter/services/resume_svc.py::_alias_pattern()`
ASCII 分支的**左边界**从 `\b` 改为 `(?<![A-Za-z0-9_])`，与右边界 `(?![A-Za-z_])` 对称。
同时把"两侧都不能用 `\b`"的成因写进注释与函数 docstring（原先的注释还在教人用 `\b`，属于过期说明）。

**为什么是这个改法**：`\b` 的边界基于 `\w`，而 Python 的 `\w` **包含汉字** →
"悉"与"J"之间没有词边界。只看 ASCII 字母数字下划线做边界，汉字就成了合法边界，
而 `XJava`（前面是 ASCII 字母）仍被正确拒绝。右边界**保留不含数字**的写法，
这样 `Vue3` / `TypeScript5` 仍能命中。

**实测效果**（原来整句只抽到 0~2 个词）：
```
JD: 熟悉Java开发，用Go写过高并发服务，熟练Python编程，使用Docker部署，了解MySQL与Redis
抽出: Python、Java、Go、MySQL、Redis、并发、Docker
```

**守住它的测试**：
- `tests/unit/test_resume_svc.py::test_ascii_skill_adjacent_to_chinese_is_matched`
  （5 条参数化：`熟悉Java开发` / `用Go写后端服务` / `熟练Python编程` / `使用Docker部署服务` / `掌握Vue3组件开发`）
- `tests/unit/test_resume_svc.py::test_adjacency_does_not_break_word_boundary`
  （反向断言：`熟悉JavaScript开发` 不命中 Java、`用过Google分析` 不命中 Go、`XJava开发` 被拒）
- 验证会话的 `tests/verify/test_v_boundary.py` 那 7 条 red 全部转绿。

**对既有数据的影响**：真实简历重新解析后技能数与 `content_hash` **完全不变**
（它的技能栏本来就用 `、` 分隔，没踩到这个坑）→ 不需要重导、缓存不会失效。

### #2 JD 自带 `</jd>` 可提前闭合标签 —— 已修

**改了什么**：`src/jobhunter/services/match_llm_svc.py`
1. 新增 `_neutralize_delimiters()`：把文本里形如 `</jd>` / `<jd>` / `</resume>` / `</ jd >`
   的片段（大小写不敏感、容忍标签内空白）的尖括号换成全角 `＜＞`。
   JD 正文与简历摘要**都过一遍**。
2. 表头五字段（公司/职位/城市/经验/学历）从标签外**移进 `<jd>` 标签内**
   （它们来自页面抓取，同样是用户可控文本）。
3. 提示词结构改为先给标签再给正文，`rule_hint`（我们自己生成的锚点）留在标签外。

**为什么"换成全角尖括号"而不是删掉**：删掉会让 JD 与原文不一致，而排障时希望任何改动都看得见；
全角版本人还能看出这里原本写过 `<jd>`，但语义上不再是标签。**故意不用零宽字符**
（不可见字符会让日志和排错变得难以理解）。

**守住它的测试**：
- `tests/unit/test_match_llm.py::test_jd_cannot_close_its_own_delimiter`（新增）
- `tests/unit/test_match_llm.py::test_prompt_satisfies_json_mode_requirements`（**按指示收紧**）：
  从「标签存在」改为「`<jd>` 与 `</jd>` 各恰好 1 个 + JD 正文/公司名/职位名都落在闭合标签之前」
- 验证会话的 `tests/verify/test_v_adversarial.py::test_jd_cannot_close_its_own_delimiter` 转绿。

### #3 LLM 层只 flush 不 commit —— 已修

**改了什么**：`src/jobhunter/services/match_llm_svc.py::_apply()` 的收尾从 `session.flush()`
改为 `session.commit()`；模块 docstring 与函数 docstring 同步改为"立刻 commit 落盘"。

**为什么 commit 放在 `_apply()` 而不是调用方**：缓存行（`llm_cache`）与岗位分（`jobs.llm_score`）
是**同一条结果**，必须在同一事务里提交，否则可能出现"缓存里有、岗位上没有"
（下次命中缓存却仍显示没分）。这一次 commit 同时覆盖 `put_cached()` 的写入与字段更新。

**并发安全性**：每个任务的 DB 段是同步的（唯一的 `await` 在调 Provider 处），
不存在"A 提交到一半被 B 插进来"；`sessionmaker` 设了 `expire_on_commit=False`，
提交后对象不过期，后续任务继续复用同一 Session。已写进 `_apply()` 的 docstring。

**守住它的测试**：验证会话的
`tests/verify/test_v_failure.py::test_llm_results_are_durable_without_the_cli_commit`
（跑完 LLM 层**不调用 CLI 的 commit**，用新连接必须能看到 `llm_cache` 行与 `jobs.llm_score`）→ 转绿。

---

## 附：验证会话的 xfail 标记处理

修完 3 个 🔴 后复跑 `pytest tests/verify -q -ra`：**105 passed, 15 xfailed, 1 xpassed, 0 failed**。

**新增 1 条 XPASS（请删除它的 `xfail` 标记）**：

| 用例 | 对应 ISSUE | 为什么会 XPASS |
|---|---|---|
| `tests/verify/test_v_adversarial.py::test_untrusted_job_header_fields_are_inside_a_delimiter` | **#8（🟡）** | 按 #2 的指示"顺手把表头五字段也放进标签内"，这条的断言已经成立 → 从 xfail 变成 XPASS |

其余 15 条 xfail 保持 XFAIL（🟡 未修，符合预期）。
> ⚠️ #8 的**主要问题**（表头字段在标签外）已随 #2 修掉，但 `rule_hint` 仍在标签外 ——
> 它由我们自己生成（不含用户输入），风险低；是否也要收进标签内，等你确认。

---

## 附：P4 / P5 未到期但**必须在阶段内兑现**的两条红线

这两条不算 P3 的问题，但登记在此以免遗忘（详细论证见 `docs/REVIEW-P3.md` §B1 与 §D）：

| 编号 | 阶段 | 等级 | 要求 | 建议的验证断言 |
|---|---|---|---|---|
| P4-GREETING | P4 | 🔴 | 开场白必须复用共享层产物 `jobs.jd_keywords_json`（否则"开场白必然沾边"落空） | 「开场白里出现的 JD 关键词 ⊆ `jobs.jd_keywords_json`」+「开场白不含简历里没有的技能」 |
| P5-FIDELITY | P5 | 🔴 | `FidelityGuard` 必须是**代码强制**（生成/审核/导出三处），并用**恶意 fixture** 做确定性断言 | 喂含 `Kubernetes` / `300%` / `10个模块` / `2024年` 的"生成结果"，断言全部被拦；不得写"调真实 LLM 再断言输出"的测试 |

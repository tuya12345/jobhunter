# STATUS —— 阶段交付说明（给验证会话看）

> **维护规则**：每完成一个阶段，由开发角色更新本文件，然后停下等验证。
> 内容只写**事实与本阶段范围**：做了什么 / 关键文件 / 已知问题 / 建议重点验证的地方。
> **不写自评结论、不写对抗性测试** —— 那是验证会话的职责。

---

## P4.5 —— 零手打跟进（★ MVP 完成）

**状态**：✅ 开发完成 → 已按流程提交（`start` + 交付声明第 1 轮），**等待复验**
**测试**：`pytest tests/unit` **412 passed**；`pytest tests/` = 574 passed / 0 failed（见 §4）
**阶段标记**：`jh --version` = `phase P4.5`

### 0. 本轮补记（真实 key 首次联调 + 按流程提交）

**① 真实 LLM 路径终于跑通了，并立刻暴露一个 P4 的真缺陷**

用户提供了测试 key（写在 `.env`，gitignored）。第一次跑真模型：

```
jh today --limit 2  →  4 个版本全部降级成模板
日志：完整版 195 字（限 150）、精简版 112 字（限 60）
```

**闸门是对的，但模型侧没人兜住** —— 钱花了、拿到的是模板，而这个功能的价值恰恰在"像人写的"。
**所有单测都是绿的**（mock 的固定话术很短，天然不触发字数问题）——
这是"只有跑真东西才会发现"的典型。

修法（`greeting_svc.py`，提示词 v1→v3）：

| 改动 | 为什么 |
|---|---|
| 提示词写清"**按字符数**，含标点，一个汉字算 1 个字符" | 原来只写"80-150 字"，模型理解成"差不多这个量级" |
| 给**更窄的目标区间**（完整版 100-130、精简版 30-55） | 让它别贴着上限写 |
| 要求"**写完自己数一遍**" | 不给自检要求，它就不数 |
| 精简版额外说明"要填进**有字数上限的输入框**，一句话 + 一个问题就够" | 二次联调它仍写 69/66 字，加了这句才达标 |
| **新增一次"只修字数"的修复调用** | 这类失败可数、可验证、改短不引入新事实 —— 最适合让模型自己改 |
| `is_length_only_failure()`：**编造类失败不许走修复** | 那是信任问题不是笔误；让同一个模型再写一遍，无法保证它是"照着改"还是继续编 |
| 字数判定收敛到 `_length_problems()` **单点** | 原来闸门与修复判定各一套措辞 → "纯字数问题"判错（被自己的测试打脸） |

**实测三轮（同一批岗位、真实 key）**：

| 轮次 | 结果是模型文本的版本 |
|---|---|
| 第一次 | **0 / 4** |
| 加修复调用后 | **5 / 6**（精简版仍超） |
| 收紧精简版提示词后 | **6 / 6**（完整版 98~142 字、精简版 50~58 字） |

**② 按平台流程提交**：P4 通过后 `start P4.5` + 交付声明（含 6 条 `known_issues` 与 5 条 `verify_hints`）。
`#46`（P4.5 未走流程）已在本轮消解。

**③ 真实联调复现了 🟡 #30**：模型写"算法"被判成"简历里没有这个技能" → 整版降级。
`算法` 这类动作/通用词在技能词表里，而 `resume_skill_pool` 只读结构化技能字段。
**未擅自修**（🟡 需用户确认），证据已写进平台交付声明。

### 1. 本阶段做了什么

| 命令 | 行为 |
|---|---|
| `jh apply 3=applied 7=replied` | ★ **批量打标**：按 `jh today` 的序号一次处理一整轮 |
| `jh apply "#12=replied"` | ★ 按 **job_id** 打标（`#` 前缀）—— 跟进老岗位时序号已经不可用了（它们不在今天的清单里），id 是唯一稳定的定位方式 |
| `jh apply "字节" --status viewed` | ★ **模糊匹配**公司名/职位名；**命中多条时列出来让你选，绝不猜**（`--yes` 才全改） |
| `jh apply --pending` | ★ 待处理清单：还没投的 + 投后 3/7/14 天没动静的，带 job_id / 状态 / 投后天数 / 为什么在这里 |
| 任何 `jh` 命令 | ★ **顶部固定提醒**（Typer 全局 callback）：`📌 待处理：投后 3 天未跟进 N 条 ｜ …` |
| `jh apply --timeline <id>` | 仍是占位（P6） |

**顶部提醒的四类统计**（`services/reminder_svc.py`）：

| 类别 | 口径 |
|---|---|
| 投后 3/7/14 天未跟进 | 状态仍是 `applied`/`viewed` 且 `applied_at` 的自然日差 ≥ 阈值。**三个桶是累计的**（第 20 天那条同时属于三个桶）—— 用户要的是"有几条到了该处理的时间点"，不是"恰好第 3 天的那几条" |
| 本周面试 | `interviews` 今天起 7 天内、状态 `待面` |
| 笔试/测评截止 | `assessments` 3 天内截止、状态 `待做` |
| Offer 签约截止 | `offers` 3 天内截止、状态 `待定` |

> 后三类依赖 P6 的表（当前没有写入路径，生产环境里必然为空），但查询逻辑与渲染都已就位并有测试
> （用手工插入的行验证），P6 一接上就能用。

### 2. 三条实现纪律（`reminder_svc` 的模块 docstring 里写死了）

1. **空的时候一个字都不打印** —— 每天被"你没有被提醒"刷屏，第三天就会把提醒整个关掉。
2. **绝不能因为提醒失败而让命令失败** —— 库没建/表被删/文件损坏都退化成"没有提醒"。
   它在**每条**命令前都跑，没有资格阻断主命令。
3. **快** —— 只做 4 个带索引的小查询。

### 3. 关键文件

| 文件 | 职责 |
|---|---|
| `services/reminder_svc.py` | 顶部提醒：采集（4 类）+ 渲染；`get_reminders()` 自己开短连接，可注入 `session`/`now` |
| `services/apply_svc.py` | 新增 `list_actionable()` + `PendingRow`（待处理清单，按紧急度排序） |
| `cli/commands/apply_cmd.py` | 批量打标（`_mark_batch`）、模糊匹配（`_mark_by_keyword`）、`--pending` |
| `cli/app.py` | 顶部提醒的调用点（P0 就固定好的位置，现在真的会打印了）；console 改用共享的 `width=200` |
| `services/greeting_svc.py` | 开场白提示词 v3 + 一次"只修字数"的修复调用 + 字数判定单点 |
| `tests/unit/test_followup.py` | 新增 23 条自测 |
| `tests/unit/test_greeting_svc.py` | 新增 5 条（修复调用生效 / 修不好则降级 / 编造类不走修复 / 纯字数判定 / 提示词含可数预算） |

### 4. 那两条失效用例（已由验证会话处理，本轮确认）

`tests/verify/test_v_checklist12.py::test_unimplemented_features_report_honestly` 原本断言
`["apply", "3=applied"]`（批量打标）与 `["apply", "--pending"]`（待处理列表）仍是占位 ——
P4.5 实现它们后这两条自然失效。**验证会话已自行把它们移出占位清单**
（并加了一条临时塞子 `test_p45_commands_are_no_longer_placeholders`）。
我按角色边界没有动那个文件；`["apply", "--timeline", "1"]`（P6）仍然有效。

> 相关：这是我第一次"因为实现了新功能而让对方的测试失效"——**必须由对方改**，
> 否则测试这道独立防线就变成了开发可以自行调节的旋钮（平台 #45 记的就是这类越界）。

### 5. 已知问题 / 未覆盖的地方

1. **模糊匹配只看公司名与职位名**，不支持拼音/别名（"字节"能中，"bytedance" 不能）。
2. **批量打标是"全对才执行"**：只要有一处错（状态拼错 / 序号或 id 找不到）就**整批不动**并列出全部错误。
   这是刻意的（一半成功一半失败会让人不知道该不该重跑），但**不能部分重试**。
3. **`--yes` 会改掉所有命中项**，包括那些你可能没细看的 —— 只在确认关键词足够独特时用。
4. **顶部提醒的 3/7/14 天阈值写在代码里**（`FOLLOWUP_DAYS`），没有做成 config。
5. **`jh apply N=...` 的序号来自今天的快照**；跨天之后昨天的序号不再可用（改用 `#job_id`）。
6. **提醒不区分"渠道"**：同一个岗位在不同渠道投两次会各算一条。
7. 真实 LLM 路径（匹配精评 / 开场白生成）**仍未验收** —— 密钥现已配好（`.env`，gitignored），
   等用户安排联调。

### 6. 建议重点验证的地方

- **批量打标的事务语义**：有错时是否真的一条都没动；`#id` 与 `序号` 两种定位是否都对。
- **模糊匹配不猜**：命中多条时必须零改动；`--yes` 才全改。
- **提醒的三个桶**：跨天边界（今天投的、恰好第 3 天、第 14 天）、终态（offer/rejected）不该被催。
- **提醒的健壮性**：没有数据库 / 空库 / 表被删时，任何 `jh` 命令是否照常工作（这是纪律 ②）。
- **提醒的噪音**：没有待处理事项时是否**完全静默**（`--no-remind` 与 `show_reminders: false` 也要能关）。
- **隐私**：提醒行里只有公司/职位/时间，不含简历内容与联系方式。

---

## P4 —— 待投清单 + 开场白 + 投递打标 + 导出

**状态**：✅ 开发完成 → 🔧 **一轮验证修复完成（3 个 🔴 已关闭）** + 一轮全项目复查，**等待复验**
**测试**：`pytest` **550 passed, 21 xfailed, 0 failed, 0 xpassed**；`ruff check` / `format` 干净

### 0. 本轮修复与复查（对应 `docs/REVIEW-P4.md` / `docs/ISSUES.md`）

| ISSUE | 等级 | 修了什么 | 关键文件 |
|---|---|---|---|
| #29 | 🔴 | 开场白路径的 `<jd>` 可被标题里的 `</jd>` 逃逸 → **新增 `llm/safety.py`**：把"分隔标签约定 + 失效化"收成单点，并提供 `wrap_delimited(tag, body)` 作为**拼标签的唯一入口**（漏掉中和在结构上不再可能） | `llm/safety.py`（新）、`services/match_llm_svc.py`、`services/greeting_svc.py` |
| #32 | 🔴 | `jh today` 生成 N 条开场白却只在最后 commit 一次 → 改**每完成一条就 commit**（与 `match_llm_svc._apply()` 同一条纪律） | `services/today_svc.py::ensure_item` |
| #33 | 🔴 | 序号快照被本轮结果覆盖（`--limit` 缩短它、空结果清空它）→ 快照改存"**今天的完整达线清单**"（在 `limit` 之前取），且**空结果不写** | `services/today_svc.py` |
| #44 | 🔴 隐私 | **开发会话自查发现**：`resume_svc.py` 注释里用了真实简历片段（真名 / 学校 / 打码手机号，共 6 处）→ 全部替换为虚构数据 | `services/resume_svc.py` |
| — | — | 复查另一 AI 助手（WorkBuddy）的越界改动：**实质代码改动正确（已被更强的结构替代）、测试改动是收紧而非放宽、台账改动是纠正** → 全部保留、不回退；它的 `.workbuddy-ai/` 工作目录加入 `.gitignore` | `.gitignore` |

> 详细记录见 `docs/ISSUES.md` 的"已关闭"一节（含 #29/#32/#33/#44 的改法与守住它们的测试，
> 以及 WorkBuddy 越界改动的逐项复核结论）。
> ⚠️ **#44 有一个需要用户拍板的遗留**：`git log` 的历史提交里仍含那些真实字符串 ——
> 公开仓库前要么重写历史，要么接受痕迹。见 `docs/ISSUES.md` #44 的提醒。

### 1. 本阶段做了什么

| 命令 | 行为 |
|---|---|
| `jh today [--limit N] [--min-score N] [--variant X]` | 出清单：match_score 降序 → 排除已投/已跳过 → **同公司只留分数最高的一条** → 为每条生成开场白 + 写 `applications(status=pending)` |
| `jh today --copy N [--short]` | 把第 N 条（完整版/精简版）复制到剪贴板；剪贴板不可用时把原文打出来，不让人白跑 |
| `jh today --check` | 今天已投 X / 上限 N（从 `applications.applied_at` **实时聚合**，不建计数表） |
| `jh today --export md\|csv` | 导出到 `output/`，**只写新文件**（带时间戳，重名再加 `-2`） |
| `jh apply <序号> --status S` / `jh apply --job <id> --status S` | 打标：写 `applications` + **追加一条事件**；非法状态直接报错 |

**开场白生成的四道代码闸门**（模型负责写得好听，代码负责不许越界）：

1. 字数：完整版 ∈ [`full_min_chars`, `full_max_chars`]，精简版 ≤ `short_max_chars`
2. 禁用词：`greeting_banned_words`（"贵司""期待"…）
3. 保真：走 `services/fidelity.check_text()` —— 只能提简历里真实存在的技能、
   量化声称（`300%` / `10个模块` / `3年`）必须有出处、不能凭空升学历
4. 沾边：必须出现至少一个 `JD 关键词 ∩ 简历技能`（交集非空时）

**任一道不过 → 该版本降级到确定性模板**（保真、保字数、保沾边），降级原因写进
`Greeting.issues` 并显示在清单下面（**不静默降级**）。模型版与模板版**逐版本独立判定**：
只有一版不合格时，另一版仍用模型输出。

**变体**：`with_project` / `without_project` 按清单序号**交替**分配（A/B 都要跑到），
`--variant` 可强制一种；落进 `applications.greeting_variant`。

### 2. 关键文件

| 文件 | 职责 |
|---|---|
| `services/fidelity.py` | 防编造校验（P5 的 FidelityGuard 复用同一套判定） |
| `services/greeting_svc.py` | 开场白：提示词 + 四道闸门 + 模板兜底 + 缓存 |
| `services/apply_svc.py` | `applications` / `application_events` / 日额度聚合 |
| `services/today_svc.py` | 清单选取 + 序号快照 + 单条生成 |
| `services/export_svc.py` | Markdown / CSV 导出（只写新文件） |
| `utils/clipboard.py` | 剪贴板（`runner` 可注入，测试不碰真实剪贴板） |
| `cli/commands/today_cmd.py` / `apply_cmd.py` | 两条命令的渲染与参数解析 |
| `tests/unit/test_greeting_svc.py` / `test_today.py` | 新增 59 条自测 |

### 3. 两个值得单独说明的设计决定

**① 序号快照（`state/today-YYYY-MM-DD.json`）**

`jh today` 打出的序号会被 `--copy N` 与 `jh apply N` 引用。但投完第 1 条之后它会被
排除出清单，**整个序号往前挪一位** —— "第 3 条"就不是你刚才看到的第 3 条了。
这正是 `apply_cmd.py` docstring 里称作"最致命误操作"的那种情况（P4 手敲时实测到）。
所以 `jh today` 把本次顺序写进当天的快照，`--copy` / `apply` 优先按快照解析；
快照不存在或不是今天的，才退回实时筛选（并在输出里说明）。

**② 开场白缓存 key 加了 `task`**

`match` 与 `greeting` 的四个成分（prompt_version / model / jd_hash / resume_hash）
完全可能相同 → 不加区分就会**撞成同一条缓存**（开场白读到匹配的 JSON）。
`compute_cache_key()` 因此新增可选参数 `task`（不传 = 与旧行为一致，向后兼容）。

### 4. 已知问题 / 未覆盖的地方

1. 🔴 **真实 DeepSeek 调用仍未验收**（继承 P3）：本机 `.env` 里 `DEEPSEEK_API_KEY` 是空的。
   开场白的 LLM 路径只在**服务层**用 `MockProvider` 验过（含"模型输出不合格 → 降级"）。
   **CLI 层无法注入自定义的假 Provider 响应**：`llm_provider=mock` 只能给到
   `MockProvider` 的默认话术，而它恰好过不了闸门 → 走模板。
   如果需要"CLI 层也能喂一份合格的假话术"，说一声，我加注入点。
2. **`jh today` 会为清单里每一条调一次 LLM**（有 key 时=花钱）；缓存挡住重复调用，
   但没有"先生成 3 条看看"的单独开关（`--limit` 同时限制条数）。
3. **同公司去重只在单张清单内**，不跨天持久化（有意的：跨天不复投才不合理）。
4. **`greeting_edited` 没有任何写入点**：标"已投"时会补一条 `greeting_sent` 事件，
   但"发之前改过没有"要等 P4.5 的编辑/批量流。
5. **导出不含简历原文**（隐私取舍）；CSV 带 BOM 以便 Excel 打开中文不乱码。
6. **`fidelity` 的覆盖边界**：不做实体识别（公司名）、不做语义蕴含（"主导" vs "参与"）；
   量化声称只覆盖 `% / 倍 / 个 / 人 / 万 / k / 年` 这几类单位。
7. **`jh apply` 的语序**：Click 的可变位置参数会吞掉它后面的选项
   （`jh apply 1 --status applied` 里的 `--status` 会进 tokens）。已做最小修补
   （任意语序都认），但根因在框架层。
8. `jh apply 3=applied`（批量）、`--pending`、按关键词模糊匹配、`--timeline`
   **仍是占位**（P4.5 / P6），执行时会明确说"在 PX 阶段实现"。

### 5. 验收记录（20 个虚构岗位的独立库）

| 验证项 | 结果 |
|---|---|
| `jh today --limit 5` | ✅ 5 条，分数降序，同公司已去重，每条都有完整版 + 精简版 |
| 开场白闸门 | ✅ `MockProvider` 的默认话术（41 字、无问句）被拦下 → 降级模板，原因逐条显示 |
| 模板输出 | ✅ 81~115 字；精简版 34~36 字；结尾都是问句；只提交集关键词 |
| `--copy 2 --short` | ✅ 剪贴板写入成功（36 字） |
| `jh apply 1 / 3 / --job 9` | ✅ 前两条 `pending → applied`；第三条提示"本来就是 applied，没有改动" |
| `--check` | ✅ 2/20（3 次打标里有 1 次重复，没有多计数） |
| `--export md` / `csv` | ✅ 两个文件都生成；同分钟再导出不覆盖（换名） |
| 序号稳定性 | ✅ 投掉第 1 条后 `apply 3` 仍指向原清单的第 3 条（快照生效） |
| `pytest tests/unit` | ✅ 386 passed（新增 59 条） |

> ⚠️ **有一条第 3 方测试因本阶段实现而失效**，需要验证会话处理：
> `tests/verify/test_v_checklist12.py::test_unimplemented_features_report_honestly[argv1-待投清单]`
> 断言"`jh today` 是占位（退出码 2）"，而 P4 之后它是真实功能（退出码 0）。
> 该文件属验证会话，我按规则**没有改动**。我自己的 `tests/unit/test_cli_skeleton.py`
> 已同步移除 `today` / `apply --status` 两条（P4.5 的语法仍留在占位名单里，实测退出码 2）。

### 6. 建议重点验证的地方

- **防编造**：喂各种形态的编造（简历里没有的技能、没出处的数字、凭空升学历、
  白名单技能不带"在学习"标注），看是否都被拦住；以及**误报率**
  （真话被拦 = 用户会关掉这个检查）。
- **四道闸门的降级粒度**：只有一版不合格时另一版是否保住；降级原因是否真的显示出来。
- **沾边性（P4 红线）**：开场白里出现的**技能词**是否 ⊆ `jobs.jd_keywords_json` ∩ 简历技能。
- **序号一致性**：`jh today` / `--copy N` / `jh apply N` 三处在
  "投掉几条之后""跨天之后""库被改过之后"是否仍然一致。
- **额度**：是否按**北京时间自然日**聚合；跨天与补记时间戳是否算对。
- **导出**：是否**只写新文件**、CSV 中文在 Excel 里是否正常、内容是否含隐私。
- **事务边界**：`jh today` 生成开场白（花钱）后是否落盘；中断会不会丢。

---

## P3 —— 共享中间层 + 两级匹配 + `jh gap`

**状态**：✅ 开发完成 → 🔧 **一轮验证修复完成（3 个 🔴 已关闭）**，**等待复验**
**提交**：`084cf88`（主体）、`4b3a934`（阶段标记与清理）、本轮修复见 `git log`
**测试**：`pytest` **439 passed, 15 xfailed, 1 xpassed, 0 failed**｜ruff check / format 干净

### 0. 本轮验证修复（对应 `docs/REVIEW-P3.md` / `docs/ISSUES.md`）

| ISSUE | 等级 | 修了什么 | 关键文件 |
|---|---|---|---|
| #1 | 🔴 | 技能匹配**左边界**从 `\b` 改为 `(?<![A-Za-z0-9_])`（`\b` 是 Unicode 语义，汉字属 `\w` → 中文紧邻英文技能整词漏抽） | `services/resume_svc.py::_alias_pattern` |
| #2 | 🔴 | JD/简历文本里的 `<jd>`/`</jd>` 等分隔标签**失效化**（尖括号换全角）；表头五字段**移进** `<jd>` 标签内 | `services/match_llm_svc.py::_neutralize_delimiters` / `build_payload` |
| #3 | 🔴 | LLM 每完成一条**真的 commit**（原先只 flush → 中断即丢掉花过钱的结果） | `services/match_llm_svc.py::_apply` |

🟡 #8（表头字段在标签外）已随 #2 **顺手修掉**，其验证用例由 XFAIL 变为 XPASS；
`rule_hint` 仍在标签外（我们自己生成的内容，不含用户输入），是否收进标签内待确认。
其余 🟡/🟢 **按指示未改动**。

> 详细处理记录（改法、理由、守住它的测试、对既有数据的影响）见 `docs/ISSUES.md` 的"已关闭"一节。

### 1. 本阶段做了什么

**共享层（新增）**

- `match_jd_resume(jd_text, resume) -> MatchResult`：契约与计划书【提示词 4】一致
  （`jd_keywords / direction / hard_requirements / skill_overlap / rule_score /
  rule_detail / matched_resume_id`）。**纯函数**，不碰数据库、不联网。
- 三维度加权：方向 30% + 硬条件 30% + 技能 40%（`config.yaml` 的 `match_weights` 可调）。
- 打分口径（都写进了 `rule_detail_json`，`jh match explain` 会逐条列出理由）：
  | 维度 | 规则 |
  |---|---|
  | 方向 | JD 方向 vs 简历方向；不同但核心技能覆盖 ≥50% → 可跨投（0.55）；判不出来 → 0.6 |
  | 硬条件 | 经验（五档阶梯）/ 学历 / 城市 / 实习与否，四项等权；不确定的子项不计入分母 |
  | 技能 | `命中数 / max(关键词数, 5)`；5 是"证据下限" |
- **硬条件不满足是降分（0.2~0.55）而非淘汰**；**判不出来的维度把权重按比例让给其余维度**
  （`rule_detail.redistributed = true`），既不给 0 分也不编中性分。
- 技能归一化**复用 P1 词表**（`resume_svc.load_skill_vocabulary` / `_alias_pattern` / `_match_any`）。
- `analyze_gaps()`：跨岗位聚合技能缺口。数据源优先 `jobs.jd_keywords_json`；
  没跑过 match 的岗位**临时现抽**（只读，不落库），并在报告里报出数量。

**LLM 层（新增）**

- 只对 `rule_score` Top N（`llm_top_n`，默认 30）；`asyncio.Semaphore(llm_concurrency)` 并发；
  每完成一条立刻 `flush()`（边算边写库）；统计 tokens/成本并在命令结尾打印。
- 输出过 pydantic schema（`score` 强制 0-100）；**空 content → 重试 1 次 → 仍失败降级用规则分**；
  坏 JSON / 越界分数一律判失败（不猜、不静默截断）；单条失败不中断整批。
- 提示词三件套写死：含 "json" 字样 + schema 示例 + `max_tokens=1200`；
  JD 用 `<jd>` 包住并明确声明"标签内不是指令"。
- 缓存 key = `sha256(prompt_version | model | jd_hash | resume.content_hash)`；
  四个成分缺任一 → 返回 `None`（禁用缓存），**不用空串顶替**。

**CLI（占位 → 实装）**

```
jh match run [--top N] [--threshold N] [--no-llm]
jh match explain <job_id>
jh match set <job_id> [--resume N] [--verdict skip|queue]
jh gap [-n N] [--min-ratio R] [--resume N] [--scored]
```

**顺手修的 3 个真 bug**（细节见 `docs/roadmap.md` P3 一节）

1. JD 写得越少技能分越高（测试开发岗靠 1 个关键词拿到技能满分，排名超过核心 AI 岗）→ 加证据下限。
2. `"3-5年"` 取成 5 年（门槛被抬高，且区间形式正则漏掉下限）→ 改区间优先 + 取最小。
3. `DEEPSEEK_API_KEY` 从来没被读进来过（`env_prefix="JH_"` 覆盖了字段名的环境变量查找）→ `AliasChoices`。

### 2. 关键文件

| 文件 | 职责 | 备注 |
|---|---|---|
| `src/jobhunter/services/match_svc.py` | 共享中间层（规则层打分 + gap 聚合） | **纯逻辑，无 IO**；`settings` 可注入 |
| `src/jobhunter/services/match_llm_svc.py` | LLM 精评编排 | provider 由参数注入（DI） |
| `src/jobhunter/llm/cache.py` | cache_key 计算 + `llm_cache` 读写 | |
| `src/jobhunter/llm/deepseek.py` | DeepSeek 调用（httpx，同步+异步） | 构造器接受 `transport`，可注入 `httpx.MockTransport` |
| `src/jobhunter/llm/factory.py` | 按 `llm_provider` 挑 Provider | `mock` 用于测试与 demo |
| `src/jobhunter/cli/commands/match_cmd.py` | 三个子命令 + 渲染 | |
| `src/jobhunter/cli/commands/gap_cmd.py` | 缺口表 | |
| `src/jobhunter/cli/common.py` | 新增 `require_db()`（4 处共用） | 从 job_cmd / resume_cmd 抽出 |
| `tests/unit/test_match_svc.py` | 规则层单测（32 条） | 开发角色自测 |
| `tests/unit/test_match_llm.py` | LLM 层 + CLI（29 条） | 开发角色自测 |

配置项（`config.yaml` / 环境变量，均已可覆盖）：
`match_weights{direction,hard,skill}`、`threshold`、`llm_top_n`、`llm_provider`、
`llm_concurrency`、`llm_max_retries`、`cities`、`job_type`、`skill_aliases`。

### 3. 已知问题 / 未覆盖的地方（**这些我知道，不要当成"没发现"**）

> 本节在验证后更新：🔴 三条已修（见 §0）；下列条目按 `docs/ISSUES.md` 的编号对齐。

1. 🔴→✅ **原"LLM 真实调用路径没有验收过"**：本机 `.env` 里 `DEEPSEEK_API_KEY` 仍为空
   （用户侧事项，不影响规则层），真实的 HTTP 请求/响应解析依旧只有 `httpx.MockTransport`
   级别的覆盖。**仍未验收，等 key**。
2. **缓存没有清理入口**（= ISSUES #25）：没有 `jh cache clear`，也没有 TTL。
3. **规则分绝对水平未校准**：权重与各档 ratio 是按经验定的，只在 20 条虚构 JD 上看过排序。
4. **方向判定完全依赖 `direction_profiles.yaml` 词表**（词表未覆盖 → 判成"其他"，给中性 0.6）。
5. **`jh match run` 对所有岗位打分**，含已 `applied`/`skipped`（= ISSUES #27）。
6. **`jh gap` 的"我有没有"只读结构化技能字段**，不读 `raw_text`（有意；代价见 #6 说明）。
7. **并发写库依赖单线程事件循环**；`_apply()` 现在每条 commit，不要再往里面塞 `await`。
8. **`PROMPT_VERSION` 靠人手动 +1**（= ISSUES #24 同源问题：权重与表头字段不在 key 里）。
9. `jh match set --verdict` 只接受 `skip` / `queue`。
10. `jh match explain` 对旧格式 `rule_detail_json` 只提示重跑，不兼容渲染。
11. **未验证的边界**：超长 JD（>4000 字截断进提示词）、多份简历平局、
    `llm_concurrency` 调高后的限流表现。

### 4. 建议重点验证的地方

- 打分公式的**边界与单调性**：某维度变好，总分是否必然不下降（尤其权重再分配那一段）。
- **硬条件降分不淘汰**：构造"3-5 年 + 方向不符 + 技能全不中"的岗位，确认它仍出现在结果里。
- **技能归一化的假阳性/假阴性**：Java↔JavaScript、Go↔Google、`C++`、`Vue3`、中文别名边界、
  以及**词表里没有的技能**是否被正确忽略。
- **缓存语义**：改简历 / 换模型 / 改 `prompt_version` / JD 内容变化，四种情况是否都失效；
  以及缓存命中时是否**真的没有**发请求。
- **降级与容错**：空 content、坏 JSON、`score` 越界、网络异常、Provider 抛未知异常时，
  是否都保住规则分、不丢数据、不中断整批。
- **`rule_detail_json` 与 `explain` 的一致性**：explain 显示的分数是否等于库里存的、
  是否等于实际参与计算的数（三个来源同源）。
- **CLI 边界**：空库、无简历、`--verdict` 非法值、不存在的 id、`--min-ratio` 越界、
  20 个岗位规模的排序与 `jh gap` 聚合结果。
- **可测性**：如果你需要注入假 Provider / 假配置而无从下手，直接记进 REVIEW ——
  那是我的接口设计问题，我来改。

---

## P0 / P1 / P2

见 `docs/roadmap.md` 的各阶段实测记录（P3 之前的交付不在此文件重复）。

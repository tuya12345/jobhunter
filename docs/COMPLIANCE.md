# COMPLIANCE —— 合规与隐私自检（P9）

> 这份文档回答两个问题：**这个工具会不会替我干违法/违规的事**，以及
> **它会不会把我的隐私泄露出去**。
>
> 每一条都附**可复现的证据**（命令 + 结果），不是"我认为没问题"。
> 生成日期：2026-09-19（P9 收尾）｜ 复跑方式见每节末尾。
> ★ 2026-09-20 增补：**P7 消费端落地**后的本机服务边界（§0 结论表新增"本机服务边界"一行、
> §1 新增线索页调用点、§2 新增"标记投递"、§5.3 改成实测的 `--dry-run`、
> **新增 §5.6（为什么不做鉴权）**、§8 边界改写）。

---

## 0. 一句话结论

| 维度 | 结论 |
|---|---|
| 自动化边界 | ✅ 代码里**没有任何**"向招聘平台写入"的调用（投递/点赞/私信/关注/自动翻页） |
| 频率克制 | ✅ 抓取路径带**随机延时（2–5 秒，可配）+ 出错即停**，且默认路径只抓"用户给的单个链接"（**无自动翻页**，见 §5.2） |
| 本机服务边界 | ✅ `jh leads serve` 起的线索页是**本机**服务：默认只绑 `127.0.0.1`（**不是 `0.0.0.0`**）、**不做鉴权**（前提正是"只监听回环"，见 §5.6）；页面只读 + 一个写动作，而且那个写动作**只能由人点击**触发 |
| 隐私（工作区） | ✅ 真实姓名/手机/邮箱/学校/项目名**都不在仓库里**（本轮清掉了最后 2 处真实项目名） |
| 隐私（git 历史） | ⚠️ **真实姓名仍在历史里**（22 个提交 / 2 个文件 + 2 条提交信息）→ 见 §4，**需要发布前决策** |
| 依赖与迁移 | ✅ `uv.lock` 已提交；`upgrade→downgrade→upgrade` 三连通过；PRAGMA（WAL/外键/busy_timeout）已开 |
| 导出安全 | ✅ 简历/清单/周报/统计**只写新文件**；唯一的例外是 `.ics`（幂等快照，见 §5.4） |

---

## 1. 所有"会对外部世界动手"的调用点（逐个判定）

扫描命令（对 `src/` 做模式匹配，人工逐条看上下文）：

```powershell
# ★ 必须带 --untracked：P7 新增的 src/jobhunter/web/ 等文件在提交前属于"未跟踪"，
#   裸 git grep 只搜已跟踪文件 —— 会正好漏掉这次新加的"对外服务"那一层。
#   （本次更新文档时实测：不带它时 src/jobhunter/web/app.py 完全不出现在结果里。）
git grep -n --untracked -E "httpx\.|requests\.|playwright|page\.(click|fill|press|goto|type)|uiautomator2|\badb\b|subprocess\.|fastapi|uvicorn" -- src/
```

| 调用点 | 类型 | 判定 |
|---|---|---|
| `llm/deepseek.py`（`httpx.Client` / `AsyncClient`） | 向 **LLM 服务商**发请求 | ✅ 合规：这是用户自己的 API Key、用户自己付钱；只发提示词，不发投递动作 |
| `adapters/manual.py`（`sync_playwright` + `page.goto`） | 打开**用户自己粘贴的岗位链接**并读取正文 | ✅ 合规：**只读**。`goto` 是唯一的页面动作，没有任何 `click`/`fill`/`press`。★ 撞到**登录墙 / 安全验证页**时**不去绕过**：识别出来（页面有登录特征词且无岗位结构）→ 标 `failed`、清空正文、一条都不入库，并告诉用户走"浏览器另存为网页 + `--html --url --platform`"或 `--manual`（ISSUES #62） |
| `adapters/cdp.py`（`connect_over_cdp`）+ `jh job add --cdp` | 连到**用户自己的浏览器实例**（专用 profile）去读页面（ISSUES #65） | ✅ 合规：用的是**用户自己的浏览器、自己的会话**，**不伪造任何凭据**（没有假 Cookie / Referer / 指纹，没有登录自动化），**仍然只抓用户手动给的那一个链接**；专用 profile 落在 `state/cdp-profile`（.gitignore 覆盖，里面会有登录态）。两条守卫：① `tests/unit/test_cdp.py` 用假 Playwright 断言 `click`/`fill`/`press`/`type` **从未被调用**；② AST 守卫扫 `src/` 拦下"操作页面"的调用。详见 §5.7 |
| `utils/clipboard.py`（`subprocess.run(["clip"])`） | 把文本写入**本机剪贴板** | ✅ 合规：本机操作，不涉及任何外部服务 |
| `cli/commands/leads_cmd.py`（`uvicorn.run(...)`）+ `web/app.py`（FastAPI 路由） | `jh leads serve` 起一个**本机 HTTP 服务**（浏览器里看线索 / 标记投递） | ✅ 合规：**默认只绑 `127.0.0.1`**（`uvicorn.run(create_app(), host="127.0.0.1", port=8787)`，不是 `0.0.0.0`）；页面**只读 + 一个写动作**，且该写动作由**人工点击**触发、走 `apply_svc.set_status`（与 CLI **同一个写入口**）；**不做自动投递、不做批量**、不增删线索。为什么不做鉴权见 §5.6 |
| `adapters/boss.py`（自动搜索） | —— | ❌ **未实现**（P2 的 spike 结论：自动翻页会撞风控，主路径改成"人给链接"） |
| `adapters/app/`（模拟器侧） | —— | ➡️ **已移出本仓库**：采集端（含模拟器）独立部署在 `projects/leadhunter`（命令 `lh`）。本仓库 `src/` 里**没有** `import uiautomator2`，也没有 `jh leads add / probe / capture / export` 这些命令 —— 所以"模拟器侧的只读承诺"要在那个仓库里单独自检 |

**没有任何调用会**：提交投递、发送消息、点赞/关注、绕过验证码、伪造签名、批量遍历岗位列表。
**也不会绕过登录**：需要登录的岗位页（BOSS 详情页）**如实报失败并拒绝入库**，
不伪造 Cookie / Referer / 指纹，不把登录页正文当成 JD（ISSUES #62 —— 那是"不报错却存垃圾"，
比抓不到糟得多）。
**抓取可以复用用户自己已登录的浏览器实例（CDP，`jh job add --cdp`）**：
用的是**用户自己的会话**，不伪造任何凭据；**仍然只抓用户手动给的那一个链接**，
没有翻页、没有批量（专用 profile 在 `state/cdp-profile`，不进仓库）。
线索页也一样：它**唯一的写动作**是"人点一下 → 往**本机库**里记一笔投递"，不向任何平台发请求，
也不存在"批量"这个入口。

> 这条边界还有一道**机器守卫**：`tests/verify/test_v_checklist12.py` 里有一条 AST 扫描用例
> （`test_no_auto_apply_or_platform_write_calls_in_source`），把 `client.post` / `page.click` /
> `locator.fill` / `d.swipe` 这类写操作方法列进禁用名单，出现即测试失败。
> 我在 P4.5 曾因内部函数名叫 `check` 撞上这份名单而改名 —— **改的是我的代码，不是那条守卫**。
>
> ★ P7 加了页面之后复核过一遍（2026-09-20）：两条守卫用例**仍然通过**。原因是判定口径分得清
> 方向 —— `web/app.py` 里的 `@app.post("/api/leads/{lead_id}/apply")` 是**入站**路由，
> 而守卫认的是**出站** HTTP 客户端的接收者名（`_HTTP_CLIENT_RECEIVERS`：`client` / `http` /
> `httpx` / `requests` / `session_http` / `aiohttp` / `resp` / `request`），`app` 不在其中；
> 真正的写库调用在 `services/` 里，页面只是调用它。复跑：
> `uv run pytest tests/verify/test_v_checklist12.py -q -k "write_calls_in_source or network_write_endpoints"` → `2 passed`。

---

## 2. 必须人工的动作（确认代码没有自动执行）

| 动作 | 代码里的状态 | 证据 |
|---|---|---|
| 点"投递"按钮 | 工具**只出清单 + 话术**，投递由人完成 | `jh today` 输出后需人工复制话术；`README` 的合规声明 |
| 发送开场白/追问话术 | **只生成，不发送**；剪贴板是唯一出口 | `greeting_svc` / `followup_svc` 没有网络发送路径；`jh followup` 结尾打印"本工具不会替你发送任何消息" |
| 发送简历 | 导出**新文件**，由人上传 | `tailor_svc.export_version` 只写文件 |
| 打标"已投/已读/已回复" | 必须人手敲 `jh apply` | `apply_svc.set_status` 的唯一调用方是 CLI 命令 |
| **线索页上的"标记投递"** | 必须**人在页面上点一下** —— 没有自动、没有批量 | 页面唯一的写接口是 `POST /api/leads/{id}/apply`，只由前端点击触发（`web/app.py` 里没有定时器/循环/批量入口）；它调 `leads_svc.link_lead_to_application` → `apply_svc.set_status`，**与 `jh apply` 同一个写入口**。页面**不向任何招聘平台发请求**：这一步只是往本机库记一笔"我投了" |
| 修改状态机（拒绝/接受 Offer） | 必须人手敲 | `OFFER_STATUSES` 只被 CLI 写入 |

> 换句话说：**工具的终点是"把该发的东西写好、把该记的事记下"，中间那一下永远由人点。**

---

## 3. 隐私自检（工作区）

复跑：`python "D:\deep seek harness\try anything\p9-compliance-scan.py"`（脚本不在仓库里，避免"扫描器自己带 PII"）

### 3.1 `.gitignore` 覆盖（计划书点名 9 条）

`✅ 全部命中`：`.env`、`*.db`、**`*.db-wal`**、**`*.db-shm`**、`data/`、`output/`、`state/`、`*.docx`、`*.pdf`。

> `*.db-wal` / `*.db-shm` 是最容易漏的一条：SQLite 开 WAL 之后，**真实数据会先落在 `.db-wal`**，
> 只忽略 `*.db` 等于把真实投递记录提交上去。`.gitignore` 里已写明这条理由。

### 3.2 敏感路径有没有被跟踪

`✅ 无`：`git ls-files` 里没有 `data/`、`output/`、`state/`、`*.db*`、`*.docx`、`.env`。

### 3.3 全仓库扫描（密钥 / 真实手机号 / 邮箱 / 身份证）

对 **147 个被跟踪文件**扫描，25 处命中，**逐条判定如下（全部是"看起来像"而非真实数据）**：

| 位置 | 命中类型 | 判定 |
|---|---|---|
| `src/jobhunter/utils/logging.py` | 手机号/邮箱/身份证**形态** | ✅ 是**脱敏过滤器自己的测试样例**（占位段，如 `138xxxx0078`） |
| `tests/unit/test_logging_redaction.py`、`test_docx_reader.py`、`test_resume_svc.py`、`tests/verify/test_v_normal_path.py` | 同上 | ✅ 同一批占位样例（脱敏功能的输入） |
| `src/jobhunter/models/company.py`、`src/jobhunter/services/resume_svc.py` | `xx@example.com` / `a@b.com` | ✅ 文档注释里的示例地址 |
| `uv.lock` | 6 处"像手机号" | ✅ **误报**：包哈希/版本号里的连续数字串（`uv.lock` 由 uv 生成，无人工内容） |

**结论**：工作区里**没有**真实姓名、真实手机号、真实邮箱、真实身份证、API Key。

### 3.4 本轮清掉的两处（真实项目名）

P9 复查时用"从本机 gitignored 来源取真实标识、再回扫仓库"的办法（脚本见 §7），
发现 **2 个来自真实简历的项目名**曾出现在 3 个文件里（P1 时期留下）：

| 文件 | 形态 | 处理 |
|---|---|---|
| `tests/unit/test_match_svc.py` | 夹具里的项目名 | ✅ 换成完全虚构的占位名 |
| `tests/unit/test_resume_svc.py` | docstring 里**逐字引用**了真实项目标题行 | ✅ 改成"同形状的虚构示例 + 说明真实标题的形态"，不再引用原文 |
| `src/jobhunter/services/resume_svc.py` | 注释里举的"中点分隔"例子 | ✅ 换成虚构写法 |

> ⚠️ 这三处在 **git 历史里仍然存在**（见 §4）。工作区现在是干净的。
> 这正是"隐私问题一旦提交就跟着仓库走"的实例 —— 所以规则是**一开始就别让它进来**。

### 3.5 日志脱敏

- `utils/logging.py` 挂了脱敏 Filter（手机号/邮箱/身份证 → 打码），有单测（`test_logging_redaction.py`）。
- 日志**不打**简历正文；`STATUS.md`/`ISSUES.md` 里也只记"哪里有过、什么类型"，从不复述内容
  （P4 曾因为"把泄露的原文当证据贴进 ISSUES.md" 被登记为 🔴 —— 那次教训写进了台账）。

---

## 4. ⚠️ git 历史里的真实姓名（**发布前必须决策**）

复跑：`python "D:\deep seek harness\try anything\p9-pii-history-check.py"`
（脚本从**本机 gitignored 来源**取真实标识，再扫描历史；输出只打计数与打码形式，**不复述内容**）

| 项 | 历史命中 | 涉及提交 | 涉及文件 |
|---|---|---|---|
| 真实姓名 | 107 行 | **22 个提交** | `docs/ISSUES.md`、`src/jobhunter/services/resume_svc.py` |
| 真实项目名 A | 26 行 | 26 个提交 | `tests/unit/test_resume_svc.py` |
| 真实项目名 B | 16 行 | 16 个提交 | `tests/unit/test_match_svc.py` |
| 提交信息里的姓名/项目名 | 2 + 1 条 | —— | commit message |
| 工作区（当前文件） | **0** | —— | 已清净 |

**两个选项（需要人决定，代码层面无法替用户选）：**

```powershell
# 选项 A：重写历史（会改变所有 commit 的 SHA；私密仓库 + 未公开，现在做代价最小）
uv tool install git-filter-repo        # 或 pip install git-filter-repo
git filter-repo --replace-text replacements.txt   # 每行 "<真实串>==><占位>"
git push --force --all; git push --force --tags

# 选项 B：接受痕迹（仓库公开后任何人都能 git log -S 找到）
#   适合"反正简历本来就是给 HR 看的"这种心态；但姓名 + 学校/项目名可组合定位到本人
```

**我的建议**：**公开之前做选项 A**（现在是私密仓库，重写历史没有协作者受影响；
一旦公开，重写历史就等于对所有人强制 rebase）。但这是用户的个人数据，**必须由用户决定**。

---

## 5. 频率控制与写文件检查

### 5.1 随机延时

- 配置：`config.yaml` 的 `rate_limit.min_delay_seconds / max_delay_seconds`（默认 2–5 秒）。
- 使用点：`adapters/manual.py`（抓职位详情前 `random.uniform(low, high)`）——
  `git grep rate_limit -- src/` 可见 `manual.py:186-187`。
- 主路径**默认只抓一个链接**（用户粘贴的那个），不做列表遍历。

### 5.2 翻页：**当前没有自动翻页路径**

★ 这一节以前写的是「翻页上限」：`rate_limit.max_pages`（默认 3，上限 50，配置校验强制），
并称"对未来可能的适配器生效"。P9 复查时（ISSUES #18）核实：**该配置项全仓零读取点**，
`adapters/base.py` 的 `search()` 只有签名、没有实现、也没有任何调用方。
也就是说，那份"控制"只存在于文档里 —— 而**合规文档声称一个不存在的控制，
比一个没人读的配置项严重得多**（它是"自评不可信"的起点）。

处理：删掉配置项与字段定义，本节改成如实描述：

- 当前的抓取路径是**用户手动给的单个链接**（`adapters/manual.py`），逐条打开详情页；
- 没有任何形式的列表遍历 / 自动翻页 / 批量抓取（`adapters/boss.py` 的自动搜索**未实现**，见 §1）；
- 所以"翻页上限"这个控制**不需要存在**：没有翻页，就没有要限的东西。
  将来若真的实现自动搜索，配置项、真实读取点、本节说明必须**同时**补齐。

### 5.3 `--dry-run`

`jh job add --dry-run`：只解析并打印，**不写库**（`job_cmd.py:120-125`）。
`jh leads import --dry-run`（P7 消费端）：**连一次 UPDATE 都不发** —— 它先在内存里把"要做什么"
算成一份计划（`ImportPlanRow` 列表），再决定是执行还是只报出来，
所以 dry-run 与真跑走的是**同一段判定逻辑**（不会"dry-run 说没事、真跑写坏了"）。
实测（2026-09-20，库指到全新临时目录）：

```text
> jh leads import docs/leads-snapshot-sample.json --dry-run
dry-run：（dry-run：什么都没写库）共 4 条 → 新增 4 条、更新 0 条
新增    星澜科技  —
…（4 条）
  库里没有任何改动（连一次 UPDATE 都没发）。去掉 --dry-run 才会真的导入。
```

真跑两次则是「第一次 新增 4 条 / 第二次 新增 0 条、更新 4 条（其中 0 条内容有变化）」——
**库里始终是 4 行**（幂等，验收会真的去数行数）。

### 5.4 导出只写新文件

| 导出 | 语义 | 证据 |
|---|---|---|
| 定制简历 docx / md | 只写新文件 + 校验原简历 sha256 未变 | `tailor_svc.export_version`（有测试） |
| 今日清单 md / csv | 只写新文件（`unique_path`） | `export_svc.export_markdown/csv` |
| 周报 md | 只写新文件 | `report_svc.export_report` |
| 统计 md / csv | 只写新文件 | `stats_cmd._export` |
| 日历 `.ics` | ★ **覆盖同一个文件**（唯一例外） | `cal_svc.export_ics` |

`.ics` 为什么是例外：它是**幂等快照**。若按"只写新文件"生成 `jobhunter-2.ics`、`jobhunter-3.ics`，
用户每次导入日历都会多出一份**重复事件** —— 那是更坏的结果。取舍写在 `cal_svc.export_ics` 的 docstring 里。

**独立验证（P9 复查时补做的，ISSUES #52）**：除了仓库内的文本级断言（CRLF / 75 字节折行 /
转义 / UID 稳定），我还用**第三方库** `icalendar` 解析了真实的导出产物：

```powershell
uv run --with icalendar python <scratch>/check_ics.py   # 脚本在仓库外
```

结果：`PRODID` / `VERSION` 正确、**3 个 VEVENT 全部读出**、UID 唯一、
`DTSTART/DTEND` 解析成 UTC aware datetime、会议链接（含 `?a=1&b=2`）与
**含分号/逗号的公司名**都能原样读回（说明转义是对的）、无 BOM、33 行 CRLF。
⚠️ 但"能不能导入手机日历/Outlook"这一步**仍然需要人来做** —— 第三方解析通过
只证明它符合 RFC，不等于某个具体日历应用会接受它。

### 5.5 浏览器 UA / locale：为什么固定成"中文 + 桌面版 Chrome"

P9 复查时把这一条单独记下来（ISSUES #19），因为**"设了一个看起来很正常的 UA"很容易被读成伪装**，
所以把目的与边界写清楚：

| 项 | 值 | 目的 |
|---|---|---|
| `locale` | `zh-CN` | 让站点返回**中文**页面。默认 `en-US` 会拿到英文页面，公司名/岗位名变成英文，关键词抽取与匹配全部失真 |
| `user_agent` | 固定的桌面版 Chrome UA（`adapters/manual.py`） | 让站点返回**桌面版页面**。移动版是另一套 DOM，本项目的解析规则（最长文本块 + `data-test` 属性）在移动版上不成立 |
| `viewport` | 1440×900 | 同上：桌面版布局 |

**它不是用来规避风控的**，这一点有三条可核实的事实：

1. UA 在代码里是**字面量常量**，整个进程只发这一个，**不随请求轮换、不按站点切换**；
2. 没有伪造 `Referer` / `Cookie` / 指纹，没有代理池，没有验证码绕过；
3. 抓取路径仍然只是"**用户手动粘贴的单个链接**"（§5.1），没有列表遍历、没有自动翻页（§5.2），
   且每次抓取前有 2–5 秒随机延时。

写出来的理由：**不写，读者只能猜**——而"我们设了 UA"和"我们为什么设 UA"是两件事，
后者才是取舍。

### 5.6 线索页（`jh leads serve`）：为什么不做鉴权

`jh leads serve` 会在本机起一个 HTTP 服务（默认 `http://127.0.0.1:8787`），页面上是你的线索：
公司名、**原始那句话**、来源平台、备注（备注里常常有 HR 的邮箱或微信）。它**没有登录、
没有口令、没有任何鉴权** —— 这是**刻意**的，而这个做法只在**一个前提下**成立：
**它只监听回环地址（`127.0.0.1`）**。

| 问题 | 答案 |
|---|---|
| 谁能访问到它？ | 只有**这台机器**上的人/进程。默认值就是回环：`DEFAULT_HOST = "127.0.0.1"`（`cli/commands/leads_cmd.py`），`uvicorn.run(create_app(), host=host, port=port)`。实测（2026-09-20）：服务起来后 `Get-NetTCPConnection -LocalPort 8787 -State Listen` 显示 `LocalAddress = 127.0.0.1`，**不是 `0.0.0.0`** |
| 为什么不做鉴权？ | 三条，缺一条这个决定就不成立：① **单机个人工具** —— 只有你一个人用，没有"第二个用户"需要区分；② **数据不出本机** —— 页面读写的都是本机 SQLite，前端也不引用任何外部 CDN/接口，没有任何一条请求把你的线索发出去；③ 加鉴权要多维护一套会话/令牌代码，多一个你会忘记的密码，而它挡不住"能坐到你电脑前的人"。**所以"不做鉴权"不是"来不及做"，而是它的安全模型本来就是"回环地址即边界"**（P8 的设计文档也写明本地工具不做用户系统） |
| 它会不会替我干点什么？ | 不会。页面**只读 + 一个写动作**：人点"标记投递"才记一笔，**没有自动投递、没有批量**，也不能新增/删除线索（§1、§2） |
| 那风险在哪？ | 在你**主动把它开出去**的时候 —— 见下 |

**如果改成 `--host 0.0.0.0`，等于把这个页面给同一个局域网里的人看**：同一个 Wi-Fi
（宿舍 / 咖啡厅 / 公司网络）下任何人扫到这个端口，就能看到你的线索原文、备注里的联系方式，
还能替你点"标记投递"（往你的库里写记录）。**这一步没有任何技术护栏**：页面没有登录，
能连上的人就是"你"。文档不推荐这么做，也不会为它补一个"更宽松的默认值"。

真要给别人演示，按下面顺序选（越靠前越安全）：

1. **截图 / 录屏** —— 最省事，也不会多开一个口子；
2. **保持默认 `127.0.0.1`**，用 SSH 端口转发让对端经你这条加密通道看
   （`ssh -L 8787:127.0.0.1:8787 <你的机器>`）—— 服务本身**始终没有对外监听**；
3. 非要绑 `0.0.0.0`：至少放在**你自己的、带鉴权的反向代理**后面，并换掉默认端口。
   **风险由你承担** —— 页面上没有登录，任何能连上的人都能读能写。

> 顺带一句：验收里那条「实际起服务，确认监听的**不是** `0.0.0.0`」验的正是这件事 ——
> 不是"我们记得写了 `127.0.0.1`"，而是**这个默认值真的没有被改坏**。

### 5.7 本地 CDP（`jh job add --cdp`）：用它自己的浏览器，不伪造任何凭据

**为什么会有这条路**：默认抓取会**新起一个 headless 浏览器** —— 它没有登录态，
所以需要登录的站点（BOSS）只会给登录页（ISSUES #62 之后，工具会**如实报失败**，
但这仍然是"抓不到"）。`--cdp` 改成连到**用户自己的浏览器实例**去读页面。

| 问题 | 答案 |
|---|---|
| 这算不算"绕过登录"？ | **不算**。用的是**用户自己的浏览器、自己的会话** —— 就像他本人打开那个页面一样。没有伪造 Cookie / Referer / User-Agent / 指纹，没有验证码识别，**没有任何自动登录**（不填表单、不点按钮） |
| 那它做了什么？ | 只做三件事：`connect_over_cdp` 连上、`goto` 打开**用户手动给的那一个链接**、读取内容。源码里有 AST 守卫拦下 `click`/`fill`/`press`/`type`（§1 表格那两条），另有一条用假 Playwright 断言这些方法**从未被调用** |
| 会不会翻页/批量？ | 不会。`jh job add` 的选项里**没有**任何翻页/批量入口（有用例断言这一点），命令行里也不会出现任何目标 URL 列表 —— CDP 让翻页技术上更容易，所以这条红线更要守住 |
| 为什么起**专用**浏览器实例，而不是连我日常那个？ | 两个实测原因：① **Edge/Chrome 已经在运行时 `--remote-debugging-port` 完全无效**（新进程只是把标签页交给现有实例，端口根本不会起来）；② 专用实例不打扰用户日常那个浏览器。第一次用要在那个窗口里登录一次，之后一直可用 |
| 那个专用目录里有我的登录态，安全吗？ | 它在 **`state/cdp-profile`** 下：`state/` 已被 `.gitignore` 覆盖，**绝不会进仓库**（有用例守住这个位置）。它就是你本机的一个浏览器 profile，与你日常 profile 同等敏感 —— 别把它拷给别人、也别提交 |
| 它会不会替我关掉浏览器？ | **不会，而且抓完什么都不关**。连过去的是**你自己的**浏览器实例：我们只导航 + 读内容，页面留在那儿（停在刚抓完的那个链接上 —— 你能看见工具读了哪个页面），下次抓取**复用同一个标签页**。★ 实测踩过的坑：初版"抓完把页面关掉"看着干净，但那个专用窗口只有一个标签页，而 Edge/Chrome **关掉最后一个标签就整个退出** —— 用户刚登录好、跑一次抓取窗口就没了。所以现在连页面也不关（有用例断言 `close` 从未被调用） |

---

## 6. 依赖、迁移与工程底座

| 检查项 | 结果 |
|---|---|
| `uv.lock` 已提交 | ✅ `git ls-files uv.lock` 命中；CI 里 `uv lock --check` 守住"改了依赖没提交锁" |
| 迁移可逆 | ✅ `upgrade→downgrade→upgrade` 三连：11 表 → 0 表 → 11 表（`tests/unit/test_smoke.py`） |
| 当前版本 | `0006`（`0005` P7 消费端：`companies` 加 `http_status` / `checked_at` / `external_id` / `collected_at` / `name_norm`（唯一索引）、`applications` 加 `company_id`；`0006`：`applications.job_id` 改成**唯一索引**——一个岗位只允许一条投递记录）。实测 `jh init` 打印「迁移版本：0006」 |
| PRAGMA | ✅ `journal_mode=wal`、`foreign_keys=1`、`busy_timeout=5000`（每个连接都挂） |
| 时区 | 库里一律 naive UTC，显示转北京时间（`utils/time.py`） |
| 编码 | 启动即 `force_utf8_output()`；所有文件读写显式 `encoding`（ruff `PLW1514`） |
| CI | ubuntu + windows 双平台；`ruff check` / `ruff format --check` / `pytest`（默认排除 live/emulator） |

---

## 7. 复跑方式（把"结论"变成"可复现的证据"）

```powershell
# ① 工作区 + 历史 + 依赖 + 自动化面（一条命令出全部素材）
& "D:\deep seek harness\projects\jobhunter\.venv\Scripts\python.exe" -X utf8 `
  "D:\deep seek harness\try anything\p9-compliance-scan.py"

# ② 真实标识的历史痕迹（从本机 gitignored 来源取标识，输出只打码）
& "D:\deep seek harness\projects\jobhunter\.venv\Scripts\python.exe" -X utf8 `
  "D:\deep seek harness\try anything\p9-pii-history-check.py"

# ③ 合规守卫（验证会话的 AST 扫描 + 仓库单测）
cd "D:\deep seek harness\projects\jobhunter"; .\.venv\Scripts\python.exe -m pytest -q

# ④ 隐私扫描（ISSUES #54）：工作区里不许有真实身份标识
#    样本来源：环境变量 JH_PII_SAMPLES，或本机 state/pii-samples.txt（已 gitignore）
#    ★ 没配样本时用例会 **skip**（不是 pass）—— "没扫" 这件事必须看得见
$env:JH_PII_SAMPLES = "<真名>,<学校>,<打码手机号>"
.\.venv\Scripts\python.exe -m pytest tests/verify/test_v_p9.py::test_working_tree_has_no_real_identifiers -rs -v
```

> CI 里同样有这一步（`.github/workflows/ci.yml`），样本来自 **repository secret
> `JH_PII_SAMPLES`**（逗号分隔）—— secret 不进仓库，正好满足"检测器不能携带它要检测的秘密"。
> 公开仓库前请在 Settings → Secrets 里加上它；不加也不会红，但会在 CI 摘要里显示为 skipped。

> 两个脚本刻意**放在仓库外**：扫描器本身不该进版本库（它会读真实标识）。

---

## 8. 已知边界（诚实登记）

1. **真实姓名/项目名仍在 git 历史**（§4）—— 等用户决策。
2. **模拟器 / 采集侧不归本仓库**：P7 在 2026-09-19 拆成了采集端（独立部署
   `projects/leadhunter`，命令 `lh`）与消费端（本仓库）。本仓库只有 `jh leads import`
   与 `jh leads serve`，`src/` 里没有模拟器依赖、也没有 `jh leads add / probe / capture / export`
   这些命令 —— 所以"不做批量抓取 / 不绕风控 / 不做任何写操作"这三条对**采集端**的约束，
   要在那个仓库里单独自检（由那边的文档负责）。
   本仓库这一侧的边界已按 P7 的落地更新：§1（本机服务只绑回环 + 唯一写动作）、
   §2（标记投递必须人工点击）、§5.3（`--dry-run` 不写库）、§5.6（不做鉴权的前提）。
3. **`adapters/boss.py` 是空的**：计划书里的自动搜索在 P2 就降级成了 spike，
   spike 的 `--live` 我从未跑过（`spikes/boss_search_spike.py` 默认干跑）。
4. **`.ics` 覆盖同名文件**是唯一偏离"只写新文件"的地方，理由见 §5.4。
5. 本文档的扫描是**模式匹配**，不是数据分类器：它能抓住"手机号/邮箱/身份证/密钥形态"，
   抓不住"一段没有明显标识符、但能定位到人的经历描述"。后者的防线是**纪律**
   （真实简历的文本一律不进仓库），不是脚本。

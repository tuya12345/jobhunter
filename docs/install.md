# 安装与第一次运行

> 这份文档写给**第一次从 GitHub clone 下来、还没跑过一条命令**的人。
> 目标是：从零到 `jh today` 出一份待投清单，中途每一步都告诉你**成功时应该看到什么**。
>
> 本文里的输出都是**真实跑出来的**（Windows + Python 3.13.7 + uv 0.12.13），
> 不是"应该是这样"；只有长输出做了裁剪，**裁剪处一律用 `…` 或"（略）"标出**。
> 示例中的公司名、简历、链接全是虚构的。
>
> 配套阅读：[`docs/config.md`](config.md)（配置逐字段说明）、
> [`docs/architecture.md`](architecture.md)（分层与目录职责）、
> [`docs/data_model.md`](data_model.md)（11 张表）。

---

## 0. 前置条件

| 需要 | 版本 / 检查命令 | 说明 |
|---|---|---|
| Python | **≥ 3.13**：`py -3.13 -V`（Windows）/ `python3.13 -V` | `pyproject.toml` 里写死 `requires-python = ">=3.13"`（用到 PEP 695 语法与 `datetime.UTC`）。上界不封。 |
| uv | `uv --version` | **推荐**。`uv sync` 严格按仓库里的 `uv.lock` 建环境，可复现。 |
| Git | `git --version` | 只是用来 clone；下载 zip 解压也行。 |
| 网络 | 能访问 PyPI | 装 `[browser]` 时还要从 Playwright 的 CDN 下载 Chromium 内核。 |

各平台的差异（**先看这张表再动手**）：

| 事项 | Windows（PowerShell） | macOS / Linux |
|---|---|---|
| 列已装的 Python | `py -0` | `python3 -V`、`ls /usr/bin/python3*` |
| 装 uv | `py -m pip install uv` | `python3 -m pip install uv`（或见 [uv 官方安装页](https://docs.astral.sh/uv/getting-started/installation/)） |
| 激活虚拟环境（方式二） | `.\.venv\Scripts\Activate.ps1`（cmd 用 `.venv\Scripts\activate.bat`） | `source .venv/bin/activate` |
| 不激活也能用 | `.\.venv\Scripts\jh.exe --help` | `./.venv/bin/jh --help` |
| 控制台编码 | 中文系统默认 cp936、英文系统 cp1252 → 见 [§7 第 3 条](#7-常见问题) | 一般本来就是 UTF-8 |

两条**现在就该知道**的约定：

1. **在仓库根目录运行 `jh`**。`config.yaml` 与 `.env` 都是按"当前工作目录"查找的
   （数据库路径不受影响 —— 它按 `pyproject.toml` 所在目录解析，见 [§6](#6-数据落在哪里)）。
2. 本工具**不提供自动投递**：脚本只做「录入 / 抓取 / 打分 / 写话术 / 出清单」，
   最后的投递按钮必须由你点。合规边界见 [README](../README.md) 与 [`docs/COMPLIANCE.md`](COMPLIANCE.md)。

> **只想先看看它长什么样？** 装完依赖后直接跑 `jh demo`：
> 全部数据都是假的（1 份虚构简历 + 6 个虚构岗位），库建在系统临时目录，**不联网、不花钱、不碰你的真库**。
> 现在就跑没问题 —— 它不需要 API Key。

---

## 1. 方式一：uv（推荐，可复现）

```powershell
git clone <仓库地址> jobhunter      # 或下载 zip 解压
cd jobhunter
uv sync                             # 建 .venv + 按 uv.lock 装依赖，一次到位
uv run jh --help                    # 验证 CLI 可用
```

`uv sync` 成功时会看到（实测，节选）：

```text
Using CPython 3.13.7 interpreter at: D:\python\python.exe
Creating virtual environment at: .venv
Resolved 66 packages in 3ms
Installed 46 packages in 7.30s
 + alembic==1.20.0
 + jobhunter==0.1.0 (from file:///.../jobhunter)
 + pydantic==2.13.5
 ...（略）
```

`uv run jh --help` 会打印命令表（下面前几行是原样，中段命令为了省篇幅折叠成一行，实际会逐条列出）：

```text
 Usage: jh [OPTIONS] COMMAND [ARGS]...

 JobHunter · 求职猎手 —— 本地优先的求职辅助工具。

 抓岗位详情 → 两级匹配打分 → 按 JD 定制开场白 → 跟踪投递/笔试/面试/Offer。

⚠️ 本工具**不做自动投递**：脚本只负责出清单，投递按钮由你点。

┌─ Commands ──────────────────────────────────────────────────────────────────┐
│ init        初始化项目：建目录 + 建库 + 跑迁移（幂等，可重复执行）          │
│ resume      简历：导入解析 / 列表 / 拆分方向（P1）                          │
│ job         岗位：给链接抓详情 / 批量 / 手动粘贴 / 列表（P2）               │
│ match       匹配打分：跑分 / 明细解释 / 人工裁决（P3）                      │
│ gap         反向技能缺口分析：跨岗位聚合，告诉你该补什么（P3）              │
│ today       今日待投清单：打分排序 / 一键复制开场白 / 额度检查（P4）        │
│ apply       投递打标：批量(3=applied) / 模糊匹配 / 单条 / 待处理列表 /      │
│             时间轴                                                          │
│ interview · question · assessment · offer · weak · stats · report ·         │
│ followup · cal · tailor · leads · demo                                      │
└─────────────────────────────────────────────────────────────────────────────┘
```

能看到这张表，就说明装好了。

### 用 uv 的三条注意

1. **`jh` 没进系统 PATH**，所以要写成 `uv run jh init`、`uv run jh today`。
   （想直接敲 `jh`，就先 `uv sync`，再用 `.\.venv\Scripts\jh.exe` 或激活虚拟环境，见方式二。）
2. **别再跑一次不带 `--extra` 的 `uv sync`**：实测 `uv sync --extra docx` 之后，再跑
   `uv sync`（不带 extra），uv 会把不在默认依赖集合里的包**卸掉**：

   ```text
   Uninstalled 2 packages in 206ms
    - lxml==6.1.3
    - python-docx==1.2.0
   ```

   之后 `jh resume add 简历.docx` 又会报"缺 python-docx"。
   （实测补充：`uv run jh ...` **不会**卸掉已装好的包，它只补齐缺的 —— 所以遭殃的只有
   `uv sync` 这一步。真被卸了就重新 `uv sync --extra docx`，或者写成最保险的
   `uv run --extra docx jh resume add 简历.docx`，带全参数谁也不用猜。）
3. `uv sync` 默认**包含 dev 依赖组**（pytest / ruff / mypy 都在，实测已安装），
   所以想跑测试不用额外装东西。

`--extra` 可以叠加，一次装齐最常见的一组：

```powershell
uv sync --extra docx --extra browser
uv run playwright install chromium
uv run playwright install chromium-headless-shell   # ★ 别漏这条
```

> **为什么要装两个 Chromium**：Playwright 1.62 起，**默认无头模式用的是
> `chromium-headless-shell`**（一个更精简的独立二进制）。只装 `chromium` 会在抓取时报
> `BrowserType.launch: Executable doesn't exist ... chrome-headless-shell.exe`。
> 这个坑很隐蔽 —— 安装命令成功、包也在，只有真正 launch 的时候才失败。

---

## 2. 方式二：标准 venv + pip

适合"我不想引入 uv"或"我要把它装进已有的虚拟环境"。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows PowerShell
# .venv\Scripts\activate.bat        # Windows cmd
# source .venv/bin/activate         # macOS / Linux
pip install -e ".[dev]"
jh --help
```

macOS / Linux 同上，把第一行换成 `python3 -m venv .venv`。

成功后 `jh` 就在虚拟环境里（Windows `.venv\Scripts\jh.exe`，macOS/Linux `.venv/bin/jh`），
激活状态下可以直接敲 `jh`。没激活也行，直接给全路径：`.\.venv\Scripts\jh.exe --help`。

### ★ 关于 `".[dev]"`：pip 只警告，不装 dev 工具

实测（Python 3.13.7 + pip 25.2，一条干净的 venv）：

```text
WARNING: jobhunter 0.1.0 does not provide the extra 'dev'
Would install Mako-1.4.1 ...（中间略）... jobhunter-0.1.0 ... typer-0.27.2 tzdata-2026.4
（这一串列表里没有 pytest / ruff / mypy）
```

**原因**：`dev` 不在 `[project.optional-dependencies]` 里，它是 `[dependency-groups]` 里的
**PEP 735 依赖组**，pip 不认识 → 只对本条 extra 发一句警告，然后照常装主依赖。

**影响**：基础安装是**成功**的，日常使用完全没问题；只是**没有** pytest / ruff / mypy。
要跑测试或 lint，用 uv（`uv sync` 自带 dev 组）或补一句：

```powershell
pip install pytest pytest-asyncio pytest-cov ruff mypy
```

需要 Word / 浏览器能力时，extras 要显式写出来（逗号分隔）：

```powershell
pip install -e ".[docx,browser]"
```

其他与方式一的差异：

- **`pip` 只装你在 extras 里点名的那几个**（方式一的 `uv sync` 同理，两者都不会"自动"给你装 Playwright）。
- **uv 建的环境里没有 pip**：在方式一的环境里敲 `python -m pip` 会得到
  `No module named pip`。要么用 `uv pip install ...`，要么就用标准 venv。
- 装完 `[browser]` 之后同样要下载浏览器内核：
  `.\.venv\Scripts\playwright.exe install chromium` 与 `... install chromium-headless-shell`。

---

## 3. 可选依赖：什么时候需要哪个 extra

主依赖（`uv sync` 默认装的那 10 个）**只够跑规则层**。下面这些 extras 按需装，
目的是让默认安装又小又快。

| extra | 装了什么 | 什么时候需要 | 不装会怎样 |
|---|---|---|---|
| `browser` | `playwright>=1.45` | `jh job add <链接>` 从页面抓 JD 正文（P2 主路径）；也是 P7 抓屏的前提 | 抓取失败：日志里出现"没装 Playwright，无法抓取"，汇总里计入"失败 N"。**仍可**用 `--manual` / `--text` / `--html` 录入岗位 |
| `docx` | `python-docx>=1.1` | 读 `.docx` 简历（`jh resume add`）、导出 docx（`jh tailor export`） | `jh resume add 简历.docx` → `导入失败：读取 .docx 需要 python-docx…`；导出 → `导出 docx 需要 python-docx，但当前环境没装`，可改用 `--format md` 或先转 `.txt` |
| `llm` | `openai>=1.30` | 目前**不需要**（见下） | 实测**不影响任何功能** |
| `emulator` | `uiautomator2==3.2.2`（钉死版本） | P7 的 App 线索**技术验证**（尚未实现，见 [§9](#9-模拟器p7-才需要现在可以先跳过)） | 现在不需要装 |

**关于 `[llm]` 的实话**：这一组声明的是 OpenAI SDK，但当前的 DeepSeek Provider 是
用 `httpx` **手写**的（`src/jobhunter/llm/deepseek.py` 开头解释了为什么：只用两个请求字段、
三个响应字段，为它引入一整个 SDK 不划算，而且出错时能看到原始响应体）。
`httpx` 属于主依赖 —— 所以**没装 `[llm]` extra 也能跑 LLM 精评**，只要 `.env` 里有 key。
这一组是留给将来（function calling / 流式工具调用）的。

装完 `[browser]` 必须再下浏览器内核（命令见 [§1](#1-方式一uv推荐可复现) / [§2](#2-方式二标准-venv--pip)）。
`[emulator]` 的额外步骤见 [§9](#9-模拟器p7-才需要现在可以先跳过)。

---

## 4. 配置密钥与个人偏好

两个文件，分工完全不同：

| 文件 | 放什么 | 会不会进 Git |
|---|---|---|
| `.env` | 密钥 + **本机路径**（简历路径、库路径） | **永不提交**（`.gitignore` 已排除 `.env` 与 `.env.*`） |
| `config.yaml` | 你的求职偏好（关键词 / 城市 / 分数线 / 权重 / 禁用词…） | 可以直接提交（不含隐私） |

```powershell
copy .env.example .env      # Windows
# cp .env.example .env      # macOS / Linux
# 然后编辑 .env，填 DEEPSEEK_API_KEY
```

`.env` 里的变量（完整解释见 [`docs/config.md`](config.md)）：

| 变量 | 必填 | 作用 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 否 | **不填也能用**：规则层打分与 `jh gap` 是纯本地的，一分钱不花。只有 LLM 精评需要它。环境变量名写 `DEEPSEEK_API_KEY` 或 `JH_DEEPSEEK_API_KEY` 都可以 |
| `DEEPSEEK_BASE_URL` | 否 | 默认 `https://api.deepseek.com`，一般不用改 |
| `DEEPSEEK_MODEL` | 否 | 默认 `deepseek-chat`。改它会让 LLM 缓存自动失效（cache_key 里含模型名） |
| `JH_DB_URL` | 否 | 不填就是 `<仓库>/data/jobhunter.db`；想把库放加密盘/别的盘时用，例如 `sqlite:///D:/secure/jobhunter.db`（**用正斜杠**） |
| `JH_DEFAULT_RESUME_PATH` | 否（强烈建议） | 配了之后 `jh resume add` / `jh resume split` 不传路径就用这份。★ 简历文件名常带真名，所以**只写在这里，不要写进 `config.yaml`** |
| `JH_LOG_LEVEL` | 否 | ⚠️ **写在 `.env` 里无效**：日志模块直接读进程环境变量。要看得在 shell 里设，见下 |

```powershell
# 临时开 DEBUG 日志（日志始终脱敏，不会打印简历原文）
$env:JH_LOG_LEVEL = "DEBUG"; jh today
```

```bash
# macOS / Linux
JH_LOG_LEVEL=DEBUG jh today
```

`config.yaml` 是"我想找什么样的工作"：`keywords` / `cities` / `job_type` /
`threshold`（分数线，默认 60）/ `daily_limit`（每天最多投几条）/ `llm_top_n` /
`match_weights` / `skill_aliases` / `greeting_banned_words` / `show_reminders` 等。
每一项在文件里都有中文注释，逐字段说明见 [`docs/config.md`](config.md)。

> ★ 只改 `match_weights` 里的某一项时要小心：**三项之和必须等于 100**，
> 否则启动时会直接报 `match_weights 三项之和必须等于 100，当前是 110`。

改完存盘即生效（下次运行 `jh` 重新读）。优先级：**显式传参 > 环境变量 `JH_*` > `.env` > `config.yaml` > 代码默认值**。
想用另一份配置跑一次：`jh --config D:/path/other.yaml today`。

---

## 5. 第一次运行

> 下文统一写作 `jh <子命令>`。按你的安装方式替换成：
> `uv run jh ...`（方式一）或 `.\.venv\Scripts\jh.exe ...`（方式二，Windows）/
> `./.venv/bin/jh ...`（macOS、Linux）。已激活虚拟环境就直接敲 `jh`。
>
> **都请在仓库根目录执行。**

### ① `jh init` —— 建目录 + 建库 + 跑迁移（幂等）

```powershell
jh init
```

它做四件事：建 `data/` `output/` `state/` → Alembic 迁移到最新（11 张业务表）→
打开连接并**读回** PRAGMA 校验 → 打印小结。成功时你会看到：

```text
JobHunter 初始化

① 目录已就绪
  项目根目录：D:\...\jobhunter
  数据目录：D:\...\jobhunter\data
  导出目录：D:\...\jobhunter\output
  状态目录：D:\...\jobhunter\state

② 执行数据库迁移（Alembic upgrade head）…
✓ 数据库结构与迁移脚本一致

③ 数据库自检
  数据库：D:\...\jobhunter\data\jobhunter.db
  迁移版本：0002
  业务表数量：11 张
  表：application_events, applications, assessments, companies, interview_questions, interviews, jobs, llm_cache, offers, resume_versions, resumes

④ PRAGMA 校验通过
  journal_mode：wal
  foreign_keys：1
  busy_timeout：5000

✓ 初始化完成。下一步：`jh --help` 看全部命令（其余命令将在后续阶段实现）
```

没配密钥时会**多一行**提醒（不影响使用）：

```text
! 未检测到 DEEPSEEK_API_KEY（.env）。P0 阶段不影响使用；P3 的 LLM 精评需要它，届时请复制 .env.example 为 .env 并填写。
```

★ **可以重复执行**：第二次起第②步会变成
`✓ 数据库已存在，迁移已确认到最新（数据未改动）` —— 不重建、不清数据。
（中间那些 `INFO [alembic.runtime.migration]` 是 Alembic 的日志，属正常。）

### ② `jh resume add 我的简历.docx` —— 导入简历

```powershell
jh resume add 我的简历.docx
jh resume list            # 看已导入的简历
```

成功时你会看到（下面是用一份虚构 `.txt` 简历实跑的）：

```text
✓ 已导入：id=1　sample-resume　方向=AI Agent
   教育经历  2
   技能      9
   项目      2
   实习      1
   未识别块  0
  内容 hash：d5d999e23272f96e…（改简历会自动失效 LLM 缓存）
  ★ 已设为默认简历

下一步：jh resume split 1 看该拆哪些方向
```

- 支持 `.docx`（推荐，需要 `[docx]` extra）以及 `.txt` / `.md`（不需要额外依赖）。
- **幂等**：同一份内容重复导入不会新建记录，只会说
  `这份内容库里已有，已更新描述信息`（避免重复占 id）。
- 之后可以 `jh resume split` —— 它给建议（哪些项目前置、哪些弱化、还缺什么技能），
  **不改动你的简历文件**；加 `--save` 才落成 Markdown 到 `output/`。

### ③ `jh job add <链接>` —— 录入岗位（主路径：人给链接）

```powershell
jh job add https://example.com/job/1
jh job list
jh job show 1 --jd          # 看完整字段与 JD 正文
```

字段解析得干净时，你会看到（这一条是用 `jh job add --text "…"` 录入一段 JD 文本实跑的，
字段齐全、抓取状态 `ok`）：

```text
解析到 1 条岗位
┌───┬──────────────────────┬─────────────────────────────┬──────┬─────────────┬──────┬───────┬──────┐
│ # │ 公司                 │ 职位                        │ 城市 │ 薪资        │ 实习 │    JD │ 抓取 │
├───┼──────────────────────┼─────────────────────────────┼──────┼─────────────┼──────┼───────┼──────┤
│ 1 │ 某某网络科技有限公司 │ AI Agent 开发工程师（校招） │ 深圳 │ 20-30K·14薪 │  ?   │ 343字 │ ok   │
└───┴──────────────────────┴─────────────────────────────┴──────┴─────────────┴──────┴───────┴──────┘
没抓到的字段（留空而不是猜）：platform_job_id

✓ 入库完成：新增 1
  库里现有：new 1

下一步：jh job list 看列表　｜　jh job show <id> 看详情
```

走链接主路径时，前面会多一行 `抓取中… <链接>`；**能解析出多少字段完全取决于那个页面**。
如果页面根本不是岗位详情页（比如随手拿个网址试），你会看到字段大量留空、状态是 `partial`
—— 工具**不会替你猜**（下面只摘录关键行）：

```text
  抓取中… https://example.com/job/1
解析到 1 条岗位
│ 1 │ —  │ Example Domain │ — │ — │ ? │ 144字 │ partial │
没抓到的字段（留空而不是猜）：platform_job_id、company、city、experience、education、salary_text

✓ 入库完成：新增 1
```

几点要知道的：

- **抓取状态**：`ok`（抓到正文）/ `partial`（页面结构不标准，只抓到部分字段）/
  `failed`（没找到 JD 正文，不计入入库）。
- **不猜字段**：抓不到的一律留空，并在"没抓到的字段"里列出来。所以看到公司/城市是 `—`
  不是 bug，是"宁可空着也不编"。
- **一次可以给多个链接**；也可以批量：`jh job add --file jobs.txt`（一行一个 URL，
  `#` 开头是注释）。
- **抓不到时的降级路径**（网络差、要登录、SPA 渲染不出正文时用）：

  ```powershell
  jh job add --manual               # 手动粘贴 JD 文本
  #   粘贴完按 Ctrl+Z 再回车（Windows）/ Ctrl+D（macOS、Linux）结束输入
  jh job add --text "公司：… 职位：… 岗位职责：…"   # 直接给一段文本（脚本化调用）
  jh job add --html page.html       # 从浏览器另存的网页解析（离线也能用）
  jh job add --dry-run https://example.com/job/1    # 只解析打印，不写库
  ```

- 合规：`jh job add <url>` **只读取**页面内容，不点投递、不点赞、不私信、不绕验证码，
  请求之间有随机延时，失败即跳过（不重试轰炸）。

### ④ `jh match run` —— 两级匹配打分

```powershell
jh match run               # 规则层全量打分 → 只对规则分 Top N 走 LLM 精评
jh match run --no-llm      # 只跑规则层：0 成本、秒出
jh match run --all         # 连已投 / 已跳过的岗位也重算（默认跳过它们，省钱）
jh match explain 1         # 看某条的分维度明细（调权重靠它）
```

★ **默认只算"还没处理过"的岗位**（`applied` / `skipped`，以及已经有投递记录的）：
给不会再投的岗位重新算一遍分、写一次推荐理由，是纯粹浪费钱（LLM 精评是按条计费的）。
跳过多少条、为什么跳过都会打在屏幕上；要让它们也一起重算就加 `--all`。

规则层成功时你会看到：

```text
  本次打分 3 条，跳过 2 条已处理过的（已投 1 条、已跳过 1 条）—— 跳过 ≠ 没分：它们的分数还是上次算的那个
  ↳ 要连它们一起重算：jh match run --all

规则层结果（共 3 条）
┌────┬────┬────────┬──────┬────────┬──────┬──────────────────────┬─────────────────────────────┬──────┐
│ id │ 分 │ 规则分 │ 方向 │ 硬条件 │ 技能 │ 公司                 │ 职位                        │ 城市 │
├────┼────┼────────┼──────┼────────┼──────┼──────────────────────┼─────────────────────────────┼──────┤
│  1 │ 83 │     83 │   30 │     30 │   23 │ 某某网络科技有限公司 │ AI Agent 开发工程师（校招） │ 深圳 │
└────┴────┴────────┴──────┴────────┴──────┴──────────────────────┴─────────────────────────────┴──────┘
  分数线 60：有 1 条达标（`jh today` 会按它筛，P4）
```

**没配 key 时**，你会看到这样两行 —— 这是**设计行为，不是错误**（退出码仍是 0）：

```text
LLM 精评（规则分前 1 条，模型 deepseek-chat，并发 4）

! 没有配置 deepseek_api_key，本次只用规则层打分
  规则分已经算好并落库，`jh match explain <id>` 照样能看到明细。要开精评：在项目根的 .env 里填 DEEPSEEK_API_KEY
```

它的意思是："规则分已经算好并入库了，只是想开精评得填 key。"
工具选择**明确降级 + 提示**，而不是抛一个看不懂的连接错误。

### ⑤ `jh today` —— 今日待投清单 + 开场白

```powershell
jh today
```

成功时你会看到：

```text
今日待投（1 条）
┌───┬────┬──────────────────────┬─────────────────────────────┬──────┬───────────────┬──────────────┬──────┐
│ # │ 分 │ 公司                 │ 职位                        │ 城市 │ 用哪份简历    │ 变体         │ 链接 │
├───┼────┼──────────────────────┼─────────────────────────────┼──────┼───────────────┼──────────────┼──────┤
│ 1 │ 83 │ 某某网络科技有限公司 │ AI Agent 开发工程师（校招） │ 深圳 │ sample-resume │ with_project │ —    │
└───┴────┴──────────────────────┴─────────────────────────────┴──────┴───────────────┴──────────────┴──────┘

1. 某某网络科技有限公司 · AI Agent 开发工程师（校招） （模板兜底）　99 字
   您好，我是AI Agent方向的应届生。看到岗位要求Python、FastAPI、Redis，我在校园二手交易平台里实际用过FastAPI、Redis。方便的话，想请教一下这个岗位目前最看重哪一块？
   精简版（38 字）：AI Agent方向，Python、FastAPI 有项目经验，方便聊聊吗？

  过滤前达线 1 条；今天已投 0/20

下一步：jh today --copy 1 复制第 1 条开场白　｜　投完 jh apply 1 --status applied　｜　jh today --export md 导出
```

常用变体：

```powershell
jh today --limit 5         # 只看前 5 条
jh today --copy 1          # 把第 1 条开场白复制到剪贴板（--short 复制精简版）
jh today --check           # 只看"今天已投 X / 上限 N"
jh today --export md       # 导出到 output/（md 或 csv，只写新文件）
```

`--copy` 成功时会说 `✓ 已复制第 1 条（公司 · 职位）的完整版开场白（99 字）`，
然后你去招聘 App 里粘贴即可（**发不发、发哪条由你决定**）。

★ 两点与"库内数据"有关的约定：

- `--copy N` 如果**重新生成**了话术（缓存失效时才会，例如你刚改过简历），
  新文本会**回写进 `applications.greeting_text`** —— 库里始终是你在屏幕上看到的那一版
  （否则下次看到旧话术，会以为工具记错了）。回写**不会**动 `greeting_sent`：
  生成不等于已发出，那个标记只由你的 `--sent` / `jh apply` 决定。
- `--export` 出来的 md/csv 里，每条都带**岗位 id**（`jh apply --job <id>` 直接能用），
  简历那一列只写「简历 #id」而**不写文件名** —— 导出物常被丢进聊天窗口/云笔记，
  而简历文件名里往往带着真名。

> **关于顶部那行提醒**：任何 `jh` 命令开头都可能出现
> `📌 待处理：投后 3 天未跟进 3 条 ｜ …`。
> **没有待处理事项时一个字都不打印**（否则第三天你就会把它关掉）。
> `--no-remind` 或 `config.yaml` 的 `show_reminders: false` 可以关掉。

到这里，从零到"有清单、有话术"的主流程就跑通了。之后每天：
`jh job add <链接>` → `jh match run` → `jh today` → 投完 `jh apply 1 --status applied`。
完整命令清单见 [README](../README.md) 的「快速开始」。

---

## 6. 数据落在哪里

三个目录，都在**仓库根目录**下（`jh init` 会把它们建出来并打印绝对路径）：

| 目录 | 放什么 | 能否删 |
|---|---|---|
| `data/` | **SQLite 数据库** `data/jobhunter.db`（+ WAL 的 `-wal` / `-shm`）；P7 的抓屏产物在 `data/leads/` | 删了 = 所有投递记录没了 |
| `output/` | 导出产物：待投清单 md/csv、简历建议、周报、`.ics`、定制简历 docx | 可以删，重新导出即可（导出**只写新文件**，不覆盖旧文件） |
| `state/` | 运行时状态，例如今日额度 `state/today-2026-09-19.json` | 可以删，会自动重建 |

关于路径的三件事：

1. **数据库默认位置是 `<仓库根>/data/jobhunter.db`**，仓库根 = `pyproject.toml` 所在目录
   （不是当前工作目录）—— 所以你在哪个目录敲 `jh` 都不会"换一个库"。
2. 想放别处（加密盘、数据盘）：在 `.env` 里设 `JH_DB_URL=sqlite:///D:/secure/jobhunter.db`，
   或用环境变量 `JH_DATA_DIR` / `JH_OUTPUT_DIR` / `JH_STATE_DIR` 改三个目录。
   **`jh init` 打印的"数据目录 / 导出目录 / 状态目录"就是当前实际生效的**，用它确认。
3. `.gitignore` 已经排除（**所以不会被误提交**）：`.env`、`data/`、`output/`、`state/`、
   `*.db`、`*.db-wal`、`*.db-shm`、`*.docx`、`*.pdf`、`*.doc`、`*简历*.txt` 等。
   连 `.db-wal` 都要忽略的原因：SQLite 开了 WAL 后，**最新数据可能还在 `-wal` 文件里**，
   只忽略 `*.db` 等于把真实投递记录提交上去。

**备份 / 搬走**：

```powershell
# ① 最稳：先关掉所有还在跑的 jh 命令，再整个拷走（含 -wal / -shm，别只拷 .db）
Copy-Item -Recurse data D:\backup\jobhunter-data
```

- **要连 `.db-wal` / `.db-shm` 一起拷**（或者先停用 `jh` 再拷）。WAL 模式下只拷
  `jobhunter.db` 有可能丢掉最近写入的内容。
- 换机器：拷 `data/` + `.env` + `config.yaml` 过去即可（在新机器上仍要先 `uv sync`）。
- 不建议把库直接放在网盘/多端同步目录里（WAL + 多设备同时写有冲突风险）；非要放，
  至少保证同一时间只有一台设备在用。
- `jh demo` 用的是系统临时目录里的独立库（`%TEMP%\jh-demo-*` / `/tmp/jh-demo-*`），
  **正常结束会删掉**；中途失败会残留，手动删掉即可。

---

## 7. 常见问题

### 1）`jh : 无法将"jh"项识别为 cmdlet…` / `jh: command not found`

**现象**：刚装完，敲 `jh --help` 报找不到命令。
**原因**：`jh` 只装进了**虚拟环境**，没有进系统 PATH；你既没激活环境，也没用 `uv run`。
**解决**（任选其一）：

```powershell
uv run jh --help                          # 方式一
.\.venv\Scripts\jh.exe --help             # Windows，不激活也能用
.\.venv\Scripts\Activate.ps1; jh --help   # Windows，先激活
```

```bash
source .venv/bin/activate && jh --help    # macOS / Linux
```

全都失败 → 用兜底入口（`src/jobhunter/__main__.py` 就是为这种情况留的）：
`.\.venv\Scripts\python.exe -m jobhunter --version`，应该输出
`JobHunter 0.1.0 (phase P6)`。若这一步也失败，说明依赖没装：回到 [§1](#1-方式一uv推荐可复现) / [§2](#2-方式二标准-venv--pip)。

### 2）PowerShell 报"禁止运行脚本"，激活不了虚拟环境

**现象**：`.\.venv\Scripts\Activate.ps1` 被拒绝执行（执行策略限制）。
**原因**：Windows 默认的 PowerShell 执行策略不允许运行未签名的脚本。
**解决**：**不用改策略**，直接调用可执行文件 `.\.venv\Scripts\jh.exe --help`；
或者用 cmd（`.venv\Scripts\activate.bat`）；确实想改就
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`（本机实测该策略下激活正常）。
检查当前策略：`Get-ExecutionPolicy -List`。

### 3）Windows 上中文/emoji 显示成乱码或 `?`

**现象**：`jh --help` 里的 `→`、`·`、emoji 显示成问号，或者干脆整条命令失败。
**原因**：Python 的 stdout/stderr 编码取自运行环境 —— 中文 Windows 是 cp936、
英文 Windows 与 GitHub Actions runner 是 **cp1252**。cp1252 下打印 `→`/emoji 会抛
`UnicodeEncodeError`，**让整条命令以非 0 退出**（在英文 Windows 上 `jh --help` 都会崩）。
**本项目已经内置解决**：`src/jobhunter/__init__.py` 在**导入时**就调用
`utils/io.py` 的 `force_utf8_output()`，把 stdout/stderr 切成 UTF-8 并设 `errors="replace"`
（必须在任何输出之前做，因为 rich 的 `Console` 在构造时就把编码缓存下来了）。
所以剩下的花屏基本是**终端自己**的问题。
**解决**：

```powershell
chcp 65001        # 把控制台代码页切成 UTF-8
```

再换用 Windows Terminal 与一款支持中文的等宽字体。**你自己写脚本处理导出的 md/csv 时，
务必显式 `encoding="utf-8"`** —— 仓库用 ruff 的 `PLW1514` 守着这条，`utils/io.py` 是全项目
唯一允许读写文本文件的地方。

### 4）`jh match run` 说"没有配置 deepseek_api_key，本次只用规则层打分"

**现象**：见 [§5 ④](#5-第一次运行) 的输出。
**原因**：`.env` 里没填 `DEEPSEEK_API_KEY`，**或者你不在仓库根目录运行**（`.env` 按当前工作目录查找），
于是 `settings.llm_enabled` 为 False。
**解决**：**这是设计行为，不是错误** —— 规则层打分与 `jh gap` 完全不需要密钥，纯本地、零成本。
想要 LLM 精评：`copy .env.example .env` → 填 key → 回仓库根目录再跑。
验证是否读到了：`jh init` 不再打印"未检测到 DEEPSEEK_API_KEY"。

### 5）`数据库还没初始化。请先运行：jh init`

**现象**：`jh job list` 之类的命令直接报这句并退出（退出码 1）。
**原因**：库文件或表还不存在（工具把原本的 `no such table: jobs` 换成了这句人话）。
**解决**：`jh init`（幂等，随便重复跑）。如果你设过 `JH_DATA_DIR` / `JH_DB_URL`，
确认它指的是你期望的那个库 —— `jh init` 会把实际路径打印出来。

### 6）找不到库 / 换了台机器数据"不见了"

**现象**：明明导过岗位，`jh job list` 却是空的（或提示未初始化）。
**原因**：库路径默认按 **`pyproject.toml` 所在目录**解析，所以换目录运行不会换库；
真正会"换库"的是：设过 `JH_DATA_DIR` / `JH_DB_URL`、用了另一个 clone、
或者在新机器上还没把 `data/` 拷过来。
**解决**：看 `jh init` 打印的"数据库"那一行（就是当前实际生效的路径）。
要把库固定到某处，在 `.env` 里写 `JH_DB_URL=sqlite:///D:/secure/jobhunter.db`（**正斜杠**）。
搬机器：把 `data/` 整个拷过去（含 `-wal` / `-shm`）。

### 7）`jh job add <链接>` 抓不到 JD

**现象**：汇总里出现 `失败 1`，或者入库了但公司/城市是 `—`、JD 字数很少。
**原因**：页面需要登录 / 是 JS 渲染的 SPA / 被风控拦 / 它根本不是岗位详情页；
也可能是没装 `[browser]` extra 或漏了 `playwright install`。
**解决**：先看失败清单给的原因，再走降级路径（[§5 ③](#5-第一次运行)）：

- `（打开失败/超时）` → 网络或超时；换网络重试，或直接 `--manual` 粘贴。
- `（页面里没找到 JD 正文，可改用 --manual 粘贴）` → 页面结构不认识，用 `--manual` / `--text`。
- 日志里"没装 Playwright，无法抓取" → `uv sync --extra browser` 并装两个 Chromium（见 §1）。
- 抓到的字段不全（`partial`）→ 正常现象，工具**不猜**字段：缺的留空并列出来，
  你可以 `jh job show <id> --jd` 看正文，必要时补录一条。

注意：工具**只读页面**，不点投递、不绕验证码、不做自动搜索翻页。

### 8）导出 docx 报缺 python-docx

**现象**：`jh tailor export 1` 报
`导出 docx 需要 python-docx，但当前环境没装`。
**原因**：没装 `[docx]` extra（方式一里如果之后又跑了不带 `--extra` 的 `uv sync`，也会被卸掉）。
**解决**：

```powershell
uv sync --extra docx                     # 或用 pip install -e ".[docx]"
uv run --extra docx jh tailor export 1   # 想写明就带上 --extra（见 §1 第 2 条）
```

不想装就导出 Markdown：`jh tailor export 1 --format md`（**不需要额外依赖**）。
读简历（`jh resume add 简历.docx`）同样需要这个 extra，不行就先把简历另存为 `.txt`。

### 9）简历是 `.doc` 或 `.pdf`

**现象**：`jh resume add 我的简历.pdf` → `导入失败：不支持的文件格式：.pdf`，
后面跟着一段指引。
**原因**：只支持 `.docx` 与 `.txt` / `.md`。**PDF 不做解析是有意的**：PDF 里没有段落结构，
抽出来的文本顺序不可靠，硬解析出来的简历会比手动复制更差。
**解决**：`.doc` → 用 Word 另存为 `.docx`；`.pdf` → 复制成 `.txt`，或用 Word 转成 `.docx`。

### 10）`jh today --copy N` 复制失败（多见于 Linux）

**现象**：`! 复制到剪贴板失败（这个系统上可能没有 clip/pbcopy/xclip），下面是原文：`
**原因**：剪贴板靠系统命令实现：Windows `clip`、macOS `pbcopy`、Linux `xclip` / `xsel`，
Linux 桌面默认可能都没装。
**解决**：`sudo apt install xclip`（或 `xsel`）；命令已经把开场白原文打出来了，
手动选中复制也一样。

### 11）`jh demo` 结束时报 `RuntimeError: 还原失败：配置单例还指着演示库`

**现象**：演示跑完最后一步崩了（退出码 1），临时目录也留了下来。
**原因**：你自己设过 `JH_DATA_DIR` / `JH_OUTPUT_DIR` / `JH_STATE_DIR` 环境变量。
`jh demo` 为了隔离会临时改这三个变量，退出时**把它们删掉**再和进入时的库路径比对，
于是对不上（这是它的自我断言，防止假数据落到真库）。
**解决**：跑 `jh demo` 前清掉这几个变量（新开一个终端最省事）。残留目录手动删：
`Remove-Item -Recurse -Force $env:TEMP\jh-demo-*`（macOS/Linux `rm -rf /tmp/jh-demo-*`）。
正常结束的 `jh demo` 会自己删除临时目录（实测：退出后该目录不存在）。

### 12）`pip install -e ".[dev]"` 警告 `does not provide the extra 'dev'`

**现象**：见 [§2](#2-方式二标准-venv--pip)。
**原因**：`dev` 是 PEP 735 依赖组，不是 extra，pip 不认识（只警告，不报错）。
**解决**：基础安装本来就是成功的；要跑测试/lint 就用 uv（`uv sync` 自带 dev 组），
或 `pip install pytest pytest-asyncio pytest-cov ruff mypy`。
需要文档/浏览器能力请写 `pip install -e ".[docx,browser]"`。

### 13）在 uv 建的环境里 `python -m pip` 报 `No module named pip`

**原因**：uv 建虚拟环境时默认不装 pip。
**解决**：用 `uv pip install ...` / `uv add ...`；或者按 [§2](#2-方式二标准-venv--pip) 用标准 venv
（`python -m venv`）。**别把两种方式混在同一个 `.venv` 上。**

### 14）任何命令顶部都不显示"待处理"提醒

**现象**：看不到 `📌 待处理：…` 那一行。
**原因**：**没有待处理事项时它就是不打任何字**（有意设计：否则你会习惯性忽略它）。
或者 `config.yaml` 里 `show_reminders: false` / 你加了 `--no-remind`。
**解决**：确认方式 —— 投完标后过几天再看；`jh apply --pending` 可以主动看清单。

### 15）`jh leads …` 报"该功能尚未实现"

**现象**：跑 `jh leads probe` / `jh leads add <file>` / `jh leads capture`，出来一个黄色面板
写着"该功能尚未实现 / 计划阶段：P7"，命令以退出码 2 结束。
**原因**：P7 还没实现，这几个是**占位命令**（本项目的约定：占位命令**不假装成功**，
会明确说明阶段并用非 0 退出码结束）。
**解决**：现在用不到它，跳过即可；真到 P7 落地时再看 [§9](#9-模拟器p7-才需要现在可以先跳过)。
和它无关的功能（`jh job add` / `jh match run` / `jh today` …）**不受影响**。

### 16）一启动就报 `配置错误`（config.yaml 里有写错的项）

**现象**：任何命令都以 `配置错误` + 一段中文说明结束（退出码 1）。
**原因**：这是**故意的** —— 配置写错时"静默无效"比"启动报错"坏得多。当前会拦下这些：

| 配置项 | 会被拒的写法 | 为什么要拦 |
|---|---|---|
| `learning_whitelist` | 词表里没有的技能（多为拼写错误，如 `LanchChain`）、空白项 | 错词永远不会命中任何 JD，"我在学 X"规则静默失效 —— 你只会觉得功能没效果 |
| `greeting_banned_words` | 空串、纯空白、首尾带空格、单条超 12 字 | 禁用词是**朴素子串匹配**：空串等于拦一切，一整句话会大面积误伤正常文案 |
| `rate_limit.min/max_delay_seconds` | `min > max`、负数 | 延时是合规红线（`0` 会静默关掉延时） |
| `skill_aliases` | 空白的目标名、空别名列表 | 写错会让技能归一化静默失效 |

**解决**：照报错里那句"期望……"改。想给 `learning_whitelist` 加一个词表里没有的新技能：
**先**在 `skill_aliases` 里把它写成规范名（这样它才会进词表、也才能从 JD 里抽出来），**再**放进白名单。
参考 [config.md](config.md) 的 §3 / §4。

### 17）设置了 `greeting_banned_words` / `learning_whitelist`，但感觉"没生效"

**现象**：改了这两个配置，重跑 `jh today`，开场白看起来没变化。
**原因**：多数情况下是**真的生效了，只是你看到的不是新生成的文案** —— 命中缓存时不会再调模型。
不过从 **ISSUES #55** 起，缓存 key 里包含**整份提示词的指纹**，这两个配置都会进提示词，
所以**改了它们缓存就会自动失效**、一定会重新生成。
**解决**：若仍怀疑，用 `jh cache stats` 看条目数、`jh cache clear --task greeting` 清掉再跑一次（见 [config.md](config.md) §11）。

---

## 8. 卸载与清理

本项目是**纯 CLI**：不写注册表、不装系统服务、不常驻后台、不往系统目录塞东西。
所以"卸载"基本上就是删目录。

```powershell
# ① 如果你只是在项目里跑（方式一/方式二的默认情况）：删掉整个 clone 目录即可
#    （.venv 就在里面，一起没了）
Remove-Item -Recurse -Force D:\path\to\jobhunter

# ② 清 uv 的全局缓存（可选；它是所有项目共用的，清掉只会让下次安装变慢）
uv cache clean
uv cache dir            # 想看看缓存放在哪

# ③ 如果你是 pip 装到别的环境里的（不是 -e 到项目内）
pip uninstall jobhunter

# ④ 删掉演示残留（正常结束不会有）
Remove-Item -Recurse -Force $env:TEMP\jh-demo-*
```

要彻底清掉个人数据，删这几处（**先备份要留的**）：

| 位置 | 内容 |
|---|---|
| `<仓库>/data/` | 数据库（投递记录、简历结构化数据、LLM 缓存）+ `data/leads/` 抓屏产物 |
| `<仓库>/output/` | 所有导出物（清单、周报、定制简历、`.ics`） |
| `<仓库>/state/` | 今日额度等运行时状态 |
| `<仓库>/.env` | 密钥与本机路径 |
| `JH_DB_URL` / `JH_DATA_DIR` 指向的目录 | 如果你把库放到过别处 |
| `%TEMP%\jh-demo-*`（macOS/Linux `/tmp/jh-demo-*`） | 演示库残留 |
| `~/.cache/uv`（`uv cache dir` 显示的路径） | uv 下载的 wheel 缓存（与隐私无关） |

---

## 9. 模拟器（P7 才需要，现在可以先跳过）

> ### ⚠️ 这部分**尚未实现**，下面的命令**现在跑不通**
>
> `jh leads probe` / `jh leads capture` / `jh leads add` / `jh leads export`
> 目前都是**占位命令**：执行后只会打印一个"该功能尚未实现"的面板，并以**退出码 2** 结束。
> 实测（面板里的"现在就可用：…"那一长串已省略）：

```text
┌───────────────────────────────── 🚧 未实现 ─────────────────────────────────┐
│ 该功能尚未实现                                                              │
│                                                                             │
│   功能：jh leads probe（serial=None）                                       │
│   计划阶段：P7                                                              │
│   当前阶段：P6                                                              │
│   ……（下面列的是当前可用的命令）                                          │
└─────────────────────────────────────────────────────────────────────────────┘
```

**本节只说明"将来要用 App 线索功能时，届时需要准备什么"**，不是现在要做的事。
现在装 `[emulator]` extra 不会有任何用处 —— 但也不会有坏处（它只是个 Python 包）。

### 届时会需要的东西

1. **一个安卓模拟器**。最省事的是 Android Studio 自带的 AVD（ADB 是现成的）；
   第三方模拟器（MuMu、夜神、雷电之类）也能用，但它们各自用自己的 ADB 端口，
   要按它自己的文档 `adb connect 127.0.0.1:<它告诉你的端口>` —— **端口随版本变化，
   别照抄网上的数字**。
2. **打开 ADB 调试并能看到设备**：

   ```powershell
   adb devices
   # 期望看到类似（emulator-5554 是你的设备序列号）
   # List of devices attached
   # emulator-5554   device
   ```

3. **装 extras 并把 ATX 推到设备上**：

   ```powershell
   uv sync --extra emulator                       # uiautomator2==3.2.2（钉死版本）
   uv run --extra emulator python -m uiautomator2 init   # 首次把 ATX-agent 推进模拟器
   adb devices                                    # 再确认一次设备还在
   uv run --extra emulator jh leads probe --serial emulator-5554
   #   ↑ 现在会报"未实现"，P7 之后才会 dump 控件树
   ```

   `[emulator]` 里 `uiautomator2` 的版本是**钉死**的（`==3.2.2`）：上游改动频繁，
   不钉版本会导致"半年前能跑的脚本突然失灵"，而这类环境问题最难排查。

4. **知道抓屏产物落在哪**：`data/leads/`（一次抓屏一个子目录）。

### 这个阶段已知的现实难点（项目自己的记录）

控件 id 不稳定甚至不存在、WebView 里的内容取不到、部分国内 ROM 装不上 ATX、
无线 ADB 容易掉线。所以 P7 的路线是"**人工操作 + 机器抓屏**"而不是全自动
（`jh leads capture` 就是这个意思：你在模拟器里手动操作，按回车抓一屏）。

**合规红线（P7 也一样）**：只读、不做任何写操作（不点赞、不私信、不关注）、
不做批量抓取、不绕风控；建议用备用账号。

> P7 真正落地时会补一份 `docs/emulator.md`（模拟器 / ADB / uiautomator2 的完整教程，
> 见 [`docs/architecture.md`](architecture.md) 的目录规划）。**现在这个文件还不存在** ——
> 如果你在仓库里找不到它，那是正常的。

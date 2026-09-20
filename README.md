# JobHunter · 求职猎手

> 本地优先的**开源求职辅助工具**：抓岗位详情 → 两级匹配打分 → 按 JD 定制开场白 → 跟踪投递全流程。
> 用 Python 3.13 + Typer + SQLite + Playwright + DeepSeek 写成，**代码当学习材料**（详细中文注释 + 讲原理）。

> [!WARNING]
> **⚠️ 合规声明（请先读这段）**
> 1. 本工具是求职**辅助**工具，**不提供自动投递功能**。脚本只做
>    「录入 / 抓取 / 打分 / 写话术 / 出清单」，**最后的投递按钮必须由人点击**。
> 2. 请遵守各招聘平台、社交平台的**用户协议**。本项目**不绕过**验证码、风控、签名机制，
>    也不做自动搜索翻页；频率控制（随机延时 2–5 秒 / 出错即停 / `--dry-run`）默认开启。
> 3. 使用者**自行承担**使用本工具产生的一切风险与后果。
> 4. 所有数据**只存在你本机**。密钥放 `.env`（不入库），数据库与导出物已在 `.gitignore` 中排除。

---

## 这个工具解决什么问题

三个最痛的（本质是同一个病：**没从 JD 出发**）：

| 痛点 | 本工具的做法 |
|---|---|
| 用同一份简历海投，跟很多岗位不匹配 | **两级匹配打分**（规则层 0 成本粗筛 → 只对 Top N 走 LLM 精评）+ `jh gap` 告诉你**该补什么技能** |
| 写的开场白跟岗位要求根本不沾边 | 共享中间层的 **JD 关键词 + 简历真实亮点** 生成开场白（完整版 + 60 字精简版） |
| 投完就忘、基本没管理 | **零手打跟进**：批量打标 `jh apply 3=applied 7=replied` + 每条命令顶部固定提醒 |

### 核心架构思想：共享「JD 解析 + 简历匹配」中间层

```
              JD + 简历
                  ↓
        ┌── 共享中间层（只算一次）──┐
        │  jd_keywords / direction  │
        │  hard_requirements        │
        │  skill_overlap{matched,   │
        │    missing} / rule_score  │
        └───────────┬───────────────┘
                    │  产物 jd_keywords_json 落库到 jobs 表
        ┌───────────┼───────────────┐
        ↓           ↓               ↓
   ① 匹配打分   ② 定制简历      ③ 生成开场白
   （能不能投） （怎么改）      （怎么开口）
```

**为什么这样设计**：产物只算一次、三处复用 → 开场白**天然引用 JD 关键词**所以必然沾边，
且省掉重复的模型调用。

---

## 功能一览

| 阶段 | 能力 | 命令 |
|---|---|---|
| P1 | 简历导入解析（docx/txt，含文本框与表格）+ 按方向拆分 | `jh resume add / list / split` |
| P2 | 岗位录入（**人给链接** → 抓详情 → 三层去重） | `jh job add <url｜--file｜--manual｜--html>` |
| P3 | **两级匹配**（规则粗筛 + LLM 精评）+ 反向技能缺口 | `jh match run / explain / set`、`jh gap` |
| P4 | 今日待投清单 + 开场白（完整/精简）+ 导出 | `jh today [--copy N｜--check｜--export md]` |
| P4.5 | **零手打跟进**：批量打标 / 模糊匹配 / 顶部固定提醒 | `jh apply 3=applied 7=replied`、`jh apply --pending` |
| P5 | **AI 定制简历** + `FidelityGuard`（代码强制不编造） | `jh tailor brief / analyze / approve / export` |
| P6 | 看板时间轴 / 面试 / 问题记录 / 笔试 / Offer 比较 / 转化率 / 周报 / 日历 | `jh interview · question · weak · assessment · offer · followup · stats · report · cal` |
| **P9** | **`jh demo`（离线假数据全流程）+ 合规自检** | `jh demo`、[COMPLIANCE.md](docs/COMPLIANCE.md) |
| **P7** | 线索**消费端**：导入采集端的 JSON 快照 → 本机线索页（看线索 / 标记投递） | `jh leads import <快照.json>`、`jh leads serve` |
| P8 | Web 界面（其余页面移出主线）—— ★ 线索页已随 P7 消费端落地，是 P8 的第一次落地 | `jh leads serve` |

> ★ **P7 在 2026-09-19 拆成了"采集端"和"消费端"两个部署**，本仓库只有**消费端**，
> 所以这里的 `jh leads` **只有 `import` 与 `serve` 两个子命令**：
>
> | | 在哪 | 命令 |
> |---|---|---|
> | **采集端** | **不在本仓库**：独立部署 `projects/leadhunter` | `lh leads add / list / export / apply / probe / capture` |
> | **消费端** | 本仓库 | `jh leads import <快照.json>`、`jh leads serve` |
>
> 两边**不共用数据库**，唯一的接口是采集端导出的那份 JSON 快照
> （契约见 [docs/leads-snapshot.md](docs/leads-snapshot.md)，样例见
> [docs/leads-snapshot-sample.json](docs/leads-snapshot-sample.json)）：采集端导出 → 你把文件拷过来 →
> `jh leads import` 入库 → `jh leads serve` 在浏览器里看。
>
> **为什么这么拆**：采集端要装安卓模拟器那一套依赖（`uiautomator2`）、要连设备、要在本机跑；
> 消费端只要一个库和一张网页。混在一个仓库里，等于让"看线索"这件事也背上那份环境负担。
> 所以 `add / probe / capture / export` **已经不在这里了**（原来的占位命令已删除）——
> 要采集请去 leadhunter 用 `lh`。

### 先看演示（不需要 API Key、不需要真去投简历）

```bash
jh demo            # 全部数据都是假的，库建在临时目录，退出即销毁
jh demo --keep     # 想自己翻着看就保留下来
```

真实运行输出（节选，`jh demo` 一次跑完 ①~⑪ 全流程）：

```text
① 建库：sqlite:///C:/Users/.../Temp/jh-demo-xxxx/data/jobhunter.db
   （临时目录，退出即销毁；真实库一个字节都没动）
③ 两级匹配（规则层，本地计算，不花钱）
    66 分　甲公司 · Python 后端开发实习生　(Python 开发)
    41 分　丙公司 · 大模型应用开发实习生　(AI Agent)
    30 分　戊公司 · Java 后端开发（3-5 年）　(Python 开发)
④ 今日待投清单（取分数最高的 2 条）
   #1　66 分　甲公司 · Python 后端开发实习生　134 字开场白
      您好，我是 Python 后端方向的应届生。看到岗位要求 Python 与 FastAPI，……
⑤ 打标跟进（每次状态变更都会写入 append-only 事件流）
   甲公司 · Python 后端开发实习生 → offer
   乙公司 · 后端开发工程师（校招） → rejected
⑦ 薄弱点（按「没答上来次数」排序，实录与预测分列）
   Redis 缓存　没答上来 1/2　🔴 最先补：反复答不上来
⑧ 转化率（样本量很小（6 条）——分组之间的差异基本是噪声，别据此下结论）
   投递 6　已读率 66.7%（4/6）　回复率 50.0%（3/6）　约面率 33.3%（2/6）
⑪ 周报与产物   已写出：…/Temp/jh-demo-xxxx/output/report-20260919.md
```

> 这个工具是**纯 CLI**，所以这里用的是**真实终端输出**而不是截图/GIF
> （造一张假截图比贴真输出更容易骗人）。想自己看全量输出：`jh demo`。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/install.md](docs/install.md) | 安装与第一次运行、常见问题、数据落在哪 |
| [docs/config.md](docs/config.md) | **config.yaml 与 .env 逐字段说明**（含"写错了会怎样"） |
| [docs/COMPLIANCE.md](docs/COMPLIANCE.md) | ★ 合规与隐私自检（自动化边界 / 频率控制 / PII 扫描 / 历史痕迹） |
| [docs/architecture.md](docs/architecture.md) | 分层、目录结构、关键设计取舍 |
| [docs/data_model.md](docs/data_model.md) | 11 张表 + 迁移说明 |
| [docs/STATUS.md](docs/STATUS.md) | 分阶段交付说明（做了什么 / 已知边界 / 建议验证点） |
| [docs/roadmap.md](docs/roadmap.md) | 完整路线图与阶段验收标准 |

---

## 安装

### 方式一：uv（推荐，可复现）

```bash
# Windows 安装 uv（任选其一）
py -m pip install uv
# 或见 https://docs.astral.sh/uv/getting-started/installation/

git clone <this-repo> && cd jobhunter
uv sync                 # 依赖 + 虚拟环境一次到位（严格按 uv.lock）
uv run jh --help        # 验证 CLI 可用
```

### 方式二：标准 venv + pip

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -e .
pip install pytest ruff mypy    # ★ 开发工具要单独装，见下面的说明
jh --help
```

> ⚠️ **别写 `pip install -e ".[dev]"`**（P9 实测）：`dev` 是 **PEP 735 依赖组**
> （`[dependency-groups]`），不是 optional extra。pip 只会打一句
> `WARNING: jobhunter 0.1.0 does not provide the extra 'dev'` 然后**什么都不装** ——
> 基础安装是成功的，但 pytest/ruff 一个都没有，之后 `pytest` 会报 command not found。
> 想要一步到位就用**方式一（uv）**：`uv sync` 会把 dev 组一起装上。

### 可选依赖（按需装，主依赖保持干净）

| 命令 | 用途 | 哪个阶段 |
|---|---|---|
| `uv sync --extra browser` | Playwright 抓岗位详情 | P2 |
| `uv sync --extra docx` | 解析/导出 Word 简历 | P1 / P5 |
| `uv sync --extra web` | `fastapi` + `uvicorn`：本机线索页 | P7 消费端（**只有 `jh leads serve` 需要**） |
| `uv sync --extra emulator` | 安卓模拟器抓屏（技术验证） | **不在本仓库**了：采集端独立部署在 `projects/leadhunter`（命令 `lh`） |

> ★ **`jh leads import` 不需要 `[web]` extra**：导入只用 pydantic（主依赖）读 JSON、写
> `companies` 表；只有 `jh leads serve` 要起 FastAPI 服务。所以"只想导不想看页面"的人
> 不用装这两个包 —— 反过来，没装时 `jh leads serve` 会给一句中文提示告诉你装哪条命令。
> ★ **`[emulator]` 现在在本仓库用不到**：`src/` 里没有任何 `import uiautomator2`，
> 抓屏那套（`probe` / `capture`）在 leadhunter 里。这个 extra 留着是为了将来，
> 装不装都不影响本仓库的任何命令。

> ★ **没有 `llm` extra 要装**：DeepSeek 是用 `httpx` 手写的（`httpx` 是主依赖），
> 不需要任何额外包就能跑 LLM 精评。（`pyproject.toml` 里保留了一个 `[llm]` extra，
> 但当前代码路径用不到它，留着是为了将来换 SDK 时有个位置。）
>
> ⚠️ **`uv sync` 会卸掉没写进命令行的 extra**（实测：裸 `uv sync` 会把 `python-docx` 卸掉）。
> 日常用 `uv run jh …` 不会触发这件事；只有你又跑了一次裸 `uv sync` 才会。
> 所以习惯是：`uv sync --extra docx` 之后一律用 `uv run`。

装完可选依赖后，还需要下载浏览器内核：

```bash
uv run playwright install chromium
uv run playwright install chromium-headless-shell   # ★ 别漏这条
```

> **为什么要装两个**：Playwright 1.62 起，**默认的无头模式用的是
> `chromium-headless-shell`**（一个更精简的独立二进制），
> 只装 `chromium` 会在抓取时报
> `BrowserType.launch: Executable doesn't exist ... chrome-headless-shell.exe`。
> 这个坑很隐蔽：安装命令成功、包也在，只有真正 launch 的时候才失败。

### 配置密钥

```bash
copy .env.example .env      # Windows（macOS/Linux: cp .env.example .env）
# 编辑 .env，填入 DEEPSEEK_API_KEY
```

> ★ **不填也能用**：规则层打分（`jh match run --no-llm`）和 `jh gap` 完全不需要密钥，
> 它们是纯本地的。只有 LLM 精评需要 —— 没配时命令会**明确提示并降级**，
> 而不是抛一个看不懂的连接错误。
> 环境变量名写 `DEEPSEEK_API_KEY` 或 `JH_DEEPSEEK_API_KEY` 都可以。

`config.yaml` 是**个人求职偏好**（关键词 / 城市 / 分数线 / 权重 / 禁用词…），可以直接提交；
`.env` 只放密钥，**永不提交**。

> ★ **`.env` 是你本机的私密配置，请不要提交或贴给别人**（ISSUES #28）。
> 它已经被 `.gitignore` 挡住、也不在版本控制里，但里面除了 `DEEPSEEK_API_KEY`，
> 通常还会有 `JH_DEFAULT_RESUME_PATH`（简历路径，文件名往往带你的真实姓名）。
> 仓库本身是干净的（受跟踪文件里没有你的个人信息，见 [隐私说明](#隐私说明)），
> 这份提醒针对的是**你自己那个文件**——别把它粘进 issue、聊天窗口或截图里。
> 提交前想自查一遍，用 `git status --porcelain` 确认 `.env` 没出现在待提交列表里。

---

## 快速开始

```bash
jh init                     # ① 建库 + 跑迁移到最新（幂等，可重复执行）
jh --help                   # 看全部命令
```

`jh init` 做完这几件事：建 `data/` `output/` `state/` 目录 → 用 Alembic 升级到最新版本
（11 张表）→ 打开连接并确认 PRAGMA（`journal_mode=WAL`、`busy_timeout=5000`、
`foreign_keys=ON`）→ 打印小结。

```bash
# ② 导入简历（解析成结构化数据；同一份内容重复导入不会新建记录）
jh resume add 我的简历.docx
jh resume list
jh resume split            # ★ 帮我拆方向：哪些项目该前置、哪些该弱化、还缺什么技能
                           #   --save 会把建议落成 Markdown

# ③ 录入岗位（★ 主路径：人给链接 → 工具抓详情）
jh job add https://example.com/job/123
jh job add --file jobs.txt      # 批量（一行一个 URL，# 开头是注释）
jh job add --manual             # 手动粘贴 JD（网络不通也能用，Ctrl+Z/Ctrl+D 结束）
jh job list
jh job show 1 --jd              # 看完整字段与 JD 正文
jh job skip 1                   # 标为"不投"（后续再抓到也不会改回）
```

**配置默认简历**（可选但强烈建议）：在 `.env` 里写一行就不用每次输路径 ——

```
JH_DEFAULT_RESUME_PATH=D:/path/to/你的简历.docx
```

```bash
# ④ 两级匹配打分（P3）
jh match run                    # 规则层全量打分 → 只对规则分 Top N 走 LLM 精评
jh match run --no-llm           # 只跑规则层：0 成本、秒出
jh match explain 1              # ★ 看某个岗位的分维度得分明细（调权重靠它）
jh match set 1 --verdict skip   # 人工裁决：我的话比模型大
jh match set 1 --resume 2       # 指定这个岗位用哪份简历

# ⑤ 反向技能缺口（P3）—— 告诉我"该补什么"，而不是"这个岗位缺什么"
jh gap                          # 跨所有岗位聚合：某技能出现在 78% 的 JD 里而我没有
jh gap -n 10 --min-ratio 0.3    # 只看出现率 ≥30% 的前 10 项

# ⑥ 今日待投清单（P4）—— 出清单 + 生成开场白 + 一键复制 + 打标
jh today                        # 按分数排序出清单，为每条生成开场白（完整版 + 精简版）
jh today --limit 5              # 只看前 5 条
jh today --copy 1               # ★ 把第 1 条的开场白复制到剪贴板（--short 复制精简版）
jh today --check                # 今天已投 X / 上限 N
jh today --export md            # 导出到 output/（md 或 csv，只写新文件）

# ⑦ 零手打跟进（P4.5）—— 一条命令处理一整轮，不用敲 20 次
jh apply 1 --status applied     # 投完打标（序号与 jh today 一致）
jh apply 3=applied 7=replied    # ★ 批量打标：一次搞定一整轮
jh apply "#12=replied"          # 按 job_id 打标（最不会认错，id 见 --pending）
jh apply "字节" --status viewed  # ★ 模糊匹配公司/职位名；命中多条会列出来让你选
jh apply --pending              # 看待处理清单：还没投的 + 投后 3/7/14 天没动静的

# ⑧ 定制简历（P5）—— brief 不花钱；改写建议逐条审核，保真由代码强制
jh tailor brief 1               # ★ 重点讲哪 3 个项目、突出哪 2 个技能、要避开什么（不调模型）
jh tailor analyze 1             # 生成字段级改写建议（调模型；逐条过 FidelityGuard）
jh tailor list                  # 看历史版本
jh tailor diff 3                # 看某版建议的前后对比（原 → 改 + 理由）
jh tailor approve 3 --all       # 逐条批准（approve 前会**再校验一遍**）
jh tailor export 1              # 导出 docx（★ 只写新文件，原简历 hash 不变）

# ⑨ 面试与问题（P6）—— 把"记录工具"变成"准备工具"
jh interview add --job 3 --round 一面 --at "2026-09-11 14:00" --mode 视频
jh interview list --upcoming    # 接下来要面什么
jh question predict 3           # ★ 按 JD 猜 5 道可能被问的题（source=predicted）
jh question add --interview 2 --q "..." --rating bad --category "Redis 缓存" --weak
jh weak                         # ★ 该补什么：按知识点聚合、按"没答上来次数"排序
jh apply --timeline 3           # 这单一路怎么走过来的（读 append-only 事件流）

# ⑩ 笔试 / Offer / 追问（P6）
jh assessment add --job 3 --kind 笔试 --deadline "2026-09-11 23:59"
jh assessment list --upcoming
jh offer add --job 3 --salary "20-25K" --salary-month 22000 --months 15 --deadline "2026-09-20"
jh offer compare                # ★ 年包 = 月薪 × 月数，横向比（15 薪 vs 13 薪差 5 万/年）
jh followup 3 --copy            # ★ 追问话术（投了 N 天没动静）；本工具不会替你发送

# ⑪ 复盘：统计 / 周报 / 日历（P6）
jh stats                        # 平台转化率 / 开场白变体 / 方向回音（只记录，不宣称显著性）
jh report                       # 求职周报 Markdown（只写新文件）
jh cal export                   # 面试 + 笔试/Offer 截止导出 .ics（给自己手机日历提醒）
```

**MVP 到此完成** —— 从零到能用的完整流程就是上面①~⑪：

```
jh init → jh resume add → jh job add <链接> → jh match run → jh gap
       → jh today → jh today --copy N → 去招聘 App 粘贴投递 → jh apply N=applied
       → 想为某个岗位定制简历：jh tailor brief N → analyze N → approve → export
       → 约上面：jh interview add → 面完 jh question add（--weak 标答崩的）
       → 收到笔试/Offer：jh assessment add / jh offer add → jh offer compare
       → 每周复盘：jh stats / jh report；手机提醒：jh cal export
       → 之后每天随便敲任何一条 jh 命令，顶部都会告诉你"有几条该跟进了"
```

### 线索消费端（P7）：导入快照 + 本机线索页

线索的**采集**在另一个仓库（独立部署 `projects/leadhunter`，命令 `lh`），本仓库只负责**消费**：

```bash
uv sync --extra web                      # 只有 jh leads serve 需要（import 只用 pydantic）

cd web                                   # ★ 前端产物不进仓库，第一次要先构建
npm ci
npm run build
cd ..

jh leads import leads.json --dry-run     # ★ 先看"会改什么"：一个字都不写库
jh leads import leads.json               # 幂等：同一份文件导两次 → 新增 0 条、更新 N 条
jh leads serve                           # 打开 http://127.0.0.1:8787（只读 + 标记投递）
```

两件必须知道的事：

- **别用 `--host 0.0.0.0`**：线索页默认只监听 `127.0.0.1`，而且**没有鉴权** ——
  绑到 `0.0.0.0` 等于把线索（公司名、原始那句话、备注里可能有的邮箱/微信）给同一个
  Wi-Fi 下的人看。理由写在 [docs/COMPLIANCE.md](docs/COMPLIANCE.md) §5.6。
- **标记投递是人工点击**：页面上点一下才记一次，**不做自动投递、不做批量**（合规红线）。

细节（`--dry-run` 的真实输出、页面没构建时怎么办、导入是不是幂等）见
[docs/install.md §9](docs/install.md#9-p7-消费端导入线索快照--打开本机线索页)。

> **顶部提醒**（P4.5）挂在 Typer 全局 callback 上，所以**任何** `jh` 命令开头都会显示：
> ```
> 📌 待处理：投后 3 天未跟进 3 条 ｜ 投后 7 天无动静 2 条 ｜ 投后 14 天建议放弃 1 条
> （jh apply --pending 看待处理清单并批量打标）
> ```
> 没有待处理事项时**一个字都不打印**（否则第三天你就会把它关掉，然后连真有提醒时也看不见）。
> `--no-remind` 或 `config.yaml` 的 `show_reminders: false` 可以关掉。

打分口径（权重都在 `config.yaml` 的 `match_weights` 里，可调）：

| 维度 | 默认权重 | 怎么算 |
|---|---|---|
| 岗位方向 | 30% | JD 方向 vs 简历方向；方向不同但核心技能对得上 → 可跨投 |
| 硬条件 | 30% | 经验年限 / 学历 / 城市 / 实习与否，四项等权 |
| 技能交集 | 40% | JD 抽到的技能里简历命中了多少（覆盖率） |

> ★ 硬条件不满足是**大幅降分，不是直接淘汰** —— 应届生投"3 年以下"的岗位很常见，
> 直接淘汰等于静默丢掉机会。
> ★ 判不出来的维度（比如 JD 太短、一个技能词都没抽到）会把权重**让给**其余维度，
> 而不是给 0 分；`jh match explain` 里会写明 `redistributed`。

> 当前进度：**P0–P6 + P9 已完成，P7 的消费端（`jh leads import` / `jh leads serve`）也已落地** ——
> `jh --help` 里列出的命令现在都是真能跑的，本仓库**没有占位命令**了。
> 线索的**采集端**是另一个独立部署（`projects/leadhunter`，命令 `lh`），不在本仓库；
> P8 的其余页面（今日清单 / 看板 / 面试问题 / 统计）仍未做 —— 线索页是 P8 的第一次落地。

### 转化率为什么可信（P6 的统计口径）

`jh stats` / `jh report` 只做一件事：**把"至少走到过哪一步"数出来**。

| 口径 | 怎么算 | 为什么这么定 |
|---|---|---|
| 投递数 | 有 `applied_at` 的投递 | `pending`（还没投）不算，否则分母被灌水 |
| 回复率 | "至少走到过 `replied`" / 投递数 | ★ 按**当前状态**算会系统性低估：现在被拒的那条，可能走到过二面 |
| 约面率 | "至少走到过 `assessment`" / 投递数 | 笔试/测评邀请也是"对方要我了" |
| 每个比率 | 都带分子/分母，如 `25.0%（6/24）` | 只有百分比时，`100%` 是 1/1 还是 20/20 看不出来 —— 而这决定它是不是噪声 |

**只记录对比，不宣称统计显著性**：一个秋招可能就 100 次投递，分到两个开场白变体各 50 次，
回复事件可能只有个位数。所以工具只打印数字与样本量，样本 < 10 条时直接提示"别据此下结论"，
**不会**生成"建议使用变体 A"这类伪结论。

事件流 `application_events` 是 **append-only** 的（只追加、不改不删）—— 这正是上面这些
口径能长期复现的原因：三个月后回头算转化率，结果和当时一致。

### 定制简历为什么"改了但不编"（P5 的 FidelityGuard）

改写建议**只由模型提出、由代码判定、由人批准**：

```
模型：这条句子可以改得更好
代码：这个改法有没有编造？（FidelityGuard.check(原文, 改写)）
人：  逐条点头（jh tailor approve）
```

五项检查全部是**确定性的**（不依赖模型，`300%` / `10个模块` / `2024年` / `Kubernetes` 都拦得住）：

| 检查项 | 拦什么 |
|---|---|
| 命名实体白名单 | 生成文本里的公司/学校名必须在原简历出现过 |
| 数字守恒 | 数字**连同单位**要有出处（"从 300ms 降到 80ms"不许改成"提升 300%"）；"3 个模块 → 10 个模块"直接拦 |
| 时间守恒 | 年份/月份不许新增或修改（`2026.03` 不能变成 `2026 年 3 月-至今`） |
| 技能词表 | 简历技能表 + `learning_whitelist`（带"在学习"标注）之外的技能词不许出现 |
| 长度上限 | 改写不是扩写（默认不超过原文 150%，短句另有绝对余量） |

**在【生成】【approve】【export】三处强制调用**，违规即拒绝写入并说明原因；
被测住的是**恶意 fixture**（手写一段编造文本断言被拦），
不是"调真实 LLM 再断言它没编"——后者测的是模型心情，只会变成 flaky 测试被删掉。

导出**永远写新文件**，并校验原简历的 sha256 未变 —— 原文件就是你的回滚点。

### 开场白为什么"沾边且不编造"

生成出来的每一句话都要过**四道代码闸门**（不是靠提示词祈祷）：

| 闸门 | 拦什么 |
|---|---|
| 字数 | 完整版 80-150 字、精简版 ≤60 字（BOSS 有输入框限制） |
| 禁用词 | `greeting_banned_words`：贵司 / 期待 / 三生有幸… 套话一眼就露馅 |
| **保真** | 只能提简历里**真实存在**的技能；`300%`、`10个模块`、`3年` 这类量化声称必须有出处；不能凭空升学历 |
| 沾边 | 必须出现 `JD 关键词 ∩ 简历技能` 里的词 |

任何一道不过 → 那一版**降级到确定性模板**（保真、保字数、保沾边），
并在清单下写明降级原因。**宁可给一句普通的真话，也不给一句漂亮但编造的话。**

### 合规边界（本工具的"只做什么"）

- `jh job add <url>` **只读取**页面内容：不点投递、不点赞、不私信，不做任何写操作；
  不绕过验证码/风控/签名；请求之间有随机延时；失败即跳过，不重试轰炸。
- 自动搜索翻页**不在主线**（BOSS 有 `__zp_stoken__` 签名 + 字体反爬）。
  「刷到岗位 → 存下来打分」才是被支持的用法。
- 导出一律**写新文件**；`jh resume split` 只给建议，**不改动你的简历文件**。

---

## 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P0** | 骨架 + 工程底座（uv 锁 / Alembic / WAL+外键 / UTC / 强制 UTF-8 / 日志脱敏 / CI 双平台） | ✅ |
| **P1** | 简历导入解析 + `jh resume split`（帮我拆方向） | ✅ |
| **P2** | ⭐ `jh job add <url｜--file｜--manual>` 主路径 + 三层去重 | ✅ |
| **P3** | ⭐ 共享层 + 两级匹配 + `jh match explain` + `jh gap` 反向缺口分析 | ✅ |
| **P4** | ⭐ 待投清单 + 额度 + 开场白（完整/精简 + 防编造）+ 导出 + 投递打标 | ✅ |
| **P4.5** | ⭐ 零手打跟进（批量打标 + 模糊匹配 + 顶部固定提醒）→ **★ MVP 完成** | ✅ |
| **P5** | AI 定制简历（`tailor brief` → 完整版 + **FidelityGuard** 代码强制不编造） | ✅ |
| **P6** | 看板时间轴 + 面试 + 问题记录 + 笔试/Offer + 统计 + 周报 + `.ics` | ✅ |
| **P9** | 开源收尾（README 定稿 / `jh demo` 独立库 / LICENSE / **COMPLIANCE.md** / CI coverage / tag） | ✅ |
| P7 | 线索：**消费端**（`jh leads import` 幂等导入 JSON 快照 + `jh leads serve` 本机线索页）已落地；**采集端独立部署** `projects/leadhunter`（命令 `lh`），采集命令不在本仓库 | ✅ |
| P8 | Web 界面（其余页面移出主线，可延后）—— ★ 线索页（`jh leads serve`）已作为 P8 的第一次落地 | ⬜ |

> **当前状态**：P0–P6 + P9 已完成，P7 消费端已落地（`jh --version` 打印 `phase P9`）。
> 本仓库**只有消费端**：`jh leads import`（导快照）+ `jh leads serve`（看线索 / 标记投递）。
> **采集类命令（`add` / `probe` / `capture` / `export`）不在这里**，它们在独立部署的
> `projects/leadhunter`（命令 `lh`）里 —— 所以 README 与 `docs/COMPLIANCE.md` 都不会声称
> 本仓库"支持 App 线索采集"（它连模拟器依赖都没有：`src/` 里没有 `import uiautomator2`）。

---

## 常见问题（速查）

| 现象 | 原因 | 怎么办 |
|---|---|---|
| `jh: command not found` | 没激活虚拟环境 / 没装 | 见 [docs/install.md](docs/install.md) 第 1–2 节 |
| 命令跑了但"未配置 LLM" | `.env` 里没填 `DEEPSEEK_API_KEY`（或不在当前工作目录） | **这是设计行为**：只用规则分，功能照常。填 Key 见 [docs/config.md](docs/config.md) §7 |
| Windows 中文乱码 | 裸 `open()` 用了 cp936 | 仓库已强制 UTF-8；自己写脚本时显式 `encoding="utf-8"` |
| 数据库没初始化 | 还没跑过 `jh init` | `jh init`（幂等，可重复跑） |
| 想让别人看效果但没 Key | —— | `jh demo`（离线假数据，不花钱、不碰真库） |
| 定位不到库 / 想把库搬到别的盘 | 路径解析 | 设 `JH_DATA_DIR`（见 [docs/config.md](docs/config.md) §8） |
| 简历是 `.doc` / `.pdf` | 不支持解析 | 用 Word 另存为 `.docx`，或复制成 `.txt` |
| 导出 docx 报缺 python-docx | 没装 `[docx]` extra | `uv sync --extra docx`，或 `--format md` |
| `jh leads serve` 报"缺少 Web 依赖" | 没装 `[web]` extra（`jh leads import` 不需要它） | `uv sync --extra web`（或 `pip install -e ".[web]"`） |
| 打开线索页看到"页面还没构建" | 前端产物 `web/dist` 不存在（**不进仓库，是正常状态**） | 在 `web/` 里 `npm ci && npm run build`；只调接口可以用 `/api/leads` |
| 担心隐私 / 是否违规 | —— | 见 [docs/COMPLIANCE.md](docs/COMPLIANCE.md)（含实测扫描结果） |

更多（含 8 条以上）见 [docs/install.md](docs/install.md#常见问题)。

---

## 工程底座（P0 就建好，避免后期返工）

| 项 | 做法 | 为什么 |
|---|---|---|
| 依赖锁 | `uv` + 提交 `uv.lock` | 可复现，杜绝"我这儿能跑" |
| 迁移 | Alembic，`jh init` 自动 `upgrade head` | 以后加字段不删库 → 不丢真实投递记录 |
| SQLite PRAGMA | 每连接 `journal_mode=WAL` + `busy_timeout=5000` + `foreign_keys=ON` | SQLite **默认不开外键**；WAL 让读写不互斥 |
| 时间 | 只存 **UTC aware**，展示转 `Asia/Shanghai` | 避免已弃用的 `utcnow()` 与跨时区计算错误 |
| 编码 | 统一 `utils/io.py` 强制 `encoding="utf-8"` + ruff `PLW1514` | Windows 裸 `open()` 默认 cp936，读中文简历必崩 |
| 日志 | `Filter` 自动打码手机号/邮箱；**不打印简历原文** | 隐私红线，用测试守住而不是靠自觉 |
| 测试分层 | markers `live` / `emulator`，默认排除 | CI 不依赖网络/模拟器 |
| CI | ubuntu **+ windows** 双平台矩阵 | 主战场是 Windows，只跑 Linux 是虚假安全感 |

---

## 项目结构

```
src/jobhunter/
├── cli/          Typer 命令层（只解析参数 + 渲染，不含业务逻辑）
├── services/     业务层：resume_svc（简历解析/拆分）
│                 job_svc（岗位入库 + 三层去重）
│                 match_svc（★ 共享中间层：JD↔简历规则层打分 + gap 聚合）
│                 match_llm_svc（LLM 精评：并发 + 缓存 + 重试降级）
│                 greeting_svc（开场白：提示词 + 四道闸门 + 模板兜底）
│                 fidelity（防编造校验，P5 的 FidelityGuard 复用）
│                 today_svc / apply_svc / export_svc（待投清单 / 投递打标 / 导出）
│                 leads_svc（★ P7 消费端：快照导入（幂等）+ 线索→投递的唯一写入口）
│                 reminder_svc（顶部待处理提醒，挂在全局 callback 上）
├── adapters/     外部世界：manual.py（人给 URL 抓详情，主路径）
│                 + parse_html.py / parse_text.py（抓取与解析分开，见下）
├── llm/          base（Provider 契约）/ deepseek（DeepSeek 实现）/ mock（测试用）
│                 cache（cache_key + llm_cache 读写）/ factory（按配置挑 Provider）
├── storage/      engine / session / PRAGMA / Alembic 调用
├── models/       11 张表的 ORM 定义
├── config/       pydantic-settings 配置（config.yaml + .env）+ direction_profiles.yaml
├── web/          ★ P7 消费端：FastAPI 应用（只搬运 —— 把 services 的结果变 JSON、
│                 把页面上的点击变成一次服务调用；接口契约见 docs/P7-消费端API.md）
└── utils/        io（强制 UTF-8）/ time（UTC）/ logging（脱敏）/ docx（Word 读取）

web/              ★ P7 消费端的前端（React + Vite）：**源码进仓库、构建产物 `web/dist` 不进**
```

**一条值得说明的分层取舍**：抓取（`adapters/manual.py`，需要浏览器与网络）
与解析（`adapters/parse_html.py` / `parse_text.py`，纯函数）是**分开**的。
这样测试只需 mock HTML 就能精确断言解析行为，不需要访问真实招聘站点 ——
把"会随机失败的部分"关在一个很薄的层里（详细理由见 `docs/architecture.md`）。

详细分层职责、每一项技术决定的取舍，见 [`docs/architecture.md`](docs/architecture.md)；
11 张表的设计与索引理由见 [`docs/data_model.md`](docs/data_model.md)；
各阶段实现计划与实测记录（含踩坑清单）见 [`docs/roadmap.md`](docs/roadmap.md)；
**当前阶段交付说明（做了什么 / 已知问题 / 建议重点验证什么）见 [`docs/STATUS.md`](docs/STATUS.md)**；
验证问题的处理台账见 [`docs/ISSUES.md`](docs/ISSUES.md)。

---

## 隐私说明

- 所有数据**只在你本机**，不上传任何服务器（除你主动调用 LLM 时的 JD 文本/简历片段）。
- `.gitignore` 已排除：`.env`、`*.db`、**`*.db-wal`、`*.db-shm`**、`data/`、`output/`、
  `state/`、`*.docx`、`*.pdf`。
  （**为什么连 `.db-wal` 都要忽略**：SQLite 开了 WAL 后，最新数据可能还在 `.db-wal` 里，
  只忽略 `*.db` 等于把真实投递记录提交上去。）
- 日志自动脱敏（手机号/邮箱打码），且**不打印简历原文**。
- 导出简历**只写新文件**，绝不覆盖原件。
- ★ **线索页（`jh leads serve`）只监听 `127.0.0.1`，而且没有鉴权** —— 它就是个"给你自己看的
  本机页面"。所以**不要**用 `--host 0.0.0.0` 把它暴露到局域网：那等于把线索（公司名、
  原始那句话、备注里可能有的邮箱/微信）给同一个 Wi-Fi 下的人看，他们还能替你点"标记投递"。
  「为什么不做鉴权」写在 [docs/COMPLIANCE.md](docs/COMPLIANCE.md) §5.6。

## 开发

```bash
uv run ruff check .                  # lint（含 PLW1514 强制显式编码）
uv run pytest                        # 确定性测试（默认排除 live/emulator）
uv run pytest -m live                # 需要真实网络/LLM 的测试（nightly）
uv run pytest -m emulator            # 需要模拟器/设备的测试（★ 本仓库现在没有这类测试：
                                     #   抓屏在采集端 leadhunter，marker 留着是为了将来）
cd web && npm test                   # 前端纯函数测试（node --test，零额外依赖）
cd web && npm run build              # 构建 web/dist（CI 里也跑这一步；产物不进仓库）
```

## License

MIT（见 LICENSE，P9 补）。**不鼓励**将本工具用于违反平台协议的行为。

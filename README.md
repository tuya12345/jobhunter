# JobHunter · 求职猎手

> 本地优先的**开源求职辅助工具**：抓岗位详情 → 两级匹配打分 → 按 JD 定制开场白 → 跟踪投递全流程。
> 用 Python 3.13 + Typer + SQLite + Playwright + DeepSeek 写成，**代码当学习材料**（详细中文注释 + 讲原理）。

> [!WARNING]
> **⚠️ 合规声明（请先读这段）**
> 1. 本工具是求职**辅助**工具，**不提供自动投递功能**。脚本只做
>    「录入 / 抓取 / 打分 / 写话术 / 出清单」，**最后的投递按钮必须由人点击**。
> 2. 请遵守各招聘平台、社交平台的**用户协议**。本项目**不绕过**验证码、风控、签名机制，
>    也不做自动搜索翻页；频率控制（随机延时 / 翻页上限 / `--dry-run`）默认开启。
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
pip install -e ".[dev]"
jh --help
```

### 可选依赖（按需装，主依赖保持干净）

| 命令 | 用途 | 哪个阶段 |
|---|---|---|
| `uv sync --extra browser` | Playwright 抓岗位详情 | P2 |
| `uv sync --extra docx` | 解析/导出 Word 简历 | P1 / P5 |
| `uv sync --extra llm` | 调用 DeepSeek | P3+ |
| `uv sync --extra emulator` | 安卓模拟器抓屏（技术验证） | P7 |

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
jh apply 1 --status applied     # 投完打标（序号与 jh today 一致；也可 --job <id>）
```

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

> 当前进度：**P0 / P1 / P2 / P3 / P4 已完成**，其余命令仍是**占位**，
> 执行时会明确提示"该功能在 PX 阶段实现"，不会假装成功。

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
| **P4.5** | ⭐ 零手打跟进（批量打标 + 模糊匹配 + 顶部提醒）→ **★ MVP 完成** | ⬜ 下一步 |
| P5 | AI 定制简历（先 `tailor brief`，再完整版 + **FidelityGuard**） | ⬜ |
| P6 | 看板 + 面试 + 问题记录 + 笔试/Offer + 统计 + 报告 + `.ics` | ⬜ |
| P7 | App 线索（降级为技术验证：1 平台 + `probe` + 手动抓屏） | ⬜ |
| P8 | Web 界面（移出主线，可延后） | ⬜ |
| P9 | 开源收尾（README 定稿 / demo 独立库 / LICENSE / COMPLIANCE） | ⬜ |

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
├── adapters/     外部世界：manual.py（人给 URL 抓详情，主路径）
│                 + parse_html.py / parse_text.py（抓取与解析分开，见下）
├── llm/          base（Provider 契约）/ deepseek（DeepSeek 实现）/ mock（测试用）
│                 cache（cache_key + llm_cache 读写）/ factory（按配置挑 Provider）
├── storage/      engine / session / PRAGMA / Alembic 调用
├── models/       11 张表的 ORM 定义
├── config/       pydantic-settings 配置（config.yaml + .env）+ direction_profiles.yaml
└── utils/        io（强制 UTF-8）/ time（UTC）/ logging（脱敏）/ docx（Word 读取）
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

## 开发

```bash
uv run ruff check .                  # lint（含 PLW1514 强制显式编码）
uv run pytest                        # 确定性测试（默认排除 live/emulator）
uv run pytest -m live                # 需要真实网络/LLM 的测试（nightly）
uv run pytest -m emulator            # 需要模拟器的测试（P7）
```

## License

MIT（见 LICENSE，P9 补）。**不鼓励**将本工具用于违反平台协议的行为。

# JobHunter · 架构与目录职责（v0 · 定稿设计，实现随阶段推进）

> 本文是 **提示词 0 的产出①（项目目录结构）**，也是后续所有阶段的"地图"。
> 状态标记：[P0] 本阶段落地 · [P1+] 后续阶段落地 · [占位] 本阶段只建空壳

---

## 0. 一句话定位

**JobHunter（求职猎手）**：本地优先的开源求职**辅助**工具。
抓岗位详情 → 两级匹配打分 → 判断"能不能投" → 生成沾边的开场白 → 跟踪投递/笔试/面试/Offer → 渠道复盘。

**三条不可逾越的红线**（写进 README，且用代码守住，不靠提示词祈祷）：

1. **不做全自动投递** —— 脚本止于"录入 / 抓取 / 打分 / 写话术 / 出清单"，**投递按钮由人点**。
2. **不做自动搜索翻页** —— BOSS 有 `__zp_stoken__` 签名 + 字体反爬。主路径 = **人给链接 → 工具抓详情**；自动搜索降级为"可选适配器 + 必须先过 spike"。
3. **AI 改简历不得编造** —— 由代码级 `FidelityGuard`（实体白名单 + 数字守恒 + 时间守恒 + 技能词表 + 长度上限）在【生成】【审核】【导出】三处强制校验，违规拒绝写入。

---

## 1. 架构主线：共享「JD 解析 + 简历匹配」中间层

三个核心痛点（**简历不匹配 / 开场白不沾边 / 逐岗改太累**）本质是同一个病：**没从 JD 出发**。

所以设一个共享中间层，**只算一次，三处复用**：

```
                    JD 文本 + 简历（结构化）
                              │
                              ▼
        ┌─────────── services/match_svc.py ───────────┐
        │  match_jd_resume(jd_text, resume)            │
        │   ├─ jd_keywords[]        技能/要求（词表优先）│
        │   ├─ direction            AI Agent/前端/其他  │
        │   ├─ hard_requirements    经验/学历/城市/应届  │
        │   ├─ skill_overlap        {matched[], missing[]}
        │   ├─ rule_score / rule_detail                 │
        │   └─ matched_resume_id    推荐用哪份简历        │
        └───────────────────┬─────────────────────────┘
                            │  产物 jd_keywords_json 落库到 jobs 表
        ┌───────────────────┼─────────────────────────┐
        ▼                   ▼                         ▼
  ① 匹配打分          ② 定制简历                ③ 生成开场白
  （能不能投）        （怎么改）                （怎么开口）
   rule + LLM 两级    tailor brief/analyze      greeting 完整版+60字精简版
                     + FidelityGuard 强制       + 禁用词表 + 变体 A/B
```

**为什么这样分层（关键取舍）**：
- **必然沾边**：开场白引用的是同一份 `jd_keywords_json`，而不是重新"猜"一遍 JD，所以不会跑偏。
- **省成本**：JD 解析 + 关键词抽取只做一次，三处复用；LLM 缓存 key 又进一步避免重复调用。
- **可解释**：分维度得分落 `rule_detail_json`，`jh match explain <job_id>` 能打印明细 → 调权重有依据（评审原话："否则调权重无法进行"）。
- **两个"消除幻觉"设计**：`jh gap`（反向告诉你**该补什么技能**，而不是逐岗说缺什么）+ `jh question predict`（用 JD 预测面试题）。这是把"记录工具"变成"决策工具"。

---

## 2. 分层图

```
┌──────────────────────── CLI（Typer, jobhunter/cli/）─────────────────────────┐
│ jh init / resume(add|list|split) / job(add|list|skip)                        │
│ match(run|explain|set) / gap / today / apply / interview / question / weak   │
│ stats / report / tailor(brief|analyze|diff|approve|export) / followup        │
│ leads(import|serve) / offer(add|compare) / assessment(add|list)              │
│ cal(export) / demo                                                           │
│ ★ 全局 callback：每条命令开头打印"待处理提醒"（P0 占位，P4.5 实现）             │
└───────────────────────────────────┬─────────────────────────────────────────┘
                                    │  只做参数解析 + 展示；不含业务逻辑
┌───────────────────────────────────┴─────────────────────────────────────────┐
│                            Services（纯业务，可被 CLI / Web 复用）             │
│ resume_svc │ job_svc │ match_svc(★核心) │ gap_svc │ tailor_svc │             │
│ greeting_svc │ track_svc │ leads_svc │ report_svc │ ★ fidelity(FidelityGuard)│
└──────┬──────────────────────┬────────────────────────┬────────────────────┘
       │                      │                        │
┌──────┴────────┐   ┌─────────┴──────────┐   ┌─────────┴─────────────────────┐
│  Adapters     │   │  LLM Provider      │   │  Storage                      │
│ manual.py ★主 │   │ deepseek.py        │   │ SQLAlchemy 2.x + Alembic      │
│ boss.py(可选) │   │ openai.py / mock   │   │ SQLite: WAL + FK + busy_timeout│
│ app_*.py(旧)  │   │ + llm_cache 表      │   │ models/ = ORM 定义             │
└───────────────┘   └────────────────────┘   └───────────────────────────────┘
       ★ manual.py = 人给 URL 抓详情（主路径）
       ⚠️ `app_*.py` 是**旧架构**的模拟器适配器：P7 拆分后采集端搬去了独立部署
          `leadhunter`，本仓库不再有 App 抓取（`[emulator]` extra 只是历史遗留声明）。
```

**分层规则（硬约束）**：
- `cli/` 只做参数解析、调用 service、rich 渲染。**禁止**在 CLI 里写业务逻辑。
- `services/` 不直接依赖 Typer/rich，返回**领域对象或 pydantic 模型** → 将来 P8 的 FastAPI 能原样复用。
- `adapters/` 负责"外部世界"（网页/App），**统一接口 `JobSource`**；抓取失败必须能降级。
- `llm/` 只负责"把 prompt 送出去、把结构化结果拿回来"，**不管业务语义**。
- `storage/` 只负责连接、会话、PRAGMA、迁移；`models/` 只负责 ORM 定义。
- `utils/` 无业务依赖（io/time/logging/text），可被任何层引用。

---

## 3. 目录结构（本阶段落地物）

```
projects/jobhunter/
├── .github/workflows/ci.yml          [P0] ubuntu + windows 双平台矩阵
├── .gitignore                        [P0] 含 *.db-wal / *.db-shm（隐私红线）
├── .env.example                      [P0] 密钥占位（真实 .env 永不入库）
├── README.md                         [P0] 从 P0 就维护（含合规声明）
├── LICENSE                           [P9] MIT
├── COMPLIANCE.md                     [P9] 合规自检清单
├── pyproject.toml                    [P0] uv 工程 + 依赖 + ruff + pytest 配置
├── uv.lock                           [P0] 锁依赖，可复现
├── config.yaml                       [P0] 个人配置默认值（关键词/城市/分数线…）
├── alembic.ini                       [P0] 迁移配置
├── data/                             [P0] 运行时数据目录（gitignore；库文件在此）
│   └── （只有 jobhunter.db）          ⚠️ 原来的 data/leads/（抓屏产物）已随采集端搬走
├── output/                           [P0] 导出产物（gitignore，只写新文件）
├── state/                            [P0] 运行时状态（gitignore；P7 的 CDP 专用 profile 也在这里）
├── docs/
│   ├── architecture.md               [P0] ★ 本文
│   ├── data_model.md                 [P0] ★ 全表 schema + 索引理由 + Alembic 方案
│   ├── roadmap.md                    [P0] ★ P0 实现计划（骨架优先）
│   ├── config.md                     [P9] config.yaml 逐字段说明
│   ├── install.md                    [P9] 安装教程（含模拟器那步）
│   └── emulator.md                   [旧] 模拟器/ADB/uiautomator2 教程（采集端已搬走，留作参考）
├── alembic/
│   ├── env.py                        [P0] 读 settings 的 DB URL，target_metadata = Base.metadata
│   ├── script.py.mako                [P0]
│   └── versions/
│       └── 0001_initial_schema.py    [P0] ★ v3 全部表的初始迁移
├── spikes/
│   └── boss_search_spike.py          [P2] 自动搜索可行性 spike（2h，可选）
├── prompts/
│   ├── tailor.md                     [P5] ★ 七要素提示词（版本号写在文件里，见"提示词版本纪律"）
│   ├── question.md                   [P6] ★ JD 预测面试题
│   └── followup.md                   [P6] ★ 追问话术
│                                     ⚠️ match / greeting 的提示词目前仍内联在各自 svc 里
│                                        （`match_llm_svc.SYSTEM_PROMPT` / `greeting_svc.SYSTEM_PROMPT`）；
│                                        要不要一起搬到 prompts/ 记在 P9 收尾清单里
├── src/jobhunter/                    ★ src 布局：强制安装后导入，避免"本地目录假通过"
│   ├── __init__.py
│   ├── __main__.py                   [P0] python -m jobhunter 入口
│   ├── cli/
│   │   ├── __init__.py
│   │   ├── app.py                    [P0] ★ 组装 Typer app + 全局 callback（提醒占位）
│   │   ├── common.py                 [P0] 公共参数/输出辅助（table/panel/json）
│   │   └── commands/
│   │       ├── __init__.py           [P0] 命令注册表（集中登记，避免 app.py 膨胀）
│   │       ├── init_cmd.py           [P0] ★ 唯一真正实现的命令
│   │       ├── resume_cmd.py         [P0 占位 / P1 实现]
│   │       ├── job_cmd.py            [P0 占位 / P2 实现]
│   │       ├── match_cmd.py          [P0 占位 / P3 实现]
│   │       ├── gap_cmd.py            [P0 占位 / P3 实现]
│   │       ├── today_cmd.py          [P0 占位 / P4 实现]
│   │       ├── apply_cmd.py          [P0 占位 / P4+P4.5 实现] ★ 单命令应用
│   │       ├── tailor_cmd.py         [P0 占位 / P5 实现] ★ brief/analyze/list/diff/approve/export
│   │       ├── interview_cmd.py      [P0 占位 / P6 实现]
│   │       ├── question_cmd.py       [P0 占位 / P6 实现]
│   │       ├── assessment_cmd.py     [P0 占位 / P6 实现]
│   │       ├── offer_cmd.py          [P0 占位 / P6 实现]
│   │       ├── weak_cmd.py           [P0 占位 / P6 实现]
│   │       ├── stats_cmd.py          [P0 占位 / P6 实现]
│   │       ├── report_cmd.py         [P0 占位 / P6 实现]
│   │       ├── followup_cmd.py       [P0 占位 / P6 实现]
│   │       ├── leads_cmd.py          [P0 占位 / P7 消费端实现：import + serve]
│   │       └── demo_cmd.py           [P0 占位 / P9 实现]
│   ├── services/
│   │   ├── __init__.py
│   │   ├── reminder_svc.py           [P0] ★ 顶部提醒统计（P0 先返回空结构）
│   │   ├── resume_svc.py             [P1]
│   │   ├── job_svc.py                [P2]
│   │   ├── match_svc.py              [P3] ★ 核心
│   │   ├── gap_svc.py                [P3]
│   │   ├── greeting_svc.py           [P4]
│   │   ├── apply_svc.py              [P4/P4.5] 投递状态与事件流
│   │   ├── today_svc.py              [P4] 待投清单
│   │   ├── export_svc.py             [P4] 导出（只写新文件）
│   │   ├── tailor_svc.py             [P5] ★ 定制简历（brief/analyze/approve/export）
│   │   ├── fidelity.py               [P4/P5] ★ FidelityGuard（防编造，三处强制）
│   │   ├── interview_svc.py          [P6] 面试场次
│   │   ├── question_svc.py           [P6] 面试问题（记录 + JD 预测）
│   │   ├── assessment_svc.py         [P6] 笔试/测评 + 截止提醒
│   │   ├── offer_svc.py              [P6] Offer 登记与年包比较
│   │   ├── weak_svc.py               [P6] 薄弱点聚合（按没答上来次数）
│   │   ├── track_svc.py              [P6] 投递时间轴（读 append-only 事件流）
│   │   ├── stats_svc.py              [P6] 转化率口径（"至少走到过"）
│   │   ├── report_svc.py             [P6] 周报 Markdown
│   │   ├── followup_svc.py           [P6] 追问话术（只生成，不发送）
│   │   ├── cal_svc.py                [P6] .ics 导出（面试/笔试/Offer 截止）
│   │   └── leads_svc.py              [P7] 消费端：快照导入 + 线索分组/三态 + 标记投递
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── base.py                   [P0] ★ JobSource / JobDetail 契约（定死接口）
│   │   ├── manual.py                 [P2] ★ 主路径：人给 URL 抓详情（含 --cdp 连用户自己的浏览器）
│   │   ├── cdp.py                    [P7] 本地 CDP：找/起专用 profile 的浏览器（只读那一个链接）
│   │   ├── boss.py                   [P2 可选] 自动搜索（spike 通过才启用）
│   │   └── app/                      [旧] 模拟器侧（uiautomator2）—— 采集端已搬去 `leadhunter`
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── base.py                   [P0] ★ LLMProvider 抽象
│   │   ├── deepseek.py               [P3] DeepSeek（OpenAI 兼容）
│   │   ├── mock.py                   [P0] ★ 测试用假 Provider（CI 不联网）
│   │   ├── cache.py                  [P3] llm_cache 读写 + cache_key 计算
│   │   ├── safety.py                 [P4.5] ★ wrap_delimited（不可信输入的中和）
│   │   └── prompts.py                [P6] ★ prompts/*.md 的加载 + 版本纪律（三处共用）
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── db.py                     [P0] ★ engine/session + PRAGMA(WAL/FK/busy_timeout)
│   │   └── migrate.py                [P0] ★ jh init → alembic upgrade head（编程式）
│   ├── models/
│   │   ├── __init__.py               [P0] 汇总导出 + Base
│   │   ├── base.py                   [P0] ★ DeclarativeBase + UTCDateTime + 命名约定
│   │   ├── resume.py                 [P0] Resume / ResumeVersion
│   │   ├── job.py                    [P0] Job
│   │   ├── application.py            [P0] Application / ApplicationEvent
│   │   ├── interview.py              [P0] Interview / InterviewQuestion
│   │   ├── assessment.py             [P0] Assessment
│   │   ├── offer.py                  [P0] Offer
│   │   ├── company.py                [P0] Company
│   │   └── llm_cache.py              [P0] LLMCache
│   ├── config/
│   │   ├── __init__.py
│   │   ├── settings.py               [P0] ★ pydantic-settings：config.yaml + .env
│   │   ├── paths.py                  [P0] 统一路径解析（data/output/state）
│   │   └── skill_aliases.yaml        [P3] 技能归一化词表
│   └── utils/
│       ├── __init__.py
│       ├── io.py                     [P0] ★ 强制 UTF-8 读写（Windows cp936 坑）
│       ├── time.py                   [P0] ★ UTC aware 存 / Asia/Shanghai 展示
│       ├── logging.py                [P0] ★ 脱敏 Filter（手机号/邮箱打码）
│       ├── text.py                   [P3] 归一化/词边界匹配（Java ≠ JavaScript）
│       └── html.py                   [P2] JD 正文抽文本
└── tests/
    ├── conftest.py                   [P0] ★ 临时库 fixture（绝不碰真库）
    ├── fixtures/                     [P0] 假数据（真实信息一律不得进仓库）
    └── unit/
        ├── test_smoke.py             [P0] ★ 建库 + 建表 + 迁移到位
        ├── test_logging_redaction.py [P0] ★ 日志不泄露手机号/邮箱
        ├── test_io_encoding.py       [P0] ★ 强制 UTF-8（含中文/emoji 往返）
        ├── test_time_utc.py          [P0] ★ UTC 存 / 上海展示往返
        └── test_cli_skeleton.py      [P0] ★ `jh --help` 含全部命令；init 幂等
```

---

## 4. 关键技术决定（P0 定死，后续不返工）

| 决定 | 做法 | 为什么（取舍） |
|---|---|---|
| **src 布局** | 代码放 `src/jobhunter/` | 避免"在仓库根目录能 import 但装完不能"的假通过 |
| **依赖锁** | `uv` + 提交 `uv.lock` | 可复现；评审明确要求 |
| **迁移** | Alembic，`jh init` 自动 `upgrade head` | P0 就上，以后加字段不删库 → 不丢真实投递数据 |
| **SQLite PRAGMA** | 每连接 `journal_mode=WAL` + `busy_timeout=5000` + `foreign_keys=ON` | SQLite 默认**不开外键**；WAL 让读写不互斥，避免 P8 Web 并发写报 database is locked |
| **时间** | 只存 **UTC aware**，展示层转 `Asia/Shanghai` | 不用已弃用的 `utcnow()`；跨时区/夏令时不会错 |
| **编码** | 统一 `utils/io.py` 强制 `encoding="utf-8"`；ruff 开 `PLW1514` | Windows 裸 `open()` 默认 cp936，读中文简历必崩 |
| **日志** | `logging.Filter` 自动打码手机号/邮箱；**任何日志不打印简历原文** | 隐私红线，且要用测试守住（不靠自觉） |
| **测试分层** | pytest markers `live` / `emulator`，CI 跑 `-m "not live and not emulator"` | CI 不依赖网络/模拟器 → 不会三天两头红灯 |
| **CI** | ubuntu-latest + **windows-latest** 矩阵 | 只跑 Ubuntu 是"虚假安全感"：本项目主战场是 Windows |
| **防编造** | `FidelityGuard` 代码强制 + **恶意 fixture** 确定性断言 | 不写"调真实 LLM 断言输出"这种不稳定测试（必被删） |
| **JD 不可信** | JD 文本包标签 + LLM 输出过 pydantic schema + 边界校验 | 防 prompt injection |
| **LLM 缓存 key** | `sha256(prompt_version｜model｜jd_hash｜resume.content_hash)` | **改了简历不会命中旧分**（评审指出的原始 bug） |
| **结构化输出** | 提示词含 "json" + 给 schema 示例 + `max_tokens` 给足；空 content 重试+降级 | DeepSeek JSON 模式的"三件套" |
| **外部依赖隔离** | `playwright` / `python-docx` / `uiautomator2` 拆成 extras；uiautomator2 **钉版本** | 主依赖干净；模拟器装不上不阻塞主干 |

### 依赖分组

| extra / group | 内容 | 谁在用 |
|---|---|---|
| 主依赖 | typer, rich, pydantic, pydantic-settings, SQLAlchemy, alembic, PyYAML, httpx, python-dateutil | 全项目 |
| `[browser]` | playwright | P2 抓详情 / P7 |
| `[docx]` | python-docx | P1 简历解析、P5 导出 |
| `[llm]` | openai（DeepSeek 走 OpenAI 兼容） | P3+ |
| `[emulator]` | uiautomator2（**钉版本**） | P7 技术验证 |
| dev group | pytest, pytest-asyncio, pytest-cov, ruff, mypy | 开发/CI |

---

## 4.5 两个 Typer 使用约束（P0 实测踩出来的，改 CLI 前必读）

这两条都是"程序不报错、只表现得很怪"的类型，所以写进文档并用测试守住
（`tests/unit/test_cli_skeleton.py`）。

### ① `add_typer` 必须显式传 `name`

```python
app.add_typer(command_app, name="today")  # ✅
app.add_typer(command_app)  # ❌ 子命令会被摊平到上一层
```

不传 name 时 Typer **只发一条 UserWarning 就继续跑**，后果是 `jh --help` 里
出现一堆孤立的 `add` / `list` / `export` / `compare`，而 `jh today`、`jh weak`
这些命令**整体消失**。防：注册表 `COMMANDS` 里显式给名字 + 顶层"孤立子命令"断言。

### ② 组回调带位置参数 → 子命令永远无法分派

```python
@app.callback(invoke_without_command=True)
def root(ctx, pairs: list[str] = typer.Argument(None)): ...
```

只要回调声明了位置参数（哪怕 variadic），`jh xxx timeline 5` 里的 `timeline`
会被当成那个位置参数吃掉，**子命令根本不执行**（连 `--help` 都出不来）。

因此本项目对**单动作命令**统一采用"单命令应用"（callback + `invoke_without_command`
+ `no_args_is_help=False`）：

| 命令 | 形态 | 说明 |
|---|---|---|
| `gap` / `today` / `weak` / `stats` / `report` / `followup` / `demo` | 单命令应用 | 只有一个动作，不需要子命令 |
| `apply` | **单命令应用** | ★ 因为必须保住 `jh apply 3=applied 7=replied` 这个位置参数语法（零手打的关键）。代价：原计划的 `apply mark` / `apply timeline` 子命令改为 `jh apply 3 --status applied` 与 `jh apply --timeline <job_id>` |
| `resume` / `job` / `match` / `interview` / `question` / `assessment` / `offer` / `tailor` / `leads` / `cal` | 多子命令组 | 回调**不带**位置参数，子命令正常分派 |

---

## 4.6 P2 现场补充：去重的三层判定与字段白名单

`services/job_svc.py` 里有一条**必须原文照读**的纪律，写在模块顶部：

> 去重命中时**绝不覆盖人工决策字段**。

实现方式是三份显式字段清单（`job_svc.py` 里一一对应，文档口径与代码必须一致）：

| 清单 | 内容 | 规则 |
|---|---|---|
| `REFRESHABLE_FIELDS` | company / title / city / experience / education / is_intern / salary_* / jd_text / jd_hash / jd_fetch_status / dedup_norm_hash | 重复遇到同一岗位时**允许刷新** |
| `FIRST_WINS_FIELDS` | platform_job_id / url / source_keyword | **首次为准**：库里有值就不动，库里为**空**时才补写 |
| `DECISION_FIELDS` | status / match_score / llm_score / rule_score / rule_detail_json / reason_json / matched_resume_id / jd_keywords_json / found_at | **任何情况下都不被去重逻辑覆盖** |

**`url` / `source_keyword` 为什么从「可刷新」挪进「首次为准」**（ISSUE #20 的裁决 A）：

它们是**来源信息**（"这个岗位是从哪找到的"），与 `DECISION_FIELDS` 守的是同一条纪律 ——
后来的那次抓取**并不比第一次更权威**。覆盖的后果不是报错，而是"记忆被篡改"：
同一岗位先是从内推群里的链接录进来、后来又从搜索词刷到，覆盖之后库里只剩后一个渠道，
`jh stats` 复盘"哪个渠道有效"时看到的是一份被改写过的历史。
`platform_job_id` 本来就是这个规则（原来写成循环里的一行特例），三者语义相同，
现在合成一份清单，不必再靠"记得那行特例"。

★ 边界（反向守卫，同样有用例守）：**「首次为准」不等于「永远留空」**。
第一次手动粘贴的 JD 没有 URL、也没写 `--keyword`，第二次带着链接录进来时，
库里那一列是空的 → **补上**；否则一条本来能补全的信息就永久丢了。
规则是"有值不动、空值才补"，而且是**逐字段**判断（已有 `url` 不妨碍补 `source_keyword`）。

四条测试从不同方向守住它（都在 `tests/unit/test_job_svc.py`）：
- `test_dedup_never_touches_decision_fields`：把决策字段写成"不可能由代码推导"的值，
  再跑一次 upsert，断言它们一个都没变、而抓取字段确实刷新了；
- `test_decision_fields_and_refreshable_fields_do_not_overlap`：三份清单**两两不许有交集**
  （元测试 —— 将来有人往可刷新清单里加 `status` 会立刻失败）；
- `test_dedup_keeps_the_first_url_and_source_keyword`：第二次录入不改写已有来源；
- `test_dedup_backfills_url_and_source_keyword_when_first_was_empty` +
  `test_first_wins_is_per_field_not_all_or_nothing`：第一次为空时必须补上（逐字段）。

### 去重的三层判定（② 与 ④ 都是真实页面验收后补的）

```
① platform + platform_job_id 精确命中          → 同一岗位（最可靠）
② URL 完全相同                                → 同一岗位（★ 验收后补）
③ dedup_norm_hash 命中（公司+职位+城市 归一化） → 同一岗位（兜底）
④ 否决：两个**都非空且不同**的平台 ID 之间，即使 ③ 命中也不合并
```

**② 为什么必须补**（实测：同一个链接加三次 → 库里 3 条重复）：
公司自建招聘站（美团这类）的页面**根本不写公司名**，于是 ③ 的三件套缺一件、
直接放弃匹配；而岗位 ID 又藏在查询参数里（`?jobUnionId=…`），① 也抠不到。
"URL 一模一样"是极强的证据 —— 同一个链接第二次打开还是同一个岗位。
（同时给 `official` 平台补了参数式 URL 模式，让 ① 也能覆盖这类站点。）

**④ 为什么必须有**（实测踩出来的真实缺陷）：同一家公司在同一城市挂两个相似标题的岗位
（`AI Agent 开发工程师` 与 `AI Agent 开发工程师（急招）`），平台 ID 是 job-001 / job-002。
没有 ④ 时 ③ 会命中，把两条**并成一条**：用户再也看不到第二个岗位，
job-002 的 JD / 薪资还会覆盖到 job-001 那条记录上（信息串味），
而投递记录只认 job-001，事后完全对不上账。
（`platform_job_id` 现在另有 `FIRST_WINS_FIELDS` 兜着、不至于被改写身份，
但"两个岗位并成一条"这个后果只能靠 ④ 挡 —— 所以 ④ 不能因为有了那层保护就删。）

### 抓取质量的独立交叉验证（用 harness 的浏览器工具）

抓取的准确性没法靠单测证明（要真实站点），所以用 **`browser-automation` 技能**做了对照：
浏览器打开同一个岗位页 → 取渲染后的真实文本 → 与库里存的 JD **逐行比对**。

实测结论（美团 AI Agent 岗位）：标题/更新时间/工作地点/部门完全一致，
11 项关键内容全部命中，导航（首页/LongCat人才招聘/北斗计划…）与页脚
（版权所有/京ICP备…）**零残留** —— 这比"看一眼觉得对"硬得多。

### 归一化 hash 那一层的"宁漏不错"取舍

（这一层按现在的编号是**第三层**，②是 URL、①是 platform_job_id。
标题刻意不写层号 —— 编号已经变过一次，下次再变就不用改标题了。）

`compute_dedup_norm_hash` 在**三个字段缺任何一个**时返回 `None`（放弃模糊匹配）：

- **错合并**的代价：两个岗位的记录被并成一个 → 用户看到"我没投过这个岗位却显示已投"；
- **漏合并**的代价：清单里多一条相似记录 → 手动 `jh job skip` 一下即可。

所以选了保守的一方。归一化只穿透"HR 改标题不改岗位"那类噪音
（大小写/空格/全角/薪资/急招之类的修饰词/公司的城市括号与"有限公司"后缀），
而**刻意保留** `（实习）`（实习与全职是两个岗位，合并会造成错投）。

---

## 5. 三层防线的落地位置（面试可讲）
| 防线 | 代码位置 | 测试位置 |
|---|---|---|
| 不做全自动投递 | `adapters/manual.py` 只读不写；CLI 无"提交"动作 | 代码评审 + COMPLIANCE.md 自检 |
| 不做自动搜索 | `boss.py` 默认不注册；需 spike 通过 | `spikes/boss_search_spike.py` |
| 不编造 | `services/fidelity.py`，生成/审核/导出三处强制 | 恶意 fixture 断言全拦（P5） |
| 不泄露隐私 | `.gitignore` / `utils/logging.py` / 只写新文件 | 脱敏测试 + 原文件 hash 不变测试 |

---

## 6. 阶段推进状态
| 阶段 | 内容 | 状态 |
|---|---|---|
| 提示词 0 | 目录结构 + schema/Alembic 方案 + P0 计划 | ✅ 已产出（3 份 docs） |
| **P0** | 骨架 + 工程底座 + CI + README | ✅ **已完成**（结束时 107 测试绿；双平台 CI 绿） |
| **P1** | 简历导入解析 + `jh resume split` | ✅ **已完成**（176 测试绿；双平台 CI 绿） |
| **P2** | `jh job add <url\|--file\|--manual\|--html>` + 三层去重 | ✅ **已完成**（254 测试绿；抓取链路端到端验证通过） |
| P3 | 共享层 + 两级匹配 + `jh match explain` + `jh gap` | ✅ **已完成** |
| P4 | 待投清单 + 额度 + 开场白（完整/精简）+ 导出 | ✅ **已完成** |
| P4.5 | 零手打跟进（批量打标 + 模糊匹配 + 顶部提醒）→ **★ MVP 完成** | ✅ **已完成** |
| P5 | AI 定制简历（`tailor brief` → 完整版 + FidelityGuard） | ✅ **已完成**（本轮交付；详见 `STATUS.md`） |
| P6 | 看板 + 面试 + 问题记录 + 笔试/Offer + 统计 + 报告 + .ics | ✅ **已完成**（详见 `STATUS.md`） |
| P7 | 线索：**采集端搬去独立部署 `leadhunter`**（`lh`），本仓库只留**消费端**（`jh leads import` / `jh leads serve` + 线索页） | ✅ **消费端已完成**（2026-09-20；采集端不在这里）。⚠️ 本文件里凡是把 P7 描述成"采集端在本仓库（模拟器 / 抓屏 / `app_*.py`）"的段落都是**旧架构**，以 README 与 `docs/P7-消费端API.md` 为准 |
| P8 | Web 界面（其余页面移出主线；★ 线索页已随 P7 消费端落地，是 P8 的第一次落地） | 🚧 线索页已落地 |
| P9 | 开源收尾（README 定稿 / demo 独立库 / LICENSE / COMPLIANCE） | ✅ **已完成** |

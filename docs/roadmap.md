# JobHunter · P0 实现计划（提示词 0 产出③）

> **P0 目标**：搭骨架 + 把工程底座一次性建好（评审原话："2 小时的事，省掉后续返工"）。
> **P0 边界**：除 `jh init` 外，所有命令都是**占位**；**不写任何 P1+ 业务逻辑**。
> 原则：**骨架优先（skeleton-first）** —— 先把所有文件和各层契约立起来（含 P1+ 的空壳与
> 已定死的接口），再填工程底座。这样后续每个阶段都是"往已有格子填内容"，
> 不会因为"当时没想清楚分层"而返工。

---

## 0. P0 完成情况（实测结果）

| 验证项 | 结果 |
|---|---|
| `uv sync` + `uv lock --check` | ✅ 通过（66 个包） |
| `ruff check .` | ✅ All checks passed（含 `PLW1514`） |
| `ruff format --check .` | ✅ 全部已格式化 |
| `pytest` | ✅ **103 passed**（5 个测试文件） |
| `jh init` | ✅ 11 张表 / `alembic_version=0001` / PRAGMA 三项生效 |
| `jh init` 幂等 | ✅ 重复执行提示"已存在"，不丢数据 |
| 迁移可逆 | ✅ upgrade → downgrade → upgrade 三连通过 |
| 外键强制 | ✅ 非法 `job_id` 插入抛 IntegrityError |
| `jh --help` | ✅ 19 个顶层命令全部可见 |
| 占位命令 | ✅ 退出码 2 + "该功能尚未实现 + 计划阶段" |
| **CI（GitHub Actions）** | ✅ **ubuntu-latest + windows-latest 双平台全绿**<br>https://github.com/tuya12345/jobhunter/actions/runs/34926006464 |

### ★ CI 第一次跑就抓到一个真 bug（这就是双平台的价值）

第一次 CI：**ubuntu 全绿，windows 挂在「确认 CLI 入口可用」**。

- **现象**：GitHub Actions 的 windows runner 控制台编码是 **cp1252**（英文 Windows），
  而 `jh --help` 文案里有 `→`、`·`、`⚠️`、emoji，rich 写流时抛
  `UnicodeEncodeError: 'charmap' codec can't encode characters in position 12-15`
  → **`jh --help` 整个命令非 0 退出**。也就是说：CLI 在英文 Windows 上完全不可用。
- **为什么本地发现不了**：中文 Windows 是 cp936，也编不了这些字符，
  但触发时机不同（我的 `setup_logging` 恰好先 reconfigure 了 stdout）；
  ubuntu 是 UTF-8，永远正常。**只有"英文 Windows + 真实 CI"这个组合能暴露它。**
- **根因**：rich 的 `Console` 在**构造时**缓存终端编码，而各命令模块的
  `console = Console()` 是模块级对象（import 时创建）→ 在入口点里改编码已经太晚。
- **修法**：`utils/io.py` 加 `force_utf8_output()`（stdout/stderr 切
  `utf-8 + errors="replace"`，幂等、无 `reconfigure` 的流静默跳过），
  并在 `jobhunter/__init__.py` 包导入的**最前面**调用。
- **固化**：新增回归测试 `test_cli_survives_non_utf8_console`
  （子进程 + `PYTHONIOENCODING` 分别设 cp1252 / gbk / ascii，
  断言退出码 0、无 UnicodeEncodeError、中文与命令名都在输出里）。

**结论**：如果只跑 ubuntu，"英文 Windows 上 CLI 完全不可用"这个致命问题会一直藏着 ——
这正好印证了技术评审那句"CI 只有 Ubuntu 是**虚假安全感**"。

### P0 期间实测发现并修掉的工程问题（都是真实踩坑）

| 问题 | 现象 | 修法 |
|---|---|---|
| **Windows 无系统 tzdata** | `ZoneInfo("Asia/Shanghai")` 抛 `ZoneInfoNotFoundError`，整个包 import 失败 | 主依赖加 `tzdata`（不是可选项，展示时区固定北京时间） |
| **★ 非 UTF-8 控制台 CLI 崩溃** | GitHub Actions **windows-latest**（cp1252）跑 `jh --help` 抛 `UnicodeEncodeError` → 命令非 0 退出，CLI 在英文 Windows 上完全不可用 | 包导入时 `force_utf8_output()`；新增子进程回归测试（cp1252/gbk/ascii） |
| **Alembic 用系统编码读 `alembic.ini`** | 中文注释 → `UnicodeDecodeError: 'gbk' codec can't decode byte 0x85`，迁移根本跑不起来 | `alembic.ini` 保持**纯 ASCII**（并写明原因，防止后人再写中文） |
| **`add_typer` 不传 name** | 只发一条 UserWarning 就把子命令摊平到顶层 → `jh today`、`jh weak` 等命令**整体消失** | 注册表显式给名字 + `test_no_orphan_subcommands_at_top_level` 守住 |
| **组回调带位置参数 → 子命令无法分派** | `jh apply timeline 5` 里 `timeline` 被当成位置参数，子命令不执行 | `apply` 改单命令应用；`--timeline` / `--status` 走选项（详见 architecture.md §4.5） |
| **单命令应用写成 `@app.command()`** | `jh today --limit 5` 报 `No such option: --limit` | 统一 `callback + invoke_without_command`；`test_single_command_subapps_accept_options` 守住 |
| **pydantic 私有属性陷阱** | `_yaml_file: Path \| None = None` 被包成 `ModelPrivateAttr`，`Path(...)` 抛 TypeError → **配置文件根本没被读取**（静默！） | config.yaml 路径改为显式参数传递 |
| **pytest 往 root logger 加 handler** | 脱敏 Filter 只挂在 setup 时的 handler 上，pytest 之后加的 handler 会漏掉 → 脱敏形同虚设 | 逐个 handler 检查并挂 Filter（幂等） |
| **`strptime` 无年份解析即将变更** | Python 3.13 起对 `"%m-%d"` 发 DeprecationWarning，3.15 会改行为 | 自己按"展示时区今年"显式补年份 |

---

## 1. 阶段拆解（6 步，每步都可单独验证）

### 步骤 1／6 · 工程与依赖（uv）

| 动作 | 细节 |
|---|---|
| 建 `pyproject.toml` | 项目元数据；`requires-python = ">=3.13"`；`[project.scripts] jh = "jobhunter.cli.app:main"`；src 布局 `[tool.hatchling]` 或 setuptools 配 `src/` |
| 依赖分组 | 主依赖：`typer` `rich` `pydantic` `pydantic-settings` `SQLAlchemy` `alembic` `PyYAML` `httpx` `python-dateutil`；<br>extras：`[browser]`=playwright、`[docx]`=python-docx、`[llm]`=openai、`[emulator]`=uiautomator2（**钉版本**）<br>dev group：`pytest` `pytest-asyncio` `pytest-cov` `ruff` `mypy` |
| ruff 配置 | `line-length=100`、`target-version="py313"`；**开 `PLW1514`**（强制显式 `encoding=`）；lint 规则集 `E,F,W,I,N,UP,B,PL,SIM` |
| pytest 配置 | `testpaths=["tests"]`；**注册 markers `live` / `emulator`**；`addopts = "-m 'not live and not emulator'"`（本地和 CI 默认一致） |
| 锁依赖 | `uv sync` → 提交 `uv.lock` |

**验证**：`uv sync` 成功；`uv run jh --help` 能打印（此时命令可全是占位）；`uv run ruff check .` 通过。

---

### 步骤 2／6 · 配置层 + 工具层（工程底座的"血"）

| 文件 | 负责什么 | 关键取舍 |
|---|---|---|
| `config/settings.py` | `pydantic-settings` 读 `config.yaml` + `.env`；**校验**（权重和=100、阈值 0-100、额度>0、延时区间合法）；单例 `get_settings()` | 密钥**只**从 `.env` 读；`config.yaml` 可提交（不含隐私） |
| `config/paths.py` | 统一 `data/` `output/` `state/` 路径解析（可被测试覆盖成临时目录） | 路径不散落在业务代码里，方便测试隔离 |
| `utils/io.py` | `read_text / write_text / read_json / write_json / ensure_dir`，**全部强制 `encoding="utf-8"`** | ★ Windows 裸 `open()` 默认 cp936 → 读中文简历必崩；由 ruff `PLW1514` 长期守住 |
| `utils/time.py` | `utc_now()`（**aware**）、`to_utc(dt)`、`to_local(dt)`（Asia/Shanghai）、`fmt_local(dt)`、`days_since(dt)` | 不用已弃用的 `utcnow()`；存储层只认 UTC aware |
| `utils/logging.py` | `RedactingFilter`：手机号 `1[3-9]\d{9}` → `138****8888`；邮箱 `a***@domain`；`setup_logging()` 挂到 root handler | ★ **任何日志都不打印简历原文**（规则：日志只打摘要，正文走 DEBUG 且默认关闭） |
| `config.yaml` | 提示词里那套个人默认值（keywords / cities / job_type / threshold / daily_limit / match_dimensions / skill_aliases / learning_whitelist / greeting_banned_words / 延时） | 首次 `jh init` 不改它，只读 |
| `.env.example` | `DEEPSEEK_API_KEY=`、`DEEPSEEK_BASE_URL=`、`JH_DB_URL=` 占位 | 真 `.env` 进 `.gitignore` |

**验证**：三个 utils 的单测（编码 / 时区 / 脱敏）在步骤 5 统一跑。

---

### 步骤 3／6 · 数据层（models + Alembic + PRAGMA）

| 文件 | 内容 |
|---|---|
| `models/base.py` | `DeclarativeBase` + **命名约定**（否则 SQLite 上 Alembic 无法 drop 匿名约束）+ `UTCDateTime` TypeDecorator + `JsonText` 便捷类型 |
| `models/*.py` | 11 张表全部定义（字段/索引/外键删除策略见 `docs/data_model.md`） |
| `storage/db.py` | `create_engine`（`connect_args={"check_same_thread": False}`）+ `@event.listens_for(Engine, "connect")` 执行 **PRAGMA `journal_mode=WAL` / `busy_timeout=5000` / `foreign_keys=ON`**；`session_scope()` 上下文管理器 |
| `storage/migrate.py` | 用 **Alembic Python API** 跑 `upgrade head`（不用 subprocess，跨平台稳）；查询当前版本 |
| `alembic.ini` + `alembic/env.py` + `script.py.mako` | `env.py` 从 settings 取 URL、`target_metadata=Base.metadata`、**`render_as_batch=True`**（SQLite 改表必需） |
| `alembic/versions/0001_initial_schema.py` | 11 张表 + 21 个索引/唯一约束；`downgrade()` 逆序 drop |
| `cli/commands/init_cmd.py` | `jh init`：建目录 → `upgrade head` → 建 engine 验 PRAGMA → 打印小结；**幂等** |

**验证**：`jh init` 成功；`sqlite3` 查 11 张表在；PRAGMA 三项符合预期。

---

### 步骤 4／6 · CLI 骨架（Typer）

| 项 | 做法 |
|---|---|
| `cli/app.py` | 组装 Typer（`no_args_is_help=True`）；`@app.callback()` 全局 callback：**读配置 → 打印顶部"待处理提醒" → 再执行子命令**；`--version`/`--no-remind` 开关；`main()` 入口 |
| `services/reminder_svc.py` | `get_reminders(session) -> ReminderSummary`；**P0 返回空结构**（0 条），P4.5 填真实统计 |
| `cli/commands/*.py` | 18 个命令文件。除 `init` 外统一 `NotImplementedError` + **友好中文提示"该功能在 PX 阶段实现"**（不抛栈、不假成功） |
| `cli/common.py` | rich 表格/面板/JSON 输出辅助；`echo_stub(feature, phase)` 统一占位输出 |
| 命令清单（必须全部可见） | `init` `resume` `job` `match` `gap` `today` `apply` `interview` `question` `assessment` `weak` `stats` `report` `tailor` `followup` `leads` `offer` `cal` `demo` |
| 占位子命令 | 按计划预先声明（如 `job add/list/skip`、`match run/explain/set`、`apply mark/list-pending`、`tailor brief/analyze/diff/approve/export`、`leads add/probe/capture/export`…）→ 将来只填实现，不改 CLI 形状 |

**为什么占位也要把子命令声明全**：CLI 就是这份工具的"产品说明书"。
先定形状 → 你能在 P0 就一眼看出**最终会得到什么**，也方便你提前提反对意见。

**验证**：`jh --help` 列出全部 19 个命令；`jh doctor`（若加）或任意占位命令给出友好提示；
`jh init` 真跑通；全局 callback 在任何命令前都打印提醒行。

---

### 步骤 5／6 · 测试（P0 的 5 个，全部确定性、不联网）

| 测试文件 | 断言 |
|---|---|
| `test_smoke.py` | ① 临时库上 `jh init` 后 **11 张表全在** + `alembic_version == 0001`；② **PRAGMA 三项**（wal / 5000 / 1）符合；③ `upgrade→downgrade→upgrade` 可逆；④ `jh init` **跑两次幂等**；⑤ **插非法 `job_id` 的外键记录必须抛 IntegrityError**（守住"外键真的开了"） |
| `test_logging_redaction.py` | 日志里手机号/邮箱被脱敏；**原文不出现在任何 handler 输出里**；简历正文不会被打印 |
| `test_io_encoding.py` | 中文/emoji 往返一致；文件确实是 UTF-8 字节；写入含 `\n` 的文本在 Windows 上不被转成 `\r\n` 以外的意外行为（显式 `newline=""`） |
| `test_time_utc.py` | `utc_now()` 是 aware；存取往返仍是 aware 且等于原值；`to_local` 得到 Asia/Shanghai 且偏移 +08:00；`days_since` 跨时区算对 |
| `test_cli_skeleton.py` | `jh --help` 含全部命令名；占位命令返回码非 0 且提示含阶段名；`--no-remind` 时**不**打印提醒 |

**测试隔离铁律**：所有测试用 `tmp_path` 临时库 + monkeypatch 配置路径，**绝不碰 `data/jobhunter.db`**
（conftest 里写死"若解到真实库路径则直接 fail"，防止手滑污染真实投递数据）。

---

### 步骤 6／6 · CI + 文档（README 从 P0 就维护）

| 交付 | 内容 |
|---|---|
| `.github/workflows/ci.yml` | matrix `os: [ubuntu-latest, windows-latest]`、Python 3.13；步骤：`astral-sh/setup-uv` → `uv sync` → `uv run ruff check .` → `uv run pytest`（默认 marker 已排除 live/emulator）；**`uv lock --check`** 确认锁文件与 pyproject 一致 |
| `.gitignore` | `.env`、`*.db`、**`*.db-wal`、`*.db-shm`**、`data/`、`output/`、`state/`、`*.docx`、`*.pdf`、`__pycache__`、`.venv`、`.ruff_cache`、`.pytest_cache`、`*.egg-info` |
| `README.md` v1 | 简介 + **⚠️ 合规声明**（辅助工具、**不提供自动投递**、遵守平台协议、风险自担）+ 功能列表 + 安装（uv/venv 两法）+ `jh init` 快速开始 + 目录结构 + 路线图 + 隐私说明 |
| `docs/architecture.md` `data_model.md` `roadmap.md` | 本阶段已产出，随实现微调 |
| git | `projects/jobhunter` 独立仓库；**首个 commit 前用 `git status` 确认没有 `.env`/`*.db`/真实 docx 混入** |

**CI 双平台的意义**：本项目主战场是 **Windows**（BOSS 网页版 + 模拟器）。
只跑 Ubuntu 是"虚假安全感"—— cp936 编码坑、路径分隔符、`asyncio` 事件循环策略
全都是 Windows 特有的坑。

---

## 2. 交付物清单（P0 Done = 下面全部为真）— ✅ 已全部达成

- [ ] `uv sync` 一把过；`uv.lock` 已提交
- [ ] 目录结构与 `docs/architecture.md` 第 3 节**完全一致**（含 P1+ 空壳，标注阶段）
- [ ] `jh --help` 列出全部 19 个命令；占位命令给出"该功能在 PX 实现"的中文提示
- [ ] `jh init` 成功：11 张表 + `alembic_version=0001` + PRAGMA 三项生效；**可重复执行**
- [ ] `jh init` 之后 **PRAGMA foreign_keys=ON 真的生效**（有测试证明）
- [ ] `utils/io.py` `utils/time.py` `utils/logging.py` 三个底座就位且有测试
- [ ] 日志脱敏生效；**全仓库无硬编码密钥**、无真实手机号/邮箱/姓名
- [ ] `.gitignore` 覆盖 `.db-wal` / `.db-shm`
- [ ] `pytest` 5 个测试文件全绿；`ruff check` 零告警（含 `PLW1514`）
- [ ] `.github/workflows/ci.yml` 双平台矩阵就位（本地无法跑 Actions → 以"本地等价命令全绿 + 配置无误"为验收，README 注明）
- [ ] `README.md` v1（含合规声明）+ `docs/` 三份设计文档
- [ ] 环境说明：uv 安装步骤写进 README（本次已装 `uv 0.12.13`）

---

## 3. 需求 → 交付物 → 证据 对照表（防止漏项）

| 提示词 1 的要求 | 落在哪 | 怎么证明 |
|---|---|---|
| 1 目录结构 | 全仓库 | `docs/architecture.md` §3 逐行对照 |
| 2 uv 锁依赖 + `[emulator]` extra | `pyproject.toml` / `uv.lock` | `uv sync` + `uv lock --check`；`uv sync --extra emulator` 可不装 |
| 3 配置校验 + `.env` | `config/settings.py` | 非法配置（如阈值 200、权重和≠100）**必须报错**的用例 |
| 4 11 张表 + Alembic + PRAGMA | `models/` `alembic/` `storage/db.py` | `test_smoke.py` 五项断言 |
| 5 io/time/日志脱敏 | `utils/` | `test_io_encoding` / `test_time_utc` / `test_logging_redaction` |
| 6 CLI 骨架 19 命令 | `cli/` | `test_cli_skeleton.py` |
| 7 全局 callback 提醒（占位） | `cli/app.py` + `services/reminder_svc.py` | 任意命令前打印；`--no-remind` 可关 |
| 8 CI 双平台 | `.github/workflows/ci.yml` | 配置 + 本地等价命令全绿 |
| 9 pytest markers | `pyproject.toml` | `pytest --markers` 能看到 `live` / `emulator` |
| 10 .gitignore | `.gitignore` | `git check-ignore -v data/jobhunter.db` 等 |
| 11 三个测试 | `tests/unit/` | 实为 5 个文件（多加了 time 与 cli skeleton），全绿 |
| 12 文档 + README | `README.md` `docs/` | 文件存在 + 含合规声明 |

---

## 4. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| Windows 编码坑（cp936） | 读中文简历崩溃 | `utils/io.py` 统一封装 + ruff `PLW1514` + 跨平台 CI |
| SQLite 外键默认关闭 | 约束形同虚设，脏数据悄无声息 | 连接时 PRAGMA + **专门的 IntegrityError 测试** |
| Alembic 在 SQLite 上改表失败 | 将来加字段要删库 → 丢真实投递数据 | `render_as_batch=True` + 迁移可逆测试 |
| 测试误写真实库 | 污染真实投递记录 | conftest 强制 tmp 路径 + 解到真库路径直接 fail |
| playwright/uiautomator2 装不上 | 阻塞主干开发 | 拆 extras，主依赖不含浏览器/模拟器；CI 只装 dev |
| 依赖版本漂移 | "我这儿能跑" | `uv.lock` 提交 + CI `uv lock --check` |
| **uv 未安装**（已解决） | 无法锁依赖 | 本次已装 `uv 0.12.13`；README 写明安装方式 |

---

## 5. 停点声明（遵守"严格按阶段"）

P0 完成后**停下来等确认**，不触碰：
- ❌ P1 简历解析（`resume_svc` 真实现、`python-docx` 解析、`resume split`）
- ❌ P2 抓取（`manual.py` / Playwright / 去重逻辑 / `boss` spike）
- ❌ P3 匹配（`match_svc` 打分公式、技能归一化、LLM Provider 真调用、`llm_cache` 读写）
- ❌ P4 / P4.5 / P5 / P6 / P7 / P8 / P9 全部

P0 里唯一会被后续阶段复用的"提前定死的东西"是**接口与形状**：
`adapters/base.py` 的 `JobSource` 契约、`llm/base.py` 的 `LLMProvider` 契约、
`cli/` 的命令与子命令形状、`models/` 的 11 张表。**这些是设计成果，不是 P1+ 代码。**

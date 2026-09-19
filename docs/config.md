# 配置说明

> **两个文件就够**：`config.yaml`（你的求职偏好，**会提交进 Git**）与 `.env`（密钥 + 本机路径，**永不提交**，从 `.env.example` 复制一份改）。
> 改完**存盘即生效**：每次运行 `jh` 都会重新读一遍（同一个进程内配置有缓存，`jh --config` 会主动清掉）。
> 优先级从高到低：**显式传参 > 环境变量 `JH_*` > `.env` > `config.yaml` > 代码默认值**（§0）。
> 写错**不会静默**：`src/jobhunter/config/settings.py` 里带了 pydantic 校验，写错的字段会在启动时变成一句明确报错（§11 列了常见错法）。
> 建议始终在**仓库根目录**运行 `jh`：`config.yaml` 与 `.env` 都按"当前工作目录"查找（§0 有细节）。

本文里 `settings.py:210` 这类引用都相对 `src/jobhunter/`。

---

## 0. 三层来源与优先级

`AppSettings` 的取值顺序写死在 `settings.py:303-333`（`settings_customise_sources`）：

| 优先级 | 层 | 写在哪 / 怎么给 | 例子 |
|---|---|---|---|
| ① 最高 | 显式传参 | `jh --config other.yaml`，或代码里 `AppSettings(threshold=70)` | 一次性试用另一套配置 |
| ② | 环境变量 | `JH_` + 字段名大写 | `JH_THRESHOLD=70`、`JH_LLM_PROVIDER=mock` |
| ③ | `.env` | 复制 `.env.example` 得来，只在里面放密钥与本机路径 | `DEEPSEEK_API_KEY=sk-…`、`JH_DB_URL=…` |
| ④ | `config.yaml` | 仓库自带一份可直接用；就是你的求职偏好 | `threshold: 60` |
| ⑤ 最低 | 代码默认值 | `settings.py` 里的 `Field(default=…)` | `threshold` 默认 60 |

几条**必须知道**的细节（都是踩过的坑）：

- **只覆盖"写了的那几个键"**：`config.yaml` 里只写 3 行是合法的，其余继续往下取默认值。
  但**嵌套小节是整体构造的**：写 `match_weights:` 时缺的键会用该字段自己的默认值补齐
  （`hard`/`skill` 缺失 → 回落 30/40），所以"只改一项"极容易把三项之和弄成 110（§2 有实例）。
  （已验证：`AppSettings(match_weights={"direction": 40})` → 报 `当前是 110`。）
- **嵌套字典是"合并"而不是"整体替换"**（跨来源也一样）：实测环境变量
  `JH_SKILL_ALIASES='{"Java": ["JS"]}'` 与 `config.yaml` 里那张别名表的结果是**两张表的并集**
  （标量字段则正常覆盖，如 `JH_THRESHOLD=77`）。所以想**删掉** `config.yaml` 里的一条别名，
  只能改 `config.yaml` 本身，别指望用环境变量或 `.env` 盖掉它。
- **`config.yaml` 找哪一份**（`_load_default_yaml()`，`settings.py:398-410`）：
  `--config` / `JH_CONFIG` → **当前工作目录**的 `config.yaml` → **项目根**（含 `pyproject.toml` 的那一层，`config/paths.py:76-94`）的 `config.yaml`。
- **`.env` 只在当前工作目录找**（`env_file=".env"`，`settings.py:196`）。
  在别处运行 `jh`，`.env` 读不到 → 密钥静默失效（命令照跑，LLM 层一直"未配置"）。
  实测：在仓库根运行 `AppSettings()` 时 `llm_enabled=True`，换到别的目录运行同一个包就是 `False`。
- **`JH_CONFIG` 指向不存在的文件不会报错**：pydantic-settings 对不存在的 yaml 直接跳过
  （`pydantic_settings/sources/base.py` 的 `_read_files`：`if not file_path.is_file(): continue`），
  而 `settings.py:430-438` 也只在文件存在时才采用它 → 结果是**静默全部回落默认值**，
  你改的偏好一个都没生效，却没有任何提示。改配置不生效时先怀疑这里。
- **`JH_CONFIG` / `JH_LOG_LEVEL` 必须是真正的环境变量**：它们由代码直接读 `os.environ`
  （`settings.py:324`、`settings.py:434`、`utils/logging.py:163`），而 `.env` 里的值**不会**进入 `os.environ`
  （pydantic-settings 只把它喂给 settings，全项目没有调用 `load_dotenv`）→ 写在 `.env` 里对这俩无效。
- **密钥只能放 `.env`**：`config.yaml` 是要提交进仓库的（`settings.py:16` 明写了这条纪律）。
  `.env.example` 里全是空占位，不含任何真实值。
- **同进程内改了环境变量，要清缓存**：`get_settings()` 带 `lru_cache`（`settings.py:413`），
  `jh --config` 的做法就是"写 `JH_CONFIG` + `reset_settings()`"（`cli/app.py:110-117`）。
  命令行用户不必关心（每次运行都是新进程）。
- **想拿一套配置试试又不碰真实数据**：`jh demo` 会把 `JH_DATA_DIR`/`JH_OUTPUT_DIR`/`JH_STATE_DIR`
  指到临时目录、把 `JH_LLM_PROVIDER` 设为 `mock`，并**断言**真的切过去了（`cli/commands/demo_cmd.py:269-293`），
  退出即销毁。

---

## 1. 求职意图（keywords / cities / job_type）

### `keywords`

- **是什么**：一句"我想找什么"的声明，用来描述你的搜索意图（默认写了四条方向词）。
- **默认值**：`["AI Agent", "大模型应用", "Python 开发", "前端开发"]`（`settings.py:203-205`）。
- **取值/校验**：字符串列表，**不能为空**（`settings.py:340-341`）。
- ⚠️ **写错了会怎样**：
  · 写成标量 `keywords: AI Agent`（YAML 里这是字符串）→ 启动报
  `keywords: Input should be a valid list`；
  · 写成空列表 → `Value error, keywords 不能为空：没有搜索意图就无法判断岗位方向是否匹配`。
- ⚠️ **它现在不参与打分**：全仓库只有两处读它 —— 上面那条非空校验与 `settings_snapshot()`
  的打印字段（`settings.py:484`）。匹配打分用的是 `cities` / `job_type` / `match_weights`。
  它是给**可选抓取适配器**（P7/P8 的"按关键词搜索"）预留的意图声明。
  **改它不会让任何分数变化**，别指望加一个词就能刷出相关岗位。

```yaml
keywords:
  - AI Agent
  - 大模型应用
  - Python 开发
  - 前端开发
```

### `cities`

- **是什么**：意向城市列表，硬条件维度里的"城市"就是拿它比的。
- **默认值**：`["深圳", "广州", "东莞"]`（`settings.py:206`）。
- **取值/校验**：字符串列表，**不能为空**（`settings.py:342-343`）。
- **命中规则（重要）**：**精确字符串比较**（`match_svc.py:696`）。命中 → 该维度 1.0；
  不命中 → 降到 **0.4** 并在 `jh match explain` 里写明"XX 不在意向城市（…）里"（`match_svc.py:125, 698`）——
  降分**不淘汰**（异地机会可能你愿意搬，也可能是远程）。
- ⚠️ **写错了会怎样**：
  · 空列表 → `Value error, cities 不能为空：城市是硬条件维度之一`（所以 `_check_city` 里"没配 cities"的分支
    实际上走不到，那是防御性代码）；
  · 写成 `深圳市`（而不是 JD 里出现的 `深圳`）**不报错**，只是永远命中不了 → 所有岗位的城市维度都吃 0.4 的降分，
    你会得出"这工具打分怎么这么严"的错误结论。对着 `jh match explain` 里打印的城市名抄。

### `job_type`

- **是什么**：你要实习、全职还是都要。它决定"实习/全职"这一项是符合还是**重降分**。
- **默认值**：`both`（`settings.py:207`）。**取值范围**：只能是 `intern` / `fulltime` / `both`（`Literal`）。
- ⚠️ **写错了会怎样**：
  · 写 `parttime` / `实习` → 启动报 `job_type: Input should be 'intern', 'fulltime' or 'both'`；
  · 写 `intern` 却去投全职岗：**不会**被淘汰，但这一维度只拿 `HARD_CONFLICT_RATIO = 0.2`，
    并在 explain 里写"这是全职岗，但 config 里 job_type=intern（路线不符，重降分）"（`match_svc.py:701-718`）——
    如果你其实两边都投，老老实实写 `both`，否则等于自己给自己降分。

```yaml
cities:
  - 深圳
  - 广州
  - 东莞

job_type: both      # intern / fulltime / both
```

---

## 2. 打分与额度（threshold / daily_limit / llm_top_n / match_weights / match_dimensions）

### `threshold`

- **是什么**：分数线。低于它的岗位不进待投清单。
- **默认值**：`60`。**取值范围**：`0 ≤ threshold ≤ 100`（`settings.py:210`）。
- **消费者**：`jh today` 的过滤（`today_svc.py:115`）+ `jh match run` 排名小结里的分数线（`match_cmd.py:61`，仅用于显示）。
  临时覆盖：`jh today --min-score 40`、`jh match run --threshold 70`。
- ⚠️ **写错了会怎样**：
  · `threshold: 200` → 启动即报 `threshold: Input should be less than or equal to 100`，
    CLI 打印"配置错误"后以退出码 1 结束（`cli/app.py:125-128`）。
    **为什么当初要拦这个**：待投清单会永远是空的，你第一反应是"抓取坏了"，永远想不到是配置（`settings.py:13`）。
  · `threshold: 0` 合法，但所有**打过分**的岗位都会进清单（包括方向、城市全不匹配的），清单会被灌水。

### `daily_limit`

- **是什么**：每天最多投几条 —— 防海投上瘾、保护账号的**提醒额度**。
- **默认值**：`20`。**取值范围**：`1 ≤ daily_limit ≤ 200`（`settings.py:211`）。
- **消费者**：`jh today --check` 打印"今天已投 X / 上限 N"，清单末尾也带一行（`today_cmd.py:122-126, 217`）。
  "今天已投几条"来自**投递记录**里当天 `applied_at` 的条数（`apply_svc.count_applied_today`）。
- ⚠️ **写错了会怎样**：
  · `0` → `daily_limit: Input should be greater than or equal to 1`；`500` → `less than or equal to 200`。
  · ★ 它是**提醒，不是硬拦截**：超了不会阻止你生成清单、也不会阻止 `jh apply` 打标（全仓库没有
    "超额度就拒绝"的分支，`today_svc.py:97-99` 只是算出一个 `remaining_quota` 显示给你看）。
    把它当成"你给自己定的纪律"，不是权限控制。

### `llm_top_n`

- **是什么**：只有规则分排前 N 的岗位才走 LLM 精评（省钱 + 提速）。
- **默认值**：`30`。**取值范围**：`0 ≤ llm_top_n ≤ 1000`（`settings.py:212`）。
- **消费者**：`jh match run` 取 `ordered[:max(0, top_n)]`（`match_cmd.py:95-108`）；`--top N` 可临时覆盖。
- ⚠️ **写错了会怎样**：
  · `0` **合法**：会打印"`llm_top_n=0`，没有岗位需要精评"并跳过 LLM 层（等价于 `--no-llm`）——
    适合"我现在只想白嫖规则层"。
  · 调到 200+：规则层永远是 0 成本全量的，但精评是**真花钱**的（并发 4，每条一次调用；
    命中 `llm_cache` 的不重复花钱），别顺手多写个 0。

### `match_weights`

- **是什么**：三维度权重（百分比）。`direction` 岗位方向、`hard` 硬条件、`skill` 技能关键词交集。
- **默认值**：`direction: 30`、`hard: 30`、`skill: 40`（`settings.py:87-89`）。
- **取值/校验**：每项 `0..100`（`ge=0, le=100`）**且三项之和必须 = 100**（`model_validator`，`settings.py:91-100`）。
- ⚠️ **写错了会怎样（本文最该背下来的一条）**：
  · `30/30/35` → 启动报
    `match_weights 三项之和必须等于 100，当前是 95 (direction=30, hard=30, skill=35)。否则所有岗位的 rule_score 会整体偏移，且极难排查。`
  · **只写一项也一样会被拦**：`match_weights: {direction: 40}` 会让 `hard`/`skill` 回落默认 30/40 → 合计 110 → 同样报错。
    想只调一项，就把三项都写全。
  · 为什么这条要这么狠（`settings.py:10-14`）：权重和是 95 时，**所有**岗位的规则分都偏低 5%，
    现象是"这工具打分怎么这么严"，你几乎不可能自己定位到是配置。
- **怎么调**：先 `jh match explain <job_id>` 看分维度明细，再回来改（`config.yaml:34-35`）。
- **维度语义**（`settings.py:80-83`、`match_svc.py:109-129`）：方向错 → 分再高也白投（方向不符 `0.15`，
  但技能覆盖对得上时可按 `0.55` 跨投）；硬条件不满足是**降分不淘汰**（普通不满足 `0.35`，路线冲突 `0.2`）；
  技能是"JD 抽到的技能里简历命中了多少"。
- ⚠️ **判不出来的维度不会按 0 分算**：它会把权重**让给**其余维度，`jh match explain` 里会标 `redistributed`
  （`match_svc.py:106, 830, 936`）。所以"某维度没判出来"≠"那维度得了 0 分"。

```yaml
match_weights:
  direction: 30   # 岗位方向
  hard: 30        # 硬条件（经验/应届/城市/学历）
  skill: 40       # 技能关键词交集
  # 三项之和必须 = 100，否则启动即报错
```

### `match_dimensions`

- **是什么**：一份"打分维度"的**可读声明**（默认三项，和上面三个维度一一对应）。
- **默认值**：`["技能关键词", "岗位方向", "经验年限/是否应届"]`（`settings.py:215-217`）。
- **取值/校验**：字符串列表；**没有任何校验**（没有 `ge/le`，没有 `Literal`，没有 validator）。
- ⚠️ **写错了会怎样**：★ **它目前不被任何代码消费** —— 全仓库只有 `config.yaml`、`settings.py`
  与 `docs/roadmap.md` 提到它，`jh match explain` 用的维度名是 `match_svc.py` 里写死的。
  所以：改它**不会**改变任何分数、任何输出（写成标量才会报 `Input should be a valid list`）。
  它的价值是"把打分维度写进配置里给人看"，别在这儿花时间调参 —— 要调分请改 `match_weights`。

---

## 3. 技能归一化与白名单（skill_aliases / learning_whitelist）

### `skill_aliases`

- **是什么**：技能归一化词表，"规范名 → 它的所有等价写法"。作用有两个：
  ① 同一技能的多种写法在统计/报告里只算**一项**（`大模型` / `LLM` / `大语言模型` 不会重复计数，`resume_svc.py:359-361`）；
  ② 解决**短词误命中**（见下）。
- **默认值**：`{}`（空 dict，代码默认不带任何别名；仓库的 `config.yaml` 给了一份可直接抄的实用词表）。
- **取值/校验**：`dict[str, list[str]]`；**目标名不能是空白**、**别名列表不能为空**（`settings.py:348-353`）。
- ⚠️ **写错了会怎样**：
  · `Java:` 后面什么都不写（YAML 里解析成 `None`）→ `skill_aliases.Java: Input should be a valid list`；
  · `Java: []`（空列表）→ `Value error, skill_aliases['Java'] 的别名列表不能为空`；
  · `Java: JavaScript`（写成标量）→ `skill_aliases.Java: Input should be a valid list`；
  · 目标名写成 `" "` → `Value error, skill_aliases 里出现了空白的目标名`；
  · 别名列表里塞空串（`- ""`）**不报错**，但这一条永远匹配不上 —— 属于静默失效，别这么写。
- **★ 为什么必须有这张表**：中文没有词边界，`re.search("Java", "JavaScript")`、`re.search("Go", "Google")`
  都会命中 —— 简历里写了 JavaScript 的人会被系统认为"会 Java"，匹配分虚高（`resume_svc.py:401-406`）。
  项目按写法分两类处理（`resume_svc.py:407-455`）：
  · **纯 ASCII 写法**（`Java` / `Go` / `Vue` / `Kubernetes`）自动加边界（不允许紧贴着字母数字），
    所以 `Java` 不会命中 `JavaScript`、`Go` 不会命中 `Google`；
  · **含中文的写法不加边界**（因为 `\b` 的 `\w` 包含汉字，加了 `\b` 反而在"做大模型应用"里永远匹配不上，
    `resume_svc.py:380-395`）→ **中文别名要自己写全**（"大模型应用""大语言模型""检索增强生成"都得列），
    否则会漏命中。
- **和内置词表的关系**：内置词表 `_BUILTIN_SKILLS` 与这张表**合并**；这里的 key 若是内置词表里没有的技能，
  会新建一条（`resume_svc.py:351-377`）。所以你可以用 `config.yaml` 扩词表，不需要改代码。
- 什么时候该加：`jh gap` 输出的"缺失技能"里出现了明显是同一个技能的不同写法，或
  `jh match explain` 里出现了你其实会的技能被判成"缺"。

```yaml
skill_aliases:
  LLM:
    - 大模型
    - 大语言模型
    - 大模型应用
    - LLM
    - large language model
  RAG:
    - RAG
    - 检索增强
    - 检索增强生成
  Kubernetes:
    - Kubernetes
    - K8s
  JavaScript:
    - JavaScript
    - JS
    - ECMAScript      # ★ 有它才不会把 JS 算成 Java
```

### `learning_whitelist`

- **是什么**："我在学"白名单：**允许提到**这些技能，但**必须**同时带一个"在学习"的标注词。
- **默认值**：`[]`（仓库的 `config.yaml` 里给了 `LangChain` / `Kubernetes` 两个例子）。
- **取值/校验**：字符串列表；★ **每一项都必须是技能词表里已有的技能**
  （内置词表见 `src/jobhunter/config/vocabulary.py`，或你在 `skill_aliases` 里声明过的规范名）——
  写错的词会在加载配置时直接报错并告诉你怎么改（ISSUES #43）。
  · 为什么这么严：白名单里的错词（九成是拼写错误，如 `LanchChain`）**永远不会命中任何 JD**，
    "我在学 X"这条规则就静默失效了 —— 你只会觉得"这个功能没效果"；
  · 想加一个词表里没有的新技能：**先**在 `skill_aliases` 里把它写成规范名（那样它才会进词表、
    也才能从 JD 里抽出来），**再**放进白名单。
- **★ 它不是"假装会"的后门**（`services/fidelity.py:27, 299-302`）：
  · 白名单里的技能如果**没有**标注词，判定与没进白名单**完全一样** —— 照样报
    `skill_not_in_resume`（"简历里没有这个技能，属于编造"）；
  · 只有"命中白名单 **且** 文本里出现标注词"时，才记一条 note（"…白名单技能且已标注「在学习」，允许出现"）并放行。
- **标注词是写死的六个**（`fidelity.py:143`）：`在学习` / `正在学` / `学习中` / `还在学` / `刚入门` / `初步了解`。
  写"了解过 Kubernetes"**不算**；写"Kubernetes 正在学"才算。
- ⚠️ **粒度提醒**：判断方式是"**整段文本里出现任一标注词**"（`annotated = any(marker in text …)`，
  `fidelity.py:283`）——所以一段话里只要有一处"在学习"，这段话里所有白名单技能都会被当作"已标注"。
  它解决的是"刚做了 demo 但还不熟，完全不许提反而失真"（`config.yaml:91`），
  **不是**一个精确到词的合规控制。
- **副作用（好事）**：白名单里的技能会被并入技能词表（`resume_svc.py:372-375`）——否则
  "我在学 LangChain"这句话连 `LangChain` 都抽不出来，白名单机制形同虚设；
  `jh tailor brief` 也会把命中的白名单技能单独提示"在学习"（`tailor_svc.py:491`）。

```yaml
learning_whitelist:
  - LangChain
  - Kubernetes      # 提的时候必须带"在学习/正在学/…"之一
```

---

## 4. 开场白（greeting_banned_words / greeting.*）

### `greeting_banned_words`

- **是什么**：开场白禁用词（LLM 特别爱写套话，HR 一眼看出是群发）。
- **默认值**：`[]`（代码默认一个词都没有；仓库的 `config.yaml` 给了一份 8 词的实用清单：
  `贵司` / `期待` / `三生有幸` / `百忙之中` / `万分感谢` / `希望能有机会` / `感谢您的阅读` / `冒昧打扰`）。
- **取值/校验**：字符串列表，可以一个都不填（`[]` 是合法配置）。但**填进来的每一项都要合规**
  （ISSUES #43）：不许空串、不许纯空白、首尾不能有空格、**单项最长 12 字**。
  不合规会在加载配置时报中文错并指出是第几项、哪个值、期望什么。
- **命中后果**：命中即视为**该版本不合格**（`greeting_svc.py:371-373`，"用了禁用词「X」"）。
  纯字数问题允许让模型修一次，**禁用词与保真类失败一律直接降级到确定性模板**
  （`greeting_svc.py:177-194`：保真类失败是信任问题，不能让同一个模型"再写一遍"）。
- ⚠️ **写错了会怎样**：匹配是**朴素子串**（`word in text`）。所以：
  · 填 `期待` 会连带拦掉"期待已久""不期待"这类正常表达；
  · 填太短的词（`我`、`的`、`你`）→ 几乎每一版都命中 → 全部降级成模板，你会以为"LLM 坏了"。
  只留"套话级"的词。写空项/纯空白/超 12 字的词会在加载配置时**直接报错**
  （改前是静默丢掉空项 —— 你以为配上了，其实没有；超长词则会大面积误伤）。

### `greeting.full_min_chars` / `greeting.full_max_chars`

- **是什么**：完整版开场白的字数**闭区间**（按字符数，中英文都算 1）。
- **默认值**：`80` / `150`。**取值范围**：各自 `≥ 1`（`ge=1`），且 `full_min_chars ≤ full_max_chars`（`settings.py:110-121`）。
- **判定点**：`full_min ≤ len(text) ≤ full_max`，唯一实现在 `_length_problems()`（`greeting_svc.py:156-174`）。
- ⚠️ **写错了会怎样**：
  · `full_min_chars: 200` 而 `full_max_chars: 150` → `greeting.full_min_chars(200) 不能大于 full_max_chars(150)`；
  · 区间定得太窄（如 `100-105`）→ 模型很难一次命中，会频繁走"修一次 → 还不行 → 模板兜底"，
    你看到的开场白会越来越像模板（模板本身是保真、保字数、保沾边的，所以它一定"能用"，只是不漂亮）。

### `greeting.short_max_chars`

- **是什么**：精简版开场白的**上限**。★ **它存在的唯一理由是"输入框有字数限制"**：
  BOSS 的开场白输入框有字数上限，80-150 字的完整版在很多岗位**根本发不出去**，
  所以必须同时产出 ≤ 60 字的精简版（`settings.py:103-108`）。
- **默认值**：`60`。**取值范围**：`≥ 1`，且**必须严格小于** `full_max_chars`（`settings.py:122-126`）。
- **怎么用**：`jh today --copy 3 --short` 复制的就是这一版；清单里两个版本都会打印。
- ⚠️ **写错了会怎样**：
  · `short_max_chars: 150`（等于 `full_max_chars`）→ `greeting.short_max_chars(150) 应小于 full_max_chars(150)，否则精简版没有意义`
    （validator 用的是 `>=`，"等于"也不行）；
  · 设成 10 这类极小值 → 模板兜底的最简形式"有相关项目经验，方便聊聊吗？"（`greeting_svc.py:456`）
    也超过 10 字 → 闸门**永远过不去**，你只会看到降级原因。想不出血的数字就保持 60。

### `greeting.github_url`

- **是什么**：你的 GitHub 地址（优势项），会附在开场白里。
- **默认值**：`""`（空 = 完全不用）。
- **非空时的两处作用**：① 写进提示词"我的 GitHub（可以附在结尾）：…"（`greeting_svc.py:246-247`）；
  ② 模板兜底时，字数不够就拿它凑够 `full_min_chars`（`greeting_svc.py:438-442`）。
- ⚠️ **写错了会怎样**：工具**不校验**这个地址，会原样写进开场白（模板路径下更是必然出现）。
  填错等于你自己在开场白里写了一个错链接；它属于"你自己声明的事实"，与保真检查无关。

```yaml
greeting_banned_words:
  - 贵司
  - 期待
  - 三生有幸

greeting:
  full_min_chars: 80    # 完整版下限
  full_max_chars: 150   # 完整版上限
  short_max_chars: 60   # 精简版上限（应对输入框字数限制）→ 必须 < full_max_chars
  github_url: ""        # 填了就会附在开场白里，会原样写出，工具不校验
```

---

## 5. 定制简历（tailor.*）

这五个数字会**同时**进两处：提示词里的约束（劝模型守规矩）与 FidelityGuard 的判定（代码强制）。
所以**改小只会更严格（拦得更多），不会放水**（`config.yaml:116-117`）。

### `tailor.max_length_ratio`

- **是什么**：改写后的长度上限比例 —— "改写不是扩写"。
- **默认值**：`1.5`。**取值范围**：`1.0 ≤ ratio ≤ 5.0`（`ge=1.0, le=5.0` + 一个额外的 `model_validator`，`settings.py:143, 150-157`）。
- ⚠️ **写错了会怎样**：`0.8` → `Input should be greater than or equal to 1`（校验器原话：
  "改写至少要允许和原文一样长"）；`10` → `less than or equal to 5`。

### `tailor.length_floor_extra`

- **是什么**：短句的**绝对余量**（字符数）。
- **默认值**：`30`。**取值范围**：`0 ≤ extra ≤ 500`（`settings.py:144`）。
- **★ 它和 `max_length_ratio` 的关系（必须懂）**：真正的上限不是"原文 × 比例"，而是
  **`max(原文长度 × max_length_ratio, 原文长度 + length_floor_extra)`**（`services/fidelity.py:696-697`）。
  超了报 `too_long`："改写后 N 字，超过原文 M 字的 150%（上限 L 字）—— 改写不是扩写"。
- **★ 为什么短句要绝对余量**（`settings.py:133-137`、`config.yaml:121-122`）：
  原句只有 4 个字（"参与重构"）时，150% 就是 6 个字 —— **任何通顺的改写都会超**，
  这条检查会退化成"短句一律不许改"，那是纯误报；而**误报会让人把整个检查关掉**，宁可留一点余量。
- ⚠️ **写错了会怎样**：
  · 设成 `0` → 短句（大致 60 字以下）的改写几乎必然被判超长，你会在 `jh tailor diff` 里看到一片
    "超过原文 150%"；
  · 设成 `500` → 长句仍受 1.5 倍约束（因为取 `max`），但短句会变得很宽松（4 字原句理论上能改到 504 字），
    长度这道闸门基本只剩"防离谱"。

### `tailor.max_suggestions`

- **是什么**：一次最多给多少条改写建议。★ 它是**给模型的提示词参数，不是代码截断**
  （`settings.py:139-141`）：模型被要求少给几条，而不是"多出来的被程序扔掉"。
- **默认值**：`12`。**取值范围**：`1 ≤ n ≤ 100`（`settings.py:145`）。
- ⚠️ **写错了会怎样**：调大 = 你要**逐条审核**的条数变多，而"逐条审核"正是这个功能的安全边界
  （原话：一次改 20 处以上，就没耐心逐条审了）。调成 1 则基本只能改一处。
  它不会让你少花钱以外的任何东西变好，建议保持默认。

### `tailor.brief_projects` / `tailor.brief_skills`

- **是什么**：`jh tailor brief` 里"重点讲几个项目 / 突出几个技能"（该命令**不调模型、不花钱**）。
- **默认值**：`3` / `2`。**取值范围**：各自 `1..10`（`settings.py:146-148`）。
- **消费者**：`tailor_svc.py:395`（取前 N 个项目）、`tailor_svc.py:465`（取前 N 个技能）。
- ⚠️ **写错了会怎样**：`0` → `Input should be greater than or equal to 1`；`11` → `less than or equal to 10`。
  数值越大 brief 越啰嗦；它不影响最终改写建议的质量。

```yaml
tailor:
  max_length_ratio: 1.5    # 1.0-5.0；上限 = max(原文×1.5, 原文+30)
  length_floor_extra: 30   # 0-500；短句的绝对余量，别设 0
  max_suggestions: 12      # 1-100；给模型的提示词参数（不是程序截断）
  brief_projects: 3        # 1-10
  brief_skills: 2          # 1-10
```

---

## 6. 频率克制（rate_limit.*）

★ 先说结论：**这两个是合规参数，不是性能参数**（`settings.py:160-165`）。
本项目的合规态度是"随机延时 + `--dry-run` + 出错即停 + 只抓用户给的单个链接"，
目的是不给目标站点造成压力、也不被风控标记。想"跑得更快"的正确做法是少录岗位、
用 `jh match run --no-llm`、靠缓存，而不是调这里。

### `rate_limit.min_delay_seconds` / `max_delay_seconds`

- **是什么**：抓取每个岗位详情前的**随机延时区间**（`random.uniform(min, max)`，`adapters/manual.py:181-192`）。
- **默认值**：`2.0` / `5.0`。**取值范围**：各自 `≥ 0.0`，且 `min ≤ max`（`settings.py:167-177`）。
- ⚠️ **写错了会怎样**：
  · `min: 5` 而 `max: 2` → `rate_limit.min_delay_seconds(5.0) 不能大于 max_delay_seconds(2.0)`；
  · 负数 → `Input should be greater than or equal to 0`；
  · ★ **两个都设 0（或 `max: 0`）会静默关掉延时**：`if high <= 0: return`（`manual.py:188-189`），
    批量导入就变成连续请求。这等于把合规红线自己拆了 —— 站点压力、风控标记、账号风险都归你，
    而且 README 的合规声明还在那儿写着。**不建议动。**

### ~~`rate_limit.max_pages`~~ —— **已删除（ISSUES #18）**

这一节以前介绍"翻页上限"。P9 复查时核实：**它全仓没有任何读取点**，
`JobSource.search()` 也只有签名、没有实现、没有调用方 ——
也就是说，那份"上限"只存在于配置与文档里，从来没有生效过。

一个**没人读的配置项**本身危害有限，但它让 `docs/COMPLIANCE.md` 声称了
一个不存在的控制（"抓取路径带……翻页上限"），而那是合规自评要拿给人看的部分。
所以删掉配置项、删掉 `search()` 的 `max_pages` 参数，并把 COMPLIANCE 改成如实描述
（当前**没有自动翻页路径**：默认只抓用户手动给的单个链接）。

将来真要做自动搜索：**参数、配置项、文档说明要一起加回来**，不要只加其中一样。

```yaml
rate_limit:
  min_delay_seconds: 2.0   # 合规红线：抓详情前的随机延时
  max_delay_seconds: 5.0
```

---

## 7. LLM 与密钥（.env 里的 DEEPSEEK_* / llm_provider / llm_concurrency / llm_max_retries）

★ **密钥只能放 `.env`**（或真正的环境变量）：`config.yaml` 会被提交进仓库（`settings.py:16`）。

### `deepseek_api_key` / `deepseek_base_url` / `deepseek_model`

- **是什么**：大模型密钥、地址、模型名（DeepSeek 走 OpenAI 兼容协议）。
- **默认值**：`""` / `https://api.deepseek.com` / `deepseek-chat`（`settings.py:251-266`）。
- **★ 三种写法都认，顺序即优先级**（`AliasChoices`，`settings.py:251-266`）：
  ① `DEEPSEEK_API_KEY` → ② `JH_DEEPSEEK_API_KEY` → ③ `deepseek_api_key`（字段名本身，yaml/代码里用）。
  **照 `.env.example` 的官方写法 `DEEPSEEK_API_KEY=…` 写就行。**
- **为什么要有这个兼容**（`settings.py:239-250`，一次真实的 P0 级 bug）：
  `model_config` 里的 `env_prefix="JH_"` 对本类**所有**字段生效，于是这个字段实际去找的环境变量名是
  `JH_DEEPSEEK_API_KEY`；而 `.env.example`（以及按它写的真实 `.env`）里写的是 `DEEPSEEK_API_KEY` ——
  名字对不上，密钥**从来没被读进来过**，而且完全静默：命令照跑，只是 LLM 层一直"未配置"，
  用户会以为是自己没充钱。
- ⚠️ **写错了会怎样**：
  · 没填 / 填错 → `llm_enabled=False`（`settings.py:384-387`）。**不影响**规则层打分
    （`jh match run --no-llm`）、`jh gap`、`jh tailor brief`，它们完全不需要密钥；
    受影响的是 LLM 精评、开场白生成（会降级成模板并写明原因）、`jh tailor analyze`。
  · **填了却不生效**，按这个顺序查：① 是不是不在仓库根目录运行 `jh`（`.env` 只在**当前工作目录**找）；
    ② 是不是误写进了 `config.yaml`（那是错的，且会被提交）；③ 是不是把它写成了 `.env` 里带引号/空格的格式问题。
  · ★ **别把整个 settings 对象 dump 出去**：`utils/logging.py` 的脱敏 Filter 只认手机号/邮箱/身份证，
    **不认 API key**（`settings.py:472-475`）。要打印配置就用 `settings_snapshot()`
    （白名单字段，**不含** key，`settings.py:469-502`），不要 `settings.model_dump()`。
  · 换 `deepseek_model`：★ 会让 LLM 缓存**自动失效** —— 缓存 key = `sha256(prompt_version | model | jd_hash | resume.content_hash)`
    （`llm/cache.py:99`）→ 重新跑会重新计费，这是**预期行为**（旧模型的评分不该复用）。

```dotenv
# .env（永不提交）
DEEPSEEK_API_KEY=sk-你的key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
```

### `llm_provider`

- **是什么**：用哪个 Provider：`deepseek`（真调用）或 `mock`（本地假 Provider）。
- **默认值**：`deepseek`。**取值范围**：`Literal["deepseek", "mock"]`（`settings.py:273`）。
- ⚠️ **写错了会怎样**：
  · 写 `openai` / `ollama` → `Input should be 'deepseek' or 'mock'`；
  · ★ `JH_LLM_PROVIDER=mock` **不会报错，只会给你编的分数**：mock 是给测试与 `jh demo` 用的确定性假 Provider
    （`settings.py:269-272`、`llm/factory.py:40-46`：测试必须能在完全确定性条件下跑匹配，
    否则 CI 会因网络抖动随机变红）。它返回**写死的**结果：精评固定 `"score": 70`，
    文案带「（mock）」前缀（`llm/mock.py:101-123`）。手动设成 mock 时，你会拿到"看起来很正常"的假结果 ——
    排查"分数怎么不对"时先确认这个变量。

### `llm_concurrency`

- **是什么**：LLM 精评的并发度（`asyncio.Semaphore`，`match_llm_svc.py:372`）。
- **默认值**：`4`。**取值范围**：`1 ≤ n ≤ 16`（`settings.py:276`）。
- ⚠️ **写错了会怎样**：`0` → `greater than or equal to 1`；`32` → `less than or equal to 16`。
  **为什么封顶 16**（`settings.py:274-275`）：这是**个人工具**，4 路已经能把 30 条岗位压到十几秒，
  再高收益极小，只会更容易触发限流。

### `llm_max_retries`

- **是什么**：业务层重试次数（空 content / 坏 JSON）。总尝试次数 = `1 + llm_max_retries`（`match_llm_svc.py:450`）。
- **默认值**：`1`。**取值范围**：`0 ≤ n ≤ 3`（`settings.py:280`）。
- ⚠️ **写错了会怎样**：设 `3` → 连续失败时要烧 4 次 token 才降级到规则分。
  **为什么默认只重试 1 次**（`settings.py:277-280`）：空 content 是**偶发**的，重试一次基本就好；
  连续两次失败说明不是抖动，继续烧 token 不如直接降级 —— 用户至少还能看到分数。
  设 `0` 也可以，只是偶发抖动时会直接降级。

---

## 8. LLM 缓存（不是配置项：**没有 TTL**，只有看板与清理入口）

- **是什么**：`llm_cache` 是"同样的输入不重复付费"的那张表。它**没有任何配置开关**，
  也没有 TTL（用户 2026-09-19 裁决 **A：加 `jh cache stats` + `jh cache clear`，不做 TTL**）。
- **为什么不做 TTL**：缓存的 key 里已经含 `prompt_version` / `model` / `jd_hash` /
  `resume.content_hash` / 提示词指纹 —— **该失效的时候它自己就失效了**。
  再叠一层时间维度只会让人搞不清"为什么昨天还在的缓存今天没了"，
  而它省下的磁盘（几百 KB）与调用（几毛钱）都不值得换这份困惑。
- **想清就手动清**（缓存是**纯派生数据**，删掉不影响业务数据）：

  ```bash
  jh cache stats                  # 按任务看条目数 / tokens / 成本 / 被复用次数 / 命中率
  jh cache stats --task match      # 只看某一类任务
  jh cache clear                   # 清全部（会先告诉你将删多少条，再问一次）
  jh cache clear --task greeting --yes   # 只清一类、不再确认
  ```

  `jh cache clear` **只删 `llm_cache` 一张表**（`models/llm_cache.py` 的那句承诺）。
  非交互终端（管道 / CI）不会替你确认，要真删得显式加 `--yes`。
- **命中率口径**（`jh cache stats` 会把它原样打印出来，不必猜）：

  ```
  命中率 = Σhits ÷ (Σhits + 行数)
  每一行 = 一次真实付费调用；hits = 这行结果被复用了几次
  省下   = Σ(每行 cost × 该行 hits)；没记成本的行不计入
  ```

- ⚠️ **两个容易看错的数字**：
  · **`tokens` / `cost` 列是"这一行当初花了多少"**，不是"本次调用花了多少"。
    `jh match run` 命中缓存时本次成本记 **0**，另起一行显示"命中缓存省下 X"（ISSUES #26）——
    两笔账不要相加。
  · **清空缓存 = 命中率统计归零**（统计口径就存在这张表上，没有第二张流水表）：
    这是有意的，为的是让 `jh cache clear` 真的"什么都不留下"。

---

## 9. 本地路径与默认简历（data_dir / output_dir / state_dir / db_url / default_resume_path）

### `data_dir` / `output_dir` / `state_dir`

- **是什么**：三个数据目录。默认布局（`config/paths.py:19-23, 97-112`）：
  `data/`（SQLite 库 + 抓屏产物 `data/leads/`）、`output/`（导出的 md/csv/docx）、
  `state/`（运行时状态，如 `today-<日期>.json`）。三个都被 `.gitignore` 排除。
- **默认值**：`None` —— 不写就用 `<项目根>/data`、`<项目根>/output`、`<项目根>/state`。
  项目根 = 含 `pyproject.toml` 的那一层；用 `--config` 指定了 config.yaml 时，根取
  **config.yaml 所在目录**（`settings.py:362-375`）—— 这正是"把整个项目搬走还能继续用"的原因。
- **怎么覆盖**：`config.yaml` 里写 `data_dir: D:/secure/jh-data`，
  或环境变量 `JH_DATA_DIR` / `JH_OUTPUT_DIR` / `JH_STATE_DIR`（`paths.py:100-106` 直接读环境变量；
  同时它们也是 `AppSettings` 的字段，所以 `.env` 里写也有效）。
- ★ **这三个变量存在的意义**（`paths.py:100-102`）：
  ① **把库放到加密盘 / 别的盘**（简历、投递记录、公司名都在库里）；
  ② **测试隔离** —— 本项目对"测试写到真实库上"零容忍（测试洗掉真实投递记录是不可接受的意外），
  所以 `jh demo` 就是靠这三个变量把整套流程跑在临时目录里并**断言**切换成功（`demo_cmd.py:269-293`）。
- ⚠️ **写错了会怎样**：
  · ★ 只改 `data_dir` **不会自动搬走已有数据**：SQLite 文件不会自己移动，`jh init` 只会在新位置建一个**空库** ——
    现象是"我的投递记录全没了"，其实旧库还老老实实躺在老地方（`<项目根>/data/jobhunter.db`）。
    要搬就手动移动 `jobhunter.db`（**连 `.db-wal` / `.db-shm` 一起**，或先让程序正常退出再移动），
    否则最近写入的数据可能还在 WAL 里没落盘。
  · 目录不存在时不用担心：建库前会先 `ensure_all()` 把三个目录建出来（`paths.py:65-73`）——
    之所以要显式建，是因为 SQLite 在**目录**不存在时只报一句含糊的 `unable to open database file`。
  · 目录不可写（只读盘/UAC）→ 建库失败，报 `unable to open database file` 或权限错误。

### `db_url`

- **是什么**：直接给 SQLAlchemy 连接串。给了它就**优先于**按 paths 推导（`settings.py:377-382`）。
- **默认值**：`None`。
- **怎么用**：`db_url: sqlite:///D:/secure/jobhunter.db`，或 `.env` 里 `JH_DB_URL=sqlite:///D:/secure/jobhunter.db`
  （`.env.example:21` 就是这个例子）。
- ⚠️ **写错了会怎样**：
  · Windows 上路径**必须用正斜杠**（`sqlite:///D:/secure/jobhunter.db`）：反斜杠会被 URL 解析器吃掉，
    所以项目内部也是先 `as_posix()` 再拼（`paths.py:52-58`）。
  · 同时写了 `db_url` 和 `data_dir` 时 **`db_url` 赢**，`data_dir` 变成只管抓屏产物目录 ——
    用 `jh init` 打印的"数据库"路径确认到底用了哪个。
  · 指到一个不存在的目录 → 同上，含糊的 `unable to open database file`。

### `default_resume_path`

- **是什么**：默认简历文件路径。`jh resume add` **不传路径**时用它，省得每次敲长路径。
- **默认值**：`None`。
- **怎么用**：只写在 `.env` 里 —— `JH_DEFAULT_RESUME_PATH=D:/path/to/你的简历.docx`
  （`config.yaml` 会提交进仓库，而**简历文件名往往带真名**，`settings.py:296-297`）。
  支持 `.docx`（推荐）以及 `.txt` / `.md`。
- ⚠️ **两点容易误解**：
  · 它只影响 `jh resume add`（`resume_cmd.py:81`）。**`jh resume split` 不传 id 用的是库里标为默认的那份简历**
    （`resumes.is_default`，`resume_cmd.py:222-229`），不是这个路径 ——
    `settings.py:296-297` 的注释写成"split 也用这份"，与代码不完全一致，按代码行为理解。
  · 路径含空格/中文：`.env` 里用正斜杠或双反斜杠、必要时加引号；文件不存在会明确报
    `找不到文件：<路径>`（`resume_cmd.py:97-99`），不会静默跳过。

---

## 10. 环境变量一览

**通用规则**：任何字段都能用 `JH_` + 字段名大写覆盖（`env_prefix="JH_"`，`settings.py:195`），
如 `JH_THRESHOLD=70`、`JH_LLM_CONCURRENCY=2`。**列表/字典类字段要写成 JSON**：
`JH_KEYWORDS='["AI Agent","Python 开发"]'`（难写，建议还是放 `config.yaml`）。

| 变量名 | 作用 | 默认 | 备注 |
|---|---|---|---|
| `JH_CONFIG` | 指定 config.yaml 路径 | 无（当前目录 → 项目根） | 等价于 `jh --config <路径>`；★ 文件不存在会**静默**回落默认值；必须是真环境变量，写 `.env` 里无效 |
| `DEEPSEEK_API_KEY` | 大模型密钥 | `""` | 官方写法（`.env.example`）；也认 `JH_DEEPSEEK_API_KEY` 与 `deepseek_api_key`（顺序即优先级） |
| `DEEPSEEK_BASE_URL` | API 地址 | `https://api.deepseek.com` | 自建网关/代理才需要改 |
| `DEEPSEEK_MODEL` | 模型名 | `deepseek-chat` | ★ 改了会让 LLM 缓存自动失效（key 含 model）→ 重新计费 |
| `JH_DATA_DIR` | 数据目录（库 + 抓屏产物） | `<项目根>/data` | 给"库放加密盘"或"测试隔离"用；**不搬旧数据** |
| `JH_OUTPUT_DIR` | 导出目录 | `<项目根>/output` | 同上 |
| `JH_STATE_DIR` | 运行时状态目录 | `<项目根>/state` | 同上 |
| `JH_DB_URL` | 直接给 SQLAlchemy 连接串 | 无（按 paths 推导） | 优先于上面三个目录；Windows 用正斜杠 |
| `JH_DEFAULT_RESUME_PATH` | 默认简历路径 | 无 | `jh resume add` 不传路径时用；含真名，只放 `.env` |
| `JH_LLM_PROVIDER` | Provider：`deepseek` / `mock` | `deepseek` | ★ 设成 `mock` 会拿到**假分数**（不报错）；测试与 `jh demo` 用它保证确定性 |
| `JH_LLM_CONCURRENCY` | 精评并发度 | `4`（1-16） | 个人工具，别调高 |
| `JH_LLM_MAX_RETRIES` | 业务层重试次数 | `1`（0-3） | 总尝试 = 1 + 该值 |
| `JH_THRESHOLD` | 匹配分数线 | `60`（0-100） | 也可 `jh today --min-score` / `jh match run --threshold` 临时覆盖 |
| `JH_SHOW_REMINDERS` | 是否显示顶部待处理提醒 | `true` | 单次用 `jh --no-remind` 关 |
| `JH_LOG_LEVEL` | 日志级别 | `INFO` | ★ 由 `utils/logging.py:154-163` **直接读 `os.environ`**，**不是** `AppSettings` 字段；写在 `.env` 里**无效**（`.env` 的值不会进 `os.environ`）。要 DEBUG 用真正的环境变量或 `jh -v/--verbose` |

> ⚠️ `.env` 与"真正的环境变量"不是一回事：`.env` 的键会被 pydantic-settings 喂给配置字段
> （所以 `JH_DB_URL`、`JH_DEFAULT_RESUME_PATH` 写在 `.env` 里有效），
> 但**不会**进入 `os.environ`（全项目没有调用 `load_dotenv`）——
> 凡是由代码直接读 `os.environ` 的名字（`JH_CONFIG`、`JH_LOG_LEVEL`）都必须在 `.env` 之外设置。

---

## 11. 常见改错（症状 → 原因 → 怎么改）

先记住统一的表现形式：配置有问题时 CLI 会打印

```
配置错误
配置有问题，请检查 config.yaml：
<pydantic 的字段级报错>
```

然后以退出码 1 结束（`settings.py:413-427` + `cli/app.py:125-128`）——
也就是说**大多数写错的字段都会在启动时就被点名**，而不是悄悄算错分。

| # | 症状 | 原因 | 怎么改 |
|---|---|---|---|
| 1 | 启动就 `配置错误`，报错里带字段名 | 某个字段没过校验 | 按报错里的字段名与当前值改。报错文本会直接给出数字，如 `match_weights 三项之和必须等于 100，当前是 95` |
| 2 | 所有岗位的分数都"偏低一点"，说不清为什么 | `match_weights` 三项之和 ≠ 100（老版本上会静默偏移） | 把三项改到合计 100；现在会被启动校验拦住。只想改一项时**三项都写全**（缺的会回落默认值，容易凑成 110） |
| 3 | 待投清单永远是空的 | `threshold` 定得比实际分数高 | 先 `jh match run --no-llm` 看规则层排名的分数分布，再把 `threshold` 调到略低于主流分数；临时试可用 `jh today --min-score 40`。`threshold: 200` 现在会直接报错（`less than or equal to 100`） |
| 4 | 明明有高分岗位，`jh today` 却一条都不出 | 分数线 + **同公司只保留最高分那条** + 已有"非 pending 投递记录"的岗位不再推（`today_svc.py:117-149`） | 用 `jh today --min-score 0` 对照；`jh job list` 看是不是同公司重复；`jh apply --pending` 看是不是已经打标了 |
| 5 | 开场白总是"模板味"，清单下面写着降级原因 | 四道闸门在拦：字数、禁用词、保真（不许提简历里没有的技能/数字）、沾边（必须提到 JD 关键词）（`greeting_svc.py:355-393`） | 按清单里写的原因改：补 `skill_aliases`、把"在做 demo 的技能"放进 `learning_whitelist` 并带"在学习"标注、放宽字数区间、去掉太短的禁用词 |
| 6 | 每条开场白的降级理由都是"用了禁用词「X」" | `greeting_banned_words` 里有太短的词，匹配是朴素子串 | 只留套话级词（贵司/三生有幸/冒昧打扰…），删掉"期待""我""的"这类会误伤的词 |
| 7 | 改完 `config.yaml` 好像完全没生效 | ① 不在仓库根运行；② `JH_CONFIG` 指向了不存在的文件（**静默**回落默认值）；③ 在 Python 里改环境变量后没调 `reset_settings()`（`lru_cache` 单例） | 用 `jh --config <路径> …` 显式指定；检查环境变量 `JH_CONFIG` 是否指错；Python 里改完环境变量要 `reset_settings()` |
| 8 | `.env` 里填了密钥，还是"未配置" | `.env` 只在**当前工作目录**找（`env_file=".env"`） | `cd` 到仓库根再跑 `jh`；确认 key 写在 `.env` 而不是 `config.yaml`；确认变量名是 `DEEPSEEK_API_KEY`（三种写法都认） |
| 9 | `Java` 被算成"会 JavaScript"，或 `Go` 命中 `Google` | 词表边界问题；中文别名缺写法 | 用 `skill_aliases` 把写法列全（§3）。ASCII 写法会自动加边界，**中文写法没有边界，必须自己写全** |
| 10 | "我在学 Kubernetes"被判成编造 | 没进 `learning_whitelist`，或进了但同一段文本里没有标注词 | 加进白名单，并让文案出现 `在学习`/`正在学`/`学习中`/`还在学`/`刚入门`/`初步了解` 之一（六个词写死在 `services/fidelity.py:143`） |
| 11 | `jh tailor` 说"改写后 N 字超过上限"，短句特别容易中招 | `length_floor_extra` 太小或为 0：短句的 150% 只有几个字 | 保持默认 30（或调大）。别为了"更严"设 0 —— 全是误报，最后你会把整个长度检查当噪声 |
| 12 | 想把抓取调快点，把延时设成 0 | `rate_limit` 是合规红线，不是性能参数 | 别改。要快就用 `jh match run --no-llm`、靠 LLM 缓存、一次少录几个岗位 |
| 13 | 改了 `data_dir`，投递记录"全没了" | 库文件不会自己搬家，新位置是个空库 | 旧库还在老位置（`<项目根>/data/jobhunter.db`）。要搬就手动移动 `jobhunter.db` **连 `.db-wal`/`.db-shm` 一起**（或先正常退出程序） |
| 14 | `.env` 里写 `JH_LOG_LEVEL=DEBUG` 没反应 | 它由 `utils/logging.py` 直接读 `os.environ`，而 `.env` 的值不进 `os.environ` | 用真正的环境变量（Windows：`$env:JH_LOG_LEVEL='DEBUG'`）或直接加 `jh -v/--verbose` |
| 15 | 改了提示词文件，感觉"完全没起作用" | LLM 缓存 key 含 `prompt_version`，正文改了但版本号没 +1 → 旧结果被复用 | 见 §12：改提示词**必须**把第一行 `<!-- prompt-version: N -->` 加一 |
| 16 | 分数看起来都差不多，或文案里带「（mock）」 | 环境里设了 `JH_LLM_PROVIDER=mock`（假 Provider，固定返回 `score: 70` 等写死结果） | 取消这个环境变量（`jh demo` 才会主动用它跑演示），或换回 `deepseek` |

---

## 12. 改配置后要做什么

总原则：**库里已经存下来的东西（分数 / JD 关键词 / 定制版本 / 投递记录）不会因为你改配置而自动重算。**
`jh today` 读的是库里存好的 `jobs.match_score`，所以"改了权重但分数没变"是预期行为，不是 bug。

| 你改了什么 | 要不要重跑 | 为什么 |
|---|---|---|
| `keywords`、`match_dimensions` | **什么都不用做** | 这两个字段不参与打分（§1、§2） |
| `cities`、`job_type`、`match_weights`、`skill_aliases` | **`jh match run`**（重新打分） | 分数是打分时算出来**存进库里**的（`jobs.match_score`）；JD 关键词与技能命中也在那时用当前词表算出并落 `jd_keywords_json`（`match_svc.py:902, 1059`；简历技能池同样是打分时按当前词表重算的，`match_svc.py:972-1002`）。★ 默认**跳过已投 / 已跳过的岗位**（ISSUE #27：给不会再投的岗位重算纯属浪费钱），要让它们也一起重算就加 `--all`；本次"算了多少条、跳过多少条"会打在屏幕上 |
| `threshold` | **不必重跑打分**，但清单会立刻变 | `jh today` 是实时读配置过滤的（`today_svc.py:115`）；`jh match run` 里的分数线只用于显示小结 |
| 简历文件本身（改了 docx） | **`jh resume add`（重新导入）→ `jh match run`** | 同一份内容重复导入不会新建记录；内容变了 `content_hash` 就变，而它是 LLM 缓存 key 的一部分（`resume_svc.py:30`），旧分自动失效 |
| `greeting.*`、`greeting_banned_words`、`learning_whitelist` | 重新跑 **`jh today`** | 开场白是每次运行现生成的；闸门每次都会重新判定。★ 现在**LLM 缓存也会随之失效**（ISSUES #55）：缓存 key 里含"整份提示词的指纹"，而这些配置都会进提示词 —— 所以不会再把旧文案当成新的给你 |
| 提示词（仓库根的 `prompts/tailor.md` / `question.md` / `followup.md`，以及代码内嵌的 `PROMPT_VERSION` / `GREETING_PROMPT_VERSION` 常量） | **必须把版本号 +1**，然后重跑对应命令 | 缓存 key 含 `prompt_version`，"改了正文不 +1 就等于没改"（`llm/prompts.py:9-11`；key 定义见 `llm/cache.py:99`）。文件型提示词改第一行的 `<!-- prompt-version: N -->`；内嵌的改常量（开场白在 `services/greeting_svc.py:71`，精评在 `services/match_llm_svc.py:81`） |
| `tailor.*` | 只影响**之后新生成**的定制版本 → `jh tailor analyze <job_id>` | 已存下来的 `resume_versions` 不会被改写 |
| `rate_limit.*` | 下一次抓取（`jh job add`）即生效 | 延时在抓取时代入（`adapters/manual.py:186`） |
| `DEEPSEEK_MODEL` | 下次调用生效，缓存**自动失效** | 缓存 key 含 model → 会重新计费，这是预期的 |
| `.env` 里的密钥 / `JH_DB_URL` / `JH_DATA_DIR` 等 | 下一次进程启动生效 | ★ 换了 `JH_DATA_DIR`/`JH_DB_URL` 等于换了库：旧数据在旧库里，需要自己搬（§9） |

---

## 13. 附：`show_reminders` / `config_file`，以及全部 27 个字段索引

### `show_reminders`

- **是什么**：任何 `jh` 命令顶部那行"📌 待处理：…"提醒的开关。
- **默认值**：`True`。**取值/校验**：布尔；写 `"maybe"` 会报
  `Input should be a valid boolean, unable to interpret input`（`yes/no/on/off/1/0` 都能识别）。
- ⚠️ **写错了会怎样**：`false` 只是不显示提醒，**不影响提醒本身的统计**（`jh apply --pending` 照样能看）。
  设计上"没有待处理事项时一个字都不打印"，所以它不会变成噪音（`cli/app.py:130-133`）。
  单次命令可以用 `jh --no-remind <命令>` 临时关掉。

```yaml
show_reminders: true
```

### `config_file`

- **是什么**：**config.yaml 自己的位置**，由 CLI 注入（`jh --config` / `JH_CONFIG` 的落地处），
  用来让 `settings.paths.root` 指向 config.yaml 所在目录（`settings.py:289-293, 362-375`）。
- **默认值**：`None`（不传就是 None，此时项目根退回 `find_project_root()` 的探测结果）。
- ⚠️ **不要手写它**：它**不是**"用户配置"，写进 `config.yaml` 没有意义
  （它是一个 `Path` 字段，写一个不存在的路径只会让"项目根"算到别处）。
  要换配置文件，用 `jh --config <路径>` 或环境变量 `JH_CONFIG`。

### 全部 27 个 `AppSettings` 字段索引

`python -c "…; print(sorted(AppSettings.model_fields))"` 打出来的字段全部在下面（一个不漏）。
嵌套小节里的子字段（`match_weights` 3 个、`greeting` 4 个、`tailor` 5 个、`rate_limit` 3 个，共 15 个）
在各节里逐条写了。

| 字段 | 在哪一节 | 一句话作用 | 默认值 |
|---|---|---|---|
| `keywords` | §1 | 求职意图声明（**目前不参与打分**） | 4 条方向词 |
| `cities` | §1 | 意向城市（硬条件之一，精确匹配） | 深圳/广州/东莞 |
| `job_type` | §1 | 实习 / 全职 / 都要 | `both` |
| `threshold` | §2 | 分数线（`jh today` 的过滤线） | `60` |
| `daily_limit` | §2 | 每日投递提醒额度（**不硬拦**） | `20` |
| `llm_top_n` | §2 | 只对规则分前 N 做 LLM 精评 | `30` |
| `match_weights` | §2 | 三维度权重（合计必须 100） | 30/30/40 |
| `match_dimensions` | §2 | 打分维度的可读声明（**无代码消费**） | 3 项 |
| `skill_aliases` | §3 | 技能别名/归一化（解 `Java`≠`JavaScript`） | `{}` |
| `learning_whitelist` | §3 | "我在学"白名单（提了必须带标注；**词必须是词表里已有的技能**） | `[]` |
| `greeting_banned_words` | §4 | 开场白禁用词（不许空项/纯空白/超 12 字） | `[]` |
| `greeting` | §4 | 开场白字数约束 + GitHub | 80/150/60/`""` |
| `tailor` | §5 | 定制简历的硬约束 | 1.5/30/12/3/2 |
| `rate_limit` | §6 | 随机延时（合规红线；~~翻页上限~~ 已删，#18） | 2.0/5.0 |
| `show_reminders` | §13 | 顶部待处理提醒开关 | `true` |
| `deepseek_api_key` | §7 | 密钥（只从 `.env`/环境变量来） | `""` |
| `deepseek_base_url` | §7 | API 地址 | `https://api.deepseek.com` |
| `deepseek_model` | §7 | 模型名（改了缓存自动失效） | `deepseek-chat` |
| `llm_provider` | §7 | `deepseek` / `mock` | `deepseek` |
| `llm_concurrency` | §7 | 精评并发度 | `4`（1-16） |
| `llm_max_retries` | §7 | 业务层重试次数 | `1`（0-3） |
| `db_url` | §9 | 直接给库连接串（优先于目录） | `None` |
| `data_dir` | §9 | 数据目录（库 + 抓屏产物） | `None`（→ `<项目根>/data`） |
| `output_dir` | §9 | 导出目录 | `None`（→ `<项目根>/output`） |
| `state_dir` | §9 | 运行时状态目录 | `None`（→ `<项目根>/state`） |
| `default_resume_path` | §9 | 默认简历（只放 `.env`） | `None` |
| `config_file` | §13 | config.yaml 自身位置（CLI 注入） | `None` |

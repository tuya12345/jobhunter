# JobHunter · 数据模型定稿（提示词 0 产出②）

> **11 张表**：`resumes` / `resume_versions` / `jobs` / `applications` / `application_events` /
> `interviews` / `interview_questions` / `assessments` / `offers` / `companies` / `llm_cache`
>
> 本文 = schema 设计 + 索引与约束的**理由** + Alembic 初始迁移方案。实现落在 `models/` 与
> `alembic/versions/0001_initial_schema.py`。

---

## 0. 全局约定（先定死，避免逐表重复）

| 约定 | 做法 | 理由 |
|---|---|---|
| 主键 | `id INTEGER PRIMARY KEY AUTOINCREMENT`（SQLAlchemy `Integer + autoincrement`） | 单机自用，整数主键最省事、便于 `jh apply 3=applied` 这种序号打标 |
| 时间列 | 全部 `UTCDateTime`（自定义 TypeDecorator） | SQLite 不存时区。写入时统一转 **UTC aware**，读出来也是 aware；展示层再转 Asia/Shanghai。**不用已弃用的 `utcnow()`** |
| 命名约定 | `MetaData(naming_convention=...)` 显式指定 ix/uq/ck/fk/pk 命名模板 | 否则 SQLite 上 Alembic **无法 drop 匿名约束**，将来改表要删库 |
| 外键 | 全部显式声明 + 每连接 `PRAGMA foreign_keys=ON` | SQLite 默认**不开**外键，必须手动开，否则约束形同虚设 |
| 级联 | 从属数据 `ondelete="CASCADE"`（事件/问题/面试/笔试）；**引用型不级联**（Set NULL / 限制） | 删投递要连带清事件；但删岗位**不能**连带删掉我的投递记录（那是我的资产） |
| JSON 列 | `Text` 存 JSON 字符串，命名后缀 `_json`，ORM 侧提供 `dict` 便捷属性 | SQLite 无原生 JSON 类型；显式 `_json` 后缀一眼看出"这是字符串不是对象" |
| 布尔列 | `Boolean` + `server_default` | SQLite 存 0/1，跨库行为一致 |
| 空字符串 vs NULL | 未抓到的字段存 **NULL**，不存 `""` | 将来 `WHERE salary_min IS NOT NULL` 才准 |

### ★ UTCDateTime 的实现要点（P0 就要写对）

```
process_bind_param (写入)：naive → 视为 UTC 并补 tzinfo；aware → astimezone(UTC)
process_result_value (读取)：SQLite 返回 naive → 补 tzinfo=UTC
```
**为什么要这么绕**：SQLite 只有 TEXT/REAL/INTEGER，没有 `TIMESTAMP WITH TIME ZONE`。
如果不补，`aware - naive` 相减会直接抛 `TypeError`，而且"投递 3 天未跟进"这类计算
会在跨时区/夏令时上出错。

---

## 1. `resumes` —— 简历主表

| 列 | 类型 | 可空 | 说明 |
|---|---|---|---|
| `id` | Integer PK | | |
| `name` | Text | ✅ | 简历名，如"主简历" |
| `direction` | Text | ✅ | 方向，如 `AI Agent` / `前端`（`jh resume split` 帮我拆） |
| `file_path` | Text | ✅ | **原始 docx 路径**（只记路径，不复制文件入库） |
| `content_json` | Text(`_json`) | ✅ | 结构化解析结果：`{basic{...}, education[], skills[], projects[], internships[], raw_text}` |
| `content_hash` | Text | ✅ | **内容 hash（sha256）** ★ LLM 缓存 key 的组成部分 → 改简历自动失效旧分 |
| `is_default` | Boolean | | 默认 `false`；匹配时挑不到方向就用默认简历 |
| `created_at` | UTCDateTime | | 默认 `now(UTC)` |

**索引**：`ix_resumes_direction`（`direction`）、`ix_resumes_content_hash`（`content_hash`，缓存命中查得快）。
**为什么 `content_hash` 必须存**：不然缓存 key 每次都要重算全文 hash，且"简历改了但 key 没变"的 bug 会重现（评审原始问题 #3）。

---

## 2. `resume_versions` —— 定制简历版本（P5）

| 列 | 类型 | 可空 | 说明 |
|---|---|---|---|
| `id` | Integer PK | | |
| `resume_id` | FK→`resumes.id` | ❌ | `ondelete=CASCADE`（删简历就删它的定制版本） |
| `job_id` | FK→`jobs.id` | ✅ | 为哪个岗位定制；**`ondelete=SET NULL`**：岗位被清理也不该丢失我的定制稿记录 |
| `file_path` | Text | ✅ | 导出的**新 docx 路径**（绝不覆盖原件） |
| `suggestions_json` | Text(`_json`) | ✅ | `{match_analysis{...}, suggestions[{section, original, revised, reason}]}` |
| `approved_ids` | Text(`_json`) | ✅ | 我逐条批准的建议 id 列表（人工审核留痕） |
| `fidelity_report_json` | Text(`_json`) | ✅ | ★ **FidelityGuard 检查结果**（通过/拦截项+原因），可回滚、可复盘 |
| `created_at` | UTCDateTime | | |

**索引**：`ix_resume_versions_resume_id`、`ix_resume_versions_job_id`。

---

## 3. `jobs` —— 岗位表（最重的一张，字段最多）

| 列 | 类型 | 可空 | 说明 |
|---|---|---|---|
| `id` | Integer PK | | 也是 `jh apply 3=applied` 里那个"序号"的来源 |
| `platform` | Text | ❌ | `boss` / `manual` / `official`… |
| **`platform_job_id`** | Text | ✅ | ★ **平台内唯一 ID —— 去重最可靠的一层** |
| `company` | Text | ✅ | |
| `title` | Text | ✅ | |
| `city` | Text | ✅ | |
| `experience` | Text | ✅ | 原文经验要求，如"3-5年"（保留原文，别急着解析丢信息） |
| `education` | Text | ✅ | 学历要求 |
| **`is_intern`** | Boolean | ✅ | ★ intern / fulltime 区分（原表漏了）；`NULL` = 未识别 |
| `salary_text` | Text | ✅ | 原文薪资，如"15-25K·14薪" |
| **`salary_min` / `salary_max`** | Integer | ✅ | ★ 数值化才能按钱筛/排序 |
| `jd_text` | Text | ✅ | JD 正文 |
| **`jd_hash`** | Text | ✅ | ★ `sha256(归一化 jd_text)`；LLM 缓存 key 的组成部分 |
| **`jd_fetch_status`** | Text | ✅ | ★ `ok` / `failed` / `partial` —— 抓取失败必须留痕，不能假装有 JD |
| `url` | Text | ✅ | |
| **`source_keyword`** | Text | ✅ | ★ 来自哪个搜索词/来源，用于"哪个渠道有效"复盘 |
| **`dedup_norm_hash`** | Text | ✅ | ★ `sha256(归一化(company｜title｜city))` 兜底去重 |
| **`jd_keywords_json`** | Text(`_json`) | ✅ | ★★ **共享中间层产物**，被 ①匹配 ②定制简历 ③开场白 三处复用 |
| **`rule_score`** | Integer | ✅ | ★ 规则层总分 0-100（0 成本，先算） |
| **`rule_detail_json`** | Text(`_json`) | ✅ | ★ 分维度明细 → `jh match explain <job_id>` 打印；**调权重靠它归因** |
| `llm_score` | Integer | ✅ | LLM 精评分（只对 Top N 算） |
| `match_score` | Integer | ✅ | 最终分（默认= `llm_score`，没有就退化为 `rule_score`） |
| `matched_resume_id` | FK→`resumes.id` | ✅ | `ondelete=SET NULL` |
| `reason_json` | Text(`_json`) | ✅ | LLM 给的 `{strengths[], gaps[], missing_keywords[], recommended_resume, reason}` |
| `status` | Text | ❌ | `new → scored → queued → applied / skipped`，默认 `new` |
| `found_at` | UTCDateTime | | 首次入库 |
| `last_seen_at` | UTCDateTime | | ★ 重复遇到时**只更新这个**（证明岗位还挂着） |
| `created_at` | UTCDateTime | | |

### 索引（6 个，每个都有明确用途）

| 索引 | 列 | 用途 |
|---|---|---|
| `ix_jobs_platform_job_id` | `platform_job_id` | ★ 去重第一层，命中率最高 → 必须走索引 |
| `ix_jobs_dedup_norm_hash` | `dedup_norm_hash` | 去重第二层兜底 |
| `ix_jobs_status` | `status` | `jh job list --status`、`jh today` 排除已投 |
| `ix_jobs_match_score` | `match_score` | `jh today` 按分降序 |
| `ix_jobs_company_city` | `(company, city)` | ★ 复合索引：**同公司同天不推多条**的去重查询 |
| `uq_jobs_platform_platform_job_id` | UNIQUE `(platform, platform_job_id)` | 数据库层兜住重复（`NULL` 不参与唯一约束，手抓岗位不受影响） |

### ★★ 去重规则（写进代码注释，评审重点）

```
命中已有记录（platform_job_id 或 dedup_norm_hash）时：
  【只更新】last_seen_at、salary_text、jd_fetch_status、jd_text/jd_hash（内容变了才更新）
  【绝不覆盖】status、match_score、llm_score、rule_score、rule_detail_json、
              matched_resume_id、reason_json、jd_keywords_json
              —— 这些是"人工决策 + 已花成本算出来的结果"
```
**为什么**：去重命中如果顺手 `UPDATE` 覆盖，会把我 `skipped` 掉的岗位重新变回 `new`、
把已经算好的分数清空、甚至让我改了简历后重复付费调用 LLM。这是评审明确点出的坑（#4）。

---

## 4. `applications` —— 投递记录

| 列 | 类型 | 可空 | 说明 |
|---|---|---|---|
| `id` | Integer PK | | |
| `job_id` | FK→`jobs.id` | ❌ | **`ondelete=RESTRICT`**（默认）：有投递记录的岗位不许被静默删除 |
| `resume_version_id` | FK→`resume_versions.id` | ✅ | `SET NULL`（用的是哪版简历） |
| `platform` | Text | ✅ | |
| `channel` | Text | ✅ | ★ 招聘网站 / 官网 / 内推 / HR直招 —— 渠道转化率统计的口径来源 |
| `greeting_text` | Text | ✅ | 开场白（**含完整版**） |
| `greeting_variant` | Text | ✅ | ★ `with_project` / `without_project`（A/B 只测这一个变量） |
| **`greeting_sent`** | Boolean | | ★ 闭环：**是否真的发出去了** |
| **`greeting_edited`** | Boolean | | ★ 闭环：**发之前我改没改**（衡量生成质量的关键信号） |
| `status` | Text | ❌ | `pending → applied → viewed → replied → assessment → interview → offer / rejected`，默认 `pending` |
| `applied_at` / `viewed_at` / `replied_at` | UTCDateTime | ✅ | 关键节点时间戳（"投后 3/7/14 天"提醒靠它算） |
| `note` | Text | ✅ | |

**索引**：`ix_applications_job_id`、`ix_applications_status`（`jh apply --list-pending`）、
`ix_applications_applied_at`（★ **日额度与"投后 N 天"统计**：`jh today --check` 今天已投几条 = 按 `applied_at` 当天的 count）。
**不额外建计数表**：日额度**从 `applications.applied_at` 实时聚合**得出 —— 单一事实来源，不会出现"计数器和真实记录对不上"。

**字段更新纪律**：`status` 变更**必须同时写 `application_events`**（下一张表），
状态列是"当前值"，事件流是"历史"，两者永不冲突。

---

## 5. `application_events` —— 事件流（append-only）★

| 列 | 类型 | 可空 | 说明 |
|---|---|---|---|
| `id` | Integer PK | | |
| `application_id` | FK→`applications.id` | ❌ | `ondelete=CASCADE`（投递都没了，它的历史也留着没意义） |
| `event_type` | Text | ❌ | `status_change` / `followup` / `note` / `greeting_sent` / `assessment_invite` … |
| `from_status` / `to_status` | Text | ✅ | 状态变更前后 |
| `payload_json` | Text(`_json`) | ✅ | 自由扩展（谁改的、原话术、失败原因…） |
| `occurred_at` | UTCDateTime | ❌ | ★ **业务时间**（"什么时候真的发生了"，可能是我事后补记的） |
| `created_at` | UTCDateTime | | **系统时间**（"什么时候写进库的"） |

**索引**：`ix_application_events_application_id`、`ix_application_events_occurred_at`。

**为什么用它取代单用途的 `followups` 表**（评审采纳项）：
- 表数不增（不是一个新表，是"把 followups 泛化"）
- 功能更多：能画投递时间轴、能归因"这单为啥黄了"
- **append-only 的硬好处**：历史不可篡改 → 统计口径永远可复现。
  `occurred_at` 与 `created_at` 分开，是为了区分"真实发生时间"和"我补记的时间"
  （补记不会污染时间轴）。

---

## 6. `interviews` —— 面试

| 列 | 类型 | 可空 | 说明 |
|---|---|---|---|
| `id` | Integer PK | | |
| `application_id` | FK→`applications.id` | ❌ | `CASCADE` |
| `round` | Text | ✅ | `一面` / `二面` / `HR面` |
| `scheduled_at` | UTCDateTime | ✅ | ★ "本周面试 N 场"提醒靠它 |
| `mode` | Text | ✅ | 视频 / 电话 / 现场 |
| `meeting_link` | Text | ✅ | |
| `location` | Text | ✅ | |
| `status` | Text | ❌ | `待面 → 已面 → 出结果`，默认 `待面` |
| `note` | Text | ✅ | |

**索引**：`ix_interviews_application_id`、`ix_interviews_scheduled_at`（`jh interview list --upcoming` 倒序取）。

---

## 7. `interview_questions` —— 面试问题 + 我的回答 + 自评

| 列 | 类型 | 可空 | 说明 |
|---|---|---|---|
| `id` | Integer PK | | |
| `interview_id` | FK→`interviews.id` | ✅ | `CASCADE`。★ **P6 由 NOT NULL 改为可空**（迁移 `0002`） |
| `job_id` | FK→`jobs.id` | ✅ | ★ **P6 新增**：`predicted`（JD 预测题）挂岗位；`SET NULL` |
| `question` | Text | ❌ | |
| `my_answer` | Text | ✅ | |
| `rating` | Text | ✅ | ★ 3 档：`bad` / `ok` / `good`（不搞 5 档，样本太小没意义） |
| `category` | Text | ✅ | 知识点，如"前端性能"→ `jh weak` 按它聚合 |
| `is_weak` | Boolean | | ★ 手动标记"没答上来"→ `jh weak` 按**没答上来次数**降序 |
| **`source`** | Text | ✅ | ★★ `recorded`（我记的） / `predicted`（**JD 预测的**）—— 让"记录工具"变成"准备工具" |
| `created_at` | UTCDateTime | | |

**索引**：`ix_interview_questions_interview_id`、`ix_interview_questions_job_id`、
`ix_question_category`（`category`）、`ix_question_is_weak`（`is_weak`）→ `jh weak` 聚合走索引。
**为什么 `source` 很重要**：预测题和真实被问的题**必须能区分**，否则 `jh weak` 会被
"我自己猜的题"污染，结论失真。

**为什么改可空 + 加 `job_id`（迁移 `0002`，P6 实现时才暴露的设计缺口）**：

两张来源的**归属**根本不同 —— 真实题属于**某一场面试**，预测题属于**某个岗位**。
而 `jh question predict <job_id>` 拿的是岗位，这个动作又通常发生在"面试还没约上"的时候。
坚持 NOT NULL 只有两条路：逼用户为一个还不存在的面试编时间，或者造一条假的 `interviews`
记录来挂预测题 —— 后者会污染 `jh interview list`、`jh stats` 的面试场次统计、
以及顶部"本周面试"提醒。**为了装下一条数据而污染三个功能的输入**，所以改 schema。

回滚（`downgrade`）会先删掉 `interview_id IS NULL` 的预测题再改回 NOT NULL：
这是**有损回滚**，但只影响 predicted 行，真实记录一条都不动（迁移文件里写明了）。

---

## 8. `assessments` —— 笔试 / 在线测评 / AI 面 ★（v3 新增）

| 列 | 类型 | 可空 | 说明 |
|---|---|---|---|
| `id` | Integer PK | | |
| `application_id` | FK→`applications.id` | ❌ | `CASCADE` |
| `kind` | Text | ❌ | `笔试` / `测评` / `AI面` |
| `invited_at` | UTCDateTime | ✅ | 收到邀请 |
| **`deadline_at`** | UTCDateTime | ✅ | ★★ **截止时间** → "⏳ 笔试截止 1 条（9-11 饿了么）"提醒的数据源 |
| `platform` | Text | ✅ | 牛客 / 赛码 / 北森… |
| `status` | Text | ✅ | 待做 / 已做 / 已过期 |
| `score` | Text | ✅ | 用 Text 不用 Integer：很多测评不给分只给"通过/未通过" |
| `note` | Text | ✅ | |

**索引**：`ix_assessments_application_id`、`ix_assessments_deadline_at`（★ 顶部提醒查"即将截止"）。
**为什么必须有**：海投**必定**遇到笔试/测评，而 v2 的状态机里根本没有这个环节 —— 漏了会导致
"岗位卡在 replied 就再也不会提醒我"。

---

## 9. `offers` —— Offer ★（v3 新增）

| 列 | 类型 | 可空 | 说明 |
|---|---|---|---|
| `id` | Integer PK | | |
| `application_id` | FK→`applications.id` | ❌ | `CASCADE` |
| `company` / `title` | Text | ✅ | 冗余存一份：offer 到手后可能脱离原岗位记录独立比较 |
| `salary_text` | Text | ✅ | 原文 |
| **`salary_month`** | Integer | ✅ | 月薪（数值化才能横向比） |
| `months` | Integer | ✅ | 发几个月（如 14）→ 年包 = `salary_month × months` |
| `location` | Text | ✅ | |
| **`deadline_at`** | UTCDateTime | ✅ | ★★ **签约截止日** → `jh offer compare` + 截止提醒 |
| `status` | Text | ✅ | 待定 / 已接受 / 已拒绝 |
| `pros` / `cons` | Text | ✅ | 优缺点（决策留痕，将来复盘"当时为什么选它"） |
| `note` | Text | ✅ | |

**索引**：`ix_offers_application_id`、`ix_offers_deadline_at`。
**为什么必须有**：v2 的 8 张表里**居然没有 Offer** —— 整个工具的终点站缺失。

---

## 10. `companies` —— 官网投递线索（P7）

| 列 | 类型 | 可空 | 说明 |
|---|---|---|---|
| `id` | Integer PK | | |
| `name` | Text | ❌ | 公司名 |
| `official_url` | Text | ✅ | 官网招聘页 |
| `source_platform` | Text | ✅ | 贴吧 / 小黑盒 / 小红书 / 知乎 / 微信公众号 |
| **`source_type`** | Text | ✅ | ★ 官网 / 内推 / HR直招 —— 决定"该怎么投"，导出时按它分组 |
| `evidence_text` | Text | ✅ | 线索原文（"在哪看到的"） |
| `evidence_url` | Text | ✅ | |
| `verified` | Boolean | | 可达性检查过了吗（HEAD 记状态码） |
| `note` | Text | ✅ | |
| `created_at` | UTCDateTime | | |

**索引**：`ix_companies_name`（同公司合并去重）、`ix_companies_source_type`（`jh leads export` 分组）。

---

## 11. `llm_cache` —— LLM 缓存 ★（v3 新增）

| 列 | 类型 | 可空 | 说明 |
|---|---|---|---|
| **`cache_key`** | Text **PRIMARY KEY** | ❌ | ★★ `sha256(prompt_version ｜ model ｜ jd_hash ｜ resume.content_hash)` |
| `task` | Text | ✅ | `match` / `greeting` / `tailor` / `jd_parse` / `question_predict` / `leads_parse` |
| `response_json` | Text(`_json`) | ❌ | 原始结构化响应 |
| `tokens_in` / `tokens_out` | Integer | ✅ | ★ 成本可见 |
| `cost` | Numeric/Real | ✅ | ★ 累计花了多少钱（DeepSeek 便宜也要能看见） |
| `created_at` | UTCDateTime | | |

**索引**：主键即索引；另加 `ix_llm_cache_task`（按任务统计命中率/成本）。

### ★★ 为什么缓存 key 必须**包含简历 hash**（评审原始 bug）

v2 的 key 只用 `jd_hash`。后果：**我改了简历，同一个岗位的匹配分仍然返回旧值** ——
而这恰恰是这个工具最核心的价值（"我改了简历，分数该变"）。
`prompt_version` 也是必须的：**提示词改了，旧结果就不该复用**（否则调提示词毫无效果，
你会以为"改了没用"）。

`model` 同理：DeepSeek 换模型/换版本，缓存必须失效。

---

## 12. 表间关系总览

```
resumes ──1:N──> resume_versions ──N:1──> jobs
   │                                        │
   └──1:N（matched_resume_id，SET NULL）────┘
                                            │
                                    1:N (RESTRICT)
                                            ▼
                                       applications ──1:N──> application_events   (CASCADE, append-only)
                                            ├──1:N──> interviews ──1:N──> interview_questions (CASCADE)
                                            ├──1:N──> assessments  (CASCADE)
                                            └──1:N──> offers       (CASCADE)

companies   : 独立（P7 线索池，通过 applications.channel=官网/内推 复用看板）
llm_cache   : 独立（cache_key 主键，无外键 —— 缓存不该被业务删除牵连）
```

**外键删除策略为什么这样定**：

| 关系 | 策略 | 理由 |
|---|---|---|
| `applications → jobs` | **RESTRICT** | 投递记录是我的资产。删岗位前必须先处理投递，**防误删历史** |
| `application_events / interviews / questions / assessments / offers → applications` | CASCADE | 这些是投递的**附属数据**，脱离投递没有意义 |
| `resume_versions → resumes` | CASCADE | 简历删了，基于它的定制稿也没法追溯 |
| `resume_versions / jobs → jobs / resumes`（引用型） | SET NULL | **不要因为删了一个岗位就丢了我的定制稿记录** |
| `llm_cache` | 无外键 | 缓存是纯派生数据，与业务生命周期解耦；清缓存随时可做 |

---

## 13. Alembic 初始迁移方案（`0001_initial_schema`）

### 13.1 配置

| 文件 | 关键内容 |
|---|---|
| `alembic.ini` | `script_location = alembic`；**不写死 `sqlalchemy.url`**（URL 从代码注入，避免把真实路径提交进仓库） |
| `alembic/env.py` | ① `from jobhunter.config.settings import get_settings` 取 DB URL；② `from jobhunter.models import Base` → `target_metadata = Base.metadata`；③ `render_as_batch=True`（★ **SQLite 必须开**：SQLite 不支持 `ALTER COLUMN`，batch 模式用"建新表→拷数据→换名"模拟，否则将来改字段直接失败） |
| `alembic/script.py.mako` | 标准模板（略作中文化注释） |

### 13.2 迁移内容（`upgrade()`）

按**依赖顺序**建表（被引用者先建）：

```
1  resumes
2  jobs                      (FK: matched_resume_id → resumes)
3  resume_versions           (FK: resume_id → resumes, job_id → jobs)
4  applications              (FK: job_id → jobs[RESTRICT], resume_version_id → resume_versions)
5  application_events        (FK: application_id → applications[CASCADE])
6  interviews                (FK: application_id → applications[CASCADE])
7  interview_questions       (FK: interview_id → interviews[CASCADE])
8  assessments               (FK: application_id → applications[CASCADE])
9  offers                    (FK: application_id → applications[CASCADE])
10 companies
11 llm_cache
```

每表随后 `create_index(...)` —— 共 **21 个索引/唯一约束**（含 6 个 jobs 索引 + 1 个唯一约束），
清单见上文各节表格。

`downgrade()`：**逆序 drop**（先删有外键依赖的），保证 `upgrade head → downgrade base → upgrade head`
可反复跑通 —— 这一条会写成 P0 的测试。

### 13.3 `jh init` 的执行流程

```
1. 解析配置（config.yaml + .env）→ 得到 DB 路径、数据目录
2. 建目录：data/（SQLite 文件所在）、output/、state/
3. 用 Alembic 的 Python API 执行 upgrade head：
      from alembic import command; from alembic.config import Config
      command.upgrade(cfg, "head")
   （不用 subprocess 调 alembic 命令：跨平台更稳、错误可捕获、Windows 上不会有路径/编码坑）
4. 建 engine → 执行 PRAGMA（WAL / busy_timeout=5000 / foreign_keys=ON）→ 验证：
      PRAGMA journal_mode  == wal
      PRAGMA foreign_keys  == 1
5. 打印小结：库路径、当前 alembic 版本、11 张表就位
```
**幂等要求**：重复 `jh init` 不报错、不重建、不清数据（第二次只打印"已是最新"）。

### 13.4 P0 要写的迁移相关测试

| 测试 | 断言 |
|---|---|
| `test_smoke.py` | `jh init` 后 11 张表**全部存在**；`alembic_version` 表 = `0001`；PRAGMA 三项符合预期 |
| 同上 | `upgrade head → downgrade base → upgrade head` 三连跑通（证明迁移可逆） |
| 同上 | `jh init` **跑两次**不报错（幂等） |
| 同上 | 插一条 `applications` 指向不存在的 `job_id` → **必须抛 IntegrityError**（证明 `foreign_keys=ON` 真的生效，不是摆设） |

最后一条是重点：**SQLite 默认不开外键**，如果没显式开 PRAGMA，这条测试会"静默通过"
（插进去不报错）—— 它专门用来守住这个坑。

---

## 14. P0 范围声明

P0 **只建表结构与迁移**，不写任何业务查询逻辑。
- ✅ 落地：`models/` 全部 ORM 定义、`alembic/versions/0001_initial_schema.py`、`storage/db.py`、`storage/migrate.py`
- ❌ 不在 P0：`job_svc` 去重逻辑（P2）、`match_svc` 打分（P3）、`fidelity`（P5）等
- 数据表虽多，但**只有 `jh init` 会真正用到它们**（建表）；其余命令 P0 阶段是占位。

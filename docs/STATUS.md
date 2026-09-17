# STATUS —— 阶段交付说明（给验证会话看）

> **维护规则**：每完成一个阶段，由开发角色更新本文件，然后停下等验证。
> 内容只写**事实与本阶段范围**：做了什么 / 关键文件 / 已知问题 / 建议重点验证的地方。
> **不写自评结论、不写对抗性测试** —— 那是验证会话的职责。

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

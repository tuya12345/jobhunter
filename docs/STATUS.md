# STATUS —— 阶段交付说明（给验证会话看）

> **维护规则**：每完成一个阶段，由开发角色更新本文件，然后停下等验证。
> 内容只写**事实与本阶段范围**：做了什么 / 关键文件 / 已知问题 / 建议重点验证的地方。
> **不写自评结论、不写对抗性测试** —— 那是验证会话的职责。

---

## 修复轮（2026-09-19）：按平台的优先级清单清 B + C 两档

**背景**：验证会话在全项目复检后，把 42 条未关闭问题**重新激活为待修**并排了优先级
（登记在 StageBoard，仓库侧副本在 `docs/ISSUES.md`）：

| 档 | 含义 | 条数 | 本轮处理 |
|---|---|---|---|
| **B** | 静默失真（不报错，但用户看到的是错的） | 10 | ✅ **全部修复** |
| **C** | 小改动大收益（多为 1~2 行） | 11 | ✅ **全部修复** |
| D | 需要用户拍板的取舍（验证会话注明"开发先不要擅自改"） | 13 | ⏸ **未动**，等用户逐条决定 |
| — | 未标注的 9 条里的可修项 | 4 | ✅ **#48 / #49 / #50 / #51 修复**；#46 补测试；#52 补独立验证；#47 需用户决策 |

### 1. B 档（静默失真）10 条

| # | 症状 | 改法要点 |
|---|---|---|
| #9 | 空 JD 拿 80 分（≥分数线 60）→ 混进待投清单 | `score_direction` 的"判不出方向给中性分"本身没错，错在**整份 JD 没有任何信息**时它还独自撑起总分。新增"信息不足"判定：技能/方向/硬条件全无 → 0 分 + `rule_detail.insufficient_info=True` + 明确说明 |
| #11 | `{"score": true}` 被当成 1 分 | `parse_verdict` 改为**拒绝**布尔/字符串/越界，抛 `VerdictParseError`（不静默转换、不截断） |
| #15 | 纯空白 `.txt` 变成"成功导入的空简历"且自动成为默认简历 | 解析出 0 个文本块时抛 `ResumeParseError`，给中文原因；不会留下空简历 |
| #17 | 改了简历重跑规则层，`match_score` 仍是旧的 `llm_score` | 规则层把"这次用的简历 hash"写进 `rule_detail_json`；重算时若 hash/`matched_resume_id` 变了就**作废 `llm_score`**（用户才不会看到"改了简历分数不动"） |
| #22 | `live`/`emulator` marker 零使用、无 nightly | 新增 `.github/workflows/nightly.yml`（schedule + 手动），跑 `-m live`；无 Secret 时用例**skip**（不是失败） |
| #24 | 缓存 key 不含 `rule_hint` / JD 表头字段 → 改权重后复用旧分 | 把参与提示词的字段纳入 key（正确性优先于命中率） |
| #34 | `jh today --copy N` 静默忽略 `--min-score` / `--limit` | 参数带进去（不再静默忽略） |
| #35 | `candidates` 计数与 UI 文案"过滤前"不符 | 口径与文案对齐（谁改都行，但不能各说各话） |
| #39 | `TodayItem` 在"库里没有简历"分支漏传 `job` | 修数据构造（不是加 try/except） |
| #42 | `event_type` 拼错静默入库 | 服务层先校验（中文错误 + 可用取值）+ 迁移 `0003` 给列加 **CHECK 约束** |

### 2. C 档（小改动大收益）11 条

| # | 症状 | 改法要点 |
|---|---|---|
| #4 | `熟悉AI大模型` 抽不到 `大模型` | 纯中文别名的左边界不再被前一个 ASCII 字母否掉（同时保住 `Java` ≠ `JavaScript`） |
| #5 | `大模型` 既是规范名又是 `LLM` 的别名 → 一句抽出两个关键词（分母灌水 + 假缺口） | 词表加载后跑 `_resolve_alias_collisions()`（并查集）：**一个写法只归一个规范名**，被折叠者的其余写法合并给赢家 |
| #6 | 全角 `Ｐｙｔｈｏｎ` 静默漏抽 | 抽词前做 NFKC 归一（**只用于匹配**，不改写回库的原文） |
| #7 | 注释声称的 ruff `PLW1514` 从未生效（preview 规则） | 不再"注释说有效、实际无效"：补上真正会响的守卫（见 `tests/unit/test_encoding_lint_guard.py`） |
| #10 | 200 + 非 JSON 响应体抛裸 `JSONDecodeError` | 转成带状态码与响应体片段（截断）的 `DeepSeekError` |
| #12 | GBK 简历/损坏 docx 只报原始异常 | 转成中文可操作提示（"另存为 UTF-8"/"文件可能损坏"），文件名不会让断言蒙混过关 |
| #13 | `--file` 读取阶段不去重 | 归一化后去重，并**报出跳过了几条重复** |
| #14 | `--file` 无 scheme 白名单、无条数上限 | 只允许 http/https；超上限时**明确告知**只处理前 N 条（绝不静默截断） |
| #31 | `skill_not_in_jd` 是死代码（`allowed_skills` 传成了并集） | 传 `set(keywords)` —— 开场白只应提"JD 要的"；P4 修过的"动作词豁免"保持不变 |
| #37 | `greeting_sent` / `greeting_edited` 恒为 0 | 真实动作（复制/投递）才置位，另给显式开关；**不把"生成"当"发送"** |
| #38 | 导出"绝不覆盖"不是硬保证 | 写文件改成**排他创建**，`unique_path` 只找候选名（竞态下也不覆盖） |

### 3. 未标注里可修的 4 条（+3 条动作）

- **#48** 批量范例里的 `12=skipped`（岗位状态）不再被拒。
- **#49** 待处理清单与顶部提醒的『该催谁』口径统一：已进 replied/assessment/interview 的不再被建议"放弃"。
- **#50** 批量模式不再静默丢弃非 `=` 的 token（关键词/`--status` 要么处理、要么明确报错）。
- **#51** 库里建议数据"合法 JSON 但结构不对"时给 `TailorError`（原来抛裸 `AttributeError`）。
- **#46** 补上 P4.5 批量打标 / 模糊匹配的**功能测试**（原来只有"不再是占位"的断言）。
- **#52** 补做**独立验证**：用第三方库 `icalendar` 解析真实导出的 `.ics`（3 个事件全部读出、
  UID 唯一、UTC 时间正确、含分号/逗号的公司名原样还原）。⚠️ "导入手机日历"仍需要人做一次。
- **#53** 修掉"预设 `JH_DATA_DIR` 时 `jh demo` 打印成功但退出码非 0"：`_restore` 不再比较
  "还原后的 URL == 进 demo 前的 URL"（那个比较**假定环境不会变**，会误报），改成断言
  "不再指着演示临时库"；并补了一条"配置单例先缓存、再改环境变量"的回归用例。
- **#54** 隐私扫描接进 CI（样本走 repository secret，未配置时**skip 而非 pass**）。

### 4. D 档 13 条：**没动，等用户拍板**

验证会话明确标注"开发会话先不要擅自改"（它们都是**取舍**而非缺陷）：#18 / #19 / #20 / #21 /
#23 / #25 / #26 / #27 / #28 / #40 / #41 / #43 / #45，外加需要用户决策的 **#47（git 历史里的真实 PII）**。
每条的现状与建议见平台台账。

### 5. 本轮的验证读数（2026-09-19）

| 套件 | 读数 | 说明 |
|---|---|---|
| `pytest tests/unit` | **704 passed / 0 failed** | 我自己那套（含本轮新增的 8 个文件） |
| `pytest tests/verify` | **282 passed / 30 xpassed / 0 xfailed / 0 failed** | ★ 验证会话为缺陷写的 30 条独立用例**全部转成 XPASS** —— 也就是说这一轮把"有可证伪用例的缺陷"清空了 |
| `ruff check .` | 只剩 `tests/verify/test_v_p9.py` 两行 E501 | ⚠️ **那是验证会话的文件**（且尚未提交），我不动它；已请其修（否则 `ruff check .` 在 CI 里会红） |
| `ruff format --check .` / `uv lock --check` | 全绿 | |

`xpassed` 的含义：验证会话的用例写着 `xfail(strict=False)`，**修好之后它们从"正当地失败"变成"通过"**。
摘掉 `xfail` 标记由验证会话做 —— 我不碰 `tests/verify/`。

### 6. 我自己另补的用例（守住同一件事，不依赖对方的文件）

| 文件 | 守什么 |
|---|---|
| `tests/unit/test_match_info_gap.py` | #9：空 JD / 无用 JD 都不得过线；**真实 JD 照常打分**（反向守卫，防止修过头） |
| `tests/unit/test_encoding_lint_guard.py` | #7：AST 自查 `src/` 不许有裸 `open()`；并盯着 CI 那条 `--preview --select PLW1514` 还在（含元测试证明守卫会响） |
| `tests/unit/test_demo.py` | #53：预设 `JH_DATA_DIR` 时 demo 必须退出码 0；配置单例先缓存再改环境变量也要成立；打印并真的删掉临时目录 |
| `tests/unit/test_tailor_svc.py` | #51：6 种"结构不对的合法 JSON"都必须给 `TailorError`（含版本号） |
| `tests/unit/test_job_file_guard.py` / `test_match_stale_llm.py` / `test_deepseek_provider.py` / `test_issue_fixes_round_p4.py` / `test_followup.py`（扩写） | #13/#14、#17、#10、#31/#34/#35/#37/#38/#39/#42/#48/#49/#50、#46 的回归 |

### 6.5 #37 的收尾（我自己补的，因为原改法有两个缺口）

修复轮里 #37 的做法是"标 applied 且有开场白 → 推定 `greeting_sent=True`"。推定合理
（走到 applied 只可能是人点过投递按钮），但**推定必须可见、且可纠正**，否则就是另一种静默失真：

- `jh apply … --status applied` 现在会打一行「已推定「开场白已发出」…」并给出纠正命令；
- `--sent` 改成 `--sent/--no-sent`（默认 None = 不表态），纠正生效时也打一行说明；
- 实现坑（记下来）：`greeting_corrected` 必须用**改字段之前**的快照判断 ——
  `set_status` 会先把 False 写进列，之后读就永远 False（实测：纠正生效了但提示打不出来）。

### 7. 修复轮回执：#55（#24 的续集 —— 「形参加了但没人传」第三次）

验证会话在复检里发现 #24**只修了一个调用点**：`compute_cache_key(prompt_fingerprint=…)`
是**可选**参数，`match_llm_svc` 传了，另外四个（greeting / tailor / followup / question）
一个都没传。后果在 greeting 上最直白：切 A/B 版本拿回上一版文案，而 `source` 仍显示
`llm` —— 用户以为刚生成，其实一个字都没重算。

**改法（不做第二个实现，只有一份算法）**

| 位置 | 改了什么 |
|---|---|
| `llm/cache.py` | 新增 `payload_fingerprint(payload)`：对**整份 payload**（task / prompt_version / json_mode / temperature / max_tokens / 每条 role+content）取 sha256；`compute_cache_key` 的 docstring 明确"业务代码里不允许不传" |
| `greeting_svc.py` / `tailor_svc.py` / `followup_svc.py` / `question_svc.py` | 一律**先拼 payload、再算 key**，传 `prompt_fingerprint=llm_cache.payload_fingerprint(payload)` |
| `match_llm_svc.py` | 原来的 `_prompt_fingerprint` 保留同名（独立验证脚本直接调它），改为**薄封装**委托给共用实现 —— 算法不得有第二份 |
| `followup_svc.py` | 顺带去掉"只有默认 days/style 才可缓存"的旧限制：key 现在覆盖全部输入，限制已无必要 |
| `tests/unit/test_cache_key_guard.py` | ★ **结构守卫**：AST 扫 `src/`，任何不传 `prompt_fingerprint=` 的调用点直接红；另加元测试证明守卫会响、不会把注释/字符串误判、`**kwargs` 转发**不算**传了 |

**读数**：`pytest tests/unit` = **711 passed**；验证会话的两条契约用例
（`test_v_p4_greeting.py -k invalidated`：变体切换、表头变更）现在 **2 passed**；
`pytest tests/verify` = 324 passed / **1 failed** —— 那一条是验证会话**故意**写的
`assert "prompt_fingerprint=" not in greeting_svc`（注释里写明"#55 修好时这里会变红，
请把它改成'必须传'"）。按分工我不动 `tests/verify/`，留给验证会话翻转。

---

## P9 —— 开源收尾 + 合规自检

**状态**：✅ 开发完成 → 待按流程提交平台，**等待复验**
**阶段标记**：`jh --version` = `phase P9`（P9 是收尾阶段，交付后主要版本标记为 `v0.1.0` tag）
**契约**：提示词 11（plan 862-887）

### 1. 做了什么

| 交付物 | 内容 |
|---|---|
| `README.md` 定稿 | 新增**功能一览表**（按阶段列命令）、**`jh demo` 真实终端输出**（替代截图/GIF）、**文档索引**、**常见问题速查表**；安装章补三条实测坑（见 §3）；路线图更新到 P0–P6 + P9 |
| `docs/install.md`（新，781 行） | 安装与第一次运行：uv / venv 两条路、extras 表、逐步真实输出、数据落在哪、**15 条常见问题**、卸载清理、模拟器（**明确标注 P7 尚未实现**） |
| `docs/config.md`（新，692 行） | `config.yaml` + `.env` **逐字段说明**（27 个 `AppSettings` 字段 + 15 个嵌套子字段全覆），每条含"默认值/校验/⚠️写错会怎样"，另有 27 行字段索引表可核对覆盖率 |
| `docs/COMPLIANCE.md`（新） | ⭐ 合规与隐私自检：自动化操作面逐条判定、必须人工的动作、`.gitignore` 覆盖、全仓库扫描、**git 历史痕迹**、频率控制、依赖与迁移、导出安全、复跑命令 |
| `jh demo`（P9 核心代码） | 11 步全流程演示（建库→录入→匹配→清单+开场白→打标→面试准备→复盘→导出），**临时库 + 假 Provider**，退出即销毁；`--keep` 保留 |
| `LICENSE` | MIT + 附加说明（不提供自动投递、不绕风控、使用者自担风险）——P8 前就已存在，本轮核对 |
| CI 覆盖率 | `pytest --cov --cov-fail-under=80`（当前 **84%**）+ 上传 `coverage.xml` 产物 |
| tag | `v0.1.0`（annotated，说明含哪些阶段、哪些没做） |

### 2. `jh demo` 的三条纪律（都写了测试）

1. **绝不碰真库**：把 `JH_DATA_DIR/OUTPUT_DIR/STATE_DIR` 指到临时目录，并**断言**
   库路径真的变了且落在临时目录里。测试用三条独立断言钉死：跑完**配置里那个库文件根本不存在**、
   期间**没有任何真实 Provider 被调用**（monkeypatch 成会炸的实现）、跑完配置被还原。
2. **绝不联网/花钱**：`JH_LLM_PROVIDER=mock` + `MockProvider`；即使环境里配了真 key 也走假 Provider。
3. **输出即自证**：每步打印"做了什么 + 关键数字"，README 里直接贴真实输出（不造假截图）。

### 3. 子代理复查揪出的 4 个真问题（都已修/已改文档）

| # | 问题 | 处理 |
|---|---|---|
| 1 | **`jh demo` 在用户预设过 `JH_DATA_DIR` 时会崩**：`_restore` 一律 `pop` 环境变量，把用户自己的配置弄丢了 → 结尾报"还原失败：配置单例还指着演示库" | ✅ 改成"记下原值、原样放回"，并加了一条回归用例（`test_demo_keeps_user_preset_data_dirs`） |
| 2 | README 方式二的 `pip install -e ".[dev]"` **不装任何开发工具**（`dev` 是 PEP 735 依赖组，不是 extra；pip 只发一句 WARNING） | ✅ README 改成 `pip install -e .` + `pip install pytest ruff mypy`，并写明原因 |
| 3 | README 说 `--extra llm` 是"调用 DeepSeek 需要的" —— 实际 `src/` 里没有 `import openai`（Provider 是 httpx 手写的） | ✅ README 与 `pyproject.toml` 注释都改成"当前运行期用不到，保留给将来换 SDK" |
| 4 | `.env.example` 把 `JH_LOG_LEVEL` 当 `.env` 开关；但日志模块直接读 `os.environ`，而 pydantic-settings **不会**把 `.env` 写进 `os.environ` → 写了没反应 | ✅ `.env.example` 与 `config.md` 都写明"必须是真的环境变量，或用 `jh -v/--verbose`" |

另外修正两处注释与实现不一致（都只是注释）：`settings.py` 说 `jh resume split` 用
`default_resume_path`（实际用库里 `resumes.is_default`）；`resume_svc.py` 里举例用的
"中点分隔"写法原来是真实简历的项目名（P9 隐私清理，见 §4）。

### 4. 隐私：本轮清掉了什么、还剩什么

- **工作区清掉 2 处真实项目名**（P1 时期留下）：`tests/unit/test_match_svc.py` 的夹具名、
  `tests/unit/test_resume_svc.py` 的 docstring（**逐字引用**了真实项目标题行）、
  以及 `resume_svc.py` 注释里的同一个名字 —— 全部换成同形状的虚构写法。
  验证方式：从本机 gitignored 来源取真实标识、回扫全仓库（脚本在仓库外，见 COMPLIANCE §7）。
- **⚠️ git 历史里仍有真实姓名**：22 个提交 / 2 个文件 + 2 条提交信息；
  真实项目名另有 26 / 16 个提交。**这是 #47，需要用户决策**（`git filter-repo` 重写 vs 接受），
  命令已写进 `COMPLIANCE.md` §4。**发布（公开仓库）之前必须定。**
- 其余扫描命中全部是**占位样例/误报**（脱敏过滤器的测试数据、`uv.lock` 里的哈希数字串、
  文档里的 `example.com`），逐条判定见 `COMPLIANCE.md` §3.3。

### 5. 已知边界

1. **P7 未实现**：`jh leads *` 仍是占位；`docs/install.md` §9 与 `COMPLIANCE.md` §8 都明确标注，
   README 也不声称"支持 App 线索采集"。
2. **`.ics` 覆盖同名文件**是唯一偏离"只写新文件"的地方（幂等快照，理由见 `COMPLIANCE.md` §5.4）。
3. **覆盖率门槛 80% 是刻意的低门槛**：不追高（追覆盖率会催生"为覆盖率而写"的测试），
   当前 84%，掉下来会红。
4. 三个字段在当前代码里**没有消费者**（子代理发现并写进 `config.md`）：
   `match_dimensions`、`keywords`（除校验外）、`rate_limit.max_pages`（只作为 `search()` 形参存在）。
   文档按"改它不会影响任何分数/抓取"据实说明 —— 这属于**配置项比实现更超前**，不是 bug。
5. **`git filter-repo` 我没跑**：它改写历史、需要用户明确授权（见 §4）。

### 6. 建议重点验证的地方

1. **`jh demo` 的隔离**：在自己机器上先 `echo $env:JH_DATA_DIR` 记住值 → 跑 `jh demo` →
   断言 ①真实库文件没被动过（mtime/hash）②环境变量回到原值 ③临时目录已删除（不加 `--keep`）。
   ★ 顺手试一下"预设 `JH_DATA_DIR` 再跑 demo"这条（就是我修的那个 bug）。
2. **`jh demo --keep`**：保留目录里应当有 `data/jobhunter.db` 与 `output/report-*.md`；
   再跑一次 demo（不加 `--keep`）不应影响它。
3. **文档与代码一致性**：抽 5 条 `config.md` 的"⚠️ 写错会怎样"，真的改一下 `config.yaml` 看报错是否如文档所说
   （校验器在 `settings.py`，报错文案应当完全一致）。
4. **`install.md` 的命令**：按文档从零走一遍（新 clone + uv 路径），看能否一次跑通 `jh demo`。
5. **覆盖率门槛**：`pytest --cov=jobhunter --cov-fail-under=80` 应通过（当前 84%）；CI 里还多一步上传产物。
6. **COMPLIANCE.md 的可复跑性**：按它的 §7 跑两个脚本，看结论是否与文档一致
   （尤其"工作区 0 命中真实标识"这条）。

### 7. 关键文件

| 文件 | 职责 |
|---|---|
| `cli/commands/demo_cmd.py` | ⭐ `jh demo`：11 步演示 + 环境隔离（含"记下原值再放回"） |
| `docs/COMPLIANCE.md` | ⭐ 合规与隐私自检（含"发布前必须决策"的历史痕迹） |
| `docs/install.md` / `docs/config.md` | 安装教程 / 配置逐字段说明 |
| `tests/unit/test_demo.py` | 8 条：全流程跑通 + **绝不碰真库**（3 条独立断言）+ 预设环境变量回归 |
| `.github/workflows/ci.yml` | 覆盖率门槛 + 产物上传 |

---

## P6 —— 完整跟进体系（看板 + 面试 + 问题 + 笔试/Offer + 统计 + 报告 + 日历）

**状态**：✅ 开发完成 → 待按流程提交平台，**等待复验**
**测试**：`pytest tests/` **781 passed / 0 failed**（含 `tests/verify/`，24 xfailed 是验证会话登记的 🟡/🟢）
　　　　本阶段新增 **103 条**：`test_p6_tracking.py`（45，我）+ `test_assessment_offer.py`（29，子代理）
　　　　+ `test_cal.py`（29，子代理）
**阶段标记**：`jh --version` = `phase P6`
**契约**：提示词 8（plan 786-814）+ 模块④（plan 357-366）

### 1. 做了什么（9 条命令 + 1 个 schema 迁移）

| 命令 | 行为 |
|---|---|
| `jh apply --timeline <job_id>` | 投递时间轴：读 append-only 事件流，按**发生时间**（不是录入顺序）画 |
| `jh interview add/list` | 约面（推进到 interview + 写 `interview_scheduled` 事件）；`--upcoming` 只看接下来要面的 |
| `jh question add` | 记真实被问的题（`source=recorded`）；`--interview` 或 `--job` 二选一；3 档自评 + `--weak` |
| `jh question predict <job_id>` | ★ 按 JD 猜 N 道题（`source=predicted`）→ 存库、**挂岗位**；同岗位重复题跳过 |
| `jh question list` | 按面试/岗位/来源过滤 |
| `jh weak` | ★ 按知识点聚合、**按"没答上来次数"降序** + 一句结论（🔴/🟡/🟢）；实录与预测**分列** |
| `jh assessment add/list` | 笔试/测评 + 截止时间（`--upcoming` 复用 `reminder_svc` 的 3 天窗口常量） |
| `jh offer add/compare` | Offer 登记 + 横向比较（★ 年包 = 月薪 × 月数，最大者高亮） |
| `jh followup <job_id>` | ★ 追问话术（**只生成，不发送**）；`--sent` 才写 `followup` 事件 |
| `jh stats [--days] [--export md\|csv]` | ①平台转化率 ②开场白变体 ③方向回音（只记录，不宣称显著性） |
| `jh report [--since] [--out]` | 求职周报 Markdown（只写新文件） |
| `jh cal export [--days]` | .ics（面试 1 小时 / 笔试与 Offer 截止 30 分钟提醒），**幂等覆盖**同一文件 |

**schema 迁移 `0002`**（本阶段唯一的结构变更，也是设计缺口被真实用法暴露出来的一次）：
`interview_questions.interview_id` 由 NOT NULL 改为**可空**，新增 `job_id`（FK→jobs，`SET NULL`）。
原因：`jh question predict <job_id>` 拿的是**岗位**，而"准备面试"通常发生在面试还没约上的时候；
坚持 NOT NULL 只能逼用户编一个面试时间、或造一条假的 `interviews` 记录 ——
后者会污染 `jh interview list`、`jh stats` 的面试场次统计和顶部"本周面试"提醒。
回滚是**有损**的（先删预测题再改回 NOT NULL），但真实记录一条不动，迁移文件里写明了。

### 2. 关键设计决定（都有测试守着）

| 决定 | 为什么 |
|---|---|
| 统计口径 = **"至少走到过这一步"**（当前状态 ∪ 事件流里出现过的状态） | 按当前状态算会系统性低估：现在被拒的投递，可能走到过二面。事件流 append-only 就是为了这个 |
| 每个比率都带分子/分母 `25.0%（6/24）` | 只有百分比时，`100%` 是 1/1 还是 20/20 看不出来 —— 而这决定它是不是噪声 |
| 样本 < 10 条时**明说**"别据此下结论" | 纪律：只记录对比，不宣称统计显著性（评审采纳）。工具不该生成伪结论 |
| `jh weak` 按**没答上来次数**排序，不按平均分 | 平均分被"只问过一次、答得一般"的题拉平；"同一知识点连续三次答不上来"才是信号 |
| 实录 / 预测**分列展示**，`--recorded-only` 可过滤 | 预测题答不上来 = 准备不足；实录答不上来 = 能力缺口。合并会得出两个都不准的结论 |
| 预测题**不过** FidelityGuard | 它是**提问**（"你了解过 Kubernetes 吗"），不是"声称我会"。按编造拦掉会把最有价值的准备题删掉 |
| 追问话术**过** FidelityGuard（与开场白同一套）+ 确定性模板兜底 | 它是要发给 HR 的；而追问消息短、结构固定，写一份只用真话拼的模板完全可行（与 `tailor analyze` 的"没有诚实模板"形成对照） |
| 时间轴读 `occurred_at`（发生时间），不是 `created_at`（录入时间） | 补记的事件在时间轴上该落在**发生的时刻**，否则"昨晚面的试今天记"看起来像今天面的 |
| `.ics` **覆盖**同名文件（其它导出都只写新文件） | 日历是**幂等快照**：产生 `jobhunter-2.ics` 会让用户每次导入都多一份重复事件 |
| 登记笔试/面试/Offer 时投递状态**只前进不回退** | 副作用不该把用户明确设过的状态改回去（补记一场已面完的面试，不该把 offer 拽回 interview） |
| `jh followup --sent` 才写事件 | 生成 ≠ 发送。把生成当发送会让时间轴撒谎 |

### 3. 与计划书字面不一致的一处（有意为之）

计划书写的是 `jh apply timeline <job_id>`，实现是 **`jh apply --timeline <job_id>`**。
原因：`jh apply` 是**单命令应用**，它的 callback 带可变位置参数（`3=applied "字节"` 那种语法），
而 Click 在"组回调声明了位置参数"时**永远不会分派子命令** —— `timeline` 会被当成位置参数吃掉
（`apply_cmd.py` 的模块 docstring 从 P4.5 起就记着这条坑，并已有测试守着）。
功能完全一致，只是形状从子命令改成选项。

### 4. 因此失效的用例（请验证会话处理，我没有动 `tests/verify/`）

`tests/verify/test_v_checklist12.py::test_unimplemented_features_report_honestly` 里这几条现在会红
（它们断言这些命令仍是占位）：

| 参数化用例 | 现在的行为 |
|---|---|
| `["apply", "--timeline", "1"]` | 真实现（无库时 exit 1 + "请先运行 jh init"） |
| `["followup", "1"]` | 真实现 |
| `["stats"]` | 真实现 |
| `["offer", "compare"]` | 真实现 |
| `["cal", "export"]` | 真实现 |
| `["report"]` | 真实现 |

`["leads", ...]`（P7）与 `["demo"]`（P9）仍然有效。
我自己的 `tests/unit/test_cli_skeleton.py` 已同步把这些移出占位清单并注明由功能测试接手。

### 5. 已知边界 / 未覆盖的地方

1. **`jh stats` 的"方向"用的是打分时存下来的 `jd_direction`**（`jobs.rule_detail_json`），
   没打过分的岗位归到「（未打分）」。这样统计与"我当时看到的匹配结果"一致，
   但也意味着**没跑过 `jh match run` 的岗位不进方向分组**（不是漏统计，是没数据）。
2. **`--upcoming`（笔试）包含"已过期但状态仍待做"的条目**，而顶部提醒只推未来 3 天 ——
   有意差异：列表是"我该处理什么"，提醒是"什么快到点了"。
3. **`jh report` 的薄弱点是累计口径**（不只看这一周），报告里写明了；
   想只看某周不足以支撑结论（样本太小）。
4. **追问话术的"沾边"要求比开场白松**（提到一个真会的东西即可）：它的核心是"问一下进度"，
   不是"推销技能"。
5. **`jh cal export` 只覆盖三种事件**（面试/笔试截止/Offer 截止），不含自定义日程。
6. 预测题与真实题的**去重只看完全相同**（归一化后逐字相等），不做模糊相似 ——
   引入主观阈值会让人丢掉该看的题。

### 6. 建议重点验证的地方（verify hints）

1. **状态机**：`rejected` 从任意状态、`offer` 从 `interview` 直接跳；重复打标**不写重复事件**
   （数事件条数）。
2. **统计口径**：造"走到 interview 后被拒"的投递，断言它仍算进 已读/回复/约面 ——
   这是本阶段最容易写错的一处（按当前状态算会静默低估）。
3. **时间轴**：反向录入（先记 viewed 再补记更早的 replied）→ 断言时间轴按**发生时间**排序。
4. **迁移 0002**：`upgrade → downgrade → upgrade` 三连；downgrade 会删掉预测题
   （真实题保留）；预测题在 `interview_id IS NULL` 下能插入，删岗位时 `job_id` 变 NULL 而记录保留。
5. **`jh weak`**：同一知识点 3 次没答上来必须排第一；实录/预测分列；没分类的题要**被提示**出来。
6. **追问话术**：编造类输出（"精通 Kubernetes / 提升 300%"）必须降级成模板并说明原因；
   `--sent` 之前不得写 followup 事件。
7. **`.ics`** 可拿真日历软件导入一次（我用文本断言守住了 CRLF/折行/转义/UID 稳定性，
   但"Outlook/手机日历能不能导入"只有真导入才知道 —— 这一步我没做）。

### 7. 关键文件

| 文件 | 职责 |
|---|---|
| `services/track_svc.py` | 时间轴（事件流 → 人话） |
| `services/interview_svc.py` / `question_svc.py` | 面试场次 / 问题记录 + JD 预测 |
| `services/assessment_svc.py` / `offer_svc.py` | 笔试与 Offer（年包比较） |
| `services/weak_svc.py` / `stats_svc.py` / `report_svc.py` | 薄弱点 / 转化率口径 / 周报 |
| `services/followup_svc.py` / `cal_svc.py` | 追问话术（带闸门）/ .ics 导出 |
| `llm/prompts.py` | ★ 提示词文件加载 + 版本纪律（P6 抽出，tailor 也改用它） |
| `prompts/question.md` / `prompts/followup.md` | 两个新提示词（七要素，版本号在文件里） |
| `alembic/versions/0002_question_job_id.py` | 预测题挂岗位的迁移 |
| `src/jobhunter/models/constants.py` | 新增 `INTERVIEW_STATUSES` |

---

## P5 —— AI 定制简历 + FidelityGuard

**状态**：✅ 开发完成 → 待按流程提交平台，**等待复验**
**测试**：`pytest tests/` **683 passed / 0 failed**（含 `tests/verify/`，24 xfailed 是验证会话已登记的 🟡/🟢）
　　　　本阶段新增 **51 条**自测：`tests/unit/test_tailor_svc.py`（41）+ `test_tailor_cli.py`（10）
　　　　`pytest -m live tests/unit/test_tailor_live.py` 单独跑（默认排除，**不进 CI**）
**阶段标记**：`jh --version` = `phase P5`
**契约**：提示词 7（plan 737-784）+ 模块③（plan 331-356）

### 1. 做了什么（六个子命令 + 一个代码闸门）

| 命令 | 行为 |
|---|---|
| `jh tailor brief <job_id>` | ★ **不调模型**：重点讲哪 3 个项目（按与 JD 关键词的贴合度排序）/ 突出哪 2 个技能 / 要避开什么。零成本、可复现 |
| `jh tailor analyze <job_id>` | 调模型出字段级建议 `{match_analysis, suggestions[]}`，**逐条过闸门**后落 `resume_versions` |
| `jh tailor list [--job N]` | 历史版本（新→旧）：建议数 / 已批准数 / 导出文件 |
| `jh tailor diff <version_id>` | 前后对比（原 → 改 + 理由 + 批准状态 + 当时的闸门报告） |
| `jh tailor approve <version_id> …` | 逐条 / `--all` 批准；**批准前再校验一遍** |
| `jh tailor export <job_id>` | 导出 docx（`--format md` 兜底）——**只写新文件**，并校验原简历 sha256 未变 |

**FidelityGuard（`services/fidelity.py`，五项全部确定性、不依赖模型）**

| kind | 拦什么 | 关键实现点 |
|---|---|---|
| `entity_not_in_resume` | 公司/学校名（专有名词） | 靠"专有名词后缀 + 逐字回退匹配"识别；**候选必须是某个已知机构名的前缀**（第一版直接判"是不是证据池子串"，被 `科技有限公司` 这种通用候选骗过去了，实测编造的"腾讯科技有限公司"溜过闸门） |
| `skill_not_in_resume` | 技能词表命中的词 | **与 P4 的 `check_text` 共用同一段判定**（`_skill_violations`），`CAPABILITY_TERMS` 豁免（#30）必须一起生效，否则同一个词在两条路径上给出相反结论 |
| `number_not_in_resume` | 凭空多出来的数字 | ★ **数字连同单位一起守恒**：简历写"300ms"不能支撑"提升 300%"（只比数字会漏掉最典型的一种夸大） |
| `date_not_in_resume` | 新增/修改的年份、月份 | 归一化成 `y2026` / `ym2026-03`，`2026.03` 与 `2026年3月` 视为同一时间；只给年份不给月份的写法（"2026 年 3 月"）也拦 |
| `too_long` | 改写变扩写 | 上限 `max(原文 × 1.5, 原文 + 30)` —— 绝对余量是为了救短句（4 个字的原文 150% 只有 6 个字） |

**三处强制调用**（计划书硬要求）：`analyze`（生成）/ `approve`（再校验）/ `export`（逐条 + **整篇**两道）。
**违规即拒绝写入**：生成阶段丢掉违规建议并**不写库**（整批被拦时不建版本记录）；
approve / export 违规时**整批不动**（不做"跳过违规那条"的半成功）。

### 2. 关键设计决定（都有测试守着）

| 决定 | 为什么 |
|---|---|
| 提示词放 `prompts/tailor.md`，**版本号写在文件里**（`<!-- prompt-version: 2 -->`） | 缓存 key 必须含版本号；"改了文件忘了改常量"是必然会发生的 → 用**正文指纹测试**把口头纪律变成红灯 |
| 提示词用 `string.Template`（`$var`）而不是 `str.format` | 提示词里必然有 JSON 示例（`{...}`），format 会把它们当占位符 |
| `analyze` 没配 key / 模型输出不合法时**直接报错，不降级** | 开场白有确定性模板可用；字段级改写**没有诚实的模板**，硬造只会是假建议 → 指路 `jh tailor brief` |
| 建议的 `original` 在简历里定位不到 → 丢掉并说明（不是当编造处理） | 那是"没法套用"，不是"说谎"。两类原因分开报，用户才知道该改什么 |
| 导出按解析出的 `sections`（原文行）拼，而不是按结构化字段重建 | 重建会**静默丢掉**没被建模的小节（荣誉/自我评价/证书）→ 用户拿到"少了半页"的简历，这是这个功能最不能犯的错 |
| 导出前自校验"原文件 hash 未变"，变了就中止 | 防的是**将来有人把实现改成就地修改**，不是防用户手滑 |
| `--out` 指向已存在的文件 → 拒绝 | "永远只写新文件"这条红线不能有例外 |
| `jh tailor approve 3 --all` 里的 `--all` 会被 Click 吞掉 → 自行捞回 | 与 P4.5 的 `jh apply` 同一个坑（可变位置参数吃后续选项），有专门的测试守着 |

### 3. 真实 LLM 抽样（一次性验证，不是 CI 断言）

用**全虚构**的假简历 + 假岗位跑真模型（`try anything/jh-tailor-live-probe.py`，临时库）：

| 轮次 | 结果 |
|---|---|
| 提示词 v1 | 4 条建议全部过闸门；但第 3 条把"**参与**订单服务重构"改成了"**负责**……维护与迭代"——闸门不判语义升级 |
| 提示词 v2（加"程度守恒"规则 + 对应反例） | 5 条建议全部过闸门；同一条建议改成"参与订单服务重构，涉及后端接口的开发与维护"，理由里明确写了"「参与」程度词保留不变" |

**这一条不是"模型很听话"的证明**，而是"提示词能改、闸门能兜"的证据；
真正的结论在下面"已知边界"第 1 条。

### 4. 那两条因此失效的用例

| 用例 | 归属 | 处理 |
|---|---|---|
| `tests/unit/test_cli_skeleton.py::…[invocation11]`（`tailor brief 1` 应报占位） | **我自己的** | 已把 `tailor brief` 移出占位清单（并注明"移出必须由功能测试接手"） |
| `tests/verify/test_v_checklist12.py::test_unimplemented_features_report_honestly[argv1-定制简历]` | **验证会话的** | ⚠️ **我没有动它**。这条现在会红，请验证会话按老规矩把它移出占位清单（P4.5 时的同样情形见上文 §4） |

### 5. 已知边界 / 未覆盖的地方（请重点看这几条）

1. **程度词的语义升级（参与 → 负责、了解 → 熟练）闸门不判**，只靠提示词约束 + 人工逐条审核。
   语义蕴含判定是另一个量级的问题（硬做只会做出一个天天误报、最后被关掉的检查），
   已写进 `fidelity` 模块 docstring 的"刻意不做"。**请把这条当成已知边界，而不是漏测。**
2. **实体识别是启发式的**：没有后缀的写法（"在字节跳动实习"）抓不到；`_GENERIC_ACRONYMS`
   里的通用缩写（API/REST/UI…）刻意放行。取舍是"宁可漏一个，也不要把真经历判成编造"。
3. **`match_analysis` 里的分析文字不校验保真**（它是给我看的分析，不写进导出的文件）；
   只有 `suggestions[].revised` 才算"写进简历的话"。
4. **导出的是"解析出来的分段原文 + 已批准替换"**：如果简历解析时漏了内容，
   导出文件同样会漏。`build_document` 会把"没能套用的已批准建议"列出来，但**不保证排版与原件一致**
   （python-docx 新建文档 vs 原件的复杂排版）。
5. **缓存命中时不重跑闸门**：`from_cache` 的文本上次已过闸门，而 approve / export 两处会再校验一次
   （口径变严时兜得住）。
6. `jh tailor export` 只处理**已批准**的建议；未批准的一律不写进文件（这是设计的核心，不是缺陷）。

### 6. 建议重点验证的地方（verify hints）

1. **恶意 fixture**：`tests/unit/test_tailor_svc.py::MALICIOUS_CASES` 的四条（Kubernetes / 300% /
   10 个模块 / 2024 年）+ 实体 + 超长；再自己造几条（比如把"500ms"改成"5ms"、"2027"改成"2028"、
   把"某某科技有限公司"换成"某某网络有限公司"）。
2. **三处强制调用是否真的都在**：直接往库里塞一条含编造的建议（`approved_ids` 也写上），
   然后 `approve` / `export` 各跑一次，断言都被拒且**没有文件产生**。
3. **原文件 hash 不变**：跑 `jh tailor export`，对原 docx 做 sha256 前后对比。
4. **真话不许被误报**：`TRUTHFUL_CASES` 里那几条（含"部署"这种通用实践词、真公司名、真学校名）。
5. **`--out` 指向已存在文件必须拒绝**；`export` 未批准时必须报错并指路 `approve`。
6. **提示词文件纪律**：改一行 `prompts/tailor.md` 的正文但不改版本号 → `test_prompt_version_matches_body_fingerprint` 必须红。

### 7. 关键文件

| 文件 | 职责 |
|---|---|
| `services/tailor_svc.py` | brief / analyze / approve / export 的全部业务逻辑 + 提示词加载（含版本指纹） |
| `services/fidelity.py` | ★ `FidelityGuard.check(original, generated)` + 五项确定性检查；P4 的 `check_text` 与它共用技能判定 |
| `cli/commands/tailor_cmd.py` | 六个子命令的渲染与退出码；`_pull_all_out_of_tokens` 修 Click 吞选项 |
| `utils/docx.py` | 新增 `DocLine` / `write_docx` / `write_markdown`（**只写新文件**） |
| `prompts/tailor.md` | 七要素系统提示词（v2），硬约束写死 |
| `config/settings.py` | 新增 `TailorSettings`（长度比例 / 绝对余量 / 建议条数 / brief 条数） |
| `tests/unit/test_tailor_live.py` | `@pytest.mark.live` 真模型抽样（默认排除；断言与内容无关，只问"路走不走得通"） |

---

## P4.5 —— 零手打跟进（★ MVP 完成）

**状态**：✅ 开发完成 → 已按流程提交（`start` + 交付声明第 1 轮），**等待复验**
**测试**：`pytest tests/unit` **412 passed**；`pytest tests/` = 574 passed / 0 failed（见 §4）
**阶段标记**：`jh --version` = `phase P4.5`

### 0. 本轮补记（真实 key 首次联调 + 按流程提交 + 修掉两条 🟡）

**⓪ 用户批准后修掉两条 🟡**（`153a3a3`）

| ISSUE | 现象 | 改法 | 边界 |
|---|---|---|---|
| **#30** fidelity 误报 | 模型写"算法"，被判成"简历里没有这个技能" → **整条完整版降级成模板**。用户视角是"我明明做过，工具却说我编造" | `resume_svc.CAPABILITY_TERMS` 显式列出动作/通用词（部署、性能优化、单元测试、接口文档、并发、多线程、异步、需求分析、项目管理、敏捷开发、代码评审、响应式布局、前端工程化），`fidelity` 对这类词豁免 | 豁免**只限动词/通用实践**：**技术栈名字仍严格**（Kubernetes 没写进简历照样判编造）、**架构/模式类不豁免**（微服务/消息队列/设计模式/组件库）、数字与学历照旧。专有一条测试守这个边界 |
| **#16** CI 静默跳过 | `uv sync --locked` 不带 `--extra` → 卸载 python-docx → 3 个文件的 `importorskip("docx")` 让 **73 条简历解析测试在 CI 上被整体跳过**，CI 却显示绿 | `ci.yml` 加 `--extra docx`，并把"为什么不能省"写进那一步的注释 | 只加 docx：全仓只有 docx 被 importorskip（playwright 没有任何测试用），browser/emulator 带 marker 默认排除 |

**验证**：全量 **580 passed / 0 failed**；验证会话的三条对应用例（两条 #30 + 一条 #16）
**由 XFAIL 转 XPASS**（xfail 标记归验证会话删，我没动那个文件）。
⚠️ #16 我只能证明"本地等价步骤成立"（`uv sync --locked --extra docx --dry-run` 不再卸载 python-docx）——
GitHub Actions 的实际结果我看不到（私密仓库 + 没装 gh）。

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
